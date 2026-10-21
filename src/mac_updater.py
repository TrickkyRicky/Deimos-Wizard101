"""In-app update for the macOS (Wine) zip build.

Looks for Deimos-mac-<version>-<build>.zip assets on the GitHub releases tagged mac-*,
downloads and verifies the newest one, then hands off to tools/apply_update.sh, which
overwrites the install once Deimos has exited and starts it again.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

import requests
from loguru import logger

from src.updater import ReleaseInfo

REPO_AUTHOR = "TrickkyRicky"
REPO_NAME = "Deimos-Wizard101"
TAG_PREFIX = "mac-"

# root/repos/Deimos-Wizard101/src/mac_updater.py
INSTALL_ROOT = Path(__file__).resolve().parents[3]

_ASSET_RE = re.compile(r"^Deimos-mac-(\d+(?:\.\d+)*)-(\d+)\.zip$")
_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)-(\d+)$")

# Wine maps Z:\ to the Mac root, so this is /tmp for both sides and has no spaces
_HELPER_COPY = Path("Z:/tmp/deimos_apply_update.sh")


def _update_dir(root: Path = INSTALL_ROOT) -> Path:
    d = root / ".update"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_version(root: Path = INSTALL_ROOT) -> Optional[str]:
    """Version written into the zip by the build (also present in a dev tree after a build)."""
    try:
        v = (root / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return v if _VERSION_RE.match(v) else None


def current_version(root: Path = INSTALL_ROOT) -> Optional[str]:
    """Build version for a packaged install, or None for a git checkout."""
    if (root / "repos" / REPO_NAME / ".git").exists():
        return None
    return build_version(root)


def is_supported() -> bool:
    return current_version() is not None


def _key(version: str) -> tuple[tuple[int, ...], int]:
    m = _VERSION_RE.match(version)
    return tuple(int(p) for p in m.group(1).split(".")), int(m.group(2))


def is_newer(remote: str, local: str) -> bool:
    try:
        return _key(remote) > _key(local)
    except Exception:
        return False


def get_latest_release(timeout: float = 10.0) -> Optional[ReleaseInfo]:
    """Newest zip across all mac-* releases (pre-releases included), or None on failure."""
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{REPO_AUTHOR}/{REPO_NAME}/releases",
            params={"per_page": 30},
            headers={"Accept": "application/vnd.github+json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        releases = resp.json()
    except Exception as e:
        logger.debug(f"Mac update check failed: {e}")
        return None

    best = None
    for rel in releases:
        if rel.get("draft") or not (rel.get("tag_name") or "").startswith(TAG_PREFIX):
            continue
        for asset in rel.get("assets", []):
            m = _ASSET_RE.match(asset.get("name", ""))
            if not m:
                continue
            version = f"{m.group(1)}-{m.group(2)}"
            if best is None or _key(version) > _key(best[0]):
                best = (version, asset, rel)

    if best is None:
        return None
    version, asset, rel = best
    digest = asset.get("digest") or ""
    return ReleaseInfo(
        version=version,
        exe_url=asset.get("browser_download_url"),
        sha256=digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None,
        notes_url=rel.get("html_url", f"https://github.com/{REPO_AUTHOR}/{REPO_NAME}/releases"),
        prerelease=bool(rel.get("prerelease")),
    )


def download_update(
    url: str,
    expected_sha256: Optional[str],
    *,
    progress_cb: Optional[Callable[[int], None]] = None,
    timeout: float = 30.0,
) -> Optional[Path]:
    dest = _update_dir() / "update.zip"
    hasher = hashlib.sha256()
    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))
            downloaded = 0
            last_pct = -1
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total > 0:
                        pct = int(downloaded * 100 / total)
                        if pct != last_pct:
                            last_pct = pct
                            progress_cb(pct)
    except Exception as e:
        logger.error(f"Mac update download failed: {e}")
        dest.unlink(missing_ok=True)
        return None

    if expected_sha256:
        actual = hasher.hexdigest().lower()
        if actual != expected_sha256.lower():
            logger.error(f"Mac update checksum mismatch (expected {expected_sha256}, got {actual}); discarding.")
            dest.unlink(missing_ok=True)
            return None
        logger.debug("Mac update checksum verified.")
    else:
        logger.warning("No checksum published for this release; skipping verification.")
    return dest


def _to_unix(path: Path) -> Optional[str]:
    try:
        r = subprocess.run(["winepath", "-u", str(path)], capture_output=True, text=True, timeout=20)
        out = r.stdout.strip()
        return out if r.returncode == 0 and out else None
    except Exception as e:
        logger.error(f"winepath failed for {path}: {e}")
        return None


def apply_and_relaunch(zip_path: Path, root: Path = INSTALL_ROOT) -> bool:
    """Start the Mac-side helper; the caller must exit right after this returns True."""
    unix_root = _to_unix(root)
    unix_zip = _to_unix(zip_path)
    if not unix_root or not unix_zip:
        return False
    try:
        # Copied out of the install, since the helper overwrites tools/ while it runs
        shutil.copyfile(root / "tools" / "apply_update.sh", _HELPER_COPY)
        env = dict(os.environ, DEIMOS_ROOT=unix_root, DEIMOS_ZIP=unix_zip)
        subprocess.Popen(
            ["cmd", "/c", "start", "/unix", "/bin/bash", "/tmp/deimos_apply_update.sh"],
            env=env,
        )
    except Exception as e:
        logger.error(f"Could not start the Mac update helper: {e}")
        return False
    return True
