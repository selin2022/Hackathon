"""문서 검증 (온프레미스 설계서 §4.6.3 CI 게이트).

병합 전에 실행한다. 하나라도 오류가 있으면 종료 코드 1을 반환해 병합을 막는다.

    python3 tools/validate_docs.py

`loader.parse_document`는 첫 오류에서 멈추지만, 이 도구는 **모든 문서의 모든 문제를 한 번에**
보고한다. 담당자가 고치고 다시 올리는 왕복을 줄이기 위해서다.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from app import config  # noqa: E402
from app.indexing.loader import (  # noqa: E402
    AUTHORITY_RANK, DOC_ID_RE, FRONT_MATTER_RE, REQUIRED_FIELDS,
    VALID_DOC_TYPE, VALID_SENSITIVITY, VALID_STATUS,
)

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ARTICLE_RE = re.compile(r"제\s*\d+\s*조")
HANG_MARKS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _as_str(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def check(path: Path, seen_ids: dict[str, str]) -> tuple[list[str], list[str]]:
    """(오류, 경고)를 반환한다. 오류가 하나라도 있으면 병합을 막는다."""
    errors: list[str] = []
    warnings: list[str] = []
    text = path.read_text(encoding="utf-8")

    m = FRONT_MATTER_RE.match(text)
    if not m:
        return ["front-matter가 없습니다."], []
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        return [f"front-matter YAML 파싱 실패: {exc}"], []

    missing = REQUIRED_FIELDS - set(fm)
    if missing:
        errors.append(f"필수 필드 누락: {sorted(missing)}")

    doc_id = str(fm.get("doc_id", ""))
    if doc_id and not DOC_ID_RE.match(doc_id):
        errors.append(f"doc_id 형식 오류 '{doc_id}'")
    if doc_id and doc_id in seen_ids:
        errors.append(f"doc_id 중복 — {seen_ids[doc_id]}와 같은 '{doc_id}'")
    elif doc_id:
        seen_ids[doc_id] = path.name
    # 파일명이 doc_id로 시작해야 문서를 파일 목록에서 바로 찾을 수 있다.
    if doc_id and not path.name.startswith(doc_id):
        warnings.append(f"파일명이 doc_id로 시작하지 않습니다 (doc_id={doc_id})")

    if fm.get("sensitivity") not in VALID_SENSITIVITY:
        errors.append(f"sensitivity 값 오류: {fm.get('sensitivity')!r}")
    if fm.get("status") not in VALID_STATUS:
        errors.append(f"status 값 오류: {fm.get('status')!r}")
    for key in ("audience", "acl_groups"):
        if not isinstance(fm.get(key), list) or not fm.get(key):
            errors.append(f"{key}는 비어 있을 수 없습니다.")

    # --- 날짜 -------------------------------------------------------------
    published = _as_str(fm.get("published_at", ""))
    valid_until = _as_str(fm.get("valid_until", ""))
    effective = _as_str(fm.get("effective_from") or published)
    for label, value in (("published_at", published), ("valid_until", valid_until),
                         ("effective_from", effective)):
        if value and not DATE_RE.match(value):
            errors.append(f"{label} 형식 오류 '{value}' (YYYY-MM-DD)")
    if DATE_RE.match(published or "") and DATE_RE.match(valid_until or ""):
        if valid_until <= published:
            errors.append(f"valid_until({valid_until})이 published_at({published})보다 늦어야 합니다.")
    if DATE_RE.match(effective or "") and DATE_RE.match(valid_until or ""):
        if effective > valid_until:
            errors.append(f"effective_from({effective})이 valid_until({valid_until})을 넘습니다 — 한 번도 유효하지 않은 문서입니다.")

    # --- 규정 문서 ---------------------------------------------------------
    doc_type = str(fm.get("doc_type", "안내문"))
    if doc_type not in VALID_DOC_TYPE:
        errors.append(f"doc_type 값 오류 '{doc_type}'")
    authority = str(fm.get("authority_level", "안내문"))
    if authority not in AUTHORITY_RANK:
        errors.append(f"authority_level 값 오류 '{authority}'")
    if doc_type == "규정":
        if authority == "안내문":
            errors.append("doc_type=규정이면 authority_level을 선언해야 합니다.")
        if not fm.get("effective_from"):
            warnings.append("규정인데 effective_from이 없습니다. published_at을 시행일로 간주합니다.")
        body = text[m.end():]
        if ARTICLE_RE.search(body) and not any(c in body for c in HANG_MARKS):
            # 변환 사고의 대표적 형태. 항 기호가 사라지면 조문 청킹이 동작하지 않는다.
            warnings.append("조문은 있는데 항 기호(①②③)가 없습니다. 변환 과정에서 소실되었을 수 있습니다.")
    if fm.get("statutory") is not None and not isinstance(fm.get("statutory"), list):
        errors.append("statutory는 목록이어야 합니다.")

    # --- 데모 고지 ---------------------------------------------------------
    if fm.get("demo_assumption") and "데모용 가정" not in text[:1500]:
        errors.append("demo_assumption=true인데 본문 앞부분에 데모 고지가 없습니다.")

    # --- 검색 품질 (경고) --------------------------------------------------
    # 절 제목이 문서 제목뿐이면 어떤 질의와도 겹치지 않아 요약 출처로 선택되지 못한다.
    headings = re.findall(r"^#{2,4}\s+(.*)$", text, re.M)
    if not headings:
        warnings.append("절 제목이 없습니다. 문서 전체가 한 덩어리로 검색됩니다.")
    if "자주 묻는 질문" not in text:
        warnings.append("자주 묻는 질문 절이 없습니다. FAQ 직접 매칭의 대상이 되지 않습니다.")

    return errors, warnings


def main() -> int:
    paths = sorted(config.KNOWLEDGE_DIR.glob("*.md"))
    if not paths:
        print(f"{RED}문서를 찾을 수 없습니다: {config.KNOWLEDGE_DIR}{RESET}")
        return 1

    seen_ids: dict[str, str] = {}
    total_err = total_warn = 0
    for path in paths:
        errors, warnings = check(path, seen_ids)
        total_err += len(errors)
        total_warn += len(warnings)
        if not errors and not warnings:
            print(f"{GREEN}✓{RESET} {path.name}")
            continue
        mark = f"{RED}✗{RESET}" if errors else f"{YELLOW}!{RESET}"
        print(f"{mark} {path.name}")
        for e in errors:
            print(f"    {RED}오류{RESET} {e}")
        for w in warnings:
            print(f"    {YELLOW}경고{RESET} {w}")

    print(f"\n문서 {len(paths)}종 · 오류 {total_err} · 경고 {total_warn}")
    if total_err:
        print(f"{RED}오류가 있어 병합할 수 없습니다.{RESET} (온프레미스 설계서 §4.6.3)")
        return 1
    print(f"{GREEN}검증 통과.{RESET} 이어서 `python3 tools/run_eval.py`로 회귀를 확인하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
