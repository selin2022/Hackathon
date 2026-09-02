"""문서 간 충돌 해소 (설계서 §8.3)."""
from __future__ import annotations

from app.indexing.loader import AUTHORITY_RANK

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

    # 4) 규정 위계 → 5) 카테고리 등급 → 6) 발행일.
    #
    # **위계가 발행일보다 앞선다.** 부서 지침이 더 최근이라는 이유로 취업규칙을 이기면
    # 안 된다. 규정은 상위 규범이 하위를 구속하므로, 최신성으로 가릴 문제가 아니다.
    # 안내문끼리는 위계가 모두 동급(1)이라 기존 규칙이 그대로 적용된다.
    def sort_key(c):
        return (
            AUTHORITY_RANK.get(getattr(c.chunk, "authority_level", "안내문"), 1),
            _rank(c.chunk.category),
            c.chunk.published_at,
        )

    top = max(sort_key(c) for c in kept)
    top_docs = {c.chunk.doc_id for c in kept if sort_key(c) == top}

    # 위계가 갈렸다면 하위 규정은 인용에서 제외한다. 양쪽을 나란히 보여주면
    # 사용자가 어느 쪽을 따라야 하는지 알 수 없고, 그것이 곧 잘못된 안내다.
    top_rank = top[0]
    if len({sort_key(c)[0] for c in kept}) > 1:
        kept = [c for c in kept if sort_key(c)[0] == top_rank]
        doc_ids = {c.chunk.doc_id for c in kept}
        top_docs = {c.chunk.doc_id for c in kept if sort_key(c) == top}

    unresolved = len(top_docs) > 1 and len(doc_ids) > 1
    return kept, unresolved
