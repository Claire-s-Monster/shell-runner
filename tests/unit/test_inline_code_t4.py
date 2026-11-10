"""Inline code (`-c`) passed to python / shell interpreters is always T4.

Before this change the T4 rules only matched `-c` directly after the bare verb,
so `python3 -Ic <arg>`, `python3 -W ignore -c <arg>`, `python3.12 -c <arg>`,
`bash -e -c <arg>`, `bash -ec <arg>` ... fell through to T3, which can be
approved as a reusable template. Every way of reaching `-c` must be T4.

-m decision: `python3 -m foo -c x` passes `-c` to the module, not the
interpreter. It stays T3 (the module name stops the option scan); T4 is not
required because no interpreter-level inline code is involved.
"""

from __future__ import annotations

import pytest

from shell_runner.catalog import Tier
from shell_runner.classifier import classify

CWD = "/home/test/work"
ENV = {"HOME": "/home/test"}
AGENT = "some-agent"

T1, T3, T4 = Tier.AUTO_LOG, Tier.APPROVE_ONCE, Tier.ALWAYS_APPROVE


def _tier(command: str) -> Tier:
    return classify(command, CWD, AGENT, env=ENV).command_tier


PYTHON_T4 = [
    "python -c 'x'",
    "python3 -c 'x'",
    "python3.12 -c 'x'",
    "python3.9 -c 'x'",
    "/usr/bin/python3 -c 'x'",
    "python3 -Ic 'x'",
    "python3 -IEc 'x'",
    "python3 -Bc 'x'",
    "python3 -I -c 'x'",
    "python3 -I -E -s -S -B -u -c 'x'",
    "python3 -O -OO -P -q -v -b -bb -d -i -R -x -c 'x'",
    "python3 -W ignore -c 'x'",
    "python3 -X dev -c 'x'",
    "python3 -I -W error -X utf8 -c 'x'",
    "python3 -Wignore -c 'x'",
    "python3 --check-hash-based-pycs never -c 'x'",
    "python3 --isolated -c 'x'",
    "python3 --safe-path -c 'x'",
    "python3 --isolated -I -c 'x'",
    "python3.12 -I -W ignore -c 'x'",
]

SHELL_T4 = [
    f"{verb} {flags} 'x'"
    for verb in ("bash", "sh", "dash", "zsh", "ksh")
    for flags in ("-c", "-e -c", "-ec", "-lc", "-xec", "-x -c", "+x -c", "-l -c")
] + [
    "bash -o pipefail -c 'x'",
    "bash +o history -c 'x'",
    "bash -O extglob -c 'x'",
    "bash +O extglob -c 'x'",
    "bash --norc -c 'x'",
    "bash --noprofile --norc -c 'x'",
    "bash --login -c 'x'",
    "bash --posix -c 'x'",
    "bash --rcfile rc.sh -c 'x'",
    "bash --init-file rc.sh -c 'x'",
    "bash -e -o pipefail -c 'x'",
    "bash -n -c 'x'",
    "/bin/bash -ec 'x'",
]


@pytest.mark.parametrize("command", PYTHON_T4 + SHELL_T4)
def test_inline_code_is_t4(command: str) -> None:
    assert _tier(command) == T4


# (command, tier) measured on origin/development 237dcd2 and unchanged.
UNCHANGED = [
    # syntax checks stay T1
    ("bash -n f.sh", T1),
    ("sh -n f.sh", T1),
    ("dash -n f.sh", T1),
    ("python3 -I -m py_compile f.py", T1),
    ("python3 -P -m py_compile f.py", T1),
    # not inline code
    ("python3 script.py", T3),
    ("python3 script.py -c foo", T3),
    ("python3 -m pytest -q", T3),
    ("python3 -m foo -c x", T3),
    ("python3 -I -m pip install x", T3),
    ("bash script.sh", T3),
    ("bash script.sh -c x", T3),
    ("bash -e script.sh -c x", T3),
    ("pythonista -c x", T3),
    ("pythonista", T3),
    ("bashful -c x", T3),
    ("shellcheck x.sh", T3),
    ("shellcheck -c x.sh", T3),
    ("shasum f", T3),
    ("sha256sum f", T3),
    ("shred f", T3),
    ("shred -c f", T3),
]


@pytest.mark.parametrize(("command", "expected"), UNCHANGED)
def test_non_inline_code_tier_unchanged(command: str, expected: Tier) -> None:
    assert _tier(command) == expected


WRAPPERS = [
    "env python3 -c 'x'",
    "/usr/bin/env python3 -Ic 'x'",
    "env -i bash -c 'x'",
    "env FOO=1 bash -ec 'x'",
    "nice python3 -c 'x'",
    "nice -n 5 python3 -Ic 'x'",
    "nohup python3 -c 'x'",
    "timeout 5 bash -c 'x'",
    "timeout -s KILL 5 bash -c 'x'",
    "exec python3 -c 'x'",
    "command python3 -c 'x'",
    "xargs python3 -c 'x'",
    "sudo python3 -c 'x'",
]


@pytest.mark.parametrize("command", WRAPPERS)
def test_wrapped_inline_code_is_at_least_t4(command: str) -> None:
    assert _tier(command) in (T4, Tier.DENY)


WRAPPER_OPTION_FORMS_T4 = [
    "xargs -i python3 -c 'x'",
    "xargs -l python3 -c 'x'",
    "xargs -0 -r bash -c 'x'",
    "xargs -I {} bash -c 'echo {}'",
    "xargs -n 1 -P 4 sh -c 'x'",
    "env -u FOO python3 -Ic 'x'",
    "nice -n 5 nohup timeout 5 bash -ec 'x'",
    "command -p bash -c 'x'",
    "exec -a name python3 -c 'x'",
]


@pytest.mark.parametrize("command", WRAPPER_OPTION_FORMS_T4)
def test_wrapper_option_forms_are_t4(command: str) -> None:
    assert _tier(command) == T4


_CURRENT_UNMODELLED = {
    "stdbuf -oL python3 -c 'x'": T3,
    "setsid bash -c 'x'": T3,
    "time bash -c 'x'": T3,
}


def test_unmodelled_wrappers_current_tier() -> None:
    # follow-up: wrapper not modelled
    for command in (
        "stdbuf -oL python3 -c 'x'",
        "setsid bash -c 'x'",
        "time bash -c 'x'",
    ):
        assert _tier(command) == _CURRENT_UNMODELLED[command]
