# Sourced by users ai-primary after normal canonical locking and config guards.
primary_confirmation=
case "$#" in
  0) ;;
  2) [ "$1" = --confirm-original-user-id ] || { echo "Use --confirm-original-user-id UUID." >&2; exit 2; }
     primary_confirmation=$2 ;;
  *) echo "Usage: deploy/homebox users ai-primary [--confirm-original-user-id UUID]" >&2; exit 2 ;;
esac
primary_meta preflight
IFS= read -r primary_volume <"$STATE/work/primary-volume"
IFS= read -r primary_api <"$STATE/work/primary-api"
IFS= read -r primary_instance <"$STATE/work/primary-instance"
IFS= read -r primary_native_user <"$STATE/work/primary-native-user"
# This status path is read-only and refuses a missing/unupgraded API database.
docker exec "$primary_api" python -B -m assistant.primary_binding --show >"$STATE/work/primary-binding.json"
# Inspect first: Docker must not create a replacement volume for this read.
docker volume inspect "$primary_volume" >/dev/null
docker run --rm --pull never --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --user "$primary_native_user" \
  --mount "type=bind,source=$DEPLOY/lifecycle,target=/lifecycle,readonly" \
  --mount "type=volume,source=$primary_volume,target=/native,readonly" \
  "$PYTHON" python -B /lifecycle/primary.py native "$primary_instance" >"$STATE/work/primary-candidate.json"
primary_meta review
[ -n "$primary_confirmation" ] || exit 0
primary_meta attest "$primary_confirmation"
capture
primary_meta recheck
# Only the assistant's immutable binding is written, using the attested JSON.
# No native database mount, credential, token or proof is put in command arguments.
docker exec -i "$primary_api" python -B -m assistant.primary_binding <"$STATE/work/primary-proof.json"
docker exec "$primary_api" python -B -m assistant.primary_binding --show >"$STATE/work/primary-binding.json"
capture
primary_meta complete
