"""
Converts media from media_local/_toConvert -> media_local/Converted (preserving subfolders),
provides an English local Web UI (Video & Thumbnail Studio) for converted videos organized
by hierarchy (portfolio / blog / custom folders) with live/disk previews for animated
(_thumb.webp, 640x360 @ 24fps, or 1-frame static) thumbnails, persistent settings,
and Cloudflare R2 upload.

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
SETTINGS_FILE = CONVERTED_DIR / ".thumb_studio_settings.json"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp", ".gif"}

DEFAULT_SETTINGS: dict[str, int] = {
    "max_width": 1920,
    "max_height": 1080,
    "target_fps": 30,
    "video_crf": 21,
    "video_maxrate_kbps": 5000,
    "watermark_enabled": 1,
    "watermark_opacity": 35,
    "watermark_scale_pct": 100,
    "webp_quality": 92,
    "thumb_width": 640,
    "thumb_height": 360,
    "thumb_fps": 18,
    "thumb_quality": 72,
    "thumb_static_quality": 85,
    "thumb_warn_kb": 1024,
}

WATERMARK_LINE_1 = "www.artdanylov.com"
WATERMARK_LINE_2 = "artem.danylov@gmail.com"
WATERMARK_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/segoeui.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("C:/Windows/Fonts/calibri.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
)

MAX_WIDTH = DEFAULT_SETTINGS["max_width"]
MAX_HEIGHT = DEFAULT_SETTINGS["max_height"]
TARGET_FPS = DEFAULT_SETTINGS["target_fps"]
VIDEO_CRF = DEFAULT_SETTINGS["video_crf"]
VIDEO_MAXRATE_KBPS = DEFAULT_SETTINGS["video_maxrate_kbps"]
WATERMARK_ENABLED = DEFAULT_SETTINGS["watermark_enabled"]
WATERMARK_OPACITY = DEFAULT_SETTINGS["watermark_opacity"]
WATERMARK_SCALE_PCT = DEFAULT_SETTINGS["watermark_scale_pct"]
WEBP_QUALITY = DEFAULT_SETTINGS["webp_quality"]

THUMB_WIDTH = DEFAULT_SETTINGS["thumb_width"]
THUMB_HEIGHT = DEFAULT_SETTINGS["thumb_height"]
THUMB_FPS = DEFAULT_SETTINGS["thumb_fps"]
THUMB_QUALITY = DEFAULT_SETTINGS["thumb_quality"]
THUMB_STATIC_QUALITY = DEFAULT_SETTINGS["thumb_static_quality"]
THUMB_WARN_KB = DEFAULT_SETTINGS["thumb_warn_kb"]

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


def _clamp_int(val, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(round(float(val)))))
    except Exception:
        return default


def sanitize_studio_settings(raw: dict | None) -> dict[str, int]:
    src = raw if isinstance(raw, dict) else {}
    return {
        "max_width": _clamp_int(src.get("max_width"), DEFAULT_SETTINGS["max_width"], 640, 3840),
        "max_height": _clamp_int(src.get("max_height"), DEFAULT_SETTINGS["max_height"], 360, 2160),
        "target_fps": _clamp_int(src.get("target_fps"), DEFAULT_SETTINGS["target_fps"], 15, 60),
        "video_crf": _clamp_int(src.get("video_crf"), DEFAULT_SETTINGS["video_crf"], 14, 32),
        "video_maxrate_kbps": _clamp_int(src.get("video_maxrate_kbps"), DEFAULT_SETTINGS["video_maxrate_kbps"], 1000, 25000),
        "watermark_enabled": _clamp_int(src.get("watermark_enabled"), DEFAULT_SETTINGS["watermark_enabled"], 0, 1),
        "watermark_opacity": _clamp_int(src.get("watermark_opacity"), DEFAULT_SETTINGS["watermark_opacity"], 10, 100),
        "watermark_scale_pct": _clamp_int(src.get("watermark_scale_pct"), DEFAULT_SETTINGS["watermark_scale_pct"], 50, 200),
        "webp_quality": _clamp_int(src.get("webp_quality"), DEFAULT_SETTINGS["webp_quality"], 50, 100),
        "thumb_width": _clamp_int(src.get("thumb_width"), DEFAULT_SETTINGS["thumb_width"], 320, 1280),
        "thumb_height": _clamp_int(src.get("thumb_height"), DEFAULT_SETTINGS["thumb_height"], 180, 720),
        "thumb_fps": _clamp_int(src.get("thumb_fps"), DEFAULT_SETTINGS["thumb_fps"], 6, 30),
        "thumb_quality": _clamp_int(src.get("thumb_quality"), DEFAULT_SETTINGS["thumb_quality"], 30, 100),
        "thumb_static_quality": _clamp_int(src.get("thumb_static_quality"), DEFAULT_SETTINGS["thumb_static_quality"], 40, 100),
        "thumb_warn_kb": _clamp_int(src.get("thumb_warn_kb"), DEFAULT_SETTINGS["thumb_warn_kb"], 100, 10240),
    }


def apply_studio_settings(cfg: dict[str, int]) -> dict[str, int]:
    global MAX_WIDTH, MAX_HEIGHT, TARGET_FPS, VIDEO_CRF, VIDEO_MAXRATE_KBPS, WEBP_QUALITY
    global WATERMARK_ENABLED, WATERMARK_OPACITY, WATERMARK_SCALE_PCT
    global THUMB_WIDTH, THUMB_HEIGHT, THUMB_FPS, THUMB_QUALITY, THUMB_STATIC_QUALITY, THUMB_WARN_KB
    clean = sanitize_studio_settings(cfg)
    MAX_WIDTH = clean["max_width"]
    MAX_HEIGHT = clean["max_height"]
    TARGET_FPS = clean["target_fps"]
    VIDEO_CRF = clean["video_crf"]
    VIDEO_MAXRATE_KBPS = clean["video_maxrate_kbps"]
    WATERMARK_ENABLED = clean["watermark_enabled"]
    WATERMARK_OPACITY = clean["watermark_opacity"]
    WATERMARK_SCALE_PCT = clean["watermark_scale_pct"]
    WEBP_QUALITY = clean["webp_quality"]
    THUMB_WIDTH = clean["thumb_width"]
    THUMB_HEIGHT = clean["thumb_height"]
    THUMB_FPS = clean["thumb_fps"]
    THUMB_QUALITY = clean["thumb_quality"]
    THUMB_STATIC_QUALITY = clean["thumb_static_quality"]
    THUMB_WARN_KB = clean["thumb_warn_kb"]
    return clean


def load_studio_settings() -> dict[str, int]:
    if not SETTINGS_FILE.is_file():
        return apply_studio_settings(DEFAULT_SETTINGS)
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return apply_studio_settings(data)


def save_studio_settings(cfg: dict) -> dict[str, int]:
    clean = apply_studio_settings(cfg)
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    return clean


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


def find_source_video_for_converted(conv_video: Path) -> Path | None:
    """Finds the matching original source video in _toConvert/ for a Converted/ .mp4 if present."""
    try:
        rel_parent = conv_video.relative_to(CONVERTED_DIR).parent
    except ValueError:
        return None
    src_dir = INPUT_DIR / rel_parent
    if not src_dir.is_dir():
        return None
    target_stem = conv_video.stem
    for cand in src_dir.iterdir():
        if not cand.is_file() or cand.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if cand.stem == target_stem or sanitize_stem(cand.stem) == target_stem:
            return cand
    return None


def find_source_image_for_converted(conv_image: Path) -> Path | None:
    """Finds the matching original source image/GIF in _toConvert/ for a Converted/ image if present."""
    try:
        rel_parent = conv_image.relative_to(CONVERTED_DIR).parent
    except ValueError:
        return None
    src_dir = INPUT_DIR / rel_parent
    if not src_dir.is_dir():
        return None
    target_stem = conv_image.stem
    for cand in src_dir.iterdir():
        if not cand.is_file() or cand.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if cand.stem == target_stem or sanitize_stem(cand.stem) == target_stem:
            return cand
    return None


def _escape_drawtext_value(val: str) -> str:
    return (
        val.replace("\\", "\\\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace("%", r"\%")
    )


def resolve_watermark_font_arg() -> str:
    for cand in WATERMARK_FONT_CANDIDATES:
        if cand.is_file():
            escaped = cand.as_posix().replace(":", r"\:").replace("'", r"\'")
            return f"fontfile='{escaped}':"
    return ""


def build_watermark_vf(
    enabled: bool | None = None,
    opacity_pct: int | None = None,
    scale_pct: int | None = None,
) -> str:
    """
    Builds the subtle 2-line bottom-right FFmpeg drawtext filter chain (no drop shadow)
    for converted .mp4 videos only (never used for thumbnails).
    """
    use_wm = bool(WATERMARK_ENABLED) if enabled is None else bool(enabled)
    if not use_wm:
        return ""

    op = WATERMARK_OPACITY if opacity_pct is None else _clamp_int(opacity_pct, 35, 10, 100)
    sc = WATERMARK_SCALE_PCT if scale_pct is None else _clamp_int(scale_pct, 100, 50, 200)
    alpha = f"{op / 100.0:.2f}".rstrip("0").rstrip(".")
    scale = sc / 100.0

    font_arg = resolve_watermark_font_arg()
    t1 = _escape_drawtext_value(WATERMARK_LINE_1)
    t2 = _escape_drawtext_value(WATERMARK_LINE_2)

    fs1_min = max(10, int(round(14 * scale)))
    fs2_min = max(9, int(round(13 * scale)))
    gap_min = max(12, int(round(17 * scale)))
    fs1_ratio = 0.0167 * scale
    fs2_ratio = 0.0150 * scale
    gap_ratio = 0.0185 * scale

    line1 = (
        f"drawtext={font_arg}"
        f"text='{t1}':"
        f"fontcolor=white@{alpha}:"
        f"fontsize='max({fs1_min}\\,round(h*{fs1_ratio:.5f}))':"
        r"x='w-tw-max(18\,round(w*0.015))':"
        f"y='h-th-max(16\\,round(h*0.016))-max({gap_min}\\,round(h*{gap_ratio:.5f}))'"
    )
    line2 = (
        f"drawtext={font_arg}"
        f"text='{t2}':"
        f"fontcolor=white@{alpha}:"
        f"fontsize='max({fs2_min}\\,round(h*{fs2_ratio:.5f}))':"
        r"x='w-tw-max(18\,round(w*0.015))':"
        r"y='h-th-max(16\,round(h*0.016))'"
    )
    return f"{line1},{line2}"


def encode_video(
    src: Path,
    out_video: Path,
    has_audio: bool = True,
    crf: int | None = None,
    max_bitrate_kbps: int | None = None,
    watermark: bool | None = None,
) -> None:
    """
    Encodes H.264 MP4 optimized for Cloudflare R2 streaming and universal browser hardware decoding:
    - High@L4.1, yuv420p, bt709 color signaling
    - Closed GOP with 2-second keyframe interval (-g 60) for fast HTTP Range scrubbing
    - VBV capped CRF (-maxrate / -bufsize) to prevent bandwidth spikes on 3D renders
    - Subtle 2-line bottom-right anti-theft watermark (when enabled; never applied to thumbnails)
    - Smart audio: omits audio track (-an) if source has no audio stream
    """
    if crf is None:
        crf = VIDEO_CRF
    if max_bitrate_kbps is None:
        max_bitrate_kbps = VIDEO_MAXRATE_KBPS
    vf_parts = [
        f"scale='min({MAX_WIDTH},iw)':'min({MAX_HEIGHT},ih)':force_original_aspect_ratio=decrease:flags=lanczos",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        f"fps='min({TARGET_FPS},source_fps)'",
    ]
    wm_vf = build_watermark_vf(enabled=watermark)
    if wm_vf:
        vf_parts.append(wm_vf)
    vf_filter = ",".join(vf_parts)
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


def compute_crop_rect(
    src_w: int,
    src_h: int,
    crop_zoom: float = 1.0,
    crop_pan_x: float = 0.0,
    crop_pan_y: float = 0.0,
    target_w: int | None = None,
    target_h: int | None = None,
) -> tuple[int, int, int, int]:
    """
    Computes exact integer (cw, ch, cx, cy) crop rectangle inside (src_w, src_h)
    matching target_w:target_h aspect ratio at the given zoom (>=1.0) and pan ([-1, 1]).
    """
    if target_w is None:
        target_w = THUMB_WIDTH
    if target_h is None:
        target_h = THUMB_HEIGHT
    if src_w <= 0 or src_h <= 0:
        return (target_w, target_h, 0, 0)
    zoom = max(1.0, float(crop_zoom or 1.0))
    pan_x = max(-1.0, min(1.0, float(crop_pan_x or 0.0)))
    pan_y = max(-1.0, min(1.0, float(crop_pan_y or 0.0)))

    target_ar = target_w / target_h
    if (src_w / src_h) >= target_ar:
        base_h = float(src_h)
        base_w = base_h * target_ar
    else:
        base_w = float(src_w)
        base_h = base_w / target_ar

    crop_w = base_w / zoom
    crop_h = base_h / zoom

    cw = max(2, min(src_w, int(round(crop_w))))
    ch = max(2, min(src_h, int(round(crop_h))))
    max_x = max(0.0, src_w - cw)
    max_y = max(0.0, src_h - ch)
    cx = max(0, min(src_w - cw, int(round((max_x / 2.0) * (1.0 + pan_x)))))
    cy = max(0, min(src_h - ch, int(round((max_y / 2.0) * (1.0 + pan_y)))))
    return (cw, ch, cx, cy)


def generate_thumb(
    src_video: Path,
    out_thumb: Path,
    start_time: float,
    duration: float,
    crop_zoom: float = 1.0,
    crop_pan_x: float = 0.0,
    crop_pan_y: float = 0.0,
    quality: int | None = None,
) -> Path:
    """
    Generates a WebP thumbnail (_thumb.webp) from the specified pan/zoom crop region,
    plus a 1st-frame static poster (_thumb_static.webp) for instant progressive loading.
    If duration <= 1 frame at THUMB_FPS, outputs a single static frame for both.
    Otherwise outputs a looping animated WebP and a matching 1st-frame static WebP.
    Returns the path to the generated _thumb_static.webp file.
    """
    is_single_frame = duration <= (1.0 / THUMB_FPS) + 0.005
    try:
        info = get_video_info(src_video)
        src_w, src_h = info["width"], info["height"]
    except Exception:
        src_w, src_h = 0, 0

    if src_w > 0 and src_h > 0:
        cw, ch, cx, cy = compute_crop_rect(src_w, src_h, crop_zoom, crop_pan_x, crop_pan_y)
        if cw == THUMB_WIDTH and ch == THUMB_HEIGHT:
            scale_crop = f"crop={cw}:{ch}:{cx}:{cy}"
        else:
            scale_crop = f"crop={cw}:{ch}:{cx}:{cy},scale={THUMB_WIDTH}:{THUMB_HEIGHT}:flags=lanczos"
    else:
        scale_crop = (
            f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={THUMB_WIDTH}:{THUMB_HEIGHT}"
        )

    out_static = out_thumb.with_name(f"{out_thumb.stem}_static{out_thumb.suffix}")
    static_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, start_time):.3f}",
        "-i",
        str(src_video),
        "-vf",
        scale_crop,
        "-frames:v",
        "1",
        "-c:v",
        "libwebp",
        "-lossless",
        "0",
        "-compression_level",
        "6",
        "-quality",
        str(THUMB_STATIC_QUALITY),
        str(out_static),
    ]
    subprocess.run(static_cmd, check=True)

    if is_single_frame:
        out_thumb.write_bytes(out_static.read_bytes())
    else:
        anim_cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, start_time):.3f}",
            "-t",
            f"{max(0.04, duration):.3f}",
            "-i",
            str(src_video),
            "-vf",
            f"fps={THUMB_FPS},{scale_crop}",
            "-c:v",
            "libwebp",
            "-lossless",
            "0",
            "-compression_level",
            "6",
            "-quality",
            str(max(20, min(100, int(quality))) if quality else THUMB_QUALITY),
            "-loop",
            "0",
            "-an",
            str(out_thumb),
        ]
        subprocess.run(anim_cmd, check=True)

    return out_static


def generate_image_thumb(
    src_image: Path,
    out_thumb: Path,
    animated: bool = False,
    crop_zoom: float = 1.0,
    crop_pan_x: float = 0.0,
    crop_pan_y: float = 0.0,
    start_time: float = 0.0,
    duration: float = 0.0,
    quality: int | None = None,
) -> Path:
    """
    Generates a WebP thumbnail (_thumb.webp) + 1st-frame static poster (_thumb_static.webp)
    from a converted image (including animated WebP/GIF) with pan/zoom crop and optional start/length trim.
    """
    try:
        info = get_video_info(src_image)
        src_w, src_h = info["width"], info["height"]
    except Exception:
        src_w, src_h = 0, 0

    if src_w > 0 and src_h > 0:
        cw, ch, cx, cy = compute_crop_rect(src_w, src_h, crop_zoom, crop_pan_x, crop_pan_y)
        if cw == THUMB_WIDTH and ch == THUMB_HEIGHT:
            scale_crop = f"crop={cw}:{ch}:{cx}:{cy}"
        else:
            scale_crop = f"crop={cw}:{ch}:{cx}:{cy},scale={THUMB_WIDTH}:{THUMB_HEIGHT}:flags=lanczos"
    else:
        scale_crop = (
            f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={THUMB_WIDTH}:{THUMB_HEIGHT}"
        )

    out_static = out_thumb.with_name(f"{out_thumb.stem}_static{out_thumb.suffix}")
    static_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    if start_time > 0.001:
        static_cmd.extend(["-ss", f"{start_time:.3f}"])
    static_cmd.extend([
        "-i",
        str(src_image),
        "-vf",
        scale_crop,
        "-frames:v",
        "1",
        "-c:v",
        "libwebp",
        "-lossless",
        "0",
        "-compression_level",
        "6",
        "-quality",
        str(THUMB_STATIC_QUALITY),
        str(out_static),
    ])
    subprocess.run(static_cmd, check=True)

    if not animated:
        out_thumb.write_bytes(out_static.read_bytes())
    else:
        anim_cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
        ]
        if start_time > 0.001:
            anim_cmd.extend(["-ss", f"{start_time:.3f}"])
        if duration > 0.04:
            anim_cmd.extend(["-t", f"{duration:.3f}"])
        anim_cmd.extend([
            "-i",
            str(src_image),
            "-vf",
            f"fps={THUMB_FPS},{scale_crop}",
            "-c:v",
            "libwebp",
            "-lossless",
            "0",
            "-compression_level",
            "6",
            "-quality",
            str(max(20, min(100, int(quality))) if quality else THUMB_QUALITY),
            "-loop",
            "0",
            "-an",
            str(out_thumb),
        ])
        subprocess.run(anim_cmd, check=True)

    return out_static


def convert_image(src: Path, out_image: Path, quality: int | None = None) -> None:
    if quality is None:
        quality = WEBP_QUALITY
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
        elif ext in IMAGE_EXTENSIONS and not src.stem.endswith(("_thumb", "_thumb_static", "_animthumb")):
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
        and not f.stem.endswith(("_thumb", "_thumb_static", "_animthumb"))
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
            if not WATERMARK_ENABLED and is_web_ready(info):
                remux_faststart(src, out_video, has_audio=has_audio)
                new_size = out_video.stat().st_size
            else:
                duration = max(info["duration"], 1.0)
                audio_kbps = 160 if has_audio else 0
                target_kbps = max(int((orig_size * 8 / 1000) / duration * 0.90) - audio_kbps, 300)
                encode_video(src, out_video, has_audio=has_audio, crf=23, max_bitrate_kbps=target_kbps)
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

        try:
            info = get_video_info(conv)
        except Exception:
            info = {"duration": 0.0, "fps": 30.0, "width": 1920, "height": 1080}

        dur = max(info["duration"], 0.1)
        default_anim_start = round(min(max(dur * 0.2, 0.0), max(dur - 1.0, 0.0)), 2)
        default_anim_dur = round(min(1.0, dur), 2)

        saved = saved_state.get(rel_key, {})
        anim_duration = round(max(0.04, min(dur, float(saved.get("anim_duration", default_anim_dur)))), 2)
        anim_start = round(max(0.0, min(max(0.0, dur - anim_duration), float(saved.get("anim_start", default_anim_start)))), 2)
        crop_zoom = round(max(1.0, min(8.0, float(saved.get("crop_zoom", 1.0)))), 3)
        crop_pan_x = round(max(-1.0, min(1.0, float(saved.get("crop_pan_x", 0.0)))), 4)
        crop_pan_y = round(max(-1.0, min(1.0, float(saved.get("crop_pan_y", 0.0)))), 4)
        raw_q = saved.get("thumb_quality")
        thumb_quality = _clamp_int(raw_q, 0, 20, 100) if raw_q else 0
        raw_vcrf = saved.get("video_crf")
        video_crf = _clamp_int(raw_vcrf, 0, 14, 32) if raw_vcrf else 0

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
                "video_rel": rel_key,
                "thumb_rel": out_thumb.relative_to(CONVERTED_DIR).as_posix(),
                "has_thumb": out_thumb.is_file(),
                "thumb_size_kb": round(out_thumb.stat().st_size / 1024, 1) if out_thumb.is_file() else 0,
                "duration": round(dur, 3),
                "fps": round(info["fps"] or 30.0, 2),
                "width": info["width"],
                "height": info["height"],
                "anim_start": anim_start,
                "anim_duration": anim_duration,
                "crop_zoom": crop_zoom,
                "crop_pan_x": crop_pan_x,
                "crop_pan_y": crop_pan_y,
                "thumb_quality": thumb_quality,
                "video_crf": video_crf,
                "has_source": find_source_video_for_converted(conv) is not None,
                "size_kb": round(conv.stat().st_size / 1024, 1),
            }
        )

    # 2. Standalone converted images (excluding generated _thumb / _animthumb)
    for img in sorted(CONVERTED_DIR.rglob("*")):
        if not img.is_file():
            continue
        if img.suffix.lower() not in {".webp", ".png", ".jpg", ".jpeg", ".gif"}:
            continue
        if img.stem.endswith(("_thumb", "_thumb_static", "_animthumb")):
            continue
        rel_key = img.relative_to(CONVERTED_DIR).as_posix()
        rel_parent = img.relative_to(CONVERTED_DIR).parent
        category, subfolder = split_hierarchy(rel_parent)
        out_thumb = img.parent / f"{img.stem}_thumb.webp"
        saved = saved_state.get(rel_key, {})
        catalog.append(
            {
                "kind": "image",
                "id": rel_key,
                "stem": img.stem,
                "category": category,
                "subfolder": subfolder,
                "folder": rel_parent.as_posix() if rel_parent.as_posix() != "." else "",
                "image_rel": rel_key,
                "thumb_rel": out_thumb.relative_to(CONVERTED_DIR).as_posix(),
                "has_thumb": out_thumb.is_file(),
                "thumb_size_kb": round(out_thumb.stat().st_size / 1024, 1) if out_thumb.is_file() else 0,
                "crop_zoom": round(max(1.0, min(8.0, float(saved.get("crop_zoom", 1.0)))), 3),
                "crop_pan_x": round(max(-1.0, min(1.0, float(saved.get("crop_pan_x", 0.0)))), 4),
                "crop_pan_y": round(max(-1.0, min(1.0, float(saved.get("crop_pan_y", 0.0)))), 4),
                "img_animated": bool(saved.get("img_animated", False)),
                "img_start": round(max(0.0, float(saved.get("img_start", 0.0))), 2),
                "img_duration": round(max(0.0, float(saved.get("img_duration", 0.0))), 2),
                "thumb_quality": _clamp_int(saved.get("thumb_quality"), 0, 20, 100) if saved.get("thumb_quality") else 0,
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
  .badge-warn { background: rgba(245, 158, 11, 0.22); color: #fbbf24; }
  .badge-off { background: rgba(255, 255, 255, 0.07); color: #71717a; }
  .badge-img { background: rgba(56, 189, 248, 0.18); color: #38bdf8; }
  .thumb-size-warn {
    margin-top: 6px;
    padding: 7px 9px;
    border-radius: 6px;
    background: rgba(245, 158, 11, 0.14);
    border: 1px solid rgba(245, 158, 11, 0.4);
    color: #fcd34d;
    font-size: 11.5px;
    line-height: 1.45;
  }

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
  .control-card.is-disabled { opacity: 1; }
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
  .preview-pane.is-disabled { opacity: 1; }
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
    user-select: none;
    touch-action: none;
  }
  .preview-media-box.is-pannable { cursor: grab; }
  .preview-media-box.is-dragging { cursor: grabbing; }
  .preview-media-box canvas,
  .preview-media-box img {
    width: 100%;
    height: 100%;
    display: block;
    object-fit: cover;
  }
  .preview-media-box video,
  .preview-media-box img.crop-preview-img {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    display: block;
    object-fit: fill;
    pointer-events: none;
  }
  .image-thumb-panel {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
    display: none;
    grid-template-columns: minmax(0, 340px) minmax(0, 1fr);
    gap: 14px;
    align-items: start;
  }
  .image-thumb-panel.is-open { display: grid; }
  @media (max-width: 780px) {
    .image-thumb-panel { grid-template-columns: 1fr; }
  }
  .crop-controls {
    margin-top: 6px;
    padding-top: 7px;
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .crop-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 11px;
    color: var(--muted);
    gap: 6px;
  }
  .crop-actions {
    display: flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;
  }
  .crop-hint {
    font-size: 10.5px;
    color: var(--muted);
    opacity: 0.8;
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
  .settings-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.72);
    backdrop-filter: blur(4px);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 999;
    padding: 16px;
  }
  .settings-backdrop.is-open { display: flex; }
  .settings-modal {
    width: 100%;
    max-width: 560px;
    max-height: 90vh;
    overflow-y: auto;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 18px;
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.65);
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .settings-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }
  .settings-header h2 {
    margin: 0;
    font-size: 15px;
    font-weight: 800;
  }
  .settings-group {
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid var(--border);
    border-radius: 9px;
    padding: 11px 12px;
    display: flex;
    flex-direction: column;
    gap: 9px;
  }
  .settings-group-title {
    font-size: 11px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--accent-hover);
  }
  .settings-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 9px 12px;
  }
  .settings-field {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .settings-field label {
    font-size: 11.5px;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
    gap: 6px;
  }
  .settings-field input[type="number"] {
    width: 100%;
    padding: 6px 8px;
  }
  .settings-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding-top: 8px;
    border-top: 1px solid var(--border);
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
      <button type="button" id="openSettingsBtn" title="Configure video, image, and thumbnail quality settings">
        ⚙ Settings
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
          <div style="margin-top:10px; display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap;">
            <label class="check-label">
              <input type="checkbox" id="toggleImageThumbUi" />
              <span>Create Thumbnail (_thumb.webp)</span>
            </label>
            <span id="imgThumbStatusLabel" style="font-size:11.5px; color:var(--muted);"></span>
          </div>
          <div class="image-thumb-panel" id="imageThumbPanel">
            <div>
              <div class="preview-media-box" id="imgThumbPreviewBox" title="Drag to pan, scroll wheel to zoom">
                <img id="imgThumbPreviewImg" class="crop-preview-img" alt="Thumbnail crop preview" />
              </div>
              <div class="crop-controls">
                <div class="crop-head">
                  <span>Crop / Pan &amp; Zoom</span>
                  <span id="imgCropInfoBadge" style="color:#ff9c6e; font-family:monospace;">1.00×</span>
                </div>
                <input type="range" id="imgCropZoomSlider" min="1" max="4" step="0.01" value="1" />
                <div class="crop-actions">
                  <button type="button" class="btn-sm" id="imgCropFitBtn">Fit (1×)</button>
                  <button type="button" class="btn-sm" id="imgCropNativeBtn">1:1 (640×360)</button>
                  <button type="button" class="btn-sm" id="imgCropCenterBtn">Center</button>
                </div>
                <div class="crop-hint">Drag preview to pan · Scroll wheel to zoom</div>
              </div>
            </div>
            <div style="display:flex; flex-direction:column; gap:10px;">
              <label class="check-label">
                <input type="checkbox" id="imgThumbAnimatedToggle" />
                <span>Animated (preserve GIF / animated WebP)</span>
              </label>
              <div id="imgAnimTimingRow" style="display:none; flex-direction:column; gap:6px; padding:8px 10px; background:rgba(255,255,255,0.02); border:1px solid var(--border); border-radius:7px;">
                <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                  <label style="font-size:11.5px; color:var(--muted);">Start (s):</label>
                  <input type="number" id="imgThumbStartInput" min="0" step="0.1" value="0" />
                  <label style="font-size:11.5px; color:var(--muted);">Length (s, 0=full):</label>
                  <input type="number" id="imgThumbDurInput" min="0" max="10" step="0.1" value="0" />
                  <label style="font-size:11.5px; color:var(--muted);" title="Optional per-item animated WebP quality (20–100); leave blank for global Settings default">Quality:</label>
                  <input type="number" id="imgThumbQualityInput" min="20" max="100" step="1" placeholder="auto" style="width:62px;" />
                </div>
                <div style="display:flex; align-items:center; gap:4px; flex-wrap:wrap;">
                  <button type="button" class="btn-sm img-dur-preset" data-dur="0">Full</button>
                  <button type="button" class="btn-sm img-dur-preset" data-dur="0.5">0.5s</button>
                  <button type="button" class="btn-sm img-dur-preset" data-dur="1.0">1.0s</button>
                  <button type="button" class="btn-sm img-dur-preset" data-dur="1.5">1.5s</button>
                  <button type="button" class="btn-sm img-dur-preset" data-dur="2.0">2.0s</button>
                </div>
              </div>
              <div>
                <button type="button" class="btn-primary" id="generateImageThumbBtn">⚡ Generate Image Thumbnail</button>
              </div>
              <div class="thumb-size-warn" id="imgThumbSizeWarn" style="display:none; margin-top:0;"></div>
              <div class="path-box" id="imgThumbPathBox" style="display:none; margin-top:0;">
                <span class="path-label">_thumb</span>
                <span class="path-val" id="imgThumbPathText"></span>
                <button type="button" class="btn-sm" id="copyImgThumbPathBtn">Copy</button>
                <button type="button" class="btn-sm" id="deleteImgThumbBtn" title="Delete _thumb.webp" style="color:#f87171; border-color:rgba(239,68,68,0.35);">🗑</button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div id="editorContent">
        <div class="left-column">
          <div class="player-card">
            <div class="card-title">
              <span id="currentVideoTitle" style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">Video</span>
              <div style="display:flex; align-items:center; gap:8px; flex-shrink:0;">
                <span id="videoSizeBadge" style="font-family:monospace; color:#a1a1aa; font-size:11.5px;"></span>
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
              <button type="button" class="btn-sm" id="previewAnimBtn">
                🎞 Preview Thumbnail
              </button>
              <span style="flex:1;"></span>
              <label style="font-size:11.5px; color:var(--muted);" title="Custom H.264 CRF for this video (14=best, 32=smallest; blank=Settings default)">Video CRF:</label>
              <input type="number" id="videoCrfInput" min="14" max="32" step="1" placeholder="21" style="width:58px;" />
              <button type="button" class="btn-sm" id="reencodeVideoBtn" title="Re-encode this video from _toConvert source (or Converted copy) with the specified CRF">
                🔁 Re-encode Video
              </button>
            </div>
          </div>

          <div class="control-card" id="animControlCard">
            <div class="card-title" style="margin-bottom:4px;">
              <label class="check-label">
                <input type="checkbox" id="enableAnimToggle" />
                <span id="thumbSpecLabel">Thumbnail (_thumb.webp · 640×360 @ 18 fps, 1f = static)</span>
              </label>
              <div style="display:flex; align-items:center; gap:8px;">
                <span id="thumbStatusLabel" style="font-size:11px;"></span>
                <span id="animRangeBadge" style="color:#ff9c6e; font-family:monospace;">0.00s — 1.00s (1.00s)</span>
              </div>
            </div>

            <div class="range-track-wrap" id="timelineTrack" title="Drag the orange bar or its edges to adjust start and duration">
              <div class="range-window" id="timelineWindow"></div>
              <div class="range-playhead" id="timelinePlayhead"></div>
            </div>

            <div class="control-row" style="justify-content: space-between; margin-top:6px;">
              <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                <label style="font-size:11.5px; color:var(--muted);">Start:</label>
                <input type="number" id="animStartInput" min="0" step="0.05" value="0" />
                <label style="font-size:11.5px; color:var(--muted);">Length:</label>
                <button type="button" class="btn-sm" id="durDecBtn">-0.1s</button>
                <input type="number" id="animDurInput" min="0.04" max="5.0" step="0.01" value="1.0" />
                <button type="button" class="btn-sm" id="durIncBtn">+0.1s</button>
                <label style="font-size:11.5px; color:var(--muted);" title="Optional per-video animated WebP quality (20–100); leave blank for global Settings default">Quality:</label>
                <input type="number" id="animQualityInput" min="20" max="100" step="1" placeholder="auto" style="width:62px;" />
              </div>
              <div style="display:flex; align-items:center; gap:4px; flex-wrap:wrap;">
                <button type="button" class="btn-sm dur-preset" data-dur="0.04" title="1 frame (static)">1f</button>
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
              <span>Preview</span>
              <div class="mode-tabs">
                <button type="button" class="btn-sm active-pill" id="previewSourceLiveBtn">Live</button>
                <button type="button" class="btn-sm" id="previewSourceDiskBtn">On Disk</button>
              </div>
            </div>

            <div class="stacked-previews">
              <div class="preview-pane" id="animPreviewPane">
                <div class="preview-pane-head">
                  <span id="previewSpecLabel">_thumb.webp (640×360)</span>
                  <span id="animShortBadge" style="color:#ff9c6e; font-family:monospace;">0.00s–1.00s</span>
                </div>
                <div class="preview-media-box" id="animPreviewBox" title="Drag to pan, scroll wheel to zoom">
                  <video id="animPreviewVideo" muted playsinline preload="auto"></video>
                  <img id="savedAnimImg" alt="Saved thumb" style="display:none;" />
                </div>
                <div class="crop-controls">
                  <div class="crop-head">
                    <span>Crop / Pan &amp; Zoom</span>
                    <span id="cropInfoBadge" style="color:#ff9c6e; font-family:monospace;">1.00×</span>
                  </div>
                  <input type="range" id="cropZoomSlider" min="1" max="4" step="0.01" value="1" />
                  <div class="crop-actions">
                    <button type="button" class="btn-sm" id="cropFitBtn" title="Fit full 16:9 frame (1x)">Fit (1×)</button>
                    <button type="button" class="btn-sm" id="cropNativeBtn" title="Crop exact 640×360 native pixels without scaling down">1:1 (640×360)</button>
                    <button type="button" class="btn-sm" id="cropCenterBtn" title="Center pan position">Center</button>
                  </div>
                  <div class="crop-hint">Drag preview to pan · Scroll wheel to zoom</div>
                </div>
                <div class="thumb-size-warn" id="thumbSizeWarn" style="display:none;"></div>
              </div>
            </div>

            <div style="margin-top:8px;">
              <div class="path-box">
                <span class="path-label">Video</span>
                <span class="path-val" id="videoPathText"></span>
                <button type="button" class="btn-sm" id="copyVideoPathBtn">Copy</button>
              </div>
              <div class="path-box" id="thumbPathBox" style="display:none;">
                <span class="path-label">_thumb</span>
                <span class="path-val" id="thumbPathText"></span>
                <button type="button" class="btn-sm" id="copyThumbPathBtn">Copy</button>
                <button type="button" class="btn-sm" id="deleteThumbBtn" title="Delete _thumb.webp" style="color:#f87171; border-color:rgba(239,68,68,0.35);">🗑</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>

  <div class="settings-backdrop" id="settingsBackdrop" aria-hidden="true">
    <div class="settings-modal" role="dialog" aria-modal="true" aria-labelledby="settingsModalTitle">
      <div class="settings-header">
        <h2 id="settingsModalTitle">⚙ Video &amp; Thumbnail Quality Settings</h2>
        <button type="button" class="btn-sm" id="closeSettingsBtn">✕ Close</button>
      </div>

      <div class="settings-group">
        <div class="settings-group-title" style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
          <span>Thumbnails (_thumb.webp)</span>
          <div style="display:flex; gap:4px;">
            <button type="button" class="btn-sm thumb-res-preset" data-w="320" data-h="180">320×180</button>
            <button type="button" class="btn-sm thumb-res-preset" data-w="480" data-h="270">480×270</button>
            <button type="button" class="btn-sm thumb-res-preset" data-w="640" data-h="360">640×360</button>
          </div>
        </div>
        <div class="settings-grid">
          <div class="settings-field">
            <label for="setThumbWidth">Width (px)</label>
            <input type="number" id="setThumbWidth" min="320" max="1280" step="16" />
          </div>
          <div class="settings-field">
            <label for="setThumbHeight">Height (px)</label>
            <input type="number" id="setThumbHeight" min="180" max="720" step="10" />
          </div>
          <div class="settings-field">
            <label for="setThumbFps">Animated FPS (6–30)</label>
            <input type="number" id="setThumbFps" min="6" max="30" step="1" />
          </div>
          <div class="settings-field">
            <label for="setThumbQuality">Animated Quality (30–100)</label>
            <input type="number" id="setThumbQuality" min="30" max="100" step="1" />
          </div>
          <div class="settings-field">
            <label for="setThumbStaticQuality">1f Static Quality (40–100)</label>
            <input type="number" id="setThumbStaticQuality" min="40" max="100" step="1" />
          </div>
          <div class="settings-field">
            <label for="setThumbWarnKb">Size Warning (KB)</label>
            <input type="number" id="setThumbWarnKb" min="100" max="10240" step="64" />
          </div>
        </div>
      </div>

      <div class="settings-group">
        <div class="settings-group-title" style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
          <span>Converted Videos (.mp4 · H.264)</span>
          <label class="check-label" style="font-size:11.5px; font-weight:600; text-transform:none; letter-spacing:0;" title="Burns 2-line watermark (www.artdanylov.com / artem.danylov@gmail.com) into converted .mp4 videos only; thumbnails stay clean">
            <input type="checkbox" id="setWatermarkEnabled" checked />
            <span>Watermark (.mp4 only)</span>
          </label>
        </div>
        <div class="settings-grid">
          <div class="settings-field">
            <label for="setMaxWidth">Max Width (px)</label>
            <input type="number" id="setMaxWidth" min="640" max="3840" step="16" />
          </div>
          <div class="settings-field">
            <label for="setMaxHeight">Max Height (px)</label>
            <input type="number" id="setMaxHeight" min="360" max="2160" step="16" />
          </div>
          <div class="settings-field">
            <label for="setTargetFps">Max FPS (15–60)</label>
            <input type="number" id="setTargetFps" min="15" max="60" step="1" />
          </div>
          <div class="settings-field">
            <label for="setVideoCrf">CRF Quality (14=best, 28=small)</label>
            <input type="number" id="setVideoCrf" min="14" max="32" step="1" />
          </div>
          <div class="settings-field">
            <label for="setVideoMaxrate">Max Bitrate (kbps)</label>
            <input type="number" id="setVideoMaxrate" min="1000" max="25000" step="250" />
          </div>
          <div class="settings-field">
            <label for="setWebpQuality">Image WebP Quality (50–100)</label>
            <input type="number" id="setWebpQuality" min="50" max="100" step="1" />
          </div>
          <div class="settings-field">
            <label for="setWatermarkOpacity">Watermark Opacity % (10–100)</label>
            <input type="number" id="setWatermarkOpacity" min="10" max="100" step="5" />
          </div>
          <div class="settings-field">
            <label for="setWatermarkScale">Watermark Size % (50–200)</label>
            <input type="number" id="setWatermarkScale" min="50" max="200" step="5" />
          </div>
        </div>
      </div>

      <div class="settings-footer">
        <button type="button" id="resetSettingsBtn">↺ Reset Defaults</button>
        <div style="display:flex; gap:8px;">
          <button type="button" id="cancelSettingsBtn">Cancel</button>
          <button type="button" class="btn-primary" id="saveSettingsBtn">💾 Save Settings</button>
        </div>
      </div>
    </div>
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
  function loadStoredSet(key) {
    try {
      const raw = localStorage.getItem(key);
      if (raw) {
        const arr = JSON.parse(raw);
        if (Array.isArray(arr)) return new Set(arr);
      }
    } catch {}
    return new Set();
  }
  function saveStoredSet(key, setObj) {
    try { localStorage.setItem(key, JSON.stringify(Array.from(setObj))); } catch {}
  }
  const expandedCats = loadStoredSet('ad_studio_expanded_cats');
  const expandedSubs = loadStoredSet('ad_studio_expanded_subs');
  const openImgThumbIds = loadStoredSet('ad_studio_open_img_thumb_ids');
  let lastSavedSelectedId = null;
  try { lastSavedSelectedId = localStorage.getItem('ad_studio_selected_id') || null; } catch {}

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
  const toggleImageThumbUi = document.getElementById('toggleImageThumbUi');
  const imageThumbPanel = document.getElementById('imageThumbPanel');
  const imgThumbStatusLabel = document.getElementById('imgThumbStatusLabel');
  const imgThumbPreviewBox = document.getElementById('imgThumbPreviewBox');
  const imgThumbPreviewImg = document.getElementById('imgThumbPreviewImg');
  const imgCropInfoBadge = document.getElementById('imgCropInfoBadge');
  const imgCropZoomSlider = document.getElementById('imgCropZoomSlider');
  const imgCropFitBtn = document.getElementById('imgCropFitBtn');
  const imgCropNativeBtn = document.getElementById('imgCropNativeBtn');
  const imgCropCenterBtn = document.getElementById('imgCropCenterBtn');
  const imgThumbAnimatedToggle = document.getElementById('imgThumbAnimatedToggle');
  const imgAnimTimingRow = document.getElementById('imgAnimTimingRow');
  const imgThumbStartInput = document.getElementById('imgThumbStartInput');
  const imgThumbDurInput = document.getElementById('imgThumbDurInput');
  const imgThumbQualityInput = document.getElementById('imgThumbQualityInput');
  const generateImageThumbBtn = document.getElementById('generateImageThumbBtn');
  const imgThumbSizeWarn = document.getElementById('imgThumbSizeWarn');
  const imgThumbPathBox = document.getElementById('imgThumbPathBox');
  const imgThumbPathText = document.getElementById('imgThumbPathText');
  const deleteImgThumbBtn = document.getElementById('deleteImgThumbBtn');
  const previewColResizer = document.getElementById('previewColResizer');
  const emptyState = document.getElementById('emptyState');

  const playerStage = document.getElementById('playerStage');
  const playerResizer = document.getElementById('playerResizer');
  const fitPlayerHeightBtn = document.getElementById('fitPlayerHeightBtn');
  const player = document.getElementById('videoPlayer');
  const animPreviewBox = document.getElementById('animPreviewBox');
  const animPreviewVideo = document.getElementById('animPreviewVideo');
  const savedAnimImg = document.getElementById('savedAnimImg');
  const cropZoomSlider = document.getElementById('cropZoomSlider');
  const cropInfoBadge = document.getElementById('cropInfoBadge');
  const cropFitBtn = document.getElementById('cropFitBtn');
  const cropNativeBtn = document.getElementById('cropNativeBtn');
  const cropCenterBtn = document.getElementById('cropCenterBtn');

  const currentVideoTitle = document.getElementById('currentVideoTitle');
  const videoSizeBadge = document.getElementById('videoSizeBadge');
  const currentTimeLabel = document.getElementById('currentTimeLabel');
  const animShortBadge = document.getElementById('animShortBadge');
  const animRangeBadge = document.getElementById('animRangeBadge');
  const enableAnimToggle = document.getElementById('enableAnimToggle');
  const animControlCard = document.getElementById('animControlCard');
  const animPreviewPane = document.getElementById('animPreviewPane');
  const thumbStatusLabel = document.getElementById('thumbStatusLabel');
  const thumbSizeWarn = document.getElementById('thumbSizeWarn');
  const thumbSpecLabel = document.getElementById('thumbSpecLabel');
  const previewSpecLabel = document.getElementById('previewSpecLabel');
  const settingsBackdrop = document.getElementById('settingsBackdrop');
  const openSettingsBtn = document.getElementById('openSettingsBtn');
  const closeSettingsBtn = document.getElementById('closeSettingsBtn');
  const cancelSettingsBtn = document.getElementById('cancelSettingsBtn');
  const saveSettingsBtn = document.getElementById('saveSettingsBtn');
  const resetSettingsBtn = document.getElementById('resetSettingsBtn');

  let studioSettings = {
    max_width: 1920,
    max_height: 1080,
    target_fps: 30,
    video_crf: 21,
    video_maxrate_kbps: 5000,
    watermark_enabled: 1,
    watermark_opacity: 35,
    watermark_scale_pct: 100,
    webp_quality: 92,
    thumb_width: 640,
    thumb_height: 360,
    thumb_fps: 18,
    thumb_quality: 72,
    thumb_static_quality: 85,
    thumb_warn_kb: 1024,
  };
  let THUMB_WARN_KB = studioSettings.thumb_warn_kb;

  function applySettingsToUi(cfg) {
    if (cfg && typeof cfg === 'object') {
      studioSettings = { ...studioSettings, ...cfg };
    }
    THUMB_WARN_KB = Number(studioSettings.thumb_warn_kb) || 1024;
    const tw = studioSettings.thumb_width || 640;
    const th = studioSettings.thumb_height || 360;
    const tfps = studioSettings.thumb_fps || 18;
    thumbSpecLabel.textContent = `Thumbnail (_thumb.webp · ${tw}×${th} @ ${tfps} fps, 1f = static)`;
    previewSpecLabel.textContent = `_thumb.webp (${tw}×${th})`;
    cropNativeBtn.textContent = `1:1 (${tw}×${th})`;
    cropNativeBtn.title = `Crop exact ${tw}×${th} native pixels without scaling down`;
    imgCropNativeBtn.textContent = `1:1 (${tw}×${th})`;
    imgCropNativeBtn.title = `Crop exact ${tw}×${th} native pixels without scaling down`;
    animPreviewBox.style.aspectRatio = `${tw} / ${th}`;
    imgThumbPreviewBox.style.aspectRatio = `${tw} / ${th}`;
    const qPh = `${studioSettings.thumb_quality || 72}`;
    animQualityInput.placeholder = qPh;
    imgThumbQualityInput.placeholder = qPh;
    videoCrfInput.placeholder = `${studioSettings.video_crf || 21}`;
    applyCropPreview();
    applyImageCropPreview();
    updateCheckboxVisualState();
    updateImageThumbUiState();
    updateBadgesAndTimeline();
    renderTree();
  }

  function populateSettingsForm(cfg) {
    const c = cfg || studioSettings;
    document.getElementById('setThumbWidth').value = c.thumb_width;
    document.getElementById('setThumbHeight').value = c.thumb_height;
    document.getElementById('setThumbFps').value = c.thumb_fps;
    document.getElementById('setThumbQuality').value = c.thumb_quality;
    document.getElementById('setThumbStaticQuality').value = c.thumb_static_quality;
    document.getElementById('setThumbWarnKb').value = c.thumb_warn_kb;
    document.getElementById('setMaxWidth').value = c.max_width;
    document.getElementById('setMaxHeight').value = c.max_height;
    document.getElementById('setTargetFps').value = c.target_fps;
    document.getElementById('setVideoCrf').value = c.video_crf;
    document.getElementById('setVideoMaxrate').value = c.video_maxrate_kbps;
    document.getElementById('setWatermarkEnabled').checked = Boolean(c.watermark_enabled);
    document.getElementById('setWatermarkOpacity').value = c.watermark_opacity ?? 35;
    document.getElementById('setWatermarkScale').value = c.watermark_scale_pct ?? 100;
    document.getElementById('setWebpQuality').value = c.webp_quality;
  }

  function openSettingsModal() {
    populateSettingsForm(studioSettings);
    settingsBackdrop.classList.add('is-open');
    settingsBackdrop.setAttribute('aria-hidden', 'false');
  }

  function closeSettingsModal() {
    settingsBackdrop.classList.remove('is-open');
    settingsBackdrop.setAttribute('aria-hidden', 'true');
  }

  openSettingsBtn.addEventListener('click', openSettingsModal);
  closeSettingsBtn.addEventListener('click', closeSettingsModal);
  cancelSettingsBtn.addEventListener('click', closeSettingsModal);
  settingsBackdrop.addEventListener('click', (e) => {
    if (e.target === settingsBackdrop) closeSettingsModal();
  });
  document.querySelectorAll('.thumb-res-preset').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.getElementById('setThumbWidth').value = btn.dataset.w;
      document.getElementById('setThumbHeight').value = btn.dataset.h;
    });
  });

  saveSettingsBtn.addEventListener('click', async () => {
    const payload = {
      thumb_width: Number(document.getElementById('setThumbWidth').value),
      thumb_height: Number(document.getElementById('setThumbHeight').value),
      thumb_fps: Number(document.getElementById('setThumbFps').value),
      thumb_quality: Number(document.getElementById('setThumbQuality').value),
      thumb_static_quality: Number(document.getElementById('setThumbStaticQuality').value),
      thumb_warn_kb: Number(document.getElementById('setThumbWarnKb').value),
      max_width: Number(document.getElementById('setMaxWidth').value),
      max_height: Number(document.getElementById('setMaxHeight').value),
      target_fps: Number(document.getElementById('setTargetFps').value),
      video_crf: Number(document.getElementById('setVideoCrf').value),
      video_maxrate_kbps: Number(document.getElementById('setVideoMaxrate').value),
      watermark_enabled: document.getElementById('setWatermarkEnabled').checked ? 1 : 0,
      watermark_opacity: Number(document.getElementById('setWatermarkOpacity').value),
      watermark_scale_pct: Number(document.getElementById('setWatermarkScale').value),
      webp_quality: Number(document.getElementById('setWebpQuality').value),
    };
    saveSettingsBtn.disabled = true;
    try {
      const res = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings: payload }),
      });
      const data = await res.json();
      if (data.settings) {
        applySettingsToUi(data.settings);
        showToast('Quality settings saved!');
        closeSettingsModal();
      }
    } catch {
      showToast('Failed to save settings.', true);
    } finally {
      saveSettingsBtn.disabled = false;
    }
  });

  resetSettingsBtn.addEventListener('click', async () => {
    resetSettingsBtn.disabled = true;
    try {
      const res = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reset: true }),
      });
      const data = await res.json();
      if (data.settings) {
        populateSettingsForm(data.settings);
        applySettingsToUi(data.settings);
        showToast('Settings reset to defaults.');
      }
    } catch {
      showToast('Failed to reset settings.', true);
    } finally {
      resetSettingsBtn.disabled = false;
    }
  });
  const animStartInput = document.getElementById('animStartInput');
  const animDurInput = document.getElementById('animDurInput');
  const animQualityInput = document.getElementById('animQualityInput');
  const videoCrfInput = document.getElementById('videoCrfInput');
  const reencodeVideoBtn = document.getElementById('reencodeVideoBtn');
  const previewAnimBtn = document.getElementById('previewAnimBtn');
  const previewSourceLiveBtn = document.getElementById('previewSourceLiveBtn');
  const previewSourceDiskBtn = document.getElementById('previewSourceDiskBtn');

  const videoPathText = document.getElementById('videoPathText');
  const thumbPathBox = document.getElementById('thumbPathBox');
  const thumbPathText = document.getElementById('thumbPathText');
  const deleteThumbBtn = document.getElementById('deleteThumbBtn');

  const timelineTrack = document.getElementById('timelineTrack');
  const timelineWindow = document.getElementById('timelineWindow');
  const timelinePlayhead = document.getElementById('timelinePlayhead');

  function formatSizeKb(kb) {
    const n = Number(kb) || 0;
    return n >= 1024 ? `${(n / 1024).toFixed(2)} MB` : `${Math.round(n)} KB`;
  }

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
      if (data.settings) {
        studioSettings = { ...studioSettings, ...data.settings };
        THUMB_WARN_KB = Number(studioSettings.thumb_warn_kb) || 1024;
      }
      pendingConvertCount = data.pending_to_convert || 0;
      convertNewBtn.textContent = pendingConvertCount > 0
        ? `🔄 Convert from Source Videos (${pendingConvertCount} new)`
        : '🔄 Convert from Source Videos';
      convertNewBtn.classList.toggle('active-pill', pendingConvertCount > 0);

      mediaItems = (data.items || data.videos || []).map(item => (
        item.kind === 'video'
          ? {
              ...item,
              enable_anim: Boolean(item.has_thumb),
            }
          : item
      ));

      updateFilterButtons();
      applySettingsToUi(studioSettings);
      const targetId = preserveSelected ? selectedId : (selectedId || lastSavedSelectedId);
      if (targetId && mediaItems.some(v => v.id === targetId)) {
        selectItem(targetId);
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
        if (v.kind === 'video') {
          const entry = {
            anim_start: Number(v.anim_start) || 0,
            anim_duration: Number(v.anim_duration) || 1.0,
            crop_zoom: Math.round((Number(v.crop_zoom) || 1.0) * 1000) / 1000,
            crop_pan_x: Math.round((Number(v.crop_pan_x) || 0.0) * 10000) / 10000,
            crop_pan_y: Math.round((Number(v.crop_pan_y) || 0.0) * 10000) / 10000,
          };
          if (v.thumb_quality) entry.thumb_quality = Number(v.thumb_quality);
          if (v.video_crf) entry.video_crf = Number(v.video_crf);
          state[v.id] = entry;
        } else if (v.kind === 'image' && (v.has_thumb || v.img_animated || v.thumb_quality || (v.crop_zoom && v.crop_zoom > 1.001) || Math.abs(v.crop_pan_x || 0) > 0.001 || Math.abs(v.crop_pan_y || 0) > 0.001)) {
          const entry = {
            crop_zoom: Math.round((Number(v.crop_zoom) || 1.0) * 1000) / 1000,
            crop_pan_x: Math.round((Number(v.crop_pan_x) || 0.0) * 10000) / 10000,
            crop_pan_y: Math.round((Number(v.crop_pan_y) || 0.0) * 10000) / 10000,
            img_animated: Boolean(v.img_animated),
            img_start: Number(v.img_start) || 0,
            img_duration: Number(v.img_duration) || 0,
          };
          if (v.thumb_quality) entry.thumb_quality = Number(v.thumb_quality);
          if (v.video_crf) entry.video_crf = Number(v.video_crf);
          state[v.id] = entry;
        }
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
      if (filterMissingOnly && (v.kind !== 'video' || v.has_thumb)) return false;
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
      const isCatCollapsed = !expandedCats.has(cat);

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
        if (expandedCats.has(cat)) expandedCats.delete(cat);
        else expandedCats.add(cat);
        saveStoredSet('ad_studio_expanded_cats', expandedCats);
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
          const isSubCollapsed = !expandedSubs.has(subKey);

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
              if (expandedSubs.has(subKey)) expandedSubs.delete(subKey);
              else expandedSubs.add(subKey);
              saveStoredSet('ad_studio_expanded_subs', expandedSubs);
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
                      <span>· ${formatSizeKb(item.size_kb)}</span>
                      <span class="badge ${item.has_thumb ? (item.thumb_size_kb > THUMB_WARN_KB ? 'badge-warn' : 'badge-ok') : 'badge-off'}" title="${item.has_thumb ? Math.round(item.thumb_size_kb) + ' KB' : 'No thumbnail'}">${item.has_thumb && item.thumb_size_kb > THUMB_WARN_KB ? 'THUMB ⚠' : 'THUMB'}</span>
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

    const showDiskAnim = previewSourceMode === 'disk' && v.has_thumb;

    animPreviewVideo.style.display = showDiskAnim ? 'none' : 'block';
    savedAnimImg.style.display = showDiskAnim ? 'block' : 'none';

    const ts = Date.now();
    if (v.has_thumb) savedAnimImg.src = `/api/file?rel=${encodeURIComponent(v.thumb_rel)}&t=${ts}`;
    applyCropPreview();
  }

  function computeCropMetrics(v) {
    const W = v.width || 1920;
    const H = v.height || 1080;
    const tw = studioSettings.thumb_width || 640;
    const th = studioSettings.thumb_height || 360;
    const targetAr = tw / th;
    let baseW, baseH;
    if ((W / H) >= targetAr) {
      baseH = H;
      baseW = baseH * targetAr;
    } else {
      baseW = W;
      baseH = baseW / targetAr;
    }
    const nativeZoom = baseW / tw;
    const maxZoom = Math.min(8, Math.max(4, Math.ceil(Math.max(nativeZoom, 1) * 1.5 * 10) / 10));
    const zoom = Math.max(1, Math.min(maxZoom, Number(v.crop_zoom) || 1));
    const panX = Math.max(-1, Math.min(1, Number(v.crop_pan_x) || 0));
    const panY = Math.max(-1, Math.min(1, Number(v.crop_pan_y) || 0));
    const cropW = baseW / zoom;
    const cropH = baseH / zoom;
    const maxX = Math.max(0, W - cropW);
    const maxY = Math.max(0, H - cropH);
    const cropX = (maxX / 2) * (1 + panX);
    const cropY = (maxY / 2) * (1 + panY);
    return { W, H, baseW, baseH, nativeZoom, maxZoom, zoom, panX, panY, cropW, cropH, maxX, maxY, cropX, cropY };
  }

  function applyCropPreview() {
    const v = getSelectedVideo();
    if (!v) return;
    const m = computeCropMetrics(v);
    v.crop_zoom = m.zoom;
    v.crop_pan_x = m.panX;
    v.crop_pan_y = m.panY;

    cropZoomSlider.max = String(m.maxZoom);
    cropZoomSlider.value = String(m.zoom);

    const cw = Math.round(m.cropW);
    const ch = Math.round(m.cropH);
    const isNative = m.nativeZoom > 1.01 && Math.abs(m.zoom - m.nativeZoom) < 0.02;
    cropInfoBadge.textContent = `${m.zoom.toFixed(2)}× · ${cw}×${ch}${isNative ? ' (1:1)' : ''}`;

    cropFitBtn.classList.toggle('active-pill', m.zoom <= 1.005);
    cropNativeBtn.disabled = m.nativeZoom <= 1.01;
    cropNativeBtn.classList.toggle('active-pill', isNative);
    cropCenterBtn.disabled = Math.abs(m.panX) < 0.005 && Math.abs(m.panY) < 0.005;

    const showDisk = previewSourceMode === 'disk' && v.has_thumb;
    const canPan = !showDisk && (m.maxX > 0.5 || m.maxY > 0.5);
    animPreviewBox.classList.toggle('is-pannable', canPan);

    animPreviewVideo.style.width = `${(m.W / m.cropW) * 100}%`;
    animPreviewVideo.style.height = `${(m.H / m.cropH) * 100}%`;
    animPreviewVideo.style.left = `${-(m.cropX / m.cropW) * 100}%`;
    animPreviewVideo.style.top = `${-(m.cropY / m.cropH) * 100}%`;
  }

  function updateCheckboxVisualState() {
    const v = getSelectedVideo();
    if (!v) return;
    enableAnimToggle.checked = Boolean(v.enable_anim);

    animControlCard.classList.toggle('is-disabled', !v.enable_anim);
    animPreviewPane.classList.toggle('is-disabled', !v.enable_anim);
    animPreviewPane.classList.toggle('is-enabled', Boolean(v.enable_anim));

    const sizeKb = Number(v.thumb_size_kb) || 0;
    const isHeavy = v.has_thumb && sizeKb > THUMB_WARN_KB;
    thumbStatusLabel.textContent = v.has_thumb
      ? `Saved on disk · ${Math.round(sizeKb)} KB`
      : 'Not created yet';
    thumbStatusLabel.style.color = !v.has_thumb ? 'var(--muted)' : isHeavy ? '#fbbf24' : 'var(--green)';
    if (isHeavy) {
      thumbSizeWarn.style.display = 'block';
      const warnLabel = THUMB_WARN_KB >= 1024 ? `${(THUMB_WARN_KB / 1024).toFixed(THUMB_WARN_KB % 1024 === 0 ? 0 : 1)} MB` : `${THUMB_WARN_KB} KB`;
      const curLabel = sizeKb >= 1024 ? `${(sizeKb / 1024).toFixed(2)} MB` : `${Math.round(sizeKb)} KB`;
      thumbSizeWarn.textContent = `⚠ Thumbnail is ${curLabel} (> ${warnLabel}). Shorten clip length (e.g. 0.5–1.5s), lower quality/FPS in Settings, or use 1f static.`;
    } else {
      thumbSizeWarn.style.display = 'none';
      thumbSizeWarn.textContent = '';
    }
    thumbPathBox.style.display = v.has_thumb ? 'flex' : 'none';
    previewSourceDiskBtn.disabled = !v.has_thumb;
    if (previewSourceMode === 'disk' && !v.has_thumb) {
      previewSourceMode = 'live';
    }
    updatePreviewSourceVisibility();
  }

  function setMainPreviewMode(mode) {
    mainPreviewMode = mode;
    previewAnimBtn.classList.toggle('active-pill', mainPreviewMode === 'anim');

    const v = getSelectedVideo();
    if (!v) return;

    if (mainPreviewMode === 'anim') {
      player.currentTime = v.anim_start;
      if (v.anim_duration <= 0.05) {
        player.pause();
      } else {
        player.play().catch(() => {});
      }
    }
  }

  function selectItem(id) {
    selectedId = id;
    lastSavedSelectedId = id;
    try {
      if (id) localStorage.setItem('ad_studio_selected_id', id);
      else localStorage.removeItem('ad_studio_selected_id');
    } catch {}
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
      const imgUrl = `/api/file?rel=${encodeURIComponent(item.image_rel)}&t=${Date.now()}`;
      imageViewerImg.onload = () => {
        item.width = imageViewerImg.naturalWidth || 1920;
        item.height = imageViewerImg.naturalHeight || 1080;
        imageMetaLabel.textContent = `${item.width}×${item.height} · ${Math.round(item.size_kb)} KB`;
        applyImageCropPreview();
      };
      imageViewerImg.src = imgUrl;
      imgThumbPreviewImg.src = imgUrl;

      toggleImageThumbUi.checked = openImgThumbIds.has(item.id);
      imgThumbAnimatedToggle.checked = Boolean(item.img_animated);
      imgThumbStartInput.value = String(Number(item.img_start) || 0);
      imgThumbDurInput.value = String(Number(item.img_duration) || 0);
      imgThumbQualityInput.value = item.thumb_quality ? String(item.thumb_quality) : '';
      updateImageThumbUiState();
      return;
    }

    const v = item;
    emptyState.style.display = 'none';
    imageViewerContent.style.display = 'none';
    editorContent.style.display = 'grid';
    generateSingleBtn.disabled = false;

    currentVideoTitle.textContent = `${v.id} (${v.width}×${v.height}, ${v.fps} fps)`;
    videoSizeBadge.textContent = formatSizeKb(v.size_kb);
    videoPathText.textContent = v.video_rel;
    thumbPathText.textContent = v.thumb_rel;

    previewSourceMode = 'live';
    updateCheckboxVisualState();

    const dur = Math.max(v.duration, 0.1);
    animStartInput.max = String(Math.max(0, dur - 0.04));
    animStartInput.value = String(v.anim_start);
    animDurInput.value = String(v.anim_duration);
    animQualityInput.value = v.thumb_quality ? String(v.thumb_quality) : '';
    videoCrfInput.value = v.video_crf ? String(v.video_crf) : '';
    reencodeVideoBtn.disabled = !v.has_source;
    reencodeVideoBtn.title = v.has_source
      ? 'Re-encode this video from its original source in media_local/_toConvert/'
      : 'Original source video not found in media_local/_toConvert/ (re-encoding from Converted is disabled to avoid quality loss)';

    const streamUrl = `/api/stream?id=${encodeURIComponent(v.id)}`;
    player.src = streamUrl;
    player.currentTime = v.anim_start;

    animPreviewVideo.src = streamUrl;
    animPreviewVideo.currentTime = v.anim_start;
    if (v.anim_duration <= 0.05) {
      animPreviewVideo.pause();
    } else {
      animPreviewVideo.play().catch(() => {});
    }

    setMainPreviewMode('free');
    applyCropPreview();
    updateBadgesAndTimeline();
  }

  function updateBadgesAndTimeline() {
    const v = getSelectedVideo();
    if (!v) return;
    const dur = Math.max(v.duration, 0.1);
    const isStatic = v.anim_duration <= 0.05;
    const end = Math.min(dur, v.anim_start + v.anim_duration);

    animShortBadge.textContent = isStatic
      ? `${v.anim_start.toFixed(2)}s (1f)`
      : `${v.anim_start.toFixed(2)}s–${end.toFixed(2)}s`;
    animRangeBadge.textContent = isStatic
      ? `${v.anim_start.toFixed(2)}s (1 frame static)`
      : `${v.anim_start.toFixed(2)}s — ${end.toFixed(2)}s (${v.anim_duration.toFixed(2)}s @ ${studioSettings.thumb_fps || 18}fps)`;

    const leftPct = (v.anim_start / dur) * 100;
    const widthPct = Math.min(100 - leftPct, (v.anim_duration / dur) * 100);
    timelineWindow.style.left = `${leftPct}%`;
    timelineWindow.style.width = `${Math.max(widthPct, 1)}%`;

    timelinePlayhead.style.left = `${((player.currentTime || 0) / dur) * 100}%`;
    currentTimeLabel.textContent = `${(player.currentTime || 0).toFixed(2)}s / ${dur.toFixed(2)}s`;

    document.querySelectorAll('.dur-preset').forEach(btn => {
      btn.classList.toggle('active-pill', Math.abs(parseFloat(btn.dataset.dur) - v.anim_duration) < 0.015);
    });
  }

  animPreviewVideo.addEventListener('loadeddata', () => {
    const v = getSelectedVideo();
    if (v) {
      animPreviewVideo.currentTime = v.anim_start;
      if (v.anim_duration <= 0.05) {
        animPreviewVideo.pause();
      } else {
        animPreviewVideo.play().catch(() => {});
      }
    }
  });

  animPreviewVideo.addEventListener('timeupdate', () => {
    const v = getSelectedVideo();
    if (!v) return;
    if (v.anim_duration <= 0.05) {
      if (!animPreviewVideo.paused) animPreviewVideo.pause();
      if (Math.abs(animPreviewVideo.currentTime - v.anim_start) > 0.05) {
        animPreviewVideo.currentTime = v.anim_start;
      }
      return;
    }
    const end = Math.min(v.duration, v.anim_start + v.anim_duration);
    if (animPreviewVideo.currentTime >= end || animPreviewVideo.currentTime < v.anim_start - 0.05) {
      animPreviewVideo.currentTime = v.anim_start;
      if (animPreviewVideo.paused) animPreviewVideo.play().catch(() => {});
    }
  });

  player.addEventListener('loadeddata', () => {
    const v = getSelectedVideo();
    if (v) player.currentTime = v.anim_start;
  });

  player.addEventListener('seeked', () => {
    updateBadgesAndTimeline();
  });

  player.addEventListener('timeupdate', () => {
    const v = getSelectedVideo();
    if (!v) return;
    if (mainPreviewMode === 'anim' && v.anim_duration > 0.05) {
      const end = Math.min(v.duration, v.anim_start + v.anim_duration);
      if (player.currentTime >= end || player.currentTime < v.anim_start - 0.05) {
        player.currentTime = v.anim_start;
        if (player.paused) player.play().catch(() => {});
      }
    }
    updateBadgesAndTimeline();
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

  previewAnimBtn.addEventListener('click', () => {
    setMainPreviewMode(mainPreviewMode === 'anim' ? 'free' : 'anim');
  });

  const stepPlayer = (delta) => {
    const v = getSelectedVideo();
    if (!v) return;
    player.pause();
    const next = Math.max(0, Math.min(v.duration, (player.currentTime || 0) + delta));
    player.currentTime = next;
    updateBadgesAndTimeline();
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
    if (v.anim_duration <= 0.05) {
      animPreviewVideo.pause();
    } else if (animPreviewVideo.paused) {
      animPreviewVideo.play().catch(() => {});
    }
  }

  function applyAnimDuration(newDur) {
    const v = getSelectedVideo();
    if (!v) return;
    previewSourceMode = 'live';
    const clamped = Math.round(Math.max(0.04, Math.min(Math.min(5.0, v.duration), newDur)) * 100) / 100;
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
    v.anim_start = Math.max(0, Math.min(v.duration - 0.04, parseFloat(animStartInput.value) || 0));
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

  animQualityInput.addEventListener('input', () => {
    const v = getSelectedVideo();
    if (!v) return;
    const raw = animQualityInput.value.trim();
    v.thumb_quality = raw ? Math.max(20, Math.min(100, parseInt(raw, 10) || 0)) : 0;
    scheduleSaveState();
  });

  videoCrfInput.addEventListener('input', () => {
    const v = getSelectedVideo();
    if (!v) return;
    const raw = videoCrfInput.value.trim();
    v.video_crf = raw ? Math.max(14, Math.min(32, parseInt(raw, 10) || 0)) : 0;
    scheduleSaveState();
  });

  reencodeVideoBtn.addEventListener('click', async () => {
    const v = getSelectedVideo();
    if (!v) return;
    const effCrf = v.video_crf || studioSettings.video_crf || 21;
    if (!window.confirm(`Re-encode "${v.id}" with CRF ${effCrf}? (Lower = higher quality, higher = smaller file)`)) {
      return;
    }
    reencodeVideoBtn.disabled = true;
    generateSingleBtn.disabled = true;
    reencodeVideoBtn.textContent = '⏳ Re-encoding...';
    showToast(`Re-encoding ${v.stem} (CRF ${effCrf})...`);

    // Release active video file handles on Windows before overwriting
    const savedTime = player.currentTime || v.anim_start || 0;
    for (const el of [player, animPreviewVideo]) {
      el.pause();
      el.removeAttribute('src');
      el.load();
    }
    await new Promise(r => setTimeout(r, 80));

    try {
      const res = await fetch('/api/reencode-video', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: v.id, video_crf: v.video_crf || 0 }),
      });
      const data = await res.json();
      if (!data.ok) {
        showToast(data.error || 'Re-encode failed.', true);
      } else {
        v.size_kb = Number(data.size_kb) || v.size_kb;
        videoSizeBadge.textContent = formatSizeKb(v.size_kb);
        renderTree();
        showToast(`Re-encoded ${v.stem} → ${formatSizeKb(v.size_kb)} (CRF ${data.crf}, ${data.used_source ? 'from source' : 'from Converted'})`);
      }
    } catch {
      showToast('Re-encode request failed.', true);
    } finally {
      const streamUrl = `/api/stream?id=${encodeURIComponent(v.id)}&t=${Date.now()}`;
      player.src = streamUrl;
      player.currentTime = savedTime;
      animPreviewVideo.src = streamUrl;
      animPreviewVideo.currentTime = v.anim_start;
      if (v.anim_duration > 0.05) animPreviewVideo.play().catch(() => {});
      reencodeVideoBtn.disabled = !v.has_source;
      generateSingleBtn.disabled = false;
      reencodeVideoBtn.textContent = '🔁 Re-encode Video';
    }
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
      const newStart = Math.max(0, Math.min(initialStart + initialDur - 0.04, initialStart + dt));
      v.anim_duration = Math.max(0.04, initialDur + (initialStart - newStart));
      v.anim_start = newStart;
      animPreviewVideo.currentTime = v.anim_start;
      player.currentTime = v.anim_start;
    } else if (dragMode === 'right') {
      v.anim_duration = Math.max(0.04, Math.min(v.duration - initialStart, initialDur + dt));
    }

    animStartInput.value = v.anim_start.toFixed(2);
    animDurInput.value = v.anim_duration.toFixed(2);
    updateBadgesAndTimeline();
    scheduleSaveState();
  });

  timelineTrack.addEventListener('pointerup', () => { dragMode = null; });

  // Crop / Pan & Zoom interactions on Live Preview
  function setVideoCrop(zoom, panX, panY) {
    const v = getSelectedVideo();
    if (!v) return;
    if (previewSourceMode !== 'live') {
      previewSourceMode = 'live';
      updatePreviewSourceVisibility();
    }
    v.crop_zoom = zoom;
    v.crop_pan_x = panX;
    v.crop_pan_y = panY;
    v.enable_anim = true;
    updateCheckboxVisualState();
    applyCropPreview();
    scheduleSaveState();
  }

  cropZoomSlider.addEventListener('input', () => {
    const v = getSelectedVideo();
    if (!v) return;
    setVideoCrop(parseFloat(cropZoomSlider.value) || 1, v.crop_pan_x || 0, v.crop_pan_y || 0);
  });

  cropFitBtn.addEventListener('click', () => {
    setVideoCrop(1, 0, 0);
  });

  cropNativeBtn.addEventListener('click', () => {
    const v = getSelectedVideo();
    if (!v) return;
    const m = computeCropMetrics(v);
    if (m.nativeZoom > 1.01) {
      setVideoCrop(m.nativeZoom, v.crop_pan_x || 0, v.crop_pan_y || 0);
    }
  });

  cropCenterBtn.addEventListener('click', () => {
    const v = getSelectedVideo();
    if (!v) return;
    setVideoCrop(v.crop_zoom || 1, 0, 0);
  });

  let panDragging = false;
  let panStartClientX = 0;
  let panStartClientY = 0;
  let panInitX = 0;
  let panInitY = 0;

  animPreviewBox.addEventListener('pointerdown', (e) => {
    const v = getSelectedVideo();
    if (!v) return;
    if (previewSourceMode === 'disk' && v.has_thumb) {
      previewSourceMode = 'live';
      updatePreviewSourceVisibility();
    }
    const m = computeCropMetrics(v);
    if (m.maxX <= 0.5 && m.maxY <= 0.5) return;
    panDragging = true;
    panStartClientX = e.clientX;
    panStartClientY = e.clientY;
    panInitX = m.panX;
    panInitY = m.panY;
    animPreviewBox.classList.add('is-dragging');
    animPreviewBox.setPointerCapture(e.pointerId);
  });

  animPreviewBox.addEventListener('pointermove', (e) => {
    if (!panDragging) return;
    const v = getSelectedVideo();
    if (!v) return;
    const m = computeCropMetrics(v);
    const rect = animPreviewBox.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dx = e.clientX - panStartClientX;
    const dy = e.clientY - panStartClientY;
    let nextPanX = panInitX;
    let nextPanY = panInitY;
    if (m.maxX > 0.5) {
      nextPanX = Math.max(-1, Math.min(1, panInitX - (dx * (m.cropW / rect.width)) / (m.maxX / 2)));
    }
    if (m.maxY > 0.5) {
      nextPanY = Math.max(-1, Math.min(1, panInitY - (dy * (m.cropH / rect.height)) / (m.maxY / 2)));
    }
    v.crop_pan_x = nextPanX;
    v.crop_pan_y = nextPanY;
    v.enable_anim = true;
    applyCropPreview();
    scheduleSaveState();
  });

  const endPanDrag = () => {
    if (!panDragging) return;
    panDragging = false;
    animPreviewBox.classList.remove('is-dragging');
    updateCheckboxVisualState();
  };
  animPreviewBox.addEventListener('pointerup', endPanDrag);
  animPreviewBox.addEventListener('pointercancel', endPanDrag);

  animPreviewBox.addEventListener('wheel', (e) => {
    const v = getSelectedVideo();
    if (!v) return;
    e.preventDefault();
    const m = computeCropMetrics(v);
    const factor = e.deltaY < 0 ? 1.12 : (1 / 1.12);
    const nextZoom = Math.max(1, Math.min(m.maxZoom, m.zoom * factor));
    setVideoCrop(nextZoom, m.panX, m.panY);
  }, { passive: false });

  // Keyboard shortcuts (Escape closes Settings; Space = Play/Pause, Left/Right = frame step)
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && settingsBackdrop.classList.contains('is-open')) {
      closeSettingsModal();
      return;
    }
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
    const extraMsg = item.kind === 'video' && item.has_thumb
      ? '\n\nIts generated _thumb.webp file will also be deleted.'
      : '';
    if (!window.confirm(`Delete "${item.id}" from Converted/?${extraMsg}`)) {
      return;
    }

    // Release active video handles before Windows deletes the file
    if (selectedId === item.id) {
      for (const el of [player, animPreviewVideo]) {
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
  bindCopyBtn('copyThumbPathBtn', v => v.has_thumb ? v.thumb_rel : '');
  bindCopyBtn('copyImagePathBtn', i => i.image_rel);
  bindCopyBtn('copyImageMarkdocBtn', i => i.image_rel ? `{% media src="${i.image_rel}" /%}` : '');
  bindCopyBtn('copyImgThumbPathBtn', i => i.has_thumb ? i.thumb_rel : '');

  function getSelectedImage() {
    const item = getSelectedItem();
    return item && item.kind === 'image' ? item : null;
  }

  function applyImageCropPreview() {
    const item = getSelectedImage();
    if (!item) return;
    const m = computeCropMetrics(item);
    item.crop_zoom = m.zoom;
    item.crop_pan_x = m.panX;
    item.crop_pan_y = m.panY;

    imgCropZoomSlider.max = String(m.maxZoom);
    imgCropZoomSlider.value = String(m.zoom);

    const cw = Math.round(m.cropW);
    const ch = Math.round(m.cropH);
    const isNative = m.nativeZoom > 1.01 && Math.abs(m.zoom - m.nativeZoom) < 0.02;
    imgCropInfoBadge.textContent = `${m.zoom.toFixed(2)}× · ${cw}×${ch}${isNative ? ' (1:1)' : ''}`;

    imgCropFitBtn.classList.toggle('active-pill', m.zoom <= 1.005);
    imgCropNativeBtn.disabled = m.nativeZoom <= 1.01;
    imgCropNativeBtn.classList.toggle('active-pill', isNative);
    imgCropCenterBtn.disabled = Math.abs(m.panX) < 0.005 && Math.abs(m.panY) < 0.005;

    const canPan = m.maxX > 0.5 || m.maxY > 0.5;
    imgThumbPreviewBox.classList.toggle('is-pannable', canPan);

    imgThumbPreviewImg.style.width = `${(m.W / m.cropW) * 100}%`;
    imgThumbPreviewImg.style.height = `${(m.H / m.cropH) * 100}%`;
    imgThumbPreviewImg.style.left = `${-(m.cropX / m.cropW) * 100}%`;
    imgThumbPreviewImg.style.top = `${-(m.cropY / m.cropH) * 100}%`;
  }

  function updateImageThumbUiState() {
    const item = getSelectedImage();
    if (!item) return;
    imageThumbPanel.classList.toggle('is-open', Boolean(toggleImageThumbUi.checked));
    imgAnimTimingRow.style.display = imgThumbAnimatedToggle.checked ? 'flex' : 'none';
    const curImgDur = Number(item.img_duration) || 0;
    document.querySelectorAll('.img-dur-preset').forEach((btn) => {
      btn.classList.toggle('active-pill', Math.abs(parseFloat(btn.dataset.dur) - curImgDur) < 0.02);
    });
    const sizeKb = Number(item.thumb_size_kb) || 0;
    const isHeavy = item.has_thumb && sizeKb > THUMB_WARN_KB;
    imgThumbStatusLabel.textContent = item.has_thumb
      ? `_thumb.webp saved · ${Math.round(sizeKb)} KB`
      : '';
    imgThumbStatusLabel.style.color = isHeavy ? '#fbbf24' : 'var(--green)';
    imgThumbPathBox.style.display = item.has_thumb ? 'flex' : 'none';
    imgThumbPathText.textContent = item.thumb_rel || '';
    if (isHeavy) {
      imgThumbSizeWarn.style.display = 'block';
      const warnLabel = THUMB_WARN_KB >= 1024 ? `${(THUMB_WARN_KB / 1024).toFixed(THUMB_WARN_KB % 1024 === 0 ? 0 : 1)} MB` : `${THUMB_WARN_KB} KB`;
      const curLabel = sizeKb >= 1024 ? `${(sizeKb / 1024).toFixed(2)} MB` : `${Math.round(sizeKb)} KB`;
      imgThumbSizeWarn.textContent = `⚠ Thumbnail is ${curLabel} (> ${warnLabel}). Lower quality/FPS in Settings or uncheck Animated.`;
    } else {
      imgThumbSizeWarn.style.display = 'none';
      imgThumbSizeWarn.textContent = '';
    }
    if (toggleImageThumbUi.checked) applyImageCropPreview();
  }

  toggleImageThumbUi.addEventListener('change', () => {
    const item = getSelectedImage();
    if (item) {
      if (toggleImageThumbUi.checked) openImgThumbIds.add(item.id);
      else openImgThumbIds.delete(item.id);
      saveStoredSet('ad_studio_open_img_thumb_ids', openImgThumbIds);
    }
    updateImageThumbUiState();
  });

  imgThumbAnimatedToggle.addEventListener('change', () => {
    const item = getSelectedImage();
    if (item) {
      item.img_animated = imgThumbAnimatedToggle.checked;
      updateImageThumbUiState();
      scheduleSaveState();
    }
  });

  imgThumbStartInput.addEventListener('input', () => {
    const item = getSelectedImage();
    if (!item) return;
    item.img_start = Math.max(0, parseFloat(imgThumbStartInput.value) || 0);
    scheduleSaveState();
  });

  imgThumbDurInput.addEventListener('input', () => {
    const item = getSelectedImage();
    if (!item) return;
    item.img_duration = Math.max(0, parseFloat(imgThumbDurInput.value) || 0);
    updateImageThumbUiState();
    scheduleSaveState();
  });

  imgThumbQualityInput.addEventListener('input', () => {
    const item = getSelectedImage();
    if (!item) return;
    const raw = imgThumbQualityInput.value.trim();
    item.thumb_quality = raw ? Math.max(20, Math.min(100, parseInt(raw, 10) || 0)) : 0;
    scheduleSaveState();
  });

  document.querySelectorAll('.img-dur-preset').forEach((btn) => {
    btn.addEventListener('click', () => {
      const item = getSelectedImage();
      if (!item) return;
      item.img_duration = parseFloat(btn.dataset.dur) || 0;
      imgThumbDurInput.value = String(item.img_duration);
      updateImageThumbUiState();
      scheduleSaveState();
    });
  });

  imgCropZoomSlider.addEventListener('input', () => {
    const item = getSelectedImage();
    if (!item) return;
    item.crop_zoom = parseFloat(imgCropZoomSlider.value) || 1;
    applyImageCropPreview();
    scheduleSaveState();
  });

  imgCropFitBtn.addEventListener('click', () => {
    const item = getSelectedImage();
    if (!item) return;
    item.crop_zoom = 1;
    item.crop_pan_x = 0;
    item.crop_pan_y = 0;
    applyImageCropPreview();
    scheduleSaveState();
  });

  imgCropNativeBtn.addEventListener('click', () => {
    const item = getSelectedImage();
    if (!item) return;
    const m = computeCropMetrics(item);
    if (m.nativeZoom > 1.01) {
      item.crop_zoom = m.nativeZoom;
      applyImageCropPreview();
      scheduleSaveState();
    }
  });

  imgCropCenterBtn.addEventListener('click', () => {
    const item = getSelectedImage();
    if (!item) return;
    item.crop_pan_x = 0;
    item.crop_pan_y = 0;
    applyImageCropPreview();
    scheduleSaveState();
  });

  let imgPanDragging = false;
  let imgPanStartX = 0;
  let imgPanStartY = 0;
  let imgPanInitX = 0;
  let imgPanInitY = 0;

  imgThumbPreviewBox.addEventListener('pointerdown', (e) => {
    const item = getSelectedImage();
    if (!item) return;
    const m = computeCropMetrics(item);
    if (m.maxX <= 0.5 && m.maxY <= 0.5) return;
    imgPanDragging = true;
    imgPanStartX = e.clientX;
    imgPanStartY = e.clientY;
    imgPanInitX = m.panX;
    imgPanInitY = m.panY;
    imgThumbPreviewBox.classList.add('is-dragging');
    imgThumbPreviewBox.setPointerCapture(e.pointerId);
  });

  imgThumbPreviewBox.addEventListener('pointermove', (e) => {
    if (!imgPanDragging) return;
    const item = getSelectedImage();
    if (!item) return;
    const m = computeCropMetrics(item);
    const rect = imgThumbPreviewBox.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dx = e.clientX - imgPanStartX;
    const dy = e.clientY - imgPanStartY;
    if (m.maxX > 0.5) {
      item.crop_pan_x = Math.max(-1, Math.min(1, imgPanInitX - (dx * (m.cropW / rect.width)) / (m.maxX / 2)));
    }
    if (m.maxY > 0.5) {
      item.crop_pan_y = Math.max(-1, Math.min(1, imgPanInitY - (dy * (m.cropH / rect.height)) / (m.maxY / 2)));
    }
    applyImageCropPreview();
    scheduleSaveState();
  });

  const endImgPan = () => {
    if (!imgPanDragging) return;
    imgPanDragging = false;
    imgThumbPreviewBox.classList.remove('is-dragging');
  };
  imgThumbPreviewBox.addEventListener('pointerup', endImgPan);
  imgThumbPreviewBox.addEventListener('pointercancel', endImgPan);

  imgThumbPreviewBox.addEventListener('wheel', (e) => {
    const item = getSelectedImage();
    if (!item) return;
    e.preventDefault();
    const m = computeCropMetrics(item);
    const factor = e.deltaY < 0 ? 1.12 : (1 / 1.12);
    item.crop_zoom = Math.max(1, Math.min(m.maxZoom, m.zoom * factor));
    applyImageCropPreview();
    scheduleSaveState();
  }, { passive: false });

  generateImageThumbBtn.addEventListener('click', async () => {
    const item = getSelectedImage();
    if (!item) return;
    generateImageThumbBtn.disabled = true;
    showToast(`Generating thumbnail for ${item.stem}...`);
    try {
      const res = await fetch('/api/generate-image-thumb', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: item.id,
          animated: Boolean(imgThumbAnimatedToggle.checked),
          crop_zoom: Number(item.crop_zoom) || 1.0,
          crop_pan_x: Number(item.crop_pan_x) || 0.0,
          crop_pan_y: Number(item.crop_pan_y) || 0.0,
          start_time: Number(item.img_start) || 0.0,
          duration: Number(item.img_duration) || 0.0,
          thumb_quality: Number(item.thumb_quality) || 0,
        }),
      });
      const data = await res.json();
      if (!data.ok) {
        showToast(data.error || 'Failed to generate image thumbnail.', true);
        return;
      }
      item.has_thumb = true;
      item.thumb_size_kb = Number(data.thumb_size_kb) || 0;
      item.thumb_rel = data.thumb_rel || item.thumb_rel;
      updateImageThumbUiState();
      if (item.thumb_size_kb > THUMB_WARN_KB) {
        showToast(`⚠ Large thumb (> ${THUMB_WARN_KB} KB): ${item.stem} (${Math.round(item.thumb_size_kb)} KB)`, true);
      } else {
        showToast(`Generated ${item.stem}_thumb.webp (${Math.round(item.thumb_size_kb)} KB)!`);
      }
    } catch {
      showToast('Image thumbnail generation failed.', true);
    } finally {
      generateImageThumbBtn.disabled = false;
    }
  });

  deleteImgThumbBtn.addEventListener('click', async () => {
    const item = getSelectedImage();
    if (!item || !item.has_thumb) return;
    if (!window.confirm(`Delete thumbnail "${item.thumb_rel}"?`)) return;
    deleteImgThumbBtn.disabled = true;
    try {
      const res = await fetch('/api/delete-thumb', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: item.id }),
      });
      const data = await res.json();
      if (!data.ok) {
        showToast(data.error || 'Failed to delete thumbnail.', true);
        return;
      }
      item.has_thumb = false;
      item.thumb_size_kb = 0;
      updateImageThumbUiState();
      showToast(`Deleted ${item.stem}_thumb.webp`);
    } catch {
      showToast('Failed to delete thumbnail.', true);
    } finally {
      deleteImgThumbBtn.disabled = false;
    }
  });

  deleteThumbBtn.addEventListener('click', async () => {
    const v = getSelectedVideo();
    if (!v || !v.has_thumb) return;
    if (!window.confirm(`Delete thumbnail "${v.thumb_rel}"?`)) return;
    deleteThumbBtn.disabled = true;
    try {
      const res = await fetch('/api/delete-thumb', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: v.id }),
      });
      const data = await res.json();
      if (!data.ok) {
        showToast(data.error || 'Failed to delete thumbnail.', true);
        return;
      }
      v.has_thumb = false;
      v.thumb_size_kb = 0;
      v.enable_anim = false;
      savedAnimImg.removeAttribute('src');
      updateCheckboxVisualState();
      renderTree();
      showToast(`Deleted ${v.stem}_thumb.webp`);
    } catch {
      showToast('Failed to delete thumbnail.', true);
    } finally {
      deleteThumbBtn.disabled = false;
    }
  });

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
      let heavyList = [];
      if (data.updated) {
        for (const [vidId, info] of Object.entries(data.updated)) {
          const item = mediaItems.find(v => v.id === vidId);
          if (item) {
            item.has_thumb = info.has_thumb;
            item.thumb_size_kb = Number(info.thumb_size_kb) || 0;
            item.enable_anim = info.has_thumb;
            if (item.has_thumb && item.thumb_size_kb > THUMB_WARN_KB) {
              heavyList.push(`${item.stem} (${Math.round(item.thumb_size_kb)} KB)`);
            }
          }
        }
      }
      updateCheckboxVisualState();
      renderTree();
      if (heavyList.length) {
        showToast(`⚠ Large thumb (> 1 MB): ${heavyList.join(', ')}`, true);
      } else {
        showToast(`Updated ${Object.keys(data.updated || {}).length} video(s)!`);
      }
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
    if (!v.enable_anim) {
      showToast('Check Thumbnail below first.', true);
      return;
    }
    runGenerateBatch(
      [{
        id: v.id,
        do_thumb: true,
        anim_start: Number(v.anim_start) || 0,
        anim_duration: Number(v.anim_duration) || 1.0,
        crop_zoom: Number(v.crop_zoom) || 1.0,
        crop_pan_x: Number(v.crop_pan_x) || 0.0,
        crop_pan_y: Number(v.crop_pan_y) || 0.0,
        thumb_quality: Number(v.thumb_quality) || 0,
      }],
      `Generating thumbnail for ${v.stem}...`
    );
  });

  updateAllThumbsBtn.addEventListener('click', () => {
    const targets = mediaItems.filter(v => v.kind === 'video' && (v.enable_anim || v.has_thumb));
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
        do_thumb: true,
        anim_start: Number(v.anim_start) || 0,
        anim_duration: Number(v.anim_duration) || 1.0,
        crop_zoom: Number(v.crop_zoom) || 1.0,
        crop_pan_x: Number(v.crop_pan_x) || 0.0,
        crop_pan_y: Number(v.crop_pan_y) || 0.0,
        thumb_quality: Number(v.thumb_quality) || 0,
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
    load_studio_settings()
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
                        "settings": load_studio_settings(),
                    }
                )
                return

            if parsed.path == "/api/settings":
                self._send_json({"ok": True, "settings": load_studio_settings(), "defaults": DEFAULT_SETTINGS})
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
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
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

            if self.path == "/api/settings":
                if data.get("reset"):
                    saved_cfg = save_studio_settings(DEFAULT_SETTINGS)
                else:
                    saved_cfg = save_studio_settings(data.get("settings") or {})
                wm_info = (
                    f"WM={saved_cfg['watermark_opacity']}%@{saved_cfg['watermark_scale_pct']}%"
                    if saved_cfg["watermark_enabled"]
                    else "WM=off"
                )
                print(
                    f"[Settings] Video: {saved_cfg['max_width']}x{saved_cfg['max_height']}@{saved_cfg['target_fps']}fps "
                    f"CRF={saved_cfg['video_crf']} maxrate={saved_cfg['video_maxrate_kbps']}k {wm_info} | "
                    f"Thumb: {saved_cfg['thumb_width']}x{saved_cfg['thumb_height']}@{saved_cfg['thumb_fps']}fps "
                    f"Q={saved_cfg['thumb_quality']} (1f Q={saved_cfg['thumb_static_quality']}) warn>{saved_cfg['thumb_warn_kb']}KB"
                )
                self._send_json({"ok": True, "settings": saved_cfg})
                return

            if self.path == "/api/convert-new":
                newly_converted = convert_all()
                produced_files.extend(newly_converted)
                if auto_upload and newly_converted:
                    upload_to_r2(newly_converted)
                self._send_json({"ok": True, "converted_count": len(newly_converted)})
                return

            if self.path == "/api/reencode-video":
                vid_id = str(data.get("id", "") or "").strip()
                item = catalog_map.get(vid_id)
                if not item:
                    self._send_json({"ok": False, "error": "Video not found."}, status=404)
                    return

                out_video = Path(item["video_path"])
                if not out_video.is_file():
                    self._send_json({"ok": False, "error": "Video file missing on disk."}, status=404)
                    return

                raw_crf = data.get("video_crf")
                custom_crf = _clamp_int(raw_crf, 0, 14, 32) if raw_crf else 0
                eff_crf = custom_crf if custom_crf else VIDEO_CRF

                src_video = find_source_video_for_converted(out_video)
                if not src_video:
                    self._send_json(
                        {
                            "ok": False,
                            "error": f"Original source not found in media_local/_toConvert/ for '{vid_id}'. Re-encoding from Converted/ is disabled.",
                        },
                        status=404,
                    )
                    return

                tmp_out = out_video.with_name(f"{out_video.stem}.__reencode_tmp.mp4")

                try:
                    info = get_video_info(src_video)
                    has_audio = info.get("has_audio", True)
                    orig_mb = out_video.stat().st_size / (1024 * 1024)
                    print(f"[Re-encode Video] {vid_id} (from _toConvert/{src_video.relative_to(INPUT_DIR).as_posix()}, CRF={eff_crf}, maxrate={VIDEO_MAXRATE_KBPS}k)")
                    encode_video(src_video, tmp_out, has_audio=has_audio, crf=eff_crf, max_bitrate_kbps=VIDEO_MAXRATE_KBPS)

                    replaced = False
                    last_err = None
                    for _ in range(8):
                        try:
                            os.replace(tmp_out, out_video)
                            replaced = True
                            break
                        except PermissionError as exc:
                            last_err = exc
                            time.sleep(0.15)
                    if not replaced:
                        tmp_out.unlink(missing_ok=True)
                        self._send_json({"ok": False, "error": f"Video file is in use: {last_err}"}, status=423)
                        return

                    new_size_kb = round(out_video.stat().st_size / 1024, 1)
                    print(f"  [OK] {orig_mb:.2f} MB -> {new_size_kb / 1024:.2f} MB")
                    item["size_kb"] = new_size_kb
                    item["video_crf"] = custom_crf

                    vid_state = state.get(vid_id, {})
                    if custom_crf:
                        vid_state["video_crf"] = custom_crf
                    else:
                        vid_state.pop("video_crf", None)
                    state[vid_id] = vid_state
                    save_studio_state(state)

                    produced_files.append(out_video)
                    if auto_upload:
                        upload_to_r2([out_video])

                    self._send_json({
                        "ok": True,
                        "size_kb": new_size_kb,
                        "crf": eff_crf,
                        "used_source": True,
                    })
                except Exception as exc:
                    tmp_out.unlink(missing_ok=True)
                    self._send_json({"ok": False, "error": str(exc)}, status=500)
                return

            if self.path == "/api/generate-image-thumb":
                rel = str(data.get("id", "") or "").strip()
                base = CONVERTED_DIR.resolve()
                target = (base / rel).resolve() if rel else None
                if not target or not target.is_relative_to(base) or not target.is_file():
                    self._send_json({"ok": False, "error": "Image file not found."}, status=404)
                    return

                animated = bool(data.get("animated", False))
                c_zoom = max(1.0, min(8.0, float(data.get("crop_zoom", 1.0))))
                c_pan_x = max(-1.0, min(1.0, float(data.get("crop_pan_x", 0.0))))
                c_pan_y = max(-1.0, min(1.0, float(data.get("crop_pan_y", 0.0))))
                t_start = max(0.0, float(data.get("start_time", 0.0)))
                t_dur = max(0.0, float(data.get("duration", 0.0)))
                t_q_raw = data.get("thumb_quality")
                t_q = _clamp_int(t_q_raw, 0, 20, 100) if t_q_raw else 0

                out_thumb = target.parent / f"{target.stem}_thumb.webp"
                src_image = find_source_image_for_converted(target)
                thumb_input = src_image if src_image else target
                src_tag = "source" if src_image else "Converted"
                dur_part = f", {t_start:.2f}s+{t_dur:.2f}s" if animated and t_dur > 0.04 else ""
                q_part = f", Q={t_q}" if animated and t_q else ""
                mode_label = f"animated {THUMB_FPS}fps{dur_part}{q_part}" if animated else "1f static"
                print(f"[Image Thumb] {rel} -> _thumb.webp ({THUMB_WIDTH}x{THUMB_HEIGHT}, {mode_label}, zoom={c_zoom:.2f}x, from {src_tag})")
                out_static = generate_image_thumb(thumb_input, out_thumb, animated, c_zoom, c_pan_x, c_pan_y, t_start, t_dur, t_q or None)
                thumb_kb = round(out_thumb.stat().st_size / 1024, 1)
                static_kb = round(out_static.stat().st_size / 1024, 1)
                print(f"  [OK] _thumb: {thumb_kb:.0f} KB | _thumb_static: {static_kb:.0f} KB")
                if thumb_kb > THUMB_WARN_KB:
                    print(f"  [WARN] Thumbnail is {thumb_kb / 1024:.2f} MB (> {THUMB_WARN_KB} KB).")

                img_state = {
                    "crop_zoom": round(c_zoom, 3),
                    "crop_pan_x": round(c_pan_x, 4),
                    "crop_pan_y": round(c_pan_y, 4),
                    "img_animated": animated,
                    "img_start": round(t_start, 2),
                    "img_duration": round(t_dur, 2),
                }
                if t_q:
                    img_state["thumb_quality"] = t_q
                state[rel] = img_state
                save_studio_state(state)

                batch_files = [out_thumb, out_static]
                produced_files.extend(batch_files)
                if auto_upload:
                    upload_to_r2(batch_files)

                self._send_json(
                    {
                        "ok": True,
                        "has_thumb": True,
                        "thumb_size_kb": thumb_kb,
                        "thumb_rel": out_thumb.relative_to(CONVERTED_DIR).as_posix(),
                    }
                )
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
                    t_start = float(entry.get("anim_start", item["anim_start"]))
                    t_dur = float(entry.get("anim_duration", item["anim_duration"]))
                    c_zoom = max(1.0, min(8.0, float(entry.get("crop_zoom", item.get("crop_zoom", 1.0)))))
                    c_pan_x = max(-1.0, min(1.0, float(entry.get("crop_pan_x", item.get("crop_pan_x", 0.0)))))
                    c_pan_y = max(-1.0, min(1.0, float(entry.get("crop_pan_y", item.get("crop_pan_y", 0.0)))))
                    t_q_raw = entry.get("thumb_quality", item.get("thumb_quality", 0))
                    t_q = _clamp_int(t_q_raw, 0, 20, 100) if t_q_raw else 0

                    item["anim_start"] = t_start
                    item["anim_duration"] = t_dur
                    item["crop_zoom"] = c_zoom
                    item["crop_pan_x"] = c_pan_x
                    item["crop_pan_y"] = c_pan_y
                    item["thumb_quality"] = t_q
                    vid_state = {
                        "anim_start": t_start,
                        "anim_duration": t_dur,
                        "crop_zoom": round(c_zoom, 3),
                        "crop_pan_x": round(c_pan_x, 4),
                        "crop_pan_y": round(c_pan_y, 4),
                    }
                    if t_q:
                        vid_state["thumb_quality"] = t_q
                    state[vid_id] = vid_state

                    video_path = Path(item["video_path"])
                    out_thumb = Path(item["out_thumb"])

                    if do_thumb:
                        src_video = find_source_video_for_converted(video_path)
                        thumb_input = src_video if src_video else video_path
                        src_tag = "source" if src_video else "Converted"
                        mode_label = "1f static" if t_dur <= (1.0 / THUMB_FPS) + 0.005 else f"{THUMB_FPS}fps, {t_start:.2f}s + {t_dur:.2f}s"
                        crop_label = f", zoom={c_zoom:.2f}x" if c_zoom > 1.001 or abs(c_pan_x) > 0.001 or abs(c_pan_y) > 0.001 else ""
                        q_label = f", Q={t_q}" if t_q else ""
                        print(f"[Thumb] {item['id']} -> _thumb.webp ({THUMB_WIDTH}x{THUMB_HEIGHT}, {mode_label}{crop_label}{q_label}, from {src_tag})")
                        out_static = generate_thumb(thumb_input, out_thumb, t_start, t_dur, c_zoom, c_pan_x, c_pan_y, t_q or None)
                        thumb_kb = round(out_thumb.stat().st_size / 1024, 1)
                        static_kb = round(out_static.stat().st_size / 1024, 1)
                        print(f"  [OK] _thumb: {thumb_kb:.0f} KB | _thumb_static: {static_kb:.0f} KB")
                        if thumb_kb > THUMB_WARN_KB:
                            print(f"  [WARN] Thumbnail is {thumb_kb / 1024:.2f} MB (> 1 MB) — consider shorter length or 1f static.")
                        item["has_thumb"] = True
                        item["thumb_size_kb"] = thumb_kb
                        batch_files.append(out_thumb)
                        batch_files.append(out_static)

                    updated[vid_id] = {
                        "has_thumb": item["has_thumb"],
                        "thumb_size_kb": item.get("thumb_size_kb", 0),
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
                for suffix in ("_thumb.webp", "_thumb_static.webp", "_animthumb.webp"):
                    p_extra = target.parent / f"{target.stem}{suffix}"
                    if p_extra.is_file() and p_extra != target:
                        to_remove.append(p_extra)

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

            if self.path == "/api/delete-thumb":
                rel = str(data.get("id", "") or "").strip()
                base = CONVERTED_DIR.resolve()
                target = (base / rel).resolve() if rel else None
                if not target or not target.is_relative_to(base) or not target.is_file():
                    self._send_json({"ok": False, "error": "Video file not found."}, status=404)
                    return

                to_remove = [
                    p
                    for p in (
                        target.parent / f"{target.stem}_thumb.webp",
                        target.parent / f"{target.stem}_thumb_static.webp",
                        target.parent / f"{target.stem}_animthumb.webp",
                    )
                    if p.is_file()
                ]
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
                    print(f"[Deleted Thumb] Converted/{p.relative_to(base).as_posix()}")

                item = catalog_map.get(rel)
                if item:
                    item["has_thumb"] = False
                self._send_json({"ok": True, "has_thumb": False})
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


def reencode_converted_videos(exclude: list[str] | None = None) -> list[Path]:
    """
    Re-encodes existing .mp4 files in Converted/ in-place from their original clean sources
    in _toConvert/ (preserving exact filenames and leaving _thumb.webp untouched).
    """
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    saved_state = load_studio_state()
    excl_tokens = [t.strip().replace("\\", "/").lower() for t in (exclude or []) if t and t.strip()]

    def is_excluded(conv: Path, rel_key: str) -> bool:
        rel_low = rel_key.lower()
        stem_low = conv.stem.lower()
        name_low = conv.name.lower()
        for tok in excl_tokens:
            if "/" in tok:
                if rel_low == tok or rel_low.startswith(tok.rstrip("/") + "/"):
                    return True
            else:
                if stem_low == tok or name_low == tok:
                    return True
        return False

    candidates = [
        p
        for p in sorted(CONVERTED_DIR.rglob("*.mp4"))
        if p.is_file() and not p.stem.endswith(".__reencode_tmp")
    ]

    targets: list[tuple[Path, str]] = []
    for conv in candidates:
        rel_key = conv.relative_to(CONVERTED_DIR).as_posix()
        if is_excluded(conv, rel_key):
            print(f"[Skip Excluded] Converted/{rel_key}", flush=True)
            continue
        targets.append((conv, rel_key))

    if not targets:
        print("No matching videos in Converted/ to re-encode.", flush=True)
        return []

    reencoded: list[Path] = []
    total = len(targets)
    for idx, (conv, rel_key) in enumerate(targets, start=1):
        src_video = find_source_video_for_converted(conv)
        if not src_video:
            print(f"[{idx}/{total}] [Skip Missing Source] Converted/{rel_key} (no source in _toConvert/)", flush=True)
            continue

        saved = saved_state.get(rel_key, {})
        raw_vcrf = saved.get("video_crf")
        custom_crf = _clamp_int(raw_vcrf, 0, 14, 32) if raw_vcrf else 0
        eff_crf = custom_crf if custom_crf else VIDEO_CRF

        tmp_out = conv.with_name(f"{conv.stem}.__reencode_tmp.mp4")
        src_rel = src_video.relative_to(INPUT_DIR).as_posix()
        old_mb = conv.stat().st_size / (1024 * 1024)
        orig_size = src_video.stat().st_size
        print(f"[{idx}/{total}] [Re-encode] _toConvert/{src_rel} -> Converted/{rel_key} (CRF={eff_crf})", flush=True)

        try:
            info = get_video_info(src_video)
            has_audio = info.get("has_audio", True)
            encode_video(src_video, tmp_out, has_audio=has_audio, crf=eff_crf)
            new_size = tmp_out.stat().st_size

            if not custom_crf and new_size > orig_size:
                duration = max(info["duration"], 1.0)
                audio_kbps = 160 if has_audio else 0
                target_kbps = max(int((orig_size * 8 / 1000) / duration * 0.90) - audio_kbps, 300)
                encode_video(src_video, tmp_out, has_audio=has_audio, crf=23, max_bitrate_kbps=target_kbps)
                new_size = tmp_out.stat().st_size

            os.replace(tmp_out, conv)
            print(f"  [OK] {old_mb:.2f} MB -> {new_size / (1024 * 1024):.2f} MB", flush=True)
            reencoded.append(conv)
        except Exception as exc:
            tmp_out.unlink(missing_ok=True)
            print(f"  [ERROR] Failed to re-encode {rel_key}: {exc}", flush=True)
            raise

    return reencoded


def main() -> None:
    load_studio_settings()
    parser = argparse.ArgumentParser(description="Convert local media, manage thumbnails, and sync to R2.")
    parser.add_argument("--upload", action="store_true", help="Upload newly converted files/thumbnails to R2")
    parser.add_argument("--upload-all", action="store_true", help="Upload all files in Converted/ to R2")
    parser.add_argument(
        "--reencode-videos",
        action="store_true",
        help="Re-encode existing .mp4 files in Converted/ from _toConvert/ sources (leaves thumbnails untouched)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude video stem or relative path when using --reencode-videos (repeatable, e.g. --exclude portfolio)",
    )
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

    if args.reencode_videos:
        produced = reencode_converted_videos(exclude=args.exclude)
    else:
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
