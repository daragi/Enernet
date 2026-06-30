from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import sys
import threading
import time
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List
from urllib.parse import quote

import pandas as pd
import requests
import uvicorn
from fastapi import FastAPI, File, UploadFile, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from openpyxl import Workbook

app = FastAPI(title="Enernet Safety Assignment Web Server")
app.add_middleware(GZipMiddleware, minimum_size=1000)

# 경로 설정
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
WORKBOOK_DIR = BASE_DIR / "uploaded_workbooks"
GEOCODE_DIR = BASE_DIR / "geocodes"
GEOCODE_CACHE_PATH = GEOCODE_DIR / "geocoding.json"
API_KEYS_PATH = BASE_DIR / "api_keys.json"
APP_JSON_PATH = BASE_DIR / "process_geocode.json"
if not APP_JSON_PATH.exists():
    APP_JSON_PATH = BASE_DIR / "safety_assignment_app_data.json"
MAP_HTML_PATH = BASE_DIR / "daejeon_map.html"
if not MAP_HTML_PATH.exists():
    MAP_HTML_PATH = BASE_DIR / "safety_single_house_map.html"

WORKBOOK_DIR.mkdir(parents=True, exist_ok=True)
GEOCODE_DIR.mkdir(parents=True, exist_ok=True)

# 상수 및 매핑 규칙
SHEET_NAME = "안전점검"
ADDRESS_COLUMN = "주소"
TARGET_CODES = {"H051", "H071", "H072", "H073", "H074", "H075"}
FILE_PATTERN = re.compile(r"^(H\d{3})_(\d{4}\.\d{2})_안전점검\.xlsx$")
GEOCODE_URL = "https://maps.apigw.ntruss.com/map-geocode/v2/geocode"
REQUEST_DELAY_SECONDS = 0.02
TIMEOUT_SECONDS = 15
USE_PREFIX = "단독주택"
EARTH_RADIUS_M = 6371000.0
MAX_NEIGHBOR_DISTANCE_M = 220.0
MAX_RECORD_NEIGHBORS = 6
MAX_BLOCK_ADDRESSES = 80

CENTER_COLORS = {
    "H051": "#0f766e",
    "H071": "#2563eb",
    "H072": "#16a34a",
    "H073": "#7c3aed",
    "H074": "#ea580c",
    "H075": "#dc2626",
}


@dataclass
class AddressAggregate:
    address: str
    row_count: int
    centers: list[str]
    files: list[str]
    jibun_hint: str


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, node: int) -> int:
        while self.parent[node] != node:
            self.parent[node] = self.parent[self.parent[node]]
            node = self.parent[node]
        return node

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            self.parent[left_root] = right_root
        elif self.rank[left_root] > self.rank[right_root]:
            self.parent[right_root] = left_root
        else:
            self.parent[right_root] = left_root
            self.rank[left_root] += 1


# 텍스트 노멀라이즈 및 전처리 헬퍼 함수군
def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return " ".join(str(value).replace("\u3000", " ").split()).strip()


def format_counter(counter: Counter[str]) -> dict[str, int]:
    return {key: int(value) for key, value in counter.most_common() if key}


def base_address(address: str) -> str:
    return normalize_text(re.sub(r"\s*\([^)]*\)", "", address))


def parse_street_stem(address: str) -> str:
    road = normalize_text(address.split("(")[0])
    match = re.match(r"^(.*?)(?:\s+\d[\d-]*)?$", road)
    return normalize_text(match.group(1) if match else road)


def parse_jibun_parts(address: str) -> tuple[str, int | None, int | None]:
    match = re.search(r"\(([^)]+)\)", address)
    if not match:
        return "", None, None

    inner = normalize_text(match.group(1))
    tokens = inner.split()
    dong = tokens[0] if tokens else ""
    lot_token = ""
    for token in reversed(tokens):
        if re.fullmatch(r"\d+(?:-\d+)?", token):
            lot_token = token
            break

    if not lot_token:
        return dong, None, None

    if "-" in lot_token:
        main, sub = lot_token.split("-", 1)
        try:
            return dong, int(main), int(sub)
        except ValueError:
            return dong, None, None
    try:
        return dong, int(lot_token), None
    except ValueError:
        return dong, None, None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def simplify_parenthetical(inner: str) -> str:
    tokens = normalize_text(inner).split()
    if not tokens:
        return ""

    collected: list[str] = []
    for token in tokens:
        collected.append(token)
        if re.search(r"\d", token):
            break
    return " ".join(collected)


def extract_locality_prefix(address: str) -> str:
    outer = normalize_text(address.split("(", 1)[0])
    tokens = outer.split()
    locality: list[str] = []

    for token in tokens:
        if re.search(r"\d", token):
            break
        if token.endswith(("로", "길")):
            break
        locality.append(token)

    return " ".join(locality)


def extract_jibun_hint(address: str) -> str:
    match = re.search(r"\(([^()]+)\)", address)
    if not match:
        return ""

    inner = simplify_parenthetical(match.group(1))
    locality = extract_locality_prefix(address)
    candidates = []

    if locality:
        candidates.append(f"{locality} {inner}")
    candidates.append(inner)

    for candidate in candidates:
        candidate = normalize_text(candidate)
        if candidate:
            return candidate

    return ""


def build_query_candidates(address: str, jibun_hint: str) -> list[str]:
    queries: list[str] = []

    def add(value: str) -> None:
        value = normalize_text(value)
        if value and value not in queries:
            queries.append(value)

    outer = normalize_text(re.sub(r"\s*\([^()]*\)", "", address))
    locality = extract_locality_prefix(address)

    if jibun_hint:
        add(jibun_hint)
    add(address)
    add(outer)

    if jibun_hint and locality and not jibun_hint.startswith(locality):
        add(f"{locality} {jibun_hint}")

    return queries


def load_api_keys() -> tuple[str, str, str]:
    if not API_KEYS_PATH.exists():
        return "", "", ""
    try:
        payload = json.loads(API_KEYS_PATH.read_text(encoding="utf-8"))
        map_key_id = normalize_text(payload.get("NAVER_MAPS_NCP_KEY_ID"))
        geocode_key_id = normalize_text(payload.get("NAVER_GEOCODE_KEY_ID"))
        geocode_key = normalize_text(payload.get("NAVER_GEOCODE_KEY"))
        return map_key_id, geocode_key_id, geocode_key
    except Exception:
        return "", "", ""


# 고유 주소 취합
def collect_unique_addresses(paths: list[Path]) -> list[AddressAggregate]:
    grouped: dict[str, dict[str, Any]] = {}

    for path in paths:
        try:
            frame = pd.read_excel(path, sheet_name=SHEET_NAME, usecols=[ADDRESS_COLUMN, "센터"])
        except Exception as e:
            print(f"Error loading Excel {path.name}: {e}")
            continue

        frame[ADDRESS_COLUMN] = frame[ADDRESS_COLUMN].map(normalize_text)
        frame["센터"] = frame["센터"].map(normalize_text)
        frame = frame[frame[ADDRESS_COLUMN] != ""]

        for row in frame.itertuples(index=False):
            address = getattr(row, ADDRESS_COLUMN)
            center = getattr(row, "센터")
            item = grouped.setdefault(
                address,
                {
                    "row_count": 0,
                    "centers": set(),
                    "files": set(),
                },
            )
            item["row_count"] = int(item["row_count"]) + 1
            item["centers"].add(center or path.name[:4])
            item["files"].add(path.name)

    aggregates: list[AddressAggregate] = []
    for address, item in grouped.items():
        aggregates.append(
            AddressAggregate(
                address=address,
                row_count=int(item["row_count"]),
                centers=sorted(item["centers"]),
                files=sorted(item["files"]),
                jibun_hint=extract_jibun_hint(address),
            )
        )

    aggregates.sort(key=lambda item: (-item.row_count, item.address))
    return aggregates


# 네이버 지오코딩 API 요청
def geocode_with_naver(
    session: requests.Session,
    address: AddressAggregate,
    key_id: str,
    key: str,
) -> dict[str, Any]:
    headers = {
        "X-NCP-APIGW-API-KEY-ID": key_id,
        "X-NCP-APIGW-API-KEY": key,
    }
    errors: list[str] = []

    for query in build_query_candidates(address.address, address.jibun_hint):
        try:
            response = session.get(
                GEOCODE_URL,
                params={"query": query},
                headers=headers,
                timeout=TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            errors.append(f"{query}: {exc}")
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        results = payload.get("addresses", [])
        if results:
            first = results[0]
            return {
                "status": "success",
                "query_used": query,
                "lat": float(first["y"]),
                "lon": float(first["x"]),
                "roadAddress": normalize_text(first.get("roadAddress")),
                "jibunAddress": normalize_text(first.get("jibunAddress")),
                "englishAddress": normalize_text(first.get("englishAddress")),
            }

        errors.append(f"{query}: no_result")
        time.sleep(REQUEST_DELAY_SECONDS)

    return {
        "status": "no_result",
        "query_used": "",
        "lat": None,
        "lon": None,
        "roadAddress": "",
        "jibunAddress": "",
        "englishAddress": "",
        "error": " | ".join(errors[-4:]),
    }


def load_existing_geocode_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cache: dict[str, dict[str, Any]] = {}
        for item in payload.get("records", []):
            address = normalize_text(item.get("address"))
            if address:
                cache[address] = item
        return cache
    except Exception:
        return {}


def list_matching_workbooks() -> list[tuple[Path, str, str]]:
    matches: list[tuple[Path, str, str]] = []
    for path in sorted(WORKBOOK_DIR.glob("H*.xlsx")):
        match = FILE_PATTERN.match(path.name)
        if not match:
            continue
        center, month = match.groups()
        if center not in TARGET_CODES:
            continue
        matches.append((path, center, month))
    return matches


# 앱 데이터셋 빌드 로직
def load_filtered_rows(workbooks: list[tuple[Path, str, str]]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path, center_from_name, month in workbooks:
        frame = pd.read_excel(path, sheet_name=SHEET_NAME)
        frame["source_file"] = path.name
        frame["month"] = month
        if "센터" not in frame.columns:
            frame["센터"] = center_from_name
        frames.append(frame)

    if not frames:
        raise FileNotFoundError("No matching workbooks loaded")

    data = pd.concat(frames, ignore_index=True)
    for column in ["센터", "용도", "주소", "호수", "건물번호", "세대번호", "설치유형", "특정유형"]:
        if column not in data.columns:
            data[column] = ""
        data[column] = data[column].map(normalize_text)

    data = data[data["용도"].str.startswith(USE_PREFIX)].copy()
    return data


def load_geocode_reference(geocode_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, tuple[float, float]]]:
    if not geocode_path.exists():
        return {}, {}, {}

    payload = json.loads(geocode_path.read_text(encoding="utf-8"))
    exact: dict[str, dict[str, Any]] = {}
    base_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    dong_points: dict[str, list[tuple[float, float]]] = defaultdict(list)

    for item in payload.get("records", []):
        if item.get("status") != "success":
            continue
        address = normalize_text(item.get("address"))
        jibun_address = normalize_text(item.get("jibunAddress"))
        lat = item.get("lat")
        lon = item.get("lon")
        if not address or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue

        dong, _, _ = parse_jibun_parts(address)
        if not dong and jibun_address:
            tokens = jibun_address.replace("대전광역시", "").split()
            dong = normalize_text(tokens[1] if len(tokens) > 1 else "")

        coord = {
            "lat": float(lat),
            "lon": float(lon),
            "dong": dong,
            "coord_source": "naver_geocode",
            "jibun_address": jibun_address,
            "road_address": normalize_text(item.get("roadAddress")),
            "query_used": normalize_text(item.get("query_used")),
        }
        exact[address] = coord
        base_index[base_address(address)].append(coord)
        if dong:
            dong_points[dong].append((float(lat), float(lon)))

    dong_centers: dict[str, tuple[float, float]] = {}
    for dong, points in dong_points.items():
        dong_centers[dong] = (
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        )

    return exact, dict(base_index), dong_centers


def estimate_coord(address: str, dong: str, dong_centers: dict[str, tuple[float, float]]) -> dict[str, Any]:
    base_lat, base_lon = dong_centers.get(dong, (36.3504119, 127.3845475))
    digest = int.from_bytes(address.encode("utf-8"), "little", signed=False)
    lat_offset = ((digest % 10000) / 10000.0 - 0.5) * 0.006
    lon_offset = (((digest // 10000) % 10000) / 10000.0 - 0.5) * 0.006
    return {
        "lat": base_lat + lat_offset,
        "lon": base_lon + lon_offset,
        "dong": dong,
        "coord_source": "dong_centroid_estimate",
        "legacy_block_id": "",
        "jibun_address": "",
        "road_address": "",
        "query_used": "",
    }


def resolve_coord(
    address: str,
    dong: str,
    geocode_exact: dict[str, dict[str, Any]],
    geocode_base: dict[str, list[dict[str, Any]]],
    dong_centers: dict[str, tuple[float, float]],
) -> dict[str, Any]:
    exact = geocode_exact.get(address)
    if exact:
        return dict(exact)

    geocode_matches = geocode_base.get(base_address(address), [])
    if len(geocode_matches) == 1:
        coord = dict(geocode_matches[0])
        coord["coord_source"] = "naver_geocode_base_match"
        return coord

    return estimate_coord(address, dong, dong_centers)


def aggregate_scope_rows(
    data: pd.DataFrame,
    geocode_exact: dict[str, dict[str, Any]],
    geocode_base: dict[str, list[dict[str, Any]]],
    dong_centers: dict[str, tuple[float, float]],
) -> list[dict[str, Any]]:
    groups = data.groupby(["month", "센터", "주소"], dropna=False, sort=True)
    records: list[dict[str, Any]] = []

    for record_id, ((month, center, address), group) in enumerate(groups, start=1):
        road_stem = parse_street_stem(address)
        dong, lot_main, lot_sub = parse_jibun_parts(address)
        coord = resolve_coord(
            address,
            dong,
            geocode_exact,
            geocode_base,
            dong_centers,
        )

        use_counts = Counter(group["용도"])
        install_counts = Counter(group["설치유형"])
        specific_counts = Counter(group["특정유형"])
        household_count = int(group["세대번호"].nunique())

        records.append(
            {
                "id": record_id,
                "scope_key": f"{month}|{center}",
                "month": month,
                "center": center,
                "address": address,
                "road_stem": road_stem,
                "dong": coord.get("dong") or dong,
                "lot_main": lot_main,
                "lot_sub": lot_sub,
                "lat": float(coord["lat"]),
                "lon": float(coord["lon"]),
                "orders": int(len(group)),
                "households": household_count,
                "coord_source": coord.get("coord_source", "unknown"),
                "legacy_block_id": normalize_text(coord.get("legacy_block_id")),
                "jibun_address": normalize_text(coord.get("jibun_address")),
                "road_address": normalize_text(coord.get("road_address")),
                "query_used": normalize_text(coord.get("query_used")),
                "use_counts": format_counter(use_counts),
                "install_counts": format_counter(install_counts),
                "specific_counts": format_counter(specific_counts),
                "building_numbers": sorted({value for value in group["건물번호"] if value}),
                "sample_hosu": sorted({value for value in group["호수"] if value})[:12],
                "source_files": sorted(set(group["source_file"])),
            }
        )

    return records


def affinity_score(left: dict[str, Any], right: dict[str, Any], distance_m: float) -> float:
    score = distance_m
    if left["dong"] != right["dong"]:
        score += 90.0
    if left["road_stem"] != right["road_stem"]:
        score += 50.0
        if distance_m > 70.0:
            score += 45.0
    if left["lot_main"] is not None and right["lot_main"] is not None:
        score += min(abs(left["lot_main"] - right["lot_main"]) * 4.0, 80.0)
    if left["lot_sub"] is not None and right["lot_sub"] is not None:
        score += min(abs(left["lot_sub"] - right["lot_sub"]) * 2.0, 24.0)
    return score


def split_large_component(indices: list[int], scope_records: list[dict[str, Any]]) -> list[list[int]]:
    if len(indices) <= MAX_BLOCK_ADDRESSES:
        return [indices]

    def sort_key(index: int) -> tuple[str, int, int, float, float, str]:
        item = scope_records[index]
        return (
            item["road_stem"],
            item["lot_main"] if item["lot_main"] is not None else 10**9,
            item["lot_sub"] if item["lot_sub"] is not None else 10**9,
            item["lat"],
            item["lon"],
            item["address"],
        )

    ordered = sorted(indices, key=sort_key)
    chunks: list[list[int]] = []
    current = [ordered[0]]

    for index in ordered[1:]:
        previous = scope_records[current[-1]]
        current_item = scope_records[index]
        distance_m = haversine_m(previous["lat"], previous["lon"], current_item["lat"], current_item["lon"])
        lot_gap = None
        if previous["lot_main"] is not None and current_item["lot_main"] is not None:
            lot_gap = abs(previous["lot_main"] - current_item["lot_main"])

        hard_break = (
            previous["road_stem"] != current_item["road_stem"]
            or distance_m > 65.0
            or (lot_gap is not None and lot_gap > 10)
        )
        if hard_break or len(current) >= MAX_BLOCK_ADDRESSES:
            chunks.append(current)
            current = [index]
        else:
            current.append(index)

    if current:
        chunks.append(current)

    return chunks


def build_scope_graph(scope_records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not scope_records:
        return [], []

    union = UnionFind(len(scope_records))
    per_record_neighbors: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_street_group: dict[tuple[str, str], list[int]] = defaultdict(list)

    for index, item in enumerate(scope_records):
        by_street_group[(item["dong"], item["road_stem"])].append(index)

    def lot_sort_key(index: int) -> tuple[int, int, str]:
        item = scope_records[index]
        return (
            item["lot_main"] if item["lot_main"] is not None else 10**9,
            item["lot_sub"] if item["lot_sub"] is not None else 10**9,
            item["address"],
        )

    def should_chain(prev_index: int, current_index: int) -> bool:
        left = scope_records[prev_index]
        right = scope_records[current_index]
        distance_m = haversine_m(left["lat"], left["lon"], right["lat"], right["lon"])
        score = affinity_score(left, right, distance_m)

        lot_gap = None
        if left["lot_main"] is not None and right["lot_main"] is not None:
            lot_gap = abs(left["lot_main"] - right["lot_main"])

        if distance_m <= 22.0 and score <= 95.0:
            return True
        if lot_gap is not None and lot_gap <= 4 and distance_m <= 55.0 and score <= 118.0:
            return True
        if lot_gap is not None and lot_gap <= 8 and distance_m <= 38.0 and score <= 108.0:
            return True
        return False

    for indices in by_street_group.values():
        indices.sort(key=lot_sort_key)
        if not indices:
            continue
        anchor = indices[0]
        for current in indices[1:]:
            if should_chain(anchor, current):
                union.union(anchor, current)
            else:
                anchor = current

    for left_index in range(len(scope_records)):
        left = scope_records[left_index]
        for right_index in range(left_index + 1, len(scope_records)):
            right = scope_records[right_index]
            distance_m = haversine_m(left["lat"], left["lon"], right["lat"], right["lon"])
            if distance_m > MAX_NEIGHBOR_DISTANCE_M:
                continue

            score = affinity_score(left, right, distance_m)
            per_record_neighbors[left["id"]].append(
                {"id": right["id"], "distance_m": round(distance_m, 1), "score": round(score, 1)}
            )
            per_record_neighbors[right["id"]].append(
                {"id": left["id"], "distance_m": round(distance_m, 1), "score": round(score, 1)}
            )

    block_members: dict[int, list[int]] = defaultdict(list)
    for index in range(len(scope_records)):
        block_members[union.find(index)].append(index)

    split_components: list[list[int]] = []
    for indices in block_members.values():
        split_components.extend(split_large_component(indices, scope_records))

    block_list: list[dict[str, Any]] = []
    node_to_block: dict[int, str] = {}

    for block_idx, indices in enumerate(split_components, start=1):
        block_id = f"B{block_idx}"
        lat = sum(scope_records[index]["lat"] for index in indices) / len(indices)
        lon = sum(scope_records[index]["lon"] for index in indices) / len(indices)
        orders = sum(scope_records[index]["orders"] for index in indices)
        address_ids = [scope_records[index]["id"] for index in indices]
        dongs = sorted({scope_records[index]["dong"] for index in indices if scope_records[index]["dong"]})
        streets = sorted({scope_records[index]["road_stem"] for index in indices if scope_records[index]["road_stem"]})

        for address_id in address_ids:
            node_to_block[address_id] = block_id

        block_list.append(
            {
                "id": block_id,
                "centroid": [round(lat, 7), round(lon, 7)],
                "orders": int(orders),
                "address_count": len(indices),
                "address_ids": address_ids,
                "dongs": dongs,
                "street_stems": streets,
                "neighbors": [],
            }
        )

    block_map = {block["id"]: block for block in block_list}
    block_edge_scores: dict[tuple[str, str], tuple[float, float]] = {}

    for record in scope_records:
        block_id = node_to_block[record["id"]]
        for neighbor in per_record_neighbors.get(record["id"], []):
            neighbor_block = node_to_block[neighbor["id"]]
            if neighbor_block == block_id:
                continue
            key = tuple(sorted((block_id, neighbor_block)))
            score = float(neighbor["score"])
            distance = float(neighbor["distance_m"])
            previous = block_edge_scores.get(key)
            if previous is None or score < previous[0]:
                block_edge_scores[key] = (score, distance)

    for (left_id, right_id), (score, distance) in block_edge_scores.items():
        block_map[left_id]["neighbors"].append(
            {"id": right_id, "score": round(score, 1), "distance_m": round(distance, 1)}
        )
        block_map[right_id]["neighbors"].append(
            {"id": left_id, "score": round(score, 1), "distance_m": round(distance, 1)}
        )

    for block in block_list:
        block["neighbors"].sort(key=lambda item: (item["score"], item["distance_m"], item["id"]))
        block["neighbors"] = block["neighbors"][:MAX_RECORD_NEIGHBORS]

    for record in scope_records:
        record["neighbors"] = per_record_neighbors.get(record["id"], [])
        record["neighbors"].sort(key=lambda item: (item["score"], item["distance_m"], item["id"]))
        record["neighbors"] = record["neighbors"][:MAX_RECORD_NEIGHBORS]
        record["block_id"] = node_to_block[record["id"]]

    return scope_records, block_list


# 전처리 & 지오코딩 & 데이터셋 병합 파이프라인
def run_data_processing_pipeline() -> dict[str, Any]:
    workbooks = list_matching_workbooks()
    if not workbooks:
        raise ValueError("업로드된 유효한 엑셀 파일(H*.xlsx)이 없습니다.")

    # 1. 고유 주소 추출
    aggregates = collect_unique_addresses([w[0] for w in workbooks])
    if not aggregates:
        raise ValueError("엑셀 파일에서 읽어들인 유효한 단독주택 주소가 없습니다.")

    # 대표 월 설정 (파일명에 따라 설정)
    month_tokens = [w[2] for w in workbooks]
    month_counter = Counter(month_tokens)
    month_token = month_counter.most_common(1)[0][0]
    geocode_path = GEOCODE_CACHE_PATH
    cache = load_existing_geocode_cache(geocode_path)

    # 2. 지오코딩 API 호출
    map_key_id, geocode_key_id, geocode_key = load_api_keys()
    
    success_count = 0
    failed_count = 0
    
    if map_key_id and geocode_key_id and geocode_key:
        session = requests.Session()
        for index, item in enumerate(aggregates, start=1):
            cached = cache.get(item.address)
            if cached and cached.get("status") == "success":
                record = dict(cached)
            else:
                result = geocode_with_naver(session, item, geocode_key_id, geocode_key)
                record = {
                    "address": item.address,
                    "row_count": item.row_count,
                    "centers": item.centers,
                    "files": item.files,
                    "jibun_hint": item.jibun_hint,
                    **result,
                }
                cache[item.address] = record

            if record.get("status") == "success":
                success_count += 1
            else:
                failed_count += 1
    else:
        # 네이버 API 키가 없을 시 기존 캐시만 유지하고 나머지는 미매핑 에러로 분류
        print("네이버 지오코딩 API 키 정보가 부족합니다. 캐시된 좌표만 사용합니다.")
        for item in aggregates:
            cached = cache.get(item.address)
            if cached and cached.get("status") == "success":
                success_count += 1
            else:
                cache[item.address] = {
                    "address": item.address,
                    "row_count": item.row_count,
                    "centers": item.centers,
                    "files": item.files,
                    "jibun_hint": item.jibun_hint,
                    "status": "no_result",
                    "lat": None,
                    "lon": None,
                    "roadAddress": "",
                    "jibunAddress": "",
                    "englishAddress": "",
                    "error": "Naver maps api keys unavailable",
                }
                failed_count += 1

    # 지오코딩 결과 파일 저장
    geocode_payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "month_token": month_token,
        "source_files": [w[0].name for w in workbooks],
        "success_count": success_count,
        "failed_count": failed_count,
        "records": sorted(cache.values(), key=lambda x: str(x.get("address", ""))),
    }
    geocode_path.write_text(json.dumps(geocode_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3. 배정 최적화 데이터셋 파일 생성 (Graph & Block Build)
    data = load_filtered_rows(workbooks)
    geocode_exact, geocode_base, dong_centers = load_geocode_reference(geocode_path)

    records = aggregate_scope_rows(
        data,
        geocode_exact,
        geocode_base,
        dong_centers,
    )

    grouped_records = defaultdict(list)
    for record in records:
        grouped_records[record["scope_key"]].append(record)

    scopes = {}
    months = sorted(data["month"].dropna().unique().tolist())
    centers = sorted(data["센터"].dropna().unique().tolist())

    for scope_key, scope_records in grouped_records.items():
        scope_month, scope_center = scope_key.split("|", 1)
        enriched_records, blocks = build_scope_graph(scope_records)
        total_orders = sum(item["orders"] for item in enriched_records)
        scopes[scope_key] = {
            "month": scope_month,
            "center": scope_center,
            "color": CENTER_COLORS.get(scope_center, "#334155"),
            "total_orders": int(total_orders),
            "total_addresses": len(enriched_records),
            "records": enriched_records,
            "blocks": blocks,
        }

    # 기존 safety_assignment_app_data.json 데이터가 있으면 불러와서 병합
    existing_months = []
    existing_centers = []
    existing_scopes = {}
    
    if APP_JSON_PATH.exists():
        try:
            existing_payload = json.loads(APP_JSON_PATH.read_text(encoding="utf-8"))
            existing_months = existing_payload.get("months", [])
            existing_centers = existing_payload.get("centers", [])
            existing_scopes = existing_payload.get("scopes", {})
        except Exception as e:
            print(f"기존 데이터셋 파싱 실패 (무시하고 새로 생성): {e}")

    merged_scopes = existing_scopes.copy()
    merged_scopes.update(scopes)
    
    merged_months = sorted(list(set(existing_months + months)))
    merged_centers = sorted(list(set(existing_centers + centers)))

    final_payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "months": merged_months,
        "centers": merged_centers,
        "scopes": merged_scopes,
        "filename_rule": "H[센터명]_연월_안전점검.xlsx",
        "coord_priority": ["naver_geocode", "naver_geocode_base_match", "dong_centroid_estimate"],
        "geocode_cache_file": geocode_path.name,
    }

    # safety_assignment_app_data.json 파일 생성
    APP_JSON_PATH.write_text(json.dumps(final_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "months": months,
        "centers": centers,
        "scopes_count": len(scopes),
        "total_addresses": len(records),
        "success_count": success_count,
        "failed_count": failed_count
    }


# 엑셀 다운로드 빌더
def build_workbook_bytes(rows: list[list[object]], sheet_name: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name[:31] if sheet_name else "Assignments"

    for row in rows:
        sheet.append(list(row))

    for column_cells in sheet.columns:
        max_length = 0
        column_letter = column_cells[0].column_letter
        for cell in column_cells:
            value = "" if cell.value is None else str(cell.value)
            max_length = max(max_length, len(value))
        sheet.column_dimensions[column_letter].width = min(max(max_length + 2, 10), 40)

    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


# FastAPI HTTP 라우터 엔드포인트 정의
@app.get("/", response_class=HTMLResponse)
def get_map_page(response: Response, v: str = None):
    html_path = MAP_HTML_PATH
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="safety_single_house_map.html를 찾을 수 없습니다.")
    content = html_path.read_text(encoding="utf-8")
    # ETag based on file modification time for accurate cache invalidation
    mtime = str(int(html_path.stat().st_mtime))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["ETag"] = f'"{mtime}"'
    return content




@app.get("/admin", response_class=HTMLResponse)
def get_admin_page(response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    admin_path = BASE_DIR / "admin.html"
    if not admin_path.exists():
        raise HTTPException(status_code=404, detail="admin.html를 찾을 수 없습니다.")
    return admin_path.read_text(encoding="utf-8")


@app.get("/safety_assignment_app_data.json")
def get_app_data(response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    if not APP_JSON_PATH.exists():
        return JSONResponse(status_code=404, content={"error": "구동 데이터셋이 아직 준비되지 않았습니다. 관리자 페이지(/admin)에서 엑셀을 업로드하여 데이터를 구축하세요."})
    return FileResponse(APP_JSON_PATH, media_type="application/json")


@app.post("/api/upload")
async def upload_workbooks(files: List[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="업로드할 파일을 선택해주세요.")

    saved_files = []
    
    # 1. 업로드된 파일 유효성 검사 및 저장
    for file in files:
        if not file.filename.endswith(".xlsx"):
            continue
        match = FILE_PATTERN.match(file.filename)
        if not match:
            continue
        
        target_path = WORKBOOK_DIR / file.filename
        content = await file.read()
        target_path.write_bytes(content)
        saved_files.append(file.filename)

    if not saved_files:
        raise HTTPException(status_code=400, detail="유효한 점검 데이터 엑셀 파일명(H[센터명]_[연월]_안전점검.xlsx)이 매칭되지 않았습니다.")

    # 2. 지오코딩 및 병합 파이프라인 구동
    try:
        result = await run_in_threadpool(run_data_processing_pipeline)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"데이터 변환 처리 중 오류가 발생했습니다: {str(e)}")

    return JSONResponse(content={
        "status": "success",
        "processed_files": saved_files,
        "pipeline_result": result
    })


@app.post("/api/rebuild")
async def rebuild_from_api_map():
    try:
        result = await run_in_threadpool(run_data_processing_pipeline)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"데이터 갱신 중 오류가 발생했습니다: {str(e)}")

    return JSONResponse(content={
        "status": "success",
        "source_dir": str(WORKBOOK_DIR),
        "pipeline_result": result
    })


@app.post("/api/export_assignments_xlsx")
async def export_assignments(payload: dict):
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=400, detail="rows is required")

    normalized_rows = []
    for row in rows:
        if isinstance(row, list):
            normalized_rows.append(row)
        else:
            raise HTTPException(status_code=400, detail="Each row must be a list")

    filename = str(payload.get("filename", "assignment_result"))
    # 특수문자 제거
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", filename).strip(" .") + ".xlsx"
    ascii_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._") or "assignment_result.xlsx"
    sheet_name = str(payload.get("sheet_name") or "Assignments")

    workbook_bytes = build_workbook_bytes(normalized_rows, sheet_name)
    
    headers = {
        "Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename)}"
    }
    return StreamingResponse(
        io.BytesIO(workbook_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers
    )


@app.get("/api/config")
def get_config():
    map_key_id, _, _ = load_api_keys()
    # NCP 클라이언트 ID가 로컬 config에 세팅되어 있는지 여부만 반환
    return {
        "naver_maps_key_id_set": bool(map_key_id),
        "api_keys_path_exists": API_KEYS_PATH.exists()
    }


def start_server():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()

    if getattr(sys, "frozen", False) or args.open_browser:
        url = f"http://127.0.0.1:{args.port}/"
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    print(f"Uvicorn serving Enernet App on http://{args.bind}:{args.port}")
    uvicorn.run(app, host=args.bind, port=args.port)


if __name__ == "__main__":
    start_server()
