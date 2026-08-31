"""한국어 토큰화 (설계서 §4.2). 형태소 분석기 없이 조사 제거 + bi-gram으로 처리한다."""
from __future__ import annotations

import re
import unicodedata

# 긴 것부터 매칭한다 (§4.2 단계 3)
JOSA = [
    "에서는", "에게서", "으로는", "이라고", "라고는", "에서도", "까지는", "부터는",
    "에서", "에게", "으로", "라도", "이나", "까지", "부터", "보다", "처럼", "한테",
    "에는", "만큼", "조차", "마저",
    "은", "는", "이", "가", "을", "를", "에", "와", "과", "의", "도", "만", "로", "께", "뿐",
]

# 용언 어미. §4.2가 "조사·어미 제거"를 명시하므로 어미도 함께 벗긴다.
# 조사보다 먼저 적용한다 — "신청하나요" 같은 형태는 조사 규칙으로는 잡히지 않는다.
EOMI = [
    "해야 합니다", "하시기 바랍니다", "알려주세요", "해주세요", "되나요", "하나요",
    "인가요", "은가요", "습니까", "합니까", "했는데", "하는데", "되는데",
    "합니다", "됩니다", "입니다", "하세요", "주세요", "이에요", "예요",
    "해서", "해도", "하면", "되면", "하고", "하며", "하지", "해야", "하여",
    "하는", "되는", "한", "된", "할", "될", "함", "됨", "히", "게",
]

STOPWORDS = {
    "그", "저", "이것", "그것", "어떻게", "무엇", "인가요", "하나요", "있나요",
    "됩니다", "합니다", "입니다", "건가요", "건지", "어디", "언제", "누구", "왜",
    "수", "것", "때", "등", "및", "또는", "그리고", "하는", "되는", "대한", "관한",
}

WORD_SPLIT_RE = re.compile(r"[^0-9A-Za-z가-힣]+")
MIN_STEM_LEN = 2


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


def strip_josa(word: str) -> str:
    """어미를 먼저, 그다음 조사를 1회씩 제거한다.

    제거 후 2자 미만이 되면 그 제거는 적용하지 않는다 ("이가" → "이"는 무의미).
    """
    for suffix in EOMI:
        if word.endswith(suffix) and len(word) - len(suffix) >= MIN_STEM_LEN:
            word = word[: -len(suffix)]
            break
    for suffix in JOSA:
        if word.endswith(suffix) and len(word) - len(suffix) >= MIN_STEM_LEN:
            return word[: -len(suffix)]
    return word


def bigrams(word: str) -> list[str]:
    if len(word) < 2:
        return []
    return [word[i:i + 2] for i in range(len(word) - 1)]


def tokenize(text: str, include_bigrams: bool = True) -> list[str]:
    """색인·질의 공통 토큰화.

    원본 어절과 조사 제거 어간을 둘 다 색인한다. 어느 쪽이 맞는지 모르므로
    양쪽에 걸어둔다 (§4.2 단계 4).
    """
    words = [w for w in WORD_SPLIT_RE.split(normalize(text)) if w]
    tokens: list[str] = []
    for word in words:
        if len(word) < 2:
            continue
        stem = strip_josa(word)
        # 어간 색인에서만 불용어를 제외한다 (§4.2 단계 6)
        if word not in STOPWORDS:
            tokens.append(word)
        if stem != word and stem not in STOPWORDS:
            tokens.append(stem)
        if include_bigrams:
            tokens.extend(f"__{bg}" for bg in bigrams(word))
    return tokens


def content_terms(text: str) -> list[str]:
    """멀티턴 재작성용 명사 후보 (§9.2). 불용어·조사 제거 후 2자 이상."""
    words = [w for w in WORD_SPLIT_RE.split(normalize(text)) if w]
    out: list[str] = []
    for word in words:
        stem = strip_josa(word)
        if len(stem) >= MIN_STEM_LEN and stem not in STOPWORDS and stem not in out:
            out.append(stem)
    return out
