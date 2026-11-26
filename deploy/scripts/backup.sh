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
BACKUP_DIR=$(CDPATH= cd -- "$BACKUP_DIR" && pwd -P)
archive=${1:-"homebox-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"}
case "$archive" in
  ""|*[!a-zA-Z0-9._-]*|.*) echo "Use a plain backup filename." >&2; exit 1 ;;
esac
if [ -e "$BACKUP_DIR/$archive" ] || [ -L "$BACKUP_DIR/$archive" ]; then
  echo "Backup already exists." >&2; exit 1
fi
partial=$BACKUP_DIR/.$archive.partial
if [ -e "$partial" ] || [ -L "$partial" ]; then
  echo "Backup partial already exists; it was retained." >&2; exit 1
fi
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

# The reader needs to traverse private data owned by the runtime UIDs, but has
# no permission to write an operator-owned 0700 output directory. Let the
# operator's shell create the private archive while the reader streams stdout.
if ! (
set -C
compose run --rm --no-deps -T \
  -e "BACKUP_EXTERNAL_AI=$external_ai" \
  -e "BACKUP_LAN_HTTPS=$lan_https" backup '
  umask 077
  printf "EXTERNAL_AI=%s\n" "$BACKUP_EXTERNAL_AI" >/snapshot/deployment-options.env
  printf "LAN_HTTPS=%s\n" "$BACKUP_LAN_HTTPS" >>/snapshot/deployment-options.env
  tar -czf - -C /snapshot \
    homebox assistant proxy-data proxy-config secrets config.env docker-compose.yml Caddyfile \
    docker-compose.external-ai.yml docker-compose.lan.yml Caddyfile.lan deployment-options.env
' >"$partial"
); then
  echo "Backup stream failed. No completed archive was published; any private partial file was retained." >&2
  exit 1
fi
# Mount the selected output read-only. A failed stream or validation leaves its
# private partial file in place and cannot be reported as a completed archive.
if ! compose run --rm --no-deps -T --volume "$BACKUP_DIR:/backups:ro" \
  -e "ARCHIVE=$archive" backup 'tar -tzf "/backups/.$ARCHIVE.partial" >/dev/null'; then
  echo "Backup validation failed. No completed archive was published; the private partial file was retained." >&2
  exit 1
fi
if [ -e "$BACKUP_DIR/$archive" ] || [ -L "$BACKUP_DIR/$archive" ]; then
  echo "Backup already exists." >&2; exit 1
fi
mv "$partial" "$BACKUP_DIR/$archive"
echo "Created $BACKUP_DIR/$archive (contains secrets; encrypt before copying elsewhere)"
