"""A Kiro V3 custom agent (IDE 1.x and CLI 3.x configuration reference)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeBaseResource(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)
    type: Literal["knowledgeBase"]
    source: str = Field(pattern=r"^file://")
    name: str = Field(min_length=1)
    description: str | None = None
    index_type: Literal["best", "fast"] | None = Field(None, alias="indexType")
    auto_update: bool | None = Field(None, alias="autoUpdate")


class PermissionRule(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    capability: str
    effect: Literal["allow", "ask", "deny", "evaluate"]
    match: str | list[str] | None = None
    exclude: str | list[str] | None = None


class Permissions(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    rules: list[PermissionRule]


class AgentHook(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    trigger: str
    action: dict[str, object]
    matcher: str | None = None


class AgentConfig(BaseModel):
    """Top level keys Kiro V3 reads; lint reports any other key (agent-unknown-key)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)
    name: str | None = None
    description: str | None = None
    prompt: str | None = None
    model: str | None = None
    tools: list[str] | None = None
    excluded_tools: list[str] | None = Field(None, alias="excludedTools")
    permissions: Permissions | None = None
    resources: list[str | KnowledgeBaseResource] | None = None
    mcp_servers: dict[str, dict[str, object]] | None = Field(None, alias="mcpServers")
    hooks: list[AgentHook] | None = None
    include_mcp_json: bool | None = Field(None, alias="includeMcpJson")
    include_powers: bool | None = Field(None, alias="includePowers")
    welcome_message: str | None = Field(None, alias="welcomeMessage")
    # Written by /upgrade-agent next to "permissions" (Kiro CLI 3.5).
    allowed_tools: list[str] | None = Field(None, alias="allowedTools")
    tools_settings: dict[str, object] | None = Field(None, alias="toolsSettings")
    kiro_upgrade: dict[str, object] | None = Field(None, alias="kiroUpgrade")
