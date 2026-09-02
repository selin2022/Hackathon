"""민감정보 탐지와 프롬프트 인젝션 대응 (설계서 §8.1·§8.2)."""
from __future__ import annotations

import re
from dataclasses import dataclass

# --- (a) 차단군 -------------------------------------------------------------
BLOCK_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("주민등록번호", re.compile(r"\d{6}[-\s]?[1-4]\d{6}")),
    ("카드번호", re.compile(r"\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}")),
    ("계좌번호", re.compile(r"\d{2,3}[-\s]\d{2,6}[-\s]\d{2,7}")),
    # 값이 실제로 딸려 있을 때만 차단한다. 뒤에 무엇이든 오면 막는 형태로 두면
    # "비밀번호를 잊어버렸는데 어떻게 하나요?" 같은 제도 문의가 통째로 막힌다 —
    # 온보딩에서 가장 흔한 질문 중 하나다. 비밀을 적는 사람은 조사가 아니라
    # 값을 적으므로, 뒤따르는 토큰이 영숫자 4자 이상일 때만 차단한다.
    ("비밀번호·인증번호", re.compile(
        r"(비밀번호|패스워드|임시\s?비번|인증번호|OTP)"
        r"\s*(?:[은는이가]|[:=])?\s*[\"']?[A-Za-z0-9!@#$%^&*._+\-]{4,}"
    )),
]

# --- (b) 마스킹군 -----------------------------------------------------------
MASK_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("휴대전화", re.compile(r"01[016789][-\s]?\d{3,4}[-\s]?\d{4}"), "010-****-####"),
    ("이메일", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "***@***"),
]

# --- (c) 건강·타인 정보 -----------------------------------------------------
HEALTH_TERMS = re.compile(r"검진\s?결과|진단|소견|처방|질환|수치")
INSTITUTIONAL_TERMS = re.compile(r"언제|대상|신청|절차|방법|기준|주기|공유|안내|어떻게|어디")
THIRD_PARTY = re.compile(r"동료|다른\s?직원|타인|[가-힣]{2,4}\s?씨의|[가-힣]{2,4}\s?님의|[가-힣]{2,4}\s?의\s*(건강|검진|급여|평가)")

# --- 인젝션 -----------------------------------------------------------------
INJECTION_PATTERNS = [
    re.compile(r"(이전|위의|앞의).{0,10}(지시|규칙|프롬프트|명령).{0,10}(무시|잊)"),
    re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions", re.I),
    re.compile(r"system\s*prompt", re.I),
    re.compile(r"(너의|당신의)\s*(규칙|지침|프롬프트)를?\s*(알려|출력|보여)"),
    re.compile(r"개발자\s?모드|jailbreak|DAN\s?모드", re.I),
]

BLOCK_MESSAGE = (
    "입력하신 내용에 민감정보로 보이는 값이 포함되어 있어 처리하지 않았습니다. "
    "이 챗봇에는 주민등록번호·계좌번호·비밀번호를 입력하지 마세요. "
    "질문에서 해당 값을 빼고 다시 물어봐 주세요."
)
THIRD_PARTY_MESSAGE = (
    "타인의 개인정보나 건강 정보는 확인해 드릴 수 없습니다. "
    "관련 문의는 인사팀에 확인해 주세요."
)
SAVE_BLOCK_MESSAGE = (
    "저장하려는 내용에 민감정보로 보이는 값이 포함되어 있어 저장하지 않았습니다."
)


@dataclass
class ScanResult:
    action: str          # allow | block | mask | third_party
    reason: str          # 탐지 유형 (로그에 남기는 값. 원문은 남기지 않는다)
    text: str            # 후속 처리에 사용할 텍스트 (마스킹 적용 후)
    message: str = ""    # 사용자 안내 문구
    injection: bool = False


SCRIPT_BLOCK_RE = re.compile(r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
DANGLING_SCRIPT_RE = re.compile(r"<\s*(script|style)\b[^>]*>.*", re.I | re.S)
MARKUP_RE = re.compile(r"<[^>]{0,200}>")


def strip_markup(text: str) -> str:
    """검색 질의에서 HTML/스크립트 표기를 제거한다.

    표시는 항상 textContent로 하므로 실행 위험은 없지만, 마크업 토큰이 질의에 섞이면
    검색 품질이 떨어진다. 태그뿐 아니라 script/style의 **내용까지** 제거해야 한다 —
    태그만 벗기면 'alert(1)' 같은 잔여 토큰이 질의를 오염시킨다.
    원문은 그대로 두고 검색용 질의만 정리한다.
    """
    text = SCRIPT_BLOCK_RE.sub(" ", text)
    text = DANGLING_SCRIPT_RE.sub(" ", text)
    return re.sub(r"\s+", " ", MARKUP_RE.sub(" ", text)).strip()


def _detect_injection(text: str) -> bool:
    return any(p.search(text) for p in INJECTION_PATTERNS)


def scan_input(text: str) -> ScanResult:
    """§8.1 3단 처리. 키워드만으로 차단하지 않는다 — 제도 문의를 막으면 안 된다."""
    injection = _detect_injection(text)

    for label, pattern in BLOCK_PATTERNS:
        if pattern.search(text):
            return ScanResult("block", label, "", BLOCK_MESSAGE, injection)

    if THIRD_PARTY.search(text) and HEALTH_TERMS.search(text):
        return ScanResult("third_party", "타인 개인정보 요청", "", THIRD_PARTY_MESSAGE, injection)

    masked = text
    masked_labels: list[str] = []
    for label, pattern, replacement in MASK_PATTERNS:
        if pattern.search(masked):
            masked = pattern.sub(replacement, masked)
            masked_labels.append(label)

    # (c) 본인 건강정보 서술: 제도 문의 어휘가 함께 있으면 정상 처리한다.
    if HEALTH_TERMS.search(text) and not INSTITUTIONAL_TERMS.search(text):
        return ScanResult(
            "mask", "건강정보 서술", masked,
            "건강 관련 개인 정보는 저장하지 않았습니다. 제도 안내만 제공합니다.", injection,
        )

    if masked_labels:
        return ScanResult("mask", "+".join(masked_labels), masked, "", injection)

    return ScanResult("allow", "", masked, "", injection)


def scan_for_storage(text: str) -> bool:
    """§4.C — 저장 전 검사. 민감정보가 있으면 저장하지 않는다."""
    if any(p.search(text) for _, p in BLOCK_PATTERNS):
        return False
    if HEALTH_TERMS.search(text) and not INSTITUTIONAL_TERMS.search(text):
        return False
    return True


def scrub_output(text: str, forbidden_fragments: list[str]) -> str:
    """§8.2 출력 검사. 시스템 프롬프트 조각이 새어 나오면 제거한다."""
    for frag in forbidden_fragments:
        if frag and frag in text:
            return ""
    return text
