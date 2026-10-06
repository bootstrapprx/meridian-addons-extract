"""Bounded lexical relevance; no external service or cross-user index."""
import math
import re
import unicodedata
from collections import Counter


def tokens(value):
    folded = unicodedata.normalize("NFKD", value.lower())
    return re.findall(r"\w+", "".join(char for char in folded if not unicodedata.combining(char)))


def rank(query, documents):
    """BM25 scores over the caller's already authorized candidate slice."""
    terms = set(tokens(query))
    counts = [Counter(tokens(document)) for document in documents]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / len(lengths) if lengths else 1
    average = average or 1
    document_frequency = {term: sum(term in count for count in counts) for term in terms}
    scores = []
    for count, length in zip(counts, lengths):
        score = 0
        for term in terms:
            frequency = count[term]
            if not frequency:
                continue
            matching = document_frequency[term]
            inverse = math.log(1 + (len(counts) - matching + 0.5) / (matching + 0.5))
            score += inverse * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * length / average))
        scores.append(round(score, 6))
    return scores
