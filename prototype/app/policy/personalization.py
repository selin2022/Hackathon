"""개인화 결정표 (설계서 §6). 여기 정의된 것 외의 판단은 하지 않는다."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

HEALTH_RE = re.compile(r"건강\s?검진|검진|건진")
TAX_RE = re.compile(r"연말\s?정산|정산|원천징수|소득공제")
YEAR_RE = re.compile(r"(20\d{2})\s*년")
LAST_YEAR_RE = re.compile(r"작년|지난해|전년")

# "내가 대상인가"를 묻는 질문의 표지. 결정표 판정은 이런 질문의 답이지,
# 같은 주제의 모든 질문에 대한 답이 아니다.
# "건강검진 결과는 회사에 공유되나요?"는 제도 문의이므로 대상 판정을 앞세우면
# 질문과 무관한 답이 된다.
ELIGIBILITY_RE = re.compile(
    r"대상|자격|해당(되|하|인|합|됩)|언제|몇\s?살|몇\s?년|주기|차례|"
    r"받아야|들어야|받을\s?수|받나요|받게|올해|내년|금년|저도|제가|나도"
)


@dataclass
class Personalization:
    topic: str
    verdict: str
    message: str
    basis: str
    flags: list[str]
    # 이 사용자에게 해당하지 않는 분기의 내용을 인용에서 제외하기 위한 표현.
    # 계속 근로자에게 이직자 제출 서류를 보여주면 잘못된 안내가 된다.
    exclude_terms: list[str] = field(default_factory=list)
    # 판정 문구로 한 줄 요약을 덮어쓸지 여부. 대상 여부를 묻지 않은 질문에서는
    # 문서 본문이 답이고, 판정은 답이 아니다.
    summary_override: bool = True

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "verdict": self.verdict,
            "message": self.message,
            "basis": self.basis,
            "flags": self.flags,
        }


# 연말정산 유형별로, 해당하지 않는 분기를 가리키는 표현
TAX_BRANCH_TERMS = {
    "tenured": ["이직자", "신규 입사자"],
    "transfer": ["계속 근로자", "신규 입사자"],
    "new": ["이직자", "계속 근로자"],
}


def _hire_year(user: dict) -> int | None:
    hire = user.get("hire_date")
    if not hire:
        return None
    try:
        return int(str(hire)[:4])
    except ValueError:
        return None


def health_checkup(user: dict, today: date | None = None) -> Personalization | None:
    """§6.1 — 첫 대상은 입사 다음 해, 이후 2년 주기. Δ가 홀수면 대상."""
    today = today or date.today()
    year = today.year
    y0 = _hire_year(user)

    if y0 is None or y0 > year:
        return Personalization(
            topic="health_checkup",
            verdict="unknown",
            message="입사일 정보를 확인할 수 없어 대상 여부를 판정하지 않았습니다.",
            basis="입사일 확인 불가",
            flags=["인사팀 확인 필요"],
        )

    delta = year - y0
    flags: list[str] = []
    basis = f"입사 연도 기준 ({y0}년 입사) · 조회 연도 {year}년"

    if delta == 0:
        verdict = "not_target"
        message = "입사 당해 연도는 건강검진 대상이 아니며, 입사 다음 해부터 대상이 됩니다."
        if user.get("is_transfer"):
            flags.append(
                "이직 경력 입사자는 이전 직장의 당해 연도 수검 여부에 따라 달라질 수 있어 "
                "인사팀 확인이 필요합니다."
            )
        hire = str(user.get("hire_date", ""))
        if hire[5:7] == "12":
            flags.append("12월 입사이므로 첫 검진 시기는 인사팀에 확인하시기를 권합니다.")
    elif delta % 2 == 1:
        verdict = "target"
        message = f"{year}년 건강검진 대상입니다."
    else:
        verdict = "not_target"
        message = f"{year}년은 대상이 아니며, {year + 1}년이 대상입니다."

    return Personalization("health_checkup", verdict, message, basis, flags)


def _target_tax_year(user: dict, query: str, today: date) -> int:
    """§6.2 — Yt 결정 규칙."""
    explicit = YEAR_RE.search(query)
    if explicit:
        return int(explicit.group(1))
    if LAST_YEAR_RE.search(query):
        return today.year - 1
    y0 = _hire_year(user)
    if y0 is not None and y0 == today.year:
        # 당해 입사자는 아직 첫 정산을 겪지 않았으므로 다가오는 정산을 안내한다.
        return today.year
    return today.year - 1


def year_end_tax(user: dict, query: str = "", today: date | None = None) -> Personalization | None:
    """§6.2 — 연말정산 유형 분기."""
    today = today or date.today()
    y0 = _hire_year(user)
    if y0 is None:
        return None

    yt = _target_tax_year(user, query, today)
    basis = f"입사 연도 {y0}년 · 정산 대상 연도 {yt}년"
    flags: list[str] = []

    if y0 > yt:
        return Personalization(
            "year_end_tax", "not_target",
            f"{yt}년에는 재직하지 않으셨으므로 해당 연도 연말정산 대상이 아닙니다.",
            basis, flags,
        )

    if y0 < yt:
        verdict, message = "tenured", f"{yt}년 전체 기간을 기준으로 정산하는 계속 근로자에 해당합니다."
    elif user.get("is_transfer"):
        verdict = "transfer"
        message = (
            f"{yt}년에 이직하셨으므로 이직자에 해당합니다. "
            "이전 근무지의 원천징수영수증 등 증빙 확인이 필요합니다."
        )
        flags.append("해당 연도에 두 곳 이상에서 근무한 경우에는 개별 확인이 필요합니다.")
    else:
        verdict = "new"
        message = f"{yt}년에 입사하셨으므로 입사한 달부터 연말까지의 소득이 정산 대상입니다."
        hire = str(user.get("hire_date", ""))
        if hire[5:7] == "12":
            flags.append(
                "12월 입사로 해당 연도 급여 지급 이력이 없으면 대상이 아닐 수 있어 "
                "인사팀 확인이 필요합니다."
            )

    return Personalization(
        "year_end_tax", verdict, message, basis, flags,
        exclude_terms=TAX_BRANCH_TERMS.get(verdict, []),
    )


def resolve(user: dict, query: str, today: date | None = None) -> Personalization | None:
    """질문 주제에 따라 해당하는 결정표만 적용한다.

    주제가 같아도 **대상 여부를 묻지 않은 질문**에는 판정을 답으로 내세우지 않는다.
    건강검진은 판정 외에 할 일이 없으므로 아예 적용하지 않고, 연말정산은 유형별
    분기 필터(exclude_terms)가 여전히 필요하므로 필터만 남기고 요약 덮어쓰기를 끈다.
    """
    asks_eligibility = bool(ELIGIBILITY_RE.search(query))
    if HEALTH_RE.search(query):
        return health_checkup(user, today) if asks_eligibility else None
    if TAX_RE.search(query):
        pers = year_end_tax(user, query, today)
        if pers and not asks_eligibility:
            pers.summary_override = False
        return pers
    return None


def mask_employee_no(value: str) -> str:
    return f"{value[:2]}****" if len(value) >= 2 else "****"


def mask_email(value: str) -> str:
    if "@" not in value:
        return "***"
    local, domain = value.split("@", 1)
    return f"{local[:2]}***@{domain}"
