from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import shapefile
from pyproj import CRS, Transformer

from preprocess_blocks import (
    EPSILON,
    PolygonBlock,
    bbox_distance_m,
    bbox_intersects,
    boundary_distance,
    close_ring,
    euclidean_distance,
    grid_key,
    grid_range,
    load_crs,
    load_json,
    make_segments,
    normalize_text,
    point_in_polygon,
    polygon_area_centroid,
    segments_intersect,
    shared_boundary_length,
    split_shape_rings,
    transform_ring_to_lonlat,
    write_json,
)


TRANSPORT_ROAD_CODES = {
    "UQS100",  # road
    "UQS103",  # pedestrian road
    "UQS111", "UQS112", "UQS113",  # wide road
    "UQS114", "UQS115", "UQS116",  # arterial road
    "UQS117", "UQS118", "UQS119",  # medium road
    "UQS120", "UQS121", "UQS122",  # local road
    "UQS142",  # special pedestrian road
}
TRANSPORT_EXCLUDE_CODES = {
    "UQS200",  # parking lot
    "UQS210",  # off-street parking
    "UQS520",  # urban rail
}
ROAD_BARRIER_KEYWORDS = ("광로", "대로", "중로", "소로", "도로")
ROAD_EXCLUDE_KEYWORDS = ("기타 도로시설", "주차", "철도")

TRANSPORT_PREFILTER_MARGIN_METERS = 420.0
ROAD_INDEX_CELL_METERS = 80.0
PARCEL_INDEX_CELL_METERS = 90.0
PARCEL_CONNECT_BOUNDARY_METERS = 3.0
PARCEL_MIN_SHARED_BOUNDARY_METERS = 1.0
PARCEL_NEIGHBOR_GAP_METERS = 18.0
PARCEL_CANDIDATE_CENTROID_METERS = 90.0
PARCEL_CANDIDATE_BBOX_METERS = 32.0
MAX_ASSIGNMENT_NEIGHBORS = 10


@dataclass
class RoadBarrier:
    source_index: int
    source: str
    code: str
    name: str
    bbox_xy: tuple[float, float, float, float]
    rings_xy: list[list[tuple[float, float]]]
    segments: list[tuple[tuple[float, float], tuple[float, float]]]
    area: float


@dataclass(frozen=True)
class LegalDong:
    code: str
    name: str
    sgg_code: str


class UnionFind:
    def __init__(self, values: list[str]) -> None:
        self.parent = {value: value for value in values}
        self.rank = {value: 0 for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> bool:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return False
        if self.rank[left_root] < self.rank[right_root]:
            self.parent[left_root] = right_root
        elif self.rank[left_root] > self.rank[right_root]:
            self.parent[right_root] = left_root
        else:
            self.parent[right_root] = left_root
            self.rank[left_root] += 1
        return True


def safe_number(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def load_legal_dongs(dong_shp: Path | None) -> dict[str, LegalDong]:
    if not dong_shp or not dong_shp.exists():
        return {}
    reader = shapefile.Reader(str(dong_shp), encoding="cp949", encodingErrors="replace")
    fields = [field[0] for field in reader.fields[1:]]
    dongs: dict[str, LegalDong] = {}
    for record in reader.iterRecords():
        attrs = {field: record[pos] for pos, field in enumerate(fields)}
        code = normalize_text(attrs.get("EMD_CD"))
        if not code:
            continue
        dongs[code] = LegalDong(
            code=code,
            name=normalize_text(attrs.get("EMD_NM")),
            sgg_code=normalize_text(attrs.get("COL_ADM_SE")),
        )
    print(f"legal dongs ready: {len(dongs):,}", flush=True)
    return dongs


def legal_dong_code_from_block(block: dict[str, Any] | None) -> str:
    if not block:
        return ""
    value = normalize_text(block.get("pnu") or block.get("source_key") or "")
    return value[:8] if len(value) >= 8 and value[:8].isdigit() else ""


def legal_dong_info(
    parcel_id: str,
    parcel_by_id: dict[str, dict[str, Any]],
    records_by_parcel: dict[str, list[dict[str, Any]]],
    legal_dongs: dict[str, LegalDong],
) -> dict[str, str]:
    code = legal_dong_code_from_block(parcel_by_id.get(parcel_id))
    legal_dong = legal_dongs.get(code)
    name = legal_dong.name if legal_dong else most_common([record.get("dong") for record in records_by_parcel.get(parcel_id, [])])
    sgg_code = legal_dong.sgg_code if legal_dong else code[:5]
    return {
        "legal_dong_code": code,
        "legal_dong_name": name,
        "sgg_code": sgg_code,
    }


def total_orders(records: list[dict[str, Any]]) -> int:
    return int(sum(max(0.0, safe_number(record.get("orders"))) for record in records))


def bbox_from_points(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def expand_bbox(bbox: tuple[float, float, float, float], margin: float) -> tuple[float, float, float, float]:
    return (bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin)


def transformed_records_bbox(data: dict[str, Any], transformer: Transformer) -> tuple[float, float, float, float]:
    points: list[tuple[float, float]] = []
    for scope in (data.get("scopes") or {}).values():
        for record in scope.get("records") or []:
            lat = safe_number(record.get("lat"), math.nan)
            lon = safe_number(record.get("lon"), math.nan)
            if not (math.isfinite(lat) and math.isfinite(lon)):
                continue
            x, y = transformer.transform(lon, lat)
            if math.isfinite(x) and math.isfinite(y):
                points.append((float(x), float(y)))
    if not points:
        raise ValueError("No valid record coordinates found.")
    return bbox_from_points(points)


def is_transport_road_barrier(attrs: dict[str, Any]) -> bool:
    code = normalize_text(attrs.get("A5"))
    name = normalize_text(attrs.get("A6"))
    if code in TRANSPORT_EXCLUDE_CODES:
        return False
    if code in TRANSPORT_ROAD_CODES:
        return True
    if "주차" in name or "철도" in name:
        return False
    return "도로" in name


def load_transport_barriers(transport_shp: Path, work_bbox: tuple[float, float, float, float]) -> list[RoadBarrier]:
    reader = shapefile.Reader(str(transport_shp), encoding="cp949", encodingErrors="replace")
    fields = [field[0] for field in reader.fields[1:]]
    barriers: list[RoadBarrier] = []
    total = len(reader)
    expanded = expand_bbox(work_bbox, TRANSPORT_PREFILTER_MARGIN_METERS)

    for index, shape in enumerate(reader.iterShapes()):
        if index and index % 50000 == 0:
            print(f"transport scan: {index:,}/{total:,}, barriers={len(barriers):,}", flush=True)
        if shape.shapeType not in {shapefile.POLYGON, shapefile.POLYGONM, shapefile.POLYGONZ}:
            continue
        bbox = tuple(float(value) for value in shape.bbox)
        if not bbox_intersects(bbox, expanded):
            continue
        record = reader.record(index)
        attrs = {field: record[pos] for pos, field in enumerate(fields)}
        if not is_transport_road_barrier(attrs):
            continue
        rings_xy = split_shape_rings(shape)
        if not rings_xy:
            continue
        area, _ = polygon_area_centroid(rings_xy, bbox)
        if area <= EPSILON:
            continue
        barriers.append(
            RoadBarrier(
                source_index=index,
                source="transport",
                code=normalize_text(attrs.get("A5")),
                name=normalize_text(attrs.get("A6")),
                bbox_xy=bbox,
                rings_xy=rings_xy,
                segments=make_segments(rings_xy),
                area=float(area),
            )
        )
    print(f"transport barriers ready: {len(barriers):,}", flush=True)
    return barriers


def is_road_barrier(attrs: dict[str, Any]) -> bool:
    name = normalize_text(attrs.get("DGM_NM"))
    grade = normalize_text(attrs.get("GRAD_SE"))
    code = normalize_text(attrs.get("ATRB_SE"))
    text = f"{name} {grade} {code}"
    if any(keyword in text for keyword in ROAD_EXCLUDE_KEYWORDS):
        return False
    if code in TRANSPORT_ROAD_CODES:
        return True
    return any(keyword in text for keyword in ROAD_BARRIER_KEYWORDS)


def transform_rings(
    rings: list[list[tuple[float, float]]],
    transformer: Transformer,
) -> list[list[tuple[float, float]]]:
    transformed: list[list[tuple[float, float]]] = []
    for ring in rings:
        next_ring: list[tuple[float, float]] = []
        for x, y in ring:
            tx, ty = transformer.transform(x, y)
            next_ring.append((float(tx), float(ty)))
        if next_ring:
            transformed.append(close_ring(next_ring))
    return transformed


def load_road_barriers(
    road_shp: Path,
    work_bbox_in_road_crs: tuple[float, float, float, float],
    road_to_target: Transformer,
) -> list[RoadBarrier]:
    if not road_shp.exists():
        return []
    reader = shapefile.Reader(str(road_shp), encoding="cp949", encodingErrors="replace")
    fields = [field[0] for field in reader.fields[1:]]
    barriers: list[RoadBarrier] = []
    total = len(reader)
    expanded = expand_bbox(work_bbox_in_road_crs, TRANSPORT_PREFILTER_MARGIN_METERS)

    for index, shape in enumerate(reader.iterShapes()):
        if index and index % 50000 == 0:
            print(f"road scan: {index:,}/{total:,}, barriers={len(barriers):,}", flush=True)
        if shape.shapeType not in {shapefile.POLYGON, shapefile.POLYGONM, shapefile.POLYGONZ}:
            continue
        source_bbox = tuple(float(value) for value in shape.bbox)
        if not bbox_intersects(source_bbox, expanded):
            continue
        record = reader.record(index)
        attrs = {field: record[pos] for pos, field in enumerate(fields)}
        if not is_road_barrier(attrs):
            continue
        source_rings = split_shape_rings(shape)
        if not source_rings:
            continue
        rings_xy = transform_rings(source_rings, road_to_target)
        all_points = [point for ring in rings_xy for point in ring]
        if not all_points:
            continue
        bbox = bbox_from_points(all_points)
        area, _ = polygon_area_centroid(rings_xy, bbox)
        if area <= EPSILON:
            continue
        barriers.append(
            RoadBarrier(
                source_index=index,
                source="road",
                code=normalize_text(attrs.get("ATRB_SE") or attrs.get("ROAD_TY")),
                name=normalize_text(attrs.get("DGM_NM") or attrs.get("GRAD_SE")),
                bbox_xy=bbox,
                rings_xy=rings_xy,
                segments=make_segments(rings_xy),
                area=float(area),
            )
        )
    print(f"road barriers ready: {len(barriers):,}", flush=True)
    return barriers


def build_barrier_index(barriers: list[RoadBarrier]) -> dict[tuple[int, int], list[int]]:
    index: dict[tuple[int, int], list[int]] = defaultdict(list)
    for barrier_index, barrier in enumerate(barriers):
        min_x, min_y, max_x, max_y = barrier.bbox_xy
        for gx in grid_range(min_x, max_x, ROAD_INDEX_CELL_METERS):
            for gy in grid_range(min_y, max_y, ROAD_INDEX_CELL_METERS):
                index[(gx, gy)].append(barrier_index)
    return index


def segment_bbox(left: tuple[float, float], right: tuple[float, float], margin: float = 0.0) -> tuple[float, float, float, float]:
    return (
        min(left[0], right[0]) - margin,
        min(left[1], right[1]) - margin,
        max(left[0], right[0]) + margin,
        max(left[1], right[1]) + margin,
    )


def barrier_candidates_for_segment(
    left: tuple[float, float],
    right: tuple[float, float],
    barrier_index: dict[tuple[int, int], list[int]],
) -> set[int]:
    bbox = segment_bbox(left, right, 3.0)
    ids: set[int] = set()
    for gx in grid_range(bbox[0], bbox[2], ROAD_INDEX_CELL_METERS):
        for gy in grid_range(bbox[1], bbox[3], ROAD_INDEX_CELL_METERS):
            ids.update(barrier_index.get((gx, gy), []))
    return ids


def segment_crosses_barrier(left: tuple[float, float], right: tuple[float, float], barrier: RoadBarrier) -> bool:
    if not bbox_intersects(segment_bbox(left, right, 0.0), barrier.bbox_xy):
        return False
    if point_in_polygon(left[0], left[1], barrier) or point_in_polygon(right[0], right[1], barrier):
        return True
    for barrier_segment in barrier.segments:
        if segments_intersect(left, right, barrier_segment[0], barrier_segment[1]):
            return True
    return False


def has_road_barrier_between(
    left: PolygonBlock,
    right: PolygonBlock,
    barriers: list[RoadBarrier],
    barrier_index: dict[tuple[int, int], list[int]],
) -> tuple[bool, RoadBarrier | None]:
    for barrier_id in barrier_candidates_for_segment(left.centroid_xy, right.centroid_xy, barrier_index):
        barrier = barriers[barrier_id]
        if segment_crosses_barrier(left.centroid_xy, right.centroid_xy, barrier):
            return True, barrier
    return False, None


def transform_processed_block_to_parcel(
    block: dict[str, Any],
    to_source: Transformer,
    to_wgs84: Transformer,
) -> PolygonBlock:
    rings_lonlat = block.get("geometry", {}).get("coordinates") or []
    rings_xy: list[list[tuple[float, float]]] = []
    for ring in rings_lonlat:
        xy_ring: list[tuple[float, float]] = []
        for lon, lat in ring:
            x, y = to_source.transform(float(lon), float(lat))
            xy_ring.append((float(x), float(y)))
        if xy_ring:
            rings_xy.append(close_ring(xy_ring))
    if not rings_xy:
        raise ValueError(f"Block {block.get('block_id')} has no geometry.")
    all_points = [point for ring in rings_xy for point in ring]
    bbox = bbox_from_points(all_points)
    area, centroid_xy = polygon_area_centroid(rings_xy, bbox)
    centroid_lon, centroid_lat = to_wgs84.transform(centroid_xy[0], centroid_xy[1])
    rings_lonlat_out = [transform_ring_to_lonlat(ring, to_wgs84) for ring in rings_xy]
    lon_values = [coord[0] for ring in rings_lonlat_out for coord in ring]
    lat_values = [coord[1] for ring in rings_lonlat_out for coord in ring]
    return PolygonBlock(
        source_index=0,
        source_key=normalize_text(block.get("source_key") or block.get("pnu") or block.get("block_id")),
        attrs={},
        rings_xy=rings_xy,
        rings_lonlat=rings_lonlat_out,
        bbox_xy=bbox,
        bbox_lonlat=[round(min(lon_values), 7), round(min(lat_values), 7), round(max(lon_values), 7), round(max(lat_values), 7)],
        centroid_xy=centroid_xy,
        centroid_lonlat=(float(centroid_lon), float(centroid_lat)),
        area=float(area),
        segments=make_segments(rings_xy),
    )


def build_parcel_index(parcels: dict[str, PolygonBlock]) -> dict[tuple[int, int], list[str]]:
    index: dict[tuple[int, int], list[str]] = defaultdict(list)
    for parcel_id, parcel in parcels.items():
        min_x, min_y, max_x, max_y = expand_bbox(parcel.bbox_xy, PARCEL_CANDIDATE_BBOX_METERS)
        for gx in grid_range(min_x, max_x, PARCEL_INDEX_CELL_METERS):
            for gy in grid_range(min_y, max_y, PARCEL_INDEX_CELL_METERS):
                index[(gx, gy)].append(parcel_id)
    return index


def parcel_candidate_pairs(parcels: dict[str, PolygonBlock]) -> set[tuple[str, str]]:
    index = build_parcel_index(parcels)
    pairs: set[tuple[str, str]] = set()
    for parcel_id, parcel in parcels.items():
        min_x, min_y, max_x, max_y = expand_bbox(parcel.bbox_xy, PARCEL_CANDIDATE_BBOX_METERS)
        candidate_ids: set[str] = set()
        for gx in grid_range(min_x, max_x, PARCEL_INDEX_CELL_METERS):
            for gy in grid_range(min_y, max_y, PARCEL_INDEX_CELL_METERS):
                candidate_ids.update(index.get((gx, gy), []))
        cx, cy = grid_key(parcel.centroid_xy[0], parcel.centroid_xy[1], PARCEL_INDEX_CELL_METERS)
        for gx in range(cx - 1, cx + 2):
            for gy in range(cy - 1, cy + 2):
                candidate_ids.update(index.get((gx, gy), []))
        for other_id in candidate_ids:
            if other_id == parcel_id:
                continue
            pairs.add(tuple(sorted((parcel_id, other_id))))
    return pairs


def relation_for_assignment_neighbors(distance: float, barrier: RoadBarrier | None, admin_boundary: bool = False) -> str:
    if admin_boundary:
        return "across_admin_boundary"
    if barrier:
        return "across_transport_barrier"
    if distance <= PARCEL_CONNECT_BOUNDARY_METERS:
        return "touches"
    return "near"


def build_assignment_components(
    parcel_ids: list[str],
    parcels: dict[str, PolygonBlock],
    barriers: list[RoadBarrier],
    barrier_index: dict[tuple[int, int], list[int]],
    parcel_group_keys: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[tuple[str, str], dict[str, Any]], dict[str, int]]:
    union = UnionFind(parcel_ids)
    candidate_pairs = parcel_candidate_pairs(parcels)
    close_edges: dict[tuple[str, str], dict[str, Any]] = {}
    stats = {
        "candidate_pairs": len(candidate_pairs),
        "union_edges": 0,
        "barrier_blocked_edges": 0,
        "admin_boundary_edges": 0,
        "near_unconnected_edges": 0,
    }

    for pair_index, (left_id, right_id) in enumerate(sorted(candidate_pairs), start=1):
        if pair_index % 50000 == 0:
            print(f"parcel pairs: {pair_index:,}/{len(candidate_pairs):,}", flush=True)
        left = parcels[left_id]
        right = parcels[right_id]
        centroid_distance = euclidean_distance(left.centroid_xy, right.centroid_xy)
        quick_bbox_distance = bbox_distance_m(left.bbox_xy, right.bbox_xy)
        if centroid_distance > PARCEL_CANDIDATE_CENTROID_METERS and quick_bbox_distance > PARCEL_CANDIDATE_BBOX_METERS:
            continue
        boundary = boundary_distance(left, right)
        if boundary > PARCEL_NEIGHBOR_GAP_METERS and quick_bbox_distance > PARCEL_CANDIDATE_BBOX_METERS:
            continue
        shared_length = shared_boundary_length(left, right) if boundary <= PARCEL_CONNECT_BOUNDARY_METERS else 0.0
        blocked, barrier = has_road_barrier_between(left, right, barriers, barrier_index)
        edge = {
            "left_parcel_id": left_id,
            "right_parcel_id": right_id,
            "boundary_distance_meters": round(boundary if math.isfinite(boundary) else quick_bbox_distance, 1),
            "centroid_distance_meters": round(centroid_distance, 1),
            "shared_boundary_length": round(shared_length, 1),
            "transport_barrier": bool(blocked),
            "transport_code": barrier.code if barrier else "",
            "transport_name": barrier.name if barrier else "",
            "barrier_source": barrier.source if barrier else "",
            "admin_boundary": False,
        }
        close_edges[(left_id, right_id)] = edge
        left_group = parcel_group_keys.get(left_id, "") if parcel_group_keys else ""
        right_group = parcel_group_keys.get(right_id, "") if parcel_group_keys else ""
        if left_group and right_group and left_group != right_group:
            edge["admin_boundary"] = True
            stats["admin_boundary_edges"] += 1
            continue
        if blocked:
            stats["barrier_blocked_edges"] += 1
            continue
        if shared_length >= PARCEL_MIN_SHARED_BOUNDARY_METERS:
            if union.union(left_id, right_id):
                stats["union_edges"] += 1
        else:
            stats["near_unconnected_edges"] += 1

    parcel_to_root = {parcel_id: union.find(parcel_id) for parcel_id in parcel_ids}
    return parcel_to_root, close_edges, stats


def most_common(values: list[Any]) -> str:
    counter = Counter(normalize_text(value) for value in values if normalize_text(value))
    return counter.most_common(1)[0][0] if counter else ""


def summarize_bbox_lonlat(parcels: list[PolygonBlock]) -> list[float]:
    min_lon = min(parcel.bbox_lonlat[0] for parcel in parcels)
    min_lat = min(parcel.bbox_lonlat[1] for parcel in parcels)
    max_lon = max(parcel.bbox_lonlat[2] for parcel in parcels)
    max_lat = max(parcel.bbox_lonlat[3] for parcel in parcels)
    return [round(min_lon, 7), round(min_lat, 7), round(max_lon, 7), round(max_lat, 7)]


def component_centroid(parcels: list[PolygonBlock], parcel_orders: dict[str, int], parcel_ids: list[str]) -> dict[str, float]:
    total = sum(max(1, parcel_orders.get(parcel_id, 0)) for parcel_id in parcel_ids) or 1
    x = sum(parcels[index].centroid_xy[0] * max(1, parcel_orders.get(parcel_ids[index], 0)) for index in range(len(parcel_ids))) / total
    y = sum(parcels[index].centroid_xy[1] * max(1, parcel_orders.get(parcel_ids[index], 0)) for index in range(len(parcel_ids))) / total
    lon = sum(parcels[index].centroid_lonlat[0] * max(1, parcel_orders.get(parcel_ids[index], 0)) for index in range(len(parcel_ids))) / total
    lat = sum(parcels[index].centroid_lonlat[1] * max(1, parcel_orders.get(parcel_ids[index], 0)) for index in range(len(parcel_ids))) / total
    return {"x": x, "y": y, "lon": round(lon, 7), "lat": round(lat, 7)}


def build_scope_assignment_blocks(
    scope: dict[str, Any],
    to_source: Transformer,
    to_wgs84: Transformer,
    barriers: list[RoadBarrier],
    barrier_index: dict[tuple[int, int], list[int]],
    legal_dongs: dict[str, LegalDong],
) -> tuple[dict[str, Any], dict[str, Any]]:
    parcel_blocks = scope.get("blocks") or []
    parcel_by_id: dict[str, dict[str, Any]] = {block["block_id"]: block for block in parcel_blocks if block.get("block_id")}
    parcels: dict[str, PolygonBlock] = {}
    for parcel_id, block in parcel_by_id.items():
        parcels[parcel_id] = transform_processed_block_to_parcel(block, to_source, to_wgs84)

    records_by_parcel: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unmatched_records: list[dict[str, Any]] = []
    for record in scope.get("records") or []:
        parcel_id = record.get("block_id")
        if parcel_id and parcel_id in parcels:
            records_by_parcel[parcel_id].append(record)
        else:
            unmatched_records.append(record)

    parcel_admin = {
        parcel_id: legal_dong_info(parcel_id, parcel_by_id, records_by_parcel, legal_dongs)
        for parcel_id in records_by_parcel
    }
    parcel_group_keys = {
        parcel_id: info.get("legal_dong_code") or info.get("legal_dong_name") or parcel_id
        for parcel_id, info in parcel_admin.items()
    }

    parcel_ids = sorted(records_by_parcel.keys(), key=lambda value: (parcels[value].centroid_xy[0], -parcels[value].centroid_xy[1], value))
    parcel_to_root, close_edges, stats = build_assignment_components(parcel_ids, parcels, barriers, barrier_index, parcel_group_keys)
    groups: dict[str, list[str]] = defaultdict(list)
    for parcel_id, root in parcel_to_root.items():
        groups[root].append(parcel_id)

    sorted_groups = sorted(
        groups.values(),
        key=lambda ids: (
            parcel_group_keys.get(ids[0], ""),
            min(parcels[parcel_id].centroid_xy[0] for parcel_id in ids),
            -max(parcels[parcel_id].centroid_xy[1] for parcel_id in ids),
            ids[0],
        ),
    )
    assignment_id_by_root: dict[str, str] = {}
    assignment_id_by_parcel: dict[str, str] = {}
    sequence_by_group: dict[str, int] = defaultdict(int)
    for index, ids in enumerate(sorted_groups, start=1):
        group_key = parcel_group_keys.get(ids[0], "")
        if group_key and group_key[:8].isdigit():
            sequence_by_group[group_key] += 1
            assignment_id = f"{group_key}-AB{sequence_by_group[group_key]:04d}"
        else:
            assignment_id = f"AB{index:05d}"
        root = parcel_to_root[ids[0]]
        assignment_id_by_root[root] = assignment_id
        for parcel_id in ids:
            assignment_id_by_parcel[parcel_id] = assignment_id

    assignment_neighbor_edges: dict[tuple[str, str], dict[str, Any]] = {}
    for (left_id, right_id), edge in close_edges.items():
        left_assignment = assignment_id_by_parcel.get(left_id)
        right_assignment = assignment_id_by_parcel.get(right_id)
        if not left_assignment or not right_assignment or left_assignment == right_assignment:
            continue
        key = tuple(sorted((left_assignment, right_assignment)))
        current_distance = float(edge["centroid_distance_meters"])
        previous = assignment_neighbor_edges.get(key)
        if previous is None or current_distance < float(previous["centroid_distance_meters"]):
            assignment_neighbor_edges[key] = {
                "left": key[0],
                "right": key[1],
                "relation": relation_for_assignment_neighbors(
                    float(edge["boundary_distance_meters"]),
                    None if not edge["transport_barrier"] else RoadBarrier(-1, edge.get("barrier_source", ""), edge["transport_code"], edge["transport_name"], (0, 0, 0, 0), [], [], 0),
                    bool(edge.get("admin_boundary")),
                ),
                "centroid_distance_meters": edge["centroid_distance_meters"],
                "boundary_distance_meters": edge["boundary_distance_meters"],
                "shared_boundary_length": edge.get("shared_boundary_length", 0.0),
                "transport_barrier": edge["transport_barrier"],
                "transport_code": edge["transport_code"],
                "transport_name": edge["transport_name"],
                "barrier_source": edge.get("barrier_source", ""),
                "admin_boundary": bool(edge.get("admin_boundary")),
            }

    assignment_records: list[dict[str, Any]] = []
    for record in scope.get("records") or []:
        next_record = dict(record)
        parcel_id = normalize_text(record.get("block_id"))
        admin_info = parcel_admin.get(parcel_id, {})
        next_record["parcel_block_id"] = parcel_id
        next_record["parcel_id"] = parcel_by_id.get(parcel_id, {}).get("pnu") or parcel_by_id.get(parcel_id, {}).get("source_key") or parcel_id
        next_record["assignment_block_id"] = assignment_id_by_parcel.get(parcel_id, "")
        next_record["block_id"] = next_record["assignment_block_id"]
        next_record["legal_dong_code"] = admin_info.get("legal_dong_code", "")
        next_record["legal_dong_name"] = admin_info.get("legal_dong_name", "")
        next_record["sgg_code"] = admin_info.get("sgg_code", "")
        assignment_records.append(next_record)

    blocks: list[dict[str, Any]] = []
    for assignment_id, parcel_ids_for_block in sorted(
        ((assignment_id_by_parcel[ids[0]], ids) for ids in sorted_groups),
        key=lambda item: item[0],
    ):
        block_records = [record for parcel_id in parcel_ids_for_block for record in records_by_parcel.get(parcel_id, [])]
        block_parcels = [parcels[parcel_id] for parcel_id in parcel_ids_for_block]
        block_admin = [parcel_admin.get(parcel_id, {}) for parcel_id in parcel_ids_for_block]
        legal_dong_code = most_common([item.get("legal_dong_code") for item in block_admin])
        legal_dong_name = most_common([item.get("legal_dong_name") for item in block_admin])
        sgg_code = most_common([item.get("sgg_code") for item in block_admin])
        centroid = component_centroid(block_parcels, {pid: sum(int(safe_number(r.get("orders"))) for r in records_by_parcel.get(pid, [])) for pid in parcel_ids_for_block}, parcel_ids_for_block)
        neighbors: list[dict[str, Any]] = []
        for (left_id, right_id), edge in assignment_neighbor_edges.items():
            if assignment_id not in {left_id, right_id}:
                continue
            neighbor_id = right_id if left_id == assignment_id else left_id
            neighbors.append(
                {
                    "neighbor_block_id": neighbor_id,
                    "relation": edge["relation"],
                    "centroid_distance_meters": edge["centroid_distance_meters"],
                    "boundary_distance_meters": edge["boundary_distance_meters"],
                    "shared_boundary_length": edge.get("shared_boundary_length", 0.0),
                    "transport_barrier": edge["transport_barrier"],
                    "transport_code": edge["transport_code"],
                    "transport_name": edge["transport_name"],
                    "barrier_source": edge.get("barrier_source", ""),
                    "admin_boundary": bool(edge.get("admin_boundary")),
                }
            )
        neighbors.sort(
            key=lambda item: (
                0 if item["relation"] == "touches" else 1 if item["relation"] == "near" else 2,
                safe_number(item["boundary_distance_meters"], math.inf),
                -safe_number(item.get("shared_boundary_length"), 0.0),
                safe_number(item["centroid_distance_meters"], math.inf),
                item["neighbor_block_id"],
            )
        )
        geometry = {
            "type": "MultiPolygon",
            "coordinates": [
                parcel_by_id[parcel_id].get("geometry", {}).get("coordinates")
                for parcel_id in parcel_ids_for_block
                if parcel_by_id.get(parcel_id, {}).get("geometry", {}).get("coordinates")
            ],
        }
        blocks.append(
            {
                "block_id": assignment_id,
                "geometry": geometry,
                "centroid": {"lat": centroid["lat"], "lon": centroid["lon"]},
                "bbox": summarize_bbox_lonlat(block_parcels),
                "area": round(sum(parcel.area for parcel in block_parcels), 2),
                "dong": most_common([record.get("dong") for record in block_records]),
                "legal_dong_code": legal_dong_code,
                "legal_dong_name": legal_dong_name,
                "sgg_code": sgg_code,
                "road_stem": most_common([record.get("road_stem") for record in block_records]),
                "orders": total_orders(block_records),
                "address_count": len(block_records),
                "parcel_count": len(parcel_ids_for_block),
                "parcel_block_ids": parcel_ids_for_block,
                "address_ids": [record.get("id") for record in block_records],
                "neighbors": neighbors[:MAX_ASSIGNMENT_NEIGHBORS],
            }
        )

    summary = {
        "records": len(scope.get("records") or []),
        "unmatched_records": len(unmatched_records),
        "parcel_blocks": len(parcel_ids),
        "assignment_blocks": len(blocks),
        "candidate_pairs": stats["candidate_pairs"],
        "union_edges": stats["union_edges"],
        "barrier_blocked_edges": stats["barrier_blocked_edges"],
        "admin_boundary_edges": stats["admin_boundary_edges"],
        "near_unconnected_edges": stats["near_unconnected_edges"],
        "single_parcel_assignment_blocks": sum(1 for block in blocks if block["parcel_count"] == 1),
        "max_parcels_in_block": max((block["parcel_count"] for block in blocks), default=0),
        "max_addresses_in_block": max((block["address_count"] for block in blocks), default=0),
        "max_orders_in_block": max((block["orders"] for block in blocks), default=0),
        "blocks_without_neighbors": sum(1 for block in blocks if not block["neighbors"]),
    }
    output_scope = {
        "scope_key": scope.get("scope_key") or f"{scope.get('month')}|{scope.get('center')}",
        "center": scope.get("center"),
        "month": scope.get("month"),
        "color": scope.get("color"),
        "total_orders": scope.get("total_orders"),
        "total_addresses": scope.get("total_addresses"),
        "records": assignment_records,
        "blocks": blocks,
        "assignment_block_summary": summary,
    }
    return output_scope, summary


def build_payload(
    processed_path: Path,
    transport_shp: Path,
    road_shp: Path | None,
    dong_shp: Path | None,
    output_path: Path,
    pretty: bool = False,
) -> dict[str, Any]:
    processed = load_json(processed_path)
    legal_dongs = load_legal_dongs(dong_shp)
    transport_crs = load_crs(transport_shp)
    to_transport = Transformer.from_crs(CRS.from_epsg(4326), transport_crs, always_xy=True)
    to_wgs84 = Transformer.from_crs(transport_crs, CRS.from_epsg(4326), always_xy=True)
    work_bbox = transformed_records_bbox(processed, to_transport)
    print(f"record bbox in transport CRS: {work_bbox}", flush=True)
    barriers = load_transport_barriers(transport_shp, work_bbox)
    road_barrier_count = 0
    if road_shp and road_shp.exists():
        road_crs = load_crs(road_shp)
        to_road = Transformer.from_crs(CRS.from_epsg(4326), road_crs, always_xy=True)
        road_bbox = transformed_records_bbox(processed, to_road)
        road_to_transport = Transformer.from_crs(road_crs, transport_crs, always_xy=True)
        road_barriers = load_road_barriers(road_shp, road_bbox, road_to_transport)
        road_barrier_count = len(road_barriers)
        barriers.extend(road_barriers)
    print(f"combined barriers ready: {len(barriers):,} (road={road_barrier_count:,})", flush=True)
    barrier_index = build_barrier_index(barriers)

    scopes: dict[str, Any] = {}
    summaries: dict[str, Any] = {}
    for scope_key, scope in (processed.get("scopes") or {}).items():
        print(f"building assignment blocks: {scope_key}", flush=True)
        output_scope, summary = build_scope_assignment_blocks(scope, to_transport, to_wgs84, barriers, barrier_index, legal_dongs)
        scopes[scope_key] = output_scope
        summaries[scope_key] = summary
        print(
            f"{scope_key}: parcels={summary['parcel_blocks']:,}, assignment_blocks={summary['assignment_blocks']:,}, "
            f"single={summary['single_parcel_assignment_blocks']:,}, max_addr={summary['max_addresses_in_block']}, "
            f"barrier_edges={summary['barrier_blocked_edges']:,}, admin_edges={summary['admin_boundary_edges']:,}",
            flush=True,
        )

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "processed_blocks": processed_path.name,
            "transport_shp": str(transport_shp),
            "road_shp": str(road_shp) if road_shp else "",
            "dong_shp": str(dong_shp) if dong_shp else "",
            "transport_crs": transport_crs.to_string(),
        },
        "constants": {
            "TRANSPORT_ROAD_CODES": sorted(TRANSPORT_ROAD_CODES),
            "PARCEL_CONNECT_BOUNDARY_METERS": PARCEL_CONNECT_BOUNDARY_METERS,
            "PARCEL_MIN_SHARED_BOUNDARY_METERS": PARCEL_MIN_SHARED_BOUNDARY_METERS,
            "PARCEL_NEIGHBOR_GAP_METERS": PARCEL_NEIGHBOR_GAP_METERS,
            "PARCEL_CANDIDATE_CENTROID_METERS": PARCEL_CANDIDATE_CENTROID_METERS,
            "PARCEL_CANDIDATE_BBOX_METERS": PARCEL_CANDIDATE_BBOX_METERS,
            "MAX_ASSIGNMENT_NEIGHBORS": MAX_ASSIGNMENT_NEIGHBORS,
        },
        "months": processed.get("months") or [],
        "centers": processed.get("centers") or [],
        "scopes": scopes,
        "summaries": summaries,
    }
    write_json(output_path, payload, pretty=pretty)
    return payload


def find_default_dong_shp(base_dir: Path) -> Path | None:
    candidates = sorted((base_dir / "dong").glob("*.shp"))
    return candidates[0] if candidates else None


def parse_args() -> argparse.Namespace:
    base_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Build assignment blocks using parcel polygons and transport road barriers.")
    parser.add_argument("--processed", type=Path, default=base_dir / "processed_blocks.json")
    parser.add_argument("--transport", type=Path, default=base_dir / "transport" / "AL_D142_00_20260609.shp")
    parser.add_argument("--road", type=Path, default=base_dir / "road" / "C_UQ151.shp")
    parser.add_argument("--dong", type=Path, default=find_default_dong_shp(base_dir))
    parser.add_argument("--output", type=Path, default=base_dir / "processed_assignment_blocks.json")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = build_payload(
        args.processed.resolve(),
        args.transport.resolve(),
        args.road.resolve() if args.road else None,
        args.dong.resolve() if args.dong else None,
        args.output.resolve(),
        args.pretty,
    )
    print(f"wrote {args.output}", flush=True)
    for scope_key, summary in payload["summaries"].items():
        print(scope_key, json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
