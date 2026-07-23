from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import shapefile
from pyproj import CRS, Transformer


MAX_NEAREST_POLYGON_MATCH_METERS = 30.0
TOUCH_DISTANCE_METERS = 3.0
NEAR_BLOCK_DISTANCE_METERS = 50.0
MAX_NEIGHBOR_CENTROID_DISTANCE_METERS = 180.0
SHAPE_PREFILTER_MARGIN_METERS = 260.0
POLYGON_INDEX_CELL_METERS = 80.0
NEIGHBOR_INDEX_CELL_METERS = 180.0
MAX_NEIGHBORS_PER_BLOCK = 16
EPSILON = 1e-9


@dataclass
class SourceRecord:
    scope_key: str
    record: dict[str, Any]
    x: float | None
    y: float | None
    source_block_index: int | None = None
    match_type: str = "unmatched"
    match_distance_meters: float | None = None


@dataclass
class PolygonBlock:
    source_index: int
    source_key: str
    attrs: dict[str, Any]
    rings_xy: list[list[tuple[float, float]]]
    rings_lonlat: list[list[list[float]]]
    bbox_xy: tuple[float, float, float, float]
    bbox_lonlat: list[float]
    centroid_xy: tuple[float, float]
    centroid_lonlat: tuple[float, float]
    area: float
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = field(default_factory=list)


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\u3000", " ").split()).strip()


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def decode_cpg(path: Path) -> str:
    if not path.exists():
        return "cp949"
    value = path.read_text(encoding="ascii", errors="ignore").strip()
    if not value:
        return "cp949"
    normalized = value.replace("-", "").lower()
    if normalized in {"euckr", "ksc5601", "ksx1001"}:
        return "cp949"
    return value


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict[str, Any], pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )


def load_crs(shp_path: Path) -> CRS:
    prj_path = shp_path.with_suffix(".prj")
    if prj_path.exists():
        return CRS.from_wkt(prj_path.read_text(encoding="utf-8", errors="ignore"))
    return CRS.from_epsg(5186)


def lonlat_transformer(source_crs: CRS) -> tuple[Transformer, Transformer]:
    wgs84 = CRS.from_epsg(4326)
    to_source = Transformer.from_crs(wgs84, source_crs, always_xy=True)
    to_wgs84 = Transformer.from_crs(source_crs, wgs84, always_xy=True)
    return to_source, to_wgs84


def bbox_intersects(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> bool:
    return not (left[2] < right[0] or left[0] > right[2] or left[3] < right[1] or left[1] > right[3])


def expand_bbox(bbox: tuple[float, float, float, float], margin: float) -> tuple[float, float, float, float]:
    return (bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin)


def bbox_distance_m(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    dx = max(left[0] - right[2], right[0] - left[2], 0.0)
    dy = max(left[1] - right[3], right[1] - left[3], 0.0)
    return math.hypot(dx, dy)


def grid_range(min_value: float, max_value: float, cell_size: float) -> range:
    start = math.floor(min_value / cell_size)
    end = math.floor(max_value / cell_size)
    return range(start, end + 1)


def grid_key(x: float, y: float, cell_size: float) -> tuple[int, int]:
    return (math.floor(x / cell_size), math.floor(y / cell_size))


def ring_area_and_centroid(ring: list[tuple[float, float]]) -> tuple[float, float, float]:
    if len(ring) < 3:
        return 0.0, 0.0, 0.0
    area2 = 0.0
    cx_sum = 0.0
    cy_sum = 0.0
    points = ring if ring[0] == ring[-1] else ring + [ring[0]]
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx_sum += (x1 + x2) * cross
        cy_sum += (y1 + y2) * cross
    area = area2 / 2.0
    if abs(area) < EPSILON:
        return 0.0, 0.0, 0.0
    return area, cx_sum / (6.0 * area), cy_sum / (6.0 * area)


def polygon_area_centroid(rings: list[list[tuple[float, float]]], bbox: tuple[float, float, float, float]) -> tuple[float, tuple[float, float]]:
    signed_area_total = 0.0
    cx_total = 0.0
    cy_total = 0.0
    absolute_area = 0.0
    for ring in rings:
        signed_area, cx, cy = ring_area_and_centroid(ring)
        if abs(signed_area) < EPSILON:
            continue
        signed_area_total += signed_area
        cx_total += cx * signed_area
        cy_total += cy * signed_area
        absolute_area += abs(signed_area)
    if abs(signed_area_total) >= EPSILON:
        return absolute_area, (cx_total / signed_area_total, cy_total / signed_area_total)
    xs = [point[0] for ring in rings for point in ring]
    ys = [point[1] for ring in rings for point in ring]
    if xs and ys:
        return 0.0, (sum(xs) / len(xs), sum(ys) / len(ys))
    return 0.0, ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def close_ring(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not points:
        return points
    if points[0] == points[-1]:
        return points
    return points + [points[0]]


def split_shape_rings(shape: shapefile.Shape) -> list[list[tuple[float, float]]]:
    parts = list(shape.parts) + [len(shape.points)]
    rings: list[list[tuple[float, float]]] = []
    for start, end in zip(parts, parts[1:]):
        raw = shape.points[start:end]
        ring = close_ring([(float(x), float(y)) for x, y in raw])
        if len(ring) >= 4:
            rings.append(ring)
    return rings


def transform_ring_to_lonlat(ring: list[tuple[float, float]], transformer: Transformer) -> list[list[float]]:
    coords: list[list[float]] = []
    for x, y in ring:
        lon, lat = transformer.transform(x, y)
        coords.append([round(float(lon), 7), round(float(lat), 7)])
    return coords


def make_segments(rings: list[list[tuple[float, float]]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for ring in rings:
        points = ring if ring[0] == ring[-1] else close_ring(ring)
        for left, right in zip(points, points[1:]):
            if left != right:
                segments.append((left, right))
    return segments


def point_on_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float, tolerance: float = 1e-7) -> bool:
    distance = point_to_segment_distance(px, py, ax, ay, bx, by)
    if distance > tolerance:
        return False
    return min(ax, bx) - tolerance <= px <= max(ax, bx) + tolerance and min(ay, by) - tolerance <= py <= max(ay, by) + tolerance


def point_in_ring(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    points = ring if ring[0] == ring[-1] else close_ring(ring)
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if point_on_segment(x, y, x1, y1, x2, y2):
            return True
        if (y1 > y) != (y2 > y):
            x_cross = (x2 - x1) * (y - y1) / ((y2 - y1) or EPSILON) + x1
            if x < x_cross:
                inside = not inside
    return inside


def point_in_polygon(x: float, y: float, block: PolygonBlock) -> bool:
    if not (block.bbox_xy[0] - EPSILON <= x <= block.bbox_xy[2] + EPSILON and block.bbox_xy[1] - EPSILON <= y <= block.bbox_xy[3] + EPSILON):
        return False
    inside = False
    for ring in block.rings_xy:
        if point_in_ring(x, y, ring):
            inside = not inside
    return inside


def point_to_segment_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx = bx - ax
    dy = by - ay
    length2 = dx * dx + dy * dy
    if length2 <= EPSILON:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    closest_x = ax + t * dx
    closest_y = ay + t * dy
    return math.hypot(px - closest_x, py - closest_y)


def point_to_polygon_distance(x: float, y: float, block: PolygonBlock) -> float:
    if point_in_polygon(x, y, block):
        return 0.0
    best = math.inf
    for (ax, ay), (bx, by) in block.segments:
        best = min(best, point_to_segment_distance(x, y, ax, ay, bx, by))
    return best


def orientation(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def segments_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    ax, ay = a1
    bx, by = a2
    cx, cy = b1
    dx, dy = b2
    o1 = orientation(ax, ay, bx, by, cx, cy)
    o2 = orientation(ax, ay, bx, by, dx, dy)
    o3 = orientation(cx, cy, dx, dy, ax, ay)
    o4 = orientation(cx, cy, dx, dy, bx, by)
    if o1 * o2 < 0 and o3 * o4 < 0:
        return True
    if abs(o1) <= EPSILON and point_on_segment(cx, cy, ax, ay, bx, by):
        return True
    if abs(o2) <= EPSILON and point_on_segment(dx, dy, ax, ay, bx, by):
        return True
    if abs(o3) <= EPSILON and point_on_segment(ax, ay, cx, cy, dx, dy):
        return True
    if abs(o4) <= EPSILON and point_on_segment(bx, by, cx, cy, dx, dy):
        return True
    return False


def segment_distance(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> float:
    if segments_intersect(a1, a2, b1, b2):
        return 0.0
    return min(
        point_to_segment_distance(a1[0], a1[1], b1[0], b1[1], b2[0], b2[1]),
        point_to_segment_distance(a2[0], a2[1], b1[0], b1[1], b2[0], b2[1]),
        point_to_segment_distance(b1[0], b1[1], a1[0], a1[1], a2[0], a2[1]),
        point_to_segment_distance(b2[0], b2[1], a1[0], a1[1], a2[0], a2[1]),
    )


def boundary_distance(left: PolygonBlock, right: PolygonBlock) -> float:
    if bbox_distance_m(left.bbox_xy, right.bbox_xy) > NEAR_BLOCK_DISTANCE_METERS and euclidean_distance(left.centroid_xy, right.centroid_xy) > MAX_NEIGHBOR_CENTROID_DISTANCE_METERS:
        return math.inf
    best = math.inf
    for left_seg in left.segments:
        for right_seg in right.segments:
            best = min(best, segment_distance(left_seg[0], left_seg[1], right_seg[0], right_seg[1]))
            if best <= EPSILON:
                return 0.0
    return best


def euclidean_distance(left: tuple[float, float], right: tuple[float, float]) -> float:
    return math.hypot(left[0] - right[0], left[1] - right[1])


def segment_length(segment: tuple[tuple[float, float], tuple[float, float]]) -> float:
    return euclidean_distance(segment[0], segment[1])


def shared_segment_overlap(
    left: tuple[tuple[float, float], tuple[float, float]],
    right: tuple[tuple[float, float], tuple[float, float]],
    tolerance: float = TOUCH_DISTANCE_METERS,
) -> float:
    left_len = segment_length(left)
    right_len = segment_length(right)
    if left_len <= EPSILON or right_len <= EPSILON:
        return 0.0
    ux = (left[1][0] - left[0][0]) / left_len
    uy = (left[1][1] - left[0][1]) / left_len
    vx = (right[1][0] - right[0][0]) / right_len
    vy = (right[1][1] - right[0][1]) / right_len
    if abs(ux * vx + uy * vy) < 0.985:
        return 0.0
    mid_right = ((right[0][0] + right[1][0]) / 2.0, (right[0][1] + right[1][1]) / 2.0)
    line_distance = abs(orientation(left[0][0], left[0][1], left[1][0], left[1][1], mid_right[0], mid_right[1])) / left_len
    if line_distance > tolerance:
        return 0.0
    left_t = sorted([0.0, left_len])
    right_t = sorted([
        (right[0][0] - left[0][0]) * ux + (right[0][1] - left[0][1]) * uy,
        (right[1][0] - left[0][0]) * ux + (right[1][1] - left[0][1]) * uy,
    ])
    return max(0.0, min(left_t[1], right_t[1]) - max(left_t[0], right_t[0]))


def shared_boundary_length(left: PolygonBlock, right: PolygonBlock) -> float:
    total = 0.0
    for left_seg in left.segments:
        for right_seg in right.segments:
            if segment_distance(left_seg[0], left_seg[1], right_seg[0], right_seg[1]) <= TOUCH_DISTANCE_METERS:
                total += shared_segment_overlap(left_seg, right_seg)
    return total


def parse_pnu_lot(pnu: str) -> str:
    text = normalize_text(pnu)
    if len(text) < 19 or not text.isdigit():
        return ""
    main = int(text[11:15])
    sub = int(text[15:19])
    prefix = "산" if text[10] == "2" else ""
    return f"{prefix}{main}-{sub}" if sub else f"{prefix}{main}"


def guess_source_key(attrs: dict[str, Any], source_index: int) -> str:
    pnu = normalize_text(attrs.get("PNU"))
    if pnu:
        return pnu
    sgg_oid = normalize_text(attrs.get("SGG_OID"))
    if sgg_oid:
        return sgg_oid
    return f"shape_{source_index}"


def record_to_source_record(scope_key: str, record: dict[str, Any], transformer: Transformer) -> SourceRecord:
    lat = safe_float(record.get("lat"))
    lon = safe_float(record.get("lon"))
    if lat is None or lon is None:
        return SourceRecord(scope_key=scope_key, record=record, x=None, y=None)
    try:
        x, y = transformer.transform(lon, lat)
    except Exception:
        return SourceRecord(scope_key=scope_key, record=record, x=None, y=None)
    if not (math.isfinite(x) and math.isfinite(y)):
        return SourceRecord(scope_key=scope_key, record=record, x=None, y=None)
    return SourceRecord(scope_key=scope_key, record=record, x=float(x), y=float(y))


def collect_source_records(data: dict[str, Any], transformer: Transformer) -> list[SourceRecord]:
    items: list[SourceRecord] = []
    for scope_key, scope in (data.get("scopes") or {}).items():
        for record in scope.get("records") or []:
            items.append(record_to_source_record(scope_key, record, transformer))
    return items


def records_bbox(records: list[SourceRecord]) -> tuple[float, float, float, float]:
    xs = [item.x for item in records if item.x is not None]
    ys = [item.y for item in records if item.y is not None]
    if not xs or not ys:
        raise ValueError("No valid address coordinates were found in process_geocode.json")
    return (min(xs), min(ys), max(xs), max(ys))


def shape_bbox_tuple(shape: shapefile.Shape) -> tuple[float, float, float, float]:
    bbox = shape.bbox
    return (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))


def record_attrs(reader: shapefile.Reader, index: int, fields: list[str]) -> dict[str, Any]:
    record = reader.record(index)
    return {field: record[pos] for pos, field in enumerate(fields)}


def load_candidate_polygons(shp_path: Path, address_bbox: tuple[float, float, float, float], to_wgs84: Transformer) -> dict[int, PolygonBlock]:
    cpg_path = shp_path.with_suffix(".cpg")
    encoding = decode_cpg(cpg_path)
    reader = shapefile.Reader(str(shp_path), encoding=encoding, encodingErrors="replace")
    fields = [field[0] for field in reader.fields[1:]]
    expanded = expand_bbox(address_bbox, SHAPE_PREFILTER_MARGIN_METERS)
    polygons: dict[int, PolygonBlock] = {}
    total = len(reader)
    for index, shape in enumerate(reader.iterShapes()):
        if index and index % 50000 == 0:
            print(f"loaded candidate polygons: {len(polygons):,} / scanned {index:,}/{total:,}", flush=True)
        if shape.shapeType not in {shapefile.POLYGON, shapefile.POLYGONZ, shapefile.POLYGONM}:
            continue
        bbox = shape_bbox_tuple(shape)
        if not bbox_intersects(bbox, expanded):
            continue
        rings_xy = split_shape_rings(shape)
        if not rings_xy:
            continue
        area, centroid_xy = polygon_area_centroid(rings_xy, bbox)
        if area <= EPSILON:
            continue
        attrs = record_attrs(reader, index, fields)
        rings_lonlat = [transform_ring_to_lonlat(ring, to_wgs84) for ring in rings_xy]
        lon_values = [coord[0] for ring in rings_lonlat for coord in ring]
        lat_values = [coord[1] for ring in rings_lonlat for coord in ring]
        centroid_lon, centroid_lat = to_wgs84.transform(centroid_xy[0], centroid_xy[1])
        polygons[index] = PolygonBlock(
            source_index=index,
            source_key=guess_source_key(attrs, index),
            attrs=attrs,
            rings_xy=rings_xy,
            rings_lonlat=rings_lonlat,
            bbox_xy=bbox,
            bbox_lonlat=[round(min(lon_values), 7), round(min(lat_values), 7), round(max(lon_values), 7), round(max(lat_values), 7)],
            centroid_xy=centroid_xy,
            centroid_lonlat=(float(centroid_lon), float(centroid_lat)),
            area=float(area),
            segments=make_segments(rings_xy),
        )
    print(f"candidate polygons ready: {len(polygons):,}", flush=True)
    return polygons


def build_polygon_index(polygons: dict[int, PolygonBlock]) -> dict[tuple[int, int], list[int]]:
    index: dict[tuple[int, int], list[int]] = defaultdict(list)
    for source_index, block in polygons.items():
        min_x, min_y, max_x, max_y = block.bbox_xy
        for gx in grid_range(min_x, max_x, POLYGON_INDEX_CELL_METERS):
            for gy in grid_range(min_y, max_y, POLYGON_INDEX_CELL_METERS):
                index[(gx, gy)].append(source_index)
    return index


def candidate_polygon_ids_for_point(
    x: float,
    y: float,
    polygon_index: dict[tuple[int, int], list[int]],
    radius: float = 0.0,
) -> set[int]:
    ids: set[int] = set()
    for gx in grid_range(x - radius, x + radius, POLYGON_INDEX_CELL_METERS):
        for gy in grid_range(y - radius, y + radius, POLYGON_INDEX_CELL_METERS):
            ids.update(polygon_index.get((gx, gy), []))
    return ids


def match_records_to_polygons(records: list[SourceRecord], polygons: dict[int, PolygonBlock]) -> None:
    polygon_index = build_polygon_index(polygons)
    for idx, item in enumerate(records, start=1):
        if idx % 1000 == 0:
            print(f"matched records: {idx:,}/{len(records):,}", flush=True)
        if item.x is None or item.y is None:
            continue
        inside_matches: list[PolygonBlock] = []
        for source_index in candidate_polygon_ids_for_point(item.x, item.y, polygon_index):
            block = polygons[source_index]
            if point_in_polygon(item.x, item.y, block):
                inside_matches.append(block)
        if inside_matches:
            best = min(inside_matches, key=lambda block: (block.area, block.source_index))
            item.source_block_index = best.source_index
            item.match_type = "inside_polygon"
            item.match_distance_meters = 0.0
            continue
        best_distance = math.inf
        best_block: PolygonBlock | None = None
        for source_index in candidate_polygon_ids_for_point(item.x, item.y, polygon_index, MAX_NEAREST_POLYGON_MATCH_METERS):
            block = polygons[source_index]
            if bbox_distance_m((item.x, item.y, item.x, item.y), block.bbox_xy) > MAX_NEAREST_POLYGON_MATCH_METERS:
                continue
            distance = point_to_polygon_distance(item.x, item.y, block)
            if distance < best_distance:
                best_distance = distance
                best_block = block
        if best_block is not None and best_distance <= MAX_NEAREST_POLYGON_MATCH_METERS:
            item.source_block_index = best_block.source_index
            item.match_type = "nearest_polygon"
            item.match_distance_meters = best_distance


def most_common_text(values: list[Any]) -> str:
    counter = Counter(normalize_text(value) for value in values if normalize_text(value))
    return counter.most_common(1)[0][0] if counter else ""


def build_neighbor_edges(scope_blocks: list[dict[str, Any]], source_polygons: dict[int, PolygonBlock]) -> dict[int, list[dict[str, Any]]]:
    if not scope_blocks:
        return {}
    source_ids = [block["_source_index"] for block in scope_blocks]
    source_id_set = set(source_ids)
    id_to_scope_block = {block["_source_index"]: block for block in scope_blocks}
    centroid_index: dict[tuple[int, int], list[int]] = defaultdict(list)
    bbox_index: dict[tuple[int, int], list[int]] = defaultdict(list)
    for source_id in source_ids:
        polygon = source_polygons[source_id]
        centroid_index[grid_key(polygon.centroid_xy[0], polygon.centroid_xy[1], NEIGHBOR_INDEX_CELL_METERS)].append(source_id)
        expanded_bbox = expand_bbox(polygon.bbox_xy, NEAR_BLOCK_DISTANCE_METERS)
        for gx in grid_range(expanded_bbox[0], expanded_bbox[2], NEIGHBOR_INDEX_CELL_METERS):
            for gy in grid_range(expanded_bbox[1], expanded_bbox[3], NEIGHBOR_INDEX_CELL_METERS):
                bbox_index[(gx, gy)].append(source_id)

    candidate_pairs: set[tuple[int, int]] = set()
    for source_id in source_ids:
        polygon = source_polygons[source_id]
        cx, cy = grid_key(polygon.centroid_xy[0], polygon.centroid_xy[1], NEIGHBOR_INDEX_CELL_METERS)
        for gx in range(cx - 1, cx + 2):
            for gy in range(cy - 1, cy + 2):
                for other_id in centroid_index.get((gx, gy), []):
                    if other_id != source_id and other_id in source_id_set:
                        candidate_pairs.add(tuple(sorted((source_id, other_id))))
        expanded_bbox = expand_bbox(polygon.bbox_xy, NEAR_BLOCK_DISTANCE_METERS)
        for gx in grid_range(expanded_bbox[0], expanded_bbox[2], NEIGHBOR_INDEX_CELL_METERS):
            for gy in grid_range(expanded_bbox[1], expanded_bbox[3], NEIGHBOR_INDEX_CELL_METERS):
                for other_id in bbox_index.get((gx, gy), []):
                    if other_id != source_id and other_id in source_id_set:
                        candidate_pairs.add(tuple(sorted((source_id, other_id))))

    edges: dict[int, list[dict[str, Any]]] = defaultdict(list)
    total_pairs = len(candidate_pairs)
    for pair_index, (left_id, right_id) in enumerate(sorted(candidate_pairs), start=1):
        if pair_index % 20000 == 0:
            print(f"neighbor pairs: {pair_index:,}/{total_pairs:,}", flush=True)
        left = source_polygons[left_id]
        right = source_polygons[right_id]
        centroid_distance = euclidean_distance(left.centroid_xy, right.centroid_xy)
        quick_bbox_distance = bbox_distance_m(left.bbox_xy, right.bbox_xy)
        if centroid_distance > MAX_NEIGHBOR_CENTROID_DISTANCE_METERS and quick_bbox_distance > NEAR_BLOCK_DISTANCE_METERS:
            continue
        distance = boundary_distance(left, right)
        relation = ""
        if distance <= TOUCH_DISTANCE_METERS:
            relation = "touches"
        elif distance <= NEAR_BLOCK_DISTANCE_METERS:
            relation = "near"
        elif centroid_distance <= MAX_NEIGHBOR_CENTROID_DISTANCE_METERS:
            relation = "centroid_near"
        if not relation:
            continue
        shared_length = shared_boundary_length(left, right) if distance <= TOUCH_DISTANCE_METERS else 0.0
        left_block = id_to_scope_block[left_id]
        right_block = id_to_scope_block[right_id]
        left_edge = {
            "neighbor_block_id": right_block["block_id"],
            "relation": relation,
            "centroid_distance_meters": round(centroid_distance, 1),
            "boundary_distance_meters": round(distance if math.isfinite(distance) else quick_bbox_distance, 1),
            "shared_boundary_length": round(shared_length, 1),
        }
        right_edge = {
            "neighbor_block_id": left_block["block_id"],
            "relation": relation,
            "centroid_distance_meters": round(centroid_distance, 1),
            "boundary_distance_meters": round(distance if math.isfinite(distance) else quick_bbox_distance, 1),
            "shared_boundary_length": round(shared_length, 1),
        }
        edges[left_id].append(left_edge)
        edges[right_id].append(right_edge)
    return edges


def sort_edges(edges: list[dict[str, Any]], orders_by_id: dict[str, int]) -> list[dict[str, Any]]:
    relation_order = {"touches": 0, "near": 1, "centroid_near": 2}
    return sorted(
        edges,
        key=lambda edge: (
            relation_order.get(edge.get("relation"), 9),
            safe_float(edge.get("boundary_distance_meters")) or math.inf,
            -(safe_float(edge.get("shared_boundary_length")) or 0.0),
            safe_float(edge.get("centroid_distance_meters")) or math.inf,
            -(orders_by_id.get(edge.get("neighbor_block_id"), 0)),
            normalize_text(edge.get("neighbor_block_id")),
        ),
    )[:MAX_NEIGHBORS_PER_BLOCK]


def build_scope_payloads(data: dict[str, Any], source_records: list[SourceRecord], polygons: dict[int, PolygonBlock]) -> dict[str, Any]:
    records_by_scope: dict[str, list[SourceRecord]] = defaultdict(list)
    for item in source_records:
        records_by_scope[item.scope_key].append(item)

    scopes: dict[str, Any] = {}
    for scope_key, source_scope in (data.get("scopes") or {}).items():
        items = records_by_scope.get(scope_key, [])
        matched_source_ids = sorted({item.source_block_index for item in items if item.source_block_index is not None})
        block_id_by_source = {source_id: f"B{idx:06d}" for idx, source_id in enumerate(matched_source_ids, start=1)}
        items_by_source: dict[int, list[SourceRecord]] = defaultdict(list)
        for item in items:
            if item.source_block_index is not None:
                items_by_source[item.source_block_index].append(item)

        output_records: list[dict[str, Any]] = []
        for item in items:
            record = {key: value for key, value in item.record.items() if key != "neighbors"}
            if item.source_block_index is None:
                record["block_id"] = ""
                record["match_type"] = "unmatched"
                record["match_distance_meters"] = None
            else:
                record["block_id"] = block_id_by_source[item.source_block_index]
                record["match_type"] = item.match_type
                record["match_distance_meters"] = round(float(item.match_distance_meters or 0.0), 2)
            output_records.append(record)

        scope_blocks: list[dict[str, Any]] = []
        for source_id in matched_source_ids:
            polygon = polygons[source_id]
            block_items = items_by_source[source_id]
            address_ids = [item.record.get("id") for item in block_items]
            orders = int(sum(parse_int(item.record.get("orders")) or 0 for item in block_items))
            jibun_values = [polygon.attrs.get("JIBUN"), parse_pnu_lot(normalize_text(polygon.attrs.get("PNU")))]
            jibun_values.extend(item.record.get("jibun_hint") for item in block_items)
            block = {
                "_source_index": source_id,
                "block_id": block_id_by_source[source_id],
                "source_key": polygon.source_key,
                "pnu": normalize_text(polygon.attrs.get("PNU")),
                "geometry": {
                    "type": "Polygon",
                    "coordinates": polygon.rings_lonlat,
                },
                "centroid": {
                    "lat": round(float(polygon.centroid_lonlat[1]), 7),
                    "lon": round(float(polygon.centroid_lonlat[0]), 7),
                },
                "bbox": polygon.bbox_lonlat,
                "area": round(float(polygon.area), 2),
                "dong": most_common_text([item.record.get("dong") for item in block_items]),
                "road_stem": most_common_text([item.record.get("road_stem") for item in block_items]),
                "jibun_hint": most_common_text(jibun_values),
                "orders": orders,
                "address_count": len(block_items),
                "address_ids": address_ids,
                "neighbors": [],
            }
            scope_blocks.append(block)

        edges_by_source = build_neighbor_edges(scope_blocks, polygons)
        orders_by_id = {block["block_id"]: int(block.get("orders") or 0) for block in scope_blocks}
        for block in scope_blocks:
            block["neighbors"] = sort_edges(edges_by_source.get(block["_source_index"], []), orders_by_id)
            del block["_source_index"]

        scopes[scope_key] = {
            "scope_key": scope_key,
            "center": source_scope.get("center"),
            "month": source_scope.get("month"),
            "color": source_scope.get("color"),
            "total_orders": int(source_scope.get("total_orders") or sum(parse_int(record.get("orders")) or 0 for record in output_records)),
            "total_addresses": int(source_scope.get("total_addresses") or len(output_records)),
            "blocks": scope_blocks,
            "records": output_records,
        }
        matched_count = sum(1 for record in output_records if record.get("match_type") != "unmatched")
        unmatched_count = len(output_records) - matched_count
        print(
            f"{scope_key}: records={len(output_records):,}, blocks={len(scope_blocks):,}, matched={matched_count:,}, unmatched={unmatched_count:,}",
            flush=True,
        )
    return scopes


def build_processed_payload(input_path: Path, shp_path: Path, output_path: Path, pretty: bool) -> dict[str, Any]:
    data = load_json(input_path)
    source_crs = load_crs(shp_path)
    to_source, to_wgs84 = lonlat_transformer(source_crs)
    source_records = collect_source_records(data, to_source)
    address_bbox = records_bbox(source_records)
    print(f"address bbox in source CRS: {address_bbox}", flush=True)
    polygons = load_candidate_polygons(shp_path, address_bbox, to_wgs84)
    match_records_to_polygons(source_records, polygons)
    scopes = build_scope_payloads(data, source_records, polygons)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "orders": input_path.name,
            "shp": str(shp_path.relative_to(input_path.parent) if shp_path.is_relative_to(input_path.parent) else shp_path),
            "crs": source_crs.to_string(),
        },
        "constants": {
            "MAX_NEAREST_POLYGON_MATCH_METERS": MAX_NEAREST_POLYGON_MATCH_METERS,
            "TOUCH_DISTANCE_METERS": TOUCH_DISTANCE_METERS,
            "NEAR_BLOCK_DISTANCE_METERS": NEAR_BLOCK_DISTANCE_METERS,
            "MAX_NEIGHBOR_CENTROID_DISTANCE_METERS": MAX_NEIGHBOR_CENTROID_DISTANCE_METERS,
        },
        "months": data.get("months") or sorted({scope.get("month") for scope in scopes.values() if scope.get("month")}),
        "centers": data.get("centers") or sorted({scope.get("center") for scope in scopes.values() if scope.get("center")}),
        "scopes": scopes,
    }
    write_json(output_path, payload, pretty=pretty)
    return payload


def find_default_shp(base_dir: Path) -> Path:
    candidates = sorted((base_dir / "block_geocodes").glob("*.shp"))
    if not candidates:
        raise FileNotFoundError("No .shp file found in block_geocodes")
    return candidates[0]


def parse_args() -> argparse.Namespace:
    base_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Build processed_blocks.json from process_geocode.json and cadastral shp/dbf polygons.")
    parser.add_argument("--input", type=Path, default=base_dir / "process_geocode.json")
    parser.add_argument("--shp", type=Path, default=find_default_shp(base_dir))
    parser.add_argument("--output", type=Path, default=base_dir / "processed_blocks.json")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = build_processed_payload(args.input.resolve(), args.shp.resolve(), args.output.resolve(), args.pretty)
    total_records = sum(len(scope.get("records") or []) for scope in payload["scopes"].values())
    total_blocks = sum(len(scope.get("blocks") or []) for scope in payload["scopes"].values())
    total_unmatched = sum(
        1
        for scope in payload["scopes"].values()
        for record in scope.get("records") or []
        if record.get("match_type") == "unmatched"
    )
    print(
        f"wrote {args.output} / records={total_records:,}, blocks={total_blocks:,}, unmatched={total_unmatched:,}",
        flush=True,
    )


if __name__ == "__main__":
    main()
