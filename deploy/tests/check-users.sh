#!/bin/sh
# Focused synthetic units only; no daemon socket, app data or credentials mounted.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
docker run --rm --pull never --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  --mount "type=bind,source=$ROOT/deploy/homebox,target=/source/deploy/homebox,readonly" \
  --mount "type=bind,source=$ROOT/deploy/lifecycle,target=/source/deploy/lifecycle,readonly" \
  --mount "type=bind,source=$ROOT/deploy/tests/test_users.py,target=/source/deploy/tests/test_users.py,readonly" \
  python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df \
  sh -eu -c 'sh -n /source/deploy/homebox; sh -n /source/deploy/lifecycle/primary.sh; sh -n /source/deploy/lifecycle/create-user.sh; python -B /source/deploy/tests/test_users.py'
