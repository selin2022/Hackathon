"""지식 문서 로딩과 front-matter 검증 (설계서 §3.1)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from app import config

REQUIRED_FIELDS = {
    "doc_id", "title", "version", "category", "subcategory", "owner_dept",
    "approver", "sensitivity", "audience", "acl_groups", "published_at",
    "valid_until", "status", "demo_assumption",
}
VALID_SENSITIVITY = {"public", "internal", "restricted"}
VALID_STATUS = {"draft", "review", "published", "expired"}

# --- §3.3 내규·규정 대응 -----------------------------------------------------
# 온보딩 지식베이스에는 두 종류의 문서가 섞인다. 안내문은 "이렇게 하세요"이고,
# 규정은 "제N조 ①"로 쓰인 사규다. 둘은 청킹 단위도, 인용 표기도, 충돌 시
# 우선순위도 다르다. `doc_type`이 그 갈림길이다.
VALID_DOC_TYPE = {"안내문", "규정"}

# 규정 위계. 충돌 시 **발행일이 아니라 이 순위**로 가린다 (§8.3).
# 부서 지침이 더 최근이라는 이유로 취업규칙을 이기면 안 된다.
AUTHORITY_RANK = {"취업규칙": 4, "사규": 3, "부서지침": 2, "안내문": 1}
DOC_ID_RE = re.compile(r"^[A-Z]{2,4}-\d{3}$")
FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)


class DocumentValidationError(ValueError):
    """front-matter 검증 실패. 경고가 아니라 실패로 처리한다 (§3.1)."""


@dataclass
class Document:
    doc_id: str
    title: str
    version: str
    category: str
    subcategory: str
    owner_dept: str
    approver: str
    sensitivity: str
    audience: list[str]
    acl_groups: list[str]
    published_at: str
    valid_until: str
    status: str
    demo_assumption: bool
    supersedes: str | None
    body: str
    source_path: str
    # --- §3.3 내규·규정 필드. 안내문은 기본값으로 동작해 기존 문서를 건드리지 않는다.
    doc_type: str = "안내문"
    authority_level: str = "안내문"
    effective_from: str = ""          # 시행일. 비면 published_at을 쓴다
    statutory: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, today: date | None = None) -> bool:
        today = today or date.today()
        return self.valid_until < today.isoformat()

    def is_effective(self, today: date | None = None) -> bool:
        """시행일이 지났는가.

        **개정일과 시행일은 다르다.** 내규는 "2026-01-01부터 시행" 같은 부칙을 달고
        공포일보다 늦게 효력이 생긴다. 시행 전 규정으로 답하면 아직 적용되지 않는
        기준을 안내하는 셈이므로, 색인은 하되 검색에서는 제외한다.
        """
        today = today or date.today()
        return (self.effective_from or self.published_at) <= today.isoformat()

    @property
    def authority_rank(self) -> int:
        return AUTHORITY_RANK.get(self.authority_level, 1)


def _as_date_str(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def parse_document(path: Path) -> Document:
    text = path.read_text(encoding="utf-8")
    match = FRONT_MATTER_RE.match(text)
    if not match:
        raise DocumentValidationError(f"{path.name}: front-matter가 없습니다.")

    fm = yaml.safe_load(match.group(1)) or {}
    missing = REQUIRED_FIELDS - set(fm)
    if missing:
        raise DocumentValidationError(f"{path.name}: 필수 필드 누락 {sorted(missing)}")

    doc_id = str(fm["doc_id"])
    if not DOC_ID_RE.match(doc_id):
        raise DocumentValidationError(f"{path.name}: doc_id 형식 오류 '{doc_id}'")
    if fm["sensitivity"] not in VALID_SENSITIVITY:
        raise DocumentValidationError(f"{path.name}: sensitivity 값 오류")
    if fm["status"] not in VALID_STATUS:
        raise DocumentValidationError(f"{path.name}: status 값 오류")
    for key in ("audience", "acl_groups"):
        if not isinstance(fm[key], list) or not fm[key]:
            raise DocumentValidationError(f"{path.name}: {key}는 비어 있을 수 없습니다.")

    doc_type = str(fm.get("doc_type", "안내문"))
    if doc_type not in VALID_DOC_TYPE:
        raise DocumentValidationError(f"{path.name}: doc_type 값 오류 '{doc_type}'")

    authority = str(fm.get("authority_level", "안내문"))
    if authority not in AUTHORITY_RANK:
        raise DocumentValidationError(f"{path.name}: authority_level 값 오류 '{authority}'")
    # 규정은 위계를 반드시 선언해야 한다. 선언하지 않으면 충돌 시 안내문과 같은 등급이
    # 되어, 취업규칙이 부서 지침에 밀리는 사고가 조용히 난다.
    if doc_type == "규정" and authority == "안내문":
        raise DocumentValidationError(
            f"{path.name}: doc_type=규정이면 authority_level을 선언해야 합니다."
        )

    statutory = fm.get("statutory") or []
    if not isinstance(statutory, list):
        raise DocumentValidationError(f"{path.name}: statutory는 목록이어야 합니다.")

    body = text[match.end():]
    demo = bool(fm["demo_assumption"])
    if demo and "데모용 가정" not in text[:1500]:
        raise DocumentValidationError(
            f"{path.name}: demo_assumption=true인데 본문 앞부분에 데모 고지가 없습니다."
        )

    return Document(
        doc_id=doc_id,
        title=str(fm["title"]),
        version=str(fm["version"]),
        category=str(fm["category"]),
        subcategory=str(fm["subcategory"]),
        owner_dept=str(fm["owner_dept"]),
        approver=str(fm["approver"]),
        sensitivity=str(fm["sensitivity"]),
        audience=[str(a) for a in fm["audience"]],
        acl_groups=[str(g) for g in fm["acl_groups"]],
        published_at=_as_date_str(fm["published_at"]),
        valid_until=_as_date_str(fm["valid_until"]),
        status=str(fm["status"]),
        demo_assumption=demo,
        supersedes=(str(fm["supersedes"]) if fm.get("supersedes") else None),
        body=body,
        source_path=str(path.name),
        doc_type=doc_type,
        authority_level=authority,
        effective_from=_as_date_str(fm.get("effective_from") or fm["published_at"]),
        statutory=[str(s) for s in statutory],
    )


def load_documents(directory: Path | None = None) -> list[Document]:
    """published 문서만 반환한다. 승인 없는 문서는 색인하지 않는다 (§3.1)."""
    directory = directory or config.KNOWLEDGE_DIR
    docs: list[Document] = []
    for path in sorted(directory.glob("*.md")):
        doc = parse_document(path)
        if doc.status != "published":
            continue
        docs.append(doc)
    return docs
