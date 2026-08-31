"""설계서 §4·§10 수치의 단일 출처. 임계값을 다른 모듈에 흩뿌리지 않는다."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
PROFILES_PATH = DATA_DIR / "profiles" / "demo-users.json"
GOLDEN_SET_PATH = DATA_DIR / "eval" / "golden-set.jsonl"
INDEX_DIR = DATA_DIR / "index"
def _resolve_storage_path():
    """쓰기 가능한 저장 경로를 고른다.

    서버리스 환경은 배포 디렉터리가 읽기 전용이고 /tmp만 쓸 수 있다. 그 경우 저장은
    인스턴스 수명 동안만 유지되므로 STORAGE_EPHEMERAL로 표시해 화면에 알린다.
    """
    override = os.getenv("STORAGE_DIR")
    candidates = [Path(override)] if override else [DATA_DIR / "storage", Path("/tmp/onboarding-rag")]
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return directory / "user-storage.json", directory != (DATA_DIR / "storage")
        except OSError:
            continue
    return None, True


STORAGE_PATH, STORAGE_EPHEMERAL = _resolve_storage_path()
STATIC_DIR = Path(__file__).resolve().parent / "static"

# --- §4.1 청킹 -------------------------------------------------------------
CHUNK_TARGET_CHARS = 400
CHUNK_MAX_CHARS = 700
CHUNK_MIN_CHARS = 120
CHUNK_OVERLAP_CHARS = 80

# --- §4.3 BM25 -------------------------------------------------------------
BM25_K1 = 1.2
BM25_B = 0.75

# --- §4.5 하이브리드 -------------------------------------------------------
HYBRID_ALPHA = float(os.getenv("HYBRID_ALPHA", "0.4"))  # bm25 가중치
CANDIDATE_TOP_N = 30
CONTEXT_TOP_K = 5
MAX_CHUNKS_PER_DOC = 3
# 1위 점수 대비 이 비율 미만인 후보는 컨텍스트에서 제외한다.
# 무관한 문서가 인용 목록에 딸려 들어가는 것을 막는다.
REL_SCORE_CUTOFF = 0.62

# --- §4.7 근거 충분성 게이트 ----------------------------------------------
# §11.4 튜닝 절차로 골든셋에서 실측해 확정한 값 (2026-08-31, tfidf_heading 백엔드, α=0.4).
# 정상 문항 최소 cos=0.310, 차단 대상 최대 cos=0.237 → 분리 구간의 중간값을 채택했다.
# 이 값에서 정상 16/16 통과, 차단 7/7 유지.
GATE_COS_TOP1 = float(os.getenv("GATE_COS_TOP1", "0.274"))
GATE_HYBRID_TOP1 = float(os.getenv("GATE_HYBRID_TOP1", "0.564"))
GATE_MIN_CITABLE = 1

# --- §9 세션 ---------------------------------------------------------------
SESSION_TTL_SECONDS = 30 * 60
SESSION_HISTORY_TURNS = 3

# --- §10.3 요청 제한 -------------------------------------------------------
MAX_QUERY_CHARS = 500
MAX_BODY_BYTES = 8 * 1024
RATE_LIMIT_PER_MINUTE = 20

# --- 백엔드 선택 -----------------------------------------------------------
EMBEDDING_BACKEND = os.getenv("EMBEDDING_BACKEND", "tfidf_heading")  # tfidf_heading | tfidf_svd | e5 | none
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
SVD_COMPONENTS = int(os.getenv("SVD_COMPONENTS", "128"))
# 청크 텍스트 앞 헤딩을 몇 번 반복해 가중할지 (§4.4). 실측으로 3회가 최적이었다.
HEADING_REPEAT = int(os.getenv("HEADING_REPEAT", "3"))

ANSWER_BACKEND = os.getenv("ANSWER_BACKEND", "extractive")  # extractive | llm
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")

# --- 데모 로그인 -----------------------------------------------------------
# 데모용 고정 비밀번호. 실제 인증이 아니며 운영 환경에서는 사내 SSO로 대체된다.
# 검증은 반드시 서버에서 한다 — 클라이언트에 비밀번호를 두지 않는다.
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "1234")
DEMO_PASSWORD_IS_DEFAULT = not os.getenv("DEMO_PASSWORD")

# --- 데모 고지 -------------------------------------------------------------
MODE_BADGE = "[프로토타입 데모] 합성 문서 기반 · 외부 LLM API 미사용 · 실제 사내 데이터 없음"
DEMO_CONTACT = {
    "dept": "인사팀",
    "person": "홍길동 대리",
    "email": "hr-demo@example.com",
    "is_demo": True,
}

# --- §7.4 고정 문구 --------------------------------------------------------
NO_EVIDENCE_SUMMARY = "확인 가능한 문서 근거가 충분하지 않습니다."
NO_EVIDENCE_CONTACT = (
    "인사팀 홍길동 대리(hr-demo@example.com)에게 확인해 주세요. "
    "본 연락처는 해커톤 데모용 가상 정보입니다."
)
