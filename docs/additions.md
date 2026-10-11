# Additions after the approach review

The review of `docs/approach.md` judged the approach sound and its verdict right (hold auto-send; ship as agent-assist). It found that most of the effort went into proving "hold", which the human labels alone settle, and too little into the open question: what evidence would justify auto-send, and where it comes from. These seven additions address that. Their results are sections 6–12 of `eval-harness/results/summary.md`, produced by the same `python3 run_eval.py score`.

| # | Addition | Where | Main result |
|---|---|---|---|
| 1 | **Held-out fault-injection set.** 66 cases: 45 inject a release-blocking fault into a clean answer, 11 inject a minor fault, and 10 reword a clean answer. They were frozen against the rule code and judge prompt (hash-checked), so they test generalisation rather than the 6 failures the rules were built on. | `heldout/`, `harness/heldout.py`, results §6 | The generic rules catch 5/45, and only wrong plan numbers. Both judges catch 45/45 (95% lower bound 92%) and hold none of the 10 rewordings. They hold 3–4 of the 11 minor faults, mostly where the rubric's minor/major line is fuzzy. **The judge is the guardrail; the rules are a cheap backstop.** |
| 2 | **Shadow mode.** A log schema for the agent-assist period, an analyzer that turns reviewer actions into escaped-failure rates, and an audit sampler. | `shadow/log_schema.json`, `harness/shadow.py`, results §11 | A demo on the 26 tickets, with simulated reviewer actions, runs end to end. Even zero escapes in 17 would-send drafts only bounds the escaped rate at 18%; the gate needs about 765 drafts. |
| 3 | **Gates derived from tolerable harm.** escaped rate = raw rate × (1 − recall). | `harness/gates.py`, `gates.json`, results §10 | At a 0.5% escaped-critical target, the guardrail needs about 96% recall on critical failures, which takes 85 straight catches. The pooled offline evidence (29/29) proves only 88%. The targets are placeholders for Support leadership. |
| 4 | **Reviewer view against approving on autopilot.** Claim-level flags with guide evidence beside each draft, riskiest first, plus an audit policy. | `harness/review_card.py`, `results/review_cards.md`, results §12 | 5 of the 6 release-blocking drafts show a contradicted or unsupported commercial or diagnostic claim on the card. The sixth, TCK-1023, has no claims and is caught by the error-leak and low-information checks. |
| 5 | **Routing scored as an action.** Expected action per ticket (answer, clarify, handoff, or answer+handoff), a bounce detector, and the system's decision under C3-gen versus the proposed action layer. | `reviews/actions.csv`, `harness/actions.py`, results §7 | Using the judge's `should_route_to_human` turns TCK-1012 from "reached a person only because it was held" into a routed handoff. No bouncing draft would be auto-sent under either design. |
| 6 | **Knowledge-base fixes.** Six insertions, each a restatement or scope statement traceable to the current guide, and a retrieval check before and after. | `kb/`, `harness/kb_check.py`, results §9 | TCK-1009 and TCK-1021 are generation failures: the corrective fact already ranks first, so a guide change will not fix them. Multi-part tickets lose the fact in a single query; splitting sub-questions fixes that. |
| 7 | **Staging by detected intent, not `category`.** | `harness/intents.py`, results §8 | `category` disagrees with the human-labelled intents on 6/26 tickets. A category-defined stage 2 would admit TCK-1006, TCK-1009 and TCK-1018. The detector never rates a ticket's risk below its human-labelled tier. |

## What still needs a person

1. **`eval-harness/heldout/signoff.csv`:** sign off the 66 drafted case severities. Four cases are flagged where the judges disagree with the draft (H12, H29, H39, H56). Claude's re-read agrees with the judges on H39.
2. **`eval-harness/reviews/actions.csv`:** sign off the expected action and bounce label for each ticket.
3. **`eval-harness/gates.json`:** Support leadership sets the escaped-rate targets and `set_by`.
4. **`eval-harness/kb/README.md`:** the doc owner confirms the class (b) scope statements and supplies the listed fact gaps.
5. **A second batch of held-out cases written by a person.** This removes the bias of a case author who had already read the rules.

## Cost

Judging the held-out set once with each model (Opus 5.5 and Sonnet 5.5) cost $6.18 through `claude -p`. Scoring is offline and free.
