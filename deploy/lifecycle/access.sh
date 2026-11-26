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
  [ "$LAN" = 0 ] || set -- -f "$DEPLOY/docker-compose.lan.yml" "$@"
  [ "$EXTERNAL" = 0 ] || set -- -f "$DEPLOY/docker-compose.external-ai.yml" "$@"
  set -- -f "$DEPLOY/docker-compose.yml" "$@"
  docker compose -p "$PROJECT" --env-file "$access_env" "$@"
}
confirm "Change private HTTPS access only, preserving all images, data, keys and the existing CA?"
if [ "$ACCESS_ACTION" != rollback ] && { [ "$ACCESS_ACTION" = tailscale ] || [ "$LAN" = 1 ]; }; then
  tailscale ip -4 >"$STATE/work/tailscale-ip.txt"
fi
if [ "$ACCESS_ACTION" = tailscale ]; then
  # A missing CLI/address refuses before any protected configuration change.
  dce config --quiet
  dce config --format json >"$STATE/work/config.json"
  meta config
  access_ca
  meta access-prepare
  IFS= read -r access_noop <"$STATE/work/access-noop"
  [ "$access_noop" = 0 ] || exit 0
fi
ACCESS_ACTIVE=1
IFS= read -r EXTERNAL <"$STATE/work/external"
IFS= read -r LAN <"$STATE/work/lan"
IFS= read -r access_env <"$STATE/work/access-env"
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
meta access-guard
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
meta access-guard
ACCESS_PHASE=configuring-api
meta access-phase
access_compose up -d --no-deps --no-build --pull never --wait --wait-timeout 240 assistant-api
capture
meta access-guard
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
IFS= read -r access_ip <"$STATE/work/access-ip"
IFS= read -r access_proof <"$STATE/work/access-origin-proof"
IFS= read -r access_api_image <"$STATE/work/access-api-image"
IFS= read -r access_fixed_hosts <"$STATE/work/access-fixed-hosts"
if [ "$LAN" = 1 ] && [ "$access_fixed_hosts" = 1 ]; then
  docker run --rm --network "container:$access_proxy" --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --user "$(id -u):$(id -g)" \
    -e "LAN_IP=$access_ip" -e LAN_CA_FILE=/public-ca.crt -e "LAN_EVIDENCE_FILE=$access_proof" \
    --mount "type=bind,source=$ROOT/deploy/tests/check_lan_access.py,target=/ops/check_lan_access.py,readonly" \
    --mount "type=bind,source=$STATE/work/public-ca.crt,target=/public-ca.crt,readonly" \
    --mount "type=bind,source=$STATE,target=/state" \
    "$access_api_image" python -B /ops/check_lan_access.py
fi
capture
meta access-guard
meta access-finish
ACCESS_ACTIVE=0
