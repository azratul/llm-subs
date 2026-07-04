"""Block checkpoint: hashing, resume, provider-signature scoping and the parallel path."""

from __future__ import annotations

import pysubs2
import pytest

from tests.helpers import one_line_srt
from translate_subs import config, pipeline

# --- block checkpoint / resume -------------------------------------------------------


def _job(block_id, lines, *, target="es", rules=None):
    from translate_subs.ai.job_protocol import JobLine, TranslationJobIn

    return TranslationJobIn(
        block_id=block_id,
        target=target,
        rules=rules or [],
        translate=[JobLine(id=i, text=t) for i, t in lines],
    )


def test_block_hash_is_stable_and_content_sensitive():
    from translate_subs.ai.checkpoint import block_hash

    a = _job("0001", [("0001", "hi"), ("0002", "bye")])
    same = _job("9999", [("0001", "hi"), ("0002", "bye")])  # block_id is not part of the hash
    diff_text = _job("0001", [("0001", "hi"), ("0002", "ciao")])
    diff_rules = _job("0001", [("0001", "hi"), ("0002", "bye")], rules=["formal"])
    diff_target = _job("0001", [("0001", "hi"), ("0002", "bye")], target="fr")

    assert block_hash(a) == block_hash(same)
    assert block_hash(a) != block_hash(diff_text)
    assert block_hash(a) != block_hash(diff_rules)
    assert block_hash(a) != block_hash(diff_target)


def test_block_hash_includes_surrounding_context():
    from translate_subs.ai.checkpoint import block_hash
    from translate_subs.ai.job_protocol import JobLine, TranslationJobIn

    def job(before_text: str) -> TranslationJobIn:
        return TranslationJobIn(
            block_id="0002",
            target="es",
            context_before=[JobLine(id="0001", text=before_text)],
            translate=[JobLine(id="0002", text="unchanged")],
        )

    # Same block lines, different neighbour: the context steers meaning, so the hash differs and
    # the block is re-translated rather than reusing a translation made under the old context.
    assert block_hash(job("Hello there.")) != block_hash(job("Goodbye."))


def test_block_hash_includes_prompt_version(monkeypatch):
    from translate_subs.ai import checkpoint

    job = _job("0001", [("0001", "hello")])
    original = checkpoint.block_hash(job)
    monkeypatch.setattr(checkpoint, "TRANSLATION_PROMPT_VERSION", 999)
    assert checkpoint.block_hash(job) != original


def test_checkpoint_round_trips_and_signature_mismatch_loads_empty(tmp_path):
    from translate_subs.ai.checkpoint import BlockCheckpoint, _Entry

    path = tmp_path / "cp.json"
    cp = BlockCheckpoint(path, signature="claude|", entries={})
    cp.entries["abc"] = _Entry("0001", {"0001": "Hola"}, [])
    cp.save()

    same = BlockCheckpoint.load(path, "claude|")
    assert same.entries["abc"].translations == {"0001": "Hola"}

    # A different provider/model signature must not reuse the cached blocks.
    other = BlockCheckpoint.load(path, "ollama|qwen3:4b")
    assert other.entries == {}


def test_checkpoint_corrupt_file_loads_empty(tmp_path):
    from translate_subs.ai.checkpoint import BlockCheckpoint

    path = tmp_path / "cp.json"
    path.write_text("{ not json", encoding="utf-8")
    assert BlockCheckpoint.load(path, "claude|").entries == {}


def test_checkpoint_wrong_value_types_loads_empty(tmp_path):
    from translate_subs.ai.checkpoint import CHECKPOINT_VERSION, BlockCheckpoint

    path = tmp_path / "cp.json"
    path.write_text(
        (
            '{"version":'
            f"{CHECKPOINT_VERSION}"
            ',"signature":"claude|","blocks":{"abc":{"block_id":"0001",'
            '"translations":{"0001":123},"untranslated":[]}}}'
        ),
        encoding="utf-8",
    )
    assert BlockCheckpoint.load(path, "claude|").entries == {}


class _FlakyProvider:
    """Test double: uppercases text; optionally raises on one block; records calls."""

    def __init__(self, fail_on_block=None):
        from translate_subs.ai.provider import ProviderError

        self._error = ProviderError
        self.fail_on_block = fail_on_block
        self.calls: list[str] = []
        self.untranslated_ids: list[str] = []

    def translate(self, jobs):
        self.untranslated_ids = []
        out: dict[str, str] = {}
        for job in jobs:
            self.calls.append(job.block_id)
            if job.block_id == self.fail_on_block:
                raise self._error("boom")
            for line in job.translate:
                out[line.id] = line.text.upper()
        return out


def test_translate_with_checkpoint_reuses_cached_block(tmp_path):
    from translate_subs.ai.checkpoint import (
        BlockCheckpoint,
        block_hash,
        translate_with_checkpoint,
    )

    jobs = [_job("0001", [("0001", "a")]), _job("0002", [("0002", "b")])]
    cp = BlockCheckpoint(tmp_path / "cp.json", signature="claude|")
    prov = _FlakyProvider()

    # Pre-seed block 1 as already translated; only block 2 should hit the provider.
    from translate_subs.ai.checkpoint import _Entry

    cp.entries[block_hash(jobs[0])] = _Entry("0001", {"0001": "PRE"}, [])
    events: list = []
    translations, untranslated = translate_with_checkpoint(
        prov, jobs, checkpoint=cp, on_progress=events.append
    )

    assert prov.calls == ["0002"]  # block 1 was reused, not re-translated
    assert translations == {"0001": "PRE", "0002": "B"}
    assert untranslated == []
    assert [e.reused for e in events] == [True, False]
    assert events[-1].total == 2


def test_translate_with_checkpoint_regenerates_mismatched_entry(tmp_path):
    from translate_subs.ai.checkpoint import (
        BlockCheckpoint,
        _Entry,
        block_hash,
        translate_with_checkpoint,
    )

    job = _job("0001", [("0001", "a")])
    cp = BlockCheckpoint(tmp_path / "cp.json", signature="claude|")
    cp.entries[block_hash(job)] = _Entry("0001", {"9999": "STALE"}, [])
    provider = _FlakyProvider()

    translations, _ = translate_with_checkpoint(provider, [job], checkpoint=cp)

    assert provider.calls == ["0001"]
    assert translations == {"0001": "A"}


def _multi_block_source(tmp_path, n=45):
    subs = pysubs2.SSAFile()
    for i in range(n):
        subs.events.append(pysubs2.SSAEvent(start=i * 2000, end=i * 2000 + 1500, text=f"Line {i}."))
    source = tmp_path / "ep.en.srt"
    subs.save(str(source), format_="srt")
    return source


def test_translate_resumes_after_block_failure(tmp_path, monkeypatch):
    from translate_subs.ai.provider import ProviderError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = _multi_block_source(tmp_path)  # 45 lines -> 2 blocks (40 + 5)

    # First run: the provider blows up on block 2, after block 1 was checkpointed.
    monkeypatch.setattr(
        pipeline, "make_provider", lambda *a, **k: _FlakyProvider(fail_on_block="0002")
    )
    with pytest.raises(ProviderError):
        pipeline.translate_subtitle(source, provider="claude", interactive=False, project="P")

    from translate_subs.workflows.support import episode_key

    episode = episode_key(source)
    checkpoint = tmp_path / "projects" / "P" / "es-latam" / episode / "translations.checkpoint.json"
    assert checkpoint.exists()

    # Second run: a healthy provider should only need to translate the missing block 2.
    healthy = _FlakyProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: healthy)
    result = pipeline.translate_subtitle(source, provider="claude", interactive=False, project="P")
    assert healthy.calls == ["0002"]  # block 1 reused from the checkpoint
    assert result.output_path.exists()
    assert result.output_validation.ok


def test_translate_no_resume_retranslates_all_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = _multi_block_source(tmp_path)

    first = _FlakyProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: first)
    pipeline.translate_subtitle(source, provider="claude", interactive=False, project="P")
    assert first.calls == ["0001", "0002"]

    # --no-resume ignores the checkpoint: both blocks are translated again.
    second = _FlakyProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: second)
    pipeline.translate_subtitle(
        source, provider="claude", interactive=False, project="P", force=True, resume=False
    )
    assert second.calls == ["0001", "0002"]


def test_translate_parallel_flag_is_forwarded(tmp_path, monkeypatch):
    import translate_subs.workflows.translation as wf

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = _multi_block_source(tmp_path)
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: _FlakyProvider())

    captured: dict = {}
    real = wf.translate_with_checkpoint

    def spy(*args, **kwargs):
        captured["parallel"] = kwargs.get("parallel")
        return real(*args, **kwargs)

    monkeypatch.setattr(wf, "translate_with_checkpoint", spy)

    # No flag: a non-API CLI provider defaults to 1.
    pipeline.translate_subtitle(source, provider="claude", interactive=False, project="P")
    assert captured["parallel"] == 1

    # Explicit --parallel overrides the auto-derived default.
    pipeline.translate_subtitle(
        source, provider="claude", interactive=False, project="P", force=True, parallel=3
    )
    assert captured["parallel"] == 3


# --- checkpoint signature keys on the effective model, not the --model flag (#2) ------


def test_checkpoint_signature_includes_effective_model(tmp_path, monkeypatch):
    import json
    import types

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    one_line_srt(source)

    prov = _FlakyProvider()
    prov.runner = types.SimpleNamespace(model="claude-opus-4-8")  # the runner's default model
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: prov)

    # --model omitted; the signature must still pin the model the runner actually used.
    pipeline.translate_subtitle(
        source, provider="claude", interactive=False, project="P", fmt="srt"
    )

    from translate_subs.workflows.support import episode_key

    cp = (
        tmp_path
        / "projects"
        / "P"
        / "es-latam"
        / episode_key(source)
        / "translations.checkpoint.json"
    )
    signature = json.loads(cp.read_text())["signature"]
    assert signature == "claude|claude-opus-4-8|"


def test_manifest_records_effective_model(tmp_path, monkeypatch):
    # Companion to the checkpoint test: the output manifest must also record the model the runner
    # actually used, not the (unset) --model flag, so a later default-model change is detected.
    import types

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    one_line_srt(source)

    prov = _FlakyProvider()
    prov.runner = types.SimpleNamespace(model="claude-opus-4-8")
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: prov)

    pipeline.translate_subtitle(
        source, provider="claude", interactive=False, project="P", fmt="srt"
    )

    from translate_subs.workflows.output_manifest import OutputManifest

    manifest = next((tmp_path / "projects").rglob("*.manifest.json"))
    saved = OutputManifest.model_validate_json(manifest.read_text("utf-8"))
    assert saved.model == "claude-opus-4-8"


# --- parallel translate_with_checkpoint (ollama / litellm path) ----------------------


class _ParallelProvider:
    """Test double with translate_block (thread-safe per-block method)."""

    def __init__(self):
        import threading

        self.calls: list[str] = []
        self._lock = threading.Lock()

    def translate_block(self, job):
        translations = {line.id: line.text.upper() for line in job.translate}
        with self._lock:
            self.calls.append(job.block_id)
        return translations, []

    def translate(self, jobs):
        out = {}
        for job in jobs:
            t, _ = self.translate_block(job)
            out.update(t)
        return out


def test_parallel_translate_all_blocks(tmp_path):
    from translate_subs.ai.checkpoint import BlockCheckpoint, translate_with_checkpoint

    jobs = [_job(f"000{i}", [(f"000{i}", f"line {i}")]) for i in range(1, 5)]
    cp = BlockCheckpoint(tmp_path / "cp.json", signature="ollama|")
    prov = _ParallelProvider()

    events: list = []
    translations, untranslated = translate_with_checkpoint(
        prov, jobs, checkpoint=cp, on_progress=events.append, parallel=4
    )

    assert sorted(prov.calls) == ["0001", "0002", "0003", "0004"]
    assert translations == {"0001": "LINE 1", "0002": "LINE 2", "0003": "LINE 3", "0004": "LINE 4"}
    assert untranslated == []
    assert len(events) == 4
    assert all(not e.reused for e in events)
    # All blocks were saved to the checkpoint.
    assert len(cp.entries) == 4


def test_parallel_translate_serves_cache_hits(tmp_path):
    from translate_subs.ai.checkpoint import (
        BlockCheckpoint,
        _Entry,
        block_hash,
        translate_with_checkpoint,
    )

    jobs = [_job("0001", [("0001", "a")]), _job("0002", [("0002", "b")])]
    cp = BlockCheckpoint(tmp_path / "cp.json", signature="ollama|")
    # Pre-seed block 1 as already translated.
    cp.entries[block_hash(jobs[0])] = _Entry("0001", {"0001": "PRE"}, [])
    prov = _ParallelProvider()

    events: list = []
    translations, _ = translate_with_checkpoint(
        prov, jobs, checkpoint=cp, on_progress=events.append, parallel=4
    )

    assert prov.calls == ["0002"]
    assert translations == {"0001": "PRE", "0002": "B"}
    assert [e.reused for e in events] == [True, False]


def test_parallel_translate_propagates_block_error(tmp_path):
    from translate_subs.ai.checkpoint import BlockCheckpoint, translate_with_checkpoint
    from translate_subs.ai.provider import ProviderError

    class _FailingProvider:
        def translate_block(self, job):
            raise ProviderError("backend down", retryable=False)

    jobs = [_job("0001", [("0001", "x")])]
    cp = BlockCheckpoint(tmp_path / "cp.json", signature="ollama|")

    with pytest.raises(ProviderError, match="backend down"):
        translate_with_checkpoint(_FailingProvider(), jobs, checkpoint=cp, parallel=2)


def test_parallel_failure_surfaces_without_waiting_for_inflight_blocks(tmp_path):
    """A failed block raises immediately; the pool must not drain in-flight blocks first.

    With the old `with ThreadPoolExecutor(...)` the implicit shutdown(wait=True) sat silent
    until every running block finished — up to the per-block timeout (600s) — before the user
    saw the error or the Ctrl-C took effect.
    """
    import threading
    import time as time_mod

    from translate_subs.ai.checkpoint import BlockCheckpoint, translate_with_checkpoint
    from translate_subs.ai.provider import ProviderError

    release = threading.Event()

    class _OneFailsOneHangs:
        def translate_block(self, job):
            if job.block_id == "0001":
                raise ProviderError("backend down", retryable=False)
            release.wait(timeout=5)  # simulates an in-flight HTTP call that can't be interrupted
            return {line.id: line.text for line in job.translate}, []

    jobs = [_job("0001", [("0001", "x")]), _job("0002", [("0002", "y")])]
    cp = BlockCheckpoint(tmp_path / "cp.json", signature="ollama|")

    start = time_mod.perf_counter()
    try:
        with pytest.raises(ProviderError, match="backend down"):
            translate_with_checkpoint(_OneFailsOneHangs(), jobs, checkpoint=cp, parallel=2)
        elapsed = time_mod.perf_counter() - start
    finally:
        release.set()  # let the background thread finish so pytest teardown isn't delayed
    assert elapsed < 2.0, f"error was held back {elapsed:.1f}s by in-flight blocks"


def test_parallel_provider_falls_back_to_sequential_without_translate_block(tmp_path):
    """A provider without translate_block ignores parallel > 1 and runs sequentially."""
    from translate_subs.ai.checkpoint import BlockCheckpoint, translate_with_checkpoint

    jobs = [_job("0001", [("0001", "x")]), _job("0002", [("0002", "y")])]
    cp = BlockCheckpoint(tmp_path / "cp.json", signature="s|")
    prov = _FlakyProvider()

    translations, _ = translate_with_checkpoint(prov, jobs, checkpoint=cp, parallel=8)

    assert sorted(prov.calls) == ["0001", "0002"]
    assert translations == {"0001": "X", "0002": "Y"}


def test_translate_with_checkpoint_parallel_cancels_pending_on_failure(tmp_path):
    # In the parallel path, a failing block must cancel blocks not yet started so we stop
    # spending provider calls instead of draining the whole pool.
    import threading

    from translate_subs.ai.checkpoint import BlockCheckpoint, translate_with_checkpoint
    from translate_subs.ai.provider import ProviderError

    n = 12
    jobs = [_job(f"{i:04d}", [(f"{i:04d}", f"line {i}")]) for i in range(n)]
    started: list[str] = []
    lock = threading.Lock()

    class _PoolProvider:
        # Has translate_block, so translate_with_checkpoint takes the parallel path.
        def translate_block(self, job):
            with lock:
                started.append(job.block_id)
            if job.block_id == "0000":
                raise ProviderError("boom")
            # Slow enough that, with 2 workers, most blocks are still queued (cancellable)
            # when block 0000 fails first.
            import time

            time.sleep(0.2)
            return {line.id: line.text.upper() for line in job.translate}, []

    cp = BlockCheckpoint(tmp_path / "cp.json", signature="ollama|m")
    with pytest.raises(ProviderError, match="boom"):
        translate_with_checkpoint(_PoolProvider(), jobs, checkpoint=cp, parallel=2)

    # The failing block plus at most one in-flight block may have started; the rest were cancelled.
    assert len(started) < n, f"expected pending blocks to be cancelled, all {n} started"
