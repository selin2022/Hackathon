"""LLM 병합 백엔드 (설계서 §7.5).

**역할이 좁다.** 이 모듈은 검색도, 권한 판정도, 사실 판단도 하지 않는다. 이미 추출된 근거
문장 몇 개를 받아 **한 문장으로 합치는 것**만 한다. 그 앞뒤(검색·ACL·게이트·인용 검증)는
LLM이 있든 없든 동일하게 동작한다.

## 왜 이 좁은 역할인가

추출 방식은 "원문 문장 하나"를 고르므로, 답이 두 문장에 나뉘어 있으면 반쪽만 보여준다.
이것이 LLM 없이 해결되지 않는 유일한 부류다(온프렘 설계서 §4.4). 나머지 품질 문제는
검색·문서 정비로 해결되며 그쪽이 더 싸고 검증 가능하다.

## 무엇을 포기하고 무엇을 지키는가

문장을 합치면 그 결과는 원문에 없으므로 **"모든 문장이 원문의 부분 문자열"이라는 불변 조건을
통과하지 못한다.** 그래서 검증을 교체한다.

- **포기:** 부분 문자열 일치
- **지킨다:** 숫자·날짜·금액·기한이 전부 원문에 있을 것 (`verify_facts`)
- **지킨다:** 검증 실패 시 **추출 방식 답변으로 되돌린다.** 검증 안 된 문장은 절대 내보내지 않는다

즉 LLM은 **더 나은 답을 시도할 뿐, 더 나쁜 답을 낼 수는 없다.** 네트워크 실패·모델 오류·검증
실패는 전부 추출 방식으로 떨어진다.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from app import config

# 사실 토큰: 숫자로 시작하고 단위가 붙는 형태. "15일", "100만", "09시", "30일", "8할"
# 이 값들이 틀리면 답변이 실무적으로 위험해진다 — 기한·금액·일수가 전부 여기 속한다.
FACT_TOKEN_RE = re.compile(r"\d[\d,]*\s*(?:년|월|일|시|분|주|개월|만|천|원|%|배|회|할|명|건|시간)?")
WS_RE = re.compile(r"\s+")

SYSTEM_PROMPT = (
    "당신은 사내 온보딩 안내 문서를 정리해 전달하는 도우미입니다.\n"
    "아래 '근거'에 적힌 내용만 사용해 질문에 한 문장으로 답하세요.\n"
    "규칙:\n"
    "1. 근거에 없는 사실을 추가하지 마세요. 추측하지 마세요.\n"
    "2. 숫자·날짜·금액·기한은 근거에 있는 값을 그대로 쓰세요. 계산하거나 바꾸지 마세요.\n"
    "3. 한 문장으로만 답하세요. 인사말·설명·사족을 붙이지 마세요.\n"
    "4. 근거만으로 답할 수 없으면 정확히 '답할 수 없음'이라고만 쓰세요.\n"
)

CANNOT_ANSWER = "답할 수 없음"


def _normalize(text: str) -> str:
    return WS_RE.sub("", text)


def fact_tokens(text: str) -> list[str]:
    return [_normalize(m.group()) for m in FACT_TOKEN_RE.finditer(text) if m.group().strip()]


def verify_facts(sentence: str, sources: list[str]) -> list[str]:
    """합친 문장의 사실 토큰이 전부 근거에 있는지 검사한다.

    부분 문자열 검사를 대체하는 최소 보증이다. 문장 구조는 바뀌어도 **숫자는 바뀌면 안 된다.**
    근거에 없는 토큰 목록을 반환하며, 비어 있지 않으면 그 답변은 쓰지 않는다.
    """
    corpus = _normalize(" ".join(sources))
    return [t for t in fact_tokens(sentence) if t and t not in corpus]


def _post(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def merge(query: str, sources: list[str]) -> tuple[str | None, dict]:
    """근거 문장들을 한 문장으로 합친다. 실패하면 (None, 사유)를 반환한다.

    OpenAI 호환 `/chat/completions`를 쓴다. Ollama·vLLM·사내 추론 서버가 모두 같은 규약을
    제공하므로, **엔드포인트 주소만 바꾸면 온프레미스로 전환된다.** 외부 서비스로 향하지
    않는 것은 설정이 아니라 egress 차단으로 보장한다(온프렘 §4.5.3).
    """
    if not config.LLM_BASE_URL or not config.LLM_MODEL:
        return None, {"reason": "not_configured"}
    if not sources:
        return None, {"reason": "no_sources"}

    prompt = f"근거:\n" + "\n".join(f"- {s}" for s in sources) + f"\n\n질문: {query}"
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,          # 같은 질문에 같은 답이 나와야 회귀 테스트가 성립한다
        "max_tokens": config.LLM_MAX_TOKENS,
        "stream": False,
    }
    url = config.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    try:
        body = _post(url, payload, config.LLM_TIMEOUT_SECONDS)
        text = body["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as exc:
        # 추론 서버 장애는 서비스 장애가 아니다. 추출 방식으로 계속 동작한다.
        return None, {"reason": "upstream_error", "detail": type(exc).__name__}

    if not text or CANNOT_ANSWER in text:
        return None, {"reason": "model_declined"}

    # 한 문장만 취한다. 모델이 사족을 붙여도 첫 문장까지만 쓴다.
    text = text.split("\n")[0].strip()
    if len(text) > config.LLM_MAX_SUMMARY_CHARS:
        return None, {"reason": "too_long", "chars": len(text)}

    unverified = verify_facts(text, sources)
    if unverified:
        # 근거에 없는 숫자가 생겼다 — 가장 위험한 형태의 오답이므로 버린다.
        return None, {"reason": "fact_check_failed", "tokens": unverified}

    return text, {"reason": "ok", "chars": len(text)}
