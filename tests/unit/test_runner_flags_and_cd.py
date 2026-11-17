"""Issue #98 smaller gaps: runner flag allowlists and logical-vs-physical paths.

Section 1: an UNKNOWN flag before the script/command name of uv/npm/yarn/pnpm makes the plan
UNPINNABLE (fail closed, like pixi); KNOWN flags keep the current outcome.
Section 2: `cd` is logical (`cd link/..` -> the logical parent) but a script path handed to an
interpreter is opened physically (`link/../x.sh` follows `link` first).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shell_runner import script_pin
from shell_runner.write_targets import analyze as write_analyze


def _plan(command: str, tmp_path: Path) -> script_pin.Plan:
    return script_pin.analyze(command, str(tmp_path))


def _category(command: str, tmp_path: Path) -> str:
    plan = _plan(command, tmp_path)
    if plan.unpinnable:
        return "UNPINNABLE"
    if plan.non_durable:
        return "NON-DURABLE"
    return "PINNED" if plan.refs else "NONE"


def _assert_unpinnable(command: str, tmp_path: Path) -> None:
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is True, (command, plan.refs)
    pin = script_pin.compute_pin(command, str(tmp_path))
    assert pin is not None
    assert pin.sha256 == script_pin.UNPINNABLE, command


def _assert_same_as(command: str, control: str, tmp_path: Path) -> None:
    plan = _plan(command, tmp_path)
    base = _plan(control, tmp_path)
    assert _category(command, tmp_path) == _category(control, tmp_path), (command, control)
    assert plan.refs == base.refs, (command, plan.refs, base.refs)


def _setup(tmp_path: Path) -> None:
    files = {
        "pyproject.toml": "[project]\nname='p'\nversion='0'\n",
        "package.json": '{"scripts": {"build": "node x.js"}}\n',
        "x.py": "print(1)\n",
        "x.js": "console.log(1)\n",
        "sub/package.json": '{"scripts": {"build": "node x.js"}}\n',
        "sub/pyproject.toml": "[project]\nname='s'\nversion='0'\n",
        "sub/x.py": "print(2)\n",
        "p/pyproject.toml": "[project]\nname='q'\nversion='0'\n",
        ".env": "A=1\n",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


# --- Section 1A. unknown flags fail closed ----------------------------------------------

UNKNOWN = [
    "uv run --bogus x.py",
    "uv run --bogus value x.py",
    "uv --bogus run x.py",
    "npm run --bogus build",
    "npm --bogus run build",
    "yarn --bogus run build",
    "yarn run --bogus build",
    "pnpm --bogus run build",
    "pnpm run --bogus build",
    "yarn --bogus build",
    "pnpm --bogus build",
]


@pytest.mark.parametrize("command", UNKNOWN)
def test_unknown_runner_flag_is_unpinnable(command: str, tmp_path: Path) -> None:
    _setup(tmp_path)
    _assert_unpinnable(command, tmp_path)


# --- Section 1B. known flags keep the current outcome -----------------------------------

# (command, flag-less control)
KNOWN_SAME = [
    ("uv run --with requests x.py", "uv run x.py"),
    ("uv run --with=requests x.py", "uv run x.py"),
    ("uv run -p 3.12 x.py", "uv run x.py"),
    ("uv run --python 3.12 x.py", "uv run x.py"),
    ("uv run --frozen x.py", "uv run x.py"),
    ("uv run --no-sync x.py", "uv run x.py"),
    ("uv run --locked x.py", "uv run x.py"),
    ("uv run --extra dev x.py", "uv run x.py"),
    ("uv run --group dev x.py", "uv run x.py"),
    ("uv run -q x.py", "uv run x.py"),
    ("uv run --isolated x.py", "uv run x.py"),
    ("uv --quiet run x.py", "uv run x.py"),
    ("npm run -s build", "npm run build"),
    ("npm run --silent build", "npm run build"),
    ("npm run build -- --watch", "npm run build"),
    ("npm run --if-present build", "npm run build"),
    ("yarn run build", "yarn build"),
    ("yarn -s run build", "yarn build"),
    ("pnpm run build", "pnpm build"),
    ("pnpm --silent run build", "pnpm build"),
]


@pytest.mark.parametrize(("command", "control"), KNOWN_SAME)
def test_known_runner_flag_keeps_outcome(command: str, control: str, tmp_path: Path) -> None:
    _setup(tmp_path)
    _assert_same_as(command, control, tmp_path)


@pytest.mark.parametrize("command", ["yarn build", "pnpm build", "uv run x.py", "npm run build"])
def test_flagless_runner_is_pinned(command: str, tmp_path: Path) -> None:
    _setup(tmp_path)
    assert _category(command, tmp_path) == "PINNED", command


# Guards whose flag changes the manifest/cwd: assert the concrete current outcome.
# (command, category, refs-differ-from-flagless-control)
KNOWN_CONCRETE = [
    ("uv run --project p x.py", "PINNED"),
    ("uv run --directory sub x.py", "PINNED"),
    ("yarn --cwd sub build", "PINNED"),
    ("pnpm -C sub run build", "PINNED"),
]


@pytest.mark.parametrize(("command", "category"), KNOWN_CONCRETE)
def test_known_dir_flag_concrete_outcome(command: str, category: str, tmp_path: Path) -> None:
    _setup(tmp_path)
    assert _category(command, tmp_path) == category, command


def test_uv_dir_flags_pin_the_selected_manifest(tmp_path: Path) -> None:
    _setup(tmp_path)
    assert _plan("uv run --project p x.py", tmp_path).refs != _plan("uv run x.py", tmp_path).refs
    assert (
        _plan("uv run --directory sub x.py", tmp_path).refs != _plan("uv run x.py", tmp_path).refs
    )


def test_uv_env_file_keeps_109_behaviour(tmp_path: Path) -> None:
    _setup(tmp_path)
    plan = _plan("uv run --env-file .env x.py", tmp_path)
    assert plan.unpinnable is False
    assert plan.non_durable == []
    assert len(plan.refs) > len(_plan("uv run x.py", tmp_path).refs)


# --- Section 2. logical vs physical paths -----------------------------------------------


def _symlink_tree(tmp_path: Path) -> Path:
    root = tmp_path.resolve()
    (root / "a" / "b").mkdir(parents=True)
    (root / "link").symlink_to(root / "a" / "b")
    (root / "x.sh").write_text("# top\n")
    (root / "a" / "x.sh").write_text("# a\n")
    (root / "a" / "b" / "x.sh").write_text("# a/b\n")
    return root


def _pinned_names(command: str, root: Path) -> set[str]:
    pin = script_pin.compute_pin(command, str(root))
    paths = {} if pin is None else pin.paths
    return {os.path.relpath(p, root) for p in paths if p.endswith("x.sh")}


def _unpinnable(command: str, root: Path) -> bool:
    return script_pin.analyze(command, str(root)).unpinnable


def test_physical_script_path_follows_symlink(tmp_path: Path) -> None:
    root = _symlink_tree(tmp_path)
    names = _pinned_names("bash link/../x.sh", root)
    assert "a/x.sh" in names, names


@pytest.mark.parametrize(
    "command",
    [
        "cd link/.. && bash x.sh",
        "cd link && cd .. && bash x.sh",
        "pushd link/.. && bash x.sh",
    ],
)
def test_logical_cd_pins_top_script(command: str, tmp_path: Path) -> None:
    root = _symlink_tree(tmp_path)
    assert "x.sh" in _pinned_names(command, root), command


@pytest.mark.parametrize(
    "command",
    [
        "cd link/.. && bash x.sh",
        "cd link && cd .. && bash x.sh",
    ],
)
def test_logical_cd_also_pins_physical_script(command: str, tmp_path: Path) -> None:
    root = _symlink_tree(tmp_path)
    assert "a/x.sh" in _pinned_names(command, root), command


@pytest.mark.parametrize(
    "command",
    [
        "cd -P link/.. && bash x.sh",
        "cd link && bash ../x.sh",
    ],
)
def test_physical_cd_pins_a_script(command: str, tmp_path: Path) -> None:
    root = _symlink_tree(tmp_path)
    assert "a/x.sh" in _pinned_names(command, root), command


@pytest.mark.parametrize(
    "command",
    [
        "set -P; cd link/.. && bash x.sh",
        "set -o physical; cd link/.. && bash x.sh",
    ],
)
def test_physical_option_pins_a_script_or_is_unpinnable(command: str, tmp_path: Path) -> None:
    root = _symlink_tree(tmp_path)
    assert "a/x.sh" in _pinned_names(command, root) or _unpinnable(command, root), command


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd a && bash x.sh", "a/x.sh"),
        ("bash a/b/x.sh", "a/b/x.sh"),
        ("bash ./x.sh", "x.sh"),
    ],
)
def test_no_symlink_guards(command: str, expected: str, tmp_path: Path) -> None:
    root = _symlink_tree(tmp_path)
    assert expected in _pinned_names(command, root), command


def test_write_guard_cd_is_logical(tmp_path: Path) -> None:
    root = _symlink_tree(tmp_path)
    cps = write_analyze("cd link/.. && echo hi > out.txt")
    cp = cps[-1]
    assert "out.txt" in cp.writes
    assert "link/.." in cp.bases
    logical = {os.path.normpath(os.path.join(b, "out.txt")) for b in cp.bases}
    assert "out.txt" in logical, logical
    assert not (root / "out.txt").exists()


def test_write_guard_physical_cd_has_physical_candidate(tmp_path: Path) -> None:
    root = _symlink_tree(tmp_path)
    cp = write_analyze("cd -P link/.. && echo hi > out.txt", cwd=str(tmp_path))[-1]
    assert "out.txt" in cp.writes
    candidates = {
        os.path.relpath(os.path.normpath(os.path.join(root, b, "out.txt")), root) for b in cp.bases
    }
    assert "a/out.txt" in candidates, (cp.bases, candidates)


def test_write_guard_without_cwd_has_only_logical_bases(tmp_path: Path) -> None:
    _symlink_tree(tmp_path)
    cp = write_analyze("cd -P link/.. && echo hi > out.txt")[-1]
    assert "." not in cp.bases, cp.bases
    assert "link/.." in cp.bases, cp.bases


# --- Section 3. gaps found in review ------------------------------------------------------


def _setup_gaps(tmp_path: Path) -> Path:
    _setup(tmp_path)
    extra = {
        "evil.sh": "echo hi\n",
        "evil.js": "1\n",
        "evil": "#!/bin/sh\n",
        "rc": "\n",
        "pkg/package.json": '{"scripts": {"build": "node x.js"}}\n',
        "pkg/pyproject.toml": "[project]\nname='k'\nversion='0'\n",
        "wheels/w.txt": "",
        "localmod.py": "print(1)\n",
        "uv.toml": "\n",
    }
    for name, text in extra.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path.resolve()


def _ref_paths(plan: script_pin.Plan, root: Path) -> set[str]:
    out: set[str] = set()
    for ref in plan.refs:
        for base in ref.bases or ("",):
            if ref.kind == "file":
                out.add(os.path.realpath(os.path.join(root, base, ref.operand)))
            elif ref.kind in ("search", "upward"):
                for name in ref.names:
                    out.add(os.path.realpath(os.path.join(root, base, ref.operand, name)))
    return out


def _covers(plan: script_pin.Plan, root: Path, rel: str) -> bool:
    """Some ref pins `rel` (a file, or anything inside the directory `rel`)."""
    target = os.path.realpath(root / rel)
    return any(p == target or p.startswith(target + os.sep) for p in _ref_paths(plan, root))


def _module_pinned(plan: script_pin.Plan, root: Path, module: str) -> bool:
    if any(r.kind == "module" and r.operand.split(".")[0] == module for r in plan.refs):
        return True
    return _covers(plan, root, f"{module}.py")


# A. npm flags after the script name are config.

NPM_TRAILING_FLAGS = [
    "npm run build --script-shell=./evil.sh",
    "npm test --script-shell=./evil.sh",
    "npm run build --node-options=--require=./evil.js",
    "npm run build --userconfig=./rc",
    "npm run build --bogus",
]


@pytest.mark.parametrize("command", NPM_TRAILING_FLAGS)
def test_npm_trailing_flag_is_unpinnable(command: str, tmp_path: Path) -> None:
    _setup_gaps(tmp_path)
    _assert_unpinnable(command, tmp_path)


def test_npm_flag_after_double_dash_stays_pinnable(tmp_path: Path) -> None:
    _setup_gaps(tmp_path)
    plan = _plan("npm run build -- --bogus", tmp_path)
    assert plan.unpinnable is False
    assert plan.refs


# B. npm target relocation after the script name.


@pytest.mark.parametrize(
    ("command", "member"),
    [
        ("npm run build --prefix sub", "sub/package.json"),
        ("npm run build -w pkg", "pkg/package.json"),
    ],
)
def test_npm_trailing_relocation_pins_member_or_unpinnable(
    command: str, member: str, tmp_path: Path
) -> None:
    root = _setup_gaps(tmp_path)
    plan = _plan(command, tmp_path)
    assert plan.unpinnable or _covers(plan, root, member), (command, plan.refs)


# C. workspace / filter selection.


@pytest.mark.parametrize(
    "command",
    [
        "npm -w pkg run build",
        "npm --workspace=pkg run build",
        "pnpm --filter pkg run build",
        "pnpm -F pkg build",
    ],
)
def test_workspace_member_pinned_or_unpinnable(command: str, tmp_path: Path) -> None:
    root = _setup_gaps(tmp_path)
    plan = _plan(command, tmp_path)
    assert plan.unpinnable or _covers(plan, root, "pkg/package.json"), (command, plan.refs)


def test_pnpm_recursive_is_unpinnable_or_non_durable(tmp_path: Path) -> None:
    _setup_gaps(tmp_path)
    plan = _plan("pnpm -r run build", tmp_path)
    assert plan.unpinnable or plan.non_durable, plan.refs


# D. uv path-valued flags.

UV_PATH_FLAGS = [
    ("uv run --python ./evil x.py", "evil"),
    ("uv run -p ./evil x.py", "evil"),
    ("uv run --python=./evil x.py", "evil"),
    ("uv run --with ./pkg x.py", "pkg"),
    ("uv run --with-editable ./pkg x.py", "pkg"),
    ("uv run --find-links ./wheels x.py", "wheels"),
    ("uv run -f ./wheels x.py", "wheels"),
]


@pytest.mark.parametrize(("command", "value"), UV_PATH_FLAGS)
def test_uv_path_flag_pinned_or_refused(command: str, value: str, tmp_path: Path) -> None:
    root = _setup_gaps(tmp_path)
    plan = _plan(command, tmp_path)
    assert plan.unpinnable or plan.non_durable or _covers(plan, root, value), (command, plan.refs)


@pytest.mark.parametrize(
    "command",
    ["uv run --python 3.12 x.py", "uv run -p 3.12 x.py", "uv run --with requests x.py"],
)
def test_uv_non_path_flag_value_stays_pinnable(command: str, tmp_path: Path) -> None:
    root = _setup_gaps(tmp_path)
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False, command
    assert _covers(plan, root, "x.py"), (command, plan.refs)


# E. uv run -m / --module.


@pytest.mark.parametrize("command", ["uv run -m localmod", "uv run --module localmod"])
def test_uv_module_is_pinned_or_unpinnable(command: str, tmp_path: Path) -> None:
    root = _setup_gaps(tmp_path)
    plan = _plan(command, tmp_path)
    assert plan.unpinnable or _module_pinned(plan, root, "localmod"), (command, plan.refs)


# F. uv config file.


def test_uv_config_file_flag_pins_or_unpinnable(tmp_path: Path) -> None:
    root = _setup_gaps(tmp_path)
    plan = _plan("uv run --config-file ./uv.toml x.py", tmp_path)
    assert plan.unpinnable or _covers(plan, root, "uv.toml"), plan.refs


def test_uv_implicit_uv_toml_pins_or_unpinnable(tmp_path: Path) -> None:
    root = _setup_gaps(tmp_path)
    plan = _plan("uv run x.py", tmp_path)
    assert plan.unpinnable or _covers(plan, root, "uv.toml"), plan.refs


# G. unknown-flag forms for uv.


@pytest.mark.parametrize(
    "command",
    ["uv run --bogus=val x.py", "uv run -Z x.py", "uv run --bogus -- x.py"],
)
def test_uv_unknown_flag_forms_are_unpinnable(command: str, tmp_path: Path) -> None:
    _setup_gaps(tmp_path)
    _assert_unpinnable(command, tmp_path)


@pytest.mark.parametrize("command", ["uv run -- x.py", "uv run x.py --bogus"])
def test_uv_unknown_flag_negative_controls_stay_pinnable(command: str, tmp_path: Path) -> None:
    root = _setup_gaps(tmp_path)
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False, command
    assert _covers(plan, root, "x.py"), (command, plan.refs)


# --- Section 4. second review round -------------------------------------------------------


def _setup_round2(tmp_path: Path) -> Path:
    root = _setup_gaps(tmp_path)
    extra = {
        "local-1.0.whl": "x\n",
        "pkg.tar.gz": "x\n",
        "py": "#!/bin/sh\n",
        "bc.txt": "requests\n",
        "sub/uv.toml": "\n",
    }
    for name, text in extra.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


# R1. npm whole-workspace selection.


@pytest.mark.parametrize(
    "command",
    [
        "npm -ws run build",
        "npm --workspaces run build",
        "npm --include-workspace-root run build",
    ],
)
def test_r1_npm_all_workspaces_is_unpinnable(command: str, tmp_path: Path) -> None:
    _setup_round2(tmp_path)
    _assert_unpinnable(command, tmp_path)


# R2. bare local names that uv treats as paths.


@pytest.mark.parametrize(
    "command",
    [
        "uv run --find-links wheels x.py",
        "uv run -f wheels x.py",
        "uv run --with-editable pkg x.py",
    ],
)
def test_r2_uv_bare_directory_value_is_unpinnable(command: str, tmp_path: Path) -> None:
    _setup_round2(tmp_path)
    _assert_unpinnable(command, tmp_path)


@pytest.mark.parametrize(
    ("command", "value"),
    [
        ("uv run --with local-1.0.whl x.py", "local-1.0.whl"),
        ("uv run --with pkg.tar.gz x.py", "pkg.tar.gz"),
    ],
)
def test_r2_uv_bare_archive_pinned_or_unpinnable(command: str, value: str, tmp_path: Path) -> None:
    root = _setup_round2(tmp_path)
    plan = _plan(command, tmp_path)
    assert plan.unpinnable or _covers(plan, root, value), (command, plan.refs)


def test_usability_uv_with_index_name(tmp_path: Path) -> None:
    root = _setup_round2(tmp_path)
    plan = _plan("uv run --with requests x.py", tmp_path)
    assert plan.unpinnable is False
    assert _covers(plan, root, "x.py"), plan.refs


# R3. uv.toml of a relocated project.


@pytest.mark.parametrize("command", ["uv run --directory sub x.py", "uv run --project sub x.py"])
def test_r3_uv_relocated_pins_uv_toml_or_unpinnable(command: str, tmp_path: Path) -> None:
    root = _setup_round2(tmp_path)
    plan = _plan(command, tmp_path)
    assert plan.unpinnable or _covers(plan, root, "sub/uv.toml"), (command, plan.refs)


# R5. yarn workspaces.


def test_r5a_yarn_workspace_member_pinned_or_unpinnable(tmp_path: Path) -> None:
    root = _setup_round2(tmp_path)
    plan = _plan("yarn workspace pkg run build", tmp_path)
    assert plan.unpinnable or _covers(plan, root, "pkg/package.json"), plan.refs


def test_r5b_yarn_workspace_missing_member_is_unpinnable(tmp_path: Path) -> None:
    _setup_round2(tmp_path)
    _assert_unpinnable("yarn workspace missing build", tmp_path)


@pytest.mark.parametrize(
    "command", ["yarn workspaces foreach run build", "yarn workspaces run build"]
)
def test_r5c_yarn_all_workspaces_is_unpinnable(command: str, tmp_path: Path) -> None:
    _setup_round2(tmp_path)
    _assert_unpinnable(command, tmp_path)


# R10 / R11. write guard with a symlinked cwd and odd bytes.


def test_r10_write_guard_symlinked_cwd_physical_candidate(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    (base / "real" / "a").mkdir(parents=True)
    (base / "real" / "link").symlink_to(base / "real" / "a")
    (base / "cwdlink").symlink_to(base / "real")
    cwd = str(base / "cwdlink")
    cp = write_analyze("cd -P link/.. && echo hi > out.txt", cwd=cwd)[-1]
    assert "out.txt" in cp.writes
    resolved = {os.path.realpath(os.path.join(cwd, b, "out.txt")) for b in cp.bases}
    assert os.path.realpath(base / "real" / "out.txt") in resolved, (cp.bases, resolved)


def test_r11_write_guard_lone_surrogate_does_not_raise(tmp_path: Path) -> None:
    command = "cd -P 'a\udcff' && echo hi > out.txt"
    try:
        write_analyze(command)
    except Exception:  # noqa: BLE001
        pytest.skip("the shell parser itself rejects the input")
    cp = write_analyze(command, cwd=str(tmp_path))[-1]
    assert "a\udcff" in cp.bases, cp.bases


# R13. usability controls: legitimate commands stay pinnable with the exact target.


def test_usability_npm_prefix_existing_member(tmp_path: Path) -> None:
    root = _setup_round2(tmp_path)
    plan = _plan("npm run build --prefix sub", tmp_path)
    assert plan.unpinnable is False, plan.reasons
    assert _covers(plan, root, "sub/package.json"), plan.refs


def test_usability_npm_workspace_dir_member(tmp_path: Path) -> None:
    root = _setup_round2(tmp_path)
    plan = _plan("npm -w pkg run build", tmp_path)
    assert plan.unpinnable is False, plan.reasons
    assert _covers(plan, root, "pkg/package.json"), plan.refs


def test_usability_uv_python_file(tmp_path: Path) -> None:
    root = _setup_round2(tmp_path)
    plan = _plan("uv run --python ./py x.py", tmp_path)
    assert plan.unpinnable is False, plan.reasons
    assert _covers(plan, root, "py"), plan.refs
    assert _covers(plan, root, "x.py"), plan.refs


def test_usability_uv_module(tmp_path: Path) -> None:
    root = _setup_round2(tmp_path)
    plan = _plan("uv run -m localmod", tmp_path)
    assert plan.unpinnable is False, plan.reasons
    assert _module_pinned(plan, root, "localmod"), plan.refs


def test_usability_uv_implicit_uv_toml(tmp_path: Path) -> None:
    root = _setup_round2(tmp_path)
    plan = _plan("uv run x.py", tmp_path)
    assert plan.unpinnable is False, plan.reasons
    assert _covers(plan, root, "uv.toml"), plan.refs
    assert _covers(plan, root, "x.py"), plan.refs


@pytest.mark.parametrize(
    "command",
    [
        "npm run build --no-progress",
        "npm run build --no-update-notifier",
        "npm run build --audit=false",
        "npm run build --fund=false",
    ],
)
def test_usability_npm_trailing_harmless(command: str, tmp_path: Path) -> None:
    root = _setup_round2(tmp_path)
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False, (command, plan.reasons)
    assert _covers(plan, root, "package.json"), (command, plan.refs)


@pytest.mark.parametrize(
    "command",
    [
        "uv run --no-install-project x.py",
        "uv run --no-sources x.py",
        "uv run --inexact x.py",
        "uv run --verify-hashes x.py",
    ],
)
def test_usability_uv_harmless_flags(command: str, tmp_path: Path) -> None:
    root = _setup_round2(tmp_path)
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False, (command, plan.reasons)
    assert _covers(plan, root, "x.py"), (command, plan.refs)


def test_uv_build_constraints_file_pinned_or_unpinnable(tmp_path: Path) -> None:
    root = _setup_round2(tmp_path)
    plan = _plan("uv run -b ./bc.txt x.py", tmp_path)
    assert plan.unpinnable or _covers(plan, root, "bc.txt"), plan.refs
