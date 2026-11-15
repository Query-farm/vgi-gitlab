"""Shared fixtures: the GitLab replay server."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "glpat-fixture"


class Replay:
    """A running test/mock_api/server.py."""

    def __init__(self, url: str) -> None:
        self.url = url

    def requests(self) -> list[dict[str, Any]]:
        with urllib.request.urlopen(f"{self.url}/__requests") as r:
            return list(json.load(r))

    def reset(self) -> None:
        urllib.request.urlopen(urllib.request.Request(f"{self.url}/__reset", method="POST")).close()


def _start(*args: str) -> tuple[subprocess.Popen[str], str]:
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "test" / "mock_api" / "server.py"), *args], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    assert line.startswith("PORT:"), line
    return proc, f"http://127.0.0.1:{line.removeprefix('PORT:')}"


@pytest.fixture(scope="session")
def replay() -> Iterator[Replay]:
    """The replay with the fixture token required."""
    proc, url = _start("--token", TOKEN)
    yield Replay(url)
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="session")
def flaky_replay() -> Iterator[Replay]:
    """The replay answering 429 to the first request of every path."""
    proc, url = _start("--token", TOKEN, "--flaky")
    yield Replay(url)
    proc.terminate()
    proc.wait(timeout=10)
