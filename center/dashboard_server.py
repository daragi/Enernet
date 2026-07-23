from __future__ import annotations

from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from tempfile import gettempdir
from threading import Lock, RLock
from typing import Literal
from urllib.parse import urlencode
import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import uuid

import pandas as pd
import uvicorn
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from service_order.historical_loader import load_historical_aggregates
from service_order.service_order_store import (
    DashboardStore,
    UNKNOWN_VALUE,
    normalize_order_number,
)
from service_order.error.analysis_pipeline import (
    find_date_columns,
    format_classified_orders,
    load_configuration,
    run_analysis,
    SAP_ID_PATH,
    save_classification_workbook,
    save_formatted_excel,
    select_candidate_orders,
)
from service_order.error.tools.build_error_pattern_registry import (
    build_registry_from_paths,
)
from service_order.error.privacy import mask_personal_data_frame


BASE_DIR = Path(__file__).resolve().parent
SERVICE_ORDER_DIR = BASE_DIR / "service_order"
TEMP_STORAGE_ROOT = Path(gettempdir()) / "center_dashboard"
TEMP_UPLOAD_ROOT = TEMP_STORAGE_ROOT / "uploads"
WEB_OUTPUT_ROOT = TEMP_STORAGE_ROOT / "results"
CURRENT_JOB_MANIFEST_PATH = WEB_OUTPUT_ROOT / "current_job.json"
TEMP_EXPORT_ROOT = TEMP_STORAGE_ROOT / "exports"
DATABASE_PATH = SERVICE_ORDER_DIR / "data" / "service_order_metrics.sqlite3"
HISTORICAL_SOURCE_DIR = Path(
    os.environ.get(
        "SERVICE_ORDER_HISTORICAL_SOURCE_DIR",
        r"\\DocuONE\MyDrive\개인함\오생성\study_data",
    )
)
HISTORICAL_TRUTH_PATH = Path(
    os.environ.get(
        "SERVICE_ORDER_HISTORICAL_TRUTH_PATH",
        r"\\DocuONE\MyDrive\개인함\오생성\right_data\26년오생성모음.xlsx",
    )
)
HISTORICAL_EXCEPT_LIST_PATH = Path(
    os.environ.get(
        "SERVICE_ORDER_HISTORICAL_EXCEPT_LIST_PATH",
        str(SERVICE_ORDER_DIR / "error" / "json" / "except_list.json"),
    )
)
HISTORICAL_DATE_START = "2026-01-01"
HISTORICAL_DATE_END = "2026-06-30"
HISTORICAL_IMPORT_STATE_PATH = (
    SERVICE_ORDER_DIR / "data" / "historical_import_state.json"
)
HISTORICAL_IMPORT_VERSION = 2
MAX_UPLOAD_BYTES = 150 * 1024 * 1024
ANALYSIS_LOCK = Lock()
ColumnFilterValue = str | list[str]
ANALYSIS_PROGRESS_LOCK = RLock()
ANALYSIS_PROGRESS: dict[str, dict[str, object]] = {}
PATTERN_REGISTRY_LOCK = Lock()
DASHBOARD_STORE = DashboardStore(DATABASE_PATH)
LOGGER = logging.getLogger("center.dashboard")
HISTORICAL_STATUS_LOCK = RLock()
HISTORICAL_LOAD_STATUS: dict[str, object] = {
    "state": "pending",
    "started_at": None,
    "finished_at": None,
    "result": None,
    "message": "과거 집계를 아직 불러오지 않았습니다.",
}


def set_analysis_progress(
    analysis_id: str,
    percent: int,
    message: str,
    *,
    state: Literal["running", "complete", "error"] = "running",
) -> dict[str, object]:
    record = {
        "analysis_id": analysis_id,
        "state": state,
        "percent": max(0, min(100, int(percent))),
        "message": str(message),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    with ANALYSIS_PROGRESS_LOCK:
        ANALYSIS_PROGRESS[analysis_id] = record
        while len(ANALYSIS_PROGRESS) > 100:
            ANALYSIS_PROGRESS.pop(next(iter(ANALYSIS_PROGRESS)))
        return deepcopy(record)


def analysis_progress(analysis_id: str) -> dict[str, object] | None:
    with ANALYSIS_PROGRESS_LOCK:
        record = ANALYSIS_PROGRESS.get(analysis_id)
        return deepcopy(record) if record else None


def refresh_error_rules_registry() -> dict[str, object]:
    """Rebuild the consolidated error rules after a manual approval change."""
    return build_registry_from_paths(
        truth=HISTORICAL_TRUTH_PATH,
        database=DATABASE_PATH,
        current_manifest=CURRENT_JOB_MANIFEST_PATH,
        except_list=HISTORICAL_EXCEPT_LIST_PATH,
        historical_source_dir=HISTORICAL_SOURCE_DIR,
        output=SERVICE_ORDER_DIR / "error" / "json" / "error_rules.json",
    )


def rebuild_error_rules_background(job_id: str, reason: str) -> None:
    """Serialize slow rule learning without holding the admin operation lock."""
    with PATTERN_REGISTRY_LOCK:
        try:
            summary = refresh_error_rules_registry()
            EVENTS.publish(
                "error_rules_refreshed",
                "승인 이력을 오생성 학습 규칙에 반영했습니다.",
                {
                    "job_id": job_id,
                    "reason": reason,
                    "summary": summary,
                },
            )
        except Exception:
            LOGGER.exception(
                "Failed to refresh error rules in background: %s", reason
            )


def set_historical_status(
    state: str,
    *,
    started_at: str | None,
    finished_at: str | None = None,
    result: dict[str, object] | None = None,
    message: str,
) -> None:
    with HISTORICAL_STATUS_LOCK:
        HISTORICAL_LOAD_STATUS.clear()
        HISTORICAL_LOAD_STATUS.update(
            {
                "state": state,
                "started_at": started_at,
                "finished_at": finished_at,
                "result": deepcopy(result),
                "message": message,
            }
        )


def historical_status() -> dict[str, object]:
    with HISTORICAL_STATUS_LOCK:
        return deepcopy(HISTORICAL_LOAD_STATUS)


def historical_source_signature() -> dict[str, object]:
    paths = [
        HISTORICAL_SOURCE_DIR
        / f"26년 {month}월 전체 서비스오더 리스트.xlsx"
        for month in range(1, 7)
    ]
    paths.extend(
        [HISTORICAL_TRUTH_PATH, HISTORICAL_EXCEPT_LIST_PATH, SAP_ID_PATH]
    )
    files: list[dict[str, object]] = []
    for path in paths:
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return {"version": HISTORICAL_IMPORT_VERSION, "files": files}


def cached_historical_result() -> dict[str, object] | None:
    if not HISTORICAL_IMPORT_STATE_PATH.is_file():
        return None
    try:
        state = json.loads(
            HISTORICAL_IMPORT_STATE_PATH.read_text(encoding="utf-8")
        )
        if state.get("version") != HISTORICAL_IMPORT_VERSION:
            return None
        stored = DASHBOARD_STORE.period_status(
            HISTORICAL_DATE_START,
            HISTORICAL_DATE_END,
        )
        result = state.get("result")
        if not isinstance(result, dict) or not isinstance(
            result.get("stored"), dict
        ):
            return None
        if int(stored["total_count"]) != int(
            result["stored"].get("total_count", -1)
        ):
            return None
        try:
            current_signature = historical_source_signature()
        except OSError:
            current_signature = state.get("signature")
        if current_signature != state.get("signature"):
            return None
        cached = deepcopy(result)
        cached["cached"] = True
        return cached
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def save_historical_import_state(result: dict[str, object]) -> None:
    HISTORICAL_IMPORT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": HISTORICAL_IMPORT_VERSION,
        "signature": historical_source_signature(),
        "result": result,
    }
    temporary = HISTORICAL_IMPORT_STATE_PATH.with_name(
        f".{HISTORICAL_IMPORT_STATE_PATH.name}.{uuid.uuid4().hex[:8]}.tmp"
    )
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=json_value),
        encoding="utf-8",
    )
    os.replace(temporary, HISTORICAL_IMPORT_STATE_PATH)


def load_and_store_historical_metrics() -> dict[str, object]:
    cached = cached_historical_result()
    if cached is not None:
        return cached
    sap_id_map, _ = load_configuration()
    loaded = load_historical_aggregates(
        HISTORICAL_SOURCE_DIR,
        HISTORICAL_TRUTH_PATH,
        HISTORICAL_EXCEPT_LIST_PATH,
        months=range(1, 7),
        blank_row_limit=200,
        sap_id_map=sap_id_map,
    )
    stored = DASHBOARD_STORE.replace_aggregate_period(
        loaded.aggregates,
        date_start=HISTORICAL_DATE_START,
        date_end=HISTORICAL_DATE_END,
    )
    result = {
        "truth_file": HISTORICAL_TRUTH_PATH.name,
        "source": "monthly_original_xlsx",
        "summary": asdict(loaded.summary),
        "stored": stored,
    }
    save_historical_import_state(result)
    return result


def clear_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for item in directory.iterdir():
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)


ISO_MIDNIGHT_FILTER_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:T|\s)00:00:00(?:\.0+)?(?:Z|[+-]\d{2}:?\d{2})?$"
)


def excel_filter_text(value: object, column: str | None = None) -> str:
    """Return the same compact value users see in Excel-style filters."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if str(column or "").replace(" ", "") == "오더번호":
        integer = re.fullmatch(r"([+-]?\d+)\.0+", text)
        if integer:
            return integer.group(1)
    midnight = ISO_MIDNIGHT_FILTER_PATTERN.fullmatch(text)
    return midnight.group(1) if midnight else text


def filtered_row_ids(
    frame: pd.DataFrame,
    *,
    search: str | None,
    column_filters: dict[str, ColumnFilterValue] | None,
) -> list[int]:
    row_ids = list(range(len(frame)))
    normalized_search = (search or "").strip().casefold()
    if normalized_search:
        matches = frame.astype("string").apply(
            lambda column: column.str.casefold().str.contains(
                re.escape(normalized_search), regex=True, na=False
            )
        )
        visible_mask = matches.any(axis=1).tolist()
        row_ids = [
            row_id
            for row_id, is_visible in enumerate(visible_mask)
            if is_visible
        ]
    for column, raw_value in (column_filters or {}).items():
        if column not in frame.columns:
            raise ValueError(f"존재하지 않는 필터 열입니다: {column}")
        normalized_column = frame[column].map(
            lambda value: excel_filter_text(value, column)
        ).str.casefold()
        if isinstance(raw_value, list):
            selected_values = {
                excel_filter_text(value, column).casefold()
                for value in raw_value
                if value is not None
            }
            if not selected_values:
                continue
            column_matches = normalized_column.isin(selected_values).tolist()
        else:
            value = excel_filter_text(raw_value, column).casefold()
            if not value:
                continue
            # Keep legacy scalar filters as contains-searches. New Excel-style
            # multi-select filters are arrays and use exact value matching.
            column_matches = normalized_column.str.contains(
                re.escape(value), regex=True, na=False
            ).tolist()
        row_ids = [row_id for row_id in row_ids if column_matches[row_id]]
    return row_ids


def distinct_column_values(
    frame: pd.DataFrame,
    *,
    column: str,
    search: str | None,
    limit: int,
) -> dict[str, object]:
    if column not in frame.columns:
        raise ValueError(f"존재하지 않는 필터 열입니다: {column}")
    values = frame[column].map(lambda value: excel_filter_text(value, column))
    normalized_search = (search or "").strip().casefold()
    if normalized_search:
        values = values.loc[
            values.str.casefold().str.contains(
                re.escape(normalized_search), regex=True, na=False
            )
        ]
    unique_values = sorted(set(values.tolist()), key=lambda value: value.casefold())
    total = len(unique_values)
    return {
        "column": column,
        "values": unique_values[:limit],
        "total": total,
        "truncated": total > limit,
    }


def json_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value


def json_cell_value(column: object, value: object) -> object:
    """Serialize grid values using the same compact form shown in filters."""
    normalized = json_value(value)
    if str(column).replace(" ", "") == "오더번호":
        text = excel_filter_text(normalized, "오더번호")
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
    return normalized


def clean_dimension(value: object) -> str:
    if value is None or pd.isna(value):
        return UNKNOWN_VALUE
    cleaned = str(value).strip()
    return cleaned or UNKNOWN_VALUE


def apply_error_exclusions(
    preprocessed: pd.DataFrame,
    candidates: pd.DataFrame,
    auto_errors: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, int]:
    """Split review/confirmed rows and apply persistent error exclusions.

    Excluding a confirmed error is a move back to review, not a third visual
    state in the confirmed-error grid.  Keeping excluded rows in that grid
    made its tab count disagree with the active dashboard count and left stale,
    grey rows behind after a mutation.
    """
    excluded_orders = DASHBOARD_STORE.excluded_error_order_numbers()
    active_orders = DASHBOARD_STORE.active_error_order_numbers()

    def order_mask(frame: pd.DataFrame) -> pd.Series:
        if "오더번호" not in frame.columns or not excluded_orders:
            return pd.Series(False, index=frame.index)
        return frame["오더번호"].map(normalize_order_number).isin(excluded_orders)

    if "오더번호" in candidates.columns and active_orders:
        confirmed_candidates = candidates["오더번호"].map(
            normalize_order_number
        ).isin(active_orders)
    else:
        confirmed_candidates = pd.Series(False, index=candidates.index)
    candidate_excluded = confirmed_candidates
    auto_excluded = order_mask(auto_errors)
    active_candidates = candidates.loc[~candidate_excluded].reset_index(drop=True)
    active_auto = auto_errors.loc[~auto_excluded].reset_index(drop=True)

    active_auto_orders = (
        set(active_auto["오더번호"].map(normalize_order_number))
        if "오더번호" in active_auto.columns
        else set()
    )
    if active_orders and "오더번호" in preprocessed.columns:
        persisted_mask = preprocessed["오더번호"].map(
            normalize_order_number
        ).isin(active_orders - active_auto_orders)
        persisted_confirmed = format_classified_orders(
            preprocessed.loc[persisted_mask]
        )
    else:
        persisted_confirmed = format_classified_orders(preprocessed.iloc[0:0])
    active_confirmed = pd.concat(
        [active_auto, persisted_confirmed],
        ignore_index=True,
    )

    if excluded_orders and "오더번호" in preprocessed.columns:
        preprocessed_excluded = preprocessed["오더번호"].map(
            normalize_order_number
        ).isin(excluded_orders)
        excluded_frame = format_classified_orders(
            preprocessed.loc[preprocessed_excluded]
        )
    else:
        excluded_frame = format_classified_orders(preprocessed.iloc[0:0])

    # An excluded automatic error was not part of the original candidate
    # frame.  Append it so that exclude/rollback has the same visible
    # semantics for both automatic and manually approved errors.
    if not excluded_frame.empty:
        excluded_candidates = excluded_frame.reindex(
            columns=active_candidates.columns,
            fill_value=None,
        )
        active_candidates = pd.concat(
            [active_candidates, excluded_candidates],
            ignore_index=True,
        )
        if "오더번호" in active_candidates.columns:
            active_candidates = active_candidates.assign(
                __order_key=active_candidates["오더번호"].map(
                    normalize_order_number
                )
            ).drop_duplicates("__order_key", keep="first").drop(
                columns="__order_key"
            )
        active_candidates = active_candidates.reset_index(drop=True)

    active_view = active_confirmed.copy()
    active_view.insert(0, "집계상태", "확정")
    confirmed_view = active_view.reset_index(drop=True)
    if not confirmed_view.empty:
        confirmed_view = confirmed_view.sort_values(
            ["집계상태", "오더생성일", "소분류", "오더번호"],
            ascending=[False, True, True, True],
            na_position="last",
        ).reset_index(drop=True)
    return (
        active_candidates,
        active_confirmed,
        confirmed_view,
        len(excluded_frame),
    )


def frame_order_numbers(frame: pd.DataFrame) -> set[str]:
    if "오더번호" not in frame.columns:
        return set()
    return {
        normalize_order_number(value)
        for value in frame["오더번호"]
        if normalize_order_number(value)
    }


def retain_review_scope(
    candidates: pd.DataFrame,
    reviewable_order_numbers: set[str],
) -> pd.DataFrame:
    """Prevent display masking from promoting prior normal rows to review."""
    if "오더번호" not in candidates.columns:
        return candidates.iloc[0:0].copy()
    mask = candidates["오더번호"].map(normalize_order_number).isin(
        reviewable_order_numbers
    )
    return candidates.loc[mask].reset_index(drop=True)


@dataclass
class CurrentJob:
    job_id: str
    source_name: str
    preprocessed: pd.DataFrame
    candidates: pd.DataFrame
    auto_errors: pd.DataFrame
    confirmed_errors: pd.DataFrame
    reviewable_order_numbers: set[str]
    candidate_checked: list[bool]
    created_at: str
    period: dict[str, str | None]
    preprocess_summary: dict[str, object]
    candidate_summary: dict[str, object]
    aggregate_summary: dict[str, object]
    preprocessed_file: str
    candidate_file: str


class CurrentJobManager:
    """Keeps the latest row-level job in memory and restores its XLSX snapshot."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._job: CurrentJob | None = None

    def clear(self) -> None:
        with self._lock:
            self._job = None

    def replace(
        self,
        job_id: str,
        source_name: str,
        preprocessed: pd.DataFrame,
        candidates: pd.DataFrame,
        auto_errors: pd.DataFrame | None = None,
        *,
        created_at: str | None = None,
        period: dict[str, str | None] | None = None,
        preprocess_summary: dict[str, object] | None = None,
        candidate_summary: dict[str, object] | None = None,
        aggregate_summary: dict[str, object] | None = None,
        preprocessed_file: str = "",
        candidate_file: str = "",
    ) -> CurrentJob:
        prepared = self.prepare(
            job_id,
            source_name,
            preprocessed,
            candidates,
            auto_errors,
            created_at=created_at,
            period=period,
            preprocess_summary=preprocess_summary,
            candidate_summary=candidate_summary,
            aggregate_summary=aggregate_summary,
            preprocessed_file=preprocessed_file,
            candidate_file=candidate_file,
        )
        return self.install(prepared)

    def prepare(
        self,
        job_id: str,
        source_name: str,
        preprocessed: pd.DataFrame,
        candidates: pd.DataFrame,
        auto_errors: pd.DataFrame | None = None,
        *,
        created_at: str | None = None,
        period: dict[str, str | None] | None = None,
        preprocess_summary: dict[str, object] | None = None,
        candidate_summary: dict[str, object] | None = None,
        aggregate_summary: dict[str, object] | None = None,
        preprocessed_file: str = "",
        candidate_file: str = "",
    ) -> CurrentJob:
        """Prepare all fallible current-job state before external commits."""
        with self._lock:
            safe_preprocessed, _ = mask_personal_data_frame(preprocessed)
            safe_candidates, _ = mask_personal_data_frame(candidates)
            raw_auto_errors = (
                mask_personal_data_frame(auto_errors)[0]
                if isinstance(auto_errors, pd.DataFrame)
                else format_classified_orders(safe_preprocessed.iloc[0:0])
            )
            reviewable_order_numbers = (
                frame_order_numbers(safe_candidates)
                | frame_order_numbers(raw_auto_errors)
            )
            (
                active_candidates,
                active_auto_errors,
                confirmed_errors,
                excluded_error_count,
            ) = apply_error_exclusions(
                safe_preprocessed,
                safe_candidates,
                raw_auto_errors,
            )
            effective_candidate_summary = deepcopy(candidate_summary or {})
            effective_candidate_summary.update(
                {
                    "검토후보행수": len(active_candidates),
                    "후보행수": len(active_candidates),
                    "자동오생성행수": len(active_auto_errors),
                    "관리자제외오생성행수": excluded_error_count,
                }
            )
            # Automatic exact matches never appear in the review grid. Only
            # manual approvals can make a review row start as checked.
            active_order_numbers = DASHBOARD_STORE.active_error_order_numbers(
                batch_type="manual"
            )
            checked = [False] * len(active_candidates)
            if "오더번호" in active_candidates.columns and active_order_numbers:
                checked = [
                    normalize_order_number(value) in active_order_numbers
                    for value in active_candidates["오더번호"]
                ]
            return CurrentJob(
                job_id=job_id,
                source_name=source_name,
                preprocessed=safe_preprocessed.reset_index(drop=True),
                candidates=active_candidates,
                auto_errors=active_auto_errors,
                confirmed_errors=confirmed_errors,
                reviewable_order_numbers=reviewable_order_numbers,
                candidate_checked=checked,
                created_at=created_at
                or datetime.now().astimezone().isoformat(timespec="seconds"),
                period=deepcopy(period or {"start": None, "end": None}),
                preprocess_summary=deepcopy(preprocess_summary or {}),
                candidate_summary=effective_candidate_summary,
                aggregate_summary=deepcopy(aggregate_summary or {}),
                preprocessed_file=preprocessed_file,
                candidate_file=candidate_file,
            )

    def install(self, prepared: CurrentJob) -> CurrentJob:
        """Publish an already prepared job with only an in-memory assignment."""
        with self._lock:
            self._job = prepared
            return self._job

    def status(self) -> dict[str, object] | None:
        with self._lock:
            if self._job is None:
                return None
            status = {
                "job_id": self._job.job_id,
                "source_name": self._job.source_name,
                "created_at": self._job.created_at,
                "period": deepcopy(self._job.period),
                "preprocessed_count": len(self._job.preprocessed),
                "candidate_count": len(self._job.candidates),
                "confirmed_error_count": int(
                    self._job.confirmed_errors["집계상태"].eq("확정").sum()
                ),
                "excluded_error_count": int(
                    self._job.candidate_summary.get(
                        "관리자제외오생성행수",
                        0,
                    )
                    or 0
                ),
                "checked_count": sum(self._job.candidate_checked),
                "can_rollback": DASHBOARD_STORE.has_active_error_batches(),
                "preprocess": deepcopy(self._job.preprocess_summary),
                "candidate": deepcopy(self._job.candidate_summary),
                "aggregate": deepcopy(self._job.aggregate_summary),
                "grid": {
                    "candidate": (
                        f"/api/admin/grid?job_id={self._job.job_id}"
                        "&dataset=candidate"
                    ),
                    "preprocessed": (
                        f"/api/admin/grid?job_id={self._job.job_id}"
                        "&dataset=preprocessed"
                    ),
                    "auto_error": (
                        f"/api/admin/grid?job_id={self._job.job_id}"
                        "&dataset=auto_error"
                    ),
                },
            }
            files: dict[str, dict[str, str]] = {}
            if self._job.preprocessed_file:
                files["preprocessed"] = {
                    "name": self._job.preprocessed_file,
                    "url": (
                        f"/downloads/{self._job.job_id}/"
                        f"{self._job.preprocessed_file}"
                    ),
                }
            if self._job.candidate_file:
                files["candidate"] = {
                    "name": self._job.candidate_file,
                    "url": (
                        f"/downloads/{self._job.job_id}/"
                        f"{self._job.candidate_file}"
                    ),
                }
            status["files"] = files
            return status

    def page(
        self,
        *,
        job_id: str | None,
        dataset: Literal["candidate", "auto_error", "preprocessed"],
        page: int,
        page_size: int,
        search: str | None,
        column_filters: dict[str, ColumnFilterValue] | None,
    ) -> dict[str, object]:
        with self._lock:
            job = self._require_job(job_id)
            frame = (
                job.candidates
                if dataset == "candidate"
                else job.confirmed_errors
                if dataset == "auto_error"
                else job.preprocessed
            )
            row_ids = filtered_row_ids(
                frame,
                search=search,
                column_filters=column_filters,
            )

            total = len(row_ids)
            start = (page - 1) * page_size
            selected_ids = row_ids[start : start + page_size]
            rows: list[dict[str, object]] = []
            for row_id in selected_ids:
                values = {
                    str(column): json_cell_value(column, value)
                    for column, value in frame.iloc[row_id].items()
                }
                rows.append(
                    {
                        "row_id": row_id,
                        "checked": (
                            job.candidate_checked[row_id]
                            if dataset == "candidate"
                            else False
                        ),
                        **values,
                    }
                )
            return {
                "job_id": job.job_id,
                "dataset": dataset,
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size,
                "columns": [str(column) for column in frame.columns],
                "rows": rows,
                "checked_count": sum(job.candidate_checked),
                "can_rollback": DASHBOARD_STORE.has_active_error_batches(),
            }

    def values(
        self,
        *,
        job_id: str | None,
        dataset: Literal["candidate", "auto_error", "preprocessed"],
        column: str,
        search: str | None,
        limit: int,
    ) -> dict[str, object]:
        with self._lock:
            job = self._require_job(job_id)
            frame = (
                job.candidates
                if dataset == "candidate"
                else job.confirmed_errors
                if dataset == "auto_error"
                else job.preprocessed
            )
            result = distinct_column_values(
                frame,
                column=column,
                search=search,
                limit=limit,
            )
            result.update({"job_id": job.job_id, "dataset": dataset})
            return result

    def confirmed_order_numbers(
        self,
        job_id: str | None,
        row_ids: list[int],
        *,
        required_status: Literal["확정", "제외"],
    ) -> list[str]:
        with self._lock:
            job = self._require_job(job_id)
            requested = sorted(set(int(row_id) for row_id in row_ids))
            if not requested:
                raise ValueError("처리할 확정 오생성을 선택해 주세요.")
            invalid = [
                row_id
                for row_id in requested
                if row_id < 0 or row_id >= len(job.confirmed_errors)
            ]
            if invalid:
                raise IndexError(
                    f"존재하지 않는 확정 오생성 row_id입니다: {invalid[:10]}"
                )
            selected = job.confirmed_errors.iloc[requested]
            wrong_status = selected.loc[
                selected["집계상태"].astype("string").ne(required_status)
            ]
            if not wrong_status.empty:
                raise ValueError(
                    f"'{required_status}' 상태의 행만 선택해 주세요."
                )
            return [
                normalize_order_number(value)
                for value in selected["오더번호"]
            ]

    def apply_checks(
        self,
        job_id: str | None,
        requested_updates: dict[int, bool],
    ) -> dict[str, object]:
        with self._lock:
            job = self._require_job(job_id)
            if not requested_updates:
                return {
                    "job_id": job.job_id,
                    "changed_count": 0,
                    "checked_count": sum(job.candidate_checked),
                    "candidate_count": len(job.candidates),
                }

            invalid_ids = sorted(
                row_id
                for row_id in requested_updates
                if row_id < 0 or row_id >= len(job.candidates)
            )
            if invalid_ids:
                raise IndexError(f"존재하지 않는 후보 row_id입니다: {invalid_ids[:10]}")

            changes: list[tuple[int, bool, int]] = []
            deltas: dict[tuple[str, str, str, str], int] = defaultdict(int)
            for row_id, desired in requested_updates.items():
                current = job.candidate_checked[row_id]
                if current == desired:
                    continue
                row = job.candidates.iloc[row_id]
                order_date_value = pd.to_datetime(
                    row.get("오더생성일"), errors="coerce"
                )
                if pd.isna(order_date_value):
                    raise ValueError(
                        f"row_id {row_id}의 오더생성일이 없어 집계할 수 없습니다."
                    )
                delta = 1 if desired else -1
                key = (
                    order_date_value.date().isoformat(),
                    clean_dimension(row.get("생성인")),
                    clean_dimension(row.get("사업부")),
                    clean_dimension(row.get("소분류")),
                )
                deltas[key] += delta
                changes.append((row_id, desired, delta))

            # SQLite is updated first; the in-memory state changes only after a
            # successful aggregate transaction.
            DASHBOARD_STORE.adjust_error_counts(deltas)
            for row_id, desired, _ in changes:
                job.candidate_checked[row_id] = desired
            return {
                "job_id": job.job_id,
                "changed_count": len(changes),
                "checked_count": sum(job.candidate_checked),
                "candidate_count": len(job.candidates),
            }

    def approve_rows(
        self,
        job_id: str | None,
        row_ids: list[int],
    ) -> dict[str, object]:
        with self._lock:
            job = self._require_job(job_id)
            requested = sorted(set(int(row_id) for row_id in row_ids))
            if not requested:
                raise ValueError("승인할 후보를 선택해 주세요.")
            invalid_ids = [
                row_id
                for row_id in requested
                if row_id < 0 or row_id >= len(job.candidates)
            ]
            if invalid_ids:
                raise IndexError(f"존재하지 않는 후보 row_id입니다: {invalid_ids[:10]}")
            pending_ids = [
                row_id for row_id in requested if not job.candidate_checked[row_id]
            ]
            if not pending_ids:
                raise ValueError("선택한 후보는 이미 승인되었습니다.")

            records: list[dict[str, object]] = []
            for row_id in pending_ids:
                row = job.candidates.iloc[row_id]
                order_date_value = pd.to_datetime(
                    row.get("오더생성일"), errors="coerce"
                )
                if pd.isna(order_date_value):
                    raise ValueError(
                        f"row_id {row_id}의 오더생성일이 없어 승인할 수 없습니다."
                    )
                payload = {
                    str(column): json_value(value)
                    for column, value in row.items()
                }
                records.append(
                    {
                        "candidate_row_id": row_id,
                        "order_number": row.get("오더번호"),
                        "order_date": order_date_value.date().isoformat(),
                        "person": clean_dimension(row.get("생성인")),
                        "business": clean_dimension(row.get("사업부")),
                        "subcategory": clean_dimension(row.get("소분류")),
                        "payload": payload,
                    }
                )

            approved_at = datetime.now().astimezone().isoformat(
                timespec="microseconds"
            )
            excluded_orders = DASHBOARD_STORE.excluded_error_order_numbers()
            restored_records = [
                record
                for record in records
                if normalize_order_number(record.get("order_number"))
                in excluded_orders
            ]
            new_records = [
                record
                for record in records
                if normalize_order_number(record.get("order_number"))
                not in excluded_orders
            ]
            restored_order_numbers = sorted(
                {
                    normalize_order_number(record.get("order_number"))
                    for record in restored_records
                }
            )
            restored_count = 0
            if restored_order_numbers:
                restored = DASHBOARD_STORE.restore_error_orders(
                    restored_order_numbers,
                    job_id=job.job_id,
                    restored_at=approved_at,
                )
                restored_count = int(restored["restored_count"])
            try:
                if new_records:
                    result = DASHBOARD_STORE.approve_error_batch(
                        batch_id=uuid.uuid4().hex[:16],
                        job_id=job.job_id,
                        source_name=job.source_name,
                        approved_at=approved_at,
                        records=new_records,
                    )
                else:
                    result = {
                        "batch_id": None,
                        "job_id": job.job_id,
                        "approved_count": 0,
                        "approved_at": approved_at,
                        "data_start": min(
                            str(record["order_date"]) for record in records
                        ),
                        "data_end": max(
                            str(record["order_date"]) for record in records
                        ),
                        "row_ids": [],
                        "order_numbers": [],
                    }
            except Exception:
                if restored_order_numbers:
                    DASHBOARD_STORE.exclude_error_orders(
                        restored_order_numbers,
                        job_id=job.job_id,
                        excluded_at=datetime.now().astimezone().isoformat(
                            timespec="microseconds"
                        ),
                    )
                raise
            result["new_approved_count"] = int(result["approved_count"])
            result["restored_count"] = restored_count
            result["restored_order_numbers"] = restored_order_numbers
            result["approved_count"] = (
                int(result["approved_count"]) + restored_count
            )
            result["row_ids"] = pending_ids
            result["order_numbers"] = sorted(
                {
                    normalize_order_number(record.get("order_number"))
                    for record in records
                }
            )
            for row_id in pending_ids:
                job.candidate_checked[row_id] = True
            result.update(
                {
                    "checked_count": sum(job.candidate_checked),
                    "candidate_count": len(job.candidates),
                }
            )
            return result

    def rollback_latest(self, job_id: str | None) -> dict[str, object]:
        with self._lock:
            job = self._job
            if job is not None and job_id and job_id != job.job_id:
                raise LookupError("이전 분석 작업입니다. 최신 데이터를 다시 불러와 주세요.")
            rolled_back_at = datetime.now().astimezone().isoformat(
                timespec="microseconds"
            )
            result = DASHBOARD_STORE.rollback_latest_error_batch(
                job_id=None,
                rolled_back_at=rolled_back_at,
            )
            rolled_order_numbers = set(result.get("order_numbers", []))
            if job is not None and "오더번호" in job.candidates.columns:
                for row_id, order_number in enumerate(job.candidates["오더번호"]):
                    if normalize_order_number(order_number) in rolled_order_numbers:
                        job.candidate_checked[row_id] = False
            result.update(
                {
                    "checked_count": sum(job.candidate_checked) if job else 0,
                    "candidate_count": len(job.candidates) if job else 0,
                }
            )
            return result

    def refresh_classification(
        self,
        job_id: str | None,
    ) -> dict[str, object]:
        """Reclassify and persist the active job after registry changes."""
        with self._lock:
            job = self._require_job(job_id)
            sap_id_map, _ = load_configuration()
            raw_candidates, raw_auto_errors, classification_summary = (
                select_candidate_orders(job.preprocessed, sap_id_map)
            )
            # The persisted frame is intentionally masked. Masking a customer
            # name can remove a legacy normal phrase and must not turn a row
            # that was normal at upload time into a new review candidate.
            # Newly detected automatic errors remain allowed across all rows.
            job.reviewable_order_numbers.update(
                frame_order_numbers(raw_auto_errors)
            )
            raw_candidates = retain_review_scope(
                raw_candidates,
                job.reviewable_order_numbers,
            )
            (
                candidates,
                auto_errors,
                confirmed_errors,
                excluded_error_count,
            ) = apply_error_exclusions(
                job.preprocessed,
                raw_candidates,
                raw_auto_errors,
            )
            automation_keys = {
                key: value
                for key, value in job.candidate_summary.items()
                if str(key).startswith("자동정상")
                or key == "소분류확정정상패턴수"
            }
            candidate_summary = {
                **classification_summary,
                **automation_keys,
                "검토후보행수": len(candidates),
                "후보행수": len(candidates),
                "자동오생성행수": len(auto_errors),
                "관리자제외오생성행수": excluded_error_count,
            }
            aggregate_summary = DASHBOARD_STORE.replace_totals_and_auto_errors(
                None,
                raw_auto_errors,
                batch_id=uuid.uuid4().hex[:16],
                job_id=job.job_id,
                source_name=job.source_name,
                approved_at=datetime.now().astimezone().isoformat(
                    timespec="microseconds"
                ),
                replace_totals=False,
                date_start=str(job.period.get("start") or ""),
                date_end=str(job.period.get("end") or ""),
            )

            candidate_path = (
                WEB_OUTPUT_ROOT / job.job_id / job.candidate_file
            )
            if not candidate_path.is_file():
                raise FileNotFoundError(
                    f"현재 후보 결과 파일이 없습니다: {candidate_path.name}"
                )
            temporary_candidate = candidate_path.with_name(
                f".{candidate_path.stem}_{uuid.uuid4().hex[:8]}_refresh.xlsx"
            )
            try:
                save_classification_workbook(
                    candidates,
                    auto_errors,
                    temporary_candidate,
                )
                os.replace(temporary_candidate, candidate_path)
            finally:
                temporary_candidate.unlink(missing_ok=True)

            active_manual_numbers = DASHBOARD_STORE.active_error_order_numbers(
                batch_type="manual"
            )
            checked = [False] * len(candidates)
            if "오더번호" in candidates.columns and active_manual_numbers:
                checked = [
                    normalize_order_number(value) in active_manual_numbers
                    for value in candidates["오더번호"]
                ]
            job.candidates = candidates.reset_index(drop=True)
            job.auto_errors = auto_errors.reset_index(drop=True)
            job.confirmed_errors = confirmed_errors.reset_index(drop=True)
            job.candidate_checked = checked
            job.candidate_summary = candidate_summary
            job.aggregate_summary = deepcopy(aggregate_summary)

            manifest = {
                "version": 1,
                "job_id": job.job_id,
                "source_name": job.source_name,
                "created_at": job.created_at,
                "period": deepcopy(job.period),
                "preprocess": deepcopy(job.preprocess_summary),
                "candidate": deepcopy(candidate_summary),
                "aggregate": deepcopy(aggregate_summary),
                "preprocessed_file": job.preprocessed_file,
                "candidate_file": job.candidate_file,
            }
            temporary_manifest = prepare_current_job_manifest(
                WEB_OUTPUT_ROOT,
                manifest,
            )
            os.replace(temporary_manifest, CURRENT_JOB_MANIFEST_PATH)
            return {
                "job_id": job.job_id,
                "candidate_count": len(candidates),
                "auto_error_count": len(auto_errors),
                "confirmed_error_count": int(
                    confirmed_errors["집계상태"].eq("확정").sum()
                ),
                "excluded_error_count": excluded_error_count,
                "checked_count": sum(checked),
                "candidate": deepcopy(candidate_summary),
                "aggregate": deepcopy(aggregate_summary),
            }

    def sync_active_views(
        self,
        job_id: str | None,
        *,
        restore_order_numbers: list[str] | None = None,
    ) -> dict[str, object]:
        """Refresh admin grids from persisted approvals without rerunning rules.

        Approval, exclusion and rollback are operational state changes.  They
        must be visible immediately and must not wait for the comparatively
        expensive historical pattern-registry rebuild.
        """
        with self._lock:
            job = self._require_job(job_id)
            candidate_source = job.candidates.copy()
            active_auto_numbers = DASHBOARD_STORE.active_error_order_numbers(
                batch_type="auto"
            )
            if "오더번호" in job.auto_errors.columns:
                automatic_source = job.auto_errors.loc[
                    job.auto_errors["오더번호"]
                    .map(normalize_order_number)
                    .isin(active_auto_numbers)
                ].reset_index(drop=True)
            else:
                automatic_source = job.auto_errors.iloc[0:0].copy()
            restore_numbers = {
                normalize_order_number(value)
                for value in (restore_order_numbers or [])
                if normalize_order_number(value)
            }
            restore_numbers &= job.reviewable_order_numbers
            if (
                restore_numbers
                and "오더번호" in job.preprocessed.columns
            ):
                existing_numbers = (
                    set(
                        candidate_source["오더번호"].map(
                            normalize_order_number
                        )
                    )
                    if "오더번호" in candidate_source.columns
                    else set()
                )
                missing_numbers = restore_numbers - existing_numbers
                if missing_numbers:
                    restored = job.preprocessed.loc[
                        job.preprocessed["오더번호"]
                        .map(normalize_order_number)
                        .isin(missing_numbers)
                    ].copy()
                    if not restored.empty:
                        restored = format_classified_orders(restored)
                        restored = restored.reindex(
                            columns=candidate_source.columns,
                            fill_value=None,
                        )
                        candidate_source = pd.concat(
                            [candidate_source, restored],
                            ignore_index=True,
                        )

            (
                candidates,
                active_confirmed,
                confirmed_errors,
                excluded_error_count,
            ) = apply_error_exclusions(
                job.preprocessed,
                candidate_source,
                automatic_source,
            )
            period_start = str(job.period.get("start") or "")
            period_end = str(job.period.get("end") or "")
            aggregate_summary = DASHBOARD_STORE.period_status(
                period_start,
                period_end,
            )
            candidate_summary = deepcopy(job.candidate_summary)
            candidate_summary.update(
                {
                    "검토후보행수": len(candidates),
                    "후보행수": len(candidates),
                    "자동오생성행수": len(active_confirmed),
                    "관리자제외오생성행수": excluded_error_count,
                }
            )

            candidate_path = WEB_OUTPUT_ROOT / job.job_id / job.candidate_file
            if candidate_path.is_file():
                temporary_candidate = candidate_path.with_name(
                    f".{candidate_path.stem}_{uuid.uuid4().hex[:8]}_sync.xlsx"
                )
                try:
                    save_classification_workbook(
                        candidates,
                        active_confirmed,
                        temporary_candidate,
                    )
                    os.replace(temporary_candidate, candidate_path)
                finally:
                    temporary_candidate.unlink(missing_ok=True)

            job.candidates = candidates.reset_index(drop=True)
            job.auto_errors = automatic_source.reset_index(drop=True)
            job.confirmed_errors = confirmed_errors.reset_index(drop=True)
            job.candidate_checked = [False] * len(candidates)
            job.candidate_summary = candidate_summary
            job.aggregate_summary = deepcopy(aggregate_summary)

            manifest = {
                "version": 1,
                "job_id": job.job_id,
                "source_name": job.source_name,
                "created_at": job.created_at,
                "period": deepcopy(job.period),
                "preprocess": deepcopy(job.preprocess_summary),
                "candidate": deepcopy(candidate_summary),
                "aggregate": deepcopy(aggregate_summary),
                "preprocessed_file": job.preprocessed_file,
                "candidate_file": job.candidate_file,
            }
            temporary_manifest = prepare_current_job_manifest(
                WEB_OUTPUT_ROOT,
                manifest,
            )
            os.replace(temporary_manifest, CURRENT_JOB_MANIFEST_PATH)
            confirmed_count = int(
                confirmed_errors["집계상태"].eq("확정").sum()
            )
            return {
                "job_id": job.job_id,
                "candidate_count": len(candidates),
                "auto_error_count": len(automatic_source),
                "confirmed_error_count": confirmed_count,
                "excluded_error_count": excluded_error_count,
                "checked_count": 0,
                "candidate": deepcopy(candidate_summary),
                "aggregate": deepcopy(aggregate_summary),
            }

    def _require_job(self, job_id: str | None) -> CurrentJob:
        if self._job is None:
            raise LookupError("현재 분석된 데이터가 없습니다.")
        if job_id and job_id != self._job.job_id:
            raise LookupError("이전 분석 작업입니다. 최신 데이터를 다시 불러와 주세요.")
        return self._job


class EventBroker:
    def __init__(self) -> None:
        self._lock = RLock()
        self._next_id = 1
        self._events: deque[dict[str, object]] = deque(maxlen=100)

    def publish(
        self,
        event_type: str,
        message: str,
        data: dict[str, object] | None = None,
    ) -> dict[str, object]:
        with self._lock:
            event = {
                "id": self._next_id,
                "type": event_type,
                "message": message,
                "created_at": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
                "data": data or {},
            }
            self._next_id += 1
            self._events.append(event)
            return event

    def after(self, event_id: int) -> list[dict[str, object]]:
        with self._lock:
            return [event.copy() for event in self._events if event["id"] > event_id]

    def latest(self) -> dict[str, object] | None:
        with self._lock:
            return self._events[-1].copy() if self._events else None


CURRENT_JOB = CurrentJobManager()
EVENTS = EventBroker()
EVENTS.publish("server_ready", "서비스오더 대시보드 서버에 연결되었습니다.")


def current_job_directories() -> list[Path]:
    WEB_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    return sorted(
        (
            item
            for item in WEB_OUTPUT_ROOT.iterdir()
            if item.is_dir() and re.fullmatch(r"[0-9a-f]{12}", item.name)
        ),
        key=lambda item: item.name,
    )


def cleanup_current_job_storage(keep_job_id: str | None) -> None:
    """Remove stale result jobs while leaving at most the current job."""
    WEB_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    for directory in current_job_directories():
        if directory.name != keep_job_id:
            shutil.rmtree(directory)
    for item in WEB_OUTPUT_ROOT.iterdir():
        if item.is_file() and item.name.startswith(".current_job_"):
            item.unlink(missing_ok=True)


def validated_result_filename(value: object, field: str) -> str:
    filename = str(value or "")
    if (
        not filename
        or Path(filename).name != filename
        or Path(filename).suffix.lower() != ".xlsx"
    ):
        raise ValueError(f"{field}가 올바른 XLSX 파일명이 아닙니다.")
    return filename


def validated_current_job_manifest(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("현재 작업 manifest 버전이 올바르지 않습니다.")
    job_id = str(payload.get("job_id") or "")
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise ValueError("현재 작업 job_id가 올바르지 않습니다.")
    period = payload.get("period")
    preprocess_summary = payload.get("preprocess")
    candidate_summary = payload.get("candidate")
    aggregate_summary = payload.get("aggregate")
    if not isinstance(period, dict):
        raise ValueError("현재 작업 기간 정보가 올바르지 않습니다.")
    if not isinstance(preprocess_summary, dict):
        raise ValueError("현재 작업 전처리 요약이 올바르지 않습니다.")
    if not isinstance(candidate_summary, dict):
        raise ValueError("현재 작업 후보 요약이 올바르지 않습니다.")
    if not isinstance(aggregate_summary, dict):
        raise ValueError("현재 작업 집계 요약이 올바르지 않습니다.")
    return {
        "version": 1,
        "job_id": job_id,
        "source_name": str(payload.get("source_name") or ""),
        "created_at": str(payload.get("created_at") or ""),
        "period": {
            "start": period.get("start"),
            "end": period.get("end"),
        },
        "preprocess": preprocess_summary,
        "candidate": candidate_summary,
        "aggregate": aggregate_summary,
        "preprocessed_file": validated_result_filename(
            payload.get("preprocessed_file"), "preprocessed_file"
        ),
        "candidate_file": validated_result_filename(
            payload.get("candidate_file"), "candidate_file"
        ),
    }


def clean_restored_frame(frame: pd.DataFrame) -> pd.DataFrame:
    for column in frame.select_dtypes(include=["object", "string"]).columns:
        frame[column] = frame[column].map(
            lambda value: (
                value[1:]
                if isinstance(value, str)
                and len(value) > 1
                and value.startswith("'")
                and value[1:].lstrip().startswith(("=", "+", "-", "@"))
                else value
            )
        )
    return frame


def restore_excel_frame(path: Path) -> pd.DataFrame:
    return clean_restored_frame(pd.read_excel(path, engine="openpyxl"))


def restore_classification_frames(
    path: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    sheets = pd.read_excel(path, sheet_name=None, engine="openpyxl")
    candidate_name = "후보군" if "후보군" in sheets else next(iter(sheets))
    candidates = clean_restored_frame(sheets[candidate_name])
    auto_errors = clean_restored_frame(
        sheets.get("자동오생성", candidates.iloc[0:0].copy())
    )
    return candidates, auto_errors


def infer_legacy_current_job_manifest() -> dict[str, object] | None:
    """Migrate the one result directory created before manifests were added."""
    job_directories = current_job_directories()
    if len(job_directories) != 1:
        return None
    job_dir = job_directories[0]
    preprocessed_files = sorted(
        [*job_dir.glob("*_preprocessd.xlsx"), *job_dir.glob("*_total_data.xlsx")]
    )
    candidate_files = sorted(job_dir.glob("*_candidate.xlsx"))
    if len(preprocessed_files) != 1 or len(candidate_files) != 1:
        return None

    preprocessed = restore_excel_frame(preprocessed_files[0])
    candidates = restore_excel_frame(candidate_files[0])
    if "오더생성일" not in preprocessed.columns:
        return None
    dates = pd.to_datetime(preprocessed["오더생성일"], errors="coerce").dropna()
    if dates.empty:
        return None
    source_stem = (
        preprocessed_files[0].stem.removesuffix("_preprocessd").removesuffix("_total_data")
    )
    manifest = {
        "version": 1,
        "job_id": job_dir.name,
        "source_name": f"{source_stem}.xlsx",
        "created_at": datetime.fromtimestamp(job_dir.stat().st_mtime)
        .astimezone()
        .isoformat(timespec="seconds"),
        "period": {
            "start": dates.min().strftime("%Y-%m-%d"),
            "end": dates.max().strftime("%Y-%m-%d"),
        },
        "preprocess": {"전처리완료행수": len(preprocessed)},
        "candidate": {"후보행수": len(candidates)},
        "aggregate": {
            "start": dates.min().strftime("%Y-%m-%d"),
            "end": dates.max().strftime("%Y-%m-%d"),
            "total_count": len(preprocessed),
        },
        "preprocessed_file": preprocessed_files[0].name,
        "candidate_file": candidate_files[0].name,
    }
    temporary_manifest = prepare_current_job_manifest(WEB_OUTPUT_ROOT, manifest)
    os.replace(temporary_manifest, CURRENT_JOB_MANIFEST_PATH)
    return manifest


def restore_current_job() -> dict[str, object] | None:
    CURRENT_JOB.clear()
    WEB_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    if not CURRENT_JOB_MANIFEST_PATH.is_file():
        try:
            inferred_manifest = infer_legacy_current_job_manifest()
        except Exception:
            LOGGER.exception("Failed to migrate the legacy admin analysis job")
            inferred_manifest = None
        if inferred_manifest is None:
            cleanup_current_job_storage(None)
            return None
    try:
        raw_manifest = json.loads(
            CURRENT_JOB_MANIFEST_PATH.read_text(encoding="utf-8")
        )
        manifest = validated_current_job_manifest(raw_manifest)
        job_id = str(manifest["job_id"])
        job_dir = WEB_OUTPUT_ROOT / job_id
        preprocessed_path = job_dir / str(manifest["preprocessed_file"])
        candidate_path = job_dir / str(manifest["candidate_file"])
        if not preprocessed_path.is_file() or not candidate_path.is_file():
            raise FileNotFoundError("현재 작업의 결과 XLSX 파일이 없습니다.")
        preprocessed = restore_excel_frame(preprocessed_path)
        candidates, auto_errors = restore_classification_frames(candidate_path)
        CURRENT_JOB.replace(
            job_id,
            str(manifest["source_name"]),
            preprocessed,
            candidates,
            auto_errors,
            created_at=str(manifest["created_at"]),
            period=dict(manifest["period"]),
            preprocess_summary=dict(manifest["preprocess"]),
            candidate_summary=dict(manifest["candidate"]),
            aggregate_summary=dict(manifest["aggregate"]),
            preprocessed_file=str(manifest["preprocessed_file"]),
            candidate_file=str(manifest["candidate_file"]),
        )
        # Approval state can change after the last XLSX snapshot was written.
        # Reconcile the restored grids with SQLite so admin and dashboard
        # counts start from the same authoritative state.
        CURRENT_JOB.sync_active_views(job_id)
        cleanup_current_job_storage(job_id)
        return CURRENT_JOB.status()
    except Exception:
        LOGGER.exception("Failed to restore the latest admin analysis job")
        CURRENT_JOB.clear()
        CURRENT_JOB_MANIFEST_PATH.unlink(missing_ok=True)
        cleanup_current_job_storage(None)
        return None


def prepare_current_job_manifest(
    directory: Path,
    payload: dict[str, object],
) -> Path:
    validated = validated_current_job_manifest(payload)
    path = directory / f".current_job_{validated['job_id']}.json"
    path.write_text(
        json.dumps(validated, ensure_ascii=False, indent=2, default=json_value),
        encoding="utf-8",
    )
    return path


@dataclass
class CurrentJobPromotion:
    job_id: str
    staged_output_dir: Path
    final_output_dir: Path
    moved_previous: list[tuple[Path, Path]]
    previous_manifest_backup: Path | None = None
    new_results_installed: bool = False
    new_manifest_installed: bool = False


def rollback_current_job_promotion(promotion: CurrentJobPromotion) -> None:
    """Restore the result files and manifest that preceded a promotion."""
    if promotion.new_manifest_installed:
        CURRENT_JOB_MANIFEST_PATH.unlink(missing_ok=True)
        promotion.new_manifest_installed = False
    if promotion.new_results_installed and promotion.final_output_dir.exists():
        if promotion.staged_output_dir.exists():
            shutil.rmtree(promotion.final_output_dir)
        else:
            promotion.final_output_dir.replace(promotion.staged_output_dir)
        promotion.new_results_installed = False
    for previous, backup in reversed(promotion.moved_previous):
        if backup.exists() and not previous.exists():
            backup.replace(previous)
    if (
        promotion.previous_manifest_backup is not None
        and promotion.previous_manifest_backup.exists()
    ):
        os.replace(
            promotion.previous_manifest_backup,
            CURRENT_JOB_MANIFEST_PATH,
        )


def finalize_current_job_promotion(promotion: CurrentJobPromotion) -> None:
    """Discard backups only after the aggregate transaction has committed."""
    for _, backup in promotion.moved_previous:
        shutil.rmtree(backup, ignore_errors=True)
    if promotion.previous_manifest_backup is not None:
        promotion.previous_manifest_backup.unlink(missing_ok=True)
    cleanup_current_job_storage(promotion.job_id)


def promote_current_job_results(
    *,
    staged_output_dir: Path,
    job_id: str,
    manifest_path: Path,
    transaction_dir: Path,
) -> CurrentJobPromotion:
    """Install a new result while retaining rollback files outside result storage."""
    WEB_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    final_output_dir = WEB_OUTPUT_ROOT / job_id
    if final_output_dir.exists():
        raise FileExistsError(f"이미 존재하는 분석 job_id입니다: {job_id}")

    promotion = CurrentJobPromotion(
        job_id=job_id,
        staged_output_dir=staged_output_dir,
        final_output_dir=final_output_dir,
        moved_previous=[],
    )
    try:
        if CURRENT_JOB_MANIFEST_PATH.is_file():
            manifest_backup = transaction_dir / "previous_current_job.json"
            CURRENT_JOB_MANIFEST_PATH.replace(manifest_backup)
            promotion.previous_manifest_backup = manifest_backup
        for previous in current_job_directories():
            backup = transaction_dir / f"previous_{previous.name}"
            previous.replace(backup)
            promotion.moved_previous.append((previous, backup))
        staged_output_dir.replace(final_output_dir)
        promotion.new_results_installed = True
        os.replace(manifest_path, CURRENT_JOB_MANIFEST_PATH)
        promotion.new_manifest_installed = True
    except Exception:
        rollback_current_job_promotion(promotion)
        raise

    return promotion


class CheckUpdate(BaseModel):
    row_id: int = Field(ge=0)
    checked: bool


class SingleCheckRequest(BaseModel):
    checked: bool
    job_id: str | None = None


class BatchCheckRequest(BaseModel):
    updates: list[CheckUpdate] = Field(default_factory=list)
    row_ids: list[int] = Field(default_factory=list)
    checked: bool | None = None
    job_id: str | None = None


class ApproveRequest(BaseModel):
    row_ids: list[int] = Field(min_length=1)
    job_id: str | None = None


class RollbackRequest(BaseModel):
    job_id: str | None = None


class ErrorRowsRequest(BaseModel):
    row_ids: list[int] = Field(min_length=1)
    job_id: str | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    clear_directory(TEMP_UPLOAD_ROOT)
    clear_directory(TEMP_EXPORT_ROOT)
    restored_job = await asyncio.to_thread(restore_current_job)
    if restored_job:
        EVENTS.publish(
            "analysis_restored",
            "마지막 관리자 분석 결과를 복원했습니다.",
            {
                "job_id": restored_job["job_id"],
                "preprocessed_count": restored_job["preprocessed_count"],
                "candidate_count": restored_job["candidate_count"],
            },
        )
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    set_historical_status(
        "loading",
        started_at=started_at,
        message="1~6월 전체 데이터와 확정 오생성을 집계하고 있습니다.",
    )

    async def refresh_historical_data() -> None:
        try:
            historical_result = await asyncio.to_thread(
                load_and_store_historical_metrics
            )
        except Exception as error:
            finished_at = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            LOGGER.exception("Failed to load historical service-order metrics")
            set_historical_status(
                "error",
                started_at=started_at,
                finished_at=finished_at,
                message=f"과거 데이터 연동 실패: {error}",
            )
            EVENTS.publish(
                "historical_data_error",
                "과거 서비스오더 데이터 연동을 완료하지 못했습니다.",
                {"message": str(error)},
            )
        else:
            finished_at = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            set_historical_status(
                "ready",
                started_at=started_at,
                finished_at=finished_at,
                result=historical_result,
                message="1~6월 과거 데이터 연동을 완료했습니다.",
            )
            stored = historical_result["stored"]
            EVENTS.publish(
                "historical_data_ready",
                "1~6월 서비스오더 과거 데이터를 반영했습니다.",
                {
                    "total_count": stored["total_count"],
                    "error_count": stored["error_count"],
                    "start": stored["start"],
                    "end": stored["end"],
                },
            )

    # Historical validation must not block the web server from accepting a new
    # upload. Existing SQLite aggregates remain available while it runs.
    historical_task = asyncio.create_task(refresh_historical_data())
    try:
        yield
    finally:
        if not historical_task.done():
            historical_task.cancel()
            with suppress(asyncio.CancelledError):
                await historical_task
        CURRENT_JOB.clear()
        clear_directory(TEMP_UPLOAD_ROOT)
        clear_directory(TEMP_EXPORT_ROOT)


app = FastAPI(
    title="CENTER Dashboard",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.mount("/assets", StaticFiles(directory=BASE_DIR / "assets"), name="assets")


@app.get("/")
def home() -> FileResponse:
    return FileResponse(
        BASE_DIR / "dashboard_home.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/service_order")
def service_order_redirect() -> RedirectResponse:
    return RedirectResponse("/service_order/", status_code=308)


@app.get("/service_order/")
def service_order_home() -> FileResponse:
    return FileResponse(
        SERVICE_ORDER_DIR / "service_order.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/service_order/admin")
def service_order_admin_redirect() -> RedirectResponse:
    return RedirectResponse("/service_order/admin/", status_code=308)


@app.get("/service_order/admin/")
def service_order_admin_home() -> FileResponse:
    return FileResponse(
        SERVICE_ORDER_DIR / "error" / "error_admin.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/error")
@app.get("/error/")
def legacy_error_redirect() -> RedirectResponse:
    return RedirectResponse("/service_order/", status_code=308)


@app.get("/error/admin")
@app.get("/error/admin/")
def legacy_admin_redirect() -> RedirectResponse:
    return RedirectResponse("/service_order/admin/", status_code=308)


@app.get("/error/{legacy_path:path}")
def legacy_error_path_redirect(legacy_path: str) -> RedirectResponse:
    target = (
        "/service_order/admin/"
        if legacy_path.strip("/").startswith("admin")
        else "/service_order/"
    )
    return RedirectResponse(target, status_code=308)


@app.get("/api/health")
def health() -> dict[str, object]:
    current_job = CURRENT_JOB.status()
    dashboard_status = DASHBOARD_STORE.status()
    period = current_job.get("period") if isinstance(current_job, dict) else None
    data_basis_date = (
        str(period.get("end") or "")
        if isinstance(period, dict)
        else ""
    ) or dashboard_status.get("end")
    return {
        "status": "ok",
        "server": "connected",
        "current_job": current_job,
        "dashboard": dashboard_status,
        "data_basis_date": data_basis_date,
        "historical_data": historical_status(),
    }


@app.get("/api/status")
def status() -> dict[str, object]:
    return health()


@app.get("/api/admin/current-job")
def admin_current_job() -> dict[str, object]:
    return {"current_job": CURRENT_JOB.status()}


@app.get("/api/admin/analysis-progress/{analysis_id}")
def admin_analysis_progress(analysis_id: str) -> dict[str, object]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", analysis_id):
        raise HTTPException(status_code=422, detail="올바르지 않은 분석 작업 ID입니다.")
    progress = analysis_progress(analysis_id)
    if progress is None:
        raise HTTPException(status_code=404, detail="분석 진행 상태를 찾을 수 없습니다.")
    return progress


def safe_stem(filename: str) -> str:
    stem = Path(Path(filename).name).stem.strip()
    stem = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" ._")
    return (stem or "uploaded_orders")[:100]


def parse_column_filters(
    column_filters: str | None,
) -> dict[str, ColumnFilterValue]:
    if not column_filters:
        return {}
    try:
        raw_filters = json.loads(column_filters)
    except json.JSONDecodeError as error:
        raise HTTPException(
            status_code=422,
            detail="column_filters는 JSON 객체여야 합니다.",
        ) from error
    if not isinstance(raw_filters, dict):
        raise HTTPException(
            status_code=422,
            detail="column_filters는 JSON 객체여야 합니다.",
        )
    if len(raw_filters) > 20:
        raise HTTPException(
            status_code=422,
            detail="열 필터는 한 번에 최대 20개까지 사용할 수 있습니다.",
        )
    parsed: dict[str, ColumnFilterValue] = {}
    for raw_key, raw_value in raw_filters.items():
        key = str(raw_key)
        if len(key) > 100:
            raise HTTPException(
                status_code=422,
                detail="열 이름과 필터 값은 각각 100자 이하여야 합니다.",
            )
        if isinstance(raw_value, list):
            if len(raw_value) > 200:
                raise HTTPException(
                    status_code=422,
                    detail="한 열에서는 최대 200개 값까지 선택할 수 있습니다.",
                )
            values: list[str] = []
            for item in raw_value:
                if item is None:
                    continue
                if isinstance(item, (dict, list)):
                    raise HTTPException(
                        status_code=422,
                        detail="열 필터 값은 문자열 목록이어야 합니다.",
                    )
                value = str(item).strip()
                if len(value) > 100:
                    raise HTTPException(
                        status_code=422,
                        detail="열 이름과 필터 값은 각각 100자 이하여야 합니다.",
                    )
                if value not in values:
                    values.append(value)
            if values:
                parsed[key] = values
            continue
        if raw_value is None:
            continue
        value = str(raw_value).strip()
        if len(value) > 100:
            raise HTTPException(
                status_code=422,
                detail="열 이름과 필터 값은 각각 100자 이하여야 합니다.",
            )
        if value:
            parsed[key] = value
    return parsed


def dataframe_page_payload(
    frame: pd.DataFrame,
    *,
    page: int,
    page_size: int,
    search: str | None,
    column_filters: dict[str, ColumnFilterValue] | None,
) -> dict[str, object]:
    row_ids = filtered_row_ids(
        frame,
        search=search,
        column_filters=column_filters,
    )
    total = len(row_ids)
    start_index = (page - 1) * page_size
    selected_ids = row_ids[start_index : start_index + page_size]
    rows = []
    for row_id in selected_ids:
        rows.append(
            {
                "row_id": row_id,
                **{
                    str(column): json_value(value)
                    for column, value in frame.iloc[row_id].items()
                },
            }
        )
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
        "columns": [str(column) for column in frame.columns],
        "rows": rows,
    }


@app.post("/api/admin/analyze")
async def analyze(
    file: UploadFile = File(...),
    x_analysis_id: str | None = Header(default=None, alias="X-Analysis-Id"),
) -> dict[str, object]:
    analysis_id = (x_analysis_id or uuid.uuid4().hex).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", analysis_id):
        raise HTTPException(status_code=422, detail="올바르지 않은 분석 작업 ID입니다.")
    filename = Path(file.filename or "").name
    if Path(filename).suffix.lower() != ".xlsx":
        set_analysis_progress(
            analysis_id, 0, ".xlsx 파일만 업로드할 수 있습니다.", state="error"
        )
        raise HTTPException(status_code=400, detail=".xlsx 파일만 업로드할 수 있습니다.")
    if not ANALYSIS_LOCK.acquire(blocking=False):
        set_analysis_progress(
            analysis_id,
            0,
            "다른 분석이 실행 중입니다. 완료 후 다시 시도해 주세요.",
            state="error",
        )
        raise HTTPException(
            status_code=409,
            detail="다른 분석이 실행 중입니다. 완료 후 다시 시도해 주세요.",
        )

    job_id = uuid.uuid4().hex[:12]
    upload_dir = TEMP_UPLOAD_ROOT / job_id
    output_dir = upload_dir / "result"
    upload_path = upload_dir / "source.xlsx"
    set_analysis_progress(analysis_id, 1, "업로드를 준비하고 있습니다.")
    try:
        # Keep the previous successful job available until this analysis has
        # fully completed. A failed or cancelled upload must not erase it.
        upload_dir.mkdir(parents=True, exist_ok=False)
        uploaded_bytes = 0
        expected_bytes = getattr(file, "size", None)
        with upload_path.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                uploaded_bytes += len(chunk)
                if uploaded_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="업로드 파일은 150MB를 초과할 수 없습니다.",
                    )
                destination.write(chunk)
                if isinstance(expected_bytes, int) and expected_bytes > 0:
                    upload_percent = 2 + round(
                        min(1, uploaded_bytes / expected_bytes) * 16
                    )
                    set_analysis_progress(
                        analysis_id,
                        upload_percent,
                        "Excel 파일을 서버로 가져오고 있습니다.",
                    )
        if uploaded_bytes == 0:
            raise HTTPException(status_code=400, detail="빈 파일은 처리할 수 없습니다.")

        set_analysis_progress(analysis_id, 19, "업로드를 완료하고 분석을 시작합니다.")
        result = await run_in_threadpool(
            run_analysis,
            upload_path,
            output_dir,
            safe_stem(filename),
            progress_callback=lambda percent, message: set_analysis_progress(
                analysis_id, percent, message
            ),
        )
        set_analysis_progress(analysis_id, 93, "오생성 제외 내역을 반영하고 있습니다.")
        preprocessed = result["preprocessed"]
        dashboard_totals = result["dashboard_totals"]
        raw_candidates = result["candidates"]
        raw_auto_errors = result["auto_errors"]
        (
            candidates,
            auto_errors,
            _confirmed_errors,
            excluded_error_count,
        ) = apply_error_exclusions(
            preprocessed,
            raw_candidates,
            raw_auto_errors,
        )
        candidate_summary = {
            **result["candidate_summary"],
            "검토후보행수": len(candidates),
            "후보행수": len(candidates),
            "자동오생성행수": len(auto_errors),
            "관리자제외오생성행수": excluded_error_count,
        }
        preprocessed_path = Path(result["preprocessed_path"])
        candidate_path = Path(result["candidate_path"])
        await run_in_threadpool(
            save_classification_workbook,
            candidates,
            auto_errors,
            candidate_path,
        )
        set_analysis_progress(analysis_id, 95, "대시보드 집계 수치를 계산하고 있습니다.")
        aggregate_preview = await run_in_threadpool(
            DASHBOARD_STORE.summarize_totals, dashboard_totals
        )
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        period = {
            "start": result["start_date"],
            "end": result["end_date"],
        }
        manifest_path = prepare_current_job_manifest(
            upload_dir,
            {
                "version": 1,
                "job_id": job_id,
                "source_name": filename,
                "created_at": created_at,
                "period": period,
                "preprocess": result["preprocess_summary"],
                "candidate": candidate_summary,
                "aggregate": aggregate_preview,
                "preprocessed_file": preprocessed_path.name,
                "candidate_file": candidate_path.name,
            },
        )
        prepared_job = await run_in_threadpool(
            CURRENT_JOB.prepare,
            job_id,
            filename,
            preprocessed,
            candidates,
            raw_auto_errors,
            created_at=created_at,
            period=period,
            preprocess_summary=result["preprocess_summary"],
            candidate_summary=candidate_summary,
            aggregate_summary=aggregate_preview,
            preprocessed_file=preprocessed_path.name,
            candidate_file=candidate_path.name,
        )
        promotion = await run_in_threadpool(
            promote_current_job_results,
            staged_output_dir=output_dir,
            job_id=job_id,
            manifest_path=manifest_path,
            transaction_dir=upload_dir,
        )
        try:
            set_analysis_progress(analysis_id, 97, "분석 결과를 대시보드에 반영하고 있습니다.")
            aggregate_result = await run_in_threadpool(
                DASHBOARD_STORE.replace_totals_and_auto_errors,
                dashboard_totals,
                raw_auto_errors,
                batch_id=uuid.uuid4().hex[:16],
                job_id=job_id,
                source_name=filename,
                approved_at=datetime.now().astimezone().isoformat(
                    timespec="microseconds"
                ),
            )
        except Exception:
            try:
                await run_in_threadpool(
                    rollback_current_job_promotion,
                    promotion,
                )
            except Exception:
                LOGGER.exception(
                    "Failed to restore result files after aggregate failure"
                )
            raise
        aggregate_result["auto_error_count"] = len(auto_errors)
        aggregate_result["auto_excluded_count"] = excluded_error_count
        prepared_job.aggregate_summary = deepcopy(aggregate_result)
        prepared_job.candidate_summary["자동오생성행수"] = len(auto_errors)
        prepared_job.candidate_summary[
            "관리자제외오생성행수"
        ] = excluded_error_count
        CURRENT_JOB.install(prepared_job)
        try:
            await run_in_threadpool(finalize_current_job_promotion, promotion)
        except Exception:
            # The active result and manifest are already committed. A stale
            # backup is outside WEB_OUTPUT_ROOT and is cleared with upload_dir.
            LOGGER.exception("Failed to discard previous analysis backups")

        # Uploads are intentionally inference-only.  Feeding an automatic
        # classification back into the registry would reinforce its own
        # mistakes.  Human approval/exclusion/rollback endpoints rebuild the
        # registry, then reclassify the current job immediately.
        set_analysis_progress(
            analysis_id,
            98,
            "누적된 오생성 학습 규칙의 적용 결과를 확인하고 있습니다.",
        )
        classification_summary: dict[str, object] | None = None
        error_learning: dict[str, object] = {
            "state": "applied",
            "training_trigger": "manual_approval",
        }

        set_analysis_progress(analysis_id, 99, "최종 분석 결과를 확인하고 있습니다.")

        data_basis_date = str(period["end"])
        display_basis_date = datetime.strptime(
            data_basis_date, "%Y-%m-%d"
        ).strftime("%Y.%m.%d")
        notification = EVENTS.publish(
            "analysis_completed",
            f"전체 데이터 정리와 후보군 도출을 완료했습니다. (데이터: {display_basis_date} 기준)",
            {
                "job_id": job_id,
                "data_basis_date": data_basis_date,
                "preprocessed_count": len(preprocessed),
                "candidate_count": int(
                    classification_summary.get("candidate_count", len(candidates))
                    if classification_summary
                    else len(candidates)
                ),
                "auto_error_count": int(
                    classification_summary.get(
                        "auto_error_count",
                        aggregate_result.get("auto_error_count", 0),
                    )
                    if classification_summary
                    else aggregate_result.get("auto_error_count", 0)
                ),
            },
        )
        response = CURRENT_JOB.status() or {}
        response["notification"] = notification
        response["error_learning"] = error_learning
        set_analysis_progress(analysis_id, 100, "분석을 완료했습니다.", state="complete")
        return response
    except HTTPException as error:
        set_analysis_progress(
            analysis_id,
            int((analysis_progress(analysis_id) or {}).get("percent", 0)),
            str(error.detail),
            state="error",
        )
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    except Exception as error:
        set_analysis_progress(
            analysis_id,
            int((analysis_progress(analysis_id) or {}).get("percent", 0)),
            f"분석 처리 중 오류가 발생했습니다: {error}",
            state="error",
        )
        shutil.rmtree(output_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail=f"분석 처리 중 오류가 발생했습니다: {error}",
        ) from error
    finally:
        await file.close()
        shutil.rmtree(upload_dir, ignore_errors=True)
        ANALYSIS_LOCK.release()


@app.get("/api/admin/grid")
def admin_grid(
    job_id: str | None = None,
    dataset: Literal["candidate", "auto_error", "preprocessed"] = "candidate",
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    search: str | None = Query(None, max_length=100),
    column_filters: str | None = Query(None, max_length=10000),
) -> dict[str, object]:
    parsed_filters = parse_column_filters(column_filters)
    try:
        return CURRENT_JOB.page(
            job_id=job_id,
            dataset=dataset,
            page=page,
            page_size=page_size,
            search=search,
            column_filters=parsed_filters,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/admin/grid/values")
def admin_grid_values(
    column: str = Query(..., min_length=1, max_length=100),
    job_id: str | None = None,
    dataset: Literal["candidate", "auto_error", "preprocessed"] = "candidate",
    search: str | None = Query(None, max_length=100),
    limit: int = Query(200, ge=1, le=200),
) -> dict[str, object]:
    try:
        return CURRENT_JOB.values(
            job_id=job_id,
            dataset=dataset,
            column=column,
            search=search,
            limit=limit,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.patch("/api/admin/grid/{row_id}/check")
def update_candidate_check(
    row_id: int,
    request: SingleCheckRequest,
) -> dict[str, object]:
    raise HTTPException(
        status_code=409,
        detail="체크는 임시 선택입니다. 선택 승인 버튼으로 확정해 주세요.",
    )


@app.patch("/api/admin/grid/checks")
def update_candidate_checks(request: BatchCheckRequest) -> dict[str, object]:
    raise HTTPException(
        status_code=409,
        detail="체크는 임시 선택입니다. 선택 승인 버튼으로 확정해 주세요.",
    )


@app.post("/api/admin/approve")
def approve_candidates(
    request: ApproveRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    analysis_locked = False
    try:
        if not ANALYSIS_LOCK.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail="데이터 분석이 실행 중입니다. 완료 후 다시 승인해 주세요.",
            )
        analysis_locked = True
        try:
            result = CURRENT_JOB.approve_rows(request.job_id, request.row_ids)
            sync_summary = CURRENT_JOB.sync_active_views(request.job_id)
        except Exception:
            if "result" in locals():
                if int(result.get("new_approved_count", 0) or 0):
                    CURRENT_JOB.rollback_latest(request.job_id)
                restored_order_numbers = list(
                    result.get("restored_order_numbers", [])
                )
                if restored_order_numbers:
                    DASHBOARD_STORE.exclude_error_orders(
                        restored_order_numbers,
                        job_id=request.job_id or "current",
                        excluded_at=datetime.now().astimezone().isoformat(
                            timespec="microseconds"
                        ),
                    )
                try:
                    CURRENT_JOB.sync_active_views(
                        request.job_id,
                        restore_order_numbers=list(
                            result.get("order_numbers", [])
                        ),
                    )
                except Exception:
                    LOGGER.exception(
                        "Failed to restore admin views after approval rollback"
                    )
            raise
        result["classification"] = sync_summary
        result.update(
            {
                "candidate_count": sync_summary["candidate_count"],
                "auto_error_count": sync_summary["auto_error_count"],
                "confirmed_error_count": sync_summary["confirmed_error_count"],
                "checked_count": sync_summary["checked_count"],
            }
        )
    except HTTPException:
        raise
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except IndexError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (KeyError, ValueError, sqlite3.IntegrityError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        LOGGER.exception("Failed to approve candidates and refresh admin views")
        raise HTTPException(
            status_code=500,
            detail=f"승인 후 관리자 화면 갱신에 실패했습니다: {error}",
        ) from error
    finally:
        if analysis_locked:
            ANALYSIS_LOCK.release()
    data_date = str(result["data_end"])
    display_date = datetime.strptime(data_date, "%Y-%m-%d").strftime("%Y.%m.%d")
    result["notification"] = EVENTS.publish(
        "new_errors_approved",
        (
            f"{result['approved_count']:,}건의 새로운 오생성이 생겼습니다. "
            f"(데이터: {display_date} 기준)"
        ),
        result.copy(),
    )
    background_tasks.add_task(
        rebuild_error_rules_background,
        request.job_id or "current",
        "approval",
    )
    return result


@app.post("/api/admin/rollback")
def rollback_candidates(
    request: RollbackRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    analysis_locked = False
    try:
        if not ANALYSIS_LOCK.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail="데이터 분석이 실행 중입니다. 완료 후 다시 롤백해 주세요.",
            )
        analysis_locked = True
        result = CURRENT_JOB.rollback_latest(request.job_id)
        sync_summary = CURRENT_JOB.sync_active_views(
            request.job_id,
            restore_order_numbers=list(result.get("order_numbers", [])),
        )
        result["classification"] = sync_summary
        result.update(
            {
                "candidate_count": sync_summary["candidate_count"],
                "auto_error_count": sync_summary["auto_error_count"],
                "confirmed_error_count": sync_summary["confirmed_error_count"],
                "checked_count": sync_summary["checked_count"],
            }
        )
    except HTTPException:
        raise
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        LOGGER.exception("Failed to rollback approval and refresh admin views")
        raise HTTPException(
            status_code=500,
            detail=f"승인 롤백 후 관리자 화면 갱신에 실패했습니다: {error}",
        ) from error
    finally:
        if analysis_locked:
            ANALYSIS_LOCK.release()
    order_numbers = [
        str(value) for value in result.get("order_numbers", []) if str(value)
    ]
    visible_orders = ", ".join(order_numbers[:5])
    remaining = max(0, len(order_numbers) - 5)
    order_message = (
        f" 오더번호: {visible_orders}"
        + (f" 외 {remaining}건" if remaining else "")
        if visible_orders
        else ""
    )
    result["notification"] = EVENTS.publish(
        "error_approval_rolled_back",
        (
            f"최근 승인 {result['rolled_back_count']:,}건을 롤백했습니다."
            f"{order_message}"
        ),
        result.copy(),
    )
    background_tasks.add_task(
        rebuild_error_rules_background,
        request.job_id or "current",
        "rollback",
    )
    return result


@app.post("/api/admin/errors/exclude")
def exclude_confirmed_errors(
    request: ErrorRowsRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    analysis_locked = False
    try:
        if not ANALYSIS_LOCK.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail="데이터 분석이 실행 중입니다. 완료 후 다시 시도해 주세요.",
            )
        analysis_locked = True
        order_numbers = CURRENT_JOB.confirmed_order_numbers(
            request.job_id,
            request.row_ids,
            required_status="확정",
        )
        changed_at = datetime.now().astimezone().isoformat(
            timespec="microseconds"
        )
        result = DASHBOARD_STORE.exclude_error_orders(
            order_numbers,
            job_id=request.job_id or "current",
            excluded_at=changed_at,
        )
        try:
            result["classification"] = CURRENT_JOB.sync_active_views(
                request.job_id
            )
        except Exception:
            DASHBOARD_STORE.restore_error_orders(
                order_numbers,
                job_id=request.job_id or "current",
                restored_at=datetime.now().astimezone().isoformat(
                    timespec="microseconds"
                ),
            )
            CURRENT_JOB.sync_active_views(request.job_id)
            raise
    except HTTPException:
        raise
    except (IndexError, LookupError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (KeyError, ValueError, sqlite3.IntegrityError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"확정 오생성 제외에 실패했습니다: {error}",
        ) from error
    finally:
        if analysis_locked:
            ANALYSIS_LOCK.release()
    result["notification"] = EVENTS.publish(
        "confirmed_errors_excluded",
        f"오생성 {result['excluded_count']:,}건을 후보군으로 되돌렸습니다.",
        result.copy(),
    )
    background_tasks.add_task(
        rebuild_error_rules_background,
        request.job_id or "current",
        "exclude",
    )
    return result


@app.post("/api/admin/errors/restore")
def restore_confirmed_errors(
    request: ErrorRowsRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    analysis_locked = False
    try:
        if not ANALYSIS_LOCK.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail="데이터 분석이 실행 중입니다. 완료 후 다시 시도해 주세요.",
            )
        analysis_locked = True
        order_numbers = CURRENT_JOB.confirmed_order_numbers(
            request.job_id,
            request.row_ids,
            required_status="제외",
        )
        result = DASHBOARD_STORE.restore_error_orders(
            order_numbers,
            job_id=request.job_id or "current",
            restored_at=datetime.now().astimezone().isoformat(
                timespec="microseconds"
            ),
        )
        try:
            result["classification"] = CURRENT_JOB.sync_active_views(
                request.job_id
            )
        except Exception:
            DASHBOARD_STORE.exclude_error_orders(
                order_numbers,
                job_id=request.job_id or "current",
                excluded_at=datetime.now().astimezone().isoformat(
                    timespec="microseconds"
                ),
            )
            CURRENT_JOB.sync_active_views(request.job_id)
            raise
    except HTTPException:
        raise
    except (IndexError, LookupError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (KeyError, ValueError, sqlite3.IntegrityError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"제외 오생성 복구에 실패했습니다: {error}",
        ) from error
    finally:
        if analysis_locked:
            ANALYSIS_LOCK.release()
    result["notification"] = EVENTS.publish(
        "confirmed_errors_restored",
        f"제외 오생성 {result['restored_count']:,}건을 다시 반영했습니다.",
        result.copy(),
    )
    background_tasks.add_task(
        rebuild_error_rules_background,
        request.job_id or "current",
        "restore",
    )
    return result


@app.get("/api/service-order/filters")
@app.get("/api/service_order/filters")
def dashboard_filters() -> dict[str, object]:
    return DASHBOARD_STORE.filters()


@app.get("/api/service-order/overview")
@app.get("/api/service_order/overview")
def dashboard_overview(
    scope: Literal["person", "business"] = "business",
    person: str | None = None,
    business: str | None = None,
    time_mode: Literal["year", "month", "range"] = "year",
    year: int | None = Query(None, ge=2000, le=2200),
    month: int | None = Query(None, ge=1, le=12),
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(10, ge=1, le=100),
) -> dict[str, object]:
    try:
        overview = DASHBOARD_STORE.overview(
            scope=scope,
            person=person,
            business=business,
            time_mode=time_mode,
            year=year,
            month=month,
            start=start,
            end=end,
            limit=limit,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    export_parameters = {
        "scope": scope,
        "time_mode": time_mode,
        "year": year,
        "month": month,
        "start": start,
        "end": end,
        "person": person,
        "business": business,
    }
    export_query = urlencode(
        {
            key: value
            for key, value in export_parameters.items()
            if value is not None and value != ""
        }
    )
    overview["new_errors"]["download_url"] = (
        f"/api/service-order/new-errors/export?{export_query}"
    )
    current_job = CURRENT_JOB.status()
    current_period = current_job.get("period") if isinstance(current_job, dict) else None
    data_basis_date = (
        str(current_period.get("end") or "")
        if isinstance(current_period, dict)
        else ""
    ) or DASHBOARD_STORE.status().get("end")
    overview["data_basis_date"] = data_basis_date
    # The phrase "data: YYYY.MM.DD 기준" must consistently refer to the
    # latest uploaded source date, not only the newest approved-error row.
    if data_basis_date:
        overview["new_errors"]["as_of_date"] = data_basis_date
    overview["summary"]["total_count_definition"] = (
        "생성부서=사업부이며 상태가 오더취소가 아닌 전체 데이터"
    )
    overview["summary"]["error_rate_formula"] = (
        "확정 오생성 건수 / 전체 데이터 건수 * 100"
    )
    return overview


@app.get("/api/service-order/new-errors")
@app.get("/api/service_order/new-errors")
def list_new_errors(
    scope: Literal["person", "business"] = "business",
    person: str | None = None,
    business: str | None = None,
    time_mode: Literal["year", "month", "range"] = "year",
    year: int | None = Query(None, ge=2000, le=2200),
    month: int | None = Query(None, ge=1, le=12),
    start: str | None = None,
    end: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    search: str | None = Query(None, max_length=100),
    column_filters: str | None = Query(None, max_length=10000),
) -> dict[str, object]:
    del scope  # scope controls dashboard presentation, not detail-row filtering.
    parsed_filters = parse_column_filters(column_filters)
    try:
        frame, summary = DASHBOARD_STORE.new_error_details(
            time_mode=time_mode,
            year=year,
            month=month,
            start=start,
            end=end,
            person=person,
            business=business,
        )
        response = dataframe_page_payload(
            frame,
            page=page,
            page_size=page_size,
            search=search,
            column_filters=parsed_filters,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    export_parameters = {
        "time_mode": time_mode,
        "year": year,
        "month": month,
        "start": start,
        "end": end,
        "person": person,
        "business": business,
        "search": search,
        "column_filters": (
            json.dumps(
                parsed_filters,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if parsed_filters
            else None
        ),
    }
    export_query = urlencode(
        {
            key: value
            for key, value in export_parameters.items()
            if value is not None and value != ""
        }
    )
    response.update(
        {
            "summary": {
                **summary,
                "filtered_count": response["total"],
            },
            "download_url": (
                f"/api/service-order/new-errors/export?{export_query}"
            ),
        }
    )
    return response


@app.get("/api/service-order/new-errors/values")
@app.get("/api/service_order/new-errors/values")
def new_error_filter_values(
    column: str = Query(..., min_length=1, max_length=100),
    person: str | None = None,
    business: str | None = None,
    time_mode: Literal["year", "month", "range"] = "year",
    year: int | None = Query(None, ge=2000, le=2200),
    month: int | None = Query(None, ge=1, le=12),
    start: str | None = None,
    end: str | None = None,
    search: str | None = Query(None, max_length=100),
    limit: int = Query(200, ge=1, le=200),
) -> dict[str, object]:
    try:
        frame, summary = DASHBOARD_STORE.new_error_details(
            time_mode=time_mode,
            year=year,
            month=month,
            start=start,
            end=end,
            person=person,
            business=business,
        )
        response = distinct_column_values(
            frame,
            column=column,
            search=search,
            limit=limit,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    response["period_count"] = summary["count"]
    return response


@app.get("/api/service-order/new-errors/export")
@app.get("/api/service_order/new-errors/export")
def export_new_errors(
    scope: Literal["person", "business"] = "business",
    person: str | None = None,
    business: str | None = None,
    time_mode: Literal["year", "month", "range"] = "year",
    year: int | None = Query(None, ge=2000, le=2200),
    month: int | None = Query(None, ge=1, le=12),
    start: str | None = None,
    end: str | None = None,
    search: str | None = Query(None, max_length=100),
    column_filters: str | None = Query(None, max_length=10000),
) -> FileResponse:
    del scope  # scope controls presentation; person/business carry row filtering.
    parsed_filters = parse_column_filters(column_filters)
    try:
        frame, summary = DASHBOARD_STORE.new_error_details(
            time_mode=time_mode,
            year=year,
            month=month,
            start=start,
            end=end,
            person=person,
            business=business,
        )
        selected_ids = filtered_row_ids(
            frame,
            search=search,
            column_filters=parsed_filters,
        )
        frame = frame.iloc[selected_ids].copy().reset_index(drop=True)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if frame.empty:
        raise HTTPException(
            status_code=404,
            detail="선택한 조건에 새로 승인된 오생성 데이터가 없습니다.",
        )

    for column in find_date_columns(frame.columns):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    TEMP_EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    path = TEMP_EXPORT_ROOT / f"{token}_new_errors.xlsx"
    save_formatted_excel(frame, path, "오생성")
    download_name = (
        f"{str(summary['start']).replace('-', '')}_"
        f"{str(summary['end']).replace('-', '')}_new_errors.xlsx"
    )
    return FileResponse(
        path,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        filename=download_name,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@app.get("/api/events")
@app.get("/api/service-order/events")
@app.get("/api/service_order/events")
async def event_stream(
    request: Request,
    last_event_id_header: str | None = Header(None, alias="Last-Event-ID"),
    after: int = Query(0, ge=0),
) -> StreamingResponse:
    try:
        initial_id = max(after, int(last_event_id_header or 0))
    except ValueError:
        initial_id = after

    async def generate():
        current_id = initial_id
        yield "retry: 3000\n\n"
        heartbeat = 0
        while not await request.is_disconnected():
            events = EVENTS.after(current_id)
            if events:
                for event in events:
                    current_id = int(event["id"])
                    payload = json.dumps(event, ensure_ascii=False)
                    yield (
                        f"id: {current_id}\n"
                        f"event: {event['type']}\n"
                        f"data: {payload}\n\n"
                    )
                heartbeat = 0
            else:
                heartbeat += 1
                if heartbeat >= 15:
                    yield ": keep-alive\n\n"
                    heartbeat = 0
            await asyncio.sleep(1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/notifications/latest")
@app.get("/api/service-order/notifications/latest")
@app.get("/api/service_order/notifications/latest")
def latest_notification() -> dict[str, object]:
    return {"notification": EVENTS.latest()}


@app.get("/downloads/{job_id}/{filename}")
def download(job_id: str, filename: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(status_code=404, detail="결과 파일을 찾을 수 없습니다.")
    safe_name = Path(filename).name
    if safe_name != filename or Path(safe_name).suffix.lower() != ".xlsx":
        raise HTTPException(status_code=404, detail="결과 파일을 찾을 수 없습니다.")

    job_dir = (WEB_OUTPUT_ROOT / job_id).resolve()
    file_path = (job_dir / safe_name).resolve()
    if file_path.parent != job_dir or not file_path.is_file():
        raise HTTPException(status_code=404, detail="결과 파일을 찾을 수 없습니다.")
    return FileResponse(
        file_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=safe_name,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="CENTER Dashboard server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    uvicorn.run(app, host=arguments.host, port=arguments.port, log_level="info")


if __name__ == "__main__":
    main()
