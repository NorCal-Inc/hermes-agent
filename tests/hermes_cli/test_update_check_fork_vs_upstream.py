"""``hermes update --check`` must not report fork divergence as staleness.

Regression cover for the NorCal production fork (kanban task t_de41461b).
``_cmd_update_check`` used to prefer ``upstream/main`` as its compare ref
whenever an ``upstream`` remote existed. On a fork that deliberately diverges
from Nous, that made the headline "commits behind" figure measure the whole
fork-vs-Nous delta — 16,140 commits on the live checkout, measured 2026-09-21,
against an origin-relative delta of 0. A fully current install reported a
five-figure update backlog.

The contract these tests pin:

(a) the actionable "behind" figure is origin-relative, because ``hermes update``
    installs from origin;
(b) the Nous-upstream delta may still be surfaced, but only as clearly
    labelled review material — never sharing a line (or a reading) with the
    word "behind";
(c) ``banner.py`` keeps comparing against ``origin/main`` only, upstream remote
    or not.
"""

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.main import cmd_update

FORK_ORIGIN = "git@github-hermes-agent:NorCal-Inc/hermes-agent.git"
OFFICIAL_ORIGIN = "https://github.com/NousResearch/hermes-agent.git"


def _git_side_effect(
    *,
    origin_url=FORK_ORIGIN,
    has_upstream=True,
    origin_behind="0",
    upstream_behind="16140",
    fork_only="165",
    upstream_fetch_ok=True,
    is_shallow=False,
    head_sha="head123",
    origin_sha="origin123",
):
    """Drive the ``_cmd_update_check`` git pipeline off canned counts.

    Defaults reproduce the live NorCal production checkout: current with its
    own origin, enormously diverged from Nous, carrying its own commits.
    """

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        if "remote get-url origin" in joined:
            rc = 0 if origin_url else 1
            return subprocess.CompletedProcess(cmd, rc, stdout=f"{origin_url or ''}\n", stderr="")
        if "remote get-url upstream" in joined:
            return subprocess.CompletedProcess(cmd, 0 if has_upstream else 1, stdout="", stderr="")
        if "--is-shallow-repository" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=("true\n" if is_shallow else "false\n"), stderr="")
        if "fetch" in joined and "upstream" in joined:
            rc = 0 if upstream_fetch_ok else 128
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="fatal: no upstream\n")
        if "fetch" in joined and "origin" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "rev-parse" in joined and "--verify" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if is_shallow and joined.endswith("rev-parse HEAD"):
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{head_sha}\n", stderr="")
        if is_shallow and joined.endswith("rev-parse origin/main"):
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{origin_sha}\n", stderr="")
        if "rev-list" in joined:
            if "HEAD..origin/" in joined:
                count = origin_behind
            elif "HEAD..upstream/main" in joined:
                count = upstream_behind
            elif "upstream/main..origin/main" in joined:
                count = fork_only
            else:
                raise AssertionError(f"unexpected rev-list range: {joined}")
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{count}\n", stderr="")

        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return side_effect


def _run_check(mock_run, **kwargs):
    mock_run.side_effect = _git_side_effect(**kwargs)
    cmd_update(SimpleNamespace(check=True, branch=None))


def _commands(mock_run):
    return [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]


def _behind_lines(out):
    """Every output line that states a staleness verdict."""
    return [ln for ln in out.splitlines() if "behind" in ln.lower()]


# ---------------------------------------------------------------- (a) actionable


@patch("hermes_cli.config.detect_install_method", return_value="git")
@patch("subprocess.run")
def test_diverged_fork_reports_origin_currency_not_upstream_divergence(
    mock_run, _method, capsys
):
    """Current-with-origin + hugely diverged from Nous ⇒ "up to date"."""
    _run_check(mock_run, origin_behind="0", upstream_behind="16140", fork_only="165")

    out = capsys.readouterr().out
    assert "Already up to date." in out
    # The headline verdict must never have been derived from upstream.
    assert not any("16140" in ln for ln in _behind_lines(out)), out
    assert "behind upstream/main" not in out, out

    # The compare ref genuinely is origin: verify + the verdict rev-list.
    verify = [c for c in _commands(mock_run) if "rev-parse" in c and "--verify" in c]
    assert verify and all("origin/main" in c for c in verify), verify
    assert any("rev-list HEAD..origin/main" in c for c in _commands(mock_run))


@patch("hermes_cli.config.detect_install_method", return_value="git")
@patch("subprocess.run")
def test_behind_count_is_the_origin_relative_one(mock_run, _method, capsys):
    """When origin IS ahead, the number printed is origin's, not upstream's."""
    _run_check(mock_run, origin_behind="3", upstream_behind="16140", fork_only="165")

    out = capsys.readouterr().out
    behind = _behind_lines(out)
    assert any("3 commits behind origin/main" in ln for ln in behind), out
    assert not any("16140" in ln for ln in behind), out
    assert "to install" in out


# ------------------------------------------------------- (b) review, not backlog


@patch("hermes_cli.config.detect_install_method", return_value="git")
@patch("subprocess.run")
def test_nous_divergence_is_surfaced_but_labelled_as_review_only(
    mock_run, _method, capsys
):
    from hermes_cli.update_cmd import UPSTREAM_REVIEW_TOOL, UPSTREAM_REVIEW_TOOL_REPO

    _run_check(mock_run, origin_behind="0", upstream_behind="16140", fork_only="165")

    out = capsys.readouterr().out
    # Surfaced...
    assert "16140" in out
    assert "upstream/main" in out
    # ...but disclaimed, and never as a behind-count.
    assert "NOT an update backlog" in out
    assert "relevance review" in out
    assert not any("16140" in ln for ln in _behind_lines(out)), out
    # And pointed at the out-of-band review path, not at `hermes update`.
    assert UPSTREAM_REVIEW_TOOL in out
    assert "will not install these" in out
    # The script lives in a different repo. Naming it as a bare relative path
    # would send the reader hunting for a file that is not in this checkout,
    # so the repo must be named alongside it.
    assert UPSTREAM_REVIEW_TOOL_REPO in out
    assert "not this checkout" in out


@patch("hermes_cli.config.detect_install_method", return_value="git")
@patch("subprocess.run")
def test_undiverged_fork_upstream_delta_is_reported_as_actionable(
    mock_run, _method, capsys
):
    """A fork carrying no commits of its own DOES get upstream fast-forwarded.

    ``_sync_with_upstream_if_needed`` only skips when the fork has local
    commits, so with ``fork_only == 0`` the upstream delta really is pending
    work and must not be disclaimed away.
    """
    _run_check(mock_run, origin_behind="0", upstream_behind="12", fork_only="0")

    out = capsys.readouterr().out
    assert "12 commits on upstream/main" in out
    assert "NOT an update backlog" not in out
    assert "fast-forward" in out


@patch("hermes_cli.config.detect_install_method", return_value="git")
@patch("subprocess.run")
def test_no_upstream_remote_leaves_output_origin_only(mock_run, _method, capsys):
    _run_check(mock_run, has_upstream=False, origin_behind="2")

    out = capsys.readouterr().out
    assert "2 commits behind origin/main" in out
    assert "upstream" not in out.lower()
    assert not any("fetch" in c and "upstream" in c for c in _commands(mock_run))


@patch("hermes_cli.config.detect_install_method", return_value="git")
@patch("subprocess.run")
def test_official_checkout_skips_the_fork_review_entirely(mock_run, _method, capsys):
    """Origin == Nous: there is no fork, so there is nothing to disclaim."""
    _run_check(mock_run, origin_url=OFFICIAL_ORIGIN, origin_behind="0")

    out = capsys.readouterr().out
    assert "Already up to date." in out
    assert "review" not in out.lower()


@patch("hermes_cli.config.detect_install_method", return_value="git")
@patch("subprocess.run")
def test_unreachable_upstream_does_not_fail_the_check(mock_run, _method, capsys):
    """The advisory line is best-effort; a dead upstream must not break --check."""
    _run_check(mock_run, upstream_fetch_ok=False, origin_behind="0")

    out = capsys.readouterr().out
    assert "Already up to date." in out
    assert "NOT an update backlog" not in out


@patch("hermes_cli.config.detect_install_method", return_value="git")
@patch("subprocess.run")
def test_non_main_branch_never_consults_upstream(mock_run, _method, capsys):
    mock_run.side_effect = _git_side_effect(origin_behind="1")
    cmd_update(SimpleNamespace(check=True, branch="bb/gui"))

    out = capsys.readouterr().out
    assert "upstream" not in out.lower()
    assert not any("upstream" in c for c in _commands(mock_run) if "fetch" in c)




@patch("hermes_cli.banner._github_compare_behind", return_value=0)
@patch("hermes_cli.config.detect_install_method", return_value="git")
@patch("subprocess.run")
def test_shallow_fork_still_reports_nous_review_state(
    mock_run, _method, _compare, capsys
):
    """Shallow installs must not return before the separate review-only line."""
    _run_check(
        mock_run,
        is_shallow=True,
        head_sha="local-tip",
        origin_sha="origin-tip",
        upstream_behind="40",
        fork_only="7",
    )

    out = capsys.readouterr().out
    assert "Already up to date." in out
    assert "40 commits on upstream/main" in out
    assert "NOT an update backlog" in out
    assert not any("40" in ln for ln in _behind_lines(out)), out

# ------------------------------------------------------------ (c) banner is origin-only


def test_banner_git_state_stays_origin_only_when_an_upstream_remote_exists(tmp_path):
    """The startup banner compares HEAD to origin/main — upstream is irrelevant.

    This is the path that was already correct; pin it so the fix above can't
    drift into it. ``fake_run`` refuses any command naming ``upstream/`` as a
    ref, so a regression fails loudly rather than reporting a wrong number.
    """
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    (repo_dir / ".git").mkdir(parents=True)

    results = {
        ("git", "rev-parse", "--short=8", "origin/main"): MagicMock(returncode=0, stdout="b2f477a3\n"),
        ("git", "rev-parse", "--short=8", "HEAD"): MagicMock(returncode=0, stdout="af8aad31\n"),
        ("git", "rev-list", "--count", "origin/main..HEAD"): MagicMock(returncode=0, stdout="165\n"),
    }

    def fake_run(cmd, **kwargs):
        key = tuple(cmd)
        if any("upstream/" in str(part) for part in cmd):
            raise AssertionError(f"banner must not consult upstream refs: {cmd}")
        if key not in results:
            raise AssertionError(f"unexpected command: {cmd}")
        return results[key]

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        state = banner.get_git_banner_state(repo_dir)

    # 165 commits carried on top of origin/main — ahead, not behind.
    assert state == {"upstream": "b2f477a3", "local": "af8aad31", "ahead": 165}


def test_banner_local_git_count_uses_origin_main_for_a_fork(tmp_path):
    """``_check_via_local_git`` on a fork counts HEAD..origin/main, full stop."""
    from hermes_cli import banner

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    def fake_run(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "remote get-url origin" in joined:
            return MagicMock(returncode=0, stdout=f"{FORK_ORIGIN}\n")
        if "--is-shallow-repository" in joined:
            return MagicMock(returncode=0, stdout="false\n")
        if "fetch" in joined:
            assert "upstream" not in joined, joined
            return MagicMock(returncode=0, stdout="")
        if "rev-list" in joined:
            assert "HEAD..origin/main" in joined, joined
            return MagicMock(returncode=0, stdout="0\n")
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        assert banner._check_via_local_git(repo_dir) == 0
