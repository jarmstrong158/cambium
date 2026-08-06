"""cp1252 mojibake detection and repair. The ONE copy on the cambium side.

There are two copies of this rule in the world -- this one and
context-keeper's mojibake.py -- and that is a deliberate, bounded cost: cambium
must run when the sibling checkout is absent or mid-refactor, which is exactly
when you want to inspect the stores. Two is the floor. Three was not.

Three is what there briefly was. tools/dashboard.py and tools/repair_mojibake.py
each carried their own list, and they disagreed: the repair script was missing
the maths marker, so a field reading "set to <mojibake>1e9 logits" was offered
to nobody. looks_like_mojibake GATES demojibake, so a marker a list lacks is a
field the repair never attempts -- context-keeper's con-016-16be, reproduced
inside cambium within hours of being written down. Both tools now import here.

Kept in step with context-keeper's list by hand. When that one changes, change
this one: the marker set is the coverage, and coverage is the whole product.
"""

# Two-and-three character sequences that only arise from reading UTF-8 as
# cp1252. Each begins with the mis-decoded lead byte of a multi-byte UTF-8
# sequence, which is why a bare em-dash or a bare multiplication sign is NOT
# here -- those are legitimate characters and matching them would flag correct
# text as damaged. "â€" is the 2-char prefix of the whole smart-quote
# and dash family, so the longer variants of it need no separate entry.
MARKERS = (
    "â€",        # em/en-dash and smart-quote family
    "Ã©",        # e-acute
    "Ã¨",        # e-grave
    "Ã¼",        # u-umlaut
    "Ã±",        # n-tilde
    "Ã ",        # a-grave
    "Ã´",        # o-circumflex
    "Ã¶",        # o-umlaut
    "Ã¤",        # a-umlaut
    "Ã—",        # multiplication sign
    "Ã·",        # division sign
    "â„",        # trademark / numero
    "âˆ",        # maths (minus, product, element-of)
    "â†’",  # rightwards arrow
    "Â ",        # non-breaking space
    "Â·",        # middle dot
    "Â«",        # guillemets
    "Â»",
    "Â°",        # degree
    "Â±",        # plus-minus
)


def looks_like_mojibake(text):
    return isinstance(text, str) and any(m in text for m in MARKERS)


def demojibake(text):
    """Repaired text, or None when this is not recoverable cp1252 damage.

    Verified as an exact inverse: re-applying the corruption to the candidate
    must reproduce the input byte for byte, or the field is left alone. An
    approximate repair of recorded reasoning is worse than legible damage,
    because it looks correct.
    """
    if not looks_like_mojibake(text):
        return None
    try:
        repaired = text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    if repaired == text:
        return None
    try:
        if repaired.encode("utf-8").decode("cp1252") != text:
            return None
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    return repaired


def ascii_safe(text):
    """Console-safe. Windows stdout is cp1252, and a SUCCESSFUL repair very
    often produces exactly the characters cp1252 cannot encode -- an arrow, an
    em-dash, a curly quote. Printing the fix would otherwise crash on the same
    encoding boundary the fix exists to heal."""
    return str(text).encode("ascii", "replace").decode("ascii")
