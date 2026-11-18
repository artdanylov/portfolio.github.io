"""
One-click site release script:
1. Converts & uploads new media to Cloudflare R2 (scripts/media_sync.py --upload)
2. Verifies production Astro build (npm.cmd run build)
3. Commits and pushes changes to GitHub (triggers GitHub Actions deploy)
"""

import glob
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def find_git() -> str | None:
    git_bin = shutil.which("git")
    if git_bin:
        return git_bin
    candidates = [
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        *glob.glob(str(Path.home() / r"AppData\Local\GitHubDesktop\app-*\resources\app\git\cmd\git.exe")),
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=BASE_DIR, check=check)


def main() -> None:
    print("=== 1/3 Converting and uploading new media to Cloudflare R2 ===")
    run([sys.executable, "scripts/media_sync.py", "--upload"])

    print("\n=== 2/3 Verifying Astro production build ===")
    run(["npm.cmd", "run", "build"])

    print("\n=== 3/3 Publishing to Git ===")
    git_bin = find_git()
    if not git_bin:
        print("Git CLI not found on PATH. Open GitHub Desktop to Commit + Push manually.")
        return

    status = subprocess.run([git_bin, "status", "--short"], cwd=BASE_DIR, capture_output=True, text=True)
    if not status.stdout.strip():
        print("No changes to commit.")
        return

    print(status.stdout)
    msg = input("Commit message (Enter = auto): ").strip()
    if not msg:
        msg = f"Update site ({datetime.now().strftime('%Y-%m-%d %H:%M')})"

    run([git_bin, "add", "-A"])
    run([git_bin, "commit", "-m", msg])
    run([git_bin, "push"])
    print("\nDone! GitHub Actions will automatically build and deploy the site.")


if __name__ == "__main__":
    main()
