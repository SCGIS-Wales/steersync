"""A team knowledge note (the ``knowledge`` collection; off by default).

Kiro does not parse this front matter: notes are retrieved through a Kiro knowledge base
(the ``knowledge`` tool), never loaded as steering. The schema exists for steersync,
reviewers and a future shared index, and is kept compact so the summary stays inside the
first embedded chunk.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeNote(BaseModel):
    """Required: title, domain, type, status, verified_on. Steering keys are forbidden."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    title: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    type: str = Field(min_length=1)
    status: str = Field(min_length=1)
    verified_on: date
    review_by: date | None = None
    aliases: list[str] = []
    tags: list[str] = []
    related: list[str] = []
    sources: list[str] = []
    relations: list[dict[str, str]] = []
