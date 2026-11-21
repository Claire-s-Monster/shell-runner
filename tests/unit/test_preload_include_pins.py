"""Issue #98 item 2: preload/include flags, plus the item 1 leftovers.

PINNED: the local file the interpreter loads is in the pin (and the command is not
unpinnable). NON-DURABLE: pinned, but never reusable (include-dir flags are approve-once,
like PERL5LIB/RUBYLIB/PYTHONPATH). UNCHANGED: regression guard, refs identical to a
control command that has no such flag/import.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from shell_runner import script_pin
from shell_runner.classifier import classify

AGENT = "focused-code-modifier"  # AUTO_CAPPED cap


def _make(tmp_path: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _plan(command: str, tmp_path: Path) -> script_pin.Plan:
    return script_pin.analyze(command, str(tmp_path))


def _assert_pinned(command: str, tmp_path: Path, pinned: tuple[str, ...]) -> None:
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False, command
    pin = script_pin.compute_pin(command, str(tmp_path))
    assert pin is not None, command
    assert pin.sha256 != script_pin.UNPINNABLE, command
    for name in pinned:
        real = str((tmp_path / name).resolve())
        digest = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        assert pin.paths.get(real) == digest, (command, name, pin.paths)


def _assert_non_durable(command: str, tmp_path: Path) -> None:
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False, command
    assert plan.non_durable, command
    cls = classify(command, str(tmp_path), AGENT)
    assert cls.non_promotable is True, command
    assert cls.non_durable_reason is not None, command


def _assert_unchanged(command: str, control: str, tmp_path: Path) -> None:
    plan = _plan(command, tmp_path)
    base = _plan(control, tmp_path)
    assert plan.unpinnable is False, command
    assert plan.non_durable == [], command
    assert plan.refs == base.refs, (command, plan.refs, base.refs)


# --- 1-8. PINNED -----------------------------------------------------------------

PINNED = [
    ("node -r ./hook.js app.js", {"hook.js": "h", "app.js": "a"}, ("hook.js", "app.js")),
    ("node --require=./hook.js app.js", {"hook.js": "h", "app.js": "a"}, ("hook.js", "app.js")),
    ("node --require ./hook.js app.js", {"hook.js": "h", "app.js": "a"}, ("hook.js", "app.js")),
    ("node --import ./hook.mjs app.js", {"hook.mjs": "h", "app.js": "a"}, ("hook.mjs", "app.js")),
    ("ruby -r ./lib.rb x.rb", {"lib.rb": "l", "x.rb": "x"}, ("lib.rb", "x.rb")),
    ("ruby -r./lib.rb x.rb", {"lib.rb": "l", "x.rb": "x"}, ("lib.rb", "x.rb")),
    ("python -c 'import foo'", {"foo.py": "v=1\n"}, ("foo.py",)),
    ("python3 -c 'from foo import bar'", {"foo.py": "bar=1\n"}, ("foo.py",)),
    ("python -c 'import os, foo'", {"foo.py": "v=1\n"}, ("foo.py",)),
    (
        "python -c 'import foo.sub'",
        {"foo/__init__.py": "", "foo/sub.py": "v=1\n"},
        ("foo/__init__.py", "foo/sub.py"),
    ),
    ("python -c 'import json'", {"json.py": "v=1\n"}, ("json.py",)),
    ("python -m foo", {"foo.py": "v=1\n"}, ("foo.py",)),
    ("python -m pkg", {"pkg/__main__.py": "v=1\n"}, ("pkg/__main__.py",)),
    ("node -e \"require('./x')\"", {"x.js": "1\n"}, ("x.js",)),
    ("node -e \"require('./x.js')\"", {"x.js": "1\n"}, ("x.js",)),
    ("ruby -e \"require_relative 'x'\"", {"x.rb": "1\n"}, ("x.rb",)),
]


@pytest.mark.parametrize(("command", "files", "pinned"), PINNED, ids=[p[0] for p in PINNED])
def test_preload_flag_or_import_pins_local_file(
    tmp_path: Path, command: str, files: dict[str, str], pinned: tuple[str, ...]
) -> None:
    _make(tmp_path, files)
    _assert_pinned(command, tmp_path, pinned)


def test_import_after_cd_pins_file_in_new_cwd(tmp_path: Path) -> None:
    _make(tmp_path, {"sub/foo.py": "v=1\n"})
    _assert_pinned("cd sub && python -c 'import foo'", tmp_path, ("sub/foo.py",))


# --- 9-10. NON-DURABLE -------------------------------------------------------------

NON_DURABLE = [
    ("perl -I lib x.pl", {"x.pl": "1;\n", "lib/m.pm": "1;\n"}),
    ("perl -Ilib x.pl", {"x.pl": "1;\n", "lib/m.pm": "1;\n"}),
    ("ruby -I lib x.rb", {"x.rb": "1\n", "lib/m.rb": "1\n"}),
    ("ruby -Ilib x.rb", {"x.rb": "1\n", "lib/m.rb": "1\n"}),
    ("perl -I. -Mfoo -e 1", {"foo.pm": "1;\n"}),
    ("node -r somepkg app.js", {"app.js": "a\n"}),
]


@pytest.mark.parametrize(("command", "files"), NON_DURABLE, ids=[p[0] for p in NON_DURABLE])
def test_include_dir_flag_is_non_durable(
    tmp_path: Path, command: str, files: dict[str, str]
) -> None:
    _make(tmp_path, files)
    _assert_non_durable(command, tmp_path)


# --- 11-12. UNCHANGED --------------------------------------------------------------

UNCHANGED = [
    ("python -m pytest -q", "python -m pytest", {}),
    ("python -m json.tool", "python -m json.tool", {}),
    ("python -m pip --version", "python -m pip", {}),
    ("perl -MData::Dumper -e 1", "perl -e 1", {}),
    ("perl -Mlocalmod -e 1", "perl -e 1", {}),
    ("perl -Mlocalmod -e 1", "perl -e 1", {"localmod.pm": "1;\n"}),
    ("ruby -rjson -e 1", "ruby -e 1", {}),
    ("ruby -rlib x.rb", "ruby x.rb", {"lib.rb": "l\n", "x.rb": "x\n"}),
    ("node -e 'console.log(1)'", "node -e '1'", {}),
]


@pytest.mark.parametrize(
    ("command", "control", "files"),
    UNCHANGED,
    ids=[f"{c}|{'with-files' if f else 'bare'}" for c, _, f in UNCHANGED],
)
def test_ordinary_commands_are_unchanged(
    tmp_path: Path, command: str, control: str, files: dict[str, str]
) -> None:
    _make(tmp_path, files)
    _assert_unchanged(command, control, tmp_path)


# case 13 (imports inside string literals) is deliberately not asserted.

# --- 14-15. item 1 leftovers: env assignments as operands ------------------------

# xargs tail is `pixi run test` (a runner, not an interpreter): `xargs <interpreter>` is
# already unpinnable, which would mask the env check.
OPERAND_NON_DURABLE = [
    "find . -name '*.sh' -exec env BASH_ENV=./evil.sh bash y.sh \\;",
]


@pytest.mark.parametrize("command", OPERAND_NON_DURABLE)
def test_loader_env_as_operand_is_non_durable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, {"evil.sh": "e\n", "y.sh": "y\n", "pixi.toml": "[project]\n"})
    _assert_non_durable(command, tmp_path)


def test_xargs_env_runner_is_unpinnable_and_still_env_checked(tmp_path: Path) -> None:
    # xargs feeds stdin words to a runner (`pixi run test`), so the executed code is not
    # statically knowable: UNPINNABLE. The env check still runs, so the PYTHONPATH reason
    # is recorded as well.
    _make(tmp_path, {"pixi.toml": "[project]\n"})
    plan = _plan("printf 'a\\n' | xargs env PYTHONPATH=. pixi run test", tmp_path)
    assert plan.unpinnable is True
    assert any("PYTHONPATH" in reason for reason in plan.non_durable), plan.non_durable


@pytest.mark.parametrize(
    ("command", "control"),
    [
        ("printf 'a\\n' | xargs env FOO=1 echo", "printf 'a\\n' | xargs echo"),
        ("find . -exec env FOO=1 echo {} \\;", "find . -exec echo {} \\;"),
    ],
)
def test_harmless_env_as_operand_is_unchanged(tmp_path: Path, command: str, control: str) -> None:
    _assert_unchanged(command, control, tmp_path)


# --- 16-17. uv run --env-file ------------------------------------------------------


@pytest.mark.parametrize("flag", ["--env-file .env", "--env-file=.env"])
def test_env_file_with_loader_var_is_pinned_and_non_durable(tmp_path: Path, flag: str) -> None:
    _make(tmp_path, {".env": "PYTHONPATH=.\n", "x.py": "print(1)\n"})
    command = f"uv run {flag} python x.py"
    _assert_pinned(command, tmp_path, (".env", "x.py"))
    _assert_non_durable(command, tmp_path)


@pytest.mark.parametrize("flag", ["--env-file .env", "--env-file=.env"])
def test_env_file_harmless_is_pinned_but_durable(tmp_path: Path, flag: str) -> None:
    _make(tmp_path, {".env": "FOO=1\n", "x.py": "print(1)\n"})
    command = f"uv run {flag} python x.py"
    _assert_pinned(command, tmp_path, (".env",))
    assert _plan(command, tmp_path).non_durable == [], command


def test_env_file_missing_mirrors_missing_script(tmp_path: Path) -> None:
    """Missing-file behaviour of `bash ./missing.sh`: non_promotable, and the pin is not ok.

    Mirrors test_unresolvable_script_shape_is_non_promotable in
    test_script_content_binding.py.
    """
    _make(tmp_path, {"x.py": "print(1)\n"})
    assert classify("bash ./missing.sh", str(tmp_path), AGENT).non_promotable is True
    command = "uv run --env-file missing.env python x.py"
    assert classify(command, str(tmp_path), AGENT).non_promotable is True
    pin = script_pin.compute_pin(command, str(tmp_path))
    assert pin is not None
    assert pin.ok is False
    assert pin.paths.get(str((tmp_path / "missing.env").resolve())) == script_pin.MISSING


# =====================================================================================
# Review round 1 (#98)
# =====================================================================================

_MANIFESTS = {
    "pixi.toml": "[project]\n",
    "Makefile": "all:\n\techo hi\n",
    "package.json": '{"scripts": {"build": "echo hi"}}\n',
    "x.py": "print(1)\n",
}


def _assert_unpinnable(command: str, tmp_path: Path) -> None:
    assert _plan(command, tmp_path).unpinnable is True, command


# --- A. xargs fails closed -----------------------------------------------------------

XARGS_UNPINNABLE = [
    "xargs make",
    "xargs pixi run",
    "xargs pixi run test",
    "xargs pixi run bash",
    "xargs uv run",
    "xargs uv run x.py",
    "xargs npm run",
    "xargs yarn run build",
    "xargs nice bash",
    "xargs env python",
    "xargs timeout 5 bash",
    "xargs --process-slot-var V bash",
    "xargs -P2 env FOO=1 bash",
]


@pytest.mark.parametrize("command", XARGS_UNPINNABLE)
def test_xargs_runner_or_interpreter_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, _MANIFESTS)
    _assert_unpinnable(command, tmp_path)


@pytest.mark.parametrize("command", ["xargs echo", "xargs rm -f"])
def test_xargs_harmless_tail_is_not_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, _MANIFESTS)
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False, command
    assert plan.non_durable == [], command
    assert plan.refs == [] or not plan.refs, (command, plan.refs)


# --- B. options after the -e/-c code are still scanned -----------------------------


@pytest.mark.parametrize(
    ("command", "files", "pinned"),
    [
        ("node -e 'x' -r ./evil.js", {"evil.js": "e\n"}, ("evil.js",)),
        ("ruby -e 'x' -r ./evil.rb", {"evil.rb": "e\n"}, ("evil.rb",)),
    ],
    ids=["node", "ruby"],
)
def test_preload_after_inline_code_is_pinned(
    tmp_path: Path, command: str, files: dict[str, str], pinned: tuple[str, ...]
) -> None:
    _make(tmp_path, files)
    _assert_pinned(command, tmp_path, pinned)


@pytest.mark.parametrize("command", ["perl -e 1 -I /tmp/e", "ruby -e 1 -I lib"])
def test_include_dir_after_inline_code_is_non_durable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, {"lib/m.rb": "1\n"})
    _assert_non_durable(command, tmp_path)


# --- C. unknown cd target plus inline imports ----------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cd \"$X\" && python -c 'import foo'",
        "cd $(mktemp -d) && node -e \"require('./x')\"",
        'pushd "$X" && ruby -e "require_relative \'x\'"',
    ],
)
def test_unknown_cd_with_inline_imports_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, {"foo.py": "v=1\n", "x.js": "1\n", "x.rb": "1\n"})
    _assert_unpinnable(command, tmp_path)


def test_unknown_cd_without_imports_pins_current_behaviour(tmp_path: Path) -> None:
    # Pinned at HEAD a3506b7: no imports means the inline-code scan does not make it
    # unpinnable, and nothing is flagged non-durable.
    plan = _plan("cd \"$X\" && python -c 'print(1)'", tmp_path)
    assert plan.unpinnable is False
    assert plan.non_durable == []


# --- D. unparseable / expanded inline code containing an import ----------------------


@pytest.mark.parametrize(
    "command",
    [
        'python -c "import $M"',
        'python -c "import `echo foo`"',
        "python -c 'import foo('",
        "node -e \"require('./$X')\"",
        "node -e \"require('./' + x)\"",
        "node -e 'require(`./${x}`)'",
        "node -e \"import('./' + x)\"",
    ],
)
def test_unresolvable_inline_import_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, {"foo.py": "v=1\n", "x.js": "1\n"})
    _assert_unpinnable(command, tmp_path)


@pytest.mark.parametrize(
    ("command", "control"),
    [
        ("python -c 'print(1'", "python -c 'pass'"),
        ("python -c \"print('$HOME')\"", "python -c 'pass'"),
        ("node -e 'console.log(1)'", "node -e '1'"),
    ],
)
def test_unparseable_inline_without_import_is_unchanged(
    tmp_path: Path, command: str, control: str
) -> None:
    _assert_unchanged(command, control, tmp_path)


# --- E. node inline absolute paths ---------------------------------------------------


@pytest.mark.parametrize(
    ("template", "name"),
    [
        ("node -e \"require('{p}')\"", "e.js"),
        ("node -e \"import('{p}')\"", "x.mjs"),
    ],
    ids=["require", "import"],
)
def test_node_inline_absolute_path_is_pinned(tmp_path: Path, template: str, name: str) -> None:
    _make(tmp_path, {name: "1\n"})
    command = template.format(p=str(tmp_path / name))
    _assert_pinned(command, tmp_path, (name,))


# --- F. node inline bare require -----------------------------------------------------


@pytest.mark.parametrize("mod", ["fs", "node:fs", "path"])
def test_node_inline_builtin_require_is_unchanged(tmp_path: Path, mod: str) -> None:
    _assert_unchanged(f"node -e \"require('{mod}')\"", "node -e '1'", tmp_path)


def test_node_inline_bare_package_is_non_durable(tmp_path: Path) -> None:
    _assert_non_durable("node -e \"require('somepkg')\"", tmp_path)


# --- G. ruby inline load -------------------------------------------------------------


def test_ruby_inline_load_is_pinned(tmp_path: Path) -> None:
    _make(tmp_path, {"x.rb": "1\n"})
    _assert_pinned("ruby -e \"load './x.rb'\"", tmp_path, ("x.rb",))


# --- H. --env-file -------------------------------------------------------------------


def test_env_file_path_value_is_non_durable(tmp_path: Path) -> None:
    _make(tmp_path, {".env": "PATH=/tmp/x\n", "x.py": "print(1)\n"})
    _assert_non_durable("uv run --env-file .env python x.py", tmp_path)


def test_env_file_export_with_tab_is_non_durable(tmp_path: Path) -> None:
    _make(tmp_path, {".env": "export\tNODE_OPTIONS=--require=./e.js\n", "x.py": "print(1)\n"})
    _assert_non_durable("uv run --env-file .env python x.py", tmp_path)


def test_env_file_loader_var_after_large_padding_is_not_durable(tmp_path: Path) -> None:
    padding = "# " + "p" * 62 + "\n"
    text = padding * (1024 * 1024 // len(padding) + 2) + "PYTHONPATH=.\n"
    assert len(text) > 1024 * 1024
    _make(tmp_path, {".env": text, "x.py": "print(1)\n"})
    plan = _plan("uv run --env-file .env python x.py", tmp_path)
    assert not plan.unpinnable
    assert any("PYTHONPATH" in reason for reason in plan.non_durable), plan.non_durable


# --- I. minor gaps -------------------------------------------------------------------


@pytest.mark.parametrize("command", ["perl -Mlib=/tmp/e -e 1", "perl -Mlib=lib x.pl"])
def test_perl_lib_pragma_is_non_durable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, {"x.pl": "1;\n", "lib/m.pm": "1;\n"})
    _assert_non_durable(command, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "node --loader ./l.mjs app.js",
        "node --experimental-loader=./l.mjs app.js",
        "node --import=./l.mjs app.js",
    ],
)
def test_node_loader_flags_pin_the_loader(tmp_path: Path, command: str) -> None:
    _make(tmp_path, {"l.mjs": "l\n", "app.js": "a\n"})
    _assert_pinned(command, tmp_path, ("l.mjs",))


def test_ruby_chdir_resolves_preload_in_new_dir(tmp_path: Path) -> None:
    _make(tmp_path, {"sub/x.rb": "1\n"})
    _assert_pinned("ruby -C sub -r ./x.rb -e 1", tmp_path, ("sub/x.rb",))


def test_python_heredoc_import_is_pinned(tmp_path: Path) -> None:
    # Spec: asserted PINNED even if the analyzer cannot see heredoc bodies today.
    _make(tmp_path, {"foo.py": "v=1\n"})
    _assert_pinned("python3 - <<'EOF'\nimport foo\nEOF", tmp_path, ("foo.py",))


# --- J. ruby require regex: no polynomial backtracking (CodeQL py/polynomial-redos) ------


# =====================================================================================
# PR #109 review fixes (#98)
# =====================================================================================

ABSENT_IMPORTS = [
    ("python -c 'import json'", "json.py"),
    ("python3 -c 'from foo import bar'", "foo/__init__.py"),
    ("node -e \"require('./x')\"", "x.js"),
    ("node -e \"import('./x')\"", "x.mjs"),
    ("ruby -e \"require_relative 'x'\"", "x.rb"),
    ("ruby -e \"load './x'\"", "x.rb"),
]


@pytest.mark.parametrize(("command", "created"), ABSENT_IMPORTS, ids=[c for c, _ in ABSENT_IMPORTS])
def test_absent_import_is_pinned_missing_and_creating_it_invalidates(
    tmp_path: Path, command: str, created: str
) -> None:
    pin = script_pin.compute_pin(command, str(tmp_path))
    assert pin is not None
    assert pin.ok is True
    assert script_pin.pin_matches(command, str(tmp_path), pin.sha256)
    _make(tmp_path, {created: "evil = 1\n"})
    assert not script_pin.pin_matches(command, str(tmp_path), pin.sha256)


def test_absent_import_does_not_make_command_non_promotable(tmp_path: Path) -> None:
    plan = _plan("python -c 'import json'", tmp_path)
    assert plan.unpinnable is False
    assert plan.non_durable == []
    assert plan.refs
    assert all(ref.script is False and ref.required is False for ref in plan.refs)


@pytest.mark.parametrize(
    "command",
    [
        "node -e 'require/*c*/(\"./x\")'",
        "node -e 'require /* a */ /* b */ (\"./x\")'",
        "node -e 'import/*c*/(\"./x\")'",
        "node -e 'import/*c*/\"./x\"'",
        "node -e 'require // c\n(\"./x\")'",
        'node -e \'"/*"; require/*c*/("./x"); "*/"\'',
        'node -e \'/"/; require/*c*/("./x")\'',
    ],
)
def test_node_comment_between_require_and_argument_is_pinned(tmp_path: Path, command: str) -> None:
    _make(tmp_path, {"x.js": "1\n"})
    _assert_pinned(command, tmp_path, ("x.js",))


def test_node_comment_with_dynamic_argument_is_unpinnable(tmp_path: Path) -> None:
    _assert_unpinnable("node -e 'require/*c*/(x)'", tmp_path)


@pytest.mark.parametrize(
    ("command", "files", "pinned"),
    [
        ("node -e \"require('./data')\"", {"data.json": "{}\n"}, ("data.json",)),
        ("node -e \"require('./d')\"", {"d/index.json": "{}\n"}, ("d/index.json",)),
    ],
)
def test_node_resolution_includes_json(
    tmp_path: Path, command: str, files: dict[str, str], pinned: tuple[str, ...]
) -> None:
    _make(tmp_path, files)
    _assert_pinned(command, tmp_path, pinned)


@pytest.mark.parametrize(
    "command",
    [
        "ruby -e 'require \"./#{x}\"'",
        "ruby -e 'f = \"./x\"; require f'",
        "ruby -e 'load f'",
        "ruby -e 'require_relative(f)'",
        'ruby -e "require \\"./$X\\""',
    ],
)
def test_ruby_non_literal_require_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, {"x.rb": "1\n"})
    _assert_unpinnable(command, tmp_path)


def test_ruby_literal_require_is_not_dynamic(tmp_path: Path) -> None:
    _make(tmp_path, {"x.rb": "1\n"})
    assert _plan('ruby -e \'require "json"; require_relative "x"\'', tmp_path).unpinnable is False


def test_attached_inline_code_does_not_hide_trailing_preload(tmp_path: Path) -> None:
    _make(tmp_path, {"hook.rb": "h\n"})
    _assert_pinned("ruby '-ex=1;r=2e' -r ./hook.rb", tmp_path, ("hook.rb",))


def test_ruby_home_relative_preload_is_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _make(home, {"hook.rb": "h\n"})
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "work"
    cwd.mkdir()
    command = "ruby -r ~/hook.rb -e 1"
    pin = script_pin.compute_pin(command, str(cwd))
    assert pin is not None
    assert pin.paths.get(str((home / "hook.rb").resolve())) == hashlib.sha256(b"h\n").hexdigest()


@pytest.mark.parametrize("mod", ["fs", "node:fs", "path"])
def test_node_builtin_preload_stays_durable(tmp_path: Path, mod: str) -> None:
    _make(tmp_path, {"app.js": "a\n"})
    _assert_unchanged(f"node -r {mod} app.js", "node app.js", tmp_path)


def _assert_reusable_pin(command: str, tmp_path: Path) -> script_pin.ScriptPin:
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False, command
    assert plan.non_durable == [], command
    pin = script_pin.compute_pin(command, str(tmp_path))
    assert pin is not None
    assert pin.sha256 != script_pin.UNPINNABLE, command
    assert script_pin.pin_matches(command, str(tmp_path), pin.sha256)
    return pin


def test_many_absent_python_imports_stay_pinnable(tmp_path: Path) -> None:
    mods = ", ".join(f"m{i}" for i in range(1, 18))
    _assert_reusable_pin(f"python -c 'import {mods}'", tmp_path)


def test_many_absent_python_imports_after_cd_stay_pinnable(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    mods = ", ".join(f"m{i}" for i in range(1, 10))
    _assert_reusable_pin(f"cd sub && python -c 'import {mods}'", tmp_path)


def test_many_absent_node_requires_stay_pinnable_and_creation_invalidates(
    tmp_path: Path,
) -> None:
    command = 'node -e "' + ";".join(f"require('./{c}')" for c in "abcde") + '"'
    pin = _assert_reusable_pin(command, tmp_path)
    _make(tmp_path, {"c.js": "evil = 1\n"})
    assert not script_pin.pin_matches(command, str(tmp_path), pin.sha256)


def test_over_file_cap_of_existing_files_is_unpinnable(tmp_path: Path) -> None:
    n = script_pin.MAX_FILES + 1
    _make(tmp_path, {f"m{i}.py": "x = 1\n" for i in range(n)})
    mods = ", ".join(f"m{i}" for i in range(n))
    pin = script_pin.compute_pin(f"python -c 'import {mods}'", str(tmp_path))
    assert pin is not None
    assert pin.unpinnable


def test_over_missing_cap_is_unpinnable(tmp_path: Path) -> None:
    n = script_pin.MAX_MISSING_FILES // 7 + 2  # node: 7 candidates per absent spec
    command = 'node -e "' + ";".join(f"require('./a{i}')" for i in range(n)) + '"'
    pin = script_pin.compute_pin(command, str(tmp_path))
    assert pin is not None
    assert pin.unpinnable


def test_ruby_requires_long_whitespace_run_is_fast() -> None:
    import time

    code = "require" + " " * 30_000 + "x"
    start = time.perf_counter()
    assert script_pin._ruby_requires(code) == []
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("require './x'", ["./x"]),
        ("require('./x')", ["./x"]),
        ("require ( './x' )", ["./x"]),
        ("require (  './x'", ["./x"]),
        ('require_relative "x"', ["x"]),
        ("load './x.rb'", ["./x.rb"]),
        ("require 'json'", []),
        ("foo.require './x'", []),
    ],
)
def test_ruby_requires_forms(code: str, expected: list[str]) -> None:
    assert script_pin._ruby_requires(code) == expected
