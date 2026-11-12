"""
scanner.py
==========

Recursive filesystem scanner for a static HTML website.

Walks the selected website folder, classifies files as *images* or *scannable
website files* (html/css/js/etc.), prunes excluded folders, and collects
metadata (size, modified date, optional SHA-256 hash) for each file found.

Filename/extension/recency-based protection is intentionally handled later by
:mod:`comparison_engine`, not here — see :meth:`WebsiteScanner._classify_file`
for why.

The data classes defined here (:class:`ImageFile`, :class:`WebsiteFile`,
:class:`ScanResult`) are the shared vocabulary used by the reference parser,
comparison engine, and report generator in later phases.

Performance: per-file work (``stat()`` plus, for images, an optional
SHA-256 hash read) is I/O-bound, so it runs on a :class:`ThreadPoolExecutor`
— each file is read exactly once, and this is the only place image bytes are
read at all (reference parsing reads website files, never images). Directory
traversal itself stays single-threaded since ``os.walk`` needs to prune
subdirectories in place as it goes; only the per-file classification, which
dominates runtime on sites with tens of thousands of images, is parallelized.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Union

from config import AppConfig
from utils import compute_sha256, is_hidden_folder_name, matches_any_path_pattern

logger = logging.getLogger(__name__)

# Progress callback signature: (files_scanned, total_estimate, current_path)
ProgressCallback = Callable[[int, int, str], None]


@dataclass
class ScannedFile:
    """Base metadata shared by every file discovered during a scan."""

    absolute_path: Path
    relative_path: str  # POSIX-style, relative to the website root
    size_bytes: int
    modified_time: datetime
    sha256: Optional[str] = None

    @property
    def folder(self) -> str:
        """Return the POSIX-style parent folder of this file, relative to the root."""
        parent = str(Path(self.relative_path).parent).replace("\\", "/")
        return "" if parent == "." else parent

    @property
    def filename(self) -> str:
        return Path(self.relative_path).name


@dataclass
class ImageFile(ScannedFile):
    """An image discovered on disk."""


@dataclass
class WebsiteFile(ScannedFile):
    """A scannable website file (HTML, CSS, JS, JSON, XML, TXT)."""


@dataclass
class ScanStats:
    """Summary counters produced by a single scan run."""

    total_files_seen: int = 0
    images_found: int = 0
    website_files_found: int = 0
    errors: int = 0
    duration_seconds: float = 0.0


@dataclass
class ScanResult:
    """The full output of :meth:`WebsiteScanner.scan`."""

    root_folder: Path
    images: List[ImageFile] = field(default_factory=list)
    website_files: List[WebsiteFile] = field(default_factory=list)
    stats: ScanStats = field(default_factory=ScanStats)


#: Default worker count for per-file scanning. This work is I/O-bound
#: (stat + optional file-content hashing), so it benefits from more threads
#: than there are CPU cores, especially on network-backed storage.
DEFAULT_SCAN_WORKERS = 16


class WebsiteScanner:
    """Recursively scans a static website folder for images and source files."""

    def __init__(
        self,
        root_folder: Path,
        config: AppConfig,
        compute_hashes: bool = False,
        max_workers: int = DEFAULT_SCAN_WORKERS,
    ) -> None:
        """
        Args:
            root_folder: The website's root directory.
            config: Loaded application configuration (exclusion rules, extensions).
            compute_hashes: Whether to compute a SHA-256 hash for every image
                (needed for duplicate detection). Costs extra I/O time.
            max_workers: Thread pool size for per-file classification.
        """
        self.root_folder = Path(root_folder).resolve()
        self.config = config
        self.compute_hashes = compute_hashes
        self.max_workers = max_workers

        self._image_exts = {ext.lower() for ext in config.supported_image_extensions}
        self._scan_exts = {ext.lower() for ext in config.supported_scan_extensions}

    def scan(self, progress_callback: Optional[ProgressCallback] = None) -> ScanResult:
        """Walk the website folder and classify every file found.

        Args:
            progress_callback: Optional callable invoked periodically with
                ``(files_scanned, total_estimate, current_path)`` so a GUI can
                update a progress bar without blocking.

        Returns:
            A populated :class:`ScanResult`.
        """
        if not self.root_folder.is_dir():
            raise FileNotFoundError(f"Website folder does not exist: {self.root_folder}")

        start_time = time.monotonic()
        result = ScanResult(root_folder=self.root_folder)

        all_paths = list(self._walk_files())
        total_estimate = len(all_paths)
        logger.info("Discovered %d candidate files under %s", total_estimate, self.root_folder)

        completed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_path = {
                executor.submit(self._classify_file, path): path for path in all_paths
            }
            for future in as_completed(future_to_path):
                absolute_path = future_to_path[future]
                result.stats.total_files_seen += 1
                completed += 1
                try:
                    classified = future.result()
                except (OSError, PermissionError) as exc:
                    result.stats.errors += 1
                    logger.warning("Error reading file %s: %s", absolute_path, exc)
                    classified = None

                if isinstance(classified, ImageFile):
                    result.images.append(classified)
                    result.stats.images_found += 1
                elif isinstance(classified, WebsiteFile):
                    result.website_files.append(classified)
                    result.stats.website_files_found += 1

                if progress_callback and (completed % 25 == 0 or completed == total_estimate):
                    progress_callback(completed, total_estimate, str(absolute_path))

        result.stats.duration_seconds = time.monotonic() - start_time
        logger.info(
            "Scan complete: %d images, %d website files, %d errors in %.2fs",
            result.stats.images_found,
            result.stats.website_files_found,
            result.stats.errors,
            result.stats.duration_seconds,
        )
        return result

    def _walk_files(self):
        """Yield absolute file paths under the root, pruning excluded directories early."""
        for current_dir, dir_names, file_names in os.walk(self.root_folder):
            current_path = Path(current_dir)
            rel_dir = self._to_relative_posix(current_path)

            # Prune excluded / hidden directories in-place so os.walk skips them.
            pruned = []
            for dir_name in dir_names:
                candidate_rel = f"{rel_dir}/{dir_name}" if rel_dir else dir_name
                if is_hidden_folder_name(dir_name):
                    continue
                if matches_any_path_pattern(candidate_rel, self.config.excluded_folders):
                    continue
                pruned.append(dir_name)
            dir_names[:] = pruned

            for file_name in file_names:
                yield current_path / file_name

    def _classify_file(self, absolute_path: Path) -> Optional[Union[ImageFile, WebsiteFile]]:
        """Classify a single file as an image, a website file, or irrelevant.

        Pure function (no shared-state mutation) so it can run safely on a
        worker thread: it reads only ``self``'s read-only config/extension
        sets, and returns its result for the caller to fold into
        :class:`ScanResult` on the main thread.

        Note: ``excluded_filenames``, ``excluded_extensions`` and
        ``recent_file_days`` are intentionally NOT applied here. Website
        files must always be scanned for references (skipping one could
        make a genuinely-used image look unused), and matching images must
        still be recorded so the Comparison Engine can mark them
        ``Protected`` and surface them in reports rather than making them
        silently disappear. Only ``excluded_folders`` prunes the walk
        (see :meth:`_walk_files`).
        """
        rel_posix = self._to_relative_posix(absolute_path)
        extension = absolute_path.suffix.lower()

        is_image = extension in self._image_exts
        is_website_file = extension in self._scan_exts
        if not is_image and not is_website_file:
            return None  # Irrelevant file type; ignore silently.

        stat_result = absolute_path.stat()
        modified_time = datetime.fromtimestamp(stat_result.st_mtime)

        if is_image:
            sha256 = compute_sha256(absolute_path) if self.compute_hashes else None
            return ImageFile(
                absolute_path=absolute_path,
                relative_path=rel_posix,
                size_bytes=stat_result.st_size,
                modified_time=modified_time,
                sha256=sha256,
            )

        return WebsiteFile(
            absolute_path=absolute_path,
            relative_path=rel_posix,
            size_bytes=stat_result.st_size,
            modified_time=modified_time,
        )

    def _to_relative_posix(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root_folder).as_posix()
        except ValueError:
            return path.as_posix()
