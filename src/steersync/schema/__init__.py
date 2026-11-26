"""Pydantic models of the Kiro files steersync syncs, exported as JSON Schema.

``steersync schema export`` writes them to ``schemas/``; a golden test fails when the models
and the committed schemas drift. Lint rules give the readable messages; the models are the
machine readable contract for editors and other tools.
"""

from collections.abc import Mapping

from pydantic import BaseModel

from steersync.schema.agent import AgentConfig
from steersync.schema.frontmatter import SkillFrontMatter, SteeringFrontMatter
from steersync.schema.hook import HookFile
from steersync.schema.knowledge import KnowledgeNote

KINDS: Mapping[str, type[BaseModel]] = {
    "steering": SteeringFrontMatter,
    "skill": SkillFrontMatter,
    "agent": AgentConfig,
    "hook": HookFile,
    "knowledge": KnowledgeNote,
}


def json_schema(kind: str) -> dict[str, object]:
    """The JSON Schema of ``kind`` with its Kiro field names (aliases)."""
    schema = KINDS[kind].model_json_schema(by_alias=True)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
