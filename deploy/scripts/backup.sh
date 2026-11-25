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
external_override=${HOMEBOX_BACKUP_EXTERNAL_AI:-0}
lan_override=${HOMEBOX_BACKUP_LAN_HTTPS:-0}
stop_timeout=${HOMEBOX_BACKUP_STOP_TIMEOUT:-45}
case "$external_override:$lan_override" in
  0:0|0:1|1:0|1:1) ;;
  *) echo "Backup overlay flags must be 0 or 1." >&2; exit 1 ;;
esac
case "$stop_timeout" in
  -1) ;;
  ""|*[!0-9]*) echo "Use a nonnegative stop timeout, or -1 for unlimited grace." >&2; exit 1 ;;
esac

compose() {
  if [ -n "${HOMEBOX_COMPOSE_OVERRIDE:-}" ]; then
    set -- -f "$HOMEBOX_COMPOSE_OVERRIDE" "$@"
  fi
  if [ "$lan_override" = 1 ]; then
    set -- -f docker-compose.lan.yml "$@"
  fi
  if [ "$external_override" = 1 ]; then
    set -- -f docker-compose.external-ai.yml "$@"
  fi
  docker compose --env-file "$ENV_FILE" -f docker-compose.yml "$@"
}
running=$(compose ps --status running --services)
external_ai=0
lan_https=0
api=$(compose ps -a -q assistant-api)
if [ -n "$api" ]; then
  files=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' $api)
  case "$files" in
    *"/docker-compose.external-ai.yml,"*|*"/docker-compose.external-ai.yml") external_ai=1 ;;
  esac
  case "$files" in
    *"/docker-compose.lan.yml,"*|*"/docker-compose.lan.yml") lan_https=1 ;;
  esac
fi
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
  compose stop --timeout "$stop_timeout" $stopped
fi

# The writers are stopped; SQLite databases, WAL files, uploads and attachments
# are copied together. Keep secrets with the encrypted assistant credentials.
compose run --rm --no-deps -T -e "ARCHIVE=$archive" \
  -e "BACKUP_EXTERNAL_AI=$external_ai" \
  -e "BACKUP_LAN_HTTPS=$lan_https" \
  -e "BACKUP_UID=$(id -u)" -e "BACKUP_GID=$(id -g)" backup '
  umask 077
  test ! -e "/backups/$ARCHIVE"
  printf "EXTERNAL_AI=%s\n" "$BACKUP_EXTERNAL_AI" >/snapshot/deployment-options.env
  printf "LAN_HTTPS=%s\n" "$BACKUP_LAN_HTTPS" >>/snapshot/deployment-options.env
  tar -czf "/backups/.$ARCHIVE.partial" -C /snapshot \
    homebox assistant proxy-data proxy-config secrets config.env docker-compose.yml Caddyfile \
    docker-compose.external-ai.yml docker-compose.lan.yml Caddyfile.lan deployment-options.env
  tar -tzf "/backups/.$ARCHIVE.partial" >/dev/null
  chown "$BACKUP_UID:$BACKUP_GID" "/backups/.$ARCHIVE.partial"
  mv "/backups/.$ARCHIVE.partial" "/backups/$ARCHIVE"
  echo "Created /backups/$ARCHIVE (contains secrets; encrypt before copying elsewhere)"
'
