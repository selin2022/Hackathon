"""BM25 (설계서 §4.3). 의존성 없이 직접 구현한다."""
from __future__ import annotations

import math
from collections import Counter

from app import config
from app.retrieval.tokenizer_ko import tokenize


class BM25:
    def __init__(self, corpus: list[str], k1: float | None = None, b: float | None = None):
        self.k1 = config.BM25_K1 if k1 is None else k1
        self.b = config.BM25_B if b is None else b
        self.docs: list[Counter[str]] = []
        self.doc_len: list[int] = []
        self.df: Counter[str] = Counter()

        for text in corpus:
            tokens = tokenize(text)
            tf = Counter(tokens)
            self.docs.append(tf)
            self.doc_len.append(len(tokens))
            self.df.update(tf.keys())

        self.n = len(self.docs)
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0
        self.idf: dict[str, float] = {}
        for term, freq in self.df.items():
            # BM25+ 계열의 음수 idf를 피하기 위한 표준 보정
            self.idf[term] = math.log(1 + (self.n - freq + 0.5) / (freq + 0.5))

    def score_all(self, query: str) -> list[float]:
        q_tokens = tokenize(query)
        scores = [0.0] * self.n
        if not q_tokens or not self.avgdl:
            return scores
        for term in q_tokens:
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i, tf in enumerate(self.docs):
                f = tf.get(term, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl)
                scores[i] += idf * (f * (self.k1 + 1)) / denom
        return scores


def minmax_normalize(scores: list[float]) -> list[float]:
    """후보군 내 min-max 정규화 → [0, 1] (§4.3)."""
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi - lo < 1e-12:
        return [0.0 if hi <= 0 else 1.0] * len(scores)
    return [(s - lo) / (hi - lo) for s in scores]
