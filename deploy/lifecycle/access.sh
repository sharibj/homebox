# Sourced only by the explicit access command, after canonical locking/sanitizing.
access_ca() {
  docker volume inspect "${PROJECT}_caddy_data" >"$STATE/work/access-ca-volume.json"
  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --mount "type=volume,source=${PROJECT}_caddy_data,target=/ca,readonly" \
    "$PYTHON" python -B -c '
import hashlib,json,stat
from pathlib import Path
p=Path("/ca/caddy/pki/authorities/local")
print(json.dumps({n:{"sha256":hashlib.sha256((p/n).read_bytes()).hexdigest(),
 "mode":stat.S_IMODE((p/n).stat().st_mode),"uid":(p/n).stat().st_uid,"gid":(p/n).stat().st_gid}
 for n in ("root.crt","root.key")}))
' >"$STATE/work/access-ca.json"
}
access_compose() {
  [ -z "$TEST_OVERRIDE" ] || set -- -f "$TEST_OVERRIDE" "$@"
  [ "$access_rescue" = 0 ] || set -- -f "$DEPLOY/docker-compose.lan-rescue.yml" "$@"
  [ "$LOCAL_LAN" = 0 ] || set -- -f "$DEPLOY/docker-compose.lan-local.yml" "$@"
  [ "$LAN" = 0 ] || set -- -f "$DEPLOY/docker-compose.lan.yml" "$@"
  [ "$EXTERNAL" = 0 ] || set -- -f "$DEPLOY/docker-compose.external-ai.yml" "$@"
  set -- -f "$DEPLOY/docker-compose.yml" "$@"
  docker compose -p "$PROJECT" --env-file "$access_env" "$@"
}
access_refresh_addresses() {
  if [ "$ACCESS_LOCAL" = 1 ] || [ "${LOCAL_LAN:-0}" = 1 ]; then
    command -v ip >/dev/null 2>&1 || {
      echo "LAN interface validation requires the host's iproute2 ip command." >&2; return 1;
    }
    ip -j -d link show >"$STATE/work/local-lan-links.json"
    ip -j -4 address show >"$STATE/work/local-lan-addresses.json"
    sh "$DEPLOY/lifecycle/local_lan_evidence.sh" >"$STATE/work/local-lan-devices.tsv"
  fi
  if { [ "$ACCESS_ACTION" != rollback ] && [ "${1:-}" != recovery ]; } || [ "${access_rescue:-0}" = 1 ]; then
    tailscale ip -4 >"$STATE/work/tailscale-ip.txt"
  fi
}
access_guard() {
  access_refresh_addresses
  meta access-guard
}
access_restore_on_error() {
  access_pending=$(meta access-is-pending) || return 1
  [ "$access_pending" = 1 ] || return 0
  access_refresh_addresses recovery || return 1
  meta access-auto-restore || return 1
  IFS= read -r access_restore_attempt <"$STATE/work/access-restore-attempt"
  # Separate errexit scope: recovery must stop at its first failed guard.
  (
    printf 'access-restore %s\n' "$access_restore_attempt"
    trap - EXIT HUP INT TERM
    set -eu
    ACCESS_ACTION=rollback
    ACCESS_RECOVERY_ONLY=1
    . "$DEPLOY/lifecycle/access.sh"
  ) >"$STATE/work/access-exact-restore.log" 2>&1
  access_restore_result=$?
  if [ "$access_restore_result" -eq 0 ]; then
    return 0
  fi
  access_refresh_addresses recovery || return 1
  tailscale ip -4 >"$STATE/work/tailscale-ip.txt" || return 1
  meta access-rescue || return 1
  (
    trap - EXIT HUP INT TERM
    set -eu
    ACCESS_ACTION=rollback
    ACCESS_RECOVERY_ONLY=1
    . "$DEPLOY/lifecycle/access.sh"
  ) >"$STATE/work/access-tailnet-rescue.log" 2>&1
}
if [ "${ACCESS_LISTEN_ALL:-0}" = 1 ]; then
  confirm "Publish HTTPS 443/8443 on ALL IPv4 interfaces, preserving images, data, keys and the existing CA?"
else
  confirm "Apply the selected HTTPS access configuration, preserving images, data, keys and the existing CA?"
fi
access_refresh_addresses
if [ "$ACCESS_ACTION" = tailscale ] || [ "$ACCESS_ACTION" = lan ]; then
  # A missing CLI/address refuses before any protected configuration change.
  dce config --quiet
  dce config --format json >"$STATE/work/config.json"
  meta config
  access_ca
  if [ "$ACCESS_ACTION" = lan ]; then
    meta access-local-preflight
    access_api=$(dce ps -q assistant-api)
    docker exec "$access_api" python -B -c \
      'from assistant.config import Config; print("1" if "lan_origin" in Config.__dataclass_fields__ else "0")' \
      >"$STATE/work/access-api-lan-capability"
    IFS= read -r access_capable <"$STATE/work/access-api-lan-capability"
    [ "$access_capable" = 1 ] || {
      echo "The installed assistant API predates LAN support. Update sources, run deploy/homebox --yes redeploy, then repeat access lan." >&2
      exit 1
    }
    IFS= read -r access_needs_native <"$STATE/work/access-needs-native"
    if [ "$access_needs_native" = 1 ]; then
      access_backup="access-native-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
      printf '%s\n' "$access_backup" >"$STATE/work/access-backup-name"
      COMPOSE_PROJECT_NAME="$PROJECT" HOMEBOX_DEPLOY_DIR="$DEPLOY" \
        HOMEBOX_BACKUP_DIR="$DEPLOY/backups" HOMEBOX_BACKUP_EXTERNAL_AI="$EXTERNAL" \
        HOMEBOX_BACKUP_LAN_HTTPS="$LAN" HOMEBOX_BACKUP_LOCAL_LAN="$LOCAL_LAN" \
        HOMEBOX_BACKUP_STOP_TIMEOUT=-1 HOMEBOX_COMPOSE_OVERRIDE="$TEST_OVERRIDE" \
        sh "$DEPLOY/scripts/backup.sh" "$access_backup"
      meta access-backup-record
      capture
      dce config --format json >"$STATE/work/config.json"
      meta config
      access_ca
    fi
  fi
  meta access-prepare
  IFS= read -r access_noop <"$STATE/work/access-noop"
  [ "$access_noop" = 0 ] || exit 0
fi
ACCESS_ACTIVE=1
IFS= read -r EXTERNAL <"$STATE/work/external"
IFS= read -r LAN <"$STATE/work/lan"
IFS= read -r LOCAL_LAN <"$STATE/work/local-lan"
IFS= read -r access_env <"$STATE/work/access-env"
IFS= read -r access_native <"$STATE/work/access-native"
IFS= read -r access_rescue <"$STATE/work/access-rescue"
# Add only new operation-specific aliases. Never overwrite an existing alias.
while IFS='|' read -r access_image access_alias; do
  if access_actual=$(docker image inspect --format '{{.Id}}' "$access_alias" 2>/dev/null); then
    [ "$access_actual" = "$access_image" ] || { echo "Access image alias drift; refusing." >&2; exit 1; }
  else
    docker image inspect "$access_image" >/dev/null
    docker tag "$access_image" "$access_alias"
  fi
done <"$STATE/work/access-aliases"
while IFS='|' read -r access_image access_alias; do
  [ "$(docker image inspect --format '{{.Id}}' "$access_alias")" = "$access_image" ] || {
    echo "Access target image differs from its captured exact ID." >&2; exit 1;
  }
done <"$STATE/work/access-target-images"
access_compose config --quiet
access_compose config --format json >"$STATE/work/config.json"
meta access-candidate
access_guard
ACCESS_PHASE=closing
meta access-phase
access_proxy=$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" \
  --filter label=com.docker.compose.service=proxy)
[ -z "$access_proxy" ] || docker stop --timeout -1 $access_proxy
# No forced cancellation: the unchanged API must drain before its configuration changes.
access_attempt=0
while :; do
  access_api=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" \
    --filter label=com.docker.compose.service=assistant-api)
  [ -n "$access_api" ] || break
  docker exec "$access_api" python -B -c '
import json,os,sqlite3
p=os.environ.get("ASSISTANT_STATE_PATH","/data/assistant.db")
with sqlite3.connect("file:"+p+"?mode=ro",uri=True,timeout=5) as c:
 print(json.dumps([c.execute("SELECT count(*) FROM capture_jobs WHERE status=?",(s,)).fetchone()[0] for s in ("queued","running")]+[c.execute("SELECT count(*) FROM "+t+" WHERE status=?",("running",)).fetchone()[0] for t in ("actions","chat_turns")]))
' >"$STATE/work/counts.json"
  if meta idle; then break; fi
  access_attempt=$((access_attempt + 1))
  [ "$access_attempt" -lt 60 ] || { echo "Access work did not drain; ingress remains closed." >&2; exit 1; }
  sleep 5
done
capture
access_guard
if [ "$access_native" = 1 ]; then
  ACCESS_PHASE=configuring-native
  meta access-phase
  access_compose up -d --no-deps --no-build --pull never --wait --wait-timeout 240 homebox
  capture
  access_guard
fi
ACCESS_PHASE=configuring-api
meta access-phase
access_compose up -d --no-deps --no-build --pull never --wait --wait-timeout 240 assistant-api
capture
access_guard
ACCESS_PHASE=configuring-proxy
meta access-phase
access_compose up -d --no-deps --no-build --pull never --wait --wait-timeout 240 proxy
capture
access_ca
meta access-verify
access_proxy=$(access_compose ps -q proxy)
access_pwa=$(access_compose ps -q pwa)
meta prepare-static
docker cp "$access_pwa:/usr/share/nginx/html/." "$STATE/work/static/"
docker cp "$access_proxy:/data/caddy/pki/authorities/local/root.crt" "$STATE/work/public-ca.crt"
docker run --rm --network "container:$access_proxy" --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$ROOT,target=/source,readonly" \
  --mount "type=bind,source=$STATE,target=/state" \
  "$PYTHON" python -B /source/deploy/lifecycle/metadata.py access-smoke
meta access-proof-paths
IFS= read -r access_api_image <"$STATE/work/access-api-image"
IFS= read -r access_homebox_host <"$STATE/work/access-homebox-host"
IFS= read -r access_assistant_host <"$STATE/work/access-assistant-host"
IFS= read -r access_proof_attempt <"$STATE/work/access-proof-attempt"
IFS= read -r access_proof_config <"$STATE/work/access-proof-config"
while IFS='|' read -r access_ip access_local_listener access_proof; do
  docker run --rm --network "container:$access_proxy" --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --user "$(id -u):$(id -g)" \
    -e "LAN_IP=$access_ip" -e LAN_CA_FILE=/public-ca.crt -e "LAN_EVIDENCE_FILE=$access_proof" \
    -e "LAN_DUAL_LOCAL=$access_local_listener" \
    -e "LAN_HOMEBOX_HOST=$access_homebox_host" -e "LAN_ASSISTANT_HOST=$access_assistant_host" \
    -e "LAN_ACCESS_ATTEMPT=$access_proof_attempt" -e "LAN_ACCESS_CONFIG_SHA256=$access_proof_config" \
    --mount "type=bind,source=$ROOT/deploy/tests/check_lan_access.py,target=/ops/check_lan_access.py,readonly" \
    --mount "type=bind,source=$STATE/work/public-ca.crt,target=/public-ca.crt,readonly" \
    --mount "type=bind,source=$STATE,target=/state" \
    "$access_api_image" python -B /ops/check_lan_access.py
done <"$STATE/work/access-origin-checks"
capture
access_guard
meta access-finish
ACCESS_ACTIVE=0
