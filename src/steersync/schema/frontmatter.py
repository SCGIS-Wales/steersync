"""Front matter of steering files and SKILL.md, and organisational metadata.

Kiro V3 reads ``inclusion``, ``fileMatchPattern``, ``name`` and ``description`` in steering
front matter. Anything a team adds for itself goes in one ``metadata`` map of strings, whose
fields ``lint.frontmatter.metadata_fields`` describes; :func:`validate_org_metadata` checks
it. Skills follow the Agent Skills specification, which has its own ``metadata`` map.
"""

import re
from collections.abc import Mapping
from datetime import date
from typing import Annotated, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Inclusion = Literal["always", "fileMatch", "manual", "auto"]
SkillName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=64)]


class SteeringFrontMatter(BaseModel):
    """What Kiro reads from a steering file; other keys are reported by fm-unknown-key."""

    # extra="allow": unknown keys are reported by the fm-unknown-key lint rule, not here.
    model_config = ConfigDict(extra="allow", populate_by_name=True, frozen=True)
    inclusion: Inclusion = "always"
    file_match_pattern: str | list[str] | None = Field(None, alias="fileMatchPattern")
    name: str | None = None
    description: str | None = None
    metadata: dict[str, str] | None = None

    @model_validator(mode="after")
    def _mode_rules(self) -> SteeringFrontMatter:
        if self.inclusion == "fileMatch" and not self.file_match_pattern:
            raise ValueError("fileMatch requires fileMatchPattern")
        if self.inclusion == "auto" and not (self.name and self.description):
            raise ValueError("auto requires name and description")
        return self


class SkillFrontMatter(BaseModel):
    """SKILL.md front matter (Agent Skills specification)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)
    name: SkillName
    description: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    license: str | None = None
    compatibility: Annotated[str, StringConstraints(max_length=500)] | None = None
    metadata: dict[str, str] | None = None
    allowed_tools: str | None = Field(None, alias="allowed-tools")


class FieldSpec(BaseModel):
    """One organisational metadata field (lint.frontmatter.metadata_fields)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    pattern: str | None = None
    list: bool = False
    separator: str = ","
    allowed: tuple[str, ...] = ()  # an empty list allows any value
    type: Literal["string", "date"] = "string"
    warn_when_past: bool = False
    enum: tuple[str, ...] = ()


class Issue(NamedTuple):
    field: str
    severity: Literal["error", "warning"]
    message: str


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def validate_org_metadata(
    meta: Mapping[str, object], fields: Mapping[str, FieldSpec], *, today: date | None = None
) -> list[Issue]:
    """Check a ``metadata`` map against the configured fields; unknown fields are allowed."""
    issues: list[Issue] = []
    now = today or date.today()  # noqa: DTZ011 - a review date is a calendar date
    for name, value in meta.items():
        if not isinstance(value, str):
            issues.append(
                Issue(name, "warning", f"metadata.{name} is not a string; quote the value")
            )
            continue
        spec = fields.get(name)
        if spec is None:
            continue
        parts = [p.strip() for p in value.split(spec.separator)] if spec.list else [value]
        parts = [p for p in parts if p]
        for part in parts:
            if spec.pattern and not re.search(spec.pattern, part):
                issues.append(
                    Issue(
                        name, "warning", f"metadata.{name} {part!r} does not match {spec.pattern}"
                    )
                )
            if spec.allowed and part not in spec.allowed:
                issues.append(
                    Issue(name, "warning", f"metadata.{name} {part!r} is not one of {spec.allowed}")
                )
            if spec.enum and part not in spec.enum:
                issues.append(
                    Issue(name, "warning", f"metadata.{name} must be one of {', '.join(spec.enum)}")
                )
        if spec.type == "date":
            when = _date(value)
            if when is None:
                issues.append(Issue(name, "warning", f"metadata.{name} is not a YYYY-MM-DD date"))
            elif spec.warn_when_past and when < now:
                issues.append(Issue(name, "warning", f"metadata.{name} {value} has passed"))
    return issues
