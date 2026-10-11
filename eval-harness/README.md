# Ship/hold evaluation harness: Dify support-ticket RAG assistant

This harness evaluates 26 prerecorded assistant answers against the supplied product guide, and replays several quality-layer designs to show which bad answers would still reach a customer under each.

The product guide is the only source of truth. No live assistant endpoint is used.

## Quick start (offline, about 1 second)

```bash
cd eval-harness
python3 run_eval.py check-labels   # validate labels, expected actions and the held-out set
python3 run_eval.py score          # rebuild everything in results/ from cached judge outputs
```

Requirements: Python 3.10+ and the standard library only. Scoring needs no network access and no credentials. The LLM-judge outputs are cached in `results/judge_cache/` (the 26 tickets) and `results/heldout_judge_cache/` (the held-out set).

## Layout

| Path | What it is |
|---|---|
| `data/tickets.csv`, `data/product-guide.md` | The supplied inputs, unmodified. |
| `reviews/rubric.md` | Grading rubric: five binary criteria, severity levels, failure-mode tags. |
| `reviews/labels.csv` | Reference labels, one row per ticket. Claude drafted them; the `human_signoff` column records the human review. |
| `prompts/judge_system.md`, `prompts/judge_schema.json` | Frozen LLM-judge prompt and output schema. These are generic, with no ticket-specific hints. |
| `harness/judge.py` | LLM judge, with `claude -p` and Anthropic API backends, caching, and prompt fingerprinting. |
| `harness/similarity.py` | Tier-1 shallow grounding check (DoorDash-style sliding-window similarity, TF-IDF). |
| `harness/rules.py` | Deterministic input routing and output checks, each tagged `generic` or `regression`. |
| `harness/stats.py` | Wilson intervals, confusion matrices, Cohen's kappa. |
| `harness/report.py` | Scoring, guardrail replay, and report writer. |
| `heldout/mutations.json`, `heldout/signoff.csv`, `heldout/MANIFEST.json` | Held-out fault-injection set: the injected edits, their human sign-off sheet, and the hashes of the rule code and judge prompt it was frozen against. |
| `harness/heldout.py` | Renders, freezes and scores the held-out set. |
| `reviews/actions.csv`, `harness/actions.py` | Expected action per ticket (answer / clarify / handoff), bounce labels, bounce detector, and routing-as-action scoring. |
| `harness/intents.py` | Multi-intent detector and risk tiers for staging the rollout by intent instead of `category`. |
| `kb/` , `harness/kb_check.py` | Proposed guide changes (`kb/README.md`) and a before/after retrieval check. |
| `gates.json`, `harness/gates.py` | Escaped-rate targets (placeholders) and the gate arithmetic derived from them. |
| `shadow/log_schema.json`, `harness/shadow.py` | Shadow-mode log contract for agent-assist, the analyzer behind the shadow gates, and the audit sampler. |
| `harness/review_card.py` | Reviewer view for agent-assist (`results/review_cards.md`). |
| `harness/additions.py` | Report sections 6–12. |
| `results/summary.md` | The human-readable report. **Start here.** |
| `results/review_cards.md` | What a support agent would see beside each draft: claim-level flags with guide evidence, riskiest first. |
| `results/*.csv`, `results/summary.json`, `results/shadow_demo_log.jsonl` | Per-ticket results, judge agreement, similarity sweep, replay table, held-out cases, routing actions, intents, KB check, gates, and the synthetic shadow-mode demo log. |

## Method

1. **Reference labels (primary evidence).** Each answer is graded pass/fail on five criteria (`accurate`, `grounded`, `complete`, `escalation_ok`, `customer_safe`) and given a severity: none, minor, major, or critical. Major and critical are **release-blocking**.
   - Agreed rule for claims that may be true but are not in the guide: an unsupported technical detail is minor; an unsupported commercial or entitlement promise is major.
2. **LLM judge.** A reference-grounded, claim-level judge that sees the full guide:
   - it lists the ticket's sub-questions;
   - it marks each atomic claim supported, contradicted, or unsupported, with a verbatim quote;
   - it assesses escalation and customer safety;
   - it assigns a severity.

   It runs as an isolated process and never sees the reference labels. Two models (Opus 5.5 and Sonnet 5.5) each ran twice, to measure accuracy against the labels, run-to-run consistency, and cross-model agreement.
3. **Tier-1 similarity check.** This reproduces the first layer of DoorDash's guardrail. It scores how similar each answer sentence is to sliding windows over the guide, and sweeps the threshold to see what a cheap pre-filter would let through.
4. **Deterministic rules.**
   - Input-side routing: out-of-scope topics taken from the guide's own "not covered" section, multi-part tickets, and low-information tickets.
   - Output-side checks: leaked error strings, number grounding, plan-table consistency, and three regression rules.
5. **Guardrail replay.** Each design is applied to the 26 answers. For each one, the harness counts:
   - release-blocking answers that escape to the customer;
   - the auto-send rate;
   - acceptable drafts held for a human;
   - judge calls.

   | Config | Design |
   |---|---|
   | C0 | No quality layer. |
   | C1 | DoorDash-faithful: tier-1 check, then judge only on flagged drafts (at 25%, 50% and 75% flag rates). |
   | C2 | Judge on every draft. |
   | R-gen | Generic rules only. |
   | C3-gen / C3 | Recommended: routing, rules, and judge on every draft. |

Added after the approach review (`../docs/additions.md`), as report sections 6–12:

6. **Held-out fault-injection set.** 66 edited answers (45 release-blocking, 11 minor, 10 rewordings) score the frozen rules and judge on faults they were not built around. `check-labels` and the report warn if the rule code or judge prompt changes after the freeze.
7. **Routing as an action.** Scores whether tickets that need a person are handed off by a decision, and whether drafts that bounce the customer ("contact support") would be auto-sent.
8. **Staging by detected intent.** Compares rollout stages defined by `category` with stages defined by detected intents.
9. **Knowledge-base check.** Ranks guide chunks for each failing ticket before and after the proposed guide changes.
10. **Release gates.** Derives the recall and sample sizes each gate needs from the escaped-rate targets in `gates.json`.
11. **Shadow mode.** Analyzes agent-assist logs into escaped-failure rates; the shipped log is a synthetic demo.
12. **Reviewer view.** Writes `results/review_cards.md`.

## Re-running the LLM judge (optional)

Cached outputs are reused; pass `--force` to regenerate.

**Claude Code CLI backend** (default; uses your logged-in `claude` CLI, no API key):

```bash
python3 run_eval.py judge --model opus --runs 2 --workers 4
python3 run_eval.py judge --model sonnet --runs 2 --workers 4
python3 run_eval.py score
```

Each call runs this command from an empty temporary directory:

```
claude -p --tools "" --strict-mcp-config --no-session-persistence --system-prompt <rubric> --json-schema <schema>
```

That isolates the judge: it has no file access, no MCP servers, no project instructions, and its output is checked against the schema.

**Anthropic API backend:** run `pip install anthropic`, set `ANTHROPIC_API_KEY`, then add `--backend api`. It uses structured outputs (`output_config.format`) and records refusals as errors rather than falling back to another model.

Cost and time: the full set (2 models × 2 runs × 26 tickets) cost about $3.64 and took about 10 minutes with 4 workers per model.

**Held-out set:**

```bash
python3 run_eval.py judge --set heldout --model opus --runs 1 --workers 4
python3 run_eval.py judge --set heldout --model sonnet --runs 1 --workers 4
python3 run_eval.py score
```

One run per model over the 66 cases cost $6.18. If you change `harness/rules.py`, `harness/similarity.py` or the judge prompt, the held-out results stop measuring generalisation; the report says so. Write new held-out cases and re-freeze with `python3 run_eval.py heldout-freeze --author <name> --force`.

Each cached record stores:
- the prompt fingerprint (scoring refuses to mix outputs from different judge prompts);
- the model actually used;
- cost, latency and CLI version.

## Reviewing the labels

Open `reviews/labels.csv`. Each row has:
- the per-criterion verdicts and severity;
- failure-mode tags;
- `offending_text`: the exact problem span from the answer;
- guide references;
- `expected_behavior` and `rationale`.

To sign off, put your initials in `human_signoff`, or edit a label and re-run `python3 run_eval.py score`. The report header shows how many labels have been signed off.

## Known limitations

- **Small sample.** n = 26, with about 6 release-blocking answers. Every rate is reported with a 95% interval.
- **Written after reading the sample.** The rubric, labels and rules were authored after reading the answers. The judge prompt is generic and was frozen before any judge output was seen.
- **Optimistic replay.** The judge and the replay use the full guide; a production guardrail sees only the retrieved chunks.
- **No retrieval logs.** Retrieved contexts weren't logged, so retrieval errors can't be separated from generation errors.
- **Lexical, not semantic.** The tier-1 check is TF-IDF, a stand-in for a semantic similarity model.
- **Same model family.** The reference-label draft and the judge both come from Claude. The process isolation and human sign-off mitigate this but don't remove it.
- **Held-out author knew the rules.** The held-out cases were written by Claude after reading `harness/rules.py`; their severities await sign-off in `heldout/signoff.csv`. Injected faults are cleaner than natural ones, so held-out recall is an upper bound.
- **Shadow mode is a demo.** `results/shadow_demo_log.jsonl` simulates reviewer actions from the reference labels; real gate evidence needs production shadow logs.
