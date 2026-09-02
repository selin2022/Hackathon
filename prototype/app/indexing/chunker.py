"""마크다운 섹션 기반 청킹 (설계서 §4.1)."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from app import config
from app.indexing.loader import Document

HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+|(?<=다\.)\s*|(?<=요\.)\s*|(?<=습니다\.)\s*")

# --- §4.1.1 조문 인식 --------------------------------------------------------
# 규정은 "제3조(연차휴가) ① … ② …" 구조다. 안내문과 달리 **항 중간에서 자르면 안 된다.**
# "① 연차는 15일로 한다"의 앞부분만 잘려 인용되면 조건절이 사라진 잘못된 안내가 된다.
ARTICLE_RE = re.compile(r"제\s*(\d+)\s*조")
HANG_MARKS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
HANG_SPLIT_RE = re.compile(f"(?=[{HANG_MARKS}])")


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    doc_version: str
    title: str
    section_path: str
    text: str
    char_start: int
    char_len: int
    acl_groups: list[str]
    audience: list[str]
    sensitivity: str
    category: str
    published_at: str
    valid_until: str
    demo_assumption: bool
    # --- §3.3 내규·규정
    doc_type: str = "안내문"
    authority_level: str = "안내문"
    effective_from: str = ""
    statutory: list[str] = field(default_factory=list)
    article_ref: str = ""            # "제3조 제2항" — 규정 문서의 인용 표기

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def embed_text(self) -> str:
        """§4.1 — 문서 제목과 섹션 경로를 본문 앞에 붙인다. 검색 품질에 가장 큰 영향."""
        return f"{self.section_path}\n{self.text}"


def _split_sections(body: str) -> list[tuple[str, str, int]]:
    """(section_path, text, char_start) 목록으로 나눈다."""
    lines = body.split("\n")
    stack: list[str] = []
    sections: list[tuple[str, str, int]] = []
    buf: list[str] = []
    buf_start = 0
    cursor = 0
    current_path = ""

    def flush(start: int) -> None:
        text = "\n".join(buf).strip()
        if text:
            sections.append((current_path, text, start))
        buf.clear()

    for line in lines:
        m = HEADING_RE.match(line)
        if m:
            flush(buf_start)
            level = len(m.group(1))
            title = m.group(2).strip()
            stack[:] = stack[: level - 1]
            while len(stack) < level - 1:
                stack.append("")
            stack.append(title)
            current_path = " > ".join(p for p in stack if p)
            buf_start = cursor + len(line) + 1
        else:
            buf.append(line)
        cursor += len(line) + 1
    flush(buf_start)
    return sections


def _is_table_block(text: str) -> bool:
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return False
    pipe_lines = sum(1 for l in lines if l.strip().startswith("|"))
    return pipe_lines >= max(2, len(lines) // 2)


def _split_long(text: str) -> list[str]:
    """700자 초과 시 문장 경계로 재분할. 표는 분할하지 않는다."""
    if len(text) <= config.CHUNK_MAX_CHARS or _is_table_block(text):
        return [text]

    parts = [p for p in SENTENCE_END_RE.split(text) if p and p.strip()]
    out: list[str] = []
    cur = ""
    for part in parts:
        candidate = (cur + " " + part).strip() if cur else part
        if len(candidate) > config.CHUNK_TARGET_CHARS and cur:
            out.append(cur)
            tail = cur[-config.CHUNK_OVERLAP_CHARS:] if config.CHUNK_OVERLAP_CHARS else ""
            cur = (tail + " " + part).strip()
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return out or [text]


def _split_articles(text: str) -> list[str]:
    """규정 본문을 **항 경계에서만** 나눈다.

    안내문용 `_split_long`은 문장 경계로 자르는데, 규정에 그대로 쓰면 한 항의 본문과
    단서(`다만 …`)가 서로 다른 청크로 갈라진다. 조건이 떨어져 나간 조문은 잘못된 안내다.
    항 하나가 상한을 넘어도 **자르지 않는다** — 법령 조항은 통째로 인용되어야 한다.
    """
    parts = [p.strip() for p in HANG_SPLIT_RE.split(text) if p.strip()]
    if len(parts) <= 1:
        return [text]

    out: list[str] = []
    cur = ""
    for part in parts:
        candidate = f"{cur}\n{part}" if cur else part
        if cur and len(candidate) > config.CHUNK_MAX_CHARS:
            out.append(cur)
            cur = part
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return out


def _article_ref(section_path: str, text: str) -> str:
    """인용 표기를 만든다. 규정은 절 이름이 아니라 **조문 번호**로 인용해야 한다."""
    m = ARTICLE_RE.search(section_path)
    if not m:
        return ""
    ref = f"제{int(m.group(1))}조"
    hangs = [HANG_MARKS.index(ch) + 1 for ch in text if ch in HANG_MARKS]
    if not hangs:
        return ref
    if len(hangs) == 1:
        return f"{ref} 제{hangs[0]}항"
    return f"{ref} 제{min(hangs)}~{max(hangs)}항"


def chunk_document(doc: Document) -> list[Chunk]:
    root = doc.title
    chunks: list[Chunk] = []
    raw: list[tuple[str, str, int]] = []

    for section_path, text, start in _split_sections(doc.body):
        # 본문 H1이 문서 제목과 같으면 경로가 중복되므로 제거한다.
        parts = [p for p in section_path.split(" > ") if p and p != root]
        path = " > ".join([root, *parts]) if parts else root
        splitter = _split_articles if doc.doc_type == "규정" else _split_long
        for piece in splitter(text):
            raw.append((path, piece, start))

    # 최소 길이 미만은 다음 조각과 병합 (§4.1).
    # 규정은 병합하지 않는다 — 짧은 조문을 옆 조문에 붙이면 인용이 "제3조"인지
    # "제4조"인지 흐려진다. 조문은 짧아도 독립된 단위다.
    merged: list[tuple[str, str, int]] = []
    for path, text, start in raw:
        if doc.doc_type == "규정":
            merged.append((path, text, start))
            continue
        if merged and len(text) < config.CHUNK_MIN_CHARS:
            p_path, p_text, p_start = merged[-1]
            if len(p_text) + len(text) <= config.CHUNK_MAX_CHARS:
                merged[-1] = (p_path, f"{p_text}\n{text}", p_start)
                continue
        merged.append((path, text, start))

    for i, (path, text, start) in enumerate(merged):
        chunks.append(
            Chunk(
                chunk_id=f"{doc.doc_id}#{i:04d}",
                doc_id=doc.doc_id,
                doc_version=doc.version,
                title=doc.title,
                section_path=path,
                text=text,
                char_start=start,
                char_len=len(text),
                acl_groups=list(doc.acl_groups),
                audience=list(doc.audience),
                sensitivity=doc.sensitivity,
                category=doc.category,
                published_at=doc.published_at,
                valid_until=doc.valid_until,
                demo_assumption=doc.demo_assumption,
                doc_type=doc.doc_type,
                authority_level=doc.authority_level,
                effective_from=doc.effective_from,
                statutory=list(doc.statutory),
                article_ref=_article_ref(path, text),
            )
        )
    return chunks


def chunk_documents(docs: list[Document]) -> list[Chunk]:
    out: list[Chunk] = []
    for doc in docs:
        out.extend(chunk_document(doc))
    return out
