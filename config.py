"""
config.py
=========

Configuration management for Image Cleaner.

Responsible for loading, validating, saving, and providing safe defaults for
``config.json``. All other modules should obtain settings through an
:class:`AppConfig` instance rather than reading the JSON file directly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "config.json"


def _default_excluded_folders() -> List[str]:
    return [
        ".git",
        ".svn",
        "node_modules",
        "__pycache__",
        "backup",
        "logs",
        "reports",
    ]


def _default_excluded_extensions() -> List[str]:
    return []


def _default_excluded_filenames() -> List[str]:
    return ["Thumbs.db", ".DS_Store"]


def _default_supported_image_extensions() -> List[str]:
    return [
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".svg",
        ".avif",
        ".ico",
        ".bmp",
        ".tif",
        ".tiff",
    ]


def _default_supported_scan_extensions() -> List[str]:
    return [".html", ".htm", ".css", ".js", ".json", ".xml", ".txt"]


@dataclass
class AppConfig:
    """Typed representation of ``config.json``.

    All list-based settings support simple wildcard patterns (``*``, ``?``)
    which are matched using :func:`fnmatch.fnmatch`.
    """

    excluded_folders: List[str] = field(default_factory=_default_excluded_folders)
    excluded_extensions: List[str] = field(default_factory=_default_excluded_extensions)
    excluded_filenames: List[str] = field(default_factory=_default_excluded_filenames)
    recent_file_days: int = 0
    backup_enabled: bool = True
    detect_duplicates: bool = True
    detect_missing_images: bool = True
    detect_broken_references: bool = True
    supported_image_extensions: List[str] = field(
        default_factory=_default_supported_image_extensions
    )
    supported_scan_extensions: List[str] = field(
        default_factory=_default_supported_scan_extensions
    )
    theme: str = "light"  # GUI appearance: "light" or "dark"

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain ``dict`` suitable for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        """Build an :class:`AppConfig`, ignoring unknown keys and filling
        in defaults for any missing ones."""
        defaults = cls()
        known_fields = {f for f in defaults.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        merged = {**defaults.to_dict(), **filtered}
        return cls(**merged)


class ConfigManager:
    """Loads and persists :class:`AppConfig` to a JSON file on disk."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)

    def load(self) -> AppConfig:
        """Load configuration from disk, creating a default file if needed.

        Returns:
            AppConfig: The loaded (or newly created default) configuration.
        """
        if not self.config_path.exists():
            logger.info("No config file found at %s, creating default.", self.config_path)
            default = AppConfig()
            self.save(default)
            return default

        try:
            with self.config_path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            return AppConfig.from_dict(raw)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to read config file (%s). Using defaults.", exc)
            return AppConfig()

    def save(self, config: AppConfig) -> None:
        """Persist the given configuration to disk as pretty-printed JSON."""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with self.config_path.open("w", encoding="utf-8") as fh:
                json.dump(config.to_dict(), fh, indent=4, sort_keys=True)
        except OSError as exc:
            logger.error("Failed to save config file (%s): %s", self.config_path, exc)
            raise
