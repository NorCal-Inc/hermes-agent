"""Kanban triage specifier — flesh out a one-liner into a real spec.

Used by ``hermes kanban specify [task_id | --all]``. Takes a task that
lives in the Triage column (a rough idea, typically only a title), calls
the auxiliary LLM to produce:

  * A tightened title (optional — only replaces if the model proposes a
    materially different one)
  * A concrete body: goal, proposed approach, acceptance criteria

and then flips the task ``triage -> todo`` via
``kanban_db.specify_triage_task``. The dispatcher promotes it to
``ready`` on its next tick (or immediately if there are no open parents).

Design notes
------------

* This module intentionally mirrors ``hermes_cli/goals.py`` — same aux
  client pattern, same "empty config => skip, don't crash" tolerance.
  Keeps the surface area tiny and the failure modes predictable.

* The prompt is a short system + user pair. We ask for JSON with
  ``{title, body}``; if parsing fails, we fall back to treating the
  whole response as the body and leave the title untouched. No
  retry loop — one shot, keep cost bounded.

* Structured output / JSON mode is not requested explicitly so the
  specifier works on providers that don't implement it. The parse
  is lenient (tolerates markdown code fences around the JSON).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb

from utils import env_int

HERMES_KANBAN_SPECIFY_MAX_TOKENS = max(
    1500,
    env_int("HERMES_KANBAN_SPECIFY_MAX_TOKENS", 6000),
)

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
# Subject-noun vocabulary for the board-artifact itself (as opposed to the
# real-world deliverable the artifact tracks). Matched with \b...\b so
# generic prose is never caught: "dashboard"/"keyboard"/"aboard" don't
# contain a word-bounded "board", and plural "issues" doesn't contain a
# word-bounded "issue" (the trailing "s" blocks the boundary). Concept-based
# by design (subject noun x lifecycle verb) rather than a growing literal
# phrase list -- per Christopher/Erika direction 2026-10-26 after two
# whack-a-mole repair cycles, and widened to cover "board", "issue", "pull
# request" and "PR" after verifier t_df958275 found those four subjects
# missing from the original card|task|ticket-only set.
_SUBJECT_NOUNS = r"(?:card|task|ticket|board|issue|pull request|PR)"

_FORBIDDEN_ACCEPTANCE_LINE_PATTERNS = [
    re.compile(
        r"\b" + _SUBJECT_NOUNS + r"\b[^\n]{0,40}\b(marked|flagged|set)\b[^\n]{0,20}\b(done|complete[d]?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(mark|marking)\b[^\n]{0,40}\b" + _SUBJECT_NOUNS + r"\b[^\n]{0,20}\b(done|complete[d]?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bcheckpoint\b[^\n]{0,40}\badvance", re.IGNORECASE),
    re.compile(r"\badvance[sd]?\b[^\n]{0,40}\bcheckpoint\b", re.IGNORECASE),
    # "<subject> reaches/moves to/transitions to/advances to/becomes/goes
    # to/is done-or-complete[d]-or-closed-or-resolved-or-finished" -- the
    # ending set was widened beyond done/complete[d] to also catch "The
    # board transitions to closed." (verifier t_df958275).
    re.compile(
        r"\b" + _SUBJECT_NOUNS + r"\b[^\n]{0,40}"
        r"\b(reaches|moves to|transitions to|advances to|becomes|goes to|is)\b"
        r"[^\n]{0,20}\b(done|complete[d]?|closed?|resolved?|finished)\b",
        re.IGNORECASE,
    ),
    # Direct adjacency, no connector word at all (e.g. "Task completed.",
    # "Card done.") -- catches the LLM dropping the connector entirely,
    # which the prior attempt's patterns all required one to be present.
    re.compile(
        r"\b" + _SUBJECT_NOUNS + r"\b\s*(?:is\s+)?(?:done|complete[d]?)\b",
        re.IGNORECASE,
    ),
    # Modal / auxiliary completion: "The task must be completed.", "The
    # ticket is closed.", "Card should be resolved.", "The issue is
    # resolved." Only a whitelist of glue words may sit between subject and
    # verb so ordinary criteria like "Task runner script completes in 2
    # minutes" are not caught.
    re.compile(
        r"\b" + _SUBJECT_NOUNS + r"\b(?:\s+(?:must|should|shall|will|can|is|are|be|been|being|"
        r"get|gets|then|also|finally|successfully|automatically|has|have|had|as|"
        r"marked|set|flagged|to|the|a)\b){0,6}"
        r"\s+(?:done|complete[sd]?|closed?|closes|resolved?|resolves|finished|finishes)\b",
        re.IGNORECASE,
    ),
    # Imperative / gerund close-out of the artifact: "Close the card after
    # verification.", "Resolve this ticket.", "Complete the task.",
    # "Finish the pull request after review." (verifier t_df958275).
    re.compile(
        r"\b(?:close|closing|resolve|resolving|finish|finishing|complete|completing)\b"
        r"\s+(?:out\s+)?(?:the|this|that|a|its|each)?\s*" + _SUBJECT_NOUNS + r"\b",
        re.IGNORECASE,
    ),
    # General artifact-subject x lifecycle-stem match: "The task progresses.",
    # "The board advances.", "The issue transitions after verification.",
    # "The pull request progression is recorded.", "The PR advanced after
    # review." Only whitelisted glue words may sit between subject and stem.
    re.compile(
        r"\b" + _SUBJECT_NOUNS + r"\b(?:\s+(?:must|should|shall|will|can|is|are|be|been|being|"
        r"get|gets|then|also|finally|successfully|automatically|has|have|had|as|"
        r"the|a|status|state|lifecycle)\b){0,6}"
        r"\s+(?:progress\w*|advanc\w*|transition\w*|proceed\w*|"
        r"(?:moves?|moved|moving)\s+(?:forward|ahead|on|to\s+the\s+next))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:progress\w*|advanc\w*|transition\w*|proceed\w*)\b\s+(?:of\s+)?(?:the|this|that|a|its|each)?\s*"
        + _SUBJECT_NOUNS + r"\b",
        re.IGNORECASE,
    ),
    # Checkpoint progress/progression: "The checkpoint progresses.",
    # "Checkpoint progression is recorded.", "Move the checkpoint forward."
    re.compile(
        r"\bcheckpoint\b[^\n]{0,40}\b(?:progress\w*|moves?|moved|proceeds?|increments?|"
        r"updates? (?:to|forward)|goes forward)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:progress\w*|moves?|moved|proceeds?|increments?)\b[^\n]{0,40}\bcheckpoint\b",
        re.IGNORECASE,
    ),
]


def _is_forbidden_acceptance_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return any(p.search(stripped) for p in _FORBIDDEN_ACCEPTANCE_LINE_PATTERNS)


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
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _extract_json_blob(raw: str) -> Optional[dict]:
    """Lenient JSON extraction — tolerates fenced code blocks and
    leading/trailing whitespace. Returns None if nothing parses."""
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    # Greedy: find the first `{` and last `}` and try that slice.
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(val, dict):
        return None
    return val


def _profile_author() -> str:
    """Mirror of ``hermes_cli.kanban._profile_author``. Kept local to
    avoid a circular import when kanban.py imports this module."""
    return (
        os.environ.get("HERMES_PROFILE")
        or os.environ.get("USER")
        or "specifier"
    )


def specify_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> SpecifyOutcome:
    """Specify a single triage task and promote it to ``todo``.

    Returns an outcome describing what happened. Never raises for expected
    failure modes (task not in triage, no aux client configured, API
    error, malformed response) — those surface via ``ok=False`` so the
    ``--all`` sweep can continue past individual failures.
    """
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        loop_pending = task is not None and kb.block_loop_repair_pending(conn, task_id)
    if task is None:
        return SpecifyOutcome(task_id, False, "unknown task id")
    if task.status != "triage":
        return SpecifyOutcome(
            task_id, False, f"task is not in triage (status={task.status!r})"
        )
    if loop_pending:
        return SpecifyOutcome(task_id, False, kb._block_loop_refusal(task_id))

    try:
        from agent.auxiliary_client import call_llm
    except Exception as exc:  # pragma: no cover — import smoke test
        logger.debug("specify: auxiliary client import failed: %s", exc)
        return SpecifyOutcome(task_id, False, "auxiliary client unavailable")

    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=_truncate(task.title or "", 400),
        body=_truncate(task.body or "(no body)", 4000),
    )

    try:
        # Route through call_llm so auxiliary.triage_specifier.* config
        # (provider/model/base_url, extra_body, reasoning_effort, retries)
        # all apply — the direct-create path dropped extra_body (#35566).
        resp = call_llm(
            task="triage_specifier",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=HERMES_KANBAN_SPECIFY_MAX_TOKENS,
            timeout=timeout or 120,
        )
    except Exception as exc:
        logger.info(
            "specify: API call failed for %s (%s) — skipping",
            task_id, exc,
        )
        return SpecifyOutcome(
            task_id, False, f"LLM error: {type(exc).__name__}"
        )

    try:
        raw = (resp.choices[0].message.content or "").strip()
    except Exception:
        raw = ""

    parsed = _extract_json_blob(raw)

    new_title: Optional[str]
    new_body: Optional[str]
    if parsed is None:
        # Fall back: treat the whole reply as the body, leave title as-is.
        # Worst case the user edits afterward — still better than stranding
        # the task in triage on a malformed LLM reply.
        stripped_raw = raw.strip()
        if not stripped_raw:
            return SpecifyOutcome(
                task_id, False, "LLM returned an empty response"
            )
        new_title = None
        new_body = stripped_raw
    else:
        title_val = parsed.get("title")
        body_val = parsed.get("body")
        new_title = (
            title_val.strip()
            if isinstance(title_val, str) and title_val.strip()
            else None
        )
        new_body = (
            body_val if isinstance(body_val, str) and body_val.strip() else None
        )
        if new_body is None and new_title is None:
            return SpecifyOutcome(
                task_id, False, "LLM response missing title and body"
            )

    # Mechanical guard (t_adee1291): strip any unmeetable completion/
    # checkpoint-advancement acceptance-criterion line BEFORE the card is
    # ever persisted, regardless of whether the LLM honored the system
    # prompt above. See the module-level comment near
    # ``_strip_unmeetable_acceptance_criteria`` for the full rationale.
    new_body, stripped = _strip_unmeetable_acceptance_criteria(new_body, task_id)

    with kb.connect_closing() as conn:
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
    """Return task ids currently in the triage column.

    ``tenant`` narrows the sweep; ``None`` returns every triage task.
    """
    with kb.connect_closing() as conn:
        tasks = kb.list_tasks(
            conn,
            status="triage",
            tenant=tenant,
            include_archived=False,
        )
    return [t.id for t in tasks]
