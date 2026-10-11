"""Tier-1 shallow grounding check, modelled on DoorDash's first guardrail layer.

DoorDash compares the response to knowledge-base segments with a sliding-window similarity check
and only sends low-similarity responses on to an LLM evaluator. This module reproduces that idea
offline: TF-IDF cosine (unigrams + bigrams) between each answer sentence and sliding windows of
1-3 consecutive guide sentences. It is a lexical proxy for DoorDash's semantic check and needs no
model or network access.

A response's tier-1 score is its weakest sentence (min over sentences of the best-matching window),
which is the most sensitive aggregation and so gives the check its best chance to catch a
hallucinated sentence.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

STOPWORDS = set(
    """a about above after again all also am an and any are as at be because been before being
    below between both but by can could did do does doing don down during each either few for from
    further had has have having he her here hers him his how i if in into is it its itself just
    let me more most my no nor not now of off on once only or other our ours out over own same she
    should so some such than that the their theirs them then there these they this those through
    to too under until up very was we were what when where which while who whom why will with would
    you your yours yourself i'd i'll i'm it's that's there's they're we're we've you'd you'll
    you're you've isn't aren't wasn't won't don't doesn't didn't can't hi hey hello yes""".split()
)
TOKEN_RE = re.compile(r"[a-z0-9]+(?:['\-][a-z0-9]+)*")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[\"'(])")
SIGNATURE_RE = re.compile(r"--\s*([A-Z][a-zA-Z]+)")


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str, drop: frozenset = frozenset()) -> list[str]:
    tokens = [_stem(t) for t in TOKEN_RE.findall(text.lower())]
    return [t for t in tokens if t not in STOPWORDS and t not in drop]


def features(tokens: list[str]) -> Counter:
    grams = Counter(tokens)
    grams.update(f"{a} {b}" for a, b in zip(tokens, tokens[1:]))
    return grams


def split_sentences(text: str) -> list[str]:
    sentences = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*").strip()
        if not line or line.startswith("#") or set(line) <= set("|-: "):
            continue
        if line.startswith("|"):
            # Keep each table row as one segment.
            sentences.append(" ".join(cell.strip() for cell in line.strip("|").split("|")))
            continue
        sentences.extend(s.strip() for s in SENTENCE_SPLIT_RE.split(line) if s.strip())
    return sentences


def guide_windows(guide: str, sizes: tuple[int, ...] = (1, 2, 3)) -> list[str]:
    """Sliding windows of consecutive sentences, never crossing a section boundary."""
    windows = []
    for section in re.split(r"\n(?=## )", guide):
        sentences = split_sentences(section)
        for size in sizes:
            for i in range(len(sentences) - size + 1):
                windows.append(" ".join(sentences[i:i + size]))
    return windows


def customer_names(ticket_text: str) -> frozenset:
    """Customer names from the ticket signature, so greetings do not count as ungrounded text."""
    return frozenset(name.lower() for name in SIGNATURE_RE.findall(ticket_text))


class TfidfIndex:
    def __init__(self, documents: list[str]):
        self.documents = documents
        doc_features = [features(tokenize(d)) for d in documents]
        df = Counter(term for f in doc_features for term in f)
        n = len(documents)
        self.idf = {term: math.log((1 + n) / (1 + count)) + 1 for term, count in df.items()}
        # Terms that never appear in the guide get the highest idf, so novel content lowers similarity.
        self.unseen_idf = math.log(1 + n) + 1
        self.vectors = [self._weigh(f) for f in doc_features]

    def _weigh(self, feats: Counter) -> dict[str, float]:
        vec = {t: (1 + math.log(c)) * self.idf.get(t, self.unseen_idf) for t, c in feats.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def best_match(self, tokens: list[str]) -> tuple[float, int]:
        query = self._weigh(features(tokens))
        best_score, best_idx = 0.0, -1
        for idx, vec in enumerate(self.vectors):
            score = sum(w * vec.get(t, 0.0) for t, w in query.items())
            if score > best_score:
                best_score, best_idx = score, idx
        return best_score, best_idx


@dataclass
class SentenceScore:
    sentence: str
    score: float
    best_window: str


@dataclass
class ResponseScore:
    ticket_id: str
    sentences: list[SentenceScore]

    @property
    def min_score(self) -> float:
        return min((s.score for s in self.sentences), default=1.0)

    @property
    def mean_score(self) -> float:
        return sum(s.score for s in self.sentences) / len(self.sentences) if self.sentences else 1.0


def score_response(
    index: TfidfIndex, ticket_id: str, ticket_text: str, answer: str, min_tokens: int = 4
) -> ResponseScore:
    names = customer_names(ticket_text)
    scored = []
    for sentence in split_sentences(answer):
        tokens = tokenize(sentence, drop=names)
        if len(tokens) < min_tokens:
            continue  # pleasantries and fragments carry no checkable content
        score, idx = index.best_match(tokens)
        scored.append(SentenceScore(sentence, score, index.documents[idx] if idx >= 0 else ""))
    return ResponseScore(ticket_id, scored)


def sentence_containing(response: ResponseScore, fragment: str) -> SentenceScore | None:
    """The scored sentence that overlaps a labelled offending fragment."""
    fragment = fragment.strip()
    for s in response.sentences:
        if fragment and (fragment in s.sentence or s.sentence in fragment):
            return s
    head = fragment[:40]
    for s in response.sentences:
        if head and head in s.sentence:
            return s
    return None
