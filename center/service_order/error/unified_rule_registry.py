"""Transactional helpers for the consolidated rule registries.

The module owns only the mutable portions of ``normal_rules.json`` and the
atomic persistence of both consolidated rule documents.  Manual, scoped and
legacy-collision origins are treated as immutable when automatic-normal
learning evidence is replaced.

Callers still need to hold the dashboard's registry lock for a complete
read/modify/write transaction.  ``expected_sha256`` provides an additional
optimistic-concurrency guard against a stale writer.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Literal
import uuid


BASE_DIR = Path(__file__).resolve().parent
NORMAL_RULES_PATH = BASE_DIR / "json" / "normal_rules.json"
ERROR_RULES_PATH = BASE_DIR / "json" / "error_rules.json"
NORMAL_RULES_VIEW_PATH = BASE_DIR / "json" / "normal_rules_보기용.json"
AUTO_NORMAL_SOURCE = "auto_normal_pattern.json"
AUTO_NORMAL_ROLE = "auto_normal"
NORMAL_ACTIVE_STATUSES = frozenset({"active", "enabled", "확정", "정상"})
ERROR_DECISIONS = frozenset(
    {"auto_error", "review_lock", "audit_only", "inactive", "evidence_only"}
)
NORMAL_CATALOG_KEY = "소분류별_규칙"
NORMAL_CATALOG_GUIDE_KEY = "보기용_안내"
NORMAL_CATALOG_TYPE_ORDER = (
    "단일키워드",
    "2어절문구",
    "3~5어절문구",
    "비연속조합",
    "반복문장",
)
NORMAL_CATALOG_ROLE_ORDER = (
    "정상",
    "소분류우선",
    "일반충돌",
    "강한충돌",
    "후보잠금",
    "자동수집제안",
    "기타",
)


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _document_digest(document: dict[str, object]) -> str:
    payload = deepcopy(document)
    payload.pop("document_sha256", None)
    return _sha256_bytes(_stable_json(payload).encode("utf-8"))


def _file_digest(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_document(
    source: Path | dict[str, object],
    *,
    label: str,
) -> dict[str, object]:
    if isinstance(source, Path):
        if not source.is_file():
            raise FileNotFoundError(source)
        value = json.loads(source.read_text(encoding="utf-8"))
    else:
        value = deepcopy(source)
    if not isinstance(value, dict) or not isinstance(value.get("rules"), list):
        raise ValueError(f"{label}의 최상위 rules 배열이 필요합니다.")
    return value


def _normalize_text(value: object) -> str:
    text = str(value).lower()
    text = re.sub(r"\d+", " ", text)
    text = re.sub(r"[^가-힣a-z]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normal_match(pattern_type: str, raw_pattern: str) -> dict[str, object]:
    if pattern_type == "비연속조합":
        raw_parts = [part.strip() for part in raw_pattern.split("+")]
        if len(raw_parts) != 2:
            raise ValueError(f"비연속조합은 두 부분이어야 합니다: {raw_pattern}")
        parts = sorted(_normalize_text(part) for part in raw_parts)
        if any(not part for part in parts):
            raise ValueError(f"비연속조합에 빈 부분이 있습니다: {raw_pattern}")
        return {
            "kind": "unordered_parts",
            "value": " + ".join(parts),
            "parts": parts,
        }
    value = _normalize_text(raw_pattern)
    if not value:
        raise ValueError(f"정규화 후 빈 패턴입니다: {raw_pattern}")
    if pattern_type == "반복문장":
        kind = "exact_normalized"
    elif len(value.split()) == 1:
        kind = "token"
    else:
        kind = "contiguous"
    return {"kind": kind, "value": value, "tokens": value.split()}


def _normal_key(owner: str, pattern: dict[str, object]) -> str:
    return _stable_json(
        {
            "source_subcategory": owner,
            "kind": str(pattern.get("kind") or ""),
            "value": str(pattern.get("value") or ""),
        }
    )


def _normal_rule_id(key: str) -> str:
    return f"normal_{_sha256_bytes(key.encode('utf-8'))[:20]}"


def _origin_items(record: dict[str, object]) -> list[dict[str, object]]:
    evidence = record.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError(
            f"정상 규칙 {record.get('rule_id')}의 evidence가 객체가 아닙니다."
        )
    origins = evidence.get("origins")
    if not isinstance(origins, list):
        raise ValueError(
            f"정상 규칙 {record.get('rule_id')}의 evidence.origins가 배열이 아닙니다."
        )
    if any(not isinstance(origin, dict) for origin in origins):
        raise ValueError(
            f"정상 규칙 {record.get('rule_id')}에 객체가 아닌 origin이 있습니다."
        )
    return origins  # type: ignore[return-value]


def _is_auto_origin(origin: dict[str, object]) -> bool:
    return (
        str(origin.get("source") or "").strip() == AUTO_NORMAL_SOURCE
        or str(origin.get("role") or "").strip() == AUTO_NORMAL_ROLE
    )


def _dominant_auto_record(record: dict[str, object]) -> bool:
    status = str(record.get("status") or "").strip().casefold()
    pattern = _normalize_text(record.get("pattern") or "")
    return (
        status in {"active", "proposed"}
        and len(pattern.split()) >= 3
        and int(record.get("normal_support") or 0) >= 10
        and int(record.get("other_subcategory_frequency") or 0) == 0
        and int(record.get("confirmed_error_pattern_hits") or 0) == 0
    )


def _auto_origin(record: dict[str, object]) -> dict[str, object]:
    status = str(record.get("status") or "").strip().casefold()
    pattern_type = str(record.get("pattern_type") or "").strip()
    raw_pattern = str(record.get("pattern") or "").strip()
    dominant = _dominant_auto_record(record)
    metrics = {
        key: deepcopy(value)
        for key, value in record.items()
        if key not in {"source_subcategory", "pattern_type", "pattern", "status"}
    }
    return {
        "source": AUTO_NORMAL_SOURCE,
        "status": status,
        "role": AUTO_NORMAL_ROLE,
        "scope": (
            "cross_subcategory_candidate_guard"
            if dominant
            else "source_subcategory_override"
            if status == "active"
            else "learning_evidence_only"
        ),
        "strength": (
            "candidate_guard"
            if dominant
            else "override"
            if status == "active"
            else "evidence_only"
        ),
        "legacy_pattern_type": pattern_type,
        "raw_pattern": raw_pattern,
        "evidence": metrics,
    }


def _new_normal_rule(
    owner: str,
    pattern: dict[str, object],
) -> dict[str, object]:
    key = _normal_key(owner, pattern)
    return {
        "rule_id": _normal_rule_id(key),
        "source_subcategory": owner,
        "pattern": deepcopy(pattern),
        "source": [],
        "status": "proposed",
        "scope": {},
        "strength": "evidence_only",
        "evidence": {"origins": []},
        "behavior": {
            "own_normal": False,
            "override_collision": False,
            "cross_collision": "none",
            "dominant_other_candidate": False,
        },
    }


def _recompute_normal_rule(record: dict[str, object]) -> None:
    origins = _origin_items(record)
    own_normal = False
    override = False
    hard = False
    soft = False
    dominant = False
    has_proposed = False
    has_blocked = False

    for item in origins:
        role = str(item.get("role") or "").strip()
        status = str(item.get("status") or "").strip().casefold()
        strength = str(item.get("strength") or "").strip().casefold()
        origin_scope = str(item.get("scope") or "").strip().casefold()
        if role in {"manual_normal", "owner_priority"}:
            own_normal = True
            soft = True
        elif role == "legacy_collision":
            soft = True
        elif role == "scoped_normal" and status in NORMAL_ACTIVE_STATUSES:
            own_normal = True
            override = True
            soft = True
        elif role == AUTO_NORMAL_ROLE and status == "active":
            own_normal = True
            override = True
            soft = True
        if strength == "hard":
            hard = True
        if origin_scope == "cross_subcategory_candidate_guard":
            dominant = True
        has_proposed = has_proposed or status == "proposed"
        has_blocked = has_blocked or status.startswith("blocked")

    cross_collision = "hard" if hard else "soft" if soft else "none"
    record["behavior"] = {
        "own_normal": own_normal,
        "override_collision": override,
        "cross_collision": cross_collision,
        "dominant_other_candidate": dominant,
    }
    record["scope"] = {
        "source_subcategory": record["source_subcategory"],
        "normal_application": "owner_only" if own_normal else "none",
        "cross_subcategory": (
            "candidate_guard"
            if dominant
            else f"{cross_collision}_collision"
            if cross_collision != "none"
            else "none"
        ),
    }
    if override:
        record["strength"] = "override"
    elif dominant:
        record["strength"] = "candidate_guard"
    elif hard:
        record["strength"] = "hard"
    elif soft:
        record["strength"] = "soft"
    else:
        record["strength"] = "evidence_only"

    if own_normal or hard or soft:
        record["status"] = "active"
    elif dominant:
        record["status"] = "active_guard"
    elif has_proposed:
        record["status"] = "proposed"
    elif has_blocked:
        record["status"] = "blocked_error"
    else:
        record["status"] = "inactive"
    record["source"] = sorted(
        {
            str(item.get("source") or "").strip()
            for item in origins
            if str(item.get("source") or "").strip()
        }
    )
    origins.sort(
        key=lambda item: (
            str(item.get("source") or ""),
            str(item.get("legacy_pattern_type") or ""),
            str(item.get("raw_pattern") or ""),
        )
    )


def _recompute_normal_summary(document: dict[str, object]) -> None:
    rules = document["rules"]
    source_counts: Counter[str] = Counter()
    behavior_counts: Counter[str] = Counter()
    origin_count = 0
    for record in rules:
        origins = _origin_items(record)
        origin_count += len(origins)
        for item in origins:
            source = str(item.get("source") or "").strip()
            if source:
                source_counts[source] += 1
        behavior = record.get("behavior")
        if not isinstance(behavior, dict):
            raise ValueError(f"정상 규칙 {record.get('rule_id')}의 behavior가 없습니다.")
        behavior_counts[
            f"own_normal_{str(bool(behavior.get('own_normal'))).lower()}"
        ] += 1
        behavior_counts[f"cross_{behavior.get('cross_collision', 'none')}"] += 1
        behavior_counts[
            f"override_{str(bool(behavior.get('override_collision'))).lower()}"
        ] += 1
        behavior_counts[
            f"dominant_{str(bool(behavior.get('dominant_other_candidate'))).lower()}"
        ] += 1

    previous = document.get("migration_summary")
    if not isinstance(previous, dict):
        previous = {}
    summary = {
        **previous,
        "source_origin_count": origin_count,
        "canonical_rule_count": len(rules),
        "merged_duplicate_origin_count": max(0, origin_count - len(rules)),
        "source_counts": dict(sorted(source_counts.items())),
        "behavior_counts": dict(sorted(behavior_counts.items())),
    }
    document["migration_summary"] = summary


def _normal_catalog_type(record: dict[str, object]) -> str:
    pattern = record.get("pattern") or {}
    if not isinstance(pattern, dict):
        return "기타"
    kind = str(pattern.get("kind") or "").strip()
    tokens = pattern.get("tokens")
    if isinstance(tokens, list):
        token_count = len(tokens)
    else:
        token_count = len(str(pattern.get("value") or "").split())
    if kind == "token":
        return "단일키워드"
    if kind == "contiguous":
        return "2어절문구" if token_count == 2 else "3~5어절문구"
    if kind == "unordered_parts":
        return "비연속조합"
    if kind == "exact_normalized":
        return "반복문장"
    return "기타"


def build_normal_rule_catalog(
    document: dict[str, object],
) -> dict[str, dict[str, dict[str, list[str]]]]:
    """Return a compact, legacy-shaped view grouped by owner and phrase type.

    The catalog is derived data for people reading ``normal_rules.json``.  The
    runtime continues to use the canonical ``rules`` array, so a rule may be
    shown under more than one role when its effective behavior has multiple
    purposes.
    """
    rules = document.get("rules")
    if not isinstance(rules, list):
        raise ValueError("normal_rules.json의 rules 배열이 필요합니다.")
    grouped: dict[str, dict[str, dict[str, set[str]]]] = {}
    for index, raw_record in enumerate(rules):
        if not isinstance(raw_record, dict):
            raise ValueError(f"normal rules[{index}]가 객체가 아닙니다.")
        owner = str(raw_record.get("source_subcategory") or "").strip()
        pattern = raw_record.get("pattern") or {}
        behavior = raw_record.get("behavior") or {}
        if not owner or not isinstance(pattern, dict) or not isinstance(behavior, dict):
            raise ValueError(f"normal rules[{index}]의 보기용 필드가 불완전합니다.")
        value = str(pattern.get("value") or "").strip()
        if not value:
            raise ValueError(f"normal rules[{index}]의 pattern.value가 없습니다.")
        pattern_type = _normal_catalog_type(raw_record)
        roles: list[str] = []
        if bool(behavior.get("own_normal")):
            roles.append("정상")
        if bool(behavior.get("override_collision")):
            roles.append("소분류우선")
        collision = str(behavior.get("cross_collision") or "none").strip().casefold()
        if collision == "soft":
            roles.append("일반충돌")
        elif collision == "hard":
            roles.append("강한충돌")
        if bool(behavior.get("dominant_other_candidate")):
            roles.append("후보잠금")
        has_auto_proposal = any(
            _is_auto_origin(origin)
            and str(origin.get("status") or "").strip().casefold() == "proposed"
            for origin in _origin_items(raw_record)
        )
        if (
            str(raw_record.get("status") or "").strip().casefold() == "proposed"
            or has_auto_proposal
        ):
            roles.append("자동수집제안")
        if not roles:
            roles.append("기타")

        type_node = grouped.setdefault(owner, {}).setdefault(pattern_type, {})
        for role in roles:
            type_node.setdefault(role, set()).add(value)

    result: dict[str, dict[str, dict[str, list[str]]]] = {}
    type_rank = {value: index for index, value in enumerate(NORMAL_CATALOG_TYPE_ORDER)}
    role_rank = {value: index for index, value in enumerate(NORMAL_CATALOG_ROLE_ORDER)}
    for owner in sorted(grouped):
        result[owner] = {}
        for pattern_type in sorted(
            grouped[owner],
            key=lambda value: (type_rank.get(value, len(type_rank)), value),
        ):
            role_values = grouped[owner][pattern_type]
            result[owner][pattern_type] = {
                role: sorted(role_values[role])
                for role in sorted(
                    role_values,
                    key=lambda value: (role_rank.get(value, len(role_rank)), value),
                )
            }
    return result


def _attach_normal_rule_catalog(document: dict[str, object]) -> dict[str, object]:
    prepared = deepcopy(document)
    prepared.pop(NORMAL_CATALOG_GUIDE_KEY, None)
    prepared.pop(NORMAL_CATALOG_KEY, None)
    guide = {
        "설명": "판정용 rules 배열을 소분류와 문구 유형별로 다시 펼친 보기용 목록입니다.",
        "실제판정기준": "서버는 아래 rules 배열을 사용하며 이 목록은 저장할 때 자동 갱신됩니다.",
        "중복표시": "하나의 규칙이 여러 역할을 가지면 각 역할 목록에 중복 표시됩니다.",
        "유형순서": list(NORMAL_CATALOG_TYPE_ORDER),
        "역할": {
            "정상": "해당 소분류에서 정상으로 제외",
            "소분류우선": "타 소분류 충돌이 있어도 현재 소분류 정상 판정을 우선",
            "일반충돌": "다른 소분류에서는 약한 충돌 신호",
            "강한충돌": "다른 소분류에서는 후보로 남기는 강한 충돌 신호",
            "후보잠금": "문맥 충돌 시 유사도 자동 승격을 막고 후보로 유지",
            "자동수집제안": "근거가 더 쌓이기 전까지 판정에 사용하지 않는 제안",
            "기타": "현재 판정 역할이 없는 보존 규칙",
        },
    }
    # Keep the human-readable view at the top of the file when it is opened.
    return {
        NORMAL_CATALOG_GUIDE_KEY: guide,
        NORMAL_CATALOG_KEY: build_normal_rule_catalog(prepared),
        **prepared,
    }


def build_normal_rules_view(document: dict[str, object]) -> dict[str, object]:
    """Build the small, reviewer-facing companion to the runtime registry.

    ``normal_rules.json`` deliberately retains provenance, effective behavior and
    learning evidence because the matcher needs those fields.  They make the
    executable file noisy for a person who only wants to review phrases.  This
    companion contains the same effective rules, grouped by subcategory and
    role, without IDs, source-history or duplicate evidence payloads.
    """
    return {
        "안내": {
            "용도": "보기 전용 정상 규칙 목록입니다. 수정은 normal_rules.json의 rules 또는 관리자 기능에서 합니다.",
            "구성": "소분류 > 문구 유형 > 정상·소분류우선·일반충돌·강한충돌·후보잠금",
            "반영": "normal_rules.json이 저장될 때 자동으로 다시 생성됩니다.",
        },
        "규칙수": len(document.get("rules", [])),
        "생성시각": _now(),
        NORMAL_CATALOG_KEY: build_normal_rule_catalog(document),
    }


def refresh_normal_rules_view(
    document: dict[str, object] | None = None,
    *,
    path: Path = NORMAL_RULES_VIEW_PATH,
) -> Path:
    """Write the compact read-only view atomically and return its path."""
    source = document or _load_document(NORMAL_RULES_PATH, label="normal_rules.json")
    view = build_normal_rules_view(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}_{uuid.uuid4().hex[:8]}_writing.json")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(view, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _max_snapshot_end(registry: dict[str, object]) -> str | None:
    snapshots = registry.get("evidence_snapshots")
    if not isinstance(snapshots, list):
        return None
    values = sorted(
        str(snapshot.get("end") or "")
        for snapshot in snapshots
        if isinstance(snapshot, dict) and str(snapshot.get("end") or "")
    )
    return values[-1] if values else None


def validate_normal_rules_document(document: dict[str, object]) -> dict[str, int]:
    rules = document.get("rules")
    if not isinstance(rules, list):
        raise ValueError("normal_rules.json의 rules 배열이 필요합니다.")
    ids: set[str] = set()
    keys: set[str] = set()
    origin_count = 0
    for index, record in enumerate(rules):
        if not isinstance(record, dict):
            raise ValueError(f"normal rules[{index}]가 객체가 아닙니다.")
        rule_id = str(record.get("rule_id") or "").strip()
        owner = str(record.get("source_subcategory") or "").strip()
        pattern = record.get("pattern")
        if not rule_id or not owner or not isinstance(pattern, dict):
            raise ValueError(f"normal rules[{index}]의 식별 필드가 불완전합니다.")
        if not str(pattern.get("kind") or "") or not str(pattern.get("value") or ""):
            raise ValueError(f"normal rules[{index}]의 pattern이 불완전합니다.")
        if rule_id in ids:
            raise ValueError(f"normal rule_id가 중복됩니다: {rule_id}")
        key = _normal_key(owner, pattern)
        if key in keys:
            raise ValueError(f"정규화상 동일한 정상 규칙이 중복됩니다: {owner}")
        ids.add(rule_id)
        keys.add(key)
        origin_count += len(_origin_items(record))
        behavior = record.get("behavior")
        if not isinstance(behavior, dict):
            raise ValueError(f"normal rules[{index}]의 behavior가 없습니다.")
        if str(behavior.get("cross_collision") or "none") not in {
            "none",
            "soft",
            "hard",
        }:
            raise ValueError(f"normal rules[{index}]의 충돌 강도가 잘못됐습니다.")
    catalog = document.get(NORMAL_CATALOG_KEY)
    if catalog is not None and catalog != build_normal_rule_catalog(document):
        raise ValueError("normal_rules.json의 소분류별 보기 목록이 rules와 다릅니다.")
    return {"rule_count": len(rules), "origin_count": origin_count}


def validate_error_rules_document(document: dict[str, object]) -> dict[str, int]:
    rules = document.get("rules")
    if not isinstance(rules, list):
        raise ValueError("error_rules.json의 rules 배열이 필요합니다.")
    ids: set[str] = set()
    semantic_keys: set[tuple[str, str, str]] = set()
    auto_count = 0
    for index, record in enumerate(rules):
        if not isinstance(record, dict):
            raise ValueError(f"error rules[{index}]가 객체가 아닙니다.")
        rule_id = str(record.get("rule_id") or "").strip()
        owner = str(record.get("source_subcategory") or "").strip()
        pattern = record.get("pattern")
        decision = str(record.get("decision") or "").strip().casefold()
        if (
            not rule_id
            or not owner
            or not isinstance(pattern, dict)
            or not str(pattern.get("kind") or "")
            or not str(pattern.get("value") or "")
        ):
            raise ValueError(f"error rules[{index}]의 식별 필드가 불완전합니다.")
        if decision not in ERROR_DECISIONS:
            raise ValueError(f"error rules[{index}]의 decision이 잘못됐습니다: {decision}")
        if rule_id in ids:
            raise ValueError(f"error rule_id가 중복됩니다: {rule_id}")
        semantic_key = (
            owner,
            str(pattern.get("kind") or ""),
            _normalize_text(pattern.get("value") or ""),
        )
        if semantic_key in semantic_keys:
            raise ValueError(
                "정규화상 동일한 오생성 규칙이 중복됩니다: "
                f"{owner}/{pattern.get('value')}"
            )
        ids.add(rule_id)
        semantic_keys.add(semantic_key)
        auto_count += int(decision == "auto_error")
    return {"rule_count": len(rules), "auto_error_count": auto_count}


def extract_auto_normal_registry(
    source: Path | dict[str, object] = NORMAL_RULES_PATH,
) -> dict[str, object]:
    """Extract a legacy-shaped auto-normal registry from consolidated rules."""
    document = _load_document(source, label="normal_rules.json")
    validate_normal_rules_document(document)
    records: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in document["rules"]:
        owner = str(record.get("source_subcategory") or "").strip()
        for item in _origin_items(record):
            if not _is_auto_origin(item):
                continue
            pattern_type = str(item.get("legacy_pattern_type") or "").strip()
            raw_pattern = str(item.get("raw_pattern") or "").strip()
            status = str(item.get("status") or "").strip().casefold()
            if not owner or not pattern_type or not raw_pattern or not status:
                raise ValueError(
                    f"자동 정상 origin의 필수 값이 없습니다: {record.get('rule_id')}"
                )
            key = (owner, pattern_type, raw_pattern)
            if key in seen:
                raise ValueError(f"자동 정상 origin이 중복됩니다: {key}")
            seen.add(key)
            metrics = item.get("evidence")
            if not isinstance(metrics, dict):
                raise ValueError(f"자동 정상 origin evidence가 객체가 아닙니다: {key}")
            records.append(
                {
                    "source_subcategory": owner,
                    "pattern_type": pattern_type,
                    "pattern": raw_pattern,
                    "status": status,
                    **deepcopy(metrics),
                }
            )

    source_policies = document.get("source_policies")
    if not isinstance(source_policies, dict):
        source_policies = {}
    policy = source_policies.get(AUTO_NORMAL_SOURCE, {})
    if not isinstance(policy, dict):
        policy = {}
    learning = document.get("learning")
    if not isinstance(learning, dict):
        learning = {}
    snapshots = learning.get("evidence_snapshots", [])
    if not isinstance(snapshots, list):
        raise ValueError("normal_rules.json learning.evidence_snapshots가 배열이 아닙니다.")
    records.sort(
        key=lambda item: (
            str(item["source_subcategory"]),
            str(item["pattern_type"]),
            str(item["pattern"]),
        )
    )
    return {
        "version": 2,
        "policy": deepcopy(policy),
        "evidence_snapshots": deepcopy(snapshots),
        "records": records,
    }


def build_merged_auto_normal_document(
    document: dict[str, object],
    registry: dict[str, object],
    *,
    training_cutoff: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return a merged document without mutating or writing either input."""
    merged = _load_document(document, label="normal_rules.json")
    validate_normal_rules_document(merged)
    if not isinstance(registry, dict) or not isinstance(registry.get("records"), list):
        raise ValueError("자동 정상 registry의 records 배열이 필요합니다.")
    snapshots = registry.get("evidence_snapshots", [])
    policy = registry.get("policy", {})
    if not isinstance(snapshots, list):
        raise ValueError("자동 정상 registry의 evidence_snapshots가 배열이 아닙니다.")
    if not isinstance(policy, dict):
        raise ValueError("자동 정상 registry의 policy가 객체가 아닙니다.")

    templates: dict[str, dict[str, object]] = {}
    rules_by_key: dict[str, dict[str, object]] = {}
    preserved_origin_count = 0
    removed_auto_origin_count = 0
    for raw_record in merged["rules"]:
        record = deepcopy(raw_record)
        owner = str(record.get("source_subcategory") or "").strip()
        pattern = record.get("pattern")
        if not isinstance(pattern, dict):
            raise ValueError(f"정상 규칙 pattern이 객체가 아닙니다: {record.get('rule_id')}")
        key = _normal_key(owner, pattern)
        templates[key] = deepcopy(record)
        all_origins = _origin_items(record)
        preserved = [deepcopy(item) for item in all_origins if not _is_auto_origin(item)]
        removed_auto_origin_count += len(all_origins) - len(preserved)
        preserved_origin_count += len(preserved)
        record["evidence"]["origins"] = preserved
        if preserved:
            _recompute_normal_rule(record)
            rules_by_key[key] = record

    added_keys: set[tuple[str, str, str]] = set()
    added_auto_origin_count = 0
    for index, raw_item in enumerate(registry["records"]):
        if not isinstance(raw_item, dict):
            raise ValueError(f"자동 정상 records[{index}]가 객체가 아닙니다.")
        item = deepcopy(raw_item)
        owner = str(item.get("source_subcategory") or "").strip()
        pattern_type = str(item.get("pattern_type") or "").strip()
        raw_pattern = str(item.get("pattern") or "").strip()
        status = str(item.get("status") or "").strip().casefold()
        if not owner or not pattern_type or not raw_pattern or not status:
            raise ValueError(f"자동 정상 records[{index}]의 필수 값이 없습니다.")
        origin_key = (owner, pattern_type, raw_pattern)
        if origin_key in added_keys:
            raise ValueError(f"자동 정상 record가 중복됩니다: {origin_key}")
        added_keys.add(origin_key)
        pattern = _normal_match(pattern_type, raw_pattern)
        key = _normal_key(owner, pattern)
        if key not in rules_by_key:
            if key in templates:
                record = deepcopy(templates[key])
                record["evidence"] = {"origins": []}
            else:
                record = _new_normal_rule(owner, pattern)
            rules_by_key[key] = record
        rules_by_key[key]["evidence"]["origins"].append(_auto_origin(item))
        added_auto_origin_count += 1

    output_rules = sorted(
        rules_by_key.values(),
        key=lambda item: (
            str(item.get("source_subcategory") or ""),
            str((item.get("pattern") or {}).get("kind") or ""),
            str((item.get("pattern") or {}).get("value") or ""),
        ),
    )
    for record in output_rules:
        _recompute_normal_rule(record)
    merged["rules"] = output_rules

    source_policies = merged.get("source_policies")
    if not isinstance(source_policies, dict):
        source_policies = {}
        merged["source_policies"] = source_policies
    source_policies[AUTO_NORMAL_SOURCE] = deepcopy(policy)
    learning = merged.get("learning")
    if not isinstance(learning, dict):
        learning = {}
        merged["learning"] = learning
    learning["evidence_snapshots"] = deepcopy(snapshots)
    learning["overlapping_period_policy"] = policy.get(
        "overlapping_period_policy"
    )

    normal_learning_cutoff = training_cutoff or _max_snapshot_end(registry)
    if normal_learning_cutoff:
        merged["normal_learning_cutoff"] = normal_learning_cutoff
    merged["updated_at"] = _now()
    merged["runtime_revision"] = int(merged.get("runtime_revision") or 0) + 1
    _recompute_normal_summary(merged)
    merged = _attach_normal_rule_catalog(merged)
    validation = validate_normal_rules_document(merged)
    merged["document_sha256"] = _document_digest(merged)
    summary: dict[str, object] = {
        **validation,
        "preserved_non_auto_origin_count": preserved_origin_count,
        "removed_auto_origin_count": removed_auto_origin_count,
        "added_auto_origin_count": added_auto_origin_count,
        "auto_record_count": len(registry["records"]),
        "training_cutoff": merged.get("training_cutoff"),
        "normal_learning_cutoff": merged.get("normal_learning_cutoff"),
        "runtime_revision": merged["runtime_revision"],
        "document_sha256": merged["document_sha256"],
    }
    return merged, summary


def _atomic_write_document(
    path: Path,
    document: dict[str, object],
    *,
    kind: Literal["normal", "error"],
    expected_sha256: str | None = None,
) -> dict[str, object]:
    if expected_sha256 is not None and path.is_file():
        current = _file_digest(path)
        if current != expected_sha256:
            raise RuntimeError(
                f"{path.name}이 다른 작업에서 변경되었습니다: "
                f"expected={expected_sha256}, actual={current}"
            )
    payload = deepcopy(document)
    if kind == "normal":
        payload = _attach_normal_rule_catalog(payload)
    payload["document_sha256"] = _document_digest(payload)
    validation = (
        validate_normal_rules_document(payload)
        if kind == "normal"
        else validate_error_rules_document(payload)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}_{uuid.uuid4().hex[:8]}_writing.json")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        reread = _load_document(temporary, label=path.name)
        if reread.get("document_sha256") != _document_digest(reread):
            raise RuntimeError(f"{path.name} 임시 파일의 문서 해시가 일치하지 않습니다.")
        if kind == "normal":
            validate_normal_rules_document(reread)
        else:
            validate_error_rules_document(reread)
        os.replace(temporary, path)
        if kind == "normal" and path.resolve() == NORMAL_RULES_PATH.resolve():
            refresh_normal_rules_view(payload)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        **validation,
        "path": str(path.resolve()),
        "file_sha256": _file_digest(path),
        "document_sha256": payload["document_sha256"],
    }


def merge_auto_normal_registry(
    registry: dict[str, object],
    *,
    path: Path = NORMAL_RULES_PATH,
    expected_sha256: str | None = None,
    training_cutoff: str | None = None,
) -> dict[str, object]:
    """Merge auto-normal evidence into the consolidated file and atomically save it."""
    current = _load_document(path, label="normal_rules.json")
    merged, summary = build_merged_auto_normal_document(
        current,
        registry,
        training_cutoff=training_cutoff,
    )
    written = _atomic_write_document(
        path,
        merged,
        kind="normal",
        expected_sha256=expected_sha256,
    )
    return {**summary, **written}


def refresh_normal_rule_catalog(
    *,
    path: Path = NORMAL_RULES_PATH,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    """Rebuild the human-readable catalog and atomically save normal rules."""
    current = _load_document(path, label="normal_rules.json")
    return _atomic_write_document(
        path,
        current,
        kind="normal",
        expected_sha256=expected_sha256,
    )


def upsert_manual_normal_rules(
    entries: list[dict[str, str]],
    *,
    path: Path = NORMAL_RULES_PATH,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    """Add owner-scoped manual normal rules to the executable rule array.

    The human-readable ``소분류별_규칙`` section is derived data, so editing
    that section alone cannot affect classification. This helper records a
    durable manual origin and rebuilds the catalog atomically.
    """
    current = _load_document(path, label="normal_rules.json")
    # The catalog is a generated view. A user may have edited that section
    # directly, leaving it temporarily out of sync with executable rules.
    # Discard and rebuild it from the authoritative array below.
    current.pop(NORMAL_CATALOG_GUIDE_KEY, None)
    current.pop(NORMAL_CATALOG_KEY, None)
    validate_normal_rules_document(current)
    rules_by_key: dict[str, dict[str, object]] = {}
    for raw_record in current["rules"]:
        owner = str(raw_record.get("source_subcategory") or "").strip()
        pattern = raw_record.get("pattern")
        if isinstance(pattern, dict):
            rules_by_key[_normal_key(owner, pattern)] = raw_record

    changed_count = 0
    for index, entry in enumerate(entries):
        owner = str(entry.get("source_subcategory") or "").strip()
        pattern_type = str(entry.get("pattern_type") or "").strip()
        raw_pattern = str(entry.get("pattern") or "").strip()
        if not owner or not pattern_type or not raw_pattern:
            raise ValueError(f"수동 정상 규칙[{index}]의 필수 값이 없습니다.")
        pattern = _normal_match(pattern_type, raw_pattern)
        key = _normal_key(owner, pattern)
        record = rules_by_key.get(key)
        if record is None:
            record = _new_normal_rule(owner, pattern)
            current["rules"].append(record)
            rules_by_key[key] = record

        manual_origin = {
            "source": "manual_normal",
            "status": "active",
            "role": "scoped_normal",
            "scope": "source_subcategory_override",
            "strength": "override",
            "legacy_pattern_type": pattern_type,
            "raw_pattern": raw_pattern,
            "evidence": {"added_manually": True},
        }
        origins = _origin_items(record)
        retained = [
            origin
            for origin in origins
            if not (
                str(origin.get("source") or "") == "manual_normal"
                and str(origin.get("role") or "")
                in {"manual_normal", "scoped_normal"}
            )
        ]
        if retained != origins or manual_origin not in origins:
            record["evidence"]["origins"] = [*retained, manual_origin]
            changed_count += 1
        _recompute_normal_rule(record)

    current["rules"].sort(
        key=lambda record: (
            str(record.get("source_subcategory") or ""),
            str((record.get("pattern") or {}).get("kind") or ""),
            str((record.get("pattern") or {}).get("value") or ""),
        )
    )
    current["updated_at"] = _now()
    current["runtime_revision"] = int(current.get("runtime_revision") or 0) + 1
    _recompute_normal_summary(current)
    written = _atomic_write_document(
        path,
        current,
        kind="normal",
        expected_sha256=expected_sha256,
    )
    return {
        "manual_rule_count": len(entries),
        "changed_count": changed_count,
        **written,
    }


def write_error_rules_atomic(
    document: dict[str, object],
    *,
    path: Path = ERROR_RULES_PATH,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    """Validate and atomically replace the consolidated error-rule document."""
    prepared = _load_document(document, label="error_rules.json")
    prepared["updated_at"] = _now()
    prepared["runtime_revision"] = int(prepared.get("runtime_revision") or 0) + 1
    return _atomic_write_document(
        path,
        prepared,
        kind="error",
        expected_sha256=expected_sha256,
    )


def _non_auto_origin_digest(document: dict[str, object]) -> str:
    values: list[dict[str, object]] = []
    for record in document["rules"]:
        values.append(
            {
                "key": _normal_key(record["source_subcategory"], record["pattern"]),
                "origins": [
                    item
                    for item in _origin_items(record)
                    if not _is_auto_origin(item)
                ],
            }
        )
    values.sort(key=lambda item: str(item["key"]))
    return _sha256_bytes(_stable_json(values).encode("utf-8"))


def self_check(
    normal_path: Path = NORMAL_RULES_PATH,
    error_path: Path = ERROR_RULES_PATH,
) -> dict[str, object]:
    """Round-trip current documents in a temporary directory without mutating live files."""
    normal = _load_document(normal_path, label="normal_rules.json")
    error = _load_document(error_path, label="error_rules.json")
    registry = extract_auto_normal_registry(normal)
    before_non_auto = _non_auto_origin_digest(normal)
    before_auto = _stable_json(registry)
    before_behavior = _stable_json(
        [
            (record["rule_id"], record.get("behavior"), record.get("status"))
            for record in normal["rules"]
        ]
    )
    promoted_registry = deepcopy(registry)
    promoted_record = next(
        (
            record
            for record in promoted_registry["records"]
            if str(record.get("status") or "").casefold() != "active"
        ),
        None,
    )
    if promoted_record is None:
        raise AssertionError("승격 재계산을 검증할 자동 정상 제안 규칙이 없습니다.")
    promoted_record["status"] = "active"
    promoted_document, _ = build_merged_auto_normal_document(
        normal,
        promoted_registry,
        training_cutoff=str(normal.get("training_cutoff") or "") or None,
    )
    promoted_pattern = _normal_match(
        str(promoted_record["pattern_type"]),
        str(promoted_record["pattern"]),
    )
    promoted_key = _normal_key(
        str(promoted_record["source_subcategory"]),
        promoted_pattern,
    )
    promoted_lookup = {
        _normal_key(record["source_subcategory"], record["pattern"]): record
        for record in promoted_document["rules"]
    }
    promoted_behavior = promoted_lookup[promoted_key]["behavior"]
    with TemporaryDirectory(prefix="unified_rule_registry_") as directory:
        root = Path(directory)
        normal_copy = root / "normal_rules.json"
        error_copy = root / "error_rules.json"
        normal_copy.write_text(
            json.dumps(normal, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        normal_result = merge_auto_normal_registry(
            registry,
            path=normal_copy,
            expected_sha256=_file_digest(normal_copy),
            training_cutoff=str(normal.get("training_cutoff") or "") or None,
        )
        merged = _load_document(normal_copy, label="normal_rules.json")
        error_result = write_error_rules_atomic(error, path=error_copy)
        after_behavior = _stable_json(
            [
                (record["rule_id"], record.get("behavior"), record.get("status"))
                for record in merged["rules"]
            ]
        )
        checks = {
            "non_auto_origins_preserved": (
                before_non_auto == _non_auto_origin_digest(merged)
            ),
            "auto_registry_round_trip": (
                before_auto == _stable_json(extract_auto_normal_registry(merged))
            ),
            "effective_behavior_preserved": before_behavior == after_behavior,
            "active_auto_recomputes_own_normal": bool(
                promoted_behavior.get("own_normal")
            ),
            "active_auto_recomputes_override": bool(
                promoted_behavior.get("override_collision")
            ),
            "live_normal_unchanged": _load_document(
                normal_path, label="normal_rules.json"
            )
            == normal,
            "live_error_unchanged": _load_document(
                error_path, label="error_rules.json"
            )
            == error,
        }
        if not all(checks.values()):
            failed = sorted(key for key, value in checks.items() if not value)
            raise AssertionError(f"통합 규칙 자체검증 실패: {failed}")
        return {
            "valid": True,
            "checks": checks,
            "extracted_auto_records": len(registry["records"]),
            "normal_write": normal_result,
            "error_write": error_result,
        }


if __name__ == "__main__":
    print(json.dumps(self_check(), ensure_ascii=False, indent=2))
