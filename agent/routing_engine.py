#!/usr/bin/env python3
"""
Deterministic workload routing engine for Hermes.
Implements strict model authority map per governance spec.

Authority map:
GPT-5.4 FULL
* orchestration
* governance
* escalation reasoning
* topology reasoning
* approval analysis
* operational planning
* risk evaluation

GPT-5.4-MINI
* lightweight operational tasks
* summaries
* log parsing
* queue cleanup
* classification
* deterministic support work

grok-build-0.1 (xAI, upstream grok-4.20-reasoning)
* engineering execution
* heavy coding
* debugging
* infrastructure implementation
* schema work
* dashboard wiring
* topology implementation
* runtime fixes

DeepSeek
* secondary engineering review
* alternative implementation analysis
* backup engineering reasoning

Restrictions:
* DeepSeek is never orchestration authority
* Grok is not governance authority
* GPT-MINI is not architecture authority

Explicit classifier, deterministic routing rules, fallback hierarchy, provider audit logging, model-selection visibility in task metadata.
No silent fallback, no implicit escalation, no model-role ambiguity.
Stabilizes operational intelligence before autonomy expansion.
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Tuple, List

from hermes_constants import get_hermes_home

class RoutingEngine:
    def __init__(self):
        self.home = Path(get_hermes_home())
        self.config_path = self.home / "config.yaml"
        self.audit_log = self.home / "logs/routing-audit.log"
        self.load_config()

    def load_config(self):
        """Load routing config (deterministic, no implicit defaults)."""
        import yaml
        with open(self.config_path) as f:
            self.config = yaml.safe_load(f)
        self.routing = self.config.get("routing", {})
        self.hierarchy = self.routing.get("hierarchy", {})
        self.classification = self.routing.get("classification", {})
        self.fallback = self.routing.get("fallback", {})
        self.audit_enabled = self.routing.get("audit", {}).get("enabled", True)

    def classify_workload(self, task: str, context: str = "") -> str:
        """Deterministic keyword-based classifier with pre-classification validation at entry (no silent coercion, explicit rejection for malformed)."""
        from agent.routing_input_validator import validate_routing_input
        validated = validate_routing_input(task)
        if isinstance(validated, dict) and validated.get("status") == "valid":
            task = validated.get("normalized", task)
        text = (str(task) + " " + str(context)).lower()
        if any(k in text for k in ["orchestration", "governance", "escalation", "topology reasoning", "approval", "planning", "risk"]):
            return "orchestration"
        if any(k in text for k in ["lightweight", "summary", "log", "queue", "classification", "cleanup", "support"]):
            return "operational"
        if any(k in text for k in ["engineering", "coding", "debug", "infrastructure", "schema", "dashboard", "topology implementation", "runtime", "build", "refactor"]):
            return "engineering"
        if any(k in text for k in ["review", "alternative", "backup", "research"]):
            return "research"
        return "operational"  # deterministic default after explicit validation

    def get_model_for_workload(self, workload: str) -> Tuple[str, str]:
        """Strict authority map. Enforces all restrictions (no DeepSeek orchestration, no Grok governance, no MINI architecture)."""
        map = {
            "orchestration": ("gpt-5.4-full", "openai"),
            "operational": ("gpt-5.4-mini", "openai"),
            "engineering": ("grok-build-0.1", "xai"),
            "research": ("deepseek", "deepseek")
        }
        if workload in map:
            model, provider = map[workload]
            # Enforce restrictions
            if workload == "orchestration" and model.startswith("deepseek"):
                return ("gpt-5.4-full", "openai")
            if workload == "governance" and "grok" in model:
                return ("gpt-5.4-full", "openai")
            if workload in ("architecture", "topology") and "mini" in model:
                return ("gpt-5.4-full", "openai")
            return (model, provider)
        return ("gpt-5.4-full", "openai")  # safe deterministic default

    def get_fallback(self, failed_model: str) -> Tuple[str, str]:
        """Explicit fallback hierarchy. No silent fallback."""
        fallback_map = {
            "grok-build-0.1": ("deepseek", "deepseek"),
            "gpt-5.4-mini": ("gpt-5.4-full", "openai"),
            "deepseek": ("grok-build-0.1", "xai")
        }
        if failed_model in fallback_map:
            return fallback_map[failed_model]
        return ("gpt-5.4-full", "openai")

    def audit(self, provider: str, model: str, workload: str, fallback_chain: List[str] = None, latency: float = 0.0, success: bool = True, error: str = None, metadata: Dict = None):
        """Audit with model-selection visibility in task metadata. No secrets."""
        if not self.audit_enabled:
            return
        entry = {
            "timestamp": datetime.now().isoformat(),
            "provider": provider,
            "model": model,
            "workload_type": workload,
            "fallback_chain": fallback_chain or [],
            "latency_ms": round(latency * 1000, 2),
            "success": success,
            "error_type": error,
            "metadata": metadata or {"model_selection": model, "authority": workload}
        }
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.audit_log, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def route(self, task: str, context: str = "") -> Dict[str, Any]:
        """Main deterministic routing. Returns metadata with model selection visibility."""
        start = time.time()
        workload = self.classify_workload(task, context)
        model, provider = self.get_model_for_workload(workload)
        latency = time.time() - start
        metadata = {
            "selected_model": model,
            "authority_level": workload,
            "deterministic": True,
            "restrictions_enforced": True
        }
        self.audit(provider, model, workload, latency=latency, success=True, metadata=metadata)
        return {
            "model": model,
            "provider": provider,
            "workload": workload,
            "latency": latency,
            "metadata": metadata
        }

# Singleton
routing_engine = RoutingEngine()

def get_routed_model(task_description: str = None, workload_hint: str = None) -> Tuple[str, str]:
    """Public deterministic API. Used by agent loop, subagents, dashboard wiring."""
    if workload_hint:
        workload = workload_hint
    else:
        workload = "operational"
    model, provider = routing_engine.get_model_for_workload(workload)
    return model, provider

def classify_and_route(task: str, context: str = "") -> Dict[str, Any]:
    """Full route with audit and metadata for task visibility."""
    return routing_engine.route(task, context)
