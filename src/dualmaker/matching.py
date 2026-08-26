from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from .defaults import DEFAULT_IGNORED_DIR_NAMES, DEFAULT_TAG
from .errors import PairingError
from .languages import base_language, is_portuguese, normalize_language
from .metadata import MediaInspector
from .models import MediaAsset, PairCandidate
from .naming import natural_sort_key, parse_identity

LOGGER = logging.getLogger("dualmaker")
TVRIP_MARKERS = ("tvrip", "hdtv", "pdtv", "sdtv", "broadcast")


def _identity_sort_key(identity_key: tuple[object, ...]) -> tuple[object, ...]:
    """Sort episode identities by numeric season/episode, not their display text."""

    kind = str(identity_key[0]) if identity_key else "unknown"
    title = str(identity_key[1]) if len(identity_key) > 1 else ""
    if kind == "episode":
        season = identity_key[2] if len(identity_key) > 2 else -1
        episodes = identity_key[3] if len(identity_key) > 3 else ()
        return (
            0,
            natural_sort_key(title),
            int(season) if isinstance(season, int) else -1,
            tuple(int(item) for item in episodes) if isinstance(episodes, tuple) else (),
        )
    if kind == "movie":
        year = identity_key[2] if len(identity_key) > 2 else -1
        return (1, natural_sort_key(title), int(year) if isinstance(year, int) else -1)
    return (2, natural_sort_key(title), tuple(str(item) for item in identity_key[2:]))


def source_kind(asset: MediaAsset) -> str:
    stem = asset.path.stem.casefold()
    return "tvrip" if any(marker in stem for marker in TVRIP_MARKERS) else "dual"


def _audio_languages(asset: MediaAsset) -> set[str]:
    return {
        base_language(track.effective_language)
        for track in asset.audio_tracks
        if normalize_language(track.effective_language) != "und" and not track.commentary
    }


def _has_portuguese(asset: MediaAsset) -> bool:
    return any(
        is_portuguese(track.effective_language) and not track.commentary
        for track in asset.audio_tracks
    )


def infer_untagged_avi_dub_language(asset: MediaAsset, language: str) -> bool:
    """Infer the configured dub language for a one-track legacy AVI.

    AVI does not carry dependable language tags.  Restrict inference to a
    single program audio stream with no language tag; multi-track files remain
    unmodified so an operator can identify the intended track explicitly.
    """

    if asset.path.suffix.lower() != ".avi":
        return False
    program_audio = [track for track in asset.audio_tracks if not track.commentary]
    if len(program_audio) != 1 or normalize_language(program_audio[0].effective_language) != "und":
        return False
    inferred = normalize_language(language)
    program_audio[0].language = inferred
    program_audio[0].language_ietf = inferred
    program_audio[0].properties["language_inferred_from_avi"] = True
    return True


def _looks_generated(
    path: Path,
    *,
    ignored_dir_names: tuple[str, ...],
    ignored_paths: tuple[Path, ...],
) -> bool:
    resolved = path.resolve()
    return (
        any(parent.name in ignored_dir_names for parent in path.parents)
        or any(resolved.is_relative_to(root.resolve()) for root in ignored_paths)
    )


def discover_mkvs(
    path: Path,
    recursive: bool = False,
    *,
    ignored_dir_names: tuple[str, ...] = DEFAULT_IGNORED_DIR_NAMES,
    ignored_paths: tuple[Path, ...] = (),
    tag: str = DEFAULT_TAG,
) -> list[Path]:
    """Return eligible Matroska and legacy AVI inputs without filename guessing.

    Generated files live in configured output/work directories, which are reliable
    boundaries.  A ``.DUAL-<tag>`` suffix is *not* a safe generated-file marker:
    release groups often use the same word as an operator's configured output tag
    (for example ``DUAL-RiPER`` with ``tag: RiPER``).

    ``tag`` remains accepted for backward-compatible callers.  It deliberately
    does not participate in discovery filtering.
    """
    del tag
    root = path.expanduser().resolve()
    if not root.is_dir():
        raise PairingError(f"Scan path is not a directory: {root}")
    iterator = root.rglob("*") if recursive else root.iterdir()
    return sorted(
        (
            item
            for item in iterator
            if item.is_file()
            and item.suffix.lower() in {".mkv", ".avi"}
            and not _looks_generated(
                item,
                ignored_dir_names=ignored_dir_names,
                ignored_paths=ignored_paths,
            )
        ),
        key=lambda item: natural_sort_key(item),
    )


def inspect_directory(
    path: Path,
    *,
    recursive: bool = False,
    inspector: MediaInspector | None = None,
) -> list[MediaAsset]:
    inspector = inspector or MediaInspector()
    assets = []
    for media_path in discover_mkvs(path, recursive=recursive):
        asset = inspector.inspect(media_path)
        asset.identity = parse_identity(media_path)
        assets.append(asset)
    return assets


def _roles(left: MediaAsset, right: MediaAsset) -> tuple[MediaAsset, MediaAsset] | None:
    left_pt = _has_portuguese(left)
    right_pt = _has_portuguese(right)
    if not left_pt and not right_pt:
        return None
    if left_pt and right_pt:
        left_marker = "dual" in left.path.stem.casefold()
        right_marker = "dual" in right.path.stem.casefold()
        if left_marker != right_marker:
            return (right, left) if left_marker else (left, right)
        left_languages = len(_audio_languages(left))
        right_languages = len(_audio_languages(right))
        if left_languages != right_languages:
            return (right, left) if left_languages > right_languages else (left, right)
        return None
    return (right, left) if left_pt else (left, right)


def _shared_originals(normal: MediaAsset, dual: MediaAsset) -> tuple[str, ...]:
    shared = _audio_languages(normal) & _audio_languages(dual)
    return tuple(sorted(lang for lang in shared if lang != "pt"))


def _alignment_mode(shared_originals: tuple[str, ...]) -> str:
    """Choose the acoustic evidence available for a candidate pair.

    A Portuguese-only legacy source has no dialogue language in common with the
    master.  It can still be aligned experimentally from preserved music and
    effects, but must never be described as a common-original comparison.
    """

    return "common-original" if shared_originals else "cross-language-events"


def _same_episode_slot(left: MediaAsset, right: MediaAsset) -> bool:
    """Whether explicitly supplied files identify the same numbered episode.

    Portuguese legacy releases commonly use a translated series title, so an
    explicit ``--dual``/``--normal`` pair cannot require the normalized title
    text to be identical.  Season/episode identity remains mandatory.
    """

    if left.identity is None or right.identity is None:
        return False
    return (
        left.identity.kind == "episode"
        and right.identity.kind == "episode"
        and left.identity.season == right.identity.season
        and left.identity.episodes == right.identity.episodes
    )


def score_pair(
    normal: MediaAsset,
    dual: MediaAsset,
    *,
    kind: str | None = None,
    allow_translated_episode_title: bool = False,
) -> tuple[float, tuple[str, ...], list[str]]:
    if normal.identity is None or dual.identity is None:
        raise PairingError("Assets must have parsed identities before scoring")
    translated_episode_title = (
        normal.identity.key != dual.identity.key
        and allow_translated_episode_title
        and _same_episode_slot(normal, dual)
    )
    if normal.identity.key != dual.identity.key and not translated_episode_title:
        return 0.0, (), ["content identity differs"]
    kind = kind or source_kind(dual)
    shared = _shared_originals(normal, dual)
    if kind == "tvrip" and not shared:
        return 0.0, (), [
            "Portuguese-only TVRip requires a shared original reference for segment validation"
        ]
    duration_delta = abs(normal.duration - dual.duration)
    duration_ratio = duration_delta / max(normal.duration, dual.duration, 1.0)
    maximum_duration_ratio = 0.50 if kind == "tvrip" else 0.20
    if duration_ratio > maximum_duration_ratio:
        return 0.0, shared, [f"duration differs by {duration_ratio:.1%}"]
    score = 1.0 - min(duration_ratio, maximum_duration_ratio) * (
        0.9 if kind == "tvrip" else 2.5
    )
    reasons = (
        [f"shared original language: {', '.join(shared)}"]
        if shared
        else [
            (
                "no shared original language; using experimental cross-language "
                "sound-event anchors"
            )
        ]
    )
    if translated_episode_title:
        reasons.insert(
            0,
            "explicit episode pair uses different series titles; matched by season/episode number",
        )
    if kind == "tvrip":
        reasons.append("TVRip/broadcast source marker; segmented analysis required")
    if "dual" in dual.path.stem.casefold():
        score += 0.025
        reasons.append("DUAL filename marker")
    reasons.append(f"duration delta: {duration_delta:.3f}s")
    return min(score, 1.0), shared, reasons


def collect_pair_candidates(
    assets: Iterable[MediaAsset],
) -> tuple[dict[tuple[object, ...], list[PairCandidate]], list[str]]:
    groups: dict[tuple[object, ...], list[MediaAsset]] = defaultdict(list)
    skipped: list[str] = []
    for asset in assets:
        if asset.identity is None:
            asset.identity = parse_identity(asset.path)
        if not asset.identity.title:
            skipped.append(f"{asset.path}: empty normalized title")
            continue
        groups[asset.identity.key].append(asset)

    all_candidates: dict[tuple[object, ...], list[PairCandidate]] = {}
    for identity_key, group in sorted(
        groups.items(), key=lambda item: _identity_sort_key(item[0])
    ):
        candidates: list[PairCandidate] = []
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                roles = _roles(left, right)
                if roles is None:
                    continue
                normal, dual = roles
                kind = source_kind(dual)
                score, shared, reasons = score_pair(normal, dual, kind=kind)
                if score <= 0:
                    continue
                candidates.append(
                    PairCandidate(
                        normal=normal,
                        dual=dual,
                        identity=normal.identity,  # type: ignore[arg-type]
                        score=score,
                        shared_original_languages=shared,
                        reasons=reasons,
                        source_kind=kind,  # type: ignore[arg-type]
                        alignment_mode=_alignment_mode(shared),  # type: ignore[arg-type]
                    )
                )
        candidates.sort(
            key=lambda candidate: (-candidate.score, natural_sort_key(candidate.dual.path))
        )
        if not candidates:
            if len(group) > 1:
                skipped.append(
                    f"{identity_key}: files found, but no unique Portuguese/original role pair"
                )
            continue
        all_candidates[identity_key] = candidates
    return all_candidates, skipped


def find_pair_candidates(assets: Iterable[MediaAsset]) -> tuple[list[PairCandidate], list[str]]:
    grouped_candidates, skipped = collect_pair_candidates(assets)
    selected: list[PairCandidate] = []
    for identity_key, candidates in grouped_candidates.items():
        if len(candidates) > 1 and candidates[0].score - candidates[1].score < 0.05:
            choices = ", ".join(
                f"{item.dual.path.name} + {item.normal.path.name} ({item.score:.3f})"
                for item in candidates[:4]
            )
            skipped.append(f"{identity_key}: ambiguous candidates: {choices}")
            continue
        selected.append(candidates[0])
    return selected, skipped


def require_explicit_pair(normal: MediaAsset, dual: MediaAsset) -> PairCandidate:
    normal.identity = normal.identity or parse_identity(normal.path)
    dual.identity = dual.identity or parse_identity(dual.path)
    roles = _roles(normal, dual)
    if roles is None or roles != (normal, dual):
        raise PairingError(
            "Explicit --normal must lack Portuguese program audio and --dual must contain it"
        )
    if normal.identity.key != dual.identity.key and not _same_episode_slot(normal, dual):
        raise PairingError(
            f"Explicit inputs have different identities: {normal.identity.key} != {dual.identity.key}"
        )
    kind = source_kind(dual)
    score, shared, reasons = score_pair(
        normal,
        dual,
        kind=kind,
        allow_translated_episode_title=True,
    )
    if score <= 0:
        raise PairingError("Explicit inputs did not pass identity/source matching")
    return PairCandidate(  # type: ignore[arg-type]
        normal,
        dual,
        normal.identity,
        score,
        shared,
        reasons,
        kind,
        _alignment_mode(shared),
    )


def require_explicit_tvrip_pair(master: MediaAsset, tvrip: MediaAsset) -> PairCandidate:
    """Build an explicitly role-assigned experimental TVRip candidate.

    The first beta deliberately requires a shared non-Portuguese reference track:
    Milksync can then find precise audio cut points while video anchors independently
    validate every resulting segment. A Portuguese-only broadcast cannot be proven
    acoustically against an English master and is rejected rather than guessed.
    """

    master.identity = master.identity or parse_identity(master.path)
    tvrip.identity = tvrip.identity or parse_identity(tvrip.path)
    if master.identity.key != tvrip.identity.key:
        raise PairingError(
            f"Explicit inputs have different identities: {master.identity.key} != "
            f"{tvrip.identity.key}"
        )
    if not _has_portuguese(tvrip):
        raise PairingError("Explicit --tvrip source contains no Portuguese program audio")
    score, shared, reasons = score_pair(master, tvrip, kind="tvrip")
    if not shared:
        raise PairingError(
            "Experimental TVRip sync currently requires a tagged non-Portuguese reference "
            "language in both sources; a Portuguese-only TVRip cannot be synchronized safely"
        )
    if score <= 0:
        raise PairingError("Explicit TVRip and master did not pass identity/source matching")
    return PairCandidate(
        master,
        tvrip,
        master.identity,
        score,
        shared,
        reasons,
        "tvrip",
    )
