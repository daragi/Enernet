"""Build unified exact and scoped-phrase error rules from validated data.

The registry key is ``source_subcategory + normalize_text_value(detail)``.
Only signatures that never occur on a non-error row become ``auto_error``;
ambiguous exact signatures become ``review_lock`` and ambiguous phrases remain
``audit_only`` evidence in ``error_rules.json``.
Phrase rules are learned only from historical truth and human-approved manual
rows. Automatic classifications are never fed back as truth.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
from itertools import combinations
import json
import os
from numbers import Integral, Real
from pathlib import Path
import re
import sqlite3
import sys
import uuid

import pandas as pd


ERROR_DIR = Path(__file__).resolve().parents[1]
CENTER_DIR = ERROR_DIR.parents[1]
if str(CENTER_DIR) not in sys.path:
    sys.path.insert(0, str(CENTER_DIR))

from service_order.error.pattern_matching import ordered_distinct_token_match
DEFAULT_TRUTH_PATH = Path(
    r"\\DocuONE\MyDrive\개인함\오생성\right_data\26년오생성모음.xlsx"
)
DEFAULT_DATABASE_PATH = ERROR_DIR.parent / "data" / "service_order_metrics.sqlite3"
DEFAULT_MANIFEST_PATH = (
    Path(os.environ.get("TEMP", "."))
    / "center_dashboard"
    / "results"
    / "current_job.json"
)
LEGACY_OUTPUT_PATH = ERROR_DIR / "json" / "error_pattern.json"
DEFAULT_OUTPUT_PATH = ERROR_DIR / "json" / "error_rules.json"
DEFAULT_EXCEPT_LIST_PATH = ERROR_DIR / "json" / "except_list.json"
DEFAULT_BASELINE_PATH = (
    ERROR_DIR.parent / "data" / "error_learning_baseline.sqlite3"
)
DEFAULT_HISTORICAL_SOURCE_DIR = Path(
    os.environ.get(
        "SERVICE_ORDER_HISTORICAL_SOURCE_DIR",
        r"\\DocuONE\MyDrive\개인함\오생성\study_data",
    )
)

ORDER_NUMBER = "오더번호"
STATUS = "상태"
DETAIL = "내역"
DEPARTMENT = "생성부서"
BUSINESS = "사업부"
SUBCATEGORY = "소분류"
BUSINESS_DEPARTMENT = "사업부"
BUSINESS_VALUES = frozenset({"중부", "북부", "남부", "동부", "서부"})
CANCELLED_ORDER_STATUS = "오더취소"
REQUIRED_COLUMNS = (
    ORDER_NUMBER,
    STATUS,
    DETAIL,
    DEPARTMENT,
    BUSINESS,
    SUBCATEGORY,
)
MIN_PHRASE_TOKENS = 2
MAX_PHRASE_TOKENS = 4
BASELINE_SCHEMA_VERSION = 1
# Bump only when preprocessing filters or error-signature normalization changes.
# Phrase matching and inference-only changes do not invalidate 1~6월 signatures.
BASELINE_SIGNATURE_VERSION = 1
MIN_PHRASE_SUPPORT = {2: 3, 3: 2, 4: 2}
# Full-sentence equality is intentionally not required.  When enough manual
# approvals accumulate, repeated informative tokens may form an ordered core
# phrase even when names, dates, amounts, or free-form wording appear between
# them.  Core phrases need stronger, diverse evidence than contiguous phrases.
MIN_CORE_PHRASE_SUPPORT = {2: 3, 3: 2}
MIN_CORE_DISTINCT_SIGNATURES = {2: 2, 3: 2}
MAX_CORE_TOKEN_SPAN = 7
GENERIC_PHRASE_TOKENS = frozenset(
    {
        "고객",
        "요청",
        "확인",
        "안내",
        "처리",
        "완료",
        "예정",
        "문의",
        "통화",
        "전화",
        "방문",
        "접수",
        "오더",
        "사항",
        "내용",
        "관련",
        "드립니다",
        "바랍니다",
    }
)

DEFAULT_PHRASE_MATCHING = {
    "strategy": "ordered_distinct_tokens",
    "max_spacing_split_tokens": 2,
    "max_gap_tokens": 6,
    "learning_policy": {
        "contiguous_min_support": MIN_PHRASE_SUPPORT,
        "ordered_core_min_support": MIN_CORE_PHRASE_SUPPORT,
        "ordered_core_min_distinct_signatures": (
            MIN_CORE_DISTINCT_SIGNATURES
        ),
        "ordered_core_max_token_span": MAX_CORE_TOKEN_SPAN,
        "same_subcategory_only": True,
        "manual_approval_only": True,
        "zero_observed_normal_for_auto": True,
    },
}
DEFAULT_SIMILARITY = {
    "minimum_error_score": 0.75,
    "maximum_normal_score": 0.55,
    "minimum_margin": 0.20,
    "minimum_informative_tokens": 2,
    "prefilter_limit": 30,
    "candidate_only": True,
    "same_subcategory_only": True,
}
ERROR_RULE_SOURCE = "error_pattern.json"


def normalize_text_value(value: object) -> str:
    """Match the production normalization used by analysis_pipeline.py."""

    text = "" if value is None or pd.isna(value) else str(value)
    text = text.lower()
    text = re.sub(r"\d+", " ", text)
    text = re.sub(r"[^가-힣a-z]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def signature_phrases(signature: object) -> set[tuple[str, ...]]:
    """Return conservative contiguous phrases eligible for scoped learning."""
    tokens = normalize_text_value(signature).split()
    phrases: set[tuple[str, ...]] = set()
    for token_count in range(MIN_PHRASE_TOKENS, MAX_PHRASE_TOKENS + 1):
        for start in range(0, len(tokens) - token_count + 1):
            phrase = tuple(tokens[start : start + token_count])
            informative = [
                token
                for token in phrase
                if token not in GENERIC_PHRASE_TOKENS and len(token) >= 2
            ]
            if not informative:
                continue
            if len("".join(phrase)) < 5:
                continue
            phrases.add(phrase)
    return phrases


def signature_core_phrases(signature: object) -> set[tuple[str, ...]]:
    """Return non-contiguous, informative token combinations in text order.

    These combinations generalize repeated administrator-approved errors while
    remaining scoped to the source subcategory.  A bounded span prevents two
    unrelated parts of a long memo from becoming one automatic rule.
    """
    tokens = normalize_text_value(signature).split()
    informative = [
        (index, token)
        for index, token in enumerate(tokens)
        if token not in GENERIC_PHRASE_TOKENS and len(token) >= 2
    ]
    phrases: set[tuple[str, ...]] = set()
    for token_count in sorted(MIN_CORE_PHRASE_SUPPORT):
        for selected in combinations(informative, token_count):
            indices = [item[0] for item in selected]
            if indices[-1] - indices[0] + 1 > MAX_CORE_TOKEN_SPAN:
                continue
            # Contiguous windows are already covered by signature_phrases().
            if all(
                right == left + 1
                for left, right in zip(indices, indices[1:])
            ):
                continue
            phrase = tuple(item[1] for item in selected)
            if len("".join(phrase)) < 5:
                continue
            phrases.add(phrase)
    return phrases


def compact_text(value: object) -> str:
    return normalize_text_value(value).replace(" ", "")


def stable_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_error_key(
    source_subcategory: str,
    match_kind: str,
    value: str,
) -> str:
    return stable_json(
        {
            "source_subcategory": source_subcategory,
            "kind": match_kind,
            "value": value,
        }
    )


def stable_error_rule_id(
    source_subcategory: str,
    match_kind: str,
    value: str,
) -> str:
    key = canonical_error_key(source_subcategory, match_kind, value)
    return f"error_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:20]}"


def _unified_decision(status: str, match_kind: str) -> tuple[str, str]:
    normalized = status.strip().casefold()
    if normalized == "active":
        return (
            "auto_error",
            "exact" if match_kind == "exact_normalized" else "phrase",
        )
    if normalized == "ambiguous":
        return (
            ("review_lock", "review")
            if match_kind == "exact_normalized"
            else ("audit_only", "audit")
        )
    raise ValueError(f"해석할 수 없는 오생성 규칙 상태입니다: {status}")


def _load_existing_unified_document(path: Path) -> dict[str, object]:
    candidates = [path]
    if path != DEFAULT_OUTPUT_PATH:
        candidates.append(DEFAULT_OUTPUT_PATH)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(document, dict)
            and document.get("schema_version") == 1
            and isinstance(document.get("rules"), list)
        ):
            return document
    return {}


def _resolved_output_path(path: Path) -> Path:
    """Do not recreate the retired legacy registry through an old caller."""
    requested = Path(path)
    if requested.resolve() == LEGACY_OUTPUT_PATH.resolve():
        return DEFAULT_OUTPUT_PATH
    return requested


def _manifest_training_cutoff(
    manifest_path: Path,
    existing_document: dict[str, object],
) -> str:
    values: list[str] = []
    existing = str(existing_document.get("training_cutoff") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", existing):
        values.append(existing)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        period = manifest.get("period", {})
        current_end = str(period.get("end") or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", current_end):
            values.append(current_end)
    except (AttributeError, KeyError, OSError, json.JSONDecodeError):
        pass
    if not values:
        raise ValueError("error_rules.json의 학습 기준일을 확인할 수 없습니다.")
    return max(values)


def _registry_source_manifest(args: argparse.Namespace) -> list[dict[str, object]]:
    sources = (
        ("historical_truth", Path(args.truth)),
        ("approval_database", Path(args.database)),
        ("current_job_manifest", Path(args.current_manifest)),
        ("except_list", Path(args.except_list)),
        ("historical_baseline", Path(args.baseline)),
    )
    manifest: list[dict[str, object]] = []
    for role, path in sources:
        if not path.is_file():
            continue
        stat = path.stat()
        manifest.append(
            {
                "role": role,
                "file": path.name,
                "size_bytes": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        )
    return manifest


def phrase_matches_signature(
    signature: object,
    phrase: tuple[str, ...],
) -> bool:
    """Match phrase tokens in order with bounded, distinct-token gaps."""
    normalized_signature = normalize_text_value(signature)
    normalized_phrase = tuple(normalize_text_value(token) for token in phrase)
    return ordered_distinct_token_match(normalized_signature, normalized_phrase)


def build_phrase_records(
    total_counts: Counter[tuple[str, str]],
    confirmed_counts: Counter[tuple[str, str]],
) -> list[dict[str, object]]:
    """Build subcategory-scoped phrase rules with observed-normal protection."""
    seed_phrase_counts: Counter[tuple[str, tuple[str, ...]]] = Counter()
    core_phrase_counts: Counter[tuple[str, tuple[str, ...]]] = Counter()
    core_signature_evidence: dict[
        tuple[str, tuple[str, ...]], set[str]
    ] = {}
    for (source_subcategory, signature), count in confirmed_counts.items():
        for phrase in signature_phrases(signature):
            seed_phrase_counts[(source_subcategory, phrase)] += int(count)
        for phrase in signature_core_phrases(signature):
            key = (source_subcategory, phrase)
            core_phrase_counts[key] += int(count)
            core_signature_evidence.setdefault(key, set()).add(signature)

    contiguous_eligible = {
        key
        for key, count in seed_phrase_counts.items()
        if count >= MIN_PHRASE_SUPPORT[len(key[1])]
    }
    core_eligible = {
        key
        for key, count in core_phrase_counts.items()
        if count >= MIN_CORE_PHRASE_SUPPORT[len(key[1])]
        and len(core_signature_evidence.get(key, set()))
        >= MIN_CORE_DISTINCT_SIGNATURES[len(key[1])]
    }
    eligible_keys = contiguous_eligible | core_eligible
    eligible_by_subcategory: dict[str, set[tuple[str, ...]]] = {}
    for source_subcategory, phrase in eligible_keys:
        eligible_by_subcategory.setdefault(source_subcategory, set()).add(phrase)

    normal_signature_counts = total_counts.copy()
    normal_signature_counts.subtract(confirmed_counts)
    for (source_subcategory, signature), count in normal_signature_counts.items():
        if count < 0:
            raise ValueError(
                "문구 모집단의 정상 건수가 음수입니다: "
                f"{source_subcategory}/{signature} ({int(count)})"
            )

    confirmed_match_counts: Counter[
        tuple[str, tuple[str, ...]]
    ] = Counter()
    normal_match_counts: Counter[tuple[str, tuple[str, ...]]] = Counter()
    confirmed_matching_signatures: dict[
        tuple[str, tuple[str, ...]], set[str]
    ] = {}
    for counts, destination, signature_destination in (
        (confirmed_counts, confirmed_match_counts, confirmed_matching_signatures),
        (normal_signature_counts, normal_match_counts, None),
    ):
        for (source_subcategory, signature), count in counts.items():
            if count <= 0:
                continue
            eligible = eligible_by_subcategory.get(source_subcategory)
            if not eligible:
                continue
            for phrase in eligible:
                if phrase_matches_signature(signature, phrase):
                    key = (source_subcategory, phrase)
                    destination[key] += int(count)
                    if signature_destination is not None:
                        signature_destination.setdefault(key, set()).add(signature)

    records: list[dict[str, object]] = []
    for source_subcategory, phrase in sorted(
        eligible_keys,
        key=lambda item: (item[0], len(item[1]), item[1]),
    ):
        confirmed_count = int(
            confirmed_match_counts[(source_subcategory, phrase)]
        )
        normal_count = int(normal_match_counts[(source_subcategory, phrase)])
        records.append(
            {
                "source_subcategory": source_subcategory,
                "phrase": " ".join(phrase),
                "token_count": len(phrase),
                "confirmed_count": confirmed_count,
                "normal_count": normal_count,
                "distinct_confirmed_signatures": len(
                    confirmed_matching_signatures.get(
                        (source_subcategory, phrase), set()
                    )
                ),
                "learning_modes": [
                    mode
                    for mode, keys in (
                        ("contiguous", contiguous_eligible),
                        ("ordered_core", core_eligible),
                    )
                    if (source_subcategory, phrase) in keys
                ],
                "status": "active" if normal_count == 0 else "ambiguous",
            }
        )
    return records


def build_unified_error_document(
    records: list[dict[str, object]],
    phrase_records: list[dict[str, object]],
    *,
    training_cutoff: str,
    existing_document: dict[str, object] | None = None,
    source_manifest: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Convert learned exact/phrase evidence directly to error_rules schema."""
    existing_document = existing_document or {}
    rules: dict[str, dict[str, object]] = {}
    source_counts: Counter[str] = Counter()

    def add_rule(
        item: dict[str, object],
        *,
        match_kind: str,
        raw_field: str,
    ) -> None:
        source_subcategory = str(
            item.get("source_subcategory") or ""
        ).strip()
        raw_value = str(item.get(raw_field) or "").strip()
        value = normalize_text_value(raw_value)
        status = str(item.get("status") or "").strip().casefold()
        if not source_subcategory or not value or not status:
            raise ValueError(f"불완전한 오생성 규칙입니다: {item}")

        decision, strength = _unified_decision(status, match_kind)
        pattern = {
            "kind": match_kind,
            "value": value,
            "tokens": value.split(),
        }
        key = canonical_error_key(source_subcategory, match_kind, value)
        evidence_values = {
            key: deepcopy(value)
            for key, value in item.items()
            if key not in {"source_subcategory", raw_field, "status"}
        }
        origin = {
            "source": ERROR_RULE_SOURCE,
            "status": status,
            "scope": "source_subcategory_only",
            "strength": strength,
            "raw_pattern": raw_value,
            "evidence": evidence_values,
        }
        if key in rules:
            current = rules[key]
            if (
                current["status"] != status
                or current["decision"] != decision
            ):
                raise ValueError(
                    "동일한 오생성 규칙의 판정이 충돌합니다: "
                    f"{source_subcategory}/{value}"
                )
            current["evidence"]["origins"].append(origin)
            return

        rules[key] = {
            "rule_id": stable_error_rule_id(
                source_subcategory,
                match_kind,
                value,
            ),
            "source_subcategory": source_subcategory,
            "pattern": pattern,
            "source": [ERROR_RULE_SOURCE],
            "status": status,
            "scope": {
                "source_subcategory": source_subcategory,
                "application": "source_subcategory_only",
            },
            "strength": strength,
            "decision": decision,
            "evidence": {"origins": [origin]},
        }

    for item in records:
        if not isinstance(item, dict):
            raise ValueError("exact 오생성 규칙은 객체여야 합니다.")
        add_rule(item, match_kind="exact_normalized", raw_field="signature")
        source_counts["exact"] += 1
    for item in phrase_records:
        if not isinstance(item, dict):
            raise ValueError("phrase 오생성 규칙은 객체여야 합니다.")
        add_rule(
            item,
            match_kind="ordered_distinct_tokens",
            raw_field="phrase",
        )
        source_counts["phrase"] += 1

    output_rules = sorted(
        rules.values(),
        key=lambda item: (
            item["source_subcategory"],
            item["pattern"]["kind"],
            item["pattern"]["value"],
        ),
    )
    origin_count = sum(
        len(item["evidence"]["origins"]) for item in output_rules
    )
    if origin_count != sum(source_counts.values()):
        raise AssertionError("오생성 규칙의 모든 근거가 보존되지 않았습니다.")
    if len({item["rule_id"] for item in output_rules}) != len(output_rules):
        raise AssertionError("error rule_id가 중복됩니다.")

    decisions = Counter(str(item["decision"]) for item in output_rules)
    statuses = Counter(str(item["status"]) for item in output_rules)
    phrase_matching = deepcopy(DEFAULT_PHRASE_MATCHING)
    existing_phrase_matching = existing_document.get("phrase_matching", {})
    if isinstance(existing_phrase_matching, dict):
        phrase_matching.update(existing_phrase_matching)
        # The learning policy describes the running builder, not an older
        # generated registry, so always refresh it from the current code.
        phrase_matching["learning_policy"] = deepcopy(
            DEFAULT_PHRASE_MATCHING["learning_policy"]
        )
    similarity = existing_document.get("similarity", DEFAULT_SIMILARITY)
    if not isinstance(phrase_matching, dict):
        raise ValueError("기존 error_rules.json의 phrase_matching이 객체가 아닙니다.")
    if not isinstance(similarity, dict):
        raise ValueError("기존 error_rules.json의 similarity가 객체가 아닙니다.")

    rules_digest = hashlib.sha256(
        stable_json(output_rules).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "rule_version": f"cutoff-{training_cutoff}",
        "generated_at": datetime.now().astimezone().isoformat(),
        "training_cutoff": training_cutoff,
        "source_rules_sha256": rules_digest,
        "source_manifest": deepcopy(source_manifest or []),
        "phrase_matching": deepcopy(phrase_matching),
        "similarity": deepcopy(similarity),
        "migration_summary": {
            "source_origin_count": int(sum(source_counts.values())),
            "canonical_rule_count": len(output_rules),
            "merged_duplicate_origin_count": (
                int(sum(source_counts.values())) - len(output_rules)
            ),
            "source_counts": dict(sorted(source_counts.items())),
            "decision_counts": dict(sorted(decisions.items())),
            "status_counts": dict(sorted(statuses.items())),
        },
        "rules": output_rules,
    }


def normalize_order_number(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, Integral) and not isinstance(value, bool):
        return str(int(value))
    if isinstance(value, Real) and not isinstance(value, bool):
        number = float(value)
        if not pd.notna(number):
            return None
        if number.is_integer():
            return str(int(number))
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if re.fullmatch(r"[+-]?\d+\.0+", text):
        return text.split(".", 1)[0].lstrip("+")
    return text


def subcategory_key(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return re.sub(r"\s+", "", str(value).strip())


def load_exclusion_keys(path: Path) -> set[str]:
    values = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(values, list):
        raise ValueError(f"except_list 최상위 값이 배열이 아닙니다: {path}")
    return {subcategory_key(value) for value in values if subcategory_key(value)}


def validate_columns(frame: pd.DataFrame, source: str) -> None:
    missing = sorted(set(REQUIRED_COLUMNS) - set(frame.columns))
    if missing:
        raise KeyError(f"{source} 필수 열 누락: {missing}")


def filter_valid_rows(
    frame: pd.DataFrame,
    exclusion_keys: set[str],
    *,
    source: str,
) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = frame.columns.astype(str).str.strip()
    validate_columns(frame, source)

    department = frame[DEPARTMENT].astype("string").str.strip()
    business = frame[BUSINESS].astype("string").str.strip()
    subcategory = frame[SUBCATEGORY].astype("string").str.strip()
    status = frame[STATUS].astype("string").str.strip()
    keep = (
        department.eq(BUSINESS_DEPARTMENT).fillna(False)
        & business.isin(BUSINESS_VALUES).fillna(False)
        & ~subcategory.map(subcategory_key).isin(exclusion_keys).fillna(False)
        & ~status.eq(CANCELLED_ORDER_STATUS).fillna(False)
    )
    return frame.loc[keep].copy()


def signature_rows(
    frame: pd.DataFrame,
    sap_id_map: dict[str, str],
    *,
    allow_empty_signature: bool = False,
) -> pd.DataFrame:
    from service_order.error.analysis_pipeline import (  # local: avoid cycle
        build_proper_nouns,
        normalize_details,
    )

    result = pd.DataFrame(index=frame.index)
    result["order_number"] = frame[ORDER_NUMBER].map(normalize_order_number)
    result["source_subcategory"] = (
        frame[SUBCATEGORY].astype("string").str.strip().fillna("")
    )
    proper_nouns = build_proper_nouns(frame, sap_id_map)
    result["signature"] = normalize_details(frame[DETAIL], proper_nouns)
    if result["order_number"].isna().any():
        raise ValueError(
            f"오더번호 결측 {int(result['order_number'].isna().sum()):,}건"
        )
    if result["source_subcategory"].eq("").any():
        raise ValueError("소분류 결측이 있습니다.")
    if not allow_empty_signature and result["signature"].eq("").any():
        raise ValueError("정규화 후 빈 내역이 있습니다.")
    return result.reset_index(drop=True)


def load_truth_workbook(
    path: Path,
    exclusion_keys: set[str],
) -> pd.DataFrame:
    workbook = pd.ExcelFile(path)
    frames: list[pd.DataFrame] = []
    for month in range(1, 7):
        sheet = f"{month}월"
        if sheet not in workbook.sheet_names:
            raise KeyError(f"정답 시트 누락: {sheet}")
        raw = pd.read_excel(workbook, sheet_name=sheet)
        frames.append(
            filter_valid_rows(raw, exclusion_keys, source=f"{path.name}/{sheet}")
        )
    return pd.concat(frames, ignore_index=True)


def load_historical_total(
    path: Path,
    exclusion_keys: set[str],
) -> pd.DataFrame:
    """Read an original monthly workbook with the production preprocessor."""
    from service_order.error.analysis_pipeline import (  # local: avoid cycle
        load_configuration,
        preprocess_orders,
        read_order_excel,
    )

    sap_id_map, _ = load_configuration()
    raw, _ = read_order_excel(path)
    preprocessed, _ = preprocess_orders(raw, sap_id_map, exclusion_keys)
    return preprocessed


def load_active_sqlite_truth(
    path: Path,
    exclusion_keys: set[str],
) -> pd.DataFrame:
    with sqlite3.connect(path) as connection:
        batch_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(error_approval_batches)"
            ).fetchall()
        }
        # 자동 분류 결과를 다시 정답으로 학습하면 규칙이 자기증폭됩니다.
        # batch_type 도입 전 DB는 모든 기존 배치를 수동 승인으로 간주하고,
        # 도입 후에는 사람에게 승인된 manual 배치만 정답으로 사용합니다.
        batch_type_clause = (
            "AND b.batch_type = 'manual'" if "batch_type" in batch_columns else ""
        )
        has_exclusions = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'error_exclusions'
            """
        ).fetchone()
        exclusion_clause = (
            """
            AND NOT EXISTS (
                SELECT 1 FROM error_exclusions AS x
                WHERE x.order_number = d.order_number
                  AND x.restored_at IS NULL
            )
            """
            if has_exclusions
            else ""
        )
        rows = connection.execute(
            f"""
            SELECT d.payload_json
            FROM error_approval_details AS d
            JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
            WHERE b.rolled_back_at IS NULL
              {batch_type_clause}
              {exclusion_clause}
            ORDER BY d.detail_id
            """
        ).fetchall()
    payloads = [json.loads(row[0]) for row in rows]
    if not payloads:
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS))
    raw = pd.DataFrame(payloads)
    return filter_valid_rows(raw, exclusion_keys, source=path.name)


def load_active_sqlite_negative_truth(
    path: Path,
    exclusion_keys: set[str],
) -> pd.DataFrame:
    """Load manager-excluded errors as normal counterexamples."""
    with sqlite3.connect(path) as connection:
        has_exclusions = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'error_exclusions'
            """
        ).fetchone()
        if not has_exclusions:
            return pd.DataFrame(columns=list(REQUIRED_COLUMNS))
        rows = connection.execute(
            """
            SELECT d.order_number, d.payload_json
            FROM error_approval_details AS d
            JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
            JOIN error_exclusions AS x ON x.order_number = d.order_number
            WHERE b.rolled_back_at IS NULL
              AND x.restored_at IS NULL
            ORDER BY d.detail_id DESC
            """
        ).fetchall()
    payload_by_order: dict[str, dict[str, object]] = {}
    for order_number, payload_json in rows:
        key = normalize_order_number(order_number)
        if key is None or key in payload_by_order:
            continue
        payload_by_order[key] = json.loads(payload_json)
    if not payload_by_order:
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS))
    raw = pd.DataFrame(payload_by_order.values())
    return filter_valid_rows(raw, exclusion_keys, source=f"{path.name}/exclusions")


def current_preprocessed_path(manifest_path: Path) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    job_id = str(manifest["job_id"])
    filename = str(manifest["preprocessed_file"])
    path = manifest_path.parent / job_id / filename
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def update_counts(
    frame: pd.DataFrame,
    truth_ids: set[str],
    total_counts: Counter[tuple[str, str]],
    matched_truth_source: dict[str, tuple[str, str]],
    sap_id_map: dict[str, str],
) -> tuple[int, int]:
    signatures = signature_rows(
        frame,
        sap_id_map,
        allow_empty_signature=True,
    )
    total_counts.update(
        zip(signatures["source_subcategory"], signatures["signature"])
    )

    matched = 0
    for row in signatures.itertuples(index=False):
        if row.order_number not in truth_ids:
            continue
        matched += 1
        actual = (str(row.source_subcategory), str(row.signature))
        order_number = str(row.order_number)
        if order_number in matched_truth_source:
            raise ValueError(
                "정답 오더가 전체 데이터에서 두 번 이상 매칭됩니다: "
                f"오더={order_number}"
            )
        # The total-data row is authoritative for future inference.  The truth
        # workbook can contain later annotations or manually amended details.
        matched_truth_source[order_number] = actual
    return len(signatures), matched


def update_current_truth_sources(
    frame: pd.DataFrame,
    truth_ids: set[str],
    matched_truth_source: dict[str, tuple[str, str]],
    sap_id_map: dict[str, str],
) -> tuple[int, int]:
    """Refresh overlapping manual truth text without treating unlabeled rows as normal."""
    signatures = signature_rows(
        frame,
        sap_id_map,
        allow_empty_signature=True,
    )
    matched = 0
    for row in signatures.itertuples(index=False):
        order_number = str(row.order_number)
        if order_number not in truth_ids:
            continue
        matched += 1
        matched_truth_source[order_number] = (
            str(row.source_subcategory),
            str(row.signature),
        )
    return len(signatures), matched


def file_fingerprint(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def content_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def historical_sources(args: argparse.Namespace) -> list[Path]:
    sources: list[Path] = []
    for month in range(1, 7):
        source = (
            args.historical_source_dir
            / f"26년 {month}월 전체 서비스오더 리스트.xlsx"
        )
        if not source.is_file():
            raise FileNotFoundError(
                f"{month}월 과거 원본 파일을 찾지 못했습니다: {source}"
            )
        sources.append(source)
    return sources


def baseline_fingerprint(
    args: argparse.Namespace,
    sources: list[Path],
) -> str:
    from service_order.error import analysis_pipeline

    payload = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "signature_version": BASELINE_SIGNATURE_VERSION,
        "sources": [file_fingerprint(path) for path in sources],
        "truth": file_fingerprint(args.truth),
        "except_list_sha256": content_digest(args.except_list),
        "sap_id_sha256": content_digest(analysis_pipeline.SAP_ID_PATH),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_baseline(
    path: Path,
    fingerprint: str,
    truth_ids: set[str],
) -> tuple[
    Counter[tuple[str, str]],
    dict[str, tuple[str, str]],
    int,
] | None:
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(path) as connection:
            metadata = dict(
                connection.execute("SELECT key, value FROM metadata").fetchall()
            )
            if metadata.get("fingerprint") != fingerprint:
                return None
            if int(metadata.get("schema_version", 0)) != BASELINE_SCHEMA_VERSION:
                return None
            counts = Counter(
                {
                    (str(owner), str(signature)): int(row_count)
                    for owner, signature, row_count in connection.execute(
                        """
                        SELECT source_subcategory, signature, row_count
                        FROM signature_counts
                        """
                    )
                }
            )
            truth_sources = {
                str(order_number): (str(owner), str(signature))
                for order_number, owner, signature in connection.execute(
                    """
                    SELECT order_number, source_subcategory, signature
                    FROM truth_sources
                    """
                )
            }
            if set(truth_sources) != truth_ids:
                return None
            return counts, truth_sources, int(metadata["valid_row_count"])
    except (KeyError, sqlite3.DatabaseError, ValueError):
        return None


def save_baseline(
    path: Path,
    fingerprint: str,
    total_counts: Counter[tuple[str, str]],
    truth_sources: dict[str, tuple[str, str]],
    valid_row_count: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.stem}_{uuid.uuid4().hex[:8]}_writing.sqlite3"
    )
    connection: sqlite3.Connection | None = None
    try:
        with sqlite3.connect(temporary) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = OFF;
                PRAGMA synchronous = OFF;
                CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE signature_counts (
                    source_subcategory TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    PRIMARY KEY (source_subcategory, signature)
                );
                CREATE TABLE truth_sources (
                    order_number TEXT PRIMARY KEY,
                    source_subcategory TEXT NOT NULL,
                    signature TEXT NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                (
                    ("schema_version", str(BASELINE_SCHEMA_VERSION)),
                    ("fingerprint", fingerprint),
                    ("valid_row_count", str(valid_row_count)),
                ),
            )
            connection.executemany(
                """
                INSERT INTO signature_counts(
                    source_subcategory, signature, row_count
                ) VALUES (?, ?, ?)
                """,
                (
                    (owner, signature, int(count))
                    for (owner, signature), count in total_counts.items()
                ),
            )
            connection.executemany(
                """
                INSERT INTO truth_sources(
                    order_number, source_subcategory, signature
                ) VALUES (?, ?, ?)
                """,
                (
                    (order_number, owner, signature)
                    for order_number, (owner, signature) in truth_sources.items()
                ),
            )
            connection.commit()
        connection.close()
        connection = None
        temporary.replace(path)
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)


def load_or_build_historical_baseline(
    args: argparse.Namespace,
    exclusion_keys: set[str],
    truth_ids: set[str],
    sap_id_map: dict[str, str],
) -> tuple[
    Counter[tuple[str, str]],
    dict[str, tuple[str, str]],
    int,
    bool,
]:
    sources = historical_sources(args)
    fingerprint = baseline_fingerprint(args, sources)
    if not args.rebuild_baseline:
        cached = load_baseline(args.baseline, fingerprint, truth_ids)
        if cached is not None:
            counts, truth_sources, row_count = cached
            return counts, truth_sources, row_count, True

    total_counts: Counter[tuple[str, str]] = Counter()
    matched_truth_source: dict[str, tuple[str, str]] = {}
    valid_row_count = 0
    matched_truth_count = 0
    for source in sources:
        raw = load_historical_total(source, exclusion_keys)
        valid = filter_valid_rows(raw, exclusion_keys, source=source.name)
        row_count, matched_count = update_counts(
            valid,
            truth_ids,
            total_counts,
            matched_truth_source,
            sap_id_map,
        )
        valid_row_count += row_count
        matched_truth_count += matched_count
    if matched_truth_count != len(truth_ids):
        raise ValueError(
            "1~6월 전체 데이터와 정답 매칭 불일치: "
            f"{matched_truth_count:,}/{len(truth_ids):,}"
        )
    save_baseline(
        args.baseline,
        fingerprint,
        total_counts,
        matched_truth_source,
        valid_row_count,
    )
    return total_counts, matched_truth_source, valid_row_count, False


def build_registry(args: argparse.Namespace) -> dict[str, object]:
    output_path = _resolved_output_path(Path(args.output))
    existing_document = _load_existing_unified_document(output_path)
    exclusion_keys = load_exclusion_keys(args.except_list)
    from service_order.error.analysis_pipeline import load_configuration

    sap_id_map, _ = load_configuration()

    truth_jan_jun = signature_rows(
        load_truth_workbook(args.truth, exclusion_keys),
        sap_id_map,
    )
    truth_july = signature_rows(
        load_active_sqlite_truth(args.database, exclusion_keys),
        sap_id_map,
    )
    negative_truth = signature_rows(
        load_active_sqlite_negative_truth(args.database, exclusion_keys),
        sap_id_map,
        allow_empty_signature=True,
    )
    truth = pd.concat([truth_jan_jun, truth_july], ignore_index=True)
    duplicated_truth = truth["order_number"].duplicated(keep=False)
    if duplicated_truth.any():
        sample = truth.loc[duplicated_truth, "order_number"].head(10).tolist()
        raise ValueError(f"정답 오더번호 중복: {sample}")

    historical_truth_ids = set(truth_jan_jun["order_number"])
    current_truth_ids = set(truth_july["order_number"])
    (
        total_counts,
        matched_truth_source,
        valid_row_count,
        baseline_reused,
    ) = load_or_build_historical_baseline(
        args,
        exclusion_keys,
        historical_truth_ids,
        sap_id_map,
    )

    for row in truth_july.itertuples(index=False):
        matched_truth_source[str(row.order_number)] = (
            str(row.source_subcategory),
            str(row.signature),
        )

    current_path = current_preprocessed_path(args.current_manifest)
    current_raw = pd.read_excel(current_path)
    current_valid = filter_valid_rows(
        current_raw, exclusion_keys, source=current_path.name
    )
    row_count, current_matched_count = update_current_truth_sources(
        current_valid,
        current_truth_ids,
        matched_truth_source,
        sap_id_map,
    )
    valid_row_count += row_count

    confirmed_counts = Counter(matched_truth_source.values())
    # Only 1~6월 full data is an exhaustive normal/confirmed population.
    # Current uploads are unlabeled until a person approves a row, so treating
    # every unmatched current row as normal would immediately suppress a valid
    # learned rule. Add only human-approved current truth to the denominator.
    for order_number in current_truth_ids:
        total_counts[matched_truth_source[order_number]] += 1
    total_counts.update(
        zip(
            negative_truth["source_subcategory"],
            negative_truth["signature"],
        )
    )

    records: list[dict[str, object]] = []
    for source_subcategory, signature in sorted(confirmed_counts):
        confirmed_count = int(confirmed_counts[(source_subcategory, signature)])
        total_count = int(total_counts[(source_subcategory, signature)])
        normal_count = total_count - confirmed_count
        if normal_count < 0:
            raise ValueError(
                f"서명 총건수가 정답보다 작습니다: {source_subcategory}/{signature}"
            )
        records.append(
            {
                "source_subcategory": source_subcategory,
                "signature": signature,
                "confirmed_count": confirmed_count,
                "normal_count": normal_count,
                "status": "active" if normal_count == 0 else "ambiguous",
            }
        )

    phrase_records = build_phrase_records(total_counts, confirmed_counts)
    active = [record for record in records if record["status"] == "active"]
    ambiguous = [record for record in records if record["status"] == "ambiguous"]
    active_phrases = [
        record for record in phrase_records if record["status"] == "active"
    ]
    ambiguous_phrases = [
        record for record in phrase_records if record["status"] == "ambiguous"
    ]
    training_cutoff = _manifest_training_cutoff(
        Path(args.current_manifest),
        existing_document,
    )
    document = build_unified_error_document(
        records,
        phrase_records,
        training_cutoff=training_cutoff,
        existing_document=existing_document,
        source_manifest=_registry_source_manifest(args),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_name(
        f".{output_path.stem}_{uuid.uuid4().hex[:8]}_writing.json"
    )
    try:
        temporary_output.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_output.replace(output_path)
    finally:
        temporary_output.unlink(missing_ok=True)

    return {
        "truth_jan_jun": len(truth_jan_jun),
        "truth_july": len(truth_july),
        "truth_total": len(truth),
        "negative_truth_rows": len(negative_truth),
        "valid_total_rows": valid_row_count,
        "matched_truth_rows": len(matched_truth_source),
        "current_truth_rows_refreshed": current_matched_count,
        "historical_baseline_reused": baseline_reused,
        "unique_signatures": len(records),
        "active_signatures": len(active),
        "active_confirmed_rows": sum(
            int(record["confirmed_count"]) for record in active
        ),
        "ambiguous_signatures": len(ambiguous),
        "ambiguous_confirmed_rows": sum(
            int(record["confirmed_count"]) for record in ambiguous
        ),
        "ambiguous_normal_rows": sum(
            int(record["normal_count"]) for record in ambiguous
        ),
        "phrase_rules": len(phrase_records),
        "active_phrase_rules": len(active_phrases),
        "active_phrase_confirmed_coverage": sum(
            int(record["confirmed_count"]) for record in active_phrases
        ),
        "ambiguous_phrase_rules": len(ambiguous_phrases),
        "schema_version": int(document["schema_version"]),
        "rule_version": str(document["rule_version"]),
        "training_cutoff": str(document["training_cutoff"]),
        "rule_count": len(document["rules"]),
        "generated_at": datetime.now().astimezone().isoformat(),
        "output": str(output_path.resolve()),
    }


def build_registry_from_paths(
    *,
    truth: Path = DEFAULT_TRUTH_PATH,
    database: Path = DEFAULT_DATABASE_PATH,
    current_manifest: Path = DEFAULT_MANIFEST_PATH,
    except_list: Path = DEFAULT_EXCEPT_LIST_PATH,
    historical_source_dir: Path = DEFAULT_HISTORICAL_SOURCE_DIR,
    baseline: Path = DEFAULT_BASELINE_PATH,
    output: Path = DEFAULT_OUTPUT_PATH,
    rebuild_baseline: bool = False,
) -> dict[str, object]:
    """Rebuild the deduplicated registry for the running dashboard server."""
    return build_registry(
        argparse.Namespace(
            truth=truth,
            database=database,
            current_manifest=current_manifest,
            except_list=except_list,
            historical_source_dir=historical_source_dir,
            baseline=baseline,
            output=output,
            rebuild_baseline=rebuild_baseline,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--truth", type=Path, default=DEFAULT_TRUTH_PATH)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--current-manifest", type=Path, default=DEFAULT_MANIFEST_PATH
    )
    parser.add_argument("--except-list", type=Path, default=DEFAULT_EXCEPT_LIST_PATH)
    parser.add_argument(
        "--historical-source-dir",
        type=Path,
        default=DEFAULT_HISTORICAL_SOURCE_DIR,
    )
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE_PATH)
    parser.add_argument("--rebuild-baseline", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser.parse_args()


if __name__ == "__main__":
    print(
        json.dumps(
            build_registry(parse_args()), ensure_ascii=False, indent=2
        )
    )
