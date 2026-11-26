"""A Kiro V3 hook file, ``.kiro/hooks/<id>.json``."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Trigger = Literal[
    "SessionStart",
    "SessionEnd",
    "Stop",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "PreCompact",
    "PostCompact",
    "UserPromptSubmit",
    "PreTaskExec",
    "PostTaskExec",
    "PostFileCreate",
    "PostFileSave",
    "PostFileDelete",
    "Manual",
]


class CommandAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["command"]
    command: str = Field(min_length=1)


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["agent"]
    prompt: str = Field(min_length=1)


class HookEntry(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    name: str = Field(min_length=1)
    trigger: Trigger
    action: CommandAction | AgentAction = Field(discriminator="type")
    matcher: str | None = None
    timeout: int | None = Field(None, ge=0)
    enabled: bool = True


class HookFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["v1"]
    hooks: list[HookEntry]
