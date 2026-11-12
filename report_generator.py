"""
report_generator.py
====================

Writes the reports produced by a scan + comparison run:

- ``ReferencedImages.csv``
- ``UnusedImages.csv``
- ``ProtectedImages.csv``
- ``BrokenImages.csv``
- ``SummaryReport.csv``
- ``SummaryReport.html`` — an HTML dashboard styled with real Tailwind CSS
  (loaded from the CDN — this is the one report file that requires an
  internet connection to render styled; everything else in this app,
  including this file's own data, works fully offline) with summary cards,
  a donut chart, clickable links to the CSV reports, and client-side
  search/filter/pagination over the image and broken-reference tables.

and, once their owning phases exist, ``DuplicateImages.csv`` (Phase 7) and
``LiquidWebComparison.csv`` (Phase 8) — both are already wired into
:meth:`ReportGenerator.generate_all` behind optional parameters so no
rework is needed when that data becomes available.

Every per-image CSV report shares the same column layout (``Image``,
``Path``, ``Folder``, ``File Size``, ``Modified Date``, ``Reference Count``,
``Recommendation``, ``Reason``); ``BrokenImages.csv`` uses its own
``Page`` / ``Missing Image`` / ``Reference Type`` layout since a broken
reference has no corresponding file on disk to describe.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from string import Template
from typing import Dict, Iterable, List, Optional

from comparison_engine import (
    ComparisonReport,
    ComparisonSummary,
    ImageComparisonResult,
    ImageStatus,
    MissingImageResult,
)
from scanner import ScanResult
from utils import human_readable_size

logger = logging.getLogger(__name__)

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_IMAGE_REPORT_FIELDNAMES = [
    "Image",
    "Path",
    "Folder",
    "File Size",
    "Modified Date",
    "Reference Count",
    "Recommendation",
    "Reason",
]

_BROKEN_REPORT_FIELDNAMES = ["Page", "Missing Image", "Reference Type"]

_SUMMARY_FIELDNAMES = ["Metric", "Value"]

_RECOMMENDATION_BY_STATUS = {
    ImageStatus.REFERENCED: "Keep",
    ImageStatus.UNUSED: "Move to Backup",
    ImageStatus.PROTECTED: "Keep (Protected)",
}

# Precomposed Tailwind classes for each status, used directly in generated
# markup. Kept as short semantic names (badge-*, stat-card-*) rather than
# writing out the full Tailwind utility list per usage — with 50,000+ image
# sites being this app's target scale, that would multiply the size of
# every single generated row and meaningfully slow report generation. The
# `@apply` block in _TAILWIND_COMPONENT_CSS composes each of these once;
# it's still genuine Tailwind (composing utilities), just not repeated
# inline thousands of times.
_STATUS_BADGE_CLASS = {
    ImageStatus.REFERENCED: "badge-referenced",
    ImageStatus.UNUSED: "badge-unused",
    ImageStatus.PROTECTED: "badge-protected",
}

# Hex values match Tailwind's default palette exactly, so the one element
# that genuinely can't be a static utility class — the donut chart's
# computed multi-stop conic-gradient — still looks identical to the
# Tailwind-classed badges/cards next to it.
_STATUS_COLOR = {
    ImageStatus.REFERENCED: "#10b981",  # emerald-500
    ImageStatus.UNUSED: "#ef4444",  # red-500
    ImageStatus.PROTECTED: "#3b82f6",  # blue-500
}


# --------------------------------------------------------------------------- #
# HTML dashboard: real Tailwind CSS via the CDN Play script (per explicit
# user request — this is the one place in the app that requires a network
# connection to render styled; the underlying report data is still generated
# entirely offline, and the page degrades to unstyled-but-readable HTML
# without internet rather than failing outright).
# --------------------------------------------------------------------------- #

_TAILWIND_CONFIG_JS = """
tailwind.config = {
  darkMode: 'media',
  theme: {
    extend: {
      colors: { brand: { DEFAULT: '#4f46e5', hover: '#4338ca' } }
    }
  }
};
"""

# Composed once via Tailwind's @apply (supported by the Play CDN inside a
# <style type="text/tailwindcss"> block) so per-row generated markup can use
# short semantic class names instead of repeating full utility lists.
_TAILWIND_COMPONENT_CSS = """
@layer components {
  .stat-card { @apply rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-sm p-4; }
  .stat-card-total { @apply stat-card border-t-4 border-t-indigo-600; }
  .stat-card-referenced { @apply stat-card border-t-4 border-t-emerald-500; }
  .stat-card-unused { @apply stat-card border-t-4 border-t-red-500; }
  .stat-card-protected { @apply stat-card border-t-4 border-t-blue-500; }
  .stat-card-missing { @apply stat-card border-t-4 border-t-amber-500; }
  .stat-card-savings { @apply stat-card border-t-4 border-t-fuchsia-500; }
  .stat-value { @apply text-2xl font-bold text-slate-900 dark:text-white; }
  .stat-label { @apply mt-1 text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400; }

  .panel { @apply rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-sm p-6 mb-6; }
  .panel-title { @apply text-base font-semibold text-slate-900 dark:text-white mb-4; }

  .legend-dot-referenced { @apply h-3 w-3 rounded-full bg-emerald-500 shrink-0; }
  .legend-dot-unused { @apply h-3 w-3 rounded-full bg-red-500 shrink-0; }
  .legend-dot-protected { @apply h-3 w-3 rounded-full bg-blue-500 shrink-0; }

  .report-link { @apply inline-flex items-center gap-2 rounded-full bg-indigo-600 hover:bg-indigo-700 dark:bg-indigo-500 dark:hover:bg-indigo-400 text-white text-sm font-semibold px-4 py-2 transition-colors shadow-sm; }

  .toolbar-input { @apply flex-1 min-w-[220px] rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 px-3 py-2 text-sm text-slate-900 dark:text-white placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500; }
  .toolbar-select { @apply rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 px-2 py-1.5 text-sm text-slate-900 dark:text-white; }
  .filter-btn { @apply rounded-lg border px-3 py-2 text-sm font-medium transition-colors; }
  .filter-btn-active { @apply filter-btn bg-indigo-600 text-white border-indigo-600; }
  .filter-btn-inactive { @apply filter-btn bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 border-slate-300 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-700; }

  .data-table { @apply min-w-full text-sm; }
  .data-table thead { @apply bg-slate-50 dark:bg-slate-800/60; }
  .data-table th { @apply px-3 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400 whitespace-nowrap; }
  .data-table td { @apply px-3 py-2.5 whitespace-nowrap text-slate-700 dark:text-slate-300; }
  .data-table tbody tr { @apply border-t border-slate-100 dark:border-slate-800; }
  .data-table tbody tr:hover { @apply bg-slate-50 dark:bg-slate-800/40; }

  .badge { @apply inline-flex items-center rounded-full px-2.5 py-1 text-xs font-semibold; }
  .badge-referenced { @apply badge bg-emerald-100 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-400; }
  .badge-unused { @apply badge bg-red-100 text-red-700 dark:bg-red-500/15 dark:text-red-400; }
  .badge-protected { @apply badge bg-blue-100 text-blue-700 dark:bg-blue-500/15 dark:text-blue-400; }
  .badge-missing { @apply badge bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-400; }

  .page-btn { @apply rounded-lg border border-slate-300 dark:border-slate-700 px-3 py-1.5 text-sm text-slate-700 dark:text-slate-300 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-50 dark:hover:bg-slate-800 transition-colors; }
}
"""

_DASHBOARD_JS = """
(function () {
  // Reusable client-side "data table" behavior: given a <tbody>, a rows-per-page
  // <select>, and prev/next buttons, shows only the current page of whichever
  // rows currently match (all rows, unless a getMatching() filter is supplied).
  function createPager(options) {
    var allRows = Array.prototype.slice.call(options.tbody.querySelectorAll('tr'));
    var getMatching = options.getMatching || function () { return allRows; };
    var page = 1;

    function render() {
      var matching = getMatching(allRows);
      var rawValue = options.rowsPerPageSelect ? options.rowsPerPageSelect.value : 'all';
      var rowsPerPage = rawValue === 'all' ? Math.max(matching.length, 1) : parseInt(rawValue, 10);
      var totalPages = Math.max(1, Math.ceil(matching.length / rowsPerPage));
      if (page > totalPages) page = totalPages;
      if (page < 1) page = 1;

      var start = (page - 1) * rowsPerPage;
      var end = start + rowsPerPage;
      var visible = matching.slice(start, end);

      allRows.forEach(function (row) { row.style.display = 'none'; });
      visible.forEach(function (row) { row.style.display = ''; });

      if (options.pageInfo) {
        options.pageInfo.textContent = matching.length === 0
          ? 'No rows to display'
          : 'Showing ' + (start + 1) + '–' + Math.min(end, matching.length) +
            ' of ' + matching.length + '   •   Page ' + page + ' of ' + totalPages;
      }
      if (options.prevBtn) options.prevBtn.disabled = page <= 1;
      if (options.nextBtn) options.nextBtn.disabled = page >= totalPages;
      if (options.emptyState) options.emptyState.style.display = matching.length === 0 ? 'block' : 'none';
    }

    if (options.prevBtn) options.prevBtn.addEventListener('click', function () { page -= 1; render(); });
    if (options.nextBtn) options.nextBtn.addEventListener('click', function () { page += 1; render(); });
    if (options.rowsPerPageSelect) {
      options.rowsPerPageSelect.addEventListener('change', function () { page = 1; render(); });
    }

    render();
    return { refresh: function () { page = 1; render(); } };
  }

  var imageTbody = document.querySelector('#imageTable tbody');
  if (imageTbody) {
    var searchBox = document.getElementById('imageSearch');
    var filterButtons = document.querySelectorAll('[data-filter]');
    var activeFilter = 'all';

    var imagePager = createPager({
      tbody: imageTbody,
      rowsPerPageSelect: document.getElementById('imageRowsPerPage'),
      prevBtn: document.getElementById('imagePrevPage'),
      nextBtn: document.getElementById('imageNextPage'),
      pageInfo: document.getElementById('imagePageInfo'),
      emptyState: document.getElementById('emptyState'),
      getMatching: function (allRows) {
        var term = (searchBox.value || '').toLowerCase();
        return allRows.filter(function (row) {
          var status = row.getAttribute('data-status');
          var haystack = row.getAttribute('data-search') || '';
          var matchesFilter = activeFilter === 'all' || status === activeFilter;
          var matchesSearch = term === '' || haystack.indexOf(term) !== -1;
          return matchesFilter && matchesSearch;
        });
      }
    });

    searchBox.addEventListener('input', function () { imagePager.refresh(); });
    filterButtons.forEach(function (button) {
      button.addEventListener('click', function () {
        filterButtons.forEach(function (b) {
          b.classList.remove('filter-btn-active');
          b.classList.add('filter-btn-inactive');
        });
        button.classList.remove('filter-btn-inactive');
        button.classList.add('filter-btn-active');
        activeFilter = button.getAttribute('data-filter');
        imagePager.refresh();
      });
    });
  }

  var brokenTbody = document.querySelector('#brokenTable tbody');
  if (brokenTbody) {
    createPager({
      tbody: brokenTbody,
      rowsPerPageSelect: document.getElementById('brokenRowsPerPage'),
      prevBtn: document.getElementById('brokenPrevPage'),
      nextBtn: document.getElementById('brokenNextPage'),
      pageInfo: document.getElementById('brokenPageInfo')
    });
  }
})();
"""

_HTML_TEMPLATE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>$tailwind_config</script>
<style type="text/tailwindcss">$tailwind_components</style>
</head>
<body class="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 dark:from-slate-950 dark:to-slate-900 text-slate-900 dark:text-slate-100 font-sans antialiased">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-10 py-8 lg:py-12">

    <header class="mb-8 flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3">
      <div>
        <h1 class="text-3xl font-bold tracking-tight">$title</h1>
        <p class="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Website: <span class="font-medium text-slate-700 dark:text-slate-200">$website_folder</span>
        </p>
      </div>
      <div class="inline-flex items-center gap-2 rounded-full bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 px-4 py-2 text-xs text-slate-500 dark:text-slate-400 shadow-sm">
        <span class="h-2 w-2 rounded-full bg-emerald-500"></span>
        Generated $generated_at
      </div>
    </header>

    <section class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-8">
      $cards
    </section>

    <section class="panel">
      <div class="panel-title">Overview</div>
      <div class="flex flex-wrap items-center gap-8">
        <div class="relative h-40 w-40 shrink-0 rounded-full" style="background: conic-gradient($donut_gradient);">
          <div class="absolute inset-3 rounded-full bg-white dark:bg-slate-900 flex flex-col items-center justify-center">
            <div class="text-2xl font-bold">$total_images</div>
            <div class="text-xs text-slate-500 dark:text-slate-400">images</div>
          </div>
        </div>
        <div class="flex flex-col gap-2.5">
          $legend
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-title">Reports</div>
      <div class="flex flex-wrap gap-2.5">
        $links
      </div>
    </section>

    <section class="panel">
      <div class="panel-title">Images ($total_images)</div>
      <div class="flex flex-wrap items-center gap-2 mb-4">
        <input type="text" id="imageSearch" placeholder="Search by filename or path..." class="toolbar-input">
        <button type="button" class="filter-btn-active" data-filter="all">All</button>
        <button type="button" class="filter-btn-inactive" data-filter="referenced">Referenced</button>
        <button type="button" class="filter-btn-inactive" data-filter="unused">Unused</button>
        <button type="button" class="filter-btn-inactive" data-filter="protected">Protected</button>
        <div class="flex items-center gap-2 ml-auto text-sm text-slate-500 dark:text-slate-400">
          <label for="imageRowsPerPage">Rows per page</label>
          <select id="imageRowsPerPage" class="toolbar-select">
            <option value="25" selected>25</option>
            <option value="50">50</option>
            <option value="100">100</option>
            <option value="250">250</option>
            <option value="all">All</option>
          </select>
        </div>
      </div>
      <div class="overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-800">
        <table id="imageTable" class="data-table">
          <thead>
            <tr>
              <th>Image</th><th>Path</th><th>Folder</th><th>Size</th><th>Modified</th>
              <th>Refs</th><th>Status</th><th>Confidence</th><th>Recommendation</th><th>Reason</th>
            </tr>
          </thead>
          <tbody>
            $table_rows
          </tbody>
        </table>
        <div id="emptyState" class="hidden text-center text-sm text-slate-500 dark:text-slate-400 py-10">No images match the current search/filter.</div>
      </div>
      <div class="flex flex-wrap items-center justify-between gap-3 mt-4">
        <div id="imagePageInfo" class="text-sm text-slate-500 dark:text-slate-400"></div>
        <div class="flex gap-2">
          <button type="button" class="page-btn" id="imagePrevPage">Prev</button>
          <button type="button" class="page-btn" id="imageNextPage">Next</button>
        </div>
      </div>
    </section>

    $broken_section

  </div>
  <script>$js</script>
</body>
</html>
""")


@dataclass
class DuplicateGroupRow:
    """One row of duplicate-image data, produced by Phase 7's duplicate detector."""

    sha256: str
    image_path: str
    file_size: int
    is_primary: bool
    duplicate_of: Optional[str]


@dataclass
class LiquidWebComparisonRow:
    """One row comparing this app's verdict against a Liquid Web orphan report."""

    image_path: str
    liquidweb_says_unused: bool
    app_says_referenced: bool
    agreement: bool
    note: str


class ReportGenerator:
    """Writes CSV reports from a :class:`ComparisonReport` into an output folder."""

    def __init__(self, output_folder: Path) -> None:
        self.output_folder = Path(output_folder)

    def generate_all(
        self,
        comparison_report: ComparisonReport,
        scan_result: ScanResult,
        duplicate_groups: Optional[List[DuplicateGroupRow]] = None,
        liquidweb_rows: Optional[List[LiquidWebComparisonRow]] = None,
    ) -> Dict[str, Path]:
        """Generate every available report and return a name -> path mapping.

        ``duplicate_groups`` / ``liquidweb_rows`` are optional so this method
        already has the right shape for Phases 7 and 8 to plug into without
        changes here.
        """
        self.output_folder.mkdir(parents=True, exist_ok=True)

        generated: Dict[str, Path] = {
            "referenced": self.generate_referenced_images_csv(comparison_report),
            "unused": self.generate_unused_images_csv(comparison_report),
            "protected": self.generate_protected_images_csv(comparison_report),
            "broken": self.generate_broken_images_csv(comparison_report),
            "summary": self.generate_summary_csv(comparison_report, scan_result),
        }

        if duplicate_groups:
            generated["duplicates"] = self.generate_duplicate_images_csv(duplicate_groups)
        if liquidweb_rows:
            generated["liquidweb"] = self.generate_liquidweb_comparison_csv(liquidweb_rows)

        generated["dashboard"] = self.generate_summary_html(comparison_report, scan_result, generated)

        logger.info("Generated %d report file(s) in %s", len(generated), self.output_folder)
        return generated

    # ------------------------------------------------------------------ #
    # Per-image reports
    # ------------------------------------------------------------------ #
    def generate_referenced_images_csv(self, report: ComparisonReport) -> Path:
        results = [r for r in report.results if r.status is ImageStatus.REFERENCED]
        return self._write_image_csv("ReferencedImages.csv", results)

    def generate_unused_images_csv(self, report: ComparisonReport) -> Path:
        results = [r for r in report.results if r.status is ImageStatus.UNUSED]
        return self._write_image_csv("UnusedImages.csv", results)

    def generate_protected_images_csv(self, report: ComparisonReport) -> Path:
        results = [r for r in report.results if r.status is ImageStatus.PROTECTED]
        return self._write_image_csv("ProtectedImages.csv", results)

    def generate_broken_images_csv(self, report: ComparisonReport) -> Path:
        rows = [
            {
                "Page": row["page"],
                "Missing Image": row["missing"],
                "Reference Type": row["reference_type"],
            }
            for row in self._dedupe_broken_rows(report.missing)
        ]
        path = self.output_folder / "BrokenImages.csv"
        self._write_csv(path, _BROKEN_REPORT_FIELDNAMES, rows)
        return path

    @staticmethod
    def _dedupe_broken_rows(missing: List[MissingImageResult]) -> List[Dict[str, object]]:
        """Collapse (page, missing image) duplicates into one row each.

        The same broken image is often referenced from the same page more
        than once — e.g. via both ``src`` and ``srcset``, or the same tag
        repeated in the markup — and each occurrence is captured as its own
        reference. Users care about *which pages reference which missing
        images*, not how many times one page happens to reference the same
        one, so rows are deduplicated per (page, missing image) pair here;
        if a page reaches the same missing image through more than one
        reference type, those types are combined into a single cell rather
        than producing a duplicate-looking row per type.
        """
        rows: List[Dict[str, object]] = []
        for item in sorted(missing, key=lambda m: m.normalized_path):
            types_by_page: Dict[str, List[str]] = {}
            for ref in item.references:
                types = types_by_page.setdefault(ref.source_file, [])
                if ref.reference_type not in types:
                    types.append(ref.reference_type)
            for page in sorted(types_by_page):
                rows.append(
                    {
                        "page": page,
                        "missing": item.normalized_path,
                        "reference_type": ", ".join(types_by_page[page]),
                        "confidence": item.confidence,
                    }
                )
        return rows

    def generate_summary_csv(self, report: ComparisonReport, scan_result: ScanResult) -> Path:
        summary = report.summary
        rows = [
            {"Metric": "Website Folder", "Value": str(scan_result.root_folder)},
            {"Metric": "Report Generated", "Value": datetime.now().strftime(DATE_FORMAT)},
            {"Metric": "Total Images Scanned", "Value": summary.total_images},
            {"Metric": "Referenced Images", "Value": summary.referenced_count},
            {"Metric": "Unused Images", "Value": summary.unused_count},
            {"Metric": "Protected Images", "Value": summary.protected_count},
            {"Metric": "Missing / Broken References", "Value": summary.missing_count},
            {"Metric": "Website Files Scanned", "Value": scan_result.stats.website_files_found},
            {"Metric": "Scan Duration (seconds)", "Value": f"{scan_result.stats.duration_seconds:.2f}"},
            {"Metric": "Scan Errors", "Value": scan_result.stats.errors},
        ]
        path = self.output_folder / "SummaryReport.csv"
        self._write_csv(path, _SUMMARY_FIELDNAMES, rows)
        return path

    # ------------------------------------------------------------------ #
    # Phase 7 / Phase 8 hooks
    # ------------------------------------------------------------------ #
    def generate_duplicate_images_csv(self, duplicate_groups: List[DuplicateGroupRow]) -> Path:
        rows = [
            {
                "SHA256": d.sha256,
                "Image Path": d.image_path,
                "File Size": human_readable_size(d.file_size),
                "Is Primary": "Yes" if d.is_primary else "No",
                "Duplicate Of": d.duplicate_of or "",
            }
            for d in duplicate_groups
        ]
        path = self.output_folder / "DuplicateImages.csv"
        self._write_csv(path, ["SHA256", "Image Path", "File Size", "Is Primary", "Duplicate Of"], rows)
        return path

    def generate_liquidweb_comparison_csv(self, liquidweb_rows: List[LiquidWebComparisonRow]) -> Path:
        rows = [
            {
                "Image Path": r.image_path,
                "Liquid Web Says Unused": "Yes" if r.liquidweb_says_unused else "No",
                "App Says Referenced": "Yes" if r.app_says_referenced else "No",
                "Agreement": "Yes" if r.agreement else "No",
                "Note": r.note,
            }
            for r in liquidweb_rows
        ]
        path = self.output_folder / "LiquidWebComparison.csv"
        self._write_csv(
            path,
            ["Image Path", "Liquid Web Says Unused", "App Says Referenced", "Agreement", "Note"],
            rows,
        )
        return path

    # ------------------------------------------------------------------ #
    # HTML dashboard
    # ------------------------------------------------------------------ #
    def generate_summary_html(
        self,
        report: ComparisonReport,
        scan_result: ScanResult,
        generated_reports: Optional[Dict[str, Path]] = None,
    ) -> Path:
        """Render the self-contained ``SummaryReport.html`` dashboard.

        Args:
            report: The comparison results to visualize.
            scan_result: The scan this comparison was based on (for the
                website folder path and file counts).
            generated_reports: The name -> path mapping already produced by
                :meth:`generate_all` (or a subset of it), used to build the
                "Reports" panel's clickable links. Only files that actually
                exist are linked.
        """
        generated_reports = generated_reports or {}
        summary = report.summary
        unused_bytes = sum(
            r.image.size_bytes for r in report.results if r.status is ImageStatus.UNUSED
        )

        page = _HTML_TEMPLATE.substitute(
            title="Image Cleaner - Summary Report",
            website_folder=escape(str(scan_result.root_folder)),
            generated_at=datetime.now().strftime(DATE_FORMAT),
            total_images=summary.total_images,
            cards=self._render_cards(summary, unused_bytes),
            donut_gradient=self._render_donut_gradient(summary),
            legend=self._render_legend(summary),
            links=self._render_report_links(generated_reports),
            table_rows=self._render_table_rows(report.results) or self._empty_row(),
            broken_section=self._render_broken_section(report.missing),
            tailwind_config=_TAILWIND_CONFIG_JS,
            tailwind_components=_TAILWIND_COMPONENT_CSS,
            js=_DASHBOARD_JS,
        )

        path = self.output_folder / "SummaryReport.html"
        try:
            path.write_text(page, encoding="utf-8")
            logger.info("Wrote dashboard %s", path)
        except OSError as exc:
            logger.error("Failed to write dashboard %s: %s", path, exc)
            raise
        return path

    @staticmethod
    def _render_cards(summary: ComparisonSummary, unused_bytes: int) -> str:
        cards = [
            ("Total Images", summary.total_images, "stat-card-total"),
            ("Referenced", summary.referenced_count, "stat-card-referenced"),
            ("Unused", summary.unused_count, "stat-card-unused"),
            ("Protected", summary.protected_count, "stat-card-protected"),
            ("Missing / Broken", summary.missing_count, "stat-card-missing"),
            ("Potential Space Savings", human_readable_size(unused_bytes), "stat-card-savings"),
        ]
        parts = []
        for label, value, card_class in cards:
            parts.append(
                f'<div class="{card_class}">'
                f'<div class="stat-value">{escape(str(value))}</div>'
                f'<div class="stat-label">{escape(label)}</div></div>'
            )
        return "\n".join(parts)

    @staticmethod
    def _render_donut_gradient(summary: ComparisonSummary) -> str:
        total = summary.referenced_count + summary.unused_count + summary.protected_count
        if total == 0:
            return "#d1d5db 0 100%"

        segments = [
            (summary.referenced_count, _STATUS_COLOR[ImageStatus.REFERENCED]),
            (summary.unused_count, _STATUS_COLOR[ImageStatus.UNUSED]),
            (summary.protected_count, _STATUS_COLOR[ImageStatus.PROTECTED]),
        ]
        stops = []
        cursor = 0.0
        for count, color in segments:
            if count == 0:
                continue
            start = cursor
            cursor += (count / total) * 100
            stops.append(f"{color} {start:.2f}% {cursor:.2f}%")
        return ", ".join(stops) if stops else "#d1d5db 0 100%"

    @staticmethod
    def _render_legend(summary: ComparisonSummary) -> str:
        rows = [
            ("Referenced", summary.referenced_count, "legend-dot-referenced"),
            ("Unused", summary.unused_count, "legend-dot-unused"),
            ("Protected", summary.protected_count, "legend-dot-protected"),
        ]
        parts = []
        for label, count, dot_class in rows:
            parts.append(
                f'<div class="flex items-center gap-2.5 text-sm text-slate-700 dark:text-slate-300">'
                f'<span class="{dot_class}"></span>{escape(label)}: <span class="font-semibold">{count}</span></div>'
            )
        return "\n".join(parts)

    @staticmethod
    def _render_report_links(generated_reports: Dict[str, Path]) -> str:
        labels = {
            "referenced": "Referenced Images (CSV)",
            "unused": "Unused Images (CSV)",
            "protected": "Protected Images (CSV)",
            "broken": "Broken Images (CSV)",
            "summary": "Summary (CSV)",
            "duplicates": "Duplicate Images (CSV)",
            "liquidweb": "Liquid Web Comparison (CSV)",
        }
        parts = []
        for key, label in labels.items():
            path = generated_reports.get(key)
            if path is None:
                continue
            parts.append(f'<a class="report-link" href="{escape(path.name)}">{escape(label)}</a>')
        if not parts:
            return '<span class="text-sm text-slate-500 dark:text-slate-400">No report files were generated.</span>'
        return "\n".join(parts)

    @staticmethod
    def _render_table_rows(results: List[ImageComparisonResult]) -> str:
        rows = []
        for result in sorted(results, key=lambda r: r.image.relative_path):
            image = result.image
            status_key = result.status.value.lower()  # matches the filter buttons' data-filter values
            badge_class = _STATUS_BADGE_CLASS[result.status]
            search_blob = escape(f"{image.filename} {image.relative_path}".lower())
            rows.append(
                "<tr data-status=\"{status_key}\" data-search=\"{search}\">"
                "<td>{name}</td><td>{path}</td><td>{folder}</td><td>{size}</td>"
                "<td>{modified}</td><td>{refs}</td>"
                "<td><span class=\"{badge_class}\">{status_label}</span></td>"
                "<td>{confidence}%</td><td>{recommendation}</td><td class=\"whitespace-normal max-w-xs\">{reason}</td></tr>".format(
                    status_key=status_key,
                    badge_class=badge_class,
                    search=search_blob,
                    name=escape(image.filename),
                    path=escape(image.relative_path),
                    folder=escape(image.folder or "(root)"),
                    size=escape(human_readable_size(image.size_bytes)),
                    modified=escape(image.modified_time.strftime(DATE_FORMAT)),
                    refs=result.reference_count,
                    status_label=escape(result.status.value),
                    confidence=round(result.confidence * 100),
                    recommendation=escape(_RECOMMENDATION_BY_STATUS[result.status]),
                    reason=escape(result.reason),
                )
            )
        return "\n".join(rows)

    @staticmethod
    def _empty_row() -> str:
        return (
            '<tr><td colspan="10" class="text-center text-slate-500 dark:text-slate-400 py-10">'
            "No images found.</td></tr>"
        )

    @staticmethod
    def _render_broken_section(missing: List[MissingImageResult]) -> str:
        if not missing:
            return ""
        rows = []
        for row in ReportGenerator._dedupe_broken_rows(missing):
            rows.append(
                "<tr><td>{page}</td><td>{missing}</td><td>{ref_type}</td>"
                "<td><span class=\"badge-missing\">{confidence}%</span></td></tr>".format(
                    page=escape(str(row["page"])),
                    missing=escape(str(row["missing"])),
                    ref_type=escape(str(row["reference_type"])),
                    confidence=round(row["confidence"] * 100),
                )
            )
        table_rows = "\n".join(rows)
        return (
            '<section class="panel">'
            f'<div class="panel-title">Broken References ({len(missing)})</div>'
            '<div class="flex flex-wrap items-center gap-2 mb-4">'
            '<div class="flex items-center gap-2 ml-auto text-sm text-slate-500 dark:text-slate-400">'
            '<label for="brokenRowsPerPage">Rows per page</label>'
            '<select id="brokenRowsPerPage" class="toolbar-select">'
            '<option value="25" selected>25</option>'
            '<option value="50">50</option>'
            '<option value="100">100</option>'
            '<option value="all">All</option>'
            "</select></div></div>"
            '<div class="overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-800">'
            '<table id="brokenTable" class="data-table"><thead><tr>'
            "<th>Page</th><th>Missing Image</th><th>Reference Type</th><th>Confidence</th>"
            f"</tr></thead><tbody>{table_rows}</tbody></table></div>"
            '<div class="flex flex-wrap items-center justify-between gap-3 mt-4">'
            '<div class="text-sm text-slate-500 dark:text-slate-400" id="brokenPageInfo"></div>'
            '<div class="flex gap-2">'
            '<button type="button" class="page-btn" id="brokenPrevPage">Prev</button>'
            '<button type="button" class="page-btn" id="brokenNextPage">Next</button>'
            "</div></div></section>"
        )

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _image_row(result: ImageComparisonResult) -> Dict[str, object]:
        image = result.image
        return {
            "Image": image.filename,
            "Path": image.relative_path,
            "Folder": image.folder or "(root)",
            "File Size": human_readable_size(image.size_bytes),
            "Modified Date": image.modified_time.strftime(DATE_FORMAT),
            "Reference Count": result.reference_count,
            "Recommendation": _RECOMMENDATION_BY_STATUS[result.status],
            "Reason": result.reason,
        }

    def _write_image_csv(self, filename: str, results: List[ImageComparisonResult]) -> Path:
        # Sort the (already in-memory) result objects, then build row dicts
        # lazily during the write itself rather than materializing a second
        # full list of rows alongside `results` — matters at the 50,000+
        # image scale this app targets, where each redundant list is
        # several tens of MB.
        results.sort(key=lambda r: r.image.relative_path)
        path = self.output_folder / filename
        self._write_csv(path, _IMAGE_REPORT_FIELDNAMES, (self._image_row(r) for r in results))
        return path

    def _write_csv(self, path: Path, fieldnames: List[str], rows: Iterable[Dict[str, object]]) -> int:
        """Write rows (a list or a lazily-evaluated generator) as CSV. Returns the row count."""
        count = 0
        try:
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)
                    count += 1
            logger.info("Wrote %s (%d row(s))", path, count)
        except OSError as exc:
            logger.error("Failed to write report %s: %s", path, exc)
            raise
        return count
