from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_PATH = BASE_DIR / "processed_assignment_blocks.json"
OUTPUT_DIR = BASE_DIR / "boundary_map"
H074_PEOPLE_COUNT = 33

ZONE_COLORS = [
    "#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2",
    "#4f46e5", "#65a30d", "#be123c", "#0f766e", "#b45309", "#7c3aed",
    "#0284c7", "#15803d", "#b91c1c", "#a21caf", "#ca8a04", "#0369a1",
    "#4338ca", "#047857", "#9f1239", "#6d28d9", "#c2410c", "#0e7490",
    "#1d4ed8", "#3f6212", "#991b1b", "#86198f", "#854d0e", "#155e75",
    "#312e81", "#166534", "#7f1d1d",
]


def load_payload() -> dict[str, Any]:
    with DATA_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def safe_number(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def format_scope_key(scope: dict[str, Any]) -> str:
    return f"{scope.get('month') or ''}|{scope.get('center') or ''}"


def block_id(block: dict[str, Any]) -> str:
    return str(block.get("block_id") or block.get("id") or "")


def record_block_id(record: dict[str, Any]) -> str:
    return str(record.get("assignment_block_id") or record.get("block_id") or "")


def total_orders(records: list[dict[str, Any]]) -> int:
    return int(sum(max(0.0, safe_number(record.get("orders"))) for record in records))


def bbox_from_blocks(blocks: list[dict[str, Any]]) -> list[float]:
    bboxes = [block.get("bbox") for block in blocks if isinstance(block.get("bbox"), list) and len(block["bbox"]) == 4]
    if not bboxes:
        return [127.35, 36.30, 127.45, 36.40]
    return [
        min(bbox[0] for bbox in bboxes),
        min(bbox[1] for bbox in bboxes),
        max(bbox[2] for bbox in bboxes),
        max(bbox[3] for bbox in bboxes),
    ]


def feature_for_block(block: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    props = {
        "block_id": block_id(block),
        "orders": int(safe_number(block.get("orders"))),
        "address_count": int(safe_number(block.get("address_count"))),
        "parcel_count": int(safe_number(block.get("parcel_count"))),
        "dong": block.get("legal_dong_name") or block.get("dong") or "",
        "legal_dong_code": block.get("legal_dong_code") or "",
        "road_stem": block.get("road_stem") or "",
    }
    if extra:
        props.update(extra)
    return {
        "type": "Feature",
        "geometry": normalize_geometry(block.get("geometry") or {"type": "MultiPolygon", "coordinates": []}),
        "properties": props,
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
    coordinates = geometry.get("coordinates") or []
    normalized: list[Any] = []
    for polygon in coordinates:
        # Older generated files had one extra wrapper: [[ring]] instead of [ring].
        while isinstance(polygon, list) and len(polygon) == 1 and is_polygon(polygon[0]):
            polygon = polygon[0]
        normalized.append(polygon)
    return {"type": "MultiPolygon", "coordinates": normalized}


def scope_features(scope: dict[str, Any]) -> list[dict[str, Any]]:
    return [feature_for_block(block) for block in scope.get("blocks") or []]


def records_for_scope(scope: dict[str, Any], zone_by_block: dict[str, int] | None = None) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for record in scope.get("records") or []:
        lat = safe_number(record.get("lat"), math.nan)
        lon = safe_number(record.get("lon"), math.nan)
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        rid = record_block_id(record)
        output.append(
            {
                "id": record.get("id"),
                "lat": round(lat, 7),
                "lon": round(lon, 7),
                "orders": int(safe_number(record.get("orders"))),
                "address": record.get("address") or "",
                "road_address": record.get("road_address") or "",
                "jibun_address": record.get("jibun_address") or "",
                "block_id": rid,
                "dong": record.get("legal_dong_name") or record.get("dong") or "",
                "zone_id": zone_by_block.get(rid) if zone_by_block else None,
            }
        )
    return output


def html_page(title: str, subtitle: str, data: dict[str, Any], mode: str) -> str:
    payload_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body {{ height: 100%; margin: 0; font-family: Arial, "Malgun Gothic", sans-serif; color: #172033; }}
    body {{ display: grid; grid-template-columns: minmax(300px, 360px) 1fr; background: #f5f7fa; }}
    aside {{ overflow: auto; border-right: 1px solid #d8dee8; background: #ffffff; padding: 18px; }}
    h1 {{ margin: 0 0 6px; font-size: 20px; line-height: 1.35; }}
    .subtitle {{ color: #5b6472; font-size: 13px; line-height: 1.45; margin-bottom: 16px; }}
    .stats {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 16px; }}
    .stat {{ border: 1px solid #d8dee8; border-radius: 6px; padding: 10px; background: #fbfcfe; }}
    .stat span {{ display: block; color: #6b7280; font-size: 12px; margin-bottom: 4px; }}
    .stat strong {{ font-size: 18px; }}
    .legend {{ display: grid; gap: 7px; margin: 12px 0 18px; }}
    .legend-row {{ display: flex; align-items: center; gap: 8px; font-size: 12px; color: #475569; }}
    .swatch {{ width: 16px; height: 12px; border-radius: 2px; border: 1px solid rgba(0,0,0,.12); flex: 0 0 auto; }}
    .list {{ display: grid; gap: 6px; }}
    .row {{ border: 1px solid #d8dee8; border-radius: 6px; padding: 9px; background: #fff; cursor: pointer; text-align: left; }}
    .row:hover {{ border-color: #64748b; }}
    .row strong {{ display: block; font-size: 13px; margin-bottom: 4px; }}
    .row span {{ display: block; color: #64748b; font-size: 12px; line-height: 1.35; }}
    #map {{ height: 100%; width: 100%; }}
    .leaflet-popup-content {{ font-size: 12px; line-height: 1.45; }}
    @media (max-width: 820px) {{
      body {{ grid-template-columns: 1fr; grid-template-rows: 40vh 60vh; }}
      aside {{ order: 2; border-right: 0; border-top: 1px solid #d8dee8; }}
      #map {{ order: 1; }}
    }}
  </style>
</head>
<body>
  <aside>
    <h1>{title}</h1>
    <div class="subtitle">{subtitle}</div>
    <div id="stats" class="stats"></div>
    <div id="legend" class="legend"></div>
    <div id="list" class="list"></div>
  </aside>
  <main id="map"></main>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const DATA = {payload_json};
    const MODE = "{mode}";
    const map = L.map("map", {{ preferCanvas: true }});
    L.tileLayer("https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 20,
      attribution: "&copy; OpenStreetMap contributors"
    }}).addTo(map);

    const bounds = [[DATA.bbox[1], DATA.bbox[0]], [DATA.bbox[3], DATA.bbox[2]]];
    map.fitBounds(bounds, {{ padding: [24, 24] }});

    const fmt = new Intl.NumberFormat("ko-KR");
    const maxOrders = Math.max(1, ...DATA.features.map(f => Number(f.properties.orders || 0)));
    const densityColors = ["#e0f2fe", "#93c5fd", "#38bdf8", "#2563eb", "#1e3a8a"];
    const zoneColors = {json.dumps(ZONE_COLORS, ensure_ascii=False)};

    function densityColor(orders) {{
      const ratio = Math.max(0, Math.min(1, Number(orders || 0) / maxOrders));
      const idx = Math.min(densityColors.length - 1, Math.floor(ratio * densityColors.length));
      return densityColors[idx];
    }}

    function featureColor(feature) {{
      if (MODE === "assignment") {{
        const zone = Number(feature.properties.zone_id || 0);
        return zone ? zoneColors[(zone - 1) % zoneColors.length] : "#94a3b8";
      }}
      return "#ef4444";
    }}

    function popupHtml(props) {{
      const zone = props.zone_id ? `<div><b>배정:</b> 인원 ${{props.zone_id}}</div>` : "";
      return `
        <div><b>블럭:</b> ${{props.block_id}}</div>
        ${{zone}}
        <div><b>오더:</b> ${{fmt.format(props.orders || 0)}}건</div>
        <div><b>주소:</b> ${{fmt.format(props.address_count || 0)}}개</div>
        <div><b>동:</b> ${{props.dong || ""}} ${{props.legal_dong_code || ""}}</div>
        <div><b>도로:</b> ${{props.road_stem || ""}}</div>
      `;
    }}

    const layerById = new Map();
    const blockLayer = L.geoJSON(DATA.features, {{
      style: feature => ({{
        color: featureColor(feature),
        fillColor: featureColor(feature),
        weight: MODE === "assignment" ? 1.4 : 2.2,
        fillOpacity: MODE === "assignment" ? 0.42 : 0.04,
        opacity: 1
      }}),
      onEachFeature: (feature, layer) => {{
        layer.bindPopup(popupHtml(feature.properties));
        layer.on("mouseover", () => layer.setStyle({{ weight: 3, fillOpacity: 0.58 }}));
        layer.on("mouseout", () => blockLayer.resetStyle(layer));
        layerById.set(feature.properties.block_id, layer);
      }}
    }}).addTo(map);

    const pointLayer = L.layerGroup().addTo(map);
    if (MODE === "assignment") {{
      for (const record of DATA.records || []) {{
        const color = record.zone_id ? zoneColors[(record.zone_id - 1) % zoneColors.length] : "#111827";
        L.circleMarker([record.lat, record.lon], {{
          radius: Math.max(3, Math.min(8, Math.sqrt(record.orders || 1) + 2)),
          color,
          fillColor: color,
          fillOpacity: 0.78,
          weight: 1
        }}).bindPopup(`
          <div><b>주소ID:</b> ${{record.id}}</div>
          <div><b>오더:</b> ${{fmt.format(record.orders || 0)}}건</div>
          <div><b>블럭:</b> ${{record.block_id}}</div>
          ${{record.zone_id ? `<div><b>배정:</b> 인원 ${{record.zone_id}}</div>` : ""}}
          <div>${{record.address || ""}}</div>
        `).addTo(pointLayer);
      }}
    }}

    function renderStats() {{
      const rows = [
        ["오더", `${{fmt.format(DATA.summary.total_orders)}}건`],
        ["주소", `${{fmt.format(DATA.summary.address_count)}}개`],
        ["블럭", `${{fmt.format(DATA.summary.block_count)}}개`],
        [MODE === "assignment" ? "인원" : "법정동", MODE === "assignment" ? `${{DATA.summary.people_count}}명` : `${{fmt.format(DATA.summary.dong_count)}}개`]
      ];
      document.getElementById("stats").innerHTML = rows.map(([label, value]) => `
        <div class="stat"><span>${{label}}</span><strong>${{value}}</strong></div>
      `).join("");
    }}

    function renderLegend() {{
      if (MODE === "assignment") {{
        document.getElementById("legend").innerHTML = DATA.zones.map(zone => `
          <div class="legend-row">
            <span class="swatch" style="background:${{zone.color}}"></span>
            <span>인원 ${{zone.zone_id}} · ${{fmt.format(zone.orders)}}건 · ${{fmt.format(zone.block_count)}}블럭</span>
          </div>
        `).join("");
        return;
      }}
      document.getElementById("legend").innerHTML = `
        <div class="legend-row"><span class="swatch" style="background:#ef4444"></span><span>개선 블럭 경계</span></div>
        <div class="legend-row"><span class="swatch" style="background:rgba(239,68,68,.08)"></span><span>블럭 내부 영역</span></div>
      `;
    }}

    function renderList() {{
      const items = MODE === "assignment"
        ? DATA.zones
        : DATA.features.map(f => f.properties).sort((a, b) => (b.orders || 0) - (a.orders || 0)).slice(0, 40);
      document.getElementById("list").innerHTML = items.map(item => {{
        if (MODE === "assignment") {{
          return `<button class="row" data-zone="${{item.zone_id}}">
            <strong>인원 ${{item.zone_id}} · ${{fmt.format(item.orders)}}건</strong>
            <span>${{fmt.format(item.address_count)}}주소 · ${{fmt.format(item.block_count)}}블럭 · 목표 대비 ${{item.deviation_pct >= 0 ? "+" : ""}}${{item.deviation_pct.toFixed(1)}}%</span>
          </button>`;
        }}
        return `<button class="row" data-block="${{item.block_id}}">
          <strong>${{item.block_id}} · ${{fmt.format(item.orders)}}건</strong>
          <span>${{item.dong || ""}} · ${{fmt.format(item.address_count)}}주소 · ${{fmt.format(item.parcel_count)}}필지</span>
        </button>`;
      }}).join("");
      document.querySelectorAll("[data-block]").forEach(button => {{
        button.addEventListener("click", () => {{
          const layer = layerById.get(button.dataset.block);
          if (layer) {{
            map.fitBounds(layer.getBounds(), {{ padding: [32, 32], maxZoom: 18 }});
            layer.openPopup();
          }}
        }});
      }});
      document.querySelectorAll("[data-zone]").forEach(button => {{
        button.addEventListener("click", () => {{
          const zoneId = Number(button.dataset.zone);
          const layers = [];
          for (const feature of DATA.features) {{
            if (Number(feature.properties.zone_id) === zoneId) {{
              const layer = layerById.get(feature.properties.block_id);
              if (layer) layers.push(layer);
            }}
          }}
          if (layers.length) {{
            const group = L.featureGroup(layers);
            map.fitBounds(group.getBounds(), {{ padding: [32, 32] }});
          }}
        }});
      }});
    }}

    renderStats();
    renderLegend();
    renderList();
  </script>
</body>
</html>
"""


def write_center_map(scope: dict[str, Any]) -> Path:
    center = scope.get("center") or "CENTER"
    month = scope.get("month") or ""
    blocks = scope.get("blocks") or []
    features = scope_features(scope)
    dongs = {block.get("legal_dong_code") or block.get("dong") for block in blocks if block.get("legal_dong_code") or block.get("dong")}
    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scope_key": format_scope_key(scope),
        "bbox": bbox_from_blocks(blocks),
        "features": features,
        "records": records_for_scope(scope),
        "summary": {
            "total_orders": int(scope.get("total_orders") or total_orders(scope.get("records") or [])),
            "address_count": len(scope.get("records") or []),
            "block_count": len(blocks),
            "dong_count": len(dongs),
        },
    }
    title = f"{center} {month} 블럭 경계"
    subtitle = "법정동 경계와 도로 경계를 반영한 운영용 블럭입니다."
    path = OUTPUT_DIR / f"assignment_blocks_{center}.html"
    path.write_text(html_page(title, subtitle, data, "blocks"), encoding="utf-8")
    return path


def distance_m(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_c = left.get("centroid") or {}
    right_c = right.get("centroid") or {}
    lat1 = math.radians(safe_number(left_c.get("lat")))
    lat2 = math.radians(safe_number(right_c.get("lat")))
    lon1 = math.radians(safe_number(left_c.get("lon")))
    lon2 = math.radians(safe_number(right_c.get("lon")))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def route_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    block_map = {block_id(block): block for block in blocks if block_id(block)}
    remaining = set(block_map)
    route: list[dict[str, Any]] = []
    current_id = min(
        remaining,
        key=lambda bid: (
            safe_number((block_map[bid].get("centroid") or {}).get("lon")),
            -safe_number((block_map[bid].get("centroid") or {}).get("lat")),
            bid,
        ),
    )
    relation_rank = {"touches": 0, "near": 1, "across_admin_boundary": 2, "across_transport_barrier": 3}
    while remaining:
        current = block_map[current_id]
        route.append(current)
        remaining.remove(current_id)
        if not remaining:
            break
        neighbor_ids = [
            edge.get("neighbor_block_id")
            for edge in current.get("neighbors") or []
            if edge.get("neighbor_block_id") in remaining
        ]
        if neighbor_ids:
            current_id = min(
                neighbor_ids,
                key=lambda bid: (
                    relation_rank.get(
                        next((edge.get("relation") for edge in current.get("neighbors") or [] if edge.get("neighbor_block_id") == bid), ""),
                        9,
                    ),
                    distance_m(current, block_map[bid]),
                    -safe_number(block_map[bid].get("orders")),
                    bid,
                ),
            )
        else:
            current_id = min(
                remaining,
                key=lambda bid: (
                    distance_m(current, block_map[bid]),
                    safe_number((block_map[bid].get("centroid") or {}).get("lon")),
                    -safe_number(block_map[bid].get("orders")),
                    bid,
                ),
            )
    return route


def partition_route(route: list[dict[str, Any]], people_count: int) -> list[list[dict[str, Any]]]:
    n = len(route)
    if people_count <= 0:
        return []
    people = min(people_count, n)
    orders = [int(safe_number(block.get("orders"))) for block in route]
    prefix = [0]
    for value in orders:
        prefix.append(prefix[-1] + value)
    target = prefix[-1] / people_count
    dp = [[math.inf] * (n + 1) for _ in range(people + 1)]
    prev = [[-1] * (n + 1) for _ in range(people + 1)]
    dp[0][0] = 0.0
    for person in range(1, people + 1):
        min_i = person
        max_i = n - (people - person)
        for end in range(min_i, max_i + 1):
            for start in range(person - 1, end):
                if not math.isfinite(dp[person - 1][start]):
                    continue
                segment_orders = prefix[end] - prefix[start]
                cost = dp[person - 1][start] + (segment_orders - target) ** 2
                if cost < dp[person][end]:
                    dp[person][end] = cost
                    prev[person][end] = start
    chunks: list[list[dict[str, Any]]] = []
    end = n
    for person in range(people, 0, -1):
        start = prev[person][end]
        if start < 0:
            start = person - 1
        chunks.append(route[start:end])
        end = start
    chunks.reverse()
    while len(chunks) < people_count:
        chunks.append([])
    return chunks


def build_h074_assignment(scope: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    route = route_blocks(scope.get("blocks") or [])
    chunks = partition_route(route, H074_PEOPLE_COUNT)
    zone_by_block: dict[str, int] = {}
    records_by_block: dict[str, list[dict[str, Any]]] = {}
    for record in scope.get("records") or []:
        records_by_block.setdefault(record_block_id(record), []).append(record)
    zones: list[dict[str, Any]] = []
    target = int(scope.get("total_orders") or total_orders(scope.get("records") or [])) / H074_PEOPLE_COUNT
    for index, blocks in enumerate(chunks, start=1):
        ids = [block_id(block) for block in blocks]
        for bid in ids:
            zone_by_block[bid] = index
        zone_records = [record for bid in ids for record in records_by_block.get(bid, [])]
        orders = total_orders(zone_records)
        zones.append(
            {
                "zone_id": index,
                "color": ZONE_COLORS[(index - 1) % len(ZONE_COLORS)],
                "orders": orders,
                "address_count": len(zone_records),
                "block_count": len(ids),
                "block_ids": ids,
                "deviation_pct": ((orders - target) / target * 100) if target else 0,
            }
        )
    return zones, zone_by_block


def write_h074_assignment(scope: dict[str, Any]) -> Path:
    zones, zone_by_block = build_h074_assignment(scope)
    blocks = scope.get("blocks") or []
    features = [
        feature_for_block(
            block,
            {
                "zone_id": zone_by_block.get(block_id(block)),
                "person": f"인원 {zone_by_block.get(block_id(block))}" if zone_by_block.get(block_id(block)) else "",
            },
        )
        for block in blocks
    ]
    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scope_key": format_scope_key(scope),
        "bbox": bbox_from_blocks(blocks),
        "features": features,
        "records": records_for_scope(scope, zone_by_block),
        "zones": zones,
        "summary": {
            "total_orders": int(scope.get("total_orders") or total_orders(scope.get("records") or [])),
            "address_count": len(scope.get("records") or []),
            "block_count": len(blocks),
            "people_count": H074_PEOPLE_COUNT,
        },
    }
    title = f"H074 {scope.get('month') or ''} 33명 블럭 배정"
    subtitle = "도로/법정동 블럭 순서를 기준으로 33개 연속 구간으로 나눈 간단 배정안입니다."
    path = OUTPUT_DIR / "H074_33_actual_assignment.html"
    path.write_text(html_page(title, subtitle, data, "assignment"), encoding="utf-8")
    return path


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = load_payload()
    scopes = payload.get("scopes") or {}
    written: list[Path] = []
    for scope in scopes.values():
        written.append(write_center_map(scope))
    h074_scope = next((scope for scope in scopes.values() if scope.get("center") == "H074"), None)
    if h074_scope:
        written.append(write_h074_assignment(h074_scope))
    for path in written:
        print(path.relative_to(BASE_DIR))


if __name__ == "__main__":
    main()
