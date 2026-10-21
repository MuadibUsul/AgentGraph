"""Reviewed document proposals, Proposal Arena votes, and safe apply."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, effective_agent, ensure_namespace, require_permission
from app.engine.merge import three_way_document
from app.errors import (
    ApiError,
    Forbidden,
    NotFound,
    ProposalConflict,
    VersionConflict,
)
from app.models import (
    DocumentProposal,
    DocumentVersion,
    ProposalVote,
    SharedDocument,
)
from app.schemas import (
    ProposalApplyRequest,
    ProposalApplyResponse,
    ProposalCreateRequest,
    ProposalListResponse,
    ProposalOut,
    ProposalRejectRequest,
    ProposalVoteOut,
    ProposalVoteRequest,
)
from app.services.common import (
    append_event,
    document_out,
    record_audit,
    utcnow,
)
from app.services.documents import (
    _get_document,
    _record_version,
    content_hash,
)
from app.services.policies import enforce_policy
from app.services.retrieval import reindex_document_chunks
from app.services.taint import (
    compute_document_taint,
    propagate_from_document,
    refresh_document_taint,
)


async def _votes(
    session: AsyncSession, proposal_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ProposalVote]]:
    if not proposal_ids:
        return {}
    rows = (
        await session.execute(
            select(ProposalVote).where(ProposalVote.proposal_id.in_(proposal_ids))
        )
    ).scalars().all()
    grouped: dict[uuid.UUID, list[ProposalVote]] = {}
    for row in rows:
        grouped.setdefault(row.proposal_id, []).append(row)
    return grouped


def _proposal_out(
    proposal: DocumentProposal, votes: list[ProposalVote]
) -> ProposalOut:
    vote_items = [
        ProposalVoteOut(
            voter=vote.voter,
            score=vote.score,
            comment=vote.comment,
            created_at=vote.created_at,
        )
        for vote in votes
    ]
    total = sum(vote.score for vote in votes)
    return ProposalOut(
        id=proposal.id,
        doc_key=proposal.doc_key,
        title=proposal.title,
        content=proposal.content,
        frontmatter=proposal.frontmatter or {},
        base_version=proposal.base_version,
        base_content_hash=proposal.base_content_hash,
        mode=proposal.mode,
        status=proposal.status,
        created_by=proposal.created_by,
        reviewed_by=proposal.reviewed_by,
        review_note=proposal.review_note,
        applied_version=proposal.applied_version,
        applied_by=proposal.applied_by,
        tainted=proposal.tainted,
        taint_sources=proposal.taint_sources or [],
        conflict_details=proposal.conflict_details,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
        votes=vote_items,
        vote_score=round(total / len(votes), 2) if votes else 0,
        vote_count=len(votes),
    )


async def _get_proposal(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    proposal_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> DocumentProposal:
    query = select(DocumentProposal).where(
        DocumentProposal.id == proposal_id,
        DocumentProposal.workspace_id == workspace_id,
    )
    if for_update:
        query = query.with_for_update()
    proposal = (await session.execute(query)).scalar_one_or_none()
    if proposal is None:
        raise NotFound("document_proposal")
    return proposal


async def create_proposal(
    session: AsyncSession,
    principal: Principal,
    doc_key: str,
    request: ProposalCreateRequest,
) -> ProposalOut:
    require_permission(principal, "doc:propose")
    document = await _get_document(session, principal.workspace.id, doc_key)
    ensure_namespace(principal, document.namespace, write=True)
    agent = effective_agent(principal, request.agent)
    if document.version != request.base_version:
        raise VersionConflict("shared_document", document_out(document).model_dump(mode="json"))
    base = (
        await session.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.version == request.base_version,
            )
        )
    ).scalar_one_or_none()
    if base is None:
        raise NotFound("document_version", "base version no longer exists")
    title = request.title or document.title
    await enforce_policy(
        session,
        principal,
        resource_type="document",
        key=doc_key,
        action="propose",
        payload=request.frontmatter,
        content_bytes=len(request.content.encode("utf-8")),
    )
    preview = SharedDocument(
        workspace_id=document.workspace_id,
        doc_key=document.doc_key,
        title=title,
        content=request.content,
        frontmatter=request.frontmatter,
        trust_level=document.trust_level,
        verification_status=document.verification_status,
    )
    tainted, taint_sources = await compute_document_taint(session, preview)
    proposal = DocumentProposal(
        workspace_id=principal.workspace.id,
        doc_key=doc_key,
        title=title,
        content=request.content,
        frontmatter=request.frontmatter,
        base_version=request.base_version,
        base_content_hash=base.content_hash,
        mode=request.mode,
        status="PENDING",
        created_by=agent,
        session_batch_id=request.session_batch_id,
        tainted=tainted,
        taint_sources=taint_sources,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(proposal)
    await session.flush()
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=agent,
        event_type="PROPOSAL_CREATED",
        payload={
            "proposal_id": str(proposal.id),
            "doc_key": doc_key,
            "base_version": request.base_version,
            "mode": request.mode,
            "tainted": tainted,
            "note": request.note,
        },
        namespace=document.namespace,
    )
    await session.commit()
    return _proposal_out(proposal, [])


async def list_proposals(
    session: AsyncSession,
    principal: Principal,
    *,
    doc_key: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> ProposalListResponse:
    require_permission(principal, "proposal:read")
    query = select(DocumentProposal).where(
        DocumentProposal.workspace_id == principal.workspace.id
    )
    if doc_key:
        document = await _get_document(session, principal.workspace.id, doc_key)
        ensure_namespace(principal, document.namespace)
        query = query.where(DocumentProposal.doc_key == doc_key)
    if status:
        query = query.where(DocumentProposal.status == status.upper())
    proposals = (
        await session.execute(
            query.order_by(DocumentProposal.created_at.desc()).limit(min(limit, 200))
        )
    ).scalars().all()
    votes = await _votes(session, [proposal.id for proposal in proposals])
    return ProposalListResponse(
        items=[
            _proposal_out(proposal, votes.get(proposal.id, []))
            for proposal in proposals
        ]
    )


async def get_proposal(
    session: AsyncSession, principal: Principal, proposal_id: uuid.UUID
) -> ProposalOut:
    require_permission(principal, "proposal:read")
    proposal = await _get_proposal(session, principal.workspace.id, proposal_id)
    votes = (await _votes(session, [proposal.id])).get(proposal.id, [])
    return _proposal_out(proposal, votes)


async def vote_proposal(
    session: AsyncSession,
    principal: Principal,
    proposal_id: uuid.UUID,
    request: ProposalVoteRequest,
) -> ProposalOut:
    require_permission(principal, "proposal:vote")
    proposal = await _get_proposal(session, principal.workspace.id, proposal_id)
    if proposal.status != "PENDING":
        raise ApiError(409, "proposal_not_pending", "only pending proposals can be voted on")
    if proposal.created_by == principal.agent_signature:
        raise ApiError(
            403,
            "self_vote_forbidden",
            "an agent cannot vote on its own proposal",
            details={
                "proposal_id": str(proposal.id),
                "voter": principal.agent_signature,
                "created_by": proposal.created_by,
            },
        )
    vote = (
        await session.execute(
            select(ProposalVote).where(
                ProposalVote.proposal_id == proposal.id,
                ProposalVote.voter == principal.agent_signature,
            )
        )
    ).scalar_one_or_none()
    if vote is None:
        vote = ProposalVote(
            workspace_id=principal.workspace.id,
            proposal_id=proposal.id,
            voter=principal.agent_signature,
            score=request.score,
            comment=request.comment,
            created_at=utcnow(),
        )
        session.add(vote)
    else:
        vote.score = request.score
        vote.comment = request.comment
    proposal.updated_at = utcnow()
    await session.flush()
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="PROPOSAL_VOTED",
        payload={
            "proposal_id": str(proposal.id),
            "doc_key": proposal.doc_key,
            "score": request.score,
        },
    )
    await session.commit()
    votes = (await _votes(session, [proposal.id])).get(proposal.id, [])
    return _proposal_out(proposal, votes)


async def apply_proposal(
    session: AsyncSession,
    principal: Principal,
    proposal_id: uuid.UUID,
    request: ProposalApplyRequest,
) -> ProposalApplyResponse:
    if not principal.is_human or not principal.is_commander:
        raise Forbidden("a human owner/reviewer must apply a proposal")
    require_permission(principal, "doc:write")
    proposal = await _get_proposal(
        session, principal.workspace.id, proposal_id, for_update=True
    )
    if proposal.status != "PENDING":
        raise ApiError(
            409,
            "proposal_not_pending",
            f"proposal is already {proposal.status.lower()}",
        )
    document = await _get_document(
        session, principal.workspace.id, proposal.doc_key, for_update=True
    )
    ensure_namespace(principal, document.namespace, write=True)
    if (
        request.expected_document_version is not None
        and request.expected_document_version != document.version
        and not request.rebase
    ):
        raise VersionConflict(
            "shared_document", document_out(document).model_dump(mode="json")
        )
    base = (
        await session.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.version == proposal.base_version,
            )
        )
    ).scalar_one_or_none()
    if base is None:
        raise ProposalConflict(
            str(proposal.id),
            [
                {
                    "field": "base_version",
                    "reason": "the proposal base version no longer exists",
                }
            ],
        )

    rebased = False
    if document.version == proposal.base_version:
        title = proposal.title
        content = proposal.content
        frontmatter = proposal.frontmatter or {}
    else:
        if not request.rebase:
            raise VersionConflict(
                "shared_document", document_out(document).model_dump(mode="json")
            )
        merged = three_way_document(
            base_content=base.content,
            current_content=document.content,
            proposed_content=proposal.content,
            base_title=base.title,
            current_title=document.title,
            proposed_title=proposal.title,
            base_frontmatter=base.frontmatter or {},
            current_frontmatter=document.frontmatter or {},
            proposed_frontmatter=proposal.frontmatter or {},
        )
        if merged.conflicts:
            proposal.conflict_details = {
                "conflicts": [conflict.as_dict() for conflict in merged.conflicts]
            }
            proposal.updated_at = utcnow()
            await session.commit()
            raise ProposalConflict(
                str(proposal.id),
                [conflict.as_dict() for conflict in merged.conflicts],
            )
        title = merged.title or document.title
        content = merged.content
        frontmatter = merged.frontmatter
        rebased = True

    await enforce_policy(
        session,
        principal,
        resource_type="document",
        key=proposal.doc_key,
        action="write",
        payload=frontmatter,
        content_bytes=len(content.encode("utf-8")),
    )
    before = document_out(document, include_content=False).model_dump(mode="json")
    batch = request.session_batch_id or uuid.uuid4()
    document.title = title
    document.content = content
    document.content_hash = content_hash(content)
    document.frontmatter = frontmatter
    document.verification_status = "DRAFT"
    document.verified_version = None
    document.verified_hash = None
    document.version += 1
    document.updated_by = principal.agent_signature
    document.updated_at = utcnow()
    await session.flush()
    await refresh_document_taint(session, document)
    await reindex_document_chunks(session, document)
    await _record_version(
        session,
        document,
        author=principal.agent_signature,
        batch_id=batch,
        change_summary=f"proposal {proposal.id} applied"
        + (" with rebase" if rebased else ""),
    )
    await record_audit(
        session,
        workspace_id=principal.workspace.id,
        session_batch_id=batch,
        agent_signature=principal.agent_signature,
        target_table="shared_documents",
        target_record_id=document.id,
        operation="UPDATE",
        before=before,
        after=document_out(document, include_content=False).model_dump(mode="json"),
    )
    proposal.status = "APPLIED"
    proposal.reviewed_by = principal.agent_signature
    proposal.review_note = request.note
    proposal.applied_version = document.version
    proposal.applied_by = principal.agent_signature
    proposal.updated_at = utcnow()
    siblings = (
        await session.execute(
            select(DocumentProposal).where(
                DocumentProposal.workspace_id == principal.workspace.id,
                DocumentProposal.doc_key == proposal.doc_key,
                DocumentProposal.status == "PENDING",
                DocumentProposal.id != proposal.id,
            )
        )
    ).scalars().all()
    for sibling in siblings:
        sibling.status = "SUPERSEDED"
        sibling.reviewed_by = principal.agent_signature
        sibling.review_note = f"superseded by proposal {proposal.id}"
        sibling.updated_at = utcnow()
    await propagate_from_document(session, document)
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="PROPOSAL_APPLIED",
        payload={
            "proposal_id": str(proposal.id),
            "doc_key": proposal.doc_key,
            "version": document.version,
            "rebased": rebased,
        },
        namespace=document.namespace,
    )
    await session.commit()
    votes = (await _votes(session, [proposal.id])).get(proposal.id, [])
    return ProposalApplyResponse(
        proposal=_proposal_out(proposal, votes),
        document=document_out(document),
        rebased=rebased,
    )


async def reject_proposal(
    session: AsyncSession,
    principal: Principal,
    proposal_id: uuid.UUID,
    request: ProposalRejectRequest,
) -> ProposalOut:
    if not principal.is_human or not principal.is_commander:
        raise Forbidden("a human owner/reviewer must reject a proposal")
    proposal = await _get_proposal(
        session, principal.workspace.id, proposal_id, for_update=True
    )
    if proposal.status != "PENDING":
        raise ApiError(
            409,
            "proposal_not_pending",
            f"proposal is already {proposal.status.lower()}",
        )
    proposal.status = "REJECTED"
    proposal.reviewed_by = principal.agent_signature
    proposal.review_note = request.note
    proposal.updated_at = utcnow()
    await session.flush()
    await append_event(
        session,
        workspace_id=principal.workspace.id,
        agent=principal.agent_signature,
        event_type="PROPOSAL_REJECTED",
        payload={
            "proposal_id": str(proposal.id),
            "doc_key": proposal.doc_key,
            "note": request.note,
        },
    )
    await session.commit()
    votes = (await _votes(session, [proposal.id])).get(proposal.id, [])
    return _proposal_out(proposal, votes)
