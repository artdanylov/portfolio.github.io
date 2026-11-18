"""
Converts media from media_local/_toConvert -> media_local/Converted (preserving subfolders),
provides an English local Web UI (Video & Thumbnail Studio) for converted videos organized
by hierarchy (portfolio / blog / custom folders) with independent checkboxes, stacked live/disk
previews for static (_thumb.webp, 960x540) and animated (_animthumb.webp, 640x360 @ 24fps)
thumbnails, persistent settings, and Cloudflare R2 upload.

Usage:
  python scripts/media_sync.py                    # convert new/updated files in _toConvert -> Converted
  python scripts/media_sync.py --upload           # convert new/updated files + upload to R2
  python scripts/media_sync.py --thumbs           # open Video & Thumbnail Studio for converted videos
  python scripts/media_sync.py --thumbs --upload  # open Studio + auto-upload generated files to R2
  python scripts/media_sync.py --upload-all       # convert + upload all files in Converted/ to R2
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MEDIA_DIR = BASE_DIR / "media_local"
INPUT_DIR = MEDIA_DIR / "_toConvert"
CONVERTED_DIR = MEDIA_DIR / "Converted"
STATE_FILE = CONVERTED_DIR / ".thumb_studio_state.json"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp", ".gif"}

MAX_WIDTH = 1920
MAX_HEIGHT = 1080
TARGET_FPS = 30
VIDEO_CRF = 18
VIDEO_MAXRATE_KBPS = 8000
WEBP_QUALITY = 92

THUMB_WIDTH = 960
THUMB_HEIGHT = 540
THUMB_QUALITY = 90

ANIM_THUMB_WIDTH = 640
ANIM_THUMB_HEIGHT = 360
ANIM_THUMB_FPS = 24
ANIM_THUMB_QUALITY = 82

CYRILLIC_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", "є": "ye", "і": "i",
    "ї": "yi", "ґ": "g",
}


def load_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_studio_state() -> dict[str, dict]:
    if not STATE_FILE.is_file():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_studio_state(state: dict[str, dict]) -> None:
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def is_up_to_date(src: Path, out_file: Path) -> bool:
    """True if converted output already exists and is newer than or same age as source."""
    return out_file.exists() and out_file.stat().st_mtime >= src.stat().st_mtime


def sanitize_stem(stem: str) -> str:
    """
    Replaces spaces, Cyrillic letters, parentheses, and URL-unsafe characters
    with clean ASCII characters ([A-Za-z0-9_-]) before conversion.
    """
    transliterated = []
    for ch in stem.strip():
        low = ch.lower()
        if low in CYRILLIC_MAP:
            rep = CYRILLIC_MAP[low]
            transliterated.append(rep.capitalize() if ch.isupper() else rep)
        else:
            transliterated.append(ch)
    ascii_str = unicodedata.normalize("NFKD", "".join(transliterated)).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", ascii_str)
    cleaned = re.sub(r"-+", "-", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_-")
    return cleaned or "media"


def sanitize_source_file(src: Path) -> Path:
    """Renames a pending source file in _toConvert to a clean URL-safe filename before conversion."""
    clean_stem = sanitize_stem(src.stem)
    if clean_stem == src.stem:
        return src
    target = src.with_name(f"{clean_stem}{src.suffix.lower()}")
    counter = 1
    while target.exists() and target.resolve() != src.resolve():
        target = src.with_name(f"{clean_stem}_{counter}{src.suffix.lower()}")
        counter += 1
    print(f"[Sanitize Name] {src.name} -> {target.name}")
    src.rename(target)
    return target


def clean_existing_main_media(out_dir: Path, stem: str) -> None:
    """Removes previous main converted file for stem without deleting _thumb / _animthumb."""
    for ext in (".mp4", ".webp"):
        candidate = out_dir / f"{stem}{ext}"
        if candidate.is_file():
            candidate.unlink(missing_ok=True)


def get_video_info(src: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,pix_fmt,width,height,r_frame_rate:format=duration",
        "-of",
        "json",
        str(src),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    streams = data.get("streams", [])
    v_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    fmt = data.get("format", {})
    fps_str = v_stream.get("r_frame_rate", "30/1")
    try:
        num, den = map(int, fps_str.split("/")) if "/" in fps_str else (int(float(fps_str)), 1)
        fps = num / den if den else 30.0
    except Exception:
        fps = 30.0

    return {
        "codec": v_stream.get("codec_name", ""),
        "pix_fmt": v_stream.get("pix_fmt", ""),
        "width": int(v_stream.get("width", 0) or 0),
        "height": int(v_stream.get("height", 0) or 0),
        "fps": fps,
        "duration": float(fmt.get("duration", 0.0) or 0.0),
        "has_audio": has_audio,
    }


def is_web_ready(info: dict) -> bool:
    return (
        info["codec"] == "h264"
        and info["pix_fmt"] == "yuv420p"
        and info["width"] <= MAX_WIDTH
        and info["height"] <= MAX_HEIGHT
        and info["fps"] <= TARGET_FPS + 0.5
    )


def encode_video(
    src: Path,
    out_video: Path,
    has_audio: bool = True,
    crf: int = VIDEO_CRF,
    max_bitrate_kbps: int = VIDEO_MAXRATE_KBPS,
) -> None:
    """
    Encodes H.264 MP4 optimized for Cloudflare R2 streaming and universal browser hardware decoding:
    - High@L4.1, yuv420p, bt709 color signaling
    - Closed GOP with 2-second keyframe interval (-g 60) for fast HTTP Range scrubbing
    - VBV capped CRF (-maxrate / -bufsize) to prevent bandwidth spikes on 3D renders
    - Smart audio: omits audio track (-an) if source has no audio stream
    """
    vf_filter = (
        f"scale='min({MAX_WIDTH},iw)':'min({MAX_HEIGHT},ih)':force_original_aspect_ratio=decrease:flags=lanczos,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
        f"fps='min({TARGET_FPS},source_fps)'"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vf",
        vf_filter,
        "-c:v",
        "libx264",
        "-crf",
        str(crf),
        "-preset",
        "slow",
        "-profile:v",
        "high",
        "-level:v",
        "4.1",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(TARGET_FPS * 2),
        "-x264-params",
        "open-gop=0",
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
    ]
    if max_bitrate_kbps:
        cmd.extend(["-maxrate", f"{max_bitrate_kbps}k", "-bufsize", f"{max_bitrate_kbps * 2}k"])
    if has_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "160k", "-ac", "2"])
    else:
        cmd.append("-an")
    cmd.extend(["-movflags", "+faststart", str(out_video)])
    subprocess.run(cmd, check=True)


def remux_faststart(src: Path, out_video: Path, has_audio: bool = True) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-c:v",
        "copy",
    ]
    if has_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "160k"])
    else:
        cmd.append("-an")
    cmd.extend(["-movflags", "+faststart", str(out_video)])
    subprocess.run(cmd, check=True)


def generate_static_thumb(src_video: Path, out_thumb: Path, timestamp: float) -> None:
    """Generates a 960x540 (16:9) static WebP thumbnail at `timestamp` seconds."""
    vf = (
        f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={THUMB_WIDTH}:{THUMB_HEIGHT}"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, timestamp):.3f}",
        "-i",
        str(src_video),
        "-vf",
        vf,
        "-frames:v",
        "1",
        "-c:v",
        "libwebp",
        "-lossless",
        "0",
        "-compression_level",
        "6",
        "-quality",
        str(THUMB_QUALITY),
        "-sharp_yuv",
        "1",
        str(out_thumb),
    ]
    subprocess.run(cmd, check=True)


def generate_anim_thumb(src_video: Path, out_anim: Path, start_time: float, duration: float) -> None:
    """Generates a 640x360 @ 24fps looping animated WebP thumbnail."""
    vf = (
        f"fps={ANIM_THUMB_FPS},"
        f"scale={ANIM_THUMB_WIDTH}:{ANIM_THUMB_HEIGHT}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={ANIM_THUMB_WIDTH}:{ANIM_THUMB_HEIGHT}"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, start_time):.3f}",
        "-t",
        f"{max(0.2, duration):.3f}",
        "-i",
        str(src_video),
        "-vf",
        vf,
        "-c:v",
        "libwebp",
        "-lossless",
        "0",
        "-compression_level",
        "6",
        "-quality",
        str(ANIM_THUMB_QUALITY),
        "-loop",
        "0",
        "-an",
        str(out_anim),
    ]
    subprocess.run(cmd, check=True)


def convert_image(src: Path, out_image: Path, quality: int = WEBP_QUALITY) -> None:
    scale_filter = f"scale='min({MAX_WIDTH},iw)':-1:force_original_aspect_ratio=decrease:flags=lanczos"
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vf",
        scale_filter,
        "-c:v",
        "libwebp",
        "-quality",
        str(quality),
        "-compression_level",
        "6",
    ]
    if src.suffix.lower() == ".gif":
        cmd.extend(["-loop", "0", "-an"])
    else:
        cmd.extend(["-sharp_yuv", "1"])
    cmd.append(str(out_image))
    subprocess.run(cmd, check=True)


def count_pending_to_convert() -> int:
    """Returns the number of new/updated videos or images waiting in _toConvert."""
    if not INPUT_DIR.exists():
        return 0
    count = 0
    for src in INPUT_DIR.rglob("*"):
        if not src.is_file():
            continue
        ext = src.suffix.lower()
        rel_parent = src.relative_to(INPUT_DIR).parent
        out_dir = CONVERTED_DIR / rel_parent
        clean_stem = sanitize_stem(src.stem)
        if ext in VIDEO_EXTENSIONS:
            legacy_out = out_dir / f"{src.stem}.mp4"
            clean_out = out_dir / f"{clean_stem}.mp4"
            if not is_up_to_date(src, legacy_out) and not is_up_to_date(src, clean_out):
                count += 1
        elif ext in IMAGE_EXTENSIONS and not src.stem.endswith("_thumb") and not src.stem.endswith("_animthumb"):
            legacy_out = out_dir / f"{src.stem}.webp"
            clean_out = out_dir / f"{clean_stem}.webp"
            if not is_up_to_date(src, legacy_out) and not is_up_to_date(src, clean_out):
                count += 1
    return count


def convert_all() -> list[Path]:
    """
    Converts only new/updated videos and images from _toConvert -> Converted.
    Automatically sanitizes spaces and invalid characters in new filenames before conversion.
    """
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)

    files = [f for f in INPUT_DIR.rglob("*") if f.is_file()]
    videos = [f for f in files if f.suffix.lower() in VIDEO_EXTENSIONS]
    images = [
        f
        for f in files
        if f.suffix.lower() in IMAGE_EXTENSIONS
        and not f.stem.endswith("_thumb")
        and not f.stem.endswith("_animthumb")
    ]

    if not videos and not images:
        print(f"No source files found in '{INPUT_DIR}'.")
        return []

    produced: list[Path] = []

    for src in videos:
        rel_parent = src.relative_to(INPUT_DIR).parent
        out_dir = CONVERTED_DIR / rel_parent
        out_dir.mkdir(parents=True, exist_ok=True)

        legacy_video = out_dir / f"{src.stem}.mp4"
        clean_stem = sanitize_stem(src.stem)
        clean_video = out_dir / f"{clean_stem}.mp4"

        if is_up_to_date(src, legacy_video):
            rel_out = legacy_video.relative_to(CONVERTED_DIR).as_posix()
            print(f"[Skip Video] {src.relative_to(INPUT_DIR).as_posix()} (Converted/{rel_out} is up to date)")
            continue
        if is_up_to_date(src, clean_video):
            rel_out = clean_video.relative_to(CONVERTED_DIR).as_posix()
            print(f"[Skip Video] {src.relative_to(INPUT_DIR).as_posix()} (Converted/{rel_out} is up to date)")
            continue

        src = sanitize_source_file(src)
        out_video = out_dir / f"{src.stem}.mp4"
        rel_out = out_video.relative_to(CONVERTED_DIR).as_posix()

        clean_existing_main_media(out_dir, src.stem)
        orig_size = src.stat().st_size
        print(f"[Convert Video] {src.relative_to(INPUT_DIR).as_posix()} -> Converted/{rel_out}")

        info = get_video_info(src)
        has_audio = info.get("has_audio", True)
        encode_video(src, out_video, has_audio=has_audio, crf=VIDEO_CRF)
        new_size = out_video.stat().st_size

        if new_size > orig_size:
            if is_web_ready(info):
                remux_faststart(src, out_video, has_audio=has_audio)
                new_size = out_video.stat().st_size
            else:
                duration = max(info["duration"], 1.0)
                audio_kbps = 160 if has_audio else 0
                target_kbps = max(int((orig_size * 8 / 1000) / duration * 0.90) - audio_kbps, 300)
                encode_video(src, out_video, has_audio=has_audio, crf=20, max_bitrate_kbps=target_kbps)
                new_size = out_video.stat().st_size

        print(f"  [OK] {orig_size / (1024 * 1024):.2f} MB -> {new_size / (1024 * 1024):.2f} MB")
        produced.append(out_video)

    for src in images:
        rel_parent = src.relative_to(INPUT_DIR).parent
        out_dir = CONVERTED_DIR / rel_parent
        out_dir.mkdir(parents=True, exist_ok=True)

        legacy_image = out_dir / f"{src.stem}.webp"
        clean_stem = sanitize_stem(src.stem)
        clean_image = out_dir / f"{clean_stem}.webp"

        if is_up_to_date(src, legacy_image):
            rel_out = legacy_image.relative_to(CONVERTED_DIR).as_posix()
            print(f"[Skip Image] {src.relative_to(INPUT_DIR).as_posix()} (Converted/{rel_out} is up to date)")
            continue
        if is_up_to_date(src, clean_image):
            rel_out = clean_image.relative_to(CONVERTED_DIR).as_posix()
            print(f"[Skip Image] {src.relative_to(INPUT_DIR).as_posix()} (Converted/{rel_out} is up to date)")
            continue

        src = sanitize_source_file(src)
        out_image = out_dir / f"{src.stem}.webp"
        rel_out = out_image.relative_to(CONVERTED_DIR).as_posix()

        clean_existing_main_media(out_dir, src.stem)
        orig_size = src.stat().st_size
        print(f"[Convert Image] {src.relative_to(INPUT_DIR).as_posix()} -> Converted/{rel_out}")

        convert_image(src, out_image, quality=WEBP_QUALITY)
        new_size = out_image.stat().st_size
        for fallback_q in (82, 75):
            if new_size <= orig_size:
                break
            convert_image(src, out_image, quality=fallback_q)
            new_size = out_image.stat().st_size

        print(f"  [OK] {orig_size / 1024:.0f} KB -> {new_size / 1024:.0f} KB")
        produced.append(out_image)

    return produced


def collect_converted_media_catalog() -> list[dict]:
    """
    Collects all ready .mp4 videos AND standalone converted images (excluding generated
    _thumb and _animthumb files) in media_local/Converted/, extracts their hierarchy
    (top-level category like 'portfolio' or 'blog' + subfolder), and merges saved settings.
    """
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    saved_state = load_studio_state()
    catalog: list[dict] = []

    def split_hierarchy(rel_parent: Path) -> tuple[str, str]:
        parts = rel_parent.parts
        if len(parts) == 0:
            return "root", "(root)"
        if len(parts) == 1:
            return parts[0], "(root)"
        return parts[0], "/".join(parts[1:])

    # 1. Converted videos (.mp4)
    for conv in sorted(CONVERTED_DIR.rglob("*.mp4")):
        if not conv.is_file():
            continue
        rel_key = conv.relative_to(CONVERTED_DIR).as_posix()
        rel_parent = conv.relative_to(CONVERTED_DIR).parent
        category, subfolder = split_hierarchy(rel_parent)

        out_dir = conv.parent
        out_thumb = out_dir / f"{conv.stem}_thumb.webp"
        out_anim = out_dir / f"{conv.stem}_animthumb.webp"

        try:
            info = get_video_info(conv)
        except Exception:
            info = {"duration": 0.0, "fps": 30.0, "width": 1920, "height": 1080}

        dur = max(info["duration"], 0.1)
        default_thumb = round(min(max(dur * 0.25, 0.0), max(dur - 0.1, 0.0)), 2)
        default_anim_start = round(min(max(dur * 0.2, 0.0), max(dur - 1.0, 0.0)), 2)
        default_anim_dur = round(min(1.0, dur), 2)

        saved = saved_state.get(rel_key, {})
        thumb_time = round(max(0.0, min(dur, float(saved.get("thumb_time", default_thumb)))), 2)
        anim_duration = round(max(0.2, min(dur, float(saved.get("anim_duration", default_anim_dur)))), 2)
        anim_start = round(max(0.0, min(max(0.0, dur - anim_duration), float(saved.get("anim_start", default_anim_start)))), 2)

        catalog.append(
            {
                "kind": "video",
                "id": rel_key,
                "stem": conv.stem,
                "category": category,
                "subfolder": subfolder,
                "folder": rel_parent.as_posix() if rel_parent.as_posix() != "." else "",
                "video_path": str(conv),
                "out_thumb": str(out_thumb),
                "out_anim": str(out_anim),
                "video_rel": rel_key,
                "thumb_rel": out_thumb.relative_to(CONVERTED_DIR).as_posix(),
                "anim_rel": out_anim.relative_to(CONVERTED_DIR).as_posix(),
                "has_thumb": out_thumb.is_file(),
                "has_anim": out_anim.is_file(),
                "duration": round(dur, 3),
                "fps": round(info["fps"] or 30.0, 2),
                "width": info["width"],
                "height": info["height"],
                "thumb_time": thumb_time,
                "anim_start": anim_start,
                "anim_duration": anim_duration,
                "size_kb": round(conv.stat().st_size / 1024, 1),
            }
        )

    # 2. Standalone converted images (excluding generated _thumb / _animthumb)
    for img in sorted(CONVERTED_DIR.rglob("*")):
        if not img.is_file():
            continue
        if img.suffix.lower() not in {".webp", ".png", ".jpg", ".jpeg", ".gif"}:
            continue
        if img.stem.endswith("_thumb") or img.stem.endswith("_animthumb"):
            continue
        rel_key = img.relative_to(CONVERTED_DIR).as_posix()
        rel_parent = img.relative_to(CONVERTED_DIR).parent
        category, subfolder = split_hierarchy(rel_parent)
        catalog.append(
            {
                "kind": "image",
                "id": rel_key,
                "stem": img.stem,
                "category": category,
                "subfolder": subfolder,
                "folder": rel_parent.as_posix() if rel_parent.as_posix() != "." else "",
                "image_rel": rel_key,
                "size_kb": round(img.stat().st_size / 1024, 1),
            }
        )

    return catalog


THUMB_STUDIO_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>ArtDanylov — Video & Thumbnail Studio</title>
<style>
  :root {
    --bg: #0b0b0c;
    --panel: #141417;
    --panel-2: #1c1c21;
    --border: rgba(255, 255, 255, 0.11);
    --text: #f4f4f5;
    --muted: #a1a1aa;
    --accent: #e85d1a;
    --accent-hover: #ff7433;
    --green: #22c55e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 9px 16px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    gap: 12px;
    flex-shrink: 0;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 800;
    letter-spacing: 0.03em;
    font-size: 14px;
    min-width: 0;
  }
  .brand-badge {
    background: var(--accent);
    color: #fff;
    padding: 2px 7px;
    border-radius: 6px;
    font-size: 11px;
  }
  .header-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  button {
    font-family: inherit;
    font-size: 12.5px;
    font-weight: 600;
    border-radius: 7px;
    border: 1px solid var(--border);
    background: var(--panel-2);
    color: var(--text);
    padding: 6px 12px;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s, opacity 0.15s;
    white-space: nowrap;
  }
  button:hover:not(:disabled) {
    background: #27272e;
    border-color: rgba(255, 255, 255, 0.25);
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  button.btn-primary {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }
  button.btn-primary:hover:not(:disabled) {
    background: var(--accent-hover);
    border-color: var(--accent-hover);
  }
  button.btn-sm {
    padding: 4px 9px;
    font-size: 11.5px;
  }
  button.active-pill {
    background: rgba(232, 93, 26, 0.22);
    border-color: var(--accent);
    color: #ff9c6e;
  }
  .workspace {
    flex: 1;
    display: grid;
    grid-template-columns: 280px 1fr;
    min-height: 0;
  }
  .sidebar {
    background: var(--panel);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .sidebar-top {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .search-input {
    width: 100%;
    padding: 6px 9px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: #09090b;
    color: #fff;
    font-size: 12px;
  }
  .filter-row {
    display: flex;
    gap: 5px;
    flex-wrap: wrap;
  }
  .video-list {
    flex: 1;
    overflow-y: auto;
    padding: 8px;
    display: flex;
    flex-direction: column;
    gap: 7px;
  }
  .tree-cat {
    border: 1px solid var(--border);
    border-radius: 9px;
    background: rgba(255, 255, 255, 0.015);
    overflow: hidden;
  }
  .tree-cat-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 7px 10px;
    background: rgba(255, 255, 255, 0.04);
    font-size: 11.5px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #fff;
    cursor: pointer;
    user-select: none;
  }
  .tree-cat-header:hover { background: rgba(255, 255, 255, 0.07); }
  .tree-header-actions {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
  .icon-btn {
    padding: 2px 5px;
    font-size: 11px;
    line-height: 1.2;
    border-radius: 5px;
    border: 1px solid transparent;
    background: transparent;
    color: var(--muted);
    cursor: pointer;
  }
  .icon-btn:hover {
    background: rgba(255, 255, 255, 0.1);
    color: #fff;
    border-color: var(--border);
  }
  .icon-btn.icon-del:hover {
    background: rgba(239, 68, 68, 0.18);
    color: #f87171;
    border-color: rgba(239, 68, 68, 0.4);
  }
  .video-item-actions {
    display: flex;
    align-items: center;
    gap: 2px;
    opacity: 0.25;
    flex-shrink: 0;
    transition: opacity 0.15s;
  }
  .video-item:hover .video-item-actions,
  .video-item.selected .video-item-actions {
    opacity: 1;
  }
  .tree-cat-body {
    padding: 5px 7px;
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .tree-sub { display: flex; flex-direction: column; gap: 3px; }
  .tree-sub-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px 7px;
    font-size: 11px;
    font-weight: 700;
    color: var(--muted);
    cursor: pointer;
    border-radius: 5px;
    user-select: none;
  }
  .tree-sub-header:hover { background: rgba(255, 255, 255, 0.04); color: var(--text); }
  .tree-sub-items {
    padding-left: 8px;
    border-left: 1px solid rgba(255, 255, 255, 0.08);
    margin-left: 7px;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .video-item {
    display: flex;
    align-items: center;
    padding: 7px 9px;
    border-radius: 7px;
    border: 1px solid transparent;
    background: rgba(255, 255, 255, 0.02);
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
  }
  .video-item:hover { background: rgba(255, 255, 255, 0.06); }
  .video-item.selected {
    background: rgba(232, 93, 26, 0.15);
    border-color: var(--accent);
  }
  .video-meta { flex: 1; min-width: 0; }
  .video-title {
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .video-sub {
    font-size: 10px;
    color: var(--muted);
    margin-top: 2px;
    display: flex;
    gap: 5px;
    align-items: center;
  }
  .badge {
    font-size: 9px;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 4px;
    text-transform: uppercase;
  }
  .badge-ok { background: rgba(34, 197, 94, 0.18); color: #4ade80; }
  .badge-off { background: rgba(255, 255, 255, 0.07); color: #71717a; }
  .badge-img { background: rgba(56, 189, 248, 0.18); color: #38bdf8; }

  /* Main editor layout designed to fit comfortably on screen vertically */
  .editor {
    padding: 12px 16px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-height: 0;
  }
  #editorContent {
    display: none;
    grid-template-columns: minmax(0, 1fr) 10px var(--preview-col-w, 310px);
    gap: 4px;
    align-items: start;
  }
  @media (max-width: 1080px) {
    #editorContent {
      grid-template-columns: 1fr;
      gap: 12px;
    }
    .col-resizer {
      display: none !important;
    }
  }
  .col-resizer {
    align-self: stretch;
    cursor: ew-resize;
    display: flex;
    align-items: center;
    justify-content: center;
    user-select: none;
    border-radius: 4px;
    transition: background 0.15s;
  }
  .col-resizer::before {
    content: "";
    width: 4px;
    height: 64px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.16);
    transition: background 0.15s, height 0.15s;
  }
  .col-resizer:hover {
    background: rgba(255, 255, 255, 0.04);
  }
  .col-resizer:hover::before {
    background: var(--accent);
    height: 96px;
  }
  #imageViewerContent {
    display: none;
    flex-direction: column;
    gap: 12px;
    max-width: 1080px;
    width: 100%;
    margin: 0 auto;
  }
  .image-stage {
    width: 100%;
    max-height: 66vh;
    background: #000;
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 10px;
  }
  .image-stage img {
    max-width: 100%;
    max-height: calc(66vh - 20px);
    object-fit: contain;
    display: block;
    border-radius: 4px;
  }
  .left-column {
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-width: 0;
  }
  .right-column {
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-width: 0;
  }
  .player-card, .preview-card, .control-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 11px 13px;
    transition: opacity 0.2s, border-color 0.2s;
  }
  .control-card.is-disabled { opacity: 0.55; }
  .player-stage {
    display: flex;
    justify-content: center;
    align-items: center;
    width: 100%;
    height: var(--player-h, 54vh);
    min-height: 220px;
    max-height: 78vh;
    background: #000;
    border-radius: 7px;
    overflow: hidden;
  }
  #videoPlayer {
    width: auto;
    height: 100%;
    max-width: 100%;
    max-height: 100%;
    background: #000;
    display: block;
    object-fit: contain;
  }
  .player-resizer {
    height: 10px;
    margin-top: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: ns-resize;
    user-select: none;
    border-radius: 4px;
    transition: background 0.15s;
  }
  .player-resizer::before {
    content: "";
    width: 56px;
    height: 4px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.18);
    transition: background 0.15s, width 0.15s;
  }
  .player-resizer:hover {
    background: rgba(255, 255, 255, 0.04);
  }
  .player-resizer:hover::before {
    background: var(--accent);
    width: 78px;
  }
  /* Stacked vertical previews in the right column */
  .stacked-previews {
    display: flex;
    flex-direction: column;
    gap: 9px;
  }
  .preview-pane {
    background: #09090b;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 7px;
    display: flex;
    flex-direction: column;
    gap: 5px;
    transition: opacity 0.2s, border-color 0.2s;
  }
  .preview-pane.is-disabled { opacity: 0.42; }
  .preview-pane.is-enabled { border-color: rgba(232, 93, 26, 0.45); }
  .preview-pane-head {
    font-size: 11px;
    font-weight: 700;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 4px;
  }
  .preview-media-box {
    position: relative;
    width: 100%;
    aspect-ratio: 16 / 9;
    background: #000;
    border-radius: 5px;
    overflow: hidden;
    border: 1px solid var(--border);
  }
  .preview-media-box canvas,
  .preview-media-box video,
  .preview-media-box img {
    width: 100%;
    height: 100%;
    display: block;
    object-fit: cover;
  }
  .card-title {
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
    margin-bottom: 7px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }
  .control-row {
    display: flex;
    align-items: center;
    gap: 7px;
    flex-wrap: wrap;
    margin-top: 8px;
  }
  input[type="range"] {
    width: 100%;
    accent-color: var(--accent);
    cursor: pointer;
    margin: 2px 0;
  }
  input[type="number"] {
    width: 68px;
    padding: 4px 7px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: #09090b;
    color: #fff;
    font-size: 12px;
  }
  .range-track-wrap {
    position: relative;
    height: 30px;
    background: #09090b;
    border: 1px solid var(--border);
    border-radius: 7px;
    margin: 6px 0;
    user-select: none;
    overflow: hidden;
  }
  .range-window {
    position: absolute;
    top: 0;
    bottom: 0;
    background: rgba(232, 93, 26, 0.35);
    border-left: 3px solid var(--accent);
    border-right: 3px solid var(--accent);
    cursor: grab;
  }
  .range-window:active { cursor: grabbing; }
  .range-playhead {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 2px;
    background: #fff;
    pointer-events: none;
  }
  .range-thumb-marker {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 2px;
    background: #22c55e;
    pointer-events: none;
  }
  .path-box {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    background: #09090b;
    border: 1px solid var(--border);
    padding: 5px 8px;
    border-radius: 7px;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 11px;
    color: #d4d4d8;
    margin-top: 6px;
  }
  .path-label {
    font-family: system-ui, sans-serif;
    font-size: 9.5px;
    font-weight: 700;
    text-transform: uppercase;
    color: var(--muted);
    min-width: 46px;
  }
  .path-val {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .empty-state {
    margin: auto;
    text-align: center;
    color: var(--muted);
  }
  .status-toast {
    font-size: 12px;
    color: var(--green);
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .check-label {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    color: var(--text);
    cursor: pointer;
    font-size: 12.5px;
    font-weight: 600;
    text-transform: none;
    letter-spacing: 0;
  }
  .check-label input[type="checkbox"] {
    accent-color: var(--accent);
    width: 15px;
    height: 15px;
    cursor: pointer;
  }
  .mode-tabs {
    display: inline-flex;
    gap: 4px;
  }
</style>
</head>
<body>
  <header>
    <div class="brand">
      <span class="brand-badge">AD</span>
      <span>Video &amp; Thumbnail Studio</span>
      <span id="summaryText" style="font-weight:400; color:var(--muted); font-size:12px;"></span>
      <span id="statusToast" class="status-toast"></span>
    </div>
    <div class="header-actions">
      <button type="button" id="convertNewBtn" title="Convert any new source videos/images from media_local/_toConvert">
        🔄 Convert from Source Videos
      </button>
      <button type="button" class="btn-primary" id="generateSingleBtn" title="Generate checked thumbnail(s) for the selected video">
        ⚡ Generate Checked Thumbnail(s)
      </button>
      <button type="button" id="updateAllThumbsBtn" title="Regenerate and overwrite all existing thumbnails across all videos">
        ♻️ Update All Thumbnails
      </button>
      <button type="button" id="finishBtn">
        ✅ Finish &amp; Close
      </button>
    </div>
  </header>

  <div class="workspace">
    <aside class="sidebar">
      <div class="sidebar-top">
        <div class="filter-row">
          <button type="button" class="btn-sm" id="filterPortfolioBtn" style="flex:1;">Portfolio</button>
          <button type="button" class="btn-sm" id="filterBlogBtn" style="flex:1;">Blog</button>
        </div>
        <div class="filter-row">
          <button type="button" class="btn-sm" id="filterVideoBtn" style="flex:1;">Video</button>
          <button type="button" class="btn-sm" id="filterImagesBtn" style="flex:1;">Images</button>
          <button type="button" class="btn-sm" id="filterMissingBtn" style="flex:1;">No Thumbs</button>
        </div>
      </div>
      <div class="video-list" id="videoList"></div>
    </aside>

    <main class="editor" id="editorPane">
      <div class="empty-state" id="emptyState">Select a converted video or image from the folder tree on the left</div>

      <div id="imageViewerContent">
        <div class="player-card">
          <div class="card-title">
            <span id="imageViewerTitle">Image</span>
            <span id="imageMetaLabel" style="font-family:monospace; color:#fff;">—</span>
          </div>
          <div class="image-stage">
            <img id="imageViewerImg" alt="Preview" />
          </div>
          <div class="path-box" style="margin-top:10px;">
            <span class="path-label">Image</span>
            <span class="path-val" id="imagePathText"></span>
            <button type="button" class="btn-sm" id="copyImagePathBtn">Copy</button>
          </div>
          <div class="path-box">
            <span class="path-label">Markdoc</span>
            <span class="path-val" id="imageMarkdocText"></span>
            <button type="button" class="btn-sm" id="copyImageMarkdocBtn">Copy</button>
          </div>
        </div>
      </div>

      <div id="editorContent">
        <div class="left-column">
          <div class="player-card">
            <div class="card-title">
              <span id="currentVideoTitle" style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">Video</span>
              <div style="display:flex; align-items:center; gap:8px; flex-shrink:0;">
                <button type="button" class="btn-sm" id="fitPlayerHeightBtn" title="Auto-fit player height so all controls fit on one screen">
                  ⇳ Fit Screen
                </button>
                <span id="currentTimeLabel" style="font-family:monospace; color:#fff;">0.00s / 0.00s</span>
              </div>
            </div>
            <div class="player-stage" id="playerStage">
              <video id="videoPlayer" controls playsinline preload="auto"></video>
            </div>
            <div class="player-resizer" id="playerResizer" title="Drag up/down to resize player height (double-click to auto-fit)"></div>
            <div class="control-row">
              <button type="button" class="btn-sm" id="stepBack1s">-1s</button>
              <button type="button" class="btn-sm" id="stepBackFrame">-1f</button>
              <button type="button" class="btn-sm" id="stepFwdFrame">+1f</button>
              <button type="button" class="btn-sm" id="stepFwd1s">+1s</button>
              <button type="button" class="btn-sm" id="previewThumbBtn">
                🖼 Preview Thumbnail
              </button>
              <button type="button" class="btn-sm" id="previewAnimBtn">
                🎞 Preview Animated Thumbnail
              </button>
            </div>
          </div>

          <div class="control-card" id="thumbControlCard">
            <div class="card-title" style="margin-bottom:4px;">
              <label class="check-label">
                <input type="checkbox" id="enableThumbToggle" />
                <span>1. Static Thumbnail (_thumb.webp · 960×540)</span>
              </label>
              <span id="thumbStatusLabel" style="font-size:11px;"></span>
            </div>
            <input type="range" id="thumbSlider" min="0" max="10" step="0.01" value="0" />
          </div>

          <div class="control-card" id="animControlCard">
            <div class="card-title" style="margin-bottom:4px;">
              <label class="check-label">
                <input type="checkbox" id="enableAnimToggle" />
                <span>2. Animated Thumbnail (_animthumb.webp · 640×360 @ 24 fps)</span>
              </label>
              <span id="animRangeBadge" style="color:#ff9c6e; font-family:monospace;">0.00s — 1.00s (1.00s)</span>
            </div>

            <div class="range-track-wrap" id="timelineTrack" title="Drag the orange bar or its edges to adjust start and duration">
              <div class="range-thumb-marker" id="timelineThumbMarker"></div>
              <div class="range-window" id="timelineWindow"></div>
              <div class="range-playhead" id="timelinePlayhead"></div>
            </div>

            <div class="control-row" style="justify-content: space-between; margin-top:6px;">
              <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                <label style="font-size:11.5px; color:var(--muted);">Start:</label>
                <input type="number" id="animStartInput" min="0" step="0.05" value="0" />
                <label style="font-size:11.5px; color:var(--muted);">Length:</label>
                <button type="button" class="btn-sm" id="durDecBtn">-0.1s</button>
                <input type="number" id="animDurInput" min="0.2" max="5.0" step="0.05" value="1.0" />
                <button type="button" class="btn-sm" id="durIncBtn">+0.1s</button>
              </div>
              <div style="display:flex; align-items:center; gap:4px; flex-wrap:wrap;">
                <button type="button" class="btn-sm dur-preset" data-dur="0.3">0.3s</button>
                <button type="button" class="btn-sm dur-preset" data-dur="0.5">0.5s</button>
                <button type="button" class="btn-sm dur-preset" data-dur="0.75">0.75s</button>
                <button type="button" class="btn-sm dur-preset" data-dur="1.0">1.0s</button>
                <button type="button" class="btn-sm dur-preset" data-dur="1.25">1.25s</button>
                <button type="button" class="btn-sm dur-preset" data-dur="1.5">1.5s</button>
                <button type="button" class="btn-sm dur-preset" data-dur="2.0">2.0s</button>
                <button type="button" class="btn-sm dur-preset" data-dur="3.0">3.0s</button>
              </div>
            </div>
          </div>
        </div>

        <div class="col-resizer" id="previewColResizer" title="Drag left/right to resize Previews column (double-click to reset)"></div>

        <div class="right-column">
          <div class="preview-card">
            <div class="card-title">
              <span>Previews</span>
              <div class="mode-tabs">
                <button type="button" class="btn-sm active-pill" id="previewSourceLiveBtn">Live</button>
                <button type="button" class="btn-sm" id="previewSourceDiskBtn">On Disk</button>
              </div>
            </div>

            <div class="stacked-previews">
              <div class="preview-pane" id="thumbPreviewPane">
                <div class="preview-pane-head">
                  <span>1. Static _thumb (960×540)</span>
                  <span id="thumbTimeBadge" style="color:var(--green); font-family:monospace;">0.00s</span>
                </div>
                <div class="preview-media-box">
                  <canvas id="thumbCanvas" width="960" height="540"></canvas>
                  <img id="savedThumbImg" alt="Saved static thumb" style="display:none;" />
                </div>
              </div>

              <div class="preview-pane" id="animPreviewPane">
                <div class="preview-pane-head">
                  <span>2. Animated _animthumb (24fps)</span>
                  <span id="animShortBadge" style="color:#ff9c6e; font-family:monospace;">0.00s–1.00s</span>
                </div>
                <div class="preview-media-box">
                  <video id="animPreviewVideo" muted playsinline preload="auto"></video>
                  <img id="savedAnimImg" alt="Saved animated thumb" style="display:none;" />
                </div>
              </div>
            </div>

            <video id="thumbCaptureVideo" muted playsinline preload="auto" style="display:none;"></video>

            <div style="margin-top:8px;">
              <div class="path-box">
                <span class="path-label">Video</span>
                <span class="path-val" id="videoPathText"></span>
                <button type="button" class="btn-sm" id="copyVideoPathBtn">Copy</button>
              </div>
              <div class="path-box">
                <span class="path-label">_thumb</span>
                <span class="path-val" id="thumbPathText"></span>
                <button type="button" class="btn-sm" id="copyThumbPathBtn">Copy</button>
              </div>
              <div class="path-box">
                <span class="path-label">_anim</span>
                <span class="path-val" id="animPathText"></span>
                <button type="button" class="btn-sm" id="copyAnimPathBtn">Copy</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>

<script>
  let mediaItems = [];
  let pendingConvertCount = 0;
  let activeCategory = null; // null | 'portfolio' | 'blog'
  let activeKind = null;     // null | 'video' | 'image'
  let filterMissingOnly = false;
  let selectedId = null;
  let mainPreviewMode = 'free'; // 'free' | 'thumb' | 'anim'
  let previewSourceMode = 'live'; // 'live' | 'disk'
  let pendingSnap = false;
  let saveTimeout = null;
  const collapsedCats = new Set();
  const collapsedSubs = new Set();

  const videoListEl = document.getElementById('videoList');
  const summaryText = document.getElementById('summaryText');
  const statusToast = document.getElementById('statusToast');
  const convertNewBtn = document.getElementById('convertNewBtn');
  const generateSingleBtn = document.getElementById('generateSingleBtn');
  const updateAllThumbsBtn = document.getElementById('updateAllThumbsBtn');
  const editorContent = document.getElementById('editorContent');
  const imageViewerContent = document.getElementById('imageViewerContent');
  const imageViewerTitle = document.getElementById('imageViewerTitle');
  const imageMetaLabel = document.getElementById('imageMetaLabel');
  const imageViewerImg = document.getElementById('imageViewerImg');
  const imagePathText = document.getElementById('imagePathText');
  const imageMarkdocText = document.getElementById('imageMarkdocText');
  const previewColResizer = document.getElementById('previewColResizer');
  const emptyState = document.getElementById('emptyState');

  const playerStage = document.getElementById('playerStage');
  const playerResizer = document.getElementById('playerResizer');
  const fitPlayerHeightBtn = document.getElementById('fitPlayerHeightBtn');
  const player = document.getElementById('videoPlayer');
  const thumbCaptureVideo = document.getElementById('thumbCaptureVideo');
  const animPreviewVideo = document.getElementById('animPreviewVideo');
  const canvas = document.getElementById('thumbCanvas');
  const ctx = canvas.getContext('2d');
  const savedThumbImg = document.getElementById('savedThumbImg');
  const savedAnimImg = document.getElementById('savedAnimImg');

  const currentVideoTitle = document.getElementById('currentVideoTitle');
  const currentTimeLabel = document.getElementById('currentTimeLabel');
  const thumbTimeBadge = document.getElementById('thumbTimeBadge');
  const animShortBadge = document.getElementById('animShortBadge');
  const animRangeBadge = document.getElementById('animRangeBadge');
  const thumbSlider = document.getElementById('thumbSlider');
  const enableThumbToggle = document.getElementById('enableThumbToggle');
  const enableAnimToggle = document.getElementById('enableAnimToggle');
  const thumbControlCard = document.getElementById('thumbControlCard');
  const animControlCard = document.getElementById('animControlCard');
  const thumbPreviewPane = document.getElementById('thumbPreviewPane');
  const animPreviewPane = document.getElementById('animPreviewPane');
  const thumbStatusLabel = document.getElementById('thumbStatusLabel');
  const animStartInput = document.getElementById('animStartInput');
  const animDurInput = document.getElementById('animDurInput');
  const previewThumbBtn = document.getElementById('previewThumbBtn');
  const previewAnimBtn = document.getElementById('previewAnimBtn');
  const previewSourceLiveBtn = document.getElementById('previewSourceLiveBtn');
  const previewSourceDiskBtn = document.getElementById('previewSourceDiskBtn');

  const videoPathText = document.getElementById('videoPathText');
  const thumbPathText = document.getElementById('thumbPathText');
  const animPathText = document.getElementById('animPathText');

  const timelineTrack = document.getElementById('timelineTrack');
  const timelineWindow = document.getElementById('timelineWindow');
  const timelinePlayhead = document.getElementById('timelinePlayhead');
  const timelineThumbMarker = document.getElementById('timelineThumbMarker');

  function showToast(msg, isError = false) {
    statusToast.textContent = msg;
    statusToast.style.color = isError ? '#f87171' : 'var(--green)';
    setTimeout(() => {
      if (statusToast.textContent === msg) statusToast.textContent = '';
    }, 3200);
  }

  async function loadCatalog(preserveSelected = true) {
    try {
      const res = await fetch('/api/videos');
      const data = await res.json();
      pendingConvertCount = data.pending_to_convert || 0;
      convertNewBtn.textContent = pendingConvertCount > 0
        ? `🔄 Convert from Source Videos (${pendingConvertCount} new)`
        : '🔄 Convert from Source Videos';
      convertNewBtn.classList.toggle('active-pill', pendingConvertCount > 0);

      mediaItems = (data.items || data.videos || []).map(item => (
        item.kind === 'video'
          ? {
              ...item,
              enable_thumb: Boolean(item.has_thumb),
              enable_anim: Boolean(item.has_anim),
            }
          : item
      ));

      updateFilterButtons();
      renderTree();
      if (preserveSelected && selectedId && mediaItems.some(v => v.id === selectedId)) {
        selectItem(selectedId);
      } else {
        const firstVisible = getVisibleItems()[0];
        if (firstVisible) selectItem(firstVisible.id);
      }
    } catch (err) {
      showToast('Failed to load media catalog.', true);
    }
  }

  function scheduleSaveState() {
    if (saveTimeout) clearTimeout(saveTimeout);
    saveTimeout = setTimeout(async () => {
      const state = {};
      for (const v of mediaItems) {
        if (v.kind !== 'video') continue;
        state[v.id] = {
          thumb_time: Number(v.thumb_time) || 0,
          anim_start: Number(v.anim_start) || 0,
          anim_duration: Number(v.anim_duration) || 1.0,
        };
      }
      try {
        await fetch('/api/save-state', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ state }),
        });
      } catch {
        // Ignore transient save errors
      }
    }, 350);
  }

  function getVisibleItems() {
    return mediaItems.filter(v => {
      if (activeCategory && v.category.toLowerCase() !== activeCategory) return false;
      if (activeKind && v.kind !== activeKind) return false;
      if (filterMissingOnly && (v.kind !== 'video' || (v.has_thumb && v.has_anim))) return false;
      return true;
    });
  }

  function getSelectedItem() {
    return mediaItems.find(v => v.id === selectedId) || null;
  }

  function getSelectedVideo() {
    const item = getSelectedItem();
    return item && item.kind === 'video' ? item : null;
  }

  function updateFilterButtons() {
    document.getElementById('filterPortfolioBtn').classList.toggle('active-pill', activeCategory === 'portfolio');
    document.getElementById('filterBlogBtn').classList.toggle('active-pill', activeCategory === 'blog');
    document.getElementById('filterVideoBtn').classList.toggle('active-pill', activeKind === 'video');
    document.getElementById('filterImagesBtn').classList.toggle('active-pill', activeKind === 'image');
    document.getElementById('filterMissingBtn').classList.toggle('active-pill', filterMissingOnly);
  }

  function renderTree() {
    const visible = getVisibleItems();
    const videosOnly = mediaItems.filter(v => v.kind === 'video');
    const imagesOnly = mediaItems.filter(v => v.kind === 'image');
    const thumbCount = videosOnly.filter(v => v.has_thumb).length;
    summaryText.textContent = `${videosOnly.length} videos · ${imagesOnly.length} images · ${thumbCount} _thumb`;

    const catOrder = ['portfolio', 'blog'];
    const grouped = new Map();

    for (const v of visible) {
      const cat = v.category || 'other';
      const sub = v.subfolder || '(root)';
      if (!grouped.has(cat)) grouped.set(cat, new Map());
      const subMap = grouped.get(cat);
      if (!subMap.has(sub)) subMap.set(sub, []);
      subMap.get(sub).push(v);
    }

    const sortedCats = Array.from(grouped.keys()).sort((a, b) => {
      const ia = catOrder.indexOf(a.toLowerCase());
      const ib = catOrder.indexOf(b.toLowerCase());
      if (ia !== -1 && ib !== -1) return ia - ib;
      if (ia !== -1) return -1;
      if (ib !== -1) return 1;
      return a.localeCompare(b);
    });

    videoListEl.innerHTML = '';

    for (const cat of sortedCats) {
      const subMap = grouped.get(cat);
      let catTotal = 0;
      for (const arr of subMap.values()) catTotal += arr.length;

      const catWrap = document.createElement('div');
      catWrap.className = 'tree-cat';
      const isCatCollapsed = collapsedCats.has(cat);

      const catHeader = document.createElement('div');
      catHeader.className = 'tree-cat-header';
      catHeader.innerHTML = `
        <span>${isCatCollapsed ? '▸' : '▾'} 📁 ${cat}</span>
        <span class="tree-header-actions">
          <span style="color:var(--muted); font-weight:600;">${catTotal}</span>
          <button type="button" class="icon-btn open-folder-btn" title="Open folder in Explorer">📂</button>
        </span>
      `;
      catHeader.querySelector('.open-folder-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        openInExplorer(cat === 'root' ? '' : cat, false);
      });
      catHeader.addEventListener('click', () => {
        if (collapsedCats.has(cat)) collapsedCats.delete(cat);
        else collapsedCats.add(cat);
        renderTree();
      });
      catWrap.appendChild(catHeader);

      if (!isCatCollapsed) {
        const catBody = document.createElement('div');
        catBody.className = 'tree-cat-body';

        const sortedSubs = Array.from(subMap.keys()).sort((a, b) => a.localeCompare(b));
        for (const sub of sortedSubs) {
          const items = subMap.get(sub);
          const subKey = `${cat}/${sub}`;
          const isSubCollapsed = collapsedSubs.has(subKey);

          const subWrap = document.createElement('div');
          subWrap.className = 'tree-sub';

          if (sub !== '(root)') {
            const subHeader = document.createElement('div');
            subHeader.className = 'tree-sub-header';
            subHeader.innerHTML = `
              <span>${isSubCollapsed ? '▸' : '▾'} 📂 ${sub}</span>
              <span class="tree-header-actions">
                <span>${items.length}</span>
                <button type="button" class="icon-btn open-folder-btn" title="Open folder in Explorer">📂</button>
              </span>
            `;
            subHeader.querySelector('.open-folder-btn').addEventListener('click', (e) => {
              e.stopPropagation();
              openInExplorer(`${cat}/${sub}`, false);
            });
            subHeader.addEventListener('click', () => {
              if (collapsedSubs.has(subKey)) collapsedSubs.delete(subKey);
              else collapsedSubs.add(subKey);
              renderTree();
            });
            subWrap.appendChild(subHeader);
          }

          if (!isSubCollapsed || sub === '(root)') {
            const itemsContainer = document.createElement('div');
            itemsContainer.className = sub === '(root)' ? '' : 'tree-sub-items';
            for (const item of items) {
              const row = document.createElement('div');
              row.className = 'video-item' + (item.id === selectedId ? ' selected' : '');
              const metaHtml = item.kind === 'video'
                ? `
                  <div class="video-meta">
                    <div class="video-title" title="${item.id}">${item.stem}</div>
                    <div class="video-sub">
                      <span>${item.duration.toFixed(1)}s</span>
                      <span class="badge ${item.has_thumb ? 'badge-ok' : 'badge-off'}">THUMB</span>
                      <span class="badge ${item.has_anim ? 'badge-ok' : 'badge-off'}">ANIM</span>
                    </div>
                  </div>
                `
                : `
                  <div class="video-meta">
                    <div class="video-title" title="${item.id}">${item.stem}</div>
                    <div class="video-sub">
                      <span class="badge badge-img">IMG</span>
                      <span>${Math.round(item.size_kb)} KB</span>
                    </div>
                  </div>
                `;
              row.innerHTML = `
                ${metaHtml}
                <div class="video-item-actions">
                  <button type="button" class="icon-btn reveal-btn" title="Reveal in Explorer">📂</button>
                  <button type="button" class="icon-btn icon-del delete-btn" title="Delete file from Converted">🗑</button>
                </div>
              `;
              row.querySelector('.reveal-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                openInExplorer(item.id, true);
              });
              row.querySelector('.delete-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                deleteMediaItem(item);
              });
              row.addEventListener('click', () => selectItem(item.id));
              itemsContainer.appendChild(row);
            }
            subWrap.appendChild(itemsContainer);
          }

          catBody.appendChild(subWrap);
        }

        catWrap.appendChild(catBody);
      }

      videoListEl.appendChild(catWrap);
    }
  }

  function updatePreviewSourceVisibility() {
    const v = getSelectedVideo();
    previewSourceLiveBtn.classList.toggle('active-pill', previewSourceMode === 'live');
    previewSourceDiskBtn.classList.toggle('active-pill', previewSourceMode === 'disk');
    if (!v) return;

    const showDiskThumb = previewSourceMode === 'disk' && v.has_thumb;
    const showDiskAnim = previewSourceMode === 'disk' && v.has_anim;

    canvas.style.display = showDiskThumb ? 'none' : 'block';
    savedThumbImg.style.display = showDiskThumb ? 'block' : 'none';

    animPreviewVideo.style.display = showDiskAnim ? 'none' : 'block';
    savedAnimImg.style.display = showDiskAnim ? 'block' : 'none';

    const ts = Date.now();
    if (v.has_thumb) savedThumbImg.src = `/api/file?rel=${encodeURIComponent(v.thumb_rel)}&t=${ts}`;
    if (v.has_anim) savedAnimImg.src = `/api/file?rel=${encodeURIComponent(v.anim_rel)}&t=${ts}`;
  }

  function updateCheckboxVisualState() {
    const v = getSelectedVideo();
    if (!v) return;
    enableThumbToggle.checked = Boolean(v.enable_thumb);
    enableAnimToggle.checked = Boolean(v.enable_anim);

    thumbControlCard.classList.toggle('is-disabled', !v.enable_thumb);
    animControlCard.classList.toggle('is-disabled', !v.enable_anim);
    thumbPreviewPane.classList.toggle('is-disabled', !v.enable_thumb);
    thumbPreviewPane.classList.toggle('is-enabled', Boolean(v.enable_thumb));
    animPreviewPane.classList.toggle('is-disabled', !v.enable_anim);
    animPreviewPane.classList.toggle('is-enabled', Boolean(v.enable_anim));

    thumbStatusLabel.textContent = v.has_thumb ? 'Saved on disk' : 'Not created yet';
    thumbStatusLabel.style.color = v.has_thumb ? 'var(--green)' : 'var(--muted)';
    previewSourceDiskBtn.disabled = !(v.has_thumb || v.has_anim);
    if (previewSourceMode === 'disk' && !(v.has_thumb || v.has_anim)) {
      previewSourceMode = 'live';
    }
    updatePreviewSourceVisibility();
  }

  function setMainPreviewMode(mode) {
    mainPreviewMode = mode;
    previewThumbBtn.classList.toggle('active-pill', mainPreviewMode === 'thumb');
    previewAnimBtn.classList.toggle('active-pill', mainPreviewMode === 'anim');

    const v = getSelectedVideo();
    if (!v) return;

    if (mainPreviewMode === 'thumb') {
      player.pause();
      pendingSnap = true;
      player.currentTime = v.thumb_time;
    } else if (mainPreviewMode === 'anim') {
      player.currentTime = v.anim_start;
      player.play().catch(() => {});
    }
  }

  function selectItem(id) {
    selectedId = id;
    renderTree();
    const item = getSelectedItem();
    if (!item) {
      emptyState.style.display = 'block';
      editorContent.style.display = 'none';
      imageViewerContent.style.display = 'none';
      generateSingleBtn.disabled = true;
      return;
    }

    if (item.kind === 'image') {
      player.pause();
      animPreviewVideo.pause();
      emptyState.style.display = 'none';
      editorContent.style.display = 'none';
      imageViewerContent.style.display = 'flex';
      generateSingleBtn.disabled = true;

      imageViewerTitle.textContent = item.image_rel;
      imageMetaLabel.textContent = `${Math.round(item.size_kb)} KB`;
      imagePathText.textContent = item.image_rel;
      imageMarkdocText.textContent = `{% media src="${item.image_rel}" /%}`;
      imageViewerImg.onload = () => {
        imageMetaLabel.textContent = `${imageViewerImg.naturalWidth}×${imageViewerImg.naturalHeight} · ${Math.round(item.size_kb)} KB`;
      };
      imageViewerImg.src = `/api/file?rel=${encodeURIComponent(item.image_rel)}&t=${Date.now()}`;
      return;
    }

    const v = item;
    emptyState.style.display = 'none';
    imageViewerContent.style.display = 'none';
    editorContent.style.display = 'grid';
    generateSingleBtn.disabled = false;

    currentVideoTitle.textContent = `${v.id} (${v.width}×${v.height}, ${v.fps} fps)`;
    videoPathText.textContent = v.video_rel;
    thumbPathText.textContent = v.thumb_rel;
    animPathText.textContent = v.anim_rel;

    previewSourceMode = 'live';
    updateCheckboxVisualState();

    const dur = Math.max(v.duration, 0.1);
    thumbSlider.max = String(dur);
    thumbSlider.value = String(v.thumb_time);
    animStartInput.max = String(Math.max(0, dur - 0.2));
    animStartInput.value = String(v.anim_start);
    animDurInput.value = String(v.anim_duration);

    pendingSnap = true;
    const streamUrl = `/api/stream?id=${encodeURIComponent(v.id)}`;
    player.src = streamUrl;
    player.currentTime = v.thumb_time;

    thumbCaptureVideo.src = streamUrl;
    thumbCaptureVideo.currentTime = v.thumb_time;

    animPreviewVideo.src = streamUrl;
    animPreviewVideo.currentTime = v.anim_start;
    animPreviewVideo.play().catch(() => {});

    setMainPreviewMode('free');
    updateBadgesAndTimeline();
  }

  function snapCanvasToSource(videoEl) {
    const srcEl = videoEl || thumbCaptureVideo;
    if (!srcEl.videoWidth || !srcEl.videoHeight) return;
    const targetW = 960, targetH = 540;
    const srcRatio = srcEl.videoWidth / srcEl.videoHeight;
    const dstRatio = targetW / targetH;
    let sx = 0, sy = 0, sw = srcEl.videoWidth, sh = srcEl.videoHeight;
    if (srcRatio > dstRatio) {
      sw = srcEl.videoHeight * dstRatio;
      sx = (srcEl.videoWidth - sw) / 2;
    } else {
      sh = srcEl.videoWidth / dstRatio;
      sy = (srcEl.videoHeight - sh) / 2;
    }
    ctx.drawImage(srcEl, sx, sy, sw, sh, 0, 0, targetW, targetH);
  }

  function updateBadgesAndTimeline() {
    const v = getSelectedVideo();
    if (!v) return;
    const dur = Math.max(v.duration, 0.1);
    const end = Math.min(dur, v.anim_start + v.anim_duration);

    thumbTimeBadge.textContent = `${v.thumb_time.toFixed(2)}s`;
    animShortBadge.textContent = `${v.anim_start.toFixed(2)}s–${end.toFixed(2)}s`;
    animRangeBadge.textContent = `${v.anim_start.toFixed(2)}s — ${end.toFixed(2)}s (${v.anim_duration.toFixed(2)}s @ 24fps)`;

    const leftPct = (v.anim_start / dur) * 100;
    const widthPct = Math.min(100 - leftPct, (v.anim_duration / dur) * 100);
    timelineWindow.style.left = `${leftPct}%`;
    timelineWindow.style.width = `${Math.max(widthPct, 1)}%`;

    timelineThumbMarker.style.left = `${(v.thumb_time / dur) * 100}%`;
    timelinePlayhead.style.left = `${((player.currentTime || 0) / dur) * 100}%`;
    currentTimeLabel.textContent = `${(player.currentTime || 0).toFixed(2)}s / ${dur.toFixed(2)}s`;

    document.querySelectorAll('.dur-preset').forEach(btn => {
      btn.classList.toggle('active-pill', Math.abs(parseFloat(btn.dataset.dur) - v.anim_duration) < 0.02);
    });
  }

  thumbCaptureVideo.addEventListener('loadeddata', () => {
    const v = getSelectedVideo();
    if (v) thumbCaptureVideo.currentTime = v.thumb_time;
  });

  thumbCaptureVideo.addEventListener('seeked', () => {
    snapCanvasToSource(thumbCaptureVideo);
  });

  animPreviewVideo.addEventListener('loadeddata', () => {
    const v = getSelectedVideo();
    if (v) {
      animPreviewVideo.currentTime = v.anim_start;
      animPreviewVideo.play().catch(() => {});
    }
  });

  animPreviewVideo.addEventListener('timeupdate', () => {
    const v = getSelectedVideo();
    if (!v) return;
    const end = Math.min(v.duration, v.anim_start + v.anim_duration);
    if (animPreviewVideo.currentTime >= end || animPreviewVideo.currentTime < v.anim_start - 0.05) {
      animPreviewVideo.currentTime = v.anim_start;
      if (animPreviewVideo.paused) animPreviewVideo.play().catch(() => {});
    }
  });

  player.addEventListener('loadeddata', () => {
    const v = getSelectedVideo();
    if (v && pendingSnap) player.currentTime = v.thumb_time;
  });

  player.addEventListener('seeked', () => {
    if (pendingSnap) {
      snapCanvasToSource(player);
      pendingSnap = false;
    }
    updateBadgesAndTimeline();
  });

  player.addEventListener('play', () => {
    if (mainPreviewMode === 'thumb') setMainPreviewMode('free');
  });

  player.addEventListener('timeupdate', () => {
    const v = getSelectedVideo();
    if (!v) return;
    if (mainPreviewMode === 'anim') {
      const end = Math.min(v.duration, v.anim_start + v.anim_duration);
      if (player.currentTime >= end || player.currentTime < v.anim_start - 0.05) {
        player.currentTime = v.anim_start;
        if (player.paused) player.play().catch(() => {});
      }
    }
    updateBadgesAndTimeline();
  });

  enableThumbToggle.addEventListener('change', () => {
    const v = getSelectedVideo();
    if (!v) return;
    v.enable_thumb = enableThumbToggle.checked;
    updateCheckboxVisualState();
  });

  enableAnimToggle.addEventListener('change', () => {
    const v = getSelectedVideo();
    if (!v) return;
    v.enable_anim = enableAnimToggle.checked;
    updateCheckboxVisualState();
  });

  previewSourceLiveBtn.addEventListener('click', () => {
    previewSourceMode = 'live';
    updatePreviewSourceVisibility();
  });

  previewSourceDiskBtn.addEventListener('click', () => {
    previewSourceMode = 'disk';
    updatePreviewSourceVisibility();
  });

  previewThumbBtn.addEventListener('click', () => {
    setMainPreviewMode(mainPreviewMode === 'thumb' ? 'free' : 'thumb');
  });

  previewAnimBtn.addEventListener('click', () => {
    setMainPreviewMode(mainPreviewMode === 'anim' ? 'free' : 'anim');
  });

  thumbSlider.addEventListener('input', () => {
    const v = getSelectedVideo();
    if (!v) return;
    previewSourceMode = 'live';
    v.thumb_time = parseFloat(thumbSlider.value) || 0;
    v.enable_thumb = true;
    updateCheckboxVisualState();
    thumbCaptureVideo.currentTime = v.thumb_time;
    setMainPreviewMode('thumb');
    updateBadgesAndTimeline();
    scheduleSaveState();
  });

  const stepPlayer = (delta) => {
    const v = getSelectedVideo();
    if (!v) return;
    if (mainPreviewMode === 'thumb') {
      const next = Math.max(0, Math.min(v.duration, v.thumb_time + delta));
      v.thumb_time = Math.round(next * 100) / 100;
      thumbSlider.value = String(v.thumb_time);
      v.enable_thumb = true;
      updateCheckboxVisualState();
      thumbCaptureVideo.currentTime = v.thumb_time;
      setMainPreviewMode('thumb');
      updateBadgesAndTimeline();
      scheduleSaveState();
    } else {
      player.pause();
      const next = Math.max(0, Math.min(v.duration, (player.currentTime || 0) + delta));
      player.currentTime = next;
      updateBadgesAndTimeline();
    }
  };

  document.getElementById('stepBack1s').addEventListener('click', () => stepPlayer(-1));
  document.getElementById('stepFwd1s').addEventListener('click', () => stepPlayer(1));
  document.getElementById('stepBackFrame').addEventListener('click', () => {
    const v = getSelectedVideo();
    stepPlayer(-1 / (v ? v.fps : 24));
  });
  document.getElementById('stepFwdFrame').addEventListener('click', () => {
    const v = getSelectedVideo();
    stepPlayer(1 / (v ? v.fps : 24));
  });

  function syncAnimPreviewPlayer() {
    const v = getSelectedVideo();
    if (!v) return;
    animPreviewVideo.currentTime = v.anim_start;
    if (animPreviewVideo.paused) animPreviewVideo.play().catch(() => {});
  }

  function applyAnimDuration(newDur) {
    const v = getSelectedVideo();
    if (!v) return;
    previewSourceMode = 'live';
    const clamped = Math.round(Math.max(0.2, Math.min(Math.min(5.0, v.duration), newDur)) * 100) / 100;
    v.anim_duration = clamped;
    if (v.anim_start + v.anim_duration > v.duration) {
      v.anim_start = Math.max(0, Math.round((v.duration - v.anim_duration) * 100) / 100);
      animStartInput.value = String(v.anim_start);
    }
    animDurInput.value = String(v.anim_duration);
    v.enable_anim = true;
    updateCheckboxVisualState();
    syncAnimPreviewPlayer();
    setMainPreviewMode('anim');
    updateBadgesAndTimeline();
    scheduleSaveState();
  }

  animStartInput.addEventListener('input', () => {
    const v = getSelectedVideo();
    if (!v) return;
    previewSourceMode = 'live';
    v.anim_start = Math.max(0, Math.min(v.duration - 0.2, parseFloat(animStartInput.value) || 0));
    v.enable_anim = true;
    updateCheckboxVisualState();
    syncAnimPreviewPlayer();
    setMainPreviewMode('anim');
    updateBadgesAndTimeline();
    scheduleSaveState();
  });

  animDurInput.addEventListener('input', () => {
    applyAnimDuration(parseFloat(animDurInput.value) || 1.0);
  });

  document.getElementById('durDecBtn').addEventListener('click', () => {
    const v = getSelectedVideo();
    if (v) applyAnimDuration(v.anim_duration - 0.1);
  });

  document.getElementById('durIncBtn').addEventListener('click', () => {
    const v = getSelectedVideo();
    if (v) applyAnimDuration(v.anim_duration + 0.1);
  });

  document.querySelectorAll('.dur-preset').forEach(btn => {
    btn.addEventListener('click', () => applyAnimDuration(parseFloat(btn.dataset.dur)));
  });

  // Dragging on the range timeline track
  let dragMode = null;
  let dragStartX = 0;
  let initialStart = 0;
  let initialDur = 1.0;

  timelineTrack.addEventListener('pointerdown', (e) => {
    const v = getSelectedVideo();
    if (!v) return;
    previewSourceMode = 'live';
    const rect = timelineTrack.getBoundingClientRect();
    const winRect = timelineWindow.getBoundingClientRect();
    dragStartX = e.clientX;
    initialStart = v.anim_start;
    initialDur = v.anim_duration;

    if (Math.abs(e.clientX - winRect.left) <= 10) {
      dragMode = 'left';
    } else if (Math.abs(e.clientX - winRect.right) <= 10) {
      dragMode = 'right';
    } else if (e.clientX >= winRect.left && e.clientX <= winRect.right) {
      dragMode = 'move';
    } else {
      const clickTime = ((e.clientX - rect.left) / rect.width) * v.duration;
      v.anim_start = Math.max(0, Math.min(v.duration - v.anim_duration, clickTime - v.anim_duration / 2));
      animStartInput.value = v.anim_start.toFixed(2);
      syncAnimPreviewPlayer();
      dragMode = 'move';
      initialStart = v.anim_start;
    }
    v.enable_anim = true;
    updateCheckboxVisualState();
    setMainPreviewMode('anim');
    timelineTrack.setPointerCapture(e.pointerId);
    updateBadgesAndTimeline();
    scheduleSaveState();
  });

  timelineTrack.addEventListener('pointermove', (e) => {
    if (!dragMode) return;
    const v = getSelectedVideo();
    if (!v) return;
    const rect = timelineTrack.getBoundingClientRect();
    const dt = ((e.clientX - dragStartX) / rect.width) * v.duration;

    if (dragMode === 'move') {
      v.anim_start = Math.max(0, Math.min(v.duration - v.anim_duration, initialStart + dt));
      animPreviewVideo.currentTime = v.anim_start;
      player.currentTime = v.anim_start;
    } else if (dragMode === 'left') {
      const newStart = Math.max(0, Math.min(initialStart + initialDur - 0.2, initialStart + dt));
      v.anim_duration = Math.max(0.2, initialDur + (initialStart - newStart));
      v.anim_start = newStart;
      animPreviewVideo.currentTime = v.anim_start;
      player.currentTime = v.anim_start;
    } else if (dragMode === 'right') {
      v.anim_duration = Math.max(0.2, Math.min(v.duration - initialStart, initialDur + dt));
    }

    animStartInput.value = v.anim_start.toFixed(2);
    animDurInput.value = v.anim_duration.toFixed(2);
    updateBadgesAndTimeline();
    scheduleSaveState();
  });

  timelineTrack.addEventListener('pointerup', () => { dragMode = null; });

  // Keyboard shortcuts (Space = Play/Pause, Left/Right = frame step)
  document.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea') return;
    const v = getSelectedVideo();
    if (!v) return;
    if (e.code === 'Space') {
      e.preventDefault();
      if (player.paused) player.play().catch(() => {});
      else player.pause();
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      stepPlayer(-1 / (v.fps || 24));
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      stepPlayer(1 / (v.fps || 24));
    }
  });

  document.getElementById('filterPortfolioBtn').addEventListener('click', () => {
    activeCategory = activeCategory === 'portfolio' ? null : 'portfolio';
    updateFilterButtons();
    renderTree();
  });
  document.getElementById('filterBlogBtn').addEventListener('click', () => {
    activeCategory = activeCategory === 'blog' ? null : 'blog';
    updateFilterButtons();
    renderTree();
  });
  document.getElementById('filterVideoBtn').addEventListener('click', () => {
    activeKind = activeKind === 'video' ? null : 'video';
    updateFilterButtons();
    renderTree();
  });
  document.getElementById('filterImagesBtn').addEventListener('click', () => {
    activeKind = activeKind === 'image' ? null : 'image';
    if (activeKind === 'image') filterMissingOnly = false;
    updateFilterButtons();
    renderTree();
  });
  document.getElementById('filterMissingBtn').addEventListener('click', () => {
    filterMissingOnly = !filterMissingOnly;
    if (filterMissingOnly && activeKind === 'image') activeKind = 'video';
    updateFilterButtons();
    renderTree();
  });

  async function openInExplorer(rel, selectFile = false, root = 'converted') {
    try {
      const res = await fetch('/api/open-explorer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rel, select: selectFile, root }),
      });
      const data = await res.json();
      if (!data.ok) showToast(data.error || 'Could not open Explorer.', true);
    } catch {
      showToast('Failed to open Explorer.', true);
    }
  }

  async function deleteMediaItem(item) {
    const extraMsg = item.kind === 'video' && (item.has_thumb || item.has_anim)
      ? '\n\nIts generated _thumb.webp and _animthumb.webp files will also be deleted.'
      : '';
    if (!window.confirm(`Delete "${item.id}" from Converted/?${extraMsg}`)) {
      return;
    }

    // Release active video handles before Windows deletes the file
    if (selectedId === item.id) {
      for (const el of [player, thumbCaptureVideo, animPreviewVideo]) {
        el.pause();
        el.removeAttribute('src');
        el.load();
      }
      selectedId = null;
      await new Promise(r => setTimeout(r, 80));
    }

    try {
      const res = await fetch('/api/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: item.id, kind: item.kind }),
      });
      const data = await res.json();
      if (!data.ok) {
        showToast(data.error || 'Failed to delete file.', true);
        return;
      }
      showToast(`Deleted ${item.stem}`);
      await loadCatalog(true);
      if (!selectedId) {
        emptyState.style.display = 'block';
        editorContent.style.display = 'none';
        imageViewerContent.style.display = 'none';
        generateSingleBtn.disabled = true;
      }
    } catch {
      showToast('Delete request failed.', true);
    }
  }

  function bindCopyBtn(btnId, getText) {
    document.getElementById(btnId).addEventListener('click', (e) => {
      const item = getSelectedItem();
      if (!item) return;
      const text = getText(item);
      if (!text) return;
      navigator.clipboard.writeText(text);
      const btn = e.target;
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = 'Copy'; }, 1200);
    });
  }

  bindCopyBtn('copyVideoPathBtn', v => v.video_rel);
  bindCopyBtn('copyThumbPathBtn', v => v.thumb_rel);
  bindCopyBtn('copyAnimPathBtn', v => v.anim_rel);
  bindCopyBtn('copyImagePathBtn', i => i.image_rel);
  bindCopyBtn('copyImageMarkdocBtn', i => i.image_rel ? `{% media src="${i.image_rel}" /%}` : '');

  async function runGenerateBatch(itemsPayload, toastLabel) {
    generateSingleBtn.disabled = true;
    updateAllThumbsBtn.disabled = true;
    showToast(toastLabel);
    try {
      const res = await fetch('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: itemsPayload }),
      });
      const data = await res.json();
      if (data.updated) {
        for (const [vidId, info] of Object.entries(data.updated)) {
          const item = mediaItems.find(v => v.id === vidId);
          if (item) {
            item.has_thumb = info.has_thumb;
            item.has_anim = info.has_anim;
            item.enable_thumb = info.has_thumb;
            item.enable_anim = info.has_anim;
          }
        }
      }
      updateCheckboxVisualState();
      renderTree();
      showToast(`Updated ${Object.keys(data.updated || {}).length} video(s)!`);
    } catch {
      showToast('Thumbnail generation failed. Check console.', true);
    } finally {
      generateSingleBtn.disabled = !getSelectedVideo();
      updateAllThumbsBtn.disabled = false;
    }
  }

  generateSingleBtn.addEventListener('click', () => {
    const v = getSelectedVideo();
    if (!v) return;
    if (!v.enable_thumb && !v.enable_anim) {
      showToast('Check Static Thumbnail and/or Animated Thumbnail below first.', true);
      return;
    }
    runGenerateBatch(
      [{
        id: v.id,
        do_thumb: Boolean(v.enable_thumb),
        do_anim: Boolean(v.enable_anim),
        thumb_time: Number(v.thumb_time) || 0,
        anim_start: Number(v.anim_start) || 0,
        anim_duration: Number(v.anim_duration) || 1.0,
      }],
      `Generating thumbnail(s) for ${v.stem}...`
    );
  });

  updateAllThumbsBtn.addEventListener('click', () => {
    const targets = mediaItems.filter(v => v.kind === 'video' && (v.enable_thumb || v.enable_anim || v.has_thumb || v.has_anim));
    if (!targets.length) {
      showToast('No videos have existing or enabled thumbnails to update.', true);
      return;
    }
    if (!window.confirm(`Rebuild and overwrite thumbnails for ${targets.length} video(s) using saved settings?`)) {
      return;
    }
    runGenerateBatch(
      targets.map(v => ({
        id: v.id,
        do_thumb: Boolean(v.enable_thumb || v.has_thumb),
        do_anim: Boolean(v.enable_anim || v.has_anim),
        thumb_time: Number(v.thumb_time) || 0,
        anim_start: Number(v.anim_start) || 0,
        anim_duration: Number(v.anim_duration) || 1.0,
      })),
      `Rebuilding thumbnails for ${targets.length} video(s)...`
    );
  });

  convertNewBtn.addEventListener('click', async () => {
    convertNewBtn.disabled = true;
    convertNewBtn.textContent = '⏳ Converting Source Videos...';
    showToast('Converting new files from _toConvert...');
    try {
      const res = await fetch('/api/convert-new', { method: 'POST' });
      const data = await res.json();
      await loadCatalog(true);
      showToast(`Converted ${data.converted_count || 0} new file(s).`);
    } catch {
      showToast('Conversion failed. Check console.', true);
    } finally {
      convertNewBtn.disabled = false;
    }
  });

  document.getElementById('finishBtn').addEventListener('click', async () => {
    document.getElementById('finishBtn').disabled = true;
    await fetch('/api/close', { method: 'POST' });
    document.body.innerHTML = `
      <div style="margin:auto; text-align:center; padding:40px;">
        <h2 style="margin-bottom:8px;">Video &amp; Thumbnail Studio Closed</h2>
        <p style="color:#a1a1aa;">Settings saved. You can close this browser tab.</p>
      </div>
    `;
    window.close();
  });

  // Resizable main video player height (drag handle + Fit Screen + persistence)
  function setPlayerHeightPx(px, persist = true) {
    const minH = 220;
    const maxH = Math.round(window.innerHeight * 0.78);
    const clamped = Math.max(minH, Math.min(maxH, Math.round(px)));
    playerStage.style.setProperty('--player-h', `${clamped}px`);
    if (persist) {
      try { localStorage.setItem('ad_studio_player_h', String(clamped)); } catch {}
    }
  }

  function autoFitPlayerToScreen() {
    const headerH = document.querySelector('header')?.offsetHeight || 52;
    const thumbCardH = thumbControlCard.offsetHeight || 74;
    const animCardH = animControlCard.offsetHeight || 128;
    // Account for editor padding, gaps, player header, resizer, and control row
    const chromeH = headerH + thumbCardH + animCardH + 132;
    const availH = Math.max(220, window.innerHeight - chromeH);
    setPlayerHeightPx(availH, true);
  }

  try {
    const savedH = parseInt(localStorage.getItem('ad_studio_player_h') || '', 10);
    if (!Number.isNaN(savedH) && savedH >= 220) {
      setPlayerHeightPx(savedH, false);
    }
  } catch {}

  let resizeDragActive = false;
  let resizeStartY = 0;
  let resizeStartH = 0;

  playerResizer.addEventListener('pointerdown', (e) => {
    resizeDragActive = true;
    resizeStartY = e.clientY;
    resizeStartH = playerStage.getBoundingClientRect().height;
    playerResizer.setPointerCapture(e.pointerId);
  });

  playerResizer.addEventListener('pointermove', (e) => {
    if (!resizeDragActive) return;
    setPlayerHeightPx(resizeStartH + (e.clientY - resizeStartY), true);
  });

  playerResizer.addEventListener('pointerup', () => { resizeDragActive = false; });
  playerResizer.addEventListener('dblclick', () => autoFitPlayerToScreen());
  fitPlayerHeightBtn.addEventListener('click', () => autoFitPlayerToScreen());

  // Resizable right Previews column width (horizontal splitter + persistence)
  function setPreviewColWidthPx(px, persist = true) {
    const minW = 240;
    const maxW = Math.min(580, Math.round(window.innerWidth * 0.45));
    const clamped = Math.max(minW, Math.min(maxW, Math.round(px)));
    editorContent.style.setProperty('--preview-col-w', `${clamped}px`);
    if (persist) {
      try { localStorage.setItem('ad_studio_preview_w', String(clamped)); } catch {}
    }
  }

  try {
    const savedW = parseInt(localStorage.getItem('ad_studio_preview_w') || '', 10);
    if (!Number.isNaN(savedW) && savedW >= 240) {
      setPreviewColWidthPx(savedW, false);
    }
  } catch {}

  let colDragActive = false;
  let colStartX = 0;
  let colStartW = 310;

  previewColResizer.addEventListener('pointerdown', (e) => {
    colDragActive = true;
    colStartX = e.clientX;
    const rightCol = editorContent.querySelector('.right-column');
    colStartW = rightCol ? rightCol.getBoundingClientRect().width : 310;
    previewColResizer.setPointerCapture(e.pointerId);
  });

  previewColResizer.addEventListener('pointermove', (e) => {
    if (!colDragActive) return;
    // Dragging left increases right-column width; dragging right decreases it
    setPreviewColWidthPx(colStartW + (colStartX - e.clientX), true);
  });

  previewColResizer.addEventListener('pointerup', () => { colDragActive = false; });
  previewColResizer.addEventListener('dblclick', () => setPreviewColWidthPx(310, true));

  loadCatalog(false);
</script>
</body>
</html>
"""


def run_thumbnail_studio(auto_upload: bool = False) -> list[Path]:
    """
    Launches the local English Video & Thumbnail Studio for converted videos & images in Converted/.
    """
    catalog = collect_converted_media_catalog()
    catalog_map = {item["id"]: item for item in catalog if item.get("kind") == "video"}
    state = load_studio_state()
    produced_files: list[Path] = []
    done_event = threading.Event()

    class StudioHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            pass

        def _send_json(self, obj: dict, status: int = 200) -> None:
            payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                body = THUMB_STUDIO_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/videos":
                nonlocal catalog, catalog_map
                catalog = collect_converted_media_catalog()
                catalog_map = {item["id"]: item for item in catalog if item.get("kind") == "video"}
                self._send_json(
                    {
                        "items": catalog,
                        "pending_to_convert": count_pending_to_convert(),
                    }
                )
                return

            if parsed.path == "/api/file":
                qs = urllib.parse.parse_qs(parsed.query)
                rel = (qs.get("rel") or [""])[0]
                file_path = (CONVERTED_DIR / rel).resolve()
                if not file_path.is_relative_to(CONVERTED_DIR.resolve()) or not file_path.is_file():
                    self.send_error(404)
                    return
                content_type, _ = mimetypes.guess_type(str(file_path))
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type or "application/octet-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if parsed.path == "/api/stream":
                qs = urllib.parse.parse_qs(parsed.query)
                vid_id = (qs.get("id") or [""])[0]
                item = catalog_map.get(vid_id)
                if not item:
                    self.send_error(404)
                    return
                file_path = Path(item["video_path"])
                if not file_path.is_file():
                    self.send_error(404)
                    return

                stat = file_path.stat()
                content_type, _ = mimetypes.guess_type(str(file_path))
                content_type = content_type or "video/mp4"
                range_header = self.headers.get("Range")

                try:
                    if range_header and range_header.startswith("bytes="):
                        parts = range_header.replace("bytes=", "").split("-")
                        start = int(parts[0]) if parts[0] else 0
                        end = int(parts[1]) if len(parts) > 1 and parts[1] else stat.st_size - 1
                        end = min(end, stat.st_size - 1)
                        length = max(0, end - start + 1)

                        self.send_response(206)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Content-Range", f"bytes {start}-{end}/{stat.st_size}")
                        self.send_header("Content-Length", str(length))
                        self.end_headers()
                        with file_path.open("rb") as f:
                            f.seek(start)
                            remaining = length
                            while remaining > 0:
                                chunk = f.read(min(65536, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                    else:
                        self.send_response(200)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Content-Length", str(stat.st_size))
                        self.end_headers()
                        with file_path.open("rb") as f:
                            while True:
                                chunk = f.read(65536)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                except (ConnectionResetError, BrokenPipeError):
                    pass
                return

            self.send_error(404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            data = json.loads(raw)

            if self.path == "/api/save-state":
                incoming = data.get("state", {})
                if isinstance(incoming, dict):
                    state.update(incoming)
                    save_studio_state(state)
                self._send_json({"ok": True})
                return

            if self.path == "/api/convert-new":
                newly_converted = convert_all()
                produced_files.extend(newly_converted)
                if auto_upload and newly_converted:
                    upload_to_r2(newly_converted)
                self._send_json({"ok": True, "converted_count": len(newly_converted)})
                return

            if self.path == "/api/generate":
                items = data.get("items", [])
                updated: dict[str, dict] = {}
                batch_files: list[Path] = []
                for entry in items:
                    vid_id = entry.get("id")
                    item = catalog_map.get(vid_id)
                    if not item:
                        continue
                    do_thumb = bool(entry.get("do_thumb", False))
                    do_anim = bool(entry.get("do_anim", False))
                    t_thumb = float(entry.get("thumb_time", item["thumb_time"]))
                    t_start = float(entry.get("anim_start", item["anim_start"]))
                    t_dur = float(entry.get("anim_duration", item["anim_duration"]))

                    item["thumb_time"] = t_thumb
                    item["anim_start"] = t_start
                    item["anim_duration"] = t_dur
                    state[vid_id] = {
                        "thumb_time": t_thumb,
                        "anim_start": t_start,
                        "anim_duration": t_dur,
                    }

                    video_path = Path(item["video_path"])
                    out_thumb = Path(item["out_thumb"])
                    out_anim = Path(item["out_anim"])

                    if do_thumb:
                        print(f"[Static Thumb] {item['id']} -> _thumb.webp ({THUMB_WIDTH}x{THUMB_HEIGHT} @ {t_thumb:.2f}s)")
                        generate_static_thumb(video_path, out_thumb, t_thumb)
                        print(f"  [OK] _thumb: {out_thumb.stat().st_size / 1024:.0f} KB")
                        item["has_thumb"] = True
                        batch_files.append(out_thumb)

                    if do_anim:
                        print(
                            f"[Anim Thumb] {item['id']} -> _animthumb.webp "
                            f"({ANIM_THUMB_WIDTH}x{ANIM_THUMB_HEIGHT} @ {ANIM_THUMB_FPS}fps, {t_start:.2f}s + {t_dur:.2f}s)"
                        )
                        generate_anim_thumb(video_path, out_anim, t_start, t_dur)
                        print(f"  [OK] _animthumb: {out_anim.stat().st_size / 1024:.0f} KB")
                        item["has_anim"] = True
                        batch_files.append(out_anim)

                    updated[vid_id] = {
                        "has_thumb": item["has_thumb"],
                        "has_anim": item["has_anim"],
                    }

                save_studio_state(state)
                produced_files.extend(batch_files)
                if auto_upload and batch_files:
                    upload_to_r2(batch_files)

                self._send_json({"ok": True, "updated": updated})
                return

            if self.path == "/api/open-explorer":
                root_kind = data.get("root", "converted")
                rel = str(data.get("rel", "") or "").strip()
                select_file = bool(data.get("select", False))
                base = INPUT_DIR.resolve() if root_kind == "source" else CONVERTED_DIR.resolve()
                base.mkdir(parents=True, exist_ok=True)
                target = (base / rel).resolve() if rel else base
                if not target.is_relative_to(base) or not target.exists():
                    self._send_json({"ok": False, "error": "Target path not found."}, status=404)
                    return
                try:
                    if os.name == "nt":
                        if select_file and target.is_file():
                            subprocess.Popen(["explorer.exe", f"/select,{target}"])
                        else:
                            folder = target if target.is_dir() else target.parent
                            subprocess.Popen(["explorer.exe", str(folder)])
                    else:
                        folder = target if target.is_dir() else target.parent
                        webbrowser.open(folder.as_uri())
                    self._send_json({"ok": True})
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=500)
                return

            if self.path == "/api/delete":
                rel = str(data.get("id", "") or "").strip()
                kind = str(data.get("kind", "") or "")
                base = CONVERTED_DIR.resolve()
                target = (base / rel).resolve() if rel else None
                if not target or not target.is_relative_to(base) or not target.is_file():
                    self._send_json({"ok": False, "error": "File not found."}, status=404)
                    return

                to_remove = [target]
                if kind == "video":
                    out_thumb = target.parent / f"{target.stem}_thumb.webp"
                    out_anim = target.parent / f"{target.stem}_animthumb.webp"
                    if out_thumb.is_file():
                        to_remove.append(out_thumb)
                    if out_anim.is_file():
                        to_remove.append(out_anim)

                deleted_paths: list[str] = []
                for p in to_remove:
                    removed = False
                    last_err = None
                    for _ in range(6):
                        try:
                            p.unlink(missing_ok=True)
                            removed = True
                            break
                        except PermissionError as exc:
                            last_err = exc
                            time.sleep(0.12)
                    if not removed:
                        self._send_json({"ok": False, "error": f"File is in use: {last_err}"}, status=423)
                        return
                    deleted_paths.append(p.relative_to(base).as_posix())
                    print(f"[Deleted] Converted/{p.relative_to(base).as_posix()}")

                if rel in state:
                    del state[rel]
                    save_studio_state(state)

                self._send_json({"ok": True, "deleted": deleted_paths})
                return

            if self.path == "/api/close":
                save_studio_state(state)
                self._send_json({"ok": True})
                done_event.set()
                return

            self.send_error(404)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 4322), StudioHandler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), StudioHandler)
    port = server.server_port
    url = f"http://127.0.0.1:{port}/"
    print(f"\n[Video & Thumbnail Studio] Running at: {url}")
    print("Generate thumbnails or copy CMS paths in your browser, then click 'Finish & Close'...")

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    webbrowser.open(url)

    try:
        done_event.wait()
        time.sleep(0.15)
    except KeyboardInterrupt:
        print("\n[Video & Thumbnail Studio] Closed by user.")
    finally:
        server.shutdown()
        server.server_close()

    return produced_files


def upload_to_r2(files: list[Path]) -> None:
    load_env(BASE_DIR / ".env")
    account_id = os.getenv("R2_ACCOUNT_ID")
    access_key = os.getenv("R2_ACCESS_KEY_ID")
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
    bucket = os.getenv("R2_BUCKET")

    if not all([account_id, access_key, secret_key, bucket]):
        print("R2 credentials missing. Copy .env.example to .env and fill R2_* variables.")
        return

    try:
        import boto3
    except ImportError:
        print("Install boto3 to upload to R2: pip install boto3")
        return

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )

    seen: set[Path] = set()
    for file_path in files:
        if file_path in seen or not file_path.is_file():
            continue
        seen.add(file_path)
        key = file_path.relative_to(CONVERTED_DIR).as_posix()
        content_type, _ = mimetypes.guess_type(str(file_path))
        extra_args = {
            "ContentType": content_type or "application/octet-stream",
            "CacheControl": "public, max-age=31536000",
        }
        print(f"Uploading -> s3://{bucket}/{key}")
        s3.upload_file(str(file_path), bucket, key, ExtraArgs=extra_args)
        print(f"  Ready: https://media.artdanylov.com/{key}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert local media, manage thumbnails, and sync to R2.")
    parser.add_argument("--upload", action="store_true", help="Upload newly converted files/thumbnails to R2")
    parser.add_argument("--upload-all", action="store_true", help="Upload all files in Converted/ to R2")
    parser.add_argument(
        "--thumbs",
        action="store_true",
        help="Open the interactive Video & Thumbnail Studio for converted videos in Converted/",
    )
    args = parser.parse_args()

    if args.thumbs:
        run_thumbnail_studio(auto_upload=args.upload)
        if args.upload_all:
            all_files = [
                p
                for p in [*CONVERTED_DIR.rglob("*.mp4"), *CONVERTED_DIR.rglob("*.webp")]
                if p.is_file()
            ]
            if all_files:
                upload_to_r2(all_files)
        return

    produced = convert_all()
    if args.upload_all:
        all_files = [
            p
            for p in [*CONVERTED_DIR.rglob("*.mp4"), *CONVERTED_DIR.rglob("*.webp")]
            if p.is_file()
        ]
        if all_files:
            upload_to_r2(all_files)
    elif args.upload:
        if produced:
            upload_to_r2(produced)
        else:
            print("No new converted files to upload to R2.")


if __name__ == "__main__":
    main()
