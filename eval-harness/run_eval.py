#!/usr/bin/env python3
"""Command-line entry point for the ship/hold evaluation harness.

  python run_eval.py check-labels          validate reviews/labels.csv against the ticket data
  python run_eval.py judge --model opus    run the LLM judge (writes results/judge_cache/)
  python run_eval.py judge --set heldout   run the judge on the held-out fault-injection set
  python run_eval.py heldout-freeze        record what the held-out set was created against
  python run_eval.py score                 score everything offline and write results/
"""

import argparse
import json
import sys

from harness.data import load_guide, load_labels, load_tickets, validate_labels


def cmd_check_labels(_args) -> int:
    from harness.actions import load_expected_actions
    from harness.heldout import integrity_warnings, load_mutants

    tickets = load_tickets()
    problems = validate_labels(tickets, load_labels())
    missing = {t.id for t in tickets} - load_expected_actions().keys()
    problems += [f"{tid}: no row in reviews/actions.csv" for tid in sorted(missing)]
    try:
        print(f"[labels] held-out set: {len(load_mutants())} cases render")
    except ValueError as e:
        problems.append(f"heldout/mutations.json: {e}")
    for warning in integrity_warnings():
        print(f"[labels] WARNING: {warning}")
    for p in problems:
        print(f"[labels] {p}")
    print(f"[labels] {'OK' if not problems else f'{len(problems)} problem(s)'}")
    return 1 if problems else 0


def cmd_judge(args) -> int:
    from harness.judge import CACHE_DIR, HELDOUT_CACHE_DIR, run_judge

    if args.set == "heldout":
        from harness.heldout import integrity_warnings, load_mutants

        for warning in integrity_warnings():
            print(f"[judge] WARNING: {warning}")
        tickets = [m.ticket for m in load_mutants()]
    else:
        tickets = load_tickets()
    if args.ids:
        wanted = set(args.ids.split(","))
        tickets = [t for t in tickets if t.id in wanted]
    run_judge(
        tickets,
        load_guide(),
        model=args.model,
        runs=args.runs,
        backend=args.backend,
        effort=args.effort,
        workers=args.workers,
        force=args.force,
        cache_dir=HELDOUT_CACHE_DIR if args.set == "heldout" else CACHE_DIR,
    )
    return 0


def cmd_heldout_freeze(args) -> int:
    from harness.heldout import MANIFEST_PATH, freeze

    manifest = freeze(author=args.author, force=args.force)
    print(f"[heldout] froze {MANIFEST_PATH} ({json.dumps(manifest['guardrail_sha256'])})")
    return 0


def cmd_score(args) -> int:
    from harness.report import score_all

    score_all(judge_models=args.judge_models.split(","), primary_judge=args.primary_judge)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-labels", help="validate the reference labels, expected actions and held-out set").set_defaults(func=cmd_check_labels)

    judge = sub.add_parser("judge", help="run the LLM judge and cache its outputs")
    judge.add_argument("--model", choices=["opus", "sonnet"], default="opus")
    judge.add_argument("--backend", choices=["claude-cli", "api"], default="claude-cli")
    judge.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="high")
    judge.add_argument("--runs", type=int, default=1, help="independent repeats per ticket")
    judge.add_argument("--workers", type=int, default=4)
    judge.add_argument("--set", choices=["sample", "heldout"], default="sample",
                       help="sample = the 26 tickets; heldout = the fault-injection set")
    judge.add_argument("--ids", help="comma-separated ticket ids (default: all)")
    judge.add_argument("--force", action="store_true", help="re-run even if cached")
    judge.set_defaults(func=cmd_judge)

    freeze = sub.add_parser("heldout-freeze", help="hash the guardrail code and judge prompt the held-out set is tested against")
    freeze.add_argument("--author", required=True)
    freeze.add_argument("--force", action="store_true", help="overwrite an existing manifest")
    freeze.set_defaults(func=cmd_heldout_freeze)

    score = sub.add_parser("score", help="score from cached judgments; no network needed")
    score.add_argument("--judge-models", default="opus,sonnet")
    score.add_argument("--primary-judge", default="opus")
    score.set_defaults(func=cmd_score)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
