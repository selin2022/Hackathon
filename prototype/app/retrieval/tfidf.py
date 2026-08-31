"""TF-IDF 벡터화 (설계서 §4.4).

scikit-learn을 쓰지 않고 직접 구현한다. 이유는 두 가지다.

1. sklearn은 scipy를 끌고 와 배포 용량이 100MB를 넘는다. 서버리스 배포에 부담이 크다.
2. TF-IDF 자체가 단순하고, 한국어 토큰화를 이미 직접 통제하고 있어 얻는 것이 없다.

희소 표현을 유지하므로 청크 수가 늘어도 메모리가 선형으로만 증가한다.
"""
from __future__ import annotations

import math
from collections import Counter

import numpy as np


class SparseVector:
    """{term_index: weight} 형태의 L2 정규화된 희소 벡터."""

    __slots__ = ("weights",)

    def __init__(self, weights: dict[int, float]):
        norm = math.sqrt(sum(w * w for w in weights.values()))
        if norm > 0:
            weights = {i: w / norm for i, w in weights.items()}
        self.weights = weights

    def dot(self, other: "SparseVector") -> float:
        # 항이 적은 쪽을 순회한다.
        a, b = (self.weights, other.weights)
        if len(a) > len(b):
            a, b = b, a
        return sum(w * b.get(i, 0.0) for i, w in a.items())


class SparseMatrix:
    """행이 SparseVector인 행렬. `matrix @ query_vector`로 전체 유사도를 얻는다."""

    __slots__ = ("rows", "shape", "nbytes", "dtype")

    def __init__(self, rows: list[SparseVector], vocab_size: int):
        self.rows = rows
        self.shape = (len(rows), vocab_size)
        nnz = sum(len(r.weights) for r in rows)
        # 인덱스(int64) + 값(float64) 기준 개략 크기
        self.nbytes = nnz * 16
        self.dtype = "sparse-float64"

    def __matmul__(self, query: SparseVector) -> np.ndarray:
        return np.fromiter(
            (row.dot(query) for row in self.rows), dtype=np.float64, count=len(self.rows)
        )

    def __len__(self) -> int:
        return len(self.rows)


class TfidfVectorizer:
    """학습 코퍼스 기준 TF-IDF.

    idf 평활화는 scikit-learn 기본값과 같은 형태를 쓴다:
        idf(t) = ln((1 + n) / (1 + df(t))) + 1
    """

    def __init__(self, tokenizer):
        self._tokenize = tokenizer
        self.vocab: dict[str, int] = {}
        self.idf: list[float] = []
        self._fitted = False

    def fit(self, texts: list[str]) -> "TfidfVectorizer":
        df: Counter[str] = Counter()
        for text in texts:
            df.update(set(self._tokenize(text)))

        n = len(texts)
        self.vocab = {term: i for i, term in enumerate(sorted(df))}
        self.idf = [0.0] * len(self.vocab)
        for term, index in self.vocab.items():
            self.idf[index] = math.log((1 + n) / (1 + df[term])) + 1.0
        self._fitted = True
        return self

    def transform_one(self, text: str) -> SparseVector:
        if not self._fitted:
            raise RuntimeError("fit을 먼저 호출해야 합니다.")
        counts = Counter(self._tokenize(text))
        weights: dict[int, float] = {}
        for term, count in counts.items():
            index = self.vocab.get(term)
            if index is None:  # 학습 코퍼스에 없는 용어는 버린다
                continue
            weights[index] = count * self.idf[index]
        return SparseVector(weights)

    def transform(self, texts: list[str]) -> SparseMatrix:
        return SparseMatrix([self.transform_one(t) for t in texts], len(self.vocab))
