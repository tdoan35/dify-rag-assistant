"""Release gates derived from tolerable harm instead of fixed sample sizes.

The rate of bad answers that reach customers is

    escaped rate = raw failure rate x (1 - guardrail recall)

so the guardrail recall a gate needs follows from the raw rate and the escaped rate the business
will tolerate, and the evidence a gate needs follows from Wilson bounds: a perfect record on n
cases only proves a recall of wilson(n, n).lower. Targets live in `gates.json` and are placeholders
until Support leadership sets them.
"""

import json
from dataclasses import dataclass

from .data import ROOT
from .stats import wilson

GATES_PATH = ROOT / "gates.json"


def load_gate_config() -> dict:
    return json.loads(GATES_PATH.read_text(encoding="utf-8"))


def required_recall(raw_rate: float, target_escaped: float) -> float:
    """Guardrail recall needed so that raw_rate x (1 - recall) <= target_escaped."""
    return 0.0 if raw_rate <= target_escaped else 1 - target_escaped / raw_rate


def positives_needed(recall: float, misses: int = 0, limit: int = 1_000_000) -> int | None:
    """Fewest labelled positives, with `misses` of them missed, whose Wilson lower bound on recall
    reaches `recall`. None if `recall` cannot be shown within `limit` cases."""
    if recall <= 0:
        return 0
    lo, hi = misses + 1, misses + 1
    while wilson(hi - misses, hi)[0] < recall:
        lo, hi = hi, hi * 2
        if hi > limit:
            return None
    while lo < hi:  # the lower bound rises with n for a fixed number of misses
        mid = (lo + hi) // 2
        if wilson(mid - misses, mid)[0] >= recall:
            hi = mid
        else:
            lo = mid + 1
    return lo


def cases_to_bound_rate(target: float, events: int = 0, limit: int = 10_000_000) -> int | None:
    """Fewest observed cases, with `events` failures among them, whose Wilson upper bound on the
    failure rate is at or below `target`."""
    lo, hi = max(1, events), max(1, events)
    while wilson(events, hi)[1] > target:
        lo, hi = hi, hi * 2
        if hi > limit:
            return None
    while lo < hi:
        mid = (lo + hi) // 2
        if wilson(events, mid)[1] <= target:
            hi = mid
        else:
            lo = mid + 1
    return lo


@dataclass
class Evidence:
    """caught/total for one source of recall evidence (for example, the held-out set)."""
    source: str
    caught: int
    total: int

    @property
    def lower(self) -> float:
        return wilson(self.caught, self.total)[0] if self.total else 0.0


def recall_gate(name: str, raw_k: int, raw_n: int, target: float, evidence: list[Evidence]) -> dict:
    """Is the guardrail's demonstrated recall enough to keep escapes under `target`?

    The required recall uses the raw rate's point estimate (status) and its Wilson upper bound
    (shown as the conservative requirement). Evidence is pooled across sources.
    """
    raw = raw_k / raw_n
    raw_hi = wilson(raw_k, raw_n)[1]
    need = required_recall(raw, target)
    need_hi = required_recall(raw_hi, target)
    caught = sum(e.caught for e in evidence)
    total = sum(e.total for e in evidence)
    lower = wilson(caught, total)[0] if total else 0.0
    misses = total - caught
    return {
        "gate": name,
        "raw_rate": f"{raw_k}/{raw_n} ({raw:.1%}; upper {raw_hi:.1%})",
        "target_escaped": target,
        "required_recall": need,
        "required_recall_conservative": need_hi,
        "evidence": f"{caught}/{total} caught" + (f" ({', '.join(f'{e.source} {e.caught}/{e.total}' for e in evidence)})" if len(evidence) > 1 else ""),
        "demonstrated_recall_lower": lower,
        "positives_needed": positives_needed(need, misses),
        "positives_needed_conservative": positives_needed(need_hi, misses),
        # Offline evidence (the sample plus synthetic held-out faults) can only show readiness for a
        # shadow-mode pilot; the shadow-mode gates decide auto-send.
        "status": "MET on offline evidence" if total and lower >= need else "NOT MET",
    }


def escape_gate(name: str, escaped: int, would_send: int, target: float) -> dict:
    """Measured in shadow mode: is the escaped rate among would-have-auto-sent drafts under target?"""
    upper = wilson(escaped, would_send)[1] if would_send else 1.0
    return {
        "gate": name,
        "target_escaped": target,
        "observed": f"{escaped}/{would_send}" if would_send else "no shadow data",
        "upper_bound": upper,
        "drafts_needed_if_zero_escapes": cases_to_bound_rate(target, 0),
        "drafts_needed_if_one_escape": cases_to_bound_rate(target, 1),
        "status": "MET" if would_send and upper <= target else ("NO DATA" if not would_send else "NOT MET"),
    }


def target_table(raw_k: int, raw_n: int, targets: list[float]) -> list[list]:
    """What different escaped-rate targets would demand, for the raw rate raw_k/raw_n."""
    raw = raw_k / raw_n
    raw_hi = wilson(raw_k, raw_n)[1]
    rows = []
    for t in targets:
        need, need_hi = required_recall(raw, t), required_recall(raw_hi, t)
        n, n_hi = positives_needed(need), positives_needed(need_hi)
        rows.append([f"{t:.2%}", f"{need:.1%}", n if n is not None else ">1M",
                     f"{need_hi:.1%}", n_hi if n_hi is not None else ">1M",
                     cases_to_bound_rate(t, 0)])
    return rows
