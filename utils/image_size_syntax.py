"""Distinguish compact panel layouts from pixel dimensions in image prompts."""

import re


_PIXEL_PREFIX = re.compile(
    r"(?:像素|分辨率|尺寸|\bpixels?|\bresolution|\bsize)"
    r"\s*(?:设为|设置为|应为|为|是|of\b|is\b)?\s*[:：=]?\s*[(（]?\s*$",
    re.IGNORECASE,
)
_PIXEL_SUFFIX = re.compile(
    r"\s*[)）]?\s*(?:像素|px(?![a-z])|pixels?(?![a-z]))", re.IGNORECASE
)


def is_panel_grid(text: str, match: re.Match[str]) -> bool:
    """Treat bare, small NxM counts as panels; explicit pixel wording wins.

    A compact grid has 1–16 rows/columns. Larger values keep the existing
    resolution interpretation. This only classifies size tokens: the original
    prompt, including all layout instructions, is left intact for the model.
    """
    # Avoid parsing arbitrarily large integers before the resolver's guards.
    if any(len(value) > 2 for value in match.groups()):
        return False
    if not all(1 <= int(value) <= 16 for value in match.groups()):
        return False
    return not (
        _PIXEL_PREFIX.search(text[:match.start()])
        or _PIXEL_SUFFIX.match(text[match.end():])
    )
