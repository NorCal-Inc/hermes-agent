"""hermes webhook — manage dynamic webhook subscriptions from the CLI.

Usage:
    hermes webhook subscribe <name> [options]
    hermes webhook list
    hermes webhook remove <name>
    hermes webhook test <name> [--payload '{"key": "value"}']

Subscriptions persist to ~/.hermes/webhook_subscriptions.json and are
hot-reloaded by the webhook adapter without a gateway restart.
"""

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Dict

from hermes_constants import display_hermes_home
from utils import atomic_replace
from hermes_cli.config import cfg_get


_SUBSCRIPTIONS_FILENAME = "webhook_subscriptions.json"
_SUBSCRIPTIONS_FILE_MODE = 0o600


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _subscriptions_path() -> Path:
    return _hermes_home() / _SUBSCRIPTIONS_FILENAME


def _load_subscriptions() -> Dict[str, dict]:
    path = _subscriptions_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_subscriptions(subs: Dict[str, dict]) -> None:
    path = _subscriptions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # webhook_subscriptions.json contains per-route HMAC secrets — write
    # via tempfile + chmod 0o600 before the atomic rename so a permissive
    # umask cannot leave the secrets readable to other local users in the
    # window between create and rename.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(subs, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, _SUBSCRIPTIONS_FILE_MODE)
        atomic_replace(tmp_path, path)
        # Re-assert after rename in case the destination existed with a
        # broader mode and atomic_replace preserved it.
        os.chmod(path, _SUBSCRIPTIONS_FILE_MODE)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _get_webhook_config() -> dict:
    """Load webhook platform config. Returns {} if not configured."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return cfg_get(cfg, "platforms", "webhook", default={})
    except Exception:
        return {}


def _is_webhook_enabled() -> bool:
    return bool(_get_webhook_config().get("enabled"))


def _get_webhook_base_url() -> str:
    wh = _get_webhook_config().get("extra", {})
    host = wh.get("host", "0.0.0.0")
    port = wh.get("port", 8644)
    display_host = "localhost" if host == "0.0.0.0" else host
    return f"http://{display_host}:{port}"


def _setup_hint() -> str:
    _dhh = display_hermes_home()
    return f"""
  Webhook platform is not enabled. To set it up:

  1. Run the gateway setup wizard:
     hermes gateway setup

  2. Or manually add to {_dhh}/config.yaml:
     platforms:
       webhook:
         enabled: true
         extra:
           host: "0.0.0.0"
           port: 8644
           secret: "your-global-hmac-secret"

  3. Or set environment variables in {_dhh}/.env:
     WEBHOOK_ENABLED=true
     WEBHOOK_PORT=8644
     WEBHOOK_SECRET=your-global-secret

  Then start the gateway: hermes gateway run
"""


def _require_webhook_enabled() -> bool:
    """Check webhook is enabled. Print setup guide and return False if not."""
    if _is_webhook_enabled():
        return True
    print(_setup_hint())
    return False


_ROUTE_PRESETS = {
    "lifewiki-nightly": {
        "description": "Nightly LifeWiki operational summary ingest",
        "events": ["nightly_summary"],
        "owner_company": "LifeWiki",
        "supervising_team_leader": "LifeWiki_Lead",
        "routing_class": "company-private",
        "fallback_behavior": "suppress-or-escalate",
        "prompt": (
            "LifeWiki nightly operational summary\n"
            "source: {source}\n"
            "company: {company}\n"
            "summary_date: {summary_date}\n"
            "severity: {severity}\n"
            "required_action: {required_action}\n"
            "escalation_state: {escalation_state}\n\n"
            "compressed_summary:\n{summary}\n\n"
            "unresolved_incidents:\n{unresolved_incidents}\n\n"
            "repeated_failures:\n{repeated_failures}\n\n"
            "stripe_anomalies:\n{stripe_anomalies}\n\n"
            "routing_violations:\n{routing_violations}\n\n"
            "escalation_summaries:\n{escalation_summaries}\n\n"
            "deployment_failures:\n{deployment_failures}\n\n"
            "monitoring_anomalies:\n{monitoring_anomalies}\n\n"
            "worker_noise_patterns:\n{worker_noise_patterns}\n\n"
            "operational_bottlenecks:\n{operational_bottlenecks}\n"
        ),
        "validation": {
            "required_fields": [
                "source",
                "company",
                "summary_date",
                "summary",
                "unresolved_incidents",
                "repeated_failures",
                "stripe_anomalies",
                "routing_violations",
                "escalation_summaries",
                "deployment_failures",
                "monitoring_anomalies",
                "worker_noise_patterns",
                "operational_bottlenecks",
                "severity",
                "required_action",
                "escalation_state",
            ],
            "allowed_companies": ["LifeWiki"],
        },
    },
    "stripe-payment-success": {
        "description": "TripTracker/Orion payment success telemetry",
        "events": ["payment_intent.succeeded", "checkout.session.completed"],
        "owner_company": "variable",
        "supervising_team_leader": "TripTracker_Lead|Orion_Formation_Services_Lead",
        "routing_class": "company-private",
        "fallback_behavior": "suppress-or-escalate",
        "prompt": (
            "Stripe payment success\n"
            "source: {source}\n"
            "company: {company}\n"
            "customer_identifier: {customer_identifier}\n"
            "severity: {severity}\n"
            "required_action: {required_action}\n"
            "escalation_state: {escalation_state}\n"
            "event_id: {event_id}\n"
        ),
        "validation": {
            "required_fields": [
                "source",
                "company",
                "event_id",
                "severity",
                "required_action",
                "escalation_state",
            ],
        },
    },
    "stripe-payment-failure": {
        "description": "TripTracker/Orion payment failure telemetry",
        "events": ["payment_intent.payment_failed", "invoice.payment_failed"],
        "owner_company": "variable",
        "supervising_team_leader": "TripTracker_Lead|Orion_Formation_Services_Lead",
        "routing_class": "company-private",
        "fallback_behavior": "suppress-or-escalate",
        "prompt": (
            "Stripe payment failure\n"
            "source: {source}\n"
            "company: {company}\n"
            "customer_identifier: {customer_identifier}\n"
            "severity: {severity}\n"
            "required_action: {required_action}\n"
            "escalation_state: {escalation_state}\n"
            "retry_state: {retry_state}\n"
            "event_id: {event_id}\n"
        ),
        "validation": {
            "required_fields": [
                "source",
                "company",
                "event_id",
                "severity",
                "required_action",
                "escalation_state",
            ],
        },
    },
    "stripe-checkout-abandonment": {
        "description": "Checkout abandonment telemetry",
        "events": ["checkout.session.expired"],
        "owner_company": "variable",
        "supervising_team_leader": "TripTracker_Lead|Orion_Formation_Services_Lead",
        "routing_class": "company-private",
        "fallback_behavior": "suppress-or-escalate",
        "prompt": (
            "Stripe checkout abandonment\n"
            "source: {source}\n"
            "company: {company}\n"
            "customer_identifier: {customer_identifier}\n"
            "severity: {severity}\n"
            "required_action: {required_action}\n"
            "escalation_state: {escalation_state}\n"
            "event_id: {event_id}\n"
        ),
        "validation": {
            "required_fields": [
                "source",
                "company",
                "event_id",
                "severity",
                "required_action",
                "escalation_state",
            ],
        },
    },
    "stripe-subscription-cancellation": {
        "description": "Subscription cancellation telemetry",
        "events": ["customer.subscription.deleted"],
        "owner_company": "variable",
        "supervising_team_leader": "TripTracker_Lead|Orion_Formation_Services_Lead",
        "routing_class": "company-private",
        "fallback_behavior": "suppress-or-escalate",
        "prompt": (
            "Stripe subscription cancellation\n"
            "source: {source}\n"
            "company: {company}\n"
            "customer_identifier: {customer_identifier}\n"
            "severity: {severity}\n"
            "required_action: {required_action}\n"
            "escalation_state: {escalation_state}\n"
            "event_id: {event_id}\n"
        ),
        "validation": {
            "required_fields": [
                "source",
                "company",
                "event_id",
                "severity",
                "required_action",
                "escalation_state",
            ],
        },
    },
    "stripe-dispute-opened": {
        "description": "Dispute opened telemetry",
        "events": ["charge.dispute.created"],
        "owner_company": "variable",
        "supervising_team_leader": "TripTracker_Lead|Orion_Formation_Services_Lead",
        "routing_class": "company-private",
        "fallback_behavior": "suppress-or-escalate",
        "prompt": (
            "Stripe dispute opened\n"
            "source: {source}\n"
            "company: {company}\n"
            "customer_identifier: {customer_identifier}\n"
            "severity: {severity}\n"
            "required_action: {required_action}\n"
            "escalation_state: {escalation_state}\n"
            "event_id: {event_id}\n"
        ),
        "validation": {
            "required_fields": [
                "source",
                "company",
                "event_id",
                "severity",
                "required_action",
                "escalation_state",
            ],
        },
    },
    "stripe-refund-issued": {
        "description": "Refund issued telemetry",
        "events": ["charge.refunded", "refund.created"],
        "owner_company": "variable",
        "supervising_team_leader": "TripTracker_Lead|Orion_Formation_Services_Lead",
        "routing_class": "company-private",
        "fallback_behavior": "suppress-or-escalate",
        "prompt": (
            "Stripe refund issued\n"
            "source: {source}\n"
            "company: {company}\n"
            "customer_identifier: {customer_identifier}\n"
            "severity: {severity}\n"
            "required_action: {required_action}\n"
            "escalation_state: {escalation_state}\n"
            "event_id: {event_id}\n"
        ),
        "validation": {
            "required_fields": [
                "source",
                "company",
                "event_id",
                "severity",
                "required_action",
                "escalation_state",
            ],
        },
    },
}

def _preset_route(preset: str) -> dict:
    spec = _ROUTE_PRESETS.get(preset)
    if not spec:
        raise KeyError(preset)
    route = {
        "description": spec["description"],
        "events": list(spec.get("events", [])),
        "prompt": spec.get("prompt", ""),
        "skills": list(spec.get("skills", [])),
        "deliver": spec.get("deliver", "log"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "validation": spec.get("validation", {}),
    }
    if spec.get("deliver_only"):
        route["deliver_only"] = True
    if spec.get("deliver_extra"):
        route["deliver_extra"] = dict(spec["deliver_extra"])
    return route


def webhook_command(args):
    """Entry point for 'hermes webhook' subcommand."""
    sub = getattr(args, "webhook_action", None)

    if not sub:
        print("Usage: hermes webhook {subscribe|list|remove|test}")
        print("Run 'hermes webhook --help' for details.")
        return

    if not _require_webhook_enabled():
        return

    if sub in {"subscribe", "add"}:
        _cmd_subscribe(args)
    elif sub in {"list", "ls"}:
        _cmd_list(args)
    elif sub in {"remove", "rm"}:
        _cmd_remove(args)
    elif sub == "test":
        _cmd_test(args)


def _cmd_subscribe(args):
    name = args.name.strip().lower().replace(" ", "-")
    if not re.match(r'^[a-z0-9][a-z0-9_-]*$', name):
        print(f"Error: Invalid name '{name}'. Use lowercase alphanumeric with hyphens/underscores.")
        return

    subs = _load_subscriptions()
    is_update = name in subs

    secret = args.secret or secrets.token_urlsafe(32)
    preset = getattr(args, "preset", "") or ""
    events: list[str] = []

    if preset:
        try:
            route = _preset_route(preset)
        except KeyError:
            print(f"Error: Unknown preset '{preset}'.")
            return
    else:
        events = [e.strip() for e in args.events.split(",") if e.strip()] if args.events else []
        route = {
            "description": args.description or f"Agent-created subscription: {name}",
            "events": events,
            "prompt": args.prompt or "",
            "skills": [s.strip() for s in args.skills.split(",") if s.strip()] if args.skills else [],
            "deliver": args.deliver or "log",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    route["secret"] = secret
    route["description"] = args.description or route.get("description", f"Agent-created subscription: {name}")

    if args.prompt:
        route["prompt"] = args.prompt
    if args.events and not preset:
        route["events"] = [e.strip() for e in args.events.split(",") if e.strip()]
    if args.skills and not preset:
        route["skills"] = [s.strip() for s in args.skills.split(",") if s.strip()]
    if args.deliver and not preset:
        route["deliver"] = args.deliver or route.get("deliver", "log")
    if preset:
        route["preset"] = preset

    if getattr(args, "deliver_only", False):
        if route["deliver"] == "log":
            print(
                "Error: --deliver-only requires --deliver to be a real target "
                "(telegram, discord, slack, github_comment, etc.) — not 'log'."
            )
            return
        route["deliver_only"] = True

    if args.deliver_chat_id:
        route["deliver_extra"] = {"chat_id": args.deliver_chat_id}

    subs[name] = route
    _save_subscriptions(subs)

    base_url = _get_webhook_base_url()
    status = "Updated" if is_update else "Created"

    print(f"\n  {status} webhook subscription: {name}")
    print(f"  URL:    {base_url}/webhooks/{name}")
    print(f"  Secret: {secret}")
    if events:
        print(f"  Events: {', '.join(events)}")
    else:
        print("  Events: (all)")
    print(f"  Deliver: {route['deliver']}")
    if route.get("deliver_only"):
        print("  Mode: direct delivery (no agent, zero LLM cost)")
    if route.get("prompt"):
        prompt_preview = route["prompt"][:80] + ("..." if len(route["prompt"]) > 80 else "")
        label = "Message" if route.get("deliver_only") else "Prompt"
        print(f"  {label}: {prompt_preview}")
    print(f"\n  Configure your service to POST to the URL above.")
    print(f"  Use the secret for HMAC-SHA256 signature validation.")
    print(f"  The gateway must be running to receive events (hermes gateway run).\n")


def _cmd_list(args):
    subs = _load_subscriptions()
    if not subs:
        print("  No dynamic webhook subscriptions.")
        print("  Create one with: hermes webhook subscribe <name>")
        return

    base_url = _get_webhook_base_url()
    print(f"\n  {len(subs)} webhook subscription(s):\n")
    for name, route in subs.items():
        events = ", ".join(route.get("events", [])) or "(all)"
        deliver = route.get("deliver", "log")
        if route.get("deliver_only"):
            deliver = f"{deliver} (direct — no agent)"
        desc = route.get("description", "")
        print(f"  ◆ {name}")
        if desc:
            print(f"    {desc}")
        print(f"    URL:     {base_url}/webhooks/{name}")
        print(f"    Events:  {events}")
        print(f"    Deliver: {deliver}")
        print()


def _cmd_remove(args):
    name = args.name.strip().lower()
    subs = _load_subscriptions()

    if name not in subs:
        print(f"  No subscription named '{name}'.")
        print("  Note: Static routes from config.yaml cannot be removed here.")
        return

    del subs[name]
    _save_subscriptions(subs)
    print(f"  Removed webhook subscription: {name}")


def _cmd_test(args):
    """Send a test POST to a webhook route."""
    name = args.name.strip().lower()
    subs = _load_subscriptions()

    if name not in subs:
        print(f"  No subscription named '{name}'.")
        return

    route = subs[name]
    secret = route.get("secret", "")
    base_url = _get_webhook_base_url()
    url = f"{base_url}/webhooks/{name}"

    payload = args.payload or '{"test": true, "event_type": "test", "message": "Hello from hermes webhook test"}'

    import hmac
    import hashlib
    sig = "sha256=" + hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()

    print(f"  Sending test POST to {url}")
    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            data=payload.encode(),
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "test",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            print(f"  Response ({resp.status}): {body}")
    except Exception as e:
        print(f"  Error: {e}")
        print("  Is the gateway running? (hermes gateway run)")
