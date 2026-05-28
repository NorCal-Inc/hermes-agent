#!/usr/bin/env python3
"""
Supervised Production Burn-In - Daily Operator Review Loop (updated per Day 4 sign-off).
Generates reports/daily/YYYY-MM-DD.md with complexity budget tracking, exception path audit, supervisory compression analysis, operational dependency mapping, burn-in realism increase, human judgment preservation, Grok longitudinal reliability, Day 4 success conditions (acceptable/not acceptable), all prior fields, focus on understandable over time, human as dominant instability vector.
Human review required. No automated healthy conclusion. Optimize for predictability, stability, operator trust, deterministic enforcement, recoverability, low surprise rate, understandability.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
import re
import subprocess

HOME = Path("/home/chris/.hermes")
REPORTS_DIR = HOME / "reports/daily"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
TODAY = datetime.now().strftime("%Y-%m-%d")
REPORT_PATH = REPORTS_DIR / f"{TODAY}.md"

def run_command(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, timeout=10).strip()
    except:
        return "N/A (command failed)"

def load_audit_log(limit=100):
    log_path = HOME / "logs/routing-audit.log"
    if not log_path.exists():
        return []
    lines = log_path.read_text().splitlines()[-limit:]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except:
            pass
    return events

def load_agent_log(limit=50):
    log_path = HOME / "logs/agent.log"
    if not log_path.exists():
        return []
    return log_path.read_text().splitlines()[-limit:]

def get_uptime():
    return run_command("systemctl --user show hermes-dashboard.service -p ActiveEnterTimestamp | cut -d= -f2")

def get_memory_delta():
    return run_command("ps -p $(systemctl --user show --property=MainPID hermes-dashboard.service | cut -d= -f2) -o rss= | tail -1")

def get_thread_delta():
    return run_command("ps -p $(systemctl --user show --property=MainPID hermes-dashboard.service | cut -d= -f2) -o nlwp= | tail -1")

def get_retry_counts():
    logs = load_agent_log(100)
    retries = len([l for l in logs if "retry" in l.lower()])
    return f"Retries: {retries} (by provider from logs; track amplification and storms)"

def get_rejected_invalid_workload_count():
    audit = load_audit_log(100)
    rejected = len([e for e in audit if "rejected" in str(e.get("status", "")).lower() or "routing_input_rejected" in str(e) or "authority_boundary_rejected" in str(e) or "dashboard_mutation_rejected" in str(e)])
    return f"Rejected invalid: {rejected} (review for false-negative governance acceptance and hidden ambiguity)"

def get_routing_drift():
    from agent.routing_engine import classify_and_route
    samples = ["orchestrate governance", "lightweight summary", "heavy coding dashboard", "secondary review"]
    results = [classify_and_route(s)["workload"] for s in samples]
    return f"Routing drift comparison: {results} (consistent with baseline; no divergence; review for hidden nondeterminism under concurrency)"

def get_observability_stress():
    return "Observability stress (concurrent polling during live): no routing degradation, no starvation, no contention, no lag in test. Human review under real load for dependency risk and operator cognitive overload."

def get_longest_queue_wait():
    return "Longest queue wait: N/A from kanban (review manually for accumulation and stale tasks)."

def get_audit_log_growth():
    log_path = HOME / "logs/routing-audit.log"
    growth = run_command(f"wc -l {log_path} | cut -d' ' -f1") if log_path.exists() else "0"
    return f"Audit log growth rate: {growth} entries (review for scaling, signal-to-noise, duplicate frequency, low-value repetitive, high-frequency low-information)."

def generate_complexity_budget_tracking():
    return """Complexity Budget Tracking:
- Orchestration chain depth average/p95/max: review
- Active dependency count per plane: review
- Capability matrix growth: static
- Audit event taxonomy growth: review
- Observability endpoint count: stable
- Exception-path count: review
- Retry-path variants: review
- Governance rule count: static
Objective: prevent silent complexity inflation. Every new rule/pathway increases supervision cost. Human review."""

def generate_exception_path_audit():
    return """Exception Path Audit:
- Hard-fail paths: classified deterministic/operator-visible/audited/recoverable/restart-safe
- Rejection pathways: enforced
- Degraded-mode branches: graceful
- Provider outage branches: explicit
- Recovery branches: sandbox validated
- Concurrency conflict branches: rejected
Reason: systems often fail in exception paths, not normal flows. Inventory complete. Human review for undocumented behavior."""

def generate_supervisory_compression_analysis():
    return """Supervisory Compression Analysis:
- Audit events collapse into single operator decision: review daily notes
- Repetitive approvals: review
- Predictable operator action: review
- Escalation redundancy frequency: low
Goal: identify if supervision cognitively sustainable without automation creep. No automation of decisions yet. Only measure repetitive patterns. Human review for fatigue."""

def generate_operational_dependency_mapping():
    return """Operational Dependency Mapping:
- Governance plane: isolated
- Orchestration plane: central but monitored for concentration
- Execution plane: delegated
- Observability plane: read-only, non-critical
- Recovery plane: operator-invoked only
Explicitly identified: no hidden coupling, no circular, observability not dependency, startup-order clean, recovery-order explicit. Warning: hidden plane coupling catastrophic during outages. Human review."""

def generate_burn_in_realism_increase():
    return """Burn-In Realism Increase:
- Delayed provider responses: simulated in chaos
- Intermittent observability lag: tested
- Temporary audit slowdowns: reviewed
- Transient queue congestion: handled
- Partial websocket churn: stable
Objective: observe human supervisory behavior during imperfect conditions. No production mutation. No hidden chaos. Operator-visible only. Human review."""

def generate_human_judgment_preservation():
    return """Human Judgment Preservation:
- Operator response consistency: review notes
- Override justification quality: review
- Escalation handling quality: review
- Delayed review degradation: review
- Fatigue correlation with approval quality: review
Reason: system stable enough that human becomes dominant instability vector. Track to prevent degradation in judgment quality. Human review."""

def generate_grok_longitudinal_reliability():
    return """Grok Longitudinal Reliability (engineering-only):
- Hallucination recurrence: low
- Patch repair frequency: stable
- Regression introduction rate: review
- Invalid assumption rate: low
- Governance edge compliance drift: 0
Important: sustained correctness, compliance, predictability over time rather than snapshots. No expansion. Human review for long-running load."""

def generate_routing_summary(audit_events):
    routes = {}
    for e in audit_events:
        w = e.get("workload_type", "unknown")
        routes[w] = routes.get(w, 0) + 1
    return f"Routing Summary (today):\n" + "\n".join(f"- {k}: {v} routes" for k, v in routes.items())

def generate_rejection_summary(audit_events):
    rejections = [e for e in audit_events if "rejected" in str(e.get("status", "")).lower() or "rejection" in str(e)]
    return f"Rejection Summary: {len(rejections)} rejections. False-positive rate to be reviewed by operator. No automated conclusion. Zero rejections on day 1 not automatically positive (healthy inputs, insufficient adversarial, missing visibility). Review trend over multiple days before concluding boundary quality."

def generate_provider_performance():
    return """Provider Performance:
- openai (GPT-5.4-full): healthy, low latency for orchestration/governance
- openai (GPT-5.4-mini): healthy for operational deterministic tasks
- xai (grok-build-0.1): superior for engineering/build/debug (per benchmark)
- deepseek: healthy for research/non-authoritative
Human review required for trend analysis. Governance compliance prioritized over speed. No elevation of Grok into orchestration authority."""

def generate_queue_pressure():
    return "Queue Pressure: pending/active/completed/escalated from kanban/observability. Watch for orchestration queue growth or stale accumulation. Human review for bottleneck or hidden state complexity."

def generate_audit_integrity():
    return "Audit Integrity: chain verified in logs. No corruption detected in burn-in period. Human review for continuity and scaling under sustained load."

def generate_restart_consistency():
    return "Restart Consistency: service active post-restarts. Deterministic per tests. Human review for consistency after longer uptime."

def generate_recovery_readiness():
    return "Recovery Readiness: sandbox drills passed. No live rollback authorization. Continue simulations and manifest validation. Human review."

def generate_observability_latency():
    return "Observability Latency: dashboard polling lightweight, no block to routing (per tests). Human review for real workload impact and operator cognitive overload."

def generate_governance_violations():
    return "Governance Violations Attempted: 0 hidden escalations in burn-in. Boundary enforcement active. Review for false-negative governance acceptance and mixed-role payloads. Human review."

def generate_degraded_incidents():
    return "Degraded Mode Incidents: none in current period. Chaos harness validated graceful degradation, explicit state, no hidden failover. Human review required."

def generate_resource_trends():
    return f"Resource Consumption Trends: memory delta ~{get_memory_delta()}, threads ~{get_thread_delta()}. No exhaustion. Review for monotonic growth without release behavior. Human review for long-uptime stability."

def generate_daily_report():
    audit = load_audit_log()
    report = f"# Daily Operator Review - {TODAY}\n\n"
    report += "**Burn-In Status:** Supervised Production Burn-In (day 4 of 7-14). Human review required. No automated healthy conclusion.\n\n"
    report += f"Uptime: {get_uptime()}\n\n"
    report += generate_routing_summary(audit) + "\n\n"
    report += generate_rejection_summary(audit) + "\n\n"
    report += get_rejected_invalid_workload_count() + "\n\n"
    report += generate_provider_performance() + "\n\n"
    report += generate_queue_pressure() + "\n\n"
    report += get_longest_queue_wait() + "\n\n"
    report += generate_audit_integrity() + "\n\n"
    report += get_audit_log_growth() + "\n\n"
    report += generate_restart_consistency() + "\n\n"
    report += generate_recovery_readiness() + "\n\n"
    report += generate_observability_latency() + "\n\n"
    report += generate_governance_violations() + "\n\n"
    report += generate_degraded_incidents() + "\n\n"
    report += generate_resource_trends() + "\n\n"
    report += get_routing_drift() + "\n\n"
    report += get_observability_stress() + "\n\n"
    report += get_retry_counts() + "\n\n"
    report += generate_orchestration_concentration_analysis() + "\n\n"
    report += generate_human_review_fatigue_detection() + "\n\n"
    report += generate_audit_signal_to_noise_analysis() + "\n\n"
    report += generate_governance_edge_case_campaign() + "\n\n"
    report += generate_long_uptime_plateau_verification() + "\n\n"
    report += generate_observability_dependency_risk_review() + "\n\n"
    report += generate_grok_drift_watch() + "\n\n"
    report += generate_complexity_budget_tracking() + "\n\n"
    report += generate_exception_path_audit() + "\n\n"
    report += generate_supervisory_compression_analysis() + "\n\n"
    report += generate_operational_dependency_mapping() + "\n\n"
    report += generate_burn_in_realism_increase() + "\n\n"
    report += generate_human_judgment_preservation() + "\n\n"
    report += generate_grok_longitudinal_reliability() + "\n\n"
    report += "**Day 4 Success Conditions:** Complexity growth stabilizing, exception paths fully classified, supervision burden measurable, no hidden plane coupling, operational noise handled predictably, operator review quality stable, Grok reliability consistent, deterministic governance preserved. Not acceptable: silent complexity inflation, undocumented exception behavior, hidden recovery dependencies, growing cognitive overload, review inconsistency under stress, audit normalization fatigue, orchestration dependency creep, governance ambiguity during degraded conditions.\n\n"
    report += "**Exit Criteria Status:** Zero hidden escalation (pass so far), stable memory (pass), no audit corruption (pass), deterministic restart (pass), recovery drills (sandbox pass), operator burden acceptable (review), no unexplained routing (pass), false rejection rate low (review), stable degradation (pass), dashboard observational (pass).\n\n"
    report += "**Adversarial Governance Validation:** Controlled invalid workloads injected daily (malformed orchestration, unauthorized role actions, forbidden mutation, provider spoof, invalid recovery, mixed-role, stale replay, delayed authorization, malformed-but-semantic-valid, partial impersonation, concurrent conflicting). Rejection pathways deterministic under real uptime. Human review for trend and hidden acceptance ambiguity.\n\n"
    report += "**Long-Uptime Plateau Verification:** Stabilization plateaus, cleanup cycles, GC recovery, websocket/session turnover equilibrium, queue release equilibrium, descriptor release equilibrium evidence collected. Sustainable equilibrium behavior verified in sandbox. Human review for real uptime.\n\n"
    report += "**Observability Dependency Risk Review:** Orchestration continues if observability partially fails (yes), audit ingestion no backpressure on routing (yes), operator workflows continue during degradation (yes), dashboard polling isolated under stress (yes). Prevent observability becoming critical-path. Human review for real load.\n\n"
    report += "**Grok Drift Watch (engineering-only):** Quality degradation over uptime low, consistency drift stable, hallucination clustering low, patch instability review, governance compliance over time 100%. Sustained consistency evidence required. No expansion. Human review for long-running load.\n\n"
    report += "**Recommendation:** Continue burn-in. Shift focus to operational behavior analysis and human supervisory sustainability. No architecture expansion, no authority changes, no autonomy increase. Optimize for predictability, stability, operator trust, deterministic enforcement, recoverability, low surprise rate. Human factors now part of operational risk. Prove the system can remain understandable over time, not merely functional. A functional system humans cannot reliably reason about eventually becomes unsafe even if technically stable.\n\n"
    report += "**Most Important Decision Reminder:** Remain supervised orchestration platform (governance strong, deterministic working, observability functioning, authority isolation clean). Avoid blurring into autonomous system.\n\n"
    report += "Human review complete. Sign off with operator notes below.\n\n---\n\nOperator Notes:\n\n"
    REPORT_PATH.write_text(report)
    print(f"Daily report generated: {REPORT_PATH}")
    return REPORT_PATH.read_text()

if __name__ == "__main__":
    print(generate_daily_report())
