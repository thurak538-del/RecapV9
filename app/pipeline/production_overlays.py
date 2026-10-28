"""Apply draggable user production overlays and privacy blur rectangles with FFmpeg."""
from __future__ import annotations
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from app.pipeline.gpu_utils import get_video_encoder_args


def _escape(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _probe_size(path: Path) -> tuple[int, int]:
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(path)], capture_output=True, text=True, check=True)
    stream = json.loads(result.stdout)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def apply_production_overlays(video_path: Path, output_path: Path, options: Optional[Dict[str, Any]] = None) -> Path:
    """Render {title, extra_text, logo_path, blur_boxes} over an MP4.

    Coordinates and box dimensions are percentages of the final frame, so the
    same saved configuration remains valid across output aspect ratios.
    """
    options = dict(options or {})
    video_path, output_path = Path(video_path), Path(output_path)
    if not any((str(options.get("title", "")).strip(), str(options.get("extra_text", "")).strip(), options.get("logo_path"), options.get("blur_boxes"))):
        return video_path
    width, height = _probe_size(video_path)
    boxes = options.get("blur_boxes") or []
    with tempfile.TemporaryDirectory(prefix="recap-overlays-") as td:
        temp = Path(td)
        filters: List[str] = []
        current = "0:v"
        for i, box in enumerate(boxes):
            try:
                x, y = [max(0.0, min(99.0, float(box[k]))) for k in ("x", "y")]
                w, h = [max(1.0, min(100.0, float(box[k]))) for k in ("w", "h")]
            except (KeyError, TypeError, ValueError):
                continue
            x0, y0 = f"iw*{x/100:.5f}", f"ih*{y/100:.5f}"
            bw, bh = f"iw*{w/100:.5f}", f"ih*{h/100:.5f}"
            filters += [f"[{current}]split[base{i}][roi{i}]",
                        f"[roi{i}]crop=w={bw}:h={bh}:x={x0}:y={y0},boxblur=luma_radius=8:chroma_radius=4:luma_power=2:chroma_power=2[blur{i}]",
                        f"[base{i}][blur{i}]overlay=x=W*{x/100:.5f}:y=H*{y/100:.5f}[vblur{i}]"]
            current = f"vblur{i}"
        title = str(options.get("title", "")).strip()
        extra = str(options.get("extra_text", "")).strip()
        for key, text in (("title", title), ("extra", extra)):
            if not text:
                continue
            textfile = temp / f"{key}.txt"; textfile.write_text(text, encoding="utf-8")
            x = max(0.0, min(100.0, float(options.get(f"{key}_x", 50))))
            y = max(0.0, min(100.0, float(options.get(f"{key}_y", 12 if key == "title" else 82))))
            size = max(12, min(220, int(options.get(f"{key}_size", 64 if key == "title" else 42))))
            font = str(options.get("font_path", ""))
            font_opt = f":fontfile='{_escape(Path(font))}'" if font and Path(font).is_file() else ""
            label = f"vtext{len(filters)}"
            filters.append(f"[{current}]drawtext=textfile='{_escape(textfile)}':expansion=none{font_opt}:fontsize={size}:fontcolor=white:borderw=3:bordercolor=black:x=w*{x/100:.5f}-text_w/2:y=h*{y/100:.5f}[{label}]")
            current = label
        logo = Path(str(options.get("logo_path") or ""))
        inputs = ["-i", str(video_path)]
        if logo.is_file():
            inputs += ["-i", str(logo)]
            scale_pct = max(5.0, min(45.0, float(options.get("logo_width_pct", 16))))
            opacity = max(0.0, min(1.0, float(options.get("logo_opacity", 1))))
            lx = max(0.0, min(100.0, float(options.get("logo_x", 82))))
            ly = max(0.0, min(100.0, float(options.get("logo_y", 8))))
            filters += [f"[1:v]scale=w={width*scale_pct/100:.0f}:-1,format=rgba,colorchannelmixer=aa={opacity:.3f}[logo]"]
            label = "vlogo"
            filters.append(f"[{current}][logo]overlay=x=(W-w)*{lx/100:.5f}:y=(H-h)*{ly/100:.5f}[{label}]")
            current = label
        if not filters:
            return video_path
        graph = ";".join(filters + [f"[{current}]null[vout]"])
        cmd = ["ffmpeg", "-hide_banner", "-y", *inputs, "-filter_complex", graph, "-map", "[vout]", "-map", "0:a?", *get_video_encoder_args(cq=19, crf=18), "-c:a", "copy", "-movflags", "+faststart", str(output_path)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"Overlay rendering failed: {result.stderr[-2000:]}")
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("Overlay rendering produced no video.")
    return output_path
