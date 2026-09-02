"""FAQ 고신뢰 직접 매칭 (설계서 §4.8).

온보딩 문서의 `자주 묻는 질문` 절은 이미 **질문 → 답** 쌍이다. 사용자의 질문이 그중 하나와
사실상 같은 질문이면, 문장을 고르는 단계를 거칠 필요 없이 그 답을 그대로 쓰는 것이 정확하다.

일반 검색은 절 단위로 점수를 매기므로 FAQ 절이 불리하다. 절 제목이 항상 "자주 묻는 질문"이라
질의와 겹치는 토큰이 없기 때문이다(§11.4.1 교훈 7). 그래서 FAQ만 따로 색인해 질문끼리 대조한다.

**임계값을 높게 잡는 이유는 측정 때문이다.** 유사도를 0.28까지 낮추면
"퇴사하면 계정은 어떻게 되나요?"가 `검진을 받지 못하면 어떻게 되나요?`와 매칭됐다 — 주제가
전혀 다른데 문장 구조가 같아서다. 0.41에서는 "입사 서류로 무엇을 제출해야 하나요?"가 제출
**기한** FAQ에 걸려 오히려 기존 답보다 나빠졌다. 구조 유사도는 주제 일치를 뜻하지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app import config
from app.indexing.chunker import Chunk
from app.indexing.loader import Document
from app.retrieval.tokenizer_ko import STOPWORDS, content_terms, tokenize

FAQ_HEADING_RE = re.compile(r"^#{2,4}\s*자주\s*묻는\s*질문\s*$", re.M)
NEXT_HEADING_RE = re.compile(r"^#{1,4}\s+", re.M)
PAIR_RE = re.compile(r"\*\*(.+?)\*\*\s*\n(.+?)(?=\n\s*\n|\Z)", re.S)

# 주제어 판정에서 뺄 의문사 어간. 토크나이저가 어미를 벗기면서 "어떻게"→"어떻"이 되는데,
# 이것이 주제어로 잡히면 "어떻게 되나요" 구조만 같아도 매칭이 성립한다.
INTERROGATIVE_STEMS = {
    "어떻", "어떤", "무엇", "얼마", "며칠", "언제", "어디", "누구", "무슨", "몇",
}
PREDICATE_ENDINGS = ("요", "다", "까", "죠", "지")


@dataclass
class FaqEntry:
    doc_id: str
    question: str
    answer: str
    chunk: Chunk

    @property
    def question_tokens(self) -> set[str]:
        return set(tokenize(self.question))


def _faq_body(doc: Document) -> str:
    m = FAQ_HEADING_RE.search(doc.body)
    if not m:
        return ""
    rest = doc.body[m.end():]
    nxt = NEXT_HEADING_RE.search(rest)
    return rest[: nxt.start()] if nxt else rest


def build_index(documents: list[Document], chunks: list[Chunk]) -> list[FaqEntry]:
    """문서별 FAQ 쌍을 뽑아 **답이 실제로 들어 있는 청크**에 연결한다.

    청크 연결이 필요한 이유는 두 가지다. 하나는 권한 — FAQ도 청크와 똑같이 ACL을 통과해야
    한다. 다른 하나는 인용 — 답변에는 근거 문서와 절이 함께 표시되어야 한다.
    """
    by_doc: dict[str, list[Chunk]] = {}
    for c in chunks:
        by_doc.setdefault(c.doc_id, []).append(c)

    entries: list[FaqEntry] = []
    for doc in documents:
        body = _faq_body(doc)
        if not body:
            continue
        for question, answer in PAIR_RE.findall(body):
            question = question.strip()
            answer = " ".join(answer.split())
            if not question or not answer:
                continue
            # 답 문장이 그대로 들어 있는 청크를 찾는다. 없으면 인용을 걸 수 없으므로 버린다.
            head = answer[:40]
            owner = next((c for c in by_doc.get(doc.doc_id, []) if head in c.text), None)
            if owner is None:
                continue
            entries.append(FaqEntry(doc.doc_id, question, answer, owner))
    return entries


def _subject_terms(query: str) -> list[str]:
    out = []
    for term in content_terms(query):
        if len(term) < 2 or term in STOPWORDS or term in INTERROGATIVE_STEMS:
            continue
        if term.endswith(PREDICATE_ENDINGS):
            continue
        out.append(term)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def match(query: str, entries: list[FaqEntry]) -> tuple[FaqEntry, float] | None:
    """질의와 사실상 같은 FAQ 질문을 찾는다. 없으면 None.

    두 조건을 **모두** 만족해야 한다.
      1. 자카드 유사도 ≥ `FAQ_MATCH_MIN`
      2. 질의의 주제어가 FAQ 질문·답에 적어도 하나 등장 (의문사는 주제어가 아니다)
    """
    if not entries:
        return None
    q_tokens = set(tokenize(query))
    best, best_score = None, 0.0
    for entry in entries:
        score = _jaccard(q_tokens, entry.question_tokens)
        if score > best_score:
            best, best_score = entry, score
    if best is None or best_score < config.FAQ_MATCH_MIN:
        return None

    haystack = f"{best.question} {best.answer}"
    if not any(term in haystack for term in _subject_terms(query)):
        return None
    return best, round(best_score, 4)
