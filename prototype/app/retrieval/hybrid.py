"""하이브리드 검색과 근거 충분성 게이트 (설계서 §4.5·§4.6·§4.7)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from app import config
from app.indexing.chunker import Chunk, chunk_documents
from app.indexing.loader import load_documents
from app.retrieval import acl, faq
from app.retrieval.bm25 import BM25, minmax_normalize
from app.retrieval.embedder import EmbeddingBackend, build_embedder
from app.retrieval.tokenizer_ko import STOPWORDS, bigrams, content_terms

# 종결어미로 끝나는 토큰은 주제어가 아니다. "궁금해요", "싶어요"를 미지 주제로 오인하면
# 정상적인 존댓말 질문이 통째로 막힌다.
PREDICATE_ENDINGS = ("요", "다", "까", "죠", "지")
MIN_SUBJECT_LEN = 3


@dataclass
class Candidate:
    chunk: Chunk
    bm25_score: float
    bm25_norm: float
    cosine: float
    hybrid_score: float

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk.chunk_id,
            "doc_id": self.chunk.doc_id,
            "title": self.chunk.title,
            "version": self.chunk.doc_version,
            "section_path": self.chunk.section_path,
            "text": self.chunk.text,
            "bm25_score": round(self.bm25_score, 4),
            "bm25_norm": round(self.bm25_norm, 4),
            "cosine": round(self.cosine, 4),
            "hybrid_score": round(self.hybrid_score, 4),
            "demo_assumption": self.chunk.demo_assumption,
        }


@dataclass
class SearchResult:
    query: str
    candidates: list[Candidate]
    filtered_out: int
    acl_blocked: bool
    evidence_sufficient: bool
    gate_signals: dict
    faq: object | None = None      # FaqEntry — 고신뢰 FAQ 매칭 결과 (§4.8)
    faq_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "candidates": [c.to_dict() for c in self.candidates],
            "filtered_out": self.filtered_out,
            "evidence_sufficient": self.evidence_sufficient,
            "gate_signals": self.gate_signals,
        }


class Retriever:
    def __init__(self, embedder: EmbeddingBackend | None = None):
        self.documents = load_documents()
        self.chunks: list[Chunk] = chunk_documents(self.documents)
        self.docs_by_id = {d.doc_id: d for d in self.documents}

        corpus = [c.embed_text for c in self.chunks]
        self.bm25 = BM25(corpus)

        self.faq_entries = faq.build_index(self.documents, self.chunks)

        self.embedder = embedder or build_embedder()
        if self.embedder.available:
            self.embedder.fit_corpus(corpus)
            self.matrix = self.embedder.encode_documents(corpus)
        else:
            self.matrix = np.zeros((len(self.chunks), 1), dtype=np.float32)

    @property
    def vector_bytes(self) -> int:
        return int(getattr(self.matrix, "nbytes", 0))

    def unknown_subjects(self, query: str) -> list[str]:
        """지식베이스에 아예 없는 주제어를 찾는다 (§4.7).

        코사인은 질의 전체와의 **평균적** 유사도라, 질문이 길고 정중할수록 부수 어휘
        (회사·복지·지원금·지급)가 점수를 끌어올려 정작 없는 주제를 덮어버린다.
        "회사 복지 중 사내 동호회 지원금 지급일이 알고 싶어요"가 경조사 지원 내용으로
        답해진 것이 그 사례다 — 같은 질문을 짧게 던지면 정상적으로 막혔다.
        즉 자연스럽게 물을수록 게이트가 약해지는, 방향이 거꾸로 된 결함이었다.

        한국어는 조사·어미가 붙어 어절 단위 대조가 어긋나므로 **bi-gram 단위**로 본다.
        "지급일"은 `지급`이 코퍼스에 있어 통과하고, "동호회"는 `동호`·`호회` 어느 것도
        없어 미지 주제로 판정된다.
        """
        out: list[str] = []
        for term in content_terms(query):
            if len(term) < MIN_SUBJECT_LEN or term in STOPWORDS:
                continue
            if term.endswith(PREDICATE_ENDINGS):
                continue
            grams = bigrams(term)
            if grams and not any(f"__{g}" in self.bm25.idf for g in grams):
                out.append(term)
        return out

    @property
    def alpha(self) -> float:
        # 임베딩이 없으면 BM25 단독으로 동작한다.
        return 1.0 if not self.embedder.available else config.HYBRID_ALPHA

    def search(
        self,
        query: str,
        user: dict,
        today: date | None = None,
        top_k: int | None = None,
    ) -> SearchResult:
        top_k = top_k or config.CONTEXT_TOP_K
        n = len(self.chunks)
        if n == 0:
            return SearchResult(query, [], 0, False, False, {})

        bm25_scores = self.bm25.score_all(query)
        bm25_top = sorted(range(n), key=lambda i: bm25_scores[i], reverse=True)[
            : config.CANDIDATE_TOP_N
        ]

        if self.embedder.available:
            qvec = self.embedder.encode_query(query)
            cosines = (self.matrix @ qvec).astype(float).tolist()
            vec_top = sorted(range(n), key=lambda i: cosines[i], reverse=True)[
                : config.CANDIDATE_TOP_N
            ]
        else:
            cosines = [0.0] * n
            vec_top = []

        pool = sorted(set(bm25_top) | set(vec_top))

        # [5] 1차 권한 필터 — 결합 전에 적용한다 (§4.6)
        pool_chunks = [self.chunks[i] for i in pool]
        allowed_chunks, filtered_out, acl_blocked = acl.filter_chunks(
            pool_chunks, user, today
        )
        allowed_ids = {c.chunk_id for c in allowed_chunks}
        pool = [i for i in pool if self.chunks[i].chunk_id in allowed_ids]
        if not pool:
            return SearchResult(
                query, [], filtered_out, acl_blocked, False,
                {"cos_top1": 0.0, "hybrid_top1": 0.0, "citable_chunks": 0},
            )

        pool_bm25_norm = minmax_normalize([bm25_scores[i] for i in pool])
        alpha = self.alpha
        scored: list[Candidate] = []
        for rank, i in enumerate(pool):
            cos = max(0.0, cosines[i])
            hybrid = alpha * pool_bm25_norm[rank] + (1 - alpha) * cos
            scored.append(
                Candidate(self.chunks[i], bm25_scores[i], pool_bm25_norm[rank], cos, hybrid)
            )
        scored.sort(key=lambda c: c.hybrid_score, reverse=True)

        # 한 문서가 결과를 독점하지 않도록 제한하고(§4.5),
        # 1위 대비 상대 점수가 낮은 후보는 인용에서 제외한다.
        #
        # 단 상대 점수 컷은 **다른 문서에만** 적용한다. 1위 문서는 이미 정답 문서로
        # 판정된 것이므로, 그 문서의 다른 절은 점수가 낮아도 맥락으로 포함한다.
        # (이 컷을 문서 구분 없이 적용하면 "무엇을 제출하나요"에 FAQ 절만 남고
        #  정작 제출물 목록이 있는 절이 잘려 나간다.)
        best = scored[0].hybrid_score if scored else 0.0
        top_doc = scored[0].chunk.doc_id if scored else None
        floor = best * config.REL_SCORE_CUTOFF
        selected: list[Candidate] = []
        per_doc: dict[str, int] = {}
        for cand in scored:
            same_doc = cand.chunk.doc_id == top_doc
            if selected and not same_doc and cand.hybrid_score < floor:
                continue
            used = per_doc.get(cand.chunk.doc_id, 0)
            if used >= config.MAX_CHUNKS_PER_DOC:
                continue
            per_doc[cand.chunk.doc_id] = used + 1
            selected.append(cand)
            if len(selected) >= top_k:
                break

        signals = {
            "cos_top1": round(max((c.cosine for c in selected), default=0.0), 4),
            "hybrid_top1": round(selected[0].hybrid_score if selected else 0.0, 4),
            "citable_chunks": len(selected),
            "unknown_subjects": self.unknown_subjects(query),
        }
        sufficient = self._gate(signals)

        # §4.8 — FAQ 고신뢰 매칭. 질문끼리 사실상 일치하면 점수 게이트보다 강한 근거다.
        # 다만 미지 주제어(G5)는 어떤 경우에도 뒤집지 않는다.
        hit = None if signals["unknown_subjects"] else faq.match(query, self.faq_entries)
        faq_entry, faq_score = (None, 0.0)
        if hit is not None:
            entry, score = hit
            # FAQ도 청크와 똑같이 권한을 통과해야 한다. 여기를 빼면 부서 전용 문서의
            # FAQ가 검색 필터를 우회해 새어 나간다.
            if acl.is_visible(entry.chunk, user, today):
                faq_entry, faq_score = entry, score
                signals["faq_score"] = score
                if not any(c.chunk.chunk_id == entry.chunk.chunk_id for c in selected):
                    selected.insert(0, Candidate(entry.chunk, 0.0, 0.0, 0.0, 0.0))
                    signals["citable_chunks"] = len(selected)
                sufficient = True

        return SearchResult(query, selected, filtered_out, acl_blocked, sufficient,
                            signals, faq_entry, faq_score)

    def _gate(self, signals: dict) -> bool:
        """§4.7 — 모든 조건을 만족해야 답변을 생성한다."""
        if signals["citable_chunks"] < config.GATE_MIN_CITABLE:
            return False
        # 질문의 주제어가 지식베이스에 아예 없으면 점수와 무관하게 답하지 않는다.
        # 점수는 "얼마나 비슷한가"를 재지만, 이 검사는 "그 주제가 있기는 한가"를 묻는다.
        if signals.get("unknown_subjects"):
            return False
        if signals["hybrid_top1"] < config.GATE_HYBRID_TOP1:
            return False
        if self.embedder.available and signals["cos_top1"] < config.GATE_COS_TOP1:
            return False
        return True
