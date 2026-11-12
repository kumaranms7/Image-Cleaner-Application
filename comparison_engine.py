"""
comparison_engine.py
=====================

Compares the images found on disk (:mod:`scanner`) against the image
references extracted from the website's markup/CSS/JS (:mod:`reference_parser`,
:mod:`normalizer`) and classifies every image as **Referenced**, **Unused**,
or **Protected** — plus a separate list of **Missing** (broken) references
whose target image does not exist on disk at all.

Protection rules
----------------

``excluded_filenames``, ``excluded_extensions`` and ``recent_file_days`` are
applied *here*, not in the scanner. An image matching one of these rules is
never recommended for removal, but unlike a scan-time exclusion it still
shows up in every report with a clear reason — visibility matters more than
tidiness when the cost of a wrong guess is a missing image on a live site.

Confidence scores
------------------

Every result carries a ``confidence`` float in ``[0.0, 1.0]`` representing
how sure the engine is that its classification is safe to act on. Confidence
is deliberately conservative: ambiguous evidence lowers confidence rather
than picking a side, since the whole application is built around "safety
over speed."
"""

from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Iterable, List, Optional

from config import AppConfig
from normalizer import normalize_reference, normalize_scanned_path
from reference_parser import RawReference
from scanner import ImageFile, ScanResult
from utils import matches_any_pattern

logger = logging.getLogger(__name__)


class ImageStatus(str, Enum):
    """The classification assigned to a scanned image."""

    REFERENCED = "Referenced"
    UNUSED = "Unused"
    PROTECTED = "Protected"


@dataclass
class ReferenceInfo:
    """One concrete place an image (or missing image) is referenced from."""

    source_file: str
    reference_type: str
    line_number: Optional[int]
    raw_value: str
    is_external: bool


@dataclass
class ImageComparisonResult:
    """The comparison outcome for a single image found on disk."""

    image: ImageFile
    status: ImageStatus
    reference_count: int
    references: List[ReferenceInfo]
    confidence: float
    reason: str


@dataclass
class MissingImageResult:
    """A reference that points at an image which does not exist on disk."""

    normalized_path: str
    references: List[ReferenceInfo]
    confidence: float
    reason: str


@dataclass
class ComparisonSummary:
    """Aggregate counts for a comparison run."""

    total_images: int = 0
    referenced_count: int = 0
    unused_count: int = 0
    protected_count: int = 0
    missing_count: int = 0


@dataclass
class ComparisonReport:
    """The full output of :meth:`ComparisonEngine.compare`."""

    results: List[ImageComparisonResult] = field(default_factory=list)
    missing: List[MissingImageResult] = field(default_factory=list)
    summary: ComparisonSummary = field(default_factory=ComparisonSummary)


class ComparisonEngine:
    """Classifies scanned images as referenced, unused, or protected."""

    def __init__(self, config: AppConfig, site_domains: Optional[Iterable[str]] = None) -> None:
        """
        Args:
            config: Application configuration (protection rules, feature toggles).
            site_domains: Known hostnames for this website, used to decide
                whether an absolute URL reference is internal or external.
                See :func:`normalizer.normalize_reference`.
        """
        self.config = config
        self.site_domains = list(site_domains or [])

    def compare(self, scan_result: ScanResult, raw_references: List[RawReference]) -> ComparisonReport:
        """Compare scanned images against parsed references.

        Args:
            scan_result: Output of :meth:`scanner.WebsiteScanner.scan`.
            raw_references: Output of :meth:`reference_parser.ReferenceParser.parse_all`.

        Returns:
            A populated :class:`ComparisonReport`.
        """
        image_index = self._build_image_index(scan_result.images)
        reference_index = self._build_reference_index(raw_references)

        report = ComparisonReport()

        for key, images_at_key in image_index.items():
            refs = reference_index.get(key, [])
            for image in images_at_key:
                result = self._classify_image(image, refs, len(images_at_key) > 1)
                report.results.append(result)

        if self.config.detect_missing_images or self.config.detect_broken_references:
            for key, refs in reference_index.items():
                if key not in image_index and refs:
                    report.missing.append(self._build_missing_result(key, refs))

        report.summary = self._summarize(report)
        logger.info(
            "Comparison complete: %d referenced, %d unused, %d protected, %d missing.",
            report.summary.referenced_count,
            report.summary.unused_count,
            report.summary.protected_count,
            report.summary.missing_count,
        )
        return report

    # ------------------------------------------------------------------ #
    # Index construction
    # ------------------------------------------------------------------ #
    def _build_image_index(self, images: List[ImageFile]) -> Dict[str, List[ImageFile]]:
        index: Dict[str, List[ImageFile]] = {}
        for image in images:
            key = normalize_scanned_path(image.relative_path)
            index.setdefault(key, []).append(image)
        return index

    def _build_reference_index(self, raw_references: List[RawReference]) -> Dict[str, List[ReferenceInfo]]:
        index: Dict[str, List[ReferenceInfo]] = {}
        for raw_ref in raw_references:
            source_dir = posixpath.dirname(raw_ref.source_file)
            normalized = normalize_reference(
                raw_ref.raw_value, source_dir=source_dir, site_domains=self.site_domains
            )
            if not normalized.is_comparable:
                continue
            info = ReferenceInfo(
                source_file=raw_ref.source_file,
                reference_type=raw_ref.reference_type,
                line_number=raw_ref.line_number,
                raw_value=raw_ref.raw_value,
                is_external=normalized.is_external,
            )
            index.setdefault(normalized.normalized_key, []).append(info)
        return index

    # ------------------------------------------------------------------ #
    # Classification
    # ------------------------------------------------------------------ #
    def _classify_image(
        self, image: ImageFile, refs: List[ReferenceInfo], key_is_ambiguous: bool
    ) -> ImageComparisonResult:
        protection_reason = self._protection_reason(image)

        if protection_reason:
            return ImageComparisonResult(
                image=image,
                status=ImageStatus.PROTECTED,
                reference_count=len(refs),
                references=refs,
                confidence=1.0,
                reason=protection_reason,
            )

        if refs:
            confidence = self._referenced_confidence(refs, key_is_ambiguous)
            distinct_files = len({r.source_file for r in refs})
            return ImageComparisonResult(
                image=image,
                status=ImageStatus.REFERENCED,
                reference_count=len(refs),
                references=refs,
                confidence=confidence,
                reason=f"Referenced from {distinct_files} file(s).",
            )

        confidence = 0.85 if not key_is_ambiguous else 0.6
        reason = "No references found anywhere in the scanned website files."
        if key_is_ambiguous:
            reason += " Multiple scanned images share this normalized path; verify manually."

        return ImageComparisonResult(
            image=image,
            status=ImageStatus.UNUSED,
            reference_count=0,
            references=[],
            confidence=confidence,
            reason=reason,
        )

    def _protection_reason(self, image: ImageFile) -> Optional[str]:
        """Return a human-readable reason if this image should never be
        recommended for removal, or ``None`` if no protection rule applies."""
        if matches_any_pattern(image.filename, self.config.excluded_filenames):
            return f"Matches excluded filename pattern (e.g. '{image.filename}')."

        extension = "." + image.filename.rsplit(".", 1)[-1].lower() if "." in image.filename else ""
        if extension in {ext.lower() for ext in self.config.excluded_extensions}:
            return f"Matches excluded extension ('{extension}')."

        if self.config.recent_file_days and self.config.recent_file_days > 0:
            cutoff = datetime.now() - timedelta(days=self.config.recent_file_days)
            if image.modified_time >= cutoff:
                return (
                    f"Modified within the last {self.config.recent_file_days} day(s) "
                    f"({image.modified_time:%Y-%m-%d}); protected as a recent file."
                )

        return None

    @staticmethod
    def _referenced_confidence(refs: List[ReferenceInfo], key_is_ambiguous: bool) -> float:
        distinct_files = len({r.source_file for r in refs})
        all_external = all(r.is_external for r in refs)

        confidence = 0.9 if not all_external else 0.6
        confidence += min(distinct_files - 1, 3) * 0.03
        if key_is_ambiguous:
            confidence -= 0.15

        return round(max(0.0, min(confidence, 1.0)), 2)

    def _build_missing_result(self, key: str, refs: List[ReferenceInfo]) -> MissingImageResult:
        distinct_files = len({r.source_file for r in refs})
        confidence = round(min(0.6 + min(distinct_files - 1, 4) * 0.08, 1.0), 2)
        return MissingImageResult(
            normalized_path=key,
            references=refs,
            confidence=confidence,
            reason=f"Referenced from {distinct_files} file(s) but no matching image was found on disk.",
        )

    @staticmethod
    def _summarize(report: ComparisonReport) -> ComparisonSummary:
        summary = ComparisonSummary(total_images=len(report.results))
        for result in report.results:
            if result.status is ImageStatus.REFERENCED:
                summary.referenced_count += 1
            elif result.status is ImageStatus.UNUSED:
                summary.unused_count += 1
            elif result.status is ImageStatus.PROTECTED:
                summary.protected_count += 1
        summary.missing_count = len(report.missing)
        return summary
