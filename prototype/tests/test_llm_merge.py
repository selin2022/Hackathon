"""§7.5 LLM 병합의 안전 속성 검사.

검사하는 것은 "합치기를 잘하는가"가 아니라 **"나쁜 답을 내보내지 않는가"**다.
추론 서버가 무엇을 반환하든, 검증을 통과하지 못하면 추출 방식 답변이 유지되어야 한다.
"""
from __future__ import annotations

from datetime import date

import pytest

from app import config
from app.answer import llm
from app.service import ChatService

TODAY = date(2026, 9, 2)

SOURCES = [
    "연차유급휴가는 사용하려는 날의 5일 전까지 전자결재로 신청한다.",
    "1년간 8할 이상 출근한 사원에게 15일의 유급휴가를 준다.",
]


@pytest.mark.parametrize("sentence", [
    "연차는 15일이며 사용 5일 전까지 신청합니다.",
    "연차 15일, 5일 전 신청, 8할 출근 요건입니다.",
])
def test_사실이_모두_근거에_있으면_통과한다(sentence):
    assert llm.verify_facts(sentence, SOURCES) == []


@pytest.mark.parametrize("sentence,bad", [
    ("연차는 20일이며 사용 5일 전까지 신청합니다.", "20일"),
    ("연차는 15일이며 사용 3일 전까지 신청합니다.", "3일"),
    ("연차는 15일이며 30일 전까지 신청합니다.", "30일"),
])
def test_근거에_없는_숫자는_잡아낸다(sentence, bad):
    """가장 위험한 오답 형태. 문장은 자연스러운데 기한·일수만 틀린 경우."""
    assert bad in llm.verify_facts(sentence, SOURCES)


def test_엔드포인트_미설정이면_병합하지_않는다():
    assert llm.merge("연차 며칠 전?", SOURCES) == (None, {"reason": "not_configured"})


def test_추론서버_장애는_서비스_장애가_아니다(monkeypatch):
    """LLM이 죽어도 답변은 나가야 한다. 이것이 온프레미스에서 중요한 성질이다."""
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://127.0.0.1:59999/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "dummy")
    monkeypatch.setattr(config, "LLM_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(config, "ANSWER_BACKEND", "llm")

    out = ChatService().handle("연차는 며칠 전까지 신청해야 하나요?", "234567", today=TODAY)
    assert out.ok
    assert "5일 전" in out.answer["summary"]
    assert out.meta["llm_merge"]["reason"] == "upstream_error"
    assert not out.answer.get("merged_by_llm")


def _fake_response(content: str):
    return {"choices": [{"message": {"content": content}}]}


def test_검증_실패하면_추출_답변이_유지된다(monkeypatch):
    """모델이 근거에 없는 숫자를 만들어내면 그 문장은 버린다."""
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "dummy")
    monkeypatch.setattr(config, "ANSWER_BACKEND", "llm")
    monkeypatch.setattr(llm, "_post",
                        lambda *a, **k: _fake_response("연차는 20일이며 3일 전까지 신청합니다."))

    out = ChatService().handle("연차는 며칠 전까지 신청해야 하나요?", "234567", today=TODAY)
    assert out.ok
    assert out.meta["llm_merge"]["reason"] == "fact_check_failed"
    assert not out.answer.get("merged_by_llm")
    assert "20일" not in out.answer["summary"]


def test_검증_통과하면_병합_문장을_쓴다(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "dummy")
    monkeypatch.setattr(config, "ANSWER_BACKEND", "llm")
    merged = "연차는 1년간 8할 이상 출근 시 15일이 발생하며, 사용 5일 전까지 신청합니다."
    monkeypatch.setattr(llm, "_post", lambda *a, **k: _fake_response(merged))

    out = ChatService().handle("연차는 며칠 전까지 신청해야 하나요?", "234567", today=TODAY)
    assert out.ok
    assert out.answer.get("merged_by_llm")
    assert out.answer["summary"] == merged


def test_모델이_모른다고_하면_추출_답변을_쓴다(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "dummy")
    monkeypatch.setattr(config, "ANSWER_BACKEND", "llm")
    monkeypatch.setattr(llm, "_post", lambda *a, **k: _fake_response("답할 수 없음"))

    out = ChatService().handle("연차는 며칠 전까지 신청해야 하나요?", "234567", today=TODAY)
    assert out.ok
    assert out.meta["llm_merge"]["reason"] == "model_declined"
    assert not out.answer.get("merged_by_llm")


def test_개인화_판정은_병합이_덮어쓰지_않는다(monkeypatch):
    """결정표 산출물은 문서 인용이 아니므로 LLM이 손대면 안 된다."""
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://llm.internal/v1")
    monkeypatch.setattr(config, "LLM_MODEL", "dummy")
    monkeypatch.setattr(config, "ANSWER_BACKEND", "llm")
    monkeypatch.setattr(llm, "_post", lambda *a, **k: _fake_response("아무 말이나 합니다."))

    out = ChatService().handle("올해 건강검진 대상인가요?", "234567", today=TODAY)
    assert out.ok
    assert "대상입니다" in out.answer["summary"]
    assert "llm_merge" not in out.meta
