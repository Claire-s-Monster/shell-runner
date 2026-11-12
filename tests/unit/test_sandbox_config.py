"""sandbox_config: ProtectSystem=strict properties for detached job units (issue #82)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from shell_runner import sandbox_config
from shell_runner.sandbox_config import (
    EXTRA_RW_ENV,
    SANDBOX_ENV,
    sandbox_canary,
    sandbox_enabled,
    sandbox_properties,
)

RUNTIME = "/run/user/1234"
ENV = {"XDG_RUNTIME_DIR": RUNTIME}
DROPIN = Path(__file__).resolve().parents[2] / "scripts/shell-runner.service.d/sandbox.conf"


@pytest.mark.parametrize("environ", [{}, {SANDBOX_ENV: "1"}, {SANDBOX_ENV: ""}, {SANDBOX_ENV: "x"}])
def test_enabled_unless_zero(environ: dict[str, str]) -> None:
    assert sandbox_enabled(environ) is True


def test_disabled_with_zero() -> None:
    assert sandbox_enabled({SANDBOX_ENV: "0"}) is False
    assert sandbox_properties({SANDBOX_ENV: "0", **ENV}) == []


def test_default_properties() -> None:
    assert sandbox_properties(ENV) == [
        "ProtectSystem=strict",
        "PrivateUsers=yes",
        f"ReadWritePaths=-/tmp -/var/tmp -{RUNTIME}",
    ]


def test_runtime_dir_fallback_uses_uid() -> None:
    props = sandbox_properties({})
    assert props[2].endswith(f" -/run/user/{os.getuid()}")


def test_extra_paths_appended_in_order() -> None:
    props = sandbox_properties({**ENV, EXTRA_RW_ENV: "/data/a:/data/b"})
    assert props[2] == f"ReadWritePaths=-/tmp -/var/tmp -{RUNTIME} -/data/a -/data/b"


def test_extra_paths_deduplicated() -> None:
    props = sandbox_properties({**ENV, EXTRA_RW_ENV: f"/tmp:/data/a:/data/a:{RUNTIME}"})
    assert props[2] == f"ReadWritePaths=-/tmp -/var/tmp -{RUNTIME} -/data/a"


@pytest.mark.parametrize(
    "bad",
    ["relative/path", "/with space", "/with\ttab", "/pct%h", "/dollar$X", "/back\\slash", "/nl\nx"],
)
def test_invalid_extra_entries_dropped_with_warning(
    bad: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="shell_runner.sandbox_config"):
        props = sandbox_properties({**ENV, EXTRA_RW_ENV: f"/ok:{bad}:/ok2"})
    assert props[2] == f"ReadWritePaths=-/tmp -/var/tmp -{RUNTIME} -/ok -/ok2"
    assert EXTRA_RW_ENV in caplog.text


def test_empty_entries_ignored() -> None:
    assert sandbox_properties({**ENV, EXTRA_RW_ENV: "::"}) == sandbox_properties(ENV)


def _statvfs(flags: dict[str, int]):  # type: ignore[no-untyped-def]
    def fake(path: str) -> os.statvfs_result:
        return os.statvfs_result((0, 0, 0, 0, 0, 0, 0, 0, flags[path], 255))

    return fake


def test_canary_disabled() -> None:
    assert sandbox_canary({SANDBOX_ENV: "0"}, statvfs=_statvfs({})) == "disabled"


def test_canary_enforced() -> None:
    fake = _statvfs({"/": os.ST_RDONLY, "/usr": os.ST_RDONLY})
    assert sandbox_canary({}, statvfs=fake) == "enforced"


@pytest.mark.parametrize("flags", [{"/": 0, "/usr": 0}, {"/": os.ST_RDONLY, "/usr": 0}])
def test_canary_absent_when_not_readonly(flags: dict[str, int]) -> None:
    assert sandbox_canary({}, statvfs=_statvfs(flags)) == "absent"


def test_canary_absent_on_oserror() -> None:
    def boom(path: str) -> os.statvfs_result:
        raise OSError("nope")

    assert sandbox_canary({}, statvfs=boom) == "absent"


def test_canary_default_statvfs_runs() -> None:
    assert sandbox_canary({}) in ("enforced", "absent")


def _dropin_directives() -> dict[str, str]:
    lines = [ln.strip() for ln in DROPIN.read_text().splitlines()]
    assert "[Service]" in lines
    return dict(
        ln.split("=", 1) for ln in lines if ln and not ln.startswith(("#", "[")) and "=" in ln
    )


def test_service_dropin_matches_sandbox_properties() -> None:
    expected = [p.replace(RUNTIME, "%t") for p in sandbox_properties(ENV)]
    assert [f"{k}={v}" for k, v in _dropin_directives().items()] == expected


def test_module_has_no_other_public_surprises() -> None:
    assert sandbox_config.BASE_RW_PATHS == ("/tmp", "/var/tmp")
