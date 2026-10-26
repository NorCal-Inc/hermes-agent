"""Regression tests for the completion/checkpoint acceptance-criterion ban.

Root cause (t_734e14f9): kanban_specify.py's generated "Acceptance
criteria" section could include a line describing board completion or
checkpoint-advancement mechanics (e.g. "card marked done", "checkpoint
advances"). Under Gauntlet enforcement, every independent verifier checks
acceptance criteria literally, and verification always runs BEFORE
completion -- so a criterion describing the completed/checkpointed state
can never be true at verification time. That produced a structurally
unmeetable criterion and a permanent FAIL/block loop (see t_dbe6b067,
verifier t_8676bb75: PASS on all 4 real operational criteria, FAIL only on
the "card marked done and checkpoint advances" line).

First fix (t_734e14f9): the specifier's system prompt forbids generating
any completion/checkpoint-mechanics acceptance criterion. That fix is
advisory only -- the LLM can still emit the forbidden pattern on a bad
day, reproducing the same block-loop/attempt-budget-exhaustion failure.

Hardening fix (t_adee1291, this file): a deterministic, code-level filter
(``_strip_unmeetable_acceptance_criteria`` in ``hermes_cli.kanban_specify``)
now strips any acceptance-criteria line matching board-completion/
checkpoint-advancement mechanics BEFORE the card is ever persisted,
regardless of what the LLM produced. The tests below feed
``specify_task`` a fake LLM response that deliberately violates the
prompt instruction and assert the violating line never reaches the
persisted card body -- proving the guard works even when the prompt
instruction fails.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_specify as spec
from hermes_cli.kanban_specify import (
    _SYSTEM_PROMPT,
    _strip_unmeetable_acceptance_criteria,
)

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


# ---------------------------------------------------------------------------
# Static system-prompt contract (prior attempt's 3 tests -- unchanged
# assertions, kept passing by this attempt per the task's requirement #4).
# ---------------------------------------------------------------------------


def test_system_prompt_forbids_completion_checkpoint_criteria():
    """The prompt must explicitly tell the model not to emit a
    completion/checkpoint-advancement acceptance criterion."""
    prompt = _SYSTEM_PROMPT.lower()

    # The rule must mention the two concrete phrasings that caused the
    # original incident (t_dbe6b067's unmeetable 5th criterion).
    assert "card marked done" in prompt
    assert "checkpoint advances" in prompt

    # And it must explain *why*: verification precedes completion, so
    # such a criterion is unmeetable at verification time.
    assert "unmeetable" in prompt
    assert "verification" in prompt


def test_system_prompt_acceptance_criteria_section_present():
    """Sanity: the acceptance-criteria section itself still exists (the
    new rule must live inside it, not float disconnected)."""
    assert "**Acceptance criteria**" in _SYSTEM_PROMPT


def test_no_literal_forbidden_criterion_line_in_prompt_examples():
    """The rule text itself must not accidentally read as an example
    acceptance-criteria line a naive substring scan would approve, i.e.
    it must be phrased as a prohibition, not a template line."""
    # Pull just the bullet that introduces the rule.
    match = re.search(
        r"- Acceptance criteria describe.*?construction, stalling the task forever\.",
        _SYSTEM_PROMPT,
        re.DOTALL,
    )
    assert match, "expected the completion/checkpoint prohibition bullet to be present verbatim"
    bullet = match.group(0)
    assert "write a criterion like" in bullet.lower()


# ---------------------------------------------------------------------------
# Mechanical guard: unit-level coverage of the filter function itself.
# ---------------------------------------------------------------------------


_VIOLATING_BODY = """**Goal**
Ship the thing.

**Approach**
- Do the work.
- Test the work.

**Acceptance criteria**
- The thing actually works end to end.
- Card marked done and checkpoint advances.
- All existing tests still pass.

**Out of scope**
- Unrelated refactors.
"""


def test_guard_strips_card_marked_done_line():
    cleaned, removed = _strip_unmeetable_acceptance_criteria(_VIOLATING_BODY, "t_fake")
    assert removed == ["- Card marked done and checkpoint advances."]
    assert "card marked done" not in cleaned.lower()
    assert "checkpoint advances" not in cleaned.lower()
    # The real criteria around it must survive untouched.
    assert "The thing actually works end to end." in cleaned
    assert "All existing tests still pass." in cleaned
    assert "**Goal**" in cleaned and "**Out of scope**" in cleaned


@pytest.mark.parametrize(
    "line",
    [
        "- Card marked done.",
        "- Task is marked done.",
        "- Ticket flagged complete.",
        "- Mark the card done.",
        "- Marking the task complete.",
        "- Checkpoint advances once this lands.",
        "- The checkpoint advances.",
        "- Card reaches done.",
        "- Task moves to done.",
        # Variants reported by independent verifier t_0daf626b as missed by
        # the prior attempt's patterns (FAIL: "explicit board-completion
        # variants such as 'Task completed.', 'The card is complete.', and
        # 'Ticket transitions to complete.' remain persistable").
        "- Task completed.",
        "- The card is complete.",
        "- Ticket transitions to complete.",
        "- Card done.",
        "- Task is complete.",
        # Variants reported by verifier t_b6e46baa: modal completion,
        # close/closed/resolved terminology, checkpoint progress/progression.
        "- The task must be completed.",
        "- Close the card after verification.",
        "- The ticket is closed.",
        "- The checkpoint progresses.",
        "- Checkpoint progression is recorded.",
        "- The ticket should be resolved.",
        "- Resolve the ticket.",
        "- The card gets closed automatically.",
        "- The task will be finished.",
        "- Progress the checkpoint.",
        # Variants reported by independent verifier t_df958275: the subject
        # noun set was missing "board", "issue", "pull request", and "PR"
        # entirely, so lifecycle-mechanics criteria about those artifacts
        # survived. These are the four exact misses it named.
        "- The issue is resolved.",
        "- Finish the pull request after review.",
        "- The board transitions to closed.",
        "- The PR moves to done.",
    ],
)
def test_guard_catches_forbidden_phrasing_variants(line):
    body = f"**Acceptance criteria**\n{line}\n- A real criterion stays.\n"
    cleaned, removed = _strip_unmeetable_acceptance_criteria(body, "t_fake")
    assert removed, f"expected guard to catch: {line!r}"
    assert "A real criterion stays." in cleaned


# ---------------------------------------------------------------------------
# Concept-matrix coverage (t_adee1291, repair after t_df958275's FAIL):
# cross a complete subject-noun set (card/task/ticket/checkpoint/board/
# issue/pull request/PR) against the lifecycle-verb set, including fresh
# combinations no prior verifier named individually, to prove this is a
# generalizing concept match rather than another literal-phrase list that
# only covers previously-reported strings.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "- The board is closed.",
        "- The issue should be closed.",
        "- Resolve the board.",
        "- Finish the ticket.",
        "- The PR must be completed.",
        "- Close the issue after merge.",
        "- The board moves to done.",
        "- The pull request transitions to resolved.",
    ],
)
def test_guard_catches_fresh_unseen_subject_verb_combinations(line):
    """None of these exact strings was named by any prior verifier --
    they're synthesized here to prove the matcher generalizes across the
    full subject x lifecycle-verb matrix instead of only recognizing the
    specific examples that have already been reported."""
    body = f"**Acceptance criteria**\n{line}\n- A real criterion stays.\n"
    cleaned, removed = _strip_unmeetable_acceptance_criteria(body, "t_fake")
    assert removed, f"expected guard to catch previously-unseen combo: {line!r}"
    assert "A real criterion stays." in cleaned


@pytest.mark.parametrize(
    "line",
    [
        # Real, checkable outcomes about the artifact's content or a
        # concrete external action taken on it -- not board-state mechanics
        # -- must survive even though they contain a subject noun.
        "- The bug is fixed.",
        "- The known issue list is empty.",
        "- No new issues introduced.",
        "- PR #12 merged to main after review.",
        "- The PR is merged.",
        "- The pull request author is notified.",
        "- Issues reproduced in staging first.",
        # Words that merely contain "board"/"pr"/"issue" as a substring,
        # not as the standalone artifact-noun token.
        "- The dashboard displays real-time data.",
        "- The onboard flow works.",
        "- Keyboard shortcuts respond correctly.",
        "- Approach documented clearly.",
        "- Pricing displayed correctly.",
        "- The print job finishes.",
    ],
)
def test_guard_does_not_false_positive_on_widened_subject_set(line):
    """Widening the subject-noun set to board/issue/pull request/PR must
    not start catching legitimate, checkable criteria about those same
    artifacts, or unrelated words that merely contain the substrings."""
    body = f"**Acceptance criteria**\n{line}\n- A real criterion stays.\n"
    cleaned, removed = _strip_unmeetable_acceptance_criteria(body, "t_fake")
    assert removed == [], f"expected guard to leave alone: {line!r}, but stripped {removed!r}"
    assert line.lstrip("- ").rstrip(".") in cleaned or line[2:] in cleaned


def test_guard_leaves_non_acceptance_sections_alone():
    """Completion-adjacent wording outside the Acceptance criteria section
    is narration, not a literal checklist item a verifier tests -- the
    guard must not mangle unrelated prose."""
    body = (
        "**Goal**\n"
        "Once this card is marked done, operators stop seeing the alert.\n\n"
        "**Acceptance criteria**\n"
        "- A real, verifiable criterion.\n"
    )
    cleaned, removed = _strip_unmeetable_acceptance_criteria(body, "t_fake")
    assert removed == []
    assert cleaned == body


def test_guard_leaves_generic_completion_language_untouched():
    """Generic, verifiable completion language ('the migration completes
    without errors') must survive -- only board/card/checkpoint mechanics
    are forbidden."""
    body = (
        "**Acceptance criteria**\n"
        "- The migration completes without errors.\n"
        "- The job runs to completion in under 2 minutes.\n"
        "- Task completion metrics are logged to the dashboard.\n"
        "- The ticketing integration test suite passes.\n"
    )
    cleaned, removed = _strip_unmeetable_acceptance_criteria(body, "t_fake")
    assert removed == []
    assert cleaned == body


def test_guard_passthrough_on_empty_or_none_body():
    assert _strip_unmeetable_acceptance_criteria(None, "t_fake") == (None, [])
    assert _strip_unmeetable_acceptance_criteria("", "t_fake") == ("", [])


# ---------------------------------------------------------------------------
# End-to-end: specify_task() with a fake LLM reply that violates the
# prompt instruction -- proves the guard runs on the real persistence
# path, not just in isolation.
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_llm_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def test_specify_task_strips_violation_before_persisting(kanban_home, monkeypatch):
    """The LLM "fails to follow the system prompt instruction" on purpose
    here -- the mechanical guard must still keep the violating line out of
    the persisted card."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rough idea", triage=True)

    import agent.auxiliary_client as aux

    violating_reply = json.dumps(
        {
            "title": "Do the thing",
            "body": _VIOLATING_BODY,
        }
    )
    monkeypatch.setattr(aux, "call_llm", lambda *a, **k: _fake_llm_response(violating_reply))

    out = spec.specify_task(tid, author="claude")

    assert out.ok is True
    assert out.stripped_criteria == ["- Card marked done and checkpoint advances."]

    with kb.connect_closing() as conn:
        persisted = kb.get_task(conn, tid)
    assert persisted.status in ("todo", "ready")
    # The violating line must never reach the persisted body.
    assert "card marked done" not in (persisted.body or "").lower()
    assert "checkpoint advances" not in (persisted.body or "").lower()
    # The legitimate criteria around it must still be there.
    assert "The thing actually works end to end." in persisted.body
    assert "All existing tests still pass." in persisted.body


def test_specify_task_clean_response_unaffected(kanban_home, monkeypatch):
    """Guard must be a no-op (and not report any stripped criteria) when
    the LLM actually follows the prompt instruction."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rough idea", triage=True)

    import agent.auxiliary_client as aux

    clean_body = (
        "**Goal**\nDo the thing.\n\n"
        "**Acceptance criteria**\n- The thing works end to end.\n"
    )
    clean_reply = json.dumps({"title": "Do the thing", "body": clean_body})
    monkeypatch.setattr(aux, "call_llm", lambda *a, **k: _fake_llm_response(clean_reply))

    out = spec.specify_task(tid, author="claude")

    assert out.ok is True
    assert out.stripped_criteria == []
    with kb.connect_closing() as conn:
        persisted = kb.get_task(conn, tid)
    assert persisted.body == clean_body


def test_specify_task_fallback_raw_body_also_guarded(kanban_home, monkeypatch):
    """When the LLM reply isn't valid JSON, specify_task falls back to
    treating the whole reply as the body (existing behavior) -- the guard
    must still apply to that fallback path."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rough idea", triage=True)

    import agent.auxiliary_client as aux

    # Not JSON -- triggers the fallback branch in specify_task.
    monkeypatch.setattr(aux, "call_llm", lambda *a, **k: _fake_llm_response(_VIOLATING_BODY))

    out = spec.specify_task(tid, author="claude")

    assert out.ok is True
    assert out.stripped_criteria == ["- Card marked done and checkpoint advances."]
    with kb.connect_closing() as conn:
        persisted = kb.get_task(conn, tid)
    assert "card marked done" not in (persisted.body or "").lower()


def test_specify_task_strips_modal_close_checkpoint_variants_before_persisting(
    kanban_home, monkeypatch
):
    """Verifier t_b6e46baa variants must not reach the persisted card on
    either the parsed-JSON or raw-fallback path."""
    import agent.auxiliary_client as aux

    body = (
        "**Goal**\nDo the thing.\n\n"
        "**Acceptance criteria**\n"
        "- The task must be completed.\n"
        "- Close the card after verification.\n"
        "- The ticket is closed.\n"
        "- The checkpoint progresses.\n"
        "- The migration completes without errors.\n"
    )
    for reply in (json.dumps({"title": "Do the thing", "body": body}), body):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="rough idea", triage=True)
        monkeypatch.setattr(aux, "call_llm", lambda *a, **k: _fake_llm_response(reply))
        out = spec.specify_task(tid, author="claude")
        assert out.ok is True
        assert len(out.stripped_criteria) == 4
        with kb.connect_closing() as conn:
            persisted = kb.get_task(conn, tid)
        low = (persisted.body or "").lower()
        assert "must be completed" not in low
        assert "close the card" not in low
        assert "is closed" not in low
        assert "checkpoint progresses" not in low
        assert "The migration completes without errors." in persisted.body


def test_specify_task_strips_board_issue_pr_variants_before_persisting(
    kanban_home, monkeypatch
):
    """Verifier t_df958275's four explicitly-named gaps (board/issue/pull
    request/PR subjects missing from the matcher) must not reach the
    persisted card on either the parsed-JSON or raw-fallback path, while a
    legitimate PR-outcome criterion in the same body survives."""
    import agent.auxiliary_client as aux

    body = (
        "**Goal**\nDo the thing.\n\n"
        "**Acceptance criteria**\n"
        "- The issue is resolved.\n"
        "- Finish the pull request after review.\n"
        "- The board transitions to closed.\n"
        "- The PR moves to done.\n"
        "- PR #12 merged to main after review.\n"
    )
    for reply in (json.dumps({"title": "Do the thing", "body": body}), body):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="rough idea", triage=True)
        monkeypatch.setattr(aux, "call_llm", lambda *a, **k: _fake_llm_response(reply))
        out = spec.specify_task(tid, author="claude")
        assert out.ok is True
        assert len(out.stripped_criteria) == 4
        with kb.connect_closing() as conn:
            persisted = kb.get_task(conn, tid)
        low = (persisted.body or "").lower()
        assert "issue is resolved" not in low
        assert "finish the pull request" not in low
        assert "transitions to closed" not in low
        assert "pr moves to done" not in low
        # The legitimate, checkable PR-merge criterion must survive.
        assert "PR #12 merged to main after review." in persisted.body


@pytest.mark.parametrize("line", [
    "The task progresses.",
    "The board advances.",
    "The issue transitions after verification.",
    "The pull request progression is recorded.",
    "The PR advanced after review.",
    "Ticket status transitions to closed.",
])
def test_forbidden_lifecycle_stem_variants(line):
    from hermes_cli.kanban_specify import _is_forbidden_acceptance_line
    assert _is_forbidden_acceptance_line(line)


@pytest.mark.parametrize("line", [
    "Signup flow advances to the payment step.",
    "Dashboard progress bar renders.",
    "The issues list loads.",
])
def test_lifecycle_stem_false_positives_survive(line):
    from hermes_cli.kanban_specify import _is_forbidden_acceptance_line
    assert not _is_forbidden_acceptance_line(line)


# ---------------------------------------------------------------------------
# Exhaustive Cartesian-product matrix (t_adee1291, final repair 2026-10-26
# after independent verifier t_7d9d1978's FAIL): "The test file contains six
# selected lifecycle cases, not the required generated exhaustive matrix."
# and "an 8-subject x 22-form probe caught 131/176 and missed 45".
#
# This generates every (subject noun) x (lifecycle verb-stem, grammatical
# form) combination programmatically -- not a curated list a human picked --
# in both common sentence shapes ("The <subject> <form>." and "The
# <subject> <form> after verification.") and asserts every single one is
# caught by the live guard. Replacing this matrix with hand-picked examples
# again would silently narrow the acceptance bar back down; keep it
# generated.
# ---------------------------------------------------------------------------

_MATRIX_SUBJECTS = [
    "card", "task", "ticket", "checkpoint", "board", "issue", "pull request", "PR",
]

# Every lifecycle stem crossed with its common grammatical forms (base/
# -s/-ed/-ing, plus modal/aux-prefixed forms where natural English allows
# them). This is the generative inventory the production regex's suffix
# tolerance (complet(e|es|ed|ing), etc.) is supposed to cover in full.
_MATRIX_FORMS = [
    "completes", "completed", "completing", "is complete", "is completed",
    "must be completed",
    "closes", "closed", "closing", "is closed", "is closing",
    "resolves", "resolved", "resolving", "is resolved",
    "finishes", "finished", "finishing", "is finished",
    "progresses", "progressed", "progressing", "progression is recorded",
    "advances", "advanced", "advancing",
    "transitions", "transitioned", "transitioning",
    "proceeds", "proceeded", "proceeding",
    "done",
]


def _generate_matrix_lines():
    for subject in _MATRIX_SUBJECTS:
        for form in _MATRIX_FORMS:
            yield f"The {subject} {form}."
            yield f"The {subject} {form} after verification."


@pytest.mark.parametrize("line", list(_generate_matrix_lines()))
def test_exhaustive_subject_lifecycle_matrix_is_caught(line):
    """Every generated (subject x lifecycle-form) combination -- not just
    the specific sentences any prior verifier happened to name -- must be
    recognized as an unmeetable board-mechanics criterion."""
    from hermes_cli.kanban_specify import _is_forbidden_acceptance_line
    assert _is_forbidden_acceptance_line(line), f"matrix combination not caught: {line!r}"


@pytest.mark.parametrize("line", [
    # Imperative/gerund shape crossed over the same subject x stem matrix,
    # covering "Close the card after verification." word order too.
    "Close the card.",
    "Resolve the ticket.",
    "Finish the pull request.",
    "Complete the task.",
    "Progress the checkpoint.",
    "Advance the board.",
    "Transition the issue.",
    "Proceed the PR.",
])
def test_exhaustive_imperative_matrix_is_caught(line):
    from hermes_cli.kanban_specify import _is_forbidden_acceptance_line
    assert _is_forbidden_acceptance_line(line), f"imperative combination not caught: {line!r}"


@pytest.mark.parametrize("line", [
    "The bug is fixed.",
    "The known issue list is empty.",
    "No new issues introduced.",
    "PR #12 merged to main after review.",
    "The PR is merged.",
    "The pull request author is notified.",
    "Issues reproduced in staging first.",
    "The dashboard displays real-time data.",
    "The onboard flow works.",
    "Keyboard shortcuts respond correctly.",
    "Approach documented clearly.",
    "Pricing displayed correctly.",
    "The print job finishes.",
    "The migration completes without errors.",
    "The job runs to completion in under 2 minutes.",
    "Task completion metrics are logged to the dashboard.",
    "The ticketing integration test suite passes.",
    "Signup flow advances to the payment step.",
    "Dashboard progress bar renders.",
    "The issues list loads.",
    "The task goes smoothly.",
    "The build goes green.",
    "The user goes to checkout.",
    "The config updates to v2.",
    "The settings are set correctly.",
    "The flag is set to true.",
    "The threshold is reached.",
    "The deadline is reached by Friday.",
])
def test_exhaustive_matrix_adjacent_false_positives_survive(line):
    """Widening the matcher to a full generative matrix must not start
    catching legitimate, checkable criteria that merely share a word with
    the subject-noun or movement-verb vocabulary."""
    from hermes_cli.kanban_specify import _is_forbidden_acceptance_line
    assert not _is_forbidden_acceptance_line(line), f"false positive: {line!r}"


# ---------------------------------------------------------------------------
# Noun-form lifecycle vocabulary ("completion", "closure", "resolution",
# "progression", "transition" as nouns, not verbs) -- found by an
# independent probe reproducing verifier t_7d9d1978's methodology beyond
# the 22 forms it enumerated: "Checkpoint completion is recorded." and
# "The ticket's closure is logged." passed the original 8x22 matrix (which
# used verb-suffix forms only) but were not covered by any pattern until
# this noun-form pattern was added. Covered by its own dedicated pattern
# because a bare "<subject> <noun-form-stem>" (e.g. "Task completion
# metrics...") must NOT be forbidden -- only when the noun form is itself
# the clause's subject, immediately followed by a state-describing verb.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", [
    "Checkpoint completion is recorded.",
    "The ticket's closure is logged.",
    "Ticket resolution confirmed.",
    "Checkpoint closure happens automatically.",
    "The issue's resolution is documented.",
    "Card completion is confirmed.",
    "Board transition is noted.",
])
def test_noun_form_lifecycle_state_is_caught(line):
    from hermes_cli.kanban_specify import _is_forbidden_acceptance_line
    assert _is_forbidden_acceptance_line(line), f"noun-form variant not caught: {line!r}"


@pytest.mark.parametrize("line", [
    # "completion"/"closure"/etc. as a noun ADJUNCT modifying a different
    # noun (metrics, rate, time) must survive -- it describes the real
    # deliverable's measurable property, not the artifact's own lifecycle
    # state.
    "Task completion metrics are logged to the dashboard.",
    "Ticket closure rate improves week over week.",
    "Issue resolution time is under 4 hours.",
])
def test_noun_form_adjunct_usage_survives(line):
    from hermes_cli.kanban_specify import _is_forbidden_acceptance_line
    assert not _is_forbidden_acceptance_line(line), f"false positive on adjunct usage: {line!r}"
