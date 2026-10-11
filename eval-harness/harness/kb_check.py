"""Would retrieval surface the fact that corrects each failure, before and after the KB changes?

The guide is split the way a default Dify General chunker would split it: on blank lines, with
heading-only blocks merged into the block that follows (a table stays one chunk). Each probe is a
ticket, or one sub-question of a multi-part ticket. The check reports the rank of the first chunk
that states the corrective fact, for `data/product-guide.md` and `kb/product-guide.proposed.md`.

Retrieval is TF-IDF keyword matching, which approximates Dify's Economical (keyword) index, not
its High Quality (vector) index. A failure where the corrective chunk is already ranked first
points at generation, not retrieval: changing the guide will not fix it on its own.
"""

import re
from dataclasses import dataclass

from .data import DATA_DIR, ROOT
from .similarity import TfidfIndex, features, tokenize

PROPOSED_GUIDE = ROOT / "kb" / "product-guide.proposed.md"
TOP_K = 3


def chunk_guide(text: str) -> list[str]:
    chunks, pending = [], ""
    for block in (b.strip() for b in text.split("\n\n")):
        if not block:
            continue
        if block.startswith("#") and "\n" not in block:
            pending += block + "\n"
            continue
        chunks.append(pending + block)
        pending = ""
    if pending:
        chunks.append(pending.strip())
    return chunks


class RankingIndex(TfidfIndex):
    def rank(self, text: str) -> list[int]:
        query = self._weigh(features(tokenize(text)))
        scores = [sum(w * vec.get(t, 0.0) for t, w in query.items()) for vec in self.vectors]
        return sorted(range(len(scores)), key=lambda i: -scores[i])


@dataclass(frozen=True)
class Probe:
    ticket_id: str
    query_kind: str  # "whole ticket" or "sub-question"
    query: str
    fact: str
    pattern: re.Pattern


SSO_ENTERPRISE = re.compile(r"(single sign-on|sso)[\s\S]*(enterprise|any cloud)|(enterprise|any cloud)[\s\S]*(single sign-on|sso)", re.IGNORECASE)
PROBES = [
    Probe("TCK-1009", "whole ticket", "Does Dify support SSO/SAML login for workspace members? We'd want to roll this out for our whole team.",
          "SSO is Enterprise-only, not on any Cloud plan", SSO_ENTERPRISE),
    Probe("TCK-1006", "whole ticket", "Two things: first, our Sandbox app's API calls just started failing with rate limit errors and we haven't changed anything. Second, if we upgrade to Team, does that get us SSO for our members? We have 40 people who'd need to log in with our company SSO.",
          "SSO is Enterprise-only, not on any Cloud plan", SSO_ENTERPRISE),
    Probe("TCK-1006", "sub-question", "if we upgrade to Team, does that get us SSO for our members? We have 40 people who'd need to log in with our company SSO.",
          "SSO is Enterprise-only, not on any Cloud plan", SSO_ENTERPRISE),
    Probe("TCK-1005", "whole ticket", "What exactly do we get if we upgrade to the Team plan, and how much is it?",
          "support levels are not documented for any plan", re.compile(r"support (channels|levels|response)", re.IGNORECASE)),
    Probe("TCK-1012", "whole ticket", "Can I use my own model with Dify, or am I stuck with what's in the Marketplace?",
          "community and self-hosted provider plugins exist", re.compile(r"community provider|self-hosted or local model runtimes", re.IGNORECASE)),
    Probe("TCK-1015", "whole ticket", "Our knowledge base upload keeps failing partway through, and separately, could we drop a couple of Team seats to save money mid-cycle without losing any of our data?",
          "billing is per workspace, not per seat", re.compile(r"not per individual seat|does not change a plan's price", re.IGNORECASE)),
    Probe("TCK-1015", "sub-question", "could we drop a couple of Team seats to save money mid-cycle without losing any of our data?",
          "billing is per workspace, not per seat", re.compile(r"not per individual seat|does not change a plan's price", re.IGNORECASE)),
    Probe("TCK-1021", "whole ticket", "I just connected Anthropic as a provider for my support bot, and also, my workflow's retrieval step has started returning zero chunks for queries that used to work. Can you help with both?",
          "empty retrieval is not a propagation delay", re.compile(r"provider-propagation delay|still propagating", re.IGNORECASE)),
    Probe("TCK-1021", "sub-question", "my workflow's retrieval step has started returning zero chunks for queries that used to work.",
          "empty retrieval is not a propagation delay", re.compile(r"provider-propagation delay|still propagating", re.IGNORECASE)),
]


def first_hit_rank(index: RankingIndex, probe: Probe) -> tuple[int | None, str]:
    """1-based rank of the first chunk stating the corrective fact, and that chunk's opening."""
    for rank, idx in enumerate(index.rank(probe.query), start=1):
        if probe.pattern.search(index.documents[idx]):
            return rank, index.documents[idx].replace("\n", " ")[:70]
    return None, ""


def run_kb_check() -> list[dict]:
    guides = {
        "current": (DATA_DIR / "product-guide.md").read_text(encoding="utf-8"),
        "proposed": PROPOSED_GUIDE.read_text(encoding="utf-8"),
    }
    indexes = {name: RankingIndex(chunk_guide(text)) for name, text in guides.items()}
    rows = []
    for probe in PROBES:
        row = {"ticket": probe.ticket_id, "query": probe.query_kind, "corrective_fact": probe.fact}
        for name, index in indexes.items():
            rank, chunk = first_hit_rank(index, probe)
            row[f"{name}_rank"] = rank
            row[f"{name}_in_top_{TOP_K}"] = int(rank is not None and rank <= TOP_K)
            row[f"{name}_chunk"] = chunk
        rows.append(row)
    return rows
