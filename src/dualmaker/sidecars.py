"""Discovery and explicit language resolution for external subtitle sidecars."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from .defaults import SIDECAR_SUBTITLE_EXTENSIONS
from .errors import ConfigurationError
from .languages import normalize_language
from .models import PairCandidate, SidecarSubtitle, SidecarSubtitleCandidate
from .naming import natural_sort_key, parse_identity

_LANGUAGE_TAG_RE = re.compile(r"^(?:[A-Za-z]{2,3}|und)(?:[-_][A-Za-z0-9]{2,8})*$")
_PORTUGUESE_SIDECAR_RE = re.compile(
    r"(?:^|[. _-])(?:pt(?:[-_][a-z]{2})?|pob|por(?:tuguese)?)(?:$|[. _-])",
    re.IGNORECASE,
)


def _sidecar_source(source: str, path: Path) -> str:
    """Treat an explicitly Portuguese sidecar as a DUAL-side subtitle.

    Users commonly name a translated sidecar after the master release, e.g.
    ``master.pt-BR.srt``.  Its filename prefix must not make it inherit the
    master's timeline.
    """
    if source == "master" and _PORTUGUESE_SIDECAR_RE.search(path.stem):
        return "dual"
    return source


def discover_pair_sidecars(candidate: PairCandidate) -> list[SidecarSubtitleCandidate]:
    """Find supported sidecars named for either source file in a pair.

    Exact basename matches are preferred, with ``<video-name>.<label>.srt``
    style sidecars accepted as well.  This deliberately does not scrape every
    subtitle in the directory: an external subtitle must be visibly attached
    to one of the selected release files.
    """
    found: dict[Path, SidecarSubtitleCandidate] = {}
    for source, video_path in (("dual", candidate.dual.path), ("master", candidate.normal.path)):
        try:
            siblings = video_path.parent.iterdir()
        except OSError as exc:
            raise ConfigurationError(
                f"Cannot inspect sidecars next to {video_path}: {exc}"
            ) from exc
        stem = video_path.stem.casefold()
        for sibling in siblings:
            if not sibling.is_file() or sibling.suffix.casefold() not in SIDECAR_SUBTITLE_EXTENSIONS:
                continue
            sidecar_stem = sibling.stem.casefold()
            if sidecar_stem == stem or sidecar_stem.startswith(f"{stem}."):
                found.setdefault(
                    sibling.resolve(),
                    SidecarSubtitleCandidate(
                        sibling.resolve(),
                        _sidecar_source(source, sibling),  # type: ignore[arg-type]
                    ),
                )
    # When a subtitle is not named after the release, accept exactly one
    # text subtitle whose parsed identity is the selected pair's identity.
    # This keeps a directory containing subtitles for neighboring episodes
    # safe: their SxxExx key will not match this candidate.
    if not found:
        pair_key = candidate.identity.key
        directories = {candidate.dual.path.parent, candidate.normal.path.parent}
        fallback: list[Path] = []
        unlabelled: list[Path] = []
        for directory in directories:
            try:
                siblings = directory.iterdir()
            except OSError as exc:
                raise ConfigurationError(
                    f"Cannot inspect sidecars next to {directory}: {exc}"
                ) from exc
            for sibling in siblings:
                if sibling.is_file() and sibling.suffix.casefold() in {".srt", ".ass"}:
                    identity = parse_identity(sibling)
                    if identity.key == pair_key:
                        fallback.append(sibling.resolve())
                    elif identity.kind == "unknown":
                        unlabelled.append(sibling.resolve())
        fallback = list(dict.fromkeys(fallback))
        if not fallback and len(set(unlabelled)) == 1:
            fallback = [unlabelled[0]]
        if len(fallback) == 1:
            path = fallback[0]
            found[path] = SidecarSubtitleCandidate(path, "dual")

    source_order = {"dual": 0, "master": 1}
    return sorted(
        found.values(),
        key=lambda item: (source_order[item.source], natural_sort_key(item.path)),
    )


def normalize_sidecar_language(value: str, *, label: str) -> str:
    raw = value.strip()
    if not _LANGUAGE_TAG_RE.fullmatch(raw):
        raise ConfigurationError(
            f"{label} must use an ISO-639/BCP-47 language tag such as pt-BR, en, or es-419; "
            f"received {value!r}"
        )
    return normalize_language(raw)


def sidecar_languages_from_overrides(
    sidecars: Iterable[SidecarSubtitleCandidate], overrides: Iterable[str]
) -> dict[Path, str]:
    """Resolve repeatable ``PATH=LANGUAGE`` CLI/config values without guessing."""
    candidates = list(sidecars)
    selected: dict[Path, str] = {}
    for raw in overrides:
        selector, separator, language = raw.rpartition("=")
        if not separator or not selector.strip() or not language.strip():
            raise ConfigurationError(
                "--sidecar-language must use PATH=LANGUAGE, for example "
                "--sidecar-language 'episode.DUAL.srt=pt-BR'"
            )
        selector = selector.strip()
        matching = [
            item
            for item in candidates
            if selector == item.path.name or selector == str(item.path)
        ]
        if not matching:
            raise ConfigurationError(f"No discovered sidecar matches {selector!r}")
        if len(matching) > 1:
            paths = ", ".join(str(item.path) for item in matching)
            raise ConfigurationError(
                f"Sidecar selector {selector!r} is ambiguous; use an absolute path: {paths}"
            )
        path = matching[0].path
        if path in selected:
            raise ConfigurationError(f"Language was specified more than once for sidecar {path}")
        selected[path] = normalize_sidecar_language(language, label=f"language for {path.name}")
    return selected


def resolve_sidecar_subtitles(
    sidecars: Iterable[SidecarSubtitleCandidate],
    languages: Mapping[Path, str],
    *,
    default_dual_language: str | None = None,
) -> list[SidecarSubtitle]:
    """Resolve languages, defaulting only DUAL-attached sidecars when configured."""
    resolved: list[SidecarSubtitle] = []
    for sidecar in sidecars:
        language = languages.get(sidecar.path)
        if language is None and sidecar.source == "dual" and default_dual_language:
            language = normalize_sidecar_language(
                default_dual_language,
                label="sidecar_dual_language",
            )
        if language is None:
            raise ConfigurationError(
                f"External subtitle language is required for {sidecar.path}. Run with --interactive "
                "to choose it, or pass --sidecar-language 'PATH=LANGUAGE'."
            )
        resolved.append(SidecarSubtitle(sidecar.path, sidecar.source, language))
    return resolved
