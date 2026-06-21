"""Per-subtitle readability metrics and limits."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReadabilityLimits:
    max_chars_per_line: int = 42
    max_lines: int = 2
    max_chars_per_second: float = 18.0


@dataclass(frozen=True)
class LineMetrics:
    chars_total: int  # visible chars, excluding line breaks
    max_line_chars: int
    n_lines: int
    duration_ms: int
    cps: float  # chars per second

    def char_budget(self, limits: ReadabilityLimits) -> int:
        """Max chars that fit the cps limit for this line's duration."""
        return int(limits.max_chars_per_second * self.duration_ms / 1000)


def measure(text: str, start_ms: int, end_ms: int) -> LineMetrics:
    lines = text.split("\n")
    chars_total = sum(len(line) for line in lines)
    duration_ms = max(0, end_ms - start_ms)
    seconds = duration_ms / 1000
    cps = chars_total / seconds if seconds > 0 else float("inf")
    return LineMetrics(
        chars_total=chars_total,
        max_line_chars=max((len(line) for line in lines), default=0),
        n_lines=len(lines),
        duration_ms=duration_ms,
        cps=cps,
    )


def exceeds(metrics: LineMetrics, limits: ReadabilityLimits) -> list[str]:
    """Human-readable reasons the line breaks the limits (empty if it passes)."""
    reasons = []
    if metrics.max_line_chars > limits.max_chars_per_line:
        reasons.append(f"line too long ({metrics.max_line_chars} > {limits.max_chars_per_line})")
    if metrics.n_lines > limits.max_lines:
        reasons.append(f"too many lines ({metrics.n_lines} > {limits.max_lines})")
    if metrics.cps > limits.max_chars_per_second:
        reasons.append(f"too fast ({metrics.cps:.1f} > {limits.max_chars_per_second} cps)")
    return reasons


def violations(metrics: LineMetrics, limits: ReadabilityLimits) -> set[str]:
    """The set of limit axes a line breaks, as stable keys (for comparing two versions)."""
    broken = set()
    if metrics.max_line_chars > limits.max_chars_per_line:
        broken.add("line_length")
    if metrics.n_lines > limits.max_lines:
        broken.add("line_count")
    if metrics.cps > limits.max_chars_per_second:
        broken.add("cps")
    return broken


def is_safe_improvement(
    original: LineMetrics, candidate: LineMetrics, limits: ReadabilityLimits
) -> bool:
    """Whether replacing `original` with `candidate` is worth writing.

    Accept a candidate that is fully within the limits, or one that — while still over — does
    not introduce a *new* kind of violation and is genuinely shorter. A compaction that adds a
    violation (e.g. splits one long line into three) or grows the text is rejected so `--apply`
    never makes a subtitle worse.
    """
    candidate_broken = violations(candidate, limits)
    if not candidate_broken:
        return True
    original_broken = violations(original, limits)
    return candidate_broken <= original_broken and candidate.chars_total < original.chars_total
