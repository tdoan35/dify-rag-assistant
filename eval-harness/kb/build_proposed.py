#!/usr/bin/env python3
"""Rebuild kb/product-guide.proposed.md from data/product-guide.md.

Each change is an explicit insertion with an id that kb/README.md explains. Class (a) restates a
fact the guide already contains, in a form and place retrieval will surface; class (b) is a scope
statement the doc owner must confirm. Nothing here adds a new product fact.

    python3 kb/build_proposed.py
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "product-guide.md"
TARGET = ROOT / "kb" / "product-guide.proposed.md"


def insert_after(text: str, anchor: str, addition: str) -> str:
    if text.count(anchor) != 1:
        raise SystemExit(f"anchor not found exactly once: {anchor[:60]!r}")
    return text.replace(anchor, anchor + addition)


def replace(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"text not found exactly once: {old[:60]!r}")
    return text.replace(old, new)


def build(g: str) -> str:
    # K1 (a) §2: bringing your own model goes through plugins; custom integrations are routed.
    g = insert_after(
        g, "this guide does not enumerate which self-hosted runtimes currently have an official plugin.",
        "\n\n**Using your own model.** A workspace is not limited to the officially published providers: community "
        "provider plugins are also listed in the Marketplace, and self-hosted or local model runtimes can be connected "
        "through a provider plugin when one is published. This guide does not list which runtimes have a plugin, and "
        "building a custom integration with the plugin SDK is not covered here; route those questions to the "
        "specialist team.")

    # K2 (a) §3: the guide's negative statement about empty retrieval, as its own chunk.
    g = insert_after(
        g, "not by an account or provider-propagation delay.",
        "\n\n**Empty retrieval is not a propagation delay.** If a retrieval step returns zero chunks, the cause is not "
        "an account change or a newly connected model provider still propagating, so there is nothing to wait for. "
        "Check the score threshold (set too high, it discards every chunk), whether the document is still being "
        "indexed, and whether the query matches any ingested content (use Hit Testing).")

    # K3 (a) §4: per-workspace billing means seats do not change the price, as its own short chunk.
    g = insert_after(
        g, "annual billing is discounted roughly 17% versus paying monthly.",
        "\n\n**Seats and price.** Because Dify Cloud is billed per workspace, not per seat, adding or removing members "
        "(seats) does not change a plan's price. The member count matters only against the plan's member cap "
        "(see Section 1).")

    # K4 (a) §4 table: the Enterprise-only capabilities as a column, so each Cloud row says "Not included".
    g = replace(g, "| Plan | Price | Message credits | Team members | Apps | Knowledge documents | Knowledge data storage | API rate limit |",
                "| Plan | Price | Message credits | Team members | Apps | Knowledge documents | Knowledge data storage | API rate limit | SSO/SAML, detailed audit logging, dedicated CSM |")
    g = replace(g, "|---|---|---|---|---|---|---|---|", "|---|---|---|---|---|---|---|---|---|")
    for row_start, cell in [("| Sandbox |", "Not included"), ("| Professional |", "Not included"),
                            ("| Team |", "Not included"),
                            ("| Enterprise |", "Enterprise capabilities; scope negotiated with sales")]:
        line = next(l for l in g.splitlines() if l.startswith(row_start))
        g = replace(g, line, f"{line} {cell} |")

    # K5 (a) + (b) §4: what no Cloud plan includes; support levels are not documented here.
    g = insert_after(
        g, "| Enterprise capabilities; scope negotiated with sales |",
        "\n\n**What no Cloud plan includes.** Single sign-on (SSO/SAML), detailed audit logging and a dedicated customer "
        "success manager are not part of any Cloud plan, including Team; they belong to the separately sold Enterprise "
        "product (see below). This guide does not document support channels, support response times or "
        "priority-support levels for any plan, so questions about support levels go to sales rather than being "
        "answered from this table.")

    # K6 (b) "Not covered": name the gaps the assistant filled from its own training data.
    g = replace(
        g, "or workflow-level orchestration features beyond the retrieval node settings described above.",
        "workflow-level orchestration features beyond the retrieval node settings described above, support channels "
        "or priority-support levels for any plan, or proration and refunds for mid-cycle plan changes.")
    return g


if __name__ == "__main__":
    TARGET.write_text(build(SOURCE.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
