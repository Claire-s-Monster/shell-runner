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
    ("lua -e \"require 'x'\"", {"x", "x.lua", "x/init.lua"}),
    ("lua -l socket y.lua", {"socket", "socket.lua", "socket/init.lua", "y.lua"}),
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


@pytest.mark.parametrize("command", ["go build ./...", "go test ./..."])
def test_go_build_and_test_keep_current_outcome(tmp_path: Path, command: str) -> None:
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
]


@pytest.mark.parametrize("command", RUNNERS_UNPINNABLE)
def test_test_runners_and_generators_are_unpinnable(tmp_path: Path, command: str) -> None:
    _make(tmp_path, ("x_test.ts", "x.test.ts", "main.go"))
    _assert_unpinnable(command, tmp_path)


@pytest.mark.parametrize("command", ["go test ./...", "go build ./..."])
def test_go_test_and_build_category_is_current(tmp_path: Path, command: str) -> None:
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
