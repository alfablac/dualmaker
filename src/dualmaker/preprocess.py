from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .models import Track
from .runner import ToolRunner

LOGGER = logging.getLogger("dualmaker")
BLACK_RE = re.compile(
    r"black_start:(?P<start>-?\d+(?:\.\d+)?)\s+"
    r"black_end:(?P<end>-?\d+(?:\.\d+)?)\s+"
    r"black_duration:(?P<duration>\d+(?:\.\d+)?)"
)


@dataclass(slots=True, frozen=True)
class BlackInterval:
    start: float
    end: float
    duration: float


@dataclass(slots=True)
class RecapDecision:
    normal_trim: float = 0.0
    dual_trim: float = 0.0
    baseline_score: float | None = None
    selected_score: float | None = None
    applied: bool = False
    reason: str = "No validated one-sided recap"
    candidates: list[dict[str, float]] = field(default_factory=list)


def detect_black_intervals(
    path: Path,
    *,
    window: float = 120.0,
    minimum_duration: float = 0.6,
    runner: ToolRunner | None = None,
) -> list[BlackInterval]:
    runner = runner or ToolRunner()
    result = runner.run(
        (
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            path,
            "-t",
            str(window),
            "-vf",
            f"blackdetect=d={minimum_duration}:pix_th=0.10",
            "-an",
            "-f",
            "null",
            "-",
        ),
        check=False,
    )
    intervals = []
    for match in BLACK_RE.finditer(result.stderr):
        duration = float(match["duration"])
        if duration >= minimum_duration:
            intervals.append(BlackInterval(float(match["start"]), float(match["end"]), duration))
    return intervals


def keyframes(
    path: Path, *, window: float = 120.0, runner: ToolRunner | None = None
) -> list[float]:
    runner = runner or ToolRunner()
    result = runner.run(
        (
            "ffprobe",
            "-v",
            "error",
            "-skip_frame",
            "nokey",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            "-read_intervals",
            f"%+{window}",
            path,
        )
    )
    values = []
    for line in result.stdout.splitlines():
        try:
            values.append(float(line.strip().rstrip(",")))
        except ValueError:
            continue
    return values


def safe_master_candidates(intervals: list[BlackInterval], frame_times: list[float]) -> list[float]:
    candidates = [0.0]
    for interval in intervals:
        inside = [value for value in frame_times if interval.start <= value <= interval.end]
        if inside:
            candidates.append(min(inside, key=lambda value: abs(value - interval.end)))
            continue
        nearby = [value for value in frame_times if abs(value - interval.end) <= 0.5]
        if nearby:
            candidates.append(min(nearby, key=lambda value: abs(value - interval.end)))
    return sorted({round(value, 6) for value in candidates})


def source_candidates(intervals: list[BlackInterval]) -> list[float]:
    return sorted({0.0, *(round(interval.end, 6) for interval in intervals)})


def _binary_audio_envelope(
    path: Path,
    track: Track,
    start: float,
    *,
    duration: float = 60.0,
    tempo: float = 1.0,
    runner: ToolRunner | None = None,
) -> np.ndarray:
    import subprocess

    runner = runner or ToolRunner()
    command = [
        runner.require("ffmpeg"),
        "-v",
        "error",
        "-ss",
        str(start),
        "-i",
        str(path),
        "-map",
        f"0:a:{track.type_index}",
        "-t",
        str(duration),
        "-ac",
        "1",
        "-ar",
        "8000",
    ]
    if abs(tempo - 1.0) > 0.000_001:
        command += ["-filter:a", f"atempo={tempo:.12f}"]
    command += [
        "-f",
        "s16le",
        "-",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        env=runner.environment,
    )
    if completed.returncode != 0 or not completed.stdout:
        return np.array([], dtype=np.float32)
    samples = np.frombuffer(completed.stdout, dtype=np.int16).astype(np.float32)
    frame_size = 400
    usable = len(samples) - (len(samples) % frame_size)
    if usable < frame_size * 40:
        return np.array([], dtype=np.float32)
    frames = samples[:usable].reshape(-1, frame_size)
    spectra = np.abs(np.fft.rfft(frames * np.hanning(frame_size), axis=1))[:, 1:]
    bands = np.array_split(np.arange(spectra.shape[1]), 24)
    features = np.stack([np.log1p(spectra[:, band].mean(axis=1)) for band in bands], axis=1)
    standard_deviation = features.std(axis=0)
    if all(math.isclose(float(value), 0.0) for value in standard_deviation):
        return np.array([], dtype=np.float32)
    standard_deviation[standard_deviation == 0] = 1.0
    return ((features - features.mean(axis=0)) / standard_deviation).astype(np.float32)


def envelope_similarity(left: np.ndarray, right: np.ndarray, max_lag: int = 40) -> float:
    size = min(len(left), len(right))
    if size < 40:
        return -1.0
    left = left[:size]
    right = right[:size]
    best = -1.0
    for lag in range(-max_lag, max_lag + 1):
        if lag < 0:
            l_slice, r_slice = left[-lag:], right[: size + lag]
        elif lag > 0:
            l_slice, r_slice = left[: size - lag], right[lag:]
        else:
            l_slice, r_slice = left, right
        if len(l_slice) < 40:
            continue
        l_slice = l_slice - l_slice.mean()
        r_slice = r_slice - r_slice.mean()
        l_slice = l_slice.reshape(-1)
        r_slice = r_slice.reshape(-1)
        denominator = float(np.linalg.norm(l_slice) * np.linalg.norm(r_slice))
        if denominator == 0:
            continue
        score = float(np.dot(l_slice, r_slice) / denominator)
        best = max(best, score)
    return best


def choose_recap_trim(
    normal_path: Path,
    dual_path: Path,
    normal_original: Track,
    dual_original: Track,
    *,
    window: float = 120.0,
    runner: ToolRunner | None = None,
) -> RecapDecision:
    runner = runner or ToolRunner()
    normal_black = detect_black_intervals(normal_path, window=window, runner=runner)
    dual_black = detect_black_intervals(dual_path, window=window, runner=runner)
    normal_candidates = safe_master_candidates(
        normal_black, keyframes(normal_path, window=window, runner=runner)
    )
    dual_candidates = source_candidates(dual_black)

    cache: dict[tuple[str, float], np.ndarray] = {}

    def audio(path: Path, track: Track, start: float) -> np.ndarray:
        key = (str(path), start)
        if key not in cache:
            cache[key] = _binary_audio_envelope(path, track, start, runner=runner)
        return cache[key]

    scored: list[tuple[float, float, float]] = []
    for normal_cut in normal_candidates:
        for dual_cut in dual_candidates:
            similarity = envelope_similarity(
                audio(normal_path, normal_original, normal_cut),
                audio(dual_path, dual_original, dual_cut),
            )
            if similarity >= -0.5:
                scored.append((similarity, normal_cut, dual_cut))
    if not scored:
        return RecapDecision(reason="Could not extract comparable opening audio")
    scored.sort(key=lambda item: (-(item[0] - (item[1] + item[2]) * 0.0001), item[1] + item[2]))
    baseline = next((item[0] for item in scored if item[1] == 0 and item[2] == 0), None)
    best = scored[0]
    decision = RecapDecision(
        baseline_score=baseline,
        selected_score=best[0],
        candidates=[
            {"score": score, "normal_trim": normal, "dual_trim": dual}
            for score, normal, dual in scored[:10]
        ],
    )
    if baseline is not None and baseline >= 0.60:
        decision.reason = "Opening audio already aligns; no recap trim needed"
        return decision
    runner_up = scored[1][0] if len(scored) > 1 else -1.0
    if best[0] < 0.48 or best[0] - runner_up < 0.06 or (best[1] == 0 and best[2] == 0):
        decision.reason = "No unique high-confidence black-boundary trim"
        return decision
    decision.normal_trim = best[1]
    decision.dual_trim = best[2]
    decision.applied = True
    decision.reason = "Validated one-sided recap trim using black boundaries and common audio"
    return decision
