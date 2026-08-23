from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import click

from . import __version__
from .configuration import (
    configuration_as_dict,
    default_user_config_path,
    initialize_config_file,
    load_configuration,
    refresh_config_file,
    validate_configuration,
)
from .defaults import (
    DEFAULT_AUDIO_SELECTION_MARGIN,
    DEFAULT_AV_TOLERANCE_MS,
    DEFAULT_COLOR_MODE,
    DEFAULT_CONFLICT_POLICY,
    DEFAULT_DUB_LANGUAGE,
    DEFAULT_END_TOLERANCE_MS,
    DEFAULT_FPS_ANCHOR_CANDIDATE_COUNT,
    DEFAULT_FPS_ANCHOR_GLOBAL_COVERAGE,
    DEFAULT_FPS_ANCHOR_MIN_SEPARATION_SECONDS,
    DEFAULT_FPS_ANCHOR_SAMPLE_COUNT,
    DEFAULT_FPS_ANCHOR_WINDOW_SECONDS,
    DEFAULT_FPS_AUDIO_DURATION_RATIO_TOLERANCE,
    DEFAULT_FPS_MAX_DRIFT_SECONDS,
    DEFAULT_FPS_MIN_MATCH_CONFIDENCE,
    DEFAULT_FPS_SEARCH_RADIUS_SECONDS,
    DEFAULT_FPS_SEGMENTED_MIN_POST_MAP_ANCHORS,
    DEFAULT_FPS_SEGMENTED_MIN_POST_MAP_SPAN_SECONDS,
    DEFAULT_FPS_SPECTRAL_MAX_DISPERSION,
    DEFAULT_FPS_SPECTRAL_MAX_PROJECTED_DRIFT_SECONDS,
    DEFAULT_FPS_SPECTRAL_MAX_REFINEMENT_PASSES,
    DEFAULT_FPS_SPECTRAL_MAX_SPEED_ADJUSTMENT,
    DEFAULT_FPS_SPECTRAL_MIN_PAIRS,
    DEFAULT_FPS_SPECTRAL_MIN_POST_MAP_ANCHORS,
    DEFAULT_FPS_SPECTRAL_PAIR_MAX_SECONDS,
    DEFAULT_FPS_SPECTRAL_PAIR_MIN_SECONDS,
    DEFAULT_FPS_SPECTRAL_REFINEMENT_DAMPING,
    DEFAULT_FPS_SPECTRAL_SLOPE_CLUSTER_RADIUS,
    DEFAULT_OUTPUT_FORMAT,
    DEFAULT_RECAP_WINDOW,
    DEFAULT_SUBTITLE_POLICY,
    DEFAULT_TAG,
    ENV_PREFIX,
)
from .errors import (
    ConfigurationError,
    DualMakerError,
    ExperimentalFPSRequiredError,
    ExperimentalTVRipRequiredError,
    OutputConflictError,
    UserCancelledError,
)
from .metadata import MediaInspector
from .models import DualMakerConfig, JobResult, jsonable
from .pipeline import plan_batch, plan_explicit, process_job
from .reporting import default_report_path, write_report
from .runner import ToolRunner, check_dependencies
from .ui import TerminalUI

EPILOG = """\b
Examples:
  dualmaker                         Process pairs in the current folder
  dualmaker /media/releases         Process one folder
  dualmaker /media/shows --recursive
  dualmaker /media/releases --dry-run
  dualmaker /media/releases --interactive
  dualmaker /media/releases --json --dry-run
  dualmaker --dual dub.mkv --normal master.mkv
  dualmaker --tvrip broadcast.mkv --normal master.mkv --interactive
  dualmaker --tvrip broadcast.mkv --normal master.mkv --allow-tvrip-segment-sync --tvrip-fallback silence
  dualmaker --check-deps
  dualmaker --init-config
  dualmaker --refresh-config
  dualmaker --show-config

\b
Configuration precedence:
  command line > DUALMAKER_* environment > YAML/TOML file > built-in defaults

The preferred user file is ~/.dualmaker/config.yml and is created on the first
operational run. Local dualmaker.yml, dualmaker.yaml, and dualmaker.toml files
are also discovered. Use --config for an explicit file. Inputs are never modified.
"""

OPTION_GROUPS = {
    "Configuration and policy": {
        "config_file",
        "init_config",
        "refresh_config",
        "show_config",
        "allowed_paths",
        "required_paths",
        "enforce_paths",
        "required_group",
        "output_group",
        "output_dir_name",
        "work_dir_name",
        "ignored_dir_names",
        "ffmpeg_binary",
        "ffprobe_binary",
        "mediainfo_binary",
        "mkvmerge_binary",
        "mkvextract_binary",
        "mkvpropedit_binary",
        "minimum_mkvmerge_version",
    },
    "Discovery and output": {
        "recursive",
        "output_dir",
        "output",
        "tag",
        "conflict",
        "dual_file",
        "tvrip_file",
        "normal_file",
    },
    "Track selection": {
        "dub_language",
        "original_language",
        "dual_audio_ids",
        "normal_audio_ids",
        "dub_track_selectors",
        "original_track_selector",
        "preferred_original_source",
        "preferred_dub_source",
        "audio_codec_preference",
        "audio_selection_margin",
        "subtitle_policy",
        "sidecar_language_overrides",
        "sidecar_dual_language",
    },
    "Processing": {
        "trim_recap",
        "recap_window",
        "end_trim",
        "end_tolerance_ms",
        "reconcile_av",
        "av_tolerance_ms",
        "allow_experimental_fps_sync",
        "compatible_fps_pairs",
        "fps_max_drift_seconds",
        "fps_min_match_confidence",
        "fps_validation_positions",
        "fps_search_radius_seconds",
        "fps_speed_ratio_tolerance",
        "fps_content_speed_factors",
        "fps_audio_duration_ratio_tolerance",
        "fps_spectral_tempo_probe",
        "fps_spectral_min_pairs",
        "fps_spectral_pair_min_seconds",
        "fps_spectral_pair_max_seconds",
        "fps_spectral_max_dispersion",
        "fps_spectral_slope_cluster_radius",
        "fps_spectral_max_speed_adjustment",
        "fps_spectral_max_projected_drift_seconds",
        "fps_spectral_max_refinement_passes",
        "fps_spectral_refinement_damping",
        "fps_spectral_iterative_refinement",
        "fps_spectral_min_post_map_anchors",
        "fps_adaptive_anchors",
        "fps_anchor_sample_count",
        "fps_anchor_candidate_count",
        "fps_anchor_window_seconds",
        "fps_anchor_min_separation_seconds",
        "fps_anchor_global_coverage",
        "fps_segmented_min_post_map_anchors",
        "fps_segmented_min_post_map_span_seconds",
        "dub_gap_fallback",
        "dub_gap_min_seconds",
        "dub_gap_min_coverage",
        "dub_gap_track_title",
        "temp_dir",
        "keep_temp",
    },
    "Experimental TVRip synchronization": {
        "allow_tvrip_segment_sync",
        "tvrip_min_source_match_confidence",
        "tvrip_min_segment_confidence",
        "tvrip_spectral_min_segment_confidence",
        "tvrip_spectral_min_source_match_confidence",
        "tvrip_max_residual_seconds",
        "tvrip_min_coverage",
        "tvrip_max_segments",
        "tvrip_continue_on_validation_warnings",
        "tvrip_min_segment_seconds",
        "tvrip_max_segment_seconds",
        "tvrip_break_sensitivity_seconds",
        "tvrip_commercial_min_seconds",
        "tvrip_retain_alternative_sections",
        "tvrip_fallback",
        "tvrip_allow_speed_correction",
        "tvrip_max_speed_adjustment",
        "tvrip_require_interactive_approval",
        "tvrip_allow_partial_tracks",
        "tvrip_validation_positions",
        "tvrip_validation_window_seconds",
        "tvrip_validation_search_seconds",
        "tvrip_track_title",
    },
    "Interface and reporting": {
        "dry_run",
        "interactive",
        "report",
        "output_format",
        "json_output",
        "color",
        "progress",
        "verbose",
        "quiet",
        "check_deps",
    },
    "Advanced synchronization": {
        "align_framerate",
        "align_frames_too",
        "only_delta",
        "adjust_delay",
        "preserve_silence",
    },
}


class GroupedCommand(click.Command):
    """Keep the large command surface readable without hiding advanced controls."""

    def format_options(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        grouped: dict[str, list[tuple[str, str]]] = {name: [] for name in OPTION_GROUPS}
        grouped["Other"] = []
        for parameter in self.get_params(ctx):
            record = parameter.get_help_record(ctx)
            if record is None:
                continue
            section = next(
                (name for name, members in OPTION_GROUPS.items() if parameter.name in members),
                "Other",
            )
            grouped[section].append(record)
        for heading, records in grouped.items():
            if records:
                with formatter.section(heading):
                    formatter.write_dl(records)


def _configure_logging(config: DualMakerConfig) -> None:
    level = logging.ERROR if config.quiet else logging.DEBUG if config.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def _result_summary(result: JobResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "output": str(result.output) if result.output else None,
        "message": result.message,
        "identity": jsonable(result.plan.identity) if result.plan else None,
        "dual": str(result.plan.dual.path) if result.plan else None,
        "normal": str(result.plan.normal.path) if result.plan else None,
    }


def _exit_error(
    config: DualMakerConfig,
    message: str,
    *,
    code: int,
    hint: str | None = None,
) -> None:
    TerminalUI(config).error(message, hint=hint)
    raise click.exceptions.Exit(code)


@click.command(
    cls=GroupedCommand,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 110},
    epilog=EPILOG,
)
@click.argument("path", type=click.Path(path_type=Path), default=None, required=False)
@click.option(
    "--config",
    "config_file",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Explicit YAML or TOML configuration file.",
)
@click.option(
    "--init-config",
    is_flag=True,
    help="Create a private user YAML config if missing, then exit.",
)
@click.option(
    "--refresh-config",
    is_flag=True,
    help="Rewrite a YAML config with every current setting/comment and create a timestamped backup.",
)
@click.option("--show-config", is_flag=True, help="Validate and display resolved configuration.")
@click.option(
    "--recursive/--no-recursive",
    default=None,
    help="Scan subdirectories and preserve their layout.",
)
@click.option("--output-dir", type=click.Path(path_type=Path), help="Batch output root.")
@click.option("--output", type=click.Path(path_type=Path), help="Output file for explicit mode.")
@click.option("--tag", default=None, help=f"Release tag in output names (default: {DEFAULT_TAG}).")
@click.option(
    "--on-conflict",
    "conflict",
    type=click.Choice(["increment", "skip", "error"]),
    default=None,
    help=f"Existing-output policy (default: {DEFAULT_CONFLICT_POLICY}).",
)
@click.option("--dual", "dual_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--tvrip",
    "tvrip_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="TVRip/broadcast source for the experimental segmented workflow.",
)
@click.option(
    "--normal", "normal_file", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--dub-language", default=None, help=f"Dub language (default: {DEFAULT_DUB_LANGUAGE})."
)
@click.option("--original-language", help="Resolve a shared original language explicitly.")
@click.option(
    "--dual-audio",
    "dual_audio_ids",
    type=int,
    multiple=True,
    help="DUAL MKV track ID; repeatable.",
)
@click.option(
    "--normal-audio",
    "normal_audio_ids",
    type=int,
    multiple=True,
    help="Normal MKV track ID; repeatable.",
)
@click.option(
    "--dub-track",
    "dub_track_selectors",
    multiple=True,
    help="Retain a dub as master:ID or dual:ID; repeatable and ordered.",
)
@click.option(
    "--original-track",
    "original_track_selector",
    help="Select final original audio explicitly as master:ID or dual:ID.",
)
@click.option(
    "--prefer-original-source",
    "preferred_original_source",
    type=click.Choice(["master", "dual", "quality"]),
    default=None,
    help="Tie-break preference for original audio (default: master).",
)
@click.option(
    "--prefer-dub-source",
    "preferred_dub_source",
    type=click.Choice(["master", "dual", "quality"]),
    default=None,
    help="Tie-break preference for dub audio (default: dual).",
)
@click.option(
    "--audio-codec-preference",
    multiple=True,
    help="Codec IDs from best to worst; repeatable.",
)
@click.option(
    "--audio-selection-margin",
    type=click.FloatRange(min=0),
    default=None,
    help=f"Maximum score gap treated as ambiguous (default: {DEFAULT_AUDIO_SELECTION_MARGIN:g}).",
)
@click.option(
    "--dub-gap-fallback",
    type=click.Choice(["original", "silence", "off"]),
    default=None,
    help=(
        "For verified master scenes missing from a DUAL dub: use the master original "
        "audio, retain silence, or disable this repair (default: original)."
    ),
)
@click.option(
    "--dub-gap-min-seconds",
    type=click.FloatRange(min=0),
    default=None,
    help="Ignore shorter master-only gaps; they are too small to classify safely (default: 1s).",
)
@click.option(
    "--dub-gap-min-coverage",
    type=click.FloatRange(min=0, max=1),
    default=None,
    help="Minimum validated DUAL-dub coverage before original-language fallback is allowed.",
)
@click.option(
    "--dub-gap-track-title",
    help="Format for repaired dub tracks; accepts {mode}, {coverage}, and {fallback}.",
)
@click.option(
    "--subtitle-policy",
    type=click.Choice(["prefer-master", "exact-union"]),
    default=None,
    help=(
        "Subtitle selection: prefer master per language/forced/SDH slot, or retain the "
        f"exact-deduplicated union (default: {DEFAULT_SUBTITLE_POLICY})."
    ),
)
@click.option(
    "--sidecar-language",
    "sidecar_language_overrides",
    multiple=True,
    help=(
        "External subtitle language as PATH=LANGUAGE; repeatable. Interactive mode asks "
        "for every discovered sidecar."
    ),
)
@click.option(
    "--sidecar-dual-language",
    type=str,
    default=None,
    metavar="LANGUAGE",
    help=(
        "Default language for sidecars named after the DUAL source (default: pt-BR). "
        "Explicit --sidecar-language values take precedence."
    ),
)
@click.option("--trim-recap/--no-trim-recap", default=None, help="Detect a one-sided recap.")
@click.option(
    "--recap-window",
    type=click.FloatRange(min=10),
    default=None,
    help=f"Opening analysis seconds (default: {DEFAULT_RECAP_WINDOW:g}).",
)
@click.option(
    "--end-trim/--no-end-trim",
    default=None,
    help="Conservatively trim video beyond selected audio.",
)
@click.option(
    "--end-tolerance-ms",
    type=click.IntRange(min=0),
    default=None,
    help=f"Allowed end overrun (default: {DEFAULT_END_TOLERANCE_MS} ms).",
)
@click.option(
    "--reconcile-av/--no-reconcile-av",
    default=None,
    help="Compare both video timelines and correct a shared residual A/V offset.",
)
@click.option(
    "--av-tolerance-ms",
    type=click.IntRange(min=0),
    default=None,
    help=f"Ignore residual A/V offsets smaller than this value (default: {DEFAULT_AV_TOLERANCE_MS} ms).",
)
@click.option(
    "--allow-experimental-fps-sync/--no-allow-experimental-fps-sync",
    default=None,
    help="Explicitly permit the beta workflow when exact input frame rates differ.",
)
@click.option(
    "--compatible-fps-pair",
    "compatible_fps_pairs",
    multiple=True,
    help="Allowed rational pair RATE=RATE; repeatable.",
)
@click.option(
    "--fps-max-drift-seconds",
    type=click.FloatRange(min=0),
    default=None,
    help=f"Maximum beta validation error (default: {DEFAULT_FPS_MAX_DRIFT_SECONDS:g}s).",
)
@click.option(
    "--fps-min-match-confidence",
    type=click.FloatRange(min=0, max=1),
    default=None,
    help=f"Minimum beta content-anchor confidence (default: {DEFAULT_FPS_MIN_MATCH_CONFIDENCE:g}).",
)
@click.option(
    "--fps-validation-position",
    "fps_validation_positions",
    type=click.FloatRange(min=0, max=1, min_open=True, max_open=True),
    multiple=True,
    help="Fractional beta validation position (0–1); repeat at least three times.",
)
@click.option(
    "--fps-search-radius-seconds",
    type=click.FloatRange(min=1),
    default=None,
    help=f"Content-anchor search radius (default: {DEFAULT_FPS_SEARCH_RADIUS_SECONDS:g}s).",
)
@click.option(
    "--fps-speed-ratio-tolerance",
    type=click.FloatRange(min=0),
    default=None,
    help="Allowed error between detected linear speed and the rational FPS ratio.",
)
@click.option(
    "--fps-content-speed-factor",
    "fps_content_speed_factors",
    multiple=True,
    help="Standard source/master content-speed ratio such as 24/25; repeatable.",
)
@click.option(
    "--fps-audio-duration-ratio-tolerance",
    type=click.FloatRange(min=0, max=0.25, max_open=True),
    default=None,
    help=(
        "Maximum distance from a standard content-speed ratio nominated by the common "
        f"audio durations (default: {DEFAULT_FPS_AUDIO_DURATION_RATIO_TOLERANCE:g})."
    ),
)
@click.option(
    "--fps-spectral-tempo-probe/--no-fps-spectral-tempo-probe",
    default=None,
    help="Measure the content clock from common-original Milksync spectrogram anchors.",
)
@click.option(
    "--fps-spectral-min-pairs",
    type=click.IntRange(min=3),
    default=None,
    help=f"Minimum robust acoustic point pairs (default: {DEFAULT_FPS_SPECTRAL_MIN_PAIRS}).",
)
@click.option(
    "--fps-spectral-pair-min-seconds",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help=f"Minimum acoustic pair span (default: {DEFAULT_FPS_SPECTRAL_PAIR_MIN_SECONDS:g}s).",
)
@click.option(
    "--fps-spectral-pair-max-seconds",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help=f"Maximum acoustic pair span (default: {DEFAULT_FPS_SPECTRAL_PAIR_MAX_SECONDS:g}s).",
)
@click.option(
    "--fps-spectral-max-dispersion",
    type=click.FloatRange(min=0),
    default=None,
    help=f"Maximum robust acoustic slope dispersion (default: {DEFAULT_FPS_SPECTRAL_MAX_DISPERSION:g}).",
)
@click.option(
    "--fps-spectral-slope-cluster-radius",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help=f"Acoustic slope-cluster half-width (default: {DEFAULT_FPS_SPECTRAL_SLOPE_CLUSTER_RADIUS:g}).",
)
@click.option(
    "--fps-spectral-max-speed-adjustment",
    type=click.FloatRange(min=0, max=0.5, min_open=True, max_open=True),
    default=None,
    help=f"Largest acoustic tempo correction (default: {DEFAULT_FPS_SPECTRAL_MAX_SPEED_ADJUSTMENT:.0%}).",
)
@click.option(
    "--fps-spectral-max-projected-drift-seconds",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help=f"Maximum full-runtime drift left by a post-sync acoustic slope (default: {DEFAULT_FPS_SPECTRAL_MAX_PROJECTED_DRIFT_SECONDS:g}s).",
)
@click.option(
    "--fps-spectral-max-refinement-passes",
    type=click.IntRange(min=0, max=5),
    default=None,
    help=f"Additional render passes allowed to remove residual clock drift (default: {DEFAULT_FPS_SPECTRAL_MAX_REFINEMENT_PASSES}).",
)
@click.option(
    "--fps-spectral-refinement-damping",
    type=click.FloatRange(min=0, max=1, min_open=True),
    default=None,
    help=f"Unbracketed residual correction fraction (default: {DEFAULT_FPS_SPECTRAL_REFINEMENT_DAMPING:g}).",
)
@click.option(
    "--fps-spectral-iterative-refinement/--no-fps-spectral-iterative-refinement",
    default=None,
    help="Opt into additional Milksync renders for residual clock refinement.",
)
@click.option(
    "--fps-spectral-min-post-map-anchors",
    type=click.IntRange(min=1),
    default=None,
    help=f"Post-map video confirmations after strong pre/post acoustic proof (default: {DEFAULT_FPS_SPECTRAL_MIN_POST_MAP_ANCHORS}).",
)
@click.option(
    "--fps-adaptive-anchors/--no-fps-adaptive-anchors",
    default=None,
    help="After fixed FPS checks fail, search full timelines for informative matching scenes.",
)
@click.option(
    "--fps-anchor-sample-count",
    type=click.IntRange(min=3),
    default=None,
    help=f"Distributed master scenes considered by adaptive FPS discovery (default: {DEFAULT_FPS_ANCHOR_SAMPLE_COUNT}).",
)
@click.option(
    "--fps-anchor-candidate-count",
    type=click.IntRange(min=1),
    default=None,
    help=f"Distinct source candidates checked per adaptive scene (default: {DEFAULT_FPS_ANCHOR_CANDIDATE_COUNT}).",
)
@click.option(
    "--fps-anchor-window-seconds",
    type=click.FloatRange(min=2),
    default=None,
    help=f"Visual context required to verify an adaptive anchor (default: {DEFAULT_FPS_ANCHOR_WINDOW_SECONDS:g}s).",
)
@click.option(
    "--fps-anchor-min-separation-seconds",
    type=click.FloatRange(min=0.1),
    default=None,
    help=f"Minimum spacing between accepted adaptive anchors (default: {DEFAULT_FPS_ANCHOR_MIN_SEPARATION_SECONDS:g}s).",
)
@click.option(
    "--fps-anchor-global-coverage",
    type=click.FloatRange(min=0, max=1, min_open=True),
    default=None,
    help=f"Minimum feature share one affine adaptive mapping must cover (default: {DEFAULT_FPS_ANCHOR_GLOBAL_COVERAGE:.0%}).",
)
@click.option(
    "--fps-segmented-min-post-map-anchors",
    type=click.IntRange(min=2),
    default=None,
    help=f"Post-Milksync video matches required for a proven segmented map (default: {DEFAULT_FPS_SEGMENTED_MIN_POST_MAP_ANCHORS}).",
)
@click.option(
    "--fps-segmented-min-post-map-span-seconds",
    type=click.FloatRange(min=1),
    default=None,
    help=f"Minimum timeline span of segmented post-map matches (default: {DEFAULT_FPS_SEGMENTED_MIN_POST_MAP_SPAN_SECONDS:g}s).",
)
@click.option(
    "--allow-tvrip-segment-sync/--no-allow-tvrip-segment-sync",
    default=None,
    help="Explicitly permit experimental segmented TVRip synchronization.",
)
@click.option(
    "--tvrip-min-source-confidence",
    "tvrip_min_source_match_confidence",
    type=click.FloatRange(min=0, max=1),
    default=None,
    help="Minimum duration-weighted confidence across accepted TVRip segments.",
)
@click.option(
    "--tvrip-min-segment-confidence",
    type=click.FloatRange(min=0, max=1),
    default=None,
    help="Minimum content-anchor confidence for each TVRip segment.",
)
@click.option(
    "--tvrip-spectral-min-segment-confidence",
    type=click.FloatRange(min=0, max=1),
    default=None,
    help="Per-segment video confidence after reliable pre/post spectral proof.",
)
@click.option(
    "--tvrip-spectral-min-source-confidence",
    "tvrip_spectral_min_source_match_confidence",
    type=click.FloatRange(min=0, max=1),
    default=None,
    help="Duration-weighted video confidence after reliable pre/post spectral proof.",
)
@click.option(
    "--tvrip-max-residual-seconds",
    type=click.FloatRange(min=0),
    default=None,
    help="Maximum residual synchronization error at any segment validation point.",
)
@click.option(
    "--tvrip-min-coverage",
    type=click.FloatRange(min=0, max=1),
    default=None,
    help="Minimum validated Portuguese coverage required to retain a TVRip dub.",
)
@click.option(
    "--tvrip-max-segments",
    type=click.IntRange(min=1),
    default=None,
    help="Diagnostic TVRip segment-count warning threshold; output still continues.",
)
@click.option(
    "--tvrip-continue-on-validation-warnings/--tvrip-strict-validation",
    "tvrip_continue_on_validation_warnings",
    default=None,
    help="Continue an authorized TVRip run; local probe warnings stay diagnostic to avoid false original-audio dropouts (default: continue).",
)
@click.option(
    "--tvrip-min-segment-seconds",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Reject mapped TVRip segments shorter than this duration.",
)
@click.option(
    "--tvrip-max-segment-seconds",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Split long mappings into validation slices no longer than this.",
)
@click.option(
    "--tvrip-break-sensitivity-seconds",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Maximum offset difference for coalescing adjacent mapped buckets.",
)
@click.option(
    "--tvrip-commercial-min-seconds",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Minimum mid-program source-only interval classified as a commercial break.",
)
@click.option(
    "--tvrip-retain-alternative-sections/--no-tvrip-retain-alternative-sections",
    default=None,
    help="Retain alternate broadcast sections when the master timeline can represent them.",
)
@click.option(
    "--tvrip-fallback",
    type=click.Choice(["ask", "original", "alternate-dub", "silence", "omit"]),
    default=None,
    help="Policy for master-only content (default: ask).",
)
@click.option(
    "--tvrip-speed-correction/--no-tvrip-speed-correction",
    "tvrip_allow_speed_correction",
    default=None,
    help="Permit measured speed normalization in the TVRip beta workflow.",
)
@click.option(
    "--tvrip-max-speed-adjustment",
    type=click.FloatRange(min=0, max=1, max_open=True),
    default=None,
    help="Largest measured fractional TVRip speed correction accepted.",
)
@click.option(
    "--tvrip-require-interactive-approval/--no-tvrip-require-interactive-approval",
    default=None,
    help="Require the segment-review TUI even when unattended opt-in is set.",
)
@click.option(
    "--tvrip-allow-partial-tracks/--no-tvrip-allow-partial-tracks",
    default=None,
    help="Allow clearly labeled dubs containing validated master-only gaps.",
)
@click.option(
    "--tvrip-validation-position",
    "tvrip_validation_positions",
    type=click.FloatRange(min=0, max=1, min_open=True, max_open=True),
    multiple=True,
    help="Fractional validation point inside every TVRip segment; repeat at least three times.",
)
@click.option(
    "--tvrip-validation-window-seconds",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Video-content window decoded at every per-segment validation point.",
)
@click.option(
    "--tvrip-validation-search-seconds",
    type=click.FloatRange(min=0),
    default=None,
    help="Local search radius around each predicted TVRip content anchor.",
)
@click.option(
    "--tvrip-track-title",
    help="Format using {mode}, {coverage}, and {fallback}.",
)
@click.option("--dry-run/--no-dry-run", default=None, help="Plan without media output.")
@click.option(
    "--interactive/--no-interactive",
    default=None,
    help="Open the navigable pair-selection workflow.",
)
@click.option(
    "--temp-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Private work root; defaults below the scan path.",
)
@click.option(
    "--keep-temp/--no-keep-temp",
    default=None,
    help="Keep per-job work directories for diagnosis.",
)
@click.option(
    "--report", type=click.Path(dir_okay=False, path_type=Path), help="JSON report destination."
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["rich", "plain", "json"]),
    default=None,
    help=f"Terminal output format (default: {DEFAULT_OUTPUT_FORMAT}).",
)
@click.option("--json", "json_output", is_flag=True, help="Shortcut for --format json.")
@click.option(
    "--color",
    type=click.Choice(["auto", "always", "never"]),
    default=None,
    help=f"Color policy (default: {DEFAULT_COLOR_MODE}).",
)
@click.option("--progress/--no-progress", default=None, help="Show progress indicators.")
@click.option("--verbose/--no-verbose", default=None, help="Include diagnostic logging.")
@click.option("--quiet/--no-quiet", default=None, help="Suppress human output.")
@click.option("--check-deps", is_flag=True, help="Show dependency status and exit.")
@click.option(
    "--allowed-path",
    "allowed_paths",
    type=click.Path(file_okay=False, path_type=Path),
    multiple=True,
    help="Permit input/output below this root; repeatable.",
)
@click.option(
    "--required-path",
    "required_paths",
    type=click.Path(file_okay=False, path_type=Path),
    multiple=True,
    help="Require this path to exist at startup; repeatable.",
)
@click.option(
    "--enforce-paths/--no-enforce-paths",
    default=None,
    help="Reject paths outside --allowed-path roots.",
)
@click.option("--required-group", help="Require membership in this operating-system group.")
@click.option(
    "--output-group",
    help="Set the group owner of each published output file (requires group membership).",
)
@click.option("--output-dir-name", help="Default batch output directory name.")
@click.option("--work-dir-name", help="Default private work directory name.")
@click.option(
    "--ignore-dir",
    "ignored_dir_names",
    multiple=True,
    help="Directory name excluded during recursive scans; repeatable.",
)
@click.option("--ffmpeg", "ffmpeg_binary", type=click.Path(dir_okay=False))
@click.option("--ffprobe", "ffprobe_binary", type=click.Path(dir_okay=False))
@click.option("--mediainfo", "mediainfo_binary", type=click.Path(dir_okay=False))
@click.option("--mkvmerge", "mkvmerge_binary", type=click.Path(dir_okay=False))
@click.option("--mkvextract", "mkvextract_binary", type=click.Path(dir_okay=False))
@click.option("--mkvpropedit", "mkvpropedit_binary", type=click.Path(dir_okay=False))
@click.option(
    "--minimum-mkvmerge-version",
    type=click.IntRange(min=1),
    default=None,
    help="Minimum accepted MKVToolNix major version.",
)
@click.option(
    "--align-framerate",
    is_flag=True,
    default=None,
    help=(
        "LEGACY ALIAS: permit beta FPS analysis; speed changes still require measured drift."
    ),
)
@click.option(
    "--align-frames-too",
    is_flag=True,
    default=None,
    help="ADVANCED: refine shifts using video frames.",
)
@click.option("--only-delta", is_flag=True, default=None, help="ADVANCED: apply delta shifts only.")
@click.option(
    "--adjust-delay",
    type=float,
    help="ADVANCED: add seconds on top of automatic container-timestamp correction.",
)
@click.option(
    "--preserve-silence",
    is_flag=True,
    default=None,
    help="ADVANCED: retain trailing silence during analysis.",
)
@click.version_option(version=__version__)
def main(
    path: Path | None,
    config_file: Path | None,
    init_config: bool,
    refresh_config: bool,
    show_config: bool,
    recursive: bool | None,
    output_dir: Path | None,
    output: Path | None,
    tag: str | None,
    conflict: str | None,
    dual_file: Path | None,
    tvrip_file: Path | None,
    normal_file: Path | None,
    dub_language: str | None,
    original_language: str | None,
    dual_audio_ids: tuple[int, ...],
    normal_audio_ids: tuple[int, ...],
    dub_track_selectors: tuple[str, ...],
    original_track_selector: str | None,
    preferred_original_source: str | None,
    preferred_dub_source: str | None,
    audio_codec_preference: tuple[str, ...],
    audio_selection_margin: float | None,
    dub_gap_fallback: str | None,
    dub_gap_min_seconds: float | None,
    dub_gap_min_coverage: float | None,
    dub_gap_track_title: str | None,
    subtitle_policy: str | None,
    sidecar_language_overrides: tuple[str, ...],
    sidecar_dual_language: str | None,
    trim_recap: bool | None,
    recap_window: float | None,
    end_trim: bool | None,
    end_tolerance_ms: int | None,
    reconcile_av: bool | None,
    av_tolerance_ms: int | None,
    allow_experimental_fps_sync: bool | None,
    compatible_fps_pairs: tuple[str, ...],
    fps_max_drift_seconds: float | None,
    fps_min_match_confidence: float | None,
    fps_validation_positions: tuple[float, ...],
    fps_search_radius_seconds: float | None,
    fps_speed_ratio_tolerance: float | None,
    fps_content_speed_factors: tuple[str, ...],
    fps_audio_duration_ratio_tolerance: float | None,
    fps_spectral_tempo_probe: bool | None,
    fps_spectral_min_pairs: int | None,
    fps_spectral_pair_min_seconds: float | None,
    fps_spectral_pair_max_seconds: float | None,
    fps_spectral_max_dispersion: float | None,
    fps_spectral_slope_cluster_radius: float | None,
    fps_spectral_max_speed_adjustment: float | None,
    fps_spectral_max_projected_drift_seconds: float | None,
    fps_spectral_max_refinement_passes: int | None,
    fps_spectral_refinement_damping: float | None,
    fps_spectral_iterative_refinement: bool | None,
    fps_spectral_min_post_map_anchors: int | None,
    fps_adaptive_anchors: bool | None,
    fps_anchor_sample_count: int | None,
    fps_anchor_candidate_count: int | None,
    fps_anchor_window_seconds: float | None,
    fps_anchor_min_separation_seconds: float | None,
    fps_anchor_global_coverage: float | None,
    fps_segmented_min_post_map_anchors: int | None,
    fps_segmented_min_post_map_span_seconds: float | None,
    allow_tvrip_segment_sync: bool | None,
    tvrip_min_source_match_confidence: float | None,
    tvrip_min_segment_confidence: float | None,
    tvrip_spectral_min_segment_confidence: float | None,
    tvrip_spectral_min_source_match_confidence: float | None,
    tvrip_max_residual_seconds: float | None,
    tvrip_min_coverage: float | None,
    tvrip_max_segments: int | None,
    tvrip_continue_on_validation_warnings: bool | None,
    tvrip_min_segment_seconds: float | None,
    tvrip_max_segment_seconds: float | None,
    tvrip_break_sensitivity_seconds: float | None,
    tvrip_commercial_min_seconds: float | None,
    tvrip_retain_alternative_sections: bool | None,
    tvrip_fallback: str | None,
    tvrip_allow_speed_correction: bool | None,
    tvrip_max_speed_adjustment: float | None,
    tvrip_require_interactive_approval: bool | None,
    tvrip_allow_partial_tracks: bool | None,
    tvrip_validation_positions: tuple[float, ...],
    tvrip_validation_window_seconds: float | None,
    tvrip_validation_search_seconds: float | None,
    tvrip_track_title: str | None,
    dry_run: bool | None,
    interactive: bool | None,
    temp_dir: Path | None,
    keep_temp: bool | None,
    report: Path | None,
    output_format: str | None,
    json_output: bool,
    color: str | None,
    progress: bool | None,
    verbose: bool | None,
    quiet: bool | None,
    check_deps: bool,
    allowed_paths: tuple[Path, ...],
    required_paths: tuple[Path, ...],
    enforce_paths: bool | None,
    required_group: str | None,
    output_group: str | None,
    output_dir_name: str | None,
    work_dir_name: str | None,
    ignored_dir_names: tuple[str, ...],
    ffmpeg_binary: str | None,
    ffprobe_binary: str | None,
    mediainfo_binary: str | None,
    mkvmerge_binary: str | None,
    mkvextract_binary: str | None,
    mkvpropedit_binary: str | None,
    minimum_mkvmerge_version: int | None,
    align_framerate: bool | None,
    align_frames_too: bool | None,
    only_delta: bool | None,
    adjust_delay: float | None,
    preserve_silence: bool | None,
) -> None:
    """Create synchronized Portuguese dual-audio MKVs from matching releases."""

    if init_config and refresh_config:
        fallback = DualMakerConfig(
            output_format="json" if json_output or output_format == "json" else "rich"
        )
        _exit_error(
            fallback,
            "--init-config and --refresh-config cannot be used together",
            code=2,
        )

    if init_config:
        environment_path = os.environ.get(f"{ENV_PREFIX}CONFIG")
        target = config_file or (Path(environment_path) if environment_path else None)
        try:
            initialized_path, created = initialize_config_file(
                target or default_user_config_path(os.environ)
            )
        except ConfigurationError as exc:
            fallback = DualMakerConfig(
                output_format="json" if json_output or output_format == "json" else "rich"
            )
            _exit_error(
                fallback,
                str(exc),
                code=2,
                hint="Choose a writable .yml or .yaml path with --config.",
            )
        payload = {
            "status": "created" if created else "exists",
            "config": str(initialized_path),
        }
        if json_output or output_format == "json":
            click.echo(json.dumps(payload, ensure_ascii=False))
        elif created:
            click.secho(f"✓ Created configuration: {initialized_path}", fg="green")
        else:
            click.secho(f"• Configuration already exists: {initialized_path}", fg="yellow")
            click.echo("  It was left unchanged.")
        raise click.exceptions.Exit(0)

    if refresh_config:
        environment_path = os.environ.get(f"{ENV_PREFIX}CONFIG")
        target = config_file or (Path(environment_path) if environment_path else None)
        try:
            refreshed_path, backup = refresh_config_file(
                target or default_user_config_path(os.environ)
            )
        except ConfigurationError as exc:
            fallback = DualMakerConfig(
                output_format="json" if json_output or output_format == "json" else "rich"
            )
            _exit_error(
                fallback,
                str(exc),
                code=2,
                hint="Use --config with a writable .yml or .yaml file.",
            )
        payload = {
            "status": "refreshed",
            "config": str(refreshed_path),
            "backup": str(backup) if backup else None,
        }
        if json_output or output_format == "json":
            click.echo(json.dumps(payload, ensure_ascii=False))
        else:
            click.secho(f"✓ Refreshed configuration: {refreshed_path}", fg="green")
            if backup:
                click.echo(f"  Backup: {backup}")
        raise click.exceptions.Exit(0)

    cli_values: dict[str, Any] = {
        "path": path or (normal_file.parent if normal_file else None),
        "recursive": recursive,
        "output_dir": output_dir,
        "output": output,
        "tag": tag,
        "conflict": conflict,
        "dub_language": dub_language,
        "original_language": original_language,
        "dual_audio_ids": dual_audio_ids or None,
        "normal_audio_ids": normal_audio_ids or None,
        "dub_track_selectors": dub_track_selectors or None,
        "original_track_selector": original_track_selector,
        "preferred_original_source": preferred_original_source,
        "preferred_dub_source": preferred_dub_source,
        "audio_codec_preference": audio_codec_preference or None,
        "audio_selection_margin": audio_selection_margin,
        "dub_gap_fallback": dub_gap_fallback,
        "dub_gap_min_seconds": dub_gap_min_seconds,
        "dub_gap_min_coverage": dub_gap_min_coverage,
        "dub_gap_track_title": dub_gap_track_title,
        "subtitle_policy": subtitle_policy,
        "sidecar_language_overrides": sidecar_language_overrides or None,
        "sidecar_dual_language": sidecar_dual_language,
        "trim_recap": trim_recap,
        "recap_window": recap_window,
        "end_trim": end_trim,
        "end_tolerance_ms": end_tolerance_ms,
        "reconcile_av": reconcile_av,
        "av_tolerance_ms": av_tolerance_ms,
        "allow_experimental_fps_sync": allow_experimental_fps_sync,
        "compatible_fps_pairs": compatible_fps_pairs or None,
        "fps_max_drift_seconds": fps_max_drift_seconds,
        "fps_min_match_confidence": fps_min_match_confidence,
        "fps_validation_positions": fps_validation_positions or None,
        "fps_search_radius_seconds": fps_search_radius_seconds,
        "fps_speed_ratio_tolerance": fps_speed_ratio_tolerance,
        "fps_content_speed_factors": fps_content_speed_factors or None,
        "fps_audio_duration_ratio_tolerance": fps_audio_duration_ratio_tolerance,
        "fps_spectral_tempo_probe": fps_spectral_tempo_probe,
        "fps_spectral_min_pairs": fps_spectral_min_pairs,
        "fps_spectral_pair_min_seconds": fps_spectral_pair_min_seconds,
        "fps_spectral_pair_max_seconds": fps_spectral_pair_max_seconds,
        "fps_spectral_max_dispersion": fps_spectral_max_dispersion,
        "fps_spectral_slope_cluster_radius": fps_spectral_slope_cluster_radius,
        "fps_spectral_max_speed_adjustment": fps_spectral_max_speed_adjustment,
        "fps_spectral_max_projected_drift_seconds": fps_spectral_max_projected_drift_seconds,
        "fps_spectral_max_refinement_passes": fps_spectral_max_refinement_passes,
        "fps_spectral_refinement_damping": fps_spectral_refinement_damping,
        "fps_spectral_iterative_refinement": fps_spectral_iterative_refinement,
        "fps_spectral_min_post_map_anchors": fps_spectral_min_post_map_anchors,
        "fps_adaptive_anchors": fps_adaptive_anchors,
        "fps_anchor_sample_count": fps_anchor_sample_count,
        "fps_anchor_candidate_count": fps_anchor_candidate_count,
        "fps_anchor_window_seconds": fps_anchor_window_seconds,
        "fps_anchor_min_separation_seconds": fps_anchor_min_separation_seconds,
        "fps_anchor_global_coverage": fps_anchor_global_coverage,
        "fps_segmented_min_post_map_anchors": fps_segmented_min_post_map_anchors,
        "fps_segmented_min_post_map_span_seconds": fps_segmented_min_post_map_span_seconds,
        "allow_tvrip_segment_sync": allow_tvrip_segment_sync,
        "tvrip_min_source_match_confidence": tvrip_min_source_match_confidence,
        "tvrip_min_segment_confidence": tvrip_min_segment_confidence,
        "tvrip_spectral_min_segment_confidence": tvrip_spectral_min_segment_confidence,
        "tvrip_spectral_min_source_match_confidence": tvrip_spectral_min_source_match_confidence,
        "tvrip_max_residual_seconds": tvrip_max_residual_seconds,
        "tvrip_min_coverage": tvrip_min_coverage,
        "tvrip_max_segments": tvrip_max_segments,
        "tvrip_continue_on_validation_warnings": tvrip_continue_on_validation_warnings,
        "tvrip_min_segment_seconds": tvrip_min_segment_seconds,
        "tvrip_max_segment_seconds": tvrip_max_segment_seconds,
        "tvrip_break_sensitivity_seconds": tvrip_break_sensitivity_seconds,
        "tvrip_commercial_min_seconds": tvrip_commercial_min_seconds,
        "tvrip_retain_alternative_sections": tvrip_retain_alternative_sections,
        "tvrip_fallback": tvrip_fallback,
        "tvrip_allow_speed_correction": tvrip_allow_speed_correction,
        "tvrip_max_speed_adjustment": tvrip_max_speed_adjustment,
        "tvrip_require_interactive_approval": tvrip_require_interactive_approval,
        "tvrip_allow_partial_tracks": tvrip_allow_partial_tracks,
        "tvrip_validation_positions": tvrip_validation_positions or None,
        "tvrip_validation_window_seconds": tvrip_validation_window_seconds,
        "tvrip_validation_search_seconds": tvrip_validation_search_seconds,
        "tvrip_track_title": tvrip_track_title,
        "dry_run": dry_run,
        "interactive": interactive,
        "temp_dir": temp_dir,
        "keep_temp": keep_temp,
        "report": report,
        "output_format": "json" if json_output else output_format,
        "color": color,
        "progress": progress,
        "verbose": verbose,
        "quiet": quiet,
        "allowed_paths": allowed_paths or None,
        "required_paths": required_paths or None,
        "enforce_paths": enforce_paths,
        "required_group": required_group,
        "output_group": output_group,
        "output_dir_name": output_dir_name,
        "work_dir_name": work_dir_name,
        "ignored_dir_names": ignored_dir_names or None,
        "minimum_mkvmerge_version": minimum_mkvmerge_version,
        "align_framerate": align_framerate,
        "align_frames_too": align_frames_too,
        "only_delta": only_delta,
        "adjust_delay": adjust_delay,
        "preserve_silence": preserve_silence,
        "binaries": {
            "ffmpeg": ffmpeg_binary,
            "ffprobe": ffprobe_binary,
            "mediainfo": mediainfo_binary,
            "mkvmerge": mkvmerge_binary,
            "mkvextract": mkvextract_binary,
            "mkvpropedit": mkvpropedit_binary,
        },
    }
    try:
        config = load_configuration(
            cli_values,
            config_path=config_file,
            bootstrap_user_config=True,
        )
    except ConfigurationError as exc:
        fallback = DualMakerConfig(
            output_format="json" if json_output or output_format == "json" else "rich"
        )
        _exit_error(
            fallback,
            str(exc),
            code=2,
            hint="Run dualmaker --help for configuration options.",
        )

    _configure_logging(config)
    ui = TerminalUI(config)
    invocation_problems = []
    if dual_file and tvrip_file:
        invocation_problems.append("--dual and --tvrip are mutually exclusive")
    source_file = tvrip_file or dual_file
    if bool(source_file) != bool(normal_file):
        invocation_problems.append(
            "--tvrip and --normal must be supplied together"
            if tvrip_file
            else "--dual and --normal must be supplied together"
        )
    if config.output and not source_file:
        invocation_problems.append("--output is only valid with an explicit source and --normal")
    if invocation_problems:
        _exit_error(config, "\n".join(invocation_problems), code=2)

    explicit_inputs = tuple(
        item.expanduser().resolve() for item in (source_file, normal_file) if item is not None
    )
    try:
        validate_configuration(
            config,
            input_paths=explicit_inputs,
            require_scan_path=not bool(explicit_inputs) and not check_deps,
            validate_binaries=not check_deps,
        )
    except ConfigurationError as exc:
        _exit_error(
            config,
            str(exc),
            code=2,
            hint="Use --show-config to inspect values and their source.",
        )

    runner = ToolRunner(
        quiet=config.quiet or config.output_format == "json",
        binaries=config.binaries,
    )
    dependency_status = check_dependencies(
        runner, minimum_mkvmerge_version=config.minimum_mkvmerge_version
    )
    ui.heading(config.path, interactive=config.interactive)
    if check_deps or config.verbose:
        ui.dependency_table(dependency_status)
    missing = [tool for tool, status in dependency_status.items() if not status["ok"]]
    if check_deps:
        if config.output_format == "json":
            TerminalUI.json_result(
                {
                    "status": "ok" if not missing else "error",
                    "dependencies": dependency_status,
                }
            )
        raise click.exceptions.Exit(0 if not missing else 1)
    if missing:
        ui.dependency_table(dependency_status)
        _exit_error(
            config,
            "Required external tools are missing or unsupported: " + ", ".join(missing),
            code=1,
            hint="Install the tools or configure their exact paths with the binary options.",
        )

    if config.interactive and not (sys.stdin.isatty() and sys.stdout.isatty()):
        _exit_error(
            config,
            "Interactive mode requires an attached terminal.",
            code=2,
            hint="Run without --interactive for automation, or use --json for structured output.",
        )

    if show_config:
        resolved = configuration_as_dict(config)
        if config.output_format == "json":
            TerminalUI.json_result({"status": "ok", "configuration": resolved})
        else:
            ui.resolved_configuration(resolved)
        raise click.exceptions.Exit(0)

    results: list[JobResult] = []
    skipped: list[str] = []
    cancelled_message: str | None = None
    report_assets = []
    try:
        if source_file and normal_file:
            report_root = (
                config.output.resolve().parent
                if config.output
                else config.output_dir or normal_file.resolve().parent / config.output_dir_name
            )
            try:
                plans = [
                    plan_explicit(
                        source_file,
                        normal_file,
                        config,
                        tvrip=tvrip_file is not None,
                    )
                ]
                report_assets = [plans[0].normal, plans[0].dual]
            except OutputConflictError as exc:
                if config.conflict != "skip":
                    raise
                plans = []
                skipped.append(str(exc))
            except (ExperimentalFPSRequiredError, ExperimentalTVRipRequiredError) as exc:
                plans = []
                skipped.append(str(exc))
                inspector = MediaInspector(runner)
                report_assets = [inspector.inspect(normal_file), inspector.inspect(source_file)]
        else:
            plans, skipped, report_assets = plan_batch(config)
            report_root = config.output_dir or config.path / config.output_dir_name
        if config.output and len(plans) != 1 and not skipped:
            raise ConfigurationError("--output requires exactly one explicit pair")

        ui.scan_summary(len(report_assets), len(plans), len(skipped))
        ui.plan_table(plans, dry_run=config.dry_run)
        if config.dry_run:
            for plan in plans:
                result = JobResult(
                    status="planned", output=plan.output, message="Dry-run plan", plan=plan
                )
                results.append(result)
                ui.result(result)
        else:
            with ui.progress(len(plans)) as progress_display:
                task = progress_display.add_task("Waiting", total=len(plans))
                for plan in plans:
                    job_title = plan.identity.title
                    progress_display.update(task, description=f"Synchronizing {job_title}")
                    try:
                        result = process_job(
                            plan,
                            config,
                            runner=runner,
                            on_phase=lambda phase, title=job_title: progress_display.update(
                                task,
                                description=f"{phase}: {title}",
                            ),
                        )
                    except UserCancelledError as exc:
                        cancelled_message = str(exc)
                        progress_display.update(task, description="Cancelled safely")
                        break
                    results.append(result)
                    progress_display.advance(task)
                    ui.result(result)
    except UserCancelledError as exc:
        _exit_error(config, str(exc), code=130)
    except DualMakerError as exc:
        _exit_error(config, str(exc), code=1)

    report_path = config.report or default_report_path(Path(report_root).expanduser().resolve())
    report_payload = {
        "version": __version__,
        "config": jsonable(config),
        "dependencies": dependency_status,
        "assets": [jsonable(asset) for asset in report_assets],
        "results": [result.to_dict() for result in results],
        "skipped": skipped,
        "cancelled": cancelled_message,
    }
    write_report(report_path, report_payload)
    ui.report(report_path)
    ui.notices(skipped)
    if cancelled_message:
        ui.error(
            cancelled_message, hint="Completed outputs were retained and recorded in the report."
        )

    exit_code = (
        130
        if cancelled_message
        else 1
        if any(result.status == "failed" for result in results)
        else 2
        if skipped
        else 0
    )
    if config.output_format == "json":
        TerminalUI.json_result(
            {
                "status": "failed" if exit_code == 1 else "partial" if exit_code == 2 else "ok",
                "exit_code": exit_code,
                "report": str(report_path),
                "results": [_result_summary(result) for result in results],
                "skipped": skipped,
            }
        )
    raise click.exceptions.Exit(exit_code)
