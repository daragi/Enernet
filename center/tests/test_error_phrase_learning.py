from __future__ import annotations

from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch
import json
import sys

import pandas as pd


CENTER_DIR = Path(__file__).resolve().parents[1]
if str(CENTER_DIR) not in sys.path:
    sys.path.insert(0, str(CENTER_DIR))

from service_order.error import analysis_pipeline  # noqa: E402
from service_order.error.tools.build_error_pattern_registry import (  # noqa: E402
    build_phrase_records,
)


class ErrorPhraseLearningTest(TestCase):
    def test_repeated_non_contiguous_core_phrase_becomes_active(self) -> None:
        confirmed = Counter(
            {
                ("검침", "혹서기 현장 격월 대상 검침 수정"): 1,
                ("검침", "혹서기 방문 격월 세대 검침 확인"): 1,
                ("검침", "혹서기 안내 격월 일정 검침 변경"): 1,
            }
        )

        records = build_phrase_records(confirmed.copy(), confirmed)
        by_key = {
            (record["source_subcategory"], record["phrase"]): record
            for record in records
        }
        learned = by_key[("검침", "혹서기 격월 검침")]

        self.assertEqual(learned["confirmed_count"], 3)
        self.assertEqual(learned["normal_count"], 0)
        self.assertEqual(learned["distinct_confirmed_signatures"], 3)
        self.assertEqual(learned["learning_modes"], ["ordered_core"])
        self.assertEqual(learned["status"], "active")

    def test_core_phrase_needs_diverse_approved_sentences(self) -> None:
        confirmed = Counter(
            {
                ("검침", "혹서기 현장 격월 대상 검침 수정"): 3,
            }
        )

        records = build_phrase_records(confirmed.copy(), confirmed)
        keys = {
            (record["source_subcategory"], record["phrase"])
            for record in records
        }

        self.assertNotIn(("검침", "혹서기 격월 검침"), keys)

    def test_two_token_core_phrase_uses_lower_approved_threshold(self) -> None:
        confirmed = Counter(
            {
                ("계량기일반", "계량기 현장 사진 요청"): 2,
                ("계량기일반", "계량기 방문 사진 확인"): 1,
            }
        )

        records = build_phrase_records(confirmed.copy(), confirmed)
        by_key = {
            (record["source_subcategory"], record["phrase"]): record
            for record in records
        }
        learned = by_key[("계량기일반", "계량기 사진")]

        self.assertEqual(learned["confirmed_count"], 3)
        self.assertEqual(learned["distinct_confirmed_signatures"], 2)
        self.assertEqual(learned["learning_modes"], ["ordered_core"])
        self.assertEqual(learned["status"], "active")

    def test_confirmed_tolerant_variant_is_not_counted_as_normal(self) -> None:
        confirmed = Counter(
            {
                ("계량기일반", "계량기지침확인필요 사진촬영부탁드립니다 고객가"): 1,
                ("계량기일반", "계량기지침확인필요 사진촬영부탁드립니다 고객나"): 1,
                ("계량기일반", "계량기지침확인필요 사진촬영부탁드립니다 고객다"): 1,
                ("계량기일반", "계량기지침확인필요 계량기사진촬영부탁드립니다"): 1,
            }
        )

        records = build_phrase_records(confirmed.copy(), confirmed)
        by_key = {
            (record["source_subcategory"], record["phrase"]): record
            for record in records
        }
        learned = by_key[
            ("계량기일반", "계량기지침확인필요 사진촬영부탁드립니다")
        ]

        self.assertEqual(learned["confirmed_count"], 4)
        self.assertEqual(learned["normal_count"], 0)
        self.assertEqual(learned["status"], "active")

    def test_real_tolerant_variant_remains_normal_collision(self) -> None:
        confirmed = Counter(
            {
                ("계량기일반", "계량기지침확인필요 사진촬영부탁드립니다 고객가"): 1,
                ("계량기일반", "계량기지침확인필요 사진촬영부탁드립니다 고객나"): 1,
                ("계량기일반", "계량기지침확인필요 사진촬영부탁드립니다 고객다"): 1,
                ("계량기일반", "계량기지침확인필요 계량기사진촬영부탁드립니다"): 1,
            }
        )
        total = confirmed.copy()
        total[
            ("계량기일반", "계량기지침확인필요 고객 사진촬영부탁드립니다")
        ] += 1

        records = build_phrase_records(total, confirmed)
        by_key = {
            (record["source_subcategory"], record["phrase"]): record
            for record in records
        }
        learned = by_key[
            ("계량기일반", "계량기지침확인필요 사진촬영부탁드립니다")
        ]

        self.assertEqual(learned["confirmed_count"], 4)
        self.assertEqual(learned["normal_count"], 1)
        self.assertEqual(learned["status"], "ambiguous")

    def test_same_signature_can_have_confirmed_and_normal_occurrences(self) -> None:
        key = ("검침", "반복 오류 문구")
        confirmed = Counter({key: 3})
        total = Counter({key: 5})

        records = build_phrase_records(total, confirmed)
        by_key = {
            (record["source_subcategory"], record["phrase"]): record
            for record in records
        }
        learned = by_key[("검침", "반복 오류 문구")]

        self.assertEqual(learned["confirmed_count"], 3)
        self.assertEqual(learned["normal_count"], 2)
        self.assertEqual(learned["status"], "ambiguous")

    def test_phrase_rule_requires_support_and_zero_normal_collision(self) -> None:
        confirmed = Counter(
            {
                ("검침", "반복 오류 문구 첫번째"): 2,
                ("검침", "반복 오류 문구 두번째"): 1,
                ("체납", "반복 오류 문구 별도분류"): 3,
            }
        )
        total = confirmed.copy()
        total[("검침", "반복 오류 문구 정상사례")] += 1

        records = build_phrase_records(total, confirmed)
        by_key = {
            (record["source_subcategory"], record["phrase"]): record
            for record in records
        }

        self.assertEqual(
            by_key[("검침", "반복 오류 문구")]["status"], "ambiguous"
        )
        self.assertEqual(
            by_key[("검침", "반복 오류 문구")]["normal_count"], 1
        )
        self.assertEqual(
            by_key[("체납", "반복 오류 문구")]["status"], "active"
        )

    def test_phrase_collision_ignores_spacing_and_allows_ordered_gaps(self) -> None:
        confirmed = Counter(
            {
                ("안전점검기타", "안전점검 부적합 인입관"): 3,
            }
        )
        total = confirmed.copy()
        total[("안전점검기타", "안전 점검 일정후 부적합 처리")] += 1

        records = build_phrase_records(total, confirmed)
        by_key = {
            (record["source_subcategory"], record["phrase"]): record
            for record in records
        }

        self.assertEqual(
            by_key[("안전점검기타", "안전점검 부적합")]["status"],
            "ambiguous",
        )
        self.assertEqual(
            by_key[("안전점검기타", "안전점검 부적합")]["normal_count"],
            1,
        )

    def test_one_compound_token_cannot_satisfy_two_phrase_anchors(self) -> None:
        confirmed = Counter(
            {
                ("안전점검기타", "안전점검 요청 부적합 세대"): 3,
            }
        )
        total = confirmed.copy()
        total[("안전점검기타", "안전점검부적합일정잡기 통화")] += 1

        records = build_phrase_records(total, confirmed)
        by_key = {
            (record["source_subcategory"], record["phrase"]): record
            for record in records
        }
        learned = by_key[("안전점검기타", "안전점검 요청")]

        self.assertEqual(learned["normal_count"], 0)
        self.assertEqual(learned["status"], "active")

    def test_active_phrase_is_auto_only_in_its_subcategory(self) -> None:
        source = pd.DataFrame(
            {
                "오더번호": ["A", "B"],
                "오더생성일": [pd.Timestamp("2026-07-22")] * 2,
                "생성인": ["담당자"] * 2,
                "사업부": ["중부"] * 2,
                "소분류": ["검침", "체납"],
                "내역": [
                    "앞문장 반복 중간내용 오류 문구 뒷문장",
                    "앞문장 반복 오류 문구 뒷문장",
                ],
            }
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "error_pattern.json"
            registry.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "records": [],
                        "phrase_records": [
                            {
                                "source_subcategory": "검침",
                                "phrase": "반복 오류 문구",
                                "token_count": 3,
                                "confirmed_count": 2,
                                "normal_count": 0,
                                "status": "active",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            empty = root / "empty.json"
            empty.write_text("{}", encoding="utf-8")
            with (
                patch.object(analysis_pipeline, "ERROR_PATTERN_PATH", registry),
                patch.object(analysis_pipeline, "KEYWORD_PATH", empty),
                patch.object(analysis_pipeline, "CONFLICT_KEYWORD_PATH", empty),
                patch.object(analysis_pipeline, "PRIORITY_KEYWORD_PATH", empty),
                patch.object(
                    analysis_pipeline, "AUTO_NORMAL_PATTERN_PATH", root / "missing_auto.json"
                ),
                patch.object(
                    analysis_pipeline, "SCOPED_NORMAL_PATTERN_PATH", root / "missing_scoped.json"
                ),
            ):
                candidates, auto_errors, summary = (
                    analysis_pipeline.select_candidate_orders(source, {})
                )

        self.assertEqual(auto_errors["오더번호"].tolist(), ["A"])
        self.assertEqual(candidates["오더번호"].tolist(), ["B"])
        self.assertEqual(summary["문구조합자동오생성행수"], 1)
        self.assertEqual(summary["정확문장자동오생성행수"], 0)


if __name__ == "__main__":
    main()
