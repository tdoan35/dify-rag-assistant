"""Score the cached judgments, the tier-1 check and the rules against the reference labels, run the
guardrail replay, and write everything under results/. Runs fully offline."""

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from .data import BLOCKING_SEVERITIES, CRITERIA, RESULTS_DIR, SEVERITIES, load_guide, load_labels, load_tickets, validate_labels
from .judge import load_judgments
from .rules import apply_rules, build_rules
from .similarity import TfidfIndex, guide_windows, score_response, sentence_containing
from .stats import Confusion, fmt_rate, weighted_kappa, wilson

TIER1_FLAG_SHARES = (0.25, 0.50, 0.75)


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _md_table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _pct(x: float) -> str:
    return "n/a" if x != x else f"{x:.0%}"


def _blocking(severity: str) -> bool:
    return severity in BLOCKING_SEVERITIES


# --- Reference-label analysis -------------------------------------------------------------------

def label_summary(tickets, labels) -> dict:
    n = len(tickets)
    sev = Counter(labels[t.id]["severity"] for t in tickets)
    blocking = [t.id for t in tickets if labels[t.id]["release_blocking"]]
    critical = [t.id for t in tickets if labels[t.id]["severity"] == "critical"]

    def blocking_by(key_fn) -> list[list]:
        groups = defaultdict(list)
        for t in tickets:
            for key in key_fn(t):
                groups[key].append(labels[t.id]["release_blocking"])
        return [[k, fmt_rate(sum(v), len(v))] for k, v in sorted(groups.items())]

    return {
        "n": n,
        "severity_counts": {s: sev.get(s, 0) for s in SEVERITIES},
        "blocking_ids": blocking,
        "critical_ids": critical,
        "blocking_rate": fmt_rate(len(blocking), n),
        "critical_rate": fmt_rate(len(critical), n),
        "blocking_ci": wilson(len(blocking), n),
        "criteria_pass": {c: sum(labels[t.id][c] for t in tickets) for c in CRITERIA},
        "by_multi_part": blocking_by(lambda t: ["multi-part" if labels[t.id]["multi_part"] else "single question"]),
        "by_coverage": blocking_by(lambda t: [f"guide coverage: {labels[t.id]['guide_coverage']}"]),
        "by_category": blocking_by(lambda t: [t.category]),
        "failure_modes": Counter(m for t in tickets for m in labels[t.id]["failure_modes"]).most_common(),
    }


# --- Judge analysis -----------------------------------------------------------------------------

def judge_metrics(tickets, labels, runs: dict[int, dict[str, dict]]) -> list[dict]:
    rows = []
    truth_sev = [labels[t.id]["severity"] for t in tickets]
    truth_block = [labels[t.id]["release_blocking"] for t in tickets]
    for run, records in sorted(runs.items()):
        if len(records) < len(tickets):
            continue
        pred_sev = [records[t.id]["evaluation"]["severity"] for t in tickets]
        conf = Confusion.from_pairs(truth_block, [_blocking(s) for s in pred_sev])
        crit_truth = [s == "critical" for s in truth_sev]
        crit_caught = sum(1 for t, p in zip(crit_truth, pred_sev) if t and p == "critical")
        costs = [r["meta"].get("cost_usd") or 0 for r in records.values()]
        durations = [r["meta"].get("duration_ms") or 0 for r in records.values()]
        rows.append({
            "run": run,
            "tp": conf.tp, "fn": conf.fn, "fp": conf.fp, "tn": conf.tn,
            "tpr": conf.tpr, "tnr": conf.tnr, "kappa": conf.kappa,
            "severity_exact": sum(a == b for a, b in zip(truth_sev, pred_sev)) / len(tickets),
            "severity_wkappa": weighted_kappa(truth_sev, pred_sev),
            "critical_as_critical": f"{crit_caught}/{sum(crit_truth)}",
            "mean_cost_usd": sum(costs) / len(costs),
            "mean_seconds": sum(durations) / len(durations) / 1000,
        })
    return rows


def run_agreement(tickets, a: dict[str, dict], b: dict[str, dict]) -> tuple[float, float]:
    sev_a = [a[t.id]["evaluation"]["severity"] for t in tickets]
    sev_b = [b[t.id]["evaluation"]["severity"] for t in tickets]
    block = sum(_blocking(x) == _blocking(y) for x, y in zip(sev_a, sev_b)) / len(tickets)
    exact = sum(x == y for x, y in zip(sev_a, sev_b)) / len(tickets)
    return block, exact


def claim_profile(records: dict[str, dict]) -> dict:
    by_type = defaultdict(Counter)
    for r in records.values():
        for claim in r["evaluation"]["claims"]:
            by_type[claim["claim_type"]][claim["verdict"]] += 1
    return {k: dict(v) for k, v in sorted(by_type.items())}


# --- Guardrail replay ---------------------------------------------------------------------------

def replay(tickets, labels, held: dict[str, bool], judge_calls: int) -> dict:
    blocking = {t.id for t in tickets if labels[t.id]["release_blocking"]}
    critical = {t.id for t in tickets if labels[t.id]["severity"] == "critical"}
    escaped = sorted(i for i in blocking if not held[i])
    auto_sent = sum(not held[t.id] for t in tickets)
    held_ok = sum(held[t.id] for t in tickets if t.id not in blocking)
    return {
        "escaped_blocking": escaped,
        "escaped_critical": sorted(i for i in escaped if i in critical),
        "caught_blocking": f"{len(blocking) - len(escaped)}/{len(blocking)}",
        "auto_sent": auto_sent,
        "auto_send_rate": auto_sent / len(tickets),
        "held_non_blocking": held_ok,
        "judge_calls": judge_calls,
    }


def score_all(judge_models: list[str], primary_judge: str = "opus") -> None:
    tickets = load_tickets()
    guide = load_guide()
    labels = load_labels()
    problems = validate_labels(tickets, labels)
    if problems:
        raise SystemExit("label problems:\n" + "\n".join(problems))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ids = [t.id for t in tickets]

    # Reference labels
    summary = label_summary(tickets, labels)

    # Rules
    rules = build_rules(guide)
    rule_by_id = {r.id: r for r in rules}
    hits = {t.id: apply_rules(rules, t) for t in tickets}

    def fired(tid: str, stage: str | None = None, provenance: str | None = None) -> bool:
        return any(
            (stage is None or rule_by_id[r].stage == stage)
            and (provenance is None or rule_by_id[r].provenance == provenance)
            for r in hits[tid]
        )

    rule_rows = []
    for rule in rules:
        fired_ids = [i for i in ids if rule.id in hits[i]]
        rule_rows.append([
            f"`{rule.id}`", rule.stage, rule.provenance, rule.description,
            ", ".join(fired_ids) or "-",
            f"{sum(labels[i]['release_blocking'] for i in fired_ids)}/{len(fired_ids)}" if fired_ids else "-",
        ])

    # Tier-1 similarity
    index = TfidfIndex(guide_windows(guide))
    sim = {t.id: score_response(index, t.id, t.ticket_text, t.assistant_answer) for t in tickets}
    clean_scores = sorted(s.score for t in tickets if labels[t.id]["severity"] == "none" for s in sim[t.id].sentences)
    offending_rows = []
    for t in tickets:
        frag = labels[t.id]["offending_text"]
        if not frag:
            continue
        s = sentence_containing(sim[t.id], frag)
        if s is None:
            continue
        below = sum(c < s.score for c in clean_scores) / len(clean_scores)
        offending_rows.append([t.id, labels[t.id]["severity"], f"{s.score:.3f}", f"{below:.0%}", s.sentence[:110]])
    response_scores = sorted(sim[i].min_score for i in ids)
    sweep_rows = []
    for k in range(1, 13):
        tau = k * 0.025
        flagged = [i for i in ids if sim[i].min_score < tau]
        blk = [i for i in flagged if labels[i]["release_blocking"]]
        sweep_rows.append({
            "tau": round(tau, 3),
            "flagged_responses": len(flagged),
            "flag_share": round(len(flagged) / len(ids), 3),
            "blocking_flagged": len(blk),
            "blocking_total": len(summary["blocking_ids"]),
            "non_blocking_flagged": len(flagged) - len(blk),
            "blocking_missed": ";".join(i for i in summary["blocking_ids"] if i not in flagged),
        })

    # Judges
    judgments = {m: load_judgments(m) for m in judge_models}
    judgments = {m: runs for m, runs in judgments.items() if runs}
    judge_rows = []
    for model, runs in judgments.items():
        for row in judge_metrics(tickets, labels, runs):
            judge_rows.append({"model": model, **row})
    consistency_rows = []
    for model, runs in judgments.items():
        complete = [r for r in sorted(runs) if len(runs[r]) == len(tickets)]
        if len(complete) >= 2:
            block, exact = run_agreement(tickets, runs[complete[0]], runs[complete[1]])
            consistency_rows.append([f"{model} run{complete[0]} vs run{complete[1]}", _pct(block), _pct(exact)])
    models = list(judgments)
    if len(models) >= 2 and all(1 in judgments[m] and len(judgments[m][1]) == len(tickets) for m in models[:2]):
        block, exact = run_agreement(tickets, judgments[models[0]][1], judgments[models[1]][1])
        consistency_rows.append([f"{models[0]} run1 vs {models[1]} run1", _pct(block), _pct(exact)])

    primary = judgments.get(primary_judge, {}).get(1)
    if primary is None or len(primary) < len(tickets):
        raise SystemExit(f"primary judge {primary_judge!r} run1 is incomplete; run the judge first")

    def judge_block(tid: str, records: dict[str, dict] = primary) -> bool:
        return _blocking(records[tid]["evaluation"]["severity"])

    # Guardrail replay
    replay_rows = []
    held_maps: dict[str, dict[str, bool]] = {}

    def add(name: str, description: str, held: dict[str, bool], judge_calls: int) -> None:
        held_maps[name] = held
        replay_rows.append({"config": name, "description": description, **replay(tickets, labels, held, judge_calls)})

    add("C0", "No quality layer: every draft is sent", {i: False for i in ids}, 0)
    for share in TIER1_FLAG_SHARES:
        tau = response_scores[min(len(ids) - 1, int(share * len(ids)))]
        flagged = {i: sim[i].min_score < tau for i in ids}
        add(f"C1@{share:.0%}",
            f"DoorDash-faithful: tier-1 flags ~{share:.0%} (tau={tau:.3f}); only flagged drafts reach the LLM judge",
            {i: flagged[i] and judge_block(i) for i in ids}, sum(flagged.values()))
    add("C2", "LLM judge on every draft", {i: judge_block(i) for i in ids}, len(ids))
    add("R-gen", "Generic deterministic rules only (no LLM)",
        {i: fired(i, provenance="generic") for i in ids}, 0)
    routed = {i: fired(i, stage="input", provenance="generic") for i in ids}
    add("C3-gen", "Recommended, without hindsight rules: generic input routing + generic output rules + judge on every non-routed draft",
        {i: routed[i] or fired(i, stage="output", provenance="generic") or judge_block(i) for i in ids},
        sum(not r for r in routed.values()))
    add("C3", "Recommended: C3-gen plus the regression rules",
        {i: routed[i] or fired(i, stage="output") or judge_block(i) for i in ids},
        sum(not r for r in routed.values()))

    # Replay stability: C2 and C3-gen under every judge model/run.
    stability_rows = []
    for model, runs in judgments.items():
        for run, records in sorted(runs.items()):
            if len(records) < len(tickets):
                continue
            c2 = replay(tickets, labels, {i: judge_block(i, records) for i in ids}, len(ids))
            c3 = replay(tickets, labels,
                        {i: routed[i] or fired(i, stage="output", provenance="generic") or judge_block(i, records) for i in ids},
                        sum(not r for r in routed.values()))
            stability_rows.append([f"{model} run{run}", c2["caught_blocking"], ", ".join(c2["escaped_blocking"]) or "-",
                                   c2["held_non_blocking"], c3["caught_blocking"], ", ".join(c3["escaped_blocking"]) or "-",
                                   c3["held_non_blocking"]])

    # Per-ticket table
    per_ticket = []
    for t in tickets:
        lab = labels[t.id]
        row = {
            "id": t.id, "category": t.category, "intents": ";".join(lab["intents"]),
            "multi_part": int(lab["multi_part"]), "guide_coverage": lab["guide_coverage"],
            "human_severity": lab["severity"], "release_blocking": int(lab["release_blocking"]),
            "failure_modes": ";".join(lab["failure_modes"]),
        }
        for model, runs in judgments.items():
            for run, records in sorted(runs.items()):
                if t.id in records:
                    row[f"judge_{model}_run{run}"] = records[t.id]["evaluation"]["severity"]
        row["tier1_min_score"] = round(sim[t.id].min_score, 3)
        row["tier1_mean_score"] = round(sim[t.id].mean_score, 3)
        row["rules_fired"] = ";".join(hits[t.id])
        for name, held in held_maps.items():
            row[f"replay_{name}"] = "held" if held[t.id] else "sent"
        per_ticket.append(row)

    disagreements = []
    for t in tickets:
        j = primary[t.id]["evaluation"]
        if j["severity"] != labels[t.id]["severity"]:
            disagreements.append([t.id, labels[t.id]["severity"], j["severity"], j["rationale"].replace("|", "/")[:220]])

    profile = claim_profile(primary)

    # Write machine-readable outputs
    _write_csv(RESULTS_DIR / "per_ticket.csv", per_ticket)
    _write_csv(RESULTS_DIR / "similarity_sweep.csv", sweep_rows)
    _write_csv(RESULTS_DIR / "similarity_sentences.csv", [
        {"id": t.id, "human_severity": labels[t.id]["severity"], "score": round(s.score, 4),
         "sentence": s.sentence, "best_guide_window": s.best_window}
        for t in tickets for s in sim[t.id].sentences
    ])
    _write_csv(RESULTS_DIR / "judge_agreement.csv", [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in judge_rows])
    _write_csv(RESULTS_DIR / "guardrail_replay.csv", [
        {**r, "escaped_blocking": ";".join(r["escaped_blocking"]), "escaped_critical": ";".join(r["escaped_critical"]),
         "auto_send_rate": round(r["auto_send_rate"], 3)} for r in replay_rows
    ])
    (RESULTS_DIR / "summary.json").write_text(json.dumps({
        "labels": {k: v for k, v in summary.items() if k != "blocking_ci"},
        "judges": judge_rows,
        "replay": replay_rows,
        "claim_profile_primary_judge": profile,
    }, indent=2, default=str), encoding="utf-8")

    # Human-readable report
    signed = sum(1 for lab in labels.values() if lab["human_signoff"].strip())
    total_cost = sum((r["meta"].get("cost_usd") or 0) for runs in judgments.values()
                     for records in runs.values() for r in records.values())
    md = _render_markdown(summary, judge_rows, consistency_rows, disagreements, profile, primary_judge,
                          offending_rows, clean_scores, sweep_rows, rule_rows, replay_rows, stability_rows,
                          signed, total_cost)
    (RESULTS_DIR / "summary.md").write_text(md, encoding="utf-8")
    print(f"[score] wrote {RESULTS_DIR / 'summary.md'} and CSV/JSON outputs")


def _render_markdown(summary, judge_rows, consistency_rows, disagreements, profile, primary_judge,
                     offending_rows, clean_scores, sweep_rows, rule_rows, replay_rows, stability_rows,
                     signed, total_cost) -> str:
    sev = summary["severity_counts"]
    n = summary["n"]
    signoff = ("all labels signed off by a human reviewer" if signed == n
               else f"DRAFT: human sign-off on {signed}/{n} labels")
    out = [
        "# Evaluation results: support-ticket RAG assistant",
        "",
        f"Generated by `python run_eval.py score` from `reviews/labels.csv` ({signoff}), "
        "cached LLM-judge outputs in `results/judge_cache/`, and the offline checks in `harness/`. "
        "Every rate carries a 95% Wilson interval; n = 26, so intervals are wide. "
        f"Total cost of all cached judge calls: ${total_cost:.2f}.",
        "",
        "## 1. Reference labels (primary evidence)",
        "",
        _md_table(["Severity", "Tickets"], [[s, sev[s]] for s in SEVERITIES]),
        "",
        f"- **Release-blocking (major or critical):** {summary['blocking_rate']} — {', '.join(summary['blocking_ids'])}",
        f"- **Critical:** {summary['critical_rate']} — {', '.join(summary['critical_ids'])}",
        "- **Criteria pass counts:** " + ", ".join(f"{c} {v}/{summary['n']}" for c, v in summary["criteria_pass"].items()),
        "",
        "Release-blocking rate by slice:",
        "",
        _md_table(["Slice", "Release-blocking"], summary["by_multi_part"] + summary["by_coverage"]),
        "",
        _md_table(["Original category", "Release-blocking"], summary["by_category"]),
        "",
        "Failure modes: " + ", ".join(f"`{m}` ×{c}" for m, c in summary["failure_modes"]),
        "",
        "## 2. LLM judge vs reference labels",
        "",
        "Positive class = release-blocking. TPR = share of release-blocking answers the judge also blocks; "
        "TNR = share of acceptable answers it lets through.",
        "",
        _md_table(
            ["Judge", "Run", "TPR", "TNR", "Kappa (blocking)", "Severity exact", "Severity weighted kappa",
             "Critical rated critical", "Mean $ / judgment", "Mean s / judgment"],
            [[r["model"], r["run"], f"{_pct(r['tpr'])} ({r['tp']}/{r['tp'] + r['fn']})",
              f"{_pct(r['tnr'])} ({r['tn']}/{r['tn'] + r['fp']})", f"{r['kappa']:.2f}", _pct(r["severity_exact"]),
              f"{r['severity_wkappa']:.2f}", r["critical_as_critical"], f"${r['mean_cost_usd']:.3f}",
              f"{r['mean_seconds']:.1f}"] for r in judge_rows],
        ),
        "",
        "Judge consistency:",
        "",
        _md_table(["Comparison", "Same block/send decision", "Same severity"], consistency_rows),
        "",
        f"Severity disagreements, primary judge ({primary_judge} run1) vs reference:",
        "",
        _md_table(["Ticket", "Reference", "Judge", "Judge rationale"], disagreements) if disagreements else "_None._",
        "",
        f"Claim-level profile ({primary_judge} run1): verdict counts by claim type.",
        "",
        _md_table(["Claim type", "supported", "unsupported", "contradicted"],
                  [[k, v.get("supported", 0), v.get("unsupported", 0), v.get("contradicted", 0)] for k, v in profile.items()]),
        "",
        "## 3. Tier-1 shallow similarity check (DoorDash layer 1, lexical TF-IDF proxy)",
        "",
        f"Clean-answer sentences: n = {len(clean_scores)}, median similarity "
        f"{clean_scores[len(clean_scores) // 2]:.3f}. For each labelled problem sentence, the share of clean "
        "sentences that score *lower* (higher = harder to separate):",
        "",
        _md_table(["Ticket", "Reference", "Similarity", "Clean sentences scoring lower", "Problem sentence"], offending_rows),
        "",
        "Threshold sweep on the response score (weakest sentence):",
        "",
        _md_table(["tau", "Responses flagged", "Release-blocking flagged", "Acceptable flagged", "Release-blocking missed"],
                  [[r["tau"], f"{r['flagged_responses']} ({r['flag_share']:.0%})",
                    f"{r['blocking_flagged']}/{r['blocking_total']}", r["non_blocking_flagged"],
                    r["blocking_missed"] or "-"] for r in sweep_rows]),
        "",
        "## 4. Deterministic rules",
        "",
        "`generic` rules encode a general policy; `regression` rules encode a failure seen in this sample "
        "and prove nothing about generalisation.",
        "",
        _md_table(["Rule", "Stage", "Provenance", "Checks", "Fired on", "Of which release-blocking"], rule_rows),
        "",
        "## 5. Guardrail replay",
        "",
        f"Which release-blocking answers would still reach a customer under each quality-layer design. "
        f"LLM judge = {primary_judge} run1. A held draft goes to a human.",
        "",
        _md_table(["Config", "Design", "Release-blocking caught", "Escaped to customer", "Critical escaped",
                   "Auto-sent", "Acceptable drafts held", "Judge calls"],
                  [[r["config"], r["description"], r["caught_blocking"], ", ".join(r["escaped_blocking"]) or "-",
                    ", ".join(r["escaped_critical"]) or "-", f"{r['auto_sent']} ({r['auto_send_rate']:.0%})",
                    r["held_non_blocking"], r["judge_calls"]] for r in replay_rows]),
        "",
        "Replay stability across judge models and runs:",
        "",
        _md_table(["Judge run", "C2 caught", "C2 escaped", "C2 acceptable held",
                   "C3-gen caught", "C3-gen escaped", "C3-gen acceptable held"], stability_rows),
        "",
        "## 6. Caveats",
        "",
        "- n = 26. A perfect catch of 6/6 release-blocking answers supports a catch rate of only about 61% or more (95% lower bound).",
        "- The rubric, rules and reference labels were written after reading the sample. The judge prompt is generic and was "
        "frozen before any judge output was seen.",
        "- The judge and the replay check answers against the full guide; a production guardrail would see only retrieved chunks, "
        "so these results are optimistic.",
        "- Retrieved contexts are not logged, so retrieval failures cannot be separated from generation failures.",
        "- Tier-1 uses TF-IDF (lexical); DoorDash used a semantic similarity check.",
        "- The reference-label draft and the judge are the same model family; the judge ran in an isolated process without "
        f"access to the labels. Human sign-off: {signed}/{n} labels.",
        "- Mean judge cost per call is lower on repeat runs because the system prompt and guide are prompt-cached.",
    ]
    return "\n".join(out) + "\n"
