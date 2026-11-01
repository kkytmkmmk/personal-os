"""Bind a model quote to literal source text, allowing whitespace changes only."""
import re


def bind_quote(quote: str, source: str) -> str | None:
    if not isinstance(quote, str):
        return None
    quote = quote.strip()
    if not quote:
        return None
    if quote in source:
        return quote
    compact = re.sub(r"\s+", "", quote)
    if len(compact) < 4:
        return None
    positions = [index for index, char in enumerate(source) if not char.isspace()]
    compact_source = "".join(source[index] for index in positions)
    start = compact_source.find(compact)
    if start < 0:
        return None
    return source[positions[start]:positions[start + len(compact) - 1] + 1]
