"""Experimental, fail-closed TVRip-to-master segment synchronization policy."""

from __future__ import annotations

import logging
import statistics
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

from .avsync import VIDEO_MATCH_FPS, _extract_frames, _match_window
from .errors import ProcessingError, TVRipValidationError, UserCancelledError
from .models import (
    DualMakerConfig,
    JobPlan,
    Track,
    TVRipInterval,
    TVRipSegment,
    TVRipSyncReport,
    TVRipValidationPoint,
    jsonable,
)
from .preprocess import _binary_audio_envelope, detect_black_intervals, envelope_similarity
from .runner import ToolRunner

if TYPE_CHECKING:
    from .sync.adapter import SyncResult

LOGGER = logging.getLogger("dualmaker")


def _video_summary(plan: JobPlan, side: str) -> dict[str, object]:
    asset = plan.normal if side == "master" else plan.dual
    video = asset.video_tracks[0] if asset.video_tracks else None
    ff_video = next(
        (
            stream
            for stream in asset.ffprobe.get("streams", [])
            if stream.get("codec_type") == "video"
        ),
        {},
    )
    return {
        "identity": jsonable(plan.identity),
        "path": str(asset.path),
        "duration": asset.duration,
        "fps": jsonable(asset.frame_rate),
        "resolution": video.properties.get("pixel_dimensions") if video else None,
        "aspect_ratio": ff_video.get("display_aspect_ratio"),
        "frame_count": ff_video.get("nb_frames"),
        "chapters": len(asset.chapters),
        "audio_tracks": jsonable(asset.audio_tracks),
    }


def analyze_tvrip_sources(
    plan: JobPlan,
    *,
    source_path: Path,
    master_path: Path,
    runner: ToolRunner,
) -> dict[str, object]:
    """Collect concise comparison metadata and inexpensive broadcast clues."""

    opening_window = min(180.0, plan.dual.duration, plan.normal.duration)
    source_black = detect_black_intervals(
        source_path,
        window=opening_window,
        minimum_duration=0.6,
        runner=runner,
    )
    master_black = detect_black_intervals(
        master_path,
        window=opening_window,
        minimum_duration=0.6,
        runner=runner,
    )
    return {
        "master": _video_summary(plan, "master"),
        "tvrip": _video_summary(plan, "tvrip"),
        "duration_difference_seconds": plan.dual.duration - plan.normal.duration,
        "opening_black_intervals": {
            "master": jsonable(master_black),
            "tvrip": jsonable(source_black),
        },
        "shared_reference_tracks": {
            "master": plan.normal_original.id,
            "tvrip": plan.dual_original.id,
            "language": plan.normal_original.effective_language,
        },
    }


def _bounded_bucket(
    bucket: tuple[float, float, float],
    *,
    source_duration: float,
    master_duration: float,
) -> tuple[float, float, float, float, float] | None:
    source_start, source_end, offset = (float(value) for value in bucket)
    source_start = max(source_start, 0.0)
    source_end = min(source_end, source_duration)
    master_start = source_start + offset
    master_end = source_end + offset
    if master_start < 0:
        source_start -= master_start
        master_start = 0.0
    if master_end > master_duration:
        source_end -= master_end - master_duration
        master_end = master_duration
    if source_end <= source_start or master_end <= master_start:
        return None
    return source_start, source_end, master_start, master_end, offset


def _coalesce_buckets(
    buckets: list[tuple[float, float, float, float, float]],
    sensitivity: float,
    *,
    preserve_source_boundaries: tuple[float, ...] = (),
) -> list[tuple[float, float, float, float, float]]:
    merged: list[tuple[float, float, float, float, float]] = []
    for bucket in sorted(buckets, key=lambda item: (item[2], item[0])):
        if not merged:
            merged.append(bucket)
            continue
        previous = merged[-1]
        source_gap = bucket[0] - previous[1]
        master_gap = bucket[2] - previous[3]
        if (
            not any(abs(bucket[0] - boundary) <= 0.01 for boundary in preserve_source_boundaries)
            and
            abs(bucket[4] - previous[4]) <= sensitivity
            and abs(source_gap) <= sensitivity
            and abs(master_gap) <= sensitivity
        ):
            merged[-1] = (
                previous[0],
                bucket[1],
                previous[2],
                bucket[3],
                statistics.median((previous[4], bucket[4])),
            )
        else:
            merged.append(bucket)
    return merged


def _split_segments(
    buckets: list[tuple[float, float, float, float, float]], maximum: float
) -> list[TVRipSegment]:
    segments: list[TVRipSegment] = []
    for source_start, source_end, master_start, master_end, offset in buckets:
        cursor_source = source_start
        cursor_master = master_start
        while cursor_master < master_end - 0.001:
            duration = min(maximum, master_end - cursor_master)
            segments.append(
                TVRipSegment(
                    index=len(segments) + 1,
                    source_start=cursor_source,
                    source_end=min(cursor_source + duration, source_end),
                    master_start=cursor_master,
                    master_end=cursor_master + duration,
                    offset_seconds=offset,
                )
            )
            cursor_source += duration
            cursor_master += duration
    return segments


def _validate_segment(
    segment: TVRipSegment,
    *,
    source_path: Path,
    master_path: Path,
    source_time_scale: float,
    timeline_adjustment: float,
    config: DualMakerConfig,
    work_dir: Path,
    runner: ToolRunner,
    minimum_confidence: float | None = None,
) -> None:
    if segment.duration < config.tvrip_min_segment_seconds:
        segment.status = "rejected"
        segment.operation = "Rejected: shorter than configured minimum segment duration"
        return
    window = min(config.tvrip_validation_window_seconds, segment.duration * 0.25)
    radius = config.tvrip_validation_search_seconds
    for point_index, position in enumerate(config.tvrip_validation_positions):
        center = segment.master_start + segment.duration * position
        master_start = max(center - window / 2, segment.master_start)
        master_start = min(master_start, max(segment.master_end - window, segment.master_start))
        predicted_source = master_start - segment.offset_seconds
        source_search_start = max(predicted_source - radius, segment.source_start)
        source_search_end = min(predicted_source + window + radius, segment.source_end)
        if source_search_end - source_search_start < window:
            continue
        source_file = work_dir / f"tvrip-segment-{segment.index}-{point_index}-source.gray"
        master_file = work_dir / f"tvrip-segment-{segment.index}-{point_index}-master.gray"
        try:
            source_frames = _extract_frames(
                source_path,
                source_file,
                start=source_search_start,
                duration=source_search_end - source_search_start,
                runner=runner,
                time_scale=source_time_scale,
            )
            master_frames = _extract_frames(
                master_path,
                master_file,
                start=master_start,
                duration=window,
                runner=runner,
            )
            match = _match_window(source_frames, master_frames)
            if match is None:
                continue
            frame_index, score = match
            actual_source = source_search_start + frame_index / VIDEO_MATCH_FPS
            video_offset = master_start - actual_source
            residual = segment.offset_seconds + timeline_adjustment - video_offset
            segment.validation_points.append(
                TVRipValidationPoint(
                    position=position,
                    source_time=actual_source,
                    master_time=master_start,
                    confidence=max(min(float(score), 1.0), 0.0),
                    residual_seconds=residual,
                )
            )
        except ProcessingError:
            LOGGER.debug("Could not validate TVRip segment %d point %.2f", segment.index, position)
        finally:
            source_file.unlink(missing_ok=True)
            master_file.unlink(missing_ok=True)

    required_points = min(3, len(config.tvrip_validation_positions))
    residual_inliers = [
        point
        for point in segment.validation_points
        if abs(point.residual_seconds) <= config.tvrip_max_residual_seconds
    ]
    if residual_inliers:
        segment.confidence = statistics.median(point.confidence for point in residual_inliers)
        segment.residual_seconds = max(abs(point.residual_seconds) for point in residual_inliers)
    if len(segment.validation_points) < required_points:
        segment.status = "ambiguous"
        segment.operation = "Ambiguous: too few independent validation points"
    elif len(residual_inliers) < max(2, required_points - 1):
        segment.status = "rejected"
        segment.operation = "Rejected: fewer than two video points agree with the audio map"
    elif segment.confidence < (
        config.tvrip_min_segment_confidence
        if minimum_confidence is None
        else minimum_confidence
    ):
        segment.status = "rejected"
        segment.operation = "Rejected: content confidence below configured minimum"
    else:
        segment.status = "accepted"
        segment.operation = "Trim TVRip-only material and synchronize matching content"


def _merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + 0.01:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _complement(ranges: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in _merge_ranges(ranges):
        if start > cursor + 0.01:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration - 0.01:
        gaps.append((cursor, duration))
    return gaps


def _source_only_classification(
    start: float, end: float, duration: float, commercial: float
) -> str:
    length = end - start
    edge_window = min(120.0, max(duration * 0.08, 10.0))
    if start <= edge_window:
        return "probable pre-roll, alternate recap, opening, or station material"
    if end >= duration - edge_window:
        return "probable preview, alternate credits, or station material"
    if length >= commercial:
        return "probable commercial/broadcast break"
    return "short broadcaster-only edit or bumper"


def _refresh_report(report: TVRipSyncReport, master_duration: float) -> None:
    accepted = [segment for segment in report.segments if segment.status == "accepted"]
    report.accepted_segments = len(accepted)
    report.ambiguous_segments = sum(segment.status == "ambiguous" for segment in report.segments)
    report.rejected_segments = sum(segment.status == "rejected" for segment in report.segments)
    accepted_ranges = _merge_ranges(
        [(segment.master_start, segment.master_end) for segment in accepted]
    )
    covered = sum(end - start for start, end in accepted_ranges)
    report.coverage = min(covered / max(master_duration, 1.0), 1.0)
    report.master_only = [
        TVRipInterval(
            start,
            end,
            "master-only",
            (
                "content absent or not validated in TVRip"
                if report.workflow == "tvrip"
                else "content absent from the DUAL dub/reference timeline"
            ),
        )
        for start, end in _complement(accepted_ranges, master_duration)
        if end - start >= max(report.minimum_master_gap_seconds, 0.01)
    ]
    weighted = sum(segment.confidence * segment.duration for segment in accepted)
    report.source_match_confidence = weighted / covered if covered else 0.0


def _telecine_acoustic_map_evidence(
    plan: JobPlan,
    sync: SyncResult,
    *,
    bounded: list[tuple[float, float, float, float, float]],
    master_duration: float,
    config: DualMakerConfig,
    workflow: str,
) -> dict[str, object]:
    """Decide whether the explicit telecine fallback has a complete audio map.

    This is deliberately much narrower than the normal TVRip validator. It is
    reachable only after the operator enabled the segmented workflow and the
    FPS stage classified the input as a 5:4 telecine candidate. In that case a
    remaster may be visually incomparable to the broadcast encode, whereas
    Milksync has already correlated the common-original audio. We require that
    map to be complete and multi-point. A measured bounded speed correction is
    permitted too, but its *rendered* clock must agree with the post-sync
    observation and every kept segment is checked acoustically.
    """

    preflight = plan.fps.validation.get("telecine_acoustic_preflight", {})
    spectral_preflight = plan.fps.validation.get("spectral_tempo_probe", {})
    spectral_post = plan.fps.validation.get("spectral_post_sync_validation", {})
    post_relative_speed = (
        spectral_post.get("relative_speed_factor")
        if isinstance(spectral_post, dict)
        else None
    )
    if post_relative_speed is None and isinstance(spectral_post, dict):
        post_relative_speed = spectral_post.get("speed_factor")
    post_clock_matches_render = bool(
        post_relative_speed is not None and abs(float(post_relative_speed) - 1.0) <= 0.003
    )
    spectral_evidence = bool(
        isinstance(spectral_preflight, dict)
        and isinstance(spectral_post, dict)
        and (
            (
                spectral_preflight.get("fallback_accepted")
                and spectral_post.get("fallback_accepted")
            )
            or (
                # The first telecine pass can defer its clock decision. Once
                # the complete map measures a stable drift and the corrected
                # render reports a unit residual, it is stronger evidence than
                # the deliberately sparse preflight.
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
    mapped_ranges = _merge_ranges([(item[2], item[3]) for item in bounded])
    mapped_seconds = sum(end - start for start, end in mapped_ranges)
    mapped_coverage = mapped_seconds / max(master_duration, 1.0)
    sync_coverage = sync.sync_coverage
    valid = bool(
        workflow == "tvrip"
        and (config.allow_tvrip_segment_sync or config.interactive)
        and isinstance(preflight, dict)
        and preflight.get("enabled")
        and spectral_evidence
        and abs(sync.speed_correction_factor - 1.0) <= config.tvrip_max_speed_adjustment
        and len(sync.shift_points) >= 3
        and sync_coverage is not None
        and sync_coverage >= config.tvrip_min_coverage
        and mapped_coverage >= config.tvrip_min_coverage
    )
    return {
        "enabled": valid,
        "reason": (
            "Explicit telecine TVRip workflow has a complete, multi-point common-original "
            "acoustic map; visual remaster comparison is supplementary"
            if valid
            else "Telecine acoustic-map conditions were not all satisfied"
        ),
        "sync_coverage": sync_coverage,
        "map_point_count": len(sync.shift_points),
        "mapped_coverage": mapped_coverage,
        "mapped_seconds": mapped_seconds,
        "speed_correction_factor": sync.speed_correction_factor,
        "post_relative_speed_factor": post_relative_speed,
        "spectral_evidence": spectral_evidence,
    }


def _accept_segments_from_telecine_acoustic_map(
    segments: list[TVRipSegment],
    *,
    config: DualMakerConfig,
) -> None:
    """Mark mapped intervals as audio-validated without erasing video evidence."""

    for segment in segments:
        segment.status = "accepted"
        segment.confidence = max(
            segment.confidence,
            config.tvrip_spectral_min_segment_confidence,
        )
        segment.operation = (
            "Accepted: common-original acoustic map validated this telecine TVRip interval; "
            "cross-release visual measurements are supplementary"
        )


def _telecine_acoustic_probe_positions(
    duration: float,
    window: float,
    config: DualMakerConfig,
) -> tuple[float, ...]:
    """Return bounded probe starts without leaving a long unaudited run.

    The broad validation positions are useful for short buckets, but a
    300-second open-ended bucket can otherwise hide an alternate scene between
    them.  Always include both edges as well: a release-only scene is most
    likely to begin directly after a Milksync bucket boundary.  Add probes
    where the distance between those fixed positions would exceed the
    configured gap.
    """

    usable = max(duration - window, 0.0)
    if usable <= 0:
        return (0.0,)
    configured = sorted(
        {
            round(usable * position, 6)
            for position in config.tvrip_validation_positions
        }
    )
    positions: list[float] = [0.0, usable, *configured]
    boundaries = sorted(set(positions))
    for left, right in pairwise(boundaries):
        cursor = left
        while right - cursor > config.tvrip_acoustic_segment_max_gap_seconds:
            cursor += config.tvrip_acoustic_segment_max_gap_seconds
            positions.append(round(cursor, 6))
    return tuple(sorted(set(positions))) or (0.0,)


def _common_original_probe(
    plan: JobPlan,
    sync: SyncResult,
    *,
    source_path: Path,
    master_path: Path,
    source_start: float,
    master_start: float,
    window: float,
    runner: ToolRunner,
) -> dict[str, object]:
    """Compare a local mapped window without turning silence into a false cut.

    The audio envelope intentionally returns an empty array for silence and
    for a failed decode.  Treating either as an error for a whole 300-second
    bucket caused the inverse failure: a short quiet moment replaced a valid
    Portuguese scene with the master original.  A one-sided empty probe is
    still evidence that this *local* mapping is unsafe, whereas matching
    silence is neutral and can retain the mapped Portuguese track.

    The caller converts one-sided/unrelated probes into tightly padded master
    fallback intervals.  It never expands one uncertain five-second probe
    into an unrelated complete bucket.
    """

    result: dict[str, object] = {
        "source_start": source_start,
        "master_start": master_start,
        "window_seconds": window,
    }
    try:
        source_audio = _binary_audio_envelope(
            source_path,
            plan.dual_original,
            source_start * sync.speed_correction_factor,
            duration=window * sync.speed_correction_factor,
            tempo=sync.speed_correction_factor,
            runner=runner,
        )
    except ProcessingError as error:
        source_audio = []
        result["source_error"] = str(error)
    try:
        master_audio = _binary_audio_envelope(
            master_path,
            plan.normal_original,
            master_start,
            duration=window,
            runner=runner,
        )
    except ProcessingError as error:
        master_audio = []
        result["master_error"] = str(error)

    source_present = bool(len(source_audio))
    master_present = bool(len(master_audio))
    result.update(source_present=source_present, master_present=master_present)
    if not source_present and not master_present:
        # Both tracks are silent/unavailable for this small window.  There is
        # no audible material to substitute, so preserve the established map
        # rather than replacing a much larger valid Portuguese range.
        result.update(state="both-silent", similarity=1.0)
    elif not source_present:
        result.update(state="source-only-master-audible", similarity=-1.0)
    elif not master_present:
        result.update(state="master-only-source-audible", similarity=-1.0)
    else:
        result.update(
            state="comparable",
            similarity=envelope_similarity(source_audio, master_audio),
        )
    return result


def _telecine_segment_slice(
    segment: TVRipSegment,
    master_start: float,
    master_end: float,
    *,
    accepted: bool,
) -> TVRipSegment:
    """Project one verified or rejected master slice back to source time."""

    master_span = max(segment.master_end - segment.master_start, 0.000_001)
    source_span = max(segment.source_end - segment.source_start, 0.0)
    source_scale = source_span / master_span
    source_start = segment.source_start + (master_start - segment.master_start) * source_scale
    source_end = segment.source_start + (master_end - segment.master_start) * source_scale
    validation_points = [
        point
        for point in segment.validation_points
        if master_start - 0.001 <= point.master_time <= master_end + 0.001
    ]
    return replace(
        segment,
        source_start=max(segment.source_start, min(source_start, segment.source_end)),
        source_end=max(segment.source_start, min(source_end, segment.source_end)),
        master_start=master_start,
        master_end=master_end,
        offset_seconds=master_start - source_start,
        confidence=segment.confidence if accepted else 0.0,
        residual_seconds=segment.residual_seconds if accepted else None,
        status="accepted" if accepted else "rejected",
        operation=(
            "Accepted: local common-original audio proved this telecine TVRip interval"
            if accepted
            else (
                "Rejected: local common-original audio did not prove this telecine "
                "TVRip interval; master fallback will replace it"
            )
        ),
        validation_points=validation_points,
    )


def _split_telecine_segment_at_rejections(
    segment: TVRipSegment,
    rejected_master_ranges: list[tuple[float, float]],
) -> list[TVRipSegment]:
    """Keep verified portions while exposing failed local probes as master gaps."""

    clipped = _merge_ranges(
        [
            (
                max(segment.master_start, start),
                min(segment.master_end, end),
            )
            for start, end in rejected_master_ranges
        ]
    )
    if not clipped:
        return [segment]
    boundaries = sorted(
        {
            segment.master_start,
            segment.master_end,
            *(value for interval in clipped for value in interval),
        }
    )
    parts: list[TVRipSegment] = []
    for start, end in pairwise(boundaries):
        if end - start <= 0.001:
            continue
        midpoint = (start + end) / 2
        rejected = any(left <= midpoint <= right for left, right in clipped)
        parts.append(
            _telecine_segment_slice(segment, start, end, accepted=not rejected)
        )
    return parts or [segment]


def _validate_telecine_acoustic_segments(
    plan: JobPlan,
    sync: SyncResult,
    report: TVRipSyncReport,
    *,
    source_path: Path,
    master_path: Path,
    config: DualMakerConfig,
    runner: ToolRunner,
) -> dict[str, object]:
    """Audit every usable telecine map bucket with the common original audio.

    A complete Milksync map proves that several anchors agree, but it cannot
    prove that every interval between those anchors exists in both releases.
    In particular, an HDTV can contain a short scene or alternate ending that
    a WEB/BD master omits.  The explicit telecine fallback deliberately lets
    the acoustic map supersede *visual* mismatches; it must not turn that
    allowance into permission to copy an acoustically unrelated interval.

    This check therefore uses the already selected common-original tracks at
    regular bounded positions, including both edges, in each independently
    mapped bucket. Matching silence is retained: it is not proof of a
    different scene and replacing a whole bucket because of it can remove a
    valid Portuguese scene.

    A local envelope comparison is deliberately weaker evidence than the
    complete Milksync map.  In the normal authorized TVRip mode it is recorded
    as a diagnostic only: different mixes, compression and music can make a
    short window look unrelated even when the Portuguese dub is present and
    correctly mapped.  Replacing that short window with English causes the
    audible dropouts this guard is meant to prevent.  ``--tvrip-strict-
    validation`` retains the previous fail-closed behaviour for operators who
    prefer local mismatches to become master-original fallback intervals.
    """

    evidence: dict[str, object] = {
        "enabled": False,
        "checked": False,
        "action": "not-applicable",
        "reason": "No telecine acoustic-map interval requires local validation",
        "segments": [],
    }
    if not config.tvrip_acoustic_segment_validation:
        evidence.update(
            action="disabled",
            reason="Per-segment telecine acoustic validation is disabled",
        )
        return evidence
    if not report.segments:
        return evidence
    if not source_path.is_file() or not master_path.is_file():
        evidence.update(
            action="unverifiable",
            reason="TVRip source or master file is unavailable; mapped intervals were retained",
        )
        return evidence

    evidence["enabled"] = True
    evidence["checked"] = True
    diagnostic_only = (
        config.allow_tvrip_segment_sync
        and config.tvrip_continue_on_validation_warnings
    )
    evidence["fallback_mode"] = (
        "diagnostic-only" if diagnostic_only else "strict-local-fallback"
    )
    segment_evidence: list[dict[str, object]] = []
    rejected = 0
    suspected = 0
    unverifiable = 0
    checked = 0
    partial_replacement = False
    rewritten_segments: list[TVRipSegment] = []
    for segment in report.segments:
        item: dict[str, object] = {
            "index": segment.index,
            "source_start": segment.source_start,
            "source_end": segment.source_end,
            "master_start": segment.master_start,
            "master_end": segment.master_end,
        }
        duration = min(segment.source_end - segment.source_start, segment.duration)
        window = min(config.tvrip_acoustic_segment_window_seconds, duration)
        if window < config.tvrip_acoustic_segment_min_seconds:
            item.update(
                action="unverifiable",
                reason="Mapped interval is shorter than the acoustic-validation minimum",
            )
            if config.tvrip_acoustic_segment_require_proof:
                if diagnostic_only:
                    unverifiable += 1
                    rewritten_segments.append(segment)
                    item.update(
                        action="retained-with-warning",
                        fallback_suppressed=True,
                    )
                else:
                    rejected += 1
                    rewritten_segments.extend(
                        _split_telecine_segment_at_rejections(
                            segment, [(segment.master_start, segment.master_end)]
                        )
                    )
                    report.tvrip_only.append(
                        TVRipInterval(
                            segment.source_start,
                            segment.source_end,
                            "tvrip-only",
                            "short mapped broadcast interval without sufficient local audio proof",
                        )
                    )
                    item["action"] = "replaced-with-fallback"
            else:
                rewritten_segments.append(segment)
            segment_evidence.append(item)
            continue

        samples: list[dict[str, object]] = []
        source_usable = max(segment.source_end - segment.source_start - window, 0.0)
        master_usable = max(segment.duration - window, 0.0)
        probe_positions = _telecine_acoustic_probe_positions(segment.duration, window, config)
        for position in probe_positions:
            relative_position = position / max(master_usable, 0.000_001)
            source_start = segment.source_start + source_usable * relative_position
            master_start = segment.master_start + position
            samples.append(
                _common_original_probe(
                    plan,
                    sync,
                    source_path=source_path,
                    master_path=master_path,
                    source_start=source_start,
                    master_start=master_start,
                    window=window,
                    runner=runner,
                )
            )

        if not samples:
            item.update(
                action="unverifiable",
                reason=(
                    "Could not extract enough common-original audio for local proof"
                ),
            )
            if config.tvrip_acoustic_segment_require_proof:
                if diagnostic_only:
                    unverifiable += 1
                    rewritten_segments.append(segment)
                    item.update(
                        action="retained-with-warning",
                        fallback_suppressed=True,
                    )
                else:
                    rejected += 1
                    rewritten_segments.extend(
                        _split_telecine_segment_at_rejections(
                            segment, [(segment.master_start, segment.master_end)]
                        )
                    )
                    report.tvrip_only.append(
                        TVRipInterval(
                            segment.source_start,
                            segment.source_end,
                            "tvrip-only",
                            "mapped broadcast interval without decodable local common-original proof",
                        )
                    )
                    item["action"] = "replaced-with-fallback"
            else:
                rewritten_segments.append(segment)
            segment_evidence.append(item)
            continue

        checked += 1
        similarity = min(float(sample["similarity"]) for sample in samples)
        failed = [
            sample
            for sample in samples
            if float(sample["similarity"]) < config.tvrip_acoustic_segment_min_similarity
        ]
        states: dict[str, int] = {}
        for sample in samples:
            state = str(sample["state"])
            states[state] = states.get(state, 0) + 1
        item.update(
            samples=samples,
            probe_count=len(samples),
            minimum_similarity=similarity,
            maximum_gap_seconds=config.tvrip_acoustic_segment_max_gap_seconds,
            probe_states=states,
        )
        if not failed:
            item["action"] = "retained"
            rewritten_segments.append(segment)
            segment_evidence.append(item)
            continue

        if len(failed) == len(samples):
            rejected_master_ranges = [(segment.master_start, segment.master_end)]
        else:
            padding = config.tvrip_acoustic_segment_rejection_padding_seconds
            rejected_master_ranges = [
                (
                    max(segment.master_start, float(sample["master_start"]) - padding),
                    min(segment.master_end, float(sample["master_start"]) + window + padding),
                )
                for sample in failed
            ]
        if diagnostic_only:
            # A bounded map is the evidence that drives audio selection.  A
            # local probe is still useful in the report, but must not replace
            # an otherwise mapped Portuguese interval with the master audio.
            suspected += 1
            rewritten_segments.append(segment)
            item.update(
                action="retained-with-warning",
                fallback_suppressed=True,
                suspected_master_ranges=[
                    {"start": start, "end": end}
                    for start, end in _merge_ranges(rejected_master_ranges)
                ],
            )
            segment_evidence.append(item)
            continue

        rejected += 1
        replacements = _split_telecine_segment_at_rejections(segment, rejected_master_ranges)
        rewritten_segments.extend(replacements)
        rejected_parts = [part for part in replacements if part.status == "rejected"]
        for part in rejected_parts:
            report.tvrip_only.append(
                TVRipInterval(
                    part.source_start,
                    part.source_end,
                    "tvrip-only",
                    "unmatched broadcast scene rejected by local common-original audio validation",
                )
            )
        item["action"] = (
            "replaced-with-fallback"
            if len(rejected_parts) == len(replacements)
            else "partially-replaced-with-fallback"
        )
        partial_replacement = partial_replacement or len(rejected_parts) < len(replacements)
        item["rejected_master_ranges"] = [
            {"start": part.master_start, "end": part.master_end}
            for part in rejected_parts
        ]
        segment_evidence.append(item)

    report.segments = rewritten_segments
    for index, segment in enumerate(report.segments, start=1):
        segment.index = index
    evidence["segments"] = segment_evidence
    evidence["checked_segments"] = checked
    evidence["rejected_segments"] = rejected
    evidence["suspected_segments"] = suspected
    evidence["unverifiable_segments"] = unverifiable
    evidence["minimum_similarity"] = config.tvrip_acoustic_segment_min_similarity
    evidence["window_seconds"] = config.tvrip_acoustic_segment_window_seconds
    evidence["rejection_padding_seconds"] = (
        config.tvrip_acoustic_segment_rejection_padding_seconds
    )
    evidence["require_proof"] = config.tvrip_acoustic_segment_require_proof
    if diagnostic_only and (suspected or unverifiable):
        evidence["action"] = "retained-with-warnings"
        evidence["reason"] = (
            "Local common-original probes were inconclusive or mismatched, but the "
            "Milksync-mapped Portuguese intervals were retained; use --tvrip-strict-"
            "validation to turn these diagnostics into master-original fallback"
        )
        report.warnings.append(
            "Local TVRip acoustic probes flagged "
            f"{suspected + unverifiable} mapped interval(s), but retained the mapped "
            "Portuguese audio to avoid false English dropouts"
        )
    else:
        evidence["action"] = (
            "partially-replaced-with-fallback"
            if partial_replacement
            else ("replaced-with-fallback" if rejected else "retained")
        )
        evidence["reason"] = (
            "One or more mapped TVRip intervals failed local common-original audio validation"
            if rejected
            else "Every comparable mapped TVRip interval matched the master common-original audio"
        )
    return evidence


def _validate_open_ended_terminal_tail(
    plan: JobPlan,
    sync: SyncResult,
    report: TVRipSyncReport,
    *,
    source_path: Path,
    master_path: Path,
    source_duration: float,
    master_duration: float,
    config: DualMakerConfig,
    runner: ToolRunner,
) -> dict[str, object]:
    """Prove that an unanchored final source bucket really matches the master.

    Milksync correctly represents its final map range as open ended: there is
    no subsequent correspondence point that could bound it. That is normally
    fine, but a broadcast source can contain a different preview or credits
    tail. Do not infer that the tail matches merely because it follows the
    last anchor. Compare the common-original audio over its final bounded
    window. Unlike an ordinary bounded map interval, an open-ended tail has
    no following anchor to rescue it: failed proof always becomes the
    configured master fallback, including in best-effort TVRip mode.

    The common-original probe accounts for an approved speed correction before
    comparing timestamps, so this guard is valid for both real-time telecine
    and the experimental corrected-clock path.
    """

    evidence: dict[str, object] = {
        "enabled": False,
        "checked": False,
        "action": "not-applicable",
        "reason": "No open-ended TVRip terminal bucket requires separate validation",
    }
    if not config.tvrip_terminal_tail_validation:
        evidence.update(action="disabled", reason="Terminal-tail validation is disabled")
        return evidence
    if not sync.sync_buckets or not report.segments:
        return evidence

    # ``turn_audio_shift_points_to_audio_segments`` uses a large sentinel for
    # the last bucket. Any normally bounded final bucket does not need this
    # additional check.
    final_bucket = max(sync.sync_buckets, key=lambda item: float(item[0]))
    if float(final_bucket[1]) <= source_duration + 0.01:
        return evidence
    terminal_source_start = max(float(final_bucket[0]), 0.0)
    terminal_segments = [
        segment
        for segment in report.segments
        if segment.source_start >= terminal_source_start - 0.01
    ]
    if not terminal_segments:
        return evidence
    terminal_source_end = max(segment.source_end for segment in terminal_segments)
    terminal_master_start = min(segment.master_start for segment in terminal_segments)
    terminal_master_end = max(segment.master_end for segment in terminal_segments)
    terminal_duration = min(
        terminal_source_end - terminal_source_start,
        terminal_master_end - terminal_master_start,
    )
    window = min(config.tvrip_terminal_tail_window_seconds, terminal_duration)
    evidence.update(
        {
            "enabled": True,
            "source_start": terminal_source_start,
            "source_end": terminal_source_end,
            "master_start": terminal_master_start,
            "master_end": terminal_master_end,
            "window_seconds": window,
            "minimum_similarity": config.tvrip_terminal_tail_min_similarity,
        }
    )
    if window < config.tvrip_terminal_tail_min_seconds:
        evidence.update(
            action="unverifiable",
            reason=(
                "The bounded terminal tail is shorter than the configured minimum; "
                "it was retained rather than guessed"
            ),
        )
        return evidence

    if not source_path.is_file() or not master_path.is_file():
        evidence.update(
            action="unverifiable",
            reason="Terminal source or master file is unavailable; tail was retained",
        )
        return evidence

    # A long unanchored tail needs proof throughout, including its edges, not
    # merely a good first few seconds.  Reuse the per-bucket maximum-gap rule
    # so a preview inserted between broad anchors cannot leak into the final
    # dub.
    samples: list[dict[str, object]] = []
    source_span = terminal_source_end - terminal_source_start
    master_span = terminal_master_end - terminal_master_start
    usable = max(terminal_duration - window, 0.0)
    for position in _telecine_acoustic_probe_positions(terminal_duration, window, config):
        relative_position = position / max(usable, 0.000_001)
        source_start = terminal_source_start + max(source_span - window, 0.0) * relative_position
        master_start = terminal_master_start + max(master_span - window, 0.0) * relative_position
        samples.append(
            _common_original_probe(
                plan,
                sync,
                source_path=source_path,
                master_path=master_path,
                source_start=source_start,
                master_start=master_start,
                window=window,
                runner=runner,
            )
        )

    similarity = min(float(sample["similarity"]) for sample in samples)
    failed = [
        sample
        for sample in samples
        if float(sample["similarity"]) < config.tvrip_terminal_tail_min_similarity
    ]
    states: dict[str, int] = {}
    for sample in samples:
        state = str(sample["state"])
        states[state] = states.get(state, 0) + 1
    evidence.update(
        checked=True,
        samples=samples,
        similarity=similarity,
        probe_states=states,
        maximum_gap_seconds=config.tvrip_acoustic_segment_max_gap_seconds,
    )
    if not failed:
        evidence.update(
            action="retained",
            reason="Open-ended terminal source bucket matches the master common-original audio",
        )
        return evidence

    if len(failed) == len(samples):
        rejected_master_ranges = [(terminal_master_start, terminal_master_end)]
    else:
        padding = config.tvrip_acoustic_segment_rejection_padding_seconds
        rejected_master_ranges = [
            (
                max(terminal_master_start, float(sample["master_start"]) - padding),
                min(terminal_master_end, float(sample["master_start"]) + window + padding),
            )
            for sample in failed
        ]

    terminal_ids = {id(segment) for segment in terminal_segments}
    rewritten: list[TVRipSegment] = []
    rejected_parts: list[TVRipSegment] = []
    for segment in report.segments:
        if id(segment) not in terminal_ids or segment.status != "accepted":
            rewritten.append(segment)
            continue
        replacements = _split_telecine_segment_at_rejections(
            segment,
            rejected_master_ranges,
        )
        rewritten.extend(replacements)
        rejected_parts.extend(
            part for part in replacements if part.status == "rejected"
        )
    if not rejected_parts:
        # A preceding local guard already rejected this entire range. Keep
        # that decision rather than resurrecting it just because the terminal
        # guard found another mismatch.
        rejected_parts = [
            segment for segment in terminal_segments if segment.status == "rejected"
        ]
    report.segments = rewritten
    for index, segment in enumerate(report.segments, start=1):
        segment.index = index
    for part in rejected_parts:
        report.tvrip_only.append(
            TVRipInterval(
                part.source_start,
                part.source_end,
                "tvrip-only",
                "unmatched trailing broadcast/preview material rejected by common-original audio",
            )
        )
    rejected_coverage = _merge_ranges(
        [(part.master_start, part.master_end) for part in rejected_parts]
    )
    fully_replaced = (
        len(rejected_coverage) == 1
        and rejected_coverage[0][0] <= terminal_master_start + 0.001
        and rejected_coverage[0][1] >= terminal_master_end - 0.001
    )
    evidence.update(
        action=(
            "replaced-with-fallback"
            if fully_replaced
            else "partially-replaced-with-fallback"
        ),
        rejected_master_ranges=[
            {"start": part.master_start, "end": part.master_end}
            for part in rejected_parts
        ],
        reason="Open-ended terminal source bucket contains locally unmatched common-original audio",
    )
    return evidence


def build_tvrip_sync_report(
    plan: JobPlan,
    sync: SyncResult,
    *,
    source_path: Path,
    master_path: Path,
    config: DualMakerConfig,
    work_dir: Path,
    runner: ToolRunner,
    workflow: str = "tvrip",
    minimum_master_gap_seconds: float = 0.01,
) -> TVRipSyncReport:
    source_duration = max(
        (plan.dual.duration - plan.dual_trim) / max(sync.speed_correction_factor, 0.000001),
        0.0,
    )
    master_duration = max(plan.normal.duration - plan.normal_trim, 0.0)
    report = TVRipSyncReport(
        approved=config.allow_tvrip_segment_sync or config.interactive,
        source_analysis=analyze_tvrip_sources(
            plan,
            source_path=source_path,
            master_path=master_path,
            runner=runner,
        ),
        fallback=config.tvrip_fallback,
        speed_correction=sync.speed_correction_factor,
        workflow=workflow,  # type: ignore[arg-type]
        minimum_master_gap_seconds=minimum_master_gap_seconds,
    )
    spectral_preflight = plan.fps.validation.get("spectral_tempo_probe", {})
    spectral_post = plan.fps.validation.get("spectral_post_sync_validation", {})
    spectrally_verified = bool(
        isinstance(spectral_preflight, dict)
        and spectral_preflight.get("reliable")
        and isinstance(spectral_post, dict)
        and spectral_post.get("reliable")
        and abs(
            float(
                spectral_post.get("relative_speed_factor")
                or spectral_post.get("speed_factor")
                or 0.0
            )
            - 1.0
        )
        <= 0.003
    )
    report.source_analysis["spectrally_verified_timing"] = spectrally_verified
    bounded = [
        value
        for bucket in sync.sync_buckets
        if (
            value := _bounded_bucket(
                bucket,
                source_duration=source_duration,
                master_duration=master_duration,
            )
        )
        is not None
    ]
    # A map bucket is a separate Milksync hypothesis.  Do not coalesce its
    # boundary away before the local acoustic guard has a chance to reject an
    # unmatched HDTV-only scene between otherwise valid anchors.
    source_bucket_starts = tuple(
        max(float(bucket[0]), 0.0)
        for bucket in sync.sync_buckets
    )
    bounded = _coalesce_buckets(
        bounded,
        config.tvrip_break_sensitivity_seconds,
        preserve_source_boundaries=source_bucket_starts,
    )
    report.segments = _split_segments(bounded, config.tvrip_max_segment_seconds)
    telecine_acoustic_map = _telecine_acoustic_map_evidence(
        plan,
        sync,
        bounded=bounded,
        master_duration=master_duration,
        config=config,
        workflow=workflow,
    )
    report.source_analysis["telecine_acoustic_map_validation"] = telecine_acoustic_map
    skip_visual_segment_validation = bool(
        telecine_acoustic_map["enabled"]
        and config.allow_tvrip_segment_sync
        and config.tvrip_continue_on_validation_warnings
    )
    timeline_adjustment = sync.timeline_adjustment_ms / 1000
    for segment in report.segments:
        segment.speed_factor = sync.speed_correction_factor
        if not skip_visual_segment_validation:
            _validate_segment(
                segment,
                source_path=source_path,
                master_path=master_path,
                source_time_scale=sync.speed_correction_factor,
                timeline_adjustment=timeline_adjustment,
                config=config,
                work_dir=work_dir,
                runner=runner,
                minimum_confidence=(
                    config.tvrip_spectral_min_segment_confidence
                    if spectrally_verified
                    else config.tvrip_min_segment_confidence
                ),
            )

    if telecine_acoustic_map["enabled"]:
        # A cut-heavy acoustic map can contain hundreds of independent
        # Milksync buckets. Re-running several expensive video extractions for
        # every bucket both delays the job dramatically and provides weaker
        # evidence than the accepted common-original map. The map remains the
        # selection authority; the acoustic diagnostic below is kept in the
        # report without changing a mapped dub into English.
        report.source_analysis["video_segment_validation"] = (
            {
                "enabled": False,
                "action": "skipped",
                "segment_count": len(report.segments),
                "reason": (
                    "Explicit common-original telecine map is authoritative; skipped "
                    "per-segment visual extraction to avoid repeated slow, weaker "
                    "cross-release comparisons"
                ),
            }
            if skip_visual_segment_validation
            else {
                "enabled": True,
                "action": "completed",
                "segment_count": len(report.segments),
                "reason": (
                    "Strict TVRip validation also ran per-segment visual checks before "
                    "the common-original telecine map was accepted"
                ),
            }
        )
        _accept_segments_from_telecine_acoustic_map(report.segments, config=config)
        report.warnings.append(
            "Visual remaster/TVRip comparison was inconclusive; accepted the explicit "
            "real-time telecine map from common-original acoustic synchronization"
        )
        report.source_analysis["acoustic_segment_validation"] = (
            _validate_telecine_acoustic_segments(
                plan,
                sync,
                report,
                source_path=source_path,
                master_path=master_path,
                config=config,
                runner=runner,
            )
        )
    else:
        report.source_analysis["video_segment_validation"] = {
            "enabled": True,
            "action": "completed",
            "segment_count": len(report.segments),
            "reason": "Per-segment visual validation is required without an explicit telecine audio map",
        }
        report.source_analysis["acoustic_segment_validation"] = {
            "enabled": False,
            "checked": False,
            "action": "not-applicable",
            "reason": "Local acoustic segment validation is used only by the explicit telecine fallback",
            "segments": [],
        }

    report.source_analysis["terminal_tail_validation"] = _validate_open_ended_terminal_tail(
        plan,
        sync,
        report,
        source_path=source_path,
        master_path=master_path,
        source_duration=source_duration,
        master_duration=master_duration,
        config=config,
        runner=runner,
    )

    for raw_start, raw_end in sync.delete_buckets:
        start = max(float(raw_start), 0.0)
        end = min(float(raw_end), source_duration)
        if end - start <= 0.01:
            continue
        report.tvrip_only.append(
            TVRipInterval(
                start,
                end,
                "tvrip-only",
                _source_only_classification(
                    start,
                    end,
                    source_duration,
                    config.tvrip_commercial_min_seconds,
                ),
            )
        )
    _refresh_report(report, master_duration)
    report.source_analysis["master_gap_recovery"] = _recover_mapped_master_gaps(
        plan,
        sync,
        report,
        source_path=source_path,
        master_path=master_path,
        source_duration=source_duration,
        master_duration=master_duration,
        config=config,
        runner=runner,
    )
    # Recovered gaps become accepted pieces backed by the original DUAL dub.
    # The remaining complement is the only interval eligible for fallback.
    _refresh_report(report, master_duration)
    report.source_analysis["detected_tvrip_only_seconds"] = sum(
        interval.duration for interval in report.tvrip_only
    )
    report.source_analysis["estimated_matching_content"] = report.coverage
    if config.tvrip_retain_alternative_sections and report.tvrip_only:
        report.warnings.append(
            "TVRip-only alternate material cannot be retained without changing the immutable "
            "master video timeline; those intervals remain excluded"
        )
    return report


def detected_master_only_intervals(
    plan: JobPlan,
    sync: SyncResult,
    *,
    minimum_seconds: float,
) -> list[TVRipInterval]:
    """Return material present on the immutable master timeline but absent in map.

    Milksync expresses every source-side synchronized range as
    ``(source start, source end, target offset)``.  The complement of those
    ranges after projecting them to the master timeline is exactly where its
    normal audio renderer inserts silence.  This inexpensive preflight avoids
    frame analysis and re-encoding for ordinary fully matching releases.
    """

    source_duration = max(
        (plan.dual.duration - plan.dual_trim) / max(sync.speed_correction_factor, 0.000001),
        0.0,
    )
    master_duration = max(plan.normal.duration - plan.normal_trim, 0.0)
    mapped = [
        item
        for bucket in sync.sync_buckets
        if (
            item := _bounded_bucket(
                bucket,
                source_duration=source_duration,
                master_duration=master_duration,
            )
        )
        is not None
    ]
    if not mapped:
        return []
    return [
        TVRipInterval(
            start,
            end,
            "master-only",
            "candidate master-only gap between Milksync reference matches",
        )
        for start, end in _complement([(item[2], item[3]) for item in mapped], master_duration)
        if end - start >= max(minimum_seconds, 0.01)
    ]


def _recover_mapped_master_gaps(
    plan: JobPlan,
    sync: SyncResult,
    report: TVRipSyncReport,
    *,
    source_path: Path,
    master_path: Path,
    source_duration: float,
    master_duration: float,
    config: DualMakerConfig,
    runner: ToolRunner,
) -> dict[str, object]:
    """Recover a false Milksync hole only after a longer original-audio proof.

    A bucket boundary can leave a tiny silent hole even when the broadcast dub
    is present.  Do not turn that into English (or silence) merely because the
    first map omitted it.  We test the inferred source clock from both
    neighbouring buckets against the two selected original-language tracks.
    A matching long window creates a raw-dub bridge; an unrelated window keeps
    the normal master-original fallback.
    """

    evidence: dict[str, object] = {
        "enabled": bool(report.master_only),
        "action": "not-needed" if not report.master_only else "checked",
        "window_seconds": max(
            config.tvrip_acoustic_segment_window_seconds,
            config.tvrip_acoustic_segment_max_gap_seconds,
        ),
        "minimum_similarity": config.tvrip_acoustic_segment_min_similarity,
        "gaps": [],
    }
    if not (
        config.allow_tvrip_segment_sync
        and config.tvrip_continue_on_validation_warnings
    ):
        evidence.update(
            enabled=False,
            action="disabled",
            reason="Raw-dub recovery is available in the authorized continuation workflow",
        )
        return evidence
    if not report.master_only or not source_path.is_file() or not master_path.is_file():
        if report.master_only:
            evidence.update(action="unverifiable", reason="Source or master is unavailable")
        return evidence

    # The terminal-tail guard is deliberately stricter than ordinary map-hole
    # recovery: it checks every edge of the open-ended bucket and can reject
    # an HDTV preview/credits tail. That decision must be final. A broad probe
    # beginning just before the rejected tail must not restore it.
    protected_ranges: list[tuple[float, float]] = [
        (segment.master_start, segment.master_end)
        for segment in report.segments
        if segment.status == "rejected" and segment.master_end > segment.master_start
    ]
    terminal = report.source_analysis.get("terminal_tail_validation")
    if isinstance(terminal, dict) and str(terminal.get("action", "")).endswith(
        "replaced-with-fallback"
    ):
        raw_ranges = terminal.get("rejected_master_ranges", [])
        if isinstance(raw_ranges, list):
            for raw_range in raw_ranges:
                if not isinstance(raw_range, dict):
                    continue
                try:
                    start = float(raw_range["start"])
                    end = float(raw_range["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                if end > start:
                    protected_ranges.append((start, end))
    protected_ranges = _merge_ranges(protected_ranges)
    if protected_ranges:
        evidence["protected_master_ranges"] = [
            {"start": start, "end": end} for start, end in protected_ranges
        ]

    recoverable_gaps: list[TVRipInterval] = []
    for gap in list(report.master_only):
        overlaps_terminal_tail = any(
            min(gap.end, protected_end) - max(gap.start, protected_start) > 0.001
            for protected_start, protected_end in protected_ranges
        )
        if overlaps_terminal_tail:
            # The leading edge of an open terminal map can have a different
            # offset from the preceding bounded bucket. Its small intervening
            # gap is inseparable from the rejected tail, so keep the whole
            # contiguous gap on the master rather than risk restoring an
            # alternate broadcast ending for a second or two.
            evidence["gaps"].append(  # type: ignore[index]
                {
                    "master_start": gap.start,
                    "master_end": gap.end,
                    "duration": gap.duration,
                    "action": "fallback-preserved-terminal-tail",
                    "reason": (
                        "This gap meets an explicitly rejected mapped interval, including a "
                        "terminal source tail; it cannot be recovered as raw Portuguese audio"
                    ),
                }
            )
            continue
        recoverable_gaps.append(gap)

    accepted = sorted(
        (segment for segment in report.segments if segment.status == "accepted"),
        key=lambda segment: segment.master_start,
    )
    recovered: list[dict[str, float]] = []
    for gap in recoverable_gaps:
        previous = next(
            (segment for segment in reversed(accepted) if segment.master_end <= gap.start + 0.02),
            None,
        )
        following = next(
            (segment for segment in accepted if segment.master_start >= gap.end - 0.02),
            None,
        )
        offsets = []
        for segment in (previous, following):
            if segment is not None and all(abs(segment.offset_seconds - item) > 0.001 for item in offsets):
                offsets.append(segment.offset_seconds)
        item: dict[str, object] = {
            "master_start": gap.start,
            "master_end": gap.end,
            "duration": gap.duration,
            "candidate_offsets": offsets,
        }
        best: tuple[float, float, float] | None = None
        for offset in offsets:
            source_start = gap.start - offset
            # Validate across a longer continuous window, not just the small
            # gap itself. Both original tracks must still describe the same
            # scene at the inferred clock.
            window = min(
                max(
                    gap.duration,
                    config.tvrip_acoustic_segment_window_seconds,
                    config.tvrip_acoustic_segment_max_gap_seconds,
                ),
                master_duration - gap.start,
                source_duration - source_start,
            )
            if source_start < 0 or window < config.tvrip_acoustic_segment_min_seconds:
                continue
            probe = _common_original_probe(
                plan,
                sync,
                source_path=source_path,
                master_path=master_path,
                source_start=source_start,
                master_start=gap.start,
                window=window,
                runner=runner,
            )
            similarity = float(probe["similarity"])
            item.setdefault("probes", []).append({"offset": offset, **probe})  # type: ignore[union-attr]
            if best is None or similarity > best[0]:
                best = (similarity, offset, window)
        if best is None or best[0] < config.tvrip_acoustic_segment_min_similarity:
            # Milksync quantizes correspondence boundaries to audio-analysis
            # frames. A sub-second complement between two otherwise forward,
            # adjacent map buckets is therefore not evidence of a missing dub.
            # Never replace such a micro-gap with a burst of English (or
            # silence). Bridge it from the raw Portuguese source using the
            # preceding mapped offset; longer ranges still require the full
            # common-original proof above before they can avoid fallback.
            if gap.duration < config.tvrip_acoustic_segment_min_seconds and offsets:
                offset = offsets[0]
                source_start = gap.start - offset
                source_end = source_start + gap.duration
                if 0 <= source_start and source_end <= source_duration:
                    recovered.append(
                        {
                            "master_start": gap.start,
                            "master_end": gap.end,
                            "source_start": source_start,
                            "source_end": source_end,
                            "offset_seconds": offset,
                            "validation_window_seconds": 0.0,
                            "similarity": -1.0,
                        }
                    )
                    report.segments.append(
                        TVRipSegment(
                            index=0,
                            source_start=source_start,
                            source_end=source_end,
                            master_start=gap.start,
                            master_end=gap.end,
                            offset_seconds=offset,
                            confidence=0.0,
                            status="accepted",
                            operation=(
                                "Accepted: bridged sub-second Milksync boundary from raw "
                                "Portuguese audio; insufficient duration to prove a content gap"
                            ),
                            speed_factor=sync.speed_correction_factor,
                        )
                    )
                    item.update(
                        action="bridged-micro-gap-with-raw-dub",
                        offset=offset,
                        reason=(
                            "Below tvrip_acoustic_segment_min_seconds; retained Portuguese "
                            "continuity instead of inserting a brief fallback"
                        ),
                    )
                    evidence["gaps"].append(item)  # type: ignore[index]
                    continue
            item["action"] = "fallback-retained"
            evidence["gaps"].append(item)  # type: ignore[index]
            continue

        _, offset, window = best
        source_start = gap.start - offset
        source_end = source_start + gap.duration
        if source_start < 0 or source_end > source_duration + 0.001:
            # A convincing long probe can start inside the source while the
            # map complement itself extends beyond its final decodable audio
            # packet. Rendering that would create a silent tail, so leave it
            # to the configured master fallback instead.
            item.update(
                action="fallback-retained-source-exhausted",
                source_start=source_start,
                source_end=source_end,
                source_duration=source_duration,
                reason="Inferred raw Portuguese bridge would exceed the source duration",
            )
            evidence["gaps"].append(item)  # type: ignore[index]
            continue
        recovered.append(
            {
                "master_start": gap.start,
                "master_end": gap.end,
                "source_start": source_start,
                "source_end": source_end,
                "offset_seconds": offset,
                "validation_window_seconds": window,
                "similarity": best[0],
            }
        )
        report.segments.append(
            TVRipSegment(
                index=0,
                source_start=source_start,
                source_end=source_end,
                master_start=gap.start,
                master_end=gap.end,
                offset_seconds=offset,
                confidence=best[0],
                status="accepted",
                operation="Accepted: recovered false master-only gap by long common-original proof",
                speed_factor=sync.speed_correction_factor,
            )
        )
        item.update(action="recovered-with-raw-dub", similarity=best[0], window_seconds=window)
        evidence["gaps"].append(item)  # type: ignore[index]

    if recovered:
        report.segments.sort(key=lambda segment: (segment.master_start, segment.source_start))
        for index, segment in enumerate(report.segments, start=1):
            segment.index = index
        report.source_analysis["raw_dub_gap_bridges"] = recovered
        evidence.update(action="recovered", recovered_count=len(recovered))
        report.warnings.append(
            f"Recovered {len(recovered)} apparent master-only gap(s) with raw Portuguese audio "
            "(long gaps require common-original proof; sub-second map boundaries preserve dub continuity)"
        )
    else:
        evidence["recovered_count"] = 0
    return evidence


def build_dub_gap_report(
    plan: JobPlan,
    sync: SyncResult,
    *,
    source_path: Path,
    master_path: Path,
    config: DualMakerConfig,
    work_dir: Path,
    runner: ToolRunner,
) -> TVRipSyncReport:
    """Validate a standard DUAL map before filling missing dub scenes.

    This intentionally reuses the stricter per-segment video validation from
    the TVRip workflow.  A correlation hole alone is never enough evidence to
    replace Portuguese dialogue with the original language.
    """

    report = build_tvrip_sync_report(
        plan,
        sync,
        source_path=source_path,
        master_path=master_path,
        config=config,
        work_dir=work_dir,
        runner=runner,
        workflow="dub-gap",
        minimum_master_gap_seconds=config.dub_gap_min_seconds,
    )
    report.fallback = config.dub_gap_fallback  # type: ignore[assignment]
    report.source_analysis["fallback_reference"] = {
        "source": "master",
        "path": str(master_path),
        "track_id": plan.normal_original.id,
        "language": plan.normal_original.effective_language,
        "title": plan.normal_original.title,
    }
    report.source_analysis["candidate_master_only_intervals"] = [
        {
            "start": interval.start,
            "end": interval.end,
            "duration": interval.duration,
        }
        for interval in detected_master_only_intervals(
            plan,
            sync,
            minimum_seconds=config.dub_gap_min_seconds,
        )
    ]
    return report


def approve_dub_gap_report(
    report: TVRipSyncReport,
    plan: JobPlan,
    config: DualMakerConfig,
) -> TVRipSyncReport:
    """Accept only a complete validated map before inserting original audio.

    Unlike the explicit TVRip editor, ordinary DUAL processing cannot treat a
    rejected reference segment as a missing dub segment: that would overwrite
    potentially valid Portuguese audio.  Interactive users can deliberately
    review the same segment checklist; unattended processing is all-or-nothing.
    """

    if report.workflow != "dub-gap":
        raise ProcessingError("Dub-gap approval received a non dub-gap report")
    if report.fallback == "off":
        report.result = "accepted"
        report.reason = "Dub-gap fallback disabled by configuration"
        return report

    _refresh_report(report, max(plan.normal.duration - plan.normal_trim, 0.0))
    if not config.interactive and (report.ambiguous_segments or report.rejected_segments):
        report.result = "rejected"
        report.reason = (
            "Dub-gap fallback was withheld because not every mapped DUAL section passed "
            "independent validation"
        )
        raise TVRipValidationError(report.reason, report)

    policy_config = replace(
        config,
        tvrip_fallback=report.fallback,
        tvrip_min_coverage=config.dub_gap_min_coverage,
        tvrip_track_title=config.dub_gap_track_title,
        tvrip_allow_partial_tracks=True,
    )
    return approve_tvrip_report(report, plan, policy_config)


def approve_tvrip_report(
    report: TVRipSyncReport,
    plan: JobPlan,
    config: DualMakerConfig,
) -> TVRipSyncReport:
    master_duration = max(plan.normal.duration - plan.normal_trim, 0.0)
    _refresh_report(report, master_duration)
    if (
        config.interactive
        and config.allow_tvrip_segment_sync
        and config.tvrip_continue_on_validation_warnings
    ):
        # Interactive mode remains useful for selecting pairs/tracks, but a
        # cut-heavy map can have hundreds of micro-segments. The explicit
        # continuation policy means review belongs in the JSON report after
        # output assembly, not in an impractical pre-mux checklist.
        report.approved = True
        report.warnings.append(
            "Interactive TVRip segment checklist deferred; the complete interval map "
            "will be available in the JSON report after output assembly"
        )
        report.source_analysis["interactive_segment_review"] = {
            "action": "deferred",
            "reason": "Authorized continuation policy defers cut-heavy segment review",
        }
    elif config.interactive:
        from .tui import review_dub_gap_segments, review_tvrip_segments

        reviewed = (
            review_dub_gap_segments(report, config.tvrip_fallback)
            if report.workflow == "dub-gap"
            else review_tvrip_segments(report, config.tvrip_fallback)
        )
        if reviewed is None:
            raise UserCancelledError("TVRip segment review cancelled; no output was written")
        accepted, fallback = reviewed
        for segment in report.segments:
            if segment.index in accepted and segment.status != "rejected":
                segment.status = "accepted"
                if "Ambiguous" in segment.operation:
                    segment.operation = "Accepted manually after interactive review"
            elif segment.status != "rejected":
                segment.status = "rejected"
                segment.operation = "Excluded during interactive review"
        report.fallback = fallback
        report.approved = True
        _refresh_report(report, master_duration)

    problems: list[str] = []
    if not report.segments:
        problems.append("Milksync found no bounded common-content segments")
    if len(report.segments) > config.tvrip_max_segments:
        message = (
            f"{len(report.segments)} segments exceed configured diagnostic threshold "
            f"{config.tvrip_max_segments}; continuing with the complete mapped timeline"
        )
        # Milksync can legitimately produce many short edit buckets for an
        # HDTV/telecine source.  This number is a useful review warning, not
        # proof that the rendered Portuguese timeline is unusable.  Every
        # bucket still flows through the normal local fallback policy, and the
        # full decision list is retained in the JSON report for later review.
        report.warnings.append(message)
        report.source_analysis["segment_count_diagnostic"] = {
            "observed": len(report.segments),
            "threshold": config.tvrip_max_segments,
            "action": "continued",
            "reason": "Segment count is diagnostic only; output assembly was not skipped",
        }
    omitting_tvrip = report.fallback == "omit"
    if report.ambiguous_segments and not config.interactive and not omitting_tvrip:
        problems.append(
            f"{report.ambiguous_segments} segments remain ambiguous and require interactive review"
        )
    telecine_acoustic_map = report.source_analysis.get("telecine_acoustic_map_validation", {})
    audio_map_validated = bool(
        isinstance(telecine_acoustic_map, dict) and telecine_acoustic_map.get("enabled")
    )
    minimum_source_confidence = (
        config.tvrip_spectral_min_source_match_confidence
        if report.source_analysis.get("spectrally_verified_timing") or audio_map_validated
        else config.tvrip_min_source_match_confidence
    )
    if (
        report.source_match_confidence < minimum_source_confidence
        and not omitting_tvrip
    ):
        problems.append(
            f"source confidence {report.source_match_confidence:.1%} is below "
            f"{minimum_source_confidence:.1%}"
        )
    if report.coverage < config.tvrip_min_coverage and not omitting_tvrip:
        problems.append(
            f"validated dub coverage {report.coverage:.1%} is below "
            f"{config.tvrip_min_coverage:.1%}"
        )
    if report.master_only and not config.tvrip_allow_partial_tracks and not omitting_tvrip:
        problems.append("master-only gaps exist but partial TVRip tracks are disabled")
    speed_delta = abs(report.speed_correction - 1.0)
    if speed_delta > 0 and not config.tvrip_allow_speed_correction and not omitting_tvrip:
        problems.append("TVRip speed correction is disabled by policy")
    if speed_delta > config.tvrip_max_speed_adjustment and not omitting_tvrip:
        problems.append(
            f"required speed adjustment {speed_delta:.3%} exceeds configured maximum "
            f"{config.tvrip_max_speed_adjustment:.3%}"
        )
    if report.master_only and report.fallback == "ask":
        problems.append(
            "master-only gaps require a fallback choice; use --tvrip-fallback or --interactive"
        )
    # An explicitly enabled TVRip run may be deliberately best-effort. The
    # mapped portions and master fallbacks are still rendered; the report keeps
    # every warning for the requested post-run review. A total lack of mapped
    # content and an unresolved fallback remain non-renderable states.
    non_renderable = [
        problem
        for problem in problems
        if problem == "Milksync found no bounded common-content segments"
        or problem.startswith("master-only gaps require a fallback choice")
    ]
    deferred = [problem for problem in problems if problem not in non_renderable]
    if (
        deferred
        and config.allow_tvrip_segment_sync
        and config.tvrip_continue_on_validation_warnings
    ):
        report.warnings.extend(
            f"Validation warning deferred for output assembly: {problem}"
            for problem in deferred
        )
        report.source_analysis["deferred_validation"] = {
            "enabled": True,
            "action": "continued",
            "warnings": deferred,
            "reason": (
                "Experimental TVRip processing was authorized; renderable mapped "
                "and fallback intervals were assembled for post-run review"
            ),
        }
        problems = non_renderable
    if problems:
        report.result = "rejected"
        report.reason = "; ".join(problems)
        raise TVRipValidationError(report.reason, report)
    report.result = "accepted"
    report.reason = (
        "TVRip tracks will be omitted by fallback policy"
        if omitting_tvrip
        else (
            f"Validated {report.accepted_segments} segments at "
            f"{report.coverage:.1%} Portuguese coverage"
        )
    )
    report.fallback_intervals = list(report.master_only)
    return report


def _channel_layout(track: Track) -> str:
    return {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(track.channels or 2, "stereo")


def _partition(report: TVRipSyncReport, duration: float) -> list[tuple[float, float, bool]]:
    accepted = _merge_ranges(
        [
            (segment.master_start, segment.master_end)
            for segment in report.segments
            if segment.status == "accepted"
        ]
    )
    boundaries = sorted({0.0, duration, *(value for pair in accepted for value in pair)})
    parts: list[tuple[float, float, bool]] = []
    for start, end in pairwise(boundaries):
        if end - start <= 0.001:
            continue
        midpoint = (start + end) / 2
        covered = any(left <= midpoint <= right for left, right in accepted)
        parts.append((start, end, covered))
    return parts


def _fallback_title(config: DualMakerConfig, report: TVRipSyncReport) -> str:
    fallback = ""
    if report.master_only:
        ranges = [f"{item.start:.3f}–{item.end:.3f}" for item in report.master_only]
        if len(ranges) > 3:
            total = sum(interval.duration for interval in report.master_only)
            described = f"{len(ranges)} intervals/{total:.3f}s"
        else:
            described = ", ".join(ranges)
        fallback = f"; Fallback {report.fallback}: {described}"
    return config.tvrip_track_title.format(
        mode="Segmented" if len(report.segments) > 1 else "Validated",
        coverage=report.coverage,
        fallback=fallback,
    )


def _raw_dub_bridge_for_part(
    report: TVRipSyncReport, start: float, end: float
) -> dict[str, float] | None:
    bridges = report.source_analysis.get("raw_dub_gap_bridges", [])
    if not isinstance(bridges, list):
        return None
    midpoint = (start + end) / 2
    for bridge in bridges:
        if not isinstance(bridge, dict):
            continue
        bridge_start = float(bridge.get("master_start", -1.0))
        bridge_end = float(bridge.get("master_end", -1.0))
        if bridge_start - 0.001 <= midpoint <= bridge_end + 0.001:
            return {
                "source_start": float(bridge["source_start"])
                + (start - bridge_start),
                "source_end": float(bridge["source_start"])
                + (end - bridge_start),
            }
    return None


def apply_tvrip_audio_policy(
    plan: JobPlan,
    sync: SyncResult,
    report: TVRipSyncReport,
    *,
    master_path: Path,
    dual_path: Path,
    work_dir: Path,
    config: DualMakerConfig,
    runner: ToolRunner,
) -> None:
    """Apply fallback/omission policy and label every retained TVRip dub."""

    original_dubs = list(plan.resolved_dubs)
    tvrip_indices = [index for index, choice in enumerate(original_dubs) if choice.source == "dual"]
    if not tvrip_indices:
        return
    if report.fallback == "omit":
        retained_indices = [
            index for index, choice in enumerate(original_dubs) if choice.source != "dual"
        ]
        if not retained_indices:
            report.result = "rejected"
            report.reason = (
                "TVRip fallback policy would omit every Portuguese track; no dual output remains"
            )
            raise TVRipValidationError(
                report.reason,
                report,
            )
    else:
        retained_indices = list(range(len(original_dubs)))

    duration = max(plan.normal.duration - plan.normal_trim, 0.001)
    parts = _partition(report, duration)
    raw_dub_bridges = report.source_analysis.get("raw_dub_gap_bridges", [])
    needs_rebuild = (
        retained_indices != list(range(len(original_dubs)))
        or bool(report.master_only)
        or bool(raw_dub_bridges)
    )
    replacement_files: dict[int, Path] = {}
    fallback_master_index = next(
        (index for index, choice in enumerate(original_dubs) if choice.source == "master"),
        None,
    )
    if report.fallback == "alternate-dub" and fallback_master_index is None:
        report.result = "rejected"
        report.reason = (
            "alternate-dub fallback was requested but no master-timeline Portuguese dub exists"
        )
        raise TVRipValidationError(
            report.reason,
            report,
        )

    if needs_rebuild and report.fallback != "omit":
        for dub_index in tvrip_indices:
            track = original_dubs[dub_index].track
            output = work_dir / f"tvrip-policy-dub-{dub_index}.mka"
            filters: list[str] = []
            labels: list[str] = []
            layout = _channel_layout(track)
            for part_index, (start, end, covered) in enumerate(parts):
                label = f"p{part_index}"
                labels.append(f"[{label}]")
                common = (
                    f"atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,"
                    f"aresample=48000,aformat=sample_rates=48000:channel_layouts={layout}"
                )
                bridge = _raw_dub_bridge_for_part(report, start, end) if covered else None
                if bridge is not None:
                    raw_start = bridge["source_start"] * sync.speed_correction_factor
                    raw_end = bridge["source_end"] * sync.speed_correction_factor
                    tempo = sync.speed_correction_factor
                    filters.append(
                        f"[2:a:{track.type_index}]atrim=start={raw_start:.6f}:end={raw_end:.6f},"
                        f"asetpts=PTS-STARTPTS,atempo={tempo:.9f},aresample=48000,"
                        f"aformat=sample_rates=48000:channel_layouts={layout}[{label}]"
                    )
                elif covered:
                    filters.append(f"[0:a:{dub_index}]{common}[{label}]")
                elif report.fallback == "original":
                    filters.append(
                        f"[1:a:{plan.normal_original.type_index}]{common}[{label}]"
                    )
                elif report.fallback == "alternate-dub":
                    filters.append(f"[0:a:{fallback_master_index}]{common}[{label}]")
                else:
                    filters.append(
                        f"anullsrc=r=48000:cl={layout},atrim=duration={end - start:.6f},"
                        f"asetpts=PTS-STARTPTS[{label}]"
                    )
            filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]")
            runner.run(
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    sync.path,
                    "-i",
                    master_path,
                    "-i",
                    dual_path,
                    "-filter_complex",
                    ";".join(filters),
                    "-map",
                    "[out]",
                    "-c:a",
                    "flac",
                    "-map_chapters",
                    "-1",
                    output,
                )
            )
            replacement_files[dub_index] = output
            sync.codec_fallbacks.append(
                f"TVRip fallback rendering re-encoded {original_dubs[dub_index].label} to FLAC"
            )

    if needs_rebuild:
        from .metadata import MediaInspector

        stage = MediaInspector(runner).inspect(sync.path)
        original_stage = stage.audio_tracks[sync.stage_original_index]
        rebuilt = work_dir / "synchronized-tvrip-policy.mkv"
        command: list[str | Path] = [
            "mkvmerge",
            "--no-date",
            "-o",
            rebuilt,
            "--no-audio",
            sync.path,
        ]
        for original_index in retained_indices:
            if original_index in replacement_files:
                command += ["--no-video", "--no-subtitles", replacement_files[original_index]]
            else:
                actual = stage.audio_tracks[original_index]
                command += [
                    "--no-video",
                    "--audio-tracks",
                    str(actual.id),
                    "--no-subtitles",
                    "--no-chapters",
                    "--no-global-tags",
                    "--no-attachments",
                    sync.path,
                ]
        command += [
            "--no-video",
            "--audio-tracks",
            str(original_stage.id),
            "--no-subtitles",
            "--no-chapters",
            "--no-global-tags",
            "--no-attachments",
            sync.path,
        ]
        runner.run(command)
        sync.path = rebuilt
        sync.stage_original_index = len(retained_indices)

    retained = [original_dubs[index] for index in retained_indices]
    title = _fallback_title(config, report)
    for choice in retained:
        if choice.source == "dual":
            choice.track = replace(choice.track, title=title)
    plan.dub_selections = retained
    plan.dub_tracks = [choice.track for choice in retained]
