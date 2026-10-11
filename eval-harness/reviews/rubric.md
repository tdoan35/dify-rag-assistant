# Grading rubric

The product guide (`data/product-guide.md`) is the only source of truth. A claim that may be true of the real Dify product but is absent from the guide counts as **unsupported**.

## Binary criteria (pass/fail)

| Criterion | Passes when |
|---|---|
| `accurate` | No claim contradicts the guide. |
| `grounded` | Every material claim is supported by the guide (reasonable paraphrase or direct inference from guide wording is fine). |
| `complete` | Every question in the ticket is addressed, including correcting a false premise when the guide makes the correction possible. |
| `escalation_ok` | Routes/defers when the guide does not cover the topic or explicitly says to route; does **not** defer when the guide answers the question. |
| `customer_safe` | No raw system errors, internal identifiers, or unusable output; tone fit for a customer. |

## Severity (one per ticket; worst issue wins)

| Severity | Definition | Release-blocking? |
|---|---|---|
| `critical` | A claim the guide contradicts or does not support about plan entitlements, security, billing, or root cause, which a customer would plausibly act on to their detriment (buy the wrong plan, misconfigure, wait for a fix that will not come). | Yes |
| `major` | An unsupported commercial promise (what a plan includes, support levels, pricing terms); an overconfident capability statement (positive or negative) the guide does not support; or no usable answer in the customer channel (system error, internal strings). | Yes |
| `minor` | Unsupported but harmless technical detail; invented assumptions about the customer's situation; mild overstatement; unnecessary deferral when the guide had the answer and no harm results. | No |
| `none` | Correct, grounded, complete, appropriately scoped. | No |

Agreed severity rule for "plausibly true but not in the guide" claims: **technical detail → minor; commercial/entitlement promise → major.**

## Failure-mode tags

`wrong_entitlement`, `fabricated_ui_path`, `contradicts_guide`, `wrong_root_cause`, `overconfident_negative`, `unsupported_commercial_claim`, `unsupported_technical_detail`, `invented_user_context`, `overstatement`, `over_abstention`, `missed_premise_correction`, `system_error_leak`, `no_clarification`, `missing_escalation`.

## Provenance

The rubric was written after a first read of all 26 responses (error analysis first, per standard eval practice). The same rubric is given to the LLM judge, without any ticket-specific hints.
