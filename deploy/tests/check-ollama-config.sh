#!/bin/sh
# Offline only: public fixtures, Compose rendering, and the real API resolver.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
IMAGE=${ASSISTANT_TEST_IMAGE:?Set ASSISTANT_TEST_IMAGE to the API image built from this source}
DOCKER=${DOCKER:-docker}
FIXTURE=$(mktemp -d "${TMPDIR:-/tmp}/homebox-ollama-config.XXXXXXXX")
trap 'rm -rf "$FIXTURE"' EXIT HUP INT TERM
chmod 755 "$FIXTURE"
mkdir -p "$FIXTURE/secrets/ai"
cp "$ROOT/deploy/docker-compose.yml" "$FIXTURE/docker-compose.yml"
cp "$ROOT/deploy/docker-compose.external-ai.yml" "$FIXTURE/docker-compose.external-ai.yml"
cp "$ROOT/deploy/docker-compose.lan.yml" "$FIXTURE/docker-compose.lan.yml"

# Empty host environment prevents shell AI_*/COMPOSE_* values overriding fixtures.
compose() {
  env -i PATH="$PATH" "$DOCKER" compose -p homebox-ollama-config-check "$@"
}
check() {
  "$DOCKER" run --rm -i --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 64 --memory 256m --cpus 1 \
    --tmpfs /tmp:rw,noexec,nosuid,size=8m \
    --env PYTHONPATH=/app \
    --mount "type=bind,source=$ROOT/deploy/tests,target=/checks,readonly" \
    "$IMAGE" python -B /checks/check_ollama_config.py "$1"
}
"$DOCKER" run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --pids-limit 64 --memory 256m --cpus 1 \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$ROOT/deploy/tests,target=/checks,readonly" \
  --mount "type=bind,source=$ROOT/apps/assistant-api/assistant,target=/public/assistant,readonly" \
  --mount "type=bind,source=$ROOT/deploy/.env.example,target=/public/default.env,readonly" \
  --mount "type=bind,source=$ROOT/deploy/examples/ollama-minicpm-host.env,target=/public/host.env,readonly" \
  --mount "type=bind,source=$FIXTURE,target=/fixtures" \
  "$IMAGE" python -B /checks/check_ollama_config.py prepare

for MODE in default host host-linux host-lan haiku-both external-chat external-vision gemini-chat gemini-vision gemini-both; do
  set -- --env-file "$FIXTURE/$MODE.env" -f "$FIXTURE/docker-compose.yml"
  if [ "$MODE" != default ]; then
    set -- "$@" -f "$FIXTURE/docker-compose.external-ai.yml"
  fi
  if [ "$MODE" = host-lan ]; then
    set -- "$@" -f "$FIXTURE/docker-compose.lan.yml"
  fi
  compose "$@" config --format json > "$FIXTURE/$MODE.json"
  printf '\nChecking %s\n' "$MODE"
  check "$MODE" < "$FIXTURE/$MODE.json"
  case "$MODE" in
    default)
      "$DOCKER" run --rm -i --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges:true --pids-limit 64 --memory 256m --cpus 1 \
        --mount "type=bind,source=$ROOT/deploy/tests,target=/checks,readonly" \
        "$IMAGE" python -B /checks/check_config.py < "$FIXTURE/$MODE.json"
      ;;
    external-*|gemini-*)
      "$DOCKER" run --rm -i --network none --read-only --cap-drop ALL \
        --security-opt no-new-privileges:true --pids-limit 64 --memory 256m --cpus 1 \
        --mount "type=bind,source=$ROOT/deploy/tests,target=/checks,readonly" \
        "$IMAGE" python -B /checks/check_provider_config.py "$MODE" < "$FIXTURE/$MODE.json"
      ;;
  esac
done
# Verify this checker rejects the original stale gateway profile as host Ollama.
if check host < "$FIXTURE/haiku-both.json" > "$FIXTURE/negative.log" 2>&1; then
  echo "ERROR: checker accepted stale gateway roles/keys as the host profile" >&2
  exit 1
fi
echo "PASS: ten offline profiles; stale gateway settings rejected as host Ollama."
