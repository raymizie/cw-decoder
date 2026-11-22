"""Tests for ITU Morse table and text <-> Morse conversion."""

from cw_decoder.morse import decode_morse, encode_morse


def test_alphabet_and_digits_roundtrip():
    text = "THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG 0123456789"
    morse = encode_morse(text)
    assert decode_morse(morse) == text


def test_punctuation_roundtrip():
    text = "CQ, CQ? TEST/1 = 9M2@CW.MY!"
    morse = encode_morse(text)
    assert decode_morse(morse) == text


def test_prosigns_encode_and_decode():
    text = "CQ DE 9M2CW <SK>"
    morse = encode_morse(text)
    assert "...-.-" in morse
    assert decode_morse(morse) == "CQ DE 9M2CW <SK>"

    sos_morse = encode_morse("<SOS>")
    assert sos_morse == "...---..."
    assert decode_morse(sos_morse) == "<SOS>"


def test_unicode_variants_and_spacing():
    assert decode_morse("···· · ·−·· ·−·· −−−   ·−− −−− ·−· ·−·· −··") == "HELLO WORLD"
    assert decode_morse("... --- ...") == "SOS"
    assert decode_morse(".... .. | - .... . .-. .") == "HI THERE"


def test_unknown_sequence():
    assert decode_morse(".... .. / ........-.-") == "HI ?"
    assert decode_morse(".... .. ........-.-") == "HI?"
