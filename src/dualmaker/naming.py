from __future__ import annotations

import re
from pathlib import Path

from .defaults import DEFAULT_TAG
from .models import ContentIdentity

_EPISODE_RE = re.compile(
    r"(?i)^(?P<title>.*?)[. _-]+S(?P<season>\d{1,2})(?P<episodes>E\d{1,3}(?:[. _-]*E\d{1,3})*)"
)
_YEAR_RE = re.compile(r"(?<!\d)(?P<year>19\d{2}|20\d{2}|21\d{2})(?!\d)")
_SERIES_YEAR_SUFFIX_RE = re.compile(
    r"(?i)[. _-]*[\[(](?:19\d{2}|20\d{2}|21\d{2})[\])][. _-]*$"
)
_GROUP_RE = re.compile(r"-(?P<group>[A-Za-z0-9][A-Za-z0-9._]{1,31})$")
_NATURAL_COMPONENT_RE = re.compile(r"(\d+)")
_TECHNICAL_TOKENS = {
    "2160p",
    "1080p",
    "1080i",
    "720p",
    "480p",
    "web",
    "webdl",
    "webrip",
    "bluray",
    "bdrip",
    "hdtv",
    "remux",
    "amzn",
    "dsnp",
    "hmax",
    "ma",
    "nf",
    "atvp",
    "dual",
    "multi",
    "hdr",
    "dovi",
    "dv",
    "hevc",
    "x264",
    "x265",
    "h264",
    "h265",
    "av1",
    "aac",
    "atmos",
    "ddp",
    "dd",
    "dts",
    "truehd",
}


def natural_sort_key(value: str | Path) -> tuple[tuple[int, int | str], ...]:
    """Return a deterministic human/natural sort key for paths and labels.

    Filesystem and plain string ordering put ``E10`` before ``E2``.  Release
    batches are easier to inspect and process in episode order when numeric
    runs compare as numbers instead.  Tagging each component keeps comparisons
    type-safe when one label has a number where another has text.
    """

    return tuple(
        (0, int(component)) if component.isdecimal() else (1, component.casefold())
        for component in _NATURAL_COMPONENT_RE.split(str(value))
        if component
    )


def _normalize_title(value: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    for index, token in enumerate(tokens):
        if (
            token in _TECHNICAL_TOKENS
            or re.fullmatch(r"(?:ddp?|aac|dts)\d*", token)
            or re.fullmatch(r"\d{3,4}p", token)
        ):
            tokens = tokens[:index]
            break
    return " ".join(tokens)


def _normalize_series_title(value: str) -> str:
    """Normalize a series name without treating a disambiguating year as title text.

    Library-style episode names commonly use ``Show Name (2009) - S03E01``
    while scene-style releases use ``show.name.s03e01``.  The year is useful
    to a person but should not stop an otherwise exact season/episode match.
    Only a parenthesized/bracketed suffix is removed so series genuinely named
    after a year (for example ``1923``) remain matchable.
    """
    return _normalize_title(_SERIES_YEAR_SUFFIX_RE.sub("", value))


def strip_release_group(stem: str) -> str:
    match = _GROUP_RE.search(stem)
    if not match:
        return stem
    # Avoid treating a natural hyphenated title as a release group. Release
    # names with a group normally contain at least one technical dot token.
    prefix = stem[: match.start()]
    if "." not in prefix and not re.search(r"(?i)S\d{1,2}E\d{1,3}", prefix):
        return stem
    return prefix


def parse_identity(path: str | Path) -> ContentIdentity:
    stem = strip_release_group(Path(path).stem)
    episode = _EPISODE_RE.search(stem)
    if episode:
        episodes = tuple(int(value) for value in re.findall(r"(?i)E(\d{1,3})", episode["episodes"]))
        return ContentIdentity(
            kind="episode",
            title=_normalize_series_title(episode["title"]),
            season=int(episode["season"]),
            episodes=episodes,
        )

    year_match = _YEAR_RE.search(stem)
    if year_match:
        return ContentIdentity(
            kind="movie",
            title=_normalize_title(stem[: year_match.start()]),
            year=int(year_match["year"]),
        )

    title = _normalize_title(stem)
    return ContentIdentity(kind="unknown", title=title)


def make_output_basename(normal: str | Path, tag: str = DEFAULT_TAG) -> str:
    safe_tag = re.sub(r"[^A-Za-z0-9._-]+", "", tag).strip(".-")
    if not safe_tag:
        raise ValueError("Output tag must contain at least one letter or number")
    stem = strip_release_group(Path(normal).stem)
    return f"{stem}.DUAL-{safe_tag}.mkv"


def choose_conflict_path(path: Path, policy: str = "increment") -> Path | None:
    if not path.exists():
        return path
    if policy == "skip":
        return None
    if policy == "error":
        raise FileExistsError(str(path))
    if policy != "increment":
        raise ValueError(f"Unknown conflict policy: {policy}")
    for number in range(2, 100_000):
        candidate = path.with_name(f"{path.stem}.{number}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not allocate a free output name near {path}")
