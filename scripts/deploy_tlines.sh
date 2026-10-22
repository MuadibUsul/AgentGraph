#!/usr/bin/env bash
# Deploy AgentGraph on the Tlines VPS with a database backup, a tagged
# rollback image and post-deploy verification.
#
# Run on the server from the checkout (default /home/deploy/agentgraph):
#
#   scripts/deploy_tlines.sh deploy            # backup, build, restart, verify
#   scripts/deploy_tlines.sh verify            # checks only
#   scripts/deploy_tlines.sh rollback <stamp>  # previous image + DB restore
#   scripts/deploy_tlines.sh rollback-image <stamp>   # previous image only
#
# <stamp> is printed by `deploy` and is the suffix of backups/agentgraph-<stamp>.sql.gz
# and of the image tag agentgraph:rollback-<stamp>.
#
# Secrets stay in .env; this script never prints ADMIN_TOKEN or model keys.

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
API_CONTAINER="${API_CONTAINER:-agentgraph-api}"
PUBLIC_URL="${PUBLIC_URL:-https://agentgraph.fusionpropulsion.space}"
BACKUP_DIR="${BACKUP_DIR:-backups}"
EXPECTED_SCHEMA="${EXPECTED_SCHEMA:-3.19.0}"

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }
log() { printf '\033[1m[deploy]\033[0m %s\n' "$*"; }
fail() { printf '\033[31m[deploy] %s\033[0m\n' "$*" >&2; exit 1; }

env_value() {
  # Read one key from .env without echoing the file.
  grep -E "^$1=" .env | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'
}

api_image() { compose config --images | grep -E '^agentgraph' | head -n1; }

preflight() {
  [ -f .env ] || fail ".env not found; run from the deployment directory"
  [ -f "$COMPOSE_FILE" ] || fail "$COMPOSE_FILE not found"
  docker compose version >/dev/null || fail "docker compose is not available"
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"
}

backup_db() {
  local stamp="$1" user db file
  user="$(env_value POSTGRES_USER)"; user="${user:-agentgraph}"
  db="$(env_value POSTGRES_DB)"; db="${db:-agentgraph}"
  file="$BACKUP_DIR/agentgraph-$stamp.sql.gz"
  log "backing up database to $file"
  compose exec -T db pg_dump -U "$user" -d "$db" --no-owner --clean --if-exists \
    | gzip -9 > "$file"
  chmod 600 "$file"
  [ "$(gzip -dc "$file" | head -c 4096 | wc -c)" -gt 0 ] || fail "backup is empty"
  gzip -dc "$file" | grep -q "CREATE TABLE" || fail "backup has no tables"
  log "backup ok ($(du -h "$file" | cut -f1))"
}

tag_current_image() {
  local stamp="$1" image current
  image="$(api_image)"
  current="$(docker inspect --format '{{.Image}}' "$API_CONTAINER" 2>/dev/null || true)"
  if [ -z "$current" ]; then
    log "no running $API_CONTAINER; nothing to tag for rollback"
    return
  fi
  docker image tag "$current" "agentgraph:rollback-$stamp"
  git rev-parse HEAD > "$BACKUP_DIR/agentgraph-$stamp.commit" 2>/dev/null || true
  log "tagged running image ($image) as agentgraph:rollback-$stamp"
}

in_api() {
  # Run a Python snippet inside the API container (no host port is published).
  docker exec -i "$API_CONTAINER" python - "$@"
}

wait_ready() {
  log "waiting for /readyz"
  for _ in $(seq 1 60); do
    if in_api <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:8000/readyz", timeout=3)
PY
    then
      log "ready"
      return 0
    fi
    sleep 3
  done
  return 1
}

verify() {
  log "verifying deployment"
  in_api <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
failures = []


def status(path, headers=None, method="GET", body=None):
    request = urllib.request.Request(BASE + path, headers=headers or {}, method=method, data=body)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)


code, body = status("/readyz")
check("readyz 200", code == 200, str(code))
code, body = status("/docs/")
check("public /docs/ 200", code == 200 and "AgentGraph" in body, str(code))
code, _ = status("/docs/quickstart/")
check("public /docs/quickstart/ 200", code == 200, str(code))
code, body = status("/docs/api-reference/")
check("docs API reference hides /v1/admin", code == 200 and "/v1/admin" not in body, str(code))
docs_auth = os.environ.get("DOCS_REQUIRE_AUTH", "").lower() in {"1", "true", "yes"}
if docs_auth:
    code, _ = status("/api-reference")
    check("/api-reference needs Basic auth (401)", code == 401, str(code))
    code, _ = status("/openapi.json")
    check("/openapi.json needs Basic auth (401)", code == 401, str(code))
else:
    print("SKIP Basic-auth checks: DOCS_REQUIRE_AUTH is not true in this container")
code, body = status(
    "/mcp/",
    headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    method="POST",
    body=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "deploy-check", "version": "1"}},
    }).encode(),
)
# Without a token MCP answers 401; with the auth layer intact that is the expected state.
check("MCP endpoint answers", code in (200, 401), str(code))

sys.exit(1 if failures else 0)
PY
  local user db applied
  user="$(env_value POSTGRES_USER)"; user="${user:-agentgraph}"
  db="$(env_value POSTGRES_DB)"; db="${db:-agentgraph}"
  applied="$(compose exec -T db psql -U "$user" -d "$db" -tAc \
    "SELECT 1 FROM schema_migrations WHERE version = '$EXPECTED_SCHEMA'")"
  [ "$applied" = 1 ] || fail "schema migration $EXPECTED_SCHEMA is not applied"
  log "schema $EXPECTED_SCHEMA applied"
  if [ -n "$PUBLIC_URL" ] && command -v curl >/dev/null; then
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' "$PUBLIC_URL/docs/")"
    [ "$code" = 200 ] || fail "public $PUBLIC_URL/docs/ returned $code"
    code="$(curl -s -o /dev/null -w '%{http_code}' "$PUBLIC_URL/api-reference")"
    [ "$code" = 401 ] || fail "public $PUBLIC_URL/api-reference returned $code, expected 401"
    curl -sI "$PUBLIC_URL/docs/" | grep -qi '^content-security-policy' \
      || fail "security headers missing on $PUBLIC_URL/docs/"
    log "public checks ok ($PUBLIC_URL)"
  fi
  log "verification passed"
}

restart_with_image() {
  local image="$1" target
  target="$(api_image)"
  docker image tag "$image" "$target"
  compose up -d --no-build api
}

restore_db() {
  local stamp="$1" user db file
  file="$BACKUP_DIR/agentgraph-$stamp.sql.gz"
  [ -f "$file" ] || fail "backup $file not found"
  user="$(env_value POSTGRES_USER)"; user="${user:-agentgraph}"
  db="$(env_value POSTGRES_DB)"; db="${db:-agentgraph}"
  log "stopping api and restoring $file"
  compose stop api
  gzip -dc "$file" | compose exec -T db psql -v ON_ERROR_STOP=1 -q -U "$user" -d "$db" >/dev/null
}

deploy() {
  preflight
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_db "$stamp"
  tag_current_image "$stamp"
  log "building image"
  compose build api
  log "restarting api (migrations run on start under an advisory lock)"
  compose up -d api
  if ! wait_ready || ! verify; then
    log "deployment failed verification"
    compose logs --tail 80 api || true
    if docker image inspect "agentgraph:rollback-$stamp" >/dev/null 2>&1; then
      log "rolling back to agentgraph:rollback-$stamp with database restore"
      rollback "$stamp"
    fi
    fail "deploy aborted; see the log above"
  fi
  log "deployed. rollback stamp: $stamp"
  log "  full rollback:  scripts/deploy_tlines.sh rollback $stamp"
  log "  image only:     scripts/deploy_tlines.sh rollback-image $stamp"
}

rollback() {
  local stamp="${1:?usage: rollback <stamp>}"
  preflight
  docker image inspect "agentgraph:rollback-$stamp" >/dev/null 2>&1 \
    || fail "image agentgraph:rollback-$stamp not found"
  restore_db "$stamp"
  restart_with_image "agentgraph:rollback-$stamp"
  wait_ready || fail "rolled-back api did not become ready"
  log "rolled back to $stamp (writes made after the backup were discarded)"
}

rollback_image() {
  local stamp="${1:?usage: rollback-image <stamp>}"
  preflight
  docker image inspect "agentgraph:rollback-$stamp" >/dev/null 2>&1 \
    || fail "image agentgraph:rollback-$stamp not found"
  # Migrations are additive: the previous image ignores newer tables.
  restart_with_image "agentgraph:rollback-$stamp"
  wait_ready || fail "rolled-back api did not become ready"
  log "image rolled back to $stamp; database kept as is"
}

case "${1:-}" in
  deploy) deploy ;;
  verify) preflight; verify ;;
  rollback) rollback "${2:-}" ;;
  rollback-image) rollback_image "${2:-}" ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
