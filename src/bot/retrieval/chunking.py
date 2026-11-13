"""Two chunkers over loader Blocks, for the chunking experiment (plan §7).

heading — follows document structure: one chunk per section (split further at block/row
          boundaries if it exceeds max_tokens; tiny sections merge forward). CSV: one chunk per row.
fixed   — fixed-size token windows with overlap over the flattened document text (baseline).

Token counts are an approximation (word + punctuation pieces); good enough for sizing chunks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bot.retrieval.loaders import Block, LoadedDoc

_PIECE = re.compile(r"\w+|[^\w\s]")


def count_tokens(text: str) -> int:
    return len(_PIECE.findall(text))


def slugify(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "section"


@dataclass
class Chunk:
    doc_id: str  # citation anchor, unique per (chunker, doc): 'DENT-019#2-master-2026-service'
    source_doc_id: str  # manifest id: 'DENT-019'
    title: str
    section: str  # heading path, or CSV row key
    content: str
    chunker: str
    metadata: dict = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return count_tokens(self.content)


def _block_lines(b: Block) -> list[str]:
    if b.kind == "table":
        return [f"- {r}" for r in b.rows]
    return [b.text] if b.text else []


def _unique(anchor: str, used: set[str]) -> str:
    if anchor not in used:
        used.add(anchor)
        return anchor
    i = 2
    while f"{anchor}-{i}" in used:
        i += 1
    used.add(f"{anchor}-{i}")
    return f"{anchor}-{i}"


def _base_meta(doc: LoadedDoc) -> dict:
    m = doc.meta
    return {
        "filename": m.get("filename"),
        "format": m.get("format"),
        "version": m.get("version"),
        "effective_date": m.get("effective_date"),
        "tags": m.get("tags", []),
    }


# --------------------------------------------------------------------------- heading chunker


@dataclass
class _Section:
    path: list[str]
    lines: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(count_tokens(x) for x in self.lines)


def _sections(doc: LoadedDoc) -> list[_Section]:
    """Group blocks under their nearest heading, tracking the heading path."""
    title = doc.meta.get("title", "")
    stack: list[tuple[int, str]] = []
    sections: list[_Section] = [_Section(path=[])]
    first_heading = True
    for b in doc.blocks:
        if b.kind == "heading":
            # The document's own title heading is already in the `title` column; don't nest under it.
            if first_heading and (b.level == 1 or b.text[:30].lower() in title.lower()):
                first_heading = False
                continue
            first_heading = False
            while stack and stack[-1][0] >= b.level:
                stack.pop()
            stack.append((b.level, b.text))
            sections.append(_Section(path=[t for _, t in stack]))
        else:
            sections[-1].lines.extend(_block_lines(b))
    return [s for s in sections if s.lines]


def chunk_heading(doc: LoadedDoc, max_tokens: int = 450, min_tokens: int = 40) -> list[Chunk]:
    title = doc.meta["title"]
    base = _base_meta(doc)
    used: set[str] = set()

    if all(b.kind == "row" for b in doc.blocks):  # CSV: one chunk per row
        out = []
        for b in doc.blocks:
            out.append(
                Chunk(
                    doc_id=_unique(f"{doc.doc_id}#{b.key or 'row'}", used),
                    source_doc_id=doc.doc_id,
                    title=title,
                    section=b.key,
                    content=b.text,
                    chunker="heading",
                    metadata=base,
                )
            )
        return out

    # Merge tiny sections into the following one so no chunk is a lone heading + one line.
    merged: list[_Section] = []
    carry: _Section | None = None
    for s in _sections(doc):
        if carry is not None:
            heading_line = carry.path[-1] if carry.path else ""
            s = _Section(
                path=s.path,
                lines=[heading_line, *carry.lines, *s.lines]
                if heading_line
                else [*carry.lines, *s.lines],
            )
            carry = None
        if s.tokens < min_tokens:
            carry = s
        else:
            merged.append(s)
    if carry is not None:
        if merged:
            merged[-1].lines.extend(carry.lines)
        else:
            merged.append(carry)

    out: list[Chunk] = []
    for s in merged:
        section = " > ".join(s.path)
        heading = s.path[-1] if s.path else ""
        anchor = f"{doc.doc_id}#{slugify(s.path[-1]) if s.path else 'intro'}"
        # Split oversized sections at line (paragraph / table-row) boundaries.
        parts: list[list[str]] = [[]]
        size = 0
        for line in s.lines:
            n = count_tokens(line)
            if parts[-1] and size + n > max_tokens:
                parts.append([])
                size = 0
            parts[-1].append(line)
            size += n
        for i, lines in enumerate(parts):
            body = "\n".join(lines)
            content = f"{heading}\n{body}" if heading else body
            out.append(
                Chunk(
                    doc_id=_unique(anchor, used),
                    source_doc_id=doc.doc_id,
                    title=title,
                    section=section,
                    content=content,
                    chunker="heading",
                    metadata={**base, "part": i + 1, "parts": len(parts)},
                )
            )
    return out


# --------------------------------------------------------------------------- fixed chunker


def chunk_fixed(doc: LoadedDoc, size_tokens: int = 300, overlap_tokens: int = 50) -> list[Chunk]:
    lines: list[str] = []
    for b in doc.blocks:
        lines.extend([b.text] if b.kind in ("heading", "row") else _block_lines(b))
    words = "\n".join(lines).split()
    costs = [count_tokens(w) for w in words]

    out: list[Chunk] = []
    start = 0
    n = 0
    while start < len(words):
        end, total = start, 0
        while end < len(words) and (total + costs[end] <= size_tokens or end == start):
            total += costs[end]
            end += 1
        n += 1
        out.append(
            Chunk(
                doc_id=f"{doc.doc_id}#w{n:03d}",
                source_doc_id=doc.doc_id,
                title=doc.meta["title"],
                section=f"window {n}",
                content=" ".join(words[start:end]),
                chunker="fixed",
                metadata={**_base_meta(doc), "word_start": start},
            )
        )
        if end >= len(words):
            break
        # Step back by ~overlap_tokens worth of words.
        back, k = 0, end
        while k > start + 1 and back < overlap_tokens:
            k -= 1
            back += costs[k]
        start = k
    return out


def chunk_document(doc: LoadedDoc, strategy: str, cfg: dict) -> list[Chunk]:
    if strategy == "heading":
        return chunk_heading(doc, **cfg["heading"])
    if strategy == "fixed":
        return chunk_fixed(doc, **cfg["fixed"])
    raise ValueError(f"Unknown chunking strategy: {strategy}")
