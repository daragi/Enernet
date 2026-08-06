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
    def test_range_comparison_uses_calendar_month_and_year_windows(self) -> None:
        approved_at = datetime.now().astimezone().isoformat(timespec="microseconds")
        rows = [
            ("M1", "2026-03-22", "3월 오생성"),
            ("M2", "2026-04-26", "4월 오생성"),
            ("M3", "2026-04-22", "현재 오생성"),
            ("M4", "2026-05-26", "현재 오생성"),
            ("Y1", "2025-04-22", "전년 오생성"),
            ("Y2", "2025-05-26", "전년 오생성"),
        ]
        with TemporaryDirectory() as directory:
            store = DashboardStore(Path(directory) / "metrics.sqlite3")
            store.replace_totals_and_auto_errors(
                total_frame(rows),
                total_frame([]),
                batch_id="auto-range-empty",
                job_id="range-job",
                source_name="range.xlsx",
                approved_at=approved_at,
            )
            store.approve_error_batch(
                batch_id="manual-range",
                job_id="range-job",
                source_name="range.xlsx",
                approved_at=approved_at,
                records=[
                    approval_record(index, order, order_date, detail)
                    for index, (order, order_date, detail) in enumerate(rows)
                ],
            )
            month_comparison = store.overview(
                scope="business",
                person=None,
                business=None,
                time_mode="range",
                year=None,
                month=None,
                start="2026-04-22",
                end="2026-05-26",
                comparison_mode="previous_month",
            )
            year_comparison = store.overview(
                scope="business",
                person=None,
                business=None,
                time_mode="range",
                year=None,
                month=None,
                start="2026-04-22",
                end="2026-05-26",
                comparison_mode="previous_year",
            )

        self.assertTrue(month_comparison["comparison"]["available"])
        self.assertEqual(
            month_comparison["comparison"]["previous_period"],
            {"start": "2026-03-22", "end": "2026-04-26"},
        )
        self.assertEqual(
            year_comparison["comparison"]["previous_period"],
            {"start": "2025-04-22", "end": "2025-05-26"},
        )

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
                        ("J5", "2026-07-03", "반복 내역"),
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
                    approval_record(3, "J5", "2026-07-03", "반복 내역"),
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
            overview["comparison"]["current_period"]["end"], "2026-07-03"
        )
        self.assertEqual(
            overview["comparison"]["previous_period"]["end"], "2026-06-03"
        )
        self.assertEqual(overview["comparison"]["summary"]["delta_count"], 3)
        self.assertEqual(overview["patterns"]["repeated_count"], 3)
        self.assertEqual(overview["patterns"]["new_count"], 1)
        self.assertEqual(overview["patterns"]["items"][0]["signature"], "반복 내역")
        self.assertEqual(overview["patterns"]["items"][0]["count"], 3)
        self.assertEqual(overview["business_status"]["items"][0]["error_count"], 4)

    def test_person_rankings_do_not_merge_same_name_across_businesses(self) -> None:
        approved_at = datetime.now().astimezone().isoformat(timespec="microseconds")
        rows = [
            (f"C{index}", "2026-07-10", "중부 오생성")
            for index in range(2)
        ] + [
            (f"N{index}", "2026-07-10", "북부 오생성")
            for index in range(3)
        ]
        frame = total_frame(rows)
        frame["생성인"] = "김민"
        frame["사업부"] = ["중부"] * 2 + ["북부"] * 3
        records = []
        for index, (order_number, order_date, detail) in enumerate(rows):
            record = approval_record(index, order_number, order_date, detail)
            record["person"] = "김민"
            record["business"] = frame.iloc[index]["사업부"]
            record["payload"]["생성인"] = "김민"
            record["payload"]["사업부"] = record["business"]
            records.append(record)

        with TemporaryDirectory() as directory:
            store = DashboardStore(Path(directory) / "metrics.sqlite3")
            store.replace_totals_and_auto_errors(
                frame,
                total_frame([]),
                batch_id="auto-person-empty",
                job_id="person-job",
                source_name="person.xlsx",
                approved_at=approved_at,
            )
            store.approve_error_batch(
                batch_id="manual-person",
                job_id="person-job",
                source_name="person.xlsx",
                approved_at=approved_at,
                records=records,
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

        ranked = {
            (item["name"], item["business"]): item["error_count"]
            for item in overview["rankings"]["person"]["count"]
        }
        self.assertEqual(ranked[("김민", "중부")], 2)
        self.assertEqual(ranked[("김민", "북부")], 3)

    def test_yearly_rate_and_person_detail_analysis(self) -> None:
        approved_at = datetime.now().astimezone().isoformat(timespec="microseconds")
        frame = pd.DataFrame(
            {
                "오더번호": ["Y25", "Y26A", "Y26B", "Y26C"],
                "오더생성일": pd.to_datetime(
                    ["2025-07-10", "2026-07-10", "2026-07-11", "2026-07-11"]
                ),
                "생성인": ["김민"] * 4,
                "사업부": ["중부"] * 4,
                "소분류": ["검침", "검침", "체납", "체납"],
                "내역": ["25년 내역", "검침 오류", "체납 오류", "체납 오류"],
            }
        )
        records = []
        for index, row in frame.iterrows():
            record = approval_record(
                int(index), str(row["오더번호"]), str(row["오더생성일"].date()), str(row["내역"])
            )
            record["person"] = "김민"
            record["payload"]["생성인"] = "김민"
            record["subcategory"] = str(row["소분류"])
            record["payload"]["소분류"] = str(row["소분류"])
            records.append(record)

        with TemporaryDirectory() as directory:
            store = DashboardStore(Path(directory) / "metrics.sqlite3")
            store.replace_totals_and_auto_errors(
                frame,
                total_frame([]),
                batch_id="auto-yearly-empty",
                job_id="yearly-job",
                source_name="yearly.xlsx",
                approved_at=approved_at,
            )
            store.approve_error_batch(
                batch_id="manual-yearly",
                job_id="yearly-job",
                source_name="yearly.xlsx",
                approved_at=approved_at,
                records=records,
            )
            overview = store.overview(
                scope="person",
                person="김민",
                business="중부",
                time_mode="month",
                year=2026,
                month=7,
                start=None,
                end=None,
                comparison_mode="previous_year",
            )

        yearly = {item["date"]: item for item in overview["trend"]["yearly_rate"]}
        self.assertEqual(yearly["2025"]["total_count"], 1)
        self.assertEqual(yearly["2025"]["error_count"], 1)
        self.assertEqual(yearly["2026"]["total_count"], 3)
        self.assertEqual(yearly["2026"]["error_rate"], 100.0)
        self.assertEqual(overview["person_analysis"]["comparison"]["delta_count"], 2)
        self.assertEqual(
            overview["person_analysis"]["subcategories"][0]["name"], "체납"
        )
        self.assertEqual(overview["person_analysis"]["details"][0]["count"], 2)
        self.assertEqual(
            overview["person_analysis"]["business_benchmark"],
            {
                "available": True,
                "business": "중부",
                "business_error_count": 3,
                "business_person_count": 1,
                "person_error_count": 3,
                "rank": 1,
                "person_share": 100.0,
            },
        )


if __name__ == "__main__":
    main()
