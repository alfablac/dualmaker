# dualmaker

Documentação completa em português do Brasil: [Como o dualmaker funciona](docs/COMO-FUNCIONA.pt-BR.md).

`dualmaker` builds a new MKV from two matching releases:

- a **normal** release containing the original-language audio; and
- a **DUAL** release containing Portuguese dub audio, normally with the same original language.

The normal release remains the master for video, chapters, tags, and naming. Audio is selected
independently by quality: the preferred original may come from either release, while every
retained source-side dub is synchronized with the master using the bundled `milksync`
engine. A legacy Portuguese-only AVI is also accepted: it is remuxed with stream copy to a private
MKV work file; the source is never changed.

## Requirements

- Linux or WSL
- Python 3.11 or newer
- FFmpeg and ffprobe on `PATH`
- MediaInfo CLI on `PATH`
- MKVToolNix 76 or newer (`mkvmerge`, `mkvextract`, and `mkvpropedit`) on `PATH`
- Approximately 10 GB of free memory for difficult full-length synchronization jobs

Check the external tools with:

```console
dualmaker --check-deps
```

## Installation

For a machine-wide command isolated from the system Python, use `pipx`:

```console
pipx install /home/alfablac/repos/dualmaker
dualmaker --version
dualmaker --init-config
```

`--init-config` creates `~/.dualmaker/config.yml` with private file permissions and never
overwrites it. A normal first run creates the same file automatically if no local or legacy
configuration exists. Edit it whenever defaults need to change; the package does not need to be
reinstalled.

For development:

```console
cd /home/alfablac/repos/dualmaker
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

The equivalent dependency files are `requirements.txt` for runtime use and
`requirements-dev.txt` for runtime plus test/lint tooling.

The large Atmos and DTS-HD silence samples required by milksync are installed as package data.
FFmpeg, MediaInfo, and MKVToolNix remain system dependencies so the package is not tied to a
specific Linux distribution or CPU architecture.

Rich and Textual provide the formatted command-line interface and full-screen interactive
workflow. They are installed automatically with the package.

## Terminal interface

Regular runs use readable status panels, match/track tables, color-coded results, progress bars,
and actionable error panels. Disable color with `--color never`, use stable uncolored output with
`--format plain`, or suppress it with `--quiet`.

`--interactive` opens a keyboard-driven checklist. Use the arrow keys to navigate and Space to
toggle jobs. The review screen shows the chosen DUAL source, normal master, score, and common
language before anything is processed. It provides **Back**, **Cancel**, and **Confirm** controls.
Ambiguous original languages, equivalent audio tracks, recap cuts, and experimental frame-rate
approval use radio-button or explicit action dialogs rather than free-form text. Escape or `q`
cancels safely; cancellation exits 130 and does not start another job.

For a desktop workflow on Linux, macOS, or Windows, install the same package and run
`dualmaker-gui`. It uses the platform's Tk desktop toolkit and the same discovery, matching,
planning, and processing pipeline as `dualmaker`:

```console
dualmaker-gui /media/releases
dualmaker-gui --config ~/.dualmaker/config.yml
```

The GUI scans folders in the background, displays every scored candidate (including alternatives
that the automatic CLI mode would call ambiguous), lets you select one candidate per title, and
supports manually adding an explicit master/DUAL pair. `?` buttons explain the common settings;
the full configuration remains available in YAML/TOML. On Debian/Ubuntu, install `python3-tk` if
your Python distribution does not include Tk.

For scripts and service wrappers, use `--json`. Standard output then contains exactly one JSON
document with the status, exit code, report path, concise results, and skipped items. Tool output
and interactive controls are disabled in this mode:

```console
dualmaker /media/releases --recursive --dry-run --json
dualmaker --check-deps --json
dualmaker --show-config --json
```

## Configuration

All runtime defaults and policy are resolved in one layer with this precedence:

```text
command-line option > DUALMAKER_* environment variable > YAML/TOML file > built-in default
```

The preferred user file is `~/.dualmaker/config.yml`. Create it explicitly with
`dualmaker --init-config`, or let dualmaker create it on the first operational run. Existing files
are never overwritten. Local `dualmaker.yml`, `dualmaker.yaml`, and `dualmaker.toml` files are
discovered automatically; `~/.config/dualmaker/config.toml` remains supported for backwards
compatibility. Use `--config FILE` to select another YAML or TOML file. See
[`dualmaker.example.yml`](dualmaker.example.yml) for every section. `--show-config` displays the
resolved values and where each important value came from. Generated `config.yml` files annotate
every persistent setting in place, including all sync/fallback controls; optional per-run paths
are included as commented examples under `paths`. To add new settings/comments to an existing YAML
file without losing its values, run `dualmaker --refresh-config`; it creates a timestamped private
backup before atomically replacing the config.

Common environment variables include `DUALMAKER_CONFIG`, `DUALMAKER_PATH`,
`DUALMAKER_OUTPUT_DIR`, `DUALMAKER_TEMP_DIR`, `DUALMAKER_TAG`, `DUALMAKER_DUB_LANGUAGE`,
`DUALMAKER_ALLOWED_PATHS`, `DUALMAKER_REQUIRED_PATHS`, `DUALMAKER_ENFORCE_PATHS`,
`DUALMAKER_REQUIRED_GROUP`, `DUALMAKER_OUTPUT_GROUP`, `DUALMAKER_OUTPUT_FORMAT`, `DUALMAKER_COLOR`, and one variable per
binary such as `DUALMAKER_FFMPEG` or `DUALMAKER_MKVMERGE`. Multiple paths use the platform path
separator (`:` on Linux). `DUALMAKER_CONFIG_HOME` changes the directory containing the generated
`config.yml`; it is useful for isolated services and test installations.

At startup, dualmaker verifies input readability, destination and work-directory permissions,
required paths, optional group membership, allowed-path containment, executable locations, and
the minimum MKVToolNix version. Errors identify the exact setting and a corrective option or
environment variable.

To assign a Unix group to each completed output file, configure `output_group` under `security`,
set `DUALMAKER_OUTPUT_GROUP`, or pass `--output-group GROUP`:

```yaml
security:
  output_group: media
```

The running user must belong to that group. `required_group` is separate: it only gates whether a
run is allowed to start. The output directory itself is not chowned.

## Common calls

```console
# Scan the current directory and make every unambiguous pair
dualmaker

# Scan another directory
dualmaker /media/releases

# Include season/release subdirectories and mirror them below dualmaker-output
dualmaker /media/shows --recursive

# Inspect matching and track choices without processing media
dualmaker /media/releases --dry-run

# Ask when pair, original-language, or recap choices are ambiguous
dualmaker /media/releases --interactive

# Select jobs interactively, but only review the resulting plan
dualmaker /media/releases --interactive --dry-run

# Bypass filename pairing while retaining metadata/track validation
dualmaker --dual release.DUAL-GROUP.mkv --normal release-GROUP.mkv

# Supply an exact destination for an explicit pair
dualmaker --dual dual.mkv --normal normal.mkv --output final.mkv

# Import a Portuguese-only legacy AVI. Its one untagged program track is inferred as pt-BR.
dualmaker --dual Mulher.Bionica.S01E01.avi --normal The.Bionic.Woman.S01E01.1080p.BluRay.mkv \
  --allow-experimental-fps-sync

# Explicitly permit analysis of a likely match with different exact frame rates
dualmaker --dual dual-25fps.mkv --normal master-24000-1001.mkv \
  --allow-experimental-fps-sync

# Review an editorially different broadcast source interactively
dualmaker --tvrip episode.HDTV.DUAL-GROUP.mkv --normal episode.WEB-DL-GROUP.mkv \
  --interactive

# Unattended TVRip beta with an explicit master-gap policy
dualmaker --tvrip episode.HDTV.DUAL-GROUP.mkv --normal episode.BluRay-GROUP.mkv \
  --allow-tvrip-segment-sync --tvrip-fallback silence
```

By default, outputs go into `dualmaker-output` below the scanned folder. Recursive runs preserve
the master's relative parent directory. The output name is taken from the normal release, its
final release group is removed, and `.DUAL-alfaHD.mkv` is appended:

```text
Minions.and.Monsters.2026.1080p.MA.WEB-DL.DDP5.1.Atmos.H.264-BYNDR.mkv
  -> dualmaker-output/Minions.and.Monsters.2026.1080p.MA.WEB-DL.DDP5.1.Atmos.H.264.DUAL-alfaHD.mkv
```

If that name exists, `.2`, `.3`, and so on are added before `.mkv`. Change this with
`--on-conflict skip` or `--on-conflict error`; existing MKVs are never overwritten.

## Matching and tracks

Movie matching uses normalized title and year. Episode matching uses normalized series name and
the complete `SxxExx` identity. Resolution, provider, codec, HDR/Dolby Vision, audio labels,
`DUAL`, and release-group tokens do not have to match.

Media roles come from actual audio metadata. A DUAL candidate must contain Portuguese program
audio. When a non-Portuguese language is shared with the normal release, it is the primary acoustic
reference. A Portuguese-only source instead enters the experimental cross-language sound-event
path. A one-audio AVI whose language is absent is inferred as the configured dub language (`pt-BR`
by default); multi-audio AVIs still require an explicit track choice. A `DUAL` filename token only
increases confidence; it does not override tracks. Ambiguous unattended matches are skipped and
written to the report. For an explicit episode pair, translated series titles are accepted when
the season and complete episode number match; this is how `Mulher.Bionica.S01E01.avi` can be paired
with `The.Bionic.Woman.S01E01…mkv`.

Output audio order is:

1. the best Portuguese dub, default;
2. every additional selected non-commentary Portuguese dub, non-default; and
3. the best original-language track, independently selected from either source, non-default.

Audio candidates are ranked using codec preference, bitrate, channel count, sample rate, track
duration/completeness, language/title flags, source default status, and the configured source
preference. The default tie-breaks favor DUAL for dubs and the master for original audio, but a
material quality advantage wins. Set `preferred_dub_source` or `preferred_original_source` to
`master`, `dual`, or `quality`; `quality` removes the source tie-break. A remaining score gap no
larger than `audio_selection_margin` is not guessed: interactive mode shows a navigable metadata
choice, while unattended mode skips the job. Configure `audio_codec_preference` to change codec
ranking.

Source-aware overrides use Matroska track IDs:

```console
dualmaker --dual dual.mkv --normal master.mkv \
  --dub-track dual:1 --dub-track master:4 --original-track dual:2
```

`--dual-audio ID` and `--normal-audio ID` remain compatibility aliases for DUAL dubs and the
master comparison/original track respectively.

By default, output subtitles are **master-preferred by presentation slot**: the master keeps its
regular/forced/SDH track for a language, and the DUAL source contributes only missing
language/forced/SDH variants. This avoids a second full subtitle set when distributors differ only
by tiny timestamps, styling, or caption wording. Use `--subtitle-policy exact-union` (or set
`subtitle_policy: exact-union` in the configuration file) when every non-byte-identical source
subtitle must be retained. DUAL-side text subtitles are synchronized with the imported audio.
PGS bitmap display-set timestamps are rewritten through the same speed, cut, and offset map while
their bitmap payload remains untouched. Bogus subtitle packets beyond the real source-video endpoint
are discarded. VobSub is stream-copied when all active packets use one Milksync delay (and an
optional global speed multiplier); a VobSub track spanning multiple edit delays still requires a
matching master replacement or the job stops with the affected track and reason.

External sidecars named for a selected MKV are also supported (`.srt`, `.ass`, `.ssa`, and text
`.sub`). A sidecar attached to the DUAL source defaults to `pt-BR`, so full seasons can run
unattended. Interactive mode still lets you choose every sidecar language. Override an individual
sidecar with `--sidecar-language 'Warehouse 13 … DUAL-JK.srt=en'`; a master-attached sidecar still
requires an explicit choice or mapping. Configure the DUAL default with `sidecar_dual_language`
(`DUALMAKER_SIDECAR_DUAL_LANGUAGE`) and repeatable `PATH=LANGUAGE` overrides with
`sidecar_language_overrides` (`DUALMAKER_SIDECAR_LANGUAGES`).
Every selected text sidecar is staged as UTF-8 with BOM before trimming, synchronization, or muxing;
UTF-8, Windows-1252, and Latin-1 inputs are handled without modifying the source file.

Subtitle order and dispositions are:

1. Portuguese forced; the preferred track is the sole default subtitle.
2. Portuguese regular, SDH, CC, and other variants; non-default.
3. English, including forced English; non-default.
4. Other languages alphabetically by normalized ISO/BCP-47 language tag; non-default.

Forced, hearing-impaired, title, and language metadata are preserved. Normal chapters/global
tags are retained, and font/other attachments from both files are deduplicated by content hash.

## Recaps, synchronization, and duration

With `--trim-recap` (the default), dualmaker searches the opening 120 seconds for black sections,
safe master-video keyframes, and matching spectral fingerprints from the shared original audio.
It trims only a unique, validated one-sided opening. Cross-language event synchronization leaves
the opening intact because translated dialogue cannot prove a recap boundary. Use `--recap-window`,
`--no-trim-recap`, or `--interactive` to adjust that behavior.

Milksync then calculates the source-to-master shift map from shared original-language tracks, or
from mutually strong sound events for a Portuguese-only source.
The first packet timestamp of both references is recorded for diagnosis, but is not treated as a
dub delay: codec priming and broken container PTS do not prove an offset from the master video.
The same acoustic reference map is applied to every imported Portuguese audio track and DUAL-side
subtitle. `--adjust-delay` is an explicit manual override. Video is always stream-copied from the
normal master. Imported audio is stream-copied when its codec and required edit boundaries allow
it; codec fallback required for inserted silence is reported.

### Experimental DUAL-source dub resync

`experimental_dub_resync` is enabled by default. It imports a selected Portuguese dub from the
DUAL/lower-quality release into the normal/master video. When a common original track exists,
Milksync maps that reference normally. For a Portuguese-only source, it instead uses short,
high-energy/onset windows and retains mutually strong music hits, impacts, transitions, gunshots,
doors, and footsteps as cross-language anchors; translated dialogue is not used as a linguistic
match. A stationary map becomes a constant offset; changing offsets become separate source-to-master
correction buckets. Compatible different-FPS pairs additionally require the separate
`--allow-experimental-fps-sync` workflow.

The JSON report includes `experimental_dub_resync`: selected source and master, every acoustic
anchor, offset range, correction buckets, mapped coverage, video-confirmation evidence, and a
confidence score. Cross-language jobs report `alignment_mode: cross-language-events` and use the
short event windows immediately. The default `experimental_dub_resync_min_confidence: 0.80` marks
a sparse map for review; it does not fabricate dialogue-based anchors.

For a different-FPS Portuguese-only source, dualmaker also measures the raw program clock from
those event anchors before rendering. It accepts four well-spaced event slopes (instead of the
denser common-language requirement), tolerates mix variance, and permits a bounded default 10%
speed range; PAL 25 fps → 23.976 fps is a 4.27% correction. The rendered event map gets one more
bounded correction pass when it still shows linear drift. An out-of-sync opening with good later
sections can instead indicate an alternate intro/recap: cross-language mode leaves that opening
untrimmed because dialogue cannot prove its exact cut boundary.
Disable the feature with `--no-experimental-dub-resync` (or set
`experimental_dub_resync: false`), or adjust the threshold with
`--experimental-dub-resync-min-confidence`.

### Milksync resource limits

Milksync runs numerical CQT/DTW analysis, and its native dependencies may otherwise create a
thread pool for every CPU core. Dualmaker now caps those pools to two threads, uses one concurrent
chroma extractor, and slices DTW at 25,000,000 cells by default. This avoids large virtual-memory
maps and hundreds of idle threads in season batches. Lower limits trade throughput for predictable
resource use:

```console
dualmaker /media/releases \
  --milksync-max-threads 1 \
  --milksync-chroma-workers 1 \
  --milksync-max-cost-matrix-cells 8000000
```

The equivalent `features` configuration keys are `milksync_max_threads`,
`milksync_chroma_workers`, and `milksync_max_cost_matrix_cells`. Their environment equivalents
are `DUALMAKER_MILKSYNC_MAX_THREADS`, `DUALMAKER_MILKSYNC_CHROMA_WORKERS`, and
`DUALMAKER_MILKSYNC_MAX_COST_MATRIX_CELLS`.

By default, dualmaker also compares low-resolution video fingerprints at several points across
both releases. This reconciles the audio-derived shift with the actual master-video timeline. If
the two mappings expose a stable shared A/V residual, that residual is applied to the Portuguese
dubs, the master original audio, and synchronized DUAL subtitles. Unreliable or changing frame
matches are never converted into a guessed constant delay. Use `--no-reconcile-av` to disable the
check or `--av-tolerance-ms` to change its correction threshold.

The milksync output mapping contains every selected Portuguese track followed by the selected
original track. DUAL-side tracks receive the piecewise source-to-master edit map; master-side
tracks travel through the same intermediate on the already-correct master timeline. The final
ordering mux consumes every audio role from that intermediate and never silently re-imports a
track from an earlier input.

### Missing-dub fallback

An otherwise matching DUAL release can omit a scene that exists in the master. Milksync exposes
this as a hole between its mapped shared-original (normally English) sections; without repair the
imported Portuguese track contains silence there. By default, dualmaker validates every mapped
section against the video timeline and, only when that map is complete and the remaining DUAL dub
coverage is at least 80%, inserts the master reference/original audio for each verified hole. The
resulting Portuguese track is clearly titled with its dub coverage and fallback intervals, and is
losslessly re-rendered as FLAC because it is a splice.

This is a safety feature, not an inference from durations. A correlation hole that cannot be
validated never replaces Portuguese dialogue: the normal synchronized track is retained and the
JSON report records that fallback was withheld. Interactive runs show the mapped-section checklist
and let you choose master-original audio or silence before publication.

Use `--dub-gap-fallback original|silence|off`, `--dub-gap-min-seconds`, and
`--dub-gap-min-coverage`, or set `dub_gap_fallback`, `dub_gap_min_seconds`, and
`dub_gap_min_coverage` in the `features` configuration section. The fallback is the master
reference track used in the original-language comparison, so it also works for non-English source
languages; it is never guessed from the selected output-original preference. The same policy is
inherited by an `HDTV`/TVRip-labelled DUAL source when its TVRip-specific fallback remains `ask`,
so a validated missing credits/ending range is filled too. Set `dub_gap_fallback: off` together
with `tvrip_fallback: ask` to require the historical TVRip review.

### Experimental different-FPS synchronization

Exact rational average frame rates are read from ffprobe. Equal rates use the normal supported
path. A configured compatible mismatch—such as `24000/1001` versus `24/1` or `25/1`—is beta-only.
The plan displays the nominal full-length drift. Interactive runs always require an explicit
**Continue beta** action; unattended runs skip with exit 2 unless
`--allow-experimental-fps-sync` (or its config/environment equivalent) is set.

Approval does not automatically change speed. Container cadence and program playback speed are
treated separately. Dualmaker tests real-time playback and the exact container FPS ratio, then uses
the selected common-original audio durations to nominate configured standard program-speed factors
such as `24/25`. A raw duration ratio is never applied: a nearby configured standard must still pass
independent content anchors. This handles video cadence-converted to 29.97 after a 4% program-speed
conversion without incorrectly applying the `0.8` container-rate ratio. If local windows are
displaced by commercials or editorial cuts, the adaptive fallback creates a private,
one-frame-per-second visual index, selects informative scenes across both features, verifies each
candidate over a short sequence, and accepts only a time-ordered chain. A chain spanning one
consistent affine mapping can authorize a tempo factor; a non-linear chain is explicitly marked
as broadcast/edit segmentation and can continue only through the TVRip beta workflow. It never
turns an inferred offset into a normal speed change.

Before rendering, a Milksync chroma/spectrogram preflight measures local slopes across many
common-original correspondence points. Editorial cuts change the intercept but not the slope
inside a matching section, so a robust bounded-pair fit separates missing material from true tempo.
This acoustic measurement is authoritative over a duration-nominated candidate. After rendering,
the same fit must show a residual factor near `1.0`; otherwise the job stops before publication.

Milksync then works in normalized seconds, converts every piecewise source cut back to original
timestamps, and time-stretches each retained source-side audio segment only when the content
analysis approved it. This necessarily re-encodes affected audio and can discard Atmos/object or
lossless codec metadata; the fallback is reported. Different-FPS segments always use FLAC
intermediates: separately encoding every edit segment to AAC/AC-3/E-AC-3 would preserve encoder
priming at every join and create a delay that grows through the feature. The lossless segments are
concatenated first, so no per-edit codec delay accumulates. Text subtitles are time-normalized with the
same map. PGS timestamps are mapped display set by display set; VobSub is retained only when its
packet timeline can be represented safely by one offset and global multiplier.

After synchronization, video anchors are checked against the piecewise audio map. A globally
affine FPS conversion still requires three successful checks. A segmented map normally requires
two widely separated checks. When reliable common-original spectrogram fits before and after
synchronization prove the content clock and the audio map covers at least 80% of the feature, one
video anchor may admit the map to the stricter TVRip stage; that stage still requires at least two
timing-consistent video probes for every retained segment. Reports include exact rates,
expected drift, telecine/interlace clues, every tested timing hypothesis, common-original duration
evidence, the selected clock, detected/applied speed,
confidence, anchors, codec warnings, synchronization coverage, and post-map errors. Configure
`compatible_fps_pairs`, `fps_min_match_confidence`, `fps_validation_positions`,
`fps_search_radius_seconds`, `fps_speed_ratio_tolerance`, `fps_content_speed_factors`,
`fps_audio_duration_ratio_tolerance`, `fps_spectral_*`, and the `fps_anchor_*` controls under
`features`; see the generated configuration comments for defaults. This remains experimental: when
post-map evidence is inconclusive, dualmaker records the exact samples and error in the JSON report,
emits a warning, and produces the best available output for manual review.

Matroska container duration is not trusted when a subtitle packet outlasts the feature. Dualmaker
prefers the primary video and selected audio stream durations (including Matroska `DURATION`
statistics) and seeks packet ends from those streams. A late PGS/VobSub event therefore cannot
make a 23-minute broadcast look 32 minutes long or distort FPS/synchronization coverage.

### Experimental TVRip-to-master synchronization

Use --tvrip for a broadcast source with commercials, station bumpers, local recaps, censorship,
shortened openings, altered credits, or other editorial differences. The WEB-DL/Blu-ray input
passed with --normal remains the immutable video, chapter, tag, and naming master. The TVRip
workflow is always experimental: interactive runs show a segment checklist and fallback controls;
unattended runs skip unless --allow-tvrip-segment-sync is present.

Milksync first derives precise piecewise cut points from a non-Portuguese reference track shared
by both sources. Dualmaker bounds those buckets to the real source and master durations, splits
long buckets into regular validation slices, and independently compares video-content anchors at
the beginning, middle, and end of every slice. In the explicit telecine fallback, where the
common-original acoustic map can outweigh an inconclusive visual remaster comparison, every
mapped bucket also receives a local common-original audio check. A good opening therefore cannot
validate audio after an undetected commercial or a short HDTV-only scene between two valid
anchors. Source-only ranges are excluded and classified as likely commercials, bumpers,
pre-roll/recaps, or previews/credits. Master-only and rejected ranges are reported explicitly and
never covered by stretching adjacent dialogue.

This beta currently requires a tagged shared non-Portuguese reference track. A Portuguese-only
TVRip is rejected because cross-language dialogue cannot provide a trustworthy acoustic map;
dualmaker does not guess a map from total duration. Different-FPS TVRips are supported only when
the separate --allow-experimental-fps-sync consent is also supplied and content anchors prove
either a consistent speed relationship or a time-ordered segmented mapping. The latter preserves
real-time playback unless analysis specifically approves a speed correction.

Choose master-only behavior with --tvrip-fallback:

- original: insert the master original audio and name the affected intervals.
- alternate-dub: use a Portuguese dub already aligned to the master timeline.
- silence: preserve the master duration without silently switching language.
- omit: discard TVRip tracks; the job is rejected if no Portuguese track remains.
- ask: require an interactive choice whenever a gap exists.

Fallback rendering is lossless FLAC but necessarily re-encodes the affected TVRip track. Output
titles include segmented/partial coverage and fallback ranges. The JSON report records input
metadata, exact FPS, black intervals, shift/delete buckets, every bounded segment and its three
validation points, confidence, residual error, TVRip-only material, master-only gaps, coverage,
speed correction, fallback intervals, warnings, and codec fallbacks.

All TVRip policy belongs to the dedicated tvrip configuration section. Important settings include
tvrip_min_source_match_confidence, tvrip_min_segment_confidence,
tvrip_max_residual_seconds, tvrip_min_coverage, segment duration/count limits,
commercial-break sensitivity, speed policy, partial-track policy, validation positions,
tvrip_acoustic_segment_validation and its window/gap/padding/minimum/similarity/proof thresholds, fallback, and
the track-title template. See [dualmaker.example.yml](dualmaker.example.yml).

With `--end-trim` (the default), MediaInfo durations are cross-checked against final packet times.
If the video exceeds the shortest selected audio by more than 500 ms, dualmaker attempts a
keyframe-safe trim. If a stream-copy cut cannot reach the requested end, the complete video is
retained and a warning is recorded. Use `--end-tolerance-ms` or `--no-end-trim` to change this.

## Safety, reports, and exit codes

Every job uses a private directory below `<scan-path>/.dualmaker-work` by default; dualmaker never
falls back to a shared `/tmp` directory. Override the root with `--temp-dir` or
`DUALMAKER_TEMP_DIR`. The result is structurally re-probed and validated before an atomic,
no-overwrite publication. Job files are removed after success or failure; `--keep-temp` retains
them for diagnosis.

After acoustic synchronization, the progress display reports the remaining phases explicitly:
track inspection, batched subtitle deduplication, attachment collection, final MKV writing,
duration checks, validation, and publication. Subtitle and attachment extraction is batched once
per source MKV so large files are not rescanned for every individual track.

A timestamped JSON report is written to the output directory. It contains complete MediaInfo,
ffprobe, and mkvmerge metadata, matching reasons, chosen tracks, recap trims, synchronization
points, validation results, skipped pairs, and failures. Override the location with `--report`.

- Exit `0`: all eligible jobs completed, or the dry-run plan is clean.
- Exit `1`: a dependency or processing job failed.
- Exit `2`: one or more pairs were deliberately skipped as ambiguous or conflicting.

Use `dualmaker --help` for every basic and advanced synchronization option.

## Python API

```python
from dualmaker import DualMakerConfig, make_dual, plan_pair, scan_directory

assets = scan_directory("/media/releases")
config = DualMakerConfig(trim_recap=True, end_trim=True)
plan = plan_pair("dual.mkv", "normal.mkv", config)
result = make_dual(plan, config)
```

The public data models include `MediaAsset`, `Track`, `Attachment`, `ContentIdentity`, `FrameRate`,
`AudioTrackSelection`, `FPSDecision`, `TVRipSegment`, `TVRipInterval`,
`TVRipValidationPoint`, `TVRipSyncReport`, `PairCandidate`, `JobPlan`, `JobResult`, and
`DualMakerConfig`.

## License and attribution

dualmaker is distributed under the GNU Affero General Public License, version 3 or later. It
contains a modified copy of `milksync.py`, itself derived from The Cute Collection's
milksync implementation. See `LICENSE` and `NOTICE`.
