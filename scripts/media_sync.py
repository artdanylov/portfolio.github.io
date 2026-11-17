"""
Converts media from media_local/_toConvert -> media_local/Converted (preserving subfolders)
and optionally uploads new/updated files to Cloudflare R2 (media.artdanylov.com).

Usage:
  python scripts/media_sync.py               # convert new/updated only
  python scripts/media_sync.py --upload      # convert + upload newly converted to R2
  python scripts/media_sync.py --upload-all  # convert + upload all files in Converted/
"""

import argparse
import json
import mimetypes
import os
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
MEDIA_DIR = BASE_DIR / "media_local"
INPUT_DIR = MEDIA_DIR / "_toConvert"
CONVERTED_DIR = MEDIA_DIR / "Converted"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp", ".gif"}

MAX_WIDTH = 1920
MAX_HEIGHT = 1080
TARGET_FPS = 30
VIDEO_CRF = 18
WEBP_QUALITY = 92


def load_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def is_up_to_date(src: Path, out_file: Path) -> bool:
    """True if converted output already exists and is newer than or same age as source."""
    return out_file.exists() and out_file.stat().st_mtime >= src.stat().st_mtime


def clean_existing(out_dir: Path, stem: str) -> bool:
    existed = False
    for existing_file in out_dir.glob(f"{stem}.*"):
        if existing_file.is_file():
            existing_file.unlink(missing_ok=True)
            existed = True
    return existed


def get_video_info(src: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,pix_fmt,width,height,r_frame_rate:format=duration",
        "-of", "json", str(src),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    stream = data.get("streams", [{}])[0]
    fmt = data.get("format", {})
    fps_str = stream.get("r_frame_rate", "30/1")
    num, den = map(int, fps_str.split("/")) if "/" in fps_str else (30, 1)
    return {
        "codec": stream.get("codec_name", ""),
        "pix_fmt": stream.get("pix_fmt", ""),
        "width": int(stream.get("width", 0)),
        "height": int(stream.get("height", 0)),
        "fps": num / den if den else 30.0,
        "duration": float(fmt.get("duration", 0.0)),
    }


def is_web_ready(info: dict) -> bool:
    return (
        info["codec"] == "h264"
        and info["pix_fmt"] == "yuv420p"
        and info["width"] <= MAX_WIDTH
        and info["height"] <= MAX_HEIGHT
        and info["fps"] <= TARGET_FPS + 0.5
    )


def encode_video(src: Path, out_video: Path, crf: int = VIDEO_CRF, max_bitrate_kbps: int | None = None) -> None:
    vf_filter = (
        f"scale='min({MAX_WIDTH},iw)':'min({MAX_HEIGHT},ih)':force_original_aspect_ratio=decrease:flags=lanczos,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
        f"fps='min({TARGET_FPS},source_fps)'"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-vf", vf_filter,
        "-c:v", "libx264", "-crf", str(crf), "-preset", "slow",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
    ]
    if max_bitrate_kbps:
        cmd.extend(["-maxrate", f"{max_bitrate_kbps}k", "-bufsize", f"{max_bitrate_kbps * 2}k"])
    cmd.extend(["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_video)])
    subprocess.run(cmd, check=True)


def remux_faststart(src: Path, out_video: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(out_video),
    ]
    subprocess.run(cmd, check=True)


def convert_image(src: Path, out_image: Path, quality: int = WEBP_QUALITY) -> None:
    scale_filter = f"scale='min({MAX_WIDTH},iw)':-1:force_original_aspect_ratio=decrease:flags=lanczos"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-vf", scale_filter,
        "-c:v", "libwebp", "-quality", str(quality), "-compression_level", "6",
    ]
    if src.suffix.lower() == ".gif":
        cmd.extend(["-loop", "0", "-an"])
    cmd.append(str(out_image))
    subprocess.run(cmd, check=True)


def convert_all() -> list[Path]:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)

    files = [f for f in INPUT_DIR.rglob("*") if f.is_file()]
    videos = [f for f in files if f.suffix.lower() in VIDEO_EXTENSIONS]
    images = [f for f in files if f.suffix.lower() in IMAGE_EXTENSIONS]

    if not videos and not images:
        print(f"В папке '{INPUT_DIR}' нет новых видео или изображений.")
        return []

    produced: list[Path] = []
    for src in videos:
        rel_parent = src.relative_to(INPUT_DIR).parent
        out_dir = CONVERTED_DIR / rel_parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out_video = out_dir / f"{src.stem}.mp4"
        rel_out = out_video.relative_to(CONVERTED_DIR).as_posix()
        if is_up_to_date(src, out_video):
            print(f"[Пропуск] {src.relative_to(INPUT_DIR).as_posix()} (Converted/{rel_out} актуальнее исходника)")
            continue

        clean_existing(out_dir, src.stem)
        orig_size = src.stat().st_size
        print(f"[Видео] {src.relative_to(INPUT_DIR).as_posix()} -> Converted/{rel_out}")

        info = get_video_info(src)
        encode_video(src, out_video, crf=VIDEO_CRF)
        new_size = out_video.stat().st_size

        if new_size > orig_size:
            if is_web_ready(info):
                remux_faststart(src, out_video)
                new_size = out_video.stat().st_size
            else:
                duration = max(info["duration"], 1.0)
                target_kbps = max(int((orig_size * 8 / 1000) / duration * 0.90) - 160, 300)
                encode_video(src, out_video, crf=20, max_bitrate_kbps=target_kbps)
                new_size = out_video.stat().st_size

        print(f"  [OK] {orig_size / (1024 * 1024):.2f} MB -> {new_size / (1024 * 1024):.2f} MB")
        produced.append(out_video)

    for src in images:
        rel_parent = src.relative_to(INPUT_DIR).parent
        out_dir = CONVERTED_DIR / rel_parent
        out_dir.mkdir(parents=True, exist_ok=True)
        out_image = out_dir / f"{src.stem}.webp"
        rel_out = out_image.relative_to(CONVERTED_DIR).as_posix()
        if is_up_to_date(src, out_image):
            print(f"[Пропуск] {src.relative_to(INPUT_DIR).as_posix()} (Converted/{rel_out} актуальнее исходника)")
            continue

        clean_existing(out_dir, src.stem)
        orig_size = src.stat().st_size
        print(f"[Картинка] {src.relative_to(INPUT_DIR).as_posix()} -> Converted/{rel_out}")

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
        print("Установите boto3 для загрузки в R2: pip install boto3")
        return

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )

    for file_path in files:
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", action="store_true", help="Upload newly converted files to Cloudflare R2")
    parser.add_argument("--upload-all", action="store_true", help="Upload all files in videos/ and images/ to R2")
    args = parser.parse_args()

    produced = convert_all()
    if args.upload_all:
        all_files = [
            p for p in [*CONVERTED_DIR.rglob("*.mp4"), *CONVERTED_DIR.rglob("*.webp")]
            if p.is_file()
        ]
        if all_files:
            upload_to_r2(all_files)
    elif args.upload:
        if produced:
            upload_to_r2(produced)
        else:
            print("Нет новых сконвертированных файлов для загрузки в R2.")


if __name__ == "__main__":
    main()
