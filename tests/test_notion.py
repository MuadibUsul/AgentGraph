from __future__ import annotations

from app.engine.notion import (
    looks_like_notion_html,
    normalize_notion_content,
    notion_html_to_markdown,
)


def test_notion_table_and_blocks_convert_to_markdown() -> None:
    html = (
        '<table header-row="true">'
        "<tr><td>页面或功能</td><td>未登录</td><td>已登录</td></tr>"
        "<tr><td>`/zh`、`/en`</td><td>全站导航</td><td>相同并显示账户状态</td></tr>"
        "</table>"
        "<p>说明 <strong>重点</strong> 与 <code>x</code></p>"
        "<ul><li>第一项</li><li>第二项</li></ul>"
    )
    assert looks_like_notion_html(html)
    content, converted = normalize_notion_content(html)
    assert converted
    assert "<table" not in content
    assert "| 页面或功能 | 未登录 | 已登录 |" in content
    assert "| --- | --- | --- |" in content
    assert "说明 **重点** 与 `x`" in content
    assert "- 第一项" in content


def test_notion_html_removes_active_content_and_unsafe_links() -> None:
    html = (
        "<h1>Title</h1><script>alert(1)</script>"
        '<p><a href="javascript:alert(1)">bad</a> '
        '<a href="https://example.com">good</a></p>'
    )
    markdown = notion_html_to_markdown(html)
    assert "script" not in markdown.lower()
    assert "javascript:" not in markdown
    assert "[good](https://example.com)" in markdown


def test_markdown_is_not_treated_as_notion_html() -> None:
    content = "# Title\n\nA normal paragraph with **bold** and `code`."
    assert not looks_like_notion_html(content)
    converted, changed = normalize_notion_content(content)
    assert changed is False
    assert converted == content


def test_single_inline_html_tag_is_not_converted() -> None:
    assert not looks_like_notion_html("Use <code>doc://</code> to dereference.")


def test_markdown_document_with_embedded_notion_table_preserves_prose() -> None:
    content = (
        "# Tlines PRD\n"
        "- 版本：v1.0\n"
        "## 2. 权限矩阵\n"
        '<table header-row="true"><tr><td>页面</td><td>未登录</td></tr>'
        "<tr><td>`/zh`</td><td>全站导航</td></tr></table>\n"
        "## 3. 注册墙\n"
        "重要说明。\n"
    )
    converted, changed = normalize_notion_content(content)
    assert changed
    assert converted.startswith("# Tlines PRD\n- 版本：v1.0\n## 2. 权限矩阵")
    assert "| 页面 | 未登录 |" in converted
    assert "## 3. 注册墙\n重要说明。" in converted
    assert "<table" not in converted
