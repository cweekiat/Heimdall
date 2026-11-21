#!/usr/bin/env bash
# Deploy one image tag on this VM, with backup and automatic rollback (task 064, ADR-009).
# Run by the self-hosted GitHub runner (deploy-dev / deploy-prod jobs) from a checkout of the repo:
#   IMAGE_REPO=ghcr.io/<owner> deploy/scripts/deploy.sh sha-0123abcd4567
# Host files (outside the repo, see docs/deploy-oracle.md):
#   HOSTED_ENV_FILE  default /srv/heimdall/hosted.env   (0600; HEIMDALL_ENV, HEIMDALL_DATA, TUNNEL_TOKEN, ...)
#   STATE_DIR        default /srv/heimdall/state        (current tag, analyst backups)
set -euo pipefail

TAG=${1:?usage: deploy.sh <image-tag>}
[[ $TAG =~ ^(sha-[0-9a-f]{7,40}|v[0-9]+\.[0-9]+\.[0-9]+)$ ]] || { echo "bad tag: $TAG" >&2; exit 2; }
REPO=${IMAGE_REPO:?set IMAGE_REPO, e.g. ghcr.io/owner}
ENV_FILE=${HOSTED_ENV_FILE:-/srv/heimdall/hosted.env}
STATE=${STATE_DIR:-/srv/heimdall/state}
WAIT_S=${READY_TIMEOUT_S:-300}
cd "$(dirname "$0")/.."   # deploy/

[[ -r $ENV_FILE ]] || { echo "missing $ENV_FILE" >&2; exit 2; }
DATA=$(sed -n 's/^HEIMDALL_DATA=//p' "$ENV_FILE" | tail -1)
[[ -n $DATA && -d $DATA ]] || { echo "HEIMDALL_DATA not set or missing in $ENV_FILE" >&2; exit 2; }
mkdir -p "$STATE/backups"
PREV=$(cat "$STATE/current_tag" 2>/dev/null || true)

compose() {
  HEIMDALL_IMAGE="$REPO/heimdall:$1" HEIMDALL_EDGE_IMAGE="$REPO/heimdall-edge:$1" \
    docker compose --env-file "$ENV_FILE" -f compose.yml -f compose.hosted.yml "${@:2}"
}

wait_ready() {
  local id status deadline=$((SECONDS + WAIT_S))
  while ((SECONDS < deadline)); do
    id=$(compose "$1" ps -q heimdall)
    status=$(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null || echo starting)
    [[ $status == healthy ]] && return 0
    [[ $status == unhealthy ]] && return 1
    sleep 5
  done
  return 1
}

echo "deploy $TAG (previous: ${PREV:-none})"
compose "$TAG" pull --quiet heimdall edge          # pull first: keeps downtime to the restart

# Analyst state is not rebuildable (ADR-002): copy it while the writer is stopped.
compose "${PREV:-$TAG}" stop heimdall || true
if [[ -f $DATA/analyst.duckdb ]]; then
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  cp -p "$DATA/analyst.duckdb" "$STATE/backups/analyst-$ts-before-$TAG.duckdb"
  ls -1t "$STATE"/backups/analyst-*.duckdb | tail -n +15 | xargs -r rm --   # keep 14
fi

compose "$TAG" up -d --remove-orphans
if wait_ready "$TAG"; then
  echo "$TAG" > "$STATE/current_tag"
  docker image prune -f --filter "until=168h" >/dev/null || true
  echo "deployed $TAG"
  exit 0
fi

echo "::error::$TAG did not become healthy; recent logs:" >&2
compose "$TAG" logs --tail 80 heimdall >&2 || true
if [[ -n $PREV && $PREV != "$TAG" ]]; then
  echo "rolling back to $PREV" >&2
  compose "$PREV" up -d --remove-orphans
  wait_ready "$PREV" && echo "rolled back to $PREV" >&2 || echo "::error::rollback also unhealthy" >&2
fi
exit 1
