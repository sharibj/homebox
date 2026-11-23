import os
from pathlib import Path

Path("/tmp/api-session-secret").write_text("disposable-session-secret-at-least-32-characters")
Path("/tmp/api-mcp-secret").write_text("disposable-only-mcp-shared-secret-32-bytes")
os.environ.update(
    ASSISTANT_SESSION_SECRET_FILE="/tmp/api-session-secret",
    MCP_SHARED_SECRET_FILE="/tmp/api-mcp-secret",
    ASSISTANT_PUBLIC_ORIGIN="https://inventory.test",
    ASSISTANT_STATE_PATH="/tmp/import-assistant.db",
    ASSISTANT_UPLOAD_DIR="/tmp/import-uploads",
    HOMEBOX_INSTANCE_ID="56cd30b1-7bdd-466e-9459-8b402cd3349c",
)
