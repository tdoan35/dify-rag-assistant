"""Load the ticket sample, the product guide, and the human reference labels."""

import csv
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REVIEWS_DIR = ROOT / "reviews"
RESULTS_DIR = ROOT / "results"
PROMPTS_DIR = ROOT / "prompts"

SEVERITIES = ["none", "minor", "major", "critical"]
BLOCKING_SEVERITIES = {"major", "critical"}
CRITERIA = ["accurate", "grounded", "complete", "escalation_ok", "customer_safe"]


@dataclass(frozen=True)
class Ticket:
    id: str
    ticket_text: str
    assistant_answer: str
    category: str


def load_tickets(path: Path = DATA_DIR / "tickets.csv") -> list[Ticket]:
    with open(path, newline="", encoding="utf-8") as f:
        return [Ticket(**row) for row in csv.DictReader(f)]


def load_guide(path: Path = DATA_DIR / "product-guide.md") -> str:
    return path.read_text(encoding="utf-8")


def load_labels(path: Path = REVIEWS_DIR / "labels.csv") -> dict[str, dict]:
    """Reference labels keyed by ticket id, with binary fields parsed to bool."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    labels = {}
    for row in rows:
        for field in CRITERIA + ["multi_part", "release_blocking"]:
            row[field] = row[field].strip() == "1"
        row["intents"] = [i for i in row["intents"].split(";") if i]
        row["failure_modes"] = [m for m in row["failure_modes"].split(";") if m]
        if row["severity"] not in SEVERITIES:
            raise ValueError(f"{row['id']}: unknown severity {row['severity']!r}")
        if row["release_blocking"] != (row["severity"] in BLOCKING_SEVERITIES):
            raise ValueError(f"{row['id']}: release_blocking disagrees with severity")
        labels[row["id"]] = row
    return labels


def validate_labels(tickets: list[Ticket], labels: dict[str, dict]) -> list[str]:
    """Return a list of problems: missing tickets, or offending_text not found in the answer."""
    problems = []
    by_id = {t.id: t for t in tickets}
    for ticket_id in by_id.keys() - labels.keys():
        problems.append(f"{ticket_id}: no reference label")
    for ticket_id, label in labels.items():
        ticket = by_id.get(ticket_id)
        if ticket is None:
            problems.append(f"{ticket_id}: label for unknown ticket")
        elif label["offending_text"] and label["offending_text"] not in ticket.assistant_answer:
            problems.append(f"{ticket_id}: offending_text is not a substring of the answer")
    return problems
