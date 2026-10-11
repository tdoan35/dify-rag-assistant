#!/usr/bin/env python3
"""Command-line entry point for the ship/hold evaluation harness.

  python run_eval.py check-labels          validate reviews/labels.csv against the ticket data
  python run_eval.py judge --model opus    run the LLM judge (writes results/judge_cache/)
  python run_eval.py score                 score everything offline and write results/
"""

import argparse
import sys

from harness.data import load_guide, load_labels, load_tickets, validate_labels


def cmd_check_labels(_args) -> int:
    problems = validate_labels(load_tickets(), load_labels())
    for p in problems:
        print(f"[labels] {p}")
    print(f"[labels] {'OK' if not problems else f'{len(problems)} problem(s)'}")
    return 1 if problems else 0


def cmd_judge(args) -> int:
    from harness.judge import run_judge

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
    )
    return 0


def cmd_score(args) -> int:
    from harness.report import score_all

    score_all(judge_models=args.judge_models.split(","), primary_judge=args.primary_judge)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-labels", help="validate the reference labels").set_defaults(func=cmd_check_labels)

    judge = sub.add_parser("judge", help="run the LLM judge and cache its outputs")
    judge.add_argument("--model", choices=["opus", "sonnet"], default="opus")
    judge.add_argument("--backend", choices=["claude-cli", "api"], default="claude-cli")
    judge.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="high")
    judge.add_argument("--runs", type=int, default=1, help="independent repeats per ticket")
    judge.add_argument("--workers", type=int, default=4)
    judge.add_argument("--ids", help="comma-separated ticket ids (default: all)")
    judge.add_argument("--force", action="store_true", help="re-run even if cached")
    judge.set_defaults(func=cmd_judge)

    score = sub.add_parser("score", help="score from cached judgments; no network needed")
    score.add_argument("--judge-models", default="opus,sonnet")
    score.add_argument("--primary-judge", default="opus")
    score.set_defaults(func=cmd_score)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
