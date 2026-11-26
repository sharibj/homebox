import json
import pickle

import pytest
from fastapi.testclient import TestClient

from assistant.config import Config
from assistant.main import PREFIX, create_app
from assistant.primary_binding import bind_original
from test_ai_settings import body_from, proof
from test_api import FakeClients, U1


CANONICAL = "https://inventory.test"
TAILNET = "https://100.88.42.17"
LAN = "https://192.168.50.20"


@pytest.fixture
def dual_api(monkeypatch, tmp_path):
    monkeypatch.setenv("ASSISTANT_LAN_ORIGIN", LAN)
    monkeypatch.setenv("HOMEBOX_PUBLIC_ORIGIN", TAILNET + ":8443")
    cfg = Config.load()
    cfg.state, cfg.uploads = tmp_path / "assistant.db", tmp_path / "uploads"
    fake = FakeClients()
    app = create_app(cfg, fake)
    with TestClient(app, base_url=TAILNET, headers={"Origin": CANONICAL}) as client:
        yield cfg, app, fake, client


def login(client, username="alice"):
    response = client.post(PREFIX + "/auth/login",
                           json={"username": username, "password": "synthetic-password"})
    assert response.status_code == 200, response.text
    client.headers.update({"X-CSRF-Token": response.json()["csrfToken"],
                           "X-Collection-ID": response.json()["collectionId"]})
    return response


def test_single_origin_configuration_is_unchanged_and_picklable(monkeypatch):
    monkeypatch.delenv("ASSISTANT_LAN_ORIGIN", raising=False)
    monkeypatch.setenv("HOMEBOX_PUBLIC_ORIGIN", "legacy-unused-value")
    cfg = Config.load()
    assert cfg.lan_origin == "" and cfg.homebox_public_origin == ""
    restored = pickle.loads(pickle.dumps(cfg))
    assert restored.public_companion_origins == (cfg.origin,)
    assert restored.ai_roles == cfg.ai_roles


def test_lan_alias_cannot_enable_nonsecure_session_cookies(monkeypatch, tmp_path):
    monkeypatch.setenv("ASSISTANT_PUBLIC_ORIGIN", "http://127.0.0.1")
    monkeypatch.setenv("ASSISTANT_ALLOW_INSECURE_HTTP", "true")
    monkeypatch.setenv("ASSISTANT_STATE_PATH", str(tmp_path / "assistant.db"))
    monkeypatch.setenv("ASSISTANT_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.delenv("ASSISTANT_LAN_ORIGIN", raising=False)
    assert Config.load().secure is False  # Preserve the loopback-only test exception.
    monkeypatch.setenv("ASSISTANT_LAN_ORIGIN", LAN)
    with pytest.raises(RuntimeError, match="ASSISTANT_LAN_ORIGIN requires HTTPS"):
        Config.load()
    with pytest.raises(RuntimeError, match="ASSISTANT_LAN_ORIGIN requires HTTPS"):
        create_app(clients=FakeClients())
    # Startup rejects this combination before sessions or cookies can exist.
    assert not (tmp_path / "assistant.db").exists()
    assert not (tmp_path / "uploads").exists()


@pytest.mark.parametrize("origin", [
    "https://10.0.0.1", "https://172.16.0.1", "https://172.31.255.254",
    "https://192.168.0.1", LAN, LAN + ":443",
])
def test_private_lan_configuration_is_picklable(monkeypatch, origin):
    monkeypatch.setenv("ASSISTANT_LAN_ORIGIN", origin)
    monkeypatch.setenv("HOMEBOX_PUBLIC_ORIGIN", TAILNET + ":8443")
    cfg = Config.load()
    restored = pickle.loads(pickle.dumps(cfg))
    assert restored.lan_origin == origin.removesuffix(":443")
    assert restored.public_companion_origins == (cfg.origin, restored.lan_origin, TAILNET)
    assert restored.ai_roles == cfg.ai_roles


@pytest.mark.parametrize("origin", [
    "http://192.168.50.20", "http://localhost", "https://localhost",
    "https://127.0.0.1", TAILNET, "https://169.254.1.2",
    "https://172.15.255.255", "https://172.32.0.1", "https://192.169.0.1",
    "https://8.8.8.8", "https://0.0.0.0", "https://192.0.2.1",
    "https://[fd00::1]", "https://inventory.example", "https://*.example",
    "https://user:password@192.168.50.20", "https://@192.168.50.20",
    LAN + "/", LAN + "/path", LAN + "?q=1", LAN + "#fragment",
    LAN + "?", LAN + "#", LAN + ":0", LAN + ":65536", LAN + ":",
    LAN + ":8443", LAN + ",https://10.0.0.1", " " + LAN, LAN + "\n", LAN + "\\evil",
])
def test_invalid_lan_origins_fail_closed(monkeypatch, origin):
    monkeypatch.setenv("ASSISTANT_LAN_ORIGIN", origin)
    monkeypatch.setenv("ASSISTANT_ALLOW_INSECURE_HTTP", "true")
    with pytest.raises(RuntimeError, match="ASSISTANT_LAN_ORIGIN"):
        Config.load()


@pytest.mark.parametrize("native", [
    "http://native.example:8443", "https://user@native.example:8443",
    "https://*.example:8443", "https://native.example:8443/",
    "https://native.example:8443?q=1", "https://native.example:8443#x",
    "https://native.example:65536", "https://native.example:",
    "https://native.example\n", "https://native.example\\evil",
    "https://native.example,attacker.invalid:8443", "https://native.example;evil:8443",
    "https://-native.example:8443", "https://999.999.999.999:8443",
])
def test_invalid_native_public_origin_is_rejected_when_used(monkeypatch, native):
    monkeypatch.setenv("ASSISTANT_LAN_ORIGIN", LAN)
    monkeypatch.setenv("HOMEBOX_PUBLIC_ORIGIN", native)
    with pytest.raises(RuntimeError, match="HOMEBOX_PUBLIC_ORIGIN"):
        Config.load()


@pytest.mark.parametrize("origin", [TAILNET, LAN])
def test_mapping_dtos_use_caller_origin_and_keep_canonical_storage(dual_api, origin):
    cfg, app, fake, client = dual_api
    client.base_url = origin
    client.headers["Origin"] = LAN if origin == LAN else CANONICAL
    login(client)
    entity = fake.add("Synthetic location", location=True)
    response = client.post(PREFIX + "/tags/mappings",
                           json={"targetType": "location", "entityId": entity})
    assert response.status_code == 200, response.text
    mapping = response.json()
    tag = mapping["tagId"]
    assert mapping["url"] == origin + "/t/" + tag
    row = app.state.store.one("SELECT data FROM mappings WHERE id=?", (tag,))
    assert json.loads(row["data"])["url"] == cfg.origin + "/t/" + tag
    assert LAN not in json.dumps(app.state.store.all("SELECT * FROM audit"))
    historical = json.loads(row["data"])
    historical["url"] = "https://previous.example/t/" + tag
    app.state.store.execute("UPDATE mappings SET data=? WHERE id=?", (json.dumps(historical), tag))
    before = app.state.store.one("SELECT data FROM mappings WHERE id=?", (tag,))
    for include_origin in (True, False):
        if not include_origin:
            client.headers.pop("Origin")
        listed = client.get(PREFIX + "/tags/mappings")
        assert listed.status_code == 200, listed.text
        assert listed.json()["mappings"][0]["url"] == origin + "/t/" + tag
        resolved = client.get(PREFIX + "/tags/resolve/" + tag)
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["mapping"]["url"] == origin + "/t/" + tag
        assert resolved.json()["entity"]["id"] == entity
        assert app.state.store.one("SELECT data FROM mappings WHERE id=?", (tag,)) == before
    client.headers["Origin"] = LAN if origin == LAN else CANONICAL
    changed = client.patch(PREFIX + "/tags/mappings/" + tag, json={"label": "Updated"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["url"] == origin + "/t/" + tag
    assert json.loads(app.state.store.one("SELECT data FROM mappings WHERE id=?", (tag,))["data"])["url"] == historical["url"]


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"},
    {"Host": "192.168.50.20.attacker.invalid"}, {"Host": "192.168.50.20:8443"},
    {"Origin": LAN + "/"}, {"Origin": LAN + "?"},
    {"Origin": LAN + "," + CANONICAL}, {"Origin": ""},
    {"Origin": LAN},  # A LAN Origin must not choose links on the Tailnet host.
    [("Host", "100.88.42.17"), ("Host", "attacker.invalid")],
    [("Origin", CANONICAL), ("Origin", "https://attacker.invalid")],
])
def test_unknown_or_duplicate_origin_and_host_fail_before_mapping_effects(dual_api, headers):
    _, app, fake, client = dual_api
    login(client)
    entity = fake.add("Synthetic item")
    before = fake.calls.copy()
    response = client.post(PREFIX + "/tags/mappings",
                           json={"targetType": "item", "entityId": entity}, headers=headers)
    assert response.status_code == 403, response.text
    assert app.state.store.all("SELECT * FROM mappings") == []
    assert fake.calls == before
    assert client.get(PREFIX + "/tags/mappings", headers=headers).status_code == 403
    assert client.get(PREFIX + "/tags/resolve/t_unknown", headers=headers).status_code == 403
    assert client.patch(PREFIX + "/tags/mappings/t_unknown",
                        json={"label": "No write"}, headers=headers).status_code == 403


def test_unknown_host_without_origin_and_forwarded_spoofing(dual_api):
    _, app, fake, client = dual_api
    login(client)
    client.headers.pop("Origin")
    spoofed = {"Host": "attacker.invalid", "X-Forwarded-Host": "192.168.50.20",
               "X-Forwarded-Proto": "https", "Forwarded": "host=192.168.50.20;proto=https"}
    assert client.get(PREFIX + "/tags/mappings", headers=spoofed).status_code == 403
    spoofed.pop("Host")
    response = client.get(PREFIX + "/tags/mappings", headers=spoofed)
    assert response.status_code == 200, response.text
    cfg = app.state.config
    cfg.homebox_public_origin = ""
    assert client.get(PREFIX + "/tags/mappings").status_code == 403
    # The canonical internal authority remains an exact known origin.
    assert client.get(PREFIX + "/tags/mappings", headers={"Host": "inventory.test"}).status_code == 200


@pytest.mark.parametrize("origins", [
    [], [""], [" "], [LAN + "/"], [CANONICAL + "," + LAN],
    [CANONICAL, CANONICAL], [CANONICAL, "https://attacker.invalid"],
])
def test_login_requires_exactly_one_nonempty_known_origin(dual_api, origins):
    _, app, _, client = dual_api
    client.headers.pop("Origin")
    response = client.post(PREFIX + "/auth/login",
                           json={"username": "alice", "password": "synthetic-password"},
                           headers=[("Origin", origin) for origin in origins])
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "origin_denied"
    assert app.state.store.all("SELECT * FROM sessions") == []


@pytest.mark.parametrize("origin", [TAILNET, LAN])
def test_origin_csrf_cookie_and_logout_boundaries(dual_api, origin):
    _, app, _, client = dual_api
    client.base_url = origin
    client.headers["Origin"] = LAN if origin == LAN else CANONICAL
    response = login(client)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "Domain=" not in cookie and "Path=" + PREFIX in cookie
    assert client.get(PREFIX + "/session").status_code == 200
    other = TAILNET if origin == LAN else LAN
    assert client.get(other + PREFIX + "/session").status_code == 401
    denied = client.post(PREFIX + "/auth/login", json={"username": "alice", "password": "synthetic"},
                         headers={"Origin": TAILNET})
    assert denied.status_code == 403  # Tailnet Caddy must retain its canonical rewrite.
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post(PREFIX + "/auth/logout").status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert client.post(PREFIX + "/auth/logout").status_code == 200
    assert client.get(PREFIX + "/session").status_code == 401


@pytest.mark.parametrize("origin", [TAILNET, LAN])
@pytest.mark.parametrize("username,bound,allowed", [
    ("alice", False, False), ("alice", True, True), ("bob", True, False),
])
def test_lan_access_preserves_primary_settings_permissions(dual_api, origin, username, bound, allowed):
    cfg, app, _, client = dual_api
    client.base_url = origin
    client.headers["Origin"] = LAN if origin == LAN else CANONICAL
    if bound:
        bind_original(app.state.store, proof(cfg.instance_id, U1))
    response = login(client, username)
    assert response.json()["capabilities"]["canManageAISettings"] is allowed
    current = client.get(PREFIX + "/settings/ai")
    assert current.status_code == (200 if allowed else 403)
    result = client.put(PREFIX + "/settings/ai", json=body_from(app.state.ai_settings.public()))
    assert result.status_code == (200 if allowed else 403)


def test_unset_lan_alias_does_not_enable_lan_origin(dual_api):
    cfg, app, fake, client = dual_api
    cfg.lan_origin = ""
    login(client)
    denied = client.post(PREFIX + "/auth/login", json={"username": "alice", "password": "synthetic"},
                         headers={"Origin": LAN})
    assert denied.status_code == 403
    # Legacy tags keep the configured canonical origin when dual mode is disabled.
    assert cfg.public_companion_origins == (CANONICAL,)
    entity = fake.add("Synthetic legacy item")
    mapping = client.post(PREFIX + "/tags/mappings",
                          json={"targetType": "item", "entityId": entity}).json()
    tag = mapping["tagId"]
    assert mapping["url"] == CANONICAL + "/t/" + tag
    mapping["url"] = "https://previous.example/t/" + tag
    app.state.store.execute("UPDATE mappings SET data=? WHERE id=?", (json.dumps(mapping), tag))
    assert client.get(PREFIX + "/tags/mappings").json()["mappings"][0]["url"] == mapping["url"]
    assert client.get(PREFIX + "/tags/resolve/" + tag).json()["mapping"]["url"] == mapping["url"]
    assert client.patch(PREFIX + "/tags/mappings/" + tag, json={"label": "Legacy"}).json()["url"] == mapping["url"]
