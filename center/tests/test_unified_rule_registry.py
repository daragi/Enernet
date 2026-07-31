from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import json
import sys

import pytest


CENTER_DIR = Path(__file__).resolve().parents[1]
if str(CENTER_DIR) not in sys.path:
    sys.path.insert(0, str(CENTER_DIR))

from service_order.error import unified_rule_registry  # noqa: E402
from service_order.error.tools.build_error_pattern_registry import (  # noqa: E402
    build_unified_error_document,
)


def _error_rule_map(document: dict[str, object]) -> dict[tuple[str, str], dict[str, object]]:
    return {
        (
            str(record["pattern"]["kind"]),
            str(record["pattern"]["value"]),
        ): record
        for record in document["rules"]
    }


def _auto_origin(
    pattern: str,
    *,
    status: str = "active",
    normal_support: int = 12,
) -> dict[str, object]:
    return {
        "source": "auto_normal_pattern.json",
        "status": status,
        "role": "auto_normal",
        "scope": (
            "source_subcategory_override"
            if status == "active"
            else "learning_evidence_only"
        ),
        "strength": "override" if status == "active" else "evidence_only",
        "legacy_pattern_type": "2어절문구",
        "raw_pattern": pattern,
        "evidence": {
            "normal_support": normal_support,
            "candidate_coverage": 1,
            "other_subcategory_frequency": 0,
            "distinct_normal_dates": 3,
            "distinct_normal_people": 3,
            "confirmed_error_pattern_hits": 0,
        },
    }


def _normal_rule(
    rule_id: str,
    owner: str,
    pattern: str,
    origins: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "rule_id": rule_id,
        "source_subcategory": owner,
        "pattern": {
            "kind": "contiguous",
            "value": pattern,
            "tokens": pattern.split(),
        },
        "source": sorted(
            {
                str(origin["source"])
                for origin in origins
            }
        ),
        "status": "active",
        "scope": {
            "source_subcategory": owner,
            "normal_application": "owner_only",
            "cross_subcategory": "soft_collision",
        },
        "strength": "override",
        "evidence": {"origins": origins},
        "behavior": {
            "own_normal": True,
            "override_collision": True,
            "cross_collision": "soft",
            "dominant_other_candidate": False,
        },
    }


def _normal_document() -> tuple[dict[str, object], dict[str, object]]:
    manual_origin = {
        "source": "keyword.json",
        "status": "active",
        "role": "manual_normal",
        "scope": "owner_only",
        "strength": "soft",
        "legacy_pattern_type": "2어절문구",
        "raw_pattern": "정상 업무",
        "evidence": {"manager_note": "수동 규칙은 그대로 보존"},
    }
    return (
        {
            "schema_version": 1,
            "rule_version": "cutoff-2026-07-08",
            "training_cutoff": "2026-07-08",
            "runtime_revision": 0,
            "source_policies": {
                "auto_normal_pattern.json": {
                    "overlapping_period_policy": "replace",
                }
            },
            "learning": {
                "evidence_snapshots": [
                    {
                        "start": "2026-07-01",
                        "end": "2026-07-08",
                        "patterns": [],
                    }
                ]
            },
            "migration_summary": {},
            "rules": [
                _normal_rule(
                    "normal_manual_and_auto",
                    "검침",
                    "정상 업무",
                    [deepcopy(manual_origin), _auto_origin("정상 업무")],
                ),
                _normal_rule(
                    "normal_auto_only",
                    "체납",
                    "삭제 대상",
                    [_auto_origin("삭제 대상")],
                ),
            ],
        },
        manual_origin,
    )


def _replacement_registry() -> dict[str, object]:
    return {
        "version": 2,
        "policy": {"overlapping_period_policy": "replace"},
        "evidence_snapshots": [
            {
                "start": "2026-07-09",
                "end": "2026-07-17",
                "patterns": [],
            }
        ],
        "records": [
            {
                "source_subcategory": "검침",
                "pattern_type": "2어절문구",
                "pattern": "정상 업무",
                "status": "proposed",
                "normal_support": 20,
                "candidate_coverage": 0,
                "other_subcategory_frequency": 0,
                "distinct_normal_dates": 5,
                "distinct_normal_people": 4,
                "confirmed_error_pattern_hits": 0,
            },
            {
                "source_subcategory": "계량기일반",
                "pattern_type": "2어절문구",
                "pattern": "신규 정상",
                "status": "active",
                "normal_support": 15,
                "candidate_coverage": 1,
                "other_subcategory_frequency": 0,
                "distinct_normal_dates": 4,
                "distinct_normal_people": 3,
                "confirmed_error_pattern_hits": 0,
            },
        ],
    }


def test_error_builder_maps_exact_and_phrase_statuses_to_runtime_decisions() -> None:
    document = build_unified_error_document(
        [
            {
                "source_subcategory": "검침",
                "signature": "확정 오류 문장",
                "confirmed_count": 3,
                "normal_count": 0,
                "status": "active",
            },
            {
                "source_subcategory": "체납",
                "signature": "정상과 겹치는 문장",
                "confirmed_count": 1,
                "normal_count": 2,
                "status": "ambiguous",
            },
        ],
        [
            {
                "source_subcategory": "검침",
                "phrase": "핵심 오류",
                "token_count": 2,
                "confirmed_count": 4,
                "normal_count": 0,
                "status": "active",
            },
            {
                "source_subcategory": "체납",
                "phrase": "공통 업무",
                "token_count": 2,
                "confirmed_count": 2,
                "normal_count": 1,
                "status": "ambiguous",
            },
        ],
        training_cutoff="2026-07-17",
    )

    rules = _error_rule_map(document)
    assert rules[("exact_normalized", "확정 오류 문장")]["decision"] == "auto_error"
    assert rules[("exact_normalized", "확정 오류 문장")]["strength"] == "exact"
    assert rules[("exact_normalized", "정상과 겹치는 문장")]["decision"] == "review_lock"
    assert rules[("exact_normalized", "정상과 겹치는 문장")]["strength"] == "review"
    assert rules[("ordered_distinct_tokens", "핵심 오류")]["decision"] == "auto_error"
    assert rules[("ordered_distinct_tokens", "핵심 오류")]["strength"] == "phrase"
    assert rules[("ordered_distinct_tokens", "공통 업무")]["decision"] == "audit_only"
    assert rules[("ordered_distinct_tokens", "공통 업무")]["strength"] == "audit"
    assert document["migration_summary"]["decision_counts"] == {
        "audit_only": 1,
        "auto_error": 2,
        "review_lock": 1,
    }
    assert unified_rule_registry.validate_error_rules_document(document) == {
        "rule_count": 4,
        "auto_error_count": 2,
    }


def test_auto_normal_extract_merge_preserves_manual_origins_and_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, manual_origin = _normal_document()
    original_copy = deepcopy(original)
    extracted = unified_rule_registry.extract_auto_normal_registry(original)
    assert len(extracted["records"]) == 2
    assert {
        (record["source_subcategory"], record["pattern"])
        for record in extracted["records"]
    } == {("검침", "정상 업무"), ("체납", "삭제 대상")}

    replacement = _replacement_registry()
    merged, summary = unified_rule_registry.build_merged_auto_normal_document(
        original,
        replacement,
        training_cutoff="2026-07-17",
    )
    assert original == original_copy, "순수 병합 함수가 입력 document를 변경했습니다."
    assert summary["preserved_non_auto_origin_count"] == 1
    assert summary["removed_auto_origin_count"] == 2
    assert summary["added_auto_origin_count"] == 2

    by_value = {record["pattern"]["value"]: record for record in merged["rules"]}
    assert "삭제 대상" not in by_value, "제거된 auto-only 규칙이 남았습니다."
    shared_origins = by_value["정상 업무"]["evidence"]["origins"]
    assert manual_origin in shared_origins
    assert sum(
        origin.get("role") == "manual_normal" for origin in shared_origins
    ) == 1
    assert sum(origin.get("role") == "auto_normal" for origin in shared_origins) == 1
    assert by_value["정상 업무"]["behavior"] == {
        "own_normal": True,
        "override_collision": False,
        "cross_collision": "soft",
        "dominant_other_candidate": False,
    }
    assert by_value["신규 정상"]["behavior"]["override_collision"] is True
    assert unified_rule_registry.extract_auto_normal_registry(merged) == replacement

    target = tmp_path / "normal_rules.json"
    original_bytes = (
        json.dumps(original, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    target.write_bytes(original_bytes)
    expected_hash = sha256(original_bytes).hexdigest()

    real_replace = unified_rule_registry.os.replace

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("원자 교체 직전 실패 모의")

    monkeypatch.setattr(unified_rule_registry.os, "replace", fail_replace)
    with pytest.raises(OSError, match="원자 교체 직전 실패 모의"):
        unified_rule_registry.merge_auto_normal_registry(
            replacement,
            path=target,
            expected_sha256=expected_hash,
            training_cutoff="2026-07-17",
        )
    assert target.read_bytes() == original_bytes
    assert not list(tmp_path.glob(".normal_rules_*_writing.json"))

    monkeypatch.setattr(unified_rule_registry.os, "replace", real_replace)
    write_summary = unified_rule_registry.merge_auto_normal_registry(
        replacement,
        path=target,
        expected_sha256=expected_hash,
        training_cutoff="2026-07-17",
    )
    written = json.loads(target.read_text(encoding="utf-8"))
    assert write_summary["path"] == str(target.resolve())
    assert write_summary["runtime_revision"] == 1
    assert written["document_sha256"] == write_summary["document_sha256"]
    assert unified_rule_registry.extract_auto_normal_registry(written) == replacement
    catalog = written["소분류별_규칙"]
    assert catalog["검침"]["2어절문구"]["정상"] == ["정상 업무"]
    assert catalog["검침"]["2어절문구"]["일반충돌"] == ["정상 업무"]
    assert catalog["검침"]["2어절문구"]["자동수집제안"] == ["정상 업무"]
    assert catalog["계량기일반"]["2어절문구"]["정상"] == ["신규 정상"]
    assert catalog["계량기일반"]["2어절문구"]["소분류우선"] == ["신규 정상"]
    assert not list(tmp_path.glob(".normal_rules_*_writing.json"))


def test_manual_normal_upsert_updates_executable_rule_and_catalog(tmp_path: Path) -> None:
    document, _ = _normal_document()
    target = tmp_path / "normal_rules.json"
    target.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    summary = unified_rule_registry.upsert_manual_normal_rules(
        [
            {
                "source_subcategory": "계량기일반",
                "pattern_type": "3~5어절문구",
                "pattern": "대용량 계량기 교체",
            }
        ],
        path=target,
    )
    written = json.loads(target.read_text(encoding="utf-8"))
    record = next(
        item
        for item in written["rules"]
        if item["source_subcategory"] == "계량기일반"
        and item["pattern"]["value"] == "대용량 계량기 교체"
    )

    assert summary["changed_count"] == 1
    assert record["behavior"]["own_normal"] is True
    assert record["behavior"]["override_collision"] is True
    assert record["scope"]["normal_application"] == "owner_only"
    assert written["소분류별_규칙"]["계량기일반"]["3~5어절문구"]["정상"] == [
        "대용량 계량기 교체"
    ]
    assert written["소분류별_규칙"]["계량기일반"]["3~5어절문구"]["소분류우선"] == [
        "대용량 계량기 교체"
    ]
