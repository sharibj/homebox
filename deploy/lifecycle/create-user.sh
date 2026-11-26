# Guided registration reuses native JSON invitations; registration stays closed.
primary_meta users-preflight
IFS= read -r create_native_volume <"$STATE/work/primary-volume"
IFS= read -r create_native_user <"$STATE/work/primary-native-user"
IFS= read -r create_native <"$STATE/work/primary-native"
docker volume inspect "$create_native_volume" >/dev/null
# Ownership names are a private read-only selection hint, not authorization.
docker run --rm --pull never --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --user "$create_native_user" \
  --mount "type=bind,source=$DEPLOY/lifecycle,target=/lifecycle,readonly" \
  --mount "type=volume,source=$create_native_volume,target=/native,readonly" \
  "$PYTHON" python -B /lifecycle/primary.py native-owners >"$STATE/work/household-owners.json"
create_tty_flags=-i
[ ! -t 0 ] || create_tty_flags=-it
docker run --rm "$create_tty_flags" --pull never --network "container:$create_native" \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$DEPLOY/lifecycle,target=/source/deploy/lifecycle,readonly" \
  --mount "type=bind,source=$STATE,target=/state" \
  "$PYTHON" python -B /source/deploy/lifecycle/accounts.py create "$@"
capture
primary_meta users-recheck
