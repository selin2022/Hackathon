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
    meta: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, today: date | None = None) -> bool:
        today = today or date.today()
        return self.valid_until < today.isoformat()


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
