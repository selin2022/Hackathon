"""HTTP 계층 (설계서 §10·§12).

FastAPI 대신 Starlette + Pydantic으로 구성했다. FastAPI가 얹혀 있는 바로 그 조합이며,
이 환경에서 FastAPI 패키지를 받을 수 없어 같은 구성 요소를 직접 사용한다.
라우팅·검증 코드는 FastAPI와 사실상 동일하므로 교체 비용이 거의 없다.
"""
from __future__ import annotations

import json
from datetime import date

from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from app import config, storage
from app.policy import personalization
from app.service import ChatService
from app.session import COOKIE_NAME, RateLimiter, Session, new_session, secret_is_configured

service = ChatService()
rate_limiter = RateLimiter()

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cache-Control": "no-store",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response


# --- 요청 모델 (§10) --------------------------------------------------------
class LoginRequest(BaseModel):
    employee_no: str = Field(min_length=1, max_length=32)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=config.MAX_QUERY_CHARS)


class SaveRequest(BaseModel):
    kind: str = Field(pattern="^(bookmarks|saved_answers|checklist)$")
    payload: dict = Field(default_factory=dict)


class ToggleRequest(BaseModel):
    item_id: str = Field(min_length=1, max_length=32)
    done: bool


def error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "message": message}, status_code=status)


async def read_json(request) -> dict:
    body = await request.body()
    if len(body) > config.MAX_BODY_BYTES:
        raise ValueError("요청 본문이 너무 큽니다.")
    if not body:
        return {}
    return json.loads(body)


def current_session(request) -> Session | None:
    """쿠키 서명을 검증해 세션을 복원한다. 서버에 상태를 두지 않는다."""
    return Session.from_cookie(request.cookies.get(COOKIE_NAME))


def set_session_cookie(response, session: Session) -> None:
    response.set_cookie(
        COOKIE_NAME, session.to_cookie(),
        httponly=True, samesite="strict", path="/", max_age=config.SESSION_TTL_SECONDS,
    )


def public_user(employee_no: str) -> dict:
    """마스킹된 값만 내보낸다 (§6.3)."""
    user = service.users[employee_no]
    return {
        "employee_no_masked": personalization.mask_employee_no(user["employee_no"]),
        "display_name": user["display_name"],
        "role": user["role"],
        "dept": user["dept"],
        "employment_type": user["employment_type"],
    }


# --- 라우트 -----------------------------------------------------------------
async def get_users(request):
    """데모 로그인 화면에 보여줄 가상 사용자 목록."""
    return JSONResponse({
        "ok": True,
        "mode_badge": config.MODE_BADGE,
        "storage_persistent": not config.STORAGE_EPHEMERAL,
        "users": [
            {
                "employee_no": no,
                "employee_no_masked": personalization.mask_employee_no(no),
                "display_name": u["display_name"],
                "role": u["role"],
                "dept": u["dept"],
                "employment_type": u["employment_type"],
                "hire_date": u["hire_date"],
            }
            for no, u in service.users.items()
        ],
    })


async def login(request):
    try:
        payload = LoginRequest(**await read_json(request))
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        return error("E_INPUT_INVALID", str(exc), 400)
    if payload.employee_no not in service.users:
        return error("E_AUTH", "사용자를 확인할 수 없습니다.", 401)

    session = new_session(payload.employee_no)
    response = JSONResponse({"ok": True, "user": public_user(payload.employee_no)})
    set_session_cookie(response, session)
    return response


async def logout(request):
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


async def me(request):
    session = current_session(request)
    if session is None:
        return error("E_AUTH", "세션이 없습니다. 다시 로그인해 주세요.", 401)
    return JSONResponse({"ok": True, "user": public_user(session.employee_no)})


async def chat(request):
    session = current_session(request)
    if session is None:
        return error("E_AUTH", "세션이 없습니다. 다시 로그인해 주세요.", 401)
    if rate_limiter.check(session.employee_no):
        return error("E_RATE_LIMIT", "요청이 많습니다. 잠시 후 다시 시도해 주세요.", 429)
    rate_limiter.record(session.employee_no)

    try:
        payload = ChatRequest(**await read_json(request))
    except ValueError as exc:
        code = "E_INPUT_TOO_LONG" if "max_length" in str(exc) else "E_INPUT_INVALID"
        return error(code, "질문 형식을 확인해 주세요.", 400)

    # 사용자 식별은 쿠키에서만 읽는다. 본문의 사번은 신뢰하지 않는다 (§12.2).
    outcome = service.handle(payload.message, session.employee_no, session=session)
    status = 400 if outcome.code == "E_SENSITIVE_BLOCKED" else 200
    response = JSONResponse(
        {"ok": outcome.ok, "code": outcome.code, "answer": outcome.answer, "meta": outcome.meta},
        status_code=status,
    )
    # 대화 맥락이 바뀌었으면 갱신된 쿠키를 내려보낸다.
    if session.changed:
        set_session_cookie(response, session)
    return response


async def documents(request):
    session = current_session(request)
    if session is None:
        return error("E_AUTH", "세션이 없습니다.", 401)
    return JSONResponse({"ok": True, "documents": service.list_documents(session.employee_no)})


async def get_storage(request):
    session = current_session(request)
    if session is None:
        return error("E_AUTH", "세션이 없습니다.", 401)
    items = storage.annotate_staleness(session.employee_no, service.retriever.docs_by_id)
    return JSONResponse({"ok": True, "items": items})


async def post_storage(request):
    session = current_session(request)
    if session is None:
        return error("E_AUTH", "세션이 없습니다.", 401)
    try:
        payload = SaveRequest(**await read_json(request))
    except (ValidationError, ValueError, json.JSONDecodeError):
        return error("E_INPUT_INVALID", "저장 요청 형식을 확인해 주세요.", 400)
    try:
        item = storage.add(session.employee_no, payload.kind, payload.payload)
    except storage.StorageError as exc:
        return error("E_SENSITIVE_BLOCKED", exc.message, 400)
    return JSONResponse({"ok": True, "item": item})


async def toggle_checklist(request):
    session = current_session(request)
    if session is None:
        return error("E_AUTH", "세션이 없습니다.", 401)
    try:
        payload = ToggleRequest(**await read_json(request))
    except (ValidationError, ValueError, json.JSONDecodeError):
        return error("E_INPUT_INVALID", "요청 형식을 확인해 주세요.", 400)
    item = storage.toggle_checklist(session.employee_no, payload.item_id, payload.done)
    if item is None:
        return error("E_INPUT_INVALID", "항목을 찾을 수 없습니다.", 404)
    return JSONResponse({"ok": True, "item": item})


async def delete_storage(request):
    session = current_session(request)
    if session is None:
        return error("E_AUTH", "세션이 없습니다.", 401)
    kind = request.path_params["kind"]
    item_id = request.path_params["item_id"]
    if item_id == "all":
        storage.clear(session.employee_no, None if kind == "all" else kind)
        return JSONResponse({"ok": True})
    ok = storage.remove(session.employee_no, kind, item_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


async def health(request):
    return JSONResponse({
        "ok": True,
        "documents": len(service.retriever.documents),
        "chunks": len(service.retriever.chunks),
        "embedding_backend": service.retriever.embedder.name,
        "answer_backend": config.ANSWER_BACKEND,
        "alpha": service.retriever.alpha,
        "gate": {"cos_top1": config.GATE_COS_TOP1, "hybrid_top1": config.GATE_HYBRID_TOP1},
        "storage_persistent": not config.STORAGE_EPHEMERAL,
        "session_secret_configured": secret_is_configured(),
        "mode_badge": config.MODE_BADGE,
    })


async def index(request):
    return FileResponse(config.STATIC_DIR / "index.html", headers=SECURITY_HEADERS)


routes = [
    Route("/", index),
    Route("/api/users", get_users),
    Route("/api/session", login, methods=["POST"]),
    Route("/api/session", logout, methods=["DELETE"]),
    Route("/api/me", me),
    Route("/api/chat", chat, methods=["POST"]),
    Route("/api/documents", documents),
    Route("/api/storage", get_storage),
    Route("/api/storage", post_storage, methods=["POST"]),
    Route("/api/storage/checklist/toggle", toggle_checklist, methods=["POST"]),
    Route("/api/storage/{kind}/{item_id}", delete_storage, methods=["DELETE"]),
    Route("/api/health", health),
    Mount("/static", app=StaticFiles(directory=str(config.STATIC_DIR)), name="static"),
]

app = Starlette(routes=routes, middleware=[Middleware(SecurityHeadersMiddleware)])
