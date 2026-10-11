"""Report sections added after the approach review:

  6  held-out fault-injection set       (does the guardrail generalise beyond the 6 failures it was built on?)
  7  routing as an action               (hand off internally instead of bouncing the customer)
  8  staging by detected intent         (instead of the single-label category column)
  9  knowledge-base retrieval check     (would the guide changes surface the corrective fact?)
  10 release gates derived from harm    (replaces fixed "200 tickets" style gates)
  11 shadow mode                        (how agent-assist produces the evidence for those gates)
  12 reviewer view                      (claim-level flags shown to agents; written to review_cards.md)
"""

import csv
from pathlib import Path

from . import actions as actions_mod
from .data import RESULTS_DIR
from .gates import Evidence, cases_to_bound_rate, load_gate_config, recall_gate, target_table
from .heldout import load_signoff, score_heldout
from .intents import detect_intents, risk_tier, score_intents
from .kb_check import TOP_K, run_kb_check
from .review_card import render_cards
from .shadow import analyze, audit_sample, demo_log, write_log
from .stats import wilson


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _ids(ids: list[str]) -> str:
    return ", ".join(ids) or "-"


def score_additions(ctx: dict) -> tuple[list[str], dict]:
    tickets, labels, primary_name = ctx["tickets"], ctx["labels"], ctx["primary_judge"]
    primary, judge_block = ctx["primary"], ctx["judge_block"]
    hits, rule_by_id, rules = ctx["hits"], ctx["rule_by_id"], ctx["rules"]
    cfg = load_gate_config()
    sections, data = [], {}

    # --- 6. Held-out fault-injection set --------------------------------------------------------
    ho = score_heldout(rules, ctx["judge_models"], primary_name)
    if ho is None:
        sections.append("## 6. Held-out fault-injection set\n\n_No cached judge outputs for the held-out set; run "
                        "`python run_eval.py judge --set heldout --model opus`._\n")
    else:
        notes = {k: v["notes"] for k, v in load_signoff().items() if v.get("notes")}
        n_new = sum("+" in r["base_id"] for r in ho["per_case"])
        integrity = ("Integrity check: the rule code and judge prompt are byte-identical to when the set was frozen "
                     "(`heldout/MANIFEST.json`)." if not ho["integrity"]
                     else "**Integrity warnings:** " + "; ".join(ho["integrity"]))
        rules_row = next(r for r in ho["summary_rows"] if r["detector"] == "Generic output rules")
        judge_row = next(r for r in ho["summary_rows"] if r["detector"] == f"Judge: {primary_name}")
        sections.append("\n".join([
            "## 6. Held-out fault-injection set",
            "",
            f"{ho['n']} cases: {ho['n'] - n_new} edit one of the {sum(1 for t in tickets if labels[t.id]['severity'] == 'none')} answers the "
            f"reference labels rate `none`, and {n_new} are new multi-part tickets. "
            f"{ho['n_blocking']} inject a release-blocking fault ({ho['n_critical']} critical), {ho['n_passing'] - ho['n_controls']} inject a "
            f"minor fault, and {ho['n_controls']} reword a correct answer without changing its meaning. The rules and the judge "
            "prompt were not changed after these cases were written, so this tests whether they generalise beyond the 6 "
            "failures they were built around. Spec: `heldout/mutations.json`; per-case results: `results/heldout_cases.csv`.",
            "",
            f"- {integrity}",
            f"- Case severities were drafted by Claude after reading `harness/rules.py`; human sign-off: {ho['signed']}/{ho['n']} "
            "(`heldout/signoff.csv`). A second batch written by a person would remove the author-knows-the-rules bias.",
            f"- Injected faults are cleaner than natural ones, so read these as an upper bound on real-world recall. Judge cost for this set: ${ho['cost_usd']:.2f}.",
            "",
            _table(["Detector", f"Release-blocking caught (n={ho['n_blocking']})", f"Critical caught (n={ho['n_critical']})",
                    f"Should-pass cases held (n={ho['n_passing']})", f"Rewordings held (n={ho['n_controls']})"],
                   [[r["detector"], r["blocking_caught"], r["critical_caught"], r["false_holds"], r["controls_held"]]
                    for r in ho["summary_rows"]]),
            "",
            f"The generic rules catch {rules_row['blocking_caught'].split(' ')[0]} blocking faults "
            f"(fault types: {', '.join(r['fault_type'] for r in ho['fault_rows'] if r['should_block'] == 'yes' and r['generic_rules']) or 'none'}); "
            f"the {primary_name} judge catches {judge_row['blocking_caught'].split(' ')[0]}. The judge, not the rules, is the guardrail. "
            "A perfect catch on this many cases supports a recall of at least the lower bound shown, not 100%.",
            "",
            "By fault type (cases held):",
            "",
            _table(["Fault type", "Should block", "n", "Input routing", "Generic rules",
                    *[f"Judge: {m}" for m in ho["judges"]], "C3-gen"],
                   [[r["fault_type"], r["should_block"], r["n"], r["input_routing"], r["generic_rules"],
                     *[r[f"judge_{m}"] for m in ho["judges"]], r["c3_gen"]] for r in ho["fault_rows"]]),
            "",
            "Cases where a judge's block/send decision differs from the drafted label (the reviewer decides in `heldout/signoff.csv`):",
            "",
            _table(["Case", "Fault type", "Drafted severity", "Judges", "Note"],
                   [row + [notes.get(row[0], "")] for row in ho["disagreements"]]) if ho["disagreements"] else "_None._",
            "",
            "Severity exact match with the drafted labels: " + ", ".join(f"{m} {k}/{ho['n']}" for m, k in ho["severity_exact"].items()) + ".",
            "",
        ]))
        _write_csv(RESULTS_DIR / "heldout_cases.csv", ho["per_case"])
        data["heldout"] = {k: v for k, v in ho.items() if k != "per_case"}

    # --- 7. Routing as an action ----------------------------------------------------------------
    act = actions_mod.score_actions(tickets, labels, hits, rule_by_id, primary, judge_block)
    rows = act["rows"]
    _write_csv(RESULTS_DIR / "routing_actions.csv", rows)
    needs = [r for r in rows if r["expected_action"] in actions_mod.NEEDS_HUMAN]

    def tally(key: str) -> dict:
        return {
            "routed": [r["id"] for r in needs if r[key].startswith("routed")],
            "hold": [r["id"] for r in needs if r[key].startswith("reached")],
            "missed": [r["id"] for r in needs if r[key].startswith("MISSED")],
            "bounced": [r["id"] for r in rows if r[key] == "BOUNCED customer"],
            "good_held": [r["id"] for r in rows if r[key].startswith("good draft held")],
        }

    designed, proposed = tally("c3gen_outcome"), tally("proposed_outcome")
    tp, fp, fn = act["detector"]
    interesting = [r for r in rows if r["expected_action"] != "answer" or r["draft_bounces"] or r["bounce_detector"]
                   or r["c3gen_outcome"] != r["proposed_outcome"]]
    sections.append("\n".join([
        "## 7. Routing as an action",
        "",
        "In auto-send mode the system decides what happens to a ticket: send the draft, hand it to a named human queue "
        "(the customer gets a holding reply, never the draft), or hold the draft for an agent. A draft that tells the "
        "customer to contact support from inside a support ticket is a *bounce*; the right action is an internal handoff. "
        f"Expected actions and bounce labels: `reviews/actions.csv` (drafted by Claude, human sign-off {act['signed']}/{len(rows)}).",
        "",
        "*C3-gen* is the recommended configuration as designed. *Proposed* adds two inputs to the decision: the judge's "
        "`should_route_to_human` verdict becomes a handoff, and a detected bounce holds the draft.",
        "",
        _table(["", "C3-gen", "Proposed"], [
            [f"Tickets that need a person (n={len(needs)}): routed by a decision", _ids(designed["routed"]), _ids(proposed["routed"])],
            ["... reached a person only because the draft was held", _ids(designed["hold"]), _ids(proposed["hold"])],
            ["... auto-sent, never reached a person", _ids(designed["missed"]), _ids(proposed["missed"])],
            ["Bouncing drafts auto-sent to the customer", _ids(designed["bounced"]), _ids(proposed["bounced"])],
            ["Good drafts held anyway", _ids(designed["good_held"]), _ids(proposed["good_held"])],
        ]),
        "",
        f"Bounce detector against the adjudicated labels: {tp} caught, {fp} false alarm(s), {fn} missed. "
        "A false alarm costs one unnecessary human touch; it never reaches the customer.",
        "",
        _table(["Ticket", "Expected", "Draft bounces", "Detector", "C3-gen", "Proposed"],
               [[r["id"], r["expected_action"] + (f" ({r['expected_queue']})" if r["expected_queue"] else ""),
                 "yes" if r["draft_bounces"] else "no", r["bounce_detector"] or "-",
                 f"{r['c3gen_action']}: {r['c3gen_outcome']}", f"{r['proposed_action']}: {r['proposed_outcome']}"]
                for r in interesting]),
        "",
    ]))
    data["routing"] = {"designed": designed, "proposed": proposed, "detector": act["detector"]}

    # --- 8. Staging by detected intent ----------------------------------------------------------
    it = score_intents(tickets, labels)
    _write_csv(RESULTS_DIR / "intents.csv", it["rows"])
    sections.append("\n".join([
        "## 8. Staging the rollout by detected intent",
        "",
        "The `category` column holds one label per ticket. A rollout stage defined by category admits any high-risk "
        "question hidden in a low-risk category. The detector (`harness/intents.py`) finds every intent in the ticket text "
        "and takes the highest risk tier (high = billing, plan entitlements, Enterprise/out-of-scope topics; medium = "
        "API keys, workspace, model providers; low = Knowledge Pipeline how-to). Tiers are a policy choice, not a measurement.",
        "",
        f"- `category` disagrees with the human-labelled intents on {len(it['category_mismatch'])}/26 tickets: {_ids(it['category_mismatch'])}.",
        f"- The detector matches the human intents exactly on {it['exact']}/26 and never rates a ticket's risk below its "
        f"human-labelled tier (tickets under-rated: {_ids(it['tier_underestimated'])}). It over-detects billing, which is the "
        "safe direction: it holds more, never less.",
        "",
        _table(["Stage", "Admitted", "Release-blocking admitted", "Admitted with a higher-risk intent (human labels)"],
               [[name, f"{len(s['admitted'])}: {_ids(s['admitted'])}", _ids(s["blocking"]), _ids(s["hidden_higher_risk"])]
                for name, s in it["staging"]]),
        "",
        "Stage 1 is chosen on risk grounds, not measured evidence: "
        + "; ".join(f"{name} admits {len(s['admitted'])} with {len(s['blocking'])} release-blocking "
                    f"(95% upper bound {wilson(len(s['blocking']), len(s['admitted']))[1]:.0%})"
                    for name, s in it["staging"] if name.startswith("stage 1")) + ". "
        + "At stage 2 the definitions diverge: by category it admits "
        + _ids(next(s for name, s in it["staging"] if name == "stage 2 by category")["hidden_higher_risk"])
        + ", whose human-labelled intents are higher risk than their category.",
        "",
    ]))
    data["intents"] = {k: v for k, v in it.items() if k != "rows"}

    # --- 9. Knowledge-base check ----------------------------------------------------------------
    kb = run_kb_check()
    _write_csv(RESULTS_DIR / "kb_retrieval_check.csv", kb)

    def rank(v):
        return "not found" if v is None else (f"**{v}**" if v <= TOP_K else str(v))

    generation = sorted({r["ticket"] for r in kb if r["current_rank"] == 1 and r["query"] == "whole ticket"})
    sub_ok = {r["ticket"] for r in kb if r["query"] == "sub-question" and r[f"current_in_top_{TOP_K}"]}
    diluted = sorted({r["ticket"] for r in kb if r["query"] == "whole ticket" and not r[f"current_in_top_{TOP_K}"]
                      and r["ticket"] in sub_ok})
    sections.append("\n".join([
        "## 9. Knowledge-base retrieval check",
        "",
        "`kb/product-guide.proposed.md` restates facts the guide already contains in places retrieval will find them, and "
        "adds scope statements for the doc owner to confirm (`kb/README.md`). This check splits each guide into chunks the "
        "way a default Dify General chunker would and ranks them for each failing ticket with keyword (TF-IDF) retrieval, "
        f"close to Dify's Economical index. **Bold** = within top_k={TOP_K}. The probes are the failing tickets themselves, "
        "so this is a regression check, not evidence the changes generalise.",
        "",
        _table(["Ticket", "Query", "Corrective fact", "Rank, current guide", "Rank, proposed guide"],
               [[r["ticket"], r["query"], r["corrective_fact"], rank(r["current_rank"]), rank(r["proposed_rank"])] for r in kb]),
        "",
        f"- {_ids(generation)}: the corrective fact already ranks first for the whole ticket, so these failures came from "
        "generation, not retrieval. Guide edits alone will not fix them; the guardrail must.",
        f"- {_ids(diluted)}: querying the multi-part ticket as one string pushes the corrective fact out of the top {TOP_K}; "
        "querying the sub-question on its own brings it back. Splitting sub-questions before retrieval matters as much as "
        "the guide changes.",
        "",
    ]))
    data["kb_check"] = kb

    # --- 10. Release gates ----------------------------------------------------------------------
    crit_k = sum(1 for t in tickets if labels[t.id]["severity"] == "critical")
    blk_k = sum(1 for t in tickets if labels[t.id]["release_blocking"])
    n = len(tickets)
    held_c3 = ctx["held_maps"]["C3-gen"]
    sample_crit = [t.id for t in tickets if labels[t.id]["severity"] == "critical"]
    sample_blk = [t.id for t in tickets if labels[t.id]["release_blocking"]]
    crit_evidence = [Evidence("sample", sum(held_c3[i] for i in sample_crit), len(sample_crit))]
    blk_evidence = [Evidence("sample", sum(held_c3[i] for i in sample_blk), len(sample_blk))]
    if ho is not None:
        c3 = next(r for r in ho["summary_rows"] if r["detector"] == ho["c3gen_name"])
        crit_evidence.append(Evidence("held-out", c3["_caught_critical"], ho["n_critical"]))
        blk_evidence.append(Evidence("held-out", c3["_caught_blocking"], ho["n_blocking"]))
    g_crit = recall_gate("Guardrail recall on critical failures (C3-gen)", crit_k, n, cfg["target_escaped_critical_rate"], crit_evidence)
    g_blk = recall_gate("Guardrail recall on release-blocking failures (C3-gen)", blk_k, n, cfg["target_escaped_blocking_rate"], blk_evidence)
    # Human-labelled release-blocking positives per failure mode (synthetic held-out cases do not count).
    mode_counts = {}
    for t in tickets:
        for mode in labels[t.id]["failure_modes"]:
            if labels[t.id]["release_blocking"]:
                mode_counts[mode] = mode_counts.get(mode, 0) + 1
    min_positives = cfg["judge_positives_per_failure_mode"]
    placeholder = not cfg.get("set_by")
    vol = cfg["illustrative_monthly_auto_send_volume"]

    def recall_row(g):
        return [g["gate"], f"{g['target_escaped']:.1%}", g["raw_rate"],
                f"{g['required_recall']:.1%} ({g['required_recall_conservative']:.1%})", g["evidence"],
                f"{g['demonstrated_recall_lower']:.1%}",
                f"{g['positives_needed']} ({g['positives_needed_conservative']})", g["status"]]

    sections.append("\n".join([
        "## 10. Release gates derived from tolerable harm",
        "",
        "escaped rate = raw failure rate × (1 − guardrail recall). The recall a gate needs follows from the raw rate and the "
        "escaped rate the business accepts; the evidence it needs follows from Wilson bounds. "
        + ("**Targets in `gates.json` are placeholders until Support leadership sets them.** " if placeholder else
           f"Targets set by {cfg['set_by']}. ")
        + f"At an illustrative {vol:,} auto-sent tickets a month, a {cfg['target_escaped_critical_rate']:.1%} escaped-critical "
        f"target still allows about {vol * cfg['target_escaped_critical_rate']:.0f} critical errors a month.",
        "",
        f"What a critical-escape target demands, at the sample's critical rate ({crit_k}/{n}); conservative = at its 95% upper bound:",
        "",
        _table(["Escaped-critical target", "Recall needed", "Positives to show it (no misses)",
                "Recall needed (conservative)", "Positives (conservative)", "Shadow drafts with 0 escapes to show it"],
               target_table(crit_k, n, [0.01, 0.005, 0.001])),
        "",
        "Gate status now:",
        "",
        _table(["Gate", "Target escaped", "Raw rate (sample)", "Recall needed (conservative)", "Evidence",
                "Demonstrated recall (95% lower)", "Positives needed (conservative)", "Status"],
               [recall_row(g_crit), recall_row(g_blk)]),
        "",
        "Evidence pools the 26-ticket sample with the held-out set. Held-out faults are synthetic and easier than natural "
        "ones, so 'met on offline evidence' means ready for a shadow-mode pilot, not ready to auto-send; the shadow-mode "
        "gates below decide that (section 11).",
        "",
        _table(["Gate", "Status", "Detail"], [
            ["Escaped critical rate in shadow mode", "NO DATA",
             f"needs {cases_to_bound_rate(cfg['target_escaped_critical_rate'])} "
             "would-auto-send drafts with zero critical escapes"],
            ["Escaped release-blocking rate in shadow mode", "NO DATA",
             f"needs {cases_to_bound_rate(cfg['target_escaped_blocking_rate'])} "
             "would-auto-send drafts with zero escapes"],
            [f"Judge validated on ≥{min_positives} human-labelled positives per failure mode", "NOT MET",
             "largest so far: " + (", ".join(f"`{m}` {c}" for m, c in sorted(mode_counts.items(), key=lambda kv: -kv[1])[:3]) or "none")],
            ["Retrieved chunks logged for every draft", "NOT MET", "the sample has no retrieval logs"],
            ["Pre-generation routing live in the Dify app", "NOT MET", "Question Classifier + score threshold + IF/ELSE handoff (approach §8)"],
            ["AI-response disclosure in every reply", "NOT MET", "policy item; not measurable from this data"],
        ]),
        "",
    ]))
    _write_csv(RESULTS_DIR / "gates.csv", [g_crit, g_blk])
    data["gates"] = [g_crit, g_blk]

    # --- 11. Shadow mode ------------------------------------------------------------------------
    generic_hits = {t.id: [r for r in hits[t.id] if rule_by_id[r].provenance == "generic"] for t in tickets}
    input_routes = {t.id: [r for r in generic_hits[t.id] if rule_by_id[r].stage == "input"] for t in tickets}
    log = demo_log(tickets, labels, primary, primary_name, generic_hits, input_routes, held_c3,
                   actions_mod.ROUTE_QUEUES, detect_intents, risk_tier)
    write_log(RESULTS_DIR / "shadow_demo_log.jsonl", log)
    sh = analyze(log, cfg)
    audit = audit_sample(log, cfg["audit_sample_rate"])
    sections.append("\n".join([
        "## 11. Shadow mode: where the gate evidence comes from",
        "",
        "During agent-assist every draft still goes to a person, and the full quality layer runs alongside, logging what it "
        "*would* have done (`shadow/log_schema.json`). The reviewer's one-click severity tag is the label: a draft the "
        "configuration would have auto-sent but a reviewer had to change materially is an escaped failure. These records "
        "are real production traffic, which is what the constructed 26-ticket sample cannot provide.",
        "",
        f"**Demo only.** `results/shadow_demo_log.jsonl` replays the 26 sample tickets through this pipeline with the reviewer's "
        f"action *simulated from the reference labels* ({sh['synthetic']}/{sh['n']} records synthetic). It proves the analysis "
        "runs end to end; it is not evidence. Replace it with real logs: `harness/shadow.py: load_log` + `analyze`.",
        "",
        _table(["Measure", "Demo value"], [
            ["Would auto-send", sh["would_auto_send"]],
            ["Guardrail recall on materially edited drafts", sh["guardrail_recall"]],
            ["Good drafts held (false holds)", sh["false_hold"]],
            ["Escaped (would-send drafts that needed a material fix)", _ids(sh["escaped_ids"])],
            ["Retrieved chunks logged", sh["retrieval_logged"]],
            ["Reviewer approved a draft the guardrail would hold", _ids(sh["reviewer_overrode_guardrail"])],
        ]),
        "",
        _table(["Shadow gate", "Target", "Observed", "95% upper bound", "Drafts needed (0 escapes / 1 escape)", "Status"],
               [[g["gate"], f"{g['target_escaped']:.1%}", g["observed"], f"{g['upper_bound']:.1%}",
                 f"{g['drafts_needed_if_zero_escapes']} / {g['drafts_needed_if_one_escape']}", g["status"]] for g in sh["gates"]]),
        "",
        f"Even with zero escapes, {sh['would_auto_send'].split(' ')[0]} would-send drafts bound the escaped rate only at the upper "
        "bound shown. The gate needs the number of drafts in the last column.",
        "",
        f"Audit sample the demo log would trigger ({len(audit)}): "
        + ("; ".join(f"{tid} ({why})" for tid, why in audit) or "none") + ".",
        "",
    ]))
    data["shadow_demo"] = {k: v for k, v in sh.items()}
    data["shadow_demo"]["audit"] = audit

    # --- 12. Reviewer view ----------------------------------------------------------------------
    sys_actions, bounces = {}, {}
    for t in tickets:
        routes = input_routes[t.id]
        outs = [r for r in generic_hits[t.id] if rule_by_id[r].stage == "output"]
        bounces[t.id] = actions_mod.detect_bounce(t.assistant_answer)
        sys_actions[t.id] = actions_mod.system_action(routes, outs, primary[t.id]["evaluation"], judge_block(t.id),
                                                      bounces[t.id], action_layer=True)
    cards = render_cards(tickets, primary, primary_name, hits, sys_actions, bounces, cfg["audit_sample_rate"])
    (RESULTS_DIR / "review_cards.md").write_text(cards + "\n", encoding="utf-8")
    flagged_claims = sum(c["verdict"] != "supported" for t in tickets for c in primary[t.id]["evaluation"]["claims"])
    blocking_flagged = [t.id for t in tickets if labels[t.id]["release_blocking"]
                        and any(c["verdict"] == "contradicted" or c["claim_type"] in {"commercial", "diagnostic"}
                                for c in primary[t.id]["evaluation"]["claims"] if c["verdict"] != "supported")]
    sections.append("\n".join([
        "## 12. Reviewer view for agent-assist",
        "",
        "`results/review_cards.md` shows what a support agent would see beside each draft: the system's decision, then every "
        "claim the judge could not match to the guide, next to the guide's own text, riskiest drafts first. It exists because "
        "agent-assist is only safe if reviewers catch fluent, confident errors such as the SSO answers, and people approve "
        "plausible text on autopilot. The cards show no reference labels.",
        "",
        f"- {flagged_claims} claims flagged across 26 drafts. For {len(blocking_flagged)}/{blk_k} release-blocking drafts, a "
        f"contradicted or unsupported commercial/diagnostic claim is on the card ({_ids(blocking_flagged)}).",
        "- Audit policy (second reviewer): every draft approved unchanged although the guardrail would hold it; every large "
        f"edit tagged none/minor; every untagged record; a random {cfg['audit_sample_rate']:.0%} of approved would-send drafts.",
        "",
    ]))
    return sections, data
