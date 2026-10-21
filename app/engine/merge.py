"""Line-based three-way merge for non-overlapping document edits.

The merge is deliberately conservative: any overlapping replacement, or two
different insertions at the same boundary, is reported as a conflict. Callers
return a 409 with the conflict list instead of guessing a merge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any


@dataclass(frozen=True)
class MergeConflict:
    field: str
    reason: str
    base: Any = None
    current: Any = None
    proposed: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "reason": self.reason,
            "base": self.base,
            "current": self.current,
            "proposed": self.proposed,
        }


@dataclass
class MergeResult:
    content: str
    title: str | None = None
    frontmatter: dict[str, Any] = field(default_factory=dict)
    conflicts: list[MergeConflict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts


@dataclass(frozen=True)
class _Hunk:
    start: int
    end: int
    replacement: tuple[str, ...]
    side: str


def _hunks(base: list[str], other: list[str], side: str) -> list[_Hunk]:
    matcher = SequenceMatcher(None, base, other, autojunk=False)
    result: list[_Hunk] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        result.append(_Hunk(i1, i2, tuple(other[j1:j2]), side))
    return result


def _same(hunk: _Hunk, other: _Hunk) -> bool:
    return (
        hunk.start == other.start
        and hunk.end == other.end
        and hunk.replacement == other.replacement
    )


def _overlaps(left: _Hunk, right: _Hunk) -> bool:
    if _same(left, right):
        return False
    left_insert = left.start == left.end
    right_insert = right.start == right.end
    if left_insert and right_insert:
        return left.start == right.start
    if left_insert:
        return right.start < left.start < right.end
    if right_insert:
        return left.start < right.start < left.end
    return left.start < right.end and right.start < left.end


def three_way_text(base: str, current: str, proposed: str) -> MergeResult:
    if current == base:
        return MergeResult(content=proposed)
    if proposed == base or current == proposed:
        return MergeResult(content=current)

    base_lines = base.splitlines(keepends=True)
    current_lines = current.splitlines(keepends=True)
    proposed_lines = proposed.splitlines(keepends=True)
    ours = _hunks(base_lines, current_lines, "current")
    theirs = _hunks(base_lines, proposed_lines, "proposed")

    conflicts: list[MergeConflict] = []
    seen: list[_Hunk] = []
    for left in ours:
        for right in theirs:
            if _same(left, right):
                if left not in seen:
                    seen.append(left)
                continue
            if _overlaps(left, right):
                conflicts.append(
                    MergeConflict(
                        field="content",
                        reason="overlapping text edits",
                        base="".join(base_lines[left.start : left.end]),
                        current="".join(left.replacement),
                        proposed="".join(right.replacement),
                    )
                )
    if conflicts:
        return MergeResult(content=current, conflicts=conflicts)

    edits = list(ours) + list(theirs)
    deduped: list[_Hunk] = []
    for edit in edits:
        if edit in seen:
            continue
        if any(_same(edit, existing) for existing in deduped):
            continue
        deduped.append(edit)
    deduped.sort(key=lambda item: (item.start, item.end, 0 if item.side == "current" else 1))

    output: list[str] = []
    position = 0
    for edit in deduped:
        if edit.start < position:
            conflicts.append(
                MergeConflict(
                    field="content",
                    reason="overlapping text edits after ordering",
                    base="".join(base_lines[edit.start : edit.end]),
                    current="".join(edit.replacement),
                )
            )
            break
        output.extend(base_lines[position : edit.start])
        output.extend(edit.replacement)
        position = edit.end
    if conflicts:
        return MergeResult(content=current, conflicts=conflicts)
    output.extend(base_lines[position:])
    return MergeResult(content="".join(output))


def _merge_value(
    field_name: str,
    base: Any,
    current: Any,
    proposed: Any,
    conflicts: list[MergeConflict],
) -> Any:
    if current == base:
        return proposed
    if proposed == base or current == proposed:
        return current
    if isinstance(base, dict) and isinstance(current, dict) and isinstance(proposed, dict):
        merged: dict[str, Any] = {}
        for key in set(base) | set(current) | set(proposed):
            sub_base = base.get(key, None)
            sub_current = current.get(key, None)
            sub_proposed = proposed.get(key, None)
            merged[key] = _merge_value(
                f"{field_name}.{key}",
                sub_base,
                sub_current,
                sub_proposed,
                conflicts,
            )
        return merged
    conflicts.append(
        MergeConflict(
            field=field_name,
            reason="both sides changed this field differently",
            base=base,
            current=current,
            proposed=proposed,
        )
    )
    return current


def three_way_document(
    *,
    base_content: str,
    current_content: str,
    proposed_content: str,
    base_title: str,
    current_title: str,
    proposed_title: str,
    base_frontmatter: dict[str, Any],
    current_frontmatter: dict[str, Any],
    proposed_frontmatter: dict[str, Any],
) -> MergeResult:
    text = three_way_text(base_content, current_content, proposed_content)
    conflicts = list(text.conflicts)
    title = _merge_value(
        "title", base_title, current_title, proposed_title, conflicts
    )
    frontmatter = _merge_value(
        "frontmatter",
        base_frontmatter or {},
        current_frontmatter or {},
        proposed_frontmatter or {},
        conflicts,
    )
    return MergeResult(
        content=text.content,
        title=title,
        frontmatter=frontmatter if isinstance(frontmatter, dict) else {},
        conflicts=conflicts,
    )
