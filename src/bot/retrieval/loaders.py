"""Load KB files (PDF, DOCX, Markdown, CSV) into a common block structure.

Every loader returns a list of Blocks in reading order:
  heading(level, text)  — document structure, used by the heading-aware chunker
  text(text)            — a paragraph or list item
  table(rows)           — each row rendered self-contained as "Header: value; Header: value"
                          so a single row (e.g. one tariff line) can be retrieved on its own.
CSV files additionally produce one `row` block per data row (one chunk per row).
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

BlockKind = Literal["heading", "text", "table", "row"]

# pdfplumber logs a warning per glyph for some embedded fonts; it is harmless noise.
logging.getLogger("pdfminer").setLevel(logging.ERROR)


@dataclass
class Block:
    kind: BlockKind
    text: str = ""
    level: int = 0
    rows: list[str] = field(default_factory=list)
    key: str = ""  # CSV row key (first column), used as the citation anchor


@dataclass
class LoadedDoc:
    meta: dict  # the manifest entry
    path: Path
    blocks: list[Block]

    @property
    def doc_id(self) -> str:
        return self.meta["doc_id"]


# --------------------------------------------------------------------------- helpers

_WS = re.compile(r"\s+")
# A line break right after "<alnum>-" is a wrapped token, not a word gap: "SRV-\n005A" -> "SRV-005A".
_WRAPPED_HYPHEN = re.compile(r"(?<=[A-Za-z0-9])-[ \t]*\n\s*")


def clean(text: str) -> str:
    text = _WRAPPED_HYPHEN.sub("-", text.replace("\u00a0", " "))
    return _WS.sub(" ", text).strip()


def join_lines(lines: list[str]) -> str:
    """Join wrapped PDF lines, re-attaching tokens split at a trailing hyphen."""
    return clean("\n".join(lines))


def strip_md_inline(text: str) -> str:
    """Remove inline markdown emphasis/code markers but keep the words."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text


def humanize(col: str) -> str:
    return clean(col.replace("_", " "))


def render_rows(header: list[str], rows: list[list[str]]) -> list[str]:
    """Render table rows as self-contained 'Header: value; ...' lines."""
    header = [clean(h or "") for h in header]
    out = []
    for r in rows:
        cells = [clean(c or "") for c in r]
        if not any(cells):
            continue
        parts = []
        for i, c in enumerate(cells):
            if not c:
                continue
            h = header[i] if i < len(header) and header[i] else ""
            parts.append(f"{h}: {c}" if h else c)
        out.append("; ".join(parts))
    return out


# --------------------------------------------------------------------------- Markdown


def load_markdown(path: Path) -> list[Block]:
    import frontmatter

    post = frontmatter.loads(path.read_text(encoding="utf-8-sig"))
    blocks: list[Block] = []
    para: list[str] = []
    table: list[list[str]] = []

    def flush_para() -> None:
        if para:
            blocks.append(Block("text", clean(strip_md_inline(" ".join(para)))))
            para.clear()

    def flush_table() -> None:
        if table:
            header, *body = table
            body = [r for r in body if not all(re.fullmatch(r":?-{2,}:?", c.strip()) for c in r)]
            header = [strip_md_inline(h) for h in header]
            body = [[strip_md_inline(c) for c in r] for r in body]
            blocks.append(Block("table", rows=render_rows(header, body)))
            table.clear()

    for raw in post.content.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            flush_para()
            table.append([c.strip() for c in s.strip("|").split("|")])
            continue
        flush_table()
        if m := re.match(r"^(#{1,6})\s+(.*)$", s):
            flush_para()
            blocks.append(
                Block("heading", clean(strip_md_inline(m.group(2))), level=len(m.group(1)))
            )
        elif not s or re.fullmatch(r"-{3,}|\*{3,}|_{3,}", s):
            flush_para()
        elif re.match(r"^([*+-]|\d+[.)])\s+", s):
            flush_para()
            item = re.sub(r"^([*+-]|\d+[.)])\s+", "", s)
            para.append("- " + item)
            flush_para()
        elif s.startswith(">"):
            para.append(s.lstrip("> "))
        else:
            para.append(s)
    flush_para()
    flush_table()
    return blocks


# --------------------------------------------------------------------------- DOCX


def load_docx(path: Path) -> list[Block]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(path))
    blocks: list[Block] = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = clean(p.text)
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            if m := re.match(r"Heading (\d)", style):
                blocks.append(Block("heading", text, level=int(m.group(1))))
            elif style == "Title":
                blocks.append(Block("heading", text, level=1))
            elif style.startswith("List"):
                blocks.append(Block("text", "- " + text))
            else:
                blocks.append(Block("text", text))
        elif tag == "tbl":
            t = Table(child, d)
            grid = [[c.text for c in row.cells] for row in t.rows]
            # Merged cells repeat in python-docx; collapse exact horizontal duplicates.
            grid = [[c for i, c in enumerate(r) if i == 0 or c != r[i - 1]] for r in grid]
            if grid:
                blocks.append(Block("table", rows=render_rows(grid[0], grid[1:])))
    return blocks


# --------------------------------------------------------------------------- PDF

TITLE_MIN_SIZE = 17.0
HEADING_MIN_SIZE = 12.5
FOOTER_MAX_SIZE = 9.0
RUNNING_HEADER = re.compile(r"^BRIGHTSMILE DENTAL CARE\s*·", re.IGNORECASE)


def _is_metadata_table(table: list[list[str | None]]) -> bool:
    first = " ".join(clean(c or "") for c in (table[0] if table else []))
    return first.lower().startswith("metadata field")


@dataclass
class _Line:
    text: str
    size: float
    bold: bool
    top: float
    bottom: float


def _pdf_page_items(
    page, prev_header: list[str] | None
) -> tuple[list[tuple[float, Block | _Line]], list[str] | None]:
    """Tables and text lines of one page, in vertical order (running header/footer removed)."""
    items: list[tuple[float, Block | _Line]] = []
    found = page.find_tables()
    bboxes = [t.bbox for t in found]

    for t in found:
        data = t.extract()
        if not data or _is_metadata_table(data):
            continue
        header = [clean(c or "") for c in data[0]]
        body = data[1:]
        # A table at the top of a page with the same width and no repeated header
        # is a continuation of the previous page's table.
        if (
            prev_header is not None
            and len(header) == len(prev_header)
            and header != prev_header
            and t.bbox[1] < 120
        ):
            body, header = data, prev_header
        prev_header = header
        items.append((t.bbox[1], Block("table", rows=render_rows(header, body))))

    for line in page.extract_text_lines(return_chars=True):
        text = clean(line["text"])
        cx, cy = (line["x0"] + line["x1"]) / 2, (line["top"] + line["bottom"]) / 2
        in_table = any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in bboxes)
        if not text or in_table or RUNNING_HEADER.match(text):
            continue
        chars = [c for c in line["chars"] if c["text"].strip()]
        size = max((c["size"] for c in chars), default=0)
        if size <= FOOTER_MAX_SIZE:
            continue  # page footer
        bold = all("Bold" in c["fontname"] for c in chars)
        items.append((line["top"], _Line(text, size, bold, line["top"], line["bottom"])))

    items.sort(key=lambda x: x[0])
    return items, prev_header


def _assemble_page(items: list[tuple[float, Block | _Line]], blocks: list[Block]) -> None:
    """Turn ordered lines into heading / paragraph blocks, appending to `blocks`."""
    para: list[str] = []
    last_bottom: float | None = None
    heading: tuple[float, Block] | None = None  # (font size, block) of the heading just emitted

    def flush(para: list[str] = para) -> None:
        if para:
            blocks.append(Block("text", join_lines(para)))
            para.clear()

    for _, it in items:
        if isinstance(it, Block):
            flush()
            blocks.append(it)
            last_bottom, heading = None, None
            continue
        if it.bold and it.size >= HEADING_MIN_SIZE:
            flush()
            # Wrapped heading lines (same size, adjacent) are one heading.
            if heading and heading[0] == it.size and blocks and blocks[-1] is heading[1]:
                heading[1].text = join_lines([heading[1].text, it.text])
            else:
                b = Block("heading", it.text, level=1 if it.size >= TITLE_MIN_SIZE else 2)
                blocks.append(b)
                heading = (it.size, b)
            last_bottom = it.bottom
            continue
        heading = None
        text = it.text
        gap = (it.top - last_bottom) if last_bottom is not None else 0
        if text[:1] in "•▪●◦-" or re.match(r"^\d+[.)]\s", text) or gap > 6:
            flush()
            text = re.sub(r"^[•▪●◦]\s*", "- ", text)
        para.append(text)
        last_bottom = it.bottom
    flush()


def load_pdf(path: Path) -> list[Block]:
    import pdfplumber

    blocks: list[Block] = []
    prev_header: list[str] | None = None
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            items, prev_header = _pdf_page_items(page, prev_header)
            _assemble_page(items, blocks)
    return blocks


# --------------------------------------------------------------------------- CSV


def load_csv(path: Path) -> list[Block]:
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = [humanize(h) for h in next(reader)]
    blocks: list[Block] = []
    for row in reader:
        if not any(c.strip() for c in row):
            continue
        rendered = render_rows(header, [row])
        if rendered:
            blocks.append(Block("row", text=rendered[0], key=clean(row[0])))
    return blocks


# --------------------------------------------------------------------------- dispatch

LOADERS = {".md": load_markdown, ".docx": load_docx, ".pdf": load_pdf, ".csv": load_csv}


def load_document(path: Path, meta: dict) -> LoadedDoc:
    loader = LOADERS.get(path.suffix.lower())
    if loader is None:
        raise ValueError(f"No loader for {path.suffix}: {path}")
    return LoadedDoc(meta=meta, path=path, blocks=loader(path))
