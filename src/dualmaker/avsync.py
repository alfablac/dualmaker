from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .defaults import (
    VIDEO_MATCH_FPS,
    VIDEO_MATCH_HEIGHT,
    VIDEO_MATCH_MAX_SPREAD_SECONDS,
    VIDEO_MATCH_MIN_SCORE,
    VIDEO_MATCH_REFERENCE_SECONDS,
    VIDEO_MATCH_SAMPLE_COUNT,
    VIDEO_MATCH_SEARCH_RADIUS_SECONDS,
    VIDEO_MATCH_WIDTH,
)
from .errors import ProcessingError
from .runner import ToolRunner


@dataclass(slots=True, frozen=True)
class VideoMatchSample:
    target_time: float
    source_time: float
    video_delay: float
    score: float


@dataclass(slots=True)
class AVTimelineDecision:
    enabled: bool = True
    reliable: bool = False
    applied: bool = False
    audio_delay: float | None = None
    video_delay: float | None = None
    residual: float | None = None
    adjustment_ms: int = 0
    reason: str = "Video timeline was not analyzed"
    samples: list[VideoMatchSample] = field(default_factory=list)


def _sample_centers(
    duration: float,
    positions: tuple[float, ...] | None = None,
    search_radius: float = VIDEO_MATCH_SEARCH_RADIUS_SECONDS,
) -> list[float]:
    margin = VIDEO_MATCH_REFERENCE_SECONDS + search_radius + 2.0
    if duration <= margin * 2:
        return [max(duration / 2.0, 0.0)]
    usable = duration - margin * 2
    selected = positions or tuple(
        (index + 1) / (VIDEO_MATCH_SAMPLE_COUNT + 1) for index in range(VIDEO_MATCH_SAMPLE_COUNT)
    )
    return [margin + usable * position for position in selected]


def _extract_frames(
    path: Path,
    destination: Path,
    *,
    start: float,
    duration: float,
    runner: ToolRunner,
    time_scale: float = 1.0,
) -> np.ndarray:
    seek_start = start * time_scale
    decode_duration = duration * time_scale
    timing_filter = ""
    if abs(time_scale - 1.0) > 0.000_001:
        timing_filter = (
            f"trim=duration={decode_duration:.6f},"
            f"setpts=(PTS-STARTPTS)/{time_scale:.12f},"
        )
    runner.run(
        (
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{seek_start:.6f}",
            "-i",
            path,
            "-t",
            f"{duration:.6f}",
            "-an",
            "-sn",
            "-vf",
            (
                timing_filter
                # Broadcast 29.97 sources are often interlaced even when the
                # target WEB/Blu-ray master is progressive. Deinterlace only
                # frames flagged as interlaced before comparing spatial
                # fingerprints; progressive inputs pass through unchanged.
                + "bwdif=mode=send_frame:parity=auto:deint=interlaced,"
                + f"fps={VIDEO_MATCH_FPS},"
                f"scale={VIDEO_MATCH_WIDTH}:{VIDEO_MATCH_HEIGHT},format=gray"
            ),
            "-f",
            "rawvideo",
            destination,
        )
    )
    pixels = VIDEO_MATCH_WIDTH * VIDEO_MATCH_HEIGHT
    raw = np.fromfile(destination, dtype=np.uint8)
    if len(raw) < pixels or len(raw) % pixels:
        raise ProcessingError(f"Could not decode complete comparison frames from {path}")
    return raw.reshape(-1, pixels).astype(np.float32)


def _normalized_frames(frames: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    centered = frames - frames.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(centered, axis=1, keepdims=True)
    informative = norms[:, 0] >= 100.0
    normalized = centered / np.maximum(norms, 1.0)
    return normalized, informative


def _match_window(source: np.ndarray, target: np.ndarray) -> tuple[int, float] | None:
    if len(source) < len(target) or len(target) < 4:
        return None
    source_normalized, source_informative = _normalized_frames(source)
    target_normalized, target_informative = _normalized_frames(target)
    scores: list[tuple[float, int]] = []
    for position in range(len(source) - len(target) + 1):
        usable = source_informative[position : position + len(target)] & target_informative
        if int(usable.sum()) < max(4, len(target) // 4):
            continue
        per_frame = np.sum(
            source_normalized[position : position + len(target)][usable]
            * target_normalized[usable],
            axis=1,
        )
        score = (float(np.median(per_frame)) + float(np.mean(per_frame))) / 2.0
        scores.append((score, position))
    if not scores:
        return None
    score, position = max(scores)
    return position, score


def _active_audio_delay(
    shift_points: list[tuple[float, float, float]], source_time: float, manual_delay: float
) -> float:
    active = shift_points[0][2]
    for _, point_source_time, delay in shift_points:
        if point_source_time > source_time:
            break
        active = delay
    return float(active) - manual_delay


def _source_time_for_target(
    shift_points: list[tuple[float, float, float]], target_time: float, manual_delay: float
) -> float:
    candidates = [target_time - (float(point[2]) - manual_delay) for point in shift_points]
    for candidate in candidates:
        if candidate < 0:
            continue
        active = _active_audio_delay(shift_points, candidate, manual_delay)
        if abs(candidate + active - target_time) <= 0.05:
            return candidate
    return max(candidates[0], 0.0)


def reconcile_av_timeline(
    source: Path,
    target: Path,
    *,
    duration: float,
    shift_points: list[tuple[float, float, float]],
    manual_delay: float,
    tolerance_ms: int,
    work_dir: Path,
    runner: ToolRunner,
    source_time_scale: float = 1.0,
    search_radius_seconds: float | None = None,
    sample_positions: tuple[float, ...] | None = None,
) -> AVTimelineDecision:
    """Measure and correct a constant residual between audio and video mappings.

    Milksync compares decoded common-language audio. Container packet delays are
    added separately. A release can still contain a shared A/V mux error: in
    that case both output audio tracks remain equally late or early. Comparing
    the two video timelines exposes that residual without guessing from PTS.
    """

    decision = AVTimelineDecision()
    if not shift_points:
        decision.reason = "No audio shift points were available for A/V reconciliation"
        return decision

    samples: list[VideoMatchSample] = []
    radius = search_radius_seconds or VIDEO_MATCH_SEARCH_RADIUS_SECONDS
    reference_duration = VIDEO_MATCH_REFERENCE_SECONDS
    centers = _sample_centers(duration, sample_positions, radius)
    for index, target_time in enumerate(centers):
        predicted_source_time = _source_time_for_target(
            shift_points, target_time, manual_delay
        )
        source_start = max(predicted_source_time - radius, 0.0)
        source_file = work_dir / f"video-match-source-{index}.gray"
        target_file = work_dir / f"video-match-target-{index}.gray"
        try:
            source_frames = _extract_frames(
                source,
                source_file,
                start=source_start,
                duration=reference_duration + radius * 2,
                runner=runner,
                time_scale=source_time_scale,
            )
            target_frames = _extract_frames(
                target,
                target_file,
                start=target_time,
                duration=reference_duration,
                runner=runner,
            )
            match = _match_window(source_frames, target_frames)
            if match is None:
                continue
            position, score = match
            source_time = source_start + position / VIDEO_MATCH_FPS
            samples.append(
                VideoMatchSample(
                    target_time=target_time,
                    source_time=source_time,
                    video_delay=target_time - source_time,
                    score=score,
                )
            )
        except ProcessingError:
            continue
        finally:
            source_file.unlink(missing_ok=True)
            target_file.unlink(missing_ok=True)

    decision.samples = samples
    trusted = [sample for sample in samples if sample.score >= VIDEO_MATCH_MIN_SCORE]
    minimum_samples = min(3, len(centers))
    if len(trusted) < minimum_samples:
        decision.reason = "Too few reliable cross-release video matches"
        return decision

    video_delays = [sample.video_delay for sample in trusted]
    audio_delays = [
        _active_audio_delay(shift_points, sample.source_time, manual_delay)
        for sample in trusted
    ]
    residuals = [audio - video for audio, video in zip(audio_delays, video_delays)]
    if max(residuals) - min(residuals) > VIDEO_MATCH_MAX_SPREAD_SECONDS:
        decision.reason = "Audio/video residual changes across the feature; no constant correction"
        return decision

    decision.reliable = True
    decision.audio_delay = statistics.median(audio_delays)
    decision.video_delay = statistics.median(video_delays)
    decision.residual = statistics.median(residuals)
    if abs(decision.residual) * 1000 <= tolerance_ms:
        decision.reason = "Audio and video timeline mappings agree within tolerance"
        return decision

    decision.adjustment_ms = round(-decision.residual * 1000)
    decision.applied = True
    decision.reason = "Corrected constant residual between audio and video timeline mappings"
    return decision
