"""ffmpeg/ffprobe wrapping for the media pipeline -- audio extraction from
video, duration probing, and clip slicing (both the ~9s speaker reference
samples and the 60s transcription chunks).

ffmpeg/ffprobe are external binaries, not pip packages, and this app runs
in two environments with different expectations for where they live: a
real install on PATH in production, or (locally, on Windows, per the dev's
own setup) ffmpeg.exe/ffprobe.exe dropped next to manage.py rather than
fighting with PATH. Resolution order below covers both without needing to
special-case the platform in code -- PATH is checked first regardless of
OS, so a properly-installed server never touches the local-file fallback."""

import shutil
import subprocess

from django.conf import settings


def _find_binary(name: str, setting_name: str) -> str:
    override = getattr(settings, setting_name, "")
    if override:
        return override
    found = shutil.which(name)
    if found:
        return found
    local = settings.BASE_DIR / f"{name}.exe"
    if local.exists():
        return str(local)
    raise RuntimeError(
        f"{name} not found on PATH or as {local} -- install it, or set {setting_name} in .env."
    )


def find_ffmpeg() -> str:
    return _find_binary("ffmpeg", "FFMPEG_PATH")


def find_ffprobe() -> str:
    return _find_binary("ffprobe", "FFPROBE_PATH")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{args[0]} failed (exit {result.returncode}): {result.stderr[-2000:]}")
    return result


def get_duration(path: str) -> float:
    """Duration in seconds, via ffprobe (structured output, not parsing
    ffmpeg's human-readable stderr)."""
    result = _run([
        find_ffprobe(), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        path,
    ])
    return float(result.stdout.strip())


def extract_audio(input_path: str, output_path: str) -> None:
    """Audio-only track from a video file (or just re-encodes an already-
    audio file to a consistent format) -- discards video entirely, matches
    the ask: mp4 in, audio out, nothing else. Re-encodes rather than
    stream-copying since the source codec (typically AAC in an mp4) isn't
    guaranteed compatible with the mp3 container without transcoding."""
    _run([
        find_ffmpeg(), "-y",
        "-i", input_path,
        "-vn", "-acodec", "libmp3lame", "-q:a", "2",
        output_path,
    ])


def extract_clip(input_path: str, output_path: str, start: float, end: float) -> None:
    """One slice of audio, [start, end) in seconds. Seeks after -i (slower
    than seeking before it, but frame-accurate) -- both the ~9s reference
    samples and the 60s transcription chunks need real accuracy, not just
    a fast approximate cut."""
    _run([
        find_ffmpeg(), "-y",
        "-i", input_path,
        "-ss", str(start), "-to", str(end),
        "-acodec", "libmp3lame", "-q:a", "2",
        output_path,
    ])
