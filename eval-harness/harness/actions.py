"""Routing as an action, not as text.

In auto-send mode the system, not the draft, decides what happens to a ticket: send the draft,
hand the ticket to a named human queue (the customer gets a holding reply, never the draft), or
hold the draft for an agent to review. A draft that tells the customer to "contact support" from
inside a support ticket is a bounce: the right action is an internal handoff.

`reviews/actions.csv` holds the expected action per ticket and whether each draft bounces
(drafted by Claude, pending human sign-off). This module detects bounces, derives the system's
action under two designs, and scores both against the expected actions.
"""

import csv
import re
from dataclasses import dataclass

from .data import REVIEWS_DIR

ACTIONS = ["answer", "clarify", "handoff", "answer+handoff"]
NEEDS_HUMAN = {"handoff", "answer+handoff"}

# Phrases that send the customer elsewhere instead of routing the ticket internally.
# "email" and "call" are left out: as nouns ("priority email support") they cause false alarms.
BOUNCE_RE = re.compile(
    r"\b(contact(?:ing)?|reach(?:ing)? out to|get in touch with)\b[^.?!]{0,60}?"
    r"\b(support|sales|billing|account manager|success manager|team)\b"
    r"|\bresubmit\b",
    re.IGNORECASE,
)

# Input-routing rule -> human queue.
ROUTE_QUEUES = {
    "route_out_of_scope": "sales_enterprise",
    "route_low_information": "support_agent",
    "route_multi_part": "support_agent",
}


def detect_bounce(answer: str) -> str | None:
    m = BOUNCE_RE.search(answer)
    return f"'{m.group(0)}'" if m else None


def load_expected_actions(path=REVIEWS_DIR / "actions.csv") -> dict[str, dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = {row["id"]: row for row in csv.DictReader(f)}
    for tid, row in rows.items():
        if row["expected_action"] not in ACTIONS:
            raise ValueError(f"{tid}: unknown expected_action {row['expected_action']!r}")
        row["bounce"] = row["bounce"].strip() == "1"
    return rows


@dataclass
class SystemAction:
    action: str  # auto_send | handoff | hold_for_agent
    queue: str = ""
    reason: str = ""


def system_action(input_routes: list[str], output_rules: list[str], judge_eval: dict,
                  judge_blocks: bool, bounce: str | None, action_layer: bool) -> SystemAction:
    """C3-gen decision. With `action_layer`, the judge's routing verdict and the bounce detector
    also drive the decision (the proposed change)."""
    for rule in ("route_out_of_scope", "route_low_information", "route_multi_part"):
        if rule in input_routes:
            return SystemAction("handoff", ROUTE_QUEUES[rule], rule)
    if action_layer and judge_eval["escalation"]["should_route_to_human"]:
        return SystemAction("handoff", "specialist", "judge: should route to a human")
    if output_rules or judge_blocks:
        return SystemAction("hold_for_agent", "", "output rule" if output_rules else "judge blocks")
    if action_layer and bounce:
        return SystemAction("hold_for_agent", "", f"bounce {bounce}")
    return SystemAction("auto_send")


def outcome(expected: dict, label_severity: str, act: SystemAction) -> str:
    """How the ticket fares under the system's action, from the customer's side."""
    if expected["expected_action"] in NEEDS_HUMAN:
        if act.action == "handoff":
            want = expected["handoff_queue"]
            return "routed to the right queue" if act.queue in (want, "specialist") else f"routed to {act.queue} (wanted {want})"
        if act.action == "hold_for_agent":
            return "reached a person via a hold, not a routing decision"
        return "MISSED: auto-sent, never reached a person"
    good_draft = label_severity == "none" and not expected["bounce"]
    if act.action == "auto_send":
        return "auto-sent" if good_draft else ("BOUNCED customer" if expected["bounce"] else "auto-sent (non-blocking issue)")
    if good_draft:
        return f"good draft held ({act.reason})"
    return f"flawed draft held ({act.reason})"


def score_actions(tickets, labels, hits, rule_by_id, judge_records, judge_block) -> dict:
    expected = load_expected_actions()
    missing = {t.id for t in tickets} - expected.keys()
    if missing:
        raise ValueError(f"reviews/actions.csv is missing {sorted(missing)}")
    rows = []
    for t in tickets:
        exp = expected[t.id]
        routes = [r for r in hits[t.id] if rule_by_id[r].stage == "input" and rule_by_id[r].provenance == "generic"]
        outputs = [r for r in hits[t.id] if rule_by_id[r].stage == "output" and rule_by_id[r].provenance == "generic"]
        bounce = detect_bounce(t.assistant_answer)
        j = judge_records[t.id]["evaluation"]
        designed = system_action(routes, outputs, j, judge_block(t.id), bounce, action_layer=False)
        proposed = system_action(routes, outputs, j, judge_block(t.id), bounce, action_layer=True)
        rows.append({
            "id": t.id,
            "expected_action": exp["expected_action"],
            "expected_queue": exp["handoff_queue"],
            "draft_bounces": int(exp["bounce"]),
            "bounce_detector": bounce or "",
            "judge_should_route": int(j["escalation"]["should_route_to_human"]),
            "c3gen_action": f"{designed.action}{':' + designed.queue if designed.queue else ''}",
            "c3gen_outcome": outcome(exp, labels[t.id]["severity"], designed),
            "proposed_action": f"{proposed.action}{':' + proposed.queue if proposed.queue else ''}",
            "proposed_outcome": outcome(exp, labels[t.id]["severity"], proposed),
        })
    detector_tp = sum(1 for r in rows if r["draft_bounces"] and r["bounce_detector"])
    detector_fp = sum(1 for r in rows if not r["draft_bounces"] and r["bounce_detector"])
    detector_fn = sum(1 for r in rows if r["draft_bounces"] and not r["bounce_detector"])
    signed = sum(1 for e in expected.values() if e["human_signoff"].strip())
    return {"rows": rows, "detector": (detector_tp, detector_fp, detector_fn), "signed": signed}
