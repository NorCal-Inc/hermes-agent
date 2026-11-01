"""Kanban triage specifier — flesh out a one-liner into a real spec.

``hermes kanban specify [task_id | --all]`` asks the auxiliary LLM for a
tightened title + concrete body for a Triage task, then flips it
``triage -> todo`` via ``kanban_db.specify_triage_task``.

Mirrors ``hermes_cli/goals.py``: same aux-client pattern, same "empty config
=> skip, don't crash" tolerance. One shot, no retry loop. JSON mode is not
requested (works on providers without it); the parse is lenient and falls
back to "whole reply is the body" so a malformed reply never strands a task.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

from utils import env_int

HERMES_KANBAN_SPECIFY_MAX_TOKENS = max(1500, env_int("HERMES_KANBAN_SPECIFY_MAX_TOKENS", 6000))

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Kanban triage specifier for the Hermes Agent board.
A user dropped a rough idea into the Triage column. Your job is to turn it
into a concrete, actionable task spec that an autonomous worker can pick up
and execute without further clarification.

Output a single JSON object with exactly two keys:

  {
    "title": "<tightened task title, <= 80 chars, imperative voice>",
    "body":  "<multi-line spec, see structure below>"
  }

The body MUST include these sections, each prefixed with a bold markdown
heading, in this order:

  **Goal** — one sentence, user-facing outcome.
  **Approach** — 2-5 bullets on how a worker should tackle it.
  **Acceptance criteria** — checklist of concrete, verifiable conditions.
  **Out of scope** — short list of things NOT to touch (omit if nothing
      obvious; never invent scope creep).

Rules:
  - Keep the tightened title close in meaning to the original idea — do
    NOT invent a different project.
  - If the original idea is already detailed, preserve its substance and
    just reformat into the sections above.
  - Never add invented requirements the user didn't hint at.
  - Acceptance criteria describe the WORK RESULT, never the board
    mechanics of closing the card. Every board under the Gauntlet
    lifecycle requires independent verification BEFORE a task can reach
    'done'; verification itself checks the acceptance criteria. So never
    write a criterion like "card marked done", "checkpoint advances",
    "task completed", or any paraphrase of the act of completion/
    checkpoint-advancement itself — that criterion can never be true at
    verification time (the card is still in review) and is unmeetable by
    construction, stalling the task forever. If the source material says
    something like "mark the card done to advance the checkpoint", treat
    that as operational narration to drop, not a requirement to carry
    into the checklist.
  - No preamble, no closing remarks, no code fences around the JSON.
  - Output only the JSON object and nothing else.
"""


_USER_TEMPLATE = """Task id: {task_id}
Current title: {title}
Current body:
{body}
"""


@dataclass
class SpecifyOutcome:
    """Result of specifying a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    new_title: Optional[str] = None
    stripped_criteria: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.stripped_criteria is None:
            self.stripped_criteria = []


# ---------------------------------------------------------------------------
# Mechanical guard against unmeetable completion/checkpoint acceptance
# criteria (t_adee1291, hardening t_734e14f9's advisory-only prompt fix).
#
# t_734e14f9 added the system-prompt rule above telling the specifier LLM
# not to emit a criterion describing board-completion/checkpoint-advancement
# mechanics (e.g. "card marked done", "checkpoint advances"). That rule is
# advisory: the LLM can still emit the forbidden pattern on a bad day, and
# when it does, the resulting criterion is unmeetable by construction
# (verification always runs before completion, so "card marked done" can
# never be true when an independent verifier checks it) -- producing the
# exact permanent block-loop/attempt-budget-exhaustion failure seen on
# t_dbe6b067 and t_50312b1e.
#
# This guard is a deterministic, code-level filter that runs on every
# generated body regardless of what the LLM produced, and removes any
# acceptance-criteria line matching board-completion/checkpoint-advancement
# mechanics BEFORE the card is ever persisted. It is intentionally scoped to
# lines inside the "**Acceptance criteria**" section only -- the same
# phrasing appearing in "Goal"/"Approach"/"Out of scope" prose is narration,
# not a checklist item a verifier will literally test, so it is left alone.
# ---------------------------------------------------------------------------

_SECTION_HEADING_RE = re.compile(r"^\s*\*\*(.+?)\*\*\s*:?\s*$")

# Each pattern targets board/card/checkpoint *mechanics* vocabulary, not
# generic completion language -- "the migration completes without errors"
# or "task runs to completion in 2 minutes" must survive untouched; only a
# criterion describing the act of marking the card/task/ticket done, or the
# checkpoint advancing, is unmeetable and gets stripped.
#
# ---------------------------------------------------------------------------
# True Cartesian-product matcher (t_adee1291, final repair 2026-10-26 after
# five whack-a-mole verifier cycles each closing only the specific
# sentences a verifier happened to name). Per Christopher/Erika direction:
# this must be ONE matcher built from an exhaustive subject-noun x
# lifecycle-verb-stem cross product with grammatical-suffix tolerance, not
# another curated sentence list. Verifier t_7d9d1978's final FAIL named 45
# of 176 probed combinations as unmatched -- all traceable to specific
# grammatical forms (bare gerund "completing"/"closing"/"resolving"/
# "finishing" with no "is"/modal in front, and "progression is recorded"
# word-order) that the old per-pattern approach never generalized across.
#
# Design:
#   SUBJECT_NOUNS: the board-artifact itself, never the real deliverable.
#     card | task | ticket | checkpoint | board | issue | pull request | PR
#     Matched with \b...\b so "dashboard"/"keyboard"/"onboard" and plural
#     "issues" never match the bare noun.
#   LIFECYCLE_VERB_STEM: every grammatical form (base/-s/-ed/-ing, plus the
#     bare adjective "done") of every lifecycle stem, via suffix-tolerant
#     regex groups instead of enumerating full words per form:
#     complet-, clos-, resolv-, finish-, progress-, advanc-, transition-,
#     proceed-, increment-, done.
#   GLUE: a bounded whitelist of connector words (modal/aux/adverb/
#     determiner/"after X"/"once X") that may sit between subject and verb
#     in either order, so a verifier cannot find a new adjacency variant --
#     "<subject> GLUE? <stem>" and "<stem> GLUE? <subject>" both fire,
#     covering "The task must be completed." and "Close the card after
#     verification." with the same two regexes.
#   Movement-to-terminal-state: "move/go/update" are only forbidden when
#     paired with BOTH a subject noun AND a terminal-state word (done,
#     complete[d], closed, resolved, finished, forward, ahead, "to the
#     next") in the same line -- catches "The PR moves to done." and "Move
#     the checkpoint forward." without flagging ordinary prose like "The
#     task goes smoothly." or "The config updates to v2." which contain the
#     verb but no terminal-state word.
#
# This generalizes by construction: any future subject noun or stem added
# to the two sets is automatically crossed against the other set and both
# word orders, with every grammatical suffix already tolerated -- there is
# no per-sentence-shape pattern left to miss a verifier can find by trying
# a new tense or word order on an already-covered subject/stem pair.
_SUBJECT_NOUNS = r"(?:card|task|ticket|checkpoint|board|issue|pull request|PR)"

_LIFECYCLE_VERB_STEM = (
    r"(?:"
    r"complet(?:e|es|ed|ing)|"
    r"clos(?:e|es|ed|ing)|"
    r"resolv(?:e|es|ed|ing)|"
    r"finish(?:|es|ed|ing)|"
    r"progress\w*|"
    r"advanc\w*|"
    r"transition\w*|"
    r"proceed\w*|"
    r"increment\w*|"
    r"done"
    r")"
)

_TERMINAL_STATE = (
    r"(?:done|complete[d]?|closed?|resolved?|finished|forward|ahead|on|to\s+the\s+next)"
)

_GLUE = (
    r"(?:\s+(?:must|should|shall|will|can|is|are|be|been|being|get|gets|then|also|"
    r"finally|successfully|automatically|has|have|had|as|the|a|status|state|lifecycle|"
    r"out|this|that|its|each|after\s+\w+|once\s+\w+)\b){0,6}"
)

# Verbs describing externally-driven state transitions of the artifact:
# moving/going/updating it, or someone marking/flagging/setting/reaching
# it into a terminal state. These only fire when paired with BOTH a
# subject noun AND a terminal-state word in the same line (see the two
# patterns below), so ordinary prose like "the task goes smoothly" or "the
# deadline is reached" never matches on its own.
_MOVE_VERB = (
    r"(?:mov(?:e|es|ed|ing)|go(?:es|ing)?|updat(?:e|es|ed|ing)|"
    r"mark(?:ed|ing|s)?|flag(?:ged|ging|s)?|reach(?:es|ed|ing)?|sets?)"
)

# Noun forms of the lifecycle stems ("Checkpoint completion is recorded.",
# "The ticket's closure is logged.") -- only forbidden when the noun form
# is immediately followed by a state-describing verb (is/are/happens/
# confirmed/documented/recorded/logged/...), so a noun-adjunct modifier
# before an unrelated noun ("Task completion metrics are logged to the
# dashboard.") still survives: "completion" there modifies "metrics", not
# the subject's own state.
_NOUN_FORM_STEM = r"(?:completion|closure|resolution|progression|transition)"
_NOUN_FORM_STATE_VERB = (
    r"(?:is|are|was|were|happens?|occurs?|confirmed|documented|recorded|logged|noted)"
)

_FORBIDDEN_ACCEPTANCE_LINE_PATTERNS = [
    # <subject> GLUE? <lifecycle stem>  e.g. "The task must be completed.",
    # "Card done.", "The checkpoint progresses.", "The PR advanced after
    # review.", "Checkpoint progression is recorded."
    re.compile(
        r"\b" + _SUBJECT_NOUNS + r"\b" + _GLUE + r"\s*\b" + _LIFECYCLE_VERB_STEM + r"\b",
        re.IGNORECASE,
    ),
    # <lifecycle stem> GLUE? <subject>  e.g. "Close the card after
    # verification.", "Resolve the ticket.", "Finish the pull request after
    # review.", "Progress the checkpoint."
    re.compile(
        r"\b" + _LIFECYCLE_VERB_STEM + r"\b" + _GLUE + r"\s*\b" + _SUBJECT_NOUNS + r"\b",
        re.IGNORECASE,
    ),
    # <subject> GLUE? move/go/update GLUE? <terminal state>  e.g. "The PR
    # moves to done.", "The board moves to done."
    re.compile(
        r"\b" + _SUBJECT_NOUNS + r"\b" + _GLUE + r"\s*\b" + _MOVE_VERB + r"\b(?:\s+to)?"
        + _GLUE + r"\s*\b" + _TERMINAL_STATE + r"\b",
        re.IGNORECASE,
    ),
    # move/go/update GLUE? <subject> GLUE? <terminal state>  e.g. "Move the
    # checkpoint forward."
    re.compile(
        r"\b" + _MOVE_VERB + r"\b" + _GLUE + r"\s*\b" + _SUBJECT_NOUNS + r"\b" + _GLUE
        + r"\s*\b" + _TERMINAL_STATE + r"\b",
        re.IGNORECASE,
    ),
    # <subject>('s)? ... <noun-form stem> <state verb>  e.g. "Checkpoint
    # completion is recorded.", "The ticket's closure is logged.",
    # "Ticket resolution confirmed." Does NOT fire on "Task completion
    # metrics are logged to the dashboard." because "metrics" sits between
    # the noun form and the verb there, breaking the required adjacency.
    re.compile(
        r"\b" + _SUBJECT_NOUNS + r"'?s?\b(?:\s+\w+){0,2}?\s*\b" + _NOUN_FORM_STEM
        + r"\b\s*\b" + _NOUN_FORM_STATE_VERB + r"\b",
        re.IGNORECASE,
    ),
]


# False-positive guard (t_5683eec1, follow-up to t_adee1291): a line that
# matches the lifecycle-mechanics vocabulary above is only unmeetable when
# it describes NOTHING but the board-artifact's own state transition. If
# the author tacked on a real, observable outcome -- "The issue is
# resolved: login works on Safari." -- the line is a legitimate, checkable
# criterion and must survive even though "issue ... resolved" matches the
# bare lifecycle pattern.
#
# Detection is two-part and deliberately narrow: (1) a connector token
# (colon, dash, "and", "so", "with") must appear after the matched
# lifecycle phrase, AND (2) the text after that connector must itself name
# something concretely checkable (a number, a URL, a file-looking token, or
# a named test/behaviour word) -- not just more lifecycle narration. Both
# parts are required so "Card marked done and checkpoint advances." still
# gets stripped: it has the connector "and", but what follows ("checkpoint
# advances") is itself bare lifecycle narration with no concrete marker, so
# the line stays forbidden.
_CHECKABLE_CONNECTOR_RE = re.compile(
    r":|(?<=\s)-(?=\s)|[\u2013\u2014]|\band\b|\bso\b|\bwith\b",
    re.IGNORECASE,
)

_CHECKABLE_MARKER_RE = re.compile(
    r"https?://|www\.|\d|\.\w{2,4}\b|\bfile\b|\burl\b|"
    r"\btest(?:s|ed|ing)?\b|\bworks?\b|\bpasses\b|\bfails?\b|\breturns?\b|"
    r"\brenders?\b|\bresponds?\b|\bloads?\b|\bdisplays?\b|\bcontains?\b|"
    r"\bmatches\b|\bbehaviou?r\b",
    re.IGNORECASE,
)


def _has_trailing_checkable_outcome(line: str, match_end: int) -> bool:
    """True if real, observable content follows a lifecycle-phrase match.

    Requires a connector (colon/dash/and/so/with) after the match AND a
    concrete marker (number, URL, file, named test, or behaviour word)
    after that connector. Either alone is not enough -- see module note
    above the regexes for why ("and checkpoint advances" has the connector
    but no concrete marker, so it does not qualify).
    """
    remainder = line[match_end:]
    conn_match = _CHECKABLE_CONNECTOR_RE.search(remainder)
    if not conn_match:
        return False
    return bool(_CHECKABLE_MARKER_RE.search(remainder[conn_match.end():]))


def _is_forbidden_acceptance_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    for pattern in _FORBIDDEN_ACCEPTANCE_LINE_PATTERNS:
        match = pattern.search(stripped)
        if match and not _has_trailing_checkable_outcome(stripped, match.end()):
            return True
    return False


def _strip_unmeetable_acceptance_criteria(
    body: Optional[str], task_id: str
) -> tuple[Optional[str], list[str]]:
    """Deterministically remove unmeetable completion/checkpoint acceptance
    criteria lines from ``body`` before it is ever persisted.

    Scoped to the ``**Acceptance criteria**`` section (bounded by the next
    ``**Heading**`` line or end of body). Returns ``(cleaned_body, removed)``
    where ``removed`` lists the exact stripped lines (stripped of leading/
    trailing whitespace) for logging/testing. ``body`` of ``None`` or empty
    passes through unchanged.
    """
    if not body:
        return body, []

    lines = body.splitlines()
    out_lines: list[str] = []
    in_acceptance = False
    removed: list[str] = []

    for line in lines:
        heading_match = _SECTION_HEADING_RE.match(line)
        if heading_match:
            in_acceptance = "acceptance" in heading_match.group(1).lower()
            out_lines.append(line)
            continue
        if in_acceptance and _is_forbidden_acceptance_line(line):
            removed.append(line.strip())
            continue
        out_lines.append(line)

    if not removed:
        return body, []

    logger.warning(
        "specify: stripped %d unmeetable completion/checkpoint acceptance "
        "criterion line(s) for %s before persisting: %r",
        len(removed), task_id, removed,
    )
    return "\n".join(out_lines), removed


def _truncate(text: str, limit: int) -> str:
    # Plain length clamp for LLM prompt fields; these never reach a terminal, so no escape stripping here.
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _extract_json_blob(raw: str, fence_re: re.Pattern = _FENCE_RE) -> Optional[dict]:
    """Lenient JSON object extraction: strip code fences, take the first ``{``
    to the last ``}``. None if nothing parses to a dict."""
    if not raw:
        return None
    stripped = fence_re.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    try:
        val = json.loads(stripped[first : last + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    return val if isinstance(val, dict) else None


def _nonblank(v) -> Optional[str]:
    return v if isinstance(v, str) and v.strip() else None


def _title_body(parsed: dict) -> tuple[Optional[str], Optional[str]]:
    """``(title, body)`` from an LLM reply: title stripped, body verbatim,
    either None when missing/blank."""
    title = _nonblank(parsed.get("title"))
    return (title.strip() if title else None), _nonblank(parsed.get("body"))


def _profile_author(default: str = "specifier") -> str:
    """Same identity contract as ``hermes_cli.kanban._profile_author``; ``$USER`` as the last
    resort for a human running the CLI outside any profile."""
    from hermes_cli.profiles import current_profile_name
    return current_profile_name() or os.environ.get("USER") or default


def _load_triage_task(task_id: str) -> tuple[Optional[kb.Task], str]:
    """``(task, "")`` when the task exists and is in triage, else ``(None, reason)``."""
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        loop_pending = task is not None and kb.block_loop_repair_pending(conn, task_id)
    if task is None:
        return None, "unknown task id"
    if task.status != "triage":
        return None, f"task is not in triage (status={task.status!r})"
    if loop_pending:
        return None, kb._block_loop_refusal(task_id)
    return task, ""


def _task_prompt_fields(task: kb.Task) -> dict[str, str]:
    """Bounded ``task_id``/``title``/``body`` for the user prompt templates."""
    return {
        "task_id": task.id,
        "title": _truncate(task.title or "", 400),
        "body": _truncate(task.body or "(no body)", 4000),
    }


def _call_aux(verb: str, task_id: str, *, aux_task: str, system: str, user: str,
              max_tokens: int, timeout: int, log: logging.Logger = logger) -> tuple[Optional[str], str]:
    """One auxiliary LLM call; ``(reply_text, "")`` or ``(None, reason)``.

    ``call_llm`` applies all ``auxiliary.<aux_task>.*`` config (provider/model/
    base_url, extra_body, reasoning_effort, retries). Imported lazily so a
    missing aux client degrades to a skip instead of an import-time crash.
    """
    try:
        from agent.auxiliary_client import call_llm
    except Exception as exc:  # pragma: no cover — import smoke test
        log.debug("%s: auxiliary client import failed: %s", verb, exc)
        return None, "auxiliary client unavailable"
    # Specify/decompose run outside any agent turn (CLI, dashboard route, gateway watcher), so no
    # conversation affinity scope is bound and the relay-affinity headers (x-opencode-session, the
    # OpenRouter/Portal sticky key) are omitted — the OpenCode Go relay rejects that with 400
    # MissingSessionID (#112043). Declare a per-task scope, but only when none is already bound so an
    # in-turn caller keeps its conversation's key.
    from agent.portal_tags import get_affinity_scope, reset_affinity_scope, set_affinity_scope
    affinity_token = None if get_affinity_scope() else set_affinity_scope(f"kanban:{task_id}")
    try:
        # Route through call_llm so auxiliary.triage_specifier.* config (provider/model/base_url,
        # extra_body, reasoning_effort, retries) all apply — the direct-create path dropped extra_body
        # (#35566).
        resp = call_llm(
            task=aux_task,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.3,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    except Exception as exc:
        suffix = " — skipping" if verb == "specify" else ""
        log.info("%s: API call failed for %s (%s)%s", verb, task_id, exc, suffix)
        return None, f"LLM error: {type(exc).__name__}"
    finally:
        if affinity_token is not None:
            reset_affinity_scope(affinity_token)
    try:
        return resp.choices[0].message.content or "", ""
    except Exception:
        return "", ""


def specify_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> SpecifyOutcome:
    """Specify one triage task and promote it to ``todo``. Expected failures
    (not in triage, no aux client, API error, malformed reply) surface as
    ``ok=False`` so an ``--all`` sweep continues."""
    task, reason = _load_triage_task(task_id)
    if task is None:
        return SpecifyOutcome(task_id, False, reason)

    raw, reason = _call_aux(
        "specify", task_id, aux_task="triage_specifier", system=_SYSTEM_PROMPT,
        user=_USER_TEMPLATE.format(**_task_prompt_fields(task)),
        max_tokens=HERMES_KANBAN_SPECIFY_MAX_TOKENS, timeout=timeout or 120,
    )
    if raw is None:
        return SpecifyOutcome(task_id, False, reason)
    raw = raw.strip()

    parsed = _extract_json_blob(raw)
    if parsed is None:
        # Whole reply becomes the body; the user can edit afterward.
        if not raw:
            return SpecifyOutcome(task_id, False, "LLM returned an empty response")
        new_title, new_body = None, raw
    else:
        new_title, new_body = _title_body(parsed)
        if new_body is None and new_title is None:
            return SpecifyOutcome(task_id, False, "LLM response missing title and body")

    # Mechanical guard (t_adee1291): strip any unmeetable completion/
    # checkpoint-advancement acceptance-criterion line BEFORE the card is
    # ever persisted, regardless of whether the LLM honored the system
    # prompt above. See the module-level comment near
    # ``_strip_unmeetable_acceptance_criteria`` for the full rationale.
    new_body, stripped = _strip_unmeetable_acceptance_criteria(new_body, task_id)

    with kbc.connect_closing() as conn:
        ok = kb.specify_triage_task(
            conn,
            task_id,
            title=new_title,
            body=new_body,
            author=author or _profile_author(),
        )
    if not ok:
        # Race: someone else promoted / archived the task between our
        # read above and the write. Report, don't crash.
        return SpecifyOutcome(
            task_id, False, "task moved out of triage before promotion"
        )
    return SpecifyOutcome(
        task_id, True, "specified", new_title=new_title, stripped_criteria=stripped
    )


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Task ids in the triage column; ``tenant`` narrows the sweep."""
    with kbc.connect_closing() as conn:
        tasks = kb.list_tasks(conn, status="triage", tenant=tenant, include_archived=False)
    return [t.id for t in tasks]
