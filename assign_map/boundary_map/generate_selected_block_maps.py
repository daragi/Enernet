from __future__ import annotations

import argparse
import html
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_PATH = BASE_DIR / "processed_assignment_blocks.json"
DEFAULT_OUTPUT_DIR = BASE_DIR / "boundary_map"
DEFAULT_CENTERS = ("H074", "H072")


def safe_number(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def block_id(block: dict[str, Any]) -> str:
    return str(block.get("block_id") or block.get("id") or "")


def record_block_id(record: dict[str, Any]) -> str:
    return str(record.get("assignment_block_id") or record.get("block_id") or "")


def short_block_id(value: str) -> str:
    text = str(value or "")
    return text.rsplit("|", 1)[-1] if "|" in text else text


def total_orders(records: list[dict[str, Any]]) -> int:
    return int(sum(max(0.0, safe_number(record.get("orders"))) for record in records))


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


def relation_counts(block: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for edge in block.get("neighbors") or []:
        relation = str(edge.get("relation") or "unknown")
        counts[relation] = counts.get(relation, 0) + 1
    return counts


def feature_for_block(block: dict[str, Any]) -> dict[str, Any]:
    bid = block_id(block)
    props = {
        "block_id": bid,
        "short_id": short_block_id(bid),
        "orders": int(safe_number(block.get("orders"))),
        "address_count": int(safe_number(block.get("address_count"))),
        "parcel_count": int(safe_number(block.get("parcel_count"))),
        "area": round(safe_number(block.get("area")), 1),
        "dong": block.get("legal_dong_name") or block.get("dong") or "",
        "legal_dong_code": block.get("legal_dong_code") or "",
        "road_stem": block.get("road_stem") or "",
        "neighbor_count": len(block.get("neighbors") or []),
        "relations": relation_counts(block),
    }
    return {
        "type": "Feature",
        "geometry": normalize_geometry(block.get("geometry") or {"type": "MultiPolygon", "coordinates": []}),
        "properties": props,
    }


def records_for_scope(scope: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for record in scope.get("records") or []:
        lat = safe_number(record.get("lat"), math.nan)
        lon = safe_number(record.get("lon"), math.nan)
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        bid = record_block_id(record)
        records.append(
            {
                "id": record.get("id"),
                "lat": round(lat, 7),
                "lon": round(lon, 7),
                "orders": int(safe_number(record.get("orders"))),
                "address": record.get("address") or "",
                "road_address": record.get("road_address") or "",
                "jibun_address": record.get("jibun_address") or "",
                "hosu": record.get("hosu") or "",
                "block_id": bid,
                "short_id": short_block_id(bid),
            }
        )
    return records


def labels_for_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels: list[dict[str, Any]] = []
    for block in blocks:
        centroid = block.get("centroid") or {}
        lat = safe_number(centroid.get("lat"), math.nan)
        lon = safe_number(centroid.get("lon"), math.nan)
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        bid = block_id(block)
        labels.append(
            {
                "block_id": bid,
                "short_id": short_block_id(bid),
                "lat": round(lat, 7),
                "lon": round(lon, 7),
                "orders": int(safe_number(block.get("orders"))),
            }
        )
    return labels


def neighbor_edges_for_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {block_id(block): block for block in blocks if block_id(block)}
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in blocks:
        source_id = block_id(source)
        source_center = source.get("centroid") or {}
        for edge in source.get("neighbors") or []:
            target_id = str(edge.get("neighbor_block_id") or "")
            target = by_id.get(target_id)
            if not source_id or not target:
                continue
            pair = tuple(sorted((source_id, target_id)))
            if pair in seen:
                continue
            seen.add(pair)
            relation = str(edge.get("relation") or "")
            if relation not in {"touches", "near"}:
                continue
            target_center = target.get("centroid") or {}
            lat1 = safe_number(source_center.get("lat"), math.nan)
            lon1 = safe_number(source_center.get("lon"), math.nan)
            lat2 = safe_number(target_center.get("lat"), math.nan)
            lon2 = safe_number(target_center.get("lon"), math.nan)
            if not all(math.isfinite(value) for value in (lat1, lon1, lat2, lon2)):
                continue
            edges.append(
                {
                    "from": source_id,
                    "to": target_id,
                    "relation": relation,
                    "distance": safe_number(edge.get("boundary_distance_meters")),
                    "shared": safe_number(edge.get("shared_boundary_length")),
                    "coords": [[round(lat1, 7), round(lon1, 7)], [round(lat2, 7), round(lon2, 7)]],
                }
            )
    return edges


def build_payload(scope: dict[str, Any], summary: dict[str, Any] | None) -> dict[str, Any]:
    blocks = scope.get("blocks") or []
    records = scope.get("records") or []
    dongs = {block.get("legal_dong_code") or block.get("dong") for block in blocks if block.get("legal_dong_code") or block.get("dong")}
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scope_key": scope.get("scope_key") or f"{scope.get('month') or ''}|{scope.get('center') or ''}",
        "center": scope.get("center") or "",
        "month": scope.get("month") or "",
        "bbox": bbox_from_blocks(blocks),
        "features": [feature_for_block(block) for block in blocks],
        "records": records_for_scope(scope),
        "labels": labels_for_blocks(blocks),
        "neighbor_edges": neighbor_edges_for_blocks(blocks),
        "summary": {
            "total_orders": int(scope.get("total_orders") or total_orders(records)),
            "address_count": len(records),
            "block_count": len(blocks),
            "dong_count": len(dongs),
            "single_block_count": int(safe_number((summary or {}).get("single_parcel_assignment_blocks"))),
            "max_parcels": int(safe_number((summary or {}).get("max_parcels_in_block"))),
            "max_addresses": int(safe_number((summary or {}).get("max_addresses_in_block"))),
            "max_orders": int(safe_number((summary or {}).get("max_orders_in_block"))),
            "blocks_without_neighbors": int(safe_number((summary or {}).get("blocks_without_neighbors"))),
            "candidate_pairs": int(safe_number((summary or {}).get("candidate_pairs"))),
            "union_edges": int(safe_number((summary or {}).get("union_edges"))),
            "barrier_edges": int(safe_number((summary or {}).get("barrier_blocked_edges"))),
            "admin_edges": int(safe_number((summary or {}).get("admin_boundary_edges"))),
            "near_unconnected_edges": int(safe_number((summary or {}).get("near_unconnected_edges"))),
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
      --panel: rgba(255, 255, 255, 0.82);
      --border: rgba(15, 23, 42, 0.16);
      --text: #0f172a;
      --muted: #475569;
      --accent: #dc2626;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; font-family: Arial, "Malgun Gothic", sans-serif; color: var(--text); }
    body { background: #eef2f7; }
    #map { position: fixed; inset: 0; }
    .panel {
      position: fixed;
      top: 16px;
      left: 16px;
      z-index: 900;
      width: min(360px, calc(100vw - 32px));
      max-height: calc(100vh - 32px);
      overflow: auto;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      box-shadow: 0 14px 36px rgba(15, 23, 42, 0.18);
      backdrop-filter: blur(8px);
    }
    h1 { margin: 0 0 5px; font-size: 19px; line-height: 1.35; }
    .subtitle { color: var(--muted); font-size: 12px; line-height: 1.45; margin-bottom: 12px; }
    .stats { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; margin-bottom: 12px; }
    .stat { border: 1px solid var(--border); border-radius: 6px; padding: 8px; background: rgba(255, 255, 255, 0.52); }
    .stat span { display: block; color: var(--muted); font-size: 11px; margin-bottom: 3px; }
    .stat strong { display: block; font-size: 17px; line-height: 1.2; }
    .controls { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
    .controls label {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 7px 8px;
      background: rgba(255, 255, 255, 0.48);
      color: #1e293b;
      font-size: 12px;
      cursor: pointer;
      user-select: none;
    }
    .legend { display: grid; gap: 5px; margin-bottom: 12px; }
    .legend-row { display: flex; align-items: center; gap: 7px; color: var(--muted); font-size: 11px; }
    .swatch { width: 18px; height: 10px; border-radius: 2px; border: 1px solid rgba(0, 0, 0, 0.14); }
    .list-title { margin: 10px 0 6px; font-size: 12px; font-weight: 700; color: #1e293b; }
    .list { display: grid; gap: 5px; }
    .row {
      width: 100%;
      text-align: left;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px;
      background: rgba(255, 255, 255, 0.58);
      cursor: pointer;
    }
    .row:hover { border-color: #334155; background: rgba(255, 255, 255, 0.82); }
    .row strong { display: block; font-size: 12px; margin-bottom: 3px; }
    .row span { display: block; color: var(--muted); font-size: 11px; line-height: 1.38; }
    .block-label {
      min-width: 34px;
      transform: translate(-50%, -50%);
      border: 1px solid rgba(15, 23, 42, 0.25);
      border-radius: 4px;
      padding: 1px 4px;
      background: rgba(255, 255, 255, 0.74);
      color: #0f172a;
      font-size: 10px;
      font-weight: 700;
      text-align: center;
      white-space: nowrap;
      box-shadow: 0 1px 3px rgba(15, 23, 42, 0.15);
    }
    .leaflet-popup-content { min-width: 190px; font-size: 12px; line-height: 1.5; }
    @media (max-width: 720px) {
      .panel { top: auto; bottom: 12px; left: 12px; width: calc(100vw - 24px); max-height: 42vh; }
    }
  </style>
</head>
<body>
  <div id="map"></div>
  <aside class="panel">
    <h1>__TITLE__</h1>
    <div class="subtitle">수정된 SHP 기반 블럭 경계입니다. 붉은 외곽선은 실제 병합된 assignment block이고, 점은 해당 블럭에 매칭된 오더 주소입니다.</div>
    <div id="stats" class="stats"></div>
    <div class="controls">
      <label><input id="togglePoints" type="checkbox" checked> 오더 점</label>
      <label><input id="toggleLabels" type="checkbox"> 블럭 번호</label>
      <label><input id="toggleNeighbors" type="checkbox"> 인접선</label>
    </div>
    <div class="legend">
      <div class="legend-row"><span class="swatch" style="background: rgba(220, 38, 38, .08); border-color:#dc2626"></span><span>블럭 경계</span></div>
      <div class="legend-row"><span class="swatch" style="background:#2563eb"></span><span>오더 주소 점</span></div>
      <div class="legend-row"><span class="swatch" style="background:#16a34a"></span><span>touches/near 인접 후보선</span></div>
    </div>
    <div class="list-title">오더 많은 블럭 상위 80개</div>
    <div id="list" class="list"></div>
  </aside>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const DATA = __DATA__;
    const map = L.map("map", { preferCanvas: true, zoomControl: false });
    L.control.zoom({ position: "bottomright" }).addTo(map);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 20,
      attribution: "&copy; OpenStreetMap contributors"
    }).addTo(map);

    const fmt = new Intl.NumberFormat("ko-KR");
    const bounds = [[DATA.bbox[1], DATA.bbox[0]], [DATA.bbox[3], DATA.bbox[2]]];
    map.fitBounds(bounds, { padding: [28, 28] });

    const layerById = new Map();
    const maxOrders = Math.max(1, ...DATA.features.map((f) => Number(f.properties.orders || 0)));

    function orderFill(orders) {
      const ratio = Math.max(0, Math.min(1, Number(orders || 0) / maxOrders));
      if (ratio >= 0.75) return "#fecaca";
      if (ratio >= 0.45) return "#fed7aa";
      if (ratio >= 0.25) return "#fde68a";
      if (ratio >= 0.10) return "#bfdbfe";
      return "#e0f2fe";
    }

    function popupHtml(props) {
      const relations = props.relations || {};
      const relText = Object.entries(relations).map(([key, value]) => `${key} ${value}`).join(" / ") || "-";
      return `
        <div><b>블럭:</b> ${props.short_id}</div>
        <div><b>오더:</b> ${fmt.format(props.orders || 0)}건</div>
        <div><b>주소:</b> ${fmt.format(props.address_count || 0)}개</div>
        <div><b>필지:</b> ${fmt.format(props.parcel_count || 0)}개</div>
        <div><b>동:</b> ${props.dong || ""}</div>
        <div><b>도로명:</b> ${props.road_stem || ""}</div>
        <div><b>인접:</b> ${relText}</div>
      `;
    }

    const blockLayer = L.geoJSON(DATA.features, {
      style: (feature) => ({
        color: "#dc2626",
        fillColor: orderFill(feature.properties.orders),
        weight: feature.properties.orders >= DATA.summary.max_orders ? 3 : 1.7,
        opacity: 0.95,
        fillOpacity: 0.22
      }),
      onEachFeature: (feature, layer) => {
        layerById.set(feature.properties.block_id, layer);
        layer.bindPopup(popupHtml(feature.properties));
        layer.on("mouseover", () => layer.setStyle({ weight: 3.2, fillOpacity: 0.42 }));
        layer.on("mouseout", () => blockLayer.resetStyle(layer));
      }
    }).addTo(map);

    const pointLayer = L.layerGroup();
    for (const record of DATA.records) {
      const title = record.road_address || record.jibun_address || record.address || "";
      L.circleMarker([record.lat, record.lon], {
        radius: Math.max(3, Math.min(7, Math.sqrt(record.orders || 1) + 2)),
        color: "#0f172a",
        fillColor: "#2563eb",
        fillOpacity: 0.78,
        weight: 0.8
      }).bindPopup(`
        <div><b>오더:</b> ${fmt.format(record.orders || 0)}건</div>
        <div><b>블럭:</b> ${record.short_id}</div>
        <div>${title}</div>
        ${record.hosu ? `<div>${record.hosu}</div>` : ""}
      `).addTo(pointLayer);
    }
    pointLayer.addTo(map);

    const labelLayer = L.layerGroup();
    for (const item of DATA.labels) {
      L.marker([item.lat, item.lon], {
        interactive: false,
        icon: L.divIcon({
          className: "",
          html: `<div class="block-label">${item.short_id}<br>${fmt.format(item.orders)}</div>`,
          iconSize: [1, 1],
          iconAnchor: [0, 0]
        })
      }).addTo(labelLayer);
    }

    const neighborLayer = L.layerGroup();
    for (const edge of DATA.neighbor_edges) {
      L.polyline(edge.coords, {
        color: edge.relation === "touches" ? "#16a34a" : "#0891b2",
        weight: edge.relation === "touches" ? 1.4 : 1,
        opacity: edge.relation === "touches" ? 0.72 : 0.38,
        dashArray: edge.relation === "touches" ? "" : "4 4"
      }).bindTooltip(`${edge.relation} / ${edge.distance.toFixed(1)}m / 공유 ${edge.shared.toFixed(1)}m`).addTo(neighborLayer);
    }

    function renderStats() {
      const s = DATA.summary;
      const rows = [
        ["오더", `${fmt.format(s.total_orders)}건`],
        ["주소", `${fmt.format(s.address_count)}개`],
        ["블럭", `${fmt.format(s.block_count)}개`],
        ["단독 블럭", `${fmt.format(s.single_block_count)}개`],
        ["최대 오더", `${fmt.format(s.max_orders)}건`],
        ["최대 필지", `${fmt.format(s.max_parcels)}개`],
        ["무인접 블럭", `${fmt.format(s.blocks_without_neighbors)}개`],
        ["병합 edge", `${fmt.format(s.union_edges)}개`]
      ];
      document.getElementById("stats").innerHTML = rows.map(([label, value]) => `
        <div class="stat"><span>${label}</span><strong>${value}</strong></div>
      `).join("");
    }

    function renderList() {
      const items = DATA.features
        .map((feature) => feature.properties)
        .sort((a, b) => (b.orders || 0) - (a.orders || 0) || (b.parcel_count || 0) - (a.parcel_count || 0))
        .slice(0, 80);
      document.getElementById("list").innerHTML = items.map((item) => `
        <button class="row" data-block="${item.block_id}">
          <strong>${item.short_id} · ${fmt.format(item.orders || 0)}건</strong>
          <span>${item.dong || ""} · 주소 ${fmt.format(item.address_count || 0)}개 · 필지 ${fmt.format(item.parcel_count || 0)}개 · 인접 ${fmt.format(item.neighbor_count || 0)}개</span>
        </button>
      `).join("");
      document.querySelectorAll("[data-block]").forEach((button) => {
        button.addEventListener("click", () => {
          const layer = layerById.get(button.dataset.block);
          if (!layer) return;
          map.fitBounds(layer.getBounds(), { padding: [42, 42], maxZoom: 18 });
          layer.openPopup();
        });
      });
    }

    document.getElementById("togglePoints").addEventListener("change", (event) => {
      event.target.checked ? pointLayer.addTo(map) : pointLayer.remove();
    });
    document.getElementById("toggleLabels").addEventListener("change", (event) => {
      event.target.checked ? labelLayer.addTo(map) : labelLayer.remove();
    });
    document.getElementById("toggleNeighbors").addEventListener("change", (event) => {
      event.target.checked ? neighborLayer.addTo(map) : neighborLayer.remove();
    });

    renderStats();
    renderList();
  </script>
</body>
</html>
"""


def write_map(scope: dict[str, Any], summary: dict[str, Any] | None, output_dir: Path) -> Path:
    center = str(scope.get("center") or "CENTER")
    month = str(scope.get("month") or "")
    data = build_payload(scope, summary)
    title = f"{center} {month} 개선 SHP 블럭 경계"
    page = (
        HTML_TEMPLATE
        .replace("__TITLE__", html.escape(title, quote=True))
        .replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"assignment_blocks_{center}.html"
    path.write_text(page, encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate selected center block-boundary visualization HTML files.")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--centers", nargs="+", default=list(DEFAULT_CENTERS))
    args = parser.parse_args()

    payload = json.loads(args.data.read_text(encoding="utf-8"))
    scopes = payload.get("scopes") or {}
    summaries = payload.get("summaries") or {}
    centers = {str(center).upper() for center in args.centers}

    written: list[Path] = []
    for key, scope in scopes.items():
        center = str(scope.get("center") or "").upper()
        if center not in centers:
            continue
        written.append(write_map(scope, summaries.get(key), args.output_dir))

    missing = centers - {path.stem.rsplit("_", 1)[-1].upper() for path in written}
    if missing:
        raise SystemExit(f"Missing center data: {', '.join(sorted(missing))}")

    for path in written:
        print(path)


if __name__ == "__main__":
    main()
