#!/bin/sh
# Uses only generated fixtures and uniquely named disposable Docker resources.
set -eu
DEPLOY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/homebox-backup-test.XXXXXX")
project="homebox-backup-test-$$"
export COMPOSE_PROJECT_NAME="$project"
alpine=alpine:3.24.4@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf
python=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
compose() { docker compose --env-file "$scratch/.env" -f "$scratch/docker-compose.yml" "$@"; }
cleanup() {
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker volume rm "${project}_restored" >/dev/null 2>&1 || true
  # This directory was created above by mktemp, never supplied by the caller.
  rm -rf "$scratch"
}
trap cleanup EXIT HUP INT TERM
mkdir -p "$scratch/reverse-proxy"
cp "$DEPLOY_DIR/docker-compose.yml" "$scratch/docker-compose.yml"
cp "$DEPLOY_DIR/docker-compose.external-ai.yml" "$scratch/docker-compose.external-ai.yml"
cp "$DEPLOY_DIR/docker-compose.lan.yml" "$scratch/docker-compose.lan.yml"
cp "$DEPLOY_DIR/.env.example" "$scratch/.env"
cp "$DEPLOY_DIR/.env.example" "$scratch/.env.example"
cp "$DEPLOY_DIR/reverse-proxy/Caddyfile" "$scratch/reverse-proxy/Caddyfile"
cp "$DEPLOY_DIR/reverse-proxy/Caddyfile.lan" "$scratch/reverse-proxy/Caddyfile.lan"
printf '\nPROXY_LAN_IP=192.168.250.10\n' >>"$scratch/.env"
HOMEBOX_DEPLOY_DIR="$scratch" sh "$DEPLOY_DIR/scripts/init-config.sh"
HOMEBOX_DEPLOY_DIR="$scratch" sh "$DEPLOY_DIR/scripts/init-config.sh"
HOMEBOX_DEPLOY_DIR="$scratch" sh "$DEPLOY_DIR/scripts/init-secrets.sh"
HOMEBOX_DEPLOY_DIR="$scratch" sh "$DEPLOY_DIR/scripts/init-secrets.sh"
HOMEBOX_DEPLOY_DIR="$scratch" sh "$DEPLOY_DIR/scripts/init-ai-secret.sh"
test ! -e "$scratch/secrets/ai/ai_api_key"
test ! -e "$scratch/secrets/ai/ai_chat_api_key"
test ! -e "$scratch/secrets/ai/ai_vision_api_key"
printf 'synthetic-provider-key-only\n' >"$scratch/provider-key-input"
HOMEBOX_DEPLOY_DIR="$scratch" sh "$DEPLOY_DIR/scripts/init-ai-secret.sh" shared "$scratch/provider-key-input"
printf 'must-not-replace-existing-key\n' >"$scratch/provider-key-input"
HOMEBOX_DEPLOY_DIR="$scratch" sh "$DEPLOY_DIR/scripts/init-ai-secret.sh" shared "$scratch/provider-key-input"
: >"$scratch/provider-key-input"
HOMEBOX_DEPLOY_DIR="$scratch" sh "$DEPLOY_DIR/scripts/init-ai-secret.sh" chat "$scratch/provider-key-input"
test ! -s "$scratch/secrets/ai/ai_chat_api_key"
# Remove only this generated empty fixture to test a separate nonempty chat key.
rm "$scratch/secrets/ai/ai_chat_api_key"
printf 'synthetic-chat-key-only\n' >"$scratch/provider-key-input"
HOMEBOX_DEPLOY_DIR="$scratch" sh "$DEPLOY_DIR/scripts/init-ai-secret.sh" chat "$scratch/provider-key-input"
printf 'synthetic-vision-key-only\n' >"$scratch/provider-key-input"
HOMEBOX_DEPLOY_DIR="$scratch" sh "$DEPLOY_DIR/scripts/init-ai-secret.sh" vision "$scratch/provider-key-input"

# A running, disposable PWA fixture verifies stop/resume behavior without
# starting the real applications, exposing a port, or accessing real inventory.
docker run --rm --network none --read-only \
  --mount "type=bind,source=$scratch,target=/out" "$python" python -B -c '
import json, pathlib, sys
config = {
    "services": {"pwa": {
        "image": sys.argv[1],
        "command": ["python", "-B", "/tests/proxy_fixture.py", "serve"],
        "volumes": [{"type": "bind", "source": sys.argv[2], "target": "/tests", "read_only": True}],
        "healthcheck": {"test": ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:8080/healthz\").read()"]},
    }}
}
if sys.argv[3] == "1":
    config["services"]["assistant-api"] = {
        **config["services"]["pwa"], "user": "10001:10001",
    }
lines = ["services:"]
for name, service in config["services"].items():
    lines += ["  " + name + ":", "    networks: !reset []", "    network_mode: none",
              "    depends_on: !reset {}"]
    lines += ["    " + key + ": " + json.dumps(value) for key, value in service.items()]
pathlib.Path("/out/fixture.yml").write_text("\n".join(lines) + "\n")
' "$python" "$DEPLOY_DIR/tests" "${EXTERNAL_PROVIDER_FIXTURE:-0}"
if [ "${EXTERNAL_PROVIDER_FIXTURE:-0}" = 1 ]; then
  if [ "${LAN_HTTPS_FIXTURE:-0}" = 1 ]; then
    compose -f "$scratch/docker-compose.external-ai.yml" -f "$scratch/docker-compose.lan.yml" -f "$scratch/fixture.yml" \
      up -d --no-deps --no-build --wait --wait-timeout 45 pwa assistant-api
  else
    compose -f "$scratch/docker-compose.external-ai.yml" -f "$scratch/fixture.yml" \
      up -d --no-deps --no-build --wait --wait-timeout 45 pwa assistant-api
  fi
  api_before=$(compose ps -q assistant-api)
else
  compose -f "$scratch/fixture.yml" up -d --no-deps --no-build --wait --wait-timeout 45 pwa
fi

docker run --rm --network none \
  --mount "type=volume,source=${project}_homebox_data,target=/hb" \
  --mount "type=volume,source=${project}_assistant_data,target=/assistant" \
  --mount "type=volume,source=${project}_caddy_data,target=/proxy" \
  "$alpine" sh -eu -c '
    printf "homebox fixture\n" >/hb/fixture.db
    printf "assistant fixture\n" >/assistant/fixture.db
    printf "CA fixture\n" >/proxy/fixture.crt
    chown 65532:65532 /hb/fixture.db
    chown 10001:10001 /assistant/fixture.db
    chmod 600 /hb/fixture.db /assistant/fixture.db
  '
HOMEBOX_DEPLOY_DIR="$scratch" HOMEBOX_COMPOSE_OVERRIDE="$scratch/fixture.yml" \
  HOMEBOX_BACKUP_EXTERNAL_AI="${EXTERNAL_PROVIDER_FIXTURE:-0}" \
  HOMEBOX_BACKUP_LAN_HTTPS="${LAN_HTTPS_FIXTURE:-0}" HOMEBOX_BACKUP_STOP_TIMEOUT=-1 \
  sh "$DEPLOY_DIR/scripts/backup.sh" fixture.tar.gz
if [ "${EXTERNAL_PROVIDER_FIXTURE:-0}" = 1 ]; then
  test "$(compose ps -q assistant-api)" = "$api_before"
  docker exec "$api_before" python -B -c 'from pathlib import Path; assert all((Path("/run/secrets/ai") / name).is_file() for name in ("ai_api_key", "ai_chat_api_key", "ai_vision_api_key"))'
  test "$(compose ps --status running --services | wc -l | tr -d ' ')" = 2
else
  test "$(compose ps --status running --services)" = "pwa"
fi

docker run --rm --network none --read-only \
  -e "RESTORE_UID=$(id -u)" -e "RESTORE_GID=$(id -g)" \
  --mount "type=bind,source=$scratch/backups,target=/backups,readonly" \
  --mount "type=bind,source=$DEPLOY_DIR/scripts,target=/ops,readonly" \
  --mount "type=volume,source=${project}_restored,target=/restore" \
  "$python" python -B /ops/restore.py /backups/fixture.tar.gz
docker run --rm --network none \
  -e "EXPECT_EXTERNAL=${EXTERNAL_PROVIDER_FIXTURE:-0}" \
  -e "EXPECT_LAN=${LAN_HTTPS_FIXTURE:-0}" \
  -e "EXPECT_UID=$(id -u)" \
  --mount "type=bind,source=$scratch/backups,target=/backups,readonly" \
  --mount "type=bind,source=$scratch/secrets,target=/secrets,readonly" \
  --mount "type=volume,source=${project}_restored,target=/restored" \
  "$alpine" sh -eu -c '
    test "$(cat /restored/homebox/fixture.db)" = "homebox fixture"
    test "$(cat /restored/assistant/fixture.db)" = "assistant fixture"
    test "$(cat /restored/proxy-data/fixture.crt)" = "CA fixture"
    test "$(stat -c %a /restored/assistant/fixture.db)" = "600"
    test "$(stat -c %u /restored/assistant/fixture.db)" = "10001"
    test "$(stat -c %u /restored/homebox/fixture.db)" = "65532"
    for name in homebox_api_pepper assistant_session_secret mcp_shared_secret; do
      cmp "/secrets/$name" "/restored/secrets/$name"
      test -s "/secrets/$name"
      test "$(wc -c <"/secrets/$name")" -ge 32
    done
    for name in ai_api_key ai_chat_api_key ai_vision_api_key; do
      cmp "/secrets/ai/$name" "/restored/secrets/ai/$name"
      test "$(stat -c %a "/restored/secrets/ai/$name")" = "444"
      test "$(stat -c %u "/restored/secrets/ai/$name")" = "$EXPECT_UID"
    done
    test "$(cat /restored/secrets/ai/ai_api_key)" = "synthetic-provider-key-only"
    test "$(cat /restored/secrets/ai/ai_chat_api_key)" = "synthetic-chat-key-only"
    test "$(cat /restored/secrets/ai/ai_vision_api_key)" = "synthetic-vision-key-only"
    test "$(stat -c %a /restored/secrets)" = 700
    test "$(stat -c %a /restored/secrets/ai)" = 755
    test "$(stat -c %u /restored/secrets/ai)" = "$EXPECT_UID"
    test -s /restored/config/docker-compose.external-ai.yml
    test -s /restored/config/docker-compose.lan.yml
    test -s /restored/config/Caddyfile.lan
    grep -q "^EXTERNAL_AI=${EXPECT_EXTERNAL}$" /restored/config/deployment-options.env
    grep -q "^LAN_HTTPS=${EXPECT_LAN}$" /restored/config/deployment-options.env
    test -s /restored/config/config.env
    grep -q "^HOMEBOX_INSTANCE_ID=." /restored/config/config.env
    echo "PASS: private-owner data, CA, secrets and config survive backup/restore"
  '
echo "PASS: backup resumes only the originally running service"
