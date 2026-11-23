#!/bin/sh
set -eu
DEPLOY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
prefix="homebox-proxy-test-$$"
network="$prefix-net"
data="$prefix-data"
fixture="$prefix-fixture"
proxy="$prefix-proxy"
python=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
caddy=caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0
cleanup() {
  docker rm -f "$proxy" "$fixture" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  docker volume rm "$data" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM
docker network create --internal "$network" >/dev/null
docker volume create "$data" >/dev/null
docker run -d --name "$fixture" --network "$network" \
  --network-alias homebox --network-alias assistant-api --network-alias pwa \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 128m --pids-limit 64 \
  --mount "type=bind,source=$DEPLOY_DIR/tests,target=/tests,readonly" \
  "$python" python -B /tests/proxy_fixture.py serve >/dev/null
docker run -d --name "$proxy" --network "$network" \
  --network-alias inventory.home.arpa --network-alias homebox.home.arpa \
  --read-only --tmpfs /config --tmpfs /tmp --memory 128m --pids-limit 64 \
  -e HOMEBOX_HOST=homebox.home.arpa -e ASSISTANT_HOST=inventory.home.arpa -e TLS_ISSUER=internal \
  --mount "type=bind,source=$DEPLOY_DIR/reverse-proxy/Caddyfile,target=/etc/caddy/Caddyfile,readonly" \
  --mount "type=volume,source=$data,target=/data" "$caddy" >/dev/null
docker run --rm --network "$network" --read-only --cap-drop ALL --memory 128m \
  --mount "type=volume,source=$data,target=/certs,readonly" \
  --mount "type=bind,source=$DEPLOY_DIR/tests,target=/tests,readonly" \
  "$python" python -B /tests/proxy_fixture.py check
