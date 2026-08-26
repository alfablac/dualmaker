from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Literal

from .defaults import (
    DEFAULT_ALLOW_EXPERIMENTAL_FPS_SYNC,
    DEFAULT_ALLOW_TVRIP_SEGMENT_SYNC,
    DEFAULT_AUDIO_CODEC_PREFERENCE,
    DEFAULT_AUDIO_SELECTION_MARGIN,
    DEFAULT_AV_TOLERANCE_MS,
    DEFAULT_BINARIES,
    DEFAULT_COLOR_MODE,
    DEFAULT_COMPATIBLE_FPS_PAIRS,
    DEFAULT_CONFLICT_POLICY,
    DEFAULT_DUAL_SIDECAR_LANGUAGE,
    DEFAULT_DUB_GAP_FALLBACK,
    DEFAULT_DUB_GAP_MIN_COVERAGE,
    DEFAULT_DUB_GAP_MIN_SECONDS,
    DEFAULT_DUB_GAP_TRACK_TITLE,
    DEFAULT_DUB_LANGUAGE,
    DEFAULT_END_TOLERANCE_MS,
    DEFAULT_EXPERIMENTAL_DUB_RESYNC,
    DEFAULT_EXPERIMENTAL_DUB_RESYNC_MIN_CONFIDENCE,
    DEFAULT_FPS_ADAPTIVE_ANCHORS,
    DEFAULT_FPS_ANCHOR_CANDIDATE_COUNT,
    DEFAULT_FPS_ANCHOR_GLOBAL_COVERAGE,
    DEFAULT_FPS_ANCHOR_MIN_SEPARATION_SECONDS,
    DEFAULT_FPS_ANCHOR_SAMPLE_COUNT,
    DEFAULT_FPS_ANCHOR_WINDOW_SECONDS,
    DEFAULT_FPS_AUDIO_DURATION_RATIO_TOLERANCE,
    DEFAULT_FPS_CONTENT_SPEED_FACTORS,
    DEFAULT_FPS_MAX_DRIFT_SECONDS,
    DEFAULT_FPS_MIN_MATCH_CONFIDENCE,
    DEFAULT_FPS_SEARCH_RADIUS_SECONDS,
    DEFAULT_FPS_SEGMENTED_MIN_POST_MAP_ANCHORS,
    DEFAULT_FPS_SEGMENTED_MIN_POST_MAP_SPAN_SECONDS,
    DEFAULT_FPS_SPECTRAL_ITERATIVE_REFINEMENT,
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
    DEFAULT_FPS_SPECTRAL_TEMPO_PROBE,
    DEFAULT_FPS_SPEED_RATIO_TOLERANCE,
    DEFAULT_FPS_VALIDATION_POSITIONS,
    DEFAULT_IGNORED_DIR_NAMES,
    DEFAULT_MILKSYNC_CHROMA_WORKERS,
    DEFAULT_MILKSYNC_MAX_COST_MATRIX_CELLS,
    DEFAULT_MILKSYNC_MAX_THREADS,
    DEFAULT_MIN_MKVMERGE_VERSION,
    DEFAULT_OUTPUT_DIR_NAME,
    DEFAULT_OUTPUT_FORMAT,
    DEFAULT_PREFERRED_DUB_SOURCE,
    DEFAULT_PREFERRED_ORIGINAL_SOURCE,
    DEFAULT_RECAP_WINDOW,
    DEFAULT_RECONCILE_AV,
    DEFAULT_SUBTITLE_POLICY,
    DEFAULT_TAG,
    DEFAULT_TVRIP_ACOUSTIC_SEGMENT_MAX_GAP_SECONDS,
    DEFAULT_TVRIP_ACOUSTIC_SEGMENT_MIN_SECONDS,
    DEFAULT_TVRIP_ACOUSTIC_SEGMENT_MIN_SIMILARITY,
    DEFAULT_TVRIP_ACOUSTIC_SEGMENT_REJECTION_PADDING_SECONDS,
    DEFAULT_TVRIP_ACOUSTIC_SEGMENT_REQUIRE_PROOF,
    DEFAULT_TVRIP_ACOUSTIC_SEGMENT_VALIDATION,
    DEFAULT_TVRIP_ACOUSTIC_SEGMENT_WINDOW_SECONDS,
    DEFAULT_TVRIP_ALLOW_PARTIAL_TRACKS,
    DEFAULT_TVRIP_ALLOW_SPEED_CORRECTION,
    DEFAULT_TVRIP_BREAK_SENSITIVITY_SECONDS,
    DEFAULT_TVRIP_COMMERCIAL_MIN_SECONDS,
    DEFAULT_TVRIP_CONTINUE_ON_VALIDATION_WARNINGS,
    DEFAULT_TVRIP_FALLBACK,
    DEFAULT_TVRIP_MAX_RESIDUAL_SECONDS,
    DEFAULT_TVRIP_MAX_SEGMENT_SECONDS,
    DEFAULT_TVRIP_MAX_SEGMENTS,
    DEFAULT_TVRIP_MAX_SPEED_ADJUSTMENT,
    DEFAULT_TVRIP_MIN_COVERAGE,
    DEFAULT_TVRIP_MIN_SEGMENT_CONFIDENCE,
    DEFAULT_TVRIP_MIN_SEGMENT_SECONDS,
    DEFAULT_TVRIP_MIN_SOURCE_MATCH_CONFIDENCE,
    DEFAULT_TVRIP_REQUIRE_INTERACTIVE_APPROVAL,
    DEFAULT_TVRIP_RETAIN_ALTERNATIVE_SECTIONS,
    DEFAULT_TVRIP_SPECTRAL_MIN_SEGMENT_CONFIDENCE,
    DEFAULT_TVRIP_SPECTRAL_MIN_SOURCE_MATCH_CONFIDENCE,
    DEFAULT_TVRIP_TERMINAL_TAIL_MIN_SECONDS,
    DEFAULT_TVRIP_TERMINAL_TAIL_MIN_SIMILARITY,
    DEFAULT_TVRIP_TERMINAL_TAIL_VALIDATION,
    DEFAULT_TVRIP_TERMINAL_TAIL_WINDOW_SECONDS,
    DEFAULT_TVRIP_TRACK_TITLE,
    DEFAULT_TVRIP_VALIDATION_POSITIONS,
    DEFAULT_TVRIP_VALIDATION_SEARCH_SECONDS,
    DEFAULT_TVRIP_VALIDATION_WINDOW_SECONDS,
    DEFAULT_WORK_DIR_NAME,
)

TrackKind = Literal["video", "audio", "subtitles", "buttons", "unknown"]
ConflictPolicy = Literal["increment", "skip", "error"]
OutputFormat = Literal["rich", "plain", "json"]
ColorMode = Literal["auto", "always", "never"]
SubtitlePolicy = Literal["prefer-master", "exact-union"]
AudioSource = Literal["master", "dual"]
SourceKind = Literal["dual", "tvrip"]
AlignmentMode = Literal["common-original", "cross-language-events"]
TVRipFallback = Literal["ask", "original", "alternate-dub", "silence", "omit"]
DubGapFallback = Literal["original", "silence", "off"]


def jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    return value


@dataclass(slots=True)
class Track:
    id: int
    kind: TrackKind
    type_index: int
    codec: str = ""
    codec_id: str = ""
    language: str = "und"
    language_ietf: str = "und"
    title: str = ""
    default: bool = False
    forced: bool = False
    hearing_impaired: bool = False
    commentary: bool = False
    channels: int | None = None
    bitrate: int | None = None
    sample_rate: int | None = None
    duration: float | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_language(self) -> str:
        return self.language_ietf or self.language or "und"


@dataclass(slots=True)
class Attachment:
    id: int
    name: str
    content_type: str = "application/octet-stream"
    size: int | None = None
    description: str = ""


@dataclass(slots=True, frozen=True)
class FrameRate:
    numerator: int
    denominator: int
    source: str = "avg_frame_rate"

    @property
    def decimal(self) -> float:
        return self.numerator / self.denominator

    @property
    def rational(self) -> str:
        return f"{self.numerator}/{self.denominator}"

    @property
    def display(self) -> str:
        return f"{self.decimal:.3f} fps ({self.rational})"


@dataclass(slots=True)
class AudioTrackSelection:
    source: AudioSource
    track: Track
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    explicit: bool = False

    @property
    def label(self) -> str:
        return f"{self.source}:{self.track.id}"


@dataclass(slots=True, frozen=True)
class SidecarSubtitleCandidate:
    """An external subtitle associated with either member of a release pair."""

    path: Path
    source: AudioSource


@dataclass(slots=True, frozen=True)
class SidecarSubtitle:
    """A sidecar subtitle after its language has been resolved."""

    path: Path
    source: AudioSource
    language: str


@dataclass(slots=True)
class FPSMatchSample:
    position: float
    target_time: float
    source_time: float
    score: float


@dataclass(slots=True)
class FPSDecision:
    required: bool = False
    compatible: bool = True
    approved: bool = False
    master_rate: FrameRate | None = None
    dual_rate: FrameRate | None = None
    expected_drift_seconds: float = 0.0
    proposed_speed_factor: float = 1.0
    apply_speed_correction: bool = False
    detected_speed_factor: float | None = None
    confidence: float | None = None
    reason: str = "Input frame rates have not been evaluated"
    samples: list[FPSMatchSample] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TVRipValidationPoint:
    position: float
    source_time: float
    master_time: float
    confidence: float
    residual_seconds: float


@dataclass(slots=True)
class TVRipSegment:
    index: int
    source_start: float
    source_end: float
    master_start: float
    master_end: float
    offset_seconds: float
    speed_factor: float = 1.0
    confidence: float = 0.0
    residual_seconds: float | None = None
    status: Literal["accepted", "ambiguous", "rejected"] = "ambiguous"
    operation: str = "Synchronize matching content"
    validation_points: list[TVRipValidationPoint] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(self.master_end - self.master_start, 0.0)


@dataclass(slots=True, frozen=True)
class TVRipInterval:
    start: float
    end: float
    kind: Literal["tvrip-only", "master-only"]
    classification: str

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.0)


@dataclass(slots=True)
class TVRipSyncReport:
    enabled: bool = True
    approved: bool = False
    source_analysis: dict[str, Any] = field(default_factory=dict)
    segments: list[TVRipSegment] = field(default_factory=list)
    tvrip_only: list[TVRipInterval] = field(default_factory=list)
    master_only: list[TVRipInterval] = field(default_factory=list)
    coverage: float = 0.0
    source_match_confidence: float = 0.0
    accepted_segments: int = 0
    ambiguous_segments: int = 0
    rejected_segments: int = 0
    fallback: TVRipFallback = "ask"
    fallback_intervals: list[TVRipInterval] = field(default_factory=list)
    speed_correction: float = 1.0
    warnings: list[str] = field(default_factory=list)
    result: Literal["pending", "accepted", "rejected"] = "pending"
    reason: str = "TVRip segment analysis has not run"
    workflow: Literal["tvrip", "dub-gap"] = "tvrip"
    minimum_master_gap_seconds: float = 0.01


@dataclass(slots=True, frozen=True)
class ContentIdentity:
    kind: Literal["movie", "episode", "unknown"]
    title: str
    year: int | None = None
    season: int | None = None
    episodes: tuple[int, ...] = ()

    @property
    def key(self) -> tuple[Any, ...]:
        if self.kind == "episode":
            return self.kind, self.title, self.season, self.episodes
        return self.kind, self.title, self.year


@dataclass(slots=True)
class MediaAsset:
    path: Path
    duration: float
    tracks: list[Track]
    attachments: list[Attachment] = field(default_factory=list)
    chapters: list[dict[str, Any]] = field(default_factory=list)
    identity: ContentIdentity | None = None
    mediainfo: dict[str, Any] = field(default_factory=dict)
    mkvmerge: dict[str, Any] = field(default_factory=dict)
    ffprobe: dict[str, Any] = field(default_factory=dict)
    frame_rate: FrameRate | None = None

    @property
    def audio_tracks(self) -> list[Track]:
        return [track for track in self.tracks if track.kind == "audio"]

    @property
    def subtitle_tracks(self) -> list[Track]:
        return [track for track in self.tracks if track.kind == "subtitles"]

    @property
    def video_tracks(self) -> list[Track]:
        return [track for track in self.tracks if track.kind == "video"]


@dataclass(slots=True)
class PairCandidate:
    normal: MediaAsset
    dual: MediaAsset
    identity: ContentIdentity
    score: float
    shared_original_languages: tuple[str, ...]
    reasons: list[str] = field(default_factory=list)
    source_kind: SourceKind = "dual"
    alignment_mode: AlignmentMode = "common-original"


@dataclass(slots=True)
class DualMakerConfig:
    path: Path = Path(".")
    recursive: bool = False
    output_dir: Path | None = None
    output: Path | None = None
    tag: str = DEFAULT_TAG
    conflict: ConflictPolicy = DEFAULT_CONFLICT_POLICY
    dub_language: str = DEFAULT_DUB_LANGUAGE
    original_language: str | None = None
    dual_audio_ids: tuple[int, ...] = ()
    normal_audio_ids: tuple[int, ...] = ()
    dub_track_selectors: tuple[str, ...] = ()
    original_track_selector: str | None = None
    preferred_original_source: str = DEFAULT_PREFERRED_ORIGINAL_SOURCE
    preferred_dub_source: str = DEFAULT_PREFERRED_DUB_SOURCE
    dub_gap_fallback: DubGapFallback = DEFAULT_DUB_GAP_FALLBACK  # type: ignore[assignment]
    dub_gap_min_seconds: float = DEFAULT_DUB_GAP_MIN_SECONDS
    dub_gap_min_coverage: float = DEFAULT_DUB_GAP_MIN_COVERAGE
    dub_gap_track_title: str = DEFAULT_DUB_GAP_TRACK_TITLE
    audio_codec_preference: tuple[str, ...] = DEFAULT_AUDIO_CODEC_PREFERENCE
    audio_selection_margin: float = DEFAULT_AUDIO_SELECTION_MARGIN
    subtitle_policy: SubtitlePolicy = DEFAULT_SUBTITLE_POLICY  # type: ignore[assignment]
    sidecar_language_overrides: tuple[str, ...] = ()
    sidecar_dual_language: str = DEFAULT_DUAL_SIDECAR_LANGUAGE
    trim_recap: bool = True
    recap_window: float = DEFAULT_RECAP_WINDOW
    end_trim: bool = True
    end_tolerance_ms: int = DEFAULT_END_TOLERANCE_MS
    reconcile_av: bool = DEFAULT_RECONCILE_AV
    av_tolerance_ms: int = DEFAULT_AV_TOLERANCE_MS
    experimental_dub_resync: bool = DEFAULT_EXPERIMENTAL_DUB_RESYNC
    experimental_dub_resync_min_confidence: float = (
        DEFAULT_EXPERIMENTAL_DUB_RESYNC_MIN_CONFIDENCE
    )
    milksync_max_threads: int = DEFAULT_MILKSYNC_MAX_THREADS
    milksync_chroma_workers: int = DEFAULT_MILKSYNC_CHROMA_WORKERS
    milksync_max_cost_matrix_cells: int = DEFAULT_MILKSYNC_MAX_COST_MATRIX_CELLS
    allow_experimental_fps_sync: bool = DEFAULT_ALLOW_EXPERIMENTAL_FPS_SYNC
    compatible_fps_pairs: tuple[str, ...] = DEFAULT_COMPATIBLE_FPS_PAIRS
    fps_max_drift_seconds: float = DEFAULT_FPS_MAX_DRIFT_SECONDS
    fps_min_match_confidence: float = DEFAULT_FPS_MIN_MATCH_CONFIDENCE
    fps_validation_positions: tuple[float, ...] = DEFAULT_FPS_VALIDATION_POSITIONS
    fps_search_radius_seconds: float = DEFAULT_FPS_SEARCH_RADIUS_SECONDS
    fps_speed_ratio_tolerance: float = DEFAULT_FPS_SPEED_RATIO_TOLERANCE
    fps_content_speed_factors: tuple[str, ...] = DEFAULT_FPS_CONTENT_SPEED_FACTORS
    fps_audio_duration_ratio_tolerance: float = (
        DEFAULT_FPS_AUDIO_DURATION_RATIO_TOLERANCE
    )
    fps_spectral_tempo_probe: bool = DEFAULT_FPS_SPECTRAL_TEMPO_PROBE
    fps_spectral_min_pairs: int = DEFAULT_FPS_SPECTRAL_MIN_PAIRS
    fps_spectral_pair_min_seconds: float = DEFAULT_FPS_SPECTRAL_PAIR_MIN_SECONDS
    fps_spectral_pair_max_seconds: float = DEFAULT_FPS_SPECTRAL_PAIR_MAX_SECONDS
    fps_spectral_max_dispersion: float = DEFAULT_FPS_SPECTRAL_MAX_DISPERSION
    fps_spectral_slope_cluster_radius: float = DEFAULT_FPS_SPECTRAL_SLOPE_CLUSTER_RADIUS
    fps_spectral_max_speed_adjustment: float = DEFAULT_FPS_SPECTRAL_MAX_SPEED_ADJUSTMENT
    fps_spectral_max_projected_drift_seconds: float = (
        DEFAULT_FPS_SPECTRAL_MAX_PROJECTED_DRIFT_SECONDS
    )
    fps_spectral_max_refinement_passes: int = DEFAULT_FPS_SPECTRAL_MAX_REFINEMENT_PASSES
    fps_spectral_refinement_damping: float = DEFAULT_FPS_SPECTRAL_REFINEMENT_DAMPING
    fps_spectral_iterative_refinement: bool = DEFAULT_FPS_SPECTRAL_ITERATIVE_REFINEMENT
    fps_spectral_min_post_map_anchors: int = DEFAULT_FPS_SPECTRAL_MIN_POST_MAP_ANCHORS
    fps_adaptive_anchors: bool = DEFAULT_FPS_ADAPTIVE_ANCHORS
    fps_anchor_sample_count: int = DEFAULT_FPS_ANCHOR_SAMPLE_COUNT
    fps_anchor_candidate_count: int = DEFAULT_FPS_ANCHOR_CANDIDATE_COUNT
    fps_anchor_window_seconds: float = DEFAULT_FPS_ANCHOR_WINDOW_SECONDS
    fps_anchor_min_separation_seconds: float = DEFAULT_FPS_ANCHOR_MIN_SEPARATION_SECONDS
    fps_anchor_global_coverage: float = DEFAULT_FPS_ANCHOR_GLOBAL_COVERAGE
    fps_segmented_min_post_map_anchors: int = DEFAULT_FPS_SEGMENTED_MIN_POST_MAP_ANCHORS
    fps_segmented_min_post_map_span_seconds: float = (
        DEFAULT_FPS_SEGMENTED_MIN_POST_MAP_SPAN_SECONDS
    )
    allow_tvrip_segment_sync: bool = DEFAULT_ALLOW_TVRIP_SEGMENT_SYNC
    tvrip_min_source_match_confidence: float = DEFAULT_TVRIP_MIN_SOURCE_MATCH_CONFIDENCE
    tvrip_min_segment_confidence: float = DEFAULT_TVRIP_MIN_SEGMENT_CONFIDENCE
    tvrip_spectral_min_segment_confidence: float = (
        DEFAULT_TVRIP_SPECTRAL_MIN_SEGMENT_CONFIDENCE
    )
    tvrip_spectral_min_source_match_confidence: float = (
        DEFAULT_TVRIP_SPECTRAL_MIN_SOURCE_MATCH_CONFIDENCE
    )
    tvrip_max_residual_seconds: float = DEFAULT_TVRIP_MAX_RESIDUAL_SECONDS
    tvrip_min_coverage: float = DEFAULT_TVRIP_MIN_COVERAGE
    tvrip_max_segments: int = DEFAULT_TVRIP_MAX_SEGMENTS
    tvrip_continue_on_validation_warnings: bool = DEFAULT_TVRIP_CONTINUE_ON_VALIDATION_WARNINGS
    tvrip_min_segment_seconds: float = DEFAULT_TVRIP_MIN_SEGMENT_SECONDS
    tvrip_max_segment_seconds: float = DEFAULT_TVRIP_MAX_SEGMENT_SECONDS
    tvrip_break_sensitivity_seconds: float = DEFAULT_TVRIP_BREAK_SENSITIVITY_SECONDS
    tvrip_commercial_min_seconds: float = DEFAULT_TVRIP_COMMERCIAL_MIN_SECONDS
    tvrip_retain_alternative_sections: bool = DEFAULT_TVRIP_RETAIN_ALTERNATIVE_SECTIONS
    tvrip_fallback: TVRipFallback = DEFAULT_TVRIP_FALLBACK  # type: ignore[assignment]
    tvrip_allow_speed_correction: bool = DEFAULT_TVRIP_ALLOW_SPEED_CORRECTION
    tvrip_max_speed_adjustment: float = DEFAULT_TVRIP_MAX_SPEED_ADJUSTMENT
    tvrip_require_interactive_approval: bool = DEFAULT_TVRIP_REQUIRE_INTERACTIVE_APPROVAL
    tvrip_allow_partial_tracks: bool = DEFAULT_TVRIP_ALLOW_PARTIAL_TRACKS
    tvrip_validation_positions: tuple[float, ...] = DEFAULT_TVRIP_VALIDATION_POSITIONS
    tvrip_validation_window_seconds: float = DEFAULT_TVRIP_VALIDATION_WINDOW_SECONDS
    tvrip_validation_search_seconds: float = DEFAULT_TVRIP_VALIDATION_SEARCH_SECONDS
    tvrip_track_title: str = DEFAULT_TVRIP_TRACK_TITLE
    tvrip_terminal_tail_validation: bool = DEFAULT_TVRIP_TERMINAL_TAIL_VALIDATION
    tvrip_terminal_tail_window_seconds: float = DEFAULT_TVRIP_TERMINAL_TAIL_WINDOW_SECONDS
    tvrip_terminal_tail_min_seconds: float = DEFAULT_TVRIP_TERMINAL_TAIL_MIN_SECONDS
    tvrip_terminal_tail_min_similarity: float = DEFAULT_TVRIP_TERMINAL_TAIL_MIN_SIMILARITY
    tvrip_acoustic_segment_validation: bool = DEFAULT_TVRIP_ACOUSTIC_SEGMENT_VALIDATION
    tvrip_acoustic_segment_window_seconds: float = DEFAULT_TVRIP_ACOUSTIC_SEGMENT_WINDOW_SECONDS
    tvrip_acoustic_segment_min_seconds: float = DEFAULT_TVRIP_ACOUSTIC_SEGMENT_MIN_SECONDS
    tvrip_acoustic_segment_max_gap_seconds: float = DEFAULT_TVRIP_ACOUSTIC_SEGMENT_MAX_GAP_SECONDS
    tvrip_acoustic_segment_rejection_padding_seconds: float = (
        DEFAULT_TVRIP_ACOUSTIC_SEGMENT_REJECTION_PADDING_SECONDS
    )
    tvrip_acoustic_segment_min_similarity: float = DEFAULT_TVRIP_ACOUSTIC_SEGMENT_MIN_SIMILARITY
    tvrip_acoustic_segment_require_proof: bool = DEFAULT_TVRIP_ACOUSTIC_SEGMENT_REQUIRE_PROOF
    dry_run: bool = False
    interactive: bool = False
    temp_dir: Path | None = None
    keep_temp: bool = False
    report: Path | None = None
    verbose: bool = False
    quiet: bool = False
    align_framerate: bool = False
    align_frames_too: bool = False
    only_delta: bool = False
    adjust_delay: float | None = None
    preserve_silence: bool = False
    allowed_paths: tuple[Path, ...] = ()
    required_paths: tuple[Path, ...] = ()
    enforce_paths: bool = False
    required_group: str | None = None
    output_group: str | None = None
    binaries: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_BINARIES))
    output_format: OutputFormat = DEFAULT_OUTPUT_FORMAT
    color: ColorMode = DEFAULT_COLOR_MODE
    progress: bool = True
    output_dir_name: str = DEFAULT_OUTPUT_DIR_NAME
    work_dir_name: str = DEFAULT_WORK_DIR_NAME
    ignored_dir_names: tuple[str, ...] = DEFAULT_IGNORED_DIR_NAMES
    minimum_mkvmerge_version: int = DEFAULT_MIN_MKVMERGE_VERSION
    config_file: Path | None = None
    config_sources: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class JobPlan:
    normal: MediaAsset
    dual: MediaAsset
    identity: ContentIdentity
    output: Path
    normal_original: Track
    dual_original: Track
    dub_tracks: list[Track]
    normal_subtitles: list[Track]
    dual_subtitles: list[Track]
    sidecar_subtitles: list[SidecarSubtitle] = field(default_factory=list)
    score: float = 1.0
    reasons: list[str] = field(default_factory=list)
    normal_trim: float = 0.0
    dual_trim: float = 0.0
    dub_selections: list[AudioTrackSelection] = field(default_factory=list)
    output_original: AudioTrackSelection | None = None
    fps: FPSDecision = field(default_factory=FPSDecision)
    source_kind: SourceKind = "dual"
    alignment_mode: AlignmentMode = "common-original"

    @property
    def resolved_dubs(self) -> list[AudioTrackSelection]:
        if self.dub_selections:
            return self.dub_selections
        return [AudioTrackSelection("dual", track) for track in self.dub_tracks]

    @property
    def resolved_original(self) -> AudioTrackSelection:
        return self.output_original or AudioTrackSelection("master", self.normal_original)

    def to_dict(self, include_raw_metadata: bool = True) -> dict[str, Any]:
        data = jsonable(self)
        if not include_raw_metadata:
            for side in ("normal", "dual"):
                data[side].pop("mediainfo", None)
                data[side].pop("mkvmerge", None)
                data[side].pop("ffprobe", None)
        return data


@dataclass(slots=True)
class JobResult:
    status: Literal["planned", "success", "skipped", "failed"]
    output: Path | None = None
    message: str = ""
    plan: JobPlan | None = None
    sync_points: list[tuple[float, float, float]] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)
