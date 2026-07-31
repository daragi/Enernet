from __future__ import annotations

from datetime import date
from pathlib import Path
import json
import sys

import pandas as pd


CENTER_DIR = Path(__file__).resolve().parents[1]
if str(CENTER_DIR) not in sys.path:
    sys.path.insert(0, str(CENTER_DIR))

from service_order.error.unified_rule_engine import (  # noqa: E402
    ERROR_RULES_PATH,
    NORMAL_RULES_PATH,
    select_candidate_orders_unified,
)


def _keys(frame: pd.DataFrame) -> set[str]:
    return set(frame["오더번호"].astype("string").fillna("")) - {""}


def _active_exact_error() -> tuple[str, str]:
    document = json.loads(ERROR_RULES_PATH.read_text(encoding="utf-8"))
    for record in document["rules"]:
        if record.get("decision") != "auto_error":
            continue
        pattern = record.get("pattern", {})
        if pattern.get("kind") != "exact_normalized":
            continue
        return record["source_subcategory"], pattern["value"]
    raise AssertionError("active exact error rule이 없습니다.")


def test_unified_documents_are_versioned_and_self_consistent() -> None:
    normal = json.loads(NORMAL_RULES_PATH.read_text(encoding="utf-8"))
    error = json.loads(ERROR_RULES_PATH.read_text(encoding="utf-8"))

    assert normal["schema_version"] == 1
    assert error["schema_version"] == 1
    assert normal["training_cutoff"] == "2026-07-08"
    # 정상 기준은 미래 검증을 위해 고정하지만, 승인된 오생성 규칙은
    # 운영 데이터가 누적될 때마다 cutoff와 rule_version이 함께 전진한다.
    assert error["rule_version"] == f"cutoff-{error['training_cutoff']}"
    assert date.fromisoformat(error["training_cutoff"]) >= date.fromisoformat(
        normal["training_cutoff"]
    )
    assert normal["policies"]["context_candidate_lock"]["mode"] == "active"
    normal_rule_count = int(
        normal["migration_summary"]["canonical_rule_count"]
    )
    assert len(normal["rules"]) == normal_rule_count
    error_rule_count = int(
        error["migration_summary"]["canonical_rule_count"]
    )
    assert len(error["rules"]) == error_rule_count
    assert len({record["rule_id"] for record in normal["rules"]}) == normal_rule_count
    assert len({record["rule_id"] for record in error["rules"]}) == error_rule_count
    assert error["phrase_matching"]["learning_policy"] == {
        "contiguous_min_support": {"2": 3, "3": 2, "4": 2},
        "ordered_core_min_support": {"2": 3, "3": 2},
        "ordered_core_min_distinct_signatures": {"2": 2, "3": 2},
        "ordered_core_max_token_span": 7,
        "same_subcategory_only": True,
        "manual_approval_only": True,
        "zero_observed_normal_for_auto": True,
    }

    for item in normal["source_manifest"]:
        # The source files are migration provenance and are intentionally not
        # required after the consolidated registry becomes authoritative.
        assert len(item["sha256"]) == 64


def test_context_baseline_and_strict_context_adds_one_lock() -> None:
    error_owner, error_text = _active_exact_error()
    source = pd.DataFrame(
        [
            {
                "오더번호": "normal-metering",
                "오더생성일": pd.Timestamp("2026-07-16"),
                "소분류": "검침",
                "내역": "혹서기 격월검침 문의",
            },
            {
                "오더번호": "bracket-conflict",
                "오더생성일": pd.Timestamp("2026-07-16"),
                "소분류": "계량기일반",
                "내역": "[계량기 밸브 설치 및 봉인 요망 / 체납중지세대]",
            },
            {
                "오더번호": "normal-arrears",
                "오더생성일": pd.Timestamp("2026-07-16"),
                "소분류": "체납",
                "내역": "공급중지 예고장 송달 7.23 까지",
            },
            {
                "오더번호": "normal-reissue",
                "오더생성일": pd.Timestamp("2026-07-16"),
                "소분류": "청구서재발행",
                "내역": "고지서 MMS 재발행",
            },
            {
                "오더번호": "normal-safety",
                "오더생성일": pd.Timestamp("2026-07-16"),
                "소분류": "안전점검기타",
                "내역": "안전점검",
            },
            {
                "오더번호": "unknown-review",
                "오더생성일": pd.Timestamp("2026-07-16"),
                "소분류": "검침",
                "내역": "완전히 새로운 업무 문장",
            },
            {
                "오더번호": "exact-error",
                "오더생성일": pd.Timestamp("2026-07-16"),
                "소분류": error_owner,
                "내역": error_text,
            },
        ]
    )

    compatible_candidates, compatible_auto, _ = select_candidate_orders_unified(
        source,
        enable_context_policies=False,
    )
    assert "bracket-conflict" not in _keys(compatible_candidates)
    assert "exact-error" in _keys(compatible_auto)

    candidates, auto_errors, summary = select_candidate_orders_unified(
        source,
        enable_context_policies=True,
    )
    added = _keys(candidates) - _keys(compatible_candidates)
    assert added == {"bracket-conflict"}
    assert "exact-error" in _keys(auto_errors)
    assert "exact-error" not in _keys(candidates)
    assert {
        "normal-metering",
        "normal-arrears",
        "normal-reissue",
        "normal-safety",
    }.isdisjoint(_keys(candidates))
    assert summary["대괄호타분류강신호잠금행수"] == 1
    assert summary["context_candidate_trace"]["bracket-conflict"] == {
        "signals": ["bracket"],
        "foreign_subcategories": ["공급중지"],
    }
