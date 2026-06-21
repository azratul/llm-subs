"""Small filesystem helpers shared across modules."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def default_file_mode() -> int:
    """The mode a normal `open(...,'w')` would produce: 0666 masked by the process umask.

    `tempfile.mkstemp` always creates files as 0600, so files we write atomically would be
    unreadable to other users (e.g. a Jellyfin/Plex process running as a different account).
    Re-deriving the umask restores the user's expected, share-friendly permissions.
    """
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def atomic_write_text(path: str | Path, text: str) -> None:
    """Write `text` to `path` atomically: a temp file in the same dir, then os.replace.

    An interrupted run can no longer leave a half-written, unparseable file; readers always
    see either the old or the new complete content.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, default_file_mode())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
