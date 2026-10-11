# Approach: Ship-or-Hold Evaluation of the Support-Ticket RAG Assistant

## 1. Purpose and decision

**Question.** Should Dify let customers receive responses from its support-ticket RAG assistant directly, with no human review?

**Inputs and constraints.**

| Input | Notes |
|---|---|
| 26 support tickets with prerecorded assistant responses | `eval-harness/data/tickets.csv`. The `category` column holds one label even when a ticket asks about several topics. |
| Product guide | `eval-harness/data/product-guide.md`. It is the **only** factual reference: a claim absent from the guide counts as unsupported, even if it is true of the real product. |
| No live assistant endpoint | We cannot regenerate answers, test consistency, or try fixes. |
| No logged retrieval context | We cannot separate retrieval failures from generation failures. Every judgment is end-to-end against the full guide. |

**Recommendation framing.** The team agreed on this before any scoring:

> **Hold direct-to-customer release. Ship as agent-assist now** (assistant drafts, human approves). **Move to auto-send in stages behind measurable gates** (Section 8).

The evaluation below is designed to test that framing and to put numbers on it, not to reach a verdict decided in advance. Results are reported in `eval-harness/results/summary.md`.

## 2. What the industry does

Production RAG support assistants do not treat retrieval as the safety mechanism. They surround generation with a separate quality layer, and the publicly reported failures are deployments without one.

| Who | Quality layer | Relevance here |
|---|---|---|
| **DoorDash** ([Sept 2024](https://careersatdoordash.com/blog/large-language-modules-based-dasher-support-automation/)) | **Online guardrail with two tiers:** a cheap semantic-similarity check of the response against knowledge-base segments, then an LLM evaluator (groundedness, coherence, policy), run only when the first tier flags. Failures fall back to a human. **Offline:** an LLM Judge across 5 quality dimensions, calibrated against a human review team, plus a Promptfoo regression suite that blocks prompt changes that fail. Reported about 90% fewer hallucinations and about 99% fewer severe compliance issues. | This is the reference architecture. DoorDash also noted that errors often came from the model's training data rather than the knowledge base. |
| **DoorDash simulator** ([InfoQ, Mar 2026](https://www.infoq.com/news/2026/03/doordash-llm-chatbot-simulator/)) | An LLM-simulated customer runs multi-turn conversations against the bot. An LLM-as-judge is calibrated against human judgment before it is trusted. Guardrails are validated with the full evaluation suite before deployment. | Judges must be validated before their numbers count. |
| **Intercom Fin** ([Evals & Releases, Aug 2026](https://www.intercom.com/blog/announcing-evals-and-releases); [Blueprint: Evaluate](https://fin.ai/blueprint/service/launching-ai-agents/evaluate)) | A validation step at generation time. **Evals** score simulated conversations with deterministic checks plus an LLM judge, and failing any criterion fails the scenario. **Releases** support staged ramps, A/B tests and instant rollback. **Monitors** check every live conversation. The blueprint recommends testing vague, multi-part, sensitive and edge-case queries, and checking escalation behaviour. | Per-criterion pass/fail and staged rollout replace a single go/no-go decision. |
| **Cloud grounding checks** ([AWS Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-contextual-grounding-check.html)) | Off-the-shelf grounding and relevance scores with configurable thresholds. | The docs state that **conversational QA / chatbot use cases are not supported**, and that if **any** chunk is relevant, the whole response counts as relevant. Both are a poor fit for multi-part support tickets. |
| **Cautionary cases** | **Cursor** ([Ars Technica, Apr 2025](https://arstechnica.com/ai/2025/04/cursor-ai-support-bot-invents-fake-policy-and-triggers-user-uproar/)): an unlabelled AI support agent invented a one-device login policy, and users publicly cancelled. **Air Canada** (2024): a tribunal held the airline responsible for its chatbot's misinformation. **Klarna** (2025): reportedly brought human agents back after quality concerns. | Wrong statements about entitlements and policy create business and legal exposure. |

Guidance on evaluation practice ([Hamel Husain, evals FAQ](https://hamel.dev/blog/posts/evals-faq/)):
- Use binary pass/fail rather than Likert scales.
- Treat an LLM judge as a classifier, and measure its true-positive and true-negative rates against human labels.
- Plan on 100–200 labelled examples per failure mode before trusting a judge.
- Distinguish guardrails from evaluators. Guardrails are inline, fast and conservative, and block output. Evaluators run offline, are heavier, and measure quality.

## 3. Why RAG fails in support, mapped to this sample

| Failure pattern | Mechanism | Tickets |
|---|---|---|
| **Questions outside the guide answered from the model's own knowledge** | Retrieval returns the nearest chunk, and the model fills the gap with plausible product "knowledge" instead of routing. | TCK-1006 and TCK-1009 (SSO attributed to the Team plan; the guide places SSO under Enterprise). TCK-1012 ("no way" to bring your own model; the guide mentions community and self-hosted provider plugins). |
| **Multi-part tickets** | A single retrieval query is dominated by one intent, so the second part is answered without grounding. | All 3 multi-part tickets are defective: TCK-1006, TCK-1015, TCK-1021. |
| **Negations in the guide flipped** | Many key guide facts are negative statements, and the model replaces them with a plausible positive claim. | TCK-1021: "wait 24h for propagation" when the guide says the cause is *not* a provider-propagation delay. TCK-1015: does not use "billed per workspace, not per seat" to correct the customer's premise. |
| **Product knowledge from training leaking in** | Details likely true of the real product but absent from the guide. | TCK-1005 ("priority email support" and priority processing on Team: a commercial promise). TCK-1001 (Bearer-token header: a technical detail). |
| **Pipeline failures reaching the customer** | No fallback when retrieval or generation fails. | TCK-1023: a raw `[ERROR: response_generation_timeout] … no_chunks_returned` string sent to a frustrated customer. |

**Preliminary draft labels, pending human sign-off:**
- 3 **critical**: TCK-1006, TCK-1009, TCK-1021
- 3 **major**: TCK-1005, TCK-1012, TCK-1023
- 4 **minor**: TCK-1001, TCK-1004, TCK-1015, TCK-1020
- 16 **none**

That makes **6/26 release-blocking** (preliminary).

## 4. Evaluation design

The evaluation has four layers. Each has a defined role and stated limits.

### A. Human-adjudicated reference labels (primary evidence)

**Binary criteria.** Each of these is pass/fail per ticket:
- `accurate`: no claim contradicts the guide.
- `grounded`: every material claim is supported by the guide.
- `complete`: every part of the ticket is addressed, including correcting a false premise.
- `escalation_ok`: routes when it should, and does not defer when the guide answers the question.
- `customer_safe`: no system errors or internal strings; tone fit for a customer.

**Severity.** One per ticket: `none` / `minor` / `major` / `critical`. **Release-blocking = major or critical.**

**Agreed rule for claims that are plausibly true but not in the guide:** technical detail is **minor**; a commercial or entitlement promise is **major**.

Each label also records failure-mode tags, guide section references, the offending text span, and the expected behaviour.

**Process.** Claude drafts the labels, then the human owner reviews and signs off. The rubric is in `eval-harness/reviews/rubric.md`.

### B. Deterministic checks

**Input-side routing** (before generation):
- **Out-of-scope topics**, taken from the guide's own *"What this guide does not cover"* section (SSO/SAML, audit-log export, compliance certifications, self-hosted/Community Edition, plugin SDK) and from its list of Enterprise-only capabilities.
- **Multi-part tickets**, detected from patterns such as "two things", "and separately", or multiple question groups.
- **Low-information tickets** (very few content words), routed for clarification.

**Output-side checks** (after generation):
- **System-error leak:** regex for error and internal-status strings.
- **Numeric grounding:** every number in the answer must appear in the guide or the ticket.
- **Plan fact table:** each plan-attributed quantity (price, credits, members, apps, documents, storage) must match that plan.
- **Enterprise-only features attributed to a Cloud plan** (Sandbox, Professional or Team).
- **A small "guide negation registry":** explicit "not" statements in the guide that an answer must not contradict.

> **Caveat.** Rules written *after* seeing these failures are **regression tests and guardrail candidates**, not evidence that the checks generalise. They are labelled that way in the results.

### C. Tier-1 shallow similarity check (DoorDash-style)

- **Windows:** sliding windows of 1–3 consecutive guide sentences.
- **Scoring:** each substantive answer sentence is scored by its maximum TF-IDF cosine similarity (unigrams and bigrams) to any window. The response score is its **weakest sentence**.
- **Threshold:** swept across a range rather than tuned on the labels.
- **Runtime:** Python standard library only, no API key.
- TF-IDF is a lexical stand-in for DoorDash's semantic similarity.

**Hypothesis, and what we measured.** We expected recombination errors (every word is in the guide but wrongly connected, such as "SSO" and "Team") and negation flips (the guide literally contains "provider-propagation delay") to score *high* on similarity and so skip tier 2. The measurement only partly supports this:
- **Some signal.** Labelled problem sentences do score lower than typical clean sentences, sitting between the 6th and 34th percentile of clean-sentence scores.
- **Heavy overlap.** To pass all 6 release-blocking answers on to the judge, tier 1 must flag about 75% of traffic. At a 50% flag rate it misses the flat "no" in TCK-1012. At 25% it lets four release-blocking answers through, including the SSO-on-Team critical in TCK-1006.

The conclusion holds either way: for high-risk topics, don't rely on tier 1. Run the judge on every draft (see `eval-harness/results/summary.md` §3 and §5).

### D. LLM judge (reference-grounded, claim-level)

**Procedure.** Using the full guide as reference, the judge:
1. lists the ticket's sub-questions and whether each was addressed;
2. breaks the answer into atomic claims and labels each **supported / contradicted / unsupported**, with verbatim guide evidence;
3. assesses escalation behaviour (appropriate, missing escalation, or unnecessary deferral);
4. checks customer safety;
5. assigns severity using the same rubric as layer A, with no ticket-specific hints.

**Isolation.** The judge runs as a separate `claude -p` process, launched from a scratch directory, with these flags:
- `--tools ""`
- `--strict-mcp-config`
- `--system-prompt` (replacing the default)
- `--json-schema` (structured output)
- `--no-session-persistence`

The judge therefore **never sees the reference labels** or this design discussion.

**Robustness.**
- **Two judge models:** Opus 5.5 and Sonnet 5.5. Sonnet is the cheaper candidate for a production guardrail.
- **Repeated runs** to measure stability.
- **Frozen prompt:** the judge prompt was fixed before any judge output was seen.

**Reproducibility.** Raw judge outputs, including cost and latency metadata, are cached in `eval-harness/results/`, so reviewers can re-score without credentials. An optional Anthropic API backend is available if a key is supplied.

## 5. Guardrail replay

We replay the 26 responses through candidate quality-layer configurations, using the human labels as ground truth.

| Config | Description |
|---|---|
| **C0** | No quality layer: every response is auto-sent. |
| **C1** | DoorDash-faithful: tier-1 similarity check, then the LLM judge on flagged responses only. |
| **C2** | LLM judge on every response. |
| **C3** (recommended) | Pre-generation routing (out-of-scope, multi-part, low-information) → deterministic output checks → LLM judge on every response, with priority on high-risk billing, entitlement and security topics. |

**Why C3 can afford a judge on every response.** DoorDash used a cheap first tier mainly because live chat has a tight latency and cost budget. Support tickets are **asynchronous**, so the latency budget is generous. One extra judge call per ticket is small next to a human touch or a wrong entitlement promise.

**Metrics per configuration:**
- release-blocking failures that **escape to customers** (count and ticket IDs);
- **auto-send rate** (automation);
- **false-hold rate** on good answers;
- **judge-call count and cost**, taken from the `claude -p` usage metadata.

For C1, results are shown across the threshold sweep rather than at a single tuned operating point.

## 6. Statistics

- **Intervals:** every rate carries a **Wilson 95% interval**. With n=26 these intervals are wide and are reported honestly. For example, the preliminary 6/26 release-blocking rate (about 23%) spans roughly 11–42%.
- **Judge vs human:** true-positive and true-negative rates on the release-blocking class, Cohen's kappa, and exact severity agreement.
- **Judge stability:** self-consistency across repeated runs, and agreement between Opus and Sonnet.
- **Small numbers:** with about 6 failures, even a perfect 6/6 catch only supports a lower bound of about 61% on the catch rate. Catching every failure in this sample is not proof the guardrail is reliable.

## 7. Threats to validity and limitations

| Threat | Mitigation | Remaining risk |
|---|---|---|
| n = 26 | Wilson intervals; statements kept qualitative where needed | Rates are only indicative |
| One reviewer drafted the labels | Human owner reviews and signs off | Reviewer bias reduced, not removed |
| The same model family drafted the labels and runs the judge | Process isolation (judge never sees labels); human sign-off | Correlated blind spots are possible |
| Rubric written after error analysis | Rubric is generic, with no ticket-specific hints to the judge | Some fit to this sample |
| Rules written after seeing failures | Labelled as regression tests | Generalisation untested |
| Replay checks against the full guide, not retrieved chunks | Stated explicitly | **Optimistic** relative to production |
| No retrieved-context logs | End-to-end judgment only | Retrieval and generation errors cannot be separated |
| No live endpoint | n/a | No consistency, paraphrase or perturbation testing |
| TF-IDF is lexical | Stated explicitly | An embedding model may behave differently |

## 8. Release gates: from agent-assist to auto-send

> **Revised after review** (`docs/additions.md`). Stages are defined by **detected intent**, not the single-label `category` column (results §8). Gates 1–2 are now derived from escaped-rate targets that Support leadership sets in `eval-harness/gates.json` (results §10), and measured on agent-assist shadow logs (results §11). For example, "zero critical failures in 200 tickets" would only bound the critical rate at 1.9%.

Auto-send is enabled **per detected intent, in stages**, only when all of these hold:

1. **Escaped critical rate below target, measured in shadow mode.** Among drafts the configuration *would* have auto-sent during agent-assist, the Wilson upper bound on drafts a reviewer had to fix materially must be below the target. At a 0.5% target that takes about 765 would-send drafts with zero critical escapes.
2. **Escaped release-blocking rate below its target**, measured the same way. Before the shadow pilot, guardrail recall on the held-out fault-injection set must already clear the recall the target implies (results §6, §10).
3. A **validated judge**: true-positive and true-negative rates measured against 100–200 human-labelled examples per failure mode.
4. **Pre-generation routing live in the Dify app**: a Question Classifier node for out-of-scope and multi-part tickets, a retrieval **score threshold**, and IF/ELSE handoff to a human when retrieval is empty or low-scoring ([Dify Knowledge Retrieval node](https://docs.dify.ai/en/cloud/use-dify/nodes/knowledge-retrieval)).
5. **Retrieved-chunk logging**, so retrieval and generation can be evaluated separately.
6. **AI-response disclosure** in every customer-facing reply.
7. **Staged rollout**: low-risk how-to intents first (for example Knowledge Pipeline), with live monitoring, sampled human review using the audit policy in `harness/shadow.py`, and rollback.
8. **Reviewer view during agent-assist**: the judge's claim-level flags are shown beside each draft (`results/review_cards.md`), so reviewers check specific claims instead of approving fluent text on autopilot.

## 9. Deliverables map

```
rag-ship-hold-recommendation.zip
├── eval-harness.zip          # nested archive
│   ├── data/                 # tickets.csv, product-guide.md (inputs)
│   ├── reviews/              # rubric.md, reference labels (human-signed)
│   ├── prompts/              # frozen judge prompt + JSON schema
│   ├── harness/ + run script # deterministic checks, similarity, judge, stats
│   ├── results/              # cached judge outputs, per-ticket results, summary.md
│   └── README.md             # run instructions (re-score offline; re-judge with claude -p or API key)
└── ship-hold-memo.docx       # 1–2 page release recommendation and evidence
```
