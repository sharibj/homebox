# Backup and restore

## What must stay together

Back up Homebox's database **and attachments**, assistant SQLite state and uploads, all three secret files, deployment configuration including `HOMEBOX_INSTANCE_ID`, and Caddy's local CA/certificate state. Assistant state contains encrypted Homebox credentials, action/audit records and NFC mappings. Losing the assistant session secret makes those stored credentials unusable.

The entire secret directory is included, including optional nested `secrets/ai/{ai_api_key,ai_chat_api_key,ai_vision_api_key}` files. New snapshots include the external-provider and LAN Compose overrides, `Caddyfile.lan`, and `deployment-options.env`, which records whether the running API used each override. Keep the selected shared/role configuration and keys together. Durable photo jobs and normalized uploads are part of assistant state; they are not inventory writes.

Keep the same instance UUID when restoring the **same inventory** or changing its hostname. A new unrelated inventory needs a new UUID and empty assistant state. Changing only the UUID is not a supported way to point old mappings at a different Homebox.

The backup excludes model weights. Preserve the `ollama_data` volume separately if re-downloading the exact model is not possible. Record the model ID. A full host disaster-recovery plan must also retain the matching source/images and DNS configuration.

## Offline snapshot

From the repository root:

```sh
sh deploy/scripts/backup.sh
```

For an existing deployment that uses both external AI and Wi-Fi HTTPS, select both active overrides:

```sh
HOMEBOX_BACKUP_EXTERNAL_AI=1 HOMEBOX_BACKUP_LAN_HTTPS=1 \
  HOMEBOX_BACKUP_STOP_TIMEOUT=-1 sh deploy/scripts/backup.sh
```

These flags select the base, external-AI, and LAN Compose files in that order. An existing `HOMEBOX_COMPOSE_OVERRIDE` still applies last. The flags do not configure a provider or enable LAN access. Without them, the existing base/custom-override behavior stays unchanged. Both flags accept only `0` or `1`.

Check aggregate queued/running capture and running-action counts before maintenance. Let active work drain; do not force-stop user work. The default stop grace remains 45 seconds. `HOMEBOX_BACKUP_STOP_TIMEOUT=-1` waits without a forced-kill deadline. Observe a long drain separately and report it rather than killing a request.

The script records which writer-facing services are running, stops them, and creates one archive inside the Docker backup helper. This copies databases, WAL files, uploads, attachments, mappings, secrets and proxy state while writers are stopped. It then resumes **only** the containers that were running before the backup. If copying fails, it still attempts to resume them and returns failure.

Its stop/resume list is proxy, assistant API, MCP, Homebox, and PWA. It never stops Ollama. Homebox must stop for a consistent database/WAL and attachment snapshot. Resume uses `--no-recreate`: container IDs and images remain the same, but restarted containers have new start times.

Archives appear in `deploy/backups/`, with mode `0600` and the invoking operator's ownership. The directory is `0700`. The archive contains plaintext secrets and private inventory. Encrypt it with your existing backup system before moving it off the protected host. A local permission bit is not encryption. Do not upload it, commit it, or include it in a source ZIP.

Check the command result and the resumed services:

```sh
docker compose --env-file deploy/.env -f deploy/docker-compose.yml ps
```

Monitor free space before each snapshot. A compressed archive still needs additional disk space. Do not delete the source volumes after merely creating a backup.

## Restore to new, empty destinations

Restore on a separate host or into a **new** Compose project while the source installation is stopped. Do not start a fresh API against existing assistant state with a different instance UUID. The helper refuses non-empty destinations, absolute/traversal paths, links, special files, duplicate paths and missing instance configuration.

The following example uses new volume names and a new protected directory. Replace paths and the archive name deliberately:

```sh
umask 077
mkdir -p /absolute/restore/secrets /absolute/restore/config
chmod 700 /absolute/restore /absolute/restore/secrets /absolute/restore/config
export RESTORE_PROJECT=homebox-restored-unique
for volume in homebox_data assistant_data caddy_data caddy_config; do
  docker volume create \
    --label "com.docker.compose.project=$RESTORE_PROJECT" \
    --label "com.docker.compose.volume=$volume" \
    "${RESTORE_PROJECT}_$volume"
done

docker run --rm --network none --read-only \
  -e "RESTORE_UID=$(id -u)" -e "RESTORE_GID=$(id -g)" \
  --mount "type=bind,source=$PWD/deploy/scripts,target=/ops,readonly" \
  --mount type=bind,source=/absolute/encrypted-backup-restored-locally,target=/backups,readonly \
  --mount type=bind,source=/absolute/restore/secrets,target=/restore/secrets \
  --mount type=bind,source=/absolute/restore/config,target=/restore/config \
  --mount "type=volume,source=${RESTORE_PROJECT}_homebox_data,target=/restore/homebox" \
  --mount "type=volume,source=${RESTORE_PROJECT}_assistant_data,target=/restore/assistant" \
  --mount "type=volume,source=${RESTORE_PROJECT}_caddy_data,target=/restore/proxy-data" \
  --mount "type=volume,source=${RESTORE_PROJECT}_caddy_config,target=/restore/proxy-config" \
  python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df \
  python -B /ops/restore.py /backups/homebox-SNAPSHOT.tar.gz
```

The restore keeps numeric owners inside data volumes (`65532` for Homebox and `10001` for the assistant). It assigns restored host configuration/secrets to the invoking operator. Applications remain stopped.

Compare the restored `config/config.env`, `config/docker-compose.yml`, and `config/Caddyfile` to the matching source version. Copy the restored environment and secrets into a fresh deployment directory using protected permissions, or use an explicit override with their restored paths. Retain the original instance UUID, encryption secret, API-key pepper, shared MCP secret, and Caddy state. Adjust only deployment-specific hostnames/bind addresses after reviewing DNS and certificate consequences.

Review `config/deployment-options.env` when present. `EXTERNAL_AI=1` means the restored startup command also needs `-f deploy/docker-compose.external-ai.yml`. Restore that file alongside the base Compose file and preserve `secrets/ai/` with its selected shared/chat/vision key files and original permissions. Do not silently recreate an externally configured API with only the base file: that drops its key-directory mount and outbound network. Older snapshots without these optional files still restore; use their original deployment instructions.

`LAN_HTTPS=1` also requires `-f deploy/docker-compose.lan.yml`, plus the restored `Caddyfile.lan` beside the base Caddyfile in `deploy/reverse-proxy/`. Check that the restored `PROXY_LAN_IP` is an actual RFC1918 address on the new host before starting. Keep the loopback desktop listeners, existing CA state, and exact-Origin policy. Do not substitute `0.0.0.0` or start without the LAN override: it also sets the native public URL and no-SNI IP certificate selection. LAN activation and external-AI activation are independent.

If Docker's default address pools were exhausted and you manually created the provider-egress network with an explicit subnet, retain its nonsecret creation command and subnet in the local operations record. The snapshot does not replay manually selected Docker IPAM settings. On the restore target, first check for subnet conflicts; precreate the correctly named and Compose-labeled network only when needed. Do not prune unrelated networks or change global Docker address pools as a restore shortcut.

Start the matching source/images with the new project name:

```sh
docker compose -p "$RESTORE_PROJECT" --env-file deploy/.env \
  -f deploy/docker-compose.yml up -d --no-build --wait --wait-timeout 180
```

The command above is for native Ollama without LAN access. For external AI or LAN access, insert each selected override `-f` file **before** `up`. The snapshot script resumes its existing stopped containers with `--no-recreate`, preserving their active mounts/networks; this does not replace the need to select the right overrides after a restore.

Before opening access, verify account sign-in, collection isolation, an item and its attachment bytes, assistant action/mapping state, tag resolution, and model-offline manual/search behavior. Run `PRAGMA integrity_check` for both SQLite files in a stopped or explicitly safe maintenance workflow. Do not infer a working restore from archive listing alone.

For a hostname migration, preserve the instance UUID. Keep the old NFC hostname working or redirect it; a stable tag identifier cannot redirect a hostname that no longer resolves.

## Migrating an existing Homebox-only installation

1. Stop the old Homebox writer and take a complete backup of its data and API-key pepper.
2. Keep the original data untouched for rollback. Copy its full data directory into a **new** named volume, including database/WAL files and attachments.
3. Give only that copied volume to the new rootless Homebox. If the old container used root, adjust ownership inside the **copied volume only** to `65532:65532`. Do not recursively change unrelated host directories or the original installation.
4. Use the matching fork image and verify migrations against the copy. Do not run old and new Homebox writers on one SQLite volume.
5. Start a new companion with a generated instance UUID and empty assistant state. Preserve an existing companion's UUID/state/secrets instead if this is a companion migration.
6. Verify users, memberships, item data and attachment content before changing DNS or retiring the old deployment.

The bundle does not automatically import or mutate any existing installation.
