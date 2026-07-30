"""Strict declarative contract for category plugins."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_PLUGIN_ID = r"^[a-z0-9][a-z0-9-]{0,62}$"
_CALLBACK_ID = r"^[a-z][a-z0-9_]{0,99}$"
_SERVICE_GROUP = r"^(?:homelab-[a-z0-9][a-z0-9-]{0,54})?$"


class AccessGroupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^homelab-[a-z0-9][a-z0-9-]{0,54}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    default_invite: bool = False
    administrator: bool = False


class CategoryManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_PLUGIN_ID)
    label: str = Field(min_length=1, max_length=100)
    placement: str = Field(default="control", pattern=_PLUGIN_ID)
    priority: int = Field(default=100, ge=0, le=10_000)
    always_on: bool = False
    description: str = Field(default="", max_length=500)
    compose_file: str = Field(default="docker-compose.yml", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    compose_profiles: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    validation_callback: str = Field(default="", alias="validate", pattern=_CALLBACK_ID)
    selected_compose_profiles: str = Field(default="", pattern=_CALLBACK_ID)
    service_group: str = Field(default="", pattern=_SERVICE_GROUP)
    access_group: AccessGroupManifest | None = None

    @field_validator("compose_profiles", "depends_on")
    @classmethod
    def unique_plugin_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("category identifiers must be unique")
        for value in values:
            if (
                not value
                or value != value.lower()
                or len(value) > 63
                or not value[0].isalnum()
                or not value.replace("-", "").isalnum()
            ):
                raise ValueError("category identifiers must be lowercase plugin IDs")
        return values

    @model_validator(mode="after")
    def access_group_belongs_to_category(self) -> CategoryManifest:
        if self.access_group is not None and self.access_group.name != self.service_group:
            raise ValueError("a declared access group must match the category service_group")
        return self
