from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Iterable
import json
import re
import sqlite3

import pandas as pd

from service_order.error.privacy import mask_payload, mask_personal_data_frame


BUSINESS_ORDER = ["중부", "북부", "남부", "동부", "서부"]
BUSINESS_BY_CENTER = {
    "H071": "중부",
    "H072": "북부",
    "H073": "남부",
    "H074": "동부",
    "H075": "서부",
}
UNKNOWN_VALUE = "(미확인)"


def clean_dimension_value(value: object) -> str:
    if value is None:
        return UNKNOWN_VALUE
    try:
        if pd.isna(value):
            return UNKNOWN_VALUE
    except (TypeError, ValueError):
        pass
    cleaned = str(value).strip()
    return cleaned or UNKNOWN_VALUE


def normalize_order_number(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    if re_full_integer_float(text):
        return text[:-2]
    return text


def re_full_integer_float(value: str) -> bool:
    return len(value) > 2 and value.endswith(".0") and value[:-2].isdigit()


class DashboardStore:
    """SQLite storage for dashboard aggregates and approved error evidence.

    Full uploads and review candidates remain in the current in-memory job.
    Only administrator-approved rows and confirmed exact-pattern errors are
    retained for monitoring and filtered Excel export. Rollback is restricted
    to administrator approval batches.
    """

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS service_order_metrics (
                    order_date TEXT NOT NULL,
                    person TEXT NOT NULL,
                    business TEXT NOT NULL,
                    subcategory TEXT NOT NULL,
                    total_count INTEGER NOT NULL DEFAULT 0 CHECK (total_count >= 0),
                    error_count INTEGER NOT NULL DEFAULT 0 CHECK (error_count >= 0),
                    PRIMARY KEY (order_date, person, business, subcategory)
                );

                CREATE INDEX IF NOT EXISTS idx_metrics_business_date
                    ON service_order_metrics (business, order_date);
                CREATE INDEX IF NOT EXISTS idx_metrics_person_date
                    ON service_order_metrics (person, order_date);
                CREATE INDEX IF NOT EXISTS idx_metrics_subcategory_date
                    ON service_order_metrics (subcategory, order_date);

                CREATE TABLE IF NOT EXISTS error_approval_batches (
                    batch_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    batch_type TEXT NOT NULL DEFAULT 'manual'
                        CHECK (batch_type IN ('manual', 'auto')),
                    approved_at TEXT NOT NULL,
                    data_start TEXT NOT NULL,
                    data_end TEXT NOT NULL,
                    row_count INTEGER NOT NULL CHECK (row_count >= 0),
                    rolled_back_at TEXT
                );

                CREATE TABLE IF NOT EXISTS error_approval_details (
                    detail_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    candidate_row_id INTEGER NOT NULL,
                    order_number TEXT NOT NULL,
                    order_date TEXT NOT NULL,
                    person TEXT NOT NULL,
                    business TEXT NOT NULL,
                    subcategory TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (batch_id)
                        REFERENCES error_approval_batches (batch_id),
                    UNIQUE (batch_id, candidate_row_id)
                );

                CREATE INDEX IF NOT EXISTS idx_error_details_period
                    ON error_approval_details (order_date, business, person);
                CREATE INDEX IF NOT EXISTS idx_error_details_job
                    ON error_approval_details (job_id, candidate_row_id);
                CREATE INDEX IF NOT EXISTS idx_error_batches_active
                    ON error_approval_batches (rolled_back_at, approved_at);

                CREATE TABLE IF NOT EXISTS error_exclusions (
                    order_number TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    excluded_at TEXT NOT NULL,
                    restored_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_error_exclusions_active
                    ON error_exclusions (restored_at, excluded_at);
                """
            )
            detail_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(error_approval_details)"
                ).fetchall()
            }
            if "order_number" not in detail_columns:
                connection.execute(
                    "ALTER TABLE error_approval_details ADD COLUMN order_number TEXT"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_error_details_order_number "
                "ON error_approval_details (order_number)"
            )
            batch_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(error_approval_batches)"
                ).fetchall()
            }
            if "batch_type" not in batch_columns:
                # Rows created before automatic exact-error classification are
                # all administrator approvals and therefore manual batches.
                connection.execute(
                    "ALTER TABLE error_approval_batches "
                    "ADD COLUMN batch_type TEXT NOT NULL DEFAULT 'manual'"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_error_batches_type_active "
                "ON error_approval_batches (batch_type, rolled_back_at, approved_at)"
            )

    @staticmethod
    def _clean_dimension(series: pd.Series) -> pd.Series:
        cleaned = series.astype("string").str.strip()
        return cleaned.mask(cleaned.isna() | cleaned.eq(""), UNKNOWN_VALUE)

    @classmethod
    def _business_dimension(cls, frame: pd.DataFrame) -> pd.Series:
        business = frame["사업부"].astype("string").str.strip()
        business = business.where(business.isin(BUSINESS_ORDER))
        if "서비스처리센터" in frame.columns:
            center_business = (
                frame["서비스처리센터"]
                .astype("string")
                .str.strip()
                .str.upper()
                .replace({"H051": "H073"})
                .map(BUSINESS_BY_CENTER)
            )
            business = business.fillna(center_business)
        return cls._clean_dimension(business)

    @staticmethod
    def _business_value(row: pd.Series) -> str:
        business = clean_dimension_value(row.get("사업부"))
        if business in BUSINESS_ORDER:
            return business
        center = clean_dimension_value(row.get("서비스처리센터")).upper()
        if center == "H051":
            center = "H073"
        return BUSINESS_BY_CENTER.get(center, UNKNOWN_VALUE)

    @staticmethod
    def _reapply_active_error_counts(
        connection: sqlite3.Connection,
        start_date: str,
        end_date: str,
        reset_dates: set[str] | None = None,
    ) -> None:
        approved_rows = connection.execute(
            """
            SELECT d.order_date, d.person, d.business, d.subcategory,
                   COUNT(*) AS approved_count
            FROM error_approval_details AS d
            JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
            WHERE b.rolled_back_at IS NULL
              AND d.order_date BETWEEN ? AND ?
              AND NOT EXISTS (
                  SELECT 1 FROM error_exclusions AS x
                  WHERE x.order_number = d.order_number
                    AND x.restored_at IS NULL
              )
            GROUP BY d.order_date, d.person, d.business, d.subcategory
            """,
            (start_date, end_date),
        ).fetchall()
        for row in approved_rows:
            if reset_dates is not None and row["order_date"] not in reset_dates:
                continue
            approved_count = int(row["approved_count"])
            cursor = connection.execute(
                """
                UPDATE service_order_metrics
                SET error_count = error_count + ?
                WHERE order_date = ? AND person = ?
                  AND business = ? AND subcategory = ?
                  AND error_count + ? <= total_count
                """,
                (
                    approved_count,
                    row["order_date"],
                    row["person"],
                    row["business"],
                    row["subcategory"],
                    approved_count,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "기존 승인 오생성을 새 데이터에 다시 연결할 수 없습니다: "
                    f"{row['order_date']}/{row['person']}/"
                    f"{row['business']}/{row['subcategory']}"
                )

    def active_error_order_numbers(
        self,
        *,
        batch_type: str | None = None,
    ) -> set[str]:
        if batch_type not in {None, "manual", "auto"}:
            raise ValueError(f"지원하지 않는 batch_type입니다: {batch_type}")
        type_clause = "" if batch_type is None else "AND b.batch_type = ?"
        parameters: tuple[object, ...] = () if batch_type is None else (batch_type,)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT d.order_number
                FROM error_approval_details AS d
                JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
                WHERE b.rolled_back_at IS NULL
                  AND d.order_number IS NOT NULL AND d.order_number <> ''
                  AND NOT EXISTS (
                      SELECT 1 FROM error_exclusions AS x
                      WHERE x.order_number = d.order_number
                        AND x.restored_at IS NULL
                  )
                  {type_clause}
                """,
                parameters,
            ).fetchall()
        return {str(row["order_number"]) for row in rows}

    def excluded_error_order_numbers(self) -> set[str]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT order_number FROM error_exclusions
                WHERE restored_at IS NULL
                """
            ).fetchall()
        return {str(row["order_number"]) for row in rows}

    def exclude_error_orders(
        self,
        order_numbers: Iterable[object],
        *,
        job_id: str,
        excluded_at: str,
    ) -> dict[str, object]:
        normalized = sorted(
            {
                normalize_order_number(value)
                for value in order_numbers
                if normalize_order_number(value)
            }
        )
        if not normalized:
            raise ValueError("제외할 확정 오생성을 선택해 주세요.")
        placeholders = ",".join("?" for _ in normalized)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            details = connection.execute(
                f"""
                SELECT d.order_number, d.order_date, d.person,
                       d.business, d.subcategory
                FROM error_approval_details AS d
                JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
                WHERE b.rolled_back_at IS NULL
                  AND d.order_number IN ({placeholders})
                  AND NOT EXISTS (
                      SELECT 1 FROM error_exclusions AS x
                      WHERE x.order_number = d.order_number
                        AND x.restored_at IS NULL
                  )
                """,
                normalized,
            ).fetchall()
            found = {str(row["order_number"]) for row in details}
            missing = sorted(set(normalized) - found)
            if missing:
                raise ValueError(
                    "현재 확정 오생성이 아니거나 이미 제외된 오더입니다: "
                    f"{missing[:10]}"
                )
            deltas: defaultdict[tuple[str, str, str, str], int] = defaultdict(int)
            for row in details:
                deltas[
                    (
                        str(row["order_date"]),
                        str(row["person"]),
                        str(row["business"]),
                        str(row["subcategory"]),
                    )
                ] += 1
            for (order_date, person, business, subcategory), delta in deltas.items():
                cursor = connection.execute(
                    """
                    UPDATE service_order_metrics SET error_count = error_count - ?
                    WHERE order_date = ? AND person = ? AND business = ?
                      AND subcategory = ? AND error_count >= ?
                    """,
                    (delta, order_date, person, business, subcategory, delta),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "확정 오생성 제외 집계를 찾을 수 없습니다: "
                        f"{order_date}/{person}/{business}/{subcategory}"
                    )
            connection.executemany(
                """
                INSERT INTO error_exclusions (
                    order_number, job_id, excluded_at, restored_at
                ) VALUES (?, ?, ?, NULL)
                ON CONFLICT(order_number) DO UPDATE SET
                    job_id = excluded.job_id,
                    excluded_at = excluded.excluded_at,
                    restored_at = NULL
                """,
                [(value, job_id, excluded_at) for value in normalized],
            )
            connection.commit()
        return {
            "job_id": job_id,
            "excluded_count": len(normalized),
            "order_numbers": normalized,
            "excluded_at": excluded_at,
        }

    def restore_error_orders(
        self,
        order_numbers: Iterable[object],
        *,
        job_id: str,
        restored_at: str,
    ) -> dict[str, object]:
        normalized = sorted(
            {
                normalize_order_number(value)
                for value in order_numbers
                if normalize_order_number(value)
            }
        )
        if not normalized:
            raise ValueError("복구할 제외 오생성을 선택해 주세요.")
        placeholders = ",".join("?" for _ in normalized)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active_exclusions = connection.execute(
                f"""
                SELECT order_number FROM error_exclusions
                WHERE restored_at IS NULL
                  AND order_number IN ({placeholders})
                """,
                normalized,
            ).fetchall()
            found = {str(row["order_number"]) for row in active_exclusions}
            missing = sorted(set(normalized) - found)
            if missing:
                raise ValueError(
                    "현재 제외 상태가 아닌 오더입니다: "
                    f"{missing[:10]}"
                )
            details = connection.execute(
                f"""
                SELECT d.order_number, d.order_date, d.person,
                       d.business, d.subcategory
                FROM error_approval_details AS d
                JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
                WHERE b.rolled_back_at IS NULL
                  AND d.order_number IN ({placeholders})
                """,
                normalized,
            ).fetchall()
            deltas: defaultdict[tuple[str, str, str, str], int] = defaultdict(int)
            for row in details:
                deltas[
                    (
                        str(row["order_date"]),
                        str(row["person"]),
                        str(row["business"]),
                        str(row["subcategory"]),
                    )
                ] += 1
            for (order_date, person, business, subcategory), delta in deltas.items():
                cursor = connection.execute(
                    """
                    UPDATE service_order_metrics SET error_count = error_count + ?
                    WHERE order_date = ? AND person = ? AND business = ?
                      AND subcategory = ? AND error_count + ? <= total_count
                    """,
                    (delta, order_date, person, business, subcategory, delta),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "제외 오생성 복구 집계를 찾을 수 없습니다: "
                        f"{order_date}/{person}/{business}/{subcategory}"
                    )
            connection.execute(
                f"""
                UPDATE error_exclusions SET restored_at = ?
                WHERE restored_at IS NULL
                  AND order_number IN ({placeholders})
                """,
                [restored_at, *normalized],
            )
            connection.commit()
        return {
            "job_id": job_id,
            "restored_count": len(normalized),
            "order_numbers": normalized,
            "restored_at": restored_at,
        }

    def has_active_error_batches(self) -> bool:
        """Return whether a user-rollbackable manual approval exists."""
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM error_approval_batches
                WHERE rolled_back_at IS NULL AND batch_type = 'manual' LIMIT 1
                """
            ).fetchone()
        return row is not None

    def summarize_totals(self, preprocessed: pd.DataFrame) -> dict[str, object]:
        """Build the aggregate summary without changing SQLite state."""
        required = ["오더생성일", "생성인", "사업부", "소분류"]
        missing = sorted(set(required) - set(preprocessed.columns))
        if missing:
            raise KeyError(f"집계에 필요한 열이 없습니다: {missing}")

        selected = required + (
            ["서비스처리센터"] if "서비스처리센터" in preprocessed.columns else []
        )
        frame = preprocessed.loc[:, selected].copy()
        frame["order_date"] = pd.to_datetime(
            frame["오더생성일"], errors="coerce"
        ).dt.date
        frame = frame.loc[frame["order_date"].notna()].copy()
        if frame.empty:
            raise ValueError("집계할 수 있는 오더생성일이 없습니다.")

        frame["person"] = self._clean_dimension(frame["생성인"])
        frame["business"] = self._business_dimension(frame)
        frame["subcategory"] = self._clean_dimension(frame["소분류"])
        grouped = (
            frame.groupby(
                ["order_date", "person", "business", "subcategory"],
                dropna=False,
                observed=True,
            )
            .size()
            .reset_index(name="total_count")
        )
        return {
            "start": grouped["order_date"].min().isoformat(),
            "end": grouped["order_date"].max().isoformat(),
            "total_count": int(grouped["total_count"].sum()),
            "aggregate_rows": len(grouped),
        }

    def replace_totals(self, preprocessed: pd.DataFrame) -> dict[str, object]:
        grouped, rows, start_date, end_date, uploaded_dates = (
            self._prepare_total_rows(preprocessed)
        )

        # Each uploaded date is authoritative. Approved errors are reapplied from
        # persistent detail records so re-analysis cannot erase prior approvals.
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "DELETE FROM service_order_metrics WHERE order_date = ?",
                [(value,) for value in sorted(uploaded_dates)],
            )
            connection.executemany(
                """
                INSERT INTO service_order_metrics (
                    order_date, person, business, subcategory,
                    total_count, error_count
                ) VALUES (?, ?, ?, ?, ?, 0)
                """,
                rows,
            )
            self._reapply_active_error_counts(
                connection,
                start_date,
                end_date,
                reset_dates=uploaded_dates,
            )
            connection.commit()
        return {
            "start": start_date,
            "end": end_date,
            "total_count": int(grouped["total_count"].sum()),
            "aggregate_rows": len(rows),
        }

    def _prepare_total_rows(
        self,
        preprocessed: pd.DataFrame,
    ) -> tuple[
        pd.DataFrame,
        list[tuple[str, str, str, str, int]],
        str,
        str,
        set[str],
    ]:
        required = {"오더생성일", "생성인", "사업부", "소분류"}
        missing = sorted(required - set(preprocessed.columns))
        if missing:
            raise KeyError(f"집계에 필요한 열이 없습니다: {missing}")

        selected = list(required)
        if "서비스처리센터" in preprocessed.columns:
            selected.append("서비스처리센터")
        frame = preprocessed.loc[:, selected].copy()
        frame["order_date"] = pd.to_datetime(
            frame["오더생성일"], errors="coerce"
        ).dt.date
        frame = frame.loc[frame["order_date"].notna()].copy()
        if frame.empty:
            raise ValueError("집계할 수 있는 오더생성일이 없습니다.")

        frame["person"] = self._clean_dimension(frame["생성인"])
        frame["business"] = self._business_dimension(frame)
        frame["subcategory"] = self._clean_dimension(frame["소분류"])
        grouped = (
            frame.groupby(
                ["order_date", "person", "business", "subcategory"],
                dropna=False,
                observed=True,
            )
            .size()
            .reset_index(name="total_count")
        )
        rows = [
            (
                row.order_date.isoformat(),
                str(row.person),
                str(row.business),
                str(row.subcategory),
                int(row.total_count),
            )
            for row in grouped.itertuples(index=False)
        ]
        start_date = min(row[0] for row in rows)
        end_date = max(row[0] for row in rows)
        uploaded_dates = {row[0] for row in rows}
        return grouped, rows, start_date, end_date, uploaded_dates

    @staticmethod
    def _payload_from_row(row: pd.Series) -> dict[str, object]:
        payload: dict[str, object] = {}
        for column, value in row.items():
            try:
                if pd.isna(value):
                    payload[str(column)] = None
                    continue
            except (TypeError, ValueError):
                pass
            if isinstance(value, (pd.Timestamp, datetime, date)):
                payload[str(column)] = value.isoformat()
            elif hasattr(value, "item"):
                try:
                    payload[str(column)] = value.item()
                except (TypeError, ValueError):
                    payload[str(column)] = value
            else:
                payload[str(column)] = value
        return mask_payload(payload)

    def replace_totals_and_auto_errors(
        self,
        preprocessed: pd.DataFrame | None,
        auto_errors: pd.DataFrame,
        *,
        batch_id: str,
        job_id: str,
        source_name: str,
        approved_at: str,
        replace_totals: bool = True,
        date_start: str | None = None,
        date_end: str | None = None,
    ) -> dict[str, object]:
        """Atomically replace upload totals and exact-pattern auto errors.

        Manual approvals are never removed. Active automatic details on the
        uploaded dates are replaced, and an order already present in any active
        manual batch is not inserted a second time as an automatic error.
        """
        if not batch_id.strip() or not job_id.strip():
            raise ValueError("batch_id와 job_id가 필요합니다.")
        if not isinstance(auto_errors, pd.DataFrame):
            raise TypeError("auto_errors must be a pandas DataFrame.")

        if replace_totals:
            if not isinstance(preprocessed, pd.DataFrame):
                raise TypeError("preprocessed must be a pandas DataFrame.")
            grouped, total_rows, start_date, end_date, uploaded_dates = (
                self._prepare_total_rows(preprocessed)
            )
            detail_dimension_map: dict[
                str, tuple[str, str, str, str]
            ] = {}
            if "오더번호" in preprocessed.columns:
                for _, source_row in preprocessed.iterrows():
                    order_number = normalize_order_number(
                        source_row.get("오더번호")
                    )
                    order_date = pd.to_datetime(
                        source_row.get("오더생성일"),
                        errors="coerce",
                    )
                    if not order_number or pd.isna(order_date):
                        continue
                    detail_dimension_map[order_number] = (
                        order_date.date().isoformat(),
                        clean_dimension_value(source_row.get("생성인")),
                        self._business_value(source_row),
                        clean_dimension_value(source_row.get("소분류")),
                    )
        else:
            if not date_start or not date_end:
                raise ValueError(
                    "전체 집계를 유지할 때는 date_start와 date_end가 필요합니다."
                )
            start_date = self._coerce_aggregate_date(
                date_start, "date_start"
            ).isoformat()
            end_date = self._coerce_aggregate_date(
                date_end, "date_end"
            ).isoformat()
            if start_date > end_date:
                raise ValueError("date_start는 date_end보다 늦을 수 없습니다.")
            with self._lock, self._connection() as connection:
                stored_dates = connection.execute(
                    """
                    SELECT DISTINCT order_date FROM service_order_metrics
                    WHERE order_date BETWEEN ? AND ?
                    """,
                    (start_date, end_date),
                ).fetchall()
            uploaded_dates = {str(row["order_date"]) for row in stored_dates}
            if not uploaded_dates:
                raise ValueError("유지할 기존 대시보드 집계 기간이 없습니다.")
            grouped = None
            total_rows = []
            detail_dimension_map = {}
        required = {"오더번호", "오더생성일", "생성인", "사업부", "소분류"}
        missing = sorted(required - set(auto_errors.columns))
        if missing:
            raise KeyError(f"자동 오생성 적재에 필요한 열이 없습니다: {missing}")

        auto_rows: list[tuple[int, str, str, str, str, str, str]] = []
        seen_input_orders: set[str] = set()
        duplicate_input_count = 0
        for row_id, (_, row) in enumerate(auto_errors.reset_index(drop=True).iterrows()):
            order_number = normalize_order_number(row.get("오더번호"))
            if not order_number:
                raise ValueError(f"자동 오생성 row {row_id}의 오더번호가 없습니다.")
            if order_number in seen_input_orders:
                duplicate_input_count += 1
                continue
            seen_input_orders.add(order_number)
            order_date = self._coerce_aggregate_date(
                row.get("오더생성일"), f"auto error row {row_id} order_date"
            ).isoformat()
            if order_date not in uploaded_dates:
                raise ValueError(
                    f"자동 오생성 row {row_id}의 날짜가 업로드 집계에 없습니다: "
                    f"{order_date}"
                )
            payload_json = json.dumps(
                self._payload_from_row(row),
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            auto_rows.append(
                (
                    row_id,
                    order_number,
                    order_date,
                    clean_dimension_value(row.get("생성인")),
                    self._business_value(row),
                    clean_dimension_value(row.get("소분류")),
                    payload_json,
                )
            )

        placeholders = ",".join("?" for _ in uploaded_dates)
        sorted_dates = sorted(uploaded_dates)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")

            # Operational mappings such as sap_id.json can change after an
            # approval was stored. Reconnect active detail rows by immutable
            # order number before totals are replaced, otherwise an old
            # "(미확인)" person dimension cannot be reapplied to the new metric.
            if detail_dimension_map:
                connection.executemany(
                    """
                    UPDATE error_approval_details
                    SET order_date = ?, person = ?, business = ?,
                        subcategory = ?
                    WHERE order_number = ?
                    """,
                    [
                        (*dimensions, order_number)
                        for order_number, dimensions
                        in detail_dimension_map.items()
                    ],
                )

            affected_batches = connection.execute(
                f"""
                SELECT DISTINCT d.batch_id
                FROM error_approval_details AS d
                JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
                WHERE b.batch_type = 'auto' AND b.rolled_back_at IS NULL
                  AND d.order_date IN ({placeholders})
                """,
                sorted_dates,
            ).fetchall()
            replaced_auto_count = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) AS row_count
                    FROM error_approval_details AS d
                    JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
                    WHERE b.batch_type = 'auto' AND b.rolled_back_at IS NULL
                      AND d.order_date IN ({placeholders})
                    """,
                    sorted_dates,
                ).fetchone()["row_count"]
            )
            connection.execute(
                f"""
                DELETE FROM error_approval_details
                WHERE detail_id IN (
                    SELECT d.detail_id
                    FROM error_approval_details AS d
                    JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
                    WHERE b.batch_type = 'auto' AND b.rolled_back_at IS NULL
                      AND d.order_date IN ({placeholders})
                )
                """,
                sorted_dates,
            )
            for affected in affected_batches:
                remaining = connection.execute(
                    """
                    SELECT COUNT(*) AS row_count, MIN(order_date) AS data_start,
                           MAX(order_date) AS data_end
                    FROM error_approval_details WHERE batch_id = ?
                    """,
                    (affected["batch_id"],),
                ).fetchone()
                if int(remaining["row_count"]) == 0:
                    connection.execute(
                        "DELETE FROM error_approval_batches WHERE batch_id = ?",
                        (affected["batch_id"],),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE error_approval_batches
                        SET row_count = ?, data_start = ?, data_end = ?
                        WHERE batch_id = ?
                        """,
                        (
                            int(remaining["row_count"]),
                            remaining["data_start"],
                            remaining["data_end"],
                            affected["batch_id"],
                        ),
                    )

            active_details = connection.execute(
                """
                SELECT d.order_number, b.batch_type
                FROM error_approval_details AS d
                JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
                WHERE b.rolled_back_at IS NULL
                  AND d.order_number IS NOT NULL AND d.order_number <> ''
                """
            ).fetchall()
            active_manual_orders = {
                str(row["order_number"])
                for row in active_details
                if row["batch_type"] == "manual"
            }
            active_other_orders = {
                str(row["order_number"])
                for row in active_details
                if row["batch_type"] != "manual"
            }
            skipped_manual = sum(
                row[1] in active_manual_orders for row in auto_rows
            )
            skipped_existing_auto = sum(
                row[1] in active_other_orders and row[1] not in active_manual_orders
                for row in auto_rows
            )
            retained = [
                row
                for row in auto_rows
                if row[1] not in active_manual_orders
                and row[1] not in active_other_orders
            ]

            if replace_totals:
                connection.executemany(
                    "DELETE FROM service_order_metrics WHERE order_date = ?",
                    [(value,) for value in sorted_dates],
                )
                connection.executemany(
                    """
                    INSERT INTO service_order_metrics (
                        order_date, person, business, subcategory,
                        total_count, error_count
                    ) VALUES (?, ?, ?, ?, ?, 0)
                    """,
                    total_rows,
                )
            else:
                connection.execute(
                    f"""
                    UPDATE service_order_metrics SET error_count = 0
                    WHERE order_date IN ({placeholders})
                    """,
                    sorted_dates,
                )

            if retained:
                connection.execute(
                    """
                    INSERT INTO error_approval_batches (
                        batch_id, job_id, source_name, batch_type, approved_at,
                        data_start, data_end, row_count, rolled_back_at
                    ) VALUES (?, ?, ?, 'auto', ?, ?, ?, ?, NULL)
                    """,
                    (
                        batch_id,
                        job_id,
                        source_name,
                        approved_at,
                        min(row[2] for row in retained),
                        max(row[2] for row in retained),
                        len(retained),
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO error_approval_details (
                        batch_id, job_id, candidate_row_id, order_number,
                        order_date, person, business, subcategory, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [(batch_id, job_id, *row) for row in retained],
                )

            self._reapply_active_error_counts(
                connection,
                start_date,
                end_date,
                reset_dates=uploaded_dates,
            )
            stored = connection.execute(
                f"""
                SELECT COALESCE(SUM(total_count), 0) AS total_count,
                       COALESCE(SUM(error_count), 0) AS error_count,
                       COUNT(*) AS aggregate_rows
                FROM service_order_metrics
                WHERE order_date IN ({placeholders})
                """,
                sorted_dates,
            ).fetchone()
            connection.commit()

        return {
            "start": start_date,
            "end": end_date,
            "total_count": int(stored["total_count"]),
            "error_count": int(stored["error_count"]),
            "aggregate_rows": int(stored["aggregate_rows"]),
            "auto_error_input_count": len(auto_errors),
            "auto_error_count": len(retained),
            "auto_replaced_count": replaced_auto_count,
            "auto_skipped_manual_count": skipped_manual,
            "auto_skipped_existing_count": skipped_existing_auto,
            "auto_skipped_duplicate_input_count": duplicate_input_count,
        }

    @staticmethod
    def _coerce_aggregate_date(value: object, field: str) -> date:
        """Return a date from an ISO string or a date-like scalar."""
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value.strip())
            except ValueError as error:
                raise ValueError(f"{field} must be a valid YYYY-MM-DD date.") from error

        # Handles pandas/numpy datetime scalars without interpreting numeric
        # values as nanoseconds from the Unix epoch.
        if pd.api.types.is_number(value) or pd.isna(value):
            raise ValueError(f"{field} must be a valid YYYY-MM-DD date.")
        try:
            parsed = pd.Timestamp(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field} must be a valid YYYY-MM-DD date.") from error
        if pd.isna(parsed):
            raise ValueError(f"{field} must be a valid YYYY-MM-DD date.")
        return parsed.date()

    def replace_aggregate_period(
        self,
        aggregates: pd.DataFrame,
        *,
        date_start: object,
        date_end: object,
    ) -> dict[str, object]:
        """Atomically replace one inclusive period with pre-aggregated metrics.

        Required columns are ``metric_date``, ``person``, ``business``,
        ``subcategory``, ``total_count`` and ``error_count``. Duplicate metric
        keys are combined by summing both counts before they are stored. Rows
        outside ``date_start`` through ``date_end`` are rejected, and the SQL
        delete is restricted to that same inclusive period.
        """
        if not isinstance(aggregates, pd.DataFrame):
            raise TypeError("aggregates must be a pandas DataFrame.")

        required = {
            "metric_date",
            "person",
            "business",
            "subcategory",
            "total_count",
            "error_count",
        }
        missing = sorted(required - set(aggregates.columns))
        if missing:
            raise KeyError(f"Missing aggregate columns: {missing}")
        if aggregates.empty:
            raise ValueError("aggregates must contain at least one row.")

        start_date = self._coerce_aggregate_date(date_start, "date_start")
        end_date = self._coerce_aggregate_date(date_end, "date_end")
        if end_date < start_date:
            raise ValueError("date_end must be on or after date_start.")

        columns = [
            "metric_date",
            "person",
            "business",
            "subcategory",
            "total_count",
            "error_count",
        ]
        frame = aggregates.loc[:, columns].copy()

        parsed_dates: list[date] = []
        invalid_date_rows: list[object] = []
        for index, value in frame["metric_date"].items():
            try:
                parsed_dates.append(
                    self._coerce_aggregate_date(value, f"metric_date at row {index}")
                )
            except ValueError:
                invalid_date_rows.append(index)
        if invalid_date_rows:
            raise ValueError(
                "metric_date contains invalid dates at rows: "
                f"{invalid_date_rows[:10]}"
            )
        frame["metric_date"] = parsed_dates

        outside = ~frame["metric_date"].between(start_date, end_date)
        if outside.any():
            outside_rows = frame.index[outside].tolist()
            raise ValueError(
                "Aggregate rows outside date_start/date_end are not allowed: "
                f"{outside_rows[:10]}"
            )

        for column in ("total_count", "error_count"):
            numeric = pd.to_numeric(frame[column], errors="coerce")
            invalid = numeric.isna() | ~numeric.map(
                lambda value: float(value).is_integer()
                and abs(float(value)) != float("inf")
            )
            if invalid.any():
                raise ValueError(
                    f"{column} must contain finite integers; invalid rows: "
                    f"{frame.index[invalid].tolist()[:10]}"
                )
            negative = numeric.lt(0)
            if negative.any():
                raise ValueError(
                    f"{column} cannot be negative; invalid rows: "
                    f"{frame.index[negative].tolist()[:10]}"
                )
            frame[column] = numeric.map(int).astype(object)

        error_over_total = frame["error_count"].gt(frame["total_count"])
        if error_over_total.any():
            raise ValueError(
                "error_count cannot exceed total_count; invalid rows: "
                f"{frame.index[error_over_total].tolist()[:10]}"
            )

        frame["person"] = self._clean_dimension(frame["person"])
        frame["business"] = self._clean_dimension(frame["business"])
        frame["subcategory"] = self._clean_dimension(frame["subcategory"])

        combined: dict[tuple[date, str, str, str], list[int]] = {}
        for row in frame.itertuples(index=False):
            key = (
                row.metric_date,
                str(row.person),
                str(row.business),
                str(row.subcategory),
            )
            counts = combined.setdefault(key, [0, 0])
            counts[0] += int(row.total_count)
            counts[1] += int(row.error_count)

        sqlite_integer_max = 2**63 - 1
        rows: list[tuple[str, str, str, str, int, int]] = []
        for key, (total_count, error_count) in combined.items():
            if error_count > total_count:
                raise ValueError(
                    "Combined error_count cannot exceed combined total_count for "
                    f"aggregate key: {key}"
                )
            if total_count > sqlite_integer_max or error_count > sqlite_integer_max:
                raise OverflowError(f"Aggregate count exceeds SQLite INTEGER: {key}")
            metric_date, person, business, subcategory = key
            rows.append(
                (
                    metric_date.isoformat(),
                    person,
                    business,
                    subcategory,
                    total_count,
                    error_count,
                )
            )
        rows.sort(key=lambda row: row[:4])

        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM service_order_metrics WHERE order_date BETWEEN ? AND ?",
                (start_date.isoformat(), end_date.isoformat()),
            )
            connection.executemany(
                """
                INSERT INTO service_order_metrics (
                    order_date, person, business, subcategory,
                    total_count, error_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._reapply_active_error_counts(
                connection,
                start_date.isoformat(),
                end_date.isoformat(),
            )
            stored_summary = connection.execute(
                """
                SELECT COALESCE(SUM(total_count), 0) AS total_count,
                       COALESCE(SUM(error_count), 0) AS error_count
                FROM service_order_metrics
                WHERE order_date BETWEEN ? AND ?
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchone()
            connection.commit()

        return {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "total_count": int(stored_summary["total_count"]),
            "error_count": int(stored_summary["error_count"]),
            "aggregate_rows": len(rows),
            "input_rows": len(frame),
        }

    def adjust_error_counts(
        self,
        deltas: dict[tuple[str, str, str, str], int],
    ) -> None:
        changes = [(*key, int(delta)) for key, delta in deltas.items() if delta]
        if not changes:
            return
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for order_date, person, business, subcategory, delta in changes:
                cursor = connection.execute(
                    """
                    UPDATE service_order_metrics
                    SET error_count = MAX(0, MIN(total_count, error_count + ?))
                    WHERE order_date = ? AND person = ?
                      AND business = ? AND subcategory = ?
                    """,
                    (delta, order_date, person, business, subcategory),
                )
                if cursor.rowcount != 1:
                    raise KeyError(
                        "체크한 후보의 집계 기준을 찾을 수 없습니다: "
                        f"{order_date}/{person}/{business}/{subcategory}"
                    )
            connection.commit()

    def approve_error_batch(
        self,
        *,
        batch_id: str,
        job_id: str,
        source_name: str,
        approved_at: str,
        records: list[dict[str, object]],
    ) -> dict[str, object]:
        """Persist one approved candidate batch and update metrics atomically."""
        if not batch_id.strip() or not job_id.strip():
            raise ValueError("batch_id와 job_id가 필요합니다.")
        if not records:
            raise ValueError("승인할 후보가 없습니다.")

        normalized: list[
            tuple[int, str, str, str, str, str, str]
        ] = []
        seen_row_ids: set[int] = set()
        seen_order_numbers: set[str] = set()
        deltas: dict[tuple[str, str, str, str], int] = defaultdict(int)
        for record in records:
            row_id = int(record["candidate_row_id"])
            if row_id < 0 or row_id in seen_row_ids:
                raise ValueError(f"중복되거나 잘못된 후보 row_id입니다: {row_id}")
            seen_row_ids.add(row_id)
            order_number = normalize_order_number(record.get("order_number"))
            if not order_number:
                raise ValueError(f"candidate row {row_id}의 오더번호가 없습니다.")
            if order_number in seen_order_numbers:
                raise ValueError(f"같은 오더번호가 중복 선택되었습니다: {order_number}")
            seen_order_numbers.add(order_number)
            order_date = self._coerce_aggregate_date(
                record.get("order_date"), f"candidate row {row_id} order_date"
            ).isoformat()
            person = clean_dimension_value(record.get("person"))
            business = clean_dimension_value(record.get("business"))
            subcategory = clean_dimension_value(record.get("subcategory"))
            payload = record.get("payload")
            if not isinstance(payload, dict):
                raise ValueError(f"candidate row {row_id}의 상세 데이터가 없습니다.")
            payload_json = json.dumps(
                mask_payload(payload),
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            normalized.append(
                (
                    row_id,
                    order_number,
                    order_date,
                    person,
                    business,
                    subcategory,
                    payload_json,
                )
            )
            deltas[(order_date, person, business, subcategory)] += 1

        data_start = min(row[2] for row in normalized)
        data_end = max(row[2] for row in normalized)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active_rows = connection.execute(
                """
                SELECT d.job_id, d.candidate_row_id, d.order_number
                FROM error_approval_details AS d
                JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
                WHERE b.rolled_back_at IS NULL
                """
            ).fetchall()
            already_approved = seen_row_ids.intersection(
                int(row["candidate_row_id"])
                for row in active_rows
                if row["job_id"] == job_id
            )
            duplicate_order_numbers = seen_order_numbers.intersection(
                str(row["order_number"])
                for row in active_rows
                if row["order_number"]
            )
            if already_approved or duplicate_order_numbers:
                raise ValueError(
                    "이미 승인된 오더가 포함되어 있습니다: "
                    f"{sorted(duplicate_order_numbers)[:10] or sorted(already_approved)[:10]}"
                )

            for key, delta in deltas.items():
                order_date, person, business, subcategory = key
                cursor = connection.execute(
                    """
                    UPDATE service_order_metrics
                    SET error_count = error_count + ?
                    WHERE order_date = ? AND person = ?
                      AND business = ? AND subcategory = ?
                      AND error_count + ? <= total_count
                    """,
                    (
                        delta,
                        order_date,
                        person,
                        business,
                        subcategory,
                        delta,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "승인할 후보의 집계 기준이 없거나 전체 건수를 초과합니다: "
                        f"{order_date}/{person}/{business}/{subcategory}"
                    )

            connection.execute(
                """
                INSERT INTO error_approval_batches (
                    batch_id, job_id, source_name, batch_type, approved_at,
                    data_start, data_end, row_count, rolled_back_at
                ) VALUES (?, ?, ?, 'manual', ?, ?, ?, ?, NULL)
                """,
                (
                    batch_id,
                    job_id,
                    source_name,
                    approved_at,
                    data_start,
                    data_end,
                    len(normalized),
                ),
            )
            connection.executemany(
                """
                INSERT INTO error_approval_details (
                    batch_id, job_id, candidate_row_id, order_number, order_date,
                    person, business, subcategory, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (batch_id, job_id, *row)
                    for row in normalized
                ],
            )
            connection.commit()

        return {
            "batch_id": batch_id,
            "job_id": job_id,
            "approved_count": len(normalized),
            "approved_at": approved_at,
            "data_start": data_start,
            "data_end": data_end,
            "row_ids": sorted(seen_row_ids),
            "order_numbers": sorted(seen_order_numbers),
        }

    def rollback_latest_error_batch(
        self,
        *,
        job_id: str | None = None,
        rolled_back_at: str,
    ) -> dict[str, object]:
        """Roll back the most recent active manual approval batch."""
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if job_id:
                batch = connection.execute(
                    """
                    SELECT * FROM error_approval_batches
                    WHERE rolled_back_at IS NULL AND batch_type = 'manual'
                      AND job_id = ?
                    ORDER BY approved_at DESC, rowid DESC LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
            else:
                batch = connection.execute(
                    """
                    SELECT * FROM error_approval_batches
                    WHERE rolled_back_at IS NULL AND batch_type = 'manual'
                    ORDER BY approved_at DESC, rowid DESC LIMIT 1
                    """
                ).fetchone()
            if batch is None:
                raise LookupError("롤백할 승인 내역이 없습니다.")

            details = connection.execute(
                """
                SELECT candidate_row_id, order_number, order_date,
                       person, business, subcategory,
                       EXISTS (
                           SELECT 1 FROM error_exclusions AS x
                           WHERE x.order_number = error_approval_details.order_number
                             AND x.restored_at IS NULL
                       ) AS is_excluded
                FROM error_approval_details WHERE batch_id = ?
                ORDER BY candidate_row_id
                """,
                (batch["batch_id"],),
            ).fetchall()
            deltas: dict[tuple[str, str, str, str], int] = defaultdict(int)
            for detail in details:
                if bool(detail["is_excluded"]):
                    continue
                key = (
                    detail["order_date"],
                    detail["person"],
                    detail["business"],
                    detail["subcategory"],
                )
                deltas[key] += 1
            for key, delta in deltas.items():
                order_date, person, business, subcategory = key
                cursor = connection.execute(
                    """
                    UPDATE service_order_metrics
                    SET error_count = error_count - ?
                    WHERE order_date = ? AND person = ?
                      AND business = ? AND subcategory = ?
                      AND error_count >= ?
                    """,
                    (
                        delta,
                        order_date,
                        person,
                        business,
                        subcategory,
                        delta,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "롤백할 집계 기준을 찾을 수 없거나 확정 건수가 부족합니다: "
                        f"{order_date}/{person}/{business}/{subcategory}"
                    )
            connection.execute(
                "DELETE FROM error_approval_details WHERE batch_id = ?",
                (batch["batch_id"],),
            )
            cursor = connection.execute(
                """
                UPDATE error_approval_batches SET rolled_back_at = ?
                WHERE batch_id = ? AND rolled_back_at IS NULL
                """,
                (rolled_back_at, batch["batch_id"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("승인 롤백 상태를 저장하지 못했습니다.")
            connection.commit()

        return {
            "batch_id": batch["batch_id"],
            "job_id": batch["job_id"],
            "rolled_back_count": len(details),
            "rolled_back_at": rolled_back_at,
            "row_ids": [int(row["candidate_row_id"]) for row in details],
            "order_numbers": [
                str(row["order_number"])
                for row in details
                if row["order_number"]
            ],
            "data_start": batch["data_start"],
            "data_end": batch["data_end"],
        }

    @staticmethod
    def _active_error_where(
        start_date: date,
        end_date: date,
        person: str | None,
        business: str | None,
    ) -> tuple[str, list[object]]:
        clauses = [
            "d.order_date BETWEEN ? AND ?",
            "b.rolled_back_at IS NULL",
            """NOT EXISTS (
                SELECT 1 FROM error_exclusions AS x
                WHERE x.order_number = d.order_number
                  AND x.restored_at IS NULL
            )""",
        ]
        parameters: list[object] = [start_date.isoformat(), end_date.isoformat()]
        if person:
            clauses.append("d.person = ?")
            parameters.append(person)
        if business:
            clauses.append("d.business = ?")
            parameters.append(business)
        return " AND ".join(clauses), parameters

    def new_error_summary(
        self,
        *,
        start_date: date,
        end_date: date,
        person: str | None,
        business: str | None,
    ) -> dict[str, object]:
        where, parameters = self._active_error_where(
            start_date, end_date, person, business
        )
        with self._lock, self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS count,
                       MIN(b.approved_at) AS since_date,
                       MAX(b.approved_at) AS last_updated,
                       MAX(d.order_date) AS as_of_date
                FROM error_approval_details AS d
                JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
                WHERE {where}
                """,
                parameters,
            ).fetchone()
        return {
            "count": int(row["count"]),
            "since_date": row["since_date"],
            "last_updated": row["last_updated"],
            "as_of_date": row["as_of_date"],
        }

    def new_error_details(
        self,
        *,
        time_mode: str,
        year: int | None,
        month: int | None,
        start: str | None,
        end: str | None,
        person: str | None,
        business: str | None,
    ) -> tuple[pd.DataFrame, dict[str, object]]:
        start_date, end_date, _, _ = self._time_window(
            time_mode, year, month, start, end
        )
        where, parameters = self._active_error_where(
            start_date, end_date, person, business
        )
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT d.payload_json, d.order_date, b.approved_at
                FROM error_approval_details AS d
                JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
                WHERE {where}
                ORDER BY d.order_date, b.approved_at, d.detail_id
                """,
                parameters,
            ).fetchall()
        payloads = [json.loads(row["payload_json"]) for row in rows]
        frame = pd.DataFrame(payloads)
        # Older approvals may predate upload-time masking. Apply the same
        # protection at the read boundary so the web grid and its Excel export
        # never expose a legacy raw detail value.
        frame, _ = mask_personal_data_frame(frame, copy=False)
        approved_times = [row["approved_at"] for row in rows]
        order_dates = [row["order_date"] for row in rows]
        summary = {
            "count": len(rows),
            "since_date": min(approved_times) if approved_times else None,
            "last_updated": max(approved_times) if approved_times else None,
            "as_of_date": max(order_dates) if order_dates else None,
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
        }
        return frame, summary

    def filters(self) -> dict[str, object]:
        with self._lock, self._connection() as connection:
            bounds = connection.execute(
                """
                SELECT MIN(order_date) AS start_date,
                       MAX(order_date) AS end_date
                FROM service_order_metrics
                """
            ).fetchone()
            business_rows = connection.execute(
                "SELECT DISTINCT business FROM service_order_metrics"
            ).fetchall()
            people_rows = connection.execute(
                """
                SELECT person, business, SUM(total_count) AS total_count
                FROM service_order_metrics
                GROUP BY person, business
                ORDER BY person, business
                """
            ).fetchall()
            month_rows = connection.execute(
                """
                SELECT DISTINCT SUBSTR(order_date, 1, 7) AS year_month
                FROM service_order_metrics
                ORDER BY year_month
                """
            ).fetchall()

        found_businesses = {row["business"] for row in business_rows}
        businesses = [value for value in BUSINESS_ORDER if value in found_businesses]
        businesses.extend(sorted(found_businesses - set(businesses)))
        months = [row["year_month"] for row in month_rows]
        years = sorted({int(value[:4]) for value in months})
        return {
            "businesses": businesses,
            "people": [
                {
                    "name": row["person"],
                    "business": row["business"],
                    "total_count": int(row["total_count"]),
                }
                for row in people_rows
            ],
            "years": years,
            "months": months,
            "date_bounds": {
                "start": bounds["start_date"] if bounds else None,
                "end": bounds["end_date"] if bounds else None,
            },
        }

    def status(self) -> dict[str, object]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS aggregate_rows,
                       COALESCE(SUM(total_count), 0) AS total_count,
                       COALESCE(SUM(error_count), 0) AS error_count,
                       MIN(order_date) AS start_date,
                       MAX(order_date) AS end_date
                FROM service_order_metrics
                """
            ).fetchone()
        return {
            "aggregate_rows": int(row["aggregate_rows"]),
            "total_count": int(row["total_count"]),
            "error_count": int(row["error_count"]),
            "start": row["start_date"],
            "end": row["end_date"],
        }

    def period_status(self, start_date: str, end_date: str) -> dict[str, object]:
        start = self._coerce_aggregate_date(start_date, "start_date").isoformat()
        end = self._coerce_aggregate_date(end_date, "end_date").isoformat()
        if start > end:
            raise ValueError("start_date는 end_date보다 늦을 수 없습니다.")
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS aggregate_rows,
                       COALESCE(SUM(total_count), 0) AS total_count,
                       COALESCE(SUM(error_count), 0) AS error_count,
                       MIN(order_date) AS start_date,
                       MAX(order_date) AS end_date
                FROM service_order_metrics
                WHERE order_date BETWEEN ? AND ?
                """,
                (start, end),
            ).fetchone()
        return {
            "aggregate_rows": int(row["aggregate_rows"]),
            "total_count": int(row["total_count"]),
            "error_count": int(row["error_count"]),
            "start": row["start_date"],
            "end": row["end_date"],
        }

    @staticmethod
    def _parse_iso_date(value: str, field: str) -> date:
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field}는 YYYY-MM-DD 형식이어야 합니다.") from error

    def _time_window(
        self,
        time_mode: str,
        year: int | None,
        month: int | None,
        start: str | None,
        end: str | None,
    ) -> tuple[date, date, str, list[date]]:
        status = self.status()
        latest = (
            self._parse_iso_date(str(status["end"]), "end")
            if status["end"]
            else date.today()
        )
        if time_mode == "year":
            selected_year = year or latest.year
            start_date = date(selected_year, 1, 1)
            end_date = date(selected_year, 12, 31)
            buckets = [date(selected_year, value, 1) for value in range(1, 13)]
            return start_date, end_date, "month", buckets
        if time_mode == "month":
            selected_year = year or latest.year
            selected_month = month or latest.month
            if not 1 <= selected_month <= 12:
                raise ValueError("month는 1부터 12까지 입력해야 합니다.")
            start_date = date(selected_year, selected_month, 1)
            end_date = date(
                selected_year,
                selected_month,
                monthrange(selected_year, selected_month)[1],
            )
        elif time_mode == "range":
            if not start or not end:
                raise ValueError("기간 조회에는 start와 end가 필요합니다.")
            start_date = self._parse_iso_date(start, "start")
            end_date = self._parse_iso_date(end, "end")
            if end_date < start_date:
                raise ValueError("end는 start보다 빠를 수 없습니다.")
            if (end_date - start_date).days > 1095:
                raise ValueError("한 번에 조회할 수 있는 기간은 최대 3년입니다.")
        else:
            raise ValueError("time_mode는 year, month, range 중 하나여야 합니다.")

        buckets = [
            start_date + timedelta(days=offset)
            for offset in range((end_date - start_date).days + 1)
        ]
        return start_date, end_date, "day", buckets

    @staticmethod
    def _rate(error_count: int, total_count: int) -> float:
        return round(error_count / total_count * 100, 2) if total_count else 0.0

    @staticmethod
    def _pattern_signature(value: object) -> str:
        text = "" if value is None else str(value)
        text = text.lower()
        text = re.sub(r"\d+", " ", text)
        text = re.sub(r"[^가-힣a-z]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(float(value) for value in values)
        if len(ordered) == 1:
            return round(ordered[0], 2)
        position = (len(ordered) - 1) * min(1.0, max(0.0, percentile))
        lower_index = int(position)
        upper_index = min(lower_index + 1, len(ordered) - 1)
        fraction = position - lower_index
        value = ordered[lower_index] + (
            ordered[upper_index] - ordered[lower_index]
        ) * fraction
        return round(value, 2)

    @staticmethod
    def _where_clause(
        start_date: date,
        end_date: date,
        person: str | None,
        business: str | None,
    ) -> tuple[str, list[object]]:
        clauses = ["order_date BETWEEN ? AND ?"]
        parameters: list[object] = [start_date.isoformat(), end_date.isoformat()]
        if person:
            clauses.append("person = ?")
            parameters.append(person)
        if business:
            clauses.append("business = ?")
            parameters.append(business)
        return " AND ".join(clauses), parameters

    def overview(
        self,
        *,
        scope: str,
        person: str | None,
        business: str | None,
        time_mode: str,
        year: int | None,
        month: int | None,
        start: str | None,
        end: str | None,
        limit: int = 10,
    ) -> dict[str, object]:
        if scope not in {"person", "business"}:
            raise ValueError("scope는 person 또는 business여야 합니다.")
        limit = max(1, min(int(limit), 100))
        start_date, end_date, granularity, buckets = self._time_window(
            time_mode, year, month, start, end
        )
        where, parameters = self._where_clause(
            start_date, end_date, person, business
        )
        active_error_where, active_error_parameters = self._active_error_where(
            start_date, end_date, person, business
        )
        prior_pattern_where, prior_pattern_parameters = self._active_error_where(
            date(2000, 1, 1), start_date - timedelta(days=1), person, business
        )
        previous_start: date | None = None
        previous_end: date | None = None
        comparison_current_end = end_date
        previous_where = ""
        previous_parameters: list[object] = []
        if time_mode == "month":
            previous_end = start_date - timedelta(days=1)
            previous_start = date(previous_end.year, previous_end.month, 1)
            previous_where, previous_parameters = self._where_clause(
                previous_start, previous_end, person, business
            )
        bucket_sql = (
            "SUBSTR(order_date, 1, 7)" if granularity == "month" else "order_date"
        )
        multi_business = scope == "business" and not business
        series_sql = "business" if multi_business else "''"

        with self._lock, self._connection() as connection:
            summary_row = connection.execute(
                f"""
                SELECT COALESCE(SUM(total_count), 0) AS total_count,
                       COALESCE(SUM(error_count), 0) AS error_count,
                       MAX(order_date) AS last_data_date
                FROM service_order_metrics WHERE {where}
                """,
                parameters,
            ).fetchone()
            trend_rows = connection.execute(
                f"""
                SELECT {bucket_sql} AS bucket, {series_sql} AS series_name,
                       SUM(total_count) AS total_count,
                       SUM(error_count) AS error_count
                FROM service_order_metrics WHERE {where}
                GROUP BY bucket, series_name
                ORDER BY bucket, series_name
                """,
                parameters,
            ).fetchall()
            subcategory_rows = connection.execute(
                f"""
                SELECT subcategory AS name,
                       SUM(total_count) AS total_count,
                       SUM(error_count) AS error_count
                FROM service_order_metrics WHERE {where}
                GROUP BY subcategory
                """,
                parameters,
            ).fetchall()
            business_rows = connection.execute(
                f"""
                SELECT business AS name,
                       SUM(total_count) AS total_count,
                       SUM(error_count) AS error_count
                FROM service_order_metrics WHERE {where}
                GROUP BY business
                """,
                parameters,
            ).fetchall()
            person_rows = connection.execute(
                f"""
                SELECT person AS name, MAX(business) AS business,
                       SUM(total_count) AS total_count,
                       SUM(error_count) AS error_count
                FROM service_order_metrics WHERE {where}
                GROUP BY person
                """,
                parameters,
            ).fetchall()
            new_error_row = connection.execute(
                f"""
                SELECT COUNT(*) AS count,
                       MIN(b.approved_at) AS since_date,
                       MAX(b.approved_at) AS last_updated,
                       MAX(d.order_date) AS as_of_date
                FROM error_approval_details AS d
                JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
                WHERE {active_error_where}
                """,
                active_error_parameters,
            ).fetchone()
            pattern_rows = connection.execute(
                f"""
                SELECT b.batch_type, d.subcategory, d.order_date, d.payload_json
                FROM error_approval_details AS d
                JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
                WHERE {active_error_where}
                ORDER BY d.order_date, d.detail_id
                """,
                active_error_parameters,
            ).fetchall()
            prior_pattern_rows = connection.execute(
                f"""
                SELECT d.subcategory, d.payload_json
                FROM error_approval_details AS d
                JOIN error_approval_batches AS b ON b.batch_id = d.batch_id
                WHERE {prior_pattern_where}
                ORDER BY d.order_date, d.detail_id
                """,
                prior_pattern_parameters,
            ).fetchall()

            previous_summary_row = None
            previous_business_rows: list[sqlite3.Row] = []
            previous_subcategory_rows: list[sqlite3.Row] = []
            previous_person_rows: list[sqlite3.Row] = []
            previous_trend_rows: list[sqlite3.Row] = []
            if previous_start is not None and previous_end is not None:
                current_last_value = summary_row["last_data_date"]
                if current_last_value:
                    current_last_date = self._parse_iso_date(
                        str(current_last_value), "last_data_date"
                    )
                    comparison_current_end = min(end_date, current_last_date)
                    elapsed_days = max(
                        0, (comparison_current_end - start_date).days
                    )
                    previous_end = min(
                        previous_end,
                        previous_start + timedelta(days=elapsed_days),
                    )
                    previous_where, previous_parameters = self._where_clause(
                        previous_start, previous_end, person, business
                    )
                previous_summary_row = connection.execute(
                    f"""
                    SELECT COALESCE(SUM(total_count), 0) AS total_count,
                           COALESCE(SUM(error_count), 0) AS error_count
                    FROM service_order_metrics WHERE {previous_where}
                    """,
                    previous_parameters,
                ).fetchone()
                previous_business_rows = connection.execute(
                    f"""
                    SELECT business AS name,
                           SUM(total_count) AS total_count,
                           SUM(error_count) AS error_count
                    FROM service_order_metrics WHERE {previous_where}
                    GROUP BY business
                    """,
                    previous_parameters,
                ).fetchall()
                previous_subcategory_rows = connection.execute(
                    f"""
                    SELECT subcategory AS name,
                           SUM(total_count) AS total_count,
                           SUM(error_count) AS error_count
                    FROM service_order_metrics WHERE {previous_where}
                    GROUP BY subcategory
                    """,
                    previous_parameters,
                ).fetchall()
                previous_person_rows = connection.execute(
                    f"""
                    SELECT person AS name, MAX(business) AS business,
                           SUM(total_count) AS total_count,
                           SUM(error_count) AS error_count
                    FROM service_order_metrics WHERE {previous_where}
                    GROUP BY person
                    """,
                    previous_parameters,
                ).fetchall()
                previous_trend_rows = connection.execute(
                    f"""
                    SELECT order_date AS bucket,
                           SUM(total_count) AS total_count,
                           SUM(error_count) AS error_count
                    FROM service_order_metrics WHERE {previous_where}
                    GROUP BY order_date
                    ORDER BY order_date
                    """,
                    previous_parameters,
                ).fetchall()

        total_count = int(summary_row["total_count"])
        error_count = int(summary_row["error_count"])
        new_errors = {
            "count": int(new_error_row["count"]),
            "since_date": new_error_row["since_date"],
            "last_updated": new_error_row["last_updated"],
            "as_of_date": new_error_row["as_of_date"],
        }
        bucket_keys = [
            value.strftime("%Y-%m" if granularity == "month" else "%Y-%m-%d")
            for value in buckets
        ]
        trend_values: dict[tuple[str, str], tuple[int, int]] = {}
        for row in trend_rows:
            trend_values[(row["series_name"], row["bucket"])] = (
                int(row["total_count"]),
                int(row["error_count"]),
            )

        if multi_business:
            found_series = {str(row["series_name"]) for row in trend_rows}
            series_names = [name for name in BUSINESS_ORDER if name in found_series]
            series_names.extend(sorted(found_series - set(series_names)))
        elif scope == "person":
            series_names = [person or "전체 인원"]
        else:
            series_names = [business or "전체 사업부"]

        trend_series = []
        for series_name in series_names:
            lookup_name = series_name if multi_business else ""
            points = []
            for bucket in bucket_keys:
                point_total, point_error = trend_values.get(
                    (lookup_name, bucket), (0, 0)
                )
                points.append(
                    {
                        "date": bucket,
                        "total_count": point_total,
                        "error_count": point_error,
                        "error_rate": self._rate(point_error, point_total),
                    }
                )
            trend_series.append({"name": series_name, "points": points})

        previous_total = (
            int(previous_summary_row["total_count"])
            if previous_summary_row is not None
            else 0
        )
        previous_errors = (
            int(previous_summary_row["error_count"])
            if previous_summary_row is not None
            else 0
        )
        comparison_available = previous_total > 0

        def comparison_causes(
            current_rows: Iterable[sqlite3.Row],
            previous_rows: Iterable[sqlite3.Row],
        ) -> list[dict[str, object]]:
            if not comparison_available:
                return []
            current_map = {
                str(row["name"]): {
                    "total_count": int(row["total_count"]),
                    "error_count": int(row["error_count"]),
                    "business": (
                        str(row["business"])
                        if "business" in row.keys() and row["business"] is not None
                        else ""
                    ),
                }
                for row in current_rows
            }
            previous_map = {
                str(row["name"]): {
                    "total_count": int(row["total_count"]),
                    "error_count": int(row["error_count"]),
                    "business": (
                        str(row["business"])
                        if "business" in row.keys() and row["business"] is not None
                        else ""
                    ),
                }
                for row in previous_rows
            }
            values: list[dict[str, object]] = []
            for name in set(current_map) | set(previous_map):
                current = current_map.get(
                    name, {"total_count": 0, "error_count": 0, "business": ""}
                )
                previous = previous_map.get(
                    name, {"total_count": 0, "error_count": 0, "business": ""}
                )
                current_rate = self._rate(
                    int(current["error_count"]), int(current["total_count"])
                )
                previous_rate = self._rate(
                    int(previous["error_count"]), int(previous["total_count"])
                )
                values.append(
                    {
                        "name": name,
                        "business": current["business"] or previous["business"],
                        "current_count": int(current["error_count"]),
                        "previous_count": int(previous["error_count"]),
                        "delta_count": int(current["error_count"])
                        - int(previous["error_count"]),
                        "current_rate": current_rate,
                        "previous_rate": previous_rate,
                        "delta_rate": round(current_rate - previous_rate, 2),
                    }
                )
            values.sort(
                key=lambda item: (
                    -abs(int(item["delta_count"])),
                    -int(item["current_count"]),
                    str(item["name"]),
                )
            )
            return values[:10]

        previous_daily_rates = [
            self._rate(int(row["error_count"]), int(row["total_count"]))
            for row in previous_trend_rows
            if int(row["total_count"]) > 0
        ]
        comparison = {
            "available": comparison_available,
            "current_period": {
                "start": start_date.isoformat(),
                "end": comparison_current_end.isoformat(),
            },
            "previous_period": {
                "start": previous_start.isoformat() if previous_start else None,
                "end": previous_end.isoformat() if previous_end else None,
            },
            "summary": {
                "total_count": previous_total,
                "error_count": previous_errors,
                "error_rate": self._rate(previous_errors, previous_total),
                "delta_total": total_count - previous_total,
                "delta_count": error_count - previous_errors,
                "delta_rate": round(
                    self._rate(error_count, total_count)
                    - self._rate(previous_errors, previous_total),
                    2,
                ),
            },
            "rate_baseline": {
                "center": self._rate(previous_errors, previous_total)
                if comparison_available
                else None,
                "lower": self._percentile(previous_daily_rates, 0.2),
                "upper": self._percentile(previous_daily_rates, 0.8),
            },
            "causes": {
                "business": comparison_causes(
                    business_rows, previous_business_rows
                ),
                "subcategory": comparison_causes(
                    subcategory_rows, previous_subcategory_rows
                ),
                "person": comparison_causes(person_rows, previous_person_rows),
            },
        }

        repeated_count = 0
        new_pattern_count = 0
        repeated_patterns: dict[tuple[str, str], dict[str, object]] = {}

        def pattern_key(row: sqlite3.Row) -> tuple[str, str]:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            detail = payload.get("내역") or payload.get("내역2") or ""
            signature = self._pattern_signature(detail) or "내역 미확인"
            return str(row["subcategory"]), signature

        seen_patterns = {pattern_key(row) for row in prior_pattern_rows}
        for row in pattern_rows:
            key = pattern_key(row)
            is_repeated = str(row["batch_type"]) == "auto" or key in seen_patterns
            if is_repeated:
                repeated_count += 1
            else:
                new_pattern_count += 1
                seen_patterns.add(key)
                continue
            seen_patterns.add(key)
            item = repeated_patterns.setdefault(
                key,
                {
                    "subcategory": key[0],
                    "signature": key[1],
                    "count": 0,
                    "last_date": str(row["order_date"]),
                    "months": {},
                },
            )
            item["count"] = int(item["count"]) + 1
            month_key = str(row["order_date"])[:7]
            months = item["months"]
            if isinstance(months, dict):
                months[month_key] = int(months.get(month_key, 0)) + 1
            item["last_date"] = max(
                str(item["last_date"]), str(row["order_date"])
            )
        pattern_items: list[dict[str, object]] = []
        for item in sorted(
            repeated_patterns.values(),
            key=lambda item: (
                -int(item["count"]),
                str(item["subcategory"]),
                str(item["signature"]),
            ),
        ):
            if int(item["count"]) < 2:
                continue
            monthly_counts = item.get("months", {})
            item["months"] = [
                {"month": month, "count": int(count)}
                for month, count in sorted(
                    monthly_counts.items() if isinstance(monthly_counts, dict) else []
                )
            ]
            pattern_items.append(item)
            if len(pattern_items) >= 8:
                break
        pattern_total = repeated_count + new_pattern_count
        patterns = {
            "total_count": pattern_total,
            "repeated_count": repeated_count,
            "new_count": new_pattern_count,
            "repeated_rate": round(repeated_count / pattern_total * 100, 1)
            if pattern_total
            else 0.0,
            "new_rate": round(new_pattern_count / pattern_total * 100, 1)
            if pattern_total
            else 0.0,
            "items": pattern_items,
        }

        current_business_map = {
            str(row["name"]): {
                "total_count": int(row["total_count"]),
                "error_count": int(row["error_count"]),
            }
            for row in business_rows
        }
        previous_business_map = {
            str(row["name"]): {
                "total_count": int(row["total_count"]),
                "error_count": int(row["error_count"]),
            }
            for row in previous_business_rows
        }
        business_names = [
            name
            for name in BUSINESS_ORDER
            if name in current_business_map or name in previous_business_map
        ]
        business_names.extend(
            sorted(
                (set(current_business_map) | set(previous_business_map))
                - set(business_names)
            )
        )
        business_status_items = []
        for name in business_names:
            current = current_business_map.get(
                name, {"total_count": 0, "error_count": 0}
            )
            previous = previous_business_map.get(
                name, {"total_count": 0, "error_count": 0}
            )
            current_total = int(current["total_count"])
            current_errors = int(current["error_count"])
            previous_errors = int(previous["error_count"])
            business_status_items.append(
                {
                    "name": name,
                    "total_count": current_total,
                    "error_count": current_errors,
                    "error_rate": self._rate(current_errors, current_total),
                    "previous_error_count": (
                        previous_errors if comparison_available else None
                    ),
                    "delta_count": (
                        current_errors - previous_errors
                        if comparison_available
                        else None
                    ),
                }
            )

        def ranked(rows: Iterable[sqlite3.Row]) -> dict[str, list[dict[str, object]]]:
            values = [
                {
                    "name": row["name"],
                    "business": row["business"] if "business" in row.keys() else None,
                    "total_count": int(row["total_count"]),
                    "error_count": int(row["error_count"]),
                    "error_rate": self._rate(
                        int(row["error_count"]), int(row["total_count"])
                    ),
                }
                for row in rows
            ]
            by_count = sorted(
                (item.copy() for item in values),
                key=lambda item: (-item["error_count"], -item["error_rate"], item["name"]),
            )[:limit]
            by_rate = sorted(
                (item.copy() for item in values),
                key=lambda item: (-item["error_rate"], -item["error_count"], item["name"]),
            )[:limit]
            for ranking in (by_count, by_rate):
                for index, item in enumerate(ranking, start=1):
                    item["rank"] = index
            return {"count": by_count, "rate": by_rate}

        return {
            "filters": {
                "scope": scope,
                "person": person,
                "business": business,
                "time_mode": time_mode,
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
            },
            "summary": {
                "total_count": total_count,
                "error_count": error_count,
                "error_rate": self._rate(error_count, total_count),
                "last_data_date": summary_row["last_data_date"],
            },
            "new_errors": new_errors,
            "trend": {
                "granularity": granularity,
                "series": trend_series,
            },
            "comparison": comparison,
            "patterns": patterns,
            "business_status": {"items": business_status_items},
            "rankings": {
                "subcategory": ranked(subcategory_rows),
                "person": ranked(person_rows),
            },
        }
