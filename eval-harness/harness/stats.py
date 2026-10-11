"""Small-sample statistics: Wilson intervals, confusion matrices, and agreement coefficients."""

import math
from dataclasses import dataclass

from .data import SEVERITIES


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion k/n."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def fmt_rate(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} ({k / n:.0%}; 95% CI {lo:.0%}–{hi:.0%})" if n else "n/a"


@dataclass
class Confusion:
    """Binary confusion matrix where 'positive' means release-blocking."""
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    @classmethod
    def from_pairs(cls, truth: list[bool], pred: list[bool]) -> "Confusion":
        c = cls()
        for t, p in zip(truth, pred):
            if t and p:
                c.tp += 1
            elif t:
                c.fn += 1
            elif p:
                c.fp += 1
            else:
                c.tn += 1
        return c

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.tn + self.fn

    @property
    def tpr(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else float("nan")

    @property
    def tnr(self) -> float:
        return self.tn / (self.tn + self.fp) if self.tn + self.fp else float("nan")

    @property
    def kappa(self) -> float:
        return cohen_kappa_binary(self)


def cohen_kappa_binary(c: Confusion) -> float:
    n = c.n
    if n == 0:
        return float("nan")
    observed = (c.tp + c.tn) / n
    p_yes = ((c.tp + c.fn) / n) * ((c.tp + c.fp) / n)
    p_no = ((c.tn + c.fp) / n) * ((c.tn + c.fn) / n)
    expected = p_yes + p_no
    return (observed - expected) / (1 - expected) if expected < 1 else 1.0


def weighted_kappa(a: list[str], b: list[str], categories: list[str] = SEVERITIES) -> float:
    """Linearly weighted Cohen's kappa for ordinal labels (severity)."""
    k = len(categories)
    idx = {c: i for i, c in enumerate(categories)}
    n = len(a)
    if n == 0:
        return float("nan")
    observed = [[0.0] * k for _ in range(k)]
    for x, y in zip(a, b):
        observed[idx[x]][idx[y]] += 1 / n
    row = [sum(r) for r in observed]
    col = [sum(observed[i][j] for i in range(k)) for j in range(k)]

    def weight(i: int, j: int) -> float:
        return abs(i - j) / (k - 1)

    disagree_obs = sum(weight(i, j) * observed[i][j] for i in range(k) for j in range(k))
    disagree_exp = sum(weight(i, j) * row[i] * col[j] for i in range(k) for j in range(k))
    return 1 - disagree_obs / disagree_exp if disagree_exp else 1.0
