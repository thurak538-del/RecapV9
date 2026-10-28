"""README Splitter: duration probing, silence-aware planning, and video exports.

Plans are dictionaries with ``start``, ``end`` and ``duration`` in seconds.
Export options accept ratio Original/16:9/9:16/1:1 and fit_mode
blur-background/black-bars/crop-to-fill. FFmpeg and ffprobe must be installed.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.pipeline.gpu_utils import get_video_encoder_args

_MIN_PART_SECONDS = 0.5
_SILENCE_DB = "-35dB"
_SILENCE_MIN = 0.35


def _require_tool(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise RuntimeError(f"Required executable not found: {name}")
    return executable


def probe_video(path: os.PathLike | str) -> Dict[str, Any]:
    """Return duration, dimensions, and audio presence for a valid video."""
    video_path = Path(path)
    if not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    result = subprocess.run(
        [_require_tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video_path)],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise ValueError(f"Unable to probe video: {result.stderr[-1000:]}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("ffprobe returned invalid metadata") from exc
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float((data.get("format") or {}).get("duration") or (video or {}).get("duration") or 0)
    if video is None or not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"Not a valid video file: {video_path.name}")
    return {"duration": duration, "has_audio": audio is not None,
            "width": int(video.get("width") or 0), "height": int(video.get("height") or 0)}


def _silence_boundaries(path: Path, duration: float) -> List[float]:
    """Run FFmpeg silencedetect and return midpoints of qualifying gaps."""
    command = [_require_tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path),
               "-vn", "-af", f"silencedetect=noise={_SILENCE_DB}:d={_SILENCE_MIN}", "-f", "null", "-"]
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        # No audio or unsupported audio filter: planning can still use exact cuts.
        return []
    starts: List[float] = []
    boundaries: List[float] = []
    for line in result.stderr.splitlines():
        match = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if match:
            starts.append(max(0.0, float(match.group(1))))
        match = re.search(r"silence_end:\s*([\d.]+)", line)
        if match:
            end = float(match.group(1))
            start = starts.pop(0) if starts else end
            if end > start:
                boundaries.append((start + end) / 2.0)
    return [min(duration, max(0.0, x)) for x in boundaries]


def plan_parts(path: os.PathLike | str, part_minutes: float, smart_cut: bool = False,
               max_wait_seconds: float = 20) -> List[Dict[str, float]]:
    """Plan contiguous parts; optional cuts move to the nearest nearby silence."""
    target = float(part_minutes) * 60.0
    wait = float(max_wait_seconds)
    if not math.isfinite(target) or not 0.5 <= float(part_minutes) <= 60:
        raise ValueError("part_minutes must be between 0.5 and 60")
    if not math.isfinite(wait) or wait < 0:
        raise ValueError("max_wait_seconds must be a finite non-negative number")
    video_path = Path(path)
    info = probe_video(video_path)
    duration = info["duration"]
    silences = _silence_boundaries(video_path, duration) if smart_cut and info["has_audio"] else []
    cuts = [0.0]
    cursor = 0.0
    # Each desired cut is measured from the previous actual cut to avoid a tiny tail.
    while duration - cursor > target + 1e-6:
        desired = cursor + target
        choices = [point for point in silences
                   if cursor + _MIN_PART_SECONDS <= point < duration - _MIN_PART_SECONDS
                   and abs(point - desired) <= wait]
        boundary = min(choices, key=lambda point: (abs(point - desired), point)) if choices else desired
        if boundary <= cursor + _MIN_PART_SECONDS:
            boundary = desired
        cuts.append(boundary)
        cursor = boundary
    cuts.append(duration)
    parts = [{"start": round(a, 3), "end": round(b, 3), "duration": round(b-a, 3)}
             for a, b in zip(cuts, cuts[1:])]
    # Avoid an awkward very short tail by merging it into its predecessor. A merged
    # part may exceed the target; this is safer than creating a near-empty segment.
    threshold = min(target / 4.0, 20.0)
    if len(parts) > 1 and parts[-1]["duration"] < threshold:
        parts[-2]["end"] = parts[-1]["end"]
        parts[-2]["duration"] = round(parts[-2]["end"] - parts[-2]["start"], 3)
        parts.pop()
    return parts


def _ratio_canvas(ratio: str, source_w: int, source_h: int) -> Optional[Tuple[int, int]]:
    value = str(ratio or "Original").strip().lower()
    if value == "original":
        return None
    if value not in {"16:9", "9:16", "1:1"}:
        raise ValueError("ratio must be Original, 16:9, 9:16, or 1:1")
    # Keep output practical while retaining the requested aspect ratio.
    if value == "16:9":
        return (1920, 1080) if source_w >= source_h else (1280, 720)
    if value == "9:16":
        return 1080, 1920
    return 1080, 1080


def _escape_filter_path(path: Path) -> str:
    # FFmpeg filter arguments use ':' as a separator even on Unix.
    return str(path.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _text_filter(text_path: Path, size: int, y: str, opacity: float, background: bool) -> str:
    alpha = max(0.0, min(1.0, float(opacity)))
    opts = (f"textfile='{_escape_filter_path(text_path)}':expansion=none:fontsize={size}:"
            f"fontcolor=white@{alpha:.3f}:x=(w-text_w)/2:y={y}:"
            f"borderw=2:bordercolor=black@{alpha:.3f}")
    if background:
        opts += f":box=1:boxcolor=black@{alpha * 0.65:.3f}:boxborderw=12"
    return "drawtext=" + opts


def _build_video_filter(width: int, height: int, canvas: Optional[Tuple[int, int]], fit_mode: str,
                        overlay: bool, label: bool, text_files: Sequence[Path], options: Dict[str, Any]) -> str:
    mode = str(fit_mode or "black-bars").lower()
    if mode not in {"blur-background", "black-bars", "crop-to-fill"}:
        raise ValueError("fit_mode must be blur-background, black-bars, or crop-to-fill")
    filters: List[str] = []
    if canvas is None:
        filters.append("setsar=1")
    else:
        cw, ch = canvas
        if mode == "crop-to-fill":
            filters += [f"scale={cw}:{ch}:force_original_aspect_ratio=increase:flags=lanczos", f"crop={cw}:{ch}"]
        elif mode == "blur-background":
            # A split is required to make a blurred, cover-scaled background.
            return (f"split=2[bg0][fg0];[bg0]scale={cw}:{ch}:force_original_aspect_ratio=increase:flags=lanczos,"
                    f"crop={cw}:{ch},boxblur=24:8[bg];[fg0]scale={cw}:{ch}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
                    f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1" + _append_drawtexts(overlay, label, text_files, options))
        else:
            filters += [f"scale={cw}:{ch}:force_original_aspect_ratio=decrease:flags=lanczos",
                        f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black"]
        filters.append("setsar=1")
    base = ",".join(filters)
    suffix = _append_drawtexts(overlay, label, text_files, options)
    return base + suffix


def _append_drawtexts(overlay: bool, label: bool, text_files: Sequence[Path], options: Dict[str, Any]) -> str:
    filters = []
    if overlay:
        pos = str(options.get("overlay_placement", "bottom")).lower()
        y = {"top": "h*0.06", "center": "(h-text_h)/2", "bottom": "h*0.82"}.get(pos)
        if y is None:
            raise ValueError("overlay_placement must be top, center, or bottom")
        x = "(w-text_w)/2"
        if "overlay_x" in options:
            x = f"w*{max(0.0, min(100.0, float(options['overlay_x'])))/100:.5f}"
        if "overlay_y" in options:
            y = f"h*{max(0.0, min(100.0, float(options['overlay_y'])))/100:.5f}"
        size = int(options.get("overlay_size", 48))
        opacity = float(options.get("overlay_opacity", 1.0))
        if not 8 <= size <= 300 or not 0 <= opacity <= 1:
            raise ValueError("overlay_size must be 8..300 and overlay_opacity 0..1")
        filters.append(_text_filter(text_files[0], size, y, opacity, bool(options.get("overlay_background", True))).replace("x=(w-text_w)/2", f"x={x}"))
    if label:
        pos = str(options.get("label_placement", "top-right")).lower()
        xy = {"top-left": ("w*0.04", "h*0.04"), "top-right": ("w-text_w-w*0.04", "h*0.04"),
              "bottom-left": ("w*0.04", "h-text_h-h*0.04"), "bottom-right": ("w-text_w-w*0.04", "h-text_h-h*0.04")}.get(pos)
        if xy is None:
            raise ValueError("label_placement must be top-left, top-right, bottom-left, or bottom-right")
        if "label_x" in options:
            xy = (f"w*{max(0.0, min(100.0, float(options['label_x'])))/100:.5f}", xy[1])
        if "label_y" in options:
            xy = (xy[0], f"h*{max(0.0, min(100.0, float(options['label_y'])))/100:.5f}")
        filters.append(f"drawtext=textfile='{_escape_filter_path(text_files[1])}':expansion=none:fontsize={int(options.get('label_size', 32))}:fontcolor=white:borderw=2:bordercolor=black:x={xy[0]}:y={xy[1]}")
    return ("," if filters else "") + ",".join(filters)


def export_parts(path: os.PathLike | str, output_dir: os.PathLike | str,
                 plan: List[Dict[str, Any]], options: Optional[Dict[str, Any]] = None,
                 stop_event: Optional[threading.Event] = None,
                 progress_callback: Optional[Callable[..., None]] = None) -> List[Dict[str, Any]]:
    """Render each planned segment to MP4; callback receives (message, percent)."""
    options = dict(options or {})
    video_path = Path(path)
    info = probe_video(video_path)
    if not isinstance(plan, list) or not plan:
        raise ValueError("plan must be a non-empty list of part dictionaries")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    signature = hashlib.sha256(json.dumps({"plan": plan, "options": options}, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
    manifest = output / ".split_manifest.json"
    old_signature = None
    if manifest.is_file():
        try:
            old_signature = json.loads(manifest.read_text(encoding="utf-8")).get("signature")
        except (OSError, ValueError, TypeError):
            old_signature = None
    if old_signature != signature:
        for stale in output.glob("part_*.mp4"):
            stale.unlink(missing_ok=True)
        manifest.write_text(json.dumps({"signature": signature}, indent=2), encoding="utf-8")
    canvas = _ratio_canvas(str(options.get("ratio", "Original")), info["width"], info["height"])
    overlay_text = str(options.get("overlay_text", ""))
    label_template = str(options.get("part_label", ""))
    # Validate placement / numeric text styles before creating outputs.
    overlay = bool(overlay_text.strip())
    label = bool(label_template.strip())
    results: List[Dict[str, Any]] = []
    for index, item in enumerate(plan, 1):
        if stop_event is not None and stop_event.is_set():
            break
        try:
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid part plan at index {index}") from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or end > info["duration"] + 0.05:
            raise ValueError(f"Invalid part bounds at index {index}")
        duration = min(info["duration"], end) - start
        destination = output / f"part_{index:03d}.mp4"
        if destination.is_file() and destination.stat().st_size > 0:
            try:
                rendered_duration = probe_video(destination)["duration"]
                if abs(rendered_duration - duration) <= 0.2:
                    results.append({"path": str(destination), "start": round(start, 3), "end": round(end, 3), "duration": round(duration, 3), "skipped": True})
                    if progress_callback:
                        progress_callback(f"Skipped completed part {index}/{len(plan)}", 100.0 * index / len(plan))
                    continue
            except Exception:
                pass
            destination.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix="recap-split-") as tmp:
            temp = Path(tmp)
            files: List[Path] = []
            if overlay:
                f = temp / "overlay.txt"
                f.write_text(overlay_text, encoding="utf-8")
                files.append(f)
            if label:
                burmese_digits = str.maketrans("0123456789", "၀၁၂၃၄၅၆၇၈၉")
                use_mm = "{mm}" in label_template
                total_text = str(len(plan)).translate(burmese_digits) if use_mm else str(len(plan))
                label_text = label_template.replace("{n}", str(index)).replace("{mm}", str(index).translate(burmese_digits)).replace("{total}", total_text)
                f = temp / "label.txt"
                f.write_text(label_text, encoding="utf-8")
                files.append(f)
            graph = _build_video_filter(info["width"], info["height"], canvas,
                                        str(options.get("fit_mode", "black-bars")), overlay, label, files, options)
            command = [_require_tool("ffmpeg"), "-hide_banner", "-y", "-ss", f"{start:.6f}", "-i", str(video_path),
                       "-t", f"{duration:.6f}", "-map", "0:v:0", "-vf", graph]
            if info["has_audio"]:
                command += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k", "-af", "aresample=async=1:first_pts=0"]
            else:
                command += ["-an"]
            command += get_video_encoder_args(cq=19, crf=18)
            command += ["-t", f"{duration:.6f}", "-movflags", "+faststart", str(destination)]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(f"Export failed for part {index}: {result.stderr[-2500:]}")
        entry = {"path": str(destination), "start": round(start, 3), "end": round(end, 3), "duration": round(duration, 3)}
        results.append(entry)
        if progress_callback:
            progress_callback(f"Exported part {index}/{len(plan)}", 100.0 * index / len(plan))
    return results
