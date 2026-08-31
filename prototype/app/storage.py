"""사용자 저장 항목 — 북마크·저장한 답변·체크리스트 (이용 가이드 §4).

세션과 달리 영속 저장한다. 다만 저장 전 민감정보 검사를 통과한 것만 저장하며,
본인만 조회·삭제할 수 있다. 인사담당자와 관리자도 열람할 수 없다.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone

from app import config
from app.policy import guardrails

_LOCK = threading.Lock()

KINDS = ("saved_answers", "checklist")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_user() -> dict:
    data = {kind: [] for kind in KINDS}
    # 각 목록을 마지막으로 열어본 시각. 이후 담긴 항목에 New 배지를 붙인다.
    data["last_viewed"] = {kind: None for kind in KINDS}
    return data


# 파일에 쓸 수 없는 환경(읽기 전용 파일시스템)에서는 프로세스 메모리에만 보관한다.
_MEMORY: dict = {}


def _load() -> dict:
    path = config.STORAGE_PATH
    if path is None:
        return _MEMORY
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    path = config.STORAGE_PATH
    if path is None:
        _MEMORY.clear()
        _MEMORY.update(data)
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        _MEMORY.clear()
        _MEMORY.update(data)


class StorageError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def get_all(employee_no: str) -> dict:
    with _LOCK:
        return _load().get(employee_no, _empty_user())


def _sensitive_check(*texts: str) -> None:
    for text in texts:
        if text and not guardrails.scan_for_storage(text):
            raise StorageError(guardrails.SAVE_BLOCK_MESSAGE)


def add(employee_no: str, kind: str, payload: dict) -> dict:
    if kind not in KINDS:
        raise StorageError("알 수 없는 저장 유형입니다.")

    if kind == "saved_answers":
        _sensitive_check(payload.get("query", ""), payload.get("summary", ""))
        item = {
            "id": uuid.uuid4().hex[:12],
            "query": payload.get("query", ""),
            "summary": payload.get("summary", ""),
            "actions": payload.get("actions", [])[:10],
            "citations": payload.get("citations", [])[:5],
            "cautions": payload.get("cautions", [])[:6],
            "notices": payload.get("notices", [])[:4],
            "personalization_basis": payload.get("personalization_basis", ""),
            "saved_at": _now(),
        }
    else:  # checklist
        _sensitive_check(payload.get("text", ""))
        item = {
            "id": uuid.uuid4().hex[:12],
            "text": payload.get("text", ""),
            "doc_id": payload.get("doc_id", ""),
            "doc_version": payload.get("doc_version", ""),
            "done": False,
            "done_at": None,
            "saved_at": _now(),
        }

    with _LOCK:
        data = _load()
        user = data.setdefault(employee_no, _empty_user())
        user.setdefault(kind, []).append(item)
        _save(data)
    return item


def toggle_checklist(employee_no: str, item_id: str, done: bool) -> dict | None:
    with _LOCK:
        data = _load()
        user = data.get(employee_no)
        if not user:
            return None
        for item in user.get("checklist", []):
            if item["id"] == item_id:
                item["done"] = bool(done)
                item["done_at"] = _now() if done else None
                _save(data)
                return item
    return None


def remove(employee_no: str, kind: str, item_id: str) -> bool:
    if kind not in KINDS:
        return False
    with _LOCK:
        data = _load()
        user = data.get(employee_no)
        if not user:
            return False
        before = len(user.get(kind, []))
        user[kind] = [i for i in user.get(kind, []) if i["id"] != item_id]
        if len(user[kind]) != before:
            _save(data)
            return True
    return False


def clear(employee_no: str, kind: str | None = None) -> None:
    with _LOCK:
        data = _load()
        if employee_no not in data:
            return
        if kind is None:
            data[employee_no] = _empty_user()
        elif kind in KINDS:
            data[employee_no][kind] = []
        _save(data)


def mark_seen(employee_no: str, kind: str) -> None:
    """목록을 열어본 시각을 기록한다. 이후 New 배지가 사라진다."""
    if kind not in KINDS:
        return
    with _LOCK:
        data = _load()
        user = data.setdefault(employee_no, _empty_user())
        user.setdefault("last_viewed", {})[kind] = _now()
        _save(data)


def annotate(employee_no: str, docs_by_id: dict) -> dict:
    """출처 문서 갱신 배지와 New 배지를 붙인다 (이용 가이드 §4)."""
    items = get_all(employee_no)
    last_viewed = items.get("last_viewed") or {}

    for kind in KINDS:
        seen_at = last_viewed.get(kind)
        for item in items.get(kind, []):
            item["is_new"] = bool(seen_at is None or item.get("saved_at", "") > seen_at)

    for item in items.get("checklist", []):
        doc = docs_by_id.get(item.get("doc_id"))
        saved_ver = item.get("doc_version")
        item["stale"] = (
            f"출처 문서가 v{doc.version}로 갱신되었습니다. 내용을 다시 확인해 주세요."
            if doc and saved_ver and doc.version != saved_ver else None
        )

    for item in items.get("saved_answers", []):
        stale = [
            f"{c.get('title')} v{docs_by_id[c['doc_id']].version}"
            for c in item.get("citations", [])
            if c.get("doc_id") in docs_by_id
            and c.get("version") != docs_by_id[c["doc_id"]].version
        ]
        item["stale"] = (
            f"근거 문서가 갱신되었습니다 ({', '.join(stale)}). 내용을 다시 확인해 주세요."
            if stale else None
        )

    items["counts"] = {
        "checklist": sum(1 for i in items.get("checklist", []) if not i.get("done")),
        "saved_answers": len(items.get("saved_answers", [])),
    }
    items["new_counts"] = {
        kind: sum(1 for i in items.get(kind, []) if i.get("is_new")) for kind in KINDS
    }
    return items
