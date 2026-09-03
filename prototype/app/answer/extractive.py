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

# "아래 두 가지 서류를 제출합니다"처럼 뒤 내용을 가리키기만 하는 문장은 한 줄 요약으로
# 쓰면 정작 무엇인지 안 보인다. 이런 문장은 요약 후보에서 제외하고, 가리키는 대상
# (목록 항목)이 직접 답이 되도록 한다.
FORWARD_REF_RE = re.compile(r"아래|다음과 같이|다음의|다음 표")

# 절차·목록을 묻는 질문인가. "무엇을 제출하나요"는 여러 절에 걸친 목록이 답이지만,
# "검진 결과가 회사에 공유되나요"는 한 문장이 답이다. 후자에 절차 체크리스트와
# 다른 절의 발췌를 붙이면 묻지 않은 내용이 답의 대부분을 차지한다.
HANG_PREFIX_RE = re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]\s*")

# 규정이 "내 경우에" 적용되는지 묻는 질문. 이건 검색으로 답할 수 없는 유권해석이다.
# 조문을 보여주되 판정은 하지 않는다는 것을 답변에 명시한다 (§8.5).
INTERPRETATION_RE = re.compile(
    r"제?\s*경우|이런\s?경우|저는|제가|해당(되|하|됩|합)|인정되|포함되|가능한가|"
    r"되나요|봐도\s?되|써도\s?되|쓸\s?수\s?있"
)

PROCEDURE_QUERY_RE = re.compile(
    r"무엇|뭐|뭘|어떻게|어떤|어디|순서|절차|방법|준비|과정|"
    r"제출|신청|등록|수정|반납|이수|수강|예약|작성|"
    r"해야|들어야|하나요|받나요|필요한|알려"
)

# "어떤 순서로 신청하나요" 같이 절차 자체를 묻는 질문. 이런 질문만 여러 절의 번호
# 목록을 그대로 훑는다 — "무엇을 제출하나요"처럼 절차를 묻지 않는 질문에 다른 절의
# 절차가 섞여 들어오는 것을 막기 위한 구분이다.
HOW_QUERY_RE = re.compile(r"어떻게|순서|절차|방법|과정")
MAX_ACTIONS = 6
MAX_ACTIONS_PER_CHUNK = 3
MAX_CAUTIONS = 4


TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def _clean(text: str) -> str:
    text = re.sub(r"[*_`>#]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_text(text: str) -> str:
    """마크업을 뗀 평문. LLM 병합에 넘길 근거를 만들 때 쓴다 (§7.5)."""
    return _clean(text)


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


def _heading(chunk) -> str:
    """요약 출처 선택에 쓸 절 제목. 문서 서두(절 없음) 조각은 빈 문자열이다.

    서두 조각의 경로 마지막 칸은 문서 제목이라, 질의가 문서 주제어를 포함하기만 하면
    항상 최고 점수가 되어 실제 답이 있는 절을 밀어낸다. 서두는 개요·고지문이지 답이
    아니므로 제목 일치를 점수로 쳐 주지 않는다.
    """
    parts = [p for p in chunk.section_path.split(" > ") if p and p != chunk.title]
    return parts[-1] if parts else ""


def _overlap(sentence: str, query_tokens: set[str]) -> int:
    return len(set(tokenize(sentence)) & query_tokens)


EXCERPT_MAX = 320


def _excerpt(text: str, query_tokens: set[str], exclude: set[str] | None = None) -> str:
    """질의와 가장 관련 있는 구간을 원문 그대로 발췌한다.

    요약 한 문장은 '무엇을'은 담아도 '언제까지·몇 개'를 놓친다. 사용자가 근거를
    직접 확인할 수 있어야 하므로 관련 구간을 함께 보여준다.

    줄 수는 일부러 상한을 두지 않는다 — 절 하나가 짧은 항목 여러 개로 이뤄져
    있으면(예: "유의 사항"의 5개 항목) 절 뒤쪽의 사실이 잘려 나갈 수 있다
    (실측: 4줄로 제한하면 "며칠 걸리나요" 질문의 답인 "영업일" 줄이 밀려 나갔다).

    `exclude`: 이미 "해야 할 일"/요약으로 보여준 문장 집합. 같은 목록 항목이
    발췌에도 그대로 다시 나오면 "문서 전문이 두 번 나온다"는 인상을 준다
    (예: "통장 사본" 항목이 해야 할 일에도, 발췌 카드에도 그대로 나오는 경우).
    이미 보여준 항목을 담은 줄은 발췌 후보에서 뺀다 — 단, 그렇게 다 빼서
    보여줄 줄이 하나도 안 남으면(예: 절 전체가 이미 보여준 항목뿐인 경우는
    드물지만) 아무것도 안 보여주는 것보다는 원문을 그대로 보여주는 편이 낫다.
    """
    lines = [_clean_display(l) for l in text.split("\n") if l.strip()]
    lines = [l for l in lines if l]
    if exclude:
        filtered = [l for l in lines if not any(item and item in l for item in exclude)]
        if filtered:
            lines = filtered
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
    faq_entry=None,
) -> dict:
    """상위 청크에서 5단 구조를 조립한다. 새 문장을 만들지 않는다."""
    query_tokens = set(tokenize(query))
    exclude = list(personalization.exclude_terms) if personalization else []
    procedural = bool(PROCEDURE_QUERY_RE.search(query))

    # 사실 확인 질문이면 상대 점수가 낮은 절은 같은 문서라도 인용에서 뺀다.
    # 검색기는 1위 문서의 모든 절을 맥락으로 남겨 두는데(§4.5), 그 여유는 목록형
    # 질문에 필요한 것이지 한 문장이 답인 질문에는 소음이 된다.
    if not procedural and len(candidates) > 1:
        floor = max(c.hybrid_score for c in candidates) * config.REL_SCORE_CUTOFF
        kept = [c for c in candidates if c.hybrid_score >= floor]
        candidates = kept or candidates[:1]

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

    # FAQ 정답이 이미 확정된 경우엔 그 청크 하나로만 좁힌다 (§4.8). 같은 청크 안의
    # 다른 질문·답은 이번 질문과 무관한 원문이므로, 인용·해야 할 일 어디에도 새어
    # 나가면 안 된다.
    if faq_entry is not None:
        matched = [c for c in candidates if c.chunk.chunk_id == faq_entry.chunk.chunk_id]
        if matched:
            candidates = matched

    # ① 한 줄 요약
    # 먼저 **절 제목이 질의와 가장 잘 맞는 절**을 요약 출처로 고른다. 문장 겹침만 보면
    # FAQ 절이 이긴다 — 질문을 그대로 되풀이하기 때문이다. 절 제목이 그 절의 주제를
    # 가장 정확히 알려준다.
    # 비교 대상은 경로 전체가 아니라 **마지막 절 이름**이다. 경로에는 문서 제목이 들어
    # 있어서, 전체를 비교하면 모든 절이 같은 점수가 되어 순위가 그대로 유지된다.
    source = min(
        enumerate(candidates),
        key=lambda pair: (-_overlap(_heading(pair[1].chunk), query_tokens), pair[0]),
    )[1]
    sentences = [
        s for s in split_sentences(body_of[id(source)])
        if not QUESTION_RE.search(s) and not FORWARD_REF_RE.search(s)
    ]
    if not sentences:
        # 모든 문장이 전방 참조뿐이면(드묾) 어쩔 수 없이 되돌린다 — 아예 없는 것보다 낫다.
        sentences = [
            s for s in split_sentences(body_of[id(source)]) if not QUESTION_RE.search(s)
        ]
    summary = ""
    if sentences:
        summary = min(
            enumerate(sentences),
            key=lambda pair: (-_overlap(pair[1], query_tokens), pair[0]),
        )[1]

    # 규정 조문은 "① …" 으로 시작한다. 발췌에서는 항 번호가 근거 확인에 도움이 되지만
    # 한 줄 요약의 첫 글자로 나오면 문장이 잘린 것처럼 보인다. 요약에서만 뗀다.
    summary = HANG_PREFIX_RE.sub("", summary).strip()

    # §4.8 — FAQ가 사실상 같은 질문에 답하고 있으면 그 답을 그대로 쓴다.
    # 문장을 고르는 것보다 정확하다. FAQ 답도 문서 원문이므로 불변 조건은 유지된다
    # (표기만 _clean으로 맞춘다 — 원문의 `**`·백틱이 부분 문자열 검사를 어긋나게 한다).
    if faq_entry is not None:
        summary = _clean(faq_entry.answer)

    # ② 해야 할 일 — 리스트 항목 + 행동 동사로 끝나는 문장
    # 한 절이 할당량을 다 채우면 다른 절의 항목이 통째로 빠지므로 절당 상한을 둔다.
    # 요약으로 이미 보여준 문장은 제외한다 — 같은 문장이 두 번 나오면 안 된다.
    #
    # 어느 절을 훑을지가 핵심이다. "무엇을 제출하나요"(WHAT) 같은 질문은 근거가
    # source 절(요약을 뽑은 그 절) 하나에 있는데, 검색기는 1위 문서의 다른 절도
    # 맥락으로 남겨 둔다(§4.5) — 그중 "제출 *방법*" 같은 절의 번호 목록이 무조건
    # 담기면(원래 규칙: 번호 목록은 절차로 보고 무조건 담는다) 절차가 아닌 질문에
    # 절차가 섞여 든다. "어떤 순서로 하나요"(HOW) 같은 질문만 여러 절의 절차를
    # 그대로 훑는다.
    how_query = bool(HOW_QUERY_RE.search(query))
    action_candidates = candidates if how_query else [source]
    actions: list[str] = []
    seen: set[str] = {summary} if summary else set()
    # FAQ 답을 그대로 요약으로 쓴 경우 이미 완결된 문장이다. 같은 청크의 다른 질문·답
    # 문장에서 행동 동사가 우연히 걸려 무관한 항목이 섞이지 않도록 여기서 멈춘다.
    for cand in action_candidates if (procedural and faq_entry is None) else []:
        picked = 0
        is_source = cand is source
        for item, ordered in _list_items(body_of[id(cand)]):
            if item in seen or picked >= MAX_ACTIONS_PER_CHUNK:
                continue
            if _is_caution(item):
                continue
            # 번호 목록은 절차로 보고 그대로 담는다. 불릿 목록은 원칙적으로 행동
            # 문장만 담는다 — 대상 기준·설명 문장이 섞이면 안내가 틀어진다. 다만
            # source 절 자체는 이미 질문에 대한 답으로 확정된 절이므로, 행동
            # 동사로 안 끝나는 항목(예: "~가 필요합니다")도 그대로 담는다 —
            # "무엇을 제출하나요"의 답은 절차가 아니라 항목 자체다.
            if not ordered and not ACTION_VERB_RE.search(item) and not is_source:
                continue
            seen.add(item)
            actions.append(item)
            picked += 1
        for sent in split_sentences(body_of[id(cand)]):
            if picked >= MAX_ACTIONS_PER_CHUNK:
                break
            if (
                ACTION_VERB_RE.search(sent)
                and sent not in seen
                and not _is_caution(sent)
                and not FORWARD_REF_RE.search(sent)
            ):
                seen.add(sent)
                actions.append(sent)
                picked += 1
    actions = actions[:MAX_ACTIONS]

    # ③ 참고 문서 — 인용 구간 발췌를 함께 제공한다.
    # 요약 한 문장만으로는 기한·수량 같은 핵심 사실이 빠지므로, 근거 구간을 보여준다.
    # 같은 문서의 여러 절이 인용됐다면 절 목록과 발췌를 모두 보여준다.
    # 문서 단위로만 접으면 정작 답이 있는 절이 화면에서 사라진다.
    # "자주 묻는 질문" 절은 절 제목만으로 주제를 알 수 없어(§4.5 코멘트) 다른 질문에
    # 관한 Q&A까지 후보로 남는다. FAQ가 정답으로 확정된 경우(faq_entry)는 이미 그
    # 청크 하나로 좁혀졌으니 문제가 없지만, 그렇지 않은데도 이 절이 섞여 있으면
    # 이번 질문과 무관한 질문·답이 근거 발췌에 그대로 노출된다. 절차를 직접 묻는
    # 질문(how_query)이 아니고, FAQ가 아닌 다른 절이 이미 있다면 뺀다.
    citation_candidates = candidates
    if not how_query and faq_entry is None and len(candidates) > 1:
        non_faq = [c for c in candidates if _heading(c.chunk) != "자주 묻는 질문"]
        if non_faq:
            citation_candidates = non_faq

    citations: list[dict] = []
    index: dict[str, dict] = {}
    for cand in citation_candidates:
        key = f"{cand.chunk.doc_id}@{cand.chunk.doc_version}"
        if faq_entry is not None and cand.chunk.chunk_id == faq_entry.chunk.chunk_id:
            # FAQ 청크는 여러 질문·답이 한 절에 들어 있다. 발췌를 그 절 전체에서
            # 뽑으면 이번 질문과 무관한 다른 질문·답까지 원문으로 노출된다.
            # 실제로 답으로 쓴 질문·답 쌍만 발췌로 보여준다.
            excerpt = _clean(f"{faq_entry.question}\n{faq_entry.answer}")
        else:
            excerpt = _excerpt(body_of[id(cand)], query_tokens, exclude=seen)
        if key in index:
            entry = index[key]
            if _section_name(cand.chunk) not in entry["sections"]:
                entry["sections"].append(_section_name(cand.chunk))
                entry["excerpt"] = f"{entry['excerpt']}\n\n{excerpt}"
            ref = cand.chunk.article_ref
            if ref and ref not in entry["article_refs"]:
                entry["article_refs"].append(ref)
            continue
        entry = {
            "doc_id": cand.chunk.doc_id,
            "title": cand.chunk.title,
            "version": cand.chunk.doc_version,
            "section_path": cand.chunk.section_path,
            "sections": [_section_name(cand.chunk)],
            # 규정은 절 이름이 아니라 조문 번호로 인용해야 확인이 가능하다 (§3.3).
            "article_refs": [cand.chunk.article_ref] if cand.chunk.article_ref else [],
            "authority_level": cand.chunk.authority_level,
            "effective_from": cand.chunk.effective_from,
            "published_at": cand.chunk.published_at,
            "demo_assumption": cand.chunk.demo_assumption,
            "excerpt": excerpt,
        }
        index[key] = entry
        citations.append(entry)

    # ④ 주의·예외
    # FAQ 답을 그대로 쓴 경우 생략한다 — 같은 청크의 다른 질문·답 문장에서 표현이
    # 우연히 주의 문구 패턴에 걸려, 이번 질문과 무관한 주의사항이 붙는 것을 막는다.
    #
    # 훑는 범위는 candidates 전체가 아니라 citation_candidates다 — 근거 발췌에서
    # 이미 뺀 절(예: 이번 질문과 무관한 "자주 묻는 질문" 절)의 문장이 주의·예외에는
    # 새어 들어오면, 발췌-주의 중복 검사(아래)로도 못 잡는다. 인용에서 뺀 절이면
    # 주의·예외에도 근거로 쓰지 않는 것이 일관된 규칙이다.
    cautions: list[str] = []
    if faq_entry is None:
        for cand in citation_candidates:
            for sent in split_sentences(body_of[id(cand)]):
                if any(marker in sent for marker in CAUTION_MARKERS) and sent not in cautions:
                    cautions.append(sent)
            if len(cautions) >= MAX_CAUTIONS:
                break
        cautions = cautions[:MAX_CAUTIONS]
        # 이미 다른 자리에 그대로 나온 문장은 주의·예외에서 뺀다 — 같은 내용을 두 번
        # 보여주지 않는다는 원칙을 여기서도 적용한다.
        #   1) 요약·해야 할 일에 이미 쓴 문장(seen). 예: "두 서류 모두 사본으로
        #      제출하며, 원본은 제출하지 않습니다"가 요약으로 뽑히면서 동시에
        #      "하지 않습니다" 표현 때문에 주의·예외로도 걸리는 경우.
        #   2) 근거 발췌 카드에 그대로 나온 문장(excerpt_blob). 발췌 카드와 주의·예외
        #      목록에 같은 내용이 두 번 나오면 "문서 전문이 두 번 나온다"는 인상을 준다.
        excerpt_blob = "\n".join(c["excerpt"] for c in citations)
        cautions = [c for c in cautions if c not in seen and c not in excerpt_blob]

    notices: list[str] = []
    if any(c["demo_assumption"] for c in citations):
        notices.append(templates.DEMO_ASSUMPTION_NOTICE)
    if conflict:
        notices.append(templates.CONFLICT_NOTICE)

    # §8.5 규정 문서 고지. 인용된 조문이 법정 항목이면 반드시 알린다.
    statutory: list[str] = []
    for cand in candidates:
        for item in cand.chunk.statutory:
            if item not in statutory:
                statutory.append(item)
    if statutory:
        notices.append(templates.statutory_notice(statutory))
    # 규정 적용 여부를 묻는 질문에는 판정하지 않는다는 것을 명시한다.
    if any(c.chunk.doc_type == "규정" for c in candidates) and INTERPRETATION_RE.search(query):
        notices.append(templates.REGULATION_INTERPRETATION_NOTICE)

    answer = {
        "summary": summary,
        "actions": actions,
        "citations": citations,
        "cautions": cautions,
        "notices": notices,
        "contact": dict(config.DEMO_CONTACT),
        "contact_message": "",
    }

    if personalization and personalization.summary_override:
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
    # 개인화 문구는 결정표 산출물, LLM 병합 문장은 §7.5의 사실 검증을 이미 통과한 것이므로
    # 부분 문자열 검사 대상이 아니다. 합친 문장은 정의상 원문에 없다.
    if not answer.get("personalization") and not answer.get("merged_by_llm"):
        summary = answer.get("summary")
        if summary:
            checked.append(summary)
    for sent in checked:
        if sent and sent not in corpus:
            violations.append(sent)
    return violations
