from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from difflib import SequenceMatcher
import math
import re


TOKEN_CLEAN_PATTERN = re.compile(r"[^가-힣a-z]+")
MAX_SPACING_SPLIT_TOKENS = 2
MAX_GAP_TOKENS = 6
GENERIC_SIMILARITY_TOKENS = frozenset(
    {
        "고객",
        "요청",
        "확인",
        "안내",
        "처리",
        "완료",
        "예정",
        "문의",
        "통화",
        "전화",
        "방문",
        "접수",
        "오더",
        "사항",
        "내용",
        "관련",
        "드립니다",
        "바랍니다",
        "부탁드립니다",
        "해주세요",
        "전화번호값",
        "날짜값",
        "금액값",
        "기간값",
        "수치값",
        "성명값",
        "주소값",
    }
)


def compact_token(value: object) -> str:
    return TOKEN_CLEAN_PATTERN.sub("", str(value).lower())


def ordered_distinct_token_match(
    signature: object,
    phrase: Iterable[object],
) -> bool:
    """Match ordered phrase anchors without reusing one signature token.

    A phrase anchor may span at most two adjacent tokens to tolerate spacing
    differences. Separate anchors must consume separate signature tokens and
    may have only a bounded gap. This keeps useful ordered-gap matching while
    preventing one long compound word from satisfying every anchor.
    """
    signature_tokens = [
        compact_token(token) for token in str(signature).split()
    ]
    signature_tokens = [token for token in signature_tokens if token]
    phrase_tokens = [compact_token(token) for token in phrase]
    phrase_tokens = [token for token in phrase_tokens if token]
    if not signature_tokens or not phrase_tokens:
        return False

    next_start = 0
    for phrase_index, target in enumerate(phrase_tokens):
        if phrase_index == 0:
            last_start = len(signature_tokens) - 1
        else:
            last_start = min(
                len(signature_tokens) - 1,
                next_start + MAX_GAP_TOKENS,
            )

        matched_end: int | None = None
        for start in range(next_start, last_start + 1):
            combined = ""
            last_end = min(
                len(signature_tokens),
                start + MAX_SPACING_SPLIT_TOKENS,
            )
            for end in range(start, last_end):
                combined += signature_tokens[end]
                if target in combined:
                    matched_end = end
                    break
            if matched_end is not None:
                break
        if matched_end is None:
            return False
        next_start = matched_end + 1
    return True


def compact_signature(value: object) -> str:
    return "".join(compact_token(token) for token in str(value).split())


def character_bigram_counter(value: object) -> Counter[str]:
    text = compact_signature(value)
    if not text:
        return Counter()
    if len(text) == 1:
        return Counter({text: 1})
    return Counter(text[index : index + 2] for index in range(len(text) - 1))


def counter_cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(left[key] * right[key] for key in left.keys() & right.keys())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def text_similarity(left: object, right: object) -> float:
    """Blend character bigram cosine and deterministic edit similarity."""
    left_text = compact_signature(left)
    right_text = compact_signature(right)
    if not left_text or not right_text:
        return 0.0
    bigram_score = counter_cosine(
        character_bigram_counter(left_text),
        character_bigram_counter(right_text),
    )
    sequence_score = SequenceMatcher(
        None,
        left_text,
        right_text,
        autojunk=False,
    ).ratio()
    return 0.55 * bigram_score + 0.45 * sequence_score


def informative_token_count(value: object) -> int:
    tokens = {
        compact_token(token)
        for token in str(value).split()
        if compact_token(token)
    }
    return sum(
        token not in GENERIC_SIMILARITY_TOKENS and len(token) >= 2
        for token in tokens
    )
