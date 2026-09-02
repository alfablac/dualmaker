from __future__ import annotations

import statistics
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np

from .avsync import AVTimelineDecision, _active_audio_delay, _extract_frames, _match_window
from .defaults import (
    FPS_ANCHOR_HEIGHT,
    FPS_ANCHOR_SAMPLE_RATE,
    FPS_ANCHOR_WIDTH,
    VIDEO_MATCH_FPS,
    VIDEO_MATCH_MIN_SCORE,
    VIDEO_MATCH_REFERENCE_SECONDS,
)
from .errors import ProcessingError
from .models import DualMakerConfig, FPSDecision, FPSMatchSample, MediaAsset
from .runner import ToolRunner


def _fraction(rate: object) -> Fraction:
    return Fraction(int(rate.numerator), int(rate.denominator))  # type: ignore[attr-defined]


def _canonical_rate(value: str) -> str:
    rate = Fraction(value.strip())
    return f"{rate.numerator}/{rate.denominator}"


def _compatible(left: str, right: str, configured: tuple[str, ...]) -> bool:
    wanted = frozenset((_canonical_rate(left), _canonical_rate(right)))
    for item in configured:
        first, separator, second = item.partition("=")
        if separator and frozenset((_canonical_rate(first), _canonical_rate(second))) == wanted:
            return True
    return False


def _video_rate_characteristics(master: MediaAsset, dual: MediaAsset) -> dict[str, object]:
    master_rate = _fraction(master.frame_rate) if master.frame_rate else None
    dual_rate = _fraction(dual.frame_rate) if dual.frame_rate else None
    ratio = float(dual_rate / master_rate) if master_rate and dual_rate else None
    dual_video = next(
        (
            stream
            for stream in dual.ffprobe.get("streams", [])
            if stream.get("codec_type") == "video"
        ),
        {},
    )
    field_order = str(dual_video.get("field_order") or "unknown").casefold()
    telecine_candidate = ratio is not None and abs(ratio - 1.25) <= 0.0025
    return {
        "rate_ratio": ratio,
        "dual_field_order": field_order,
        "dual_interlaced": field_order not in {"unknown", "progressive"},
        "telecine_or_frame_duplication_candidate": telecine_candidate,
        "interpretation": (
            "A 5:4 frame-cadence relationship may be 3:2 telecine/frame duplication; "
            "audio must be tested in real time before any speed correction"
            if telecine_candidate
            else "No standard 3:2 telecine cadence was inferred from the rational rates"
        ),
    }


def evaluate_fps_pair(
    master: MediaAsset,
    dual: MediaAsset,
    config: DualMakerConfig,
) -> FPSDecision:
    decision = FPSDecision(master_rate=master.frame_rate, dual_rate=dual.frame_rate)
    if master.frame_rate is None or dual.frame_rate is None:
        decision.required = True
        decision.compatible = False
        decision.reason = "An exact frame rate could not be read from both video streams"
        return decision
    master_rate = _fraction(master.frame_rate)
    dual_rate = _fraction(dual.frame_rate)
    if master_rate == dual_rate:
        decision.approved = True
        decision.reason = "Input frame rates are identical"
        return decision

    decision.required = True
    decision.validation["rate_characteristics"] = _video_rate_characteristics(master, dual)
    decision.compatible = _compatible(
        master.frame_rate.rational,
        dual.frame_rate.rational,
        config.compatible_fps_pairs,
    )
    decision.proposed_speed_factor = float(master_rate / dual_rate)
    decision.expected_drift_seconds = dual.duration / decision.proposed_speed_factor - dual.duration
    decision.approved = config.allow_experimental_fps_sync or config.align_framerate
    if not decision.compatible:
        decision.reason = (
            "Frame-rate pair is not listed in compatible_fps_pairs: "
            f"{master.frame_rate.rational} vs {dual.frame_rate.rational}"
        )
    elif decision.approved:
        decision.reason = "Experimental different-FPS synchronization was explicitly enabled"
    else:
        decision.reason = "Experimental different-FPS synchronization requires explicit approval"
    return decision


@dataclass(slots=True)
class _Hypothesis:
    speed_factor: float
    confidence: float = 0.0
    detected_speed_factor: float | None = None
    residual_drift: float | None = None
    reliable: bool = False
    samples: list[FPSMatchSample] | None = None
    segmented: bool = False
    strategy: str = "local"
    anchor_candidates: int = 0
    accepted_anchor_count: int = 0


def _comparison_deinterlace_prefix(enabled: bool) -> str:
    """Return the conservative filter prefix for interlaced-source matching.

    The master release is normally progressive, while broadcast/TVRip sources
    may carry top- or bottom-field-first H.264.  Comparing their raw decoded
    frames makes combing artefacts look like scene differences.  ``send_frame``
    preserves the source timeline (unlike bob deinterlacing), which is vital
    when testing competing FPS clock hypotheses.
    """

    return "bwdif=mode=send_frame:parity=auto:deint=interlaced," if enabled else ""


def _extract_normalized_source_frames(
    path: Path,
    destination: Path,
    *,
    normalized_start: float,
    normalized_duration: float,
    speed_factor: float,
    deinterlace: bool = False,
    runner: ToolRunner,
) -> np.ndarray:
    original_start = normalized_start * speed_factor
    original_duration = normalized_duration * speed_factor
    from .defaults import VIDEO_MATCH_HEIGHT, VIDEO_MATCH_WIDTH

    runner.run(
        (
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{original_start:.6f}",
            "-i",
            path,
            "-t",
            f"{normalized_duration:.6f}",
            "-an",
            "-sn",
            "-vf",
            (
                f"{_comparison_deinterlace_prefix(deinterlace)}"
                f"trim=duration={original_duration:.6f},"
                f"setpts=(PTS-STARTPTS)/{speed_factor:.12f},"
                f"fps={VIDEO_MATCH_FPS},"
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
        raise ProcessingError(f"Could not decode complete FPS comparison frames from {path}")
    return raw.reshape(-1, pixels).astype(np.float32)


def _analyze_hypothesis(
    source: Path,
    target: Path,
    *,
    duration: float,
    positions: tuple[float, ...],
    speed_factor: float,
    search_radius: float,
    minimum_confidence: float,
    maximum_drift: float,
    work_dir: Path,
    runner: ToolRunner,
    label: str,
    source_deinterlace: bool = False,
) -> _Hypothesis:
    result = _Hypothesis(speed_factor=speed_factor, samples=[])
    reference = VIDEO_MATCH_REFERENCE_SECONDS
    margin = reference + search_radius + 1.0
    usable_duration = max(duration - 2 * margin, 0.0)
    for index, position in enumerate(positions):
        target_time = margin + usable_duration * position if usable_duration else duration * position
        normalized_start = max(target_time - search_radius, 0.0)
        source_file = work_dir / f"fps-{label}-source-{index}.gray"
        target_file = work_dir / f"fps-{label}-target-{index}.gray"
        try:
            source_frames = _extract_normalized_source_frames(
                source,
                source_file,
                normalized_start=normalized_start,
                normalized_duration=reference + 2 * search_radius,
                speed_factor=speed_factor,
                deinterlace=source_deinterlace,
                runner=runner,
            )
            target_frames = _extract_frames(
                target,
                target_file,
                start=target_time,
                duration=reference,
                runner=runner,
            )
            match = _match_window(source_frames, target_frames)
            if match is None:
                continue
            frame, score = match
            normalized_source_time = normalized_start + frame / VIDEO_MATCH_FPS
            result.samples.append(
                FPSMatchSample(
                    position=position,
                    target_time=target_time,
                    source_time=normalized_source_time * speed_factor,
                    score=score,
                )
            )
        except ProcessingError:
            continue
        finally:
            source_file.unlink(missing_ok=True)
            target_file.unlink(missing_ok=True)

    trusted = [sample for sample in result.samples if sample.score >= VIDEO_MATCH_MIN_SCORE]
    if len(trusted) < min(3, len(positions)):
        return result
    target_times = np.asarray([sample.target_time for sample in trusted], dtype=np.float64)
    source_times = np.asarray([sample.source_time for sample in trusted], dtype=np.float64)
    slope, intercept = np.polyfit(target_times, source_times, 1)
    residuals = source_times - (slope * target_times + intercept)
    result.detected_speed_factor = float(slope)
    result.residual_drift = float(np.max(residuals) - np.min(residuals))
    coverage = len(trusted) / len(positions)
    stability = max(0.0, 1.0 - result.residual_drift / max(maximum_drift, 0.001))
    result.confidence = float(statistics.median(item.score for item in trusted)) * coverage
    result.confidence *= 0.75 + 0.25 * stability
    result.reliable = result.confidence >= minimum_confidence
    return result


def _extract_anchor_descriptors(
    path: Path,
    destination: Path,
    *,
    duration: float,
    deinterlace: bool = False,
    runner: ToolRunner,
) -> tuple[np.ndarray, np.ndarray]:
    """Decode a compact visual index for a whole feature.

    The normal FPS check intentionally looks at only three local windows.  That
    is fast for normal releases but it cannot cross a commercial break or an
    alternate broadcast opening.  This index is only used as a fail-closed
    fallback: 32x18 grayscale frames at one frame per second are small enough
    to compare the whole feature without creating a shared or long-lived cache.
    """

    runner.run(
        (
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            path,
            "-t",
            f"{duration:.6f}",
            "-an",
            "-sn",
            "-vf",
            (
                f"{_comparison_deinterlace_prefix(deinterlace)}"
                f"fps={FPS_ANCHOR_SAMPLE_RATE:g},"
                f"scale={FPS_ANCHOR_WIDTH}:{FPS_ANCHOR_HEIGHT}:flags=area,format=gray"
            ),
            "-f",
            "rawvideo",
            destination,
        )
    )
    pixels = FPS_ANCHOR_WIDTH * FPS_ANCHOR_HEIGHT
    raw = np.fromfile(destination, dtype=np.uint8)
    if len(raw) < pixels or len(raw) % pixels:
        raise ProcessingError(f"Could not decode a complete FPS anchor index from {path}")
    frames = raw.reshape(-1, pixels).astype(np.float32)
    centered = frames - frames.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(centered, axis=1)
    normalized = centered / np.maximum(norms[:, np.newaxis], 1.0)
    # A high spatial variance and a changing neighbouring frame are both useful
    # signals.  They avoid spending anchor attempts on fades, black screens and
    # static title cards whenever a nearby informative shot is available.
    motion = np.empty(len(frames), dtype=np.float32)
    motion[0] = 0.0
    if len(frames) > 1:
        motion[1:] = np.mean(np.abs(frames[1:] - frames[:-1]), axis=1)
    salience = norms * (1.0 + motion / 32.0)
    return normalized, salience


def _anchor_target_indices(
    salience: np.ndarray,
    *,
    count: int,
    window_seconds: float,
) -> list[int]:
    """Pick distributed but informative target scenes from a visual index."""

    if len(salience) < 3:
        return []
    margin = max(round(window_seconds * FPS_ANCHOR_SAMPLE_RATE), 2)
    first = min(margin, max(len(salience) - 1, 0))
    last = max(len(salience) - margin - 1, first)
    if last <= first:
        return [len(salience) // 2]
    radius = max(round(window_seconds * FPS_ANCHOR_SAMPLE_RATE * 1.5), 2)
    selected: list[int] = []
    for center in np.linspace(first, last, count):
        start = max(first, round(center) - radius)
        end = min(last + 1, round(center) + radius + 1)
        if end <= start:
            continue
        selected.append(start + int(np.argmax(salience[start:end])))
    return sorted(set(selected))


def _top_distinct_indices(
    scores: np.ndarray,
    *,
    count: int,
    minimum_separation_seconds: float,
) -> list[int]:
    separation = max(round(minimum_separation_seconds * FPS_ANCHOR_SAMPLE_RATE), 1)
    chosen: list[int] = []
    for index in np.argsort(scores)[::-1]:
        candidate = int(index)
        if any(abs(candidate - previous) < separation for previous in chosen):
            continue
        chosen.append(candidate)
        if len(chosen) >= count:
            break
    return chosen


def _sequence_score(
    source: np.ndarray,
    target: np.ndarray,
    *,
    source_index: int,
    target_index: int,
    speed_factor: float,
    window_seconds: float,
) -> float | None:
    """Verify a still-image candidate over a short time-based sequence.

    One frame alone is prone to matching fades, title cards, or repeated shots.
    Advancing source samples by the hypothesis' physical seconds per master
    second lets this verify real-time and speed-corrected explanations equally.
    """

    half = max(round(window_seconds * FPS_ANCHOR_SAMPLE_RATE / 2), 2)
    offsets = np.arange(-half, half + 1, dtype=np.int32)
    target_indices = target_index + offsets
    source_indices = np.rint(source_index + offsets * speed_factor).astype(np.int32)
    usable = (
        (target_indices >= 0)
        & (target_indices < len(target))
        & (source_indices >= 0)
        & (source_indices < len(source))
    )
    if int(usable.sum()) < 5:
        return None
    similarities = np.sum(
        source[source_indices[usable]] * target[target_indices[usable]], axis=1
    )
    return float((np.median(similarities) + np.mean(similarities)) / 2.0)


def _best_monotonic_chain(samples: list[FPSMatchSample]) -> list[FPSMatchSample]:
    """Return the strongest time-ordered set without assuming one global cut."""

    ordered = sorted(samples, key=lambda sample: (sample.target_time, sample.source_time))
    if not ordered:
        return []
    scores = [sample.score for sample in ordered]
    parents = [-1] * len(ordered)
    lengths = [1] * len(ordered)
    for index, current in enumerate(ordered):
        for previous_index, previous in enumerate(ordered[:index]):
            if (
                previous.target_time >= current.target_time - 0.001
                or previous.source_time >= current.source_time - 0.001
            ):
                continue
            candidate_length = lengths[previous_index] + 1
            candidate_score = scores[previous_index] + current.score
            if candidate_length > lengths[index] or (
                candidate_length == lengths[index] and candidate_score > scores[index]
            ):
                lengths[index] = candidate_length
                scores[index] = candidate_score
                parents[index] = previous_index
    best = max(range(len(ordered)), key=lambda index: (lengths[index], scores[index]))
    chain: list[FPSMatchSample] = []
    while best >= 0:
        chain.append(ordered[best])
        best = parents[best]
    return list(reversed(chain))


def _linear_cluster(
    samples: list[FPSMatchSample],
    *,
    speed_factor: float,
    tolerance: float,
) -> list[FPSMatchSample]:
    """Find a consistent affine portion of a candidate anchor chain."""

    if len(samples) < 3:
        return []
    offsets = [sample.source_time - speed_factor * sample.target_time for sample in samples]
    best: list[FPSMatchSample] = []
    for offset in offsets:
        current = [
            sample
            for sample in samples
            if abs((sample.source_time - speed_factor * sample.target_time) - offset) <= tolerance
        ]
        if (len(current), sum(sample.score for sample in current)) > (
            len(best),
            sum(sample.score for sample in best),
        ):
            best = current
    return best


def _finalize_anchor_hypothesis(
    result: _Hypothesis,
    *,
    target_duration: float,
    minimum_confidence: float,
    maximum_drift: float,
    minimum_separation_seconds: float,
    global_coverage_required: float,
) -> _Hypothesis:
    candidates = [sample for sample in result.samples or [] if sample.score >= VIDEO_MATCH_MIN_SCORE]
    result.anchor_candidates = len(candidates)
    chain = _best_monotonic_chain(candidates)
    result.accepted_anchor_count = len(chain)
    if len(chain) < 3:
        return result
    span = chain[-1].target_time - chain[0].target_time
    if span < minimum_separation_seconds * 2:
        return result
    result.confidence = float(statistics.median(sample.score for sample in chain))
    result.confidence *= min(len(chain) / 3.0, 1.0)

    # At one FPS an otherwise exact visual correspondence can land one frame
    # either side of the ideal second.  Allow that indexing precision while
    # retaining the configured, stricter post-Milksync A/V validation.
    cluster = _linear_cluster(
        chain,
        speed_factor=result.speed_factor,
        tolerance=max(maximum_drift * 2.0, 1.25),
    )
    if len(cluster) >= 3:
        target_times = np.asarray([sample.target_time for sample in cluster], dtype=np.float64)
        source_times = np.asarray([sample.source_time for sample in cluster], dtype=np.float64)
        slope, intercept = np.polyfit(target_times, source_times, 1)
        residuals = source_times - (slope * target_times + intercept)
        residual_drift = float(np.max(residuals) - np.min(residuals))
        result.detected_speed_factor = float(slope)
        result.residual_drift = residual_drift
        global_coverage = (
            max(sample.target_time for sample in cluster)
            - min(sample.target_time for sample in cluster)
        ) / max(target_duration, 1.0)
        result.reliable = (
            result.confidence >= minimum_confidence
            and global_coverage >= global_coverage_required
            and abs(result.detected_speed_factor - result.speed_factor)
            <= max(0.01, maximum_drift / max(span, 1.0))
            and residual_drift <= max(maximum_drift * 2.0, 1.25)
        )
        if result.reliable:
            result.samples = cluster
            return result

    # Multiple clean, monotonic short-context matches show that the material is
    # the same but that a break or an alternate cut shifted the timeline.  This
    # is *not* a green light for ordinary different-FPS processing; callers must
    # opt into the separate segmented TVRip workflow.
    result.segmented = result.confidence >= minimum_confidence
    result.detected_speed_factor = result.speed_factor if result.segmented else None
    result.residual_drift = (
        max(sample.source_time - result.speed_factor * sample.target_time for sample in chain)
        - min(sample.source_time - result.speed_factor * sample.target_time for sample in chain)
    )
    result.samples = chain
    return result


def _adaptive_anchor_hypothesis(
    source: np.ndarray,
    target: np.ndarray,
    *,
    target_indices: list[int],
    target_duration: float,
    speed_factor: float,
    config: DualMakerConfig,
) -> _Hypothesis:
    result = _Hypothesis(speed_factor=speed_factor, samples=[], strategy="adaptive")
    for target_index in target_indices:
        frame_scores = source @ target[target_index]
        source_indices = _top_distinct_indices(
            frame_scores,
            count=config.fps_anchor_candidate_count,
            minimum_separation_seconds=config.fps_anchor_window_seconds / 2.0,
        )
        for source_index in source_indices:
            score = _sequence_score(
                source,
                target,
                source_index=source_index,
                target_index=target_index,
                speed_factor=speed_factor,
                window_seconds=config.fps_anchor_window_seconds,
            )
            if score is None:
                continue
            result.samples.append(
                FPSMatchSample(
                    position=(target_index / FPS_ANCHOR_SAMPLE_RATE) / max(target_duration, 1.0),
                    target_time=target_index / FPS_ANCHOR_SAMPLE_RATE,
                    source_time=source_index / FPS_ANCHOR_SAMPLE_RATE,
                    score=score,
                )
            )
    return _finalize_anchor_hypothesis(
        result,
        target_duration=target_duration,
        minimum_confidence=config.fps_min_match_confidence,
        maximum_drift=config.fps_max_drift_seconds,
        minimum_separation_seconds=config.fps_anchor_min_separation_seconds,
        global_coverage_required=config.fps_anchor_global_coverage,
    )


def _describe_hypothesis(hypothesis: _Hypothesis) -> dict[str, object]:
    return {
        "strategy": hypothesis.strategy,
        "confidence": hypothesis.confidence,
        "detected_speed_factor": hypothesis.detected_speed_factor,
        "residual_drift_seconds": hypothesis.residual_drift,
        "reliable": hypothesis.reliable,
        "segmented": hypothesis.segmented,
        "anchor_candidates": hypothesis.anchor_candidates,
        "accepted_anchor_count": hypothesis.accepted_anchor_count,
        "samples": [
            {
                "target_time": sample.target_time,
                "source_time": sample.source_time,
                "score": sample.score,
            }
            for sample in hypothesis.samples or []
        ],
    }


def _audio_duration_speed_candidates(
    source_duration: float | None,
    target_duration: float | None,
    config: DualMakerConfig,
) -> tuple[list[tuple[str, float]], dict[str, object]]:
    """Nominate standard content clocks from the common-original durations.

    This is deliberately only a hypothesis generator.  Alternate cuts make a
    raw duration ratio unsafe as a speed value, while standards conversions
    cluster around known physical ratios such as 24/25.  The caller still has
    to prove a nominated factor against independent visual content anchors.
    """

    evidence: dict[str, object] = {
        "source_original_duration_seconds": source_duration,
        "target_original_duration_seconds": target_duration,
        "observed_ratio": None,
        "tolerance": config.fps_audio_duration_ratio_tolerance,
        "nominated": [],
    }
    if not source_duration or not target_duration or source_duration <= 0 or target_duration <= 0:
        evidence["reason"] = "Both selected common-original track durations are required"
        return [], evidence

    ratio = source_duration / target_duration
    evidence["observed_ratio"] = ratio
    candidates: list[tuple[str, float]] = []
    seen: set[int] = set()
    nominated: list[dict[str, object]] = []
    for configured in config.fps_content_speed_factors:
        try:
            rational = Fraction(configured)
        except (ValueError, ZeroDivisionError):
            # Startup validation normally catches this. Keep this library API
            # fail-safe for callers that deliberately skip configuration checks.
            continue
        factor = float(rational)
        error = abs(ratio - factor)
        identity = round(factor * 1_000_000_000)
        if error > config.fps_audio_duration_ratio_tolerance or identity in seen:
            continue
        seen.add(identity)
        label = f"audio_duration_{rational.numerator}_{rational.denominator}"
        candidates.append((label, factor))
        nominated.append(
            {
                "configured_ratio": f"{rational.numerator}/{rational.denominator}",
                "speed_factor": factor,
                "absolute_error": error,
            }
        )
    evidence["nominated"] = nominated
    evidence["reason"] = (
        "Common-original durations nominate standard content-speed candidates; "
        "acceptance still requires content anchors"
        if candidates
        else "The duration ratio is not close to a configured standard content speed"
    )
    return candidates, evidence


def analyze_fps_timing(
    source: Path,
    target: Path,
    *,
    duration: float,
    source_duration: float,
    decision: FPSDecision,
    config: DualMakerConfig,
    work_dir: Path,
    runner: ToolRunner,
    allow_segmented_mapping: bool = False,
    source_original_duration: float | None = None,
    target_original_duration: float | None = None,
) -> FPSDecision:
    if not decision.required:
        return decision
    if not decision.approved or not decision.compatible:
        raise ProcessingError(decision.reason)

    container_speed_factor = decision.proposed_speed_factor
    rate_characteristics = decision.validation.get("rate_characteristics", {})
    source_deinterlace = bool(
        isinstance(rate_characteristics, dict)
        and rate_characteristics.get("dual_interlaced")
    )
    duration_candidates, duration_evidence = _audio_duration_speed_candidates(
        source_original_duration,
        target_original_duration,
        config,
    )
    candidates: list[tuple[str, float]] = [
        ("fps_ratio", container_speed_factor),
        ("real_time", 1.0),
    ]
    for label, factor in duration_candidates:
        if any(abs(factor - existing) <= 0.000_001 for _, existing in candidates):
            continue
        candidates.append((label, factor))

    local: dict[str, _Hypothesis] = {}
    close: dict[str, bool] = {}
    for label, factor in candidates:
        hypothesis = _analyze_hypothesis(
            source,
            target,
            duration=duration,
            positions=config.fps_validation_positions,
            speed_factor=factor,
            search_radius=config.fps_search_radius_seconds,
            minimum_confidence=config.fps_min_match_confidence,
            maximum_drift=config.fps_max_drift_seconds,
            work_dir=work_dir,
            runner=runner,
            label=label,
            source_deinterlace=source_deinterlace,
        )
        local[label] = hypothesis
        close[label] = bool(
            hypothesis.detected_speed_factor is not None
            and abs(hypothesis.detected_speed_factor - factor)
            <= config.fps_speed_ratio_tolerance
            and (hypothesis.residual_drift or 0.0) <= config.fps_max_drift_seconds
        )

    audio_labels = {label for label, _ in duration_candidates}
    observed_duration_ratio = float(duration_evidence.get("observed_ratio") or 0.0)
    duration_errors = {
        label: abs(observed_duration_ratio - factor) for label, factor in duration_candidates
    }

    def selection_score(label: str, hypothesis: _Hypothesis) -> float:
        # Duration evidence never makes a hypothesis reliable on its own. Once
        # visual anchors validate two otherwise similar explanations, however,
        # a standard ratio independently measured from the common audio is the
        # physically better explanation than an accidental short-window match.
        if label not in audio_labels:
            return hypothesis.confidence
        tolerance = max(config.fps_audio_duration_ratio_tolerance, 0.000_001)
        evidence_strength = max(0.0, 1.0 - duration_errors.get(label, tolerance) / tolerance)
        return hypothesis.confidence + 0.05 * evidence_strength

    discovery: dict[str, _Hypothesis] = {}
    reliable_local = [
        (label, hypothesis)
        for label, hypothesis in local.items()
        if hypothesis.reliable and close[label]
    ]
    if not reliable_local and config.fps_adaptive_anchors:
        source_file = work_dir / "fps-adaptive-source.gray"
        target_file = work_dir / "fps-adaptive-target.gray"
        try:
            source_descriptors, _ = _extract_anchor_descriptors(
                source,
                source_file,
                duration=source_duration,
                deinterlace=source_deinterlace,
                runner=runner,
            )
            target_descriptors, target_salience = _extract_anchor_descriptors(
                target,
                target_file,
                duration=duration,
                runner=runner,
            )
            target_indices = _anchor_target_indices(
                target_salience,
                count=config.fps_anchor_sample_count,
                window_seconds=config.fps_anchor_window_seconds,
            )
            discovery = {
                label: _adaptive_anchor_hypothesis(
                    source_descriptors,
                    target_descriptors,
                    target_indices=target_indices,
                    target_duration=duration,
                    speed_factor=factor,
                    config=config,
                )
                for label, factor in candidates
            }
        except ProcessingError:
            # The local analysis has already failed.  A decoding problem while
            # creating the optional full-timeline index must not make the media
            # job less diagnosable; the final error reports the attempted paths.
            discovery = {}
        finally:
            source_file.unlink(missing_ok=True)
            target_file.unlink(missing_ok=True)

    selected_label: str | None = None
    selected: _Hypothesis | None = None
    selected_strategy = ""
    if reliable_local:
        selected_label, selected = max(
            reliable_local,
            key=lambda item: selection_score(item[0], item[1]),
        )
        selected_strategy = "local"
    else:
        reliable_adaptive = [
            (label, hypothesis)
            for label, hypothesis in discovery.items()
            if hypothesis.reliable
        ]
        if reliable_adaptive:
            selected_label, selected = max(
                reliable_adaptive,
                key=lambda item: selection_score(item[0], item[1]),
            )
            selected_strategy = "adaptive"

    # A 29.97/23.976 broadcast source can carry a telecined video cadence while
    # its common-original audio remains on the master clock. Some remastered
    # masters are visually different enough that the low-resolution frame index
    # cannot prove that correspondence, even though Milksync's acoustic map can.
    # Do not infer a container-rate speed change in that situation. An explicit
    # TVRip segmented-workflow opt-in may advance to the acoustic preflight at
    # real time, where it still has to pass the later full map and per-segment
    # validation before muxing is permitted.
    telecine_candidate = bool(
        isinstance(rate_characteristics, dict)
        and rate_characteristics.get("telecine_or_frame_duplication_candidate")
    )
    real_time = local.get("real_time")
    if (
        selected is None
        and allow_segmented_mapping
        and telecine_candidate
        and real_time is not None
    ):
        selected_label = "real_time"
        selected = real_time
        selected_strategy = "telecine-acoustic-preflight"
        decision.validation["segmented_anchor_mapping"] = True
        decision.validation["telecine_acoustic_preflight"] = {
            "enabled": True,
            "reason": (
                "Explicit 29.97/23.976 telecine TVRip candidate; visual anchors were "
                "inconclusive, so the real-time Milksync map and strict local "
                "common-original segment validation are required"
            ),
            "local_visual_samples": len(real_time.samples or []),
            "nominated_audio_clocks": [label for label, _ in duration_candidates],
        }

    if selected is None and allow_segmented_mapping:
        segmented = [
            (label, hypothesis)
            for label, hypothesis in discovery.items()
            if hypothesis.segmented and hypothesis.confidence >= config.fps_min_match_confidence
        ]
        if segmented:
            selected_label, selected = max(
                segmented,
                key=lambda item: selection_score(item[0], item[1]),
            )
            selected_strategy = "segmented"
            decision.validation["segmented_anchor_mapping"] = True
    if selected is None:
        local_summary = ", ".join(
            f"{name}={len(item.samples or [])}" for name, item in local.items()
        )
        adaptive_summary = ", ".join(
            f"{name}={item.accepted_anchor_count} ordered anchors"
            for name, item in discovery.items()
        ) or "adaptive index unavailable"
        fallback_candidates = [
            (label, hypothesis, "local") for label, hypothesis in local.items()
        ] + [
            (label, hypothesis, "adaptive") for label, hypothesis in discovery.items()
        ]
        if fallback_candidates:
            selected_label, selected, fallback_origin = max(
                fallback_candidates,
                key=lambda item: selection_score(item[0], item[1]),
            )
            selected_strategy = f"best-effort-{fallback_origin}"
        else:
            # The local hypotheses are normally always present. Keep a
            # deterministic, source-agnostic fallback for a decoder failure
            # before even one comparison window could be analyzed.
            selected_label = "container_fps_ratio"
            selected = _Hypothesis(speed_factor=container_speed_factor)
            selected_strategy = "best-effort-container"
        # A 29.97/23.976 cadence mismatch is not evidence of a 20% audio
        # tempo change. If the common-original durations do not nominate a
        # standard content clock, prefer real time when a telecine candidate
        # is otherwise inconclusive. Choosing the container ratio merely from
        # a weak visual score can stretch same-tempo DVD audio and corrupt
        # every subsequent edit boundary.
        if telecine_candidate and selected_label == "fps_ratio":
            real_time_hypothesis = discovery.get("real_time") or local.get("real_time")
            if real_time_hypothesis is not None:
                selected_label = "real_time"
                selected = real_time_hypothesis
                selected_strategy = "best-effort-telecine-real-time"
        decision.validation["best_effort_fps_fallback"] = {
            "enabled": True,
            "reason": (
                "Experimental FPS analysis was inconclusive after local and adaptive "
                f"content-anchor search ({local_summary}; {adaptive_summary})"
            ),
            "local_summary": local_summary,
            "adaptive_summary": adaptive_summary,
            "selected_hypothesis": selected_label,
            "selected_strategy": selected_strategy,
            "selected_speed_factor": selected.speed_factor,
        }

    assert selected is not None and selected_label is not None
    decision.proposed_speed_factor = selected.speed_factor
    decision.apply_speed_correction = abs(selected.speed_factor - 1.0) > 0.000_001
    decision.detected_speed_factor = selected.detected_speed_factor
    decision.confidence = selected.confidence
    decision.samples = list(selected.samples or [])

    # A telecine TVRip needs segment-level evidence whether its selected
    # hypothesis is real time or a measured bounded speed correction. A
    # broadcast frame cadence is not enough to prove an audio clock, and a
    # visually remastered source may yield only one useful frame anchor. The
    # later Milksync map plus local common-original checks remain authoritative.
    # The adapter only skips its linear tempo preflight for the real-time case;
    # a non-unit candidate still receives that independent measurement.
    if (
        allow_segmented_mapping
        and telecine_candidate
        and "telecine_acoustic_preflight" not in decision.validation
    ):
        decision.validation["segmented_anchor_mapping"] = True
        decision.validation["telecine_acoustic_preflight"] = {
            "enabled": True,
            "reason": (
                "29.97/23.976 telecine candidate selected an experimental content clock; "
                "the completed Milksync map and strict local common-original segment "
                "validation remain required"
            ),
            "selected_speed_factor": selected.speed_factor,
            "local_visual_samples": len(real_time.samples or []) if real_time else 0,
            "nominated_audio_clocks": [label for label, _ in duration_candidates],
        }
    if selected_strategy.startswith("best-effort-"):
        decision.reason = (
            "Content-anchor evidence was inconclusive; continuing with the strongest "
            f"available {selected_strategy.removeprefix('best-effort-')} hypothesis "
            "for manual review"
        )
    elif selected_strategy == "telecine-acoustic-preflight":
        decision.reason = (
            "Visual FPS anchors were inconclusive for a 29.97/23.976 telecine candidate; "
            "continuing at real time only for explicit TVRip acoustic and per-segment validation"
        )
    elif selected_label in audio_labels:
        decision.reason = (
            "Common-original durations nominated a standard content-speed ratio and "
            f"{selected_strategy} content anchors confirmed it"
        )
    elif selected_label == "real_time":
        decision.reason = (
            f"{selected_strategy.capitalize()} content anchors match in real time; "
            "no playback-speed change was applied"
        )
    elif selected_strategy == "segmented":
        decision.reason = (
            "Adaptive anchors found matching content with broadcast/edit breaks at the "
            "container FPS speed ratio; continuing with the experimental segmented workflow"
        )
    else:
        decision.reason = (
            f"{selected_strategy.capitalize()} content anchors confirm the container FPS "
            "speed ratio"
        )

    decision.validation["container_fps_speed_factor"] = container_speed_factor
    decision.validation["audio_duration_evidence"] = duration_evidence
    decision.validation["selected_hypothesis"] = {
        "name": selected_label,
        "strategy": selected_strategy,
        "speed_factor": selected.speed_factor,
    }
    decision.validation["hypotheses"] = {
        name: _describe_hypothesis(item) for name, item in local.items()
    }
    if discovery:
        decision.validation["adaptive_anchor_discovery"] = {
            "sample_rate": FPS_ANCHOR_SAMPLE_RATE,
            **{name: _describe_hypothesis(item) for name, item in discovery.items()},
        }
    if (
        not decision.apply_speed_correction
        and decision.validation.get("rate_characteristics", {}).get(
            "telecine_or_frame_duplication_candidate"
        )
    ):
        decision.reason += (
            "; 29.97/23.976 is consistent with telecine or frame duplication, "
            "so the shared original audio remains on a real-time clock"
        )
    return decision


def validate_fps_timeline(
    decision: FPSDecision,
    av_timeline: AVTimelineDecision,
    *,
    shift_points: list[tuple[float, float, float]],
    manual_delay: float,
    timeline_adjustment_ms: int,
    maximum_drift: float,
    segmented_min_samples: int = 2,
    segmented_min_span_seconds: float = 120.0,
    spectral_min_samples: int = 1,
    audio_sync_coverage: float | None = None,
    minimum_audio_coverage: float = 0.80,
) -> dict[str, object]:
    if not decision.required:
        return {"required": False, "validated": True}
    samples: list[dict[str, float]] = []
    for sample in av_timeline.samples:
        if sample.score < VIDEO_MATCH_MIN_SCORE:
            continue
        audio_delay = _active_audio_delay(shift_points, sample.source_time, manual_delay)
        error = audio_delay - sample.video_delay + timeline_adjustment_ms / 1000
        samples.append(
            {
                "target_time": sample.target_time,
                "source_time": sample.source_time,
                "score": sample.score,
                "error_seconds": error,
            }
        )
    maximum_error = max((abs(item["error_seconds"]) for item in samples), default=None)
    target_span = (
        max(item["target_time"] for item in samples)
        - min(item["target_time"] for item in samples)
        if len(samples) >= 2
        else 0.0
    )
    segmented = bool(decision.validation.get("segmented_anchor_mapping"))
    spectral_preflight = decision.validation.get("spectral_tempo_probe", {})
    spectral_post = decision.validation.get("spectral_post_sync_validation", {})
    telecine_acoustic_preflight = decision.validation.get("telecine_acoustic_preflight", {})
    post_relative_speed = (
        spectral_post.get("relative_speed_factor")
        if isinstance(spectral_post, dict)
        else None
    )
    # Older reports had no explicit relative factor because they only ever
    # rendered real-time maps. Retain that representation for compatibility.
    if post_relative_speed is None and isinstance(spectral_post, dict):
        post_relative_speed = spectral_post.get("speed_factor")
    post_clock_matches_render = bool(
        post_relative_speed is not None
        and abs(float(post_relative_speed) - 1.0) <= max(maximum_drift / 10, 0.003)
    )
    deferred_to_tvrip_segments = bool(
        segmented
        and isinstance(telecine_acoustic_preflight, dict)
        and telecine_acoustic_preflight.get("enabled")
        and audio_sync_coverage is not None
        and audio_sync_coverage >= minimum_audio_coverage
        and isinstance(spectral_preflight, dict)
        and isinstance(spectral_post, dict)
        and (
            (
                spectral_preflight.get("fallback_accepted")
                and spectral_post.get("fallback_accepted")
            )
            or (
                # A real-time telecine preflight may be deliberately
                # inconclusive. The completed map can subsequently prove a
                # stable linear program clock, trigger one corrected render,
                # and then measure a unit residual in that rendered timeline.
                spectral_preflight.get("fallback_accepted")
                and spectral_post.get("reliable")
                and post_clock_matches_render
            )
            or (
                spectral_preflight.get("reliable")
                and spectral_post.get("fallback_accepted")
            )
            or (
                spectral_preflight.get("reliable")
                and spectral_post.get("reliable")
                and post_clock_matches_render
            )
        )
    )
    spectrally_verified = bool(
        segmented
        and isinstance(spectral_preflight, dict)
        and spectral_preflight.get("reliable")
        and isinstance(spectral_post, dict)
        and spectral_post.get("reliable")
        and post_clock_matches_render
        and audio_sync_coverage is not None
        and audio_sync_coverage >= minimum_audio_coverage
    )
    initial_anchor_count = sum(
        sample.score >= VIDEO_MATCH_MIN_SCORE for sample in decision.samples
    )
    minimum_samples = (
        spectral_min_samples
        if spectrally_verified
        else (segmented_min_samples if segmented else 3)
    )
    enough_samples = deferred_to_tvrip_segments or len(samples) >= minimum_samples
    enough_span = (
        deferred_to_tvrip_segments
        or not segmented
        or minimum_samples < 2
        or target_span >= segmented_min_span_seconds
    )
    # A reliable spectral preflight and post-sync probe are independent
    # common-original measurements across the feature.  In the segmented
    # TVRip path they deliberately permit one *post-map* video confirmation;
    # requiring three weak pre-sync visual anchors as well defeats that
    # approved evidence mode for telecined/remastered releases.  Retain the
    # three-anchor requirement for every other segmented path.
    enough_initial_evidence = (
        deferred_to_tvrip_segments
        or spectrally_verified
        or not segmented
        or initial_anchor_count >= 3
    )
    validated = (
        enough_samples
        and enough_span
        and enough_initial_evidence
        and (
            deferred_to_tvrip_segments
            or (maximum_error is not None and maximum_error <= maximum_drift)
        )
    )
    result: dict[str, object] = {
        "required": True,
        "validated": validated,
        "mode": (
            "telecine-acoustic-tvrip-deferred"
            if deferred_to_tvrip_segments
            else "segmented-spectral-audio-map"
            if spectrally_verified
            else ("segmented-audio-map" if segmented else "global-fps-map")
        ),
        "spectrally_verified": spectrally_verified,
        "deferred_to_tvrip_segments": deferred_to_tvrip_segments,
        "maximum_error_seconds": maximum_error,
        "allowed_error_seconds": maximum_drift,
        "minimum_required_samples": minimum_samples,
        "target_span_seconds": target_span,
        "minimum_target_span_seconds": segmented_min_span_seconds if segmented else None,
        "initial_content_anchor_count": initial_anchor_count,
        "audio_sync_coverage": audio_sync_coverage,
        "post_relative_speed_factor": post_relative_speed,
        "samples": samples,
    }
    decision.validation["post_map"] = result
    if not validated:
        detail = (
            f"found {len(samples)}/{minimum_samples} post-map video matches, "
            f"span {target_span:.3f}s, maximum error "
            f"{maximum_error:.3f}s"
            if maximum_error is not None
            else f"found {len(samples)}/{minimum_samples} post-map video matches"
        )
        result["reason"] = (
            "Synchronized shared-audio/video evidence was insufficient: "
            f"{detail}; allowed error {maximum_drift:.3f}s"
        )
        result["warnings"] = [
            "Keeping the best available experimental synchronization map for manual review"
        ]
    return result
