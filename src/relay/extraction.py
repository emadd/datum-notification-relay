"""A minimal, pure JSON dot-path value picker for `remoteFetch` jobs.

Path grammar (deliberately small — this mirrors "simple JSON dot-path"
extraction, not a full JSONPath/JMESPath implementation):

    key                 -> object member access
    key.key2.key3       -> nested object member access
    key[0]              -> array index access
    key[0][1]           -> chained array index access
    key.items[2].value  -> mixed

No wildcards, no filters, no slices. That's intentional: this server never
needs to understand the shape of a client's payload beyond "pick one scalar
out of it," and a bigger grammar is a bigger attack surface for no benefit.
"""

from __future__ import annotations

from typing import Any, List, Union

PathToken = Union[str, int]


class ExtractionError(ValueError):
    """Raised when a path is malformed or does not resolve against the data."""


def parse_path(path: str) -> List[PathToken]:
    """Hand-rolled tokenizer (not a regex) so the "gap between tokens must be
    exactly one separator" rule is explicit and easy to reason about: a `.`
    always introduces a new key segment, `[N]` always introduces a new index
    segment, and the two may chain directly (`items[0]`) with no separator
    between a key and a following `[`.
    """
    if not path or not path.strip():
        raise ExtractionError("extraction path must not be empty")

    tokens: List[PathToken] = []
    i = 0
    n = len(path)
    expect_segment_start = True  # true right after '.', at index 0, or after ']'

    while i < n:
        ch = path[i]

        if ch == ".":
            if expect_segment_start:
                raise ExtractionError(f"unexpected '.' at index {i} in {path!r}")
            expect_segment_start = True
            i += 1
            continue

        if ch == "[":
            end = path.find("]", i)
            if end == -1:
                raise ExtractionError(f"unterminated '[' at index {i} in {path!r}")
            digits = path[i + 1 : end]
            if not digits.isdigit():
                raise ExtractionError(
                    f"expected a non-negative integer index in '[...]' at index {i} "
                    f"in {path!r}, got {digits!r}"
                )
            tokens.append(int(digits))
            i = end + 1
            expect_segment_start = False
            continue

        if ch == "]":
            raise ExtractionError(f"unexpected ']' at index {i} in {path!r}")

        # A key segment: everything up to the next '.', '[', or ']'.
        start = i
        while i < n and path[i] not in ".[]":
            i += 1
        segment = path[start:i]
        if segment == "":
            raise ExtractionError(f"empty path segment at index {start} in {path!r}")
        tokens.append(segment)
        expect_segment_start = False

    if expect_segment_start:
        # Path ended right after a '.' (e.g. "a.")
        raise ExtractionError(f"extraction path {path!r} ends with a dangling '.'")
    if not tokens:
        raise ExtractionError(f"no path segments found in {path!r}")
    return tokens


def extract(data: Any, path: str) -> Any:
    """Resolve ``path`` against ``data`` and return the value found."""
    tokens = parse_path(path)
    current = data
    for token in tokens:
        if isinstance(token, int):
            if not isinstance(current, list):
                raise ExtractionError(
                    f"expected a list at index [{token}], got {type(current).__name__}"
                )
            if token < 0 or token >= len(current):
                raise ExtractionError(f"index [{token}] out of range")
            current = current[token]
        else:
            if not isinstance(current, dict):
                raise ExtractionError(
                    f"expected an object at key {token!r}, got {type(current).__name__}"
                )
            if token not in current:
                raise ExtractionError(f"key {token!r} not found")
            current = current[token]
    return current


def extract_number(data: Any, path: str) -> float:
    """Resolve ``path`` and coerce the result to a float.

    Accepts int/float directly, and numeric strings (some APIs quote numbers).
    Rejects bool explicitly (``bool`` is a ``int`` subclass in Python and would
    otherwise silently coerce ``True``/``False`` to 1.0/0.0).
    """
    value = extract(data, path)
    if isinstance(value, bool):
        raise ExtractionError(f"value at {path!r} is a boolean, not a number")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise ExtractionError(
                f"value at {path!r} ({value!r}) is not a numeric string"
            ) from exc
    raise ExtractionError(
        f"value at {path!r} is a {type(value).__name__}, not a number"
    )
