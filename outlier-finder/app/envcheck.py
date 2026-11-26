"""Checks that the server runs in the environment the project was tested with.

Running `uvicorn` from another Python (e.g. a global conda install) silently swaps in other
package versions. One real failure: an old Brotli (1.0.x) makes the Anthropic SDK's HTTP
client crash while decompressing responses, which the SDK reports as a "connection error".
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from .config import ROOT

VENV = ROOT / ".venv"


def _brotli_problem() -> str | None:
    """httpx2 asks servers for Brotli when the module is importable, then decodes it with
    `process(data, output_buffer_limit=...)`, which Brotli < 1.2 doesn't accept."""
    try:
        import brotli
    except ImportError:
        return None  # not installed: responses come gzip-compressed instead, which always works
    d = brotli.Decompressor()
    if hasattr(d, "decompress"):
        return None  # brotlicffi, a different API that httpx2 handles
    try:
        d.process(b"", output_buffer_limit=1024)
    except TypeError:
        try:
            from importlib.metadata import version
            v = version("Brotli")
        except Exception:  # noqa: BLE001
            v = "an old version"
        return (f"Brotli {v} in this Python is too old for the Anthropic SDK and breaks Claude analyses "
                f"(\"Couldn't reach the Anthropic API\"). Run the server from the project's .venv, "
                f"or upgrade it: {sys.executable} -m pip install -U 'brotli>=1.2'")
    except Exception:  # noqa: BLE001 - empty input may raise brotli.error; the keyword was accepted
        return None
    return None


@lru_cache(maxsize=1)
def environment_problems() -> tuple[str, ...]:
    problems = []
    if VENV.exists() and Path(sys.prefix).resolve() != VENV.resolve():
        problems.append(
            f"The server is running on {sys.executable}, not the project's .venv, so its package versions "
            f"weren't tested with this app. Stop it (Ctrl+C) and start it with: "
            f".venv/bin/uvicorn app.main:app --reload")
    brotli = _brotli_problem()
    if brotli:
        problems.append(brotli)
    return tuple(problems)
