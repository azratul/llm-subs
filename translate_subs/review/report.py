"""Render a ReviewReport to Markdown (episode.review.md)."""

from __future__ import annotations

from translate_subs.review.models import Finding, ReviewReport


def _label(f: Finding) -> str:
    if f.scope == "global":
        return f"Global ({f.kind})"
    return f"Line {f.id} ({f.kind})"


def render_markdown(report: ReviewReport, manifest: dict[str, str] | None = None) -> str:
    out = [f"# Review {report.episode}", ""]

    # Provenance: which files/target/fingerprint this report was generated from, so a report left
    # behind from an earlier run can be told apart from one matching the current subtitle.
    if manifest:
        out += [f"- {key}: {value}" for key, value in manifest.items()]
        out.append("")

    out.append("## Warnings")
    warnings = report.warnings()
    if warnings:
        out += [f"- {_label(f)}: {f.message}" for f in warnings]
    else:
        out.append("_None._")
    out.append("")

    out.append("## Suggested fixes")
    fixes = report.fixes()
    if fixes:
        for f in fixes:
            tag = " [auto]" if f.auto else ""
            out.append(f"### {_label(f)}{tag}")
            if f.message:
                out.append(f"_{f.message}_")
            out.append(f"Current:   {f.current or ''}")
            out.append(f"Suggested: {f.suggested}")
            out.append("")
    else:
        out.append("_None._")
        out.append("")

    return "\n".join(out).rstrip() + "\n"
