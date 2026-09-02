"""골든셋 평가 러너 (pytest 없이 실행). tests/test_golden.py와 동일한 기준을 검사한다.

    python3 tools/run_eval.py [--verbose]
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.answer import extractive  # noqa: E402
from app.service import ChatService  # noqa: E402

TODAY = date(2026, 9, 2)   # 평가 기준일. 문서의 effective_from(§3.3)이 이 날짜보다
                           # 미래면 "시행 전"으로 검색에서 빠지므로, 문서 추가 시 함께 확인한다.
ANSWER_TYPES = {"normal", "personalized", "regulation"}
GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def load_golden() -> list[dict]:
    return [
        json.loads(line)
        for line in config.GOLDEN_SET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def render(answer: dict) -> str:
    parts = [answer.get("summary", "")]
    parts += answer.get("actions", [])
    parts += answer.get("cautions", [])
    parts += answer.get("notices", [])
    parts.append(answer.get("contact_message", ""))
    contact = answer.get("contact") or {}
    parts += [contact.get("dept", ""), contact.get("person", "")]
    for cite in answer.get("citations", []):
        parts += [cite["doc_id"], cite["title"], cite["section_path"], cite.get("excerpt", "")]
    if answer.get("personalization"):
        parts.append(answer["personalization"].get("message", ""))
        parts += answer["personalization"].get("flags", [])
    return "\n".join(p for p in parts if p)


def main() -> int:
    verbose = "--verbose" in sys.argv
    svc = ChatService()
    golden = load_golden()
    results = {i["id"]: (i, svc.handle(i["query"], i["user"], today=TODAY)) for i in golden}

    print(f"임베딩 백엔드: {svc.retriever.embedder.name} | 청크 {len(svc.retriever.chunks)}개 "
          f"| α={svc.retriever.alpha} | 게이트 cos≥{config.GATE_COS_TOP1} hybrid≥{config.GATE_HYBRID_TOP1}\n")

    failures: list[str] = []
    print(f"{'id':<7}{'type':<14}{'기대':<13}{'결과':<13}{'판정':<6} query")
    print("-" * 96)
    for item in golden:
        _, out = results[item["id"]]
        text = render(out.answer)
        expected = item["expected_behavior"]
        actual = "answer" if out.ok else ("blocked" if out.code == "E_SENSITIVE_BLOCKED" else "no_evidence")

        problems = []
        if expected == "answer":
            if not out.ok:
                problems.append(f"막힘({out.code})")
            elif not out.answer.get("citations"):
                problems.append("인용 없음")
        elif expected == "no_evidence" and not (not out.ok and out.code == "E_NO_EVIDENCE"):
            problems.append("차단 실패")
        elif expected == "blocked" and out.ok:
            problems.append("차단 실패")

        for frag in item.get("must_contain", []):
            if frag not in text:
                problems.append(f"누락:'{frag}'")
        for frag in item.get("must_not_contain", []):
            if frag in text:
                problems.append(f"노출:'{frag}'")

        # 요약 전용 단언. must_contain은 발췌문까지 훑으므로 "정답이 어딘가 있다"까지만
        # 보증한다. 정답이 **한 줄 요약으로 뽑혔는지**는 그것으로 검사할 수 없다.
        summary = out.answer.get("summary", "")
        for frag in item.get("summary_contains", []):
            if frag not in summary:
                problems.append(f"요약누락:'{frag}'")

        # 해야 할 일 전용 단언. "무엇을 제출하나요" 같은 질문은 정답이 발췌문이 아니라
        # **해야 할 일 항목**으로 정리되어 나와야 한다. must_contain만으로는 발췌문에
        # 원문이 섞여 있어도 통과하므로, 정리된 항목으로 나왔는지 따로 확인한다.
        actions_text = "\n".join(out.answer.get("actions", []))
        for frag in item.get("actions_contain", []):
            if frag not in actions_text:
                problems.append(f"해야할일누락:'{frag}'")
        for frag in item.get("summary_not_contains", []):
            if frag in summary:
                problems.append(f"요약노출:'{frag}'")

        ok = not problems
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"{item['id']:<7}{item['type']:<14}{expected:<13}{actual:<13}{mark:<15} {item['query'][:34]}")
        if problems:
            failures.append(item["id"])
            print(f"{'':>7}{RED}└ {', '.join(problems)}{RESET}")
        if verbose and out.ok:
            print(f"{'':>7}└ {out.answer['summary'][:90]}")

    # --- §11.3 지표 --------------------------------------------------------
    positives = [(g, o) for g, (i, o) in results.items() if i["type"] in ANSWER_TYPES]
    answered = [o for _, o in positives if o.ok]
    blocked_pos = [g for g, o in positives if not o.ok]
    forbidden_leak = [g for g, (i, o) in results.items() if i["type"] == "forbidden" and o.ok]

    hits = total = 0
    for gid, (item, out) in results.items():
        if item["type"] not in ANSWER_TYPES or not item["expected_docs"]:
            continue
        total += 1
        res = svc.retriever.search(item["query"], svc.users[item["user"]], today=TODAY)
        if set(item["expected_docs"]) & {c.chunk.doc_id for c in res.candidates}:
            hits += 1

    # 근거 이탈 검사 (§7.2 불변 조건)
    drift = 0
    for gid, (item, out) in results.items():
        if not out.ok:
            continue
        res = svc.retriever.search(item["query"], svc.users[item["user"]], today=TODAY)
        if extractive.verify_no_hallucination(out.answer, res.candidates):
            drift += 1

    cite_ok = all(o.answer.get("citations") for o in answered)
    metrics = [
        ("Recall@5", hits / max(1, total), 0.85, "ge"),
        ("인용 충실도", 1.0 if cite_ok else 0.0, 1.0, "ge"),
        ("근거 이탈", drift / max(1, len(answered)), 0.0, "le"),
        ("권한 차단률", 1.0 if not forbidden_leak else 0.0, 1.0, "ge"),
        ("오탐률", len(blocked_pos) / max(1, len(positives)), 0.10, "le"),
    ]
    print(f"\n{'지표':<14}{'값':>8}{'기준':>10}  판정")
    print("-" * 46)
    metric_fail = False
    for name, value, threshold, direction in metrics:
        ok = value >= threshold if direction == "ge" else value <= threshold
        metric_fail |= not ok
        sign = "≥" if direction == "ge" else "≤"
        mark = f"{GREEN}OK{RESET}" if ok else f"{RED}미달{RESET}"
        print(f"{name:<14}{value:>8.3f}{f'{sign}{threshold}':>10}  {mark}")

    if blocked_pos:
        print(f"\n{YELLOW}문서 보강 대상{RESET} (§11.4 4단계 — 차단률을 우선해 막힌 정상 문항): {blocked_pos}")

    failed = bool(failures) or metric_fail
    print(f"\n{'실패' if failed else '전체 통과'}: {len(golden) - len(failures)}/{len(golden)} 문항")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
