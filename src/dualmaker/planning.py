from __future__ import annotations

import math
from pathlib import Path

from .errors import (
    AmbiguousPairError,
    ExperimentalFPSRequiredError,
    ExperimentalTVRipRequiredError,
    OutputConflictError,
    PairingError,
)
from .fpssync import evaluate_fps_pair
from .languages import base_language, is_portuguese, normalize_language
from .models import (
    AudioSource,
    AudioTrackSelection,
    DualMakerConfig,
    JobPlan,
    MediaAsset,
    PairCandidate,
    SidecarSubtitle,
    Track,
)
from .naming import choose_conflict_path, make_output_basename


def _asset(candidate: PairCandidate, source: AudioSource) -> MediaAsset:
    return candidate.normal if source == "master" else candidate.dual


def _parse_selector(value: str) -> tuple[AudioSource, int]:
    raw_source, separator, raw_id = value.partition(":")
    source: AudioSource = "master" if raw_source.casefold() in {"master", "normal"} else "dual"
    if not separator or raw_source.casefold() not in {"master", "normal", "dual"}:
        raise PairingError(f"Audio selector must use master:ID or dual:ID: {value!r}")
    try:
        track_id = int(raw_id)
    except ValueError as exc:
        raise PairingError(f"Audio selector has an invalid track ID: {value!r}") from exc
    return source, track_id


def _by_ids(asset: MediaAsset, ids: tuple[int, ...], *, kind: str) -> list[Track]:
    if not ids:
        return []
    found = [track for track in asset.tracks if track.kind == kind and track.id in ids]
    missing = sorted(set(ids) - {track.id for track in found})
    if missing:
        raise PairingError(f"{asset.path.name} has no {kind} track ID(s): {missing}")
    order = {track_id: index for index, track_id in enumerate(ids)}
    return sorted(found, key=lambda track: order[track.id])


def _selected_track(candidate: PairCandidate, selector: str) -> tuple[AudioSource, Track]:
    source, track_id = _parse_selector(selector)
    matches = _by_ids(_asset(candidate, source), (track_id,), kind="audio")
    return source, matches[0]


def _codec_rank(track: Track, config: DualMakerConfig) -> float:
    codec = f"{track.codec_id} {track.codec}".upper()
    for index, preferred in enumerate(config.audio_codec_preference):
        token = preferred.upper()
        if codec == token or codec.startswith(token) or token in codec:
            return float(len(config.audio_codec_preference) - index) * 10.0
    return 0.0


def _score_track(
    source: AudioSource,
    track: Track,
    asset: MediaAsset,
    config: DualMakerConfig,
    *,
    role: str,
    explicit: bool = False,
) -> AudioTrackSelection:
    reasons: list[str] = []
    score = _codec_rank(track, config)
    reasons.append(f"codec {track.codec_id or track.codec or 'unknown'}")
    if track.channels:
        score += track.channels * 1.5
        reasons.append(f"{track.channels} channels")
    if track.bitrate:
        score += min(math.log2(max(track.bitrate, 1) / 64_000 + 1), 6.0)
        reasons.append(f"{track.bitrate} bps")
    if track.sample_rate:
        score += min(track.sample_rate / 48_000, 2.0)
        reasons.append(f"{track.sample_rate} Hz")
    if track.duration is not None:
        difference = abs(asset.duration - track.duration)
        tolerance = max(2.0, asset.duration * 0.002)
        if difference <= tolerance:
            score += 8.0
            reasons.append("complete duration")
        elif difference <= max(10.0, asset.duration * 0.01):
            score += 3.0
            reasons.append(f"duration differs by {difference:.2f}s")
        else:
            score -= min(difference / 10.0, 15.0)
            reasons.append(f"possibly incomplete ({difference:.2f}s short/different)")
    else:
        reasons.append("duration unavailable")
    if track.default:
        score += 0.25
        reasons.append("source default")
    if role == "dub" and normalize_language(track.effective_language) == "pt-BR":
        score += 0.25
        reasons.append("specific pt-BR tag")
    preferred = (
        config.preferred_dub_source if role == "dub" else config.preferred_original_source
    )
    if preferred == source:
        bonus = max(config.audio_selection_margin + 0.1, 1.0)
        score += bonus
        reasons.append(f"preferred {source} source")
    if explicit:
        score += 1000.0
        reasons.append("explicit user selection")
    return AudioTrackSelection(source, track, score, reasons, explicit)


def _choose_best(
    role: str,
    choices: list[AudioTrackSelection],
    config: DualMakerConfig,
) -> AudioTrackSelection:
    if not choices:
        raise PairingError(f"No eligible {role} audio track was found")
    ranked = sorted(choices, key=lambda item: (-item.score, item.source, item.track.id))
    if len(ranked) > 1 and ranked[0].score - ranked[1].score <= config.audio_selection_margin:
        if config.interactive:
            from .tui import select_audio_track

            return select_audio_track(role, ranked)
        summary = ", ".join(
            f"{item.label} {item.track.codec_id or item.track.codec} "
            f"{item.track.channels or '?'}ch score={item.score:.2f}"
            for item in ranked[:4]
        )
        option = "original-track" if role == "original" else "dub-track"
        raise AmbiguousPairError(
            f"Equivalent {role} tracks require a manual choice: {summary}. Use --interactive "
            f"or --{option} SOURCE:ID."
        )
    return ranked[0]


def _select_originals(
    candidate: PairCandidate, config: DualMakerConfig
) -> tuple[Track, Track, AudioTrackSelection]:
    requested = base_language(config.original_language) if config.original_language else None
    shared = list(candidate.shared_original_languages)
    if requested:
        if requested not in shared:
            raise PairingError(
                f"Requested original language {config.original_language!r} is not shared; "
                f"available: {', '.join(shared)}"
            )
        original_language = requested
    elif len(shared) == 1:
        original_language = shared[0]
    else:
        raise AmbiguousPairError(
            "More than one original language is shared; select one interactively or pass "
            "--original-language: " + ", ".join(shared)
        )

    explicit_normal = _by_ids(candidate.normal, config.normal_audio_ids, kind="audio")
    if len(explicit_normal) > 1:
        raise PairingError("Only one --normal-audio track may define the master reference")
    normal_options = explicit_normal or [
        track
        for track in candidate.normal.audio_tracks
        if base_language(track.effective_language) == original_language and not track.commentary
    ]
    dual_options = [
        track
        for track in candidate.dual.audio_tracks
        if base_language(track.effective_language) == original_language and not track.commentary
    ]
    if not normal_options or not dual_options:
        raise PairingError(f"Could not resolve both {original_language!r} comparison tracks")

    output_explicit: tuple[AudioSource, Track] | None = None
    if config.original_track_selector:
        output_explicit = _selected_track(candidate, config.original_track_selector)
        if base_language(output_explicit[1].effective_language) != original_language:
            raise PairingError("The explicit original track does not use the selected language")
    elif explicit_normal:
        output_explicit = ("master", explicit_normal[0])

    normal_ranked = [
        _score_track("master", track, candidate.normal, config, role="original")
        for track in normal_options
    ]
    dual_ranked = [
        _score_track("dual", track, candidate.dual, config, role="original")
        for track in dual_options
    ]
    normal_reference = max(normal_ranked, key=lambda item: item.score).track
    dual_reference = max(dual_ranked, key=lambda item: item.score).track
    if output_explicit:
        source, track = output_explicit
        output_original = _score_track(
            source,
            track,
            _asset(candidate, source),
            config,
            role="original",
            explicit=True,
        )
        if source == "master":
            normal_reference = track
        else:
            dual_reference = track
    else:
        output_original = _choose_best("original", normal_ranked + dual_ranked, config)
    return normal_reference, dual_reference, output_original


def _select_cross_language_references(
    candidate: PairCandidate,
    config: DualMakerConfig,
    dubs: list[AudioTrackSelection],
) -> tuple[Track, Track, AudioTrackSelection]:
    """Select a master original and a Portuguese event-anchor reference.

    The source-side Portuguese track is used solely to build the acoustic map;
    it is not relabelled as an original-language output track.  Milksync's
    short event windows then favor shared music, impacts, doors, and footsteps
    over translated dialogue.
    """

    requested = base_language(config.original_language) if config.original_language else None
    explicit_normal = _by_ids(candidate.normal, config.normal_audio_ids, kind="audio")
    if len(explicit_normal) > 1:
        raise PairingError("Only one --normal-audio track may define the master reference")
    if config.original_track_selector:
        source, selected = _selected_track(candidate, config.original_track_selector)
        if source != "master" or is_portuguese(selected.effective_language):
            raise PairingError(
                "Cross-language event synchronization requires a non-Portuguese "
                "master --original-track"
            )
        normal_options = [selected]
    elif explicit_normal:
        normal_options = explicit_normal
    else:
        normal_options = [
            track
            for track in candidate.normal.audio_tracks
            if not is_portuguese(track.effective_language) and not track.commentary
            and (requested is None or base_language(track.effective_language) == requested)
        ]
    if not normal_options:
        raise PairingError(
            "No non-Portuguese master audio is available for cross-language event synchronization"
        )
    languages = {base_language(track.effective_language) for track in normal_options}
    if requested and requested not in languages:
        raise PairingError(
            f"Requested original language {config.original_language!r} is not available "
            "on the master"
        )
    if len(languages) > 1 and not (explicit_normal or config.original_track_selector):
        raise AmbiguousPairError(
            "More than one master original language is available; pass --original-language "
            "or --original-track master:ID"
        )
    ranked = [
        _score_track("master", track, candidate.normal, config, role="original")
        for track in normal_options
    ]
    output_original = _choose_best("original", ranked, config)
    return output_original.track, dubs[0].track, output_original


def _select_dubs(candidate: PairCandidate, config: DualMakerConfig) -> list[AudioTrackSelection]:
    requested_language = base_language(config.dub_language)
    explicit: list[tuple[AudioSource, Track]] = []
    if config.dub_track_selectors:
        explicit = [_selected_track(candidate, item) for item in config.dub_track_selectors]
    elif config.dual_audio_ids:
        explicit = [
            ("dual", track)
            for track in _by_ids(candidate.dual, config.dual_audio_ids, kind="audio")
        ]

    if explicit:
        candidates = explicit
    else:
        candidates = [
            (source, track)
            for source in ("master", "dual")
            for track in _asset(candidate, source).audio_tracks
            if base_language(track.effective_language) == requested_language
            and not track.commentary
        ]
    invalid = [
        f"{source}:{track.id}"
        for source, track in candidates
        if not is_portuguese(track.effective_language)
    ]
    if invalid:
        raise PairingError(f"Explicit dub tracks are not Portuguese: {', '.join(invalid)}")
    choices = [
        _score_track(
            source,
            track,
            _asset(candidate, source),
            config,
            role="dub",
            explicit=bool(explicit),
        )
        for source, track in candidates
        if not track.commentary
    ]
    if not choices:
        raise PairingError("Neither source has non-commentary Portuguese program audio")
    primary = choices[0] if explicit else _choose_best("primary dub", choices, config)
    remaining = sorted(
        (item for item in choices if item is not primary),
        key=lambda item: (-item.score, item.source, item.track.id),
    )
    return [primary, *remaining]


def _default_output(candidate: PairCandidate, config: DualMakerConfig) -> Path:
    if config.output:
        return config.output.expanduser().resolve()
    output_dir = (
        config.output_dir.expanduser().resolve()
        if config.output_dir
        else candidate.normal.path.parent / config.output_dir_name
    )
    return output_dir / make_output_basename(candidate.normal.path, config.tag)


def create_job_plan(
    candidate: PairCandidate,
    config: DualMakerConfig,
    *,
    sidecar_subtitles: list[SidecarSubtitle] | None = None,
) -> JobPlan:
    if candidate.source_kind == "tvrip" and candidate.alignment_mode == "common-original":
        if config.tvrip_require_interactive_approval and not config.interactive:
            raise ExperimentalTVRipRequiredError(
                "TVRip policy requires interactive segment approval; rerun with --interactive"
            )
        if not config.interactive and not config.allow_tvrip_segment_sync:
            raise ExperimentalTVRipRequiredError(
                "An editorially different TVRip candidate requires segmented validation; "
                "pass --allow-tvrip-segment-sync or use --interactive"
            )
    dubs = _select_dubs(candidate, config)
    if candidate.alignment_mode == "cross-language-events":
        normal_original, dual_original, output_original = _select_cross_language_references(
            candidate, config, dubs
        )
    else:
        normal_original, dual_original, output_original = _select_originals(candidate, config)
    if not config.experimental_dub_resync and any(item.source == "dual" for item in dubs):
        raise PairingError(
            "Importing a dubbed track from the DUAL source onto the master video is "
            "disabled; enable experimental_dub_resync or pass "
            "--experimental-dub-resync"
        )
    fps = evaluate_fps_pair(candidate.normal, candidate.dual, config)
    if fps.required:
        if not fps.compatible:
            raise ExperimentalFPSRequiredError(fps.reason)
        if config.interactive:
            from .tui import confirm_experimental_fps

            fps.approved = confirm_experimental_fps(candidate, fps)
            if not fps.approved:
                raise ExperimentalFPSRequiredError(
                    "Experimental different-FPS synchronization was declined"
                )
            fps.reason = "Experimental different-FPS synchronization approved interactively"
        elif not fps.approved:
            raise ExperimentalFPSRequiredError(
                f"{fps.reason}; pass --allow-experimental-fps-sync to process this pair"
            )
    desired_output = _default_output(candidate, config)
    try:
        output = choose_conflict_path(desired_output, config.conflict)
    except FileExistsError as exc:
        raise OutputConflictError(str(exc)) from exc
    if output is None:
        raise OutputConflictError(f"Output exists and conflict policy is skip: {desired_output}")
    return JobPlan(
        normal=candidate.normal,
        dual=candidate.dual,
        identity=candidate.identity,
        output=output,
        normal_original=normal_original,
        dual_original=dual_original,
        dub_tracks=[item.track for item in dubs],
        normal_subtitles=list(candidate.normal.subtitle_tracks),
        dual_subtitles=list(candidate.dual.subtitle_tracks),
        sidecar_subtitles=list(sidecar_subtitles or []),
        score=candidate.score,
        reasons=list(candidate.reasons),
        dub_selections=dubs,
        output_original=output_original,
        fps=fps,
        # TVRip's segment validator compares a common original track.  A
        # Portuguese-only source instead uses the separate event-anchor path,
        # so it cannot safely enter that dialogue-dependent validator.
        source_kind=(
            candidate.source_kind
            if candidate.alignment_mode == "common-original"
            else "dual"
        ),
        alignment_mode=candidate.alignment_mode,
    )
