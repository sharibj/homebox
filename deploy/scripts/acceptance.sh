#!/bin/sh
# Isolated localhost integration only. Never uses deploy/.env or its secrets.
set -eu
DEPLOY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
action=${1:-}
target=${2:-}
case "$target" in /*) ;; *) echo "Supply an absolute, disposable acceptance directory." >&2; exit 1 ;; esac
python=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

if [ "$action" = prepare ]; then
  test ! -e "$target" || { echo "Acceptance directory must not exist yet." >&2; exit 1; }
  umask 077
  mkdir -m 700 "$target"
  cp "$DEPLOY_DIR/.env.example" "$target/.env.example"
  HOMEBOX_DEPLOY_DIR="$target" sh "$DEPLOY_DIR/scripts/init-config.sh"
  HOMEBOX_DEPLOY_DIR="$target" sh "$DEPLOY_DIR/scripts/init-secrets.sh"
  project="homebox-acceptance-$(date -u +%Y%m%d%H%M%S)-$$"
  {
    printf '\nACCEPTANCE_PROJECT=%s\n' "$project"
    printf 'ACCEPTANCE_HOMEBOX_IMAGE=%s\n' "${ACCEPTANCE_HOMEBOX_IMAGE:-homebox-inventory:local-4d19c9e4}"
    printf 'ACCEPTANCE_SECRETS_DIR=%s/secrets\n' "$target"
    printf 'ACCEPTANCE_ENV_FILE=%s/.env\n' "$target"
    printf 'ACCEPTANCE_BACKUP_DIR=%s/backups\n' "$target"
  } >>"$target/.env"
  mkdir -m 700 "$target/backups"
  printf 'Prepared %s. No containers started.\n' "$target"
  exit 0
fi

test -f "$target/.env" || { echo "Prepare a new acceptance directory first." >&2; exit 1; }
project=$(docker run --rm --network none --read-only \
  --mount "type=bind,source=$target/.env,target=/acceptance.env,readonly" \
  "$python" python -B -c '
import re
from pathlib import Path
matches = re.findall(r"(?m)^ACCEPTANCE_PROJECT=(homebox-acceptance-[a-zA-Z0-9-]+)$", Path("/acceptance.env").read_text())
if len(matches) != 1:
    raise SystemExit("Refusing a non-acceptance or ambiguous project.")
print(matches[0])
')
compose() {
  if [ "${ACCEPTANCE_VISION_FIXTURE:-0}" = 1 ]; then
    docker compose --env-file "$target/.env" -p "$project" \
      -f "$DEPLOY_DIR/docker-compose.yml" -f "$DEPLOY_DIR/docker-compose.acceptance.yml" \
      -f "$DEPLOY_DIR/docker-compose.vision-fixture.yml" "$@"
  else
    docker compose --env-file "$target/.env" -p "$project" \
      -f "$DEPLOY_DIR/docker-compose.yml" -f "$DEPLOY_DIR/docker-compose.acceptance.yml" "$@"
  fi
}
case "$action" in
  up)
    compose up -d --no-build --pull never --wait --wait-timeout 180
    docker run --rm --network "${project}_browser" --read-only --cap-drop ALL \
      --user "$(id -u):$(id -g)" \
      --mount "type=bind,source=$target,target=/acceptance" \
      --mount "type=bind,source=$DEPLOY_DIR/tests,target=/tests,readonly" \
      "$python" python -B /tests/acceptance_seed.py
    printf 'Ready: http://localhost:8088\nProtected credentials: %s/credentials.json\n' "$target"
    ;;
  backup)
    HOMEBOX_ENV_FILE="$target/.env" HOMEBOX_BACKUP_DIR="$target/backups" \
      HOMEBOX_COMPOSE_OVERRIDE="$DEPLOY_DIR/docker-compose.acceptance.yml" \
      COMPOSE_PROJECT_NAME="$project" sh "$DEPLOY_DIR/scripts/backup.sh" "${3:-acceptance.tar.gz}"
    ;;
  down)
    compose down --volumes --remove-orphans
    echo "Removed only the disposable acceptance containers/networks/volumes; protected files remain in $target."
    ;;
  config)
    compose config
    ;;
  *)
    echo "Usage: sh acceptance.sh {prepare|up|backup|down|config} /absolute/disposable-directory" >&2
    exit 1
    ;;
esac
