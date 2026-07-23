from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch
import sys

import pandas as pd


CENTER_DIR = Path(__file__).resolve().parents[1]
if str(CENTER_DIR) not in sys.path:
    sys.path.insert(0, str(CENTER_DIR))

from service_order.error.analysis_pipeline import preprocess_orders  # noqa: E402
from service_order.historical_loader import (  # noqa: E402
    _read_total_source,
    _reduce_month,
)
from service_order.service_order_store import DashboardStore  # noqa: E402


def raw_orders() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "서비스처리센터": ["H071"] * 5,
            "오더번호": ["A", "B", "C", "D", "E"],
            "오더생성일": [pd.Timestamp("2026-07-08")] * 5,
            "오더생성자": ["CSC7101"] * 5,
            "상태": ["완료", "완료", "오더취소", "완료", "완료"],
            "소분류": ["기존제외소분류", "일반", "일반", "일반", "일반"],
            "내역": ["내용 A", "내용 B", "내용 C", "내용 D", "내용 E"],
            "생성부서": ["사업부", "서비스품질팀", "사업부", "사업부", "사업부"],
            "사업부": ["중부"] * 5,
        }
    )


class TotalDataScopeTest(TestCase):
    def test_subcategory_exception_is_kept_only_in_dashboard_denominator(self) -> None:
        total_data, dashboard_totals, summary = preprocess_orders(
            raw_orders(),
            {"CSC7101": "테스트"},
            {"기존제외소분류"},
            return_dashboard_totals=True,
        )

        self.assertEqual(total_data["오더번호"].tolist(), ["D", "E"])
        self.assertEqual(dashboard_totals["오더번호"].tolist(), ["A", "D", "E"])
        self.assertEqual(summary["제외소분류행수"], 1)
        self.assertEqual(summary["표기집계행수"], 3)
        self.assertEqual(summary["표기집계추가행수"], 1)
        self.assertEqual(summary["생성부서제외행수"], 1)
        self.assertEqual(summary["오더취소제외행수"], 1)

        with TemporaryDirectory() as directory:
            store = DashboardStore(Path(directory) / "metrics.sqlite3")
            stored = store.replace_totals_and_auto_errors(
                dashboard_totals,
                total_data.loc[total_data["오더번호"].eq("D")],
                batch_id="auto-scope",
                job_id="scope-job",
                source_name="scope.xlsx",
                approved_at=datetime.now().astimezone().isoformat(
                    timespec="microseconds"
                ),
            )
            overview = store.overview(
                scope="business",
                person=None,
                business=None,
                time_mode="year",
                year=2026,
                month=None,
                start=None,
                end=None,
            )

        self.assertEqual(stored["total_count"], 3)
        self.assertEqual(stored["error_count"], 1)
        self.assertEqual(overview["summary"]["total_count"], 3)
        self.assertEqual(overview["summary"]["error_count"], 1)
        self.assertAlmostEqual(overview["summary"]["error_rate"], 33.33, places=2)

    def test_historical_reducer_uses_the_same_scope(self) -> None:
        frame = raw_orders().rename(columns={"오더생성자": "생성인"})
        with TemporaryDirectory() as directory:
            cache_path = Path(directory) / "7월.pkl"
            frame.to_pickle(cache_path)
            aggregate, counts, mismatches = _reduce_month(
                cache_path,
                7,
                {"기존제외소분류"},
                {},
                {},
            )

        self.assertEqual(counts["total_count"], 3)
        self.assertEqual(counts["excluded_subcategory_rows"], 1)
        self.assertEqual(int(aggregate["total_count"].sum()), 3)
        self.assertEqual(mismatches, [])

    def test_historical_original_keeps_name_recorded_at_creation(self) -> None:
        original = pd.DataFrame(
            {
                "오더번호": ["A"],
                "오더생성일": ["2026-01-08"],
                "상태": ["완료"],
                "소분류": ["검침"],
                "오더생성자": ["CSC7101"],
                "생성인": ["과거담당자"],
                "생성부서": ["사업부"],
                "사업부": ["중부"],
                "고객서비스처리센터": ["H071"],
            }
        )
        with (
            patch(
                "service_order.historical_loader._detect_total_header",
                return_value=0,
            ),
            patch(
                "service_order.historical_loader.pd.read_excel",
                return_value=original,
            ),
        ):
            loaded = _read_total_source(
                Path("original.xlsx"),
                {"CSC7101": "현재담당자"},
            )

        self.assertEqual(loaded.loc[0, "생성인"], "과거담당자")


if __name__ == "__main__":
    main()
