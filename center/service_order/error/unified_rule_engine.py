from __future__ import annotations

"""Frozen classifier backed only by the consolidated rule documents.

This module deliberately does not read or update the legacy keyword registries.
It reconstructs the existing matching lookups from ``normal_rules.json`` and
``error_rules.json`` and can optionally enable conservative context guards.
"""

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import json
import re

import pandas as pd

from service_order.error.analysis_pipeline import (
    SAP_ID_PATH,
    build_pattern_lookup,
    build_proper_nouns,
    format_classified_orders,
    matched_owners,
    matches_scoped_error_phrase,
    normalize_details,
    normalize_text_value,
    promote_high_confidence_similar_candidates,
    remove_known_proper_nouns,
)


BASE_DIR = Path(__file__).resolve().parent
NORMAL_RULES_PATH = BASE_DIR / "json" / "normal_rules.json"
ERROR_RULES_PATH = BASE_DIR / "json" / "error_rules.json"

_BRACKET_PATTERN = re.compile(r"[\[\uFF3B\u3010]([^\]\uFF3D\u3011]{1,100})[\]\uFF3D\u3011]")
_LEADING_GENERIC_TOKENS = frozenset(
    {
        "고객",
        "문의",
        "요청",
        "확인",
        "처리",
        "관련",
        "건",
        "내용",
        "전화",
        "통화",
    }
)
_GENERIC_BRACKET_TAGS = frozenset(
    {
        "현장",
        "사무실",
        "고객요청",
        "현장고객요청",
        "현장시간",
        "사전입력",
        "참고",
        "메모",
        "기타",
    }
)
_STRONG_SINGLE_SIGNAL_OWNERS = {
    "공급중지": frozenset({"공급중지"}),
    "공급중단": frozenset({"공급중지"}),
    "재연결": frozenset({"공급중지"}),
    "체납재연결": frozenset({"공급중지"}),
    "재공급": frozenset({"공급중지"}),
    "전산해지": frozenset({"공급중지"}),
    "예고장": frozenset({"체납"}),
    "납부약속": frozenset({"체납"}),
    "부적합": frozenset({"안전점검 부적합"}),
    "계량기교체": frozenset({"계량기일반"}),
    "난검침": frozenset({"검침"}),
    "지침수정": frozenset({"검침"}),
    "혹서기": frozenset({"검침"}),
    "격월검침": frozenset({"검침"}),
    "에쓰시지그리드": frozenset({"종합에러"}),
}
_STRONG_SEMANTIC_TYPES = frozenset(
    {"business_action", "outcome", "specific_phrase"}
)
_GENERIC_CONTEXT_PATTERNS = frozenset(
    {
        "고객",
        "문의",
        "요청",
        "확인",
        "처리",
        "관련",
        "검침",
        "계량기",
        "안전점검",
        "고지서",
        "송달",
        "전입",
        "전출",
        "mms",
    }
)


@dataclass(frozen=True)
class ContextPattern:
    rule_id: str
    owner: str
    kind: str
    value: str
    tokens: tuple[str, ...]


def _load_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(f"통합 규칙 파일이 없습니다: {path}")
    with path.open(encoding="utf-8") as file:
        document = json.load(file)
    if not isinstance(document, dict) or not isinstance(document.get("rules"), list):
        raise ValueError(f"{path.name}의 최상위 rules 배열이 필요합니다.")
    return document


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "active"}


def _owner(record: dict[str, object]) -> str:
    return str(
        record.get("source_subcategory")
        or record.get("subcategory")
        or record.get("owner")
        or ""
    ).strip()


def _pattern(record: dict[str, object]) -> tuple[str, str, tuple[str, ...]]:
    match = record.get("pattern") or record.get("match") or {}
    if not isinstance(match, dict):
        match = {}
    kind = str(
        match.get("kind")
        or record.get("pattern_kind")
        or record.get("pattern_type")
        or ""
    ).strip().casefold()
    raw_value = match.get("value", record.get("pattern", ""))
    tokens_value = match.get("tokens")
    parts_value = match.get("parts")

    if isinstance(raw_value, (list, tuple)):
        raw_value = " ".join(str(value) for value in raw_value)
    value = str(raw_value or "").strip()
    if isinstance(tokens_value, list):
        tokens = tuple(
            token
            for token in (normalize_text_value(value) for value in tokens_value)
            if token
        )
    else:
        tokens = tuple(normalize_text_value(value).split())

    if kind in {"unordered_parts", "unordered", "비연속조합"}:
        if isinstance(parts_value, list) and len(parts_value) == 2:
            parts = [str(part).strip() for part in parts_value]
        else:
            parts = [part.strip() for part in value.split("+")]
        if len(parts) == 2 and all(parts):
            value = f"{parts[0]} + {parts[1]}"
            tokens = tuple(
                token
                for part in parts
                for token in normalize_text_value(part).split()
            )
    return kind, value, tokens


def _legacy_pattern_type(kind: str) -> str:
    if kind in {"unordered_parts", "unordered", "비연속조합"}:
        return "비연속조합"
    if kind in {"exact_normalized", "exact", "sentence", "반복문장"}:
        return "반복문장"
    if kind in {"token", "single", "단일키워드"}:
        return "단일키워드"
    return "연속문구"


def _add_pattern(
    document: dict[str, dict[str, list[str]]],
    owner: str,
    kind: str,
    value: str,
) -> None:
    if not owner or not value:
        return
    values = document.setdefault(owner, {}).setdefault(
        _legacy_pattern_type(kind), []
    )
    if value not in values:
        values.append(value)


def _origin_items(record: dict[str, object]) -> list[dict[str, object]]:
    evidence = record.get("evidence") or {}
    if not isinstance(evidence, dict):
        return []
    origins = evidence.get("origins") or []
    return [item for item in origins if isinstance(item, dict)]


def _is_strict_context_rule(
    record: dict[str, object],
    behavior: dict[str, object],
    owner: str,
    kind: str,
    value: str,
    tokens: tuple[str, ...],
    *,
    unique_owner: bool,
) -> bool:
    """Return only high-purity rules suitable for context candidate locks.

    A context rule is deliberately stricter than a normal rule.  It must have
    one normal owner, and learned scoped rules must satisfy support/month
    thresholds.  This prevents broad scoped tokens such as ``mms`` or
    ``안전점검`` from turning ordinary rows into candidates.
    """
    if not unique_owner or not _bool(behavior.get("own_normal")):
        return False
    semantic_type = str(record.get("semantic_type") or "").strip().casefold()
    if semantic_type in _STRONG_SEMANTIC_TYPES:
        return True
    if _bool(behavior.get("dominant_other_candidate")):
        return True
    compact_value = "".join(tokens)
    if compact_value in _GENERIC_CONTEXT_PATTERNS:
        return False

    origins = _origin_items(record)
    if len(tokens) == 1:
        expected_owners = _STRONG_SINGLE_SIGNAL_OWNERS.get(compact_value)
        if expected_owners is not None and owner not in expected_owners:
            return False
        for item in origins:
            if str(item.get("role") or "").strip() != "scoped_normal":
                continue
            evidence = item.get("evidence") or {}
            if not isinstance(evidence, dict):
                continue
            if (
                len(compact_value) >= 4
                and int(evidence.get("training_support") or 0) >= 20
                and int(evidence.get("training_months") or 0) >= 4
                and int(evidence.get("confirmed_error_hits") or 0) == 0
            ):
                return True
        return False

    # Context locks are evidence-based.  Manual normal/priority phrases still
    # drive ordinary classification, but do not become cross-owner locks by
    # themselves; otherwise legitimate phrases such as ``고지서 송달요청``
    # inflate review rows in another allowed subcategory.
    for item in origins:
        role = str(item.get("role") or "").strip()
        evidence = item.get("evidence") or {}
        if not isinstance(evidence, dict):
            continue
        if role == "scoped_normal" and (
            int(evidence.get("training_support") or 0) >= 10
            and int(evidence.get("training_months") or 0) >= 3
            and int(evidence.get("confirmed_error_hits") or 0) == 0
        ):
            return True
        if role == "auto_normal" and (
            len(tokens) >= 3
            and int(evidence.get("normal_support") or 0) >= 10
            and int(evidence.get("other_subcategory_frequency") or 0) == 0
            and int(evidence.get("confirmed_error_pattern_hits") or 0) == 0
        ):
            return True
    return False


def _normal_lookups(document: dict[str, object]) -> dict[str, object]:
    own_document: dict[str, dict[str, list[str]]] = {}
    collision_document: dict[str, dict[str, list[str]]] = {}
    hard_document: dict[str, dict[str, list[str]]] = {}
    override_document: dict[str, dict[str, list[str]]] = {}
    dominant_document: dict[str, dict[str, list[str]]] = {}
    context_document: dict[str, dict[str, list[str]]] = {}
    parsed_records: list[
        tuple[dict[str, object], dict[str, object], str, str, str, tuple[str, ...]]
    ] = []
    own_pattern_owners: defaultdict[tuple[str, str], set[str]] = defaultdict(set)

    active_rule_count = 0
    for index, raw_record in enumerate(document["rules"]):
        if not isinstance(raw_record, dict):
            raise ValueError(f"normal_rules.json rules[{index}]가 객체가 아닙니다.")
        record = raw_record
        owner = _owner(record)
        kind, value, tokens = _pattern(record)
        behavior = record.get("behavior") or record.get("effective") or {}
        if not isinstance(behavior, dict):
            behavior = {}
        status = str(record.get("status") or "active").strip().casefold()
        if status in {"inactive", "blocked_error", "disabled", "retired"}:
            continue
        if not owner or not value:
            raise ValueError(
                "활성 통합 정상 규칙에는 source_subcategory와 pattern.value가 "
                f"필요합니다: rules[{index}]"
            )
        active_rule_count += 1
        own_normal = _bool(behavior.get("own_normal"))
        override = _bool(behavior.get("override_collision"))
        dominant = _bool(behavior.get("dominant_other_candidate"))
        cross = str(behavior.get("cross_collision") or "none").strip().casefold()

        if own_normal:
            _add_pattern(own_document, owner, kind, value)
            own_pattern_owners[(kind, value)].add(owner)
        if cross in {"hard", "soft"}:
            _add_pattern(collision_document, owner, kind, value)
        if cross == "hard":
            _add_pattern(hard_document, owner, kind, value)
        if override:
            _add_pattern(override_document, owner, kind, value)
        if dominant:
            _add_pattern(dominant_document, owner, kind, value)
        parsed_records.append((record, behavior, owner, kind, value, tokens))

    context_patterns_by_owner: defaultdict[str, list[ContextPattern]] = defaultdict(list)
    for record, behavior, owner, kind, value, tokens in parsed_records:
        if not _is_strict_context_rule(
            record,
            behavior,
            owner,
            kind,
            value,
            tokens,
            unique_owner=len(own_pattern_owners.get((kind, value), set())) == 1,
        ):
            continue
        _add_pattern(context_document, owner, kind, value)
        context_patterns_by_owner[owner].append(
            ContextPattern(
                rule_id=str(record.get("rule_id") or f"{owner}:{kind}:{value}"),
                owner=owner,
                kind=kind,
                value=value,
                tokens=tokens,
            )
        )

    return {
        "own": build_pattern_lookup([own_document]),
        "collision": build_pattern_lookup([collision_document]),
        "hard": build_pattern_lookup([hard_document]),
        "override": build_pattern_lookup([override_document]),
        "dominant": build_pattern_lookup([dominant_document]),
        "context": build_pattern_lookup([context_document]),
        "context_patterns_by_owner": dict(context_patterns_by_owner),
        "context_pattern_count": sum(
            len(values) for values in context_patterns_by_owner.values()
        ),
        "active_rule_count": active_rule_count,
    }


def _decision(record: dict[str, object]) -> str:
    decision = str(
        record.get("decision")
        or record.get("action")
        or record.get("status")
        or ""
    ).strip().casefold()
    aliases = {
        "active": "auto_error",
        "confirmed": "auto_error",
        "confirmed_error": "auto_error",
        "ambiguous": "review_lock",
        "ambiguous_review": "review_lock",
        "review": "review_lock",
        "disabled": "inactive",
    }
    return aliases.get(decision, decision)


def _error_rule_sets(document: dict[str, object]) -> dict[str, object]:
    active_exact: set[tuple[str, str]] = set()
    review_exact: set[tuple[str, str]] = set()
    active_phrases: dict[str, dict[int, set[tuple[str, ...]]]] = defaultdict(
        lambda: defaultdict(set)
    )
    review_phrases: dict[str, dict[int, set[tuple[str, ...]]]] = defaultdict(
        lambda: defaultdict(set)
    )
    active_rule_count = 0

    for index, raw_record in enumerate(document["rules"]):
        if not isinstance(raw_record, dict):
            raise ValueError(f"error_rules.json rules[{index}]가 객체가 아닙니다.")
        owner = _owner(raw_record)
        kind, value, tokens = _pattern(raw_record)
        decision = _decision(raw_record)
        if decision in {"inactive", "audit_only", "evidence_only", ""}:
            continue
        if decision not in {"auto_error", "review_lock"}:
            raise ValueError(
                f"error_rules.json rules[{index}]의 decision을 해석할 수 없습니다: "
                f"{raw_record.get('decision')}"
            )
        normalized = normalize_text_value(value)
        if not owner or not normalized:
            raise ValueError(
                "활성 통합 오생성 규칙에는 source_subcategory와 pattern.value가 "
                f"필요합니다: rules[{index}]"
            )
        active_rule_count += 1
        is_phrase = kind in {
            "ordered_distinct_tokens",
            "ordered_phrase",
            "phrase",
            "unordered_parts",
        }
        if is_phrase:
            phrase = tuple(tokens or normalized.split())
            if len(phrase) < 2:
                continue
            target = active_phrases if decision == "auto_error" else review_phrases
            target[owner][len(phrase)].add(phrase)
        elif decision == "auto_error":
            active_exact.add((owner, normalized))
        else:
            review_exact.add((owner, normalized))

    # An explicit automatic rule is the highest-priority decision.
    review_exact.difference_update(active_exact)
    return {
        "active_exact": active_exact,
        "review_exact": review_exact,
        "active_phrases": {
            owner: {length: set(values) for length, values in by_length.items()}
            for owner, by_length in active_phrases.items()
        },
        "review_phrases": {
            owner: {length: set(values) for length, values in by_length.items()}
            for owner, by_length in review_phrases.items()
        },
        "active_rule_count": active_rule_count,
    }


def _normalized_piece(value: object, proper_nouns: set[str]) -> str:
    return remove_known_proper_nouns(normalize_text_value(value), proper_nouns)


def _pattern_matches_tokens(
    tokens: tuple[str, ...],
    pattern: ContextPattern,
    *,
    prefix_only: bool = False,
) -> bool:
    expected = pattern.tokens
    if not expected:
        return False
    if pattern.kind in {"exact_normalized", "exact", "sentence", "반복문장"}:
        return tokens == expected
    if pattern.kind in {"token", "single", "단일키워드"}:
        return bool(tokens) and (
            tokens[0] == expected[0] if prefix_only else expected[0] in tokens
        )
    if pattern.kind in {"unordered_parts", "unordered", "비연속조합"}:
        if prefix_only:
            return False
        parts = [
            tuple(normalize_text_value(part).split())
            for part in pattern.value.split("+")
            if normalize_text_value(part)
        ]
        return len(parts) == 2 and all(
            any(tokens[index : index + len(part)] == part for index in range(len(tokens)))
            for part in parts
        )
    if prefix_only:
        return tokens[: len(expected)] == expected
    return any(
        tokens[index : index + len(expected)] == expected
        for index in range(len(tokens) - len(expected) + 1)
    )


def _matched_context_patterns(
    text: str,
    owners: Iterable[str],
    patterns_by_owner: dict[str, list[ContextPattern]],
    *,
    prefix_only: bool = False,
) -> dict[str, list[ContextPattern]]:
    tokens = tuple(normalize_text_value(text).split())
    result: dict[str, list[ContextPattern]] = {}
    for owner in owners:
        matched = [
            pattern
            for pattern in patterns_by_owner.get(owner, [])
            if _pattern_matches_tokens(tokens, pattern, prefix_only=prefix_only)
        ]
        if matched:
            result[owner] = matched
    return result


def _bracket_foreign_owners(
    raw_detail: object,
    owner: str,
    proper_nouns: set[str],
    context_lookup: object,
) -> set[str]:
    result: set[str] = set()
    raw = "" if pd.isna(raw_detail) else str(raw_detail)
    for match in _BRACKET_PATTERN.finditer(raw):
        content = _normalized_piece(match.group(1), proper_nouns)
        if not content or "".join(content.split()) in _GENERIC_BRACKET_TAGS:
            continue
        context_owners = matched_owners(content, context_lookup)
        # Bracket text becomes a lock only when it contains both a strong
        # owner anchor and a strong foreign business phrase.  A lone generic
        # foreign word inside brackets is not sufficient.
        if owner in context_owners:
            result.update(context_owners - {owner})
    return result


def _leading_foreign_owners(
    raw_detail: object,
    owner: str,
    proper_nouns: set[str],
    context_lookup: object,
    patterns_by_owner: dict[str, list[ContextPattern]],
) -> set[str]:
    raw = "" if pd.isna(raw_detail) else str(raw_detail)
    without_brackets = _BRACKET_PATTERN.sub(" ", raw)
    normalized = _normalized_piece(without_brackets, proper_nouns)
    meaningful = [
        token
        for token in normalized.split()
        if token not in _LEADING_GENERIC_TOKENS
    ][:3]
    if not meaningful:
        return set()
    leading_text = " ".join(meaningful)
    possible_owners = matched_owners(leading_text, context_lookup)
    matches = _matched_context_patterns(
        leading_text,
        possible_owners,
        patterns_by_owner,
        prefix_only=True,
    )
    own_specificity = max(
        (len(pattern.tokens) for pattern in matches.get(owner, [])),
        default=0,
    )
    return {
        pattern_owner
        for pattern_owner, patterns in matches.items()
        if pattern_owner != owner
        and max(len(pattern.tokens) for pattern in patterns) > own_specificity
    }


def _independent_pattern_pair(patterns: list[ContextPattern]) -> bool:
    for left_index, left in enumerate(patterns):
        left_tokens = set(left.tokens)
        for right in patterns[left_index + 1 :]:
            right_tokens = set(right.tokens)
            if left.rule_id == right.rule_id:
                continue
            # Nested variants of one phrase are one signal, not two.
            if left_tokens.issubset(right_tokens) or right_tokens.issubset(left_tokens):
                continue
            return True
    return False


def _body_conflict_owners(
    detail: str,
    owner: str,
    context_lookup: object,
    patterns_by_owner: dict[str, list[ContextPattern]],
) -> set[str]:
    possible_owners = matched_owners(detail, context_lookup) - {owner}
    matches = _matched_context_patterns(
        detail,
        possible_owners,
        patterns_by_owner,
    )
    return {
        pattern_owner
        for pattern_owner, patterns in matches.items()
        if _independent_pattern_pair(patterns)
    }


def _load_sap_id_map() -> dict[str, str]:
    if not SAP_ID_PATH.is_file():
        return {}
    with SAP_ID_PATH.open(encoding="utf-8") as file:
        document = json.load(file)
    if not isinstance(document, dict):
        raise ValueError("sap_id.json 최상위 값은 객체여야 합니다.")
    return {
        str(sap_id).strip().upper(): str(name).strip()
        for sap_id, name in document.items()
    }


def select_candidate_orders_unified(
    preprocessed: pd.DataFrame,
    *,
    enable_context_policies: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    """Classify preprocessed rows using only the consolidated rule files.

    ``enable_context_policies=False`` is the legacy-equivalence mode.  When it
    is true, bracket, leading-phrase and strong foreign-normal signals become
    conservative candidate locks.  Exact and active phrase error rules still
    take precedence over every lock.
    """
    required = {"오더번호", "오더생성일", "소분류", "내역"}
    missing = sorted(required - set(preprocessed.columns))
    if missing:
        raise KeyError(f"후보 판정 필수 열이 없습니다: {missing}")

    normal_document = _load_json(NORMAL_RULES_PATH)
    error_document = _load_json(ERROR_RULES_PATH)
    normal = _normal_lookups(normal_document)
    error = _error_rule_sets(error_document)

    subcategories = preprocessed["소분류"].astype("string").str.strip()
    details = preprocessed["내역"].astype("string").str.strip()
    valid_rows = subcategories.notna() & subcategories.ne("") & details.notna() & details.ne("")
    orders = preprocessed.loc[valid_rows].copy().reset_index(drop=True)
    orders["소분류"] = orders["소분류"].astype("string").str.strip()
    orders["내역"] = orders["내역"].astype("string").str.strip()

    proper_nouns = build_proper_nouns(orders, _load_sap_id_map())
    normalized_details = normalize_details(orders["내역"], proper_nouns)

    candidate_flags: list[bool] = []
    auto_error_flags: list[bool] = []
    candidate_lock_flags: list[bool] = []
    counters: defaultdict[str, int] = defaultdict(int)
    context_candidate_trace: dict[str, dict[str, set[str]]] = {}

    for order_number, raw_detail, subcategory, detail in zip(
        orders["오더번호"],
        orders["내역"],
        orders["소분류"],
        normalized_details,
    ):
        owner = "" if pd.isna(subcategory) else str(subcategory)
        exact_auto_error = (owner, detail) in error["active_exact"]
        phrase_auto_error = (
            not exact_auto_error
            and matches_scoped_error_phrase(owner, detail, error["active_phrases"])
        )
        auto_error = exact_auto_error or phrase_auto_error

        exact_review = (owner, detail) in error["review_exact"]
        phrase_review = (
            not exact_review
            and matches_scoped_error_phrase(owner, detail, error["review_phrases"])
        )
        protected_review = exact_review or phrase_review

        dominant_owners = matched_owners(detail, normal["dominant"])
        dominant_anomaly = any(value != owner for value in dominant_owners)
        own_owners = matched_owners(detail, normal["own"])
        override_owners = matched_owners(detail, normal["override"])
        collision_owners = matched_owners(detail, normal["collision"])
        hard_owners = matched_owners(detail, normal["hard"])
        own_match = owner in own_owners
        override_match = owner in override_owners
        other_match = any(value != owner for value in collision_owners)
        hard_other_match = any(value != owner for value in hard_owners)

        bracket_owners: set[str] = set()
        leading_owners: set[str] = set()
        strong_owners: set[str] = set()
        if enable_context_policies:
            bracket_owners = _bracket_foreign_owners(
                raw_detail, owner, proper_nouns, normal["context"]
            )
            leading_owners = _leading_foreign_owners(
                raw_detail,
                owner,
                proper_nouns,
                normal["context"],
                normal["context_patterns_by_owner"],
            )
            strong_owners = _body_conflict_owners(
                detail,
                owner,
                normal["context"],
                normal["context_patterns_by_owner"],
            )
        bracket_lock = bool(bracket_owners)
        leading_lock = bool(leading_owners)
        strong_lock = bool(strong_owners)
        context_lock = bracket_lock or leading_lock or strong_lock
        candidate_lock = protected_review or dominant_anomaly or context_lock

        normal_row = (
            not auto_error
            and not candidate_lock
            and (override_match or (own_match and not hard_other_match))
        )
        is_candidate = not auto_error and not normal_row
        candidate_flags.append(is_candidate)
        auto_error_flags.append(auto_error)
        candidate_lock_flags.append(is_candidate and candidate_lock)
        if is_candidate and context_lock:
            order_key = "" if pd.isna(order_number) else str(order_number).strip()
            if not order_key:
                order_key = f"__row_{len(candidate_flags) - 1}"
            trace = context_candidate_trace.setdefault(
                order_key,
                {"signals": set(), "foreign_subcategories": set()},
            )
            if bracket_lock:
                trace["signals"].add("bracket")
                trace["foreign_subcategories"].update(bracket_owners)
            if leading_lock:
                trace["signals"].add("leading")
                trace["foreign_subcategories"].update(leading_owners)
            if strong_lock:
                trace["signals"].add("independent_strong_pair")
                trace["foreign_subcategories"].update(strong_owners)

        counters["자기패턴일치행수"] += int(own_match)
        counters["타분류충돌행수"] += int(other_match)
        counters["구체충돌행수"] += int(hard_other_match)
        counters["일반단일충돌약화행수"] += int(normal_row and other_match)
        counters["소분류확정정상일치행수"] += int(override_match)
        counters["소분류확정정상충돌우선행수"] += int(
            normal_row and override_match and hard_other_match
        )
        counters["확정검토보호행수"] += int(protected_review)
        counters["소분류지배문구이상행수"] += int(dominant_anomaly)
        counters["대괄호타분류강신호잠금행수"] += int(bracket_lock)
        counters["앞부분타분류강신호잠금행수"] += int(leading_lock)
        counters["복수독립타분류강문구잠금행수"] += int(strong_lock)
        counters["컨텍스트후보잠금행수"] += int(context_lock)
        counters["정확문장자동오생성행수"] += int(exact_auto_error)
        counters["문구조합자동오생성행수"] += int(phrase_auto_error)
        counters["정상제외행수"] += int(normal_row)

    # Explicit review/context locks are deliberately invisible to similarity
    # promotion.  Exact/phrase automatic decisions have already won above.
    promotable_flags = [
        candidate and not locked
        for candidate, locked in zip(candidate_flags, candidate_lock_flags)
    ]
    similarity_count = promote_high_confidence_similar_candidates(
        orders,
        normalized_details,
        promotable_flags,
        auto_error_flags,
        error["active_exact"],
        error["review_exact"],
    )
    for index, locked in enumerate(candidate_lock_flags):
        if not locked:
            candidate_flags[index] = promotable_flags[index]

    candidates = format_classified_orders(orders.loc[candidate_flags])
    auto_errors = format_classified_orders(orders.loc[auto_error_flags])
    summary: dict[str, object] = {
        "분석대상행수": len(orders),
        "빈내역제외행수": int((~valid_rows).sum()),
        **dict(counters),
        "유사문장자동오생성행수": similarity_count,
        "유사도승격차단후보행수": sum(candidate_lock_flags),
        "정상제외행수": counters["정상제외행수"],
        "자동오생성행수": len(auto_errors),
        "검토후보행수": len(candidates),
        "후보행수": len(candidates),
        "컨텍스트정책활성": enable_context_policies,
        "통합정상규칙수": normal["active_rule_count"],
        "통합컨텍스트강규칙수": normal["context_pattern_count"],
        "통합오생성규칙수": error["active_rule_count"],
        "규칙버전": normal_document.get("rule_version"),
        "학습기준일": normal_document.get("training_cutoff"),
        "context_candidate_trace": {
            order_number: {
                "signals": sorted(values["signals"]),
                "foreign_subcategories": sorted(
                    values["foreign_subcategories"]
                ),
            }
            for order_number, values in sorted(context_candidate_trace.items())
        },
    }
    return candidates, auto_errors, summary


__all__ = ["select_candidate_orders_unified"]
