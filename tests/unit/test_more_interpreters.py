"""Issue #98 item 3: more interpreters (pypy, lua, R, php, csh, deno, bun, gawk, ...).

PINNED: the local file the interpreter runs/loads is in the pin (and the command is not
unpinnable). NON-DURABLE: pinned, but approve_once only (code-loading env vars).
UNPINNABLE: refused, fail closed. UNCHANGED: regression guard, refs identical to a control
command that has no such flag/import (or the current behaviour, asserted against a control).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from shell_runner import script_pin
from shell_runner.classifier import classify

AGENT = "focused-code-modifier"  # AUTO_CAPPED cap


def _make(tmp_path: Path, names: tuple[str, ...]) -> None:
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {name}\n")


def _plan(command: str, tmp_path: Path) -> script_pin.Plan:
    return script_pin.analyze(command, str(tmp_path))


def _assert_pinned(command: str, tmp_path: Path, pinned: tuple[str, ...]) -> None:
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False, (command, plan.reasons)
    pin = script_pin.compute_pin(command, str(tmp_path))
    assert pin is not None, command
    assert pin.sha256 != script_pin.UNPINNABLE, command
    for name in pinned:
        real = str((tmp_path / name).resolve())
        digest = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        assert pin.paths.get(real) == digest, (command, name, pin.paths)


def _assert_unpinnable(command: str, tmp_path: Path) -> None:
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is True, command
    pin = script_pin.compute_pin(command, str(tmp_path))
    assert pin is not None
    assert pin.sha256 == script_pin.UNPINNABLE, command


def _assert_non_durable(command: str, tmp_path: Path, pinned: tuple[str, ...]) -> None:
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False, (command, plan.reasons)
    assert plan.non_durable, command
    _assert_pinned(command, tmp_path, pinned)
    cls = classify(command, str(tmp_path), AGENT)
    assert cls.non_promotable is True, command
    assert cls.non_durable_reason is not None, command


def _assert_approve_once_only(command: str, tmp_path: Path) -> None:
    """Non-durable: not unpinnable (approve_once allowed), never covered by a reusable approval."""
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False, (command, plan.reasons)
    assert plan.non_durable, command
    cls = classify(command, str(tmp_path), AGENT)
    assert cls.script_unpinnable is False, command
    assert cls.non_promotable is True, command
    assert cls.non_durable_reason is not None, command


def _assert_unchanged(command: str, control: str, tmp_path: Path) -> None:
    plan = _plan(command, tmp_path)
    base = _plan(control, tmp_path)
    assert plan.unpinnable is False, (command, plan.reasons)
    assert plan.non_durable == [], command
    assert plan.refs == base.refs, (command, plan.refs, base.refs)


def _category(command: str, tmp_path: Path) -> str:
    plan = _plan(command, tmp_path)
    if plan.unpinnable:
        return "UNPINNABLE"
    if plan.non_durable:
        return "NON-DURABLE"
    return "PINNED" if plan.refs else "NONE"


def _pin_paths(command: str, tmp_path: Path) -> dict[str, str]:
    pin = script_pin.compute_pin(command, str(tmp_path))
    return {} if pin is None else pin.paths


# --- A. name recognition: a local script file is PINNED ---------------------------------

NAMES = [
    ("pypy x.py", ("x.py",)),
    ("pypy3 x.py", ("x.py",)),
    ("pypy3.10 x.py", ("x.py",)),
    ("pypy3 -c 'import foo'", ("foo.py",)),
    ("perl5.36 x.pl", ("x.pl",)),
    ("perl5.36.0 x.pl", ("x.pl",)),
    ("ruby3.2 x.rb", ("x.rb",)),
    ("ruby3.2.2 x.rb", ("x.rb",)),
    ("php8.2 x.php", ("x.php",)),
    ("php8 x.php", ("x.php",)),
    ("csh x.csh", ("x.csh",)),
    ("tcsh x.csh", ("x.csh",)),
    ("lua x.lua", ("x.lua",)),
    ("lua5.4 x.lua", ("x.lua",)),
    ("luajit x.lua", ("x.lua",)),
    ("Rscript x.R", ("x.R",)),
    ("tclsh x.tcl", ("x.tcl",)),
    ("tclsh8.6 x.tcl", ("x.tcl",)),
    ("wish x.tcl", ("x.tcl",)),
    ("awk -f x.awk", ("x.awk",)),  # plain awk is already pinned: the variants mirror it
    ("gawk -f x.awk", ("x.awk",)),
    ("mawk -f x.awk", ("x.awk",)),
    ("nawk -f x.awk", ("x.awk",)),
    ("deno run x.ts", ("x.ts",)),
    ("bun x.ts", ("x.ts",)),
    ("bun run x.ts", ("x.ts",)),
    ("osascript x.scpt", ("x.scpt",)),
    ("/usr/bin/pypy3 x.py", ("x.py",)),
    ("/opt/lua/bin/lua5.4 x.lua", ("x.lua",)),
]


@pytest.mark.parametrize(("command", "files"), NAMES, ids=[n[0] for n in NAMES])
def test_interpreter_name_pins_local_script(
    tmp_path: Path, command: str, files: tuple[str, ...]
) -> None:
    _make(tmp_path, files)
    _assert_pinned(command, tmp_path, files)


def test_busybox_awk_stays_unpinnable(tmp_path: Path) -> None:
    # busybox is a launcher head, and a spec must never require relaxing an existing
    # fail-closed result; whether to support busybox applets is a separate decision.
    _make(tmp_path, ("x.awk",))
    assert _plan("busybox awk -f x.awk", tmp_path).unpinnable is True


# --- B. inline code ---------------------------------------------------------------------

CSH_INLINE = [
    "csh -c 'source ./x.csh'",
    "tcsh -c 'echo hi'",
    "csh -fc 'echo hi'",
]


@pytest.mark.parametrize("command", CSH_INLINE)
def test_csh_inline_body_fails_closed(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.csh",))
    _assert_unpinnable(command, tmp_path)


INLINE_PINNED = [
    ("lua -e \"require 'x'\"", ("x.lua",)),
    ("lua -e 'require(\"x\")'", ("x.lua",)),
    ("lua -e \"dofile('x.lua')\"", ("x.lua",)),
    ("Rscript -e \"source('x.R')\"", ("x.R",)),
    ("deno eval \"import './x.ts'\"", ("x.ts",)),
    ("bun -e \"require('./x.js')\"", ("x.js",)),
    ("bun --eval \"require('./x.js')\"", ("x.js",)),
    ("php -r \"require './x.php';\"", ("x.php",)),
    ("php -r \"include('x.php');\"", ("x.php",)),
    ("lua -l x y.lua", ("x.lua", "y.lua")),
    ("lua -lx y.lua", ("x.lua", "y.lua")),
]


@pytest.mark.parametrize(("command", "files"), INLINE_PINNED, ids=[p[0] for p in INLINE_PINNED])
def test_inline_code_pins_local_files(tmp_path: Path, command: str, files: tuple[str, ...]) -> None:
    _make(tmp_path, files)
    _assert_pinned(command, tmp_path, files)


INLINE_UNCHANGED = [
    ("lua -e 'print(1)'", "lua -e 1", ()),
    ("Rscript -e 'library(stats)'", "Rscript -e 1", ()),
    ("php -r 'echo 1;'", "php -r 1", ()),
]


@pytest.mark.parametrize(
    ("command", "control", "files"), INLINE_UNCHANGED, ids=[p[0] for p in INLINE_UNCHANGED]
)
def test_inline_code_without_local_file_is_unchanged(
    tmp_path: Path, command: str, control: str, files: tuple[str, ...]
) -> None:
    _make(tmp_path, files)
    _assert_unchanged(command, control, tmp_path)


ABSENT_CANDIDATES = [
    ("lua -e \"require 'x'\"", {"x.lua", "x/init.lua"}),
    ("lua -l socket y.lua", {"socket.lua", "socket/init.lua", "y.lua"}),
]


@pytest.mark.parametrize(
    ("command", "names"), ABSENT_CANDIDATES, ids=[p[0] for p in ABSENT_CANDIDATES]
)
def test_absent_lua_candidates_are_pinned_as_missing(
    tmp_path: Path, command: str, names: set[str]
) -> None:
    """#109 semantics: a module created later changes the pin (absent candidates pinned)."""
    _make(tmp_path, ("y.lua",))
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False, (command, plan.reasons)
    assert plan.non_durable == [], command
    assert names <= {ref.operand for ref in plan.refs}, (command, plan.refs)


INLINE_UNPINNABLE = [
    'lua -e "require(m)"',
    'Rscript -e "source(f)"',
    "php -r 'require $f;'",
]


@pytest.mark.parametrize("command", INLINE_UNPINNABLE)
def test_inline_dynamic_code_fails_closed(tmp_path: Path, command: str) -> None:
    _assert_unpinnable(command, tmp_path)


# --- C. preload and config flags --------------------------------------------------------

FLAGS = [
    ("bun --preload ./p.ts x.ts", ("p.ts", "x.ts"), ("p.ts",)),
    ("bun --preload=./p.ts x.ts", ("p.ts", "x.ts"), ("p.ts",)),
    ("php -d auto_prepend_file=./p.php x.php", ("p.php", "x.php"), ("p.php",)),
    ("php -dauto_prepend_file=./p.php x.php", ("p.php", "x.php"), ("p.php",)),
    ("php -d auto_append_file=./p.php x.php", ("p.php", "x.php"), ("p.php",)),
    ("php -c ./custom.ini x.php", ("custom.ini", "x.php"), ("custom.ini",)),
    ("gawk -i ./lib.awk -f x.awk", ("lib.awk", "x.awk"), ("lib.awk",)),
    ("gawk --include=./lib.awk -f x.awk", ("lib.awk", "x.awk"), ("lib.awk",)),
    ("deno run --config ./deno.json x.ts", ("deno.json", "x.ts"), ("deno.json",)),
    ("deno run --import-map ./im.json x.ts", ("im.json", "x.ts"), ("im.json",)),
]


@pytest.mark.parametrize(("command", "files", "pinned"), FLAGS, ids=[p[0] for p in FLAGS])
def test_preload_or_config_flag_pins_file(
    tmp_path: Path, command: str, files: tuple[str, ...], pinned: tuple[str, ...]
) -> None:
    _make(tmp_path, files)
    _assert_pinned(command, tmp_path, pinned)


def test_php_no_ini_flag_is_unchanged(tmp_path: Path) -> None:
    _make(tmp_path, ("x.php",))
    _assert_unchanged("php -n x.php", "php x.php", tmp_path)


# --- D. implicit cwd files: Rscript auto-loads ./.Rprofile ------------------------------


def test_rscript_pins_rprofile(tmp_path: Path) -> None:
    _make(tmp_path, (".Rprofile", "x.R"))
    _assert_pinned("Rscript x.R", tmp_path, (".Rprofile", "x.R"))


@pytest.mark.parametrize("command", ["Rscript --vanilla x.R", "Rscript --no-init-file x.R"])
def test_rscript_skipping_init_does_not_pin_rprofile(tmp_path: Path, command: str) -> None:
    _make(tmp_path, (".Rprofile", "x.R"))
    _assert_pinned(command, tmp_path, ("x.R",))
    assert str((tmp_path / ".Rprofile").resolve()) not in _pin_paths(command, tmp_path)


# --- E. fail closed ---------------------------------------------------------------------

FAIL_CLOSED = [
    ("go run main.go", ("main.go",)),
    ("go run .", ("main.go",)),
    ("go run ./cmd/x", ("cmd/x/main.go",)),
    ("deno run https://example.com/x.ts", ()),
    ("deno run npm:cowsay", ()),
    ("deno task build", ("deno.json",)),
    ("deno run --unknown-flag x.ts", ("x.ts",)),
    ("xargs lua", ()),
    ("xargs Rscript", ()),
    ("xargs pypy3", ()),
    ("xargs deno run", ()),
]


@pytest.mark.parametrize(("command", "files"), FAIL_CLOSED, ids=[p[0] for p in FAIL_CLOSED])
def test_unpinnable_invocations_fail_closed(
    tmp_path: Path, command: str, files: tuple[str, ...]
) -> None:
    _make(tmp_path, files)
    _assert_unpinnable(command, tmp_path)


def test_bun_run_script_behaves_like_npm_run(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"build": "echo hi"}}')
    control = _category("npm run build", tmp_path)
    assert _category("bun run build", tmp_path) == control
    manifest = str((tmp_path / "package.json").resolve())
    assert manifest in _pin_paths("npm run build", tmp_path)
    assert manifest in _pin_paths("bun run build", tmp_path)


def test_bunx_behaves_like_npx(tmp_path: Path) -> None:
    assert _category("bunx cowsay", tmp_path) == _category("npx cowsay", tmp_path)


# --- F. env-var loaders are non-durable -------------------------------------------------

ENV_LOADERS = [
    ("LUA_INIT=@./evil.lua lua x.lua", "x.lua"),
    ("LUA_INIT_5_4=@./evil.lua lua5.4 x.lua", "x.lua"),
    ("LUA_PATH='./?.lua' lua x.lua", "x.lua"),
    ("LUA_CPATH='./?.so' lua x.lua", "x.lua"),
    ("R_PROFILE_USER=./p.R Rscript x.R", "x.R"),
    ("R_ENVIRON_USER=./e Rscript x.R", "x.R"),
    ("R_PROFILE=./p.R Rscript x.R", "x.R"),
    ("PHPRC=./ini php x.php", "x.php"),
    ("PHP_INI_SCAN_DIR=./d php x.php", "x.php"),
    ("AWKPATH=./lib gawk -f x.awk", "x.awk"),
]


@pytest.mark.parametrize(("command", "script"), ENV_LOADERS, ids=[p[0] for p in ENV_LOADERS])
def test_env_code_loader_is_non_durable(tmp_path: Path, command: str, script: str) -> None:
    _make(tmp_path, (script,))
    _assert_non_durable(command, tmp_path, (script,))


def test_ordinary_env_var_is_not_non_durable(tmp_path: Path) -> None:
    _make(tmp_path, ("x.lua",))
    plan = _plan("FOO=1 lua x.lua", tmp_path)
    assert plan.non_durable == []
    _assert_pinned("FOO=1 lua x.lua", tmp_path, ("x.lua",))


# --- G. regression guards ---------------------------------------------------------------

REGRESSION = [
    ("python3 x.py", "x.py"),
    ("perl x.pl", "x.pl"),
    ("node x.js", "x.js"),
    ("ruby x.rb", "x.rb"),
]


@pytest.mark.parametrize(("command", "script"), REGRESSION, ids=[p[0] for p in REGRESSION])
def test_existing_interpreters_still_pinned(tmp_path: Path, command: str, script: str) -> None:
    _make(tmp_path, (script,))
    _assert_pinned(command, tmp_path, (script,))
    assert _plan(command, tmp_path).non_durable == []


@pytest.mark.parametrize("command", ["go version", "deno --version", "bun --version"])
def test_version_queries_are_not_unpinnable(tmp_path: Path, command: str) -> None:
    assert _plan(command, tmp_path).unpinnable is False


@pytest.mark.parametrize("command", ["go build ./..."])
def test_go_build_keeps_current_outcome(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("main.go",))
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False
    assert plan.non_durable == []
    assert plan.refs == _plan("true", tmp_path).refs


# Review round 1 (item 3)


def _write(tmp_path: Path, name: str, text: str) -> None:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# A. test runners execute discovered code ---------------------------------------------

RUNNERS_UNPINNABLE = [
    "deno test",
    "deno test x_test.ts",
    "deno bench",
    "bun test",
    "bun test x.test.ts",
    "go generate ./...",
    "go test",
    "go test ./...",
    "go test -run TestX ./pkg",
    "go -C sub test ./...",
]


@pytest.mark.parametrize("command", RUNNERS_UNPINNABLE)
def test_test_runners_and_generators_are_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x_test.ts", "x.test.ts", "main.go"))
    _assert_unpinnable(command, tmp_path)


@pytest.mark.parametrize("command", ["go build ./..."])
def test_go_build_category_is_current(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("main.go", "x_test.go"))
    assert _category(command, tmp_path) == _category("true", tmp_path)


# B. flags before the subcommand ------------------------------------------------------

UNKNOWN_PRE_FLAGS = [
    "deno --config lint run evil.ts",
    "deno --unknown run x.ts",
    "bun --jsx-runtime automatic x.ts",
    "bun --filter pkg run build",
    "bun --unknown x.ts",
]


@pytest.mark.parametrize("command", UNKNOWN_PRE_FLAGS)
def test_unknown_flag_before_subcommand_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.ts", "evil.ts"))
    (tmp_path / "package.json").write_text('{"scripts": {"build": "echo hi"}}')
    _assert_unpinnable(command, tmp_path)


KNOWN_FLAG_GUARDS = [
    "deno run -A x.ts",
    "deno run --allow-read=. x.ts",
    "deno run --allow-read x.ts",
    "bun x.ts",
    "bun run x.ts",
]


@pytest.mark.parametrize("command", KNOWN_FLAG_GUARDS)
def test_known_deno_bun_flags_stay_pinned(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.ts",))
    _assert_pinned(command, tmp_path, ("x.ts",))


# C. bunfig.toml ----------------------------------------------------------------------


@pytest.mark.parametrize("command", ["bun run build", "bun x cowsay"])
def test_bun_pins_bunfig_for_package_scripts_and_x(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("bunfig.toml",))
    (tmp_path / "package.json").write_text('{"scripts": {"build": "echo hi"}}')
    pinned = ("bunfig.toml", "package.json") if command == "bun run build" else ("bunfig.toml",)
    _assert_pinned(command, tmp_path, pinned)


def test_bun_options_env_is_non_durable(tmp_path: Path) -> None:
    _make(tmp_path, ("x.ts", "p.ts"))
    _assert_non_durable("BUN_OPTIONS=--preload=./p.ts bun x.ts", tmp_path, ("x.ts",))


# D. R --------------------------------------------------------------------------------


def test_rscript_pins_renviron(tmp_path: Path) -> None:
    _make(tmp_path, (".Renviron", "x.R"))
    _assert_pinned("Rscript x.R", tmp_path, (".Renviron", "x.R"))


@pytest.mark.parametrize("command", ["Rscript --vanilla x.R", "Rscript --no-environ x.R"])
def test_rscript_skipping_environ_does_not_pin_renviron(tmp_path: Path, command: str) -> None:
    _make(tmp_path, (".Renviron", "x.R"))
    _assert_pinned(command, tmp_path, ("x.R",))
    assert str((tmp_path / ".Renviron").resolve()) not in _pin_paths(command, tmp_path)


R_ENV_LOADERS = [
    "R_LIBS=/tmp/l Rscript -e 'library(x)'",
    "R_LIBS_USER=./l Rscript x.R",
    "R_LIBS_SITE=./l Rscript x.R",
]


@pytest.mark.parametrize("command", R_ENV_LOADERS)
def test_r_library_env_is_non_durable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.R", ".Rprofile"))
    _assert_non_durable(command, tmp_path, ("x.R",) if "x.R" in command else (".Rprofile",))


R_CODE_UNPINNABLE = [
    "Rscript -e \".libPaths('/tmp/l')\"",
    "Rscript -e \"eval(parse(file='x.R'))\"",
    "Rscript -e \"do.call(source, list('x.R'))\"",
]


@pytest.mark.parametrize("command", R_CODE_UNPINNABLE)
def test_r_dynamic_code_loading_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.R",))
    _assert_unpinnable(command, tmp_path)


# E. php ------------------------------------------------------------------------------

PHP_D_SPACED = [
    "php -d 'auto_prepend_file = ./p.php' x.php",
    "php -d 'auto_prepend_file=\"./p.php\"' x.php",
]


@pytest.mark.parametrize("command", PHP_D_SPACED)
def test_php_d_spaced_or_quoted_value_pins_file(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("p.php", "x.php"))
    _assert_pinned(command, tmp_path, ("p.php",))
    for ref in _plan(command, tmp_path).refs:
        assert '"' not in ref.operand and "'" not in ref.operand, ref
    for path in _pin_paths(command, tmp_path):
        assert '"' not in path and "'" not in path, path


def test_php_d_include_path_is_non_durable(tmp_path: Path) -> None:
    _make(tmp_path, ("x.php",))
    _assert_non_durable("php -d include_path=./lib x.php", tmp_path, ("x.php",))


PHP_UNPINNABLE = [
    "php -d extension=./e.so x.php",
    "php -d zend_extension=./z.so x.php",
    "php -z ./z.so x.php",
    "php -r \"set_include_path('/tmp'); require 'x.php';\"",
    "php -r \"ini_set('include_path','/tmp'); include 'x.php';\"",
]


@pytest.mark.parametrize("command", PHP_UNPINNABLE)
def test_php_extension_or_include_path_change_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.php",))
    _assert_unpinnable(command, tmp_path)


# F. lua ------------------------------------------------------------------------------

LUA_PATH_CODE = [
    "lua -e \"package.path='/tmp/?.lua;'..package.path; require 'x'\"",
    "lua -e \"package.cpath='/tmp/?.so'; require 'x'\"",
]


@pytest.mark.parametrize("command", LUA_PATH_CODE)
def test_lua_package_path_assignment_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.lua",))
    _assert_unpinnable(command, tmp_path)


# G. gawk -----------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["gawk -l ./ext x", "gawk --load=./ext -f x.awk"])
def test_gawk_load_extension_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.awk",))
    _assert_unpinnable(command, tmp_path)


def test_awklibpath_env_is_non_durable(tmp_path: Path) -> None:
    _make(tmp_path, ("x.awk",))
    _assert_non_durable("AWKLIBPATH=./l gawk -f x.awk", tmp_path, ("x.awk",))


def test_gawk_pins_at_include(tmp_path: Path) -> None:
    _write(tmp_path, "x.awk", '@include "lib.awk"\nBEGIN { print 1 }\n')
    _make(tmp_path, ("lib.awk",))
    _assert_pinned("gawk -f x.awk", tmp_path, ("x.awk", "lib.awk"))


def test_gawk_pins_nested_at_include(tmp_path: Path) -> None:
    _write(tmp_path, "x.awk", '@include "lib.awk"\n')
    _write(tmp_path, "lib.awk", '@include "inner.awk"\n')
    _make(tmp_path, ("inner.awk",))
    _assert_pinned("gawk -f x.awk", tmp_path, ("x.awk", "lib.awk", "inner.awk"))


def test_gawk_pins_at_include_of_dash_e_source(tmp_path: Path) -> None:
    _make(tmp_path, ("x.awk", "lib.awk"))
    _assert_pinned("gawk -e '@include \"lib.awk\"' -f x.awk", tmp_path, ("x.awk", "lib.awk"))


def test_gawk_at_include_cycle_terminates_and_pins_both(tmp_path: Path) -> None:
    _write(tmp_path, "a.awk", '@include "b.awk"\n')
    _write(tmp_path, "b.awk", '@include "a.awk"\n')
    _assert_pinned("gawk -f a.awk", tmp_path, ("a.awk", "b.awk"))


# H. deno heredoc ---------------------------------------------------------------------


def test_deno_heredoc_import_is_pinned(tmp_path: Path) -> None:
    _make(tmp_path, ("x.ts",))
    _assert_pinned("deno run - <<'EOF'\nimport './x.ts'\nEOF", tmp_path, ("x.ts",))


# I. loader env -----------------------------------------------------------------------

LOADER_ENV_MORE = [
    ("PERL5DB='BEGIN{}' perl -d x.pl", "x.pl"),
    ("TCLLIBPATH=./l tclsh x.tcl", "x.tcl"),
    ("TCL_LIBRARY=./l tclsh x.tcl", "x.tcl"),
]


@pytest.mark.parametrize(
    ("command", "script"), LOADER_ENV_MORE, ids=[p[0] for p in LOADER_ENV_MORE]
)
def test_more_loader_env_is_non_durable(tmp_path: Path, command: str, script: str) -> None:
    _make(tmp_path, (script,))
    _assert_non_durable(command, tmp_path, (script,))


# J. _SUSPECT_RE fallback (only used when tokenization fails) ------------------------
# An unterminated quote makes _tokenize return None. The two tests that would require the
# commit-cac808a outcome for `go to the store` / `I wish` were skipped: that outcome
# (not unpinnable) is less strict than the current one (go / wish are now suspect words).

UNTOKENIZABLE = "echo 'unterminated"


def test_suspect_shape_fails_tokenization() -> None:
    from shell_runner.write_targets import _tokenize

    assert _tokenize(UNTOKENIZABLE) is None
    assert _tokenize(f"{UNTOKENIZABLE} go run x.go") is None


def test_untokenizable_text_with_go_run_is_unpinnable(tmp_path: Path) -> None:
    _assert_unpinnable(f"{UNTOKENIZABLE} go run x.go", tmp_path)


# K. xargs feeding bun (#98) -----------------------------------------------------------
# xargs supplies the executed script/args from stdin, so the code run by bun or bunx is not
# statically knowable: refused (UNPINNABLE), never pinned or promoted as durable.

XARGS_BUN_UNPINNABLE = [
    "printf 'a.js\\n' | xargs bun",
    "printf 'a.js\\n' | xargs bun run",
    "printf 'a.js\\n' | xargs bunx foo",
]


@pytest.mark.parametrize("command", XARGS_BUN_UNPINNABLE)
def test_xargs_feeding_bun_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("a.js",))
    _assert_unpinnable(command, tmp_path)


# L. PR #111 review round (issue #98); deep config modelling is tracked in #127 ----------

# L1. versioned interpreter heads in the untokenizable-text fallback
SUSPECT_VERSIONED = [
    f"{UNTOKENIZABLE} {head} x"
    for head in ("php8.2", "ruby3.2", "perl5.36", "php", "ruby", "perl", "ruby3.2.2")
]


@pytest.mark.parametrize("command", SUSPECT_VERSIONED)
def test_untokenizable_text_with_versioned_interpreter_is_unpinnable(
    tmp_path: Path, command: str
) -> None:
    _assert_unpinnable(command, tmp_path)


# L2. php long options: only a short allowlist of harmless ones keeps a command pinned
PHP_LONG_UNPINNABLE = [
    "php --define auto_prepend_file=./p.php x.php",
    "php --define=auto_prepend_file=./p.php x.php",
    "php --zend-extension=./z.so x.php",
    "php --zend-extension ./z.so x.php",
    "php --extension=./e.so x.php",
    "php --extension ./e.so x.php",
    "php --unknown-option x.php",
    "php -f x.php --define=auto_prepend_file=./p.php",
]


@pytest.mark.parametrize("command", PHP_LONG_UNPINNABLE)
def test_php_unknown_long_option_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.php", "p.php"))
    _assert_unpinnable(command, tmp_path)


@pytest.mark.parametrize("command", ["php --version", "php --help", "php --ini", "php --info"])
def test_php_harmless_long_option_stays_unchanged(tmp_path: Path, command: str) -> None:
    _assert_unchanged(command, "true", tmp_path)


# L3. php keeps parsing options after `-r CODE` / `-f FILE`
def test_php_option_after_inline_code_is_collected(tmp_path: Path) -> None:
    _make(tmp_path, ("p.php",))
    _assert_pinned("php -r 'echo 1;' -d auto_prepend_file=./p.php", tmp_path, ("p.php",))


def test_php_option_after_file_flag_is_collected(tmp_path: Path) -> None:
    _make(tmp_path, ("x.php", "p.php"))
    _assert_pinned("php -f x.php -d auto_prepend_file=./p.php", tmp_path, ("x.php", "p.php"))
    _assert_pinned("php -F x.php -d auto_prepend_file=./p.php", tmp_path, ("x.php", "p.php"))


PHP_TRAILING_UNPINNABLE = [
    "php -f x.php -d extension=./e.so",
    "php -r 'echo 1;' -d zend_extension=./z.so",
    "php -r 'echo 1;' -z ./z.so",
    "php -F x.php -z ./z.so",
]


@pytest.mark.parametrize("command", PHP_TRAILING_UNPINNABLE)
def test_php_trailing_loader_flag_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.php",))
    _assert_unpinnable(command, tmp_path)


def test_php_script_arguments_are_not_options(tmp_path: Path) -> None:
    _make(tmp_path, ("x.php",))
    _assert_pinned("php x.php --define=auto_prepend_file=./p.php", tmp_path, ("x.php",))


# L4. luajit -j / -O load code: approve-once only (non-durable), never unpinnable
LUAJIT_NON_DURABLE = [
    "luajit -j off x.lua",
    "luajit -jdump x.lua",
    "luajit -O3 x.lua",
    "luajit -Ofoldl x.lua",
    "luajit -e 'print(1)' -j off x.lua",
    "luajit -j dump -l ./m x.lua",
]


@pytest.mark.parametrize("command", LUAJIT_NON_DURABLE)
def test_luajit_jit_and_opt_flags_are_non_durable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.lua", "m.lua"))
    pinned = ("x.lua", "m.lua") if "-l" in command else ("x.lua",)
    _assert_non_durable(command, tmp_path, pinned)
    _assert_approve_once_only(command, tmp_path)


def test_luajit_plain_script_stays_pinned(tmp_path: Path) -> None:
    _make(tmp_path, ("x.lua",))
    _assert_pinned("luajit x.lua", tmp_path, ("x.lua",))


# L5. R: ./.Renviron is scanned for loader variables
@pytest.mark.parametrize("var", ["R_PROFILE_USER", "R_ENVIRON_USER", "R_LIBS_USER", "R_LIBS"])
def test_renviron_loader_variable_is_non_durable(tmp_path: Path, var: str) -> None:
    _make(tmp_path, ("x.R",))
    _write(tmp_path, ".Renviron", f"{var}=./evil\n")
    _assert_non_durable("Rscript x.R", tmp_path, ("x.R", ".Renviron"))


def test_renviron_loader_variable_in_cd_base_is_non_durable(tmp_path: Path) -> None:
    _make(tmp_path, ("z/x.R",))
    _write(tmp_path, "z/.Renviron", "export R_PROFILE_USER=./evil\n")
    _assert_non_durable("cd z && Rscript x.R", tmp_path, ("z/x.R", "z/.Renviron"))


def test_renviron_without_loader_variable_stays_durable(tmp_path: Path) -> None:
    _make(tmp_path, ("x.R",))
    _write(tmp_path, ".Renviron", "MY_SETTING=1\n")
    _assert_pinned("Rscript x.R", tmp_path, ("x.R", ".Renviron"))
    assert _plan("Rscript x.R", tmp_path).non_durable == []


def test_renviron_loader_variable_ignored_with_vanilla(tmp_path: Path) -> None:
    _make(tmp_path, ("x.R",))
    _write(tmp_path, ".Renviron", "R_PROFILE_USER=./evil\n")
    assert _plan("Rscript --vanilla x.R", tmp_path).non_durable == []


# L6. bun: a `preload` key in the pinned bunfig.toml
@pytest.mark.parametrize(
    "bunfig", ['preload = ["./p.ts"]\n', "[test]\npreload = './p.ts'\n", 'PRELOAD="./p.ts"\n']
)
@pytest.mark.parametrize("command", ["bun x.ts", "bun run x.ts", "bun run build", "bun x cowsay"])
def test_bunfig_preload_is_unpinnable(tmp_path: Path, command: str, bunfig: str) -> None:
    _make(tmp_path, ("x.ts", "p.ts"))
    (tmp_path / "package.json").write_text('{"scripts": {"build": "echo hi"}}')
    _write(tmp_path, "bunfig.toml", bunfig)
    _assert_unpinnable(command, tmp_path)


def test_bunfig_without_preload_stays_pinned(tmp_path: Path) -> None:
    _make(tmp_path, ("x.ts",))
    _write(tmp_path, "bunfig.toml", "[install]\nregistry = 'https://r.example'\n")
    _assert_pinned("bun x.ts", tmp_path, ("x.ts", "bunfig.toml"))


# L7. literal native-library loads
NATIVE_LOADS = [
    "lua -e \"package.loadlib('./x.so', 'luaopen_x')\"",
    "lua -e \"local f = package.loadlib('./x.so', '*')\"",
    "Rscript -e \"dyn.load('./x.so')\"",
    "Rscript -e \"library.dynam('x', 'x', '.')\"",
    "php -r \"dl('x.so');\"",
]


@pytest.mark.parametrize("command", NATIVE_LOADS)
def test_literal_native_library_load_is_unpinnable(tmp_path: Path, command: str) -> None:
    _assert_unpinnable(command, tmp_path)


@pytest.mark.parametrize(
    "command", ["php -r \"echo 'idl(1)';\"", "lua -e \"print('loadlib')\"", "Rscript -e 'print(1)'"]
)
def test_inline_code_without_native_load_stays_unchanged(tmp_path: Path, command: str) -> None:
    assert _plan(command, tmp_path).unpinnable is False, command


# L8. a lua `require` name containing `=` is not a `g=mod` pair
def test_lua_require_name_with_equals_is_unpinnable(tmp_path: Path) -> None:
    _make(tmp_path, ("m.lua",))
    _assert_unpinnable("lua -e \"require 'g=m'\"", tmp_path)
    _assert_unpinnable("lua -e \"require('a=b')\"", tmp_path)


def test_lua_l_flag_still_splits_global_name(tmp_path: Path) -> None:
    _make(tmp_path, ("m.lua", "x.lua"))
    _assert_pinned("lua -l g=m x.lua", tmp_path, ("m.lua", "x.lua"))


# L9. gawk long options: only the exact full spellings
GAWK_LONG_UNPINNABLE = [
    "gawk --loa=./ext -f x.awk",
    "gawk --incl=lib.awk -f x.awk",
    "gawk --incl lib.awk -f x.awk",
    "gawk --lo ./ext -f x.awk",
    "gawk --unknown -f x.awk",
    "gawk --load=./ext -f x.awk",
]


@pytest.mark.parametrize("command", GAWK_LONG_UNPINNABLE)
def test_gawk_unknown_or_abbreviated_long_option_is_unpinnable(
    tmp_path: Path, command: str
) -> None:
    _make(tmp_path, ("x.awk", "lib.awk"))
    _assert_unpinnable(command, tmp_path)


@pytest.mark.parametrize(
    "command", ["gawk --posix -f x.awk", "gawk --file=x.awk", "gawk --field-separator=: -f x.awk"]
)
def test_gawk_known_long_options_stay_pinned(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.awk",))
    _assert_pinned(command, tmp_path, ("x.awk",))


# L10. more loader variables
def test_tk_library_env_is_non_durable(tmp_path: Path) -> None:
    _make(tmp_path, ("x.tcl",))
    _assert_non_durable("TK_LIBRARY=./l wish x.tcl", tmp_path, ("x.tcl",))


# L11. xargs / launchers around go and awk
XARGS_GO_AWK = [
    "printf 'a\\n' | xargs go build",
    "printf 'a\\n' | xargs go test",
    "printf 'a\\n' | xargs awk '{print}'",
    "printf 'a\\n' | xargs gawk '{print}'",
    "printf 'a\\n' | xargs mawk '{print}'",
    "printf 'a\\n' | xargs nawk '{print}'",
]


@pytest.mark.parametrize("command", XARGS_GO_AWK)
def test_xargs_feeding_go_or_awk_is_unpinnable(tmp_path: Path, command: str) -> None:
    _assert_unpinnable(command, tmp_path)


@pytest.mark.parametrize("command", ["watch go test ./...", "ssh host go version"])
def test_launcher_naming_go_is_unpinnable(tmp_path: Path, command: str) -> None:
    _assert_unpinnable(command, tmp_path)


# L12. quoted heredoc bodies for interpreters without a scanner
OPAQUE_HEREDOC = [
    "csh <<'EOF'\necho hi\nEOF",
    "tcsh <<'EOF'\necho hi\nEOF",
    "tclsh <<'EOF'\nputs hi\nEOF",
    "wish <<'EOF'\nputs hi\nEOF",
    "osascript <<'EOF'\nsay \"hi\"\nEOF",
]


@pytest.mark.parametrize("command", OPAQUE_HEREDOC)
def test_opaque_interpreter_heredoc_is_unpinnable(tmp_path: Path, command: str) -> None:
    _assert_unpinnable(command, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "perl <<'EOF'\nprint 1;\nEOF",
        "python3 <<'EOF'\nprint(1)\nEOF",
        "node <<'EOF'\nconsole.log(1)\nEOF",
        "ruby <<'EOF'\nputs 1\nEOF",
        "awk -f /dev/stdin <<'EOF'\nBEGIN { print 1 }\nEOF",
    ],
)
def test_scanned_language_heredoc_behaviour_is_unchanged(tmp_path: Path, command: str) -> None:
    assert _plan(command, tmp_path).unpinnable is False, command


# L13. shell expansion inside lua / R / php inline code
EXPANDED_INLINE = [
    "lua -e \"print('$HOME'); require 'x'\"",
    "Rscript -e \"x <- '$HOME'; source('x.R')\"",
    "php -r \"echo '$HOME'; require 'x.php';\"",
]


@pytest.mark.parametrize("command", EXPANDED_INLINE)
def test_expanded_inline_code_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.lua", "x.R", "x.php"))
    _assert_unpinnable(command, tmp_path)


def test_single_quoted_inline_code_with_dollar_stays_pinned(tmp_path: Path) -> None:
    _make(tmp_path, ("x.lua",))
    _assert_pinned('lua -e \'print("$HOME"); require "x"\'', tmp_path, ("x.lua",))


# L14. URL / stream wrapper operands of R source and php include/require
SCHEME_LOADS = [
    "Rscript -e \"source('http://example.com/x.R')\"",
    "Rscript -e \"source('https://example.com/x.R')\"",
    "Rscript -e \"source('file:///tmp/x.R')\"",
    "Rscript -e \"source('ftp://example.com/x.R')\"",
    "php -r \"include 'http://example.com/x.php';\"",
    "php -r \"require 'phar:///tmp/x.phar/a.php';\"",
    "php -r \"require_once 'data://text/plain,x';\"",
    "php -r \"include 'php://stdin';\"",
    "php -r \"include 'ftp://example.com/x.php';\"",
    "php -r \"include 'file:///tmp/x.php';\"",
    "php -d auto_prepend_file=data://text/plain,x x.php",
    "php -d auto_prepend_file=http://example.com/p.php x.php",
]


@pytest.mark.parametrize("command", SCHEME_LOADS)
def test_url_or_stream_wrapper_load_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.php",))
    _assert_unpinnable(command, tmp_path)


def test_colon_in_relative_path_is_not_a_scheme(tmp_path: Path) -> None:
    _make(tmp_path, ("a:b.php", "a:b.R"))
    _assert_pinned("php -r \"include './a:b.php';\"", tmp_path, ("a:b.php",))
    _assert_pinned("Rscript -e \"source('./a:b.R')\"", tmp_path, ("a:b.R",))
    assert not _plan("php -r \"include '/tmp/a:b.php';\"", tmp_path).unpinnable
    assert not _plan("php -r \"include 'C:/x.php';\"", tmp_path).unpinnable


# L15. bun: inline code is always scanned, whatever follows it
def test_bun_inline_code_followed_by_positional_is_scanned(tmp_path: Path) -> None:
    _make(tmp_path, ("x.js",))
    _assert_unpinnable('bun -e "require(foo)" build', tmp_path)
    _assert_unpinnable('bun --eval "require(foo)" install', tmp_path)
    _assert_unpinnable('bun -p "require(foo)" pm', tmp_path)
    _assert_pinned("bun -e \"require('./x.js')\" build", tmp_path, ("x.js",))
    _assert_pinned("bun --print=\"require('./x.js')\" help", tmp_path, ("x.js",))


# L16. bun exec
@pytest.mark.parametrize("command", ["bun exec ls", "bun exec 'echo hi'", "bun exec ./x.sh"])
def test_bun_exec_is_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.sh",))
    _assert_unpinnable(command, tmp_path)


# L17. bun: options after `run` that change config/cwd or are unknown: approve-once only
BUN_RUN_OPTIONS_NON_DURABLE = [
    "bun run --cwd ./other x.ts",
    "bun run --config ./c.toml x.ts",
    "bun run --env-file ./e x.ts",
    "bun run --tsconfig-override ./t.json x.ts",
    "bun run --unknown x.ts",
    "bun run --filter pkg build",
]


@pytest.mark.parametrize("command", BUN_RUN_OPTIONS_NON_DURABLE)
def test_bun_run_unknown_or_dir_changing_option_is_non_durable(
    tmp_path: Path, command: str
) -> None:
    _make(tmp_path, ("x.ts", "other/x.ts"))
    _assert_approve_once_only(command, tmp_path)
    # nothing is pinned: --cwd may make the files it names the wrong ones
    assert _plan(command, tmp_path).refs == []
    assert _pin_paths(command, tmp_path) == {}


def test_bun_option_before_subcommand_stays_unpinnable(tmp_path: Path) -> None:
    _make(tmp_path, ("x.ts",))
    _assert_unpinnable("bun --cwd ./other run x.ts", tmp_path)


def test_bun_run_known_flag_stays_pinned(tmp_path: Path) -> None:
    _make(tmp_path, ("x.ts",))
    _assert_pinned("bun run --silent x.ts", tmp_path, ("x.ts",))


# L18. awk @include traversal budget (shared by every awk invocation of a command)
def _fan_out(tmp_path: Path, main: str, count: int, prefix: str = "l") -> None:
    _write(tmp_path, main, "".join(f'@include "{prefix}{i}.awk"\n' for i in range(count)))
    _make(tmp_path, tuple(f"{prefix}{i}.awk" for i in range(count)))


def test_awk_include_fan_out_over_file_budget_is_unpinnable(tmp_path: Path) -> None:
    _fan_out(tmp_path, "main.awk", script_pin.MAX_FILES + 8)
    _assert_unpinnable("gawk -f main.awk", tmp_path)


def test_awk_include_fan_out_within_budget_is_pinned(tmp_path: Path) -> None:
    _fan_out(tmp_path, "main.awk", 10)
    _assert_pinned("gawk -f main.awk", tmp_path, ("main.awk", "l0.awk", "l9.awk"))


def test_awk_include_budget_is_shared_across_invocations(tmp_path: Path) -> None:
    half = script_pin.MAX_FILES // 2 + 4
    _fan_out(tmp_path, "a.awk", half, "a")
    _fan_out(tmp_path, "b.awk", half, "b")
    _assert_pinned("gawk -f a.awk", tmp_path, ("a.awk",))
    _assert_pinned("gawk -f b.awk", tmp_path, ("b.awk",))
    _assert_unpinnable("gawk -f a.awk; gawk -f b.awk", tmp_path)


def test_awk_include_name_count_over_budget_is_unpinnable(tmp_path: Path) -> None:
    count = script_pin.MAX_MISSING_FILES + 20
    _write(tmp_path, "main.awk", "".join(f'@include "m{i}.awk"\n' for i in range(count)))
    _assert_unpinnable("gawk -f main.awk", tmp_path)


def test_awk_include_byte_budget_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script_pin, "MAX_TOTAL_BYTES", 100)
    pad = "# " + "x" * 80 + "\n"
    _write(tmp_path, "main.awk", '@include "a.awk"\n@include "b.awk"\n')
    _write(tmp_path, "a.awk", pad)
    _write(tmp_path, "b.awk", pad)
    assert _plan("gawk -f main.awk", tmp_path).unpinnable is True


# L19. awk_path follows every existing candidate under every base
def test_awk_follows_every_base_candidate(tmp_path: Path) -> None:
    _write(tmp_path, "main.awk", "BEGIN { print 1 }\n")
    _write(tmp_path, "z/main.awk", '@include "inner.awk"\n')
    _write(tmp_path, "z/inner.awk", '@include "deep.awk"\n')
    _make(tmp_path, ("z/deep.awk",))
    _assert_pinned(
        "cd z && gawk -f main.awk", tmp_path, ("z/main.awk", "z/inner.awk", "z/deep.awk")
    )


def test_awk_load_in_shadowed_base_candidate_is_unpinnable(tmp_path: Path) -> None:
    _write(tmp_path, "main.awk", "BEGIN { print 1 }\n")
    _write(tmp_path, "z/main.awk", '@load "ext"\n')
    _assert_unpinnable("cd z && gawk -f main.awk", tmp_path)


# L20. gawk -E FILE / --exec=FILE / --exec FILE are program files
@pytest.mark.parametrize("command", ["gawk -E x.awk", "gawk --exec=x.awk", "gawk --exec x.awk"])
def test_gawk_exec_file_is_pinned_and_followed(tmp_path: Path, command: str) -> None:
    _write(tmp_path, "x.awk", '@include "lib.awk"\n')
    _write(tmp_path, "lib.awk", '@include "inner.awk"\n')
    _make(tmp_path, ("inner.awk",))
    _assert_pinned(command, tmp_path, ("x.awk", "lib.awk", "inner.awk"))


def test_gawk_exec_file_with_load_is_unpinnable(tmp_path: Path) -> None:
    _write(tmp_path, "x.awk", '@load "ext"\n')
    _assert_unpinnable("gawk -E x.awk", tmp_path)


# L21. gawk -W opt[=val] is --opt[=val]: same checks, same abbreviation refusal
GAWK_W_UNPINNABLE = [
    "gawk -W load=./ext.so -f x.awk",
    "gawk -Wload=./ext.so -f x.awk",
    "gawk -W load ./ext.so -f x.awk",
    "gawk -W loa=./ext.so -f x.awk",
    "gawk -Wloa=./ext.so -f x.awk",
    "gawk -W incl=lib.awk -f x.awk",
    "gawk -W unknown -f x.awk",
    "gawk -W",
    "gawk -f x.awk -W load=./ext.so",
    "gawk -W posix -W load=./ext.so -f x.awk",
    "gawk -bW load=./ext.so -f x.awk",
    "gawk -W exec=y.awk",  # y.awk loads an extension
    "gawk -W source='@load \"ext\"'",
    "gawk -W include=x -f x.awk",  # x.awk loads an extension
]


@pytest.mark.parametrize("command", GAWK_W_UNPINNABLE)
def test_gawk_dash_w_bypass_forms_are_unpinnable(tmp_path: Path, command: str) -> None:
    _write(tmp_path, "x.awk", '@load "ext"\n')
    _write(tmp_path, "y.awk", '@load "ext"\n')
    _make(tmp_path, ("lib.awk",))
    _assert_unpinnable(command, tmp_path)


@pytest.mark.parametrize(
    ("command", "pinned"),
    [
        ("gawk -W file=x.awk", ("x.awk", "lib.awk")),
        ("gawk -Wfile=x.awk", ("x.awk", "lib.awk")),
        ("gawk -W file x.awk", ("x.awk", "lib.awk")),
        ("gawk -W exec=x.awk", ("x.awk", "lib.awk")),
        ("gawk -W include=lib.awk -f z.awk", ("lib.awk", "z.awk")),
        ("gawk -W source='@include \"lib.awk\"'", ("lib.awk",)),
        ("gawk -W posix -f z.awk", ("z.awk",)),
        ("gawk -Wposix -W lint -f z.awk", ("z.awk",)),
    ],
)
def test_gawk_dash_w_acts_like_its_long_form(
    tmp_path: Path, command: str, pinned: tuple[str, ...]
) -> None:
    _write(tmp_path, "x.awk", '@include "lib.awk"\n')
    _make(tmp_path, ("lib.awk", "z.awk"))
    _assert_pinned(command, tmp_path, pinned)


@pytest.mark.parametrize(
    "command",
    [
        "gawk -W version",
        "gawk -W posix '{print}'",
        "gawk -W posix '{print}' -W load=data.txt",  # after the program: a data file
    ],
)
def test_gawk_harmless_dash_w_is_not_refused(tmp_path: Path, command: str) -> None:
    plan = _plan(command, tmp_path)
    assert plan.unpinnable is False, (command, plan.reasons)
    assert plan.non_durable == []


def test_gawk_dash_w_matches_long_form_refs(tmp_path: Path) -> None:
    _make(tmp_path, ("x.awk", "lib.awk"))
    for short, long in [
        ("gawk -W file=x.awk", "gawk --file=x.awk"),
        ("gawk -W exec=x.awk", "gawk --exec=x.awk"),
        ("gawk -W include=lib.awk -f x.awk", "gawk --include=lib.awk -f x.awk"),
    ]:
        assert _plan(short, tmp_path).refs == _plan(long, tmp_path).refs, short


def test_gawk_include_value_is_not_taken_for_the_program_text(tmp_path: Path) -> None:
    # `-i FILE` consumes FILE, so the inline program that follows is still scanned
    _write(tmp_path, "inc.awk", "# inc\n")
    _make(tmp_path, ("lib.awk",))
    _assert_pinned("gawk -i lib.awk '@include \"inc.awk\"'", tmp_path, ("lib.awk", "inc.awk"))
    _assert_pinned(
        "gawk --include=lib.awk '@include \"inc.awk\"'", tmp_path, ("lib.awk", "inc.awk")
    )


def test_gawk_exec_value_is_consumed_even_if_it_looks_like_an_option(tmp_path: Path) -> None:
    # `-E -l` names a program file called `-l` (getopt takes the next word as the value)
    _make(tmp_path, ("-l",))
    plan = _plan("gawk -E -l", tmp_path)
    assert plan.unpinnable is False, plan.reasons
    assert any(r.operand == "-l" for r in plan.refs)
    _assert_unpinnable("gawk -E x.awk -l ./ext", tmp_path)


def test_gawk_include_flag_value_is_not_a_program_file(tmp_path: Path) -> None:
    _make(tmp_path, ("x.awk", "lib.awk"))
    plan = _plan("gawk -i lib.awk -f x.awk", tmp_path)
    assert plan.unpinnable is False
    assert {r.operand for r in plan.refs if r.script} == {"x.awk", "lib.awk"}


# --- PR #111 review round 3 --------------------------------------------------------------


def _paths(command: str, tmp_path: Path) -> set[str]:
    root = str(tmp_path.resolve())
    return {p.removeprefix(root + "/") for p in _pin_paths(command, tmp_path)}


# R1. lua `require` / `-l` search ./?.lua and ./?/init.lua, never the bare stem
@pytest.mark.parametrize("command", ["lua -e \"require('m')\"", "lua -l m n.lua"])
def test_lua_module_lookup_pins_lua_files_not_the_bare_stem(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("m", "m.lua", "n.lua"))
    paths = _paths(command, tmp_path)
    assert "m.lua" in paths, paths
    assert "m" not in paths, paths


def test_lua_package_directory_is_pinned_through_init(tmp_path: Path) -> None:
    _make(tmp_path, ("m/init.lua",))
    _assert_pinned("lua -e \"require('m')\"", tmp_path, ("m/init.lua",))


def test_lua_dofile_keeps_the_exact_path(tmp_path: Path) -> None:
    _make(tmp_path, ("m",))
    _assert_pinned("lua -e \"dofile('m')\"", tmp_path, ("m",))


# R2. bun resolves extensionless imports and preloads to TypeScript too
@pytest.mark.parametrize(
    "command",
    [
        "bun -e \"require('./x')\"",
        "bun -e \"import('./x')\"",
        "bun --preload ./x y.ts",
        "bun -r ./x y.ts",
    ],
)
def test_bun_extensionless_local_import_pins_typescript(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x.ts", "y.ts"))
    _assert_pinned(command, tmp_path, ("x.ts",))


def test_bun_pins_every_existing_candidate(tmp_path: Path) -> None:
    _make(tmp_path, ("x.ts", "x.js"))
    _assert_pinned("bun -e \"require('./x')\"", tmp_path, ("x.ts", "x.js"))


def test_node_import_resolution_is_unchanged(tmp_path: Path) -> None:
    _make(tmp_path, ("x.ts",))
    assert "x.ts" not in _paths("node -e \"require('./x')\"", tmp_path)


# R3. php -c: a file of any name, a directory's php-cli.ini before php.ini, loader keys refused
def test_php_c_file_without_ini_suffix_is_pinned(tmp_path: Path) -> None:
    _make(tmp_path, ("custom.conf", "x.php"))
    _assert_pinned("php -c custom.conf x.php", tmp_path, ("custom.conf", "x.php"))


def test_php_c_directory_prefers_php_cli_ini(tmp_path: Path) -> None:
    _make(tmp_path, ("conf/php-cli.ini", "conf/php.ini", "x.php"))
    _assert_pinned("php -c conf x.php", tmp_path, ("conf/php-cli.ini", "x.php"))


def test_php_c_directory_falls_back_to_php_ini(tmp_path: Path) -> None:
    _make(tmp_path, ("conf/php.ini", "x.php"))
    _assert_pinned("php -c conf x.php", tmp_path, ("conf/php.ini", "x.php"))


@pytest.mark.parametrize(
    "ini",
    [
        "auto_prepend_file=./hook.php\n",
        "auto_append_file = './hook.php'\n",
        "extension=./hook.so\n",
        'zend_extension = "opcache"\n',
        "[PHP]\n  Extension=hook\n",
    ],
)
@pytest.mark.parametrize("command", ["php -c p.ini x.php", "php -c conf x.php"])
def test_php_ini_loader_keys_are_unpinnable(tmp_path: Path, command: str, ini: str) -> None:
    _make(tmp_path, ("x.php",))
    _write(tmp_path, "p.ini", ini)
    _write(tmp_path, "conf/php.ini", ini)
    _assert_unpinnable(command, tmp_path)


@pytest.mark.parametrize(
    "ini",
    ["memory_limit=1G\n", "; extension=hook\nauto_prepend_file =\n", "display_errors=1\n"],
)
def test_php_ini_without_loader_keys_stays_pinned(tmp_path: Path, ini: str) -> None:
    _make(tmp_path, ("x.php",))
    _write(tmp_path, "p.ini", ini)
    _assert_pinned("php -c p.ini x.php", tmp_path, ("p.ini", "x.php"))


# R4. `bun run NAME.ext`: a package script of that name may take precedence over the file
def test_explicit_bun_run_pins_package_manifest_next_to_the_file(tmp_path: Path) -> None:
    _make(tmp_path, ("x.ts",))
    _write(tmp_path, "package.json", '{"scripts": {"x.ts": "echo hi"}}')
    _assert_pinned("bun run x.ts", tmp_path, ("x.ts", "package.json"))
