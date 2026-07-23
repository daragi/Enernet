from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from pyproj import Transformer


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = BASE_DIR / "processed_assignment_blocks.json"
DEFAULT_OUTPUT_HTML = BASE_DIR / "boundary_map" / "h074_33_sequential_assignment.html"
DEFAULT_OUTPUT_JSON = BASE_DIR / "boundary_map" / "h074_33_sequential_assignment_summary.json"
SCOPE_KEY = "2026.07|H074"
PEOPLE_COUNT = 33
REGULAR_JUMP_LIMIT_METERS = 500.0
SMALL_ISLAND_ORDERS = 24
SMALL_ISOLATED_BLOCK_ORDERS = 24
FAR_COMPONENT_ORDERS = 40
SMALL_ISLAND_DISTANCE_METERS = 500.0


ZONE_COLORS = [
    "#2563eb", "#dc2626", "#16a34a", "#f97316", "#7c3aed",
    "#0891b2", "#db2777", "#ca8a04", "#0f766e", "#4f46e5",
    "#65a30d", "#e11d48", "#06b6d4", "#9333ea", "#ea580c",
    "#22c55e", "#be123c", "#0284c7", "#a16207", "#8b5cf6",
    "#14b8a6", "#f59e0b", "#6366f1", "#ec4899", "#15803d",
    "#0ea5e9", "#c026d3", "#047857", "#b45309", "#1d4ed8",
    "#f43f5e", "#10b981", "#d97706",
]


def safe_number(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def order_of(record: dict[str, Any]) -> int:
    return int(max(0, safe_number(record.get("orders"))))


def distance_xy(left: dict[str, Any] | tuple[float, float], right: dict[str, Any] | tuple[float, float]) -> float:
    if isinstance(left, tuple):
        lx, ly = left
    else:
        lx, ly = safe_number(left.get("x")), safe_number(left.get("y"))
    if isinstance(right, tuple):
        rx, ry = right
    else:
        rx, ry = safe_number(right.get("x")), safe_number(right.get("y"))
    return math.hypot(lx - rx, ly - ry)


def transform_scope(scope: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:5186", always_xy=True)
    blocks: dict[str, dict[str, Any]] = {}
    for block in scope.get("blocks") or []:
        centroid = block.get("centroid") or {}
        x, y = transformer.transform(float(centroid.get("lon")), float(centroid.get("lat")))
        next_block = dict(block)
        next_block["x"] = float(x)
        next_block["y"] = float(y)
        next_block["records"] = []
        blocks[next_block["block_id"]] = next_block

    records: list[dict[str, Any]] = []
    for record in scope.get("records") or []:
        lon = safe_number(record.get("lon"), math.nan)
        lat = safe_number(record.get("lat"), math.nan)
        if not (math.isfinite(lon) and math.isfinite(lat)):
            continue
        x, y = transformer.transform(lon, lat)
        next_record = dict(record)
        next_record["x"] = float(x)
        next_record["y"] = float(y)
        records.append(next_record)
        block_id = next_record.get("assignment_block_id") or next_record.get("block_id")
        if block_id in blocks:
            blocks[block_id]["records"].append(next_record)

    return blocks, records


def build_graph(blocks: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
    graph = {block_id: set() for block_id in blocks}
    for block_id, block in blocks.items():
        for edge in block.get("neighbors") or []:
            neighbor_id = edge.get("neighbor_block_id")
            if neighbor_id in blocks:
                graph[block_id].add(neighbor_id)
                graph[neighbor_id].add(block_id)
    return graph


def connected_components(blocks: dict[str, dict[str, Any]], graph: dict[str, set[str]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    components: list[dict[str, Any]] = []
    for block_id in blocks:
        if block_id in seen:
            continue
        queue = deque([block_id])
        seen.add(block_id)
        ids: list[str] = []
        while queue:
            current = queue.popleft()
            ids.append(current)
            for neighbor in graph[current]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        orders = sum(int(blocks[item].get("orders") or 0) for item in ids)
        addresses = sum(int(blocks[item].get("address_count") or 0) for item in ids)
        total_weight = sum(max(1, int(blocks[item].get("orders") or 0)) for item in ids) or 1
        x = sum(blocks[item]["x"] * max(1, int(blocks[item].get("orders") or 0)) for item in ids) / total_weight
        y = sum(blocks[item]["y"] * max(1, int(blocks[item].get("orders") or 0)) for item in ids) / total_weight
        components.append({"ids": ids, "orders": orders, "addresses": addresses, "x": x, "y": y})
    return sorted(components, key=lambda item: item["orders"], reverse=True)


def component_distance(left: dict[str, Any], right: dict[str, Any], blocks: dict[str, dict[str, Any]]) -> float:
    best = math.inf
    for left_id in left["ids"]:
        for right_id in right["ids"]:
            best = min(best, distance_xy(blocks[left_id], blocks[right_id]))
    return best


def classify_residual_components(
    components: list[dict[str, Any]],
    blocks: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str], list[dict[str, Any]]]:
    if not components:
        return [], set(), []
    main = components[0]
    route_components: list[dict[str, Any]] = []
    residual_block_ids: set[str] = set()
    residual_reasons: list[dict[str, Any]] = []
    for component in components:
        nearest_main = 0.0 if component is main else component_distance(component, main, blocks)
        isolated_small_block = (
            component is not main
            and len(component["ids"]) == 1
            and component["orders"] <= SMALL_ISOLATED_BLOCK_ORDERS
        )
        small_far_component = (
            component is not main
            and component["orders"] <= SMALL_ISLAND_ORDERS
            and nearest_main > SMALL_ISLAND_DISTANCE_METERS
        )
        medium_far_component = (
            component is not main
            and component["orders"] <= FAR_COMPONENT_ORDERS
            and nearest_main > SMALL_ISLAND_DISTANCE_METERS * 3.0
        )
        if isolated_small_block or small_far_component or medium_far_component:
            residual_block_ids.update(component["ids"])
            if isolated_small_block:
                reason = "small_isolated_block"
            elif medium_far_component:
                reason = "medium_far_component"
            else:
                reason = "small_far_island_component"
            residual_reasons.append(
                {
                    "reason": reason,
                    "orders": component["orders"],
                    "addresses": component["addresses"],
                    "blocks": len(component["ids"]),
                    "nearest_main_meters": round(nearest_main, 1),
                    "block_ids": component["ids"],
                }
            )
        else:
            route_components.append(component)
    return route_components, residual_block_ids, residual_reasons


def choose_start_block(block_ids: set[str], blocks: dict[str, dict[str, Any]], entry: tuple[float, float] | None) -> str:
    if entry is not None:
        return min(block_ids, key=lambda block_id: (distance_xy(entry, blocks[block_id]), block_id))
    min_x = min(blocks[block_id]["x"] for block_id in block_ids)
    max_y = max(blocks[block_id]["y"] for block_id in block_ids)
    return min(
        block_ids,
        key=lambda block_id: (
            (blocks[block_id]["x"] - min_x) / 1000.0 + (max_y - blocks[block_id]["y"]) / 1000.0,
            -int(blocks[block_id].get("orders") or 0),
            block_id,
        ),
    )


def route_component_blocks(
    component: dict[str, Any],
    blocks: dict[str, dict[str, Any]],
    graph: dict[str, set[str]],
    entry: tuple[float, float] | None,
) -> list[str]:
    unvisited = set(component["ids"])
    current = choose_start_block(unvisited, blocks, entry)
    route: list[str] = []
    while unvisited:
        route.append(current)
        unvisited.remove(current)
        if not unvisited:
            break
        neighbor_candidates = [block_id for block_id in graph[current] if block_id in unvisited]
        if neighbor_candidates:
            current = min(
                neighbor_candidates,
                key=lambda block_id: (
                    distance_xy(blocks[current], blocks[block_id]),
                    -int(blocks[block_id].get("orders") or 0),
                    block_id,
                ),
            )
        else:
            current = min(
                unvisited,
                key=lambda block_id: (
                    distance_xy(blocks[current], blocks[block_id]),
                    -int(blocks[block_id].get("orders") or 0),
                    block_id,
                ),
            )
    return route


def route_components(
    components: list[dict[str, Any]],
    blocks: dict[str, dict[str, Any]],
    graph: dict[str, set[str]],
) -> list[str]:
    if not components:
        return []
    remaining = components[:]
    min_x = min(component["x"] for component in remaining)
    max_y = max(component["y"] for component in remaining)
    current_component = min(
        remaining,
        key=lambda component: ((component["x"] - min_x) / 1000.0 + (max_y - component["y"]) / 1000.0, -component["orders"]),
    )
    entry: tuple[float, float] | None = None
    full_route: list[str] = []
    while remaining:
        remaining.remove(current_component)
        route = route_component_blocks(current_component, blocks, graph, entry)
        full_route.extend(route)
        if route:
            last = blocks[route[-1]]
            entry = (last["x"], last["y"])
        if not remaining:
            break
        assert entry is not None
        current_component = min(
            remaining,
            key=lambda component: (
                min(distance_xy(entry, blocks[block_id]) for block_id in component["ids"]),
                -component["orders"],
            ),
        )
    return full_route


def order_records_in_block(records: list[dict[str, Any]], entry: tuple[float, float] | None) -> list[dict[str, Any]]:
    remaining = records[:]
    if not remaining:
        return []
    if entry is None:
        min_x = min(record["x"] for record in remaining)
        max_y = max(record["y"] for record in remaining)
        current = min(remaining, key=lambda record: ((record["x"] - min_x) / 1000.0 + (max_y - record["y"]) / 1000.0, record.get("id") or 0))
    else:
        current = min(remaining, key=lambda record: (distance_xy(entry, record), record.get("id") or 0))
    ordered: list[dict[str, Any]] = []
    while remaining:
        ordered.append(current)
        remaining.remove(current)
        if not remaining:
            break
        current = min(
            remaining,
            key=lambda record: (
                distance_xy(current, record),
                str(record.get("road_stem") or ""),
                safe_number(record.get("lot_main"), 999999),
                safe_number(record.get("lot_sub"), 999999),
                record.get("id") or 0,
            ),
        )
    return ordered


def build_record_route(block_route: list[str], blocks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    route: list[dict[str, Any]] = []
    entry: tuple[float, float] | None = None
    for block_id in block_route:
        records = order_records_in_block(blocks[block_id].get("records") or [], entry)
        for record in records:
            record["route_block_id"] = block_id
        route.extend(records)
        if records:
            entry = (records[-1]["x"], records[-1]["y"])
        else:
            entry = (blocks[block_id]["x"], blocks[block_id]["y"])
    return route


def prefix_orders(records: list[dict[str, Any]]) -> list[int]:
    prefix = [0]
    for record in records:
        prefix.append(prefix[-1] + order_of(record))
    return prefix


def segment_orders(prefix: list[int], start: int, end: int) -> int:
    return prefix[end] - prefix[start]


def hard_band_violation_penalty(orders: int, min_target: int, max_target: int) -> float:
    under = max(0, min_target - orders)
    over = max(0, orders - max_target)
    violation = under + over
    if violation <= 0:
        return 0.0
    return 100000000.0 + violation * violation * 900000.0


def target_deviation_penalty(orders: int, target: float) -> float:
    return abs(orders - target) * 120.0


def jump_distance_penalty(distance_meters: float) -> float:
    if not math.isfinite(distance_meters):
        return 100000000.0
    if distance_meters <= 400.0:
        return 0.0
    if distance_meters <= 800.0:
        return (distance_meters - 400.0) * 7.0
    if distance_meters <= 1000.0:
        return 9000.0 + (distance_meters - 800.0) * 14.0
    return 28000.0 + (distance_meters - 1000.0) * 60.0


def choose_cut_greedy(
    records: list[dict[str, Any]],
    prefix: list[int],
    start: int,
    person_index: int,
    target: float,
    min_target: int,
    max_target: int,
    people_count: int,
) -> int:
    n = len(records)
    remaining_people = people_count - person_index - 1
    if remaining_people <= 0:
        return n
    best: tuple[float, int] | None = None
    fallback: tuple[float, int] | None = None
    end = start + 1
    while end <= n:
        current_orders = segment_orders(prefix, start, end)
        remaining_orders = segment_orders(prefix, end, n)
        if current_orders > max_target + 80:
            break
        feasible_remaining = remaining_people * min_target <= remaining_orders <= remaining_people * max_target
        in_band = min_target <= current_orders <= max_target
        jump_after = distance_xy(records[end - 1], records[end]) if end < n else 0.0
        jump_boundary_bonus = min(jump_after, 900.0) / 60.0 if jump_after > REGULAR_JUMP_LIMIT_METERS else 0.0
        cost = abs(current_orders - target) - jump_boundary_bonus
        if feasible_remaining and in_band:
            candidate = (cost, end)
            if best is None or candidate < best:
                best = candidate
        fallback_penalty = (
            abs(current_orders - target)
            + max(0, min_target - current_orders) * 10
            + max(0, current_orders - max_target) * 10
            + abs(remaining_orders - remaining_people * target) * 0.05
        )
        fallback_candidate = (fallback_penalty, end)
        if fallback is None or fallback_candidate < fallback:
            fallback = fallback_candidate
        if current_orders >= max_target and feasible_remaining:
            break
        end += 1
    return (best or fallback or (0, n))[1]


def dynamic_partition_cuts(
    records: list[dict[str, Any]],
    people_count: int,
    target: float,
    min_target: int,
    max_target: int,
    prefix: list[int],
) -> list[int] | None:
    n = len(records)
    jump_penalties = [0.0] * n
    for index in range(1, n):
        jump_penalties[index] = jump_distance_penalty(distance_xy(records[index - 1], records[index]))
    jump_prefix = [0.0] * (n + 1)
    for index in range(1, n):
        jump_prefix[index + 1] = jump_prefix[index] + jump_penalties[index]

    states: dict[int, float] = {0: 0.0}
    parents: dict[tuple[int, int], int] = {}
    for person_no in range(1, people_count + 1):
        next_states: dict[int, float] = {}
        for start, previous_cost in states.items():
            end = start + 1
            while end <= n:
                orders = segment_orders(prefix, start, end)
                if orders > max_target:
                    break
                if orders >= min_target:
                    jump_after = distance_xy(records[end - 1], records[end]) if end < n else 0.0
                    internal_jump_cost = jump_prefix[end] - jump_prefix[start + 1]
                    boundary_reward = min(jump_distance_penalty(jump_after), 12000.0) * 0.18
                    cost = (
                        previous_cost
                        + target_deviation_penalty(orders, target)
                        + internal_jump_cost * 100000.0
                        - boundary_reward
                    )
                    existing = next_states.get(end)
                    if existing is None or cost < existing:
                        next_states[end] = cost
                        parents[(person_no, end)] = start
                end += 1
        states = next_states
        if not states:
            return None
    if n not in states:
        return None

    cuts = [n]
    end = n
    for person_no in range(people_count, 0, -1):
        start = parents[(person_no, end)]
        if start:
            cuts.append(start)
        end = start
    return list(reversed(cuts))


def partition_records(records: list[dict[str, Any]], people_count: int, total_orders: int) -> list[dict[str, Any]]:
    target = total_orders / people_count
    min_target = math.floor(target * 0.9)
    max_target = math.ceil(target * 1.1)
    prefix = prefix_orders(records)
    dynamic_cuts = dynamic_partition_cuts(records, people_count, target, min_target, max_target, prefix)
    zones: list[dict[str, Any]] = []
    start = 0
    for person_index in range(people_count):
        if dynamic_cuts:
            end = dynamic_cuts[person_index]
        else:
            end = choose_cut_greedy(records, prefix, start, person_index, target, min_target, max_target, people_count)
        if person_index == people_count - 1 or end < start:
            end = len(records)
        zone_records = records[start:end]
        zones.append(
            {
                "id": person_index + 1,
                "person": f"인원 {person_index + 1}",
                "color": ZONE_COLORS[person_index % len(ZONE_COLORS)],
                "records": zone_records,
                "orders": sum(order_of(record) for record in zone_records),
                "address_count": len(zone_records),
                "block_ids": list(dict.fromkeys(record.get("route_block_id") for record in zone_records if record.get("route_block_id"))),
            }
        )
        start = end
    return zones


def zone_jumps(records: list[dict[str, Any]]) -> list[float]:
    return [distance_xy(records[index - 1], records[index]) for index in range(1, len(records))]


def reentry_violations(zones: list[dict[str, Any]]) -> int:
    seen: set[str] = set()
    previous = ""
    violations = 0
    for zone in zones:
        for record in zone["records"]:
            block_id = record.get("route_block_id") or ""
            if block_id and block_id != previous:
                if previous:
                    seen.add(previous)
                if block_id in seen:
                    violations += 1
                previous = block_id
    return violations


def collect_rings(value: Any, rings: list[list[list[float]]]) -> None:
    if not isinstance(value, list) or not value:
        return
    if isinstance(value[0], list) and len(value[0]) >= 2 and isinstance(value[0][0], (int, float)):
        rings.append(value)
        return
    for item in value:
        collect_rings(item, rings)


def block_segments(block: dict[str, Any], transformer: Transformer) -> list[list[float]]:
    rings: list[list[list[float]]] = []
    collect_rings((block.get("geometry") or {}).get("coordinates") or [], rings)
    counts: dict[tuple[tuple[float, float], tuple[float, float]], int] = defaultdict(int)
    for ring in rings:
        xy: list[tuple[float, float]] = []
        for lon, lat in ring:
            x, y = transformer.transform(float(lon), float(lat))
            xy.append((round(float(x), 2), round(float(y), 2)))
        if xy and xy[0] != xy[-1]:
            xy.append(xy[0])
        for left, right in zip(xy, xy[1:]):
            key = (left, right) if left <= right else (right, left)
            counts[key] += 1
    return [[a[0], a[1], b[0], b[1]] for (a, b), count in counts.items() if count == 1]


def make_visual_payload(
    scope: dict[str, Any],
    blocks: dict[str, dict[str, Any]],
    zones: list[dict[str, Any]],
    residual_records: list[dict[str, Any]],
    block_route: list[str],
) -> dict[str, Any]:
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:5186", always_xy=True)
    zone_by_record = {record.get("id"): zone["id"] for zone in zones for record in zone["records"]}
    residual_ids = {record.get("id") for record in residual_records}
    visual_records: list[dict[str, Any]] = []
    xs: list[float] = []
    ys: list[float] = []
    for record in [record for zone in zones for record in zone["records"]] + residual_records:
        item = {
            "id": record.get("id"),
            "x": round(record["x"], 2),
            "y": round(record["y"], 2),
            "orders": order_of(record),
            "zone": zone_by_record.get(record.get("id"), 0),
            "residual": record.get("id") in residual_ids,
        }
        visual_records.append(item)
        xs.append(item["x"])
        ys.append(item["y"])
    visual_blocks: list[dict[str, Any]] = []
    route_set = set(block_route)
    for block_id in block_route:
        block = blocks[block_id]
        segments = block_segments(block, transformer)
        for x1, y1, x2, y2 in segments:
            xs.extend([x1, x2])
            ys.extend([y1, y2])
        visual_blocks.append(
            {
                "id": block_id,
                "x": round(block["x"], 2),
                "y": round(block["y"], 2),
                "orders": block.get("orders") or 0,
                "addresses": block.get("address_count") or 0,
                "segments": segments,
            }
        )
    long_jumps: list[dict[str, Any]] = []
    for zone in zones:
        records = zone["records"]
        for index in range(1, len(records)):
            jump = distance_xy(records[index - 1], records[index])
            if jump > REGULAR_JUMP_LIMIT_METERS:
                long_jumps.append(
                    {
                        "zone": zone["id"],
                        "x1": records[index - 1]["x"],
                        "y1": records[index - 1]["y"],
                        "x2": records[index]["x"],
                        "y2": records[index]["y"],
                        "meters": round(jump, 1),
                    }
                )
    return {
        "scope_key": scope.get("scope_key"),
        "records": visual_records,
        "blocks": visual_blocks,
        "zones": [
            {
                "id": zone["id"],
                "color": zone["color"],
                "orders": zone["orders"],
                "addresses": zone["address_count"],
                "blocks": len(zone["block_ids"]),
                "max_jump": round(max(zone_jumps(zone["records"]) or [0]), 1),
            }
            for zone in zones
        ],
        "long_jumps": long_jumps,
        "bbox": [min(xs) - 130, min(ys) - 130, max(xs) + 130, max(ys) + 130] if xs and ys else [0, 0, 1, 1],
    }


def build_html(data: dict[str, Any], summary: dict[str, Any]) -> str:
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    summary_json = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    colors_json = json.dumps(ZONE_COLORS, separators=(",", ":"))
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>H074 33명 순차 블럭 배정</title>
  <style>
    html, body {{ margin:0; height:100%; font-family:Segoe UI, Malgun Gothic, sans-serif; }}
    body {{ overflow:hidden; background:#fff; color:#0f172a; }}
    #canvas {{ width:100vw; height:100vh; display:block; }}
    .panel {{ position:fixed; left:14px; top:14px; width:420px; max-width:calc(100vw - 28px); background:rgba(255,255,255,.95); border:1px solid #cbd5e1; border-radius:8px; box-shadow:0 14px 32px rgba(15,23,42,.14); padding:12px; font-size:13px; }}
    h1 {{ margin:0 0 8px; font-size:16px; }}
    .meta {{ white-space:pre-line; color:#475569; line-height:1.55; }}
    label {{ display:inline-flex; gap:5px; align-items:center; margin:8px 12px 0 0; }}
    button {{ margin-top:8px; padding:7px 10px; border:1px solid #cbd5e1; border-radius:6px; background:#fff; cursor:pointer; }}
  </style>
</head>
<body>
<canvas id="canvas"></canvas>
<div class="panel">
  <h1>H074 33명 순차 블럭 배정</h1>
  <div id="meta" class="meta"></div>
  <label><input id="showBlocks" type="checkbox" checked> 블럭 경계</label>
  <label><input id="showPoints" type="checkbox" checked> 주소점</label>
  <label><input id="showJumps" type="checkbox" checked> 500m 초과</label>
  <label><input id="showLabels" type="checkbox"> 인원 라벨</label>
  <button id="fit">전체 보기</button>
</div>
<script>
const DATA = {data_json};
const SUMMARY = {summary_json};
const COLORS = {colors_json};
const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const state = {{ scale:1, ox:0, oy:0, dragging:false, lastX:0, lastY:0 }};
const bbox = DATA.bbox;
function resize() {{ canvas.width = Math.round(innerWidth * devicePixelRatio); canvas.height = Math.round(innerHeight * devicePixelRatio); fit(); }}
function worldToScreen(x,y) {{ return [(x-bbox[0])*state.scale+state.ox, (bbox[3]-y)*state.scale+state.oy]; }}
function fit() {{ const w=bbox[2]-bbox[0], h=bbox[3]-bbox[1]; state.scale=Math.min(canvas.width/w, canvas.height/h)*0.92; state.ox=(canvas.width-w*state.scale)/2; state.oy=(canvas.height-h*state.scale)/2; draw(); }}
function draw() {{
  ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.fillStyle="#fff"; ctx.fillRect(0,0,canvas.width,canvas.height);
  if (document.getElementById("showBlocks").checked) {{
    ctx.strokeStyle="#cbd5e1"; ctx.globalAlpha=.42; ctx.lineWidth=1.1;
    for (const block of DATA.blocks) {{
      ctx.beginPath();
      for (const seg of block.segments) {{ const a=worldToScreen(seg[0],seg[1]); const b=worldToScreen(seg[2],seg[3]); ctx.moveTo(a[0],a[1]); ctx.lineTo(b[0],b[1]); }}
      ctx.stroke();
    }}
    ctx.globalAlpha=1;
  }}
  if (document.getElementById("showPoints").checked) {{
    ctx.lineWidth=1; ctx.strokeStyle="#fff";
    for (const r of DATA.records) {{
      ctx.fillStyle = r.residual ? "#71717a" : COLORS[(r.zone-1) % COLORS.length];
      const p=worldToScreen(r.x,r.y); ctx.beginPath(); ctx.arc(p[0],p[1],3.15*devicePixelRatio,0,Math.PI*2); ctx.fill(); ctx.stroke();
    }}
  }}
  if (document.getElementById("showJumps").checked) {{
    ctx.strokeStyle="#111827"; ctx.globalAlpha=.7; ctx.lineWidth=1.5; ctx.setLineDash([6,5]);
    for (const j of DATA.long_jumps) {{ const a=worldToScreen(j.x1,j.y1); const b=worldToScreen(j.x2,j.y2); ctx.beginPath(); ctx.moveTo(a[0],a[1]); ctx.lineTo(b[0],b[1]); ctx.stroke(); }}
    ctx.setLineDash([]); ctx.globalAlpha=1;
  }}
  if (document.getElementById("showLabels").checked) {{
    ctx.font = `${{11*devicePixelRatio}}px Segoe UI`; ctx.fillStyle="#111827";
    for (const z of DATA.zones) {{ const rec = DATA.records.find(r => r.zone === z.id); if (!rec) continue; const p=worldToScreen(rec.x,rec.y); ctx.fillText(`${{z.id}} (${{z.orders}})`,p[0]+5,p[1]-5); }}
  }}
}}
function updateMeta() {{
  document.getElementById("meta").textContent =
    `목표 ${{SUMMARY.target}}건 / 허용 ${{SUMMARY.min_target}}~${{SUMMARY.max_target}}건\\n`+
    `자동배정 ${{SUMMARY.assigned_orders.toLocaleString("ko-KR")}}건 / 잔여 ${{SUMMARY.residual_orders.toLocaleString("ko-KR")}}건\\n`+
    `범위충족 ${{SUMMARY.band_ok}}/${{SUMMARY.people_count}}명 / 500m 초과 내부 이동 ${{SUMMARY.long_jump_zones}}명\\n`+
    `최대 내부 이동 ${{SUMMARY.max_zone_jump}}m / 블럭 되돌아감 ${{SUMMARY.reentry_violations}}건`;
}}
canvas.addEventListener("wheel", e => {{ e.preventDefault(); const f=e.deltaY<0?1.12:.89; const mx=e.offsetX*devicePixelRatio, my=e.offsetY*devicePixelRatio; state.ox=mx-(mx-state.ox)*f; state.oy=my-(my-state.oy)*f; state.scale*=f; draw(); }}, {{passive:false}});
canvas.addEventListener("mousedown", e => {{ state.dragging=true; state.lastX=e.clientX; state.lastY=e.clientY; }});
addEventListener("mouseup", () => state.dragging=false);
addEventListener("mousemove", e => {{ if(!state.dragging) return; state.ox+=(e.clientX-state.lastX)*devicePixelRatio; state.oy+=(e.clientY-state.lastY)*devicePixelRatio; state.lastX=e.clientX; state.lastY=e.clientY; draw(); }});
document.querySelectorAll("input").forEach(input => input.addEventListener("change", draw));
document.getElementById("fit").addEventListener("click", fit);
addEventListener("resize", resize);
updateMeta(); resize();
</script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Run H074 33-person sequential block assignment simulation.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--html", type=Path, default=DEFAULT_OUTPUT_HTML)
    parser.add_argument("--json", type=Path, default=DEFAULT_OUTPUT_JSON)
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    scope = payload["scopes"][SCOPE_KEY]
    blocks, records = transform_scope(scope)
    graph = build_graph(blocks)
    components = connected_components(blocks, graph)
    route_components_list, residual_block_ids, residual_component_reasons = classify_residual_components(components, blocks)
    block_route = route_components(route_components_list, blocks, graph)
    record_route = build_record_route(block_route, blocks)

    residual_records: list[dict[str, Any]] = []
    for block_id in residual_block_ids:
        for record in blocks[block_id].get("records") or []:
            next_record = dict(record)
            next_record["residual_reason"] = "small_far_island_component"
            residual_records.append(next_record)

    total_orders = int(scope.get("total_orders") or sum(order_of(record) for record in records))
    target = total_orders / PEOPLE_COUNT
    min_target = math.floor(target * 0.9)
    max_target = math.ceil(target * 1.1)
    zones = partition_records(record_route, PEOPLE_COUNT, total_orders)
    band_ok = sum(1 for zone in zones if min_target <= zone["orders"] <= max_target)
    max_jumps = [max(zone_jumps(zone["records"]) or [0.0]) for zone in zones]
    long_jump_zones = sum(1 for jump in max_jumps if jump > REGULAR_JUMP_LIMIT_METERS)
    summary = {
        "scope_key": SCOPE_KEY,
        "people_count": PEOPLE_COUNT,
        "target": round(target, 1),
        "min_target": min_target,
        "max_target": max_target,
        "assigned_orders": sum(zone["orders"] for zone in zones),
        "residual_orders": sum(order_of(record) for record in residual_records),
        "residual_addresses": len(residual_records),
        "band_ok": band_ok,
        "zone_orders": [zone["orders"] for zone in zones],
        "zone_addresses": [zone["address_count"] for zone in zones],
        "max_zone_jump": round(max(max_jumps or [0.0]), 1),
        "long_jump_zones": long_jump_zones,
        "reentry_violations": reentry_violations(zones),
        "route_blocks": len(block_route),
        "route_records": len(record_route),
        "residual_components": residual_component_reasons,
        "component_count": len(components),
        "route_component_count": len(route_components_list),
        "residual_component_count": len(residual_component_reasons),
    }
    visual = make_visual_payload(scope, blocks, zones, residual_records, block_route)
    args.html.parent.mkdir(parents=True, exist_ok=True)
    args.html.write_text(build_html(visual, summary), encoding="utf-8")
    args.json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"wrote {args.html}", flush=True)
    print(f"wrote {args.json}", flush=True)


if __name__ == "__main__":
    main()
