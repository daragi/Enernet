from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import sys

import pandas as pd


CENTER_DIR = Path(__file__).resolve().parents[1]
if str(CENTER_DIR) not in sys.path:
    sys.path.insert(0, str(CENTER_DIR))

import dashboard_server as server  # noqa: E402
from service_order.error.analysis_pipeline import (  # noqa: E402
    PREPROCESSED_COLUMN_ORDER,
    format_classified_orders,
)
from service_order.service_order_store import DashboardStore  # noqa: E402


def sample_orders() -> pd.DataFrame:
    row = {column: None for column in PREPROCESSED_COLUMN_ORDER}
    row.update(
        {
            "서비스처리센터": "H072",
            "오더번호": "90000001",
            "상태": "처리완료",
            "대분류": "요금",
            "중분류": "요금",
            "소분류": "검침",
            "내역": "승인 실시간 동기화 테스트",
            "오더생성일": pd.Timestamp("2026-07-16"),
            "오더생성자": "CSC_TEST",
            "생성인": "테스트",
            "생성부서": "사업부",
            "사업부": "북부",
        }
    )
    return pd.DataFrame([row], columns=PREPROCESSED_COLUMN_ORDER)


class AdminLiveSyncTest(TestCase):
    def test_approval_and_rollback_update_admin_and_dashboard_together(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = DashboardStore(root / "metrics.sqlite3")
            manager = server.CurrentJobManager()
            (root / "results").mkdir()
            orders = sample_orders()
            candidates = format_classified_orders(orders)
            auto_errors = format_classified_orders(orders.iloc[0:0])
            store.replace_totals(orders)

            with (
                patch.object(server, "DASHBOARD_STORE", store),
                patch.object(server, "WEB_OUTPUT_ROOT", root / "results"),
                patch.object(
                    server,
                    "CURRENT_JOB_MANIFEST_PATH",
                    root / "results" / "current_job.json",
                ),
            ):
                manager.replace(
                    "a1b2c3d4e5f6",
                    "test.xlsx",
                    orders,
                    candidates,
                    auto_errors,
                    period={"start": "2026-07-16", "end": "2026-07-16"},
                    candidate_summary={"후보행수": 1},
                    preprocessed_file="preprocessed.xlsx",
                    candidate_file="candidate.xlsx",
                )

                approved = manager.approve_rows(
                    "a1b2c3d4e5f6",
                    [0],
                )
                approved_view = manager.sync_active_views("a1b2c3d4e5f6")
                approved_metrics = store.period_status(
                    "2026-07-16",
                    "2026-07-16",
                )

                self.assertEqual(approved["approved_count"], 1)
                self.assertEqual(approved_view["candidate_count"], 0)
                self.assertEqual(approved_view["confirmed_error_count"], 1)
                self.assertEqual(approved_metrics["error_count"], 1)

                rolled_back = manager.rollback_latest("a1b2c3d4e5f6")
                rolled_back_view = manager.sync_active_views(
                    "a1b2c3d4e5f6",
                    restore_order_numbers=rolled_back["order_numbers"],
                )
                rolled_back_metrics = store.period_status(
                    "2026-07-16",
                    "2026-07-16",
                )

                self.assertEqual(rolled_back["rolled_back_count"], 1)
                self.assertEqual(rolled_back_view["candidate_count"], 1)
                self.assertEqual(rolled_back_view["confirmed_error_count"], 0)
                self.assertEqual(rolled_back_metrics["error_count"], 0)

    def test_exclusion_moves_error_to_candidate_and_reapproval_restores_it(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = DashboardStore(root / "metrics.sqlite3")
            manager = server.CurrentJobManager()
            (root / "results").mkdir()
            orders = sample_orders()
            empty = format_classified_orders(orders.iloc[0:0])
            automatic = format_classified_orders(orders)
            store.replace_totals_and_auto_errors(
                orders,
                automatic,
                batch_id="auto-live-one",
                job_id="c1c2c3d4e5f6",
                source_name="test.xlsx",
                approved_at="2026-07-23T12:00:00+09:00",
            )

            with (
                patch.object(server, "DASHBOARD_STORE", store),
                patch.object(server, "WEB_OUTPUT_ROOT", root / "results"),
                patch.object(
                    server,
                    "CURRENT_JOB_MANIFEST_PATH",
                    root / "results" / "current_job.json",
                ),
            ):
                manager.replace(
                    "c1c2c3d4e5f6",
                    "test.xlsx",
                    orders,
                    empty,
                    automatic,
                    period={"start": "2026-07-16", "end": "2026-07-16"},
                    candidate_summary={"후보행수": 0},
                    preprocessed_file="preprocessed.xlsx",
                    candidate_file="candidate.xlsx",
                )
                store.exclude_error_orders(
                    ["90000001"],
                    job_id="c1c2c3d4e5f6",
                    excluded_at="2026-07-23T12:01:00+09:00",
                )
                excluded_view = manager.sync_active_views("c1c2c3d4e5f6")

                self.assertEqual(excluded_view["candidate_count"], 1)
                self.assertEqual(excluded_view["confirmed_error_count"], 0)
                self.assertEqual(len(manager._job.confirmed_errors), 0)
                self.assertEqual(
                    store.period_status("2026-07-16", "2026-07-16")[
                        "error_count"
                    ],
                    0,
                )

                restored = manager.approve_rows("c1c2c3d4e5f6", [0])
                restored_view = manager.sync_active_views("c1c2c3d4e5f6")

                self.assertEqual(restored["approved_count"], 1)
                self.assertEqual(restored["restored_count"], 1)
                self.assertEqual(restored_view["candidate_count"], 0)
                self.assertEqual(restored_view["confirmed_error_count"], 1)
                self.assertEqual(len(manager._job.confirmed_errors), 1)
                self.assertEqual(
                    store.period_status("2026-07-16", "2026-07-16")[
                        "error_count"
                    ],
                    1,
                )

    def test_reupload_same_period_replaces_totals_instead_of_accumulating(self) -> None:
        with TemporaryDirectory() as directory:
            store = DashboardStore(Path(directory) / "metrics.sqlite3")
            first = sample_orders()
            second_row = first.copy()
            second_row["오더번호"] = "90000002"
            latest = pd.concat([first, second_row], ignore_index=True)

            store.replace_totals(first)
            self.assertEqual(
                store.period_status("2026-07-16", "2026-07-16")[
                    "total_count"
                ],
                1,
            )
            store.replace_totals(latest)
            latest_status = store.period_status(
                "2026-07-16",
                "2026-07-16",
            )

            self.assertEqual(latest_status["total_count"], 2)
            self.assertEqual(latest_status["error_count"], 0)

    def test_sap_mapping_change_reconnects_existing_approval(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = DashboardStore(root / "metrics.sqlite3")
            manager = server.CurrentJobManager()
            (root / "results").mkdir()
            original = sample_orders()
            original["생성인"] = "(미확인)"
            candidates = format_classified_orders(original)
            auto_errors = format_classified_orders(original.iloc[0:0])
            store.replace_totals(original)

            with (
                patch.object(server, "DASHBOARD_STORE", store),
                patch.object(server, "WEB_OUTPUT_ROOT", root / "results"),
                patch.object(
                    server,
                    "CURRENT_JOB_MANIFEST_PATH",
                    root / "results" / "current_job.json",
                ),
            ):
                manager.replace(
                    "b1b2c3d4e5f6",
                    "test.xlsx",
                    original,
                    candidates,
                    auto_errors,
                    period={"start": "2026-07-16", "end": "2026-07-16"},
                    preprocessed_file="preprocessed.xlsx",
                    candidate_file="candidate.xlsx",
                )
                manager.approve_rows("b1b2c3d4e5f6", [0])

                remapped = original.copy()
                remapped["생성인"] = "최신담당자"
                store.replace_totals_and_auto_errors(
                    remapped,
                    auto_errors,
                    batch_id="auto-remap-test",
                    job_id="b1b2c3d4e5f6",
                    source_name="test.xlsx",
                    approved_at="2026-07-23T12:00:00+09:00",
                )

                metrics = store.period_status(
                    "2026-07-16",
                    "2026-07-16",
                )
                self.assertEqual(metrics["error_count"], 1)

                import sqlite3

                connection = sqlite3.connect(root / "metrics.sqlite3")
                try:
                    person = connection.execute(
                        """
                        SELECT person FROM error_approval_details
                        WHERE order_number = '90000001'
                        """
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(person, "최신담당자")
