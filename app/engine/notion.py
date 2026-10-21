"""Detect and convert Notion-exported HTML into safe Markdown.

Notion's "Copy as HTML" / export output stores tables and blocks as raw HTML.
Markdown renderers correctly escape that by default, which makes uploaded
documents look like a wall of `<table><tr><td>...` text. This module normalizes
the common Notion block vocabulary to Markdown for ingestion, retrieval and
agent views, and leaves ordinary Markdown untouched.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

_HTML_TAG = re.compile(
    r"<\s*(?:table|thead|tbody|tr|td|th|h[1-6]|p|div|ul|ol|li|br|span|strong|em|blockquote|pre|code)\b",
    re.IGNORECASE,
)
_SAFE_URL_SCHEMES = {"http", "https", "mailto"}
_TABLE_RE = re.compile(r"<table[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s", re.MULTILINE)


def looks_like_notion_html(content: str) -> bool:
    """Conservatively detect a mostly-HTML document (not inline Markdown HTML)."""
    text = (content or "").strip()
    if not text:
        return False
    if "<table" in text.lower():
        return True
    if text.startswith("<") and _HTML_TAG.search(text):
        return True
    return len(_HTML_TAG.findall(text)) >= 3


def _safe_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme.lower() in _SAFE_URL_SCHEMES:
        return value
    return None


def _escape_table_cell(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value.replace("|", "\\|")


def _inline_text(node: Any) -> str:
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node))
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    children = "".join(_inline_text(child) for child in node.children)
    if name in ("strong", "b"):
        return f"**{children.strip()}**" if children.strip() else ""
    if name in ("em", "i"):
        return f"*{children.strip()}*" if children.strip() else ""
    if name in ("del", "s", "strike"):
        return f"~~{children.strip()}~~" if children.strip() else ""
    if name == "code":
        return f"`{children.strip()}`" if children.strip() else ""
    if name == "br":
        return " "
    if name == "a":
        label = children.strip() or (node.get("href") or "")
        href = _safe_url(node.get("href"))
        return f"[{label}]({href})" if href else label
    if name == "img":
        src = _safe_url(node.get("src"))
        alt = (node.get("alt") or "").strip()
        return f"![{alt}]({src})" if src else ""
    return children


def _render_table(node: Tag) -> str:
    rows = node.find_all("tr", recursive=True)
    matrix: list[list[str]] = []
    for row in rows:
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            cells = row.find_all(["th", "td"], recursive=True)
        matrix.append([_escape_table_cell(_inline_text(cell)) for cell in cells])
    matrix = [row for row in matrix if row]
    if not matrix:
        return ""
    width = max(len(row) for row in matrix)
    matrix = [row + [""] * (width - len(row)) for row in matrix]
    header = matrix[0]
    body = matrix[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines) + "\n\n"


def _render_list(node: Tag, *, ordered: bool, depth: int = 0) -> str:
    lines: list[str] = []
    index = 1
    for child in node.find_all("li", recursive=False):
        prefix = f"{index}. " if ordered else "- "
        body_parts: list[str] = []
        nested_parts: list[str] = []
        for item in child.children:
            if isinstance(item, Tag) and item.name.lower() in ("ul", "ol"):
                nested_parts.append(
                    _render_list(item, ordered=item.name.lower() == "ol", depth=depth + 1)
                )
            else:
                body_parts.append(_render_text(item))
        body = " ".join(part.strip() for part in body_parts if part.strip())
        body = re.sub(r"\n{2,}", " ", body).strip()
        lines.append("  " * depth + prefix + body)
        lines.extend(part.rstrip("\n") for part in nested_parts if part.strip())
        index += 1
    return "\n".join(lines) + "\n\n"


def _render_text(node: Any) -> str:
    """Convert a block-level node to Markdown."""
    if isinstance(node, NavigableString):
        text = re.sub(r"\s+", " ", str(node)).strip()
        return text
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    if name in (
        "script", "style", "noscript", "iframe", "object", "embed",
        "form", "input", "button",
    ):
        return ""
    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        return f"{'#' * level} {_inline_text(node).strip()}\n\n"
    if name == "p":
        text = _inline_text(node).strip()
        return f"{text}\n\n" if text else ""
    if name == "br":
        return "\n"
    if name in ("strong", "b", "em", "i", "del", "s", "strike", "code", "a", "span", "img"):
        return _inline_text(node)
    if name == "pre":
        code = node.get_text("\n").strip("\n")
        return f"```\n{code}\n```\n\n"
    if name == "blockquote":
        inner = "".join(_render_text(child) for child in node.children).strip()
        return "\n".join(f"> {line}" if line else ">" for line in inner.splitlines()) + "\n\n"
    if name == "hr":
        return "---\n\n"
    if name == "table":
        return _render_table(node)
    if name == "ul":
        return _render_list(node, ordered=False)
    if name == "ol":
        return _render_list(node, ordered=True)
    if name == "li":
        return _inline_text(node) + "\n"
    if name in ("figure", "figcaption"):
        return "".join(_render_text(child) for child in node.children)
    # div/section/article/notion blocks and unknown wrappers: recurse, but keep
    # Notion's one-div-per-line exports from collapsing into one paragraph.
    rendered = "".join(_render_text(child) for child in node.children)
    if rendered and not rendered.endswith("\n\n"):
        rendered += "\n\n"
    return rendered


def notion_html_to_markdown(content: str) -> str:
    """Convert Notion-style HTML to Markdown; return input if nothing useful."""
    soup = BeautifulSoup(content or "", "html.parser")
    for tag in soup.find_all(
        ["script", "style", "noscript", "iframe", "object", "embed", "form", "input", "button"]
    ):
        tag.decompose()
    root = soup.body or soup
    rendered = "".join(_render_text(child) for child in root.children)
    rendered = rendered.replace("\r\n", "\n").replace("\r", "\n")
    rendered = re.sub(r"[ \t]+\n", "\n", rendered)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
    return rendered or content


def _replace_table_fragments(content: str) -> str:
    def replace(match: re.Match[str]) -> str:
        markdown = notion_html_to_markdown(match.group(0)).strip()
        return f"\n\n{markdown}\n\n" if markdown else ""

    converted = _TABLE_RE.sub(replace, content)
    converted = re.sub(r"\n{3,}", "\n\n", converted).strip()
    return converted + "\n" if converted else content


def normalize_notion_content(content: str) -> tuple[str, bool]:
    """Normalize Notion HTML while preserving surrounding Markdown.

    A full Notion export is converted as a document. A Markdown document that
    contains an embedded Notion table gets only the table fragment replaced so
    headings, lists and prose keep their original formatting and newlines.
    """
    if not content or not looks_like_notion_html(content):
        return content, False
    has_markdown_blocks = bool(_MARKDOWN_HEADING.search(content))
    if content.lstrip().startswith("<") and not has_markdown_blocks:
        converted = notion_html_to_markdown(content)
    elif _TABLE_RE.search(content):
        converted = _replace_table_fragments(content)
    elif not has_markdown_blocks:
        converted = notion_html_to_markdown(content)
    else:
        return content, False
    if not converted.strip() or converted == content:
        return content, False
    return converted, True
