"""Issue #98 item 1: code-loading environment variables make a script command non-durable.

Non-durable: still pinned (approve_once binds to the script content) but never promotable
to a reusable rule. systemd-run --setenv/-E forms stay UNPINNABLE.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shell_runner import script_pin
from shell_runner.classifier import classify

AGENT = "focused-code-modifier"  # AUTO_CAPPED cap

DENYLIST = [
    "BASH_ENV",
    "ENV",
    "PYTHONSTARTUP",
    "PYTHONPATH",
    "PYTHONHOME",
    "NODE_OPTIONS",
    "RUBYOPT",
    "PERL5OPT",
    "PERL5LIB",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
]

BASES = ["bash ./x.sh", "python ./x.py"]


@pytest.fixture(autouse=True)
def _scripts(tmp_path: Path) -> None:
    (tmp_path / "x.sh").write_text("echo hi\n")
    (tmp_path / "x.py").write_text("print('hi')\n")


def _forms(assignment: str, base: str) -> list[str]:
    """Every supported way of putting `assignment` in front of `base`."""
    return [
        f"{assignment} {base}",
        f"env {assignment} {base}",
        f"env -u FOO {assignment} {base}",
        f"export {assignment}; {base}",
        f"export {assignment} && {base}",
        f"declare -x {assignment}; {base}",
    ]


def _systemd_forms(assignment: str, base: str) -> list[str]:
    return [
        f"systemd-run --setenv={assignment} {base}",
        f"systemd-run --setenv {assignment} {base}",
        f"systemd-run -E {assignment} {base}",
    ]


def _assert_unpinnable(command: str, tmp_path: Path, name: str) -> None:
    cls = classify(command, str(tmp_path), AGENT)
    assert cls.script_unpinnable is True, command
    assert cls.non_promotable is True, command
    assert name in " ".join(cls.decision_path), command
    pin = script_pin.compute_pin(command, str(tmp_path))
    assert pin is not None and pin.sha256 == script_pin.UNPINNABLE, command


def _assert_non_durable(command: str, tmp_path: Path, name: str) -> None:
    cls = classify(command, str(tmp_path), AGENT)
    assert cls.non_promotable is True, command
    assert cls.script_unpinnable is False, command
    assert cls.non_durable_reason is not None and name in cls.non_durable_reason, command
    notes = [n for n in cls.decision_path if n.startswith("non_durable:")]
    assert notes and name in " ".join(notes), command
    pin = script_pin.compute_pin(command, str(tmp_path))
    assert pin is not None and pin.sha256 != script_pin.UNPINNABLE, command
    plan = script_pin.analyze(command, str(tmp_path))
    assert plan.unpinnable is False, command
    assert plan.non_durable and name in " ".join(plan.non_durable), command


def _assert_pinnable(command: str, tmp_path: Path) -> None:
    cls = classify(command, str(tmp_path), AGENT)
    assert cls.script_unpinnable is False, command
    assert cls.non_durable_reason is None, command
    pin = script_pin.compute_pin(command, str(tmp_path))
    assert pin is not None and pin.sha256 != script_pin.UNPINNABLE, command
    assert script_pin.analyze(command, str(tmp_path)).non_durable == [], command


# --- A. denylisted loader variables ---------------------------------------------


@pytest.mark.parametrize("base", BASES)
@pytest.mark.parametrize("var", DENYLIST)
def test_denylisted_var_makes_command_non_durable(tmp_path: Path, var: str, base: str) -> None:
    for command in _forms(f"{var}=/tmp/a", base):
        _assert_non_durable(command, tmp_path, var)


@pytest.mark.parametrize("base", BASES)
@pytest.mark.parametrize("var", DENYLIST)
def test_denylisted_var_via_systemd_run_is_unpinnable(tmp_path: Path, var: str, base: str) -> None:
    for command in _systemd_forms(f"{var}=/tmp/a", base):
        _assert_unpinnable(command, tmp_path, var)


# --- B. PATH ----------------------------------------------------------------------

BAD_PATHS = [
    ".",
    "/opt/x:.",
    ":/opt/x",
    "/opt/x:",
    "/opt/x::/opt/y",
    "bin",
    "./bin",
    "/opt/x:bin",
    "/opt/x:./bin",
    "$HOME/bin",
    "/opt/x:$HOME/bin",
    "$PATHX",
    "${HOME}/bin:$PATH",
]

GOOD_PATHS = [
    "/opt/x/bin:$PATH",
    "/usr/bin:/bin",
    "${PATH}:/opt/y/bin",
]


@pytest.mark.parametrize("value", BAD_PATHS)
def test_unsafe_path_is_non_durable(tmp_path: Path, value: str) -> None:
    for command in _forms(f"PATH={value}", "bash ./x.sh"):
        _assert_non_durable(command, tmp_path, "PATH")


@pytest.mark.parametrize("value", GOOD_PATHS)
def test_safe_path_stays_pinnable(tmp_path: Path, value: str) -> None:
    for command in _forms(f"PATH={value}", "bash ./x.sh"):
        _assert_pinnable(command, tmp_path)


# --- C. regression guards ---------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "FOO=1 bash ./x.sh",
        "LANG=C python ./x.py",
        "env FOO=1 bash ./x.sh",
        "export FOO=1; bash ./x.sh",
        "MYPYTHONPATH=/x bash ./x.sh",
        "PYTHONPATH_EXTRA=1 bash ./x.sh",
        "env MYPYTHONPATH=/x bash ./x.sh",
        "export PYTHONPATH_EXTRA=1; bash ./x.sh",
    ],
)
def test_harmless_assignments_stay_pinnable(tmp_path: Path, command: str) -> None:
    _assert_pinnable(command, tmp_path)


# --- E. bypass forms found in review ----------------------------------------------


@pytest.mark.parametrize(
    ("command", "name"),
    [
        ("PYTHONPATH+=/x bash ./x.sh", "PYTHONPATH"),
        ("PATH+=:. bash ./x.sh", "PATH"),
        ("PYTHONPATH=/x; export PYTHONPATH; bash ./x.sh", "PYTHONPATH"),
        ("readonly PYTHONPATH=/x; export PYTHONPATH; bash ./x.sh", "PYTHONPATH"),
        ("declare PYTHONPATH=/x; export PYTHONPATH; bash ./x.sh", "PYTHONPATH"),
        ("export PYTHONPATH; bash ./x.sh", "PYTHONPATH"),
        ("local -x PYTHONPATH=/x; bash ./x.sh", "PYTHONPATH"),
        ("declare -rx PYTHONPATH=/x; bash ./x.sh", "PYTHONPATH"),
        ("builtin export PYTHONPATH=/x; bash ./x.sh", "PYTHONPATH"),
        ("command export PYTHONPATH=/x; bash ./x.sh", "PYTHONPATH"),
    ],
)
def test_bypass_forms_are_non_durable(tmp_path: Path, command: str, name: str) -> None:
    _assert_non_durable(command, tmp_path, name)


@pytest.mark.parametrize(
    ("command", "name"),
    [
        ("make PYTHONPATH=/x deploy_x", "PYTHONPATH"),
        ("make -C . LD_PRELOAD=/x.so all", "LD_PRELOAD"),
    ],
)
def test_make_operand_assignment_is_non_durable(tmp_path: Path, command: str, name: str) -> None:
    (tmp_path / "Makefile").write_text("deploy_x all:\n\t@echo hi\n")
    cls = classify(command, str(tmp_path), AGENT)
    assert cls.non_promotable is True, command
    assert cls.non_durable_reason is not None and name in cls.non_durable_reason, command


@pytest.mark.parametrize(
    ("command", "name"),
    [
        ("PYTHONPATH=. pytest -q", "PYTHONPATH"),
        ("NODE_OPTIONS=--require=./h.js node --version", "NODE_OPTIONS"),
    ],
)
def test_non_script_command_with_loader_var_is_non_durable(
    tmp_path: Path, command: str, name: str
) -> None:
    cls = classify(command, str(tmp_path), AGENT)
    assert cls.non_promotable is True, command
    assert cls.non_durable_reason is not None and name in cls.non_durable_reason, command


@pytest.mark.parametrize(
    "command",
    [
        "export FOO; bash ./x.sh",
        "readonly FOO=1; bash ./x.sh",
        "local -x FOO=1; bash ./x.sh",
        "builtin export FOO=1; bash ./x.sh",
    ],
)
def test_bypass_guards_stay_pinnable(tmp_path: Path, command: str) -> None:
    _assert_pinnable(command, tmp_path)


def test_make_harmless_operand_stays_durable(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("deploy_x:\n\t@echo hi\n")
    cls = classify("make FOO=1 deploy_x", str(tmp_path), AGENT)
    assert cls.non_durable_reason is None


# --- D. separate segment that cannot affect the script ----------------------------


@pytest.mark.parametrize(
    "command",
    [
        "bash ./x.sh; echo PYTHONPATH=x",
        "bash ./x.sh && echo LD_PRELOAD=/tmp/a",
    ],
)
def test_denylisted_text_in_other_segment_stays_pinnable(tmp_path: Path, command: str) -> None:
    _assert_pinnable(command, tmp_path)
