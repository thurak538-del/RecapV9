import os
import sys
import uuid
import json
import asyncio
import threading
from pathlib import Path
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import settings_manager, DATA_DIR, CUSTOM_VOICES_DIR, CUSTOM_FONTS_DIR
from app.pipeline.orchestrator import PipelineOrchestrator, STAGES
from app.pipeline.tts_engine import TTSEngine
from app.pipeline.audio_mixer import AudioMixer
from app.pipeline.gpu_utils import get_active_encoder_name, check_gpu_nvenc_available, get_encoder_hardware_desc
from app.pipeline.gpu_pool import cuda_pool
from app.queue_manager import job_queue_manager, EDGE_MAX_CONCURRENT, VOXCPM_MAX_CONCURRENT
from app.voice_clone_manager import create_job as create_voice_clone_job, get_job as get_voice_clone_job, list_jobs as list_voice_clone_jobs, VOICE_CLONE_DIR
from app.pipeline.video_merger import merge_videos, PROFILES
from app.pipeline.video_splitter import plan_parts, export_parts, probe_video
import zipfile


def _font_family_from_file(font_path: Path) -> str:
    """Read the real family name; filename stems are not valid ASS font names."""
    try:
        import subprocess
        family = subprocess.check_output(
            ["fc-scan", "--format=%{family}", str(font_path)],
            text=True, stderr=subprocess.DEVNULL,
        ).strip().split(",")[0].strip()
        if family:
            return family
    except Exception:
        pass
    return font_path.stem

app = FastAPI(
    title="AI Video Recap Studio",
    description="Video Recap & Narration Pipeline",
    version="2.2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
MERGE_DIR = DATA_DIR / "merge_jobs"
MERGE_DIR.mkdir(parents=True, exist_ok=True)
SPLIT_DIR = DATA_DIR / "split_jobs"
SPLIT_DIR.mkdir(parents=True, exist_ok=True)

jobs_state: Dict[str, Dict[str, Any]] = {}
job_event_queues: Dict[str, List[asyncio.Queue]] = {}
split_runtime: Dict[str, Dict[str, Any]] = {}


class SettingsUpdateRequest(BaseModel):
    ai_mode: Optional[str] = None
    groq_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    target_language: Optional[str] = None
    source_language: Optional[str] = None
    gemini_prompt_mode: Optional[str] = None
    voice_engine: Optional[str] = None
    edge_tts_language: Optional[str] = None
    edge_tts_voice: Optional[str] = None
    edge_tts_rate_pct: Optional[int] = None
    short_pauses: Optional[bool] = None
    output_ratio: Optional[str] = None
    output_fill: Optional[str] = None
    production_zoom: Optional[float] = None
    production_flip: Optional[bool] = None
    production_overlays: Optional[Dict[str, Any]] = None
    voice_volume_pct: Optional[int] = None
    voxcpm_voice_path: Optional[str] = None
    voxcpm_voice_name: Optional[str] = None
    voxcpm_reference_text: Optional[str] = None
    font_color: Optional[str] = None
    font_size_px: Optional[int] = None
    font_style: Optional[str] = None
    subtitle_animation: Optional[str] = None
    subtitle_pos_x: Optional[float] = None
    subtitle_pos_y: Optional[float] = None
    auto_blur_subtitles: Optional[bool] = None
    auto_blur_padding_pct: Optional[float] = None
    output_resolution: Optional[str] = None
    preserve_original_background: Optional[bool] = None
    processing_mode: Optional[str] = None
    enable_4k_filter: Optional[bool] = None
    mirror_mode_7s: Optional[bool] = None


class JobCreateRequest(BaseModel):
    video_url: Optional[str] = None
    target_language: Optional[str] = None
    source_language: Optional[str] = "auto"
    edge_tts_rate_pct: Optional[int] = None
    short_pauses: Optional[bool] = None
    output_ratio: Optional[str] = None
    output_fill: Optional[str] = None
    production_zoom: Optional[float] = None
    production_flip: Optional[bool] = None
    production_overlays: Optional[Dict[str, Any]] = None
    voice_volume_pct: Optional[int] = None
    subtitle_pos_x: Optional[float] = None
    subtitle_pos_y: Optional[float] = None
    font_style: Optional[str] = None
    subtitle_animation: Optional[str] = None
    font_size_px: Optional[int] = None
    font_color: Optional[str] = None
    subtitle_enabled: Optional[bool] = True
    output_resolution: Optional[str] = None
    preserve_original_background: Optional[bool] = None
    processing_mode: Optional[str] = "recap"


class SplitPlanRequest(BaseModel):
    part_minutes: float = 3.0
    smart_cut: bool = True
    max_wait_seconds: float = 20.0


class SplitExportRequest(BaseModel):
    ratio: str = "Original"
    fit_mode: str = "blur-background"
    overlay_text: str = ""
    overlay_size: int = 48
    overlay_opacity: float = 1.0
    overlay_opacity: float = 1.0
    overlay_background: bool = True
    overlay_placement: str = "bottom"
    overlay_x: float = 50.0
    overlay_y: float = 82.0
    show_part_number: bool = True
    part_label: str = "Part {n}/{total}"
    label_placement: str = "top-right"
    label_x: float = 75.0
    label_y: float = 4.0


class TranslationEditRequest(BaseModel):
    segments: List[Dict[str, Any]]


@app.get("/api/settings")
async def get_settings():
    return settings_manager.get_all(mask=True)


@app.get("/api/merge/profiles")
async def get_merge_profiles():
    return {
        "profiles": {
            key: {**value, "target_mbps": value["target"] / 1_000_000, "max_mbps": value["maxrate"] / 1_000_000}
            for key, value in PROFILES.items()
        },
        "recommended": "tiktok1080",
    }


@app.post("/api/merge/export")
async def export_merged_video(
    files: List[UploadFile] = File(...),
    clip_specs: str = Form("[]"),
    profile: str = Form("tiktok1080"),
    layout: str = Form("fit"),
    fps: int = Form(30),
):
    if len(files) not in (2, 3):
        raise HTTPException(status_code=400, detail="Choose 2 or 3 video clips.")
    if profile not in PROFILES:
        raise HTTPException(status_code=400, detail="Unsupported TikTok export profile.")
    if layout not in {"fit", "crop", "stretch"}:
        raise HTTPException(status_code=400, detail="Unsupported layout mode.")
    try:
        specs = json.loads(clip_specs or "[]")
        if not isinstance(specs, list):
            raise ValueError("clip_specs must be a list")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid clip settings: {exc}") from exc
    job_dir = MERGE_DIR / f"merge_{uuid.uuid4().hex[:10]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    try:
        for index, upload in enumerate(files):
            suffix = Path(upload.filename or ".mp4").suffix.lower()
            if suffix not in {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}:
                raise HTTPException(status_code=400, detail=f"Unsupported video format: {suffix}")
            target = job_dir / f"input_{index}{suffix}"
            content = await upload.read()
            if not content:
                raise HTTPException(status_code=400, detail=f"Empty video file: {upload.filename}")
            target.write_bytes(content)
            paths.append(target)
        output = job_dir / "tiktok_export.mp4"
        result = merge_videos(paths, specs, output, profile_name=profile, layout=layout, fps=max(24, min(60, fps)))
        return {"status": "completed", "job_id": job_dir.name, "file_url": f"/api/merge/jobs/{job_dir.name}/file", **result}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/merge/jobs/{job_id}/file")
async def get_merged_video(job_id: str):
    safe_id = Path(job_id).name
    output = MERGE_DIR / safe_id / "tiktok_export.mp4"
    if not output.is_file():
        raise HTTPException(status_code=404, detail="Merged export not found.")
    return FileResponse(output, media_type="video/mp4", filename="tiktok_export.mp4")


@app.post("/api/split/upload")
async def upload_split_videos(files: List[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="Select at least one video.")
    job_dir = SPLIT_DIR / f"split_{uuid.uuid4().hex[:10]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    try:
        for index, upload in enumerate(files):
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}:
                raise HTTPException(status_code=400, detail=f"Unsupported video format: {suffix}")
            path = job_dir / f"input_{index:03d}{suffix}"
            content = await upload.read()
            if not content:
                raise HTTPException(status_code=400, detail=f"Empty video: {upload.filename}")
            path.write_bytes(content)
            info = probe_video(path)
            saved.append({"name": Path(upload.filename).name, "path": path.name, **info})
        (job_dir / "inputs.json").write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"job_id": job_dir.name, "videos": saved}
    except Exception:
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


@app.post("/api/split/{job_id}/plan")
async def create_split_plan(job_id: str, payload: SplitPlanRequest):
    job_dir = SPLIT_DIR / Path(job_id).name
    inputs_file = job_dir / "inputs.json"
    if not inputs_file.is_file():
        raise HTTPException(status_code=404, detail="Split upload session not found.")
    try:
        inputs = json.loads(inputs_file.read_text(encoding="utf-8"))
        plans = []
        for video in inputs:
            parts = await asyncio.to_thread(plan_parts, job_dir / video["path"], payload.part_minutes,
                                            payload.smart_cut, payload.max_wait_seconds)
            plans.append({"name": video["name"], "path": video["path"], "parts": parts})
        record = {"part_minutes": payload.part_minutes, "smart_cut": payload.smart_cut,
                  "max_wait_seconds": payload.max_wait_seconds, "videos": plans}
        (job_dir / "plan.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return record
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/split/{job_id}/export")
async def export_split_videos(job_id: str, payload: SplitExportRequest):
    safe_id = Path(job_id).name
    job_dir = SPLIT_DIR / safe_id
    plan_file = job_dir / "plan.json"
    inputs_file = job_dir / "inputs.json"
    if not plan_file.is_file() or not inputs_file.is_file():
        raise HTTPException(status_code=409, detail="Preview cuts first, then export.")
    current = split_runtime.get(safe_id, {})
    if current.get("status") == "running":
        raise HTTPException(status_code=409, detail="This split job is already running.")
    try:
        record = json.loads(plan_file.read_text(encoding="utf-8"))
        options = payload.model_dump()
        show_number = options.pop("show_part_number")
        if not show_number:
            options["part_label"] = ""
        stop_event = threading.Event()
        split_runtime[safe_id] = {"status": "running", "progress": 0.0, "message": "Starting export", "stop_event": stop_event}
        (job_dir / "split_parts.zip").unlink(missing_ok=True)
        threading.Thread(target=_run_split_export, args=(safe_id, job_dir, record, options, stop_event), daemon=True).start()
        return {"job_id": safe_id, "status": "running", "status_url": f"/api/split/{safe_id}/status"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _run_split_export(job_id: str, job_dir: Path, record: Dict[str, Any], options: Dict[str, Any], stop_event: threading.Event):
    try:
        total = sum(len(video["parts"]) for video in record["videos"])
        completed = 0
        for video_index, video in enumerate(record["videos"], 1):
            source = job_dir / video["path"]
            out_dir = job_dir / f"video_{video_index:03d}_{Path(video['name']).stem}_parts"
            def progress(message, pct, base=completed):
                split_runtime[job_id].update(progress=min(99.0, base + pct * len(video["parts"]) / 100.0), message=message)
            results = export_parts(source, out_dir, video["parts"], options, stop_event, progress)
            completed += len(results)
            if stop_event.is_set():
                split_runtime[job_id].update(status="stopped", message="Stopped safely; completed parts are saved.", progress=min(99.0, completed * 100 / max(total, 1)))
                return
        archive = job_dir / "split_parts.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for file in job_dir.glob("*_parts/part_*.mp4"):
                bundle.write(file, file.relative_to(job_dir))
        split_runtime[job_id].update(status="completed", progress=100.0, message="All parts exported.", file_url=f"/api/split/{job_id}/file", parts_count=total)
    except Exception as exc:
        split_runtime[job_id].update(status="failed", message=str(exc))


@app.get("/api/split/{job_id}/status")
async def get_split_status(job_id: str):
    safe_id = Path(job_id).name
    state = split_runtime.get(safe_id)
    if not state:
        archive = SPLIT_DIR / safe_id / "split_parts.zip"
        if archive.is_file():
            return {"status": "completed", "progress": 100.0, "file_url": f"/api/split/{safe_id}/file"}
        return {"status": "idle", "progress": 0.0}
    return {key: value for key, value in state.items() if key != "stop_event"}


@app.post("/api/split/{job_id}/stop")
async def stop_split_export(job_id: str):
    safe_id = Path(job_id).name
    state = split_runtime.get(safe_id)
    if not state or state.get("status") != "running":
        raise HTTPException(status_code=409, detail="No active split to stop.")
    state["stop_event"].set()
    state["message"] = "Stopping after the current part…"
    return {"status": "stopping"}


@app.get("/api/split/{job_id}/file")
async def download_split_archive(job_id: str):
    archive = SPLIT_DIR / Path(job_id).name / "split_parts.zip"
    if not archive.is_file():
        raise HTTPException(status_code=404, detail="Split export not found.")
    return FileResponse(archive, media_type="application/zip", filename="split_parts.zip")


@app.post("/api/settings")
async def update_settings(payload: SettingsUpdateRequest):
    cleaned = payload.model_dump(exclude_unset=True)
    saved = settings_manager.save(cleaned)
    return {
        "status": "success",
        "message": "Settings saved securely.",
        "settings": saved
    }


@app.get("/api/fonts")
async def get_fonts():
    """Return only the installed custom fonts in the requested stable order."""
    files = []
    if CUSTOM_FONTS_DIR.exists():
        files = [p for p in CUSTOM_FONTS_DIR.iterdir()
                 if p.suffix.lower() in (".ttf", ".otf")]
    priority = {"z10-cartoon.ttf": 0, "myanmarthuriya.ttf": 1}
    files.sort(key=lambda p: (priority.get(p.name.casefold(), 2), p.name.casefold()))
    fonts, font_files = [], {}
    for path in files:
        family = _font_family_from_file(path)
        if family.casefold() in {name.casefold() for name in fonts}:
            continue
        fonts.append(family)
        font_files[family] = f"/api/fonts/file/{path.name}"
    return {"fonts": fonts, "font_files": font_files}


@app.get("/api/fonts/file/{filename}")
async def get_font_file(filename: str):
    """Serve a selected custom font for the settings preview only."""
    requested = Path(filename).name
    if requested != filename or Path(filename).suffix.lower() not in (".ttf", ".otf"):
        raise HTTPException(status_code=400, detail="Invalid font filename.")
    font_path = CUSTOM_FONTS_DIR / requested
    if not font_path.is_file():
        raise HTTPException(status_code=404, detail="Font not found.")
    media_type = "font/ttf" if font_path.suffix.lower() == ".ttf" else "font/otf"
    return FileResponse(font_path, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/fonts/upload")
async def upload_custom_font(file: UploadFile = File(...)):
    """Allow user to upload any TTF/OTF font (e.g. Pyidaungsu, Masterpiece, etc.)."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename required.")

    ext = Path(file.filename).suffix.lower()
    if ext not in ('.ttf', '.otf'):
        raise HTTPException(status_code=400, detail="Only .ttf and .otf font files are supported.")

    safe_filename = Path(file.filename).name.replace(" ", "_")
    if not safe_filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    dest = CUSTOM_FONTS_DIR / safe_filename
    with open(dest, "wb") as f:
        f.write(await file.read())

    # Use the font's internal family name, not its filename stem. ASS/libass
    # resolves the family name from the font tables.
    font_name = _font_family_from_file(dest)
    try:
        import subprocess
        subprocess.run(["fc-cache", "-f", str(CUSTOM_FONTS_DIR)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    settings_manager.save({
        "font_style": font_name,
        "custom_font_name": file.filename,
        "custom_font_path": str(dest)
    })

    return {
        "status": "success",
        "font_name": font_name,
        "filename": file.filename,
        "path": str(dest)
    }


@app.get("/api/production-assets/{filename}")
async def get_production_asset(filename: str):
    safe = Path(filename).name
    asset = DATA_DIR / "production_assets" / safe
    if safe != filename or not asset.is_file():
        raise HTTPException(status_code=404, detail="Production asset not found.")
    return FileResponse(asset)


@app.post("/api/production-assets/logo")
async def upload_production_logo(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(status_code=400, detail="Logo must be PNG, JPG or WEBP.")
    data = await file.read()
    if not data or len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Logo must be a non-empty image smaller than 15 MB.")
    asset_dir = DATA_DIR / "production_assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    filename = f"logo_{uuid.uuid4().hex[:10]}{suffix}"
    (asset_dir / filename).write_bytes(data)
    return {"status": "success", "asset": filename}


@app.get("/api/languages")
async def get_target_languages():
    return {
        "languages": [
            {"code": "my", "name": "Burmese (မြန်မာဘာသာ)", "tts_locale": "my-MM"},
            {"code": "en", "name": "English", "tts_locale": "en-US"},
            {"code": "th", "name": "Thai (ထိုင်း)", "tts_locale": "th-TH"},
            {"code": "ja", "name": "Japanese (ဂျပန်)", "tts_locale": "ja-JP"},
            {"code": "ko", "name": "Korean (ကိုရီးယား)", "tts_locale": "ko-KR"},
            {"code": "zh", "name": "Chinese (တရုတ်)", "tts_locale": "zh-CN"},
            {"code": "es", "name": "Spanish (စပိန်)", "tts_locale": "es-ES"},
            {"code": "fr", "name": "French (ပြင်သစ်)", "tts_locale": "fr-FR"},
            {"code": "de", "name": "German (ဂျာမန်)", "tts_locale": "de-DE"},
            {"code": "it", "name": "Italian (အီတလီ)", "tts_locale": "it-IT"},
            {"code": "ru", "name": "Russian (ရုရှား)", "tts_locale": "ru-RU"},
            {"code": "vi", "name": "Vietnamese (ဗီယက်နမ်)", "tts_locale": "vi-VN"}
        ],
        "current_target": settings_manager.get("target_language", "my"),
        "current_mode": settings_manager.get("gemini_prompt_mode", "translate")
    }


@app.get("/api/voices")
async def list_voices():
    edge_catalog = await TTSEngine.get_edge_catalog()
    voxcpm_voices = TTSEngine.get_voxcpm_voices()
    return {
        "edge_languages": edge_catalog["languages"],
        "edge_voices_by_language": edge_catalog["voices_by_language"],
        "voxcpm_voices": voxcpm_voices,
        "current_engine": settings_manager.get("voice_engine", "edge_tts"),
        "current_edge_language": settings_manager.get("edge_tts_language", "my-MM"),
        "current_edge_voice": settings_manager.get("edge_tts_voice", "my-MM-ThihaNeural"),
        "current_voxcpm_voice_name": settings_manager.get("voxcpm_voice_name", "reference_speaker.wav"),
        "current_voxcpm_voice_path": settings_manager.get("voxcpm_voice_path", ""),
        "current_voxcpm_ref_text": settings_manager.get("voxcpm_reference_text", "")
    }


@app.post("/api/voices/upload-reference")
async def upload_reference_audio(
    file: UploadFile = File(...),
    reference_text: Optional[str] = Form(None)
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename required.")
    if not reference_text or not reference_text.strip():
        raise HTTPException(status_code=400, detail="Reference Text ထည့်ပြီးမှ audio တင်ပါ။ အသံဖိုင်ထဲက ပြောထားသော စာသားကို တိတိကျကျ ထည့်ပါ။")

    ext = Path(file.filename).suffix.lower()
    # Browser and Kaggle uploads commonly use AAC/Opus/WebM containers.
    allowed = ('.wav', '.mp3', '.m4a', '.flac', '.ogg', '.opus', '.aac', '.webm')
    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported audio format '{ext or '(none)'}'. Allowed: WAV, MP3, M4A, FLAC, OGG, OPUS, AAC, WEBM.")

    safe_name = f"ref_{uuid.uuid4().hex[:6]}_{Path(file.filename).name.replace(' ', '_')}"
    dest = CUSTOM_VOICES_DIR / safe_name
    try:
        contents = await file.read()
        if not contents:
            raise HTTPException(status_code=400, detail="The selected audio file is empty.")
        if len(contents) > 200 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Reference audio is too large. Maximum size is 200 MB.")
        CUSTOM_VOICES_DIR.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(contents)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not save reference audio: {exc}") from exc

    save_dict = {
        "voxcpm_voice_name": file.filename,
        "voxcpm_voice_path": str(dest)
    }
    if reference_text is not None:
        save_dict["voxcpm_reference_text"] = reference_text.strip()

    settings_manager.save(save_dict)

    return {
        "status": "success",
        "filename": file.filename,
        "path": str(dest),
        "reference_text": settings_manager.get("voxcpm_reference_text", "")
    }


@app.get("/api/voiceclone/jobs")
async def list_voice_clone_history():
    return {"jobs": list_voice_clone_jobs()}


@app.get("/api/voiceclone/jobs/{job_id}")
async def get_voice_clone_status(job_id: str):
    job = get_voice_clone_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Voice Clone job not found")
    return job


@app.post("/api/voiceclone/jobs")
async def create_voice_clone(
    reference_audio: UploadFile = File(...),
    reference_text: str = Form(...),
    script: str = Form(...),
):
    if not reference_audio.filename:
        raise HTTPException(status_code=400, detail="Reference audio file is required.")
    if not reference_text.strip():
        raise HTTPException(status_code=400, detail="Reference Text ထည့်ပါ။")
    if not script.strip():
        raise HTTPException(status_code=400, detail="ပြောစေချင်တဲ့စာသား ထည့်ပါ။")
    ext = Path(reference_audio.filename).suffix.lower()
    if ext not in (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".webm"):
        raise HTTPException(status_code=400, detail="Unsupported reference audio format.")
    reference_dir = VOICE_CLONE_DIR / "references"
    reference_dir.mkdir(parents=True, exist_ok=True)
    reference_path = reference_dir / f"ref_{uuid.uuid4().hex[:10]}{ext}"
    content = await reference_audio.read()
    if not content:
        raise HTTPException(status_code=400, detail="Reference audio is empty.")
    if len(content) > 200 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Reference audio is too large (200 MB maximum).")
    reference_path.write_bytes(content)
    job_id = create_voice_clone_job(str(reference_path), Path(reference_audio.filename).name, reference_text, script)
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/voiceclone/jobs/{job_id}/file")
async def download_voice_clone(job_id: str):
    job = get_voice_clone_job(job_id)
    if not job or job.get("status") != "completed":
        raise HTTPException(status_code=404, detail="Completed Voice Clone audio not found")
    output_path = Path(job.get("output_path", ""))
    if not output_path.is_file() or VOICE_CLONE_DIR not in output_path.parents:
        raise HTTPException(status_code=404, detail="Voice Clone audio file not found")
    return FileResponse(output_path, media_type="audio/wav", filename=f"voice_clone_{job_id}.wav")


@app.get("/api/gpu")
async def get_gpu_status():
    """Returns GPU hardware acceleration status and active video encoder."""
    has_gpu = check_gpu_nvenc_available()
    return {
        "gpu_accelerated": has_gpu,
        "encoder": get_active_encoder_name(),
        "hardware": get_encoder_hardware_desc(),
        "cuda_devices": cuda_pool.status(),
    }


@app.get("/api/queue")
async def get_queue_info():
    """Returns concurrent active jobs, engine slots, and pending jobs."""
    with job_queue_manager.lock:
        active_ids = list(job_queue_manager.active_jobs.keys())
        queued_ids = [item["job_id"] for item in job_queue_manager.pending_jobs]
    return {
        "active_job_id": active_ids[0] if active_ids else None,
        "active_job_ids": active_ids,
        "running_count": len(active_ids),
        "queued_count": len(queued_ids),
        "queued_job_ids": queued_ids,
        "edge_running": job_queue_manager.active_counts["edge_tts"],
        "edge_limit": EDGE_MAX_CONCURRENT,
        "voxcpm_running": job_queue_manager.active_counts["voxcpm2"],
        "voxcpm_limit": VOXCPM_MAX_CONCURRENT,
    }


@app.get("/api/jobs")
async def list_jobs():
    """Return all jobs (active + queued + history) sorted newest first."""
    return {"jobs": job_queue_manager.list_jobs()}


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Return current state of a single job."""
    job = job_queue_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@app.post("/api/jobs")
async def create_job(payload: JobCreateRequest):
    ai_mode = settings_manager.get("ai_mode", "local")
    story_requires_gemini = payload.processing_mode == "story"
    if (ai_mode != "local" or story_requires_gemini) and not settings_manager.get_gemini_key():
        raise HTTPException(status_code=400, detail="Gemini API Key ထည့်သွင်းပေးရန် လိုအပ်ပါသည်။ (Settings တွင် ထည့်ပါ)")

    if not payload.video_url:
        raise HTTPException(status_code=400, detail="Video Link ထည့်သွင်းပေးပါ။")
    updates = {}
    if payload.target_language:
        updates["target_language"] = payload.target_language
    if payload.source_language:
        updates["source_language"] = payload.source_language
    if payload.edge_tts_rate_pct is not None:
        updates["edge_tts_rate_pct"] = max(-50, min(100, payload.edge_tts_rate_pct))
    if payload.short_pauses is not None:
        updates["short_pauses"] = payload.short_pauses
    if payload.output_ratio in ("original", "16:9", "9:16", "1:1"):
        updates["output_ratio"] = payload.output_ratio
    if payload.output_fill in ("blur", "black", "crop"):
        updates["output_fill"] = payload.output_fill
    if payload.production_zoom is not None:
        updates["production_zoom"] = max(1.0, min(1.25, payload.production_zoom))
    if payload.production_flip is not None:
        updates["production_flip"] = payload.production_flip
    if payload.production_overlays is not None:
        updates["production_overlays"] = payload.production_overlays
    if payload.voice_volume_pct is not None:
        updates["voice_volume_pct"] = max(0, min(200, payload.voice_volume_pct))
    if payload.subtitle_pos_x is not None:
        updates["subtitle_pos_x"] = payload.subtitle_pos_x
    if payload.subtitle_pos_y is not None:
        updates["subtitle_pos_y"] = payload.subtitle_pos_y
    if payload.font_style:
        updates["font_style"] = payload.font_style
    if payload.font_size_px:
        updates["font_size_px"] = payload.font_size_px
    if payload.font_color:
        updates["font_color"] = payload.font_color
    if payload.subtitle_animation in ("none", "fade", "slide", "pop"):
        updates["subtitle_animation"] = payload.subtitle_animation
    if payload.output_resolution in ("1080p", "2k", "4k", "tiktok1080", "tiktok2k", "tiktok4k"):
        updates["output_resolution"] = payload.output_resolution
    if updates:
        settings_manager.save(updates)

    subtitle_enabled = payload.subtitle_enabled if payload.subtitle_enabled is not None else True

    job_id = job_queue_manager.submit_job(
        video_url=payload.video_url,
        uploaded_video_path=None,
        target_language=payload.target_language,
        enable_subtitles=subtitle_enabled,
        font_color=payload.font_color,
        font_size_px=payload.font_size_px,
        font_style=payload.font_style,
        subtitle_pos_x=payload.subtitle_pos_x,
        subtitle_pos_y=payload.subtitle_pos_y,
        subtitle_animation=payload.subtitle_animation,
        output_resolution=payload.output_resolution,
        preserve_original_background=(bool(payload.preserve_original_background) if payload.preserve_original_background is not None else bool(settings_manager.get("preserve_original_background", False))),
        processing_mode=payload.processing_mode if payload.processing_mode in ("recap", "story", "dubbing") else "recap"
        ,source_language=payload.source_language or settings_manager.get("source_language", "auto")
        ,edge_tts_rate_pct=updates.get("edge_tts_rate_pct", settings_manager.get("edge_tts_rate_pct", 20))
        ,short_pauses=updates.get("short_pauses", settings_manager.get("short_pauses", True))
        ,output_ratio=updates.get("output_ratio", settings_manager.get("output_ratio", "original"))
        ,output_fill=updates.get("output_fill", settings_manager.get("output_fill", "blur"))
        ,production_zoom=updates.get("production_zoom", settings_manager.get("production_zoom", 1.0))
        ,production_flip=updates.get("production_flip", settings_manager.get("production_flip", False))
        ,production_overlays=updates.get("production_overlays", settings_manager.get("production_overlays", {}))
        ,voice_volume_pct=updates.get("voice_volume_pct", settings_manager.get("voice_volume_pct", 100))
    )

    q_pos = job_queue_manager.get_queue_position(job_id)
    is_running_now = job_queue_manager.active_job_id == job_id

    return {
        "status": "success",
        "job_id": job_id,
        "queue_position": q_pos,
        "is_active": is_running_now
    }


@app.post("/api/jobs/upload")
async def create_job_upload(
    file: UploadFile = File(...),
    target_language: Optional[str] = Form(None),
    source_language: Optional[str] = Form("auto"),
    edge_tts_rate_pct: Optional[int] = Form(None),
    short_pauses: Optional[str] = Form(None),
    output_ratio: Optional[str] = Form(None),
    output_fill: Optional[str] = Form(None),
    production_zoom: Optional[float] = Form(None),
    production_flip: Optional[str] = Form(None),
    production_overlays: Optional[str] = Form(None),
    voice_volume_pct: Optional[int] = Form(None),
    subtitle_pos_x: Optional[float] = Form(None),
    subtitle_pos_y: Optional[float] = Form(None),
    font_style: Optional[str] = Form(None),
    font_size_px: Optional[int] = Form(None),
    font_color: Optional[str] = Form(None),
    subtitle_animation: Optional[str] = Form(None),
    subtitle_enabled: Optional[str] = Form(None),
    output_resolution: Optional[str] = Form(None),
    preserve_original_background: Optional[str] = Form(None),
    processing_mode: Optional[str] = Form("recap")
):
    ai_mode = settings_manager.get("ai_mode", "local")
    story_requires_gemini = processing_mode == "story"
    if (ai_mode != "local" or story_requires_gemini) and not settings_manager.get_gemini_key():
        raise HTTPException(status_code=400, detail="Gemini API Key ထည့်သွင်းပေးရန် လိုအပ်ပါသည်။ (Settings တွင် ထည့်ပါ)")

    updates = {}
    if target_language:
        updates["target_language"] = target_language
    if source_language:
        updates["source_language"] = source_language
    if edge_tts_rate_pct is not None:
        updates["edge_tts_rate_pct"] = max(-50, min(100, edge_tts_rate_pct))
    if short_pauses is not None:
        updates["short_pauses"] = short_pauses.lower() not in ("false", "0", "no")
    if output_ratio in ("original", "16:9", "9:16", "1:1"):
        updates["output_ratio"] = output_ratio
    if output_fill in ("blur", "black", "crop"):
        updates["output_fill"] = output_fill
    if production_zoom is not None:
        updates["production_zoom"] = max(1.0, min(1.25, production_zoom))
    if production_flip is not None:
        updates["production_flip"] = production_flip.lower() not in ("false", "0", "no")
    if voice_volume_pct is not None:
        updates["voice_volume_pct"] = max(0, min(200, voice_volume_pct))
    if production_overlays:
        try:
            decoded_overlays = json.loads(production_overlays)
            if not isinstance(decoded_overlays, dict):
                raise ValueError("production_overlays must be an object")
            updates["production_overlays"] = decoded_overlays
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid overlay settings: {exc}") from exc
    if subtitle_pos_x is not None:
        updates["subtitle_pos_x"] = subtitle_pos_x
    if subtitle_pos_y is not None:
        updates["subtitle_pos_y"] = subtitle_pos_y
    if font_style:
        updates["font_style"] = font_style
    if font_size_px:
        updates["font_size_px"] = font_size_px
    if font_color:
        updates["font_color"] = font_color
    if subtitle_animation in ("none", "fade", "slide", "pop"):
        updates["subtitle_animation"] = subtitle_animation
    if output_resolution in ("1080p", "2k", "4k", "tiktok1080", "tiktok2k", "tiktok4k"):
        updates["output_resolution"] = output_resolution
    if updates:
        settings_manager.save(updates)

    sub_enabled = True
    if subtitle_enabled is not None:
        sub_enabled = subtitle_enabled.lower() not in ("false", "0", "no")
    keep_background = (
        preserve_original_background.lower() not in ("false", "0", "no")
        if preserve_original_background is not None
        else bool(settings_manager.get("preserve_original_background", False))
    )

    # Stash uploaded file in dedicated uploads folder
    upload_dir = DATA_DIR / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_filename = Path(file.filename).name.replace(" ", "_")
    if not safe_filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    temp_dest = upload_dir / f"up_{uuid.uuid4().hex[:6]}_{safe_filename}"
    with open(temp_dest, "wb") as f:
        f.write(await file.read())

    job_id = job_queue_manager.submit_job(
        video_url=None,
        uploaded_video_path=temp_dest,
        target_language=target_language,
        enable_subtitles=sub_enabled,
        font_color=font_color,
        font_size_px=font_size_px,
        font_style=font_style,
        subtitle_pos_x=subtitle_pos_x,
        subtitle_pos_y=subtitle_pos_y,
        subtitle_animation=subtitle_animation,
        output_resolution=output_resolution,
        preserve_original_background=keep_background,
        processing_mode=processing_mode if processing_mode in ("recap", "story", "dubbing") else "recap"
        ,source_language=source_language or settings_manager.get("source_language", "auto")
        ,edge_tts_rate_pct=updates.get("edge_tts_rate_pct", settings_manager.get("edge_tts_rate_pct", 20))
        ,short_pauses=updates.get("short_pauses", settings_manager.get("short_pauses", True))
        ,output_ratio=updates.get("output_ratio", settings_manager.get("output_ratio", "original"))
        ,output_fill=updates.get("output_fill", settings_manager.get("output_fill", "blur"))
        ,production_zoom=updates.get("production_zoom", settings_manager.get("production_zoom", 1.0))
        ,production_flip=updates.get("production_flip", settings_manager.get("production_flip", False))
        ,production_overlays=updates.get("production_overlays", settings_manager.get("production_overlays", {}))
        ,voice_volume_pct=updates.get("voice_volume_pct", settings_manager.get("voice_volume_pct", 100))
    )

    q_pos = job_queue_manager.get_queue_position(job_id)
    is_running_now = job_queue_manager.active_job_id == job_id

    return {
        "status": "success",
        "job_id": job_id,
        "queue_position": q_pos,
        "is_active": is_running_now
    }


@app.get("/api/jobs/{job_id}/stream")
async def stream_job_events(job_id: str):
    job = job_queue_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    q: asyncio.Queue = asyncio.Queue()
    with job_queue_manager.lock:
        if job_id not in job_queue_manager.event_queues:
            job_queue_manager.event_queues[job_id] = []
        job_queue_manager.event_queues[job_id].append(q)

    async def event_generator():
        try:
            latest = job.get("latest_event")
            if latest:
                yield f"data: {json.dumps(latest)}\n\n"
                if latest.get("stage") in (STAGES[7], "မအောင်မြင်ပါ") or latest.get("status") in ("completed", "failed"):
                    return
            else:
                q_pos = job_queue_manager.get_queue_position(job_id)
                init_event = {
                    "job_id": job_id,
                    "stage": job.get("stage", "တန်းစီဇယားတွင် စောင့်ဆိုင်းနေပါသည်..."),
                    "stage_index": 0 if q_pos > 0 else 1,
                    "total_stages": len(STAGES),
                    "queue_position": q_pos,
                    "message": f"Queue တွင် စောင့်ဆိုင်းနေပါသည် (နံပါတ် #{q_pos})..." if q_pos > 0 else "စတင်ပြင်ဆင်နေပါသည်...",
                    "progress": job.get("progress", 0.0),
                    "status": job.get("status", "queued")
                }
                yield f"data: {json.dumps(init_event)}\n\n"
                if job.get("status") in ("completed", "failed"):
                    return

            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("stage") in (STAGES[7], "မအောင်မြင်ပါ") or event.get("status") in ("completed", "failed"):
                        break
                except asyncio.TimeoutError:
                    # Padding forces Cloudflare/browser proxies to flush the stream.
                    yield f": {('ping ' * 500)}\n\n"
        finally:
            with job_queue_manager.lock:
                if job_id in job_queue_manager.event_queues and q in job_queue_manager.event_queues[job_id]:
                    job_queue_manager.event_queues[job_id].remove(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )



@app.get("/api/jobs/{job_id}/files/{filename}")
async def get_job_file(job_id: str, filename: str):
    job_dir = JOBS_DIR / job_id
    file_path = (job_dir / filename).resolve()
    if job_dir.resolve() not in file_path.parents:
        raise HTTPException(status_code=400, detail="Invalid file path.")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File {filename} not found.")

    media_type = "application/octet-stream"
    if filename.endswith(".mp4"):
        media_type = "video/mp4"
    elif filename.endswith(".wav"):
        media_type = "audio/wav"
    elif filename.endswith(".json"):
        media_type = "application/json"
    elif filename.endswith(".txt") or filename.endswith(".srt") or filename.endswith(".ass"):
        media_type = "text/plain; charset=utf-8"

    return FileResponse(file_path, media_type=media_type)


@app.get("/api/jobs/{job_id}/translations")
async def get_job_translations(job_id: str):
    safe_id = Path(job_id).name
    source = JOBS_DIR / safe_id / "processed_transcript.json"
    if not source.is_file():
        raise HTTPException(status_code=404, detail="Translated segments are not available for this job.")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Could not read translated segments.") from exc
    transcript_file = JOBS_DIR / safe_id / "transcript.json"
    source_map = {}
    if transcript_file.is_file():
        try:
            source_data = json.loads(transcript_file.read_text(encoding="utf-8"))
            source_map = {str(item.get("id")): item.get("text", "") for item in source_data.get("segments", [])}
        except (OSError, ValueError, TypeError):
            source_map = {}
    items = [{**item, "source_text": source_map.get(str(item.get("id")), "")} for item in data.get("segments", [])]
    return {"segments": items, "full_text": data.get("full_text", "")}


@app.post("/api/jobs/{job_id}/translations")
async def save_job_translations(job_id: str, payload: TranslationEditRequest):
    safe_id = Path(job_id).name
    source = JOBS_DIR / safe_id / "processed_transcript.json"
    if not source.is_file():
        raise HTTPException(status_code=404, detail="Translated segments are not available for this job.")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
        original = data.get("segments", [])
        if len(payload.segments) != len(original):
            raise ValueError("Segment count must not change.")
        checked = []
        for expected, edited in zip(original, payload.segments):
            text = str(edited.get("text", "")).strip()
            if not text or str(edited.get("id")) != str(expected.get("id")):
                raise ValueError("Each segment needs its original id and non-empty translated text.")
            if abs(float(edited.get("start", -1)) - float(expected.get("start", -2))) > 0.01 or abs(float(edited.get("end", -1)) - float(expected.get("end", -2))) > 0.01:
                raise ValueError("Segment timestamps are immutable.")
            checked.append({**expected, "text": text})
        data["segments"] = checked
        data["full_text"] = "\n".join(item["text"] for item in checked)
        source.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": "saved", "segments": len(checked)}
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/render-edited")
async def render_edited_job(job_id: str):
    safe_id = Path(job_id).name
    job_dir = JOBS_DIR / safe_id
    source = job_dir / "processed_transcript.json"
    videos = list(job_dir.glob("downloaded_video.*"))
    if not source.is_file() or not videos:
        raise HTTPException(status_code=404, detail="The completed source video or translated text is missing.")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
        segments = data.get("segments", [])
        if not segments:
            raise ValueError("No translated segments to render.")
        tts_engine = TTSEngine()
        audio, synced = await asyncio.to_thread(
            tts_engine.generate, segments, job_dir,
            settings_manager.get("voice_engine", "edge_tts"),
            settings_manager.get("edge_tts_voice", "my-MM-ThihaNeural"),
            settings_manager.get("voxcpm_voice_path", "") or None,
            settings_manager.get("voxcpm_reference_text", "") or None,
            None,
            int(settings_manager.get("edge_tts_rate_pct", 20)),
            bool(settings_manager.get("short_pauses", True)),
        )
        mixer = AudioMixer()
        summary_path = job_dir / "job_summary.json"
        artifacts = json.loads(summary_path.read_text(encoding="utf-8")).get("artifacts", {}) if summary_path.is_file() else {}
        bg_rel = artifacts.get("background_audio")
        background = job_dir / bg_rel if bg_rel else None
        dubbed = await asyncio.to_thread(
            mixer.mix, videos[0], audio, job_dir / "edited_dubbed.mp4",
            settings_manager.get("output_resolution", "tiktok1080"), background,
            False, False, synced,
            settings_manager.get("output_ratio", "original"), settings_manager.get("output_fill", "blur"),
            float(settings_manager.get("production_zoom", 1.0)), bool(settings_manager.get("production_flip", False)),
            int(settings_manager.get("voice_volume_pct", 100)),
        )
        from app.pipeline.production_overlays import apply_production_overlays
        overlay_options = dict(settings_manager.get("production_overlays", {}))
        for asset_key in ("logo_path", "font_path"):
            if overlay_options.get(asset_key):
                candidate = DATA_DIR / "production_assets" / Path(str(overlay_options[asset_key])).name
                overlay_options[asset_key] = str(candidate) if candidate.is_file() else ""
        dubbed = await asyncio.to_thread(
            apply_production_overlays, dubbed, job_dir / "edited_with_overlays.mp4", overlay_options
        )
        if bool(settings_manager.get("enable_subtitles", True)):
            burner = SubtitleBurner()
            final = await asyncio.to_thread(
                burner.burn, video_path=dubbed, segments=synced, output_path=job_dir / "final_video.mp4",
                font_color=settings_manager.get("font_color", "#FFFFFF"),
                font_size_px=int(settings_manager.get("font_size_px", 70)),
                font_style=settings_manager.get("font_style", "Z10-Cartoon"),
                pos_x_pct=float(settings_manager.get("subtitle_pos_x", 50)),
                pos_y_pct=float(settings_manager.get("subtitle_pos_y", 82)),
                subtitle_animation=settings_manager.get("subtitle_animation", "fade"),
            )
        else:
            import shutil
            final = job_dir / "final_video.mp4"
            shutil.copy2(dubbed, final)
        return {"status": "completed", "video_url": f"/api/jobs/{safe_id}/files/final_video.mp4"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Edited video render failed: {exc}") from exc


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
