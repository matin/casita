"""Classify a listing's dog policy from free-form text.

Returns one of: large_ok, dogs_ok, small_only, no_dogs, or None.

Priority order applied to the same blob — most-restrictive signal wins so a
post that says "dogs ok but small dogs only" lands in small_only, not dogs_ok.
"""
import re

from .models import DogPolicy

# Absolute no-dogs phrases — these are unambiguous, no negotiation possible.
# "Dogs are not allowed" (with no "large" qualifier) = no_dogs.
_NO_DOGS = re.compile(
    r"(?i)\b("
    r"no\s+dogs|no\s+pets|sorry\s*,?\s*no\s+pets|"
    r"dogs?\s*:\s*no|pet\s*policy\s*:\s*no\s+dogs|"
    r"(?<!large\s)(?<!big\s)dogs?\s+(?:are\s+)?not\s+allowed|"
    r"cats\s+only"
    r")\b"
)
# Size-restricted phrases.
_SMALL_ONLY = re.compile(
    r"(?i)\b("
    r"small\s+dogs?\s+only|small\s+pets?\s+only|"
    r"dogs?\s+under\s+\d+\s*(?:lb|lbs|pounds)|"
    r"under\s+\d+\s*(?:lb|lbs|pounds)\s+only|"
    r"weight\s+limit\s*:?\s*\d+\s*(?:lb|lbs|pounds)?|"
    r"no\s+large\s+dogs|"
    r"(?:large|big)\s+dogs?\s+(?:are\s+)?not\s+allowed"
    r")\b"
)
_LARGE_OK = re.compile(
    r"(?i)\b("
    r"large\s+dogs?\s+(ok|welcome|allowed|fine|considered)|"
    r"big\s+dogs?\s+(ok|welcome|allowed|fine|considered)|"
    r"no\s+(weight|size|breed)\s+(limit|restriction)|"
    r"any\s+size\s+dog|"
    r"dogs?\s+of\s+all\s+sizes"
    r")\b"
)
_DOGS_OK = re.compile(
    r"(?i)\b("
    r"dogs?\s+(ok|welcome|allowed|fine|considered)|"
    r"dog\s+friendly|pet\s+friendly|"
    r"pets?\s+(ok|welcome|allowed|considered)|"
    r"dogs?\s+are\s+ok"
    r")\b"
)


def classify(text: str | None, default: DogPolicy | None = None) -> DogPolicy | None:
    """Pick the strongest signal from the text.

    `default` lets callers express prior knowledge (e.g. Craigslist's
    `pets_dog=1` URL filter implies `dogs_ok` baseline before reading the body).
    """
    if not text:
        return default
    # Order: check small_only first (more specific — wins against the
    # _NO_DOGS pattern when "large dogs not allowed" appears), then no_dogs,
    # then the permissive policies.
    if _SMALL_ONLY.search(text):
        return "small_only"
    if _NO_DOGS.search(text):
        return "no_dogs"
    if _LARGE_OK.search(text):
        return "large_ok"
    if _DOGS_OK.search(text):
        return "dogs_ok"
    return default


LABELS: dict[DogPolicy, str] = {
    "large_ok": "Large dogs OK",
    "dogs_ok": "Dogs OK",
    "small_only": "Small dogs only",
    "no_dogs": "No dogs",
}
