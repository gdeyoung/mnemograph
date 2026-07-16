"""
Stopword filtering and keyword extraction for graph recall.
"""
import re

STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "how", "what", "who",
    "when", "where", "why", "do", "does", "did", "to", "from", "in", "on",
    "at", "by", "for", "with", "about", "between", "into", "through",
    "during", "before", "after", "above", "below", "up", "down",
    "this", "that", "these", "those", "it", "its", "they", "them",
    "their", "there", "here", "which", "whom", "whose",
    "can", "could", "should", "would", "may", "might", "must",
    "have", "has", "had", "been", "being", "am", "be",
    "tell", "me", "explain", "give", "show", "get", "use", "using",
    "and", "or", "but", "not", "no", "nor", "so", "than", "too",
    "very", "just", "also", "only", "own", "same",
    "like", "want", "need", "make", "made", "go", "going",
    "any", "all", "each", "few", "more", "most", "other", "some", "such",
    "if", "then", "now", "out", "off", "over", "under",
})


def extract_keywords(text: str, max_kw: int = 5) -> list[str]:
    """Extract potential entity keywords from text, filtered against stopwords."""
    words = text.split()
    keywords = []
    seen = set()
    for w in words:
        clean = re.sub(r"[^a-zA-Z0-9_-]", "", w)
        if not clean or len(clean) < 2:
            continue
        if clean.lower() in STOPWORDS:
            continue
        if clean.lower() in seen:
            continue
        keywords.append(clean)
        seen.add(clean.lower())
        if len(keywords) >= max_kw:
            break
    return keywords
