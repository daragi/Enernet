from __future__ import annotations

import argparse
import html
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_PATH = BASE_DIR / "processed_assignment_blocks.json"
DEFAULT_OUTPUT_DIR = BASE_DIR / "boundary_map"
DEFAULT_CENTERS = ("H074", "H072")

PEOPLE_COUNTS = {
    "H071": 20,
    "H072": 33,
    "H073": 33,
    "H074": 33,
    "H075": 24,
}

TOUCH_BOUNDARY_METERS = 3.0
TOUCH_SHARED_BOUNDARY_METERS = 1.0
NEAR_BOUNDARY_METERS = 18.0
NEAR_CENTROID_METERS = 90.0
WEAK_BRIDGE_METERS = 500.0
BAND_TOLERANCE = 0.10

ZONE_COLORS = [
    "#0067b1", "#d33115", "#00a65a", "#7b2cbf", "#f59f00",
    "#008c95", "#c1126b", "#5c940d", "#f76707", "#364fc7",
    "#2f9e44", "#9c36b5", "#e03131", "#0b7285", "#fab005",
    "#7048e8", "#087f5b", "#e64980", "#1971c2", "#c2410c",
    "#66a80f", "#862e9c", "#f03e3e", "#0ca678", "#f08c00",
    "#4263eb", "#a61e4d", "#228be6", "#74b816", "#d9480f",
    "#5f3dc4", "#099268", "#e67700", "#1864ab", "#c2255c",
    "#37b24d", "#ae3ec9", "#e8590c", "#1098ad", "#f06595",
]


def safe_number(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def order_of(record: dict[str, Any]) -> int:
    return int(max(0.0, safe_number(record.get("orders"))))


def total_orders(records: list[dict[str, Any]]) -> int:
    return sum(order_of(record) for record in records)


def block_id(block: dict[str, Any]) -> str:
    return str(block.get("block_id") or block.get("id") or "")


def record_block_id(record: dict[str, Any]) -> str:
    return str(record.get("assignment_block_id") or record.get("block_id") or "")


def short_block_id(value: str) -> str:
    text = str(value or "")
    return text.rsplit("|", 1)[-1] if "|" in text else text


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def block_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_center = left.get("centroid") or {}
    right_center = right.get("centroid") or {}
    return haversine_m(
        safe_number(left_center.get("lat"), math.nan),
        safe_number(left_center.get("lon"), math.nan),
        safe_number(right_center.get("lat"), math.nan),
        safe_number(right_center.get("lon"), math.nan),
    )


def record_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    return haversine_m(
        safe_number(left.get("lat"), math.nan),
        safe_number(left.get("lon"), math.nan),
        safe_number(right.get("lat"), math.nan),
        safe_number(right.get("lon"), math.nan),
    )


def weighted_centroid(blocks: list[dict[str, Any]]) -> dict[str, float]:
    total = sum(max(1.0, safe_number(block.get("orders"))) for block in blocks) or 1.0
    return {
        "lat": sum((block.get("centroid") or {}).get("lat", 0.0) * max(1.0, safe_number(block.get("orders"))) for block in blocks) / total,
        "lon": sum((block.get("centroid") or {}).get("lon", 0.0) * max(1.0, safe_number(block.get("orders"))) for block in blocks) / total,
    }


def is_coord(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    )


def is_ring(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and is_coord(value[0])


def is_polygon(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and is_ring(value[0])


def normalize_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    if geometry.get("type") != "MultiPolygon":
        return geometry
    normalized: list[Any] = []
    for polygon in geometry.get("coordinates") or []:
        while isinstance(polygon, list) and len(polygon) == 1 and is_polygon(polygon[0]):
            polygon = polygon[0]
        normalized.append(polygon)
    return {"type": "MultiPolygon", "coordinates": normalized}


def bbox_from_blocks(blocks: list[dict[str, Any]]) -> list[float]:
    bboxes = [block.get("bbox") for block in blocks if isinstance(block.get("bbox"), list) and len(block["bbox"]) == 4]
    if not bboxes:
        return [127.25, 36.20, 127.55, 36.50]
    return [
        min(bbox[0] for bbox in bboxes),
        min(bbox[1] for bbox in bboxes),
        max(bbox[2] for bbox in bboxes),
        max(bbox[3] for bbox in bboxes),
    ]


def route_edge_allowed(edge: dict[str, Any]) -> bool:
    relation = str(edge.get("relation") or "")
    boundary = safe_number(edge.get("boundary_distance_meters"), math.inf)
    centroid = safe_number(edge.get("centroid_distance_meters"), math.inf)
    shared = safe_number(edge.get("shared_boundary_length"))
    if relation == "touches":
        return boundary <= TOUCH_BOUNDARY_METERS and shared >= TOUCH_SHARED_BOUNDARY_METERS
    if relation == "near":
        return boundary <= NEAR_BOUNDARY_METERS and centroid <= NEAR_CENTROID_METERS
    return False


def relation_rank(relation: str) -> int:
    if relation == "touches":
        return 0
    if relation == "near":
        return 1
    if relation == "weak_bridge":
        return 2
    return 9


def edge_payload(source: dict[str, Any], target: dict[str, Any], edge: dict[str, Any], route_allowed: bool) -> dict[str, Any]:
    source_center = source.get("centroid") or {}
    target_center = target.get("centroid") or {}
    distance = block_distance(source, target)
    return {
        "from": block_id(source),
        "to": str(edge.get("neighbor_block_id") or ""),
        "from_short": short_block_id(block_id(source)),
        "to_short": short_block_id(str(edge.get("neighbor_block_id") or "")),
        "relation": str(edge.get("relation") or ""),
        "route_allowed": route_allowed,
        "boundary": safe_number(edge.get("boundary_distance_meters"), math.inf),
        "centroid": safe_number(edge.get("centroid_distance_meters"), distance),
        "shared": safe_number(edge.get("shared_boundary_length")),
        "distance": distance,
        "coords": [
            [round(safe_number(source_center.get("lat")), 7), round(safe_number(source_center.get("lon")), 7)],
            [round(safe_number(target_center.get("lat")), 7), round(safe_number(target_center.get("lon")), 7)],
        ],
    }


def build_route_graph(blocks: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    block_map = {block_id(block): block for block in blocks if block_id(block)}
    adjacency: dict[str, list[dict[str, Any]]] = {bid: [] for bid in block_map}
    route_edges: list[dict[str, Any]] = []
    blocked_edges: list[dict[str, Any]] = []
    best_edge_by_pair: dict[tuple[str, str], dict[str, Any]] = {}

    for source in blocks:
        source_id = block_id(source)
        if not source_id:
            continue
        for raw_edge in source.get("neighbors") or []:
            target_id = str(raw_edge.get("neighbor_block_id") or "")
            target = block_map.get(target_id)
            if not target:
                continue
            pair = tuple(sorted((source_id, target_id)))
            allowed = route_edge_allowed(raw_edge)
            payload = edge_payload(source, target, raw_edge, allowed)
            previous = best_edge_by_pair.get(pair)
            if not previous or (
                relation_rank(payload["relation"]),
                payload["boundary"],
                -payload["shared"],
                payload["centroid"],
            ) < (
                relation_rank(previous["relation"]),
                previous["boundary"],
                -previous["shared"],
                previous["centroid"],
            ):
                best_edge_by_pair[pair] = payload

    for pair, payload in best_edge_by_pair.items():
        left, right = pair
        if payload["route_allowed"]:
            adjacency[left].append({**payload, "neighbor_block_id": right if payload["from"] == left else left})
            adjacency[right].append({**payload, "neighbor_block_id": left if payload["from"] == right else payload["from"]})
            route_edges.append(payload)
        else:
            blocked_edges.append(payload)

    for edges in adjacency.values():
        edges.sort(
            key=lambda edge: (
                relation_rank(edge["relation"]),
                safe_number(edge["boundary"], math.inf),
                -safe_number(edge["shared"]),
                safe_number(edge["centroid"], math.inf),
                str(edge["neighbor_block_id"]),
            )
        )

    return adjacency, route_edges, blocked_edges, best_edge_by_pair


def build_components(
    active_ids: set[str],
    block_map: dict[str, dict[str, Any]],
    adjacency: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    seen: set[str] = set()
    for start_id in sorted(active_ids):
        if start_id in seen:
            continue
        queue = [start_id]
        seen.add(start_id)
        ids: list[str] = []
        while queue:
            current_id = queue.pop(0)
            ids.append(current_id)
            for edge in adjacency.get(current_id) or []:
                neighbor_id = str(edge.get("neighbor_block_id") or "")
                if neighbor_id in active_ids and neighbor_id not in seen:
                    seen.add(neighbor_id)
                    queue.append(neighbor_id)
        blocks = [block_map[bid] for bid in ids if bid in block_map]
        components.append(
            {
                "component_id": f"C{len(components) + 1}",
                "block_ids": ids,
                "blocks": blocks,
                "orders": sum(int(safe_number(block.get("orders"))) for block in blocks),
                "centroid": weighted_centroid(blocks),
            }
        )
    return components


def component_distance(left: dict[str, Any], right: dict[str, Any], cache: dict[tuple[str, str], float]) -> float:
    key = tuple(sorted((left["component_id"], right["component_id"])))
    if key in cache:
        return cache[key]
    best = math.inf
    for left_block in left["blocks"]:
        for right_block in right["blocks"]:
            best = min(best, block_distance(left_block, right_block))
    cache[key] = best
    return best


def build_jump_clusters(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parent = list(range(len(components)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    cache: dict[tuple[str, str], float] = {}
    for left in range(len(components)):
        for right in range(left + 1, len(components)):
            if component_distance(components[left], components[right], cache) <= WEAK_BRIDGE_METERS:
                union(left, right)

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, component in enumerate(components):
        grouped[find(index)].append(component)

    clusters: list[dict[str, Any]] = []
    for items in grouped.values():
        blocks = [block for component in items for block in component["blocks"]]
        clusters.append(
            {
                "cluster_id": f"G{len(clusters) + 1}",
                "components": items,
                "blocks": blocks,
                "block_ids": [block_id(block) for block in blocks],
                "orders": sum(component["orders"] for component in items),
                "centroid": weighted_centroid(blocks),
            }
        )
    return clusters


def northwest_score(block: dict[str, Any], min_lon: float, max_lat: float) -> float:
    center = block.get("centroid") or {}
    return abs(safe_number(center.get("lon")) - min_lon) + abs(max_lat - safe_number(center.get("lat")))


def serpentine_ranks(blocks: list[dict[str, Any]]) -> dict[str, int]:
    if not blocks:
        return {}
    centers = [(block_id(block), safe_number((block.get("centroid") or {}).get("lat")), safe_number((block.get("centroid") or {}).get("lon"))) for block in blocks]
    min_lat = min(item[1] for item in centers)
    max_lat = max(item[1] for item in centers)
    row_count = max(1, min(24, round(math.sqrt(len(blocks)))))
    lat_span = max(0.000001, max_lat - min_lat)
    rows: dict[int, list[tuple[str, float, float]]] = defaultdict(list)
    for bid, lat, lon in centers:
        row = min(row_count - 1, max(0, int((max_lat - lat) / lat_span * row_count)))
        rows[row].append((bid, lat, lon))
    ordered: list[str] = []
    for row in sorted(rows):
        items = rows[row]
        items.sort(key=lambda item: item[2], reverse=bool(row % 2))
        ordered.extend(item[0] for item in items)
    return {bid: index for index, bid in enumerate(ordered)}


def route_component_blocks(
    component: dict[str, Any],
    block_map: dict[str, dict[str, Any]],
    adjacency: dict[str, list[dict[str, Any]]],
    entry_point: dict[str, float] | None,
) -> list[str]:
    blocks = component["blocks"]
    if not blocks:
        return []
    ranks = serpentine_ranks(blocks)
    unvisited = {block_id(block) for block in blocks}
    min_lon = min(safe_number((block.get("centroid") or {}).get("lon")) for block in blocks)
    max_lat = max(safe_number((block.get("centroid") or {}).get("lat")) for block in blocks)
    if entry_point:
        current_id = min(
            unvisited,
            key=lambda bid: (
                haversine_m(entry_point["lat"], entry_point["lon"], safe_number((block_map[bid].get("centroid") or {}).get("lat")), safe_number((block_map[bid].get("centroid") or {}).get("lon"))),
                ranks.get(bid, 999999),
            ),
        )
    else:
        current_id = min(unvisited, key=lambda bid: (northwest_score(block_map[bid], min_lon, max_lat), ranks.get(bid, 999999), bid))

    route: list[str] = []
    while current_id and unvisited:
        route.append(current_id)
        unvisited.remove(current_id)
        if not unvisited:
            break
        route_neighbors = [
            str(edge.get("neighbor_block_id") or "")
            for edge in adjacency.get(current_id) or []
            if str(edge.get("neighbor_block_id") or "") in unvisited
        ]
        if route_neighbors:
            current_rank = ranks.get(current_id, 0)
            current_id = min(
                route_neighbors,
                key=lambda bid: (
                    0 if ranks.get(bid, 999999) >= current_rank else 1,
                    abs(ranks.get(bid, 999999) - current_rank),
                    block_distance(block_map[current_id], block_map[bid]),
                    bid,
                ),
            )
            continue
        weak_candidates = [
            bid
            for bid in unvisited
            if block_distance(block_map[current_id], block_map[bid]) <= WEAK_BRIDGE_METERS
        ]
        source = weak_candidates if weak_candidates else list(unvisited)
        current_rank = ranks.get(current_id, 0)
        current_id = min(
            source,
            key=lambda bid: (
                0 if ranks.get(bid, 999999) >= current_rank else 1,
                block_distance(block_map[current_id], block_map[bid]),
                abs(ranks.get(bid, 999999) - current_rank),
                bid,
            ),
        )
    return route


def route_cluster_blocks(
    cluster: dict[str, Any],
    block_map: dict[str, dict[str, Any]],
    adjacency: dict[str, list[dict[str, Any]]],
    entry_point: dict[str, float] | None,
) -> list[str]:
    components = cluster["components"]
    remaining = components[:]
    route: list[str] = []
    point = entry_point
    while remaining:
        if point:
            component = min(
                remaining,
                key=lambda item: haversine_m(point["lat"], point["lon"], item["centroid"]["lat"], item["centroid"]["lon"]),
            )
        else:
            min_lon = min(item["centroid"]["lon"] for item in remaining)
            max_lat = max(item["centroid"]["lat"] for item in remaining)
            component = min(remaining, key=lambda item: abs(item["centroid"]["lon"] - min_lon) + abs(max_lat - item["centroid"]["lat"]))
        remaining.remove(component)
        component_route = route_component_blocks(component, block_map, adjacency, point)
        route.extend(component_route)
        if component_route:
            point = block_map[component_route[-1]].get("centroid") or point
    return route


def route_link(
    left_id: str,
    right_id: str,
    block_map: dict[str, dict[str, Any]],
    best_edge_by_pair: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    left = block_map[left_id]
    right = block_map[right_id]
    pair = tuple(sorted((left_id, right_id)))
    edge = best_edge_by_pair.get(pair)
    distance = block_distance(left, right)
    if edge and edge.get("route_allowed"):
        link_type = edge["relation"]
    elif distance <= WEAK_BRIDGE_METERS:
        link_type = "weak_bridge"
    else:
        link_type = "over_500m_jump"
    left_center = left.get("centroid") or {}
    right_center = right.get("centroid") or {}
    return {
        "from": left_id,
        "to": right_id,
        "from_short": short_block_id(left_id),
        "to_short": short_block_id(right_id),
        "type": link_type,
        "distance": round(distance, 1),
        "coords": [
            [round(safe_number(left_center.get("lat")), 7), round(safe_number(left_center.get("lon")), 7)],
            [round(safe_number(right_center.get("lat")), 7), round(safe_number(right_center.get("lon")), 7)],
        ],
    }


def sort_records_along_axis(
    records: list[dict[str, Any]],
    previous_block: dict[str, Any] | None,
    current_block: dict[str, Any],
    next_block: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if len(records) <= 1:
        return records[:]
    current_center = current_block.get("centroid") or {}
    start = previous_block.get("centroid") if previous_block else None
    end = next_block.get("centroid") if next_block else None
    if start and end:
        ax = safe_number(end.get("lon")) - safe_number(start.get("lon"))
        ay = safe_number(end.get("lat")) - safe_number(start.get("lat"))
    elif end:
        ax = safe_number(end.get("lon")) - safe_number(current_center.get("lon"))
        ay = safe_number(end.get("lat")) - safe_number(current_center.get("lat"))
    elif start:
        ax = safe_number(current_center.get("lon")) - safe_number(start.get("lon"))
        ay = safe_number(current_center.get("lat")) - safe_number(start.get("lat"))
    else:
        ax, ay = 1.0, -1.0
    if abs(ax) + abs(ay) < 0.0000001:
        ax, ay = 1.0, -1.0
    scale = math.cos(math.radians(safe_number(current_center.get("lat"), 36.0)))

    def key(record: dict[str, Any]) -> tuple[float, int]:
        x = safe_number(record.get("lon")) * scale
        y = safe_number(record.get("lat"))
        projection = x * ax * scale + y * ay
        return projection, int(safe_number(record.get("id"), 0))

    return sorted(records, key=key)


def max_record_jump(records: list[dict[str, Any]]) -> float:
    best = 0.0
    for index in range(1, len(records)):
        best = max(best, record_distance(records[index - 1], records[index]))
    return best


def build_units_for_route(
    route: list[str],
    block_map: dict[str, dict[str, Any]],
    records_by_block: dict[str, list[dict[str, Any]]],
    target: float,
    min_target: int,
    max_target: int,
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for index, bid in enumerate(route):
        block = block_map[bid]
        previous_block = block_map.get(route[index - 1]) if index > 0 else None
        next_block = block_map.get(route[index + 1]) if index + 1 < len(route) else None
        records = sort_records_along_axis(records_by_block.get(bid, []), previous_block, block, next_block)
        if not records:
            continue
        block_orders = total_orders(records)
        if block_orders <= max_target:
            units.append(
                {
                    "unit_id": f"U{len(units) + 1}",
                    "block_id": bid,
                    "records": records,
                    "orders": block_orders,
                    "first": records[0],
                    "last": records[-1],
                    "internal_max_jump": max_record_jump(records),
                    "split_index": 1,
                    "split_count": 1,
                    "large_block_split": False,
                }
            )
            continue

        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_orders = 0
        for record in records:
            projected = current_orders + order_of(record)
            should_split = bool(current) and current_orders >= min_target and (
                projected > max_target or abs(current_orders - target) <= abs(projected - target)
            )
            if should_split:
                chunks.append(current)
                current = []
                current_orders = 0
            current.append(record)
            current_orders += order_of(record)
        if current:
            chunks.append(current)
        for split_index, chunk in enumerate(chunks, start=1):
            units.append(
                {
                    "unit_id": f"U{len(units) + 1}",
                    "block_id": bid,
                    "records": chunk,
                    "orders": total_orders(chunk),
                    "first": chunk[0],
                    "last": chunk[-1],
                    "internal_max_jump": max_record_jump(chunk),
                    "split_index": split_index,
                    "split_count": len(chunks),
                    "large_block_split": True,
                }
            )
    return units


def allocate_people_to_clusters(clusters: list[dict[str, Any]], people_count: int, target: float, min_target: int, max_target: int) -> None:
    if not clusters:
        return
    options: list[list[dict[str, Any]]] = []
    for cluster in clusters:
        orders = max(0, int(cluster["orders"]))
        unit_count = max(1, int(cluster.get("unit_count") or 1))
        max_people = min(unit_count, people_count)
        desired = orders / target if target else 1.0
        cluster_options: list[dict[str, Any]] = []
        for people in range(1, max_people + 1):
            average = orders / people if people else 0
            overflow = max(0.0, min_target - average) + max(0.0, average - max_target)
            deviation = average - target
            cost = overflow * overflow * people * 1_000_000 + overflow * people * 100_000_000 + deviation * deviation * people * 55
            cluster_options.append({"people": people, "cost": cost, "average": average})
        cluster["people_min"] = 1
        cluster["people_max"] = max_people
        cluster["people_desired"] = desired
        options.append(cluster_options)

    states: list[dict[int, dict[str, Any]]] = [dict() for _ in range(len(clusters) + 1)]
    states[0][0] = {"cost": 0.0, "previous_people": 0}
    for index, cluster_options in enumerate(options, start=1):
        for used_people, state in states[index - 1].items():
            for option in cluster_options:
                next_people = used_people + option["people"]
                if next_people > people_count:
                    continue
                cost = state["cost"] + option["cost"]
                existing = states[index].get(next_people)
                if not existing or cost < existing["cost"]:
                    states[index][next_people] = {
                        "cost": cost,
                        "previous_people": used_people,
                        "allocated": option["people"],
                    }

    final_people = people_count if people_count in states[len(clusters)] else None
    if final_people is None:
        possible = sorted(states[len(clusters)])
        final_people = possible[-1] if possible else 0

    allocations: list[int] = []
    cursor = final_people
    for index in range(len(clusters), 0, -1):
        state = states[index][cursor]
        allocations.insert(0, state["allocated"])
        cursor = state["previous_people"]

    for cluster, allocated in zip(clusters, allocations, strict=False):
        cluster["people_allocated"] = allocated


def unit_jump(left: dict[str, Any], right: dict[str, Any]) -> float:
    return record_distance(left["last"], right["first"])


def cut_units_for_people(
    units: list[dict[str, Any]],
    people_count: int,
    first_person_id: int,
    target: float,
    min_target: int,
    max_target: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    zones: list[dict[str, Any]] = []
    residual_records: list[dict[str, Any]] = []
    if people_count <= 0:
        for unit in units:
            residual_records.extend({**record, "residual_reason": "no_people_for_cluster"} for record in unit["records"])
        return zones, residual_records, first_person_id
    if not units:
        return zones, residual_records, first_person_id

    person_count = min(people_count, len(units))
    unit_count = len(units)
    prefix = [0]
    for unit in units:
        prefix.append(prefix[-1] + unit["orders"])

    states: list[dict[int, dict[str, Any]]] = [dict() for _ in range(person_count + 1)]
    states[0][0] = {"cost": 0.0, "previous": None}
    for person in range(1, person_count + 1):
        for start, previous_state in states[person - 1].items():
            segment_max_jump = 0.0
            for end in range(start + 1, unit_count + 1):
                unit = units[end - 1]
                segment_max_jump = max(segment_max_jump, safe_number(unit.get("internal_max_jump")))
                if end - 1 > start:
                    segment_max_jump = max(segment_max_jump, unit_jump(units[end - 2], unit))
                if segment_max_jump > WEAK_BRIDGE_METERS:
                    break
                orders = prefix[end] - prefix[start]
                deviation = orders - target
                overflow = max(0, min_target - orders) + max(0, orders - max_target)
                cost = (
                    previous_state["cost"]
                    + deviation * deviation * 55
                    + overflow * overflow * 1_000_000
                    + overflow * 100_000_000
                    + segment_max_jump * 20
                )
                existing = states[person].get(end)
                if not existing or cost < existing["cost"]:
                    states[person][end] = {"cost": cost, "previous": start}

    assigned_until = unit_count
    used_people = person_count
    if assigned_until not in states[used_people]:
        best = None
        for person in range(1, person_count + 1):
            for end, state in states[person].items():
                if not best or end > best[0] or (end == best[0] and state["cost"] < best[2]["cost"]):
                    best = (end, person, state)
        if not best:
            for unit in units:
                residual_records.extend({**record, "residual_reason": "no_feasible_cut"} for record in unit["records"])
            return zones, residual_records, first_person_id
        assigned_until, used_people, _ = best

    segments: list[list[dict[str, Any]]] = []
    end = assigned_until
    for person in range(used_people, 0, -1):
        state = states[person][end]
        start = state["previous"]
        segments.insert(0, units[start:end])
        end = start

    next_person_id = first_person_id
    for segment in segments:
        records = [record for unit in segment for record in unit["records"]]
        orders = total_orders(records)
        block_ids = []
        for unit in segment:
            if unit["block_id"] not in block_ids:
                block_ids.append(unit["block_id"])
        zones.append(
            {
                "person_id": next_person_id,
                "orders": orders,
                "address_count": len(records),
                "block_count": len(block_ids),
                "block_ids": block_ids,
                "record_ids": [record.get("id") for record in records],
                "max_jump": round(max_record_jump(records), 1),
                "deviation_pct": round(((orders - target) / target * 100), 1) if target else 0,
                "band_ok": min_target <= orders <= max_target,
                "color": ZONE_COLORS[(next_person_id - 1) % len(ZONE_COLORS)],
            }
        )
        for record in records:
            record["person_id"] = next_person_id
        next_person_id += 1

    for unit in units[assigned_until:]:
        residual_records.extend({**record, "residual_reason": "over_500m_or_no_cut"} for record in unit["records"])
    return zones, residual_records, next_person_id


def build_debug_payload(scope: dict[str, Any], summary: dict[str, Any] | None, people_count: int) -> dict[str, Any]:
    raw_blocks = scope.get("blocks") or []
    raw_records = scope.get("records") or []
    block_map = {block_id(block): {**block} for block in raw_blocks if block_id(block)}
    records_by_block: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in raw_records:
        bid = record_block_id(record)
        if bid in block_map:
            records_by_block[bid].append({**record, "block_id": bid})

    for bid, block in block_map.items():
        records = records_by_block.get(bid, [])
        block["orders"] = total_orders(records) if records else int(safe_number(block.get("orders")))
        block["address_count"] = len(records) if records else int(safe_number(block.get("address_count")))

    active_ids = {bid for bid, records in records_by_block.items() if records and total_orders(records) > 0}
    active_blocks = [block_map[bid] for bid in active_ids]
    adjacency, route_neighbor_edges, blocked_neighbor_edges, best_edge_by_pair = build_route_graph(list(block_map.values()))
    components = build_components(active_ids, block_map, adjacency)
    jump_clusters = build_jump_clusters(components)

    cluster_id_by_block: dict[str, str] = {}
    component_id_by_block: dict[str, str] = {}
    for cluster in jump_clusters:
        for bid in cluster["block_ids"]:
            cluster_id_by_block[bid] = cluster["cluster_id"]
        for component in cluster["components"]:
            for bid in component["block_ids"]:
                component_id_by_block[bid] = component["component_id"]

    initial_total_orders = total_orders(raw_records)
    initial_target = initial_total_orders / people_count if people_count else 0
    initial_min_target = math.floor(initial_target * (1 - BAND_TOLERANCE))

    ordered_jump_clusters = sorted(
        jump_clusters,
        key=lambda cluster: (
            cluster["centroid"]["lon"],
            -cluster["centroid"]["lat"],
            -cluster["orders"],
            cluster["cluster_id"],
        ),
    )

    global_route: list[str] = []
    route_links: list[dict[str, Any]] = []
    route_groups: list[dict[str, Any]] = []
    entry_point: dict[str, float] | None = None
    for cluster in ordered_jump_clusters:
        route = route_cluster_blocks(cluster, block_map, adjacency, entry_point)
        if global_route and route:
            route_links.append(route_link(global_route[-1], route[0], block_map, best_edge_by_pair))
        local_links = [route_link(route[index - 1], route[index], block_map, best_edge_by_pair) for index in range(1, len(route))]
        route_links.extend(local_links)

        group_route: list[str] = []
        group_index = 1
        for index, bid in enumerate(route):
            if index > 0:
                previous_link = local_links[index - 1]
                if previous_link["distance"] > WEAK_BRIDGE_METERS or previous_link["type"] == "over_500m_jump":
                    if group_route:
                        route_groups.append(
                            {
                                "cluster_id": f"{cluster['cluster_id']}-R{group_index}",
                                "parent_cluster_id": cluster["cluster_id"],
                                "route": group_route,
                                "block_ids": group_route[:],
                                "blocks": [block_map[item] for item in group_route],
                                "orders": sum(int(safe_number(block_map[item].get("orders"))) for item in group_route),
                                "component_count": len({component_id_by_block.get(item, "") for item in group_route if component_id_by_block.get(item, "")}),
                            }
                        )
                        group_index += 1
                    group_route = []
            group_route.append(bid)
        if group_route:
            route_groups.append(
                {
                    "cluster_id": f"{cluster['cluster_id']}-R{group_index}",
                    "parent_cluster_id": cluster["cluster_id"],
                    "route": group_route,
                    "block_ids": group_route[:],
                    "blocks": [block_map[item] for item in group_route],
                    "orders": sum(int(safe_number(block_map[item].get("orders"))) for item in group_route),
                    "component_count": len({component_id_by_block.get(item, "") for item in group_route if component_id_by_block.get(item, "")}),
                }
            )
        global_route.extend(route)
        if route:
            entry_point = block_map[route[-1]].get("centroid") or entry_point

    route_index_by_block = {bid: index + 1 for index, bid in enumerate(global_route)}

    for group in route_groups:
        group["centroid"] = weighted_centroid(group["blocks"])

    island_clusters = [group for group in route_groups if group["orders"] < initial_min_target]
    assignable_clusters = [group for group in route_groups if group["orders"] >= initial_min_target]
    island_block_ids = {bid for cluster in island_clusters for bid in cluster["block_ids"]}
    island_records = [
        {**record, "residual_reason": "island_route_chunk_preexcluded"}
        for bid in island_block_ids
        for record in records_by_block.get(bid, [])
    ]

    route_group_id_by_block: dict[str, str] = {}
    for group in route_groups:
        for bid in group["block_ids"]:
            route_group_id_by_block[bid] = group["cluster_id"]

    assignable_orders = sum(cluster["orders"] for cluster in assignable_clusters)
    target = assignable_orders / people_count if people_count else 0
    min_target = math.floor(target * (1 - BAND_TOLERANCE))
    max_target = math.ceil(target * (1 + BAND_TOLERANCE))

    ordered_clusters = sorted(
        assignable_clusters,
        key=lambda cluster: (
            route_index_by_block.get(cluster["route"][0], 999999),
            cluster["cluster_id"],
        ),
    )

    for cluster in ordered_clusters:
        units = build_units_for_route(cluster["route"], block_map, records_by_block, target, min_target, max_target)
        cluster["units"] = units
        cluster["unit_count"] = len(units)

    allocate_people_to_clusters(ordered_clusters, people_count, target, min_target, max_target)

    zones: list[dict[str, Any]] = []
    residual_records = island_records[:]
    person_cursor = 1
    for cluster in ordered_clusters:
        cluster_zones, cluster_residuals, person_cursor = cut_units_for_people(
            cluster.get("units") or [],
            int(cluster.get("people_allocated") or 0),
            person_cursor,
            target,
            min_target,
            max_target,
        )
        for zone in cluster_zones:
            zone["cluster_id"] = cluster["cluster_id"]
        zones.extend(cluster_zones)
        residual_records.extend(cluster_residuals)

    person_by_record_id: dict[Any, int] = {}
    for zone in zones:
        for record_id in zone["record_ids"]:
            person_by_record_id[record_id] = zone["person_id"]

    residual_ids = {record.get("id") for record in residual_records}
    people_by_block: dict[str, set[int]] = defaultdict(set)
    residual_orders_by_block: Counter[str] = Counter()
    for bid, records in records_by_block.items():
        for record in records:
            person_id = person_by_record_id.get(record.get("id"))
            if person_id:
                people_by_block[bid].add(person_id)
            if record.get("id") in residual_ids:
                residual_orders_by_block[bid] += order_of(record)

    features: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    for block in raw_blocks:
        bid = block_id(block)
        people = sorted(people_by_block.get(bid, set()))
        primary_person = people[0] if len(people) == 1 else None
        route_index = route_index_by_block.get(bid)
        is_island = bid in island_block_ids
        props = {
            "block_id": bid,
            "short_id": short_block_id(bid),
            "orders": int(safe_number(block_map.get(bid, block).get("orders"))),
            "address_count": int(safe_number(block_map.get(bid, block).get("address_count"))),
            "parcel_count": int(safe_number(block.get("parcel_count"))),
            "dong": block.get("legal_dong_name") or block.get("dong") or "",
            "road_stem": block.get("road_stem") or "",
            "component_id": component_id_by_block.get(bid, ""),
            "cluster_id": cluster_id_by_block.get(bid, ""),
            "route_group_id": route_group_id_by_block.get(bid, ""),
            "route_index": route_index,
            "person_id": primary_person,
            "people": people,
            "mixed": len(people) > 1,
            "island": is_island,
            "residual_orders": int(residual_orders_by_block.get(bid, 0)),
            "route_neighbor_count": sum(1 for edge in adjacency.get(bid) or [] if str(edge.get("neighbor_block_id") or "") in active_ids),
        }
        features.append(
            {
                "type": "Feature",
                "geometry": normalize_geometry(block.get("geometry") or {"type": "MultiPolygon", "coordinates": []}),
                "properties": props,
            }
        )
        center = block.get("centroid") or {}
        lat = safe_number(center.get("lat"), math.nan)
        lon = safe_number(center.get("lon"), math.nan)
        if math.isfinite(lat) and math.isfinite(lon) and (route_index or is_island):
            labels.append(
                {
                    "lat": round(lat, 7),
                    "lon": round(lon, 7),
                    "text": "섬" if is_island else str(route_index),
                    "sub": component_id_by_block.get(bid, ""),
                    "person_id": primary_person,
                }
            )

    records_payload: list[dict[str, Any]] = []
    for record in raw_records:
        bid = record_block_id(record)
        lat = safe_number(record.get("lat"), math.nan)
        lon = safe_number(record.get("lon"), math.nan)
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        record_id = record.get("id")
        residual = record_id in residual_ids
        records_payload.append(
            {
                "id": record_id,
                "lat": round(lat, 7),
                "lon": round(lon, 7),
                "orders": order_of(record),
                "block_id": bid,
                "short_id": short_block_id(bid),
                "person_id": person_by_record_id.get(record_id),
                "residual": residual,
                "residual_reason": next((item.get("residual_reason") for item in residual_records if item.get("id") == record_id), ""),
                "address": record.get("road_address") or record.get("jibun_address") or record.get("address") or "",
            }
        )

    assigned_orders = sum(zone["orders"] for zone in zones)
    residual_orders = total_orders(residual_records)
    route_jump_over_500 = [link for link in route_links if link["type"] == "over_500m_jump" or link["distance"] > WEAK_BRIDGE_METERS]
    band_violations = [zone for zone in zones if zone["orders"] > 0 and not zone["band_ok"]]
    mixed_blocks = [feature["properties"] for feature in features if feature["properties"]["mixed"]]
    relation_counts = Counter(edge["relation"] for edge in route_neighbor_edges)
    blocked_counts = Counter(edge["relation"] for edge in blocked_neighbor_edges)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "center": scope.get("center") or "",
        "month": scope.get("month") or "",
        "people_count": people_count,
        "bbox": bbox_from_blocks(raw_blocks),
        "features": features,
        "records": records_payload,
        "labels": labels,
        "route_neighbor_edges": route_neighbor_edges,
        "blocked_neighbor_edges": blocked_neighbor_edges,
        "route_links": route_links,
        "zones": zones,
        "clusters": [
            {
                "cluster_id": cluster["cluster_id"],
                "parent_cluster_id": cluster.get("parent_cluster_id", ""),
                "orders": int(cluster["orders"]),
                "block_count": len(cluster["block_ids"]),
                "component_count": int(cluster.get("component_count") or 0),
                "island": cluster in island_clusters,
                "people_allocated": int(cluster.get("people_allocated") or 0),
                "unit_count": int(cluster.get("unit_count") or 0),
            }
            for cluster in route_groups
        ],
        "summary": {
            "source_blocks": len(raw_blocks),
            "source_records": len(raw_records),
            "source_orders": initial_total_orders,
            "active_blocks": len(active_ids),
            "route_components": len(components),
            "jump_clusters": len(jump_clusters),
            "route_groups": len(route_groups),
            "island_clusters": len(island_clusters),
            "island_orders": total_orders(island_records),
            "assignable_orders": int(assignable_orders),
            "assigned_orders": int(assigned_orders),
            "residual_orders": int(residual_orders),
            "target": round(target, 1),
            "min_target": int(min_target),
            "max_target": int(max_target),
            "route_links": len(route_links),
            "route_jump_over_500": len(route_jump_over_500),
            "route_neighbor_edges": len(route_neighbor_edges),
            "blocked_neighbor_edges": len(blocked_neighbor_edges),
            "band_violations": len(band_violations),
            "mixed_blocks": len(mixed_blocks),
            "empty_people": max(0, people_count - len(zones)),
            "relation_counts": dict(relation_counts),
            "blocked_counts": dict(blocked_counts),
            "preprocess_summary": summary or {},
        },
        "validation": {
            "route_jump_over_500": route_jump_over_500[:80],
            "band_violations": band_violations,
            "mixed_blocks": mixed_blocks[:80],
            "island_clusters": island_clusters,
        },
    }


HTML_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <link rel="preconnect" href="https://unpkg.com">
  <link rel="preconnect" href="https://tile.openstreetmap.org">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    :root {
      --panel: rgba(255,255,255,.82);
      --border: rgba(15,23,42,.16);
      --text: #0f172a;
      --muted: #475569;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; font-family: Arial, "Malgun Gothic", sans-serif; color: var(--text); }
    #map { position: fixed; inset: 0; }
    .panel {
      position: fixed;
      z-index: 900;
      top: 14px;
      left: 14px;
      width: min(410px, calc(100vw - 28px));
      max-height: calc(100vh - 28px);
      overflow: auto;
      padding: 14px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 0 16px 42px rgba(15,23,42,.20);
      backdrop-filter: blur(8px);
    }
    h1 { margin: 0 0 5px; font-size: 18px; line-height: 1.35; }
    .subtitle { margin-bottom: 12px; color: var(--muted); font-size: 12px; line-height: 1.45; }
    .stats { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 6px; margin-bottom: 10px; }
    .stat { min-width: 0; padding: 7px; border: 1px solid var(--border); border-radius: 6px; background: rgba(255,255,255,.56); }
    .stat span { display: block; margin-bottom: 3px; color: var(--muted); font-size: 10px; }
    .stat strong { display: block; font-size: 15px; line-height: 1.15; overflow-wrap: anywhere; }
    .controls { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
    .controls label {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 6px 7px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: rgba(255,255,255,.52);
      font-size: 11px;
      cursor: pointer;
    }
    .tabs { display: flex; gap: 5px; margin: 8px 0; }
    .tab {
      flex: 1;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: rgba(255,255,255,.5);
      padding: 7px;
      font-size: 11px;
      cursor: pointer;
    }
    .tab.active { background: #0f172a; color: #fff; border-color: #0f172a; }
    .list { display: grid; gap: 5px; }
    .row {
      width: 100%;
      text-align: left;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: rgba(255,255,255,.62);
      padding: 8px;
      cursor: pointer;
    }
    .row:hover { border-color: #334155; background: rgba(255,255,255,.88); }
    .row strong { display: block; margin-bottom: 3px; font-size: 12px; }
    .row span { display: block; color: var(--muted); font-size: 11px; line-height: 1.36; }
    .route-label {
      min-width: 28px;
      transform: translate(-50%, -50%);
      border: 1px solid rgba(15,23,42,.24);
      border-radius: 4px;
      padding: 1px 4px;
      background: rgba(255,255,255,.82);
      color: #0f172a;
      font-size: 10px;
      font-weight: 700;
      text-align: center;
      white-space: nowrap;
      box-shadow: 0 1px 3px rgba(15,23,42,.14);
    }
    .leaflet-popup-content { min-width: 210px; font-size: 12px; line-height: 1.5; }
    @media (max-width: 760px) {
      .panel { top: auto; bottom: 12px; left: 12px; width: calc(100vw - 24px); max-height: 45vh; }
    }
  </style>
</head>
<body>
  <div id="map"></div>
  <aside class="panel">
    <h1>__TITLE__</h1>
    <div class="subtitle">route용 neighbor만으로 component를 만들고, 섬오더 선배제 후 block-unit 기준으로 순차 cut한 검증 화면입니다.</div>
    <div id="stats" class="stats"></div>
    <div class="controls">
      <label><input id="togglePoints" type="checkbox" checked> 오더점</label>
      <label><input id="toggleLabels" type="checkbox" checked> route번호</label>
      <label><input id="toggleRoute" type="checkbox" checked> 순로선</label>
      <label><input id="toggleRouteNeighbors" type="checkbox"> route neighbor</label>
      <label><input id="toggleBlocked" type="checkbox"> 제외 edge</label>
    </div>
    <div class="tabs">
      <button class="tab active" data-tab="people">인원</button>
      <button class="tab" data-tab="jumps">점프</button>
      <button class="tab" data-tab="clusters">클러스터</button>
    </div>
    <div id="list" class="list"></div>
  </aside>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const DATA = __DATA__;
    const COLORS = __COLORS__;
    const map = L.map("map", { preferCanvas: true, zoomControl: false });
    L.control.zoom({ position: "bottomright" }).addTo(map);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 20,
      attribution: "&copy; OpenStreetMap contributors"
    }).addTo(map);
    const fmt = new Intl.NumberFormat("ko-KR");
    map.fitBounds([[DATA.bbox[1], DATA.bbox[0]], [DATA.bbox[3], DATA.bbox[2]]], { padding: [24, 24] });

    const layerByBlock = new Map();
    function personColor(personId) {
      return personId ? COLORS[(personId - 1) % COLORS.length] : "#94a3b8";
    }
    function blockFill(props) {
      if (props.island || props.residual_orders > 0 && !props.person_id) return "#71717a";
      if (props.mixed) return "#f59e0b";
      if (props.person_id) return personColor(props.person_id);
      return "#e2e8f0";
    }
    function blockPopup(props) {
      const people = props.people && props.people.length ? props.people.join(", ") : "-";
      return `
        <div><b>블럭:</b> ${props.short_id}</div>
        <div><b>route:</b> ${props.route_index || "-"} / <b>component:</b> ${props.component_id || "-"}</div>
        <div><b>cluster:</b> ${props.cluster_id || "-"} / <b>route group:</b> ${props.route_group_id || "-"}</div>
        <div><b>인원:</b> ${people}${props.mixed ? " (split)" : ""}</div>
        <div><b>오더:</b> ${fmt.format(props.orders || 0)}건 / <b>주소:</b> ${fmt.format(props.address_count || 0)}개</div>
        <div><b>필지:</b> ${fmt.format(props.parcel_count || 0)}개 / <b>route neighbor:</b> ${fmt.format(props.route_neighbor_count || 0)}개</div>
        <div><b>동:</b> ${props.dong || ""}</div>
        ${props.island ? "<div><b>잔여:</b> 섬오더 선배제</div>" : ""}
        ${props.residual_orders ? `<div><b>잔여오더:</b> ${fmt.format(props.residual_orders)}건</div>` : ""}
      `;
    }
    const blockLayer = L.geoJSON(DATA.features, {
      style: feature => {
        const props = feature.properties;
        const color = props.island ? "#52525b" : props.mixed ? "#d97706" : personColor(props.person_id) || "#64748b";
        return {
          color,
          fillColor: blockFill(props),
          weight: props.mixed || props.island ? 2.6 : 1.4,
          opacity: .95,
          fillOpacity: props.person_id || props.mixed || props.island ? .34 : .10
        };
      },
      onEachFeature: (feature, layer) => {
        layerByBlock.set(feature.properties.block_id, layer);
        layer.bindPopup(blockPopup(feature.properties));
        layer.on("mouseover", () => layer.setStyle({ weight: 3.4, fillOpacity: .52 }));
        layer.on("mouseout", () => blockLayer.resetStyle(layer));
      }
    }).addTo(map);

    const pointLayer = L.layerGroup();
    for (const record of DATA.records) {
      const color = record.residual ? "#71717a" : personColor(record.person_id);
      L.circleMarker([record.lat, record.lon], {
        radius: Math.max(3, Math.min(7, Math.sqrt(record.orders || 1) + 2)),
        color: "#0f172a",
        fillColor: color,
        fillOpacity: record.residual ? .72 : .86,
        weight: .7
      }).bindPopup(`
        <div><b>오더:</b> ${fmt.format(record.orders || 0)}건</div>
        <div><b>인원:</b> ${record.person_id || "-"}</div>
        <div><b>블럭:</b> ${record.short_id}</div>
        ${record.residual ? `<div><b>잔여:</b> ${record.residual_reason}</div>` : ""}
        <div>${record.address || ""}</div>
      `).addTo(pointLayer);
    }
    pointLayer.addTo(map);

    const labelLayer = L.layerGroup();
    for (const label of DATA.labels) {
      L.marker([label.lat, label.lon], {
        interactive: false,
        icon: L.divIcon({
          className: "",
          html: `<div class="route-label">${label.text}<br>${label.sub || ""}</div>`,
          iconSize: [1, 1],
          iconAnchor: [0, 0]
        })
      }).addTo(labelLayer);
    }
    labelLayer.addTo(map);

    const routeLineLayer = L.layerGroup();
    for (const link of DATA.route_links) {
      const over = link.type === "over_500m_jump" || link.distance > 500;
      const weak = link.type === "weak_bridge";
      L.polyline(link.coords, {
        color: over ? "#dc2626" : weak ? "#f59e0b" : "#111827",
        weight: over ? 3.2 : weak ? 2.2 : 1.5,
        opacity: over ? .95 : .70,
        dashArray: over ? "8 6" : weak ? "5 5" : ""
      }).bindTooltip(`${link.from_short} → ${link.to_short} / ${link.type} / ${fmt.format(link.distance)}m`).addTo(routeLineLayer);
    }
    routeLineLayer.addTo(map);

    const routeNeighborLayer = L.layerGroup();
    for (const edge of DATA.route_neighbor_edges) {
      L.polyline(edge.coords, {
        color: edge.relation === "touches" ? "#16a34a" : "#0891b2",
        weight: edge.relation === "touches" ? 1.5 : 1,
        opacity: edge.relation === "touches" ? .74 : .42,
        dashArray: edge.relation === "touches" ? "" : "4 4"
      }).bindTooltip(`${edge.relation} / 공유 ${edge.shared.toFixed(1)}m / gap ${edge.boundary.toFixed(1)}m`).addTo(routeNeighborLayer);
    }

    const blockedLayer = L.layerGroup();
    for (const edge of DATA.blocked_neighbor_edges) {
      L.polyline(edge.coords, {
        color: edge.relation === "across_transport_barrier" ? "#dc2626" : "#64748b",
        weight: 1,
        opacity: .32,
        dashArray: "3 5"
      }).bindTooltip(`제외: ${edge.relation} / ${edge.distance.toFixed(1)}m`).addTo(blockedLayer);
    }

    function renderStats() {
      const s = DATA.summary;
      const rows = [
        ["목표", `${fmt.format(s.target)}건`],
        ["허용", `${fmt.format(s.min_target)}-${fmt.format(s.max_target)}`],
        ["배정", `${fmt.format(s.assigned_orders)}건`],
        ["잔여", `${fmt.format(s.residual_orders)}건`],
        ["component", fmt.format(s.route_components)],
        ["cluster", fmt.format(s.jump_clusters)],
        ["섬오더", `${fmt.format(s.island_orders)}건`],
        ["500m점프", fmt.format(s.route_jump_over_500)],
        ["색섞임블럭", fmt.format(s.mixed_blocks)]
      ];
      document.getElementById("stats").innerHTML = rows.map(([label, value]) => `
        <div class="stat"><span>${label}</span><strong>${value}</strong></div>
      `).join("");
    }

    function zoomBlocks(blockIds) {
      const layers = blockIds.map(id => layerByBlock.get(id)).filter(Boolean);
      if (!layers.length) return;
      map.fitBounds(L.featureGroup(layers).getBounds(), { padding: [42, 42], maxZoom: 18 });
      layers[0].openPopup();
    }

    function renderPeople() {
      document.getElementById("list").innerHTML = DATA.zones.map(zone => `
        <button class="row" data-blocks="${zone.block_ids.join("|")}">
          <strong>인원 ${zone.person_id} · ${fmt.format(zone.orders)}건 · ${zone.band_ok ? "OK" : "범위밖"}</strong>
          <span>${zone.cluster_id} · ${fmt.format(zone.address_count)}주소 · ${fmt.format(zone.block_count)}블럭 · 목표대비 ${zone.deviation_pct >= 0 ? "+" : ""}${zone.deviation_pct}% · 최대점프 ${fmt.format(zone.max_jump)}m</span>
        </button>
      `).join("");
      document.querySelectorAll("[data-blocks]").forEach(button => {
        button.addEventListener("click", () => zoomBlocks(button.dataset.blocks.split("|")));
      });
    }

    function renderJumps() {
      const items = DATA.validation.route_jump_over_500 || [];
      document.getElementById("list").innerHTML = items.length ? items.map(link => `
        <button class="row" data-blocks="${link.from}|${link.to}">
          <strong>${link.from_short} → ${link.to_short} · ${fmt.format(link.distance)}m</strong>
          <span>${link.type}</span>
        </button>
      `).join("") : `<div class="row"><strong>500m 초과 순로 점프 없음</strong><span>현재 route 상에서 강제 점프는 없습니다.</span></div>`;
      document.querySelectorAll("[data-blocks]").forEach(button => {
        button.addEventListener("click", () => zoomBlocks(button.dataset.blocks.split("|")));
      });
    }

    function renderClusters() {
      document.getElementById("list").innerHTML = DATA.clusters
        .sort((a, b) => b.orders - a.orders)
        .map(cluster => `
          <div class="row">
            <strong>${cluster.cluster_id} · ${fmt.format(cluster.orders)}건 · ${cluster.island ? "섬/500m chunk 제외" : `${cluster.people_allocated}명`}</strong>
            <span>parent ${cluster.parent_cluster_id || "-"} · ${fmt.format(cluster.block_count)}블럭 · ${fmt.format(cluster.component_count)}component · ${fmt.format(cluster.unit_count)}unit</span>
          </div>
        `).join("");
    }

    function selectTab(name) {
      document.querySelectorAll(".tab").forEach(tab => tab.classList.toggle("active", tab.dataset.tab === name));
      if (name === "people") renderPeople();
      if (name === "jumps") renderJumps();
      if (name === "clusters") renderClusters();
    }

    document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => selectTab(tab.dataset.tab)));
    document.getElementById("togglePoints").addEventListener("change", event => event.target.checked ? pointLayer.addTo(map) : pointLayer.remove());
    document.getElementById("toggleLabels").addEventListener("change", event => event.target.checked ? labelLayer.addTo(map) : labelLayer.remove());
    document.getElementById("toggleRoute").addEventListener("change", event => event.target.checked ? routeLineLayer.addTo(map) : routeLineLayer.remove());
    document.getElementById("toggleRouteNeighbors").addEventListener("change", event => event.target.checked ? routeNeighborLayer.addTo(map) : routeNeighborLayer.remove());
    document.getElementById("toggleBlocked").addEventListener("change", event => event.target.checked ? blockedLayer.addTo(map) : blockedLayer.remove());

    renderStats();
    selectTab("people");
  </script>
</body>
</html>
"""


def write_debug_map(scope: dict[str, Any], summary: dict[str, Any] | None, output_dir: Path, people_count: int) -> Path:
    payload = build_debug_payload(scope, summary, people_count)
    title = f"{payload['center']} {payload['month']} route 검증"
    page = (
        HTML_TEMPLATE
        .replace("__TITLE__", html.escape(title, quote=True))
        .replace("__DATA__", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        .replace("__COLORS__", json.dumps(ZONE_COLORS, ensure_ascii=False))
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"debug_route_{payload['center']}.html"
    path.write_text(page, encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate route-neighbor and block-unit assignment debug maps.")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--centers", nargs="+", default=list(DEFAULT_CENTERS))
    parser.add_argument("--people", type=int, default=None, help="Override people count for all selected centers.")
    args = parser.parse_args()

    payload = json.loads(args.data.read_text(encoding="utf-8"))
    scopes = payload.get("scopes") or {}
    summaries = payload.get("summaries") or {}
    requested = {str(center).upper() for center in args.centers}
    written: list[Path] = []
    result_summaries: list[tuple[str, dict[str, Any]]] = []

    for key, scope in scopes.items():
        center = str(scope.get("center") or "").upper()
        if center not in requested:
            continue
        people_count = args.people or PEOPLE_COUNTS.get(center, 33)
        path = write_debug_map(scope, summaries.get(key), args.output_dir, people_count)
        written.append(path)
        text = path.read_text(encoding="utf-8")
        marker = "const DATA = "
        start = text.find(marker)
        if start >= 0:
            json_start = start + len(marker)
            json_end = text.find(";\n    const COLORS", json_start)
            if json_end >= 0:
                result_summaries.append((center, json.loads(text[json_start:json_end])["summary"]))

    missing = requested - {path.stem.rsplit("_", 1)[-1].upper() for path in written}
    if missing:
        raise SystemExit(f"Missing center data: {', '.join(sorted(missing))}")

    for path in written:
        print(path)
    for center, summary in result_summaries:
        print(
            center,
            f"target={summary['target']}",
            f"range={summary['min_target']}-{summary['max_target']}",
            f"assigned={summary['assigned_orders']}",
            f"residual={summary['residual_orders']}",
            f"components={summary['route_components']}",
            f"clusters={summary['jump_clusters']}",
            f"route_groups={summary['route_groups']}",
            f"island_orders={summary['island_orders']}",
            f"route_jumps_500={summary['route_jump_over_500']}",
            f"mixed_blocks={summary['mixed_blocks']}",
            f"band_violations={summary['band_violations']}",
        )


if __name__ == "__main__":
    main()
