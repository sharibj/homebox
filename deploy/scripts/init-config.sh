#!/bin/sh
set -eu
DEPLOY_DIR=${HOMEBOX_DEPLOY_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}
umask 077
if [ ! -e "$DEPLOY_DIR/.env" ]; then
  cp "$DEPLOY_DIR/.env.example" "$DEPLOY_DIR/.env"
fi
chmod 600 "$DEPLOY_DIR/.env"
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$DEPLOY_DIR,target=/deployment" \
  python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df \
  python -B -c '
from pathlib import Path
import re
import uuid
path = Path("/deployment/.env")
body = path.read_text()
matches = list(re.finditer(r"(?m)^HOMEBOX_INSTANCE_ID=(.*)$", body))
if len(matches) > 1:
    raise SystemExit("Duplicate HOMEBOX_INSTANCE_ID entries; resolve manually.")
if matches and matches[0].group(1).strip():
    value = matches[0].group(1).strip().strip("\"" + chr(39))
    if uuid.UUID(value).int == 0:
        raise SystemExit("HOMEBOX_INSTANCE_ID must not be the nil UUID.")
    print("Preserved HOMEBOX_INSTANCE_ID.")
else:
    line = "HOMEBOX_INSTANCE_ID=" + str(uuid.uuid4())
    body = body[:matches[0].start()] + line + body[matches[0].end():] if matches else body.rstrip() + "\n" + line + "\n"
    path.write_text(body)
    print("Generated HOMEBOX_INSTANCE_ID in protected deployment configuration.")
'
