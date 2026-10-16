"""Wraps OpenAI's gpt-4o-transcribe-diarize (GA, /v1/audio/transcriptions)
-- speaker-labeled transcription. `known_speaker_names[]` /
`known_speaker_references[]` are the real, documented mechanism for
pinning speaker labels across independent calls (confirmed against the
installed openai==2.46.0 SDK's own types before this was written, and
against OpenAI's docs -- not guessed): pass up to 4 (name, 2-10s reference
clip) pairs and the returned `speaker` field matches the names given,
instead of restarting at A/B/C/D every call."""

from dataclasses import dataclass

from django.conf import settings
from openai import OpenAI

MODEL = "gpt-4o-transcribe-diarize"
MAX_KNOWN_SPEAKERS = 4


@dataclass
class DiarizedSegment:
    speaker: str
    start: float
    end: float
    text: str


def diarize(
    audio_path: str, known_speaker_names: list[str] | None = None, known_speaker_references: list[str] | None = None
) -> list[DiarizedSegment]:
    """Transcribe one audio file/chunk with speaker labels. Local
    timestamps (relative to the start of `audio_path`) -- callers offset
    them when merging chunks back into one full-file timeline.

    known_speaker_references must be data URLs (`data:audio/mp3;base64,...`),
    each a 2-10 second clip, paired positionally with known_speaker_names --
    both are optional (omit both for the first, unpinned pass)."""
    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    kwargs = {}
    if known_speaker_names:
        kwargs["known_speaker_names"] = known_speaker_names
        kwargs["known_speaker_references"] = known_speaker_references

    with open(audio_path, "rb") as f:
        response = client.audio.transcriptions.create(
            model=MODEL,
            file=f,
            chunking_strategy="auto",  # required by this model for inputs over 30s
            response_format="diarized_json",
            **kwargs,
        )

    return [
        DiarizedSegment(speaker=s.speaker, start=s.start, end=s.end, text=s.text) for s in response.segments
    ]
