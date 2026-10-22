"""Split text into overlapping chunks sized for the embedding model.

Sizes are in words (about 1.3 tokens each). The defaults leave room for the metadata
header that is prepended to every chunk, within bge-small's 512-token limit.
"""

import re

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _units(text: str, max_words: int) -> list[str]:
    """Paragraphs, split into sentences or word windows when a paragraph is too long."""
    units = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para.split()) <= max_words:
            units.append(para)
            continue
        for sentence in _SENTENCE.split(para):
            words = sentence.split()
            if len(words) <= max_words:
                units.append(sentence)
            else:
                units += [" ".join(words[i : i + max_words]) for i in range(0, len(words), max_words)]
    return units


def chunk_text(text: str, max_words: int = 220, overlap_words: int = 40) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    count = 0
    for unit in _units(text, max_words):
        n = len(unit.split())
        if current and count + n > max_words:
            chunks.append("\n\n".join(current))
            carry: list[str] = []
            carried = 0
            for prev in reversed(current):  # repeat the tail of the last chunk for context
                pn = len(prev.split())
                if carried + pn > overlap_words:
                    break
                carry.insert(0, prev)
                carried += pn
            current, count = (carry, carried) if carried + n <= max_words else ([], 0)
        current.append(unit)
        count += n
    if current:
        chunks.append("\n\n".join(current))
    return chunks
