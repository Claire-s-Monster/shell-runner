"""ProtectSystem=strict sandbox settings for detached job units (issue #82).

Single source of truth for the transient-unit properties; the service drop-in
scripts/shell-runner.service.d/sandbox.conf mirrors it (checked by a test).
Limitation: systemd 252 user units silently skip ProtectHome=/ReadOnlyPaths=
under /home, so /home is NOT protected.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from typing import Literal

logger = logging.getLogger(__name__)

SANDBOX_ENV = "SHELL_RUNNER_SANDBOX"
EXTRA_RW_ENV = "SHELL_RUNNER_SANDBOX_RW_PATHS"
BASE_RW_PATHS = ("/tmp", "/var/tmp")

# Absolute, and nothing systemd would split, expand or reinterpret.
_BAD_PATH_RE = re.compile(r"[\s%$\\]")

SandboxState = Literal["enforced", "absent", "disabled"]


def sandbox_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Job-unit sandboxing is on unless SHELL_RUNNER_SANDBOX=0."""
    return (environ if environ is not None else os.environ).get(SANDBOX_ENV) != "0"


def _runtime_dir(environ: Mapping[str, str]) -> str:
    return environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def _extra_paths(environ: Mapping[str, str]) -> list[str]:
    raw = environ.get(EXTRA_RW_ENV, "")
    paths: list[str] = []
    for entry in raw.split(":"):
        if not entry:
            continue
        if entry.startswith("/") and not _BAD_PATH_RE.search(entry):
            paths.append(entry)
        else:
            logger.warning("ignoring invalid entry %r in %s", entry, EXTRA_RW_ENV)
    return paths


def sandbox_properties(environ: Mapping[str, str] | None = None) -> list[str]:
    """systemd-run ``-p`` properties for a job unit; empty when disabled."""
    env = environ if environ is not None else os.environ
    if not sandbox_enabled(env):
        return []
    paths = list(dict.fromkeys([*BASE_RW_PATHS, _runtime_dir(env), *_extra_paths(env)]))
    return ["ProtectSystem=strict", "ReadWritePaths=" + " ".join("-" + p for p in paths)]


def sandbox_canary(
    environ: Mapping[str, str] | None = None,
    statvfs: Callable[[str], os.statvfs_result] = os.statvfs,
) -> SandboxState:
    """Whether this process actually sees a read-only / and /usr."""
    if not sandbox_enabled(environ):
        return "disabled"
    try:
        readonly = all(statvfs(p).f_flag & os.ST_RDONLY for p in ("/", "/usr"))
    except OSError:
        return "absent"
    return "enforced" if readonly else "absent"
