from __future__ import annotations

from app.mcp_server.server import mcp
from app.root_page import ROOT_PRIMITIVES, render_root_page

TOOL_COUNT = len(ROOT_PRIMITIVES)


def descriptor(dashboard: str | None = "/dashboard/") -> dict:
    return {
        "name": "AgentGraph",
        "version": "3.2.1",
        "docs": "/docs",
        "health": "/healthz",
        "rest": "/v1",
        "mcp": "/mcp/",
        "dashboard": dashboard,
        "agent_docs": "/v1/agent-docs",
        "first_call": "get_orientation",
        "primitives": list(ROOT_PRIMITIVES),
    }


async def test_root_primitive_list_is_the_full_mcp_surface():
    # Async on purpose: asyncio.run() would clear the event loop later tests use.
    registered = {tool.name for tool in await mcp.list_tools()}
    assert set(ROOT_PRIMITIVES) == registered
    assert len(set(ROOT_PRIMITIVES)) == len(ROOT_PRIMITIVES)
    assert "get_orientation" == ROOT_PRIMITIVES[0]
    assert "get_api_docs" == ROOT_PRIMITIVES[-1]


def test_human_root_page_keeps_json_shape_and_marks_console_entry():
    page = render_root_page(descriptor())
    assert "__CODE_LINES__" not in page
    assert "__PAYLOAD__" not in page
    assert 'id="flow"' in page
    assert 'href="/dashboard/"' in page
    assert 'class="entry"' in page
    assert f'<span class="number">{TOOL_COUNT}</span>' in page
    assert "COPY RESPONSE" in page
    assert 'href="/?format=json"' in page
    assert '<span class="value">3.2.1</span>' in page
    assert "agentgraph.session_token" in page
    assert "agentgraph.admin_token" in page
    assert '"/dashboard/login/"' in page


def test_human_root_page_without_dashboard_renders_null_without_a_link():
    page = render_root_page(descriptor(None))
    assert 'href="/dashboard/"' not in page
    assert '<span class="value">null</span>' in page
