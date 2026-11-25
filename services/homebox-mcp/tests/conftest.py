import pytest
from starlette.testclient import TestClient


@pytest.fixture(scope="session")
def mcp_client():
    from adapter.main import app

    # The MCP streamable session manager intentionally has one lifespan per
    # process. Exercise all HTTP cases through the same real application.
    with TestClient(app) as client:
        yield client
