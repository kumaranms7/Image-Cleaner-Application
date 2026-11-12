"""
app.py
======

Entry point for Image Cleaner.

Run with::

    python app.py

Also compatible with ``pyinstaller --onefile app.py`` without modification,
since all paths are resolved relative to :func:`utils.get_base_dir`, which
accounts for PyInstaller's frozen executable layout.
"""

from __future__ import annotations

import logging

from config import CONFIG_FILENAME, ConfigManager
from gui import ImageCleanerApp
from utils import ensure_app_directories, get_base_dir, setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    """Initialize directories, logging, and configuration, then launch the GUI."""
    base_dir = get_base_dir()
    ensure_app_directories(base_dir)
    setup_logging(base_dir / "logs")

    logger.info("Image Cleaner starting up. Base directory: %s", base_dir)

    config_manager = ConfigManager(base_dir / CONFIG_FILENAME)
    app = ImageCleanerApp(config_manager)

    try:
        app.mainloop()
    finally:
        logger.info("Image Cleaner shutting down.")


if __name__ == "__main__":
    main()
