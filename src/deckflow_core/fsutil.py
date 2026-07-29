"""Filesystem primitives shared by every command.

Three rules from the design document live here rather than in each command:
paths may not escape their declared root, writes are atomic, and nothing is
overwritten without an explicit instruction.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from .diagnostics import Diagnostic
from .exits import EXIT_INPUT, EXIT_OUTPUT, CoreError


def sha256_file(path: Path, *, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_within(root: Path, candidate: Path) -> Path:
    """Resolve `candidate` and prove it stays inside `root`.

    `Path.resolve` follows symlinks, so this rejects both `..` traversal and a
    symlink that points outside the declared root.
    """
    root_real = Path(root).resolve()
    target = Path(candidate)
    if not target.is_absolute():
        target = root_real / target
    target_real = target.resolve()
    if target_real != root_real and root_real not in target_real.parents:
        raise CoreError(
            Diagnostic(
                rule_id="PATH_ESCAPES_ROOT",
                severity="error",
                message="The resolved path leaves its declared root.",
                location=str(candidate),
                expected=f"a path inside {root_real}",
                actual=str(target_real),
                recovery="Pass a path inside the declared project/input/output root.",
            ),
            exit_code=EXIT_INPUT,
        )
    return target_real


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a temp file in the same directory, then rename into place.

    Same-directory matters: `os.replace` is only atomic within one filesystem,
    and a temp file under /tmp may well be on another one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def require_empty_dir(path: Path, *, overwrite: bool) -> Path:
    """An output directory must be absent or empty; `--overwrite` clears it."""
    path = Path(path)
    if path.exists():
        if not path.is_dir():
            raise CoreError(
                Diagnostic(
                    rule_id="OUTPUT_NOT_A_DIRECTORY",
                    severity="error",
                    message="The output path exists and is not a directory.",
                    location=str(path),
                    expected="an absent or empty directory",
                    actual="an existing non-directory path",
                    recovery="Pass a directory path that does not exist yet.",
                ),
                exit_code=EXIT_OUTPUT,
            )
        if any(path.iterdir()) and not overwrite:
            raise CoreError(
                Diagnostic(
                    rule_id="OUTPUT_DIRECTORY_NOT_EMPTY",
                    severity="error",
                    message="The output directory is not empty.",
                    location=str(path),
                    expected="an absent or empty directory",
                    actual="a directory with existing entries",
                    recovery="Pass an empty directory, or --overwrite to replace its contents.",
                ),
                exit_code=EXIT_OUTPUT,
            )
    return path


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
