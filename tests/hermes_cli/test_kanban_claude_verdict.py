"""The board reads the claude_verify lane's CLAUDE_VERDICT line (2026-10-26, t_693f96a2)."""

from hermes_cli import kanban_db as kb


def test_claude_verdict_line_is_read() -> None:
    assert kb._parse_verifier_verdict("checks...\n\nCLAUDE_VERDICT: PASS") == "PASS"
    assert kb._parse_verifier_verdict("CLAUDE_VERDICT: FAIL") == "FAIL"


def test_existing_markers_unchanged() -> None:
    assert kb._parse_verifier_verdict("VERDICT: PASS") == "PASS"
    assert kb._parse_verifier_verdict("ATLAS_VERDICT: FAIL") == "FAIL"
    assert kb._parse_verifier_verdict("no verdict here") is None
    assert kb._parse_verifier_verdict("VERDICT: PASS\nCLAUDE_VERDICT: FAIL") is None
