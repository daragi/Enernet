from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
import sys

import pandas as pd


CENTER_DIR = Path(__file__).resolve().parents[1]
if str(CENTER_DIR) not in sys.path:
    sys.path.insert(0, str(CENTER_DIR))

from service_order.service_order_store import DashboardStore  # noqa: E402


def total_frame(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "오더번호": [row[0] for row in rows],
            "오더생성일": [pd.Timestamp(row[1]) for row in rows],
            "생성인": ["담당자"] * len(rows),
            "사업부": ["중부"] * len(rows),
            "소분류": ["검침"] * len(rows),
            "내역": [row[2] for row in rows],
        }
    )


def approval_record(
    row_id: int,
    order_number: str,
    order_date: str,
    detail: str,
) -> dict[str, object]:
    return {
        "candidate_row_id": row_id,
        "order_number": order_number,
        "order_date": order_date,
        "person": "담당자",
        "business": "중부",
        "subcategory": "검침",
        "payload": {
            "오더번호": order_number,
            "오더생성일": order_date,
            "생성인": "담당자",
            "사업부": "중부",
            "소분류": "검침",
            "내역": detail,
        },
    }


class DashboardAnalysisTest(TestCase):
    def test_month_comparison_and_repeated_pattern_summary(self) -> None:
        empty_errors = total_frame([])
        approved_at = datetime.now().astimezone().isoformat(timespec="microseconds")
        with TemporaryDirectory() as directory:
            store = DashboardStore(Path(directory) / "metrics.sqlite3")
            store.replace_totals_and_auto_errors(
                total_frame([("J1", "2026-06-01", "반복 내역")]),
                empty_errors,
                batch_id="auto-june-empty",
                job_id="june-job",
                source_name="june.xlsx",
                approved_at=approved_at,
            )
            store.approve_error_batch(
                batch_id="manual-june",
                job_id="june-job",
                source_name="june.xlsx",
                approved_at=approved_at,
                records=[approval_record(0, "J1", "2026-06-01", "반복 내역")],
            )
            store.replace_totals_and_auto_errors(
                total_frame(
                    [
                        ("J2", "2026-07-01", "반복 내역"),
                        ("J3", "2026-07-02", "반복 내역"),
                        ("J4", "2026-07-02", "신규 내역"),
                    ]
                ),
                empty_errors,
                batch_id="auto-july-empty",
                job_id="july-job",
                source_name="july.xlsx",
                approved_at=approved_at,
            )
            store.approve_error_batch(
                batch_id="manual-july",
                job_id="july-job",
                source_name="july.xlsx",
                approved_at=approved_at,
                records=[
                    approval_record(0, "J2", "2026-07-01", "반복 내역"),
                    approval_record(1, "J3", "2026-07-02", "반복 내역"),
                    approval_record(2, "J4", "2026-07-02", "신규 내역"),
                ],
            )

            overview = store.overview(
                scope="business",
                person=None,
                business=None,
                time_mode="month",
                year=2026,
                month=7,
                start=None,
                end=None,
            )

        self.assertTrue(overview["comparison"]["available"])
        self.assertEqual(
            overview["comparison"]["current_period"]["end"], "2026-07-02"
        )
        self.assertEqual(
            overview["comparison"]["previous_period"]["end"], "2026-06-02"
        )
        self.assertEqual(overview["comparison"]["summary"]["delta_count"], 2)
        self.assertEqual(overview["patterns"]["repeated_count"], 2)
        self.assertEqual(overview["patterns"]["new_count"], 1)
        self.assertTrue(all(item["count"] >= 2 for item in overview["patterns"]["items"]))
        self.assertEqual(overview["patterns"]["items"][0]["signature"], "반복 내역")
        self.assertEqual(overview["patterns"]["items"][0]["count"], 2)
        self.assertEqual(
            overview["patterns"]["items"][0]["months"],
            [{"month": "2026-07", "count": 2}],
        )
        self.assertEqual(overview["business_status"]["items"][0]["error_count"], 3)


if __name__ == "__main__":
    main()
