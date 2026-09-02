"""§11.4 임계값 튜닝 절차.

골든셋 전 문항의 게이트 신호 분포를 측정하고, 정상 질문과 근거 없는 질문 사이에
분리 구간이 있는지 확인해 임계값을 제안한다. 추정이 아니라 측정으로 정한다.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.retrieval.embedder import build_embedder  # noqa: E402
from app.retrieval.hybrid import Retriever  # noqa: E402
from app.service import ChatService  # noqa: E402

TODAY = date(2026, 9, 2)   # 평가 기준일. 문서의 effective_from(§3.3)이 이 날짜보다
                           # 미래면 "시행 전"으로 검색에서 빠지므로, 문서 추가 시 함께 확인한다.
# 답변이 나와야 하는 유형 / 막혀야 하는 유형
ANSWER_TYPES = {"normal", "personalized", "regulation"}
BLOCK_TYPES = {"forbidden", "no_evidence"}


def load_golden() -> list[dict]:
    return [
        json.loads(line)
        for line in config.GOLDEN_SET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def measure(alpha: float, backend: str) -> dict:
    config.HYBRID_ALPHA = alpha
    # 게이트를 사실상 열어두고 신호만 관측한다.
    saved = (config.GATE_COS_TOP1, config.GATE_HYBRID_TOP1)
    config.GATE_COS_TOP1, config.GATE_HYBRID_TOP1 = -1.0, -1.0

    retriever = Retriever(embedder=build_embedder(backend))
    svc = ChatService(retriever=retriever)
    rows = []
    skipped: list[str] = []
    for item in load_golden():
        if item["type"] not in ANSWER_TYPES | BLOCK_TYPES:
            continue
        user = svc.users[item["user"]]
        res = retriever.search(item["query"], user, today=TODAY)
        # 점수 게이트가 담당하지 않는 문항은 분리 구간 계산에서 뺀다.
        # G5(미지 주제어)로 막히는 질의를 여기 섞으면 "분리 구간 없음"이 잘못 보고된다 —
        # 실제로 G-038("…동호회 지원금 지급일이 알고 싶어요")이 cos 0.2576으로 정상 문항
        # 최솟값을 넘지만, 차단은 점수가 아니라 주제어 부재로 이루어진다.
        if res.gate_signals.get("unknown_subjects"):
            skipped.append(item["id"])
            continue
        docs = {c.chunk.doc_id for c in res.candidates}
        hit = bool(set(item["expected_docs"]) & docs) if item["expected_docs"] else None
        rows.append({
            "id": item["id"],
            "type": item["type"],
            "cos": res.gate_signals.get("cos_top1", 0.0),
            "hybrid": res.gate_signals.get("hybrid_top1", 0.0),
            "recall_hit": hit,
        })

    config.GATE_COS_TOP1, config.GATE_HYBRID_TOP1 = saved
    return {"alpha": alpha, "backend": backend, "rows": rows, "skipped": skipped}


def summarize(result: dict) -> dict:
    rows = result["rows"]
    pos = [r for r in rows if r["type"] in ANSWER_TYPES]
    neg = [r for r in rows if r["type"] in BLOCK_TYPES]
    recall_rows = [r for r in pos if r["recall_hit"] is not None]
    recall = sum(1 for r in recall_rows if r["recall_hit"]) / max(1, len(recall_rows))

    out = {"alpha": result["alpha"], "backend": result["backend"], "recall@5": round(recall, 3)}
    for key in ("cos", "hybrid"):
        pos_min = min((r[key] for r in pos), default=0.0)
        neg_max = max((r[key] for r in neg), default=0.0)
        gap = pos_min - neg_max
        out[f"{key}_pos_min"] = round(pos_min, 4)
        out[f"{key}_neg_max"] = round(neg_max, 4)
        out[f"{key}_gap"] = round(gap, 4)
        out[f"{key}_suggested"] = round((pos_min + neg_max) / 2, 3) if gap > 0 else round(neg_max + 0.01, 3)
    return out


def main() -> None:
    backend = sys.argv[1] if len(sys.argv) > 1 else config.EMBEDDING_BACKEND
    print(f"임베딩 백엔드: {backend}\n")
    print(f"{'α':>5} {'Recall@5':>9} {'cos(정상최소)':>13} {'cos(비정상최대)':>15} "
          f"{'gap':>7} {'제안':>7} | {'hyb(정상최소)':>13} {'hyb(비정상최대)':>15} {'gap':>7} {'제안':>7}")
    print("-" * 118)
    best = None
    for alpha in (0.2, 0.3, 0.4, 0.5, 0.6):
        s = summarize(measure(alpha, backend))
        print(f"{s['alpha']:>5} {s['recall@5']:>9} {s['cos_pos_min']:>13} {s['cos_neg_max']:>15} "
              f"{s['cos_gap']:>7} {s['cos_suggested']:>7} | {s['hybrid_pos_min']:>13} "
              f"{s['hybrid_neg_max']:>15} {s['hybrid_gap']:>7} {s['hybrid_suggested']:>7}")
        score = (s["recall@5"], s["hybrid_gap"])
        if best is None or score > best[0]:
            best = (score, s)

    print("\n=== 권장값 ===")
    s = best[1]
    print(f"HYBRID_ALPHA     = {s['alpha']}")
    print(f"GATE_COS_TOP1    = {s['cos_suggested']}")
    print(f"GATE_HYBRID_TOP1 = {s['hybrid_suggested']}")
    print(f"Recall@5         = {s['recall@5']}")
    if s["hybrid_gap"] <= 0:
        print("\n⚠ 분리 구간이 없습니다. §11.4 4단계에 따라 차단률을 우선해 임계값을 올리고,")
        print("  그로 인해 막힌 정상 문항은 문서 보강 대상으로 기록합니다.")


if __name__ == "__main__":
    main()
