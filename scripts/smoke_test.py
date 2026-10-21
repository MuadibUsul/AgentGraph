"""Deployment smoke test for AgentGraph v3.2.

Usage:
    python scripts/smoke_test.py --admin-token "$ADMIN_TOKEN"
    python scripts/smoke_test.py --api-key "$WORKSPACE_KEY"

If neither --api-key nor --admin-token is supplied, pass one explicitly.
Requires JSONPatch-backed environment (see requirements.txt).
"""

from __future__ import annotations

import argparse
import json
import uuid

import httpx


def check(response: httpx.Response, step: str) -> dict:
    if response.status_code >= 400:
        print(f"[FAIL] {step}: HTTP {response.status_code}\n{response.text}")
        raise SystemExit(1)
    print(f"[ok] {step}")
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--admin-token", default=None)
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    admin_headers = (
        {"Authorization": f"Bearer {args.admin_token}"}
        if args.admin_token
        else None
    )

    with httpx.Client(base_url=base, timeout=30) as client:
        health = check(client.get("/readyz"), "readiness")
        print(json.dumps(health, indent=2))

        key = args.api_key
        workspace_id: str
        if key:
            who = check(
                client.get("/v1/whoami", headers={"Authorization": f"Bearer {key}"}),
                "whoami",
            )
            workspace_id = who["workspace"]["id"]
        else:
            if not admin_headers:
                print("Provide --admin-token or --api-key")
                raise SystemExit(2)
            created = check(
                client.post(
                    "/v1/admin/workspaces",
                    headers=admin_headers,
                    json={"name": "smoke-test", "tier": "developer_5usd"},
                ),
                "create workspace",
            )
            key = created["api_key"]
            workspace_id = created["workspace"]["id"]

        commander = {"Authorization": f"Bearer {key}"}
        suffix = uuid.uuid4().hex[:6]
        agent_keys: dict[str, str] = {}
        if admin_headers:
            for role in ("EXECUTOR", "AUDITOR"):
                signature = f"smoke_{role.lower()}_{suffix}"
                token = check(
                    client.post(
                        f"/v1/admin/workspaces/{workspace_id}/agent-tokens",
                        headers=admin_headers,
                        json={
                            "agent_signature": signature,
                            "role_scope": role,
                            "allowed_namespaces": ["*"],
                        },
                    ),
                    f"mint {role} token",
                )
                agent_keys[role] = token["api_key"]
        executor = (
            {"Authorization": f"Bearer {agent_keys['EXECUTOR']}"}
            if "EXECUTOR" in agent_keys
            else commander
        )
        auditor = (
            {"Authorization": f"Bearer {agent_keys['AUDITOR']}"}
            if "AUDITOR" in agent_keys
            else commander
        )

        who = check(client.get("/v1/whoami", headers=commander), "identity")
        print("workspace:", who["workspace"]["name"], "role:", who["role"])

        slot = check(
            client.put(
                f"/v1/slots/smoke:regime-{suffix}",
                headers=executor,
                json={
                    "state": {"bias": "LONG", "confidence": 0.7},
                    "goal": "smoke state",
                    "expected_version": 0,
                },
            ),
            "create state slot",
        )
        slot_key = slot["slot"]["slot_key"]
        check(
            client.post(
                f"/v1/slots/{slot_key}/patch",
                headers=executor,
                json={
                    "ops": [{"op": "replace", "path": "/confidence", "value": 0.8}],
                    "expected_version": slot["slot"]["version"],
                },
            ),
            "JSON Patch state",
        )

        task_key = f"SMOKE-{suffix}"
        check(
            client.post(
                "/v1/tasks",
                headers=commander,
                json={
                    "task_key": task_key,
                    "title": "Smoke task",
                    "assigned_role": "EXECUTOR",
                    "input_context": {"slot_key": slot_key},
                },
            ),
            "create task",
        )
        claimed = check(
            client.post(
                f"/v1/tasks/{task_key}/claim",
                headers=executor,
                json={"lease_seconds": 60},
            ),
            "claim task (TTL lease)",
        )
        print("claimed task:", claimed["task"]["status"])
        handoff = check(
            client.post(
                f"/v1/tasks/{task_key}/handoff",
                headers=executor,
                json={
                    "next_status": "REVIEWING",
                    "brief": {
                        "summary": "smoke handoff",
                        "key_metrics": {"confidence": 0.8},
                        "unverified_hypotheses": [],
                        "risk_flag": False,
                    },
                    "artifact_text": "# Smoke artifact\n\nL2 details.",
                },
            ),
            "handoff with L1 + L2",
        )
        print("artifact:", handoff["artifact_uri"])
        l2 = check(
            client.post(
                "/v1/dereference",
                headers=auditor,
                json={"uri": handoff["artifact_uri"]},
            ),
            "dereference L2",
        )
        print("L2 bytes:", len(l2["document"]["content"]))
        approved = check(
            client.post(
                f"/v1/tasks/{task_key}/review",
                headers=auditor,
                json={
                    "decision": "approve",
                    "feedback": "smoke approved",
                    "expected_version": handoff["task"]["version"],
                },
            ),
            "review task",
        )
        print("task:", approved["task"]["status"])

        doc_key = f"smoke/doc-{suffix}"
        document = check(
            client.put(
                f"/v1/documents/{doc_key}",
                headers=commander,
                json={
                    "title": "Smoke document",
                    "content": "# Smoke\n\n## Risk\nkeep 2%",
                    "frontmatter": {"status": "DRAFT"},
                    "expected_version": 0,
                },
            ),
            "create shared document",
        )
        check(
            client.post(
                f"/v1/documents/{doc_key}/verify",
                headers=auditor,
                json={
                    "expected_version": document["document"]["version"],
                    "note": "smoke verification",
                },
            ),
            "verify document version",
        )
        check(
            client.get(
                f"/v1/documents/{doc_key}/versions",
                headers=commander,
            ),
            "read version history",
        )
        check(
            client.post(
                "/v1/query",
                headers=commander,
                json={"filters": {"status": "DRAFT"}, "limit": 5},
            ),
            "query documents",
        )

        if admin_headers:
            audit = check(
                client.get(
                    f"/v1/admin/workspaces/{workspace_id}/audit",
                    headers=admin_headers,
                ),
                "audit batches",
            )
            print("audit batches:", len(audit["batches"]))
        stats = check(
            client.get("/v1/stats", headers=commander),
            "stats",
        )
        print(
            "counts:",
            {
                key: stats[key]
                for key in ("documents", "slots", "tasks", "agent_tokens")
            },
        )

    print("\nSMOKE_OK")
    if not args.api_key:
        print(f"Workspace commander key (store it): {key}")
        for role, agent_key in agent_keys.items():
            print(f"{role} agent key (store it): {agent_key}")


if __name__ == "__main__":
    main()
