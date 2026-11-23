from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class Login(Strict):
    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class Collection(Strict):
    collectionId: UUID


class Message(Strict):
    message: str = Field(min_length=1, max_length=2000)


class Create(Strict):
    name: str = Field(min_length=1, max_length=255)
    locationId: UUID | None = None
    quantity: float = Field(default=1, gt=0, le=1_000_000)
    description: str = Field(default="", max_length=1000)
    manufacturer: str = Field(default="", max_length=255)
    model: str = Field(default="", max_length=255)
    serialNumber: str = Field(default="", max_length=255)
    notes: str = Field(default="", max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    category: str | None = Field(default=None, max_length=255)


class Move(Strict):
    entityId: UUID
    locationId: UUID


class Update(Strict):
    entityId: UUID
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    manufacturer: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=255)
    serialNumber: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=1000)
    quantity: float | None = Field(default=None, gt=0, le=1_000_000)


class Tags(Strict):
    entityId: UUID
    tags: list[str] = Field(max_length=30)


class LocationCreate(Strict):
    name: str = Field(min_length=1, max_length=255)
    parentId: UUID | None = None
    description: str = Field(default="", max_length=1000)


PAYLOADS = {"item.create": Create, "item.move": Move, "item.update": Update,
            "item.tags": Tags, "location.create": LocationCreate}


class ActionRequest(Strict):
    type: Literal["item.create", "item.move", "item.update", "item.tags", "location.create"]
    payload: dict
    captureId: str | None = Field(default=None, max_length=100)


class Confirm(Strict):
    revision: int = Field(ge=1)


class MappingCreate(Strict):
    targetType: Literal["item", "location"]
    entityId: UUID
    label: str = Field(default="", max_length=255)


class MappingUpdate(Strict):
    targetType: Literal["item", "location"] | None = None
    entityId: UUID | None = None
    status: Literal["active", "revoked"] | None = None
    label: str | None = Field(default=None, max_length=255)


class VisionItem(Strict):
    name: str = Field(min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=255)
    manufacturer: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=255)
    serialNumber: str | None = Field(default=None, max_length=255)
    visible_text: list[str] = Field(default_factory=list, max_length=30)
    attributes: list[str] = Field(default_factory=list, max_length=30)
    suggested_tags: list[str] = Field(default_factory=list, max_length=30)
    quantity_estimate: float = Field(default=1, gt=0, le=1_000_000)
    confidence: float = Field(default=0, ge=0, le=1)
    needs_user_review: bool = True


class Vision(Strict):
    items: list[VisionItem] = Field(min_length=1, max_length=10)
    image_notes: str = Field(default="", max_length=1000)


class IntentChanges(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    manufacturer: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=255)
    serialNumber: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=1000)
    quantity: float | None = Field(default=None, gt=0, le=1_000_000)


class Intent(Strict):
    kind: Literal["search", "contents", "move", "update", "tags", "location.create", "item.create", "unsupported"]
    query: str = Field(default="", max_length=500)
    destination: str | None = Field(default=None, max_length=255)
    changes: IntentChanges | None = None
    tags: list[str] = Field(default_factory=list, max_length=30)
    tagsMode: Literal["add", "remove", "replace"] = "add"
