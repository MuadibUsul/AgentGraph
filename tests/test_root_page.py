from __future__ import annotations

from app.root_page import ROOT_PRIMITIVES, render_root_page


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


def test_root_primitive_list_is_the_full_mcp_surface():
    assert len(ROOT_PRIMITIVES) == 78
    assert "get_orientation" == ROOT_PRIMITIVES[0]
    assert "get_api_docs" == ROOT_PRIMITIVES[-1]


def test_human_root_page_keeps_json_shape_and_marks_console_entry():
    page = render_root_page(descriptor())
    assert "__CODE_LINES__" not in page
    assert "__PAYLOAD__" not in page
    assert 'id="flow"' in page
    assert 'href="/dashboard/"' in page
    assert 'class="entry"' in page
    assert '<span class="number">78</span>' in page
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
