# Backup and restore

## What must stay together

Back up Homebox's database **and attachments**, assistant SQLite state and uploads, all three secret files, deployment configuration including `HOMEBOX_INSTANCE_ID`, and Caddy's local CA/certificate state. Assistant state contains encrypted Homebox credentials, action/audit records and NFC mappings. Losing the assistant session secret makes those stored credentials unusable.

Keep the same instance UUID when restoring the **same inventory** or changing its hostname. A new unrelated inventory needs a new UUID and empty assistant state. Changing only the UUID is not a supported way to point old mappings at a different Homebox.

The backup excludes model weights. Preserve the `ollama_data` volume separately if re-downloading the exact model is not possible. Record the model ID. A full host disaster-recovery plan must also retain the matching source/images and DNS configuration.

## Offline snapshot

From the repository root:

```sh
sh deploy/scripts/backup.sh
```

The script records which writer-facing services are running, stops them, and creates one archive inside the Docker backup helper. This copies databases, WAL files, uploads, attachments, mappings, secrets and proxy state while writers are stopped. It then resumes **only** the containers that were running before the backup. If copying fails, it still attempts to resume them and returns failure.

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

Start the matching source/images with the new project name:

```sh
docker compose -p "$RESTORE_PROJECT" --env-file deploy/.env \
  -f deploy/docker-compose.yml up -d --no-build --wait --wait-timeout 180
```

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
