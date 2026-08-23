from __future__ import annotations

import logging
import re
from fractions import Fraction
from pathlib import Path
from typing import Any

from .errors import MetadataError
from .languages import normalize_language
from .models import Attachment, FrameRate, MediaAsset, Track
from .runner import ToolRunner

LOGGER = logging.getLogger("dualmaker")
COMMENTARY_RE = re.compile(
    r"\b(commentary|coment[aá]rio|director(?:'s)? commentary|cast commentary)\b", re.IGNORECASE
)


def _number(value: Any, kind: type[int | float]) -> int | float | None:
    if value is None or value == "":
        return None
    try:
        return kind(float(str(value).replace(" ", "")))
    except (TypeError, ValueError):
        return None


def _duration_seconds(value: Any) -> float | None:
    if isinstance(value, str) and ":" in value:
        raw = value.strip()
        parts = raw.split(":")
        if len(parts) == 3:
            try:
                hours, minutes, seconds = (float(part) for part in parts)
            except ValueError:
                pass
            else:
                return hours * 3600 + minutes * 60 + seconds
    number = _number(value, float)
    if number is None:
        return None
    # MediaInfo JSON reports modern Duration values in seconds, but older
    # builds may return milliseconds. Values over one week are necessarily ms.
    return float(number / 1000 if number > 604_800 else number)


def _kind(value: str) -> str:
    lowered = value.lower()
    if lowered in {"video", "audio", "subtitles", "buttons"}:
        return lowered
    return "unknown"


def _mediainfo_tracks(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for track in data.get("media", {}).get("track", []):
        kind = str(track.get("@type", "Unknown")).lower()
        result.setdefault(kind, []).append(track)
    return result


def _frame_rate(ffprobe: dict[str, Any]) -> FrameRate | None:
    video = next(
        (stream for stream in ffprobe.get("streams", []) if stream.get("codec_type") == "video"),
        None,
    )
    if video is None:
        return None
    for field in ("avg_frame_rate", "r_frame_rate"):
        raw = str(video.get(field) or "")
        if not raw or raw in {"0/0", "N/A"}:
            continue
        try:
            rate = Fraction(raw)
        except (ValueError, ZeroDivisionError):
            continue
        if rate > 0:
            return FrameRate(rate.numerator, rate.denominator, field)
    return None


class MediaInspector:
    """Read complete metadata while exposing stable, typed track information."""

    def __init__(self, runner: ToolRunner | None = None) -> None:
        self.runner = runner or ToolRunner()

    def inspect(self, path: str | Path) -> MediaAsset:
        media_path = Path(path).expanduser().resolve()
        if not media_path.is_file():
            raise MetadataError(f"Media file does not exist: {media_path}")
        if media_path.suffix.lower() != ".mkv":
            raise MetadataError(f"Only MKV input is supported: {media_path}")

        mediainfo = self.runner.json(("mediainfo", "--Output=JSON", media_path))
        mkvmerge = self.runner.json(("mkvmerge", "-J", media_path))
        ffprobe = self.runner.json(
            (
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-show_chapters",
                "-of",
                "json",
                media_path,
            )
        )
        if mkvmerge.get("container", {}).get("recognized") is False:
            raise MetadataError(f"mkvmerge does not recognize {media_path}")

        mi_by_kind = _mediainfo_tracks(mediainfo)
        ff_by_kind: dict[str, list[dict[str, Any]]] = {}
        for stream in ffprobe.get("streams", []):
            ff_kind = _kind(str(stream.get("codec_type", "unknown")))
            ff_by_kind.setdefault(ff_kind, []).append(stream)
        indexes: dict[str, int] = {}
        tracks: list[Track] = []
        for raw_track in mkvmerge.get("tracks", []):
            kind = _kind(str(raw_track.get("type", "unknown")))
            type_index = indexes.get(kind, 0)
            indexes[kind] = type_index + 1
            properties = dict(raw_track.get("properties") or {})
            mi_kind = "text" if kind == "subtitles" else kind
            mi_track_list = mi_by_kind.get(mi_kind, [])
            mi_track = mi_track_list[type_index] if type_index < len(mi_track_list) else {}
            ff_track_list = ff_by_kind.get(kind, [])
            ff_track = ff_track_list[type_index] if type_index < len(ff_track_list) else {}
            title = str(properties.get("track_name") or mi_track.get("Title") or "")
            language = normalize_language(
                properties.get("language_ietf")
                or properties.get("language")
                or mi_track.get("Language")
            )
            tracks.append(
                Track(
                    id=int(raw_track["id"]),
                    kind=kind,  # type: ignore[arg-type]
                    type_index=type_index,
                    codec=str(
                        mi_track.get("Format_Commercial_IfAny")
                        or mi_track.get("Format_Profile")
                        or raw_track.get("codec")
                        or mi_track.get("Format")
                        or ""
                    ),
                    codec_id=str(properties.get("codec_id") or mi_track.get("CodecID") or ""),
                    language=normalize_language(properties.get("language")),
                    language_ietf=language,
                    title=title,
                    default=bool(properties.get("default_track", False)),
                    forced=bool(properties.get("forced_track", False)),
                    hearing_impaired=bool(properties.get("flag_hearing_impaired", False)),
                    commentary=bool(properties.get("flag_commentary", False))
                    or bool(COMMENTARY_RE.search(title)),
                    channels=_number(
                        properties.get("audio_channels") or mi_track.get("Channels"), int
                    ),
                    bitrate=_number(mi_track.get("BitRate") or ff_track.get("bit_rate"), int),
                    sample_rate=_number(
                        properties.get("audio_sampling_frequency")
                        or mi_track.get("SamplingRate")
                        or ff_track.get("sample_rate"),
                        int,
                    ),
                    duration=_duration_seconds(
                        ff_track.get("duration")
                        or (ff_track.get("tags") or {}).get("DURATION")
                        or mi_track.get("Duration")
                    ),
                    properties=properties,
                )
            )

        attachments = [
            Attachment(
                id=int(item["id"]),
                name=str(item.get("file_name") or f"attachment-{item['id']}"),
                content_type=str(item.get("content_type") or "application/octet-stream"),
                size=_number(item.get("size"), int),
                description=str(item.get("description") or ""),
            )
            for item in mkvmerge.get("attachments", [])
        ]
        # Matroska's container duration can be extended by a stray subtitle
        # packet long after video/audio ended.  Release matching and timeline
        # analysis must describe the primary video, not that container tail.
        primary_video = next(
            (
                track.duration
                for track in tracks
                if track.kind == "video" and track.duration is not None and track.duration > 0
            ),
            None,
        )
        duration = primary_video or _duration_seconds(ffprobe.get("format", {}).get("duration"))
        if duration is None:
            nanoseconds = mkvmerge.get("container", {}).get("properties", {}).get("duration")
            duration = float(nanoseconds) / 1_000_000_000 if nanoseconds else None
        if duration is None:
            general = mi_by_kind.get("general", [{}])[0]
            duration = _duration_seconds(general.get("Duration"))
        if duration is None or duration <= 0:
            raise MetadataError(f"Could not determine a positive duration for {media_path}")

        return MediaAsset(
            path=media_path,
            duration=float(duration),
            tracks=tracks,
            attachments=attachments,
            chapters=list(ffprobe.get("chapters") or []),
            mediainfo=mediainfo,
            mkvmerge=mkvmerge,
            ffprobe=ffprobe,
            frame_rate=_frame_rate(ffprobe),
        )


def last_packet_end(
    path: Path,
    kind: str,
    type_index: int,
    runner: ToolRunner | None = None,
) -> float | None:
    """Return end PTS of the final packet in a stream without decoding it."""

    runner = runner or ToolRunner()
    stream_data = runner.json(
        (
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            f"{kind[0]}:{type_index}",
            "-show_entries",
            "stream=duration:stream_tags=DURATION",
            "-of",
            "json",
            path,
        )
    )
    stream = next(iter(stream_data.get("streams") or []), {})
    stream_duration = _duration_seconds(stream.get("duration")) or _duration_seconds(
        (stream.get("tags") or {}).get("DURATION")
    )
    if stream_duration is None:
        format_data = runner.json(
            (
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                path,
            )
        )
        stream_duration = _duration_seconds(format_data.get("format", {}).get("duration"))
    interval_start = max(float(stream_duration or 0) - 30.0, 0.0)
    result = runner.json(
        (
            "ffprobe",
            "-v",
            "error",
            "-read_intervals",
            f"{interval_start}%",
            "-select_streams",
            f"{kind[0]}:{type_index}",
            "-show_entries",
            "packet=pts_time,duration_time",
            "-of",
            "json",
            path,
        )
    )
    packets = result.get("packets") or []
    if not packets:
        return None
    ends = []
    for packet in packets:
        pts = _number(packet.get("pts_time"), float)
        duration = _number(packet.get("duration_time"), float) or 0.0
        if pts is not None:
            ends.append(float(pts + duration))
    return max(ends) if ends else None


def first_packet_pts(
    path: Path,
    kind: str,
    type_index: int,
    runner: ToolRunner | None = None,
) -> float | None:
    """Return the first packet PTS without decoding or scanning the whole stream."""

    runner = runner or ToolRunner()
    result = runner.json(
        (
            "ffprobe",
            "-v",
            "error",
            "-read_intervals",
            "%+#1",
            "-select_streams",
            f"{kind[0]}:{type_index}",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "json",
            path,
        )
    )
    packets = result.get("packets") or []
    if not packets:
        return None
    value = _number(packets[0].get("pts_time"), float)
    return float(value) if value is not None else None
