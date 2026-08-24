from __future__ import annotations

import json
import logging
import os
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..defaults import SIDECAR_LEGACY_TEXT_ENCODINGS, SIDECAR_TEXT_OUTPUT_ENCODING
from ..errors import ProcessingError
from ..metadata import first_packet_pts
from ..models import DualMakerConfig, JobPlan, SidecarSubtitle, Track
from ..runner import ToolRunner

LOGGER = logging.getLogger("dualmaker")
TEXT_SUBTITLE_CODEC_IDS = {"S_TEXT/UTF8", "S_TEXT/ASS", "S_TEXT/SSA", "S_TEXT/WEBVTT"}


def is_text_subtitle(track: Track) -> bool:
    codec_id = track.codec_id.upper()
    codec = track.codec.casefold()
    return codec_id in TEXT_SUBTITLE_CODEC_IDS or any(
        token in codec for token in ("subrip", "srt", "ass", "ssa", "webvtt")
    )


def estimate_spectral_tempo(
    shift_points: list[tuple[float, float, float]],
    config: DualMakerConfig,
) -> dict[str, object]:
    """Measure source tempo from many local Milksync correspondence slopes.

    A cut changes the intercept (delay) but not the slope inside each surviving
    section.  Pairing points over bounded local distances, then taking a robust
    median, separates that stepwise editorial structure from true linear drift.
    The returned speed factor is the FFmpeg ``atempo`` factor required to make
    the source clock match the target clock.
    """

    points = sorted(
        # Milksync stores (target_time, source_time, target-source delay).
        {(float(source), float(target)) for target, source, _ in shift_points},
        key=lambda item: (item[0], item[1]),
    )
    slopes: list[float] = []
    for index, (source_start, target_start) in enumerate(points):
        for source_end, target_end in points[index + 1 :]:
            source_span = source_end - source_start
            if source_span < config.fps_spectral_pair_min_seconds:
                continue
            if source_span > config.fps_spectral_pair_max_seconds:
                break
            target_span = target_end - target_start
            if target_span <= 0:
                continue
            slope = target_span / source_span
            if 0.5 <= slope <= 2.0:
                slopes.append(slope)

    result: dict[str, object] = {
        "point_count": len(points),
        "pair_count": len(slopes),
        "reliable": False,
        "target_seconds_per_source_second": None,
        "speed_factor": None,
        "dispersion": None,
        "inlier_pairs": 0,
    }
    if len(slopes) < config.fps_spectral_min_pairs:
        result["reason"] = (
            f"Only {len(slopes)} bounded acoustic pairs were available; "
            f"{config.fps_spectral_min_pairs} are required"
        )
        return result

    overall_median = statistics.median(slopes)
    cluster_radius = config.fps_spectral_slope_cluster_radius
    center = max(
        slopes,
        key=lambda candidate: (
            sum(abs(value - candidate) <= cluster_radius for value in slopes),
            -abs(candidate - overall_median),
        ),
    )
    inliers = [value for value in slopes if abs(value - center) <= cluster_radius]
    refined = statistics.median(inliers)
    refined_dispersion = statistics.median(abs(value - refined) for value in inliers)
    speed_factor = 1.0 / refined
    reliable = (
        len(inliers) >= config.fps_spectral_min_pairs
        and refined_dispersion <= config.fps_spectral_max_dispersion
        and abs(speed_factor - 1.0) <= config.fps_spectral_max_speed_adjustment
    )
    result.update(
        {
            "reliable": reliable,
            "target_seconds_per_source_second": refined,
            "speed_factor": speed_factor,
            "dispersion": refined_dispersion,
            "inlier_pairs": len(inliers),
            "inlier_fraction": len(inliers) / len(slopes),
            "reason": (
                "Robust local acoustic slopes identify one stable program clock"
                if reliable
                else "Acoustic slopes did not meet the configured stability/speed limits"
            ),
        }
    )
    return result


def post_sync_relative_speed(
    observed_speed_factor: float | None,
    applied_speed_factor: float,
) -> float | None:
    """Return the residual clock slope after the requested tempo is applied.

    Milksync measures the correspondence after ``atempo`` has rendered the
    source waveform. Its measured factor is therefore already the residual on
    the rendered timeline: a successful corrected pass reports ``1.0``.
    Dividing it by the requested factor created a fictitious residual (for
    example, ``1 / 0.9855``) and could trigger a second, wrong clock change.
    """

    if observed_speed_factor is None or applied_speed_factor <= 0:
        return None
    return observed_speed_factor


def next_spectral_speed_factor(
    current_factor: float,
    residual_factor: float,
    prior_observations: list[tuple[float, float]],
    *,
    damping: float,
) -> tuple[float, str]:
    """Return a stable next clock estimate from a noisy residual measurement.

    A full multiplicative correction can overshoot because a tiny change to the
    speed factor can make DTW choose neighboring edit anchors.  Until two
    measurements bracket zero, apply only a configurable fraction.  Once their
    signs differ, use a secant root constrained inside that measured bracket.
    """

    residual_error = residual_factor - 1.0
    corrected = current_factor * (1.0 + damping * residual_error)
    method = "damped-residual"
    if prior_observations:
        prior_factor, prior_error = prior_observations[-1]
        denominator = residual_error - prior_error
        if prior_error * residual_error < 0 and abs(denominator) > 1e-12:
            secant = current_factor - residual_error * (
                current_factor - prior_factor
            ) / denominator
            lower, upper = sorted((prior_factor, current_factor))
            if lower < secant < upper:
                corrected = secant
                method = "bracketed-secant"
    return corrected, method


@dataclass(slots=True)
class SyncResult:
    path: Path
    report_path: Path
    text_subtitles: list[Track] = field(default_factory=list)
    binary_subtitles: list[Track] = field(default_factory=list)
    sidecar_subtitles: list[SidecarSubtitle] = field(default_factory=list)
    shift_points: list[tuple[float, float, float]] = field(default_factory=list)
    sync_buckets: list[tuple[float, float, float]] = field(default_factory=list)
    delete_buckets: list[tuple[float, float]] = field(default_factory=list)
    source_reference_start: float | None = None
    target_reference_start: float | None = None
    source_dub_starts: list[float | None] = field(default_factory=list)
    observed_reference_pts_offset: float | None = None
    container_delay_adjustment: float = 0.0
    manual_delay_adjustment: float = 0.0
    effective_delay_adjustment: float = 0.0
    output_audio_mapping: list[tuple[int, int]] = field(default_factory=list)
    stage_original_index: int = 0
    timeline_adjustment_ms: int = 0
    speed_correction_factor: float = 1.0
    codec_fallbacks: list[str] = field(default_factory=list)
    sync_coverage: float | None = None

    @property
    def constant_subtitle_delay_ms(self) -> int | None:
        if not self.shift_points or self.delete_buckets:
            return None
        deltas = [point[2] for point in self.shift_points]
        if max(deltas) - min(deltas) > 0.05:
            return None
        return round(sum(deltas) / len(deltas) * 1000)


class MilksyncAdapter:
    """Run the bundled engine in an isolated process and return its sync map."""

    def __init__(self, runner: ToolRunner | None = None) -> None:
        self.runner = runner or ToolRunner()

    def _prepare_private_environment(self, temp_dir: Path) -> None:
        """Expose configured tools to bundled Milksync inside this private job."""

        shim_dir = temp_dir / "tool-shims"
        shim_dir.mkdir(parents=True, exist_ok=True)
        numba_cache_dir = temp_dir / "numba-cache"
        numba_cache_dir.mkdir(parents=True, exist_ok=True)
        for name in self.runner.binaries:
            executable = self.runner.which(name)
            if not executable:
                continue
            shim = shim_dir / name
            if not shim.exists():
                shim.symlink_to(executable)
        existing_path = self.runner.environment.get("PATH", "")
        if not existing_path.startswith(f"{shim_dir}{os.pathsep}"):
            self.runner.environment["PATH"] = os.pathsep.join((str(shim_dir), existing_path))
        self.runner.environment.setdefault("NUMBA_CACHE_DIR", str(numba_cache_dir))

    def _refine_experimental_speed(
        self,
        plan: JobPlan,
        *,
        normal_path: Path,
        dual_path: Path,
        temp_dir: Path,
        config: DualMakerConfig,
    ) -> None:
        if not plan.fps.required or not config.fps_spectral_tempo_probe:
            return
        telecine_tvrip_realtime = bool(
            plan.source_kind == "tvrip"
            and abs(plan.fps.proposed_speed_factor - 1.0) <= 0.000_001
            and isinstance(plan.fps.validation.get("telecine_acoustic_preflight"), dict)
            and plan.fps.validation["telecine_acoustic_preflight"].get("enabled")
        )
        if telecine_tvrip_realtime:
            # The generic tempo probe estimates a *linear* clock from pairs of
            # bounded Milksync anchors.  A real-time telecine TVRip does not
            # need a speed estimate, and editorial buckets commonly leave too
            # few bounded pairs for that estimator.  Do not run a second full
            # Milksync pass merely to reject an already selected real-time
            # clock. The real pass below must still produce a multi-point map,
            # pass coverage checks, and survive strict local audio validation
            # before any output can be muxed.
            plan.fps.validation["spectral_tempo_probe"] = {
                "reliable": False,
                "fallback_accepted": True,
                "skipped": True,
                "reason": (
                    "Skipped linear spectral-tempo preflight for the explicit real-time "
                    "telecine TVRip path; the completed Milksync map is validated per segment"
                ),
                "visual_candidate_before_probe": plan.fps.proposed_speed_factor,
            }
            LOGGER.info(
                "Deferring real-time telecine TVRip tempo proof to the completed map and "
                "per-segment audio validation"
            )
            return
        report = temp_dir / "spectral-tempo-map.json"
        command: list[str | Path] = [
            sys.executable,
            "-m",
            "dualmaker.sync.milksync",
            dual_path,
            normal_path,
            "--audio-tracks",
            f"0:{plan.dual_original.type_index},1:{plan.normal_original.type_index}",
            "--output-video-file-index",
            "1",
            "--temp-folder",
            temp_dir / "spectral-preflight",
            "--sync-report",
            report,
            "--skip-subtitles",
            "--analyze-only",
        ]
        if config.only_delta:
            command.append("--only-delta")
        if config.preserve_silence:
            command.append("--preserve-silence")
        LOGGER.info("Measuring program speed from common-original acoustic fingerprints")
        result = self.runner.run(command, check=False)
        if result.returncode != 0 or not report.is_file():
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise ProcessingError(
                "Experimental spectral tempo preflight failed: " f"{detail[-4000:]}"
            )
        raw_report = json.loads(report.read_text(encoding="utf-8"))
        points = [tuple(point) for point in (raw_report.get("0") or {}).get("audio_shift_points", [])]
        analysis = estimate_spectral_tempo(points, config)  # type: ignore[arg-type]
        analysis["visual_candidate_before_probe"] = plan.fps.proposed_speed_factor
        plan.fps.validation["spectral_tempo_probe"] = analysis
        acoustic_tvrip_fallback = bool(
            plan.fps.validation.get("telecine_acoustic_preflight")
        )
        if not analysis["reliable"]:
            if acoustic_tvrip_fallback:
                analysis["fallback_accepted"] = True
                analysis["reason"] = (
                    f"{analysis['reason']}; retained a real-time telecine clock and deferred "
                    "acceptance to the completed Milksync map plus TVRip segment validation"
                )
                return
            raise ProcessingError(
                "Experimental spectral tempo analysis was inconclusive: "
                f"{analysis['reason']}"
            )
        measured = float(analysis["speed_factor"])
        plan.fps.proposed_speed_factor = measured
        plan.fps.detected_speed_factor = measured
        plan.fps.apply_speed_correction = abs(measured - 1.0) > 0.000_001
        plan.fps.reason = (
            "Milksync common-original spectrograms measured a stable content clock across "
            "multiple sections; editorial steps remain in the piecewise synchronization map"
        )

    def synchronize(
        self,
        plan: JobPlan,
        *,
        normal_path: Path,
        dual_path: Path,
        temp_dir: Path,
        config: DualMakerConfig,
    ) -> SyncResult:
        output = temp_dir / "synchronized.mkv"
        report = temp_dir / "sync-map.json"
        text_subtitles = [track for track in plan.dual_subtitles if is_text_subtitle(track)]
        binary_subtitles = [track for track in plan.dual_subtitles if not is_text_subtitle(track)]

        # The bundled engine invokes canonical tool names internally. Private per-job
        # shims make configured absolute binaries authoritative without touching the
        # process-wide environment or using a shared temporary directory.
        self._prepare_private_environment(temp_dir)
        self._refine_experimental_speed(
            plan,
            normal_path=normal_path,
            dual_path=dual_path,
            temp_dir=temp_dir,
            config=config,
        )

        source_reference_start = first_packet_pts(
            dual_path,
            "audio",
            plan.dual_original.type_index,
            self.runner,
        )
        target_reference_start = first_packet_pts(
            normal_path,
            "audio",
            plan.normal_original.type_index,
            self.runner,
        )
        dubs = plan.resolved_dubs
        output_original = plan.resolved_original
        source_dub_starts = [
            first_packet_pts(
                normal_path if choice.source == "master" else dual_path,
                "audio",
                choice.track.type_index,
                self.runner,
            )
            for choice in dubs
        ]
        if source_reference_start is None or target_reference_start is None:
            observed_reference_pts_offset = None
            LOGGER.warning(
                "Could not read both original-audio packet starts; their diagnostic "
                "container PTS difference is unavailable"
            )
        else:
            observed_reference_pts_offset = (
                target_reference_start - source_reference_start
            )

        # Chroma comparison deliberately decodes audio and therefore has no
        # container PTS clock.  A difference between the two *reference audio*
        # starts is not evidence of a difference from the immutable master
        # video: applying it here used to shift every DUAL track by that value
        # (commonly one fixed -400 ms for a whole season).  Source tracks keep
        # their own PTS relationship while they are rendered, but an observed
        # reference-to-reference PTS difference is diagnostic only.  A manual
        # adjustment remains an explicit user override.
        container_delay_adjustment = 0.0
        manual_delay_adjustment = config.adjust_delay or 0.0
        effective_delay_adjustment = manual_delay_adjustment
        LOGGER.info(
            "Original-audio packet starts: source=%.6fs target=%.6fs "
            "observed-reference-offset=%s; applied-container=%+.6fs "
            "manual=%+.6fs effective=%+.6fs",
            source_reference_start or 0.0,
            target_reference_start or 0.0,
            (
                f"{observed_reference_pts_offset:+.6f}s"
                if observed_reference_pts_offset is not None
                else "unavailable"
            ),
            container_delay_adjustment,
            manual_delay_adjustment,
            effective_delay_adjustment,
        )

        # Keep the same source/target contract as milksync and the original
        # dualmilk workflow: input 0 contributes synchronized tracks, while
        # input 1 contributes the master video *and* its original audio.  The
        # final ordering mux must consume both audio roles from this result;
        # otherwise it silently bypasses part of milksync's output timeline.
        output_audio_mapping = [
            *((1 if choice.source == "master" else 0, choice.track.type_index) for choice in dubs),
            (
                1 if output_original.source == "master" else 0,
                output_original.track.type_index,
            ),
        ]
        stage_original_index = len(dubs)

        command: list[str | Path] = [
            sys.executable,
            "-m",
            "dualmaker.sync.milksync",
            dual_path,
            normal_path,
            "--audio-tracks",
            f"0:{plan.dual_original.type_index},1:{plan.normal_original.type_index}",
            "--output-video-file-index",
            "1",
            "--output-audio-mapping",
            ",".join(f"{file_index}:{track_index}" for file_index, track_index in output_audio_mapping),
            "--chapter-source",
            "1",
            "--temp-folder",
            temp_dir / "milksync",
            "--sync-report",
            report,
            "--output",
            output,
        ]
        if text_subtitles:
            command += [
                "--output-subtitle-mapping",
                ",".join(f"0:{track.type_index}" for track in text_subtitles),
            ]
        else:
            command.append("--skip-subtitles")
        if plan.fps.apply_speed_correction:
            command += [
                "--align-framerate",
                "--framerate-speed-factor",
                f"{plan.fps.proposed_speed_factor:.12f}",
            ]
        if config.align_frames_too:
            command.append("--align-frames-too")
        if config.only_delta:
            command.append("--only-delta")
        if config.preserve_silence:
            command.append("--preserve-silence")
        if abs(effective_delay_adjustment) > 0.000_001:
            command += ["--adjust-delay", f"{effective_delay_adjustment:.9f}"]

        LOGGER.info("Synchronizing %s against %s", dual_path.name, normal_path.name)
        refinement_history: list[dict[str, float | int | str]] = []
        clock_observations: list[tuple[float, float]] = []
        render_pass = 0
        while True:
            temp_option = command.index("--temp-folder")
            command[temp_option + 1] = temp_dir / f"milksync-render-{render_pass}"
            if "--framerate-speed-factor" in command:
                factor_option = command.index("--framerate-speed-factor")
                command[factor_option + 1] = f"{plan.fps.proposed_speed_factor:.12f}"
            elif plan.fps.apply_speed_correction:
                command += [
                    "--align-framerate",
                    "--framerate-speed-factor",
                    f"{plan.fps.proposed_speed_factor:.12f}",
                ]

            output.unlink(missing_ok=True)
            report.unlink(missing_ok=True)
            result = self.runner.run_live(command)
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip()
                raise ProcessingError(
                    f"milksync failed with status {result.returncode}: {detail[-6000:]}"
                )
            if not output.is_file() or not report.is_file():
                raise ProcessingError("milksync completed without its output or sync report")

            raw_report = json.loads(report.read_text(encoding="utf-8"))
            source_report = raw_report.get("0") or {}
            final_shift_points = [
                tuple(point) for point in source_report.get("audio_shift_points", [])
            ]
            if not (plan.fps.required and config.fps_spectral_tempo_probe):
                break

            residual = estimate_spectral_tempo(final_shift_points, config)  # type: ignore[arg-type]
            observed_speed_factor = residual.get("speed_factor")
            applied_speed_factor = (
                plan.fps.proposed_speed_factor if plan.fps.apply_speed_correction else 1.0
            )
            relative_speed_factor = post_sync_relative_speed(
                float(observed_speed_factor)
                if observed_speed_factor is not None
                else None,
                applied_speed_factor,
            )
            target_duration = max(plan.normal.duration - plan.normal_trim, 1.0)
            projected_drift = (
                abs(relative_speed_factor - 1.0) * target_duration
                if relative_speed_factor is not None
                else None
            )
            residual["observed_speed_factor"] = observed_speed_factor
            residual["applied_speed_factor"] = applied_speed_factor
            residual["relative_speed_factor"] = relative_speed_factor
            residual["projected_drift_seconds"] = projected_drift
            residual["render_pass"] = render_pass
            plan.fps.validation["spectral_post_sync_validation"] = residual
            plan.fps.validation["spectral_speed_refinements"] = refinement_history
            acoustic_tvrip_fallback = bool(
                plan.fps.validation.get("telecine_acoustic_preflight")
            )
            if not residual["reliable"]:
                if acoustic_tvrip_fallback:
                    residual["fallback_accepted"] = True
                    residual["reason"] = (
                        f"{residual.get('reason')}; telecine TVRip output remains subject "
                        "to per-segment common-original validation"
                    )
                    break
                raise ProcessingError(
                    "Experimental spectral post-sync validation was inconclusive: "
                    f"{residual.get('reason')}"
                )

            # A 29.97fps TV recording is sometimes a real-time telecine of a
            # 23.976fps master, but it can also be a genuinely slower program
            # clock.  The early TVRip route deliberately starts in real time
            # when its sparse probe cannot distinguish those cases.  At this
            # point we have the complete Milksync map, which is much stronger
            # evidence: a dense cluster of local correspondence slopes proves
            # a linear clock difference.  Rendering that map piece by piece
            # without correcting its clock turns every gradual delay change
            # into a short silence gap.  Correct the clock once, then rerun
            # Milksync and validate the corrected map normally.
            if (
                acoustic_tvrip_fallback
                and render_pass == 0
                and not plan.fps.apply_speed_correction
                and relative_speed_factor is not None
                and abs(relative_speed_factor - 1.0)
                > max(config.fps_speed_ratio_tolerance, 0.003)
            ):
                measured_factor = float(observed_speed_factor)
                maximum_adjustment = min(
                    config.fps_spectral_max_speed_adjustment,
                    config.tvrip_max_speed_adjustment,
                )
                if abs(measured_factor - 1.0) <= maximum_adjustment:
                    refinement_history.append(
                        {
                            "render_pass": render_pass,
                            "previous_speed_factor": 1.0,
                            "residual_speed_factor": relative_speed_factor,
                            "projected_drift_seconds": float(projected_drift or 0.0),
                            "refined_speed_factor": measured_factor,
                            "correction_method": "post-map-linear-drift-rescue",
                        }
                    )
                    plan.fps.proposed_speed_factor = measured_factor
                    plan.fps.detected_speed_factor = measured_factor
                    plan.fps.apply_speed_correction = True
                    residual["linear_drift_rescue"] = True
                    plan.fps.reason = (
                        "The completed common-original map measured a stable linear "
                        "TVRip program clock; rendering once more with that clock "
                        "prevents gradual delay changes from becoming silent joins"
                    )
                    render_pass += 1
                    LOGGER.warning(
                        "Completed TVRip map proved a stable linear clock %.9f; "
                        "rerendering with time correction instead of inserting %.3fs "
                        "of gradual timeline gaps",
                        measured_factor,
                        projected_drift or 0.0,
                    )
                    continue

            if not config.fps_spectral_iterative_refinement:
                if relative_speed_factor is None:
                    raise ProcessingError(
                        "Experimental spectral post-sync validation did not provide a "
                        "usable source-clock factor"
                    )
                if abs(relative_speed_factor - 1.0) > max(
                    config.fps_speed_ratio_tolerance,
                    0.003,
                ):
                    if acoustic_tvrip_fallback:
                        residual["fallback_accepted"] = True
                        residual["reason"] = (
                            "Post-render source-clock slope differs from the requested TVRip "
                            "tempo; retaining the piecewise map only for strict local "
                            "common-original segment validation"
                        )
                        break
                    raise ProcessingError(
                        "Experimental spectral post-sync validation found remaining "
                        "linear drift: "
                        f"observed={observed_speed_factor}, "
                        f"applied={applied_speed_factor}, "
                        f"relative={relative_speed_factor}, "
                        f"projected={projected_drift:.3f}s"
                    )
                break
            if (
                projected_drift is not None
                and projected_drift
                <= config.fps_spectral_max_projected_drift_seconds
            ):
                break
            if render_pass >= config.fps_spectral_max_refinement_passes:
                raise ProcessingError(
                    "Experimental spectral post-sync validation found cumulative drift of "
                    f"{projected_drift:.3f}s across {target_duration:.3f}s after "
                    f"{render_pass} refinement pass(es); configured limit is "
                    f"{config.fps_spectral_max_projected_drift_seconds:.3f}s"
                )

            if relative_speed_factor is None:
                raise ProcessingError(
                    "Experimental spectral refinement did not provide a usable "
                    "post-render source-clock factor"
                )
            residual_factor = relative_speed_factor
            previous_factor = plan.fps.proposed_speed_factor
            residual_error = residual_factor - 1.0
            corrected_factor, correction_method = next_spectral_speed_factor(
                previous_factor,
                residual_factor,
                clock_observations,
                damping=config.fps_spectral_refinement_damping,
            )
            clock_observations.append((previous_factor, residual_error))
            if abs(corrected_factor - 1.0) > config.fps_spectral_max_speed_adjustment:
                raise ProcessingError(
                    "Composed experimental speed correction exceeds the configured limit: "
                    f"{corrected_factor:.9f}"
                )
            refinement_history.append(
                {
                    "render_pass": render_pass,
                    "previous_speed_factor": previous_factor,
                    "residual_speed_factor": residual_factor,
                    "projected_drift_seconds": float(projected_drift or 0.0),
                    "refined_speed_factor": corrected_factor,
                    "correction_method": correction_method,
                }
            )
            plan.fps.proposed_speed_factor = corrected_factor
            plan.fps.detected_speed_factor = corrected_factor
            plan.fps.apply_speed_correction = abs(corrected_factor - 1.0) > 0.000_001
            render_pass += 1
            LOGGER.warning(
                "Refining experimental content clock %.9f → %.9f (%s) because the "
                "residual slope projects to %.3fs over the full runtime",
                previous_factor,
                corrected_factor,
                correction_method,
                projected_drift,
            )
        sync_buckets = [tuple(bucket) for bucket in source_report.get("sync_buckets", [])]
        reference_duration = max(
            (plan.dual.duration - plan.dual_trim)
            / (plan.fps.proposed_speed_factor if plan.fps.apply_speed_correction else 1.0),
            1.0,
        )
        mapped_seconds = sum(
            max(
                min(float(bucket[1]), reference_duration) - max(float(bucket[0]), 0.0),
                0.0,
            )
            for bucket in sync_buckets
        )
        return SyncResult(
            path=output,
            report_path=report,
            text_subtitles=text_subtitles,
            binary_subtitles=binary_subtitles,
            shift_points=final_shift_points,
            sync_buckets=sync_buckets,
            delete_buckets=[tuple(bucket) for bucket in source_report.get("delete_buckets", [])],
            source_reference_start=source_reference_start,
            target_reference_start=target_reference_start,
            source_dub_starts=source_dub_starts,
            observed_reference_pts_offset=observed_reference_pts_offset,
            container_delay_adjustment=container_delay_adjustment,
            manual_delay_adjustment=manual_delay_adjustment,
            effective_delay_adjustment=effective_delay_adjustment,
            output_audio_mapping=output_audio_mapping,
            stage_original_index=stage_original_index,
            speed_correction_factor=(
                plan.fps.proposed_speed_factor if plan.fps.apply_speed_correction else 1.0
            ),
            codec_fallbacks=(
                [
                    (
                        "Different-FPS time stretching requires re-encoding synchronized "
                        "source-side audio to one FLAC timeline; lossless intermediates prevent "
                        "per-edit lossy encoder priming from accumulating, but object-based "
                        "metadata may not survive"
                    )
                ]
                if plan.fps.apply_speed_correction
                else []
            ),
            sync_coverage=min(mapped_seconds / reference_duration, 1.0),
        )

    def synchronize_sidecars(
        self,
        plan: JobPlan,
        sync: SyncResult,
        *,
        normal_path: Path,
        dual_path: Path,
        temp_dir: Path,
        config: DualMakerConfig,
    ) -> None:
        """Put explicitly identified sidecars onto the same output timeline.

        Master sidecars only require the same recap trim as the master video.
        DUAL sidecars use milksync's already-validated bucket map, so they
        follow constant offsets and mid-file edits just like embedded text
        subtitles.  Unsupported/invalid sidecars fail the job rather than
        being muxed with misleading timing.
        """
        if not plan.sidecar_subtitles:
            return
        try:
            import pysubs2

            from .milksync import extract_and_sync_subtitles
        except ImportError as exc:  # pragma: no cover - declared runtime dependency
            raise ProcessingError("Sidecar subtitle support requires pysubs2") from exc

        temp_dir.mkdir(parents=True, exist_ok=True)
        output_sidecars: list[SidecarSubtitle] = []
        for index, sidecar in enumerate(plan.sidecar_subtitles):
            normalized = self._convert_sidecar_to_utf8_bom(
                sidecar.path,
                destination=temp_dir / f"sidecar-{index}-utf8bom{sidecar.path.suffix}",
            )
            prepared = self._trim_sidecar(
                normalized,
                trim=plan.dual_trim if sidecar.source == "dual" else plan.normal_trim,
                destination=temp_dir / f"sidecar-{index}-trimmed{sidecar.path.suffix}",
                pysubs2=pysubs2,
            )
            if sidecar.source == "master":
                output_sidecars.append(
                    SidecarSubtitle(prepared, sidecar.source, sidecar.language)
                )
                continue

            destination = temp_dir / f"sidecar-{index}-synchronized{sidecar.path.suffix}"
            framerate_align = (
                (1.0, plan.fps.proposed_speed_factor)
                if plan.fps.apply_speed_correction
                else None
            )
            try:
                synchronized = extract_and_sync_subtitles(
                    None,
                    0,
                    plan.normal.duration - plan.normal_trim,
                    config.only_delta,
                    sync.shift_points,
                    sync.sync_buckets,
                    sync.delete_buckets,
                    destination,
                    None,
                    None,
                    None,
                    framerate_align=framerate_align,
                    external_subtitle_file=str(prepared),
                    output_encoding=SIDECAR_TEXT_OUTPUT_ENCODING,
                )
            except Exception as exc:
                raise ProcessingError(
                    f"Could not synchronize external subtitle {sidecar.path}: {exc}"
                ) from exc
            if not synchronized or not Path(synchronized).is_file():
                raise ProcessingError(
                    f"Sidecar synchronization did not create an output for {sidecar.path}"
                )
            output_sidecars.append(
                SidecarSubtitle(Path(synchronized), sidecar.source, sidecar.language)
            )
        sync.sidecar_subtitles = output_sidecars

    @staticmethod
    def _convert_sidecar_to_utf8_bom(path: Path, *, destination: Path) -> Path:
        """Stage a text sidecar as UTF-8 with BOM without touching its source.

        Subtitle Edit calls this output form "UTF-8 with BOM".  UTF-8 input is
        retained, while legacy releases fall back to Windows-1252 (then
        ISO-8859-1) so characters such as ``á`` survive synchronization.
        """
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ProcessingError(f"Could not read external subtitle {path}: {exc}") from exc

        try:
            if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
                text = raw.decode("utf-32")
            elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                text = raw.decode("utf-16")
            elif b"\x00" in raw:
                raise ProcessingError(
                    f"External subtitle {path} appears to be binary; only text .sub files "
                    "are supported (VobSub requires its .idx companion and is not supported)"
                )
            else:
                try:
                    text = raw.decode(SIDECAR_TEXT_OUTPUT_ENCODING)
                except UnicodeDecodeError:
                    for encoding in SIDECAR_LEGACY_TEXT_ENCODINGS:
                        try:
                            text = raw.decode(encoding)
                            break
                        except UnicodeDecodeError:
                            continue
                    else:  # pragma: no cover - latin-1 decodes all byte values
                        raise ProcessingError(f"Could not decode external subtitle {path}")
            destination.write_text(text, encoding=SIDECAR_TEXT_OUTPUT_ENCODING)
        except (OSError, UnicodeError) as exc:
            raise ProcessingError(
                f"Could not convert external subtitle {path} to UTF-8 with BOM: {exc}"
            ) from exc
        return destination

    @staticmethod
    def _trim_sidecar(path: Path, *, trim: float, destination: Path, pysubs2) -> Path:
        if trim <= 0:
            return path
        try:
            subtitles = pysubs2.load(str(path))
            trim_ms = round(trim * 1000)
            for event in subtitles:
                event.start -= trim_ms
                event.end -= trim_ms
            subtitles.events = [event for event in subtitles if event.end > 0]
            for event in subtitles:
                event.start = max(event.start, 0)
            subtitles.save(str(destination), encoding=SIDECAR_TEXT_OUTPUT_ENCODING)
        except Exception as exc:
            raise ProcessingError(f"Could not trim external subtitle {path}: {exc}") from exc
        return destination
