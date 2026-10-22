from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

os.environ["ENVIRONMENT"] = "test"
os.environ["EMBEDDING_PROVIDER"] = "hash"
os.environ["ADMIN_TOKEN"] = "test-admin"
os.environ["AUTO_INIT_DB"] = "true"
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://agentgraph:agentgraph@localhost:55432/agentgraph",
)
if os.getenv("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

INTEGRATION_ENABLED = bool(os.getenv("TEST_DATABASE_URL"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def run_server(extra_env: dict[str, str] | None = None, log_name: str = ".server.log"):
    """Start the API in a subprocess against the test database."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = PROJECT_ROOT / "tests" / log_name
    env = os.environ.copy()
    env.update(
        {
            "ENVIRONMENT": "test",
            "EMBEDDING_PROVIDER": "hash",
            "ADMIN_TOKEN": "test-admin",
            "AUTO_INIT_DB": "true",
            "MCP_ENABLED": "true",
            "LEASE_REAPER_INTERVAL_SECONDS": "5",
            "SSE_REVALIDATE_SECONDS": "5",
            # Async-index tests drive the worker in-process for determinism.
            "INDEX_WORKER_ENABLED": "false",
            "FREE_MAX_DOCUMENTS": "1",
            "FREE_MAX_SLOTS": "1",
            **(extra_env or {}),
        }
    )
    log_file = log_path.open("w", encoding="utf-8")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-access-log",
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_file,
        stderr=log_file,
    )
    try:
        deadline = asyncio.get_running_loop().time() + 45
        async with httpx.AsyncClient(timeout=5) as probe:
            while True:
                if process.returncode is not None:
                    log_file.flush()
                    raise RuntimeError(
                        "api server exited during startup:\n"
                        + log_path.read_text(encoding="utf-8", errors="replace")
                    )
                if asyncio.get_running_loop().time() > deadline:
                    log_file.flush()
                    raise RuntimeError(
                        "api server did not become ready:\n"
                        + log_path.read_text(encoding="utf-8", errors="replace")
                    )
                try:
                    response = await probe.get(f"{base_url}/readyz")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
        yield base_url
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=15)
            except TimeoutError:
                process.kill()
                await process.wait()
        log_file.close()


@pytest.fixture(scope="session")
async def api_base_url() -> AsyncIterator[str]:
    if not INTEGRATION_ENABLED:
        pytest.skip("TEST_DATABASE_URL is not set")
    async with run_server() as base_url:
        yield base_url


@pytest.fixture(scope="session")
async def client(api_base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=api_base_url, timeout=60) as http:
        yield http


@pytest.fixture(scope="session")
async def enforce_client(api_base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """A second server on the same database with cost limits enforced."""
    async with run_server({"BUDGET_MODE": "enforce"}, ".server-enforce.log") as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as http:
            yield http


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-admin"}


@pytest.fixture
async def workspace(client: httpx.AsyncClient, admin_headers: dict[str, str]):
    response = await client.post(
        "/v1/admin/workspaces",
        json={"name": f"test-{uuid.uuid4().hex[:8]}", "tier": "developer_5usd"},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    created = response.json()
    return {
        "workspace": created["workspace"],
        "api_key": created["api_key"],
        "headers": {"Authorization": f"Bearer {created['api_key']}"},
    }

