"""CJK-aware text transform for FTS indexing and query matching.

FTS5's ``unicode61`` tokenizer treats a contiguous CJK run as a single token,
so Chinese substring queries almost never match. The classic fix is bigram
indexing: every CJK run is expanded into overlapping two-character tokens on
BOTH the indexing side (the ``content_chunks.fts_text`` shadow column) and the
query side (each query token becomes a phrase of its bigrams, giving exact
substring semantics). Non-CJK text passes through unchanged, so ASCII search
behaves exactly as before.
"""

import re

__all__ = ["fts_text"]

# CJK ideographs (unified + extension A + compatibility), kana, and hangul —
# the scripts where unicode61 has no word boundaries to work with.
_CJK_RUN = re.compile(r"[぀-ヿㇰ-ㇿ㐀-䶿一-鿿가-힯豈-﫿]+")


def fts_text(text: str) -> str:
    """Expand CJK runs into overlapping bigrams; keep other text verbatim."""
    pieces: list[str] = []
    last = 0
    for match in _CJK_RUN.finditer(text):
        if match.start() > last:
            segment = text[last : match.start()].strip()
            if segment:
                pieces.append(segment)
        run = match.group()
        if len(run) == 1:
            pieces.append(run)
        else:
            pieces.extend(run[index : index + 2] for index in range(len(run) - 1))
        last = match.end()
    if last < len(text):
        segment = text[last:].strip()
        if segment:
            pieces.append(segment)
    return " ".join(pieces)
