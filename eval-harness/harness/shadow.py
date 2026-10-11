"""Shadow mode: the agent-assist period is where the evidence for auto-send comes from.

While every draft still goes to a human, the full quality layer runs alongside and logs what it
would have done (`shadow/log_schema.json`). The human's action on each draft is the label: a
draft the candidate configuration would have auto-sent, but which a reviewer had to change
materially (severity major or critical), is an escaped failure. Those records come from real
production traffic, which fixes the main weakness of the 26-ticket sample (constructed, not
random), and they feed the shadow-mode release gates directly.

`audit_sample` picks which approved drafts a second reviewer re-checks, so reviewer edit tags
stay honest and approving on autopilot gets caught.
"""

import difflib
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from .data import BLOCKING_SEVERITIES
from .gates import escape_gate
from .stats import fmt_rate

REQUIRED_KEYS = ["ticket_id", "received_at", "synthetic", "intents", "risk_tier", "routing", "retrieval",
                 "draft", "guardrail", "review"]
REVIEW_ACTIONS = {"approved_unchanged", "edited", "rewritten", "handed_off", "discarded"}
EDIT_SEVERITIES = {"none", "minor", "major", "critical", ""}


def validate_record(r: dict) -> list[str]:
    """Light structural check (the full contract is shadow/log_schema.json)."""
    problems = [f"missing {k}" for k in REQUIRED_KEYS if k not in r]
    if problems:
        return problems
    if r["review"].get("action") not in REVIEW_ACTIONS:
        problems.append(f"review.action {r['review'].get('action')!r}")
    if r["review"].get("edit_severity") not in EDIT_SEVERITIES:
        problems.append(f"review.edit_severity {r['review'].get('edit_severity')!r}")
    if "would_auto_send" not in r["guardrail"]:
        problems.append("guardrail.would_auto_send missing")
    return problems


def load_log(path: Path) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            if line.strip():
                record = json.loads(line)
                if problems := validate_record(record):
                    raise ValueError(f"{path}:{line_no}: {'; '.join(problems)}")
                records.append(record)
    return records


def write_log(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def severity(r: dict) -> str:
    """Adjudicated severity: an audit overrides the reviewer's own tag."""
    return r["audit"]["auditor_severity"] if r.get("audit") else r["review"]["edit_severity"]


def _guardrail_held(r: dict) -> bool:
    return not r["guardrail"]["would_auto_send"]


def analyze(records: list[dict], cfg: dict) -> dict:
    would = [r for r in records if r["guardrail"]["would_auto_send"]]
    material = [r for r in records if severity(r) in BLOCKING_SEVERITIES]
    good = [r for r in records if severity(r) == "none"]
    escaped = [r for r in would if severity(r) in BLOCKING_SEVERITIES]
    escaped_critical = [r for r in would if severity(r) == "critical"]
    by_tier = defaultdict(lambda: [0, 0, 0])  # drafts, would-send, escaped
    for r in records:
        row = by_tier[r["risk_tier"]]
        row[0] += 1
        row[1] += r["guardrail"]["would_auto_send"]
        row[2] += r["guardrail"]["would_auto_send"] and severity(r) in BLOCKING_SEVERITIES
    return {
        "n": len(records),
        "synthetic": sum(r["synthetic"] for r in records),
        "would_auto_send": fmt_rate(len(would), len(records)),
        "guardrail_recall": fmt_rate(sum(_guardrail_held(r) for r in material), len(material)),
        "false_hold": fmt_rate(sum(_guardrail_held(r) for r in good), len(good)),
        "escaped_ids": [r["ticket_id"] for r in escaped],
        "untagged": sum(severity(r) == "" for r in records),
        "retrieval_logged": fmt_rate(sum(r["retrieval"] is not None for r in records), len(records)),
        "reviewer_overrode_guardrail": [r["ticket_id"] for r in records
                                        if _guardrail_held(r) and r["review"]["action"] == "approved_unchanged"],
        "by_tier": {tier: {"drafts": v[0], "would_send": v[1], "escaped": v[2]} for tier, v in sorted(by_tier.items())},
        "gates": [
            escape_gate("Escaped critical rate (shadow)", len(escaped_critical), len(would), cfg["target_escaped_critical_rate"]),
            escape_gate("Escaped release-blocking rate (shadow)", len(escaped), len(would), cfg["target_escaped_blocking_rate"]),
        ],
    }


def _in_sample(ticket_id: str, rate: float, salt: str) -> bool:
    """Deterministic pseudo-random draw, so an audit sample is reproducible from the log."""
    digest = hashlib.sha256(f"{salt}:{ticket_id}".encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF < rate


def audit_sample(records: list[dict], rate: float, salt: str = "audit") -> list[tuple[str, str]]:
    """Records a second reviewer should re-check, with the reason. Rules, in order:
    1. the reviewer approved, unchanged, a draft the system would not have auto-sent (automation
       bias, or a guardrail false positive: the gate numbers depend on knowing which);
    2. a large edit tagged none/minor or left untagged (the tag probably understates the fix);
    3. any record with no edit tag;
    4. a random `rate` share of approved drafts the guardrail would have auto-sent (the population
       the escaped-failure gates are measured on).
    """
    picked = []
    for r in records:
        sev = r["review"]["edit_severity"]
        edited = r["review"]["action"] in {"edited", "rewritten"}
        change = 1 - difflib.SequenceMatcher(None, r["draft"], r["review"]["final_reply"]).ratio()
        if _guardrail_held(r) and r["review"]["action"] == "approved_unchanged":
            picked.append((r["ticket_id"], "approved unchanged a draft the system would not auto-send"))
        elif edited and sev in {"", "none", "minor"} and change > 0.4:
            picked.append((r["ticket_id"], f"large edit ({change:.0%} changed) tagged {sev or 'nothing'}"))
        elif sev == "":
            picked.append((r["ticket_id"], "no edit tag"))
        elif (r["review"]["action"] == "approved_unchanged" and r["guardrail"]["would_auto_send"]
              and _in_sample(r["ticket_id"], rate, salt)):
            picked.append((r["ticket_id"], f"random {rate:.0%} sample of approved auto-send candidates"))
    return picked


def demo_log(tickets, labels, judge_records, judge_model, generic_hits, input_routes, held, queues, intents_fn, tier_fn) -> list[dict]:
    """Synthetic shadow log for the 26 sample tickets, to exercise the analysis end to end.

    The reviewer's action is SIMULATED from the reference labels (none -> approved unchanged,
    minor -> edited by deleting the labelled offending span, major/critical -> rewritten). Retrieval
    is null because the source data did not log it. Every record has synthetic=true; none of this is
    production evidence.
    """
    records = []
    for i, t in enumerate(tickets):
        lab = labels[t.id]
        j = judge_records[t.id]["evaluation"]
        routes = input_routes[t.id]
        sev = lab["severity"]
        intents = intents_fn(t.ticket_text)
        if sev == "none":
            final = t.assistant_answer
        elif sev == "minor":
            final = " ".join(t.assistant_answer.replace(lab["offending_text"], "").split())
        else:
            final = f"[simulated rewrite] {lab['expected_behavior']}"
        records.append({
            "ticket_id": t.id,
            "received_at": f"2026-10-01T09:{i:02d}:00Z",
            "synthetic": True,
            "intents": intents,
            "risk_tier": tier_fn(intents),
            "routing": {"decision": "handoff" if routes else "auto_send_candidate", "reasons": routes,
                        "queue": queues.get(routes[0], "") if routes else ""},
            "retrieval": None,
            "draft": t.assistant_answer,
            "guardrail": {
                "rules_fired": generic_hits[t.id],
                "judge_model": judge_model,
                "judge_severity": j["severity"],
                "judge_should_route": j["escalation"]["should_route_to_human"],
                "flagged_claims": sum(c["verdict"] != "supported" for c in j["claims"]),
                "would_auto_send": not held[t.id],
            },
            "review": {
                "action": {"none": "approved_unchanged", "minor": "edited"}.get(sev, "rewritten"),
                "final_reply": final,
                "edit_severity": sev,
                "edit_reasons": lab["failure_modes"],
                "reviewer": "simulated from reviews/labels.csv",
                "seconds": 0,
            },
        })
    return records
