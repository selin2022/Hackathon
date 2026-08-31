"""문서 간 충돌 해소 (설계서 §8.3)."""
from __future__ import annotations

CATEGORY_RANK = {"규정·정책": 2}  # 그 외는 1


def _rank(category: str) -> int:
    return CATEGORY_RANK.get(category, 1)


def _version_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in str(version).split("."))
    except ValueError:
        return (0,)


def resolve(candidates: list, docs_by_id: dict) -> tuple[list, bool]:
    """(정리된 후보, 미해소 충돌 여부)를 반환한다.

    같은 doc_id의 낮은 버전과 supersedes로 대체된 문서를 제거한다.
    서로 다른 문서가 남고 카테고리 등급·발행일로도 가릴 수 없으면
    판정하지 않고 양쪽을 모두 인용한다.
    """
    if len(candidates) <= 1:
        return candidates, False

    # 2) 동일 doc_id는 최신 버전만 남긴다
    best_version: dict[str, tuple[int, ...]] = {}
    for cand in candidates:
        doc_id = cand.chunk.doc_id
        ver = _version_tuple(cand.chunk.doc_version)
        if doc_id not in best_version or ver > best_version[doc_id]:
            best_version[doc_id] = ver
    kept = [
        c for c in candidates
        if _version_tuple(c.chunk.doc_version) == best_version[c.chunk.doc_id]
    ]

    # 3) supersedes로 대체된 문서 제거
    superseded: set[str] = set()
    for doc in docs_by_id.values():
        if doc.supersedes:
            superseded.add(doc.supersedes.split("@")[0])
    kept = [c for c in kept if c.chunk.doc_id not in superseded] or kept

    doc_ids = {c.chunk.doc_id for c in kept}
    if len(doc_ids) <= 1:
        return kept, False

    # 서로 다른 주제를 다루는 문서가 함께 인용된 것은 충돌이 아니다.
    # 같은 중분류를 다루는 문서가 둘 이상일 때만 충돌 후보로 본다.
    subcats = {}
    for cand in kept:
        doc = docs_by_id.get(cand.chunk.doc_id)
        if doc is None:
            continue
        subcats.setdefault(doc.subcategory, set()).add(cand.chunk.doc_id)
    if not any(len(ids) > 1 for ids in subcats.values()):
        return kept, False

    # 4)~5) 카테고리 등급 → 발행일. 최상위와 동급인 문서가 여럿이면 미해소로 본다.
    def sort_key(c):
        return (_rank(c.chunk.category), c.chunk.published_at)

    top = max(sort_key(c) for c in kept)
    top_docs = {c.chunk.doc_id for c in kept if sort_key(c) == top}
    unresolved = len(top_docs) > 1 and len(doc_ids) > 1
    return kept, unresolved
