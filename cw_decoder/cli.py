"""Command-Line Interface (CLI) for CW Decoder."""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence

from . import __version__
from .audio_io import AudioLoadError, find_ffmpeg, write_wav
from .decoder import CWDecoder, DecodeOptions, DecodeResult
from .encoder import CWEncoder, EncodeOptions
from .morse import decode_morse, encode_morse

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    _HAS_RICH = True
except Exception:
    _HAS_RICH = False


def _get_console() -> "Console | None":
    return Console() if _HAS_RICH else None


def _print_decode_result(result: DecodeResult, source_label: str, as_json: bool = False) -> None:
    if as_json:
        payload = {"source": source_label, **result.to_dict()}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    console = _get_console()
    if result.is_empty:
        msg = (
            f"Tiada isyarat CW/Morse yang jelas dikesan daripada: {source_label}\n"
            f"(Durasi: {result.duration_sec:.2f}s | SNR: {result.snr_db:.1f} dB | Nada: {result.tone_hz:.1f} Hz)"
        )
        if console:
            console.print(Panel(msg, title="CW Decoder - Tiada Isyarat", border_style="yellow"))
        else:
            print(msg)
        return

    if console:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Field", style="bold cyan")
        table.add_column("Value")
        table.add_row("Sumber", source_label)
        table.add_row("Teks", f"[bold green]{result.text}[/bold green]")
        table.add_row("Kod Morse", f"[yellow]{result.morse}[/yellow]")
        table.add_row("Kelajuan", f"{result.wpm:.1f} WPM (dot {result.dot_ms:.1f} ms)")
        table.add_row("Frekuensi", f"{result.tone_hz:.1f} Hz")
        table.add_row("SNR / Keyakinan", f"{result.snr_db:.1f} dB / {int(result.confidence * 100)}%")
        table.add_row("Durasi", f"{result.duration_sec:.2f} s")
        console.print(Panel(table, title=f"CW Decoder v{__version__}", border_style="green"))
    else:
        print(f"=== CW Decoder v{__version__} ===")
        print(f"Sumber    : {source_label}")
        print(f"Teks      : {result.text}")
        print(f"Kod Morse : {result.morse}")
        print(f"Kelajuan  : {result.wpm:.1f} WPM (dot {result.dot_ms:.1f} ms)")
        print(f"Frekuensi : {result.tone_hz:.1f} Hz")
        print(f"SNR       : {result.snr_db:.1f} dB | Keyakinan: {int(result.confidence * 100)}%")
        print(f"Durasi    : {result.duration_sec:.2f} s")


def _record_microphone(output_wav: Path, duration: float, sample_rate: int = 16000) -> str:
    """Record audio from microphone into output_wav.

    Tries sounddevice -> pyaudio -> Windows built-in winmm MCI fallback.
    Returns the backend name used.
    """
    # 1. Try sounddevice
    try:
        import numpy as np
        import sounddevice as sd  # type: ignore

        n_samples = int(round(duration * sample_rate))
        audio = sd.rec(n_samples, samplerate=sample_rate, channels=1, dtype="float32")
        sd.wait()
        write_wav(output_wav, np.asarray(audio).reshape(-1), sample_rate=sample_rate)
        return "sounddevice"
    except ImportError:
        pass

    # 2. Try pyaudio
    try:
        import wave
        import pyaudio  # type: ignore

        pa = pyaudio.PyAudio()
        chunk = 1024
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            frames_per_buffer=chunk,
        )
        frames: list[bytes] = []
        n_chunks = max(1, int(sample_rate / chunk * duration))
        try:
            for _ in range(n_chunks):
                frames.append(stream.read(chunk, exception_on_overflow=False))
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()

        output_wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(b"".join(frames))
        return "pyaudio"
    except ImportError:
        pass

    # 3. Built-in Windows MCI fallback (zero external dependencies)
    if sys.platform == "win32":
        winmm = ctypes.windll.winmm
        alias = f"cwrec_{int(time.time() * 1000) % 100000}"

        def _mci(cmd: str) -> None:
            buf = ctypes.create_unicode_buffer(256)
            err = winmm.mciSendStringW(cmd, buf, 256, None)
            if err != 0:
                err_buf = ctypes.create_unicode_buffer(256)
                winmm.mciGetErrorStringW(err, err_buf, 256)
                raise RuntimeError(f"Ralat mikrofon Windows MCI: {err_buf.value or err}")

        output_wav.parent.mkdir(parents=True, exist_ok=True)
        _mci(f"open new type waveaudio alias {alias}")
        try:
            _mci(
                f"set {alias} time format ms bitspersample 16 channels 1 "
                f"samplespersec {sample_rate} bytespersec {sample_rate * 2} alignment 2"
            )
            _mci(f"record {alias}")
            try:
                time.sleep(duration)
            except KeyboardInterrupt:
                pass
            _mci(f"stop {alias}")
            _mci(f'save {alias} "{output_wav.resolve()}"')
        finally:
            try:
                winmm.mciSendStringW(f"close {alias}", None, 0, None)
            except Exception:
                pass
        return "winmm"

    raise RuntimeError(
        "Tiada pustaka mikrofon ditemui. Sila pasang `pip install sounddevice` atau `pip install pyaudio`."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cw-decoder",
        description="CW Decoder - Nyahkod & kod bunyi Morse (Continuous Wave) melalui CLI.",
    )
    parser.add_argument("--version", action="version", version=f"CW Decoder {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    # decode
    p_dec = sub.add_parser("decode", help="Nyahkod fail audio CW (WAV/OGG/MP3/M4A) kepada perkataan")
    p_dec.add_argument("file", help="Laluan ke fail audio CW")
    p_dec.add_argument("--freq", type=float, default=None, help="Frekuensi nada CW (Hz). Auto jika kosong")
    p_dec.add_argument("--wpm", type=float, default=None, help="Kelajuan CW (WPM). Auto jika kosong")
    p_dec.add_argument("--bandwidth", type=float, default=120.0, help="Lebar jalur penapis (Hz, default: 120)")
    p_dec.add_argument("--no-prosigns", action="store_true", help="Nyahaktifkan terjemahan prosign (<SK>, <SOS>, dll)")
    p_dec.add_argument("--json", action="store_true", dest="as_json", help="Paparkan output dalam format JSON")

    # encode
    p_enc = sub.add_parser("encode", help="Jana fail audio CW (.wav) daripada teks untuk ujian")
    p_enc.add_argument("text", nargs="+", help="Teks untuk ditukar kepada bunyi CW")
    p_enc.add_argument("-o", "--output", default="cw_output.wav", help="Fail WAV output (default: cw_output.wav)")
    p_enc.add_argument("--wpm", type=float, default=20.0, help="Kelajuan WPM (default: 20)")
    p_enc.add_argument("--farnsworth", type=float, default=None, help="Kelajuan Farnsworth WPM (pilihan)")
    p_enc.add_argument("--freq", type=float, default=700.0, help="Frekuensi nada Hz (default: 700)")
    p_enc.add_argument("--sample-rate", type=int, default=16000, help="Kadar sampel Hz (default: 16000)")
    p_enc.add_argument("--noise", type=float, default=0.0, help="Tahap hingar ujian 0.0-0.5 (default: 0.0)")

    # listen (mic)
    p_mic = sub.add_parser("listen", help="Rakam bunyi CW terus daripada mikrofon dan nyahkod")
    p_mic.add_argument("-d", "--duration", type=float, default=8.0, help="Tempoh rakaman dalam saat (default: 8)")
    p_mic.add_argument("--sample-rate", type=int, default=16000, help="Kadar sampel Hz (default: 16000)")
    p_mic.add_argument("--save", default=None, help="Simpan rakaman WAV ke laluan ini (pilihan)")
    p_mic.add_argument("--freq", type=float, default=None, help="Frekuensi nada CW (Hz). Auto jika kosong")
    p_mic.add_argument("--wpm", type=float, default=None, help="Kelajuan CW (WPM). Auto jika kosong")
    p_mic.add_argument("--json", action="store_true", dest="as_json", help="Paparkan output dalam format JSON")

    # morse (text <-> morse)
    p_morse = sub.add_parser("morse", help="Tukar kod Morse teks <-> perkataan tanpa audio")
    p_morse.add_argument("content", nargs="+", help="Kod Morse (cth: '.... ..') atau teks jika guna --encode")
    p_morse.add_argument("--encode", action="store_true", help="Tukar teks kepada kod Morse")

    # doctor (check environment)
    sub.add_parser("doctor", help="Semak status persekitaran (FFmpeg, mikrofon, versi)")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = _get_console()

    if args.command == "decode":
        opts = DecodeOptions(
            freq_hz=args.freq,
            wpm=args.wpm,
            bandwidth_hz=args.bandwidth,
            use_prosigns=not args.no_prosigns,
        )
        decoder = CWDecoder(options=opts)
        try:
            result = decoder.decode_file(args.file)
        except AudioLoadError as exc:
            print(f"Ralat audio: {exc}", file=sys.stderr)
            return 1
        _print_decode_result(result, source_label=str(args.file), as_json=args.as_json)
        return 0

    if args.command == "encode":
        text = " ".join(args.text)
        opts = EncodeOptions(
            wpm=args.wpm,
            farnsworth_wpm=args.farnsworth,
            freq_hz=args.freq,
            sample_rate=args.sample_rate,
            noise_level=args.noise,
        )
        encoder = CWEncoder(options=opts)
        saved_path, morse = encoder.encode_text_to_wav(text, args.output)
        msg = (
            f"Berjaya dijana: {saved_path}\n"
            f"Teks      : {text.upper()}\n"
            f"Kod Morse : {morse}\n"
            f"Tetapan   : {args.wpm:.1f} WPM @ {args.freq:.0f} Hz"
        )
        if console:
            console.print(Panel(msg, title="CW Encoder", border_style="cyan"))
        else:
            print(msg)
        return 0

    if args.command == "listen":
        tmp_path: Path | None = None
        if args.save:
            wav_path = Path(args.save)
        else:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            tmp_path = Path(tmp.name)
            wav_path = tmp_path

        try:
            print(f"Merakam daripada mikrofon selama {args.duration:.1f} saat... (Tekan Ctrl+C untuk berhenti awal)")
            backend = _record_microphone(wav_path, duration=args.duration, sample_rate=args.sample_rate)
            print(f"Rakaman selesai ({backend}). Sedang menyahkod...")
            opts = DecodeOptions(freq_hz=args.freq, wpm=args.wpm)
            result = CWDecoder(options=opts).decode_file(wav_path)
            _print_decode_result(result, source_label=f"Mikrofon ({backend})", as_json=args.as_json)
            return 0
        except Exception as exc:
            print(f"Ralat rakaman/nyahkod: {exc}", file=sys.stderr)
            return 1
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    if args.command == "morse":
        raw = " ".join(args.content)
        if args.encode:
            print(encode_morse(raw))
        else:
            print(decode_morse(raw))
        return 0

    if args.command == "doctor":
        ffmpeg_bin = find_ffmpeg()
        print(f"CW Decoder v{__version__}")
        print(f"Python  : {sys.version.split()[0]}")
        print(f"FFmpeg  : {ffmpeg_bin or 'TIADA (WAV sahaja disokong; pasang FFmpeg atau imageio-ffmpeg untuk OGG/MP3/M4A)'}")
        mic_backends = []
        try:
            import sounddevice  # type: ignore  # noqa: F401
            mic_backends.append("sounddevice")
        except ImportError:
            pass
        try:
            import pyaudio  # type: ignore  # noqa: F401
            mic_backends.append("pyaudio")
        except ImportError:
            pass
        if sys.platform == "win32":
            mic_backends.append("winmm (Windows built-in)")
        print(f"Mikrofon: {', '.join(mic_backends) if mic_backends else 'Tiada backend'}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
