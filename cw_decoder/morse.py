"""ITU International Morse Code table including letters, digits, punctuation, and prosigns."""

from __future__ import annotations

import re

# Standard ITU Morse code mapping (character -> dots/dashes)
CHAR_TO_MORSE: dict[str, str] = {
    # Letters A-Z
    "A": ".-",
    "B": "-...",
    "C": "-.-.",
    "D": "-..",
    "E": ".",
    "F": "..-.",
    "G": "--.",
    "H": "....",
    "I": "..",
    "J": ".---",
    "K": "-.-",
    "L": ".-..",
    "M": "--",
    "N": "-.",
    "O": "---",
    "P": ".--.",
    "Q": "--.-",
    "R": ".-.",
    "S": "...",
    "T": "-",
    "U": "..-",
    "V": "...-",
    "W": ".--",
    "X": "-..-",
    "Y": "-.--",
    "Z": "--..",
    # Digits 0-9
    "0": "-----",
    "1": ".----",
    "2": "..---",
    "3": "...--",
    "4": "....-",
    "5": ".....",
    "6": "-....",
    "7": "--...",
    "8": "---..",
    "9": "----.",
    # Punctuation
    ".": ".-.-.-",
    ",": "--..--",
    "?": "..--..",
    "'": ".----.",
    "!": "-.-.--",
    "/": "-..-.",
    "(": "-.--.",
    ")": "-.--.-",
    "&": ".-...",
    ":": "---...",
    ";": "-.-.-.",
    "=": "-...-",
    "+": ".-.-.",
    "-": "-....-",
    "_": "..--.-",
    '"': ".-..-.",
    "$": "...-..-",
    "@": ".--.-.",
}

# ITU Procedural Signals (Prosigns) written in angle brackets
PROSIGN_TO_MORSE: dict[str, str] = {
    "<SOS>": "...---...",
    "<AR>": ".-.-.",     # End of message (same as +)
    "<AS>": ".-...",     # Wait (same as &)
    "<BK>": "-...-.-",   # Break
    "<BT>": "-...-",     # Pause / New paragraph (same as =)
    "<CT>": "-.-.-",     # Starting signal (KA)
    "<KA>": "-.-.-",     # Attention / Starting signal
    "<KN>": "-.--.",     # Invite specific station to transmit (same as ()
    "<SK>": "...-.-",    # End of contact
    "<SN>": "...-.",     # Understood (VE)
    "<VE>": "...-.",     # Understood
    "<HH>": "........",  # Error / Correction
}

# Reverse lookup: Morse sequence -> character / prosign
MORSE_TO_CHAR: dict[str, str] = {code: ch for ch, code in CHAR_TO_MORSE.items()}

# Add unique prosigns that don't collide with standard punctuation
for _prosign, _code in PROSIGN_TO_MORSE.items():
    if _code not in MORSE_TO_CHAR:
        MORSE_TO_CHAR[_code] = _prosign

# Full encoding table (includes prosigns)
ENCODE_TABLE: dict[str, str] = {**CHAR_TO_MORSE, **PROSIGN_TO_MORSE}

_PROSIGN_TOKEN_RE = re.compile(r"<[A-Za-z]{2,3}>")


def encode_morse(text: str) -> str:
    """Encode plain text (including optional <PROSIGN> tokens) to Morse code string.

    Letters are separated by single spaces; words are separated by ' / '.
    """
    if not text or not text.strip():
        return ""

    words = text.strip().upper().split()
    encoded_words: list[str] = []

    for word in words:
        symbols: list[str] = []
        idx = 0
        while idx < len(word):
            match = _PROSIGN_TOKEN_RE.match(word, idx)
            if match:
                token = match.group(0)
                if token in PROSIGN_TO_MORSE:
                    symbols.append(PROSIGN_TO_MORSE[token])
                    idx = match.end()
                    continue
            ch = word[idx]
            if ch in CHAR_TO_MORSE:
                symbols.append(CHAR_TO_MORSE[ch])
            idx += 1
        if symbols:
            encoded_words.append(" ".join(symbols))

    return " / ".join(encoded_words)


def decode_morse(morse: str, unknown_char: str = "?", use_prosigns: bool = True) -> str:
    """Decode a Morse code string into text.

    Accepts '.', '-', '·', '•', '–', '—', '_' as dots/dashes.
    Word separators: '/', '|', or 3+ spaces.
    Character separators: 1-2 spaces.
    """
    if not morse or not morse.strip():
        return ""

    # Normalize Unicode dot/dash variants
    normalized = (
        morse.replace("·", ".")
        .replace("•", ".")
        .replace("∙", ".")
        .replace("−", "-")
        .replace("‒", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace("_", "-")
        .replace("|", "/")
    )

    # Convert runs of 3+ spaces into explicit word separator '/'
    normalized = re.sub(r"\s{3,}", " / ", normalized.strip())
    # Ensure '/' has spaces around it
    normalized = re.sub(r"\s*/\s*", " / ", normalized)

    words = normalized.split(" / ")
    decoded_words: list[str] = []

    # Optional prosign preference for common CW prosigns
    prosign_overrides = {
        "...---...": "<SOS>",
        "...-.-": "<SK>",
        "-.-.-": "<KA>",
        "-...-.-": "<BK>",
        "...-.": "<SN>",
        "........": "<HH>",
    }

    for word in words:
        tokens = [t for t in word.strip().split() if t]
        if not tokens:
            continue
        chars: list[str] = []
        for token in tokens:
            if use_prosigns and token in prosign_overrides:
                chars.append(prosign_overrides[token])
            elif token in MORSE_TO_CHAR:
                chars.append(MORSE_TO_CHAR[token])
            else:
                chars.append(unknown_char)
        decoded_words.append("".join(chars))

    return " ".join(decoded_words)
