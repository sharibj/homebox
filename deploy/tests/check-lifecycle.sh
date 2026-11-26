#!/bin/sh
# Unit/syntax checks only. No production config, Docker socket or inventory mount.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
if ! command -v docker >/dev/null 2>&1; then
  PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH; export PATH
fi
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  --mount "type=bind,source=$ROOT/deploy/homebox,target=/source/deploy/homebox,readonly" \
  --mount "type=bind,source=$ROOT/deploy/lifecycle,target=/source/deploy/lifecycle,readonly" \
  --mount "type=bind,source=$ROOT/deploy/tests/test_lifecycle.py,target=/source/deploy/tests/test_lifecycle.py,readonly" \
  --mount "type=bind,source=$ROOT/deploy/tests/lifecycle_integration.py,target=/source/deploy/tests/lifecycle_integration.py,readonly" \
  python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df \
  sh -eu -c 'sh -n /source/deploy/homebox; python -B -c "import ast; from pathlib import Path; ast.parse(Path(\"/source/deploy/tests/lifecycle_integration.py\").read_text())"; python -B /source/deploy/tests/test_lifecycle.py'
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  --mount "type=bind,source=$ROOT/deploy/tests,target=/checks,readonly" \
  --mount "type=bind,source=$ROOT/deploy/lifecycle,target=/source/deploy/lifecycle,readonly" \
  python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df \
  sh -eu -c 'sh -n /source/deploy/lifecycle/access.sh; python -B -c "import ast; from pathlib import Path; ast.parse(Path(\"/checks/access_integration.py\").read_text())"; python -B -m unittest discover -s /checks -p test_lan_access.py -v; python -B -m unittest discover -s /checks -p test_access.py -v'
