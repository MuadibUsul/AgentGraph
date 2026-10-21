"""Markdown-aware chunking for hybrid retrieval and section-level rebase."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class Chunk:
    index: int
    heading: str | None
    content: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def _split_section(text: str, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    pieces: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > size:
            if current:
                pieces.append(current)
                current = ""
            start = 0
            while start < len(paragraph):
                end = min(start + size, len(paragraph))
                if end < len(paragraph):
                    boundary = max(
                        paragraph.rfind("\n", start, end),
                        paragraph.rfind("。", start, end),
                        paragraph.rfind(". ", start, end),
                    )
                    if boundary > start + size // 2:
                        end = boundary + 1
                piece = paragraph[start:end].strip()
                if piece:
                    pieces.append(piece)
                if end >= len(paragraph):
                    break
                start = max(end - overlap, start + 1)
            continue
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= size:
            current = candidate
        else:
            if current:
                pieces.append(current)
            current = paragraph
    if current:
        pieces.append(current)
    if overlap <= 0:
        return pieces
    with_overlap: list[str] = []
    for piece in pieces:
        if with_overlap and overlap:
            tail = with_overlap[-1][-overlap:]
            piece = f"{tail}\n{piece}" if tail else piece
        with_overlap.append(piece)
    return with_overlap


def split_markdown(
    content: str,
    *,
    chunk_size: int = 1_800,
    overlap: int = 200,
) -> list[Chunk]:
    """Split Markdown by headings, then by paragraphs when a section is long."""
    chunks: list[Chunk] = []
    heading: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        text = "\n".join(buffer).strip()
        if text:
            for piece in _split_section(text, chunk_size, overlap):
                chunks.append(
                    Chunk(index=len(chunks), heading=heading, content=piece)
                )
        buffer = []

    for line in content.splitlines():
        match = _HEADING.match(line)
        if match:
            flush()
            heading = match.group(2).strip()[:256]
            continue
        buffer.append(line)
    flush()
    if not chunks:
        chunks.append(Chunk(index=0, heading=None, content=content.strip() or " "))
    return chunks


def extract_section(content: str, heading: str) -> str | None:
    """Return the body under a Markdown heading (excluding the heading line)."""
    lines = content.splitlines()
    start: int | None = None
    level: int | None = None
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if not match:
            continue
        if start is None:
            if match.group(2).strip() == heading:
                start = index + 1
                level = len(match.group(1))
            continue
        if level is not None and len(match.group(1)) <= level:
            return "\n".join(lines[start:index]).strip()
    if start is None:
        return None
    return "\n".join(lines[start:]).strip()
