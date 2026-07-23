from __future__ import annotations

import re
from typing import Any

import pandas as pd


PHONE_PLACEHOLDER = "<전화번호>"
NAME_PLACEHOLDER = "<성명>"
EMAIL_PLACEHOLDER = "<이메일>"
IDENTIFIER_PLACEHOLDER = "<식별번호>"
ADDRESS_PLACEHOLDER = "***"
ADDRESS_COLUMNS = ("주소", "구주소")


def _address_columns(columns: object) -> list[object]:
    """Resolve address columns while tolerating a display-space in the name."""
    return [
        column
        for column in columns
        if str(column).replace(" ", "") in ADDRESS_COLUMNS
    ]

PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?82[-.\s]?)?"
    r"(?:01[016789]|02|0[3-6]\d)[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)"
)
EMAIL_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9._%+-])[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}(?![a-z0-9._%+-])"
)
RESIDENT_ID_PATTERN = re.compile(r"(?<!\d)\d{6}[-.\s]?[1-8]\d{6}(?!\d)")
LABELED_NAME_PATTERN = re.compile(
    r"(?P<label>(?:고객명|성명|명의자|이름)"
    r"(?:\s*(?:변경|수정|오등록))?\s*(?::|=|은|는)?\s+)"
    r"(?P<name>[가-힣]{2,5}|[A-Za-z][A-Za-z .'’-]{1,60})",
    re.IGNORECASE,
)
ARROW_NAME_PATTERN = re.compile(
    r"(?P<left>[가-힣]{2,5}|[A-Za-z][A-Za-z .'’-]{1,50}?)"
    r"\s*(?P<arrow>→|->)\s*"
    r"(?P<right>[가-힣]{2,5}|[A-Za-z][A-Za-z .'’-]{1,50})"
)
AFTER_PHONE_NAME_PATTERN = re.compile(
    rf"(?P<prefix>{re.escape(PHONE_PLACEHOLDER)}\s*(?:(?:로|에서|:|=)\s*)?)"
    r"(?P<name>[가-힣]{2,5})"
)
KOREAN_SURNAMES = set(
    "김이박최정강조윤장임한오서신권황안송전홍유고문양손배백허남심노하곽성차주우구민류나진지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예봉사부가복태목형피두감음빈동온호범좌팽승간상시갈단"
)
NON_PERSON_WORDS = {
    "고객명",
    "성명",
    "명의자",
    "이름",
    "이상",
    "이하",
    "이전",
    "이후",
    "김장",
    "박스",
    "정상",
    "한도",
    "요금",
    "고객",
    "담당",
    "현장",
    "검침",
    "전입",
    "전출",
    "공급",
    "서비스",
    "시설",
    "설치",
    "문의",
    "요청",
    "변경",
    "수정",
    "연락처",
    "배우자",
    "대표자",
    "신청자",
    "요청자",
}
CUSTOMER_INFO_SUBCATEGORY = "고객정보 추가/수정"


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _looks_like_korean_name(value: str) -> bool:
    return bool(
        re.fullmatch(r"[가-힣]{2,5}", value)
        and value[0] in KOREAN_SURNAMES
        and value not in NON_PERSON_WORDS
    )


def _mask_adjacent_name(match: re.Match[str]) -> str:
    name = match.group("name")
    if not _looks_like_korean_name(name):
        return match.group(0)
    return f"{match.group('prefix')}{NAME_PLACEHOLDER}"


def mask_detail_text(value: object, subcategory: object = "") -> Any:
    """Mask high-confidence personal data while preserving business wording."""
    if _is_missing(value):
        return value
    text = str(value).strip()
    if not text:
        return text

    text = EMAIL_PATTERN.sub(EMAIL_PLACEHOLDER, text)
    text = RESIDENT_ID_PATTERN.sub(IDENTIFIER_PLACEHOLDER, text)
    text = PHONE_PATTERN.sub(PHONE_PLACEHOLDER, text)

    # Customer-information changes frequently express old/new names around an
    # arrow. Limit this broader rule to that subcategory to avoid masking
    # ordinary workflow words in other service orders.
    if str(subcategory).strip() == CUSTOMER_INFO_SUBCATEGORY:
        text = ARROW_NAME_PATTERN.sub(
            lambda match: (
                f"{NAME_PLACEHOLDER} {match.group('arrow')} {NAME_PLACEHOLDER}"
            ),
            text,
        )

    # Explicit labels are reliable regardless of service-order category.
    text = LABELED_NAME_PATTERN.sub(
        lambda match: f"{match.group('label')}{NAME_PLACEHOLDER}",
        text,
    )
    text = AFTER_PHONE_NAME_PATTERN.sub(_mask_adjacent_name, text)

    return text


def mask_personal_data_frame(
    frame: pd.DataFrame,
    *,
    copy: bool = True,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Mask personal details and address columns for every persisted/displayed frame."""
    result = frame.copy() if copy else frame
    changed = pd.Series(False, index=result.index, dtype=bool)
    masked_as_text = pd.Series("", index=result.index, dtype="string")

    if "내역" in result.columns:
        original = result["내역"].copy()
        subcategories = (
            result["소분류"]
            if "소분류" in result.columns
            else pd.Series("", index=result.index, dtype="string")
        )
        result["내역"] = [
            mask_detail_text(detail, subcategory)
            for detail, subcategory in zip(original, subcategories)
        ]
        masked_as_text = result["내역"].astype("string").fillna("")
        changed |= original.astype("string").fillna("").ne(masked_as_text)

    address_mask_count = 0
    for column in _address_columns(result.columns):
        mask = result[column].map(
            lambda value: (
                not _is_missing(value)
                and bool(str(value).strip())
                and str(value).strip() != ADDRESS_PLACEHOLDER
            )
        )
        address_mask_count += int(mask.sum())
        changed |= mask
        if bool(mask.any()):
            result[column] = result[column].astype("object")
            result.loc[mask, column] = ADDRESS_PLACEHOLDER

    return result, {
        "개인정보마스킹행수": int(changed.sum()),
        "전화번호마스킹건수": int(
            masked_as_text.str.count(re.escape(PHONE_PLACEHOLDER)).sum()
        ),
        "성명마스킹건수": int(
            masked_as_text.str.count(re.escape(NAME_PLACEHOLDER)).sum()
        ),
        "이메일마스킹건수": int(
            masked_as_text.str.count(re.escape(EMAIL_PLACEHOLDER)).sum()
        ),
        "식별번호마스킹건수": int(
            masked_as_text.str.count(re.escape(IDENTIFIER_PLACEHOLDER)).sum()
        ),
        "주소마스킹건수": address_mask_count,
    }


def mask_payload(payload: dict[str, object]) -> dict[str, object]:
    """Return a sanitized copy of one service-order payload."""
    result = dict(payload)
    subcategory = result.get("소분류", "")
    if "내역" in result:
        result["내역"] = mask_detail_text(result.get("내역"), subcategory)
    if "내역2" in result:
        result["내역2"] = mask_detail_text(result.get("내역2"), subcategory)
    for column in _address_columns(result.keys()):
        value = result.get(column)
        if not _is_missing(value) and str(value).strip():
            result[column] = ADDRESS_PLACEHOLDER
    return result
