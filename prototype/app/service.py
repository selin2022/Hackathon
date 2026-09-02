"""질문 처리 파이프라인 (설계서 §4.6). API 계층과 검색·정책 계층을 잇는다."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date

from app import config
from app.answer import extractive, llm, templates
from app.policy import conflict as conflict_policy
from app.policy import guardrails, personalization
from app.retrieval import acl
from app.retrieval.hybrid import Retriever
from app.retrieval.tokenizer_ko import content_terms
from app.session import Session, query_hash

FOLLOWUP_STARTERS = ("그럼", "그러면", "그건", "그거", "이건", "저건", "거기", "그때", "더 ")
FOLLOWUP_SHORT_LEN = 15
NOUNLESS_RE = re.compile(r"^[가-힣]{0,3}(까지|어디|언제|누구|얼마|왜|뭐|무엇)[가-힣]*[?？]?$")


@dataclass
class ChatOutcome:
    ok: bool
    code: str | None
    answer: dict
    meta: dict


class ChatService:
    def __init__(self, retriever: Retriever | None = None):
        self.retriever = retriever or Retriever()
        self.users = self._load_users()
        self.log: list[dict] = []

    @staticmethod
    def _load_users() -> dict:
        raw = json.loads(config.PROFILES_PATH.read_text(encoding="utf-8"))
        return raw["users"]

    # --- §9.2 멀티턴 쿼리 재작성 -------------------------------------------
    def _rewrite(self, query: str, session: Session | None) -> str | None:
        if session is None or not session.last_query:
            return None
        stripped = query.strip()
        is_followup = (
            len(stripped) < FOLLOWUP_SHORT_LEN
            or stripped.startswith(FOLLOWUP_STARTERS)
            or bool(NOUNLESS_RE.match(stripped))
        )
        if not is_followup:
            return None
        terms = content_terms(session.last_query)[:3]
        if not terms:
            return None
        return f"{' '.join(terms)} {stripped}"

    def _record(self, session: Session | None, query: str, code: str, extra: dict) -> None:
        """질문 원문은 남기지 않는다 (§9.1)."""
        entry = {
            "ts": time.time(),
            "query_hash": query_hash(query),
            "employee_no": session.employee_no if session else None,
            "code": code,
        }
        entry.update(extra)
        self.log.append(entry)

    def handle(
        self,
        query: str,
        employee_no: str,
        session: Session | None = None,
        today: date | None = None,
    ) -> ChatOutcome:
        started = time.time()
        user = self.users.get(employee_no)
        if user is None:
            return ChatOutcome(False, "E_AUTH", templates.blocked_answer("사용자를 확인할 수 없습니다."), {})

        # [1] 입력 검증
        if not query or not query.strip():
            return ChatOutcome(False, "E_INPUT_INVALID", templates.blocked_answer("질문을 입력해 주세요."), {})
        if len(query) > config.MAX_QUERY_CHARS:
            return ChatOutcome(
                False, "E_INPUT_TOO_LONG",
                templates.blocked_answer(f"질문은 {config.MAX_QUERY_CHARS}자 이내로 입력해 주세요."), {},
            )

        # [2] 민감정보 검사
        scan = guardrails.scan_input(query)
        if scan.action in ("block", "third_party"):
            code = "E_SENSITIVE_BLOCKED"
            self._record(session, query, code, {"reason": scan.reason, "injection": scan.injection})
            return ChatOutcome(False, code, templates.blocked_answer(scan.message),
                               {"guardrail": scan.reason})

        # 마크업은 검색 질의에서 제거한다. 표시는 항상 textContent로 하므로 실행 위험은 없다.
        effective_query = guardrails.strip_markup(scan.text or query)
        if not effective_query:
            return ChatOutcome(False, "E_INPUT_INVALID",
                               templates.blocked_answer("질문을 입력해 주세요."), {})

        # [3] 멀티턴 재작성
        rewritten = self._rewrite(effective_query, session)
        search_query = rewritten or effective_query

        # [4]~[7] 검색 → 권한 필터 → 결합 → 게이트
        try:
            result = self.retriever.search(search_query, user, today=today)
        except Exception:  # pragma: no cover
            self._record(session, query, "E_UPSTREAM", {})
            return ChatOutcome(False, "E_UPSTREAM", templates.upstream_error_answer(), {})

        meta = {
            "mode": config.ANSWER_BACKEND,
            "embedding_backend": self.retriever.embedder.name,
            "rewritten_query": rewritten,
            "evidence": result.gate_signals,
            "filtered_out": result.filtered_out,
            "injection_detected": scan.injection,
        }

        if not result.evidence_sufficient:
            # 권한 차단도 근거 부족과 동일한 문구로 응답한다 (§5.4·§7.4)
            code = "E_FORBIDDEN" if result.acl_blocked and not result.candidates else "E_NO_EVIDENCE"
            self._record(session, query, code, {"signals": result.gate_signals})
            meta["elapsed_ms"] = int((time.time() - started) * 1000)
            return ChatOutcome(False, "E_NO_EVIDENCE", templates.no_evidence_answer(), meta)

        # §8.3 문서 충돌 해소
        candidates, unresolved = conflict_policy.resolve(
            result.candidates, self.retriever.docs_by_id
        )

        # §6 개인화
        pers = personalization.resolve(user, effective_query, today)

        # [8] 답변 구성
        answer = extractive.build(effective_query, candidates, pers, unresolved,
                                  faq_entry=result.faq)
        if result.faq is not None:
            meta["faq_match"] = {"doc_id": result.faq.doc_id,
                                 "question": result.faq.question,
                                 "score": result.faq_score}

        # [8-1] §7.5 LLM 병합 — 근거가 여러 문장에 흩어진 경우에만 값이 있다.
        # 개인화 판정 문구는 결정표 산출물이므로 건드리지 않는다.
        if config.ANSWER_BACKEND == "llm" and not answer.get("personalization"):
            sources = [extractive.clean_text(c.chunk.text) for c in candidates[:3]]
            merged, info = llm.merge(effective_query, sources)
            meta["llm_merge"] = info
            if merged:
                answer["summary"] = merged
                answer["merged_by_llm"] = True

        # [9] 2차 권한 재검증 — 인용된 청크가 전부 허용 범위인지 (§4.6)
        for cand in candidates:
            if not acl.is_visible(cand.chunk, user, today):
                self._record(session, query, "E_FORBIDDEN", {"stage": "post_answer"})
                meta["elapsed_ms"] = int((time.time() - started) * 1000)
                return ChatOutcome(False, "E_NO_EVIDENCE", templates.no_evidence_answer(), meta)

        # [10] 출력 검사 — 불변 조건과 시스템 프롬프트 유출
        violations = extractive.verify_no_hallucination(answer, candidates)
        if violations:
            self._record(session, query, "E_NO_EVIDENCE", {"violations": len(violations)})
            meta["elapsed_ms"] = int((time.time() - started) * 1000)
            meta["hallucination_guard"] = violations
            return ChatOutcome(False, "E_NO_EVIDENCE", templates.no_evidence_answer(), meta)

        if session is not None:
            session.add_turn(effective_query, answer.get("summary", ""))

        meta["elapsed_ms"] = int((time.time() - started) * 1000)
        self._record(session, query, "OK", {"docs": [c["doc_id"] for c in answer["citations"]]})
        return ChatOutcome(True, None, answer, meta)

    # --- 문서 목록 (§10) ---------------------------------------------------
    def list_documents(self, employee_no: str, today: date | None = None) -> list[dict]:
        user = self.users.get(employee_no, {})
        out = []
        for doc in self.retriever.documents:
            sample = next((c for c in self.retriever.chunks if c.doc_id == doc.doc_id), None)
            if sample is None or not acl.is_visible(sample, user, today):
                continue
            out.append({
                "doc_id": doc.doc_id,
                "title": doc.title,
                "version": doc.version,
                "category": doc.category,
                "subcategory": doc.subcategory,
                "owner_dept": doc.owner_dept,
                "published_at": doc.published_at,
                "valid_until": doc.valid_until,
                "status": doc.status,
                "demo_assumption": doc.demo_assumption,
                # 목록을 "전 직원 공통"과 "나에게만 열린 것"으로 가르기 위한 값.
                # 원본 acl_groups를 그대로 내보내지 않는다 — 화면에 필요한 것은 소속 구분이지
                # 그룹 이름이 아니며, 이름을 줄이는 편이 그룹 체계를 덜 드러낸다.
                "scope": self._scope(sample),
                "doc_type": doc.doc_type,
                "authority_level": doc.authority_level,
            })
        # 공통 먼저, 그 안에서 카테고리·문서ID 순. 화면에서 재정렬하지 않아도 되게 서버가 맞춘다.
        order = {"공통": 0, "소속": 1, "역할": 2}
        out.sort(key=lambda d: (order.get(d["scope"], 9), d["category"], d["doc_id"]))
        return out

    @staticmethod
    def _scope(chunk) -> str:
        if "all_employees" in chunk.acl_groups:
            return "공통"
        if any(g.startswith("dept:") for g in chunk.acl_groups):
            return "소속"
        return "역할"
