"""
utils.py
========

Small, dependency-free helper functions shared across the Image Cleaner
modules: logging setup, path helpers, wildcard matching, and hashing.

Kept free of any GUI or business-logic imports so it can be safely used by
every other module without circular-import risk.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Iterable, Optional

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_base_dir() -> Path:
    """Return the application's base directory.

    Works both when run as a plain script (``python app.py``) and when
    frozen into a single executable with PyInstaller, where ``sys.executable``
    points at the bundled ``.exe`` instead of a ``.py`` file.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def ensure_app_directories(base_dir: Path) -> None:
    """Ensure the standard ``reports/``, ``logs/`` and ``backup/`` folders exist."""
    for name in ("reports", "logs", "backup"):
        (base_dir / name).mkdir(parents=True, exist_ok=True)


def get_icon_path() -> Optional[Path]:
    """Resolve ``icon.ico``, working both from source and from a PyInstaller
    ``--onefile`` build.

    A onefile build extracts bundled data files (added via ``--add-data``)
    to a temporary directory at ``sys._MEIPASS`` on each run, which is
    distinct from the ``.exe``'s own directory (:func:`get_base_dir`) — so
    the icon has to be looked up there first when frozen.

    Returns ``None`` if no icon file is found, so callers can skip setting
    one rather than crashing a build that doesn't bundle it.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and meipass:
        bundled = Path(meipass) / "icon.ico"
        if bundled.exists():
            return bundled

    source_tree = get_base_dir() / "icon.ico"
    return source_tree if source_tree.exists() else None


def setup_logging(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    """Configure root logging with a rotating file handler and console output.

    Args:
        log_dir: Directory in which to write the ``image_cleaner.log`` file.
        level: Logging level for both handlers.

    Returns:
        The configured root logger.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "image_cleaner.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Avoid duplicate handlers if setup_logging is called more than once.
    if root_logger.handlers:
        return root_logger

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    root_logger.addHandler(console_handler)

    return root_logger


def matches_any_pattern(name: str, patterns: Iterable[str]) -> bool:
    """Case-insensitive wildcard match of ``name`` against any of ``patterns``.

    Patterns use ``fnmatch`` syntax (``*``, ``?``, ``[seq]``).
    """
    lowered = name.lower()
    for pattern in patterns:
        if fnmatch.fnmatch(lowered, pattern.lower()):
            return True
    return False


def matches_any_path_pattern(relative_posix_path: str, patterns: Iterable[str]) -> bool:
    """Match a POSIX-style relative path (e.g. ``images/01-junk/x.png``)
    against folder-style wildcard patterns such as ``images/01-junk-removal/``.

    Trailing slashes on patterns are ignored, and the match also accepts the
    pattern matching any *path segment*, not just the full path.
    """
    lowered = relative_posix_path.lower()
    for raw_pattern in patterns:
        pattern = raw_pattern.lower().strip().rstrip("/")
        if not pattern:
            continue
        if fnmatch.fnmatch(lowered, pattern):
            return True
        if fnmatch.fnmatch(lowered, pattern + "/*"):
            return True
        # Also allow matching a bare segment pattern (e.g. "logo*") anywhere.
        segments = lowered.split("/")
        if any(fnmatch.fnmatch(seg, pattern) for seg in segments):
            return True
    return False


def compute_sha256(file_path: Path, chunk_size: int = 65536) -> Optional[str]:
    """Compute the SHA-256 hash of a file's contents.

    Returns ``None`` (and logs a warning) if the file cannot be read, rather
    than raising, so callers can continue processing other files.
    """
    logger = logging.getLogger(__name__)
    hasher = hashlib.sha256()
    try:
        with file_path.open("rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()
    except (OSError, PermissionError) as exc:
        logger.warning("Could not hash file %s: %s", file_path, exc)
        return None


def human_readable_size(num_bytes: int) -> str:
    """Format a byte count as a human-readable string (e.g. ``1.4 MB``)."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def is_hidden_folder_name(name: str) -> bool:
    """Return True for dotfiles/dot-folders such as ``.git``."""
    return name.startswith(".")
