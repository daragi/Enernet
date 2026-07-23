from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import sys

import pandas as pd


CENTER_DIR = Path(__file__).resolve().parents[1]
if str(CENTER_DIR) not in sys.path:
    sys.path.insert(0, str(CENTER_DIR))

from dashboard_server import (  # noqa: E402
    dataframe_page_payload,
    distinct_column_values,
    filtered_row_ids,
    retain_review_scope,
)
from service_order.error.privacy import (  # noqa: E402
    mask_detail_text,
    mask_personal_data_frame,
)
from service_order.error.analysis_pipeline import save_formatted_excel  # noqa: E402
from service_order.service_order_store import DashboardStore  # noqa: E402


def test_detail_masking_preserves_work_wording() -> None:
    assert mask_detail_text(
        "납입자번호 문의 010-7736-7599 김순임"
    ) == "납입자번호 문의 <전화번호> <성명>"
    assert mask_detail_text(
        "고객명오등록 VO YIEAN RACHS → VO XUAN BACH",
        "고객정보 추가/수정",
    ) == "고객명오등록 <성명> → <성명>"
    assert mask_detail_text("정상 검침 507", "검침") == "정상 검침 507"


def test_address_columns_are_fully_masked() -> None:
    source = pd.DataFrame(
        {
            "내역": ["정상 처리", ""],
            "주소": ["서울시 중구 1", None],
            "구주소": ["중구동 2", ""],
        }
    )
    masked, summary = mask_personal_data_frame(source)

    assert masked["주소"].iloc[0] == "***"
    assert pd.isna(masked["주소"].iloc[1])
    assert masked["구주소"].tolist() == ["***", ""]
    assert summary["주소마스킹건수"] == 2


def test_download_workbook_masks_address_columns() -> None:
    source = pd.DataFrame(
        {
            "오더번호": [28820625],
            "주소": ["대전광역시 원본 주소"],
            "구주소": ["대전시 원본 구주소"],
            "내역": ["대용량 계량기 점검"],
        }
    )
    with TemporaryDirectory() as directory:
        path = Path(directory) / "masked.xlsx"
        save_formatted_excel(source, path, "오생성")
        downloaded = pd.read_excel(path)

    assert downloaded.loc[0, "주소"] == "***"
    assert downloaded.loc[0, "구주소"] == "***"


def test_store_masks_auto_and_manual_payloads_before_persisting() -> None:
    frame = pd.DataFrame(
        {
            "오더번호": ["A", "B"],
            "오더생성일": [pd.Timestamp("2026-07-10")] * 2,
            "생성인": ["담당자"] * 2,
            "사업부": ["북부"] * 2,
            "소분류": ["계량기일반", "고객정보 추가/수정"],
            "내역": [
                "점검요청 010-1111-2222 김하늘",
                "성명: 홍길동 연락처 042-123-4567",
            ],
        }
    )
    now = datetime.now().astimezone().isoformat(timespec="microseconds")
    with TemporaryDirectory() as directory:
        path = Path(directory) / "metrics.sqlite3"
        store = DashboardStore(path)
        store.replace_totals_and_auto_errors(
            frame,
            frame.iloc[[0]],
            batch_id="auto-mask",
            job_id="mask-job",
            source_name="mask.xlsx",
            approved_at=now,
        )
        store.approve_error_batch(
            batch_id="manual-mask",
            job_id="mask-job",
            source_name="mask.xlsx",
            approved_at=now,
            records=[
                {
                    "candidate_row_id": 1,
                    "order_number": "B",
                    "order_date": "2026-07-10",
                    "person": "담당자",
                    "business": "북부",
                    "subcategory": "고객정보 추가/수정",
                    "payload": frame.iloc[1].to_dict(),
                }
            ],
        )

        connection = sqlite3.connect(path)
        try:
            stored_payloads = [
                json.loads(row[0])["내역"]
                for row in connection.execute(
                    "SELECT payload_json FROM error_approval_details ORDER BY detail_id"
                )
            ]
        finally:
            connection.close()
        details, _ = store.new_error_details(
            time_mode="month",
            year=2026,
            month=7,
            start=None,
            end=None,
            person=None,
            business=None,
        )

    assert all("010-" not in value and "042-" not in value for value in stored_payloads)
    assert stored_payloads[0] == "점검요청 <전화번호> <성명>"
    assert stored_payloads[1] == "성명: <성명> 연락처 <전화번호>"
    assert details["내역"].tolist() == stored_payloads


def test_excel_filter_dates_are_compact_and_multi_selectable() -> None:
    frame = pd.DataFrame(
        {
            "고객방문일": [
                "2026-07-09T00:00:00",
                "2026-07-10T00:00:00",
                "2026-07-11T00:00:00",
            ]
        }
    )
    values = distinct_column_values(
        frame,
        column="고객방문일",
        search=None,
        limit=200,
    )
    selected = filtered_row_ids(
        frame,
        search=None,
        column_filters={"고객방문일": ["2026-07-09", "2026-07-11"]},
    )

    assert values["values"] == ["2026-07-09", "2026-07-10", "2026-07-11"]
    assert selected == [0, 2]


def test_order_number_is_integer_in_grid_and_filter_values() -> None:
    frame = pd.DataFrame({"오더번호": [28812757.0, 28812758.0]})
    values = distinct_column_values(
        frame, column="오더번호", search=None, limit=200
    )
    page = dataframe_page_payload(
        frame,
        page=1,
        page_size=100,
        search=None,
        column_filters={"오더번호": ["28812757"]},
    )

    assert values["values"] == ["28812757", "28812758"]
    assert page["rows"][0]["오더번호"] == 28812757


def test_masking_cannot_promote_a_prior_normal_row_to_candidate() -> None:
    classified = pd.DataFrame(
        {"오더번호": ["existing-review", "masking-only-review"]}
    )
    retained = retain_review_scope(classified, {"existing-review"})

    assert retained["오더번호"].tolist() == ["existing-review"]
