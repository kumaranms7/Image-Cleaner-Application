"""
liquidweb_importer.py
======================

Imports a Liquid Web "orphan images" report (a plain-text list of image
paths Liquid Web's own tooling considers unused) and cross-checks it against
this application's own Referenced/Unused/Protected verdicts.

External orphan-detection tools often work from access logs or a simple
filesystem/database diff, which can miss references expressed through CSS
``url()``, JSON, or JavaScript — reference types this app's parser does
cover (see :mod:`reference_parser`). Surfacing disagreements protects
against blindly trusting an external report that recommends removing an
image this app has real evidence is still in use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from comparison_engine import ComparisonReport, ImageStatus
from normalizer import normalize_reference, normalize_scanned_path
from report_generator import LiquidWebComparisonRow

logger = logging.getLogger(__name__)

_COMMENT_PREFIXES = ("#", "//", ";")


@dataclass
class LiquidWebEntry:
    """One raw path parsed out of the Liquid Web orphan report."""

    raw_line: str
    line_number: int
    normalized_path: Optional[str]


class LiquidWebImporter:
    """Parses a Liquid Web orphan-images report and compares it to this app's findings."""

    def parse_report(self, report_path: Path) -> List[LiquidWebEntry]:
        """Parse a Liquid Web orphan report into normalized entries.

        The exact export format isn't standardized, so this parser is
        intentionally permissive: one path per line; blank lines and
        ``#``/``//``/``;`` comment lines are skipped; surrounding
        quotes/whitespace are stripped; and if a line has trailing
        comma-or-tab-separated metadata (e.g. a file-size column), only the
        first field is treated as the path.
        """
        report_path = Path(report_path)
        try:
            text = report_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.error("Could not read Liquid Web report %s: %s", report_path, exc)
            raise

        entries: List[LiquidWebEntry] = []
        for line_number, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith(_COMMENT_PREFIXES):
                continue

            path_token = self._extract_path_token(line)
            if not path_token:
                continue

            normalized = normalize_reference(path_token)
            entries.append(
                LiquidWebEntry(
                    raw_line=path_token,
                    line_number=line_number,
                    normalized_path=normalized.normalized_key if normalized.is_comparable else None,
                )
            )

        logger.info("Parsed %d image path(s) from Liquid Web report %s", len(entries), report_path)
        return entries

    @staticmethod
    def _extract_path_token(line: str) -> str:
        """Pull the path-like portion out of a single report line."""
        stripped = line.strip().strip('"').strip("'")
        first_field = stripped.split(",")[0].strip()
        return first_field.split("\t")[0].strip()

    def compare(
        self, entries: List[LiquidWebEntry], comparison_report: ComparisonReport
    ) -> List[LiquidWebComparisonRow]:
        """Cross-check each parsed Liquid Web entry against this app's verdicts.

        ``agreement`` is only ``True`` when this app independently confirms
        the image has no real references (``Unused`` or ``Protected``).
        An image this app couldn't find in the current scan at all is left
        as a disagreement/uncertain case rather than assumed to agree —
        that gap could just as easily be a path-normalization mismatch as a
        genuinely already-removed file, and asserting confidence we don't
        have would work against this app's safety-first design.

        Args:
            entries: Output of :meth:`parse_report`.
            comparison_report: This app's own scan + comparison results.

        Returns:
            One :class:`LiquidWebComparisonRow` per parsed entry.
        """
        status_by_key: Dict[str, ImageStatus] = {
            normalize_scanned_path(r.image.relative_path): r.status for r in comparison_report.results
        }

        rows: List[LiquidWebComparisonRow] = []
        for entry in entries:
            if entry.normalized_path is None:
                rows.append(
                    LiquidWebComparisonRow(
                        image_path=entry.raw_line,
                        liquidweb_says_unused=True,
                        app_says_referenced=False,
                        agreement=False,
                        note=f"Line {entry.line_number}: could not parse a usable image path.",
                    )
                )
                continue

            status = status_by_key.get(entry.normalized_path)
            if status is None:
                rows.append(
                    LiquidWebComparisonRow(
                        image_path=entry.normalized_path,
                        liquidweb_says_unused=True,
                        app_says_referenced=False,
                        agreement=False,
                        note="Not found in the current website scan - verify manually "
                        "(may already be removed, or the path may not match).",
                    )
                )
                continue

            app_says_referenced = status is ImageStatus.REFERENCED
            if app_says_referenced:
                note = "Disagreement: this app found real references to this image. Do not remove."
            elif status is ImageStatus.PROTECTED:
                note = "Agree it's unreferenced, but this app protects it (matches a protection rule)."
            else:
                note = "Both Liquid Web and this app agree this image is unused."

            rows.append(
                LiquidWebComparisonRow(
                    image_path=entry.normalized_path,
                    liquidweb_says_unused=True,
                    app_says_referenced=app_says_referenced,
                    agreement=not app_says_referenced,
                    note=note,
                )
            )

        disagreements = sum(1 for r in rows if not r.agreement)
        logger.info(
            "Liquid Web comparison complete: %d entries, %d disagreement(s)/uncertain.",
            len(rows),
            disagreements,
        )
        return rows
