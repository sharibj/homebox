#!/bin/sh
set -eu
DEPLOY_DIR=${HOMEBOX_DEPLOY_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}
umask 077
mkdir -p "$DEPLOY_DIR/secrets"
chmod 700 "$DEPLOY_DIR/secrets"
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$DEPLOY_DIR/secrets,target=/secrets" \
  alpine:3.24.4@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf \
  sh -eu -c '
    umask 077
    for name in homebox_api_pepper assistant_session_secret mcp_shared_secret; do
      if [ -e "/secrets/$name" ]; then
        test -s "/secrets/$name" || { echo "Existing secret is empty: $name" >&2; exit 1; }
        echo "Preserved $name"
      else
        # 32 random bytes encoded as 64 hex characters; never print the value.
        dd if=/dev/urandom bs=32 count=1 2>/dev/null | od -An -tx1 | tr -d " \n" > "/secrets/$name"
        echo "Created $name"
      fi
      # Local Compose secrets are bind mounts. The 0700 host directory protects
      # these readable files while non-root containers can read their own mounts.
      chmod 444 "/secrets/$name"
    done
  '
