"""CW Decoder - Continuous Wave (Morse Code) Audio Decoder & Encoder."""

__version__ = "1.0.0"

from .decoder import CWDecoder, DecodeOptions, DecodeResult, decode_file, decode_samples
from .encoder import CWEncoder, EncodeOptions, encode_text_to_samples, encode_text_to_wav
from .morse import decode_morse, encode_morse

__all__ = [
    "__version__",
    "CWDecoder",
    "DecodeOptions",
    "DecodeResult",
    "decode_file",
    "decode_samples",
    "CWEncoder",
    "EncodeOptions",
    "encode_text_to_samples",
    "encode_text_to_wav",
    "decode_morse",
    "encode_morse",
]
