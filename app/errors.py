"""Domain errors and a consistent JSON error envelope."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """An expected, client-facing failure."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers or {}

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"error": error}


class QuotaExceeded(ApiError):
    def __init__(self, resource: str, limit: int) -> None:
        super().__init__(
            402,
            "quota_exceeded",
            f"workspace {resource} quota exhausted ({limit})",
            details={"resource": resource, "limit": limit},
        )


class BudgetExceeded(ApiError):
    def __init__(self, category: str, used: float, limit: float) -> None:
        super().__init__(
            402,
            "budget_exceeded",
            f"workspace {category} budget exhausted ({used:g}/{limit:g})",
            details={"category": category, "used": used, "limit": limit},
        )


class RateLimited(ApiError):
    def __init__(self, retry_after: int = 60) -> None:
        super().__init__(
            429,
            "rate_limited",
            "request rate limit exceeded; retry after the indicated window",
            headers={"Retry-After": str(retry_after)},
        )


class ProposalRequired(ApiError):
    def __init__(self, doc_key: str, policy: str) -> None:
        super().__init__(
            409,
            "proposal_required",
            "this path requires a reviewed proposal; submit one instead of "
            "writing the document directly",
            details={"doc_key": doc_key, "policy": policy},
        )


class ProposalConflict(ApiError):
    def __init__(self, proposal_id: str, conflicts: list[dict[str, Any]]) -> None:
        super().__init__(
            409,
            "proposal_conflict",
            "the proposal base is stale and its changes overlap with the "
            "current document; re-read and rebase before applying",
            details={"proposal_id": proposal_id, "conflicts": conflicts},
        )


class RebaseConflict(ApiError):
    def __init__(self, resource: str, conflicts: list[dict[str, Any]]) -> None:
        super().__init__(
            409,
            "rebase_conflict",
            f"{resource} changed since base_version and the edits overlap",
            details={"resource": resource, "conflicts": conflicts},
        )


class TaintBlocked(ApiError):
    def __init__(self, doc_key: str, sources: list[dict[str, Any]]) -> None:
        super().__init__(
            409,
            "taint_blocked",
            "agent verification cannot bless a document derived from tainted "
            "or untrusted sources; a human reviewer must verify it",
            details={"doc_key": doc_key, "taint_sources": sources},
        )


class ExternalActionError(ApiError):
    def __init__(self, message: str, *, details: Any | None = None) -> None:
        super().__init__(
            409, "external_action_error", message, details=details
        )


class VersionConflict(ApiError):
    def __init__(self, resource: str, latest: dict[str, Any] | None) -> None:
        super().__init__(
            409,
            "version_conflict",
            f"{resource} changed since expected_version; re-read and merge "
            "against the latest version",
            details={
                "resource": resource,
                "current_version": latest.get("version") if latest else None,
                "current_hash": latest.get("content_hash") if latest else None,
                "latest": latest,
            },
        )


class LeaseConflict(ApiError):
    def __init__(self, resource: str, latest: dict[str, Any] | None) -> None:
        super().__init__(
            409,
            "lease_conflict",
            f"{resource} is held by another agent with a live lease",
            details={"resource": resource, "latest": latest},
        )


class InvalidStateTransition(ApiError):
    def __init__(self, current: str, requested: str) -> None:
        super().__init__(
            409,
            "invalid_state_transition",
            f"cannot transition task from {current} to {requested}",
            details={"current": current, "requested": requested},
        )


class TaskNotReviewable(ApiError):
    def __init__(self, current: str, decision: str) -> None:
        super().__init__(
            409,
            "task_not_reviewable",
            f"cannot {decision} a task in {current} state; review requires "
            "REVIEWING, AWAITING_HUMAN or ESCALATED_TO_HUMAN",
            details={
                "current": current,
                "decision": decision,
                "allowed": ["REVIEWING", "AWAITING_HUMAN", "ESCALATED_TO_HUMAN"],
            },
        )


class EscalatedToHuman(ApiError):
    def __init__(self, latest: dict[str, Any] | None) -> None:
        super().__init__(
            409,
            "escalated_to_human",
            "task is escalated to a human commander; agent transitions are blocked",
            details={"latest": latest},
        )


class HumanSovereigntyLocked(ApiError):
    def __init__(self, resource: str, note: str | None = None) -> None:
        super().__init__(
            403,
            "HUMAN_SOVEREIGNTY_LOCKED",
            f"{resource} is locked by the human commander",
            details={"resource": resource, "note": note},
        )


class NamespaceForbidden(ApiError):
    def __init__(self, namespace: str) -> None:
        super().__init__(
            403,
            "NAMESPACE_FORBIDDEN",
            f"token is not allowed to access namespace '{namespace}'",
            details={"namespace": namespace},
        )


class Forbidden(ApiError):
    def __init__(self, message: str = "operation not permitted for this role") -> None:
        super().__init__(403, "forbidden", message)


class UndoConflict(ApiError):
    def __init__(self, batch_id: str, conflicts: list[dict[str, Any]]) -> None:
        super().__init__(
            409,
            "undo_conflict",
            "the batch target changed after this session; review the conflicts "
            "or retry with force=true",
            details={"batch_id": batch_id, "conflicts": conflicts},
        )


class NotFound(ApiError):
    def __init__(self, resource: str, message: str | None = None) -> None:
        super().__init__(
            404, "not_found", message or f"{resource} not found",
            details={"resource": resource},
        )


class EmbeddingFailure(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__(502, "embedding_failed", message)


class SlotConflict(VersionConflict):
    """Backward-compatible name for callers of the v2 service layer."""

    def __init__(self, latest: dict[str, Any] | None) -> None:
        super().__init__("context_slot", latest)


class SlotLocked(LeaseConflict):
    def __init__(self, latest: dict[str, Any] | None, owner: str | None) -> None:
        super().__init__("context_slot", latest)
        self.message = f"context slot is exclusively locked by {owner or 'another agent'}"
