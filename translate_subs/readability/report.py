"""Render a readability pass to Markdown (episode.readability.md)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReadabilityEntry:
    id: str
    reasons: list[str]
    current: str
    compact: str | None = None
    residual: list[str] = field(default_factory=list)
    rejected: bool = False  # compaction was produced but not worth applying (no improvement)


def _flat(text: str) -> str:
    return text.replace("\n", " / ")


def render_markdown(
    episode: str, entries: list[ReadabilityEntry], manifest: dict[str, str] | None = None
) -> str:
    out = [f"# Readability {episode}", ""]
    # Provenance: ties this report to the file/target/fingerprint it was generated from, so a
    # stale report from an earlier run is distinguishable from one matching the current subtitle.
    if manifest:
        out += [f"- {key}: {value}" for key, value in manifest.items()]
        out.append("")
    out.append(f"## Flagged ({len(entries)})")
    if entries:
        for e in entries:
            out.append(f"- Line {e.id}: {', '.join(e.reasons)}")
    else:
        out.append("_None._")
    out.append("")

    out.append("## Compactions")
    compacted = [e for e in entries if e.compact is not None]
    if compacted:
        for e in compacted:
            out.append(f"### Line {e.id}")
            out.append(f"Current: {_flat(e.current)}")
            out.append(f"Compact: {_flat(e.compact or '')}")
            if e.rejected:
                out.append("_Not applied: no readability improvement over the original._")
            if e.residual:
                out.append(f"_Still over: {', '.join(e.residual)}_")
            out.append("")
    else:
        out.append("_None._")
        out.append("")

    return "\n".join(out).rstrip() + "\n"
