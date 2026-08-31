"""추출 기반 답변 구성 (설계서 §7.2).

불변 조건: 요약·해야 할 일·주의 항목의 모든 문장은 인용 청크 텍스트의 부분 문자열이어야
한다. 이것이 "할루시네이션 불가"의 기계적 보증이다.
"""
from __future__ import annotations

import re

from app import config
from app.answer import templates
from app.policy.personalization import Personalization
from app.retrieval.tokenizer_ko import tokenize

SENT_SPLIT_RE = re.compile(r"(?<=니다\.)\s*|(?<=합니다\.)\s*|(?<=습니다\.)\s*|(?<=[.!?])\s+")
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.*)$")
ORDERED_ITEM_RE = re.compile(r"^\s*\d+\.\s+(.*)$")
TABLE_ROW_RE = re.compile(r"^\s*\|")
ACTION_VERB_RE = re.compile(
    r"(제출|신청|이수|확인|등록|작성|선택|입력|업로드|반납|수강|예약|문의|준비|조회)"
    r"(합니다|하세요|해야 합니다|하시기 바랍니다)\.?$"
)
CAUTION_MARKERS = ("단,", "다만", "예외", "주의", "확인 필요", "달라질 수 있",
                   "않을 수 있", "지연될 수 있", "포함되지 않습니다", "하지 않습니다",
                   "필요할 수 있", "권합니다")
# FAQ 섹션의 질문 문장은 답이 아니므로 요약 후보에서 제외한다.
QUESTION_RE = re.compile(r"(\?|나요|까요|는가요|은가요|런가요|가요)\s*$")
MAX_ACTIONS = 6
MAX_ACTIONS_PER_CHUNK = 3
MAX_CAUTIONS = 4


TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def _clean(text: str) -> str:
    text = re.sub(r"[*_`>#]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_display(line: str) -> str:
    """화면 표시용 정리. 표 구분선은 버리고, 표 행은 가운데점으로 잇는다."""
    if TABLE_SEP_RE.match(line):
        return ""
    stripped = line.strip()
    if stripped.startswith("|"):
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        return _clean(" · ".join(c for c in cells if c))
    return _clean(line)


def split_sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("|") or line.startswith("---"):
            continue
        m = LIST_ITEM_RE.match(line)
        if m:
            out.append(_clean(m.group(1)))
            continue
        for sent in SENT_SPLIT_RE.split(line):
            sent = _clean(sent)
            if len(sent) >= 8:
                out.append(sent)
    return out


def _list_items(text: str) -> list[tuple[str, bool]]:
    """(항목, 번호목록 여부). 번호 목록은 절차이고, 불릿 목록은 대개 기준·설명이다."""
    items: list[tuple[str, bool]] = []
    for line in text.split("\n"):
        stripped = line.strip()
        m = LIST_ITEM_RE.match(stripped)
        if m:
            cleaned = _clean(m.group(1))
            if len(cleaned) >= 6:
                items.append((cleaned, bool(ORDERED_ITEM_RE.match(stripped))))
    return items


def _is_caution(sentence: str) -> bool:
    return any(marker in sentence for marker in CAUTION_MARKERS)


def _section_name(chunk) -> str:
    """표시용 절 이름. 경로에서 문서 제목을 뺀다 — 인용 카드에 이미 제목이 있다."""
    parts = [p for p in chunk.section_path.split(" > ") if p and p != chunk.title]
    return " > ".join(parts) if parts else chunk.title


def _overlap(sentence: str, query_tokens: set[str]) -> int:
    return len(set(tokenize(sentence)) & query_tokens)


EXCERPT_MAX = 320


def _excerpt(text: str, query_tokens: set[str]) -> str:
    """질의와 가장 관련 있는 구간을 원문 그대로 발췌한다.

    요약 한 문장은 '무엇을'은 담아도 '언제까지·몇 개'를 놓친다. 사용자가 근거를
    직접 확인할 수 있어야 하므로 관련 구간을 함께 보여준다.
    """
    lines = [_clean_display(l) for l in text.split("\n") if l.strip()]
    lines = [l for l in lines if l]
    if not lines:
        return ""
    scored = sorted(range(len(lines)), key=lambda i: -_overlap(lines[i], query_tokens))
    center = scored[0] if scored and _overlap(lines[scored[0]], query_tokens) else 0

    out: list[str] = [lines[center]]
    total = len(lines[center])
    left, right = center - 1, center + 1
    while total < EXCERPT_MAX and (left >= 0 or right < len(lines)):
        if right < len(lines) and total + len(lines[right]) <= EXCERPT_MAX:
            out.append(lines[right])
            total += len(lines[right])
            right += 1
        elif left >= 0 and total + len(lines[left]) <= EXCERPT_MAX:
            out.insert(0, lines[left])
            total += len(lines[left])
            left -= 1
        else:
            break
    return "\n".join(out)


def filter_branches(text: str, exclude_terms: list[str]) -> str:
    """이 사용자에게 해당하지 않는 분기의 내용을 본문에서 제거한다.

    빈 줄로 구분된 블록 단위로 판단한다. 온보딩 문서는 유형별 안내와 FAQ가 모두
    블록으로 구분되므로, 블록에 다른 유형의 이름이 있으면 그 블록 전체를 뺀다.
    계속 근로자에게 이직자 제출 서류를 보여주는 것은 잘못된 안내다.
    """
    if not exclude_terms:
        return text
    blocks = re.split(r"\n\s*\n", text)
    kept = [b for b in blocks if not any(term in b for term in exclude_terms)]
    return "\n\n".join(kept)


def build(
    query: str,
    candidates: list,
    personalization: Personalization | None = None,
    conflict: bool = False,
) -> dict:
    """상위 청크에서 5단 구조를 조립한다. 새 문장을 만들지 않는다."""
    query_tokens = set(tokenize(query))
    exclude = list(personalization.exclude_terms) if personalization else []

    # 분기 필터를 적용한 본문을 후보별로 미리 만들어 둔다. 이후 모든 추출은 이 본문만 본다.
    texts: list[tuple] = []
    for cand in candidates:
        section_name = cand.chunk.section_path.split(" > ")[-1]
        if exclude and any(term in section_name for term in exclude):
            continue
        body = filter_branches(cand.chunk.text, exclude)
        if body.strip():
            texts.append((cand, body))
    if not texts:
        texts = [(candidates[0], candidates[0].chunk.text)]
    candidates = [c for c, _ in texts]
    body_of = {id(c): b for c, b in texts}

    # ① 한 줄 요약
    # 먼저 **절 제목이 질의와 가장 잘 맞는 절**을 요약 출처로 고른다. 문장 겹침만 보면
    # FAQ 절이 이긴다 — 질문을 그대로 되풀이하기 때문이다. 절 제목이 그 절의 주제를
    # 가장 정확히 알려준다.
    # 비교 대상은 경로 전체가 아니라 **마지막 절 이름**이다. 경로에는 문서 제목이 들어
    # 있어서, 전체를 비교하면 모든 절이 같은 점수가 되어 순위가 그대로 유지된다.
    source = min(
        enumerate(candidates),
        key=lambda pair: (
            -_overlap(pair[1].chunk.section_path.split(" > ")[-1], query_tokens),
            pair[0],
        ),
    )[1]
    sentences = [
        s for s in split_sentences(body_of[id(source)]) if not QUESTION_RE.search(s)
    ]
    summary = ""
    if sentences:
        summary = min(
            enumerate(sentences),
            key=lambda pair: (-_overlap(pair[1], query_tokens), pair[0]),
        )[1]

    # ② 해야 할 일 — 리스트 항목 + 행동 동사로 끝나는 문장
    # 한 절이 할당량을 다 채우면 다른 절의 항목이 통째로 빠지므로 절당 상한을 둔다.
    # 요약으로 이미 보여준 문장은 제외한다 — 같은 문장이 두 번 나오면 안 된다.
    actions: list[str] = []
    seen: set[str] = {summary} if summary else set()
    for cand in candidates:
        picked = 0
        for item, ordered in _list_items(body_of[id(cand)]):
            if item in seen or picked >= MAX_ACTIONS_PER_CHUNK:
                continue
            if _is_caution(item):
                continue
            # 번호 목록은 절차로 보고 그대로 담는다. 불릿 목록은 행동 문장만 담는다 —
            # 대상 기준·설명 문장이 "해야 할 일"에 섞이면 안내가 틀어진다.
            if not ordered and not ACTION_VERB_RE.search(item):
                continue
            seen.add(item)
            actions.append(item)
            picked += 1
        for sent in split_sentences(body_of[id(cand)]):
            if picked >= MAX_ACTIONS_PER_CHUNK:
                break
            if ACTION_VERB_RE.search(sent) and sent not in seen and not _is_caution(sent):
                seen.add(sent)
                actions.append(sent)
                picked += 1
    actions = actions[:MAX_ACTIONS]

    # ③ 참고 문서 — 인용 구간 발췌를 함께 제공한다.
    # 요약 한 문장만으로는 기한·수량 같은 핵심 사실이 빠지므로, 근거 구간을 보여준다.
    # 같은 문서의 여러 절이 인용됐다면 절 목록과 발췌를 모두 보여준다.
    # 문서 단위로만 접으면 정작 답이 있는 절이 화면에서 사라진다.
    citations: list[dict] = []
    index: dict[str, dict] = {}
    for cand in candidates:
        key = f"{cand.chunk.doc_id}@{cand.chunk.doc_version}"
        excerpt = _excerpt(body_of[id(cand)], query_tokens)
        if key in index:
            entry = index[key]
            if _section_name(cand.chunk) not in entry["sections"]:
                entry["sections"].append(_section_name(cand.chunk))
                entry["excerpt"] = f"{entry['excerpt']}\n\n{excerpt}"
            continue
        entry = {
            "doc_id": cand.chunk.doc_id,
            "title": cand.chunk.title,
            "version": cand.chunk.doc_version,
            "section_path": cand.chunk.section_path,
            "sections": [_section_name(cand.chunk)],
            "published_at": cand.chunk.published_at,
            "demo_assumption": cand.chunk.demo_assumption,
            "excerpt": excerpt,
        }
        index[key] = entry
        citations.append(entry)

    # ④ 주의·예외
    cautions: list[str] = []
    for cand in candidates:
        for sent in split_sentences(body_of[id(cand)]):
            if any(marker in sent for marker in CAUTION_MARKERS) and sent not in cautions:
                cautions.append(sent)
        if len(cautions) >= MAX_CAUTIONS:
            break
    cautions = cautions[:MAX_CAUTIONS]

    notices: list[str] = []
    if any(c["demo_assumption"] for c in citations):
        notices.append(templates.DEMO_ASSUMPTION_NOTICE)
    if conflict:
        notices.append(templates.CONFLICT_NOTICE)

    answer = {
        "summary": summary,
        "actions": actions,
        "citations": citations,
        "cautions": cautions,
        "notices": notices,
        "contact": dict(config.DEMO_CONTACT),
        "contact_message": "",
    }

    if personalization:
        answer["personalization"] = personalization.to_dict()
        # 개인화 판정 결과는 문서 인용이 아니라 결정표 산출물이므로 별도 필드로 둔다.
        answer["summary"] = personalization.message or summary
        answer["personalization_basis"] = f"판정 근거: {personalization.basis}"
        for flag in personalization.flags:
            if flag not in notices:
                notices.append(flag)

    return answer


def verify_no_hallucination(answer: dict, candidates: list) -> list[str]:
    """불변 조건 검사 (§7.2). 위반한 문장 목록을 반환한다.

    개인화 문구는 결정표 산출물이므로 검사 대상에서 제외한다.
    """
    corpus = " ".join(_clean(c.chunk.text) for c in candidates)
    violations: list[str] = []
    checked = list(answer.get("actions", [])) + list(answer.get("cautions", []))
    if not answer.get("personalization"):
        summary = answer.get("summary")
        if summary:
            checked.append(summary)
    for sent in checked:
        if sent and sent not in corpus:
            violations.append(sent)
    return violations
