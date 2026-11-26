"""Instance-wide, encrypted AI settings. This module never contacts providers."""

import json
import secrets
from dataclasses import dataclass, field
from typing import Literal

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .clients import ServiceError
from .provider_config import ProviderRoles, _endpoint
from .providers import ProviderSettings


class RoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider: Literal["ollama", "openai-compatible", "anthropic-messages", "gemini"]
    baseUrl: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=256)
    keyAction: Literal["keep", "replace", "clear"]
    apiKey: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)

    @model_validator(mode="after")
    def credential_action(self):
        if self.keyAction == "replace":
            if self.apiKey is None:
                raise ValueError("A replacement credential is required")
        elif "apiKey" in self.model_fields_set:
            raise ValueError("Only replacement accepts a credential")
        if not self.model.strip() or any(ord(c) < 32 or ord(c) == 127 for c in self.model):
            raise ValueError("A model identifier is required")
        return self


class AISettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    revision: int = Field(ge=0)
    chat: RoleUpdate
    vision: RoleUpdate


@dataclass(frozen=True)
class AISnapshot:
    roles: ProviderRoles = field(repr=False)
    chat_token: str | None = field(default=None, repr=False)
    vision_token: str | None = field(default=None, repr=False)

    __hash__ = None


class AISettings:
    def __init__(self, state, config):
        self.state = state
        self.cipher = config.cipher
        self.instance_id = config.instance_id
        self.defaults = ProviderRoles(config.chat_provider_settings, config.vision_provider_settings)

    @staticmethod
    def _role_data(role):
        return {"provider": role.kind, "baseUrl": role.base_url, "model": role.model,
                "apiKey": role.api_key}

    @staticmethod
    def _public(revision, snapshot):
        return {
            "revision": revision,
            **{name: {"provider": role.kind, "baseUrl": role.base_url, "model": role.model,
                      "keyConfigured": role.api_key is not None}
               for name, role in (("chat", snapshot.roles.chat), ("vision", snapshot.roles.vision))},
        }

    def _read(self, db):
        row = db.execute("SELECT revision,ciphertext FROM ai_settings WHERE id=1").fetchone()
        if row is None:
            return 0, AISnapshot(self.defaults)
        try:
            data = json.loads(self.cipher.decrypt(row["ciphertext"].encode()))
            if (data["instanceId"] != self.instance_id or data["revision"] != row["revision"]
                    or not isinstance(row["revision"], int) or row["revision"] < 1):
                raise ValueError("Invalid settings binding")
            roles = []
            for name in ("chat", "vision"):
                role = data[name]
                roles.append(ProviderSettings(kind=role["provider"], base_url=role["baseUrl"],
                                              model=role["model"], api_key=role["apiKey"]))
            tokens = [data[name] for name in ("chatToken", "visionToken")]
            if any(token is not None and (not isinstance(token, str) or not 16 <= len(token) <= 100)
                   for token in tokens):
                raise ValueError("Invalid settings token")
            return row["revision"], AISnapshot(ProviderRoles(*roles), *tokens)
        except (InvalidToken, ValueError, KeyError, TypeError, AttributeError, UnicodeError):
            raise ServiceError("AI_SETTINGS_UNAVAILABLE",
                               "Saved AI settings cannot be read. Restore the matching assistant data and session secret.",
                               503) from None

    def runtime_snapshot(self):
        with self.state.connect() as db:
            return self._read(db)[1]

    def snapshot(self):
        return self.runtime_snapshot().roles

    def public(self):
        with self.state.connect() as db:
            return self._public(*self._read(db))

    @staticmethod
    def _replace(body, current):
        try:
            role = ProviderSettings(kind=body.provider, base_url=body.baseUrl, model=body.model.strip(),
                                    api_key=body.apiKey if body.keyAction == "replace" else None)
            if body.keyAction == "keep":
                if _endpoint(role) != _endpoint(current):
                    raise ValueError("Credentials cannot cross provider endpoints")
                role = ProviderSettings(kind=role.kind, base_url=role.base_url,
                                        model=role.model, api_key=current.api_key)
            return role
        except ValueError:
            raise ServiceError("invalid_request",
                               "Check the provider, complete base URL, model, and credential action. "
                               "Changing an endpoint requires clearing or replacing its key.", 422) from None

    def update(self, body: AISettingsUpdate):
        with self.state.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            revision, current = self._read(db)
            if body.revision != revision:
                raise ServiceError("AI_SETTINGS_CHANGED",
                                   "AI settings changed in another tab. Reload them before saving.", 409)
            chat = self._replace(body.chat, current.roles.chat)
            vision = self._replace(body.vision, current.roles.vision)
            def token(role, previous, current_token):
                same = (_endpoint(role) == _endpoint(previous)
                        and role.model == previous.model and role.api_key == previous.api_key)
                return current_token if same else secrets.token_urlsafe(24)

            saved = AISnapshot(
                ProviderRoles(chat, vision),
                token(chat, current.roles.chat, current.chat_token),
                token(vision, current.roles.vision, current.vision_token),
            )
            revision += 1
            data = {"instanceId": self.instance_id, "revision": revision,
                    "chat": self._role_data(chat), "vision": self._role_data(vision),
                    "chatToken": saved.chat_token, "visionToken": saved.vision_token}
            encrypted = self.cipher.encrypt(json.dumps(data, separators=(",", ":")).encode()).decode()
            db.execute("INSERT INTO ai_settings(id,revision,ciphertext) VALUES(1,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,ciphertext=excluded.ciphertext",
                       (revision, encrypted))
            return self._public(revision, saved)
