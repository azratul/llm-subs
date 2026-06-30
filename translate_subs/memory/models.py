"""Per-series memory models."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, RootModel

Gender = Literal["male", "female", "unknown"]
MEMORY_SCHEMA_VERSION: Literal[1] = 1


def normalize_gender(value: str) -> Gender:
    """Coerce a free-text gender (e.g. from an LLM reply) to the allowed set; anything
    unexpected folds to 'unknown' so it never crashes the strict CharacterMemory model."""
    return cast(Gender, value) if value in ("male", "female") else "unknown"


class CharacterMemory(BaseModel):
    # Our own files: reject unknown keys and validate on assignment so a bad gender (e.g. a
    # typo in a hand-edited memory.json) fails loudly instead of silently entering memory.
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str
    gender: Gender = "unknown"
    speech_style: str | None = None
    relationships: dict[str, str] = Field(default_factory=dict)


class SeriesMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = MEMORY_SCHEMA_VERSION
    characters: list[CharacterMemory] = Field(default_factory=list)

    def find(self, name: str) -> CharacterMemory | None:
        key = name.casefold()
        for char in self.characters:
            if char.name.casefold() == key:
                return char
        return None


class StyleGuide(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = MEMORY_SCHEMA_VERSION
    locale: str = ""  # optional variant override; the --target flag drives the language
    honorifics: str = "keep"
    pronoun_policy: str = "contextual"
    formality_policy: str = "natural"
    tone: str = "natural"
    names: str = "keep_original"


class GlossaryEntries(RootModel[dict[str, str]]):
    model_config = ConfigDict(strict=True)


class GlossaryFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = MEMORY_SCHEMA_VERSION
    entries: dict[str, str] = Field(default_factory=dict)


class ConflictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["glossary", "gender"]
    key: str
    existing: str
    suggested: str


class ConflictRecords(RootModel[list[ConflictRecord]]):
    model_config = ConfigDict(strict=True)


class ConflictsFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = MEMORY_SCHEMA_VERSION
    conflicts: list[ConflictRecord] = Field(default_factory=list)
