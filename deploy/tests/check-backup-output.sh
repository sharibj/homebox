#!/bin/sh
# Focused source-only checks; no Docker socket or private deployment mounts.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
if ! command -v docker >/dev/null 2>&1; then
  PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH; export PATH
fi
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:rw,nosuid,nodev,exec,size=64m \
  --mount "type=bind,source=$ROOT/deploy/scripts/backup.sh,target=/source/deploy/scripts/backup.sh,readonly" \
  --mount "type=bind,source=$ROOT/deploy/tests/test_backup.py,target=/source/deploy/tests/test_backup.py,readonly" \
  python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df \
  sh -eu -c 'sh -n /source/deploy/scripts/backup.sh; python -B /source/deploy/tests/test_backup.py'
