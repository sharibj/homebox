#!/bin/sh
# Import an operator-supplied key without printing it or replacing an old key.
set -eu
DEPLOY_DIR=${HOMEBOX_DEPLOY_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}
umask 077
mkdir -p "$DEPLOY_DIR/secrets/ai"
chmod 700 "$DEPLOY_DIR/secrets"
# The parent protects host access; the mounted directory must allow API UID 10001.
chmod 755 "$DEPLOY_DIR/secrets/ai"
if [ "$#" -eq 0 ]; then
  echo "Prepared provider-key directory without creating any key files."
  exit 0
fi
case "${1:-}" in
  shared) name=ai_api_key; source=${2:-} ;;
  chat) name=ai_chat_api_key; source=${2:-} ;;
  vision) name=ai_vision_api_key; source=${2:-} ;;
  /*) name=ai_api_key; source=$1 ;;
  *) echo "Usage: init-ai-secret.sh [shared|chat|vision /absolute/protected/key-file]" >&2; exit 1 ;;
esac
case "$source" in /*) ;; *) echo "Supply an absolute path to a protected raw provider-key file." >&2; exit 1 ;; esac
test -f "$source" || { echo "Provider key source must be a regular file." >&2; exit 1; }
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$source,target=/input-key,readonly" \
  --mount "type=bind,source=$DEPLOY_DIR/secrets/ai,target=/ai-secrets" \
  python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df \
  python -B -c '
import os
from pathlib import Path
import sys
name = sys.argv[1]
target = Path("/ai-secrets") / name
if target.is_symlink():
    raise SystemExit("Refusing a linked provider-key destination.")
if target.exists():
    if not target.is_file():
        raise SystemExit("Existing provider key must be a regular file.")
    print("Preserved existing " + name + ".")
else:
    source = Path("/input-key")
    if source.stat().st_size > 16384:
        raise SystemExit("Provider key source is too large.")
    try:
        key = source.read_text().strip()
    except UnicodeError:
        raise SystemExit("Provider key must be UTF-8 text.")
    if any(char in key for char in "\r\n\0"):
        raise SystemExit("Provider key must contain a single line, or be intentionally empty.")
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400), "w") as output:
        output.write(key)
    print("Imported " + name + " without displaying its value.")
os.chmod(target, 0o444)
' "$name"
