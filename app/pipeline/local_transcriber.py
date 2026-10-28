"""Offline transcription using faster-whisper with a pre-downloaded Kaggle model."""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class LocalWhisperTranscriber:
    _models: Dict[str, Any] = {}

    def __init__(self, progress_callback: Optional[Callable[[str, float], None]] = None):
        self.progress_callback = progress_callback

    def _load_model(self, language: str = "auto"):
        from faster_whisper import WhisperModel

        language_code = str(language or "auto").strip().lower()
        if language_code in {"en", "english"}:
            default_model = os.getenv("RECAP_WHISPER_MODEL_EN", "small.en")
        elif language_code in {"auto", "detect", ""}:
            default_model = os.getenv("RECAP_WHISPER_MODEL_DETECT", "base")
        else:
            default_model = os.getenv("RECAP_WHISPER_MODEL_OTHER", "medium")
        model_path = os.getenv("RECAP_WHISPER_MODEL", default_model)
        device = os.getenv("RECAP_WHISPER_DEVICE", "cuda")
        device_index = int(os.getenv("RECAP_WHISPER_DEVICE_INDEX", "0"))
        cache_key = f"{model_path}|{device}|{device_index}|{os.getenv('RECAP_WHISPER_COMPUTE', 'float16')}"
        if cache_key not in self._models:
            compute_type = os.getenv("RECAP_WHISPER_COMPUTE", "float16" if device == "cuda" else "int8")
            self._models[cache_key] = WhisperModel(
                model_path,
                device=device,
                compute_type=compute_type,
                device_index=device_index,
                download_root=os.getenv("HF_HOME", "/kaggle/working/huggingface"),
            )
        return self._models[cache_key]

    def transcribe(self, audio_path: Path, output_dir: Optional[Path] = None, source_language: str = "auto") -> Dict[str, Any]:
        audio_path = Path(audio_path)
        output_dir = Path(output_dir or audio_path.parent)
        output_dir.mkdir(parents=True, exist_ok=True)
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        if self.progress_callback:
            self.progress_callback("Local Whisper model ကို load လုပ်နေပါသည်...", 10.0)

        requested_language = str(source_language or "auto").strip().lower()
        model = self._load_model(requested_language)
        # faster-whisper's path decoder passes ``metadata_errors`` to PyAV.
        # Some Kaggle/PyAV combinations removed that keyword from av.open(),
        # so decode our already-normalized 16 kHz WAV with soundfile and pass
        # samples directly. This also avoids a second FFmpeg/PyAV decode path.
        try:
            import soundfile as sf
            import numpy as np

            audio_samples, _sample_rate = sf.read(str(audio_path), dtype="float32", always_2d=False)
            if getattr(audio_samples, "ndim", 1) > 1:
                audio_samples = audio_samples.mean(axis=1)
            audio_input = np.asarray(audio_samples, dtype=np.float32)
        except Exception:
            # Keep a path fallback for installations without soundfile.
            audio_input = str(audio_path)
        language = None if requested_language in {"auto", "detect", ""} else requested_language
        language = {"english": "en", "chinese": "zh", "mandarin": "zh", "hindi": "hi", "urdu": "ur", "korean": "ko"}.get(language, language)
        if language is None and not isinstance(audio_input, str):
            # Detect language from no more than the first eight minutes.
            sample = audio_input[:8 * 60 * 16000]
            probe_segments, probe_info = model.transcribe(sample, beam_size=1, vad_filter=True,
                                                           condition_on_previous_text=False, word_timestamps=False)
            for _ in probe_segments:
                pass
            language = getattr(probe_info, "language", None)
        if language == "ur":
            # The README treats Urdu auto-detection as Hindi for this workflow.
            language = "hi"
        if requested_language in {"auto", "detect", ""}:
            model = self._load_model("en" if language == "en" else "other")
        segments_iter, info = model.transcribe(
            audio_input,
            language=language,
            beam_size=int(os.getenv("RECAP_WHISPER_BEAM_SIZE", "5")),
            vad_filter=True,
            condition_on_previous_text=True,
            word_timestamps=True,
        )
        segments: List[Dict[str, Any]] = []
        text_parts: List[str] = []
        for idx, segment in enumerate(segments_iter):
            text = (segment.text or "").strip()
            if not text:
                continue
            words = getattr(segment, "words", None) or []
            if words:
                group = []
                group_start = None
                group_end = 0.0
                for word in words:
                    token = str(getattr(word, "word", "") or "")
                    if not token.strip():
                        continue
                    start = float(getattr(word, "start", segment.start) or segment.start)
                    end = float(getattr(word, "end", start) or start)
                    if group_start is None:
                        group_start = start
                    group.append(token)
                    group_end = end
                    sentence_end = token.rstrip().endswith((".", "?", "!", "。", "！", "？"))
                    phrase_break = token.rstrip().endswith((",", ";", "，", "、")) and group_end - group_start >= 4.2
                    if sentence_end or phrase_break or group_end - group_start >= 5.6:
                        chunk = "".join(group).strip()
                        if chunk:
                            segments.append({"id": len(segments), "start": group_start, "end": group_end, "text": chunk})
                            text_parts.append(chunk)
                        group, group_start = [], None
                if group and group_start is not None:
                    chunk = "".join(group).strip()
                    if chunk:
                        segments.append({"id": len(segments), "start": group_start, "end": group_end, "text": chunk})
                        text_parts.append(chunk)
            else:
                segments.append({"id": len(segments), "start": float(segment.start), "end": float(segment.end), "text": text})
                text_parts.append(text)
            if self.progress_callback:
                self.progress_callback(f"Local Whisper transcribe လုပ်နေပါသည်... ({len(segments)} segments)", min(90.0, 20.0 + len(segments) * 0.5))

        full_text = " ".join(text_parts).strip()
        language = getattr(info, "language", "auto") or "auto"
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        if len(segments) >= 8:
            normalized = [re.sub(r"[^\w]+", " ", item["text"].casefold()).strip() for item in segments]
            counts = Counter(normalized)
            repeated = sum(1 for item in normalized if len(item) >= 8 and counts[item] >= 3)
            if repeated / len(segments) >= 0.25:
                raise ValueError("Whisper detected the same phrase repeatedly in at least a quarter of the transcript. Check Source language or use Auto-detect, then run again; translation was stopped before Gemini was called.")
        result = {"text": full_text, "segments": segments, "language": language, "duration": duration}
        (output_dir / "transcript.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        with (output_dir / "transcript.txt").open("w", encoding="utf-8") as f:
            f.write(full_text + "\n\n--- SEGMENTS WITH TIMESTAMPS ---\n")
            for seg in segments:
                f.write(f"[{seg['start']:.2f}s -> {seg['end']:.2f}s] {seg['text']}\n")
        if self.progress_callback:
            self.progress_callback("Local Whisper transcript ပြီးပါပြီ။", 100.0)
        return {"json_path": output_dir / "transcript.json", "txt_path": output_dir / "transcript.txt", **result}
