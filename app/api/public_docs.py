"""Plain-HTML documentation fallback at /docs.

Production serves the Next.js docs site (dashboard export) at /docs. Where no
console build exists (tests, API-only deployments) this router renders the
same content from ``app.docs_site`` as accessible HTML, so /docs is always the
public documentation and never the operator Swagger UI (now /api-reference).
"""

from __future__ import annotations

import html
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(include_in_schema=False)


@lru_cache(maxsize=1)
def _site() -> dict[str, Any]:
    from app.docs_site import build

    return build()


def _t(value: Any, lang: str) -> str:
    if isinstance(value, dict):
        return value.get(lang) or value.get("en") or ""
    return str(value or "")


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _block(block: dict[str, Any], lang: str) -> str:
    kind = block.get("type")
    if kind == "p":
        return f"<p>{_e(_t(block['text'], lang))}</p>"
    if kind == "h":
        return f"<h2>{_e(_t(block['text'], lang))}</h2>"
    if kind == "code":
        label = f"<figcaption>{_e(_t(block['label'], lang))}</figcaption>" if block.get(
            "label") else ""
        return f"<figure>{label}<pre><code>{_e(block['code'])}</code></pre></figure>"
    if kind == "markdown":
        return f"<pre class=\"md\">{_e(block['text'])}</pre>"
    if kind == "list":
        tag = "ol" if block.get("ordered") else "ul"
        rows = "".join(f"<li>{_e(_t(item, lang))}</li>" for item in block["items"])
        return f"<{tag}>{rows}</{tag}>"
    if kind == "callout":
        return f"<aside class=\"callout\">{_e(_t(block['text'], lang))}</aside>"
    if kind == "link":
        return f"<p><a href=\"{_e(block['href'])}\">{_e(_t(block['text'], lang))}</a></p>"
    if kind == "table":
        head = "".join(f"<th scope=\"col\">{_e(_t(col, lang))}</th>" for col in block["columns"])
        body = "".join(
            "<tr>" + "".join(f"<td>{_e(_t(cell, lang))}</td>" for cell in row) + "</tr>"
            for row in block["rows"]
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    return ""


def _api(groups: list[dict[str, Any]]) -> str:
    parts = []
    for group in groups:
        parts.append(f"<h2 id=\"{_e(group['group'])}\">{_e(group['group'])}</h2>")
        for endpoint in group["endpoints"]:
            parts.append(
                f"<h3><code>{_e(endpoint['method'])} {_e(endpoint['path'])}</code></h3>"
                f"<p>{_e(endpoint['summary'])}</p>"
            )
    return "".join(parts)


def _errors(errors: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td><code>{_e(row['code'])}</code></td><td>{_e(row['http'])}</td>"
        f"<td>{_e(row['class'])}</td><td>{_e(row['rule'])}</td></tr>"
        for row in errors["codes"]
    )
    return ("<table><thead><tr><th scope=\"col\">code</th><th scope=\"col\">HTTP</th>"
            "<th scope=\"col\">class</th><th scope=\"col\">rule</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>")


def _render(slug: str, lang: str) -> str:
    site = _site()
    page = site["pages"][slug]
    title = next(
        _t(item["title"], lang)
        for group in site["nav"] for item in group["pages"] if item["slug"] == slug
    )
    current = ' aria-current="page"'

    def nav_item(item: dict[str, Any]) -> str:
        marker = current if item["slug"] == slug else ""
        return (f'<li><a href="/docs/{_e(item["slug"])}/?lang={lang}"{marker}>'
                f'{_e(_t(item["title"], lang))}</a></li>')

    nav = "".join(
        f'<li><span class="group">{_e(_t(group["title"], lang))}</span><ul>'
        + "".join(nav_item(item) for item in group["pages"])
        + "</ul></li>"
        for group in site["nav"]
    )
    body = "".join(_block(block, lang) for block in page["blocks"])
    if "api" in page:
        body += _api(page["api"])
    if "errors" in page:
        body += _errors(page["errors"])
    other = "zh" if lang == "en" else "en"
    switch = "中文" if lang == "en" else "English"
    html_lang = "zh-CN" if lang == "zh" else "en"
    return f"""<!doctype html>
<html lang="{html_lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} · AgentGraph Docs</title>
<style>
body{{margin:0;font:16px/1.6 system-ui,sans-serif;color:#1f2133;background:#fbfbfe}}
header{{display:flex;gap:16px;align-items:center;justify-content:space-between;
padding:12px 20px;border-bottom:1px solid #e4e4f0;background:#fff}}
a{{color:#4f46e5}}main{{display:flex;gap:32px;max-width:1100px;margin:0 auto;padding:24px 20px}}
nav ul{{list-style:none;padding-left:0}}nav .group{{font-weight:600;font-size:13px;color:#6b6f8a}}
article{{flex:1;min-width:0}}pre{{background:#14152b;color:#eef;padding:12px;overflow:auto;
border-radius:8px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #e4e4f0;
padding:6px;text-align:left;vertical-align:top}}.callout{{border-left:4px solid #6366f1;
padding:8px 12px;background:#eef}}@media (max-width:720px){{main{{flex-direction:column}}}}
</style></head><body>
<a href="#content">{_e("Skip to content" if lang == "en" else "跳到正文")}</a>
<header><strong>AgentGraph Docs</strong>
<span><a href="/docs/{_e(slug)}/?lang={other}">{switch}</a> ·
<a href="/dashboard/">{_e("Console" if lang == "en" else "控制台")}</a></span></header>
<main><nav aria-label="{_e("Documentation" if lang == "en" else "文档导航")}"><ul>{nav}</ul></nav>
<article id="content"><h1>{_e(title)}</h1><p>{_e(_t(page["summary"], lang))}</p>{body}</article>
</main></body></html>"""


@router.get("/docs")
async def docs_root() -> RedirectResponse:
    return RedirectResponse(url="/docs/", status_code=307)


@router.get("/docs/", response_class=HTMLResponse)
async def docs_index(lang: str = Query(default="en", pattern="^(en|zh)$")) -> HTMLResponse:
    return HTMLResponse(_render("introduction", lang))


@router.get("/docs/{slug}/", response_class=HTMLResponse)
async def docs_page(
    slug: str, lang: str = Query(default="en", pattern="^(en|zh)$")
) -> HTMLResponse:
    if slug not in _site()["pages"]:
        return HTMLResponse("<!doctype html><title>Not found</title><p>Not found. "
                            "<a href=\"/docs/\">Docs home</a></p>", status_code=404)
    return HTMLResponse(_render(slug, lang))
