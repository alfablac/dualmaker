"""Single source of truth for dualmaker's configurable defaults."""

from __future__ import annotations

from pathlib import Path

ENV_PREFIX = "DUALMAKER_"
CONFIG_HOME_ENV = f"{ENV_PREFIX}CONFIG_HOME"
LOCAL_CONFIG_NAMES = ("dualmaker.yml", "dualmaker.yaml", "dualmaker.toml")
USER_CONFIG_RELATIVE = Path(".dualmaker/config.yml")
LEGACY_USER_CONFIG_RELATIVE = Path(".config/dualmaker/config.toml")

DEFAULT_OUTPUT_DIR_NAME = "dualmaker-output"
DEFAULT_WORK_DIR_NAME = ".dualmaker-work"
DEFAULT_TAG = "alfaHD"
DEFAULT_CONFLICT_POLICY = "increment"
DEFAULT_DUB_LANGUAGE = "pt-BR"
DEFAULT_RECAP_WINDOW = 120.0
DEFAULT_END_TOLERANCE_MS = 500
DEFAULT_RECONCILE_AV = True
DEFAULT_AV_TOLERANCE_MS = 200
DEFAULT_AUDIO_SELECTION_MARGIN = 0.75
DEFAULT_SUBTITLE_POLICY = "prefer-master"
DEFAULT_EXPERIMENTAL_DUB_RESYNC = True
# A complete one-anchor constant map is sufficient to import a simple release
# offset, while several distributed anchors raise the score to 1.0. Coverage
# remains the dominant signal. A score below this threshold triggers one
# shorter-window event-anchor retry and is retained as a report warning.
DEFAULT_EXPERIMENTAL_DUB_RESYNC_MIN_CONFIDENCE = 0.80
# Milksync's native numerical dependencies otherwise each choose their own
# CPU-pool size. Bound both those pools and the dense DTW cost matrix so batch
# jobs do not reserve one worker per host core or a large virtual address map.
DEFAULT_MILKSYNC_MAX_THREADS = 2
DEFAULT_MILKSYNC_CHROMA_WORKERS = 1
DEFAULT_MILKSYNC_MAX_COST_MATRIX_CELLS = 25_000_000
SIDECAR_SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".sub")
SIDECAR_TEXT_OUTPUT_ENCODING = "utf-8-sig"
SIDECAR_LEGACY_TEXT_ENCODINGS = ("cp1252", "latin-1")
# A sidecar named after the DUAL release normally accompanies its Portuguese
# dub. It can be changed in configuration or per file with --sidecar-language.
DEFAULT_DUAL_SIDECAR_LANGUAGE = "pt-BR"
DEFAULT_PREFERRED_ORIGINAL_SOURCE = "master"
DEFAULT_PREFERRED_DUB_SOURCE = "dual"
# When Milksync finds a verified section of the master timeline that does not
# exist in the DUAL source, preserve continuous playback by using the master
# reference/original audio for that section of the Portuguese track.  This is
# deliberately independent from the TVRip policy: ordinary WEB/BluRay pairs
# can have one missing dub scene too.
DEFAULT_DUB_GAP_FALLBACK = "original"
DEFAULT_DUB_GAP_MIN_SECONDS = 1.0
DEFAULT_DUB_GAP_MIN_COVERAGE = 0.80
# Output track labels stay readable.  Coverage, segmentation, and fallback
# intervals belong in the JSON report rather than in Matroska track metadata.
DEFAULT_DUB_GAP_TRACK_TITLE = "Portuguese (Brazil)"
DEFAULT_AUDIO_CODEC_PREFERENCE = (
    "A_TRUEHD",
    "DTS-HD MASTER AUDIO",
    "DTS XLL",
    "A_FLAC",
    "A_EAC3",
    "A_DTS",
    "A_AC3",
    "A_OPUS",
    "A_AAC",
    "A_MPEG/L3",
)
DEFAULT_ALLOW_EXPERIMENTAL_FPS_SYNC = False
DEFAULT_COMPATIBLE_FPS_PAIRS = (
    "24000/1001=24/1",
    "24000/1001=25/1",
    "24/1=25/1",
    "24/1=30/1",
    "24000/1001=30000/1001",
    "25/1=30000/1001",
)
DEFAULT_FPS_MAX_DRIFT_SECONDS = 0.50
DEFAULT_FPS_MIN_MATCH_CONFIDENCE = 0.38
DEFAULT_FPS_VALIDATION_POSITIONS = (0.10, 0.50, 0.90)
DEFAULT_FPS_SEARCH_RADIUS_SECONDS = 30.0
DEFAULT_FPS_SPEED_RATIO_TOLERANCE = 0.0025
# Container frame rate and program playback speed are different physical
# quantities.  Broadcast sources are sometimes cadence-converted to 29.97 fps
# after a 24/25 audio speed conversion, so neither the container-rate ratio nor
# real time describes their content clock.  These standard source/master
# content-speed factors may be nominated by the common-original track lengths;
# content anchors must still validate a nomination before it is applied.
DEFAULT_FPS_CONTENT_SPEED_FACTORS = (
    "24/25",
    "24000/25025",
    "25/24",
    "25025/24000",
    "1000/1001",
    "1001/1000",
)
DEFAULT_FPS_AUDIO_DURATION_RATIO_TOLERANCE = 0.01
DEFAULT_FPS_SPECTRAL_TEMPO_PROBE = True
DEFAULT_FPS_SPECTRAL_MIN_PAIRS = 12
DEFAULT_FPS_SPECTRAL_PAIR_MIN_SECONDS = 20.0
DEFAULT_FPS_SPECTRAL_PAIR_MAX_SECONDS = 180.0
DEFAULT_FPS_SPECTRAL_MAX_DISPERSION = 0.015
DEFAULT_FPS_SPECTRAL_SLOPE_CLUSTER_RADIUS = 0.003
DEFAULT_FPS_SPECTRAL_MAX_SPEED_ADJUSTMENT = 0.08
DEFAULT_FPS_SPECTRAL_MAX_PROJECTED_DRIFT_SECONDS = 0.10
DEFAULT_FPS_SPECTRAL_MAX_REFINEMENT_PASSES = 2
DEFAULT_FPS_SPECTRAL_REFINEMENT_DAMPING = 0.50
DEFAULT_FPS_SPECTRAL_ITERATIVE_REFINEMENT = False
DEFAULT_FPS_SPECTRAL_MIN_POST_MAP_ANCHORS = 1
# When fixed beginning/middle/end windows cannot survive broadcast edits, make a
# small, low-resolution content index and look for several independent scenes
# anywhere in both features.  This is experimental and remains fail-closed.
DEFAULT_FPS_ADAPTIVE_ANCHORS = True
DEFAULT_FPS_ANCHOR_SAMPLE_COUNT = 15
DEFAULT_FPS_ANCHOR_CANDIDATE_COUNT = 6
DEFAULT_FPS_ANCHOR_WINDOW_SECONDS = 8.0
DEFAULT_FPS_ANCHOR_MIN_SEPARATION_SECONDS = 45.0
DEFAULT_FPS_ANCHOR_GLOBAL_COVERAGE = 0.55
DEFAULT_FPS_SEGMENTED_MIN_POST_MAP_ANCHORS = 2
DEFAULT_FPS_SEGMENTED_MIN_POST_MAP_SPAN_SECONDS = 120.0
FPS_ANCHOR_SAMPLE_RATE = 1.0
FPS_ANCHOR_WIDTH = 32
FPS_ANCHOR_HEIGHT = 18
DEFAULT_ALLOW_TVRIP_SEGMENT_SYNC = False
DEFAULT_TVRIP_MIN_SOURCE_MATCH_CONFIDENCE = 0.55
DEFAULT_TVRIP_MIN_SEGMENT_CONFIDENCE = 0.35
DEFAULT_TVRIP_SPECTRAL_MIN_SEGMENT_CONFIDENCE = 0.25
DEFAULT_TVRIP_SPECTRAL_MIN_SOURCE_MATCH_CONFIDENCE = 0.25
DEFAULT_TVRIP_MAX_RESIDUAL_SECONDS = 0.40
DEFAULT_TVRIP_MIN_COVERAGE = 0.85
DEFAULT_TVRIP_MAX_SEGMENTS = 24
# Once the experimental TVRip workflow has been explicitly enabled, policy
# thresholds are reported for review instead of discarding a renderable file.
# Set this false to restore strict unattended rejection.
DEFAULT_TVRIP_CONTINUE_ON_VALIDATION_WARNINGS = True
DEFAULT_TVRIP_MIN_SEGMENT_SECONDS = 8.0
DEFAULT_TVRIP_MAX_SEGMENT_SECONDS = 300.0
DEFAULT_TVRIP_BREAK_SENSITIVITY_SECONDS = 0.75
DEFAULT_TVRIP_COMMERCIAL_MIN_SECONDS = 12.0
DEFAULT_TVRIP_RETAIN_ALTERNATIVE_SECTIONS = False
DEFAULT_TVRIP_FALLBACK = "ask"
DEFAULT_TVRIP_ALLOW_SPEED_CORRECTION = True
DEFAULT_TVRIP_MAX_SPEED_ADJUSTMENT = 0.05
DEFAULT_TVRIP_REQUIRE_INTERACTIVE_APPROVAL = False
DEFAULT_TVRIP_ALLOW_PARTIAL_TRACKS = True
DEFAULT_TVRIP_VALIDATION_POSITIONS = (0.08, 0.50, 0.92)
DEFAULT_TVRIP_VALIDATION_WINDOW_SECONDS = 3.0
DEFAULT_TVRIP_VALIDATION_SEARCH_SECONDS = 2.0
DEFAULT_TVRIP_TRACK_TITLE = "Portuguese (Brazil)"
# An open-ended final Milksync bucket has no following anchor to prove that a
# broadcast tail still corresponds to the master. Check its common-original
# audio separately before retaining it in an experimental TVRip output.
DEFAULT_TVRIP_TERMINAL_TAIL_VALIDATION = True
DEFAULT_TVRIP_TERMINAL_TAIL_WINDOW_SECONDS = 8.0
DEFAULT_TVRIP_TERMINAL_TAIL_MIN_SECONDS = 2.0
DEFAULT_TVRIP_TERMINAL_TAIL_MIN_SIMILARITY = 0.48
# A complete Milksync map can still bridge a short broadcast-only scene between
# two good anchors. Validate each sufficiently long mapped TVRip interval with
# the common original before copying its Portuguese dub.
DEFAULT_TVRIP_ACOUSTIC_SEGMENT_VALIDATION = True
DEFAULT_TVRIP_ACOUSTIC_SEGMENT_WINDOW_SECONDS = 5.0
DEFAULT_TVRIP_ACOUSTIC_SEGMENT_MIN_SECONDS = 2.0
# Do not leave a long unverified run inside a nominally matching map bucket.
# The guard examines a five-second reference window at least this often.
DEFAULT_TVRIP_ACOUSTIC_SEGMENT_MAX_GAP_SECONDS = 30.0
# A failed probe is expanded on either side. This deliberately favours the
# master original around an uncertain edit over leaking a dub for a scene that
# is not present in immutable master video.
DEFAULT_TVRIP_ACOUSTIC_SEGMENT_REJECTION_PADDING_SECONDS = 5.0
DEFAULT_TVRIP_ACOUSTIC_SEGMENT_MIN_SIMILARITY = 0.60
# In the telecine fallback path, an interval that cannot be locally compared
# is not evidence that the dub matches. The validator narrows that decision to
# the uncertain interval; matching silence itself remains mapped because it
# carries no audible material to replace.
DEFAULT_TVRIP_ACOUSTIC_SEGMENT_REQUIRE_PROOF = True
# Low-resolution multi-window frame matching is deliberately centralized here:
# it is accurate enough to catch constant release-timeline offsets without
# decoding either feature in full resolution.
VIDEO_MATCH_FPS = 8
VIDEO_MATCH_WIDTH = 64
VIDEO_MATCH_HEIGHT = 36
VIDEO_MATCH_REFERENCE_SECONDS = 8.0
VIDEO_MATCH_SEARCH_RADIUS_SECONDS = 4.0
VIDEO_MATCH_SAMPLE_COUNT = 5
VIDEO_MATCH_MIN_SCORE = 0.35
VIDEO_MATCH_MAX_SPREAD_SECONDS = 0.30
DEFAULT_OUTPUT_FORMAT = "rich"
DEFAULT_COLOR_MODE = "auto"
DEFAULT_MIN_MKVMERGE_VERSION = 76

DEFAULT_IGNORED_DIR_NAMES = (
    DEFAULT_OUTPUT_DIR_NAME,
    DEFAULT_WORK_DIR_NAME,
    ".git",
    ".venv",
    ".venv-ui",
    ".test-work",
    ".uv-cache",
)

BINARY_NAMES = (
    "ffmpeg",
    "ffprobe",
    "mediainfo",
    "mkvmerge",
    "mkvextract",
    "mkvpropedit",
)
DEFAULT_BINARIES = {name: name for name in BINARY_NAMES}
BINARY_VERSION_ARGS = {
    "ffmpeg": ("-version",),
    "ffprobe": ("-version",),
    "mediainfo": ("--Version",),
    "mkvmerge": ("--version",),
    "mkvextract": ("--version",),
    "mkvpropedit": ("--version",),
}

CONFIG_KEYS = {
    "path",
    "recursive",
    "output_dir",
    "output",
    "tag",
    "conflict",
    "dub_language",
    "original_language",
    "dual_audio_ids",
    "normal_audio_ids",
    "dub_track_selectors",
    "original_track_selector",
    "preferred_original_source",
    "preferred_dub_source",
    "dub_gap_fallback",
    "dub_gap_min_seconds",
    "dub_gap_min_coverage",
    "dub_gap_track_title",
    "audio_codec_preference",
    "audio_selection_margin",
    "subtitle_policy",
    "sidecar_language_overrides",
    "sidecar_dual_language",
    "trim_recap",
    "recap_window",
    "end_trim",
    "end_tolerance_ms",
    "reconcile_av",
    "av_tolerance_ms",
    "experimental_dub_resync",
    "experimental_dub_resync_min_confidence",
    "milksync_max_threads",
    "milksync_chroma_workers",
    "milksync_max_cost_matrix_cells",
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
    "tvrip_terminal_tail_validation",
    "tvrip_terminal_tail_window_seconds",
    "tvrip_terminal_tail_min_seconds",
    "tvrip_terminal_tail_min_similarity",
    "tvrip_acoustic_segment_validation",
    "tvrip_acoustic_segment_window_seconds",
    "tvrip_acoustic_segment_min_seconds",
    "tvrip_acoustic_segment_max_gap_seconds",
    "tvrip_acoustic_segment_rejection_padding_seconds",
    "tvrip_acoustic_segment_min_similarity",
    "tvrip_acoustic_segment_require_proof",
    "dry_run",
    "interactive",
    "temp_dir",
    "keep_temp",
    "report",
    "verbose",
    "quiet",
    "align_framerate",
    "align_frames_too",
    "only_delta",
    "adjust_delay",
    "preserve_silence",
    "allowed_paths",
    "required_paths",
    "enforce_paths",
    "required_group",
    "output_group",
    "output_format",
    "color",
    "progress",
    "output_dir_name",
    "work_dir_name",
    "ignored_dir_names",
    "minimum_mkvmerge_version",
    "binaries",
}

CONFIG_SECTIONS = {
    "dualmaker": CONFIG_KEYS
    - {
        "allowed_paths",
        "required_paths",
        "enforce_paths",
        "required_group",
        "output_group",
        "binaries",
        "output_format",
        "color",
        "progress",
    },
    "paths": {
        "path",
        "output_dir",
        "output",
        "temp_dir",
        "report",
        "allowed_paths",
        "required_paths",
        "enforce_paths",
        "output_dir_name",
        "work_dir_name",
        "ignored_dir_names",
    },
    "tools": set(BINARY_NAMES) | {"minimum_mkvmerge_version"},
    "security": {
        "allowed_paths",
        "required_paths",
        "enforce_paths",
        "required_group",
        "output_group",
    },
    "interface": {"output_format", "color", "progress", "quiet", "verbose"},
    "features": {
        "trim_recap",
        "end_trim",
        "reconcile_av",
        "av_tolerance_ms",
        "experimental_dub_resync",
        "experimental_dub_resync_min_confidence",
        "milksync_max_threads",
        "milksync_chroma_workers",
        "milksync_max_cost_matrix_cells",
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
        "tvrip_terminal_tail_validation",
        "tvrip_terminal_tail_window_seconds",
        "tvrip_terminal_tail_min_seconds",
        "tvrip_terminal_tail_min_similarity",
        "tvrip_acoustic_segment_validation",
        "tvrip_acoustic_segment_window_seconds",
        "tvrip_acoustic_segment_min_seconds",
        "tvrip_acoustic_segment_max_gap_seconds",
        "tvrip_acoustic_segment_rejection_padding_seconds",
        "tvrip_acoustic_segment_min_similarity",
        "tvrip_acoustic_segment_require_proof",
        "dub_gap_fallback",
        "dub_gap_min_seconds",
        "dub_gap_min_coverage",
        "dub_gap_track_title",
        "align_framerate",
        "align_frames_too",
        "only_delta",
        "adjust_delay",
        "preserve_silence",
        "subtitle_policy",
        "sidecar_language_overrides",
        "sidecar_dual_language",
    },
    "tvrip": {
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
        "tvrip_terminal_tail_validation",
        "tvrip_terminal_tail_window_seconds",
        "tvrip_terminal_tail_min_seconds",
        "tvrip_terminal_tail_min_similarity",
        "tvrip_acoustic_segment_validation",
        "tvrip_acoustic_segment_window_seconds",
        "tvrip_acoustic_segment_min_seconds",
        "tvrip_acoustic_segment_max_gap_seconds",
        "tvrip_acoustic_segment_rejection_padding_seconds",
        "tvrip_acoustic_segment_min_similarity",
        "tvrip_acoustic_segment_require_proof",
    },
}

ENV_KEYS = {
    "PATH": "path",
    "RECURSIVE": "recursive",
    "OUTPUT_DIR": "output_dir",
    "OUTPUT": "output",
    "TAG": "tag",
    "ON_CONFLICT": "conflict",
    "DUB_LANGUAGE": "dub_language",
    "ORIGINAL_LANGUAGE": "original_language",
    "TRIM_RECAP": "trim_recap",
    "RECAP_WINDOW": "recap_window",
    "END_TRIM": "end_trim",
    "END_TOLERANCE_MS": "end_tolerance_ms",
    "RECONCILE_AV": "reconcile_av",
    "AV_TOLERANCE_MS": "av_tolerance_ms",
    "EXPERIMENTAL_DUB_RESYNC": "experimental_dub_resync",
    "EXPERIMENTAL_DUB_RESYNC_MIN_CONFIDENCE": "experimental_dub_resync_min_confidence",
    "MILKSYNC_MAX_THREADS": "milksync_max_threads",
    "MILKSYNC_CHROMA_WORKERS": "milksync_chroma_workers",
    "MILKSYNC_MAX_COST_MATRIX_CELLS": "milksync_max_cost_matrix_cells",
    "ALLOW_EXPERIMENTAL_FPS_SYNC": "allow_experimental_fps_sync",
    "COMPATIBLE_FPS_PAIRS": "compatible_fps_pairs",
    "FPS_MAX_DRIFT_SECONDS": "fps_max_drift_seconds",
    "FPS_MIN_MATCH_CONFIDENCE": "fps_min_match_confidence",
    "FPS_VALIDATION_POSITIONS": "fps_validation_positions",
    "FPS_SEARCH_RADIUS_SECONDS": "fps_search_radius_seconds",
    "FPS_SPEED_RATIO_TOLERANCE": "fps_speed_ratio_tolerance",
    "FPS_CONTENT_SPEED_FACTORS": "fps_content_speed_factors",
    "FPS_AUDIO_DURATION_RATIO_TOLERANCE": "fps_audio_duration_ratio_tolerance",
    "FPS_SPECTRAL_TEMPO_PROBE": "fps_spectral_tempo_probe",
    "FPS_SPECTRAL_MIN_PAIRS": "fps_spectral_min_pairs",
    "FPS_SPECTRAL_PAIR_MIN_SECONDS": "fps_spectral_pair_min_seconds",
    "FPS_SPECTRAL_PAIR_MAX_SECONDS": "fps_spectral_pair_max_seconds",
    "FPS_SPECTRAL_MAX_DISPERSION": "fps_spectral_max_dispersion",
    "FPS_SPECTRAL_SLOPE_CLUSTER_RADIUS": "fps_spectral_slope_cluster_radius",
    "FPS_SPECTRAL_MAX_SPEED_ADJUSTMENT": "fps_spectral_max_speed_adjustment",
    "FPS_SPECTRAL_MAX_PROJECTED_DRIFT_SECONDS": "fps_spectral_max_projected_drift_seconds",
    "FPS_SPECTRAL_MAX_REFINEMENT_PASSES": "fps_spectral_max_refinement_passes",
    "FPS_SPECTRAL_REFINEMENT_DAMPING": "fps_spectral_refinement_damping",
    "FPS_SPECTRAL_ITERATIVE_REFINEMENT": "fps_spectral_iterative_refinement",
    "FPS_SPECTRAL_MIN_POST_MAP_ANCHORS": "fps_spectral_min_post_map_anchors",
    "FPS_ADAPTIVE_ANCHORS": "fps_adaptive_anchors",
    "FPS_ANCHOR_SAMPLE_COUNT": "fps_anchor_sample_count",
    "FPS_ANCHOR_CANDIDATE_COUNT": "fps_anchor_candidate_count",
    "FPS_ANCHOR_WINDOW_SECONDS": "fps_anchor_window_seconds",
    "FPS_ANCHOR_MIN_SEPARATION_SECONDS": "fps_anchor_min_separation_seconds",
    "FPS_ANCHOR_GLOBAL_COVERAGE": "fps_anchor_global_coverage",
    "FPS_SEGMENTED_MIN_POST_MAP_ANCHORS": "fps_segmented_min_post_map_anchors",
    "FPS_SEGMENTED_MIN_POST_MAP_SPAN_SECONDS": "fps_segmented_min_post_map_span_seconds",
    "ALLOW_TVRIP_SEGMENT_SYNC": "allow_tvrip_segment_sync",
    "TVRIP_MIN_SOURCE_MATCH_CONFIDENCE": "tvrip_min_source_match_confidence",
    "TVRIP_MIN_SEGMENT_CONFIDENCE": "tvrip_min_segment_confidence",
    "TVRIP_SPECTRAL_MIN_SEGMENT_CONFIDENCE": "tvrip_spectral_min_segment_confidence",
    "TVRIP_SPECTRAL_MIN_SOURCE_MATCH_CONFIDENCE": "tvrip_spectral_min_source_match_confidence",
    "TVRIP_MAX_RESIDUAL_SECONDS": "tvrip_max_residual_seconds",
    "TVRIP_MIN_COVERAGE": "tvrip_min_coverage",
    "TVRIP_MAX_SEGMENTS": "tvrip_max_segments",
    "TVRIP_CONTINUE_ON_VALIDATION_WARNINGS": "tvrip_continue_on_validation_warnings",
    "TVRIP_MIN_SEGMENT_SECONDS": "tvrip_min_segment_seconds",
    "TVRIP_MAX_SEGMENT_SECONDS": "tvrip_max_segment_seconds",
    "TVRIP_BREAK_SENSITIVITY_SECONDS": "tvrip_break_sensitivity_seconds",
    "TVRIP_COMMERCIAL_MIN_SECONDS": "tvrip_commercial_min_seconds",
    "TVRIP_RETAIN_ALTERNATIVE_SECTIONS": "tvrip_retain_alternative_sections",
    "TVRIP_FALLBACK": "tvrip_fallback",
    "TVRIP_ALLOW_SPEED_CORRECTION": "tvrip_allow_speed_correction",
    "TVRIP_MAX_SPEED_ADJUSTMENT": "tvrip_max_speed_adjustment",
    "TVRIP_REQUIRE_INTERACTIVE_APPROVAL": "tvrip_require_interactive_approval",
    "TVRIP_ALLOW_PARTIAL_TRACKS": "tvrip_allow_partial_tracks",
    "TVRIP_VALIDATION_POSITIONS": "tvrip_validation_positions",
    "TVRIP_VALIDATION_WINDOW_SECONDS": "tvrip_validation_window_seconds",
    "TVRIP_VALIDATION_SEARCH_SECONDS": "tvrip_validation_search_seconds",
    "TVRIP_TRACK_TITLE": "tvrip_track_title",
    "TVRIP_TERMINAL_TAIL_VALIDATION": "tvrip_terminal_tail_validation",
    "TVRIP_TERMINAL_TAIL_WINDOW_SECONDS": "tvrip_terminal_tail_window_seconds",
    "TVRIP_TERMINAL_TAIL_MIN_SECONDS": "tvrip_terminal_tail_min_seconds",
    "TVRIP_TERMINAL_TAIL_MIN_SIMILARITY": "tvrip_terminal_tail_min_similarity",
    "TVRIP_ACOUSTIC_SEGMENT_VALIDATION": "tvrip_acoustic_segment_validation",
    "TVRIP_ACOUSTIC_SEGMENT_WINDOW_SECONDS": "tvrip_acoustic_segment_window_seconds",
    "TVRIP_ACOUSTIC_SEGMENT_MIN_SECONDS": "tvrip_acoustic_segment_min_seconds",
    "TVRIP_ACOUSTIC_SEGMENT_MAX_GAP_SECONDS": "tvrip_acoustic_segment_max_gap_seconds",
    "TVRIP_ACOUSTIC_SEGMENT_REJECTION_PADDING_SECONDS": "tvrip_acoustic_segment_rejection_padding_seconds",
    "TVRIP_ACOUSTIC_SEGMENT_MIN_SIMILARITY": "tvrip_acoustic_segment_min_similarity",
    "TVRIP_ACOUSTIC_SEGMENT_REQUIRE_PROOF": "tvrip_acoustic_segment_require_proof",
    "DUB_GAP_FALLBACK": "dub_gap_fallback",
    "DUB_GAP_MIN_SECONDS": "dub_gap_min_seconds",
    "DUB_GAP_MIN_COVERAGE": "dub_gap_min_coverage",
    "DUB_GAP_TRACK_TITLE": "dub_gap_track_title",
    "DUB_TRACKS": "dub_track_selectors",
    "ORIGINAL_TRACK": "original_track_selector",
    "PREFERRED_ORIGINAL_SOURCE": "preferred_original_source",
    "PREFERRED_DUB_SOURCE": "preferred_dub_source",
    "AUDIO_CODEC_PREFERENCE": "audio_codec_preference",
    "AUDIO_SELECTION_MARGIN": "audio_selection_margin",
    "SUBTITLE_POLICY": "subtitle_policy",
    "SIDECAR_LANGUAGES": "sidecar_language_overrides",
    "SIDECAR_DUAL_LANGUAGE": "sidecar_dual_language",
    "TEMP_DIR": "temp_dir",
    "KEEP_TEMP": "keep_temp",
    "REPORT": "report",
    "ALLOWED_PATHS": "allowed_paths",
    "REQUIRED_PATHS": "required_paths",
    "ENFORCE_PATHS": "enforce_paths",
    "REQUIRED_GROUP": "required_group",
    "OUTPUT_GROUP": "output_group",
    "OUTPUT_FORMAT": "output_format",
    "COLOR": "color",
    "PROGRESS": "progress",
    "OUTPUT_DIR_NAME": "output_dir_name",
    "WORK_DIR_NAME": "work_dir_name",
    "IGNORED_DIR_NAMES": "ignored_dir_names",
    "MINIMUM_MKVMERGE_VERSION": "minimum_mkvmerge_version",
    "FFMPEG": "binary.ffmpeg",
    "FFPROBE": "binary.ffprobe",
    "MEDIAINFO": "binary.mediainfo",
    "MKVMERGE": "binary.mkvmerge",
    "MKVEXTRACT": "binary.mkvextract",
    "MKVPROPEDIT": "binary.mkvpropedit",
}
