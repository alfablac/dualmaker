from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import ProcessingError
from .languages import is_portuguese, normalize_language
from .metadata import MediaInspector
from .models import JobPlan, MediaAsset, Track
from .ordering import order_subtitles


def validate_output(
    path: Path,
    plan: JobPlan,
    *,
    expected_subtitle_count: int,
    expected_attachment_count: int | None = None,
    expected_chapter_count: int | None = None,
    inspector: MediaInspector | None = None,
) -> tuple[MediaAsset, dict[str, Any]]:
    inspector = inspector or MediaInspector()
    output = inspector.inspect(path)
    expected_audio_count = len(plan.dub_tracks) + 1
    failures: list[str] = []
    if len(output.video_tracks) != 1:
        failures.append(f"expected 1 video track, found {len(output.video_tracks)}")
    elif plan.normal.video_tracks:
        expected_video = plan.normal.video_tracks[0]
        actual_video = output.video_tracks[0]
        if expected_video.codec_id and actual_video.codec_id != expected_video.codec_id:
            failures.append(
                f"video codec changed from {expected_video.codec_id} to {actual_video.codec_id}"
            )
        expected_dimensions = expected_video.properties.get("pixel_dimensions")
        actual_dimensions = actual_video.properties.get("pixel_dimensions")
        if expected_dimensions and actual_dimensions != expected_dimensions:
            failures.append(
                f"video dimensions changed from {expected_dimensions} to {actual_dimensions}"
            )
    if len(output.audio_tracks) != expected_audio_count:
        failures.append(
            f"expected {expected_audio_count} audio tracks, found {len(output.audio_tracks)}"
        )
    if output.audio_tracks:
        selected_original = plan.resolved_original
        if not output.audio_tracks[0].default or not is_portuguese(
            output.audio_tracks[0].effective_language
        ):
            failures.append("first audio track is not the default Portuguese dub")
        if any(track.default for track in output.audio_tracks[1:]):
            failures.append("a non-primary audio track is marked default")
        if not all(is_portuguese(track.effective_language) for track in output.audio_tracks[:-1]):
            failures.append("not all tracks before the original are Portuguese")
        if (
            output.audio_tracks[-1].effective_language.split("-", 1)[0]
            != (selected_original.track.effective_language.split("-", 1)[0])
        ):
            failures.append("last audio track is not the selected original language")
        if (
            selected_original.track.codec_id
            and output.audio_tracks[-1].codec_id != selected_original.track.codec_id
            and not (plan.fps.apply_speed_correction and selected_original.source == "dual")
        ):
            failures.append(
                f"original audio codec changed from {selected_original.track.codec_id} "
                f"to {output.audio_tracks[-1].codec_id}"
            )
    if len(output.subtitle_tracks) != expected_subtitle_count:
        failures.append(
            f"expected {expected_subtitle_count} subtitles, found {len(output.subtitle_tracks)}"
        )
    expected_subtitle_order = order_subtitles(output.subtitle_tracks)
    if [track.id for track in output.subtitle_tracks] != [
        track.id for track in expected_subtitle_order
    ]:

        def describe(track: Track) -> str:
            language = normalize_language(track.effective_language)
            forced = "/forced" if track.forced else ""
            title = f"/{track.title}" if track.title else ""
            return f"{language}{forced}{title}"

        actual = ", ".join(describe(track) for track in output.subtitle_tracks)
        expected = ", ".join(describe(track) for track in expected_subtitle_order)
        failures.append(
            "subtitle tracks are not in the required language/forced order "
            f"(actual: [{actual}]; expected: [{expected}])"
        )
    expected_default = next(
        (
            track
            for track in output.subtitle_tracks
            if is_portuguese(track.effective_language) and track.forced
        ),
        None,
    )
    defaults = [track for track in output.subtitle_tracks if track.default]
    if expected_default is None and defaults:
        failures.append("a subtitle is default even though no Portuguese forced subtitle exists")
    if expected_default is not None and (
        len(defaults) != 1 or defaults[0].id != expected_default.id
    ):
        failures.append("the preferred Portuguese forced subtitle is not the sole default")
    if (
        expected_attachment_count is not None
        and len(output.attachments) != expected_attachment_count
    ):
        failures.append(
            f"expected {expected_attachment_count} attachments, found {len(output.attachments)}"
        )
    if expected_chapter_count is not None and len(output.chapters) != expected_chapter_count:
        failures.append(f"expected {expected_chapter_count} chapters, found {len(output.chapters)}")
    if failures:
        raise ProcessingError("Output validation failed: " + "; ".join(failures))
    return output, {
        "ok": True,
        "duration": output.duration,
        "video_tracks": len(output.video_tracks),
        "audio_tracks": len(output.audio_tracks),
        "subtitle_tracks": len(output.subtitle_tracks),
        "attachments": len(output.attachments),
        "chapters": len(output.chapters),
    }
