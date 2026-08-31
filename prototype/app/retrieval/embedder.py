"""임베딩 백엔드 (설계서 §4.4).

온프렘 전환 경계 중 하나다 (§1.3). 구현체를 교체하면 사내 임베딩 서버로 옮겨간다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np

from app import config
from app.retrieval.tfidf import SparseMatrix, SparseVector, TfidfVectorizer
from app.retrieval.tokenizer_ko import tokenize


class EmbeddingBackend(ABC):
    name: str = "base"
    available: bool = True

    def fit_corpus(self, texts: list[str]) -> None:
        """코퍼스 학습이 필요한 백엔드만 사용한다. 사전학습 모델은 no-op."""

    @abstractmethod
    def encode_documents(self, texts: list[str]): ...

    @abstractmethod
    def encode_query(self, text: str): ...


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


class TfidfHeadingEmbedder(EmbeddingBackend):
    """섹션 헤딩을 가중한 TF-IDF 코사인. 소규모 코퍼스의 기본 백엔드.

    골든셋 실측 결과 SVD 축소는 43청크 규모에서 잡음만 더했고(정상/비정상 분리 실패),
    헤딩을 반복해 가중한 순수 TF-IDF가 완전 분리를 달성했다. 문서의 절 제목이
    그 절의 주제를 가장 압축적으로 담고 있기 때문이다.

    외부 모델을 받지 않으므로 폐쇄망에서도 그대로 동작하고, 배포 의존성이 numpy뿐이다.
    """

    name = "tfidf_heading"

    def __init__(self, heading_repeat: int | None = None):
        self._repeat = heading_repeat or config.HEADING_REPEAT
        self._vectorizer = TfidfVectorizer(tokenizer=lambda t: tokenize(t))

    def _weight_heading(self, text: str) -> str:
        if self._repeat <= 1:
            return text
        heading = text.split("\n", 1)[0]
        return "\n".join([heading] * self._repeat) + "\n" + text

    def fit_corpus(self, texts: list[str]) -> None:
        self._vectorizer.fit([self._weight_heading(t) for t in texts])

    def encode_documents(self, texts: list[str]) -> SparseMatrix:
        return self._vectorizer.transform([self._weight_heading(t) for t in texts])

    def encode_query(self, text: str) -> SparseVector:
        return self._vectorizer.transform_one(text)


class E5Embedder(EmbeddingBackend):
    """multilingual-e5 계열. 프리픽스가 필수다 (§4.4).

    sentence-transformers가 설치된 환경에서만 사용한다.
    """

    name = "e5"

    def __init__(self, model_name: str | None = None):
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name or config.EMBEDDING_MODEL)

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        vecs = self._model.encode(
            [f"passage: {t}" for t in texts], normalize_embeddings=True,
        )
        return np.asarray(vecs, dtype=np.float32)

    def encode_query(self, text: str) -> np.ndarray:
        vec = self._model.encode(f"query: {text}", normalize_embeddings=True)
        return np.asarray(vec, dtype=np.float32)


class NullEmbedder(EmbeddingBackend):
    """임베딩 불가 환경. 하이브리드가 BM25 단독으로 동작한다."""

    name = "none"
    available = False

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        return np.zeros((len(texts), 1), dtype=np.float32)

    def encode_query(self, text: str) -> np.ndarray:
        return np.zeros(1, dtype=np.float32)


def build_embedder(backend: str | None = None) -> EmbeddingBackend:
    backend = backend or config.EMBEDDING_BACKEND
    try:
        if backend == "e5":
            return E5Embedder()
        if backend == "tfidf_heading":
            return TfidfHeadingEmbedder()
    except Exception as exc:  # pragma: no cover - 환경 의존
        print(f"[embedder] '{backend}' 백엔드 초기화 실패 → BM25 단독으로 동작합니다: {exc}")
        return NullEmbedder()
    return NullEmbedder()
