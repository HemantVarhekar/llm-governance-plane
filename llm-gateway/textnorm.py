"""Text normalisation for pattern matching.

WHY THIS EXISTS - measured, from a 1000-attack red team run against the
guards WITHOUT it:

    zero-width characters   62% of attacks got through
    leetspeak               50%
    homoglyphs (Cyrillic)   40%
    spaced-out letters      38%

A guard that only recognises the textbook spelling recognises nothing. Real
attackers do not type "ignore all previous instructions" - they type
"ignore␋all␋previous..." with invisible characters, or "1gn0r3", or use a
Cyrillic "о" that is pixel-identical to a Latin one.

TWO normalisations, deliberately, because the two jobs conflict:

    for_words()       aggressive. Undoes leetspeak. Used for INJECTION
                      patterns, which match words.
    for_identifiers() conservative. Never touches digits. Used for PII
                      patterns, because "undoing leetspeak" on an account
                      number would corrupt the very thing we are looking for
                      (3 -> e would turn 8841-22719 into nonsense).

NOTHING HERE CHANGES WHAT THE MODEL RECEIVES. Normalisation is applied only
to the copy used for matching; the customer's original text is passed through
untouched. Rewriting a customer's message to make matching easier would be a
different and much worse bug.
"""

from __future__ import annotations

import re
import unicodedata

# Zero-width and direction-control characters. Invisible on screen, and each
# one breaks a naive regex.
_INVISIBLE = dict.fromkeys(
    [
        0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF,          # zero width
        0x200E, 0x200F, 0x202A, 0x202B, 0x202C,          # direction marks
        0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069,
        0x00AD,                                           # soft hyphen
    ]
)

# Cyrillic and Greek letters that render identically to Latin ones.
_HOMOGLYPHS = str.maketrans({
    "а": "a", "А": "A", "е": "e", "Е": "E",
    "о": "o", "О": "O", "с": "c", "С": "C",
    "р": "p", "Р": "P", "у": "y", "У": "Y",
    "х": "x", "Х": "X", "і": "i", "І": "I",
    "ј": "j", "Ѕ": "S", "н": "h", "Н": "H",
    "к": "k", "К": "K", "м": "m", "М": "M",
    "т": "t", "Т": "T", "в": "b", "В": "B",
    "ο": "o", "Ο": "O", "α": "a", "Α": "A",
    "ε": "e", "Ε": "E", "ρ": "p", "Ρ": "P",
    "υ": "u", "Β": "B", "Κ": "K", "Μ": "M",
})

_LEET = str.maketrans({"3": "e", "1": "i", "0": "o", "@": "a", "$": "s",
                       "!": "i", "5": "s", "7": "t", "4": "a"})

# A run of single characters separated by spaces: "i g n o r e".
_SPACED_RUN = re.compile(r"(?<![^\W\d_])((?:[A-Za-z]\s){2,}[A-Za-z])(?![^\W\d_])")
# The same for digits: "8 8 4 1".
_SPACED_DIGITS = re.compile(r"(?<!\d)((?:\d[ \t]){2,}\d)(?!\d)")


# Digits written as words. The red team's single most effective PII evasion:
# "eight eight four one dash two two seven one nine" got through 100% of the
# time, because no pattern reads English as numbers.
_WORD_DIGIT = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "dash": "-", "hyphen": "-",
}
# Only convert a RUN of at least three, so ordinary prose survives: "one of
# the four channels" must not become "1 of the 4 channels", and more
# importantly "eleven years" must not be touched at all.
_WORD_RUN = re.compile(
    r"\b(?:(?:" + "|".join(_WORD_DIGIT) + r")[\s,]+){2,}"
    r"(?:" + "|".join(_WORD_DIGIT) + r")\b",
    re.IGNORECASE,
)


def _spell_out_digits(text: str) -> str:
    def convert(match: re.Match[str]) -> str:
        words = re.split(r"[\s,]+", match.group(0).strip())
        return "".join(_WORD_DIGIT.get(w.lower(), w) for w in words)

    return _WORD_RUN.sub(convert, text)


def _base(text: str) -> str:
    """Steps that are always safe: strip invisibles, fold lookalikes, NFKC."""
    if not text:
        return ""
    text = text.translate(_INVISIBLE)
    text = text.translate(_HOMOGLYPHS)
    # NFKC turns full-width and other compatibility forms into plain ASCII.
    text = unicodedata.normalize("NFKC", text)
    # Newlines break every pattern that uses [^.!?\n] to stay inside one
    # sentence. The red team exploited exactly that: splitting a phrase
    # across a line defeated six separate attacks. Fold line breaks to
    # spaces on the MATCHING COPY so a sentence cannot be hidden by
    # pressing Enter in the middle of it.
    return re.sub(r"[\r\n]+", " ", text)


def _collapse_spaced_letters(text: str) -> str:
    return _SPACED_RUN.sub(lambda m: m.group(1).replace(" ", ""), text)


def _collapse_spaced_digits(text: str) -> str:
    return _SPACED_DIGITS.sub(lambda m: re.sub(r"[ \t]", "", m.group(1)), text)


def for_words(text: str) -> str:
    """Aggressive. For matching WORDS - injection phrases, keywords.

    Undoes leetspeak, which is safe here because we are not looking at
    numbers. Collapses letters spread out with spaces.
    """
    out = _base(text)
    out = _collapse_spaced_letters(out)
    out = out.translate(_LEET)
    # a second pass: leet substitution can expose a newly-spaced run
    out = _collapse_spaced_letters(out)
    return out


def for_identifiers(text: str) -> str:
    """Conservative. For matching IDENTIFIERS - account numbers, phones.

    Deliberately does NOT undo leetspeak: turning 3 into e would destroy the
    digits we are trying to find. Joins digits that have been spread out,
    and treats a space between digit groups as a separator so that
    "8841 22719" is seen the same as "8841-22719".
    """
    out = _base(text)
    out = _spell_out_digits(out)
    out = _collapse_spaced_digits(out)
    # "(457) 55 5462" -> "457-55-5462". Found by the red team: a
    # parenthesised social security number defeated the pattern 95% of the
    # time regardless of what else was done to it.
    out = re.sub(r"\((\d{3})\)[ \t]*(\d{2})[ \t]*(\d{4})(?!\d)", r"\1-\2-\3", out)
    out = re.sub(r"\((\d{3})\)[ \t]*(\d{3})[ \t]*(\d{4})(?!\d)", r"\1-\2-\3", out)
    # Collapsing "8 8 4 1 - 2 2 7 1 9" leaves "8841 - 22719"; close the gaps
    # around a separator that sits between digits.
    out = re.sub(r"(?<=\d)\s*([-.])\s*(?=\d)", r"\1", out)
    # "8841 22719" -> "8841-22719" so one pattern covers both spellings.
    out = re.sub(r"(?<!\d)(\d{4})[ \t]+(\d{5,})(?!\d)", r"\1-\2", out)
    # "214 555 0183" -> "214-555-0183"
    out = re.sub(r"(?<!\d)(\d{3})[ \t]+(\d{3})[ \t]+(\d{4})(?!\d)", r"\1-\2-\3", out)
    return out


def both(text: str) -> tuple[str, str]:
    """Convenience: (word-normalised, identifier-normalised)."""
    return for_words(text), for_identifiers(text)
