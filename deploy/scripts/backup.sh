#!/bin/sh
set -eu
DEPLOY_DIR=${HOMEBOX_DEPLOY_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}
cd "$DEPLOY_DIR"
ENV_FILE=${HOMEBOX_ENV_FILE:-"$DEPLOY_DIR/.env"}
BACKUP_DIR=${HOMEBOX_BACKUP_DIR:-"$DEPLOY_DIR/backups"}
test -f "$ENV_FILE" || { echo "Create the deployment environment file first." >&2; exit 1; }
umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
archive=${1:-"homebox-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"}
case "$archive" in
  ""|*[!a-zA-Z0-9._-]*|.*) echo "Use a plain backup filename." >&2; exit 1 ;;
esac
test ! -e "$BACKUP_DIR/$archive" || { echo "Backup already exists." >&2; exit 1; }

compose() {
  if [ -n "${HOMEBOX_COMPOSE_OVERRIDE:-}" ]; then
    docker compose --env-file "$ENV_FILE" -f docker-compose.yml -f "$HOMEBOX_COMPOSE_OVERRIDE" "$@"
  else
    docker compose --env-file "$ENV_FILE" -f docker-compose.yml "$@"
  fi
}
running=$(compose ps --status running --services)
stopped=""
for service in $running; do
  case "$service" in
    proxy|assistant-api|homebox-mcp|homebox|pwa) stopped="$stopped $service" ;;
  esac
done
resume() {
  result=$?
  trap - EXIT HUP INT TERM
  if [ -n "$stopped" ]; then
    # Intentional word splitting: these are known Compose service names.
    compose up --no-deps --no-recreate --no-build --wait --wait-timeout 180 $stopped || result=1
  fi
  exit "$result"
}
trap resume EXIT
trap 'exit 130' HUP INT TERM
if [ -n "$stopped" ]; then
  compose stop --timeout 45 $stopped
fi

# The writers are stopped; SQLite databases, WAL files, uploads and attachments
# are copied together. Keep secrets with the encrypted assistant credentials.
compose run --rm --no-deps -T -e "ARCHIVE=$archive" \
  -e "BACKUP_UID=$(id -u)" -e "BACKUP_GID=$(id -g)" backup '
  umask 077
  test ! -e "/backups/$ARCHIVE"
  tar -czf "/backups/.$ARCHIVE.partial" -C /snapshot \
    homebox assistant proxy-data proxy-config secrets config.env docker-compose.yml Caddyfile
  tar -tzf "/backups/.$ARCHIVE.partial" >/dev/null
  chown "$BACKUP_UID:$BACKUP_GID" "/backups/.$ARCHIVE.partial"
  mv "/backups/.$ARCHIVE.partial" "/backups/$ARCHIVE"
  echo "Created /backups/$ARCHIVE (contains secrets; encrypt before copying elsewhere)"
'
