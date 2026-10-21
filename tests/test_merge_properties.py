from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.engine.merge import three_way_document, three_way_text


@settings(max_examples=100, deadline=None)
@given(st.lists(st.text(alphabet="abcde", min_size=1, max_size=20), min_size=2, max_size=12))
def test_non_overlapping_start_and_end_edits_merge(lines: list[str]) -> None:
    base = "\n".join(lines) + "\n"
    current = "CURRENT\n" + base
    proposed = base + "PROPOSED\n"
    result = three_way_text(base, current, proposed)
    assert result.ok
    assert result.content == "CURRENT\n" + base + "PROPOSED\n"


@settings(max_examples=100, deadline=None)
@given(
    st.lists(
        st.text(alphabet="abcde", min_size=1, max_size=20),
        min_size=2,
        max_size=12,
        unique=True,
    )
)
def test_non_overlapping_line_edits_merge(lines: list[str]) -> None:
    base_lines = list(lines)
    current_lines = list(lines)
    proposed_lines = list(lines)
    current_lines[0] = "FIRST"
    proposed_lines[-1] = "LAST"
    base = "\n".join(base_lines) + "\n"
    current = "\n".join(current_lines) + "\n"
    proposed = "\n".join(proposed_lines) + "\n"
    result = three_way_text(base, current, proposed)
    assert result.ok
    assert "FIRST" in result.content
    assert "LAST" in result.content


@settings(max_examples=50, deadline=None)
@given(
    st.lists(st.text(alphabet="abcde", min_size=1, max_size=10), min_size=1, max_size=8),
    st.text(alphabet="XYZ", min_size=1, max_size=10),
    st.text(alphabet="XYZ", min_size=1, max_size=10),
)
def test_same_line_overlapping_edits_conflict(
    lines: list[str], left: str, right: str
) -> None:
    if left == right:
        return
    base = "\n".join(lines) + "\n"
    current_lines = list(lines)
    proposed_lines = list(lines)
    current_lines[0] = left
    proposed_lines[0] = right
    result = three_way_text(
        base, "\n".join(current_lines) + "\n", "\n".join(proposed_lines) + "\n"
    )
    assert not result.ok
    assert any(conflict.field == "content" for conflict in result.conflicts)


def test_frontmatter_three_way_merge_and_conflict() -> None:
    merged = three_way_document(
        base_content="a\n",
        current_content="a\nb\n",
        proposed_content="x\na\n",
        base_title="T",
        current_title="T",
        proposed_title="T2",
        base_frontmatter={"status": "DRAFT", "owner": "a"},
        current_frontmatter={"status": "DRAFT", "owner": "a", "risk": 1},
        proposed_frontmatter={"status": "DRAFT", "owner": "a", "timeframe": "1w"},
    )
    assert merged.ok
    assert merged.title == "T2"
    assert merged.frontmatter == {
        "status": "DRAFT",
        "owner": "a",
        "risk": 1,
        "timeframe": "1w",
    }
    assert "b" in merged.content and "x" in merged.content

    conflict = three_way_document(
        base_content="a\n",
        current_content="a\n",
        proposed_content="a\n",
        base_title="T",
        current_title="T-one",
        proposed_title="T-two",
        base_frontmatter={"status": "DRAFT"},
        current_frontmatter={"status": "READY"},
        proposed_frontmatter={"status": "BLOCKED"},
    )
    assert not conflict.ok
    assert {item.field for item in conflict.conflicts} >= {"title", "frontmatter.status"}
