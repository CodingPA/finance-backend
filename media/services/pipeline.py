"""Orchestrates one MediaFile end to end: audio extraction, first-minute
speaker identification + reference-clip extraction, concurrent 60s-chunk
diarization with those speakers pinned, merging everything into one
absolute timeline, and grouping into ~1000-token same-speaker chunks ready
to become MediaSegment rows. Kept out of tasks.py so the Celery task stays
a thin "call this, save the result" wrapper -- same split as
companies/services/filing_ingestion.py vs. its Celery task.

The whole point of the known_speaker_names/known_speaker_references dance
is real: without it, gpt-4o-transcribe-diarize labels speakers A/B/C/D
independently *per call*, so "Speaker A" in chunk 1 has no relationship to
"Speaker A" in chunk 5 -- see docs comments in diarization.py for the
verified source. Pinning against reference clips from the first minute is
the documented fix."""

import base64
import io
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import tiktoken
from django.conf import settings

from . import ffmpeg
from .diarization import MAX_KNOWN_SPEAKERS, DiarizedSegment, diarize

FIRST_PASS_SECONDS = 60.0
CHUNK_SECONDS = 60.0
REFERENCE_CLIP_SECONDS = 9.0  # under the API's 10s cap, comfortably above its 2s floor
MAX_SEGMENT_TOKENS = 1000  # matches the Docling filing-chunk budget elsewhere in this app

ENCODING = tiktoken.get_encoding("cl100k_base")


@dataclass
class MergedSegment:
    speaker: str
    start: float  # absolute, seconds from the start of the full audio
    end: float
    text: str


def _to_data_url(path: str) -> str:
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:audio/mpeg;base64,{encoded}"


def _pick_reference_clips(audio_path: str, first_pass_segments: list[DiarizedSegment], first_pass_duration: float, tmp_dir: str):
    """Up to MAX_KNOWN_SPEAKERS (name, data-url reference clip) pairs, one
    per speaker who talked the most in the first minute. Reference window
    is a wall-clock slice starting at that speaker's single longest turn --
    not a stitched-together concatenation of their scattered lines, which
    keeps the extraction a single ffmpeg cut instead of a multi-splice."""
    by_speaker: dict[str, list[DiarizedSegment]] = {}
    for seg in first_pass_segments:
        by_speaker.setdefault(seg.speaker, []).append(seg)

    # Rank speakers by total speaking time -- if there are more than
    # MAX_KNOWN_SPEAKERS distinct speakers in the first minute, only the
    # busiest ones get pinned; the rest fall back to unpinned per-chunk
    # labels later on (a real, documented limitation, not a bug).
    ranked = sorted(by_speaker.items(), key=lambda kv: sum(s.end - s.start for s in kv[1]), reverse=True)
    ranked = ranked[:MAX_KNOWN_SPEAKERS]

    names, references = [], []
    for speaker, segments in ranked:
        longest = max(segments, key=lambda s: s.end - s.start)
        clip_end = min(longest.start + REFERENCE_CLIP_SECONDS, first_pass_duration)
        clip_path = os.path.join(tmp_dir, f"ref_{speaker}.mp3")
        ffmpeg.extract_clip(audio_path, clip_path, longest.start, clip_end)
        names.append(speaker)
        references.append(_to_data_url(clip_path))

    return names, references


def _diarize_chunk(audio_path: str, chunk_index: int, tmp_dir: str, known_names, known_refs, chunk_start: float, chunk_end: float) -> list[MergedSegment]:
    clip_path = os.path.join(tmp_dir, f"chunk_{chunk_index}.mp3")
    ffmpeg.extract_clip(audio_path, clip_path, chunk_start, chunk_end)
    segments = diarize(clip_path, known_speaker_names=known_names, known_speaker_references=known_refs)
    return [MergedSegment(speaker=s.speaker, start=s.start + chunk_start, end=s.end + chunk_start, text=s.text) for s in segments]


def transcribe(audio_path: str) -> list[MergedSegment]:
    """Full pipeline for one already-audio-only file. Returns merged
    segments in chronological order, absolute timestamps, consistent
    speaker labels throughout."""
    duration = ffmpeg.get_duration(audio_path)

    with tempfile.TemporaryDirectory(prefix="media_pipeline_") as tmp_dir:
        if duration <= FIRST_PASS_SECONDS:
            # Short enough to diarize in one call -- no cross-chunk
            # consistency problem to solve, so skip the reference-pinning
            # machinery entirely.
            segments = diarize(audio_path)
            return [MergedSegment(speaker=s.speaker, start=s.start, end=s.end, text=s.text) for s in segments]

        first_pass_path = os.path.join(tmp_dir, "first_pass.mp3")
        ffmpeg.extract_clip(audio_path, first_pass_path, 0, FIRST_PASS_SECONDS)
        first_pass_segments = diarize(first_pass_path)

        merged = [
            MergedSegment(speaker=s.speaker, start=s.start, end=s.end, text=s.text) for s in first_pass_segments
        ]

        known_names, known_refs = _pick_reference_clips(first_pass_path, first_pass_segments, FIRST_PASS_SECONDS, tmp_dir)

        chunk_bounds = []
        pos = FIRST_PASS_SECONDS
        while pos < duration:
            chunk_bounds.append((pos, min(pos + CHUNK_SECONDS, duration)))
            pos += CHUNK_SECONDS

        concurrency = settings.ASSISTANT_MEDIA_CHUNK_CONCURRENCY
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [
                pool.submit(_diarize_chunk, audio_path, i, tmp_dir, known_names, known_refs, start, end)
                for i, (start, end) in enumerate(chunk_bounds)
            ]
            for future in futures:
                merged.extend(future.result())

        merged.sort(key=lambda s: s.start)
        return merged


def group_into_segments(merged: list[MergedSegment]) -> list[MergedSegment]:
    """Collapse consecutive same-speaker MergedSegments into ~1000-token
    chunks -- one per resulting MediaSegment row. Never merges across a
    speaker change, since that would lose the "who said this" attribution
    that's the entire point of diarizing."""
    if not merged:
        return []

    grouped: list[MergedSegment] = []
    buf_speaker = merged[0].speaker
    buf_start = merged[0].start
    buf_end = merged[0].end
    buf_texts = [merged[0].text]
    buf_tokens = len(ENCODING.encode(merged[0].text))

    def flush():
        grouped.append(MergedSegment(speaker=buf_speaker, start=buf_start, end=buf_end, text=" ".join(buf_texts)))

    for seg in merged[1:]:
        seg_tokens = len(ENCODING.encode(seg.text))
        if seg.speaker != buf_speaker or buf_tokens + seg_tokens > MAX_SEGMENT_TOKENS:
            flush()
            buf_speaker, buf_start, buf_texts, buf_tokens = seg.speaker, seg.start, [], 0
        buf_end = seg.end
        buf_texts.append(seg.text)
        buf_tokens += seg_tokens

    flush()
    return grouped
