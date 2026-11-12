"""
duplicate_detector.py
======================

Finds exact duplicate images — the same file content saved under a
different filename and/or in a different folder — via SHA-256 hash
comparison. Optional: only runs against images that were hashed during the
scan (see ``AppConfig.detect_duplicates``, which also controls whether
:class:`scanner.WebsiteScanner` computes hashes in the first place).

This is deliberately additive rather than integrated into
:mod:`comparison_engine`'s Referenced/Unused/Protected classification — an
image can be simultaneously "Referenced" (safe, per the comparison engine)
and "a duplicate of another file" (a separate, informational finding). The
two reports are meant to be read together, not merged into one status.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from comparison_engine import ComparisonReport, ImageStatus
from report_generator import DuplicateGroupRow
from scanner import ImageFile, ScanResult
from utils import human_readable_size

logger = logging.getLogger(__name__)


@dataclass
class DuplicateGroup:
    """A set of images that are byte-for-byte identical (same SHA-256 hash)."""

    sha256: str
    images: List[ImageFile]
    primary: ImageFile
    file_size: int

    @property
    def duplicate_count(self) -> int:
        """Number of images in this group beyond the primary (kept) copy."""
        return len(self.images) - 1

    @property
    def wasted_bytes(self) -> int:
        """Disk space these duplicates take up beyond the one copy that's needed."""
        return self.file_size * self.duplicate_count


class DuplicateDetector:
    """Groups scanned images that share identical content."""

    def find_duplicates(
        self, scan_result: ScanResult, comparison_report: Optional[ComparisonReport] = None
    ) -> List[DuplicateGroup]:
        """Group images by identical SHA-256 hash.

        Args:
            scan_result: Must have been produced with ``compute_hashes=True``
                for any duplicates to be found — images without a hash are
                skipped and logged.
            comparison_report: Optional. When provided, a ``Referenced``
                image is preferred as the group's "primary" (the copy to
                keep), since it's already known to be safe; without it, the
                shallowest/shortest path is used as a reasonable default.

        Returns:
            One :class:`DuplicateGroup` per hash shared by 2+ images, sorted
            by wasted space (largest first) so the biggest wins surface first.
        """
        by_hash: Dict[str, List[ImageFile]] = {}
        skipped_unhashed = 0
        for image in scan_result.images:
            if not image.sha256:
                skipped_unhashed += 1
                continue
            by_hash.setdefault(image.sha256, []).append(image)

        if skipped_unhashed:
            logger.info(
                "%d image(s) had no SHA-256 hash and were skipped for duplicate "
                "detection (enable 'detect_duplicates' before scanning to hash images).",
                skipped_unhashed,
            )

        referenced_paths = self._referenced_paths(comparison_report)

        groups: List[DuplicateGroup] = []
        for sha256, images in by_hash.items():
            if len(images) < 2:
                continue
            primary = self._choose_primary(images, referenced_paths)
            groups.append(
                DuplicateGroup(
                    sha256=sha256, images=images, primary=primary, file_size=images[0].size_bytes
                )
            )

        groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
        logger.info(
            "Duplicate detection complete: %d duplicate group(s) covering %d image(s), "
            "%s potentially reclaimable.",
            len(groups),
            sum(len(g.images) for g in groups),
            human_readable_size(sum(g.wasted_bytes for g in groups)),
        )
        return groups

    @staticmethod
    def _referenced_paths(comparison_report: Optional[ComparisonReport]) -> Set[str]:
        if not comparison_report:
            return set()
        return {
            r.image.relative_path
            for r in comparison_report.results
            if r.status is ImageStatus.REFERENCED
        }

    @staticmethod
    def _choose_primary(images: List[ImageFile], referenced_paths: Set[str]) -> ImageFile:
        """Pick the copy to keep: prefer a referenced one, then the
        shallowest/shortest path as a stable, predictable tiebreaker."""
        referenced = [img for img in images if img.relative_path in referenced_paths]
        candidates = referenced if referenced else images
        return sorted(
            candidates,
            key=lambda img: (img.relative_path.count("/"), len(img.relative_path), img.relative_path),
        )[0]


def build_report_rows(groups: List[DuplicateGroup]) -> List[DuplicateGroupRow]:
    """Flatten duplicate groups into the row shape :mod:`report_generator` expects."""
    rows: List[DuplicateGroupRow] = []
    for group in groups:
        for image in sorted(group.images, key=lambda img: img.relative_path):
            is_primary = image.relative_path == group.primary.relative_path
            rows.append(
                DuplicateGroupRow(
                    sha256=group.sha256,
                    image_path=image.relative_path,
                    file_size=image.size_bytes,
                    is_primary=is_primary,
                    duplicate_of=None if is_primary else group.primary.relative_path,
                )
            )
    return rows
