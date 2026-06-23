"""Per-project default settings.

A project (`<DATA>/projects/<name>/settings.json`) can pin the options you would otherwise
repeat on every command for that series — provider, model, target language, source language,
output format, reasoning effort. They are *defaults*: an explicit CLI flag always wins, and a
field left unset falls back to the tool's built-in default. This is intentionally narrow (a few
per-project keys next to the existing memory files), not a global dotfile config.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from translate_subs.ai.cli_adapters import CLI_PROVIDERS
from translate_subs.fsutil import atomic_write_text
from translate_subs.naming import validate_target

SETTINGS_FILE = "settings.json"

# Built-in fallbacks when neither a flag nor a project setting provides a value.
BUILTIN_DEFAULTS = {"provider": "claude", "target": "es-latam", "lang": "en", "format": "ass"}

VALID_PROVIDERS = (*CLI_PROVIDERS, "identity", "file-handoff")
VALID_FORMATS = ("ass", "srt")
VALID_REASONING = ("minimal", "low", "medium", "high", "xhigh")


class ProjectSettings(BaseModel):
    # `model` is a real option name here; opt out of pydantic's protected `model_` namespace.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    provider: str | None = None
    model: str | None = None
    target: str | None = None
    lang: str | None = None
    format: str | None = None
    reasoning: str | None = None
    analyze_provider: str | None = None
    analyze_model: str | None = None
    analyze_reasoning: str | None = None

    @field_validator("target")
    @classmethod
    def _check_target(cls, value: str | None) -> str | None:
        # Validate the same way the workflows do, so an invalid target in settings.json is
        # rejected when the file is loaded rather than only at translate time.
        if value is not None:
            validate_target(value)
        return value

    @field_validator("provider", "analyze_provider")
    @classmethod
    def _check_provider(cls, value: str | None) -> str | None:
        if value is not None and value not in VALID_PROVIDERS:
            raise ValueError(
                f"unknown provider '{value}' (choose from {', '.join(VALID_PROVIDERS)})"
            )
        return value

    @field_validator("format")
    @classmethod
    def _check_format(cls, value: str | None) -> str | None:
        if value is not None and value not in VALID_FORMATS:
            raise ValueError(f"invalid format '{value}' (use ass or srt)")
        return value

    @field_validator("reasoning", "analyze_reasoning")
    @classmethod
    def _check_reasoning(cls, value: str | None) -> str | None:
        if value is not None and value not in VALID_REASONING:
            raise ValueError(
                f"invalid reasoning '{value}' (choose from {', '.join(VALID_REASONING)})"
            )
        return value


def load_settings(project_dir: str | Path) -> ProjectSettings:
    """Load a project's settings; a missing file yields all-unset defaults.

    A present-but-invalid file (bad value or unknown key, e.g. hand-edited) is reported as a
    `ValueError` so the CLI surfaces a friendly message instead of a raw pydantic traceback.
    """
    path = Path(project_dir) / SETTINGS_FILE
    if not path.exists():
        return ProjectSettings()
    try:
        return ProjectSettings.model_validate_json(path.read_text("utf-8"))
    except ValidationError as exc:
        raise ValueError(f"invalid {path}: {exc}") from exc


def save_settings(project_dir: str | Path, settings: ProjectSettings) -> None:
    atomic_write_text(Path(project_dir) / SETTINGS_FILE, settings.model_dump_json(indent=2))


def resolve(value: str | None, field: str, settings: ProjectSettings) -> str | None:
    """Pick `value` if given, else the project setting, else the built-in default (if any)."""
    if value is not None:
        return value
    from_settings = getattr(settings, field)
    if from_settings is not None:
        return from_settings
    return BUILTIN_DEFAULTS.get(field)
