from __future__ import annotations

import hashlib
import logging
import os
import struct
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .errors import OutputConflictError, ProcessingError
from .languages import normalize_language
from .metadata import MediaInspector, first_packet_pts, last_packet_end
from .models import Attachment, DualMakerConfig, JobPlan, MediaAsset, Track
from .naming import choose_conflict_path
from .ordering import preferred_portuguese_forced, subtitle_presentation_key, subtitle_sort_key
from .runner import ToolRunner
from .sync.adapter import SyncResult

try:  # ``grp`` is POSIX-only and is not present in the Windows stdlib.
    import grp
except ImportError:  # pragma: no cover - exercised on Windows
    grp = None  # type: ignore[assignment]

LOGGER = logging.getLogger("dualmaker")


@dataclass(slots=True)
class TrackRef:
    input_index: int
    path: Path
    actual: Track
    metadata: Track
    source: str
    sync_ms: int | None = None
    sync_factor: float | None = None


def _track_at(asset: MediaAsset, kind: str, type_index: int) -> Track:
    for track in asset.tracks:
        if track.kind == kind and track.type_index == type_index:
            return track
    raise ProcessingError(f"Missing {kind}:{type_index} in staged file {asset.path}")


def _subtitle_signature(reference: TrackRef, destination: Path) -> str:
    content = hashlib.sha256(destination.read_bytes()).hexdigest()
    signature = "\0".join(
        (
            content,
            normalize_language(reference.metadata.effective_language),
            reference.metadata.title.casefold(),
            str(reference.metadata.forced),
            str(reference.metadata.hearing_impaired),
        )
    )
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def _bitmap_packet_delay_ms(
    packet_timestamps: list[float],
    sync_buckets: list[tuple[float, float, float]],
    delete_buckets: list[tuple[float, float]],
    *,
    source_time_scale: float = 1.0,
    source_cutoff: float | None = None,
) -> tuple[int | None, str | None]:
    """Find one safe mux delay for a bitmap subtitle's active packet range.

    PGS and VobSub are packet-timestamped bitmap streams.  ``mkvmerge --sync``
    can stream-copy them with one offset, but cannot apply Milksync's arbitrary
    mid-file edit map.  The relevant question is therefore not whether the
    *movie* has edits, but whether this subtitle's packets cross one.  A PGS
    track that ends before a late alternate-cut edit can still be carried with
    its correct constant delay.

    Returns a millisecond delay, or a reason why a single stream-copy delay is
    unsafe.  Rounding to milliseconds matches mkvmerge's ``--sync`` output.
    """
    if not packet_timestamps:
        return None, "no readable packet timestamps"

    delay_ms: set[int] = set()
    for physical_timestamp in packet_timestamps:
        if source_cutoff is not None and physical_timestamp >= source_cutoff:
            # --stop-after-video-ends discards these packets. They must not make
            # an otherwise usable bitmap track fail merely because its metadata
            # or final display-clear packet extends past the source video.
            continue
        timestamp = physical_timestamp / source_time_scale
        if any(start <= timestamp < end for start, end in delete_buckets):
            return None, f"a packet falls in deleted source content at {timestamp:.3f}s"
        for start, end, delta in sync_buckets:
            if start <= timestamp < end:
                delay_ms.add(round(delta * 1000))
                break
        else:
            return None, f"no synchronization bucket covers packet {timestamp:.3f}s"

    if len(delay_ms) != 1:
        if not delay_ms:
            return None, "no packets remain before the source video endpoint"
        values = ", ".join(f"{value:+d} ms" for value in sorted(delay_ms))
        return None, f"packets require multiple delays ({values})"
    return delay_ms.pop(), None


def _map_bitmap_timestamp(
    timestamp: float,
    *,
    source_time_scale: float,
    sync_buckets: list[tuple[float, float, float]],
    delete_buckets: list[tuple[float, float]],
    timeline_adjustment: float,
) -> tuple[float | None, str | None]:
    normalized = timestamp / source_time_scale
    if any(start <= normalized < end for start, end in delete_buckets):
        return None, "deleted"
    for start, end, delta in sync_buckets:
        if start <= normalized < end:
            return normalized + delta + timeline_adjustment, None
    return None, f"no synchronization bucket covers {normalized:.3f}s"


def _retime_pgs_bytes(
    payload: bytes,
    *,
    source_time_scale: float,
    sync_buckets: list[tuple[float, float, float]],
    delete_buckets: list[tuple[float, float]],
    timeline_adjustment_ms: int,
    source_cutoff: float | None,
) -> tuple[bytes, dict[str, int]]:
    """Rewrite Blu-ray SUP display-set timestamps through a Milksync map.

    PGS uses a simple sequence of ``PG`` records with 90 kHz PTS/DTS fields.
    Rewriting those fields preserves the original bitmap payload and therefore
    its exact rendering while supporting both a standards-conversion stretch
    and different offsets after broadcast edits.
    """

    records: list[tuple[int, int, int, bytes]] = []
    cursor = 0
    while cursor < len(payload):
        if cursor + 13 > len(payload) or payload[cursor : cursor + 2] != b"PG":
            raise ProcessingError(f"Invalid PGS/SUP record at byte {cursor}")
        pts, dts, segment_type, size = struct.unpack_from(">IIBH", payload, cursor + 2)
        end = cursor + 13 + size
        if end > len(payload):
            raise ProcessingError(f"Truncated PGS/SUP record at byte {cursor}")
        records.append((pts, dts, segment_type, payload[cursor + 13 : end]))
        cursor = end

    output = bytearray()
    kept_sets = 0
    dropped_sets = 0
    index = 0
    timeline_adjustment = timeline_adjustment_ms / 1000
    while index < len(records):
        display_set: list[tuple[int, int, int, bytes]] = []
        while index < len(records):
            record = records[index]
            display_set.append(record)
            index += 1
            if record[2] == 0x80:  # END segment
                break
        representative = next((pts for pts, _, _, _ in display_set if pts), display_set[0][0])
        physical_time = representative / 90_000
        if source_cutoff is not None and physical_time >= source_cutoff:
            dropped_sets += 1
            continue
        mapped, issue = _map_bitmap_timestamp(
            physical_time,
            source_time_scale=source_time_scale,
            sync_buckets=sync_buckets,
            delete_buckets=delete_buckets,
            timeline_adjustment=timeline_adjustment,
        )
        if issue == "deleted":
            dropped_sets += 1
            continue
        if mapped is None:
            raise ProcessingError(f"PGS display set at {physical_time:.3f}s: {issue}")
        normalized_representative = physical_time / source_time_scale
        delta = mapped - normalized_representative
        for pts, dts, segment_type, data in display_set:
            normalized_pts = pts / 90_000 / source_time_scale
            mapped_pts = max(round((normalized_pts + delta) * 90_000), 0)
            if mapped_pts > 0xFFFFFFFF:
                raise ProcessingError("Retimed PGS timestamp exceeds its 32-bit clock")
            if dts:
                normalized_dts = dts / 90_000 / source_time_scale
                mapped_dts = max(round((normalized_dts + delta) * 90_000), 0)
            else:
                mapped_dts = 0
            output += b"PG"
            output += struct.pack(">IIBH", mapped_pts, mapped_dts, segment_type, len(data))
            output += data
        kept_sets += 1
    if not kept_sets:
        raise ProcessingError("PGS synchronization removed every display set")
    return bytes(output), {"kept_display_sets": kept_sets, "dropped_display_sets": dropped_sets}


def _stage_pgs_timeline(
    source: Path,
    track: Track,
    destination: Path,
    *,
    source_time_scale: float,
    sync_buckets: list[tuple[float, float, float]],
    delete_buckets: list[tuple[float, float]],
    timeline_adjustment_ms: int,
    source_cutoff: float | None,
    runner: ToolRunner,
) -> dict[str, int]:
    extracted = destination.with_suffix(".source.sup")
    result = runner.run(
        ("mkvextract", source, "tracks", f"{track.id}:{extracted}"),
        check=False,
    )
    if result.returncode not in (0, 1) or not extracted.is_file():
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise ProcessingError(f"Could not extract PGS track {track.id}: {detail[-2000:]}")
    try:
        retimed, statistics = _retime_pgs_bytes(
            extracted.read_bytes(),
            source_time_scale=source_time_scale,
            sync_buckets=sync_buckets,
            delete_buckets=delete_buckets,
            timeline_adjustment_ms=timeline_adjustment_ms,
            source_cutoff=source_cutoff,
        )
        destination.write_bytes(retimed)
    finally:
        extracted.unlink(missing_ok=True)
    return statistics


def _bitmap_packet_timestamps(
    source: Path,
    track: Track,
    runner: ToolRunner,
) -> tuple[list[float], str | None]:
    """Read per-packet PTS values for an embedded bitmap subtitle track."""
    try:
        payload = runner.json(
            (
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                f"s:{track.type_index}",
                "-show_entries",
                "packet=pts_time",
                "-of",
                "json",
                source,
            )
        )
        timestamps = [
            float(packet["pts_time"])
            for packet in payload.get("packets", [])
            if packet.get("pts_time") not in (None, "N/A")
        ]
    except (ProcessingError, TypeError, ValueError) as exc:
        return [], f"could not read packet timestamps: {exc}"
    return timestamps, None


def _has_master_subtitle_replacement(track: Track, master_subtitles: list[Track]) -> bool:
    """Whether master has the same playback-facing subtitle presentation."""
    language, forced, accessibility = subtitle_presentation_key(track)
    if language == "und":
        return False
    return any(
        subtitle_presentation_key(candidate) == (language, forced, accessibility)
        for candidate in master_subtitles
    )


def _dedupe_subtitles(
    references: list[TrackRef],
    work_dir: Path,
    runner: ToolRunner,
    *,
    policy: str,
) -> list[TrackRef]:
    if policy == "prefer-master":
        return _dedupe_subtitles_by_presentation(references)

    if policy != "exact-union":
        raise ProcessingError(f"Unknown subtitle policy: {policy}")

    # mkvextract scans the source for every invocation. Extract every selected
    # subtitle from a source in one pass rather than re-reading a large MKV for
    # each track.
    grouped: dict[Path, list[tuple[int, TrackRef, Path]]] = {}
    for index, reference in enumerate(references):
        if reference.source.endswith("-sidecar"):
            continue
        destination = work_dir / (
            f"subtitle-{reference.input_index}-{reference.actual.id}-{uuid.uuid4().hex}.bin"
        )
        grouped.setdefault(reference.path, []).append((index, reference, destination))

    digests: dict[int, str] = {}
    for source, items in grouped.items():
        result = runner.run(
            (
                "mkvextract",
                source,
                "tracks",
                *(f"{reference.actual.id}:{destination}" for _, reference, destination in items),
            ),
            check=False,
        )
        missing = [destination for _, _, destination in items if not destination.exists()]
        if result.returncode not in (0, 1) or missing:
            track_ids = ", ".join(str(reference.actual.id) for _, reference, _ in items)
            raise ProcessingError(f"Could not extract subtitle track(s) {track_ids} from {source}")
        for index, reference, destination in items:
            digests[index] = _subtitle_signature(reference, destination)
            destination.unlink()

    seen: set[str] = set()
    kept: list[TrackRef] = []
    # Normal references are deliberately first so an exact duplicate from the
    # DUAL source is discarded in their favor.
    for index, reference in enumerate(references):
        # Sidecars are explicitly selected by the user. Their presentation
        # policy was already applied above; byte extraction only works for MKV
        # tracks, so exact-union retains these external inputs as-is.
        if reference.source.endswith("-sidecar"):
            kept.append(reference)
            continue
        digest = digests[index]
        if digest in seen:
            LOGGER.info(
                "Dropping exact duplicate subtitle %s:%s",
                reference.path.name,
                reference.actual.id,
            )
            continue
        seen.add(digest)
        kept.append(reference)
    return kept


def _dedupe_subtitles_by_presentation(references: list[TrackRef]) -> list[TrackRef]:
    """Keep one useful subtitle presentation per language/variant slot.

    ``references`` is deliberately master-first.  That makes the master text
    the canonical release subtitle whenever both sources supply, for example,
    a regular English track.  The DUAL source still contributes absent forced,
    SDH/CC, regional-language, and other language slots.
    """
    seen: set[tuple[object, ...]] = set()
    kept: list[TrackRef] = []
    for reference in references:
        language, forced, accessibility = subtitle_presentation_key(reference.metadata)
        # Untagged subtitles cannot be classified safely.  Keep each one rather
        # than silently assuming that two unrelated tracks are interchangeable.
        slot: tuple[object, ...]
        if language == "und":
            slot = (language, forced, accessibility, reference.source, reference.actual.id)
        else:
            slot = (language, forced, accessibility)
        if slot in seen:
            LOGGER.info(
                "Dropping overlapping %s subtitle %s:%s; retaining master-preferred slot %s",
                reference.source,
                reference.path.name,
                reference.actual.id,
                "/".join(str(item) for item in slot[:3]),
            )
            continue
        seen.add(slot)
        kept.append(reference)
    return kept


def _attachment_files(
    sources: list[tuple[Path, list[Attachment]]], work_dir: Path, runner: ToolRunner
) -> list[tuple[Attachment, Path]]:
    seen: set[str] = set()
    extracted: list[tuple[Attachment, Path]] = []
    for source, attachments in sources:
        pending: list[tuple[Attachment, Path]] = []
        for attachment in attachments:
            safe_name = Path(attachment.name).name or f"attachment-{attachment.id}"
            destination = work_dir / f"attachment-{uuid.uuid4().hex}-{safe_name}"
            pending.append((attachment, destination))
        if not pending:
            continue
        result = runner.run(
            (
                "mkvextract",
                source,
                "attachments",
                *(f"{attachment.id}:{destination}" for attachment, destination in pending),
            ),
            check=False,
        )
        missing = [destination for _, destination in pending if not destination.exists()]
        if result.returncode not in (0, 1) or missing:
            attachment_ids = ", ".join(str(item.id) for item, _ in pending)
            raise ProcessingError(f"Could not extract attachment(s) {attachment_ids} from {source}")
        for attachment, destination in pending:
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            if digest in seen:
                destination.unlink()
                continue
            seen.add(digest)
            extracted.append((attachment, destination))
    return extracted


def _track_options(reference: TrackRef, *, default: bool) -> list[str]:
    track = reference.actual
    metadata = reference.metadata
    args = [
        "--language",
        f"{track.id}:{normalize_language(metadata.effective_language)}",
        "--default-track",
        f"{track.id}:{'yes' if default else 'no'}",
    ]
    if metadata.kind == "subtitles":
        args += [
            "--forced-display-flag",
            f"{track.id}:{'yes' if metadata.forced else 'no'}",
            "--hearing-impaired-flag",
            f"{track.id}:{'yes' if metadata.hearing_impaired else 'no'}",
        ]
    if metadata.title:
        args += ["--track-name", f"{track.id}:{metadata.title}"]
    if reference.sync_ms is not None:
        value = f"{track.id}:{reference.sync_ms}"
        if reference.sync_factor is not None:
            factor = Fraction(reference.sync_factor).limit_denominator(1_000_000)
            value += f",{factor.numerator}/{factor.denominator}"
        args += ["--sync", value]
    return args


def _timecode(seconds: float) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}"


def _validated_end_cut(
    master: MediaAsset,
    stage: MediaAsset,
    stage_audio: list[Track],
    timeline_adjustment_ms: int,
    tolerance_ms: int,
    runner: ToolRunner,
) -> float | None:
    video = master.video_tracks[0]
    video_packet_end = last_packet_end(master.path, "video", video.type_index, runner)
    if video_packet_end is None:
        return None
    ends: list[float] = []
    for track in stage_audio:
        packet_end = last_packet_end(stage.path, "audio", track.type_index, runner)
        if packet_end is None or track.duration is None:
            return None
        if abs(packet_end - track.duration) > 0.25:
            return None
        ends.append(packet_end + timeline_adjustment_ms / 1000)
    shortest = min(ends)
    if video_packet_end - shortest > tolerance_ms / 1000:
        return shortest
    return None


def _trim_end(
    source: Path, destination: Path, seconds: float, runner: ToolRunner
) -> tuple[Path | None, str | None]:
    """Attempt a packet-safe final cut without sacrificing a usable mux.

    ``mkvmerge --split`` cannot split every codec (notably the FLAC timeline
    used after experimental speed correction). End trimming is conservative
    housekeeping, whereas the already-written file has valid master video and
    synchronized audio. Keep that file and report the unsupported cut instead
    of failing the complete job.
    """

    result = runner.run(
        (
            "mkvmerge",
            "--no-date",
            "-o",
            destination,
            "--split",
            f"parts:00:00:00-{_timecode(seconds)}",
            source,
        ),
        check=False,
    )
    if result.returncode not in (0, 1):
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        destination.unlink(missing_ok=True)
        message = f"mkvmerge end trim was not supported (status {result.returncode}): {detail[-4000:]}"
        LOGGER.warning("Keeping the completed MKV because %s", message)
        return None, message
    if destination.exists():
        return destination, None
    generated = sorted(destination.parent.glob(f"{destination.stem}-*.mkv"))
    if len(generated) != 1:
        message = "End trimming did not create exactly one MKV"
        LOGGER.warning("Keeping the completed MKV because %s", message)
        return None, message
    generated[0].rename(destination)
    return destination, None


def _write_final_mkv(
    runner: ToolRunner,
    command: list[str | Path],
    partial: Path,
) -> None:
    """Run mkvmerge without mistaking its warning status for a failed mux.

    mkvmerge returns status 1 when it completed while emitting a warning, such
    as resolving a duplicate track UID.  A completed file in that case still
    goes through the full post-mux validation below.  Statuses other than 0/1
    and a warning status without a usable partial file remain fatal.
    """
    result = runner.run(command, check=False)
    if result.returncode == 0:
        return
    if result.returncode == 1 and partial.is_file() and partial.stat().st_size > 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        LOGGER.warning(
            "mkvmerge completed with warnings; validating the generated MKV: %s",
            detail[-4000:],
        )
        return
    detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
    raise ProcessingError(
        f"mkvmerge did not produce a usable output (status {result.returncode}): {detail[-4000:]}"
    )


def _publish_no_overwrite(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
        source.unlink()
        return
    except FileExistsError as exc:
        raise OutputConflictError(f"Output appeared during muxing: {destination}") from exc
    except OSError:
        # Some network/FUSE filesystems do not implement hard links. Reserve
        # the name exclusively, then replace only the placeholder we own.
        try:
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError as exc:
            raise OutputConflictError(f"Output appeared during muxing: {destination}") from exc
        os.close(descriptor)
        try:
            os.replace(source, destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise


def _set_output_group(path: Path, group_name: str | None) -> None:
    """Apply the requested group before the staged file is atomically published."""

    if not group_name:
        return
    if grp is None:
        raise ProcessingError(
            "output_group is only supported on operating systems with POSIX groups"
        )
    try:
        group_id = grp.getgrnam(group_name).gr_gid
    except KeyError as exc:
        raise ProcessingError(f"Output group does not exist: {group_name}") from exc
    try:
        os.chown(path, -1, group_id)
    except OSError as exc:
        raise ProcessingError(
            f"Could not set group {group_name!r} on staged output {path}: {exc}"
        ) from exc


def mux_output(
    plan: JobPlan,
    sync: SyncResult,
    *,
    normal_path: Path,
    dual_path: Path,
    work_dir: Path,
    config: DualMakerConfig,
    runner: ToolRunner | None = None,
    inspector: MediaInspector | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> tuple[Path, int, dict[str, object]]:
    runner = runner or ToolRunner()
    inspector = inspector or MediaInspector(runner)
    notify = on_phase or (lambda _message: None)
    notify("Inspecting synchronized tracks")
    master = inspector.inspect(normal_path)
    dual = inspector.inspect(dual_path)
    stage = inspector.inspect(sync.path)
    if not master.video_tracks:
        raise ProcessingError("Normal master contains no video")
    master_video = master.video_tracks[0]
    stage_dubs = [_track_at(stage, "audio", index) for index in range(len(plan.dub_tracks))]
    stage_original = _track_at(stage, "audio", sync.stage_original_index)

    normal_refs = [
        TrackRef(0, master.path, _track_at(master, "subtitles", track.type_index), track, "normal")
        for track in plan.normal_subtitles
    ]
    stage_refs = [
        TrackRef(
            1,
            stage.path,
            _track_at(stage, "subtitles", index),
            source,
            "dual-synced",
            sync.timeline_adjustment_ms or None,
        )
        for index, source in enumerate(sync.text_subtitles)
    ]
    binary_refs: list[TrackRef] = []
    bitmap_timing: list[dict[str, object]] = []
    source_time_scale = max(sync.speed_correction_factor, 0.000_001)
    source_cutoff = (
        dual.video_tracks[0].duration
        if dual.video_tracks and dual.video_tracks[0].duration is not None
        else dual.duration
    )
    for track in sync.binary_subtitles:
        if track.codec_id.casefold() == "s_hdmv/pgs":
            destination = work_dir / f"bitmap-{track.id}-synchronized.sup"
            try:
                statistics = _stage_pgs_timeline(
                    dual.path,
                    track,
                    destination,
                    source_time_scale=source_time_scale,
                    sync_buckets=sync.sync_buckets,
                    delete_buckets=sync.delete_buckets,
                    timeline_adjustment_ms=sync.timeline_adjustment_ms,
                    source_cutoff=source_cutoff,
                    runner=runner,
                )
            except ProcessingError as exc:
                issue = str(exc)
            else:
                binary_refs.append(
                    TrackRef(
                        -1,
                        destination,
                        Track(
                            0,
                            "subtitles",
                            0,
                            codec=track.codec,
                            codec_id=track.codec_id,
                            language=track.language,
                            language_ietf=track.language_ietf,
                            title=track.title,
                            default=track.default,
                            forced=track.forced,
                            hearing_impaired=track.hearing_impaired,
                        ),
                        track,
                        "dual-bitmap-sidecar",
                    )
                )
                bitmap_timing.append(
                    {
                        "track_id": track.id,
                        "codec": track.codec_id or track.codec,
                        "status": "included-retimed-pgs",
                        "source_time_scale": source_time_scale,
                        **statistics,
                    }
                )
                continue

            if (
                config.subtitle_policy == "prefer-master"
                and _has_master_subtitle_replacement(track, plan.normal_subtitles)
            ):
                LOGGER.warning(
                    "Omitting DUAL PGS subtitle track %s: %s; the master has the same "
                    "language/forced/accessibility subtitle slot",
                    track.id,
                    issue,
                )
                bitmap_timing.append(
                    {
                        "track_id": track.id,
                        "codec": track.codec_id or track.codec,
                        "status": "omitted-master-replacement",
                        "reason": issue,
                    }
                )
                continue
            raise ProcessingError(
                f"PGS subtitle track {track.id} could not be synchronized: {issue}"
            )

        timestamps, probe_issue = _bitmap_packet_timestamps(dual.path, track, runner)
        delay, map_issue = _bitmap_packet_delay_ms(
            timestamps,
            sync.sync_buckets,
            sync.delete_buckets,
            source_time_scale=source_time_scale,
            source_cutoff=source_cutoff,
        )
        issue = probe_issue or map_issue
        if issue is None and delay is not None:
            binary_refs.append(
                TrackRef(
                    2,
                    dual.path,
                    _track_at(dual, "subtitles", track.type_index),
                    track,
                    "dual-bitmap",
                    delay + sync.timeline_adjustment_ms,
                    1.0 / source_time_scale if abs(source_time_scale - 1.0) > 0.000_001 else None,
                )
            )
            bitmap_timing.append(
                {
                    "track_id": track.id,
                    "codec": track.codec_id or track.codec,
                    "status": "included",
                    "delay_ms": delay + sync.timeline_adjustment_ms,
                    "timestamp_multiplier": 1.0 / source_time_scale,
                    "packet_count": len(timestamps),
                }
            )
            continue

        if (
            config.subtitle_policy == "prefer-master"
            and _has_master_subtitle_replacement(track, plan.normal_subtitles)
        ):
            LOGGER.warning(
                "Omitting DUAL bitmap subtitle track %s (%s): %s; the master has the same "
                "language/forced/accessibility subtitle slot",
                track.id,
                track.codec_id or track.codec or "unknown codec",
                issue,
            )
            bitmap_timing.append(
                {
                    "track_id": track.id,
                    "codec": track.codec_id or track.codec,
                    "status": "omitted-master-replacement",
                    "reason": issue,
                }
            )
            continue

        raise ProcessingError(
            "Bitmap subtitle track "
            f"{track.id} ({track.codec_id or track.codec or 'unknown codec'}) cannot be "
            f"stream-copied through this Milksync map: {issue}. A PGS/VobSub track can be "
            "kept only when all of its packets need one delay. Use a matching master subtitle "
            "with --subtitle-policy prefer-master, or remove the bitmap track."
        )

    sidecar_refs = [
        TrackRef(
            -1,
            sidecar.path,
            Track(
                0,
                "subtitles",
                0,
                codec=sidecar.path.suffix.lstrip(".").upper(),
                language=sidecar.language,
                language_ietf=sidecar.language,
            ),
            Track(
                0,
                "subtitles",
                0,
                codec=sidecar.path.suffix.lstrip(".").upper(),
                language=sidecar.language,
                language_ietf=sidecar.language,
            ),
            f"{sidecar.source}-sidecar",
            sync.timeline_adjustment_ms if sidecar.source == "dual" else None,
        )
        for sidecar in sync.sidecar_subtitles
    ]

    notify("Deduplicating subtitles")
    subtitle_refs = _dedupe_subtitles(
        normal_refs
        + [ref for ref in sidecar_refs if ref.source == "master-sidecar"]
        + stage_refs
        + binary_refs
        + [ref for ref in sidecar_refs if ref.source == "dual-sidecar"],
        work_dir,
        runner,
        policy=config.subtitle_policy,
    )
    subtitle_refs.sort(key=lambda reference: subtitle_sort_key(reference.metadata))
    selected_binary_refs = [ref for ref in subtitle_refs if ref.input_index == 2]
    selected_sidecar_refs = [
        ref for ref in subtitle_refs if ref.source.endswith("-sidecar")
    ]
    next_sidecar_input = 3 if selected_binary_refs else 2
    for reference in selected_sidecar_refs:
        reference.input_index = next_sidecar_input
        next_sidecar_input += 1
    subtitle_selection = {
        "policy": config.subtitle_policy,
        "master_candidates": len(normal_refs),
        "dual_candidates": len(stage_refs) + len(binary_refs),
        "sidecar_candidates": len(sidecar_refs),
        "bitmap_timing": bitmap_timing,
        "selected": len(subtitle_refs),
        "master_selected": sum(reference.input_index == 0 for reference in subtitle_refs),
        "dual_selected": sum(reference.input_index != 0 for reference in subtitle_refs),
        "dual_omitted": len(stage_refs)
        + len(binary_refs)
        + len(sidecar_refs)
        - sum(reference.input_index != 0 for reference in subtitle_refs),
    }
    preferred_default = preferred_portuguese_forced([ref.metadata for ref in subtitle_refs])

    output = choose_conflict_path(plan.output, config.conflict)
    if output is None:
        raise OutputConflictError(f"Output already exists: {plan.output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.parent / f".{output.name}.partial-{uuid.uuid4().hex}.mkv"

    # Track order references the three fixed inputs: master, synchronized stage,
    # and (when needed) the trimmed DUAL source for bitmap subtitles.
    order = [f"0:{master_video.id}"]
    order.extend(f"1:{track.id}" for track in stage_dubs)
    order.append(f"1:{stage_original.id}")
    order.extend(f"{reference.input_index}:{reference.actual.id}" for reference in subtitle_refs)

    command: list[str | Path] = [
        "mkvmerge",
        "--no-date",
        "--stop-after-video-ends",
        "-o",
        partial,
        "--track-order",
        ",".join(order),
    ]

    # Input 0: master video, retained master subtitles, chapters, global tags,
    # and video metadata. Both audio roles come from milksync's intermediate so
    # this ordering pass cannot discard its synchronized timeline.
    command += [
        "--video-tracks",
        str(master_video.id),
        "--no-audio",
    ]
    master_sub_ids = [ref.actual.id for ref in subtitle_refs if ref.input_index == 0]
    command += (
        ["--subtitle-tracks", ",".join(map(str, master_sub_ids))]
        if master_sub_ids
        else ["--no-subtitles"]
    )
    command += ["--no-attachments"]
    for reference in [ref for ref in subtitle_refs if ref.input_index == 0]:
        command += _track_options(reference, default=reference.metadata is preferred_default)
    command.append(master.path)

    # Input 1: synchronized Portuguese dubs, the master original audio carried
    # through milksync, and synchronized text subtitles.
    stage_audio = [*stage_dubs, stage_original]
    command += [
        "--no-video",
        "--audio-tracks",
        ",".join(str(track.id) for track in stage_audio),
    ]
    stage_sub_ids = [ref.actual.id for ref in subtitle_refs if ref.input_index == 1]
    command += (
        ["--subtitle-tracks", ",".join(map(str, stage_sub_ids))]
        if stage_sub_ids
        else ["--no-subtitles"]
    )
    command += ["--no-chapters", "--no-global-tags", "--no-attachments"]
    for index, (actual, metadata) in enumerate(zip(stage_dubs, plan.dub_tracks)):
        command += _track_options(
            TrackRef(
                1,
                stage.path,
                actual,
                metadata,
                "dual-synced",
                sync.timeline_adjustment_ms or None,
            ),
            default=index == 0,
        )
    command += _track_options(
        TrackRef(
            1,
            stage.path,
            stage_original,
            plan.resolved_original.track,
            "normal-synced",
            sync.timeline_adjustment_ms or None,
        ),
        default=False,
    )
    for reference in [ref for ref in subtitle_refs if ref.input_index == 1]:
        command += _track_options(reference, default=reference.metadata is preferred_default)
    command.append(stage.path)

    if selected_binary_refs:
        binary_sub_ids = [ref.actual.id for ref in selected_binary_refs]
        command += [
            "--no-video",
            "--no-audio",
            "--subtitle-tracks",
            ",".join(map(str, binary_sub_ids)),
            "--no-chapters",
            "--no-global-tags",
            "--no-attachments",
        ]
        for reference in selected_binary_refs:
            command += _track_options(reference, default=reference.metadata is preferred_default)
        command.append(dual.path)

    for reference in selected_sidecar_refs:
        command += [
            "--no-video",
            "--no-audio",
            "--subtitle-tracks",
            str(reference.actual.id),
            "--no-chapters",
            "--no-global-tags",
            "--no-attachments",
        ]
        command += _track_options(reference, default=reference.metadata is preferred_default)
        command.append(reference.path)

    notify("Collecting attachments")
    attachments = _attachment_files(
        [(master.path, master.attachments), (dual.path, dual.attachments)], work_dir, runner
    )
    for attachment, attachment_path in attachments:
        command += ["--attachment-name", attachment.name]
        command += ["--attachment-mime-type", attachment.content_type]
        if attachment.description:
            command += ["--attachment-description", attachment.description]
        command += ["--attach-file", attachment_path]

    final_partial = partial
    end_trim_report: dict[str, object] = {
        "enabled": config.end_trim,
        "applied": False,
        "target_seconds": None,
    }
    try:
        notify("Writing final MKV")
        _write_final_mkv(runner, command, partial)
        if config.end_trim:
            notify("Checking stream durations")
            end_cut = _validated_end_cut(
                master,
                stage,
                stage_audio,
                sync.timeline_adjustment_ms,
                config.end_tolerance_ms,
                runner,
            )
            if end_cut is not None:
                end_trim_report["target_seconds"] = end_cut
                trimmed = output.parent / f".{output.name}.endtrim-{uuid.uuid4().hex}.mkv"
                trimmed, trim_reason = _trim_end(partial, trimmed, end_cut, runner)
                if trimmed is None:
                    end_trim_report["reason"] = trim_reason
                else:
                    trimmed_duration = inspector.inspect(trimmed).duration
                    if trimmed_duration <= end_cut + config.end_tolerance_ms / 1000:
                        partial.unlink(missing_ok=True)
                        final_partial = trimmed
                        end_trim_report["applied"] = True
                    else:
                        end_trim_report["reason"] = "no safe keyframe at the requested endpoint"
                        LOGGER.warning(
                            "Keeping the original end because no keyframe allowed a safe %.3fs cut",
                            end_cut,
                        )
                        trimmed.unlink(missing_ok=True)
        from .validation import validate_output

        notify("Validating final MKV")
        _, validation = validate_output(
            final_partial,
            plan,
            expected_subtitle_count=len(subtitle_refs),
            expected_attachment_count=len(attachments),
            expected_chapter_count=(None if end_trim_report["applied"] else len(master.chapters)),
            inspector=inspector,
        )
        validation["subtitle_selection"] = subtitle_selection
        expected_starts = [
            max(
                (first_packet_pts(stage.path, "audio", track.type_index, runner) or 0.0)
                + sync.timeline_adjustment_ms / 1000,
                0.0,
            )
            for track in stage_audio
        ]
        actual_starts = [
            first_packet_pts(final_partial, "audio", index, runner)
            for index in range(len(stage_audio))
        ]
        start_errors = [
            abs(actual - expected)
            for actual, expected in zip(actual_starts, expected_starts)
            if actual is not None
        ]
        if len(start_errors) != len(stage_audio) or any(error > 0.05 for error in start_errors):
            raise ProcessingError(
                "Output validation failed: requested A/V timeline correction was not preserved"
            )
        validation["audio_packet_starts"] = {
            "expected": expected_starts,
            "actual": actual_starts,
            "tolerance_seconds": 0.05,
        }
        validation["end_trim"] = end_trim_report
        # link() is an atomic, no-overwrite publication because staging and
        # destination deliberately share a filesystem.
        notify("Publishing output")
        _set_output_group(final_partial, config.output_group)
        _publish_no_overwrite(final_partial, output)
    except Exception:
        partial.unlink(missing_ok=True)
        if final_partial != partial:
            final_partial.unlink(missing_ok=True)
        raise
    return output, len(subtitle_refs), validation
