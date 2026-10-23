"""Operator CLI: schema bootstrap, workspace provisioning, and inspection."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from app import services
from app.database import apply_schema, async_session_factory
from app.errors import NotFound
from app.schemas import AgentTokenCreateRequest


async def _run(args: argparse.Namespace) -> int:
    if args.command == "init-db":
        await apply_schema()
        print("schema applied")
        return 0

    if args.command == "create-workspace":
        async with async_session_factory() as session:
            created = await services.create_workspace(
                session, name=args.name, tier=args.tier
            )
        print(json.dumps(created.model_dump(mode="json"), indent=2))
        print("\nStore this API key now; it cannot be recovered.", file=sys.stderr)
        return 0

    if args.command == "list-workspaces":
        async with async_session_factory() as session:
            rows = await services.list_workspaces(session)
        print(
            json.dumps(
                [row.model_dump(mode="json") for row in rows], indent=2
            )
        )
        return 0

    if args.command == "rotate-key":
        async with async_session_factory() as session:
            workspace, key = await services.rotate_workspace_key(
                session, uuid.UUID(args.workspace_id)
            )
        print(json.dumps({"workspace": workspace.model_dump(mode="json"),
                          "api_key": key}, indent=2))
        return 0

    if args.command == "create-agent-token":
        async with async_session_factory() as session:
            try:
                workspace = await services.get_workspace(
                    session, uuid.UUID(args.workspace_id)
                )
            except NotFound:
                print("workspace not found", file=sys.stderr)
                return 1
            created = await services.create_agent_token(
                session,
                workspace,
                AgentTokenCreateRequest(
                    agent_signature=args.agent,
                    role_scope=args.role,
                    allowed_namespaces=[
                        item.strip()
                        for item in args.namespaces.split(",")
                        if item.strip()
                    ],
                ),
            )
        print(json.dumps(created.model_dump(mode="json"), indent=2))
        print()
        print("Store this agent key now; it cannot be recovered.", file=sys.stderr)
        return 0

    if args.command == "stats":
        workspace_id = uuid.UUID(args.workspace_id)
        async with async_session_factory() as session:
            try:
                workspace = await services.get_workspace(session, workspace_id)
            except NotFound:
                print("workspace not found", file=sys.stderr)
                return 1
            stats = await services.workspace_stats(session, workspace)
        print(json.dumps(stats.model_dump(mode="json"), indent=2))
        return 0

    print(f"unknown command: {args.command}", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli", description="Veynod operator CLI"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="apply sql/init.sql idempotently")

    create = sub.add_parser("create-workspace", help="create a tenant + API key")
    create.add_argument("--name", required=True)
    create.add_argument(
        "--tier", default="developer_5usd",
        choices=["free", "developer_5usd", "pro"],
    )

    sub.add_parser("list-workspaces", help="list tenants and usage counts")

    rotate = sub.add_parser("rotate-key", help="rotate a tenant API key")
    rotate.add_argument("--workspace-id", required=True)

    agent = sub.add_parser(
        "create-agent-token", help="mint a scoped agent key"
    )
    agent.add_argument("--workspace-id", required=True)
    agent.add_argument("--agent", required=True)
    agent.add_argument(
        "--role",
        default="EXECUTOR",
        choices=["SCOUT", "ANALYST", "AUDITOR", "EXECUTOR"],
    )
    agent.add_argument(
        "--namespaces",
        default="*",
        help="comma-separated namespaces, or * for all",
    )

    stats = sub.add_parser("stats", help="show workspace usage and quotas")
    stats.add_argument("--workspace-id", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
