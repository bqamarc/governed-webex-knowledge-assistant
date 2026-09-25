from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ManifestError(ValueError):
    """Raised when a knowledge manifest is not safe to load."""


class SourceDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1, max_length=200)
    uri: str = Field(min_length=1, max_length=500)
    source_class: Literal["public", "synthetic", "restricted", "audit_only", "candidate"]
    audience: Literal["public", "restricted"]
    reviewed: bool
    effective_at: datetime | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_effective_window(self) -> SourceDefinition:
        for name, value in (
            ("effective_at", self.effective_at),
            ("expires_at", self.expires_at),
        ):
            if value is not None and value.utcoffset() is None:
                raise ValueError(f"{name} must include a timezone")
        if (
            self.effective_at is not None
            and self.expires_at is not None
            and self.expires_at <= self.effective_at
        ):
            raise ValueError("expires_at must be later than effective_at")
        return self


class KnowledgeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*$")
    question: str = Field(min_length=3, max_length=1000)
    keywords: list[str] = Field(min_length=1, max_length=40)
    answer: str = Field(min_length=3, max_length=5000)
    answerable: bool
    citation_mode: Literal["url", "title_only", "none"]
    source_url: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_citation(self) -> KnowledgeRecord:
        if self.citation_mode == "url":
            source_url = self.source_url or ""
            parsed = urlsplit(source_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or any(
                    character.isspace()
                    or ord(character) < 32
                    or ord(character) == 127
                    or character in "()[]<>\\`\"'"
                    for character in source_url
                )
            ):
                raise ValueError("URL citations require an HTTPS source_url")
        elif self.source_url is not None:
            raise ValueError("source_url is allowed only for URL citations")
        return self


class KnowledgeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    source: SourceDefinition
    records: list[KnowledgeRecord] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def validate_answerability(self) -> KnowledgeManifest:
        allowed_classes = {"public", "synthetic"}
        if any(record.answerable for record in self.records):
            if self.source.source_class not in allowed_classes:
                raise ValueError("Only public or synthetic sources may contain answerable records")
            if self.source.audience != "public" or not self.source.reviewed:
                raise ValueError("Answerable records require a reviewed public source")
            if any(record.answerable and record.citation_mode == "none" for record in self.records):
                raise ValueError("Answerable records require a title or URL citation")
        record_ids = [record.id for record in self.records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("Record ids must be unique within a manifest")
        return self


def load_manifest(path: Path) -> KnowledgeManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return KnowledgeManifest.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ManifestError(f"Invalid knowledge manifest {path}: {exc}") from exc
