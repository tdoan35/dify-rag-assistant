"""Stage the auto-send rollout by detected intent, not by the ticket's `category` column.

`category` holds one label per ticket, even when a ticket asks about several topics, so a
category-based stage can admit a high-risk question hidden in a low-risk category (TCK-1006 is
tagged `api_keys`, but its critical failure is about SSO). This module detects every intent in the
ticket text with keyword patterns drawn from the guide's own vocabulary, assigns a risk tier, and
compares stage eligibility by category with eligibility by detected intent.

Risk tiers are a policy choice, not a measurement: high = money, plan entitlements, or topics the
guide routes to specialists; low = how-to questions about the Knowledge Pipeline.
"""

import re

from .rules import route_low_information, route_multi_part, route_out_of_scope
from .similarity import SIGNATURE_RE

INTENT_PATTERNS = {
    "enterprise": r"\b(sso|saml|single sign[- ]on|audit[- ]logs?|soc ?2|compliance|certifications?|self[- ]host\w*|"
                  r"on[- ]prem\w*|community edition|plugin sdk|enterprise|success manager)\b",
    "billing_plans": r"\b(sandbox|professional|team plan|free plan|upgrade|downgrad\w*|price|pricing|cost|how much|pay|"
                     r"payment|invoice|bank transfer|billing|subscription|seats?|credits?|save money|plan)\b",
    "account_workspace": r"\b(teammates?|members?|invite\w*|roles?|team of \d+|how many people)\b",
    "api_keys": r"\b(api keys?|secret key|401|unauthori[sz]ed|api calls?|rate limit\w*|endpoint|backend)\b",
    "model_providers": r"\b(openai|anthropic|azure|model providers?|providers?|own model|my own model|marketplace)\b",
    "rag_pipeline": r"\b(chunk\w*|datasets?|knowledge base|documents?|upload\w*|index\w*|retriev\w*|hit testing|"
                    r"top_k|threshold|rerank\w*)\b",
}
INTENT_RES = {k: re.compile(v, re.IGNORECASE) for k, v in INTENT_PATTERNS.items()}
RISK = {"enterprise": "high", "billing_plans": "high", "account_workspace": "medium", "api_keys": "medium",
        "model_providers": "medium", "rag_pipeline": "low"}
TIER_ORDER = {"low": 0, "medium": 1, "high": 2}
# The reference labels' intent vocabulary, mapped onto the detector's.
LABEL_INTENT_MAP = {"enterprise_sso": "enterprise", "enterprise_compliance": "enterprise"}
CATEGORY_TIER = {"rag_pipeline": "low", "api_keys": "medium", "account_workspace": "medium",
                 "model_providers": "medium", "billing_plans": "high"}


def detect_intents(ticket_text: str) -> list[str]:
    body = SIGNATURE_RE.split(ticket_text)[0]
    return [name for name, pattern in INTENT_RES.items() if pattern.search(body)]


def risk_tier(intents: list[str]) -> str:
    """Highest-risk intent wins; a ticket with no recognisable intent is treated as high risk."""
    if not intents:
        return "high"
    return max((RISK[i] for i in intents), key=TIER_ORDER.get)


def stage_eligible(tier: str, stage: int) -> bool:
    """Stage 1 admits low-risk tickets, stage 2 adds medium, stage 3 adds high."""
    return TIER_ORDER[tier] < stage


def score_intents(tickets, labels) -> dict:
    rows = []
    for t in tickets:
        detected = detect_intents(t.ticket_text)
        human = sorted({LABEL_INTENT_MAP.get(i, i) for i in labels[t.id]["intents"]})
        routed = any(f(t, t.assistant_answer) for f in (route_out_of_scope, route_multi_part, route_low_information))
        tier = risk_tier(detected)
        rows.append({
            "id": t.id,
            "category": t.category,
            "human_intents": ";".join(human),
            "detected_intents": ";".join(detected),
            "missed_intents": ";".join(i for i in human if i not in detected),
            "extra_intents": ";".join(i for i in detected if i not in human),
            "detected_tier": tier,
            "human_tier": risk_tier(human),
            "category_tier": CATEGORY_TIER[t.category],
            "input_routed": int(routed),
            "severity": labels[t.id]["severity"],
            "release_blocking": int(labels[t.id]["release_blocking"]),
        })

    def stage(rows_in, tier_key: str, n: int, respect_routing: bool) -> dict:
        admitted = [r for r in rows_in if stage_eligible(r[tier_key], n) and not (respect_routing and r["input_routed"])]
        hidden = [r["id"] for r in admitted if TIER_ORDER[r["human_tier"]] >= n]
        return {
            "admitted": [r["id"] for r in admitted],
            "blocking": [r["id"] for r in admitted if r["release_blocking"]],
            "hidden_higher_risk": hidden,
        }

    staging = []
    for n in (1, 2):
        staging.append((f"stage {n} by category", stage(rows, "category_tier", n, respect_routing=False)))
        staging.append((f"stage {n} by detected intent (+ input routing)", stage(rows, "detected_tier", n, respect_routing=True)))
    exact = sum(1 for r in rows if not r["missed_intents"] and not r["extra_intents"])
    high_risk_missed = [r["id"] for r in rows if TIER_ORDER[r["detected_tier"]] < TIER_ORDER[r["human_tier"]]]
    category_mismatch = [r["id"] for r in rows if r["human_intents"] != r["category"]]
    return {"rows": rows, "staging": staging, "exact": exact, "tier_underestimated": high_risk_missed,
            "category_mismatch": category_mismatch}
