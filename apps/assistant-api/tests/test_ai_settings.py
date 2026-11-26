import copy
import json
import os
import pickle
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError

from assistant.ai_settings import AISettings, AISettingsUpdate
from assistant.clients import ServiceError
from assistant.config import Config
from assistant.primary_binding import BINDING_NAME, bind_original, can_manage_ai_settings
from assistant.provider_config import ProviderRoles
from assistant.providers import ProviderSettings
from assistant.state import State


PRIMARY, OTHER, GROUP, SECOND_GROUP = (str(uuid4()) for _ in range(4))
ORIGIN = "https://inventory.test"
PREFIX = "/api/assistant/v1"


def proof(instance, user=PRIMARY):
    return {"instanceId": instance, "userId": user, "createdAt": "2025-01-01T01:00:00Z",
            "nativeProofVersion": 1, "operatorConfirmedOriginal": True}


@pytest.fixture
def store(tmp_path):
    cfg = Config.load()
    cfg.state, cfg.uploads = tmp_path / "assistant.db", tmp_path / "uploads"
    state = State(cfg.state, cfg.instance_id)
    return cfg, state, AISettings(state, cfg)


def body_from(public):
    return {"revision": public["revision"],
            **{name: {key: value for key, value in public[name].items() if key != "keyConfigured"}
               | {"keyAction": "keep"} for name in ("chat", "vision")}}


def save(settings, body):
    return settings.update(AISettingsUpdate.model_validate(body))


def test_defaults_do_not_write_or_mutate_config(store):
    cfg, state, settings = store
    defaults = vars(cfg).copy()
    assert settings.public()["revision"] == 0
    assert settings.snapshot().chat == cfg.chat_provider_settings
    assert settings.runtime_snapshot().chat_token is None
    assert settings.runtime_snapshot().vision_token is None
    assert state.one("SELECT * FROM ai_settings") is None
    assert vars(cfg) == defaults
    restored = pickle.loads(pickle.dumps(cfg))
    assert {k: v for k, v in vars(restored).items() if k != "cipher"} == {
        k: v for k, v in defaults.items() if k != "cipher"}
    assert restored.cipher.decrypt(cfg.cipher.encrypt(b"synthetic")) == b"synthetic"


def test_settings_encrypted_restart_and_independent_role_tokens(store):
    cfg, state, settings = store
    body = body_from(settings.public())
    body["chat"].update(provider="openai-compatible", baseUrl="https://provider.invalid/v1",
                        model="chat-one", keyAction="replace", apiKey="synthetic-chat-key-unique-3721")
    result = save(settings, body)
    first = settings.runtime_snapshot()
    assert result["revision"] == 1 and result["chat"]["keyConfigured"]
    assert first.chat_token and first.vision_token is None
    assert "synthetic-chat-key" not in json.dumps(result)
    assert "synthetic-chat-key" not in repr(first)
    assert "synthetic-chat-key" not in repr(first.roles)
    for path in cfg.state.parent.glob("assistant.db*"):
        if path.is_file():
            assert b"synthetic-chat-key" not in path.read_bytes()
            assert b"provider.invalid" not in path.read_bytes()
    restarted = AISettings(State(cfg.state, cfg.instance_id), cfg)
    assert restarted.public() == result
    assert restarted.snapshot().chat.api_key == "synthetic-chat-key-unique-3721"
    assert restarted.runtime_snapshot() == first
    body = body_from(result)
    body["vision"]["model"] = "vision-two"
    result = save(restarted, body)
    second = restarted.runtime_snapshot()
    assert second.chat_token == first.chat_token and second.vision_token
    body = body_from(result)
    save(restarted, body)
    assert restarted.runtime_snapshot() == second
    body = body_from(restarted.public())
    body["chat"].update(keyAction="replace", apiKey="synthetic-chat-key-rotated-991")
    save(restarted, body)
    third = restarted.runtime_snapshot()
    assert third.chat_token != second.chat_token
    assert third.vision_token == second.vision_token
    assert settings.defaults.chat.api_key is None


@pytest.mark.parametrize("change", [
    {"provider": "anthropic-messages"},
    {"baseUrl": "https://elsewhere.invalid/v1"},
    {"baseUrl": "http://provider.invalid/v1"},
    {"baseUrl": "https://provider.invalid:8443/v1"},
    {"baseUrl": "https://provider.invalid/v2"},
])
def test_key_keep_cannot_cross_protocol_or_complete_endpoint(store, change):
    _, _, settings = store
    body = body_from(settings.public())
    body["chat"].update(provider="openai-compatible", baseUrl="https://provider.invalid/v1",
                        keyAction="replace", apiKey="synthetic-bound-key")
    first = save(settings, body)
    changed = body_from(first)
    changed["chat"].update(change)
    with pytest.raises(ServiceError) as error:
        save(settings, changed)
    assert error.value.status == 422
    assert settings.public() == first
    changed["chat"]["keyAction"] = "clear"
    result = save(settings, changed)
    assert not result["chat"]["keyConfigured"]
    assert settings.snapshot().chat.api_key is None


def test_model_change_and_canonical_endpoint_keep_key_but_never_share_it(store):
    _, _, settings = store
    body = body_from(settings.public())
    body["chat"].update(provider="openai-compatible", baseUrl="https://provider.invalid/v1",
                        keyAction="replace", apiKey="synthetic-role-only-key")
    result = save(settings, body)
    first_token = settings.runtime_snapshot().chat_token
    changed = body_from(result)
    changed["chat"].update(baseUrl="https://PROVIDER.invalid:443/v1/")
    result = save(settings, changed)
    assert settings.runtime_snapshot().chat_token == first_token
    changed = body_from(result)
    changed["chat"]["model"] = "new-model"
    result = save(settings, changed)
    assert result["chat"]["keyConfigured"]
    assert settings.snapshot().chat.api_key == "synthetic-role-only-key"
    assert settings.snapshot().vision.api_key is None


def test_cleared_deployment_key_never_reappears_after_restart_or_endpoint_change(store):
    cfg, state, _ = store
    deployment = ProviderSettings("openai-compatible", "https://deployment.invalid/v1",
                                  "deployment-model", "synthetic-deployment-key")
    cfg.ai_roles = ProviderRoles(deployment, deployment)
    settings = AISettings(state, cfg)
    body = body_from(settings.public())
    body["chat"].update(baseUrl="https://new.invalid/v1", keyAction="clear")
    result = save(settings, body)
    assert not result["chat"]["keyConfigured"] and result["vision"]["keyConfigured"]
    restarted = AISettings(state, cfg)
    assert restarted.snapshot().chat.api_key is None
    assert restarted.snapshot().vision.api_key == deployment.api_key
    body = body_from(result)
    body["chat"].update(baseUrl=deployment.base_url, keyAction="clear")
    save(restarted, body)
    assert AISettings(state, cfg).snapshot().chat.api_key is None


def test_entire_save_rolls_back_if_second_role_invalid(store):
    _, _, settings = store
    body = body_from(settings.public())
    body["chat"]["model"] = "would-change"
    body["vision"]["baseUrl"] = "https://invalid.invalid?key=synthetic-never-reflected"
    with pytest.raises(ServiceError) as error:
        save(settings, body)
    assert "synthetic-never-reflected" not in str(error.value)
    assert settings.public()["revision"] == 0
    assert settings.runtime_snapshot().chat_token is None


def test_concurrent_cas_has_one_winner_and_no_partial_roles(store):
    cfg, state, settings = store
    second = AISettings(state, cfg)
    bodies = [body_from(settings.public()) for _ in range(2)]
    for index, body in enumerate(bodies):
        body["chat"]["model"], body["vision"]["model"] = f"chat-{index}", f"vision-{index}"

    def attempt(pair):
        manager, body = pair
        try:
            return save(manager, body)
        except ServiceError as error:
            return error

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, zip((settings, second), bodies)))
    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, ServiceError)]
    assert len(successes) == len(failures) == 1
    assert failures[0].code == "AI_SETTINGS_CHANGED" and failures[0].status == 409
    current = settings.public()
    assert current == successes[0]
    assert current["chat"]["model"][-1] == current["vision"]["model"][-1]


@pytest.mark.parametrize("patch", [
    {"provider": "unknown"}, {"model": ""}, {"model": " \t "}, {"model": "name\ninjected"},
    {"baseUrl": ""}, {"baseUrl": "https://user:password@provider.invalid/v1"},
    {"baseUrl": "https://provider.invalid/path?key=secret"},
    {"baseUrl": "https://provider.invalid/#secret"}, {"baseUrl": "file:///etc/passwd"},
    {"baseUrl": "https://provider.invalid:0"}, {"baseUrl": "https://provider.invalid\\evil"},
    {"baseUrl": "http://[broken"}, {"baseUrl": "x" * 2049}, {"model": "m" * 257},
    {"keyAction": "replace"}, {"keyAction": "replace", "apiKey": ""},
    {"keyAction": "replace", "apiKey": "x" * 4097},
    {"keyAction": "replace", "apiKey": "invalid key"},
    {"keyAction": "clear", "apiKey": "unused-secret"},
    {"keyAction": "keep", "apiKey": None}, {"keyAction": "inherit"}, {"model": 123},
    {"unexpected": "value"},
])
def test_invalid_settings_are_atomic(store, patch):
    _, _, settings = store
    body = body_from(settings.public())
    body["chat"].update(patch)
    with pytest.raises((ValidationError, ServiceError)):
        save(settings, body)
    assert settings.public()["revision"] == 0


def test_corrupt_cross_instance_or_wrong_cipher_fails_closed(store):
    cfg, state, settings = store
    save(settings, body_from(settings.public()))
    with state.connect() as db:
        row = db.execute("SELECT * FROM ai_settings").fetchone()
    broken_cfg = copy.copy(cfg)
    broken_cfg.cipher = Fernet(Fernet.generate_key())
    with pytest.raises(ServiceError, match="Saved AI settings cannot be read"):
        AISettings(state, broken_cfg).snapshot()
    state.execute("UPDATE ai_settings SET revision=revision+1")
    with pytest.raises(ServiceError):
        settings.snapshot()
    state.execute("UPDATE ai_settings SET revision=?,ciphertext=?", (row["revision"], row["ciphertext"]))
    state.execute("DELETE FROM metadata WHERE name='homebox_instance_id'")
    with pytest.raises(RuntimeError, match="no Homebox instance binding"):
        State(cfg.state, cfg.instance_id)


@pytest.mark.parametrize("retained", ["ai_settings", "original_account"])
def test_orphan_ai_or_primary_binding_is_never_adopted_as_empty(store, retained):
    cfg, state, settings = store
    if retained == "ai_settings":
        save(settings, body_from(settings.public()))
        before = state.one("SELECT * FROM ai_settings")
    else:
        bind_original(state, proof(cfg.instance_id))
        before = state.one("SELECT value FROM metadata WHERE name=?", (BINDING_NAME,))
    state.execute("DELETE FROM metadata WHERE name='homebox_instance_id'")
    with pytest.raises(RuntimeError, match="no Homebox instance binding"):
        State(cfg.state, str(uuid4()))
    assert state.one("SELECT value FROM metadata WHERE name='homebox_instance_id'") is None
    if retained == "ai_settings":
        assert state.one("SELECT * FROM ai_settings") == before
    else:
        assert state.one("SELECT value FROM metadata WHERE name=?", (BINDING_NAME,)) == before


def test_original_binding_is_explicit_immutable_and_fail_closed(store):
    cfg, state, _ = store
    session = {"actor": PRIMARY, "user": {"id": PRIMARY, "isSuperuser": True, "isOwner": True}}
    assert not can_manage_ai_settings(state, session)
    pending = proof(cfg.instance_id)
    for invalid in ({**pending, "operatorConfirmedOriginal": False},
                    {**pending, "createdAt": "2025-01-01"},
                    {**pending, "instanceId": str(uuid4())},
                    {**pending, "extra": "reject"}):
        with pytest.raises(ValueError):
            bind_original(state, invalid)
    assert bind_original(state, pending)
    assert not bind_original(state, pending)
    assert can_manage_ai_settings(state, session)
    assert not can_manage_ai_settings(state, {**session, "actor": OTHER})
    assert not can_manage_ai_settings(state, {"actor": OTHER, "user": {"id": OTHER, "isSuperuser": True}})
    with pytest.raises(ValueError, match="cannot be reassigned"):
        bind_original(state, proof(cfg.instance_id, OTHER))
    assert can_manage_ai_settings(state, session)
    state.execute("UPDATE metadata SET value='{}' WHERE name=?", (BINDING_NAME,))
    assert not can_manage_ai_settings(state, session)


def test_provisioning_cli_only_binds_existing_matching_instance(store):
    cfg, state, _ = store
    environment = {**os.environ, "ASSISTANT_STATE_PATH": str(cfg.state)}
    shown = subprocess.run([sys.executable, "-m", "assistant.primary_binding", "--show"],
                           text=True, capture_output=True, env=environment, timeout=10)
    assert shown.returncode == 0
    assert json.loads(shown.stdout) == {"bound": False, "instanceId": cfg.instance_id}
    result = subprocess.run([sys.executable, "-m", "assistant.primary_binding"], input=json.dumps(proof(cfg.instance_id)),
                            text=True, capture_output=True, env=environment, timeout=10)
    assert result.returncode == 0, result.stderr
    assert PRIMARY not in result.stdout
    assert can_manage_ai_settings(state, {"actor": PRIMARY, "user": {"id": PRIMARY}})
    before = state.one("SELECT value FROM metadata WHERE name=?", (BINDING_NAME,))
    shown = subprocess.run([sys.executable, "-m", "assistant.primary_binding", "--show"],
                           text=True, capture_output=True, env=environment, timeout=10)
    assert shown.returncode == 0
    assert json.loads(shown.stdout) == {"bound": True, "instanceId": cfg.instance_id,
                                      "userId": PRIMARY, "createdAt": "2025-01-01T01:00:00+00:00"}
    assert state.one("SELECT value FROM metadata WHERE name=?", (BINDING_NAME,)) == before
    result = subprocess.run([sys.executable, "-m", "assistant.primary_binding"], input=json.dumps(proof(cfg.instance_id, OTHER)),
                            text=True, capture_output=True, env=environment, timeout=10)
    assert result.returncode == 1
    missing = cfg.state.parent / "missing.db"
    environment["ASSISTANT_STATE_PATH"] = str(missing)
    result = subprocess.run([sys.executable, "-m", "assistant.primary_binding"], input=json.dumps(proof(cfg.instance_id)),
                            text=True, capture_output=True, env=environment, timeout=10)
    assert result.returncode == 1 and not missing.exists()


class NativeIdentity:
    def __init__(self):
        self.live = {PRIMARY: True, OTHER: True}
        self.identity_override = None
        self.calls = []

    async def homebox(self, method, path, session=None, **kwargs):
        self.calls.append((method, path))
        if path == "/users/login":
            return {"token": "Bearer " + (PRIMARY if kwargs["json"]["username"] == "original" else OTHER)}
        actor = session["credential"].removeprefix("Bearer ")
        if not self.live.get(actor):
            raise ServiceError("homebox_access_denied", "Native account access was denied.", 401)
        if path == "/users/self":
            return {"item": {"id": self.identity_override or actor, "name": "Synthetic",
                             "email": "synthetic@example.invalid", "groupIds": [GROUP, SECOND_GROUP],
                             "defaultGroupId": GROUP, "isSuperuser": True, "isOwner": True}}
        if path == "/groups/all":
            return [{"id": group, "name": "Synthetic"} for group in (GROUP, SECOND_GROUP)]
        raise AssertionError((method, path))


@pytest.fixture
def api(store):
    from assistant.main import create_app
    cfg, state, _ = store
    native = NativeIdentity()
    app = create_app(cfg, native)
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as client:
        yield cfg, app, native, client


def login(client, name="original"):
    response = client.post(PREFIX + "/auth/login", json={"username": name, "password": "synthetic-password"})
    assert response.status_code == 200, response.text
    client.headers.update({"X-CSRF-Token": response.json()["csrfToken"],
                           "X-Collection-ID": response.json()["collectionId"]})
    return response


def test_api_default_denied_then_primary_only_with_native_revalidation(api):
    cfg, app, native, client = api
    response = login(client)
    assert response.json()["capabilities"] == {"canManageAISettings": False}
    assert client.get(PREFIX + "/settings/ai").status_code == 403
    bind_original(app.state.store, proof(cfg.instance_id))
    assert client.get(PREFIX + "/session").json()["capabilities"]["canManageAISettings"]
    response = client.get(PREFIX + "/settings/ai")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = body_from(response.json())
    body["chat"].update(keyAction="replace", apiKey="synthetic-api-secret-771")
    native.calls.clear()
    result = client.put(PREFIX + "/settings/ai", json=body)
    assert result.status_code == 200, result.text
    assert native.calls == [("GET", "/users/self")]
    assert "synthetic-api-secret" not in result.text
    assert app.state.config.chat_provider_settings.api_key is None
    assert client.get(PREFIX + "/session").json()["ai"]["chat"]["model"] == body["chat"]["model"]
    assert client.put(PREFIX + "/settings/ai", json=body).status_code == 409
    response = login(client, "other")
    assert not response.json()["capabilities"]["canManageAISettings"]
    assert client.get(PREFIX + "/settings/ai").status_code == 403
    assert client.put(PREFIX + "/settings/ai", json=body).status_code == 403
    login(client)
    native.live[PRIMARY] = False
    assert client.get(PREFIX + "/settings/ai").status_code == 401


def test_api_preserves_csrf_origin_collection_and_identity_boundaries(api):
    cfg, app, native, client = api
    bind_original(app.state.store, proof(cfg.instance_id))
    login(client)
    current = client.get(PREFIX + "/settings/ai").json()
    body = body_from(current)
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.put(PREFIX + "/settings/ai", json=body).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert client.put(PREFIX + "/settings/ai", json=body, headers={"Origin": "https://attacker.invalid"}).status_code == 403
    assert client.get(PREFIX + "/settings/ai", headers={"X-Collection-ID": SECOND_GROUP}).status_code == 409
    native.identity_override = OTHER
    assert client.get(PREFIX + "/settings/ai").status_code == 403
    native.identity_override = None
    response = client.put(PREFIX + "/settings/ai", json={**body, "revision": True})
    assert response.status_code == 422
    body["chat"].update(keyAction="replace", apiKey="synthetic-sensitive bad key")
    response = client.put(PREFIX + "/settings/ai", json=body)
    assert response.status_code == 422 and "synthetic-sensitive" not in response.text
    assert app.state.ai_settings.public() == current
    client.cookies.clear()
    assert client.get(PREFIX + "/settings/ai").status_code == 401
