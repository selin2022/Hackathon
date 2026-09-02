"""권한 판정 (설계서 §5.1). 검색 전과 답변 반환 전 두 번 호출한다 (§4.6)."""
from __future__ import annotations

from datetime import date

from app.indexing.chunker import Chunk


class AccessDecision:
    ALLOW = "allow"
    DENY_ACL = "deny_acl"
    DENY_AUDIENCE = "deny_audience"
    DENY_EXPIRED = "deny_expired"
    DENY_NOT_EFFECTIVE = "deny_not_effective"
    DENY_SENSITIVITY = "deny_sensitivity"


def evaluate(chunk: Chunk, user: dict, today: date | None = None) -> str:
    today = today or date.today()

    if not (set(user.get("acl_groups", [])) & set(chunk.acl_groups)):
        return AccessDecision.DENY_ACL

    audience = set(chunk.audience)
    if "all" not in audience and user.get("employment_type") not in audience:
        return AccessDecision.DENY_AUDIENCE

    if chunk.valid_until < today.isoformat():
        return AccessDecision.DENY_EXPIRED

    # 시행 전 규정은 색인되어 있어도 답변에 쓰지 않는다 (§3.3).
    # 개정안이 공포됐지만 아직 효력이 없는 기준으로 안내하면 지금 적용되지 않는
    # 규정을 알려주는 셈이다. 발행일이 아니라 **시행일**이 기준이다.
    if (chunk.effective_from or chunk.published_at) > today.isoformat():
        return AccessDecision.DENY_NOT_EFFECTIVE

    # restricted 문서는 소유 역할만 열람한다 (§5.1 조건 4)
    if chunk.sensitivity == "restricted" and user.get("role") != "hr_admin":
        return AccessDecision.DENY_SENSITIVITY

    return AccessDecision.ALLOW


def is_visible(chunk: Chunk, user: dict, today: date | None = None) -> bool:
    return evaluate(chunk, user, today) == AccessDecision.ALLOW


def filter_chunks(
    chunks: list[Chunk], user: dict, today: date | None = None
) -> tuple[list[Chunk], int, bool]:
    """(허용 청크, 걸러진 개수, 권한으로 막힌 것이 있었는지)를 반환한다.

    어떤 문서가 걸러졌는지는 절대 반환하지 않는다. 존재 자체가 정보이기 때문이다 (§3.5).
    """
    allowed: list[Chunk] = []
    filtered = 0
    acl_blocked = False
    for chunk in chunks:
        decision = evaluate(chunk, user, today)
        if decision == AccessDecision.ALLOW:
            allowed.append(chunk)
        else:
            filtered += 1
            if decision in (AccessDecision.DENY_ACL, AccessDecision.DENY_SENSITIVITY):
                acl_blocked = True
    return allowed, filtered, acl_blocked
