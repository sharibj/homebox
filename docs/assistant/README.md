# Inventory companion

The companion adds a separate mobile-first interface to Homebox. Homebox keeps inventory, users and collections. The Assistant API owns sessions, reviewed actions, audit state and stable NFC mappings; the private MCP adapter accesses Homebox as the current user.

- [Docker deployment and test commands](deployment.md)
- [Backup, restore and migration](backup-restore.md)
- [Accounts, collections and offline policy](access-model.md)
- [Browser/API and service contract](../../apps/assistant-api/CONTRACT.md)

Use existing native-password Homebox accounts. OIDC-only companion sign-in is not implemented. Production requires separate HTTPS hostnames and protected configuration. Model output proposes changes; user confirmation precedes inventory writes.

Offline support caches only the static shell. Real Qwen inference, CPU latency/accuracy on the target server, and target-phone camera/HEIC/NFC behavior require separate validation. A deterministic vision fixture tests the workflow, not inference.
