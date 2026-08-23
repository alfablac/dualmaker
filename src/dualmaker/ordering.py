from __future__ import annotations

import re

from .languages import base_language, is_english, is_portuguese, normalize_language
from .models import Track

SDH_RE = re.compile(r"\b(SDH|CC|hearing[ ._-]*impaired|closed[ ._-]*captions?)\b", re.IGNORECASE)


def subtitle_sort_key(track: Track) -> tuple[object, ...]:
    language = normalize_language(track.effective_language)
    if is_portuguese(language):
        language_group = 0 if track.forced else 1
        accessibility = 1 if (track.hearing_impaired or SDH_RE.search(track.title)) else 0
        return language_group, accessibility, track.type_index, track.id
    if is_english(language):
        return 2, 0 if track.forced else 1, track.type_index, track.id
    return 3, base_language(language), 0 if track.forced else 1, track.type_index, track.id


def subtitle_presentation_key(track: Track) -> tuple[str, bool, bool]:
    """Return the playback-facing subtitle slot represented by ``track``.

    Release sources regularly carry the same subtitle presentation with tiny
    timestamp, whitespace, RTL-mark, or styling differences.  Those are not
    useful parallel tracks in a final release.  A distinct language, forced
    track, or SDH/CC accessibility variant is a distinct slot and is retained.
    """
    accessibility = track.hearing_impaired or bool(SDH_RE.search(track.title))
    return normalize_language(track.effective_language), track.forced, accessibility


def order_subtitles(tracks: list[Track]) -> list[Track]:
    return sorted(tracks, key=subtitle_sort_key)


def preferred_portuguese_forced(tracks: list[Track]) -> Track | None:
    forced = [track for track in tracks if is_portuguese(track.effective_language) and track.forced]
    if not forced:
        return None
    return max(
        forced,
        key=lambda track: (
            1 if normalize_language(track.effective_language) == "pt-BR" else 0,
            1 if track.default else 0,
            0 if track.hearing_impaired else 1,
            -track.type_index,
        ),
    )
