"""Held-out fault-injection set.

The 26-ticket sample has only 6 release-blocking answers, and the rules and rubric were written
after reading them, so the sample alone cannot show whether the guardrails generalise. This module
builds a second test set by injecting known fault types into the answers the reference labels rate
`none` (plus a few new multi-part tickets), and rewording some answers without changing their
meaning as controls. See `heldout/mutations.json`.

Integrity: `heldout/MANIFEST.json` records hashes of the guardrail code and the judge prompt at the
moment the set was frozen. The report flags any later change, because a rule tuned after seeing
these cases no longer measures generalisation on them.
"""

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .data import BLOCKING_SEVERITIES, ROOT, SEVERITIES, Ticket, load_tickets
from .judge import load_prompt, prompt_fingerprint

HELDOUT_DIR = ROOT / "heldout"
SPEC_PATH = HELDOUT_DIR / "mutations.json"
SIGNOFF_PATH = HELDOUT_DIR / "signoff.csv"
MANIFEST_PATH = HELDOUT_DIR / "MANIFEST.json"
# Files whose behaviour the held-out set is meant to test without having been tuned on it.
GUARDRAIL_FILES = ["harness/rules.py", "harness/similarity.py"]


@dataclass(frozen=True)
class Mutant:
    ticket: Ticket
    base_id: str
    fault_type: str
    severity: str
    failure_modes: tuple[str, ...]
    offending_text: str
    rationale: str
    human_signoff: str

    @property
    def release_blocking(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES


def _render(m: dict, bases: dict[str, Ticket]) -> Ticket:
    op = m["op"]
    if op == "new_ticket":
        return Ticket(m["id"], m["ticket_text"], m["answer"], m["category"])
    base = bases[m["base_id"]]
    if op == "replace":
        if base.assistant_answer.count(m["find"]) != 1:
            raise ValueError(f"{m['id']}: `find` must occur exactly once in {m['base_id']}")
        answer = base.assistant_answer.replace(m["find"], m["replace"])
    elif op == "append":
        answer = base.assistant_answer + m["text"]
    elif op == "rewrite":
        answer = m["answer"]
    else:
        raise ValueError(f"{m['id']}: unknown op {op!r}")
    return Ticket(m["id"], base.ticket_text, answer, base.category)


def load_signoff(path: Path = SIGNOFF_PATH) -> dict[str, dict]:
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {row["id"]: row for row in csv.DictReader(f)}


def load_mutants(path: Path = SPEC_PATH) -> list[Mutant]:
    """Render every case into a Ticket; a human `severity_override` in signoff.csv wins."""
    spec = json.loads(path.read_text(encoding="utf-8"))
    bases = {t.id: t for t in load_tickets()}
    signoff = load_signoff()
    mutants, seen = [], set()
    for m in spec["mutants"]:
        if m["id"] in seen:
            raise ValueError(f"duplicate case id {m['id']}")
        seen.add(m["id"])
        ticket = _render(m, bases)
        if m["offending_text"] and m["offending_text"] not in ticket.assistant_answer:
            raise ValueError(f"{m['id']}: offending_text is not in the rendered answer")
        review = signoff.get(m["id"], {})
        severity = (review.get("severity_override") or "").strip() or m["severity"]
        if severity not in SEVERITIES:
            raise ValueError(f"{m['id']}: unknown severity {severity!r}")
        mutants.append(Mutant(
            ticket=ticket,
            base_id=m["base_id"],
            fault_type=m["fault_type"],
            severity=severity,
            failure_modes=tuple(m["failure_modes"]),
            offending_text=m["offending_text"],
            rationale=m["rationale"],
            human_signoff=(review.get("human_signoff") or "").strip(),
        ))
    return mutants


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_fingerprints() -> dict:
    system, schema = load_prompt()
    return {
        "spec_sha256": _sha256(SPEC_PATH),
        "guardrail_sha256": {f: _sha256(ROOT / f) for f in GUARDRAIL_FILES},
        "judge_prompt_fingerprint": prompt_fingerprint(system, schema),
    }


def freeze(author: str, force: bool = False) -> dict:
    """Record what the held-out set was created against. Refuses to overwrite unless forced."""
    if MANIFEST_PATH.exists() and not force:
        raise SystemExit(f"{MANIFEST_PATH} already exists; re-freezing resets the held-out guarantee (use --force)")
    load_mutants()  # validate before freezing
    manifest = {"frozen_on": date.today().isoformat(), "author": author, **current_fingerprints()}
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def integrity_warnings() -> list[str]:
    """Differences between the frozen manifest and the current guardrail code, prompt and spec."""
    if not MANIFEST_PATH.exists():
        return ["held-out set was never frozen (no heldout/MANIFEST.json)"]
    frozen = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    now = current_fingerprints()
    warnings = []
    if frozen["spec_sha256"] != now["spec_sha256"]:
        warnings.append("heldout/mutations.json changed after the set was frozen")
    for f, digest in frozen["guardrail_sha256"].items():
        if now["guardrail_sha256"].get(f) != digest:
            warnings.append(f"{f} changed after the held-out set was frozen; its results on this set no longer measure generalisation")
    if frozen["judge_prompt_fingerprint"] != now["judge_prompt_fingerprint"]:
        warnings.append("the judge prompt changed after the held-out set was frozen")
    return warnings


# --- Scoring ------------------------------------------------------------------------------------

def score_heldout(rules, judge_models: list[str], primary: str) -> dict | None:
    """Run the frozen rules and the cached judges on every case. None if no judge outputs exist."""
    from collections import defaultdict

    from .judge import HELDOUT_CACHE_DIR, load_judgments
    from .rules import apply_rules
    from .stats import fmt_rate, wilson

    mutants = load_mutants()
    judgments = {}
    for model in judge_models:
        runs = load_judgments(model, HELDOUT_CACHE_DIR)
        if 1 in runs and len(runs[1]) == len(mutants):
            judgments[model] = runs[1]
    if primary not in judgments:
        return None
    rule_by_id = {r.id: r for r in rules}
    blocking = [m for m in mutants if m.release_blocking]
    critical = [m for m in mutants if m.severity == "critical"]
    passing = [m for m in mutants if not m.release_blocking]
    controls = [m for m in mutants if m.severity == "none"]

    def judge_blocks(model: str, m: Mutant) -> bool:
        return judgments[model][m.ticket.id]["evaluation"]["severity"] in BLOCKING_SEVERITIES

    detail = {}
    for m in mutants:
        hits = apply_rules(rules, m.ticket)
        kinds = {r: (rule_by_id[r].stage, rule_by_id[r].provenance) for r in hits}
        routed = any(k == ("input", "generic") for k in kinds.values())
        generic_out = any(k == ("output", "generic") for k in kinds.values())
        any_out = any(k[0] == "output" for k in kinds.values())
        detail[m.ticket.id] = {
            "hits": hits,
            "routed": routed,
            "detectors": {
                "Generic output rules": generic_out,
                "All output rules (incl. regression)": any_out,
                **{f"Judge: {model}": judge_blocks(model, m) for model in judgments},
                f"Output layer: generic rules + {primary} judge": generic_out or judge_blocks(primary, m),
                f"C3-gen ({primary}): routing + generic rules + judge": routed or generic_out or judge_blocks(primary, m),
                f"C3 ({primary}): C3-gen + regression rules": routed or any_out or judge_blocks(primary, m),
            },
        }

    detector_names = list(next(iter(detail.values()))["detectors"])
    summary_rows = []
    for name in detector_names:
        def caught(group):
            return sum(detail[m.ticket.id]["detectors"][name] for m in group)
        summary_rows.append({
            "detector": name,
            "blocking_caught": fmt_rate(caught(blocking), len(blocking)),
            "blocking_lower": wilson(caught(blocking), len(blocking))[0],
            "critical_caught": fmt_rate(caught(critical), len(critical)),
            "false_holds": fmt_rate(caught(passing), len(passing)),
            "controls_held": f"{caught(controls)}/{len(controls)}",
            "_caught_blocking": caught(blocking),
            "_caught_critical": caught(critical),
        })

    by_fault = defaultdict(list)
    for m in mutants:
        by_fault[(m.fault_type, m.release_blocking)].append(m)
    fault_rows = []
    c3gen = f"C3-gen ({primary}): routing + generic rules + judge"
    for (fault, is_blocking), group in sorted(by_fault.items(), key=lambda kv: (not kv[0][1], kv[0][0])):
        row = {"fault_type": fault, "should_block": "yes" if is_blocking else "no", "n": len(group),
               "generic_rules": sum(detail[m.ticket.id]["detectors"]["Generic output rules"] for m in group),
               "input_routing": sum(detail[m.ticket.id]["routed"] for m in group)}
        for model in judgments:
            row[f"judge_{model}"] = sum(judge_blocks(model, m) for m in group)
        row["c3_gen"] = sum(detail[m.ticket.id]["detectors"][c3gen] for m in group)
        fault_rows.append(row)

    disagreements = []
    for m in mutants:
        verdicts = {model: judgments[model][m.ticket.id]["evaluation"]["severity"] for model in judgments}
        if any((v in BLOCKING_SEVERITIES) != m.release_blocking for v in verdicts.values()):
            disagreements.append([m.ticket.id, m.fault_type, m.severity,
                                  ", ".join(f"{k}: {v}" for k, v in verdicts.items())])

    severity_exact = {model: sum(judgments[model][m.ticket.id]["evaluation"]["severity"] == m.severity for m in mutants)
                      for model in judgments}
    per_case = []
    for m in mutants:
        row = {"id": m.ticket.id, "base_id": m.base_id, "fault_type": m.fault_type, "expected_severity": m.severity,
               "release_blocking": int(m.release_blocking), "rules_fired": ";".join(detail[m.ticket.id]["hits"])}
        for model in judgments:
            row[f"judge_{model}"] = judgments[model][m.ticket.id]["evaluation"]["severity"]
        row["c3_gen"] = "held" if detail[m.ticket.id]["detectors"][c3gen] else "sent"
        row["ticket_text"] = m.ticket.ticket_text
        row["assistant_answer"] = m.ticket.assistant_answer
        per_case.append(row)
    cost = sum((r["meta"].get("cost_usd") or 0) for recs in judgments.values() for r in recs.values())
    return {
        "n": len(mutants), "n_blocking": len(blocking), "n_critical": len(critical), "n_passing": len(passing),
        "n_controls": len(controls), "summary_rows": summary_rows, "fault_rows": fault_rows,
        "disagreements": disagreements, "severity_exact": severity_exact, "per_case": per_case,
        "signed": sum(1 for m in mutants if m.human_signoff), "integrity": integrity_warnings(),
        "cost_usd": cost, "judges": list(judgments), "c3gen_name": c3gen,
    }
