# Homebox inventory companion deployment

This is a separate deployment bundle. It does not change the root development Compose stack.

- [One local entrypoint for installation, source redeployment and user access](../docs/assistant/local-lifecycle.md)
- [Manual installation, Docker commands, model downloads and validation](../docs/assistant/deployment.md)
- [Backup and restore](../docs/assistant/backup-restore.md)
- [Accounts, collections, access and offline behavior](../docs/assistant/access-model.md)
- [Assistant API contract](../apps/assistant-api/CONTRACT.md)

Only the reverse proxy publishes ports. Production uses HTTPS and separate Homebox/companion hostnames. The localhost HTTP acceptance override is test-only. Do not apply it to an existing installation.
