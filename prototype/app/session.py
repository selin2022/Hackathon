"""세션과 요청 제한 (설계서 §9.1·§10.3).

**무상태 서명 쿠키**를 쓴다. 서버 메모리에 세션을 두면 서버리스처럼 인스턴스가 계속 바뀌는
환경에서 로그인이 수시로 풀린다. 쿠키에는 사번과 발급 시각, 직전 질문만 담고 HMAC으로 서명해
위조를 막는다. 질문 원문은 쿠키 밖으로 나가지 않으며 로그에는 해시만 남는다.

쿠키에 담는 것은 최소한이다. 답변 본문이나 개인 정보는 담지 않는다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections import deque

from app import config

COOKIE_NAME = "onb_sid"
_DEFAULT_SECRET = "onboarding-rag-demo-secret-not-for-production"


def _secret() -> bytes:
    return (os.getenv("SESSION_SECRET") or _DEFAULT_SECRET).encode("utf-8")


def secret_is_configured() -> bool:
    return bool(os.getenv("SESSION_SECRET"))


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class Session:
    """쿠키에서 복원되는 세션. 서버에 상태를 두지 않는다."""

    __slots__ = ("employee_no", "issued_at", "last_query", "_dirty")

    def __init__(self, employee_no: str, issued_at: float, last_query: str = ""):
        self.employee_no = employee_no
        self.issued_at = issued_at
        self.last_query = last_query
        self._dirty = False

    @property
    def expired(self) -> bool:
        return (time.time() - self.issued_at) > config.SESSION_TTL_SECONDS

    def add_turn(self, query: str, summary: str) -> None:
        # 멀티턴 재작성에 필요한 것은 직전 질문뿐이다 (§9.2).
        self.last_query = query[:200]
        self._dirty = True

    @property
    def changed(self) -> bool:
        return self._dirty

    def to_cookie(self) -> str:
        payload = json.dumps(
            {"e": self.employee_no, "t": int(self.issued_at), "q": self.last_query},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        body = _b64encode(payload)
        signature = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
        return f"{body}.{_b64encode(signature)}"

    @classmethod
    def from_cookie(cls, value: str | None) -> "Session | None":
        if not value or "." not in value:
            return None
        body, _, signature = value.partition(".")
        expected = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
        try:
            given = _b64decode(signature)
        except (ValueError, TypeError):
            return None
        # 상수 시간 비교 — 서명 위조 시도에 타이밍 정보를 주지 않는다.
        if not hmac.compare_digest(expected, given):
            return None
        try:
            payload = json.loads(_b64decode(body))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        employee_no = payload.get("e")
        if not isinstance(employee_no, str):
            return None
        session = cls(employee_no, float(payload.get("t", 0)), str(payload.get("q", "")))
        return None if session.expired else session


class RateLimiter:
    """사번 단위 요청 제한.

    인스턴스 메모리에만 존재하므로 다중 인스턴스 환경에서는 인스턴스마다 따로 센다.
    남용 방지의 1차 방어이며, 운영 환경에서는 게이트웨이나 공유 저장소로 옮긴다.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque] = {}

    def check(self, key: str, now: float | None = None) -> bool:
        """제한을 넘었으면 True."""
        now = now or time.time()
        window = self._hits.setdefault(key, deque(maxlen=config.RATE_LIMIT_PER_MINUTE * 2))
        while window and now - window[0] > 60:
            window.popleft()
        return len(window) >= config.RATE_LIMIT_PER_MINUTE

    def record(self, key: str, now: float | None = None) -> None:
        self._hits.setdefault(key, deque(maxlen=config.RATE_LIMIT_PER_MINUTE * 2)).append(
            now or time.time()
        )


def new_session(employee_no: str) -> Session:
    return Session(employee_no, time.time())


def query_hash(text: str) -> str:
    """로그용. 질문 원문 대신 해시 앞 12자만 남긴다 (§9.1)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
