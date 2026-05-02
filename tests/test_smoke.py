"""Smoke test: verify package imports cleanly and version is correct."""

import shell_runner


def test_version() -> None:
    assert shell_runner.__version__ == "0.1.0"
