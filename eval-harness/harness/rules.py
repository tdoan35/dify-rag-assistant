"""Deterministic checks: input-side routing (before generation) and output-side guardrails.

Every rule records its provenance:
  generic     encodes a general policy that is not tied to one observed failure (for example
              "every number must appear in the guide"); still chosen after reading the sample.
  regression  encodes a failure observed in this sample; it proves the failure can be caught by a
              rule and guards against regressions, but says nothing about generalisation.
"""

import re
from dataclasses import dataclass
from typing import Callable

from .data import Ticket
from .similarity import SIGNATURE_RE, split_sentences, tokenize


@dataclass(frozen=True)
class Rule:
    id: str
    stage: str  # "input" (route before generation) or "output" (check the drafted answer)
    provenance: str  # "generic" or "regression"
    description: str
    check: Callable[[Ticket, str], str | None]  # returns evidence text when the rule fires


def _ticket_body(ticket: Ticket) -> str:
    return SIGNATURE_RE.split(ticket.ticket_text)[0]


# --- Input-side routing -------------------------------------------------------------------------

# Topics the guide declares out of scope ("What this guide does not cover") plus the
# Enterprise-only capabilities it lists. These go to a specialist instead of the assistant.
OUT_OF_SCOPE_RE = re.compile(
    r"\b(sso|saml|single sign[- ]on|audit[- ]logs?|soc ?2|compliance|certifications?|"
    r"self[- ]host(?:ed|ing)?|community edition|on[- ]prem(?:ise)?|plugin sdk|enterprise)\b",
    re.IGNORECASE,
)
MULTI_PART_RE = re.compile(
    r"\b(two|both|several) (things|questions|issues)\b|\bfirst\b.*\bsecond\b|"
    r"\band separately\b|\band also\b|\bhelp with both\b",
    re.IGNORECASE | re.DOTALL,
)


def route_out_of_scope(ticket: Ticket, _answer: str) -> str | None:
    m = OUT_OF_SCOPE_RE.search(ticket.ticket_text)
    return f"mentions '{m.group(0)}'" if m else None


def route_multi_part(ticket: Ticket, _answer: str) -> str | None:
    body = _ticket_body(ticket)
    if m := MULTI_PART_RE.search(body):
        return f"multi-part marker '{m.group(0)[:30]}'"
    if len(re.findall(r"\?+", body)) >= 2:
        return "two or more questions"
    return None


def route_low_information(ticket: Ticket, _answer: str) -> str | None:
    tokens = tokenize(_ticket_body(ticket))
    return f"only {len(tokens)} content words" if len(tokens) < 6 else None


# --- Output-side guardrails ---------------------------------------------------------------------

ERROR_LEAK_RE = re.compile(
    r"\[error|retrieval_status|no_chunks_returned|_timeout\b|traceback|stack trace|exception:",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def check_error_leak(_ticket: Ticket, answer: str) -> str | None:
    m = ERROR_LEAK_RE.search(answer)
    return f"internal string '{m.group(0)}'" if m else None


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUMBER_RE.findall(text)}


def make_numeric_grounding(guide: str) -> Callable[[Ticket, str], str | None]:
    """Every number in the answer must appear in the guide or in the customer's own ticket."""
    allowed = _numbers(guide)

    def check(ticket: Ticket, answer: str) -> str | None:
        novel = sorted(_numbers(answer) - allowed - _numbers(ticket.ticket_text), key=float)
        return f"numbers not in guide or ticket: {', '.join(novel)}" if novel else None

    return check


# Plan limits from the guide's Section 4 table, used to check numbers attributed to a plan.
PLAN_FACTS = {
    "Sandbox": {"price": {0}, "credits": {200}, "members": {1}, "apps": {5}, "documents": {50},
                "storage": {"50mb"}, "calls": {5000}},
    "Professional": {"price": {59, 590}, "credits": {5000}, "members": {3}, "apps": {50},
                     "documents": {500}, "storage": {"5gb"}, "calls": set()},
    "Team": {"price": {159, 1590}, "credits": {10000}, "members": {50}, "apps": {200},
             "documents": {1000}, "storage": {"20gb"}, "calls": set()},
}
PER_RESPONSE_CREDITS = {1, 5, 10}
QUANTITY_PATTERNS = {
    "price": re.compile(r"\$\s?(\d[\d,]*)"),
    "credits": re.compile(r"(\d[\d,]*)\s+(?:message\s+)?credits?\b", re.IGNORECASE),
    "members": re.compile(r"(\d[\d,]*)\s+(?:team\s+)?(?:members|seats)\b", re.IGNORECASE),
    "apps": re.compile(r"(\d[\d,]*)\s+apps\b", re.IGNORECASE),
    "documents": re.compile(r"(\d[\d,]*)\s+(?:knowledge\s+)?documents\b", re.IGNORECASE),
    "storage": re.compile(r"(\d[\d,]*)\s*(gb|mb)\b", re.IGNORECASE),
    "calls": re.compile(r"(\d[\d,]*)\s+(?:api\s+)?calls\b", re.IGNORECASE),
}
PLAN_RE = re.compile(r"\b(Sandbox|Professional|Team)\b")


def check_plan_facts(_ticket: Ticket, answer: str) -> str | None:
    """Numbers stated next to a plan name must match that plan's row in the guide's table."""
    problems = []
    for sentence in split_sentences(answer):
        plans = set(PLAN_RE.findall(sentence)) or set(PLAN_FACTS)
        for kind, pattern in QUANTITY_PATTERNS.items():
            for m in pattern.finditer(sentence):
                if kind == "storage":
                    value = m.group(1).replace(",", "") + m.group(2).lower()
                else:
                    value = int(m.group(1).replace(",", ""))
                allowed = set().union(*(PLAN_FACTS[p][kind] for p in plans))
                if kind == "credits" and "per response" in sentence.lower():
                    allowed |= PER_RESPONSE_CREDITS
                if value not in allowed:
                    problems.append(f"{kind}={value} for {'/'.join(sorted(plans))}")
    return "; ".join(problems) or None


ENTERPRISE_FEATURE_RE = re.compile(
    r"\b(sso|saml|single sign[- ]on|audit[- ]log\w*|customer success manager)\b", re.IGNORECASE
)
CLOUD_PLAN_RE = re.compile(r"\b(Sandbox|Professional|Team plan|Team)\b")
ENTERPRISE_HEDGE_RE = re.compile(
    r"\benterprise\b|\bsales\b|not (?:available|included)|isn't (?:available|included)",
    re.IGNORECASE,
)


def check_enterprise_feature_on_cloud_plan(_ticket: Ticket, answer: str) -> str | None:
    """The guide lists SSO, audit logging and a dedicated CSM as Enterprise-only capabilities."""
    for sentence in split_sentences(answer):
        if (ENTERPRISE_FEATURE_RE.search(sentence) and CLOUD_PLAN_RE.search(sentence)
                and not ENTERPRISE_HEDGE_RE.search(sentence)):
            return f"Enterprise-only feature tied to a Cloud plan: '{sentence[:90]}'"
    return None


# Explicit negative statements in the guide, paired with patterns that assert the opposite.
GUIDE_NEGATIONS = [
    ("§3: empty retrieval is 'not [explained] by an account or provider-propagation delay'",
     re.compile(r"propagat", re.IGNORECASE),
     re.compile(r"retriev|chunks?|results?", re.IGNORECASE)),
    ("§1: 'there is no separate \"rotate\" action'",
     re.compile(r"\brotate (button|action|option)\b", re.IGNORECASE),
     re.compile(r".")),
    ("§4: invoice / bank transfer 'is not available on any plan today'",
     re.compile(r"(can|able to) pay (by|via|with) (invoice|bank transfer)", re.IGNORECASE),
     re.compile(r".")),
]


def check_guide_negations(_ticket: Ticket, answer: str) -> str | None:
    for source, assertion, context in GUIDE_NEGATIONS:
        if assertion.search(answer) and context.search(answer):
            return f"asserts what the guide rules out ({source})"
    return None


def check_per_seat_premise(ticket: Ticket, answer: str) -> str | None:
    """Ticket assumes per-seat billing; the guide says billing is per workspace (§4)."""
    body = _ticket_body(ticket)
    if (re.search(r"\bseats?\b", body, re.IGNORECASE)
            and re.search(r"save|cost|bill|price|money|cheaper", body, re.IGNORECASE)
            and not re.search(r"per[- ]workspace", answer, re.IGNORECASE)):
        return "per-seat savings premise not corrected (billing is per workspace)"
    return None


def build_rules(guide: str) -> list[Rule]:
    return [
        Rule("route_out_of_scope", "input", "generic",
             "Topic the guide declares out of scope or Enterprise-only -> specialist", route_out_of_scope),
        Rule("route_multi_part", "input", "generic",
             "Ticket asks more than one question -> human (retrieval covers one intent)", route_multi_part),
        Rule("route_low_information", "input", "generic",
             "Too little content to retrieve against -> clarify / human", route_low_information),
        Rule("error_leak", "output", "generic",
             "Raw system error or internal status string in the answer", check_error_leak),
        Rule("numeric_grounding", "output", "generic",
             "Every number must appear in the guide or the ticket", make_numeric_grounding(guide)),
        Rule("plan_facts", "output", "generic",
             "Numbers attributed to a plan must match the guide's plan table", check_plan_facts),
        Rule("enterprise_feature_on_cloud_plan", "output", "regression",
             "Enterprise-only capability attributed to a Cloud plan", check_enterprise_feature_on_cloud_plan),
        Rule("guide_negations", "output", "regression",
             "Asserts something the guide explicitly rules out", check_guide_negations),
        Rule("per_seat_premise", "output", "regression",
             "Fails to correct a per-seat billing premise", check_per_seat_premise),
    ]


def apply_rules(rules: list[Rule], ticket: Ticket) -> dict[str, str]:
    """Rule id -> evidence for every rule that fires on this ticket/answer pair."""
    hits = {}
    for rule in rules:
        evidence = rule.check(ticket, ticket.assistant_answer)
        if evidence:
            hits[rule.id] = evidence
    return hits
