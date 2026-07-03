"""Load and save per-series memory files under <projects>/<serie>/."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from translate_subs.fsutil import atomic_write_text
from translate_subs.memory.models import (
    ConflictRecord,
    ConflictRecords,
    ConflictsFile,
    GlossaryEntries,
    GlossaryFile,
    SeriesMemory,
    StyleGuide,
)

__all__ = ["ProjectMemory", "atomic_write_text"]  # re-export for existing importers

MEMORY_FILE = "memory.json"
GLOSSARY_FILE = "glossary.json"
STYLE_GUIDE_FILE = "style_guide.json"
CONFLICTS_FILE = "conflicts.json"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {path}: {exc}") from exc


def _load_glossary(path: Path) -> dict[str, str]:
    data = _load_json(path)
    try:
        if isinstance(data, dict) and "schema_version" in data:
            return GlossaryFile.model_validate(data).entries
        return GlossaryEntries.model_validate(data).root
    except ValidationError as exc:
        raise ValueError(f"invalid {path}: expected a string-to-string glossary: {exc}") from exc


def _load_conflict_records(path: Path) -> list[ConflictRecord]:
    data = _load_json(path)
    try:
        if isinstance(data, dict):
            return ConflictsFile.model_validate(data).conflicts
        return ConflictRecords.model_validate(data).root
    except ValidationError as exc:
        raise ValueError(f"invalid {path}: expected valid conflict records: {exc}") from exc


@dataclass
class ProjectMemory:
    """All series-level memory for one project directory."""

    project_dir: Path
    memory: SeriesMemory = field(default_factory=SeriesMemory)
    glossary: dict[str, str] = field(default_factory=dict)
    style_guide: StyleGuide = field(default_factory=StyleGuide)

    @classmethod
    def load(cls, project_dir: str | Path) -> ProjectMemory:
        project_dir = Path(project_dir)
        memory = SeriesMemory()
        glossary: dict[str, str] = {}
        style_guide = StyleGuide()

        mem_path = project_dir / MEMORY_FILE
        if mem_path.exists():
            try:
                memory = SeriesMemory.model_validate_json(mem_path.read_text("utf-8"))
            except (OSError, UnicodeDecodeError, ValidationError) as exc:
                raise ValueError(f"invalid {mem_path}: {exc}") from exc
        glo_path = project_dir / GLOSSARY_FILE
        if glo_path.exists():
            glossary = _load_glossary(glo_path)
        sg_path = project_dir / STYLE_GUIDE_FILE
        if sg_path.exists():
            try:
                style_guide = StyleGuide.model_validate_json(sg_path.read_text("utf-8"))
            except (OSError, UnicodeDecodeError, ValidationError) as exc:
                raise ValueError(f"invalid {sg_path}: {exc}") from exc

        return cls(project_dir, memory, glossary, style_guide)

    def save(self) -> None:
        atomic_write_text(
            self.project_dir / MEMORY_FILE, self.memory.model_dump_json(indent=2), private=True
        )
        atomic_write_text(
            self.project_dir / GLOSSARY_FILE,
            GlossaryFile(entries=self.glossary).model_dump_json(indent=2),
            private=True,
        )
        # Persist the (possibly default) style guide so it can be edited by hand.
        atomic_write_text(
            self.project_dir / STYLE_GUIDE_FILE,
            self.style_guide.model_dump_json(indent=2),
            private=True,
        )

    def load_conflicts(self) -> list[dict[str, Any]]:
        path = self.project_dir / CONFLICTS_FILE
        if not path.exists():
            return []
        return [record.model_dump() for record in _load_conflict_records(path)]

    def write_conflicts(self, conflicts: list[dict[str, Any]]) -> None:
        try:
            records = [ConflictRecord.model_validate(conflict) for conflict in conflicts]
        except ValidationError as exc:
            raise ValueError(f"invalid conflict records: {exc}") from exc
        payload = ConflictsFile(conflicts=records).model_dump_json(indent=2)
        atomic_write_text(self.project_dir / CONFLICTS_FILE, payload, private=True)

    def append_conflicts(self, conflicts: list[dict[str, Any]]) -> None:
        if not conflicts:
            return
        existing = self.load_conflicts()
        # Dedup by full content: the same unresolved conflict recurs every episode (the
        # stored decision never moves under `flag`/`keep`), so only keep one copy.
        seen = {tuple(sorted(c.items())) for c in existing}
        for c in conflicts:
            sig = tuple(sorted(c.items()))
            if sig not in seen:
                existing.append(c)
                seen.add(sig)
        self.write_conflicts(existing)
