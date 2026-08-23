"""Configuration loading, precedence, normalization, and startup validation."""

from __future__ import annotations

import grp
import os
import shutil
import textwrap
import tomllib
from collections.abc import Mapping
from dataclasses import fields
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import yaml

from .defaults import (
    BINARY_NAMES,
    CONFIG_HOME_ENV,
    CONFIG_KEYS,
    CONFIG_SECTIONS,
    DEFAULT_BINARIES,
    ENV_KEYS,
    ENV_PREFIX,
    LEGACY_USER_CONFIG_RELATIVE,
    LOCAL_CONFIG_NAMES,
    USER_CONFIG_RELATIVE,
)
from .errors import ConfigurationError
from .models import DualMakerConfig

BOOL_FIELDS = {
    "recursive",
    "trim_recap",
    "end_trim",
    "reconcile_av",
    "allow_experimental_fps_sync",
    "fps_adaptive_anchors",
    "fps_spectral_tempo_probe",
    "fps_spectral_iterative_refinement",
    "allow_tvrip_segment_sync",
    "tvrip_continue_on_validation_warnings",
    "tvrip_retain_alternative_sections",
    "tvrip_allow_speed_correction",
    "tvrip_require_interactive_approval",
    "tvrip_allow_partial_tracks",
    "tvrip_terminal_tail_validation",
    "tvrip_acoustic_segment_validation",
    "tvrip_acoustic_segment_require_proof",
    "dry_run",
    "interactive",
    "keep_temp",
    "verbose",
    "quiet",
    "align_framerate",
    "align_frames_too",
    "only_delta",
    "preserve_silence",
    "enforce_paths",
    "progress",
}
PATH_FIELDS = {"path", "output_dir", "output", "temp_dir", "report", "config_file"}
PATH_LIST_FIELDS = {"allowed_paths", "required_paths"}
STRING_LIST_FIELDS = {
    "ignored_dir_names",
    "dub_track_selectors",
    "audio_codec_preference",
    "sidecar_language_overrides",
    "compatible_fps_pairs",
    "fps_content_speed_factors",
}
FLOAT_LIST_FIELDS = {"fps_validation_positions", "tvrip_validation_positions"}
INT_LIST_FIELDS = {"dual_audio_ids", "normal_audio_ids"}
INT_FIELDS = {
    "end_tolerance_ms",
    "av_tolerance_ms",
    "minimum_mkvmerge_version",
    "tvrip_max_segments",
    "fps_anchor_sample_count",
    "fps_anchor_candidate_count",
    "fps_segmented_min_post_map_anchors",
    "fps_spectral_min_pairs",
    "fps_spectral_max_refinement_passes",
    "fps_spectral_min_post_map_anchors",
}
FLOAT_FIELDS = {
    "recap_window",
    "adjust_delay",
    "audio_selection_margin",
    "dub_gap_min_seconds",
    "dub_gap_min_coverage",
    "fps_max_drift_seconds",
    "fps_min_match_confidence",
    "fps_search_radius_seconds",
    "fps_speed_ratio_tolerance",
    "fps_audio_duration_ratio_tolerance",
    "fps_anchor_window_seconds",
    "fps_anchor_min_separation_seconds",
    "fps_anchor_global_coverage",
    "fps_segmented_min_post_map_span_seconds",
    "fps_spectral_pair_min_seconds",
    "fps_spectral_pair_max_seconds",
    "fps_spectral_max_dispersion",
    "fps_spectral_slope_cluster_radius",
    "fps_spectral_max_speed_adjustment",
    "fps_spectral_max_projected_drift_seconds",
    "fps_spectral_refinement_damping",
    "tvrip_min_source_match_confidence",
    "tvrip_min_segment_confidence",
    "tvrip_spectral_min_segment_confidence",
    "tvrip_spectral_min_source_match_confidence",
    "tvrip_max_residual_seconds",
    "tvrip_min_coverage",
    "tvrip_min_segment_seconds",
    "tvrip_max_segment_seconds",
    "tvrip_break_sensitivity_seconds",
    "tvrip_commercial_min_seconds",
    "tvrip_max_speed_adjustment",
    "tvrip_validation_window_seconds",
    "tvrip_validation_search_seconds",
    "tvrip_terminal_tail_window_seconds",
    "tvrip_terminal_tail_min_seconds",
    "tvrip_terminal_tail_min_similarity",
    "tvrip_acoustic_segment_window_seconds",
    "tvrip_acoustic_segment_min_seconds",
    "tvrip_acoustic_segment_max_gap_seconds",
    "tvrip_acoustic_segment_rejection_padding_seconds",
    "tvrip_acoustic_segment_min_similarity",
}
CHOICES = {
    "conflict": {"increment", "skip", "error"},
    "output_format": {"rich", "plain", "json"},
    "color": {"auto", "always", "never"},
    "preferred_original_source": {"master", "dual", "quality"},
    "preferred_dub_source": {"master", "dual", "quality"},
    "dub_gap_fallback": {"original", "silence", "off"},
    "tvrip_fallback": {"ask", "original", "alternate-dub", "silence", "omit"},
    "subtitle_policy": {"prefer-master", "exact-union"},
}

# The generated per-user config is intended to be edited directly. Keep its
# descriptions next to the configuration loader instead of duplicating runtime
# defaults in a static YAML template. `_default_config_document()` supplies the
# values and this map supplies the documentation for every persisted setting.
CONFIG_SETTING_COMMENTS = {
    "dualmaker.recursive": "Scan child directories and preserve their relative layout in the output directory.",
    "dualmaker.tag": "Release-group tag appended after .DUAL in every generated filename.",
    "dualmaker.conflict": "Existing-output policy: increment, skip, or error; source files are never overwritten.",
    "dualmaker.dub_language": "Portuguese language tag used to identify program dubs (pt-BR, pt, por, or pob).",
    "dualmaker.original_language": "Optional common original-language override; leave null to detect it from both inputs.",
    "dualmaker.dual_audio_ids": "Explicit Matroska audio track IDs allowed from the DUAL source, in preferred order.",
    "dualmaker.normal_audio_ids": "Explicit Matroska audio track IDs allowed from the master source, in preferred order.",
    "dualmaker.dub_track_selectors": "Explicit retained dubs as master:TRACK_ID or dual:TRACK_ID, in final order.",
    "dualmaker.original_track_selector": "Explicit final original track as master:TRACK_ID or dual:TRACK_ID.",
    "dualmaker.preferred_original_source": "Tie-break preference when two original tracks have equivalent quality.",
    "dualmaker.preferred_dub_source": "Tie-break preference when two Portuguese dubs have equivalent quality.",
    "dualmaker.audio_codec_preference": "Audio codec ranking from best to worst for automatic track selection.",
    "dualmaker.audio_selection_margin": "Score difference considered ambiguous rather than silently selecting a track.",
    "dualmaker.subtitle_policy": "prefer-master removes alternate DUAL subtitle slots; exact-union keeps non-identical tracks.",
    "dualmaker.sidecar_dual_language": "Default language for text sidecars named after the DUAL release.",
    "dualmaker.sidecar_language_overrides": "Per-sidecar PATH=LANGUAGE overrides; master sidecars normally require one.",
    "dualmaker.recap_window": "Opening seconds inspected for a one-sided recap before synchronization.",
    "dualmaker.end_tolerance_ms": "Allowed selected-audio/video end difference before a conservative stream-copy end trim.",
    "dualmaker.dry_run": "Plan and report matches without creating or modifying media outputs.",
    "dualmaker.interactive": "Use the navigable terminal review interface for ambiguous choices and pair selection.",
    "dualmaker.keep_temp": "Keep private per-job work directories below work_dir_name for diagnosis.",
    "paths.output_dir_name": "Default directory created below each scan root for completed MKVs and reports.",
    "paths.work_dir_name": "Private work-directory name created below the scan root; never use a shared /tmp directory.",
    "paths.path": "Optional default scan root; a positional CLI path overrides it for one run.",
    "paths.output_dir": "Optional default output root; otherwise output_dir_name is created below each scan root.",
    "paths.output": "Optional exact output MKV path for one explicit pair.",
    "paths.temp_dir": "Optional private work root; do not use a shared /tmp directory.",
    "paths.report": "Optional exact JSON report destination.",
    "paths.ignored_dir_names": "Directory names excluded from recursive discovery.",
    "paths.allowed_paths": "Allowed root directories when enforce_paths is enabled.",
    "paths.required_paths": "Directories that must exist before a run can start.",
    "paths.enforce_paths": "Reject input, output, report, and work paths outside allowed_paths.",
    "tools.ffmpeg": "FFmpeg executable name or an absolute path.",
    "tools.ffprobe": "ffprobe executable name or an absolute path.",
    "tools.mediainfo": "MediaInfo CLI executable name or an absolute path.",
    "tools.mkvmerge": "mkvmerge executable name or an absolute path.",
    "tools.mkvextract": "mkvextract executable name or an absolute path.",
    "tools.mkvpropedit": "mkvpropedit executable name or an absolute path.",
    "tools.minimum_mkvmerge_version": "Minimum accepted MKVToolNix major version checked at startup.",
    "security.required_group": "Unix group the running user must belong to, or null to disable the gate.",
    "security.output_group": "Unix group assigned to newly published output files, or null to keep the current group.",
    "features.trim_recap": "Detect and remove one clearly validated, one-sided opening recap before synchronization.",
    "features.end_trim": "Trim final video only when MediaInfo and ffprobe agree it outlasts selected audio.",
    "features.reconcile_av": "Compare video timelines and correct one stable shared audio/video residual.",
    "features.av_tolerance_ms": "Ignore measured shared A/V residuals no larger than this many milliseconds.",
    "features.allow_experimental_fps_sync": "Require explicit consent before attempting a compatible different-FPS synchronization.",
    "features.compatible_fps_pairs": "Rational frame-rate pairs allowed in experimental mode, written as SOURCE=MASTER.",
    "features.fps_max_drift_seconds": "Maximum beginning/middle/end experimental FPS validation error.",
    "features.fps_min_match_confidence": "Minimum content-anchor confidence for experimental FPS analysis.",
    "features.fps_validation_positions": "Fractional beginning/middle/end positions used for FPS validation.",
    "features.fps_search_radius_seconds": "Maximum local content-anchor search radius during experimental FPS analysis.",
    "features.fps_speed_ratio_tolerance": "Allowed difference between measured timing speed and the rational FPS hypothesis.",
    "features.fps_content_speed_factors": "Standard source/master program-speed ratios that common-original audio durations may nominate; each candidate still requires content-anchor validation.",
    "features.fps_audio_duration_ratio_tolerance": "Maximum difference between the common-original duration ratio and a configured content-speed factor before that factor is tested.",
    "features.fps_spectral_tempo_probe": "Use Milksync chroma/spectrogram matches to measure the program clock before experimental rendering.",
    "features.fps_spectral_min_pairs": "Minimum robust within-section acoustic point pairs required to accept a measured tempo.",
    "features.fps_spectral_pair_min_seconds": "Minimum spacing between acoustic points used to measure local tempo; shorter pairs are noisier.",
    "features.fps_spectral_pair_max_seconds": "Maximum spacing between acoustic points; limiting it prevents edit steps from masquerading as linear drift.",
    "features.fps_spectral_max_dispersion": "Maximum median absolute dispersion of acoustic local slopes accepted as a stable tempo.",
    "features.fps_spectral_slope_cluster_radius": "Half-width used to find the densest acoustic local-slope cluster; smaller values distinguish edit steps from drift more strictly.",
    "features.fps_spectral_max_speed_adjustment": "Largest fractional program-speed correction an acoustic probe may authorize.",
    "features.fps_spectral_max_projected_drift_seconds": "Largest end-of-program drift implied by the post-sync acoustic slope before another refinement pass is required.",
    "features.fps_spectral_max_refinement_passes": "Maximum additional Milksync render passes used to remove a small but cumulatively audible residual clock error.",
    "features.fps_spectral_refinement_damping": "Fraction of an unbracketed residual clock correction applied per pass; a sign-changing pair switches to a bracketed secant estimate.",
    "features.fps_spectral_iterative_refinement": "Experimental opt-in: rerun Milksync to reduce projected residual clock drift; disabled because anchor selection can change between passes.",
    "features.fps_spectral_min_post_map_anchors": "Minimum independent video confirmations after a segmented map when both pre/post acoustic tempo checks pass; per-segment TVRip validation still follows.",
    "features.fps_adaptive_anchors": "After fixed FPS anchors fail, search the full timelines for several informative matching scenes; only validated TVRip workflows may use a segmented result.",
    "features.fps_anchor_sample_count": "Number of evenly distributed master scenes inspected by adaptive FPS anchor discovery.",
    "features.fps_anchor_candidate_count": "Maximum distinct source-scene candidates retained for each adaptive FPS anchor.",
    "features.fps_anchor_window_seconds": "Seconds of visual context required to verify every adaptive FPS anchor candidate.",
    "features.fps_anchor_min_separation_seconds": "Minimum master-timeline spacing between accepted adaptive FPS anchors.",
    "features.fps_anchor_global_coverage": "Minimum master-timeline share a single adaptive affine mapping must span; shorter mappings are treated as edited segments.",
    "features.fps_segmented_min_post_map_anchors": "Minimum independently matched video points accepted after Milksync for an already-proven segmented TVRip map; normal FPS conversions still require three.",
    "features.fps_segmented_min_post_map_span_seconds": "Minimum master-timeline distance spanned by the relaxed segmented post-map video checks.",
    "features.dub_gap_fallback": "For verified master-only dub gaps: original inserts master reference audio, silence retains silence, off disables repair.",
    "features.dub_gap_min_seconds": "Ignore shorter mapped master-only gaps because they are too small to classify safely.",
    "features.dub_gap_min_coverage": "Minimum independently validated Portuguese coverage required before original-audio fallback is allowed.",
    "features.dub_gap_track_title": "Display name for repaired Portuguese dubs; synchronization diagnostics remain in the JSON report.",
    "features.align_framerate": "Advanced Milksync control for frame-rate alignment; normally use the safer FPS beta workflow instead.",
    "features.align_frames_too": "Advanced Milksync control to refine sync buckets with video frames.",
    "features.only_delta": "Advanced Milksync control to apply only delta shifts.",
    "features.adjust_delay": "Advanced manual seconds added to automatic container packet-timestamp correction; null disables it.",
    "features.preserve_silence": "Advanced control to retain trailing source silence during Milksync analysis.",
    "tvrip.allow_tvrip_segment_sync": "Permit unattended experimental segmented synchronization for HDTV/TVRip-labelled sources.",
    "tvrip.tvrip_min_source_match_confidence": "Minimum duration-weighted confidence across validated TVRip reference segments.",
    "tvrip.tvrip_min_segment_confidence": "Minimum independent video-content confidence required for every TVRip segment.",
    "tvrip.tvrip_spectral_min_segment_confidence": "Lower per-segment video threshold allowed only after reliable pre/post common-original spectral tempo proof.",
    "tvrip.tvrip_spectral_min_source_match_confidence": "Lower duration-weighted video threshold allowed only after reliable pre/post common-original spectral tempo proof.",
    "tvrip.tvrip_max_residual_seconds": "Largest allowed audio/video residual at a TVRip segment validation point.",
    "tvrip.tvrip_min_coverage": "Minimum validated source coverage required to keep a segmented TVRip dub.",
    "tvrip.tvrip_max_segments": "Diagnostic segment-count threshold for one TVRip source. Exceeding it is reported for review but does not skip output assembly; every mapped interval is still applied.",
    "tvrip.tvrip_continue_on_validation_warnings": "After explicit TVRip opt-in, continue assembling a renderable output when coverage, confidence, residual, or segment-review checks warn. Local acoustic probe mismatches remain report diagnostics so they cannot create false original-audio dropouts inside a mapped dub. Set false to restore strict local fallback and unattended rejection.",
    "tvrip.tvrip_min_segment_seconds": "Mapped segments shorter than this cannot receive independent validation.",
    "tvrip.tvrip_max_segment_seconds": "Split longer mapped ranges into regular independently validated slices.",
    "tvrip.tvrip_break_sensitivity_seconds": "Offset difference tolerated when adjacent TVRip mapping buckets are merged.",
    "tvrip.tvrip_commercial_min_seconds": "Mid-program source-only interval length classified as a probable commercial.",
    "tvrip.tvrip_retain_alternative_sections": "Record source-only alternate material; master video remains immutable so it cannot be inserted.",
    "tvrip.tvrip_fallback": "Override master-only TVRip behavior: ask, original, alternate-dub, silence, or omit.",
    "tvrip.tvrip_allow_speed_correction": "Allow a measured, bounded speed adjustment in the TVRip beta workflow.",
    "tvrip.tvrip_max_speed_adjustment": "Largest fractional speed adjustment accepted for TVRip source material.",
    "tvrip.tvrip_require_interactive_approval": "Require the segment checklist even when unattended TVRip synchronization is allowed.",
    "tvrip.tvrip_allow_partial_tracks": "Permit validated TVRip dubs with master-only intervals after applying the fallback policy.",
    "tvrip.tvrip_validation_positions": "Fractional positions independently checked inside every TVRip segment.",
    "tvrip.tvrip_validation_window_seconds": "Video duration decoded at every TVRip validation position.",
    "tvrip.tvrip_validation_search_seconds": "Local video-content search radius around each predicted TVRip validation point.",
    "tvrip.tvrip_track_title": "Display name for synchronized TVRip dubs; synchronization diagnostics remain in the JSON report.",
    "tvrip.tvrip_terminal_tail_validation": "Check an open-ended final Milksync bucket against common-original audio before retaining a possible broadcast-only ending.",
    "tvrip.tvrip_terminal_tail_window_seconds": "Maximum final-tail seconds compared with common-original audio; the available bounded tail may be shorter.",
    "tvrip.tvrip_terminal_tail_min_seconds": "Minimum comparable tail duration; shorter tails are reported as unverifiable rather than guessed. Matching silence is retained because it carries no audible material to replace.",
    "tvrip.tvrip_terminal_tail_min_similarity": "Minimum normalized common-original audio similarity needed to retain an open-ended final TVRip bucket; a lower score uses the selected fallback.",
    "tvrip.tvrip_acoustic_segment_validation": "Audit every mapped telecine interval with common-original audio so an unmatched HDTV-only scene is replaced by the configured master fallback; this also protects approved corrected-clock TVRip runs.",
    "tvrip.tvrip_acoustic_segment_window_seconds": "Common-original audio duration compared at each local telecine probe; longer windows are more resistant to brief silence and repeated shots.",
    "tvrip.tvrip_acoustic_segment_min_seconds": "Minimum mapped interval duration eligible for local telecine acoustic validation; with require_proof enabled, shorter intervals use the safe fallback instead of being guessed.",
    "tvrip.tvrip_acoustic_segment_max_gap_seconds": "Maximum distance between local acoustic probe starts in a mapped telecine interval. It also sets the long common-original validation horizon used before recovering an apparent master-only map hole with raw Portuguese audio; lower values catch shorter edits but require more FFmpeg probes.",
    "tvrip.tvrip_acoustic_segment_rejection_padding_seconds": "Master-timeline safety padding added before and after a failed local acoustic probe; the padded range uses fallback audio so a mismatched dub cannot leak across an edit.",
    "tvrip.tvrip_acoustic_segment_min_similarity": "Minimum normalized local common-original audio similarity required to retain a mapped telecine interval; a lower score uses the selected fallback.",
    "tvrip.tvrip_acoustic_segment_require_proof": "Fail closed when an interval lacks local telecine proof: replace only that interval with the configured master fallback instead of retaining unproven dub audio. Matching silence remains mapped; one-sided or unrelated audio is replaced around its probe.",
    "interface.output_format": "Terminal output: rich for formatted human output, plain, or json for scripts.",
    "interface.color": "Color policy for human terminal output.",
    "interface.progress": "Show progress indicators in non-interactive human terminal output.",
    "interface.quiet": "Suppress human-oriented terminal output while retaining machine-readable reports.",
    "interface.verbose": "Include diagnostic logging; cannot be combined with quiet.",
}

COMMENTED_PATH_EXAMPLES = (
    "Optional per-run paths; uncomment only when you want these defaults:",
    "path: /media/releases                 # Scan root; a positional CLI path overrides it.",
    "output_dir: /media/completed           # Destination root instead of output_dir_name below scan root.",
    "output: /media/completed/final.mkv     # Exact destination for one explicit pair.",
    "temp_dir: /media/releases/.dualmaker-work  # Private work root; do not use shared /tmp.",
    "report: /media/completed/dualmaker-report.json  # Exact JSON report destination.",
)

CONFIG_FILE_HEADER = (
    "# dualmaker user configuration\n"
    "# Precedence: command line > DUALMAKER_* environment > this file > defaults.\n"
    "# Edit this file at any time; reinstalling dualmaker is not required.\n"
    "# Every persistent setting is documented below; optional per-run paths stay commented.\n\n"
)


def _parse_bool(value: Any, *, label: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(
        f"{label} must be true/false, yes/no, on/off, or 1/0; received {value!r}"
    )


def _list_value(value: Any, *, separator: str = ",") -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return [part.strip() for part in str(value).split(separator) if part.strip()]


def _convert_value(key: str, value: Any, *, base: Path, label: str) -> Any:
    try:
        if key in BOOL_FIELDS:
            return _parse_bool(value, label=label)
        if key in PATH_FIELDS:
            path = Path(str(value)).expanduser()
            return (base / path).resolve() if not path.is_absolute() else path.resolve()
        if key in PATH_LIST_FIELDS:
            separator = os.pathsep if isinstance(value, str) else ","
            return tuple(
                (base / Path(str(item)).expanduser()).resolve()
                if not Path(str(item)).expanduser().is_absolute()
                else Path(str(item)).expanduser().resolve()
                for item in _list_value(value, separator=separator)
            )
        if key in STRING_LIST_FIELDS:
            return tuple(str(item).strip() for item in _list_value(value) if str(item).strip())
        if key in FLOAT_LIST_FIELDS:
            return tuple(float(item) for item in _list_value(value))
        if key in INT_LIST_FIELDS:
            return tuple(int(item) for item in _list_value(value))
        if key in INT_FIELDS:
            return int(value)
        if key in FLOAT_FIELDS:
            return None if value is None else float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"Invalid value for {label}: {value!r}") from exc
    if key in CHOICES:
        normalized = str(value).strip().casefold()
        if normalized not in CHOICES[key]:
            choices = ", ".join(sorted(CHOICES[key]))
            raise ConfigurationError(f"{label} must be one of {choices}; received {value!r}")
        return normalized
    if value is None:
        return None
    return str(value)


def _convert_binary(value: Any, *, base: Path) -> str:
    configured = Path(str(value)).expanduser()
    if configured.parent != Path(".") or configured.is_absolute():
        resolved = configured if configured.is_absolute() else base / configured
        return str(resolved.resolve())
    return str(value)


def default_user_config_path(environment: Mapping[str, str] | None = None) -> Path:
    """Return the preferred per-user YAML configuration path.

    ``DUALMAKER_CONFIG_HOME`` is intentionally a directory override rather than a
    configuration value: it makes isolated installations and tests possible without
    changing the user's HOME directory.
    """

    environment = os.environ if environment is None else environment
    configured_home = environment.get(CONFIG_HOME_ENV)
    if configured_home:
        return Path(configured_home).expanduser().resolve() / "config.yml"
    home = Path(environment.get("HOME", str(Path.home()))).expanduser().resolve()
    return home / USER_CONFIG_RELATIVE


def _default_config_document() -> dict[str, Any]:
    config = DualMakerConfig()
    return {
        "dualmaker": {
            "recursive": config.recursive,
            "tag": config.tag,
            "conflict": config.conflict,
            "dub_language": config.dub_language,
            "original_language": config.original_language,
            "dual_audio_ids": list(config.dual_audio_ids),
            "normal_audio_ids": list(config.normal_audio_ids),
            "dub_track_selectors": list(config.dub_track_selectors),
            "original_track_selector": config.original_track_selector,
            "preferred_original_source": config.preferred_original_source,
            "preferred_dub_source": config.preferred_dub_source,
            "audio_codec_preference": list(config.audio_codec_preference),
            "audio_selection_margin": config.audio_selection_margin,
            "subtitle_policy": config.subtitle_policy,
            "sidecar_language_overrides": list(config.sidecar_language_overrides),
            "sidecar_dual_language": config.sidecar_dual_language,
            "recap_window": config.recap_window,
            "end_tolerance_ms": config.end_tolerance_ms,
            "dry_run": config.dry_run,
            "interactive": config.interactive,
            "keep_temp": config.keep_temp,
        },
        "paths": {
            "output_dir_name": config.output_dir_name,
            "work_dir_name": config.work_dir_name,
            "ignored_dir_names": list(config.ignored_dir_names),
            "allowed_paths": [],
            "required_paths": [],
            "enforce_paths": config.enforce_paths,
        },
        "tools": {
            **config.binaries,
            "minimum_mkvmerge_version": config.minimum_mkvmerge_version,
        },
        "security": {
            "required_group": config.required_group,
            "output_group": config.output_group,
        },
        "features": {
            "trim_recap": config.trim_recap,
            "end_trim": config.end_trim,
            "reconcile_av": config.reconcile_av,
            "av_tolerance_ms": config.av_tolerance_ms,
            "allow_experimental_fps_sync": config.allow_experimental_fps_sync,
            "compatible_fps_pairs": list(config.compatible_fps_pairs),
            "fps_max_drift_seconds": config.fps_max_drift_seconds,
            "fps_min_match_confidence": config.fps_min_match_confidence,
            "fps_validation_positions": list(config.fps_validation_positions),
            "fps_search_radius_seconds": config.fps_search_radius_seconds,
            "fps_speed_ratio_tolerance": config.fps_speed_ratio_tolerance,
            "fps_content_speed_factors": list(config.fps_content_speed_factors),
            "fps_audio_duration_ratio_tolerance": config.fps_audio_duration_ratio_tolerance,
            "fps_spectral_tempo_probe": config.fps_spectral_tempo_probe,
            "fps_spectral_min_pairs": config.fps_spectral_min_pairs,
            "fps_spectral_pair_min_seconds": config.fps_spectral_pair_min_seconds,
            "fps_spectral_pair_max_seconds": config.fps_spectral_pair_max_seconds,
            "fps_spectral_max_dispersion": config.fps_spectral_max_dispersion,
            "fps_spectral_slope_cluster_radius": config.fps_spectral_slope_cluster_radius,
            "fps_spectral_max_speed_adjustment": config.fps_spectral_max_speed_adjustment,
            "fps_spectral_max_projected_drift_seconds": config.fps_spectral_max_projected_drift_seconds,
            "fps_spectral_max_refinement_passes": config.fps_spectral_max_refinement_passes,
            "fps_spectral_refinement_damping": config.fps_spectral_refinement_damping,
            "fps_spectral_iterative_refinement": config.fps_spectral_iterative_refinement,
            "fps_spectral_min_post_map_anchors": config.fps_spectral_min_post_map_anchors,
            "fps_adaptive_anchors": config.fps_adaptive_anchors,
            "fps_anchor_sample_count": config.fps_anchor_sample_count,
            "fps_anchor_candidate_count": config.fps_anchor_candidate_count,
            "fps_anchor_window_seconds": config.fps_anchor_window_seconds,
            "fps_anchor_min_separation_seconds": config.fps_anchor_min_separation_seconds,
            "fps_anchor_global_coverage": config.fps_anchor_global_coverage,
            "fps_segmented_min_post_map_anchors": config.fps_segmented_min_post_map_anchors,
            "fps_segmented_min_post_map_span_seconds": config.fps_segmented_min_post_map_span_seconds,
            "dub_gap_fallback": config.dub_gap_fallback,
            "dub_gap_min_seconds": config.dub_gap_min_seconds,
            "dub_gap_min_coverage": config.dub_gap_min_coverage,
            "dub_gap_track_title": config.dub_gap_track_title,
            "align_framerate": config.align_framerate,
            "align_frames_too": config.align_frames_too,
            "only_delta": config.only_delta,
            "adjust_delay": config.adjust_delay,
            "preserve_silence": config.preserve_silence,
        },
        "tvrip": {
            "allow_tvrip_segment_sync": config.allow_tvrip_segment_sync,
            "tvrip_min_source_match_confidence": config.tvrip_min_source_match_confidence,
            "tvrip_min_segment_confidence": config.tvrip_min_segment_confidence,
            "tvrip_spectral_min_segment_confidence": config.tvrip_spectral_min_segment_confidence,
            "tvrip_spectral_min_source_match_confidence": config.tvrip_spectral_min_source_match_confidence,
            "tvrip_max_residual_seconds": config.tvrip_max_residual_seconds,
            "tvrip_min_coverage": config.tvrip_min_coverage,
            "tvrip_max_segments": config.tvrip_max_segments,
            "tvrip_continue_on_validation_warnings": config.tvrip_continue_on_validation_warnings,
            "tvrip_min_segment_seconds": config.tvrip_min_segment_seconds,
            "tvrip_max_segment_seconds": config.tvrip_max_segment_seconds,
            "tvrip_break_sensitivity_seconds": config.tvrip_break_sensitivity_seconds,
            "tvrip_commercial_min_seconds": config.tvrip_commercial_min_seconds,
            "tvrip_retain_alternative_sections": config.tvrip_retain_alternative_sections,
            "tvrip_fallback": config.tvrip_fallback,
            "tvrip_allow_speed_correction": config.tvrip_allow_speed_correction,
            "tvrip_max_speed_adjustment": config.tvrip_max_speed_adjustment,
            "tvrip_require_interactive_approval": config.tvrip_require_interactive_approval,
            "tvrip_allow_partial_tracks": config.tvrip_allow_partial_tracks,
            "tvrip_validation_positions": list(config.tvrip_validation_positions),
            "tvrip_validation_window_seconds": config.tvrip_validation_window_seconds,
            "tvrip_validation_search_seconds": config.tvrip_validation_search_seconds,
            "tvrip_track_title": config.tvrip_track_title,
            "tvrip_terminal_tail_validation": config.tvrip_terminal_tail_validation,
            "tvrip_terminal_tail_window_seconds": config.tvrip_terminal_tail_window_seconds,
            "tvrip_terminal_tail_min_seconds": config.tvrip_terminal_tail_min_seconds,
            "tvrip_terminal_tail_min_similarity": config.tvrip_terminal_tail_min_similarity,
            "tvrip_acoustic_segment_validation": config.tvrip_acoustic_segment_validation,
            "tvrip_acoustic_segment_window_seconds": config.tvrip_acoustic_segment_window_seconds,
            "tvrip_acoustic_segment_min_seconds": config.tvrip_acoustic_segment_min_seconds,
            "tvrip_acoustic_segment_max_gap_seconds": config.tvrip_acoustic_segment_max_gap_seconds,
            "tvrip_acoustic_segment_rejection_padding_seconds": config.tvrip_acoustic_segment_rejection_padding_seconds,
            "tvrip_acoustic_segment_min_similarity": config.tvrip_acoustic_segment_min_similarity,
            "tvrip_acoustic_segment_require_proof": config.tvrip_acoustic_segment_require_proof,
        },
        "interface": {
            "output_format": config.output_format,
            "color": config.color,
            "progress": config.progress,
            "quiet": config.quiet,
            "verbose": config.verbose,
        },
    }


def _commented_config(document_data: dict[str, Any]) -> str:
    """Render a canonical config document with adjacent maintained help."""

    missing_comments = {
        f"{section}.{key}"
        for section, values in document_data.items()
        for key in values
        if f"{section}.{key}" not in CONFIG_SETTING_COMMENTS
    }
    if missing_comments:
        missing = ", ".join(sorted(missing_comments))
        raise RuntimeError(f"Generated config is missing setting comments: {missing}")

    rendered = yaml.safe_dump(
        document_data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    lines: list[str] = []
    section: str | None = None
    for raw_line in rendered.splitlines():
        if raw_line and not raw_line.startswith((" ", "-")) and raw_line.endswith(":"):
            if raw_line == "tools:":
                lines.extend("  # " + item for item in COMMENTED_PATH_EXAMPLES)
            section = raw_line[:-1]
        elif raw_line.startswith("  ") and not raw_line.startswith("    "):
            key = raw_line.strip().partition(":")[0]
            comment = CONFIG_SETTING_COMMENTS.get(f"{section}.{key}") if section else None
            if comment:
                width = max(48, 92 - len(raw_line) + len(key))
                lines.extend(
                    "  # " + fragment
                    for fragment in textwrap.wrap(comment, width=width, break_long_words=False)
                )
        lines.append(raw_line)

    return "\n".join(lines) + "\n"


def _commented_default_config() -> str:
    """Render all persistent defaults with adjacent, centrally maintained help."""

    return _commented_config(_default_config_document())


def initialize_config_file(path: Path | None = None) -> tuple[Path, bool]:
    """Create a default YAML config without ever replacing an existing file.

    Returns ``(resolved_path, created)``. The containing directory and file are
    private to the current user when newly created.
    """

    target = (path or default_user_config_path()).expanduser().resolve()
    if target.suffix.casefold() not in {".yml", ".yaml"}:
        raise ConfigurationError(
            f"The generated configuration must use a .yml or .yaml filename: {target}"
        )
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        document = _commented_default_config()
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(CONFIG_FILE_HEADER)
            handle.write(document)
    except FileExistsError:
        return target, False
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Cannot create configuration file {target}: {exc}") from exc
    return target, True


def refresh_config_file(path: Path | None = None) -> tuple[Path, Path | None]:
    """Rewrite a user config with all current settings, preserving values and a backup.

    Unlike ``initialize_config_file()``, this intentionally updates an existing
    file. It parses only supported settings, carries their values into the
    current canonical commented document, writes a timestamped private backup,
    and atomically replaces the original. Callers must opt in explicitly.
    """

    target = (path or default_user_config_path()).expanduser().resolve()
    if target.suffix.casefold() not in {".yml", ".yaml"}:
        raise ConfigurationError(
            f"Refreshing writes annotated YAML; choose a .yml or .yaml path: {target}"
        )
    backup: Path | None = None
    if target.exists():
        if not target.is_file():
            raise ConfigurationError(f"Configuration path is not a regular file: {target}")
        existing = _read_config(target)
        document = _default_config_document()
        for key, value in existing.items():
            if key == "binaries":
                document["tools"].update(value)
                continue
            for settings in document.values():
                if key in settings:
                    settings[key] = value
                    break
            else:
                if key in {"path", "output_dir", "output", "temp_dir", "report"}:
                    document["paths"][key] = value
                else:
                    raise ConfigurationError(
                        f"Cannot refresh unsupported configuration setting: {key}"
                    )
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = target.with_name(f"{target.name}.bak-{timestamp}")
        suffix = 2
        while backup.exists():
            backup = target.with_name(f"{target.name}.bak-{timestamp}.{suffix}")
            suffix += 1
        try:
            backup.write_bytes(target.read_bytes())
            os.chmod(backup, 0o600)
        except OSError as exc:
            raise ConfigurationError(f"Cannot create configuration backup {backup}: {exc}") from exc
    else:
        document = _default_config_document()

    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.refresh-{os.getpid()}")
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(CONFIG_FILE_HEADER)
            handle.write(_commented_config(document))
        os.replace(staged, target)
    except OSError as exc:
        raise ConfigurationError(f"Cannot refresh configuration file {target}: {exc}") from exc
    return target, backup


def _read_config(path: Path) -> dict[str, Any]:
    try:
        if path.suffix.casefold() in {".yml", ".yaml"}:
            with path.open("r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle) or {}
        else:
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
    except OSError as exc:
        raise ConfigurationError(f"Cannot read configuration file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"Invalid TOML in {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(f"Configuration root must be a mapping/table: {path}")

    flattened: dict[str, Any] = {}
    for key, value in raw.items():
        if key in CONFIG_SECTIONS:
            if not isinstance(value, dict):
                raise ConfigurationError(f"Configuration section {key!r} must be a mapping/table")
            allowed = CONFIG_SECTIONS[key]
            unknown = set(value) - allowed
            if unknown:
                raise ConfigurationError(
                    f"Unknown setting(s) in section {key!r}: {', '.join(sorted(unknown))}"
                )
            if key == "tools":
                binaries = dict(flattened.get("binaries", {}))
                for tool, tool_value in value.items():
                    if tool in BINARY_NAMES:
                        binaries[tool] = tool_value
                    else:
                        flattened[tool] = tool_value
                flattened["binaries"] = binaries
            else:
                flattened.update(value)
        elif key in CONFIG_KEYS:
            flattened[key] = value
        else:
            raise ConfigurationError(f"Unknown top-level configuration setting: {key}")
    return flattened


def _find_config_path(
    requested: Path | None,
    environment: Mapping[str, str],
    cwd: Path,
    *,
    bootstrap_user_config: bool,
) -> tuple[Path | None, bool]:
    explicit = requested or (
        Path(environment[f"{ENV_PREFIX}CONFIG"]) if environment.get(f"{ENV_PREFIX}CONFIG") else None
    )
    if explicit:
        resolved = explicit.expanduser().resolve()
        if not resolved.is_file():
            raise ConfigurationError(f"Configuration file does not exist: {resolved}")
        return resolved, True
    for name in LOCAL_CONFIG_NAMES:
        local = cwd / name
        if local.is_file():
            return local.resolve(), False
    user = default_user_config_path(environment)
    if user.is_file():
        return user.resolve(), False
    home = Path(environment.get("HOME", str(Path.home()))).expanduser().resolve()
    legacy_user = home / LEGACY_USER_CONFIG_RELATIVE
    if legacy_user.is_file():
        return legacy_user.resolve(), False
    if bootstrap_user_config:
        created_path, _ = initialize_config_file(user)
        return created_path, False
    return None, False


def load_configuration(
    cli_values: Mapping[str, Any],
    *,
    config_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    bootstrap_user_config: bool = False,
) -> DualMakerConfig:
    """Resolve defaults < YAML/TOML file < environment < explicit CLI values.

    Library callers opt into user-config creation with ``bootstrap_user_config``;
    the command-line entry point enables it on normal runs.
    """

    environment = os.environ if environment is None else environment
    cwd = (cwd or Path.cwd()).resolve()
    base_config = DualMakerConfig()
    values = {field.name: getattr(base_config, field.name) for field in fields(base_config)}
    values["binaries"] = dict(DEFAULT_BINARIES)
    sources = {key: "default" for key in values if key not in {"config_sources", "config_file"}}

    resolved_config_path, _ = _find_config_path(
        config_path,
        environment,
        cwd,
        bootstrap_user_config=bootstrap_user_config,
    )
    if resolved_config_path:
        file_values = _read_config(resolved_config_path)
        file_base = resolved_config_path.parent
        file_binaries = file_values.pop("binaries", {})
        for key, value in file_values.items():
            values[key] = _convert_value(
                key, value, base=file_base, label=f"{resolved_config_path}:{key}"
            )
            sources[key] = f"config:{resolved_config_path}"
        for name, value in file_binaries.items():
            if name not in BINARY_NAMES:
                raise ConfigurationError(f"Unknown binary name in {resolved_config_path}: {name}")
            values["binaries"][name] = _convert_binary(value, base=file_base)
            sources[f"binary.{name}"] = f"config:{resolved_config_path}"

    for env_suffix, target in ENV_KEYS.items():
        env_name = f"{ENV_PREFIX}{env_suffix}"
        if env_name not in environment:
            continue
        raw = environment[env_name]
        if target.startswith("binary."):
            name = target.partition(".")[2]
            values["binaries"][name] = _convert_binary(raw, base=cwd)
            sources[target] = f"environment:{env_name}"
        else:
            values[target] = _convert_value(target, raw, base=cwd, label=env_name)
            sources[target] = f"environment:{env_name}"

    cli_binaries = cli_values.get("binaries") or {}
    for key, value in cli_values.items():
        if key == "binaries" or value is None:
            continue
        if key not in CONFIG_KEYS:
            continue
        values[key] = _convert_value(key, value, base=cwd, label=f"--{key.replace('_', '-')}")
        sources[key] = "command-line"
    for name, value in cli_binaries.items():
        if value is not None:
            values["binaries"][name] = _convert_binary(value, base=cwd)
            sources[f"binary.{name}"] = "command-line"

    values["path"] = Path(values["path"]).expanduser().resolve()
    if values["temp_dir"] is None:
        values["temp_dir"] = values["path"] / values["work_dir_name"]
        sources["temp_dir"] = "derived:path/work_dir_name"
    values["config_file"] = resolved_config_path
    values["config_sources"] = sources
    return DualMakerConfig(**values)


def _nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(resolved == root or resolved.is_relative_to(root) for root in roots)


def validate_configuration(
    config: DualMakerConfig,
    *,
    input_paths: tuple[Path, ...] = (),
    require_scan_path: bool = True,
    validate_binaries: bool = True,
) -> None:
    """Fail early with actionable diagnostics before media processing begins."""

    problems: list[str] = []
    if require_scan_path:
        if not config.path.exists():
            problems.append(f"scan path does not exist: {config.path}")
        elif not config.path.is_dir():
            problems.append(f"scan path is not a directory: {config.path}")
        elif not os.access(config.path, os.R_OK | os.X_OK):
            problems.append(f"scan path is not readable/searchable: {config.path}")

    if not config.tag.strip() or "/" in config.tag or "\x00" in config.tag:
        problems.append("tag must be non-empty and cannot contain '/' or a NUL character")
    if config.recap_window < 10:
        problems.append("recap_window must be at least 10 seconds")
    if config.end_tolerance_ms < 0:
        problems.append("end_tolerance_ms cannot be negative")
    if config.av_tolerance_ms < 0:
        problems.append("av_tolerance_ms cannot be negative")
    if config.audio_selection_margin < 0:
        problems.append("audio_selection_margin cannot be negative")
    if config.dub_gap_min_seconds < 0:
        problems.append("dub_gap_min_seconds cannot be negative")
    if not 0 <= config.dub_gap_min_coverage <= 1:
        problems.append("dub_gap_min_coverage must be between 0 and 1")
    try:
        config.dub_gap_track_title.format(
            mode="Validated",
            coverage=0.95,
            fallback="",
        )
    except (AttributeError, IndexError, KeyError, ValueError) as exc:
        problems.append(
            "dub_gap_track_title must be a valid format string using only mode, coverage, "
            f"and fallback: {exc}"
        )
    if config.fps_max_drift_seconds < 0:
        problems.append("fps_max_drift_seconds cannot be negative")
    if not 0 <= config.fps_min_match_confidence <= 1:
        problems.append("fps_min_match_confidence must be between 0 and 1")
    if config.fps_search_radius_seconds <= 0:
        problems.append("fps_search_radius_seconds must be positive")
    if config.fps_speed_ratio_tolerance < 0:
        problems.append("fps_speed_ratio_tolerance cannot be negative")
    if not 0 <= config.fps_audio_duration_ratio_tolerance < 0.25:
        problems.append("fps_audio_duration_ratio_tolerance must be at least 0 and below 0.25")
    for value in config.fps_content_speed_factors:
        try:
            factor = float(Fraction(value))
        except (ValueError, ZeroDivisionError):
            problems.append(
                f"fps_content_speed_factors contains an invalid rational value: {value!r}"
            )
            continue
        if not 0.5 <= factor <= 2.0:
            problems.append(
                "fps_content_speed_factors values must be between 0.5 and 2.0: "
                f"{value!r}"
            )
    if config.fps_spectral_min_pairs < 3:
        problems.append("fps_spectral_min_pairs must be at least 3")
    if config.fps_spectral_pair_min_seconds <= 0:
        problems.append("fps_spectral_pair_min_seconds must be positive")
    if config.fps_spectral_pair_max_seconds <= config.fps_spectral_pair_min_seconds:
        problems.append(
            "fps_spectral_pair_max_seconds must exceed fps_spectral_pair_min_seconds"
        )
    if config.fps_spectral_max_dispersion < 0:
        problems.append("fps_spectral_max_dispersion cannot be negative")
    if config.fps_spectral_slope_cluster_radius <= 0:
        problems.append("fps_spectral_slope_cluster_radius must be positive")
    if not 0 < config.fps_spectral_max_speed_adjustment < 0.5:
        problems.append("fps_spectral_max_speed_adjustment must be greater than 0 and below 0.5")
    if config.fps_spectral_max_projected_drift_seconds <= 0:
        problems.append("fps_spectral_max_projected_drift_seconds must be positive")
    if config.fps_spectral_max_refinement_passes < 0:
        problems.append("fps_spectral_max_refinement_passes cannot be negative")
    if not 0 < config.fps_spectral_refinement_damping <= 1:
        problems.append("fps_spectral_refinement_damping must be greater than 0 and at most 1")
    if config.fps_spectral_min_post_map_anchors < 1:
        problems.append("fps_spectral_min_post_map_anchors must be at least 1")
    if config.fps_anchor_sample_count < 3:
        problems.append("fps_anchor_sample_count must be at least 3")
    if config.fps_anchor_candidate_count < 1:
        problems.append("fps_anchor_candidate_count must be at least 1")
    if config.fps_anchor_window_seconds < 2:
        problems.append("fps_anchor_window_seconds must be at least 2 seconds")
    if config.fps_anchor_min_separation_seconds <= 0:
        problems.append("fps_anchor_min_separation_seconds must be positive")
    if not 0 < config.fps_anchor_global_coverage <= 1:
        problems.append("fps_anchor_global_coverage must be greater than 0 and at most 1")
    if config.fps_segmented_min_post_map_anchors < 2:
        problems.append("fps_segmented_min_post_map_anchors must be at least 2")
    if config.fps_segmented_min_post_map_span_seconds <= 0:
        problems.append("fps_segmented_min_post_map_span_seconds must be positive")
    if len(config.fps_validation_positions) < 3 or any(
        position <= 0 or position >= 1 for position in config.fps_validation_positions
    ):
        problems.append(
            "fps_validation_positions must contain at least three values strictly between 0 and 1"
        )
    if not 0 <= config.tvrip_min_source_match_confidence <= 1:
        problems.append("tvrip_min_source_match_confidence must be between 0 and 1")
    if not 0 <= config.tvrip_min_segment_confidence <= 1:
        problems.append("tvrip_min_segment_confidence must be between 0 and 1")
    if not 0 <= config.tvrip_spectral_min_segment_confidence <= 1:
        problems.append("tvrip_spectral_min_segment_confidence must be between 0 and 1")
    if not 0 <= config.tvrip_spectral_min_source_match_confidence <= 1:
        problems.append(
            "tvrip_spectral_min_source_match_confidence must be between 0 and 1"
        )
    if config.tvrip_max_residual_seconds < 0:
        problems.append("tvrip_max_residual_seconds cannot be negative")
    if not 0 <= config.tvrip_min_coverage <= 1:
        problems.append("tvrip_min_coverage must be between 0 and 1")
    if config.tvrip_max_segments < 1:
        problems.append("tvrip_max_segments must be at least 1")
    if config.tvrip_min_segment_seconds <= 0:
        problems.append("tvrip_min_segment_seconds must be positive")
    if config.tvrip_max_segment_seconds < config.tvrip_min_segment_seconds:
        problems.append(
            "tvrip_max_segment_seconds must be at least tvrip_min_segment_seconds"
        )
    if config.tvrip_break_sensitivity_seconds <= 0:
        problems.append("tvrip_break_sensitivity_seconds must be positive")
    if config.tvrip_commercial_min_seconds <= 0:
        problems.append("tvrip_commercial_min_seconds must be positive")
    if not 0 <= config.tvrip_max_speed_adjustment < 1:
        problems.append("tvrip_max_speed_adjustment must be between 0 (inclusive) and 1")
    if len(config.tvrip_validation_positions) < 3 or any(
        position <= 0 or position >= 1 for position in config.tvrip_validation_positions
    ):
        problems.append(
            "tvrip_validation_positions must contain at least three values strictly between 0 and 1"
        )
    if config.tvrip_validation_window_seconds <= 0:
        problems.append("tvrip_validation_window_seconds must be positive")
    if config.tvrip_validation_search_seconds < config.tvrip_max_residual_seconds:
        problems.append(
            "tvrip_validation_search_seconds must be at least tvrip_max_residual_seconds"
        )
    if config.tvrip_terminal_tail_window_seconds <= 0:
        problems.append("tvrip_terminal_tail_window_seconds must be positive")
    if config.tvrip_terminal_tail_min_seconds <= 0:
        problems.append("tvrip_terminal_tail_min_seconds must be positive")
    if config.tvrip_terminal_tail_window_seconds < config.tvrip_terminal_tail_min_seconds:
        problems.append(
            "tvrip_terminal_tail_window_seconds must be at least "
            "tvrip_terminal_tail_min_seconds"
        )
    if not 0 <= config.tvrip_terminal_tail_min_similarity <= 1:
        problems.append("tvrip_terminal_tail_min_similarity must be between 0 and 1")
    if config.tvrip_acoustic_segment_window_seconds <= 0:
        problems.append("tvrip_acoustic_segment_window_seconds must be positive")
    if config.tvrip_acoustic_segment_min_seconds <= 0:
        problems.append("tvrip_acoustic_segment_min_seconds must be positive")
    if (
        config.tvrip_acoustic_segment_window_seconds
        < config.tvrip_acoustic_segment_min_seconds
    ):
        problems.append(
            "tvrip_acoustic_segment_window_seconds must be at least "
            "tvrip_acoustic_segment_min_seconds"
        )
    if not 0 <= config.tvrip_acoustic_segment_min_similarity <= 1:
        problems.append("tvrip_acoustic_segment_min_similarity must be between 0 and 1")
    if config.tvrip_acoustic_segment_max_gap_seconds <= 0:
        problems.append("tvrip_acoustic_segment_max_gap_seconds must be positive")
    if config.tvrip_acoustic_segment_rejection_padding_seconds < 0:
        problems.append(
            "tvrip_acoustic_segment_rejection_padding_seconds cannot be negative"
        )
    try:
        config.tvrip_track_title.format(
            mode="Segmented",
            coverage=0.95,
            fallback="",
        )
    except (AttributeError, IndexError, KeyError, ValueError) as exc:
        problems.append(
            "tvrip_track_title must be a valid format string using only mode, coverage, "
            f"and fallback: {exc}"
        )
    if not config.audio_codec_preference:
        problems.append("audio_codec_preference cannot be empty")
    for label, selector in (
        ("original_track_selector", config.original_track_selector),
        *(("dub_track_selectors", item) for item in config.dub_track_selectors),
    ):
        if selector is None:
            continue
        source, separator, track_id = selector.partition(":")
        if source.casefold() not in {"master", "normal", "dual"} or not separator:
            problems.append(f"{label} must use SOURCE:TRACK_ID (master or dual): {selector!r}")
            continue
        try:
            if int(track_id) < 0:
                raise ValueError
        except ValueError:
            problems.append(f"{label} has an invalid non-negative track ID: {selector!r}")
    for pair in config.compatible_fps_pairs:
        left, separator, right = pair.partition("=")
        try:
            valid = bool(separator) and Fraction(left) > 0 and Fraction(right) > 0
        except (ValueError, ZeroDivisionError):
            valid = False
        if not valid:
            problems.append(
                f"compatible_fps_pairs entry must use RATE=RATE with positive rationals: {pair!r}"
            )
    if config.minimum_mkvmerge_version < 1:
        problems.append("minimum_mkvmerge_version must be a positive integer")
    if config.verbose and config.quiet:
        problems.append("verbose and quiet modes are mutually exclusive")
    if config.output_format == "json" and config.interactive:
        problems.append("interactive mode cannot be combined with JSON output")
    if config.interactive and config.quiet:
        problems.append("interactive mode cannot be combined with quiet output")
    if not config.output_dir_name or "/" in config.output_dir_name:
        problems.append("output_dir_name must be a non-empty directory name, not a path")
    if not config.work_dir_name or "/" in config.work_dir_name:
        problems.append("work_dir_name must be a non-empty directory name, not a path")
    if config.dub_language.casefold().replace("_", "-") not in {"pt", "por", "pob", "pt-br"}:
        problems.append("dub_language must be Portuguese: pt, por, pob, or pt-BR")

    required_paths = tuple(path.expanduser().resolve() for path in config.required_paths)
    for required in required_paths:
        if not required.exists():
            problems.append(f"required path does not exist: {required}")

    allowed = tuple(path.expanduser().resolve() for path in config.allowed_paths)
    for root in allowed:
        if not root.is_dir():
            problems.append(f"allowed path is not an existing directory: {root}")
    if config.enforce_paths and not allowed:
        problems.append("path enforcement is enabled but no allowed_paths are configured")

    destinations = [config.temp_dir]
    if config.output:
        destinations.append(config.output.parent)
    elif config.output_dir:
        destinations.append(config.output_dir)
    else:
        destinations.append(config.path / config.output_dir_name)
    if config.report:
        destinations.append(config.report.parent)
    for destination in (path for path in destinations if path is not None):
        resolved = destination.expanduser().resolve()
        ancestor = _nearest_existing(resolved)
        if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
            problems.append(
                f"destination is not writable (nearest existing parent: {ancestor}): {resolved}"
            )

    checked_inputs = (config.path, *input_paths)
    if config.enforce_paths:
        for path in (*checked_inputs, *(path for path in destinations if path is not None)):
            if not _inside(path, allowed):
                problems.append(f"path is outside configured allowed_paths: {path.resolve()}")

    current_groups = {os.getgid(), os.getegid(), *os.getgroups()}
    for setting_name, group_name in (
        ("required_group", config.required_group),
        ("output_group", config.output_group),
    ):
        if not group_name:
            continue
        try:
            configured_group = grp.getgrnam(group_name)
        except KeyError:
            problems.append(f"{setting_name} does not exist: {group_name}")
        else:
            if configured_group.gr_gid not in current_groups:
                problems.append(f"current user is not a member of {setting_name} {group_name!r}")

    if validate_binaries:
        for name, configured in config.binaries.items():
            if name not in BINARY_NAMES:
                problems.append(f"unknown configured binary: {name}")
                continue
            binary = Path(configured).expanduser()
            if binary.parent != Path(".") or binary.is_absolute():
                resolved = binary.resolve()
                if not resolved.is_file():
                    problems.append(f"configured {name} binary does not exist: {resolved}")
                elif not os.access(resolved, os.X_OK):
                    problems.append(f"configured {name} binary is not executable: {resolved}")
            elif shutil.which(configured) is None:
                problems.append(
                    f"required binary {name!r} was not found; install it, add it to PATH, "
                    f"or set {ENV_PREFIX}{name.upper()}"
                )

    if problems:
        formatted = "\n".join(f"  - {problem}" for problem in problems)
        raise ConfigurationError(f"Configuration validation failed:\n{formatted}")


def configuration_as_dict(config: DualMakerConfig) -> dict[str, Any]:
    """Return a readable resolved view without duplicating dataclass serialization policy."""

    from .models import jsonable

    return jsonable(config)
