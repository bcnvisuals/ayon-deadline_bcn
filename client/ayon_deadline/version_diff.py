# -*- coding: utf-8 -*-
"""Version diff: compare a published render with its previous version.

The publish plugin `SubmitVersionDiff` writes a json "spec" and submits
three Deadline jobs (all running `ayon_console`):

1. `version-diff-render` (chunked) - one diff frame per published frame.
2. `version-diff-finalize` - collects frame stats, makes the review mp4,
   thumbnail and the farm publish metadata json.
3. `publish <metadata> --targets farm` - the standard farm publish of the
   `<product>_diff` product.

The diff is measured in display space (OCIO default display/view of the
published colorspace) after a small median + gaussian prefilter of both
frames, so render noise is averaged out and only visible changes count.
"""
import os
import re
import json
import shutil

import ayon_api

from ayon_core.lib import (
    Logger,
    run_subprocess,
    get_oiio_tool_args,
    get_ffmpeg_tool_args,
)
from ayon_core.lib.transcoding import (
    IMAGE_EXTENSIONS,
    get_oiio_info_for_input,
)

log = Logger.get_logger("VersionDiff")

DIFF_FLAG_KEY = "versionDiffResult"
BEAUTY_AOV_NAMES = {"", "beauty", "rgba"}
FRAME_REGEX = re.compile(r"(\d+)\.[^.]+$")
STATS_REGEX = re.compile(r"Stats (Min|Max|Avg): ([-0-9.eE+naif]+)")
# Overlay colour for changed pixels (display referred RGB)
HIGHLIGHT_COLOR = (1.0, 0.12, 0.06)


def is_beauty_aov(aov):
    """Return True for the beauty/main pass of a render."""
    return (aov or "").lower() in BEAUTY_AOV_NAMES


def find_previous_version(project_name, version_entity):
    """Latest earlier (non-hero) version of the same product and task.

    Returns:
        Optional[dict[str, Any]]: Version entity or None.
    """
    candidates = [
        version
        for version in ayon_api.get_versions(
            project_name,
            product_ids=[version_entity["productId"]],
            fields={"id", "version", "taskId", "attrib"},
        )
        if 0 < version["version"] < version_entity["version"]
        and version["taskId"] == version_entity["taskId"]
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda version: version["version"])


def get_image_sequence(
    project_name, version_id, anatomy, preferred_name=None
):
    """Find the main image sequence representation of a version.

    Returns:
        Optional[dict[str, Any]]: Representation name, ext, colorspace data
            and resolved file path per frame number.
    """
    candidates = []
    for repre in ayon_api.get_representations(
        project_name, version_ids=[version_id]
    ):
        ext = (repre["context"] or {}).get("ext") or ""
        if f".{ext.lower()}" not in IMAGE_EXTENSIONS:
            continue
        if repre["name"] == "thumbnail" or not repre["files"]:
            continue
        frames = {}
        for file_info in repre["files"]:
            path = anatomy.fill_root(file_info["path"])
            match = FRAME_REGEX.search(os.path.basename(path))
            if match:
                frames[int(match.group(1))] = path
        if not frames:
            continue
        candidates.append({
            "name": repre["name"],
            "ext": ext,
            "frames": frames,
            "colorspaceData": (
                (repre.get("data") or {}).get("colorspaceData") or {}
            ),
        })

    if not candidates:
        return None

    def _sort_key(item):
        return (
            item["name"] != preferred_name,
            item["ext"].lower() != "exr",
            -len(item["frames"]),
        )

    return sorted(candidates, key=_sort_key)[0]


def get_image_info(path):
    """Display resolution and channel names of an image file."""
    info = get_oiio_info_for_input(path, verbose=False, logger=log)
    width = int(info.get("full_width") or info["width"])
    height = int(info.get("full_height") or info["height"])
    return {
        "width": width,
        "height": height,
        "channelnames": list(info.get("channelnames") or []),
    }


def get_rgb_channel_indexes(channel_names):
    """Indexes of R, G, B (falls back to first channels)."""
    lower_names = [name.lower() for name in channel_names]
    indexes = []
    for wanted in ("r", "g", "b"):
        for idx, name in enumerate(lower_names):
            if name == wanted or name.endswith(f".{wanted}") or (
                name.endswith(f".{wanted}ed") and wanted == "r"
            ):
                indexes.append(idx)
                break
    if len(indexes) == 3:
        return indexes
    count = max(1, min(3, len(channel_names)))
    indexes = list(range(count))
    while len(indexes) < 3:
        indexes.append(indexes[-1])
    return indexes


def compare_sequences(new_seq, old_seq, new_info, old_info):
    """Return list of human-readable reasons why the diff can't run."""
    reasons = []
    new_res = (new_info["width"], new_info["height"])
    old_res = (old_info["width"], old_info["height"])
    if new_res != old_res:
        reasons.append(
            "resolution changed {}x{} -> {}x{}".format(*old_res, *new_res)
        )

    new_frames = sorted(new_seq["frames"])
    old_frames = sorted(old_seq["frames"])
    if new_frames != old_frames:
        reasons.append(
            "frame range changed {}-{} ({} frames) -> {}-{} ({} frames)"
            .format(
                old_frames[0], old_frames[-1], len(old_frames),
                new_frames[0], new_frames[-1], len(new_frames),
            )
        )
    return reasons


def frames_to_deadline_str(frames):
    """Compress sorted frame numbers to Deadline frame list '1-5,8,10-12'."""
    frames = sorted(frames)
    parts = []
    start = prev = frames[0]
    for frame in frames[1:] + [None]:
        if frame is not None and frame == prev + 1:
            prev = frame
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        if frame is not None:
            start = prev = frame
    return ",".join(parts)


# -------------------------------------------------------------------------
# Farm side
# -------------------------------------------------------------------------
def load_spec(spec_path):
    """Load spec json and resolve rootless paths for this machine."""
    from ayon_core.pipeline import Anatomy

    with open(_fill_root_env(spec_path), "r") as stream:
        spec = json.load(stream)
    anatomy = Anatomy(spec["project_name"])
    spec["_anatomy"] = anatomy
    spec["staging_dir"] = anatomy.fill_root(spec["staging_dir"])
    spec["frames"] = {
        int(frame): [anatomy.fill_root(path) for path in paths]
        for frame, paths in spec["frames"].items()
    }
    return spec


def _fill_root_env(path):
    """Resolve '{root[...]}' in a path using AYON_PROJECT_NAME anatomy."""
    if "{root" not in path:
        return path
    from ayon_core.pipeline import Anatomy

    return Anatomy(os.environ["AYON_PROJECT_NAME"]).fill_root(path)


def get_frame_output_path(spec, frame):
    return os.path.join(
        spec["staging_dir"],
        "{}.{}.jpg".format(
            spec["file_basename"], str(frame).zfill(spec["frame_padding"])
        ),
    )


def get_frame_stats_path(spec, frame):
    return os.path.join(
        spec["staging_dir"], "stats", f"{frame}.json"
    )


def get_working_size(spec):
    """Resolution the diff is computed and written at.

    Frames are box-downscaled to 'max_output_width' right after reading;
    comparing full 6K frames was ~6x more expensive on the farm and the
    noise prefilter blurs fine detail away anyway.
    """
    width, height = spec["width"], spec["height"]
    max_width = int(spec["settings"]["max_output_width"])
    if not max_width or width <= max_width:
        return width, height
    return max_width, int(round(height * max_width / width / 2.0) * 2)


def _display_args(path, channel_indexes, colorspace, config_path, size):
    """oiiotool args reading one frame and converting it to display."""
    args = [
        "-i", path,
        "--croptofull",
        "--ch", ",".join(str(idx) for idx in channel_indexes),
        "--fixnan", "black",
    ]
    if size:
        args.extend(["--resize:filter=box", "{}x{}".format(*size)])
    if config_path and colorspace:
        # Empty display & view -> OCIO config defaults
        args.extend([f"--ociodisplay:from={colorspace}", "", ""])
    else:
        args.extend(["--colorconvert", "linear", "sRGB"])
    args.extend(["--clamp:min=0:max=1"])
    return args


def _prefilter_args(blur):
    args = ["--median", "3x3"]
    if blur > 1:
        args.extend(["--blur", f"{blur}x{blur}"])
    return args


def _get_blur_size(spec, working_width):
    """Noise blur scaled to the working resolution (odd, at least 3)."""
    blur = int(spec["settings"]["blur_size"])
    if blur <= 1:
        return blur
    scaled = int(round(blur * working_width / spec["width"]))
    return max(3, scaled | 1)


def build_frame_args(spec, frame):
    """Build the oiiotool command creating one diff frame."""
    settings = spec["settings"]
    new_path, old_path = spec["frames"][frame]
    threshold = float(settings["threshold"]) / 255.0
    config_path = spec.get("ocio_config")

    label = "{}  v{:03d} vs v{:03d}  |  frame {}".format(
        spec["product_name"], spec["new_version"], spec["old_version"], frame
    )
    width, height = get_working_size(spec)
    resize = None
    if width != spec["width"]:
        resize = (width, height)
    blur = _get_blur_size(spec, width)

    args = get_oiio_tool_args("oiiotool")
    if config_path:
        args.extend(["--colorconfig", config_path])
    args.extend(_display_args(
        new_path, spec["new_channels"], spec.get("new_colorspace"),
        config_path, resize
    ))
    args.extend(["--label", "dispNew", "--dup"])
    args.extend(_prefilter_args(blur))
    args.extend(_display_args(
        old_path, spec["old_channels"], spec.get("old_colorspace"),
        config_path, resize
    ))
    args.extend(_prefilter_args(blur))
    args.extend([
        # Per pixel difference = max channel difference in display space
        "--absdiff", "--maxchan",
        "--printstats",
        # Hard mask -> average is the fraction of changed pixels
        "--dup",
        "--subc", str(threshold), "--mulc", "1e6",
        "--clamp:min=0:max=1",
        "--printstats",
        "--pop",
        # Soft mask (full strength at 3x threshold), grown to stay visible
        "--subc", str(threshold), "--mulc", str(1.0 / (2.0 * threshold)),
        "--clamp:min=0:max=1",
        "--dilate", "3x3",
        "--ch", "0,0,0",
        "--label", "mask",
        # Background: dimmed luminance of the new frame
        "dispNew",
        "--chsum:weight=0.2126,0.7152,0.0722",
        "--mulc", "0.45",
        "--ch", "0,0,0",
        # bg * (1 - mask) + highlight * mask
        "mask", "--mulc", "-1", "--addc", "1", "--mul",
        "mask", "--mulc", ",".join(str(v) for v in HIGHLIGHT_COLOR),
        "--add",
    ])

    font_size = max(16, int(width / 60))
    args.extend([
        f"--text:x={font_size}:y={int(font_size * 1.6)}"
        f":size={font_size}:color=1,1,1",
        label,
        "-d", "uint8",
        "--compression", "jpeg:92",
        "-o", get_frame_output_path(spec, frame),
    ])
    return args


def parse_stats(output):
    """Return (max_diff, changed_fraction) from two --printstats blocks."""
    values = {}
    blocks = []
    for key, value in STATS_REGEX.findall(output):
        values[key] = float(value)
        if key == "Avg":
            blocks.append(values)
            values = {}
    if len(blocks) < 2:
        raise RuntimeError(f"Could not parse oiiotool stats:\n{output}")
    return blocks[0]["Max"], blocks[1]["Avg"]


def render_frames(spec_path, start, end):
    """Create diff frames for frames in <start, end> (Deadline task)."""
    spec = load_spec(spec_path)
    os.makedirs(os.path.join(spec["staging_dir"], "stats"), exist_ok=True)
    frames = [
        frame for frame in sorted(spec["frames"]) if start <= frame <= end
    ]
    total = len(frames)
    for idx, frame in enumerate(frames, 1):
        args = build_frame_args(spec, frame)
        output = run_subprocess(args, logger=log)
        max_diff, changed = parse_stats(output)
        stats = {
            "frame": frame,
            "max_diff": round(max_diff * 255.0, 2),
            "changed_percent": round(changed * 100.0, 4),
        }
        with open(get_frame_stats_path(spec, frame), "w") as stream:
            json.dump(stats, stream)
        log.info(
            f"Frame {frame}: {stats['changed_percent']}% pixels changed"
            f" (max diff {stats['max_diff']}/255)"
        )
        print(f"Progress: {int(idx * 100 / total)}%")


def summarize_stats(spec, stats_by_frame):
    """Human-readable summary used as the published version comment."""
    min_percent = float(spec["settings"]["min_changed_percent"])
    changed = [
        frame
        for frame, stats in sorted(stats_by_frame.items())
        if stats["changed_percent"] >= min_percent
    ]
    header = "Diff {} v{:03d} vs v{:03d}: ".format(
        spec["product_name"], spec["new_version"], spec["old_version"]
    )
    if not changed:
        return header + (
            "no visible changes in {} frames (threshold {}/255, "
            "min {}% pixels)."
        ).format(
            len(stats_by_frame), spec["settings"]["threshold"], min_percent
        )

    peak_frame = max(
        stats_by_frame, key=lambda f: stats_by_frame[f]["changed_percent"]
    )
    return header + (
        "{}/{} frames changed ({}). Most changed frame {} "
        "({:.2f}% pixels)."
    ).format(
        len(changed),
        len(stats_by_frame),
        frames_to_deadline_str(changed),
        peak_frame,
        stats_by_frame[peak_frame]["changed_percent"],
    )


def finalize(spec_path):
    """Make review mp4 + thumbnail and the farm publish metadata json."""
    spec = load_spec(spec_path)
    anatomy = spec["_anatomy"]
    staging_dir = spec["staging_dir"]
    frames = sorted(spec["frames"])

    stats_by_frame = {}
    missing = []
    for frame in frames:
        stats_path = get_frame_stats_path(spec, frame)
        if not os.path.exists(get_frame_output_path(spec, frame)) or (
            not os.path.exists(stats_path)
        ):
            missing.append(frame)
            continue
        with open(stats_path, "r") as stream:
            stats_by_frame[frame] = json.load(stream)
    if missing:
        raise RuntimeError(
            "Missing diff frames: {}".format(frames_to_deadline_str(missing))
        )

    summary = summarize_stats(spec, stats_by_frame)
    log.info(summary)

    basename = spec["file_basename"]
    padding = spec["frame_padding"]
    frame_files = [
        os.path.basename(get_frame_output_path(spec, frame))
        for frame in frames
    ]

    # Review mp4 (ftrack reviewable)
    mp4_name = f"{basename}_h264.mp4"
    is_contiguous = frames == list(range(frames[0], frames[-1] + 1))
    fps = str(spec["fps"])
    ffmpeg_args = get_ffmpeg_tool_args("ffmpeg", "-y", "-loglevel", "error")
    if is_contiguous:
        ffmpeg_args.extend([
            "-framerate", fps,
            "-start_number", str(frames[0]),
            "-i", os.path.join(staging_dir, f"{basename}.%0{padding}d.jpg"),
        ])
    else:
        # Frames with gaps - play existing frames back to back
        list_path = os.path.join(staging_dir, "ffmpeg_frames.txt")
        with open(list_path, "w") as stream:
            for name in frame_files:
                stream.write(f"file '{name}'\nduration {1.0 / float(fps)}\n")
        ffmpeg_args.extend(["-f", "concat", "-safe", "0", "-i", list_path])
    ffmpeg_args.extend([
        "-r", fps,
        "-vf", "scale='min(1920,iw)':-2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-crf", "18", "-preset", "medium",
        os.path.join(staging_dir, mp4_name),
    ])
    run_subprocess(ffmpeg_args, logger=log)

    # Thumbnail from the most changed frame
    peak_frame = max(
        stats_by_frame, key=lambda f: stats_by_frame[f]["changed_percent"]
    )
    thumbnail_name = f"{basename}_thumb.jpg"
    shutil.copyfile(
        get_frame_output_path(spec, peak_frame),
        os.path.join(staging_dir, thumbnail_name),
    )

    with open(os.path.join(staging_dir, "stats.json"), "w") as stream:
        json.dump(
            {"summary": summary, "frames": stats_by_frame}, stream, indent=1
        )

    rootless_staging = spec["staging_dir_rootless"]
    frame_start, frame_end = frames[0], frames[-1]
    common_repre = {
        "stagingDir": rootless_staging,
        "frameStart": frame_start,
        "frameEnd": frame_end,
        "fps": spec["fps"],
    }
    representations = [
        dict(
            common_repre,
            name="jpg", ext="jpg",
            files=frame_files if len(frame_files) > 1 else frame_files[0],
            tags=[],
        ),
        dict(
            common_repre,
            name="h264", ext="mp4", files=mp4_name,
            tags=["review", "ftrackreview"],
        ),
        {
            "name": "thumbnail",
            "ext": "jpg",
            "files": thumbnail_name,
            "stagingDir": rootless_staging,
            "thumbnail": True,
            "tags": ["thumbnail"],
        },
    ]
    instance = {
        "productName": spec["diff_product_name"],
        "productType": "render",
        "productBaseType": "render",
        # 'ftrack' explicitly: ftrack profiles usually require 'review',
        #   which would also trigger ExtractReview/burnins on our jpgs
        "families": ["render", "ftrack"],
        "folderPath": spec["folder_path"],
        "task": spec["task_name"],
        "frameStart": frame_start,
        "frameEnd": frame_end,
        "handleStart": 0,
        "handleEnd": 0,
        "frameStartHandle": frame_start,
        "frameEndHandle": frame_end,
        "fps": spec["fps"],
        "resolutionWidth": get_working_size(spec)[0],
        "resolutionHeight": get_working_size(spec)[1],
        "pixelAspect": 1,
        "comment": summary,
        "source": spec["source"],
        "version": spec["new_version"],
        "inputVersions": [spec["new_version_id"], spec["old_version_id"]],
        "representations": representations,
        DIFF_FLAG_KEY: True,
    }
    if spec.get("product_group"):
        instance["productGroup"] = spec["product_group"]
    publish_job = {
        "folderPath": spec["folder_path"],
        "frameStart": frame_start,
        "frameEnd": frame_end,
        "fps": spec["fps"],
        "source": spec["source"],
        "user": spec["user"],
        "comment": summary,
        "job": {},
        "version": spec["new_version"],
        "instances": [instance],
    }
    metadata_path = anatomy.fill_root(spec["publish_metadata_path"])
    with open(metadata_path, "w") as stream:
        json.dump(publish_job, stream, indent=4, sort_keys=True)
    log.info(f"Publish metadata written to '{metadata_path}'")
