from __future__ import annotations

import logging
import math
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path

from .avsync import AVTimelineDecision, reconcile_av_timeline
from .defaults import DEFAULT_TAG
from .errors import (
    AmbiguousPairError,
    DualMakerError,
    TVRipValidationError,
    UserCancelledError,
)
from .fpssync import analyze_fps_timing, validate_fps_timeline
from .matching import (
    collect_pair_candidates,
    discover_mkvs,
    infer_untagged_avi_dub_language,
    require_explicit_pair,
    require_explicit_tvrip_pair,
)
from .metadata import MediaInspector
from .models import DualMakerConfig, JobPlan, JobResult, MediaAsset, PairCandidate, jsonable
from .mux import mux_output
from .planning import create_job_plan
from .preprocess import RecapDecision, choose_recap_trim
from .runner import ToolRunner
from .sidecars import (
    discover_pair_sidecars,
    resolve_sidecar_subtitles,
    sidecar_languages_from_overrides,
)
from .sync import MilksyncAdapter
from .trim import remux_avi_to_mkv, trim_start_copy
from .tvrip import (
    apply_tvrip_audio_policy,
    approve_dub_gap_report,
    approve_tvrip_report,
    build_dub_gap_report,
    build_tvrip_sync_report,
    detected_master_only_intervals,
)

LOGGER = logging.getLogger("dualmaker")


def _experimental_dub_resync_assessment(
    plan: JobPlan,
    sync,
    av_timeline: AVTimelineDecision,
) -> dict[str, object]:
    """Summarize whether a DUAL-source dub has enough mapped evidence.

    Milksync supplies the actual acoustic alignment. This assessment makes the
    experimental import decision inspectable: a complete constant map supports
    a fixed delay, while several buckets/offsets expose edit-aware placement.
    """

    imported_dubs = [
        selection for selection in plan.resolved_dubs if selection.source == "dual"
    ]
    anchors = [
        (float(target), float(source), float(offset))
        for target, source, offset in sync.shift_points
        if all(math.isfinite(float(value)) for value in (target, source, offset))
    ]
    offsets = [point[2] for point in anchors]
    offset_spread = max(offsets) - min(offsets) if offsets else None
    constant_offset = bool(
        offset_spread is not None and offset_spread <= 0.05
    )
    coverage = min(max(float(sync.sync_coverage or 0.0), 0.0), 1.0)
    # One complete, stationary map can prove a fixed release offset. Additional
    # distributed anchors increase confidence in a segmented/edit-aware map.
    anchor_strength = 0.0 if not anchors else min(0.75 + len(anchors) / 12.0, 1.0)
    confidence = coverage * anchor_strength
    correction_segments = [
        {
            "source_start": max(float(start), 0.0),
            "source_end": min(float(end), plan.dual.duration),
            "target_start": max(float(start) + float(offset), 0.0),
            "target_end": min(float(end) + float(offset), plan.normal.duration),
            "offset_seconds": float(offset),
        }
        for start, end, offset in sync.sync_buckets
        if float(end) > float(start) and math.isfinite(float(offset))
    ]
    video_scores = [sample.score for sample in av_timeline.samples]
    video_confirmation = (
        sum(video_scores) / len(video_scores) if video_scores else None
    )
    video_map_errors: list[float] = []
    if plan.alignment_mode == "cross-language-events":
        # A high-scoring visual match is independent of either language.  Use
        # it as a safety check on the acoustic event map: a single recurring
        # musical cue must not be allowed to create a 40-second audio bucket
        # merely because the map also has good nominal coverage.
        for sample in av_timeline.samples:
            if sample.score < 0.80:
                continue
            active_offset = anchors[0][2] if anchors else 0.0
            for _target, source, offset in anchors:
                if source > sample.source_time:
                    break
                active_offset = offset
            video_map_errors.append(active_offset - sample.video_delay)
    maximum_video_map_error = max((abs(error) for error in video_map_errors), default=None)
    video_map_consistent = (
        maximum_video_map_error is None or maximum_video_map_error <= 1.0
    )
    mode = "constant-offset" if constant_offset else "segmented-or-drifting"
    if not imported_dubs:
        reason = "No selected dub is imported from the DUAL source"
    elif not anchors:
        reason = "Milksync produced no finite acoustic anchors"
    elif coverage <= 0:
        reason = "Milksync did not map any usable source-to-master duration"
    elif not video_map_consistent:
        reason = (
            "Cross-language acoustic anchors conflict with independently matched video "
            f"by as much as {maximum_video_map_error:.3f}s; retaining the result only "
            "for manual review"
        )
        # Coverage measures the amount of generated audio, not whether those
        # buckets point at the right scene.  A visual contradiction therefore
        # caps experimental confidence even if a permissive acoustic matcher
        # reported many anchors.
        confidence = min(confidence, 0.20)
    else:
        reason = (
            f"{len(anchors)} {plan.alignment_mode.replace('-', ' ')} acoustic anchor(s), "
            f"{coverage:.1%} mapped "
            f"coverage, and {mode} timing"
        )
    return {
        "enabled": True,
        "alignment_mode": plan.alignment_mode,
        "required": bool(imported_dubs),
        "master_video": str(plan.normal.path),
        "dub_source": str(plan.dual.path),
        "imported_dubs": [selection.label for selection in imported_dubs],
        "mode": mode,
        "anchor_count": len(anchors),
        "anchor_points": [
            {
                "target_seconds": target,
                "source_seconds": source,
                "offset_seconds": offset,
            }
            for target, source, offset in anchors
        ],
        "offset_range_seconds": (
            {"minimum": min(offsets), "maximum": max(offsets), "spread": offset_spread}
            if offsets
            else None
        ),
        "correction_segments": correction_segments,
        "coverage": coverage,
        "anchor_strength": anchor_strength,
        "confidence": confidence,
        "video_confirmation_score": video_confirmation,
        "video_confirmation_reliable": av_timeline.reliable,
        "video_map_consistent": video_map_consistent,
        "video_map_maximum_error_seconds": maximum_video_map_error,
        "reason": reason,
    }


def _resolve_pair_sidecars(candidate, config: DualMakerConfig):
    sidecars = discover_pair_sidecars(candidate)
    if not sidecars:
        return []
    if config.interactive:
        from .tui import select_sidecar_languages

        languages = select_sidecar_languages(sidecars)
    else:
        languages = sidecar_languages_from_overrides(sidecars, config.sidecar_language_overrides)
    return resolve_sidecar_subtitles(
        sidecars,
        languages,
        default_dual_language=config.sidecar_dual_language,
    )


def _interactive_recap(decision: RecapDecision) -> RecapDecision:
    nonzero = [
        candidate
        for candidate in decision.candidates
        if candidate["normal_trim"] > 0 or candidate["dual_trim"] > 0
    ]
    if not nonzero:
        return decision
    from .tui import select_recap_candidate

    selected = select_recap_candidate(decision.reason, nonzero)
    if selected == 0:
        return decision
    candidate = nonzero[selected - 1]
    return RecapDecision(
        normal_trim=candidate["normal_trim"],
        dual_trim=candidate["dual_trim"],
        baseline_score=decision.baseline_score,
        selected_score=candidate["score"],
        applied=True,
        reason="Recap trim selected interactively",
        candidates=decision.candidates,
    )


def _effective_tvrip_policy(config: DualMakerConfig) -> DualMakerConfig:
    """Let the universal missing-dub policy cover HDTV/TVRip-labelled sources.

    Filename classification changes the validation workflow, not the intended
    audio result.  ``tvrip_fallback=ask`` is the historical default; when no
    TVRip-specific fallback was selected, inherit the universal policy so a
    verified missing ending is not silently rendered as silence merely because
    the DUAL filename contains ``HDTV``.  Operators can retain the old review
    behavior with ``dub_gap_fallback: off`` plus ``tvrip_fallback: ask``.
    """

    if config.tvrip_fallback != "ask" or config.dub_gap_fallback == "off":
        return config
    return replace(config, tvrip_fallback=config.dub_gap_fallback)


def process_job(
    plan: JobPlan,
    config: DualMakerConfig,
    *,
    runner: ToolRunner | None = None,
    inspector: MediaInspector | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> JobResult:
    runner = runner or ToolRunner(quiet=config.quiet, binaries=config.binaries)
    inspector = inspector or MediaInspector(runner)
    base_temp = (
        config.temp_dir.expanduser().resolve()
        if config.temp_dir
        else config.path.expanduser().resolve() / config.work_dir_name
    )
    base_temp.mkdir(mode=0o700, parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="dualmaker-", dir=base_temp))
    notify = on_phase or (lambda _message: None)
    tvrip_report = None
    dub_gap_report = None
    try:
        normal_path = plan.normal.path
        dual_path = plan.dual.path
        if normal_path.suffix.lower() == ".avi":
            notify("Losslessly remuxing AVI master to MKV")
            normal_path = remux_avi_to_mkv(
                normal_path, work_dir / "normal-source-remuxed.mkv", runner=runner
            )
        if dual_path.suffix.lower() == ".avi":
            notify("Losslessly remuxing AVI dub source to MKV")
            dual_path = remux_avi_to_mkv(
                dual_path, work_dir / "dual-source-remuxed.mkv", runner=runner
            )
        recap_report: dict[str, object] = {"enabled": config.trim_recap, "applied": False}
        if (
            config.trim_recap
            and plan.source_kind != "tvrip"
            and plan.alignment_mode == "common-original"
        ):
            notify("Analyzing opening recap")
            decision = choose_recap_trim(
                normal_path,
                dual_path,
                plan.normal_original,
                plan.dual_original,
                window=config.recap_window,
                runner=runner,
            )
            if config.interactive and not decision.applied:
                decision = _interactive_recap(decision)
            if (
                not config.interactive
                and not decision.applied
                and decision.baseline_score is not None
                and decision.baseline_score < 0.45
                and any(
                    item["normal_trim"] > 0 or item["dual_trim"] > 0 for item in decision.candidates
                )
            ):
                raise AmbiguousPairError(
                    "Opening audio does not align and no unique recap boundary was validated. Try with --no-trim-recap or --interactive to select a candidate trim, or verify the source files are correct."
                )
            plan.normal_trim = decision.normal_trim
            plan.dual_trim = decision.dual_trim
            plan.reasons.append(decision.reason)
            recap_report = jsonable(decision)
            recap_report["enabled"] = True
            if decision.normal_trim:
                normal_path = trim_start_copy(
                    normal_path,
                    work_dir / "normal-trimmed.mkv",
                    decision.normal_trim,
                    runner=runner,
                )
            if decision.dual_trim:
                dual_path = trim_start_copy(
                    dual_path,
                    work_dir / "dual-trimmed.mkv",
                    decision.dual_trim,
                    runner=runner,
                )
        elif plan.source_kind == "tvrip":
            recap_report = {
                "enabled": False,
                "applied": False,
                "reason": "TVRip openings/recaps are classified by segmented synchronization",
            }
        elif plan.alignment_mode == "cross-language-events":
            recap_report = {
                "enabled": False,
                "applied": False,
                "reason": (
                    "Recap trimming requires common-language dialogue; cross-language "
                    "event anchors retain the complete source timeline"
                ),
            }

        analysis_duration = max(
            min(
                (plan.dual.duration - plan.dual_trim)
                / (plan.fps.proposed_speed_factor if plan.fps.required else 1.0),
                plan.normal.duration - plan.normal_trim,
            ),
            1.0,
        )
        if plan.fps.required:
            notify("Analyzing experimental frame-rate timing")
            plan.fps = analyze_fps_timing(
                dual_path,
                normal_path,
                duration=analysis_duration,
                source_duration=max(plan.dual.duration - plan.dual_trim, 1.0),
                decision=plan.fps,
                config=config,
                work_dir=work_dir,
                runner=runner,
                allow_segmented_mapping=plan.source_kind == "tvrip",
                source_original_duration=(
                    plan.dual_original.duration - plan.dual_trim
                    if plan.dual_original.duration is not None
                    else None
                ),
                target_original_duration=(
                    plan.normal_original.duration - plan.normal_trim
                    if plan.normal_original.duration is not None
                    else None
                ),
            )
            fps_fallback = plan.fps.validation.get("best_effort_fps_fallback")
            if isinstance(fps_fallback, dict) and fps_fallback.get("enabled"):
                LOGGER.warning(
                    "Experimental FPS analysis is inconclusive; continuing with the "
                    "best available %s hypothesis (speed %.9f): %s",
                    fps_fallback.get("selected_strategy", "fallback"),
                    float(fps_fallback.get("selected_speed_factor", 1.0)),
                    fps_fallback.get("reason", "no further detail"),
                )
            analysis_duration = max(
                min(
                    (plan.dual.duration - plan.dual_trim)
                    / (
                        plan.fps.proposed_speed_factor
                        if plan.fps.apply_speed_correction
                        else 1.0
                    ),
                    plan.normal.duration - plan.normal_trim,
                ),
                1.0,
            )

        notify(
            "Comparing cross-language sound events"
            if plan.alignment_mode == "cross-language-events"
            else "Comparing original audio"
        )
        synchronizer = MilksyncAdapter(runner)
        sync = synchronizer.synchronize(
            plan,
            normal_path=normal_path,
            dual_path=dual_path,
            temp_dir=work_dir,
            config=config,
        )
        if plan.fps.required:
            # The common-original spectrogram preflight may refine the visual
            # candidate to a measured program clock before Milksync renders.
            analysis_duration = max(
                min(
                    (plan.dual.duration - plan.dual_trim)
                    / max(sync.speed_correction_factor, 0.000_001),
                    plan.normal.duration - plan.normal_trim,
                ),
                1.0,
            )
        if sync.sync_coverage is not None:
            sync_reason = f"validated common-track sync coverage {sync.sync_coverage:.1%}"
            for selection in [*plan.resolved_dubs, plan.resolved_original]:
                selection.reasons.append(sync_reason)
        av_timeline = AVTimelineDecision(
            enabled=config.reconcile_av or plan.fps.required,
            reason="A/V reconciliation disabled by configuration",
        )
        if config.reconcile_av or plan.fps.required:
            # The acoustic map establishes the piecewise correspondence, but
            # only video anchors can prove its constant placement relative to
            # the immutable master video. This remains necessary for TVRip
            # inputs: treating their map as authoritative used to leave a
            # season-wide residual uncorrected.
            notify("Reconciling audio and video timelines")
            av_timeline = reconcile_av_timeline(
                dual_path,
                normal_path,
                duration=analysis_duration,
                shift_points=sync.shift_points,
                manual_delay=sync.manual_delay_adjustment,
                tolerance_ms=config.av_tolerance_ms,
                work_dir=work_dir,
                runner=runner,
                source_time_scale=(
                    plan.fps.proposed_speed_factor
                    if plan.fps.apply_speed_correction
                    else 1.0
                ),
                search_radius_seconds=(
                    config.fps_search_radius_seconds if plan.fps.required else None
                ),
                sample_positions=(
                    config.fps_validation_positions if plan.fps.required else None
                ),
            )
            sync.timeline_adjustment_ms = av_timeline.adjustment_ms
            if av_timeline.applied:
                LOGGER.warning(
                    "Applying shared A/V timeline correction %+.3fs "
                    "(audio mapping %+.3fs, video mapping %+.3fs)",
                    av_timeline.adjustment_ms / 1000,
                    av_timeline.audio_delay or 0.0,
                    av_timeline.video_delay or 0.0,
                )
            elif not av_timeline.reliable:
                spectral_probe = plan.fps.validation.get("spectral_tempo_probe", {})
                if (
                    plan.fps.required
                    and isinstance(spectral_probe, dict)
                    and spectral_probe.get("reliable")
                ):
                    LOGGER.info(
                        "Auxiliary video-only A/V reconciliation was inconclusive (%s); "
                        "the common-original acoustic map remains subject to post-sync "
                        "and per-segment validation",
                        av_timeline.reason,
                    )
                else:
                    LOGGER.warning("A/V timeline reconciliation skipped: %s", av_timeline.reason)
        dub_resync = _experimental_dub_resync_assessment(plan, sync, av_timeline)
        dub_resync["minimum_confidence"] = config.experimental_dub_resync_min_confidence
        if dub_resync["required"]:
            confidence = float(dub_resync["confidence"])
            trusted = confidence >= config.experimental_dub_resync_min_confidence
            dub_resync["accepted"] = True
            dub_resync["trusted"] = trusted
            LOGGER.info(
                "Experimental dubbed-audio resync: %s; confidence %.1f%% "
                "(minimum %.1f%%)",
                dub_resync["reason"],
                confidence * 100,
                config.experimental_dub_resync_min_confidence * 100,
            )
            LOGGER.debug(
                "Experimental dubbed-audio anchors: %s; correction segments: %s",
                dub_resync["anchor_points"],
                dub_resync["correction_segments"],
            )
            if not trusted:
                evidence_name = (
                    "the cross-language event-anchor pass"
                    if plan.alignment_mode == "cross-language-events"
                    else "the shorter sound-event anchor retry"
                )
                warning = (
                    "Dubbed-audio resync remains below the configured confidence after "
                    f"{evidence_name}; retaining the best available "
                    f"map (confidence {confidence:.1%}, minimum "
                    f"{config.experimental_dub_resync_min_confidence:.1%})"
                )
                dub_resync["warnings"] = [warning]
                LOGGER.warning("%s", warning)
        else:
            dub_resync["accepted"] = True
            dub_resync["trusted"] = True
        fps_validation = validate_fps_timeline(
            plan.fps,
            av_timeline,
            shift_points=sync.shift_points,
            manual_delay=sync.manual_delay_adjustment,
            timeline_adjustment_ms=sync.timeline_adjustment_ms,
            maximum_drift=config.fps_max_drift_seconds,
            segmented_min_samples=config.fps_segmented_min_post_map_anchors,
            segmented_min_span_seconds=config.fps_segmented_min_post_map_span_seconds,
            spectral_min_samples=config.fps_spectral_min_post_map_anchors,
            audio_sync_coverage=sync.sync_coverage,
            minimum_audio_coverage=config.tvrip_min_coverage,
        )
        if plan.fps.required and not fps_validation["validated"]:
            LOGGER.warning(
                "Experimental FPS validation is inconclusive; keeping the best available "
                "synchronization map for review: %s",
                fps_validation.get("reason", "no further detail"),
            )
        if plan.source_kind == "tvrip":
            tvrip_policy_config = _effective_tvrip_policy(config)
            notify("Validating TVRip content segments")
            tvrip_report = build_tvrip_sync_report(
                plan,
                sync,
                source_path=dual_path,
                master_path=normal_path,
                config=tvrip_policy_config,
                work_dir=work_dir,
                runner=runner,
            )
            if tvrip_policy_config is not config:
                tvrip_report.warnings.append(
                    "TVRip fallback inherited the universal dub-gap policy: "
                    f"{tvrip_policy_config.tvrip_fallback}"
                )
            notify("Reviewing TVRip segment policy")
            tvrip_report = approve_tvrip_report(tvrip_report, plan, tvrip_policy_config)
            apply_tvrip_audio_policy(
                plan,
                sync,
                tvrip_report,
                master_path=normal_path,
                dual_path=dual_path,
                work_dir=work_dir,
                config=tvrip_policy_config,
                runner=runner,
            )
        elif (
            config.dub_gap_fallback != "off"
            and plan.alignment_mode == "common-original"
        ):
            candidate_gaps = detected_master_only_intervals(
                plan,
                sync,
                minimum_seconds=config.dub_gap_min_seconds,
            )
            if candidate_gaps:
                notify("Validating missing Portuguese-dub sections")
                dub_gap_report = build_dub_gap_report(
                    plan,
                    sync,
                    source_path=dual_path,
                    master_path=normal_path,
                    config=config,
                    work_dir=work_dir,
                    runner=runner,
                )
                try:
                    notify("Reviewing Portuguese-dub fallback safety")
                    dub_gap_report = approve_dub_gap_report(dub_gap_report, plan, config)
                except TVRipValidationError as exc:
                    # The regular DUAL workflow already has a valid synchronized output with
                    # silence in uncovered ranges. Do not turn an unproven fallback into a
                    # failed release, and never overwrite Portuguese with original audio when
                    # the map cannot be fully validated.
                    dub_gap_report = exc.report
                    dub_gap_report.warnings.append(
                        "Master-original fallback was withheld; the synchronized DUAL audio "
                        "was retained unchanged."
                    )
                    LOGGER.warning("Dub-gap fallback withheld: %s", exc)
                else:
                    if (
                        dub_gap_report.fallback == "original"
                        and dub_gap_report.master_only
                    ):
                        notify("Filling missing dub sections with master original audio")
                        gap_policy_config = replace(
                            config,
                            tvrip_fallback="original",
                            tvrip_min_coverage=config.dub_gap_min_coverage,
                            tvrip_track_title=config.dub_gap_track_title,
                            tvrip_allow_partial_tracks=True,
                        )
                        apply_tvrip_audio_policy(
                            plan,
                            sync,
                            dub_gap_report,
                            master_path=normal_path,
                            dual_path=dual_path,
                            work_dir=work_dir,
                            config=gap_policy_config,
                            runner=runner,
                        )
        if plan.sidecar_subtitles:
            notify("Synchronizing external subtitles")
            synchronizer.synchronize_sidecars(
                plan,
                sync,
                normal_path=normal_path,
                dual_path=dual_path,
                temp_dir=work_dir,
                config=config,
            )
        output, _, validation = mux_output(
            plan,
            sync,
            normal_path=normal_path,
            dual_path=dual_path,
            work_dir=work_dir,
            config=config,
            runner=runner,
            inspector=inspector,
            on_phase=notify,
        )
        validation["recap"] = recap_report
        validation["av_timeline"] = jsonable(av_timeline)
        validation["experimental_fps"] = jsonable(plan.fps)
        validation["experimental_fps_validation"] = fps_validation
        validation["experimental_dub_resync"] = dub_resync
        validation["experimental_tvrip"] = jsonable(tvrip_report)
        validation["dub_gap_fallback"] = jsonable(dub_gap_report)
        validation["audio_selection"] = {
            "dubs": jsonable(plan.resolved_dubs),
            "original": jsonable(plan.resolved_original),
        }
        validation["sidecar_subtitles"] = {
            "inputs": jsonable(plan.sidecar_subtitles),
            "synchronized": jsonable(sync.sidecar_subtitles),
        }
        validation["synchronization"] = {
            "source_reference_start": sync.source_reference_start,
            "target_reference_start": sync.target_reference_start,
            "source_dub_starts": sync.source_dub_starts,
            "observed_reference_pts_offset": sync.observed_reference_pts_offset,
            "container_delay_adjustment": sync.container_delay_adjustment,
            "manual_delay_adjustment": sync.manual_delay_adjustment,
            "effective_delay_adjustment": sync.effective_delay_adjustment,
            "output_audio_mapping": sync.output_audio_mapping,
            "stage_original_index": sync.stage_original_index,
            "timeline_adjustment_ms": sync.timeline_adjustment_ms,
            "speed_correction_factor": sync.speed_correction_factor,
            "codec_fallbacks": sync.codec_fallbacks,
            "sync_coverage": sync.sync_coverage,
            "sync_buckets": sync.sync_buckets,
            "delete_buckets": sync.delete_buckets,
        }
        return JobResult(
            status="success",
            output=output,
            message="Created and validated output",
            plan=plan,
            sync_points=sync.shift_points,
            validation=validation,
        )
    except UserCancelledError:
        raise
    except TVRipValidationError as exc:
        LOGGER.debug("TVRip job rejected", exc_info=True)
        detail = str(exc)
        if config.keep_temp:
            detail += f"; temporary files kept at {work_dir}"
        return JobResult(
            status="skipped",
            message=detail,
            plan=plan,
            validation={"experimental_tvrip": jsonable(exc.report)},
        )
    except Exception as exc:
        LOGGER.debug("Job failed", exc_info=True)
        detail = str(exc)
        if config.keep_temp:
            detail += f"; temporary files kept at {work_dir}"
        return JobResult(status="failed", message=detail, plan=plan)
    finally:
        if not config.keep_temp:
            shutil.rmtree(work_dir, ignore_errors=True)


def scan_assets(
    path: Path,
    *,
    recursive: bool,
    inspector: MediaInspector,
    config: DualMakerConfig | None = None,
) -> tuple[list[MediaAsset], list[str]]:
    assets: list[MediaAsset] = []
    errors: list[str] = []
    from .naming import parse_identity

    ignored_paths: tuple[Path, ...] = ()
    ignored_names = config.ignored_dir_names if config else ()
    tag = config.tag if config else DEFAULT_TAG
    if config:
        root = path.expanduser().resolve()
        # Outputs must never become new inputs.  Use resolved directory boundaries
        # instead of the configurable release-group tag: a source named
        # ``...DUAL-RiPER.mkv`` is perfectly valid when the output group is RiPER.
        output_root = config.output_dir or root / config.output_dir_name
        work_root = config.temp_dir or root / config.work_dir_name
        ignored_paths = tuple(
            item.expanduser().resolve()
            for item in (output_root, work_root)
            if item is not None
        )
    discovery_options = {
        "ignored_paths": ignored_paths,
        "tag": tag,
    }
    if ignored_names:
        discovery_options["ignored_dir_names"] = ignored_names
    for media_path in discover_mkvs(path, recursive=recursive, **discovery_options):
        try:
            asset = inspector.inspect(media_path)
            if config:
                infer_untagged_avi_dub_language(asset, config.dub_language)
            asset.identity = parse_identity(media_path)
            assets.append(asset)
        except DualMakerError as exc:
            errors.append(f"{media_path}: {exc}")
    return assets, errors


def plan_batch(
    config: DualMakerConfig,
    *,
    inspector: MediaInspector | None = None,
) -> tuple[list[JobPlan], list[str], list[MediaAsset]]:
    inspector = inspector or MediaInspector()
    root = config.path.expanduser().resolve()
    assets, skipped = scan_assets(
        root, recursive=config.recursive, inspector=inspector, config=config
    )
    grouped, unmatched = collect_pair_candidates(assets)
    skipped.extend(unmatched)
    candidate_languages: dict[tuple[object, ...], str] = {}
    if config.interactive:
        from .tui import select_candidates_interactively

        selection = select_candidates_interactively(grouped)
        candidates = selection.candidates
        candidate_languages = selection.original_languages
    else:
        candidates = []
        for identity, options in grouped.items():
            if len(options) > 1 and options[0].score - options[1].score < 0.05:
                choices = ", ".join(
                    f"{item.dual.path.name} + {item.normal.path.name} ({item.score:.3f})"
                    for item in options[:4]
                )
                skipped.append(f"{identity}: ambiguous candidates: {choices}")
            else:
                candidates.append(options[0])

    return plan_candidates(
        config,
        candidates,
        candidate_languages=candidate_languages,
        assets=assets,
        skipped=skipped,
    )


def plan_candidates(
    config: DualMakerConfig,
    candidates: Iterable[PairCandidate],
    *,
    candidate_languages: Mapping[tuple[object, ...], str] | None = None,
    assets: list[MediaAsset] | None = None,
    skipped: list[str] | None = None,
) -> tuple[list[JobPlan], list[str], list[MediaAsset]]:
    """Turn explicitly selected match candidates into executable job plans.

    The terminal TUI, desktop GUI, and future integrations can all share this
    boundary: discovery and selection happen in the front end, while planning
    and all media policy remain in the pipeline.
    """

    plans: list[JobPlan] = []
    skipped = list(skipped or [])
    candidate_languages = candidate_languages or {}
    candidates = list(candidates)
    root = config.path.expanduser().resolve()
    default_output_root = config.output_dir or root / config.output_dir_name
    for candidate in candidates:
        try:
            relative_parent = (
                candidate.normal.path.parent.relative_to(root) if config.recursive else Path()
            )
            job_config = replace(
                config,
                output_dir=default_output_root / relative_parent,
                original_language=candidate_languages.get(
                    candidate.identity.key, config.original_language
                ),
            )
            sidecars = _resolve_pair_sidecars(candidate, job_config)
            plans.append(create_job_plan(candidate, job_config, sidecar_subtitles=sidecars))
        except (DualMakerError, ValueError) as exc:
            skipped.append(f"{candidate.identity.key}: {exc}")
    return plans, skipped, assets if assets is not None else []


def plan_explicit(
    dual: Path,
    normal: Path,
    config: DualMakerConfig,
    *,
    inspector: MediaInspector | None = None,
    tvrip: bool = False,
) -> JobPlan:
    inspector = inspector or MediaInspector()
    normal_asset = inspector.inspect(normal)
    dual_asset = inspector.inspect(dual)
    infer_untagged_avi_dub_language(dual_asset, config.dub_language)
    candidate = (
        require_explicit_tvrip_pair(normal_asset, dual_asset)
        if tvrip
        else require_explicit_pair(normal_asset, dual_asset)
    )
    if (
        config.interactive
        and candidate.alignment_mode == "common-original"
        and not config.original_language
    ):
        from .tui import select_original_language

        config = replace(config, original_language=select_original_language(candidate))
    explicit_config = config
    if explicit_config.output_dir is None and explicit_config.output is None:
        explicit_config = replace(
            config, output_dir=normal_asset.path.parent / config.output_dir_name
        )
    sidecars = _resolve_pair_sidecars(candidate, explicit_config)
    return create_job_plan(candidate, explicit_config, sidecar_subtitles=sidecars)
