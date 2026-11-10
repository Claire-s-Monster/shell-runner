"""Issue #85: awk, sed and git commands that run code or write files must not auto-execute.

The normalizer collapses an awk/sed script to `<arg>`, so the T1/T2 catalog rules cannot
see it; the checks run on the raw tokens (shell_runner.exec_guard). `catalog tier` below is
`_classify_catalog(...)`, i.e. the tier before the guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shell_runner import executor
from shell_runner.catalog import Tier
from shell_runner.classifier import PERMISSIVENESS, _classify_catalog, classify

AGENT = "some-agent"
T3, T4 = Tier.APPROVE_ONCE, Tier.ALWAYS_APPROVE


@pytest.fixture(autouse=True)
def _roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "_CWD_ROOTS", [tmp_path.resolve()])
    monkeypatch.setenv("SHELL_RUNNER_CONFIG_DIR", str(tmp_path / "no-rules"))


def _cls(command: str, cwd: Path):  # noqa: ANN202
    return classify(command, str(cwd), AGENT, env={"HOME": "/nonexistent-home"})


def _assert_t3_or_stricter(command: str, cwd: Path) -> None:
    cls = _cls(command, cwd)
    assert PERMISSIVENESS[cls.command_tier] <= PERMISSIVENESS[T3], command
    assert cls.path_guarded, command  # a template approval must not unlock the variant


def _assert_unchanged(command: str, cwd: Path) -> None:
    cls = _cls(command, cwd)
    assert cls.command_tier == _classify_catalog(command, str(cwd), AGENT).command_tier, command
    assert not cls.path_guarded, command


# --- awk -------------------------------------------------------------------

AWK_T4 = [
    "awk 'BEGIN{system(\"id\")}'",
    "awk '{system($1)}' f",
    "awk 'BEGIN { system (\"x\") }' f",
    "awk -F: '{system($1)}' f",
    "gawk 'BEGIN{system(\"id\")}'",
    "awk -e 'BEGIN{system(\"id\")}'",
]

AWK_T3 = [
    "awk '{print > \"f\"}' f",
    "awk '{print >> \"f\"}' f",
    "awk '{print | \"sh\"}' f",
    "awk '{print |& \"sh\"}' f",
    "awk 'BEGIN{\"date\" | getline d}'",
    "awk 'BEGIN{getline x < \"f\"}'",
    "awk '{getline}' f",
    "awk '{close(\"f\")}' f",
    "awk '{fflush()}' f",
    "awk -f prog.awk f",
    "awk -fprog.awk f",
    "awk --file=prog.awk f",
    "awk --file prog.awk f",
    "awk --fil=prog.awk f",
    "awk -F: -f prog.awk f",
    "awk -i inplace '{print}' f",
    "awk --include x '{print}' f",
    "awk -l ext '{print}' f",
    "awk --load=ext '{print}' f",
    "awk -E prog.awk f",
    "awk --exec prog.awk f",
    "awk -e '{print > \"x\"}' f",
    "awk '@load \"x\"' f",
    "awk '$1 > 5' f",
]


@pytest.mark.parametrize("command", AWK_T4)
def test_awk_system_is_t4(tmp_path: Path, command: str) -> None:
    cls = _cls(command, tmp_path)
    assert cls.command_tier == T4, command
    assert cls.path_guarded


@pytest.mark.parametrize("command", AWK_T3)
def test_awk_file_or_pipe_or_include_is_t3(tmp_path: Path, command: str) -> None:
    _assert_t3_or_stricter(command, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "awk '{print $1}' f",
        "awk -F: '{print $2}' f",
        "awk '{print $1, $2}'",
        "awk -v n=3 '{print $n}' f",
        "awk 'NR==2{print}' f",
        "awk 'BEGIN{print 1+1}'",
    ],
)
def test_awk_positive_shapes_keep_their_tier(tmp_path: Path, command: str) -> None:
    _assert_unchanged(command, tmp_path)


def test_awk_in_a_pipeline_is_checked(tmp_path: Path) -> None:
    assert _cls("cat f | awk 'BEGIN{system(\"id\")}'", tmp_path).command_tier == T4
    _assert_t3_or_stricter("cat f | awk '{print > \"x\"}'", tmp_path)


# --- sed -------------------------------------------------------------------

SED_T3 = [
    "sed e",
    "sed 'e id' f",
    "sed '1e id' f",
    "sed 's/a/b/e' f",
    "sed 's/a/b/ge' f",
    "sed 's/a/b/;e id' f",
    "sed -e 's/a/b/e' f",
    "sed -i 's/a/b/e' f",
    "sed -i 'w out' f",
    "sed 'w out' f",
    "sed -n 'w out' f",
    "sed 's/a/b/w out' f",
    "sed 'W out' f",
    "sed 'r x' f",
    "sed 'R x' f",
    "sed -e p -e 'w x' f",
    "sed --expression='w x' f",
    "sed --expression 'w x' f",
    "sed -es/a/b/w f",
    "sed -f s.sed f",
    "sed -fs.sed f",
    "sed -nf s.sed f",
    "sed --file=s.sed f",
    "sed --file s.sed f",
    "sed -s p f1 f2",
    "sed -ns p f1 f2",
    "sed --separate p f1 f2",
    "sed -z p f",
    "sed -nz p f",
    "sed --null-data p f",
    "sed '{p}' f",
    "sed 'a text' f",
    "sed 'i text' f",
    "sed 'c text' f",
    "sed 'x;G' f",
    "sed '#n' f",
    "sed 's/a/b' f",
    "sed 's/a/b/q' f",
    "sed '1,5' f",
    "sed 'y/a/b' f",
    "sed 'p p' f",
    "sed 'unknownoption' f",
    "sed --bogus p f",
]


@pytest.mark.parametrize("command", SED_T3)
def test_sed_unsafe_or_unparseable_is_t3(tmp_path: Path, command: str) -> None:
    _assert_t3_or_stricter(command, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "sed 's/a/b/g' f",
        "sed 's/a/b/' f",
        "sed 's/a/b/gI' f",
        "sed 's/a/b/2p' f",
        "sed 's|a|b|' f",
        "sed 's,a\\,b,c,' f",
        "sed -n '1,5p' f",
        "sed -n '/foo/p' f",
        "sed -n '/a\\/b/p' f",
        "sed -n '$p' f",
        "sed -n '$=' f",
        "sed '2d' f",
        "sed '/x/!d' f",
        "sed 5q f",
        "sed 'q5' f",
        "sed -e 's/a/b/' -e '2d' f",
        "sed --expression='s/a/b/' f",
        "sed -E 's/(a)/\\1/' f",
        "sed 's/a/b/;s/c/d/' f",
        "sed '$!N;s/\\n/ /' f",
        "sed 'y/abc/xyz/' f",
        "sed -n 'n;p' f",
        "sed -i 's/a/b/' f",
        "sed -i.bak 's/a/b/' f",
    ],
)
def test_sed_positive_shapes_keep_their_tier(tmp_path: Path, command: str) -> None:
    _assert_unchanged(command, tmp_path)


def test_sed_in_a_pipeline_is_checked(tmp_path: Path) -> None:
    _assert_t3_or_stricter("cat f | sed 's/a/b/e'", tmp_path)
    _assert_unchanged("cat f | sed 's/a/b/'", tmp_path)


# --- git -------------------------------------------------------------------

GIT_T3 = [
    # transport / exec options (every form, and unique prefixes)
    "git fetch --upload-pack=x origin",
    "git fetch --upload-pack x origin",
    "git fetch --upload-p=x origin",
    "git fetch --upl x origin",
    "git fetch --receive-pack=x origin",
    "git ls-remote --upload-pack=x origin",
    "git fetch -u x origin",
    "git ls-remote -u x origin",
    "git log --exec=x",
    "git log --e=x",
    "git log --output=o",
    "git log --output o",
    "git show --outp=o HEAD",
    "git diff --ext-diff",
    "git diff --ext-d",
    "git diff --textconv",
    "git show --textconv HEAD:f",
    "git show --tex HEAD:f",
    "git -c core.pager=x log",
    "git -ccore.pager=x log",
    "git log -c core.pager=x",
    "git --config-env=core.pager=X log",
    "git --config-e=core.pager=X log",
    "git --git-dir=/x log",
    "git --git-dir /x log",
    "git --work-tree=/x status",
    "git --namespace=x log",
    "git --names=x log",
    # remote
    "git remote add x url",
    "git remote set-url x y",
    "git remote rename a b",
    "git remote remove x",
    "git remote rm x",
    "git remote set-head origin -a",
    "git remote set-branches origin x",
    "git remote prune origin",
    "git remote update",
    "git remote -v add x y",
    "git remote show",
    "git remote get-url",
    "git remote show origin extra",
    # branch / tag
    "git branch -d x",
    "git branch -D x",
    "git branch -f x",
    "git branch --force x",
    "git branch -fd x",
    "git branch -m a b",
    "git branch -M a b",
    "git branch --move a b",
    "git branch --del x",
    "git branch --set-upstream-to=o/x",
    "git branch --set-upstream-to o/x",
    "git branch -u o/x",
    "git branch --edit-description",
    "git tag -d v1",
    "git tag -f v1",
    "git tag --force v1",
    "git tag -m msg v1",
    "git tag -u key v1",
    # stash
    "git stash drop",
    "git stash clear",
    "git stash pop",
    "git stash apply",
    "git stash push",
    "git stash push -m x",
    "git stash save x",
    "git stash",
    "git stash branch b",
]


@pytest.mark.parametrize("command", GIT_T3)
def test_git_dangerous_forms_are_t3(tmp_path: Path, command: str) -> None:
    _assert_t3_or_stricter(command, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "git log --oneline -5",
        "git diff HEAD~1 -- src/",
        "git show HEAD:README.md",
        "git fetch origin",
        "git fetch --all --prune",
        "git branch -a",
        "git branch -vv",
        "git branch",
        "git branch -r",
        "git branch --list",
        "git tag",
        "git tag -l",
        "git tag -n5",
        "git remote",
        "git remote -v",
        "git remote show origin",
        "git remote get-url origin",
        "git stash list",
        "git stash show",
        "git status",
        "git status -sb",
        "git diff --cached",
        "git diff --no-ext-diff",
        "git diff --name-only",
        "git log --graph --name-status",
        "git log --word-diff",
        "git log --color",
        "git log --exit-code",
        "git -C sub status",
        "git rev-parse HEAD",
        "git describe --tags",
    ],
)
def test_git_positive_shapes_keep_their_tier(tmp_path: Path, command: str) -> None:
    _assert_unchanged(command, tmp_path)


def test_git_hazards_in_a_chain_are_checked(tmp_path: Path) -> None:
    _assert_t3_or_stricter("git status && git fetch --upload-pack=x origin", tmp_path)
    _assert_t3_or_stricter("ls | xargs echo; git remote add x y", tmp_path)


def test_git_output_to_file_is_a_write_target(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    cls = _cls("git log --output=.git/config", tmp_path)
    assert cls.flagged
    assert cls.matched_rule is not None
    assert cls.matched_rule.pattern == "path_guard:git_dir_write"
