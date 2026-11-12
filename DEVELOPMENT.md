# Image Cleaner

A Windows desktop application that analyzes a static HTML website and safely
identifies unused images. Nothing is ever deleted automatically — unused
images are only moved to a timestamped backup folder, and every action is
reported in detail.

## Requirements

- Python 3.11+
- Standard library only (Tkinter, which ships with the standard Windows
  Python installer)

## Running

```
python app.py
```

## Building a standalone .exe

**Easiest:** double-click `build.bat` (or run it from a terminal). It checks
for Python and PyInstaller (installing PyInstaller automatically if
missing), cleans any previous `build/`/`dist/` output, runs the build, and
pauses at the end so you can read the result before the window closes.

Equivalently, by hand: requires `pyinstaller` (`pip install pyinstaller`). A
pre-generated spec file (`ImageCleaner.spec`) already captures the correct
flags, so rebuilding is just:

```
pyinstaller ImageCleaner.spec
```

Or from scratch:

```
pyinstaller --onefile --windowed --name "ImageCleaner" --icon icon.ico --add-data "icon.ico;." app.py
```

This produces `dist/ImageCleaner.exe` — a single portable file. On first
run it creates `config.json`, `reports/`, `logs/`, and `backup/` next to
wherever the `.exe` actually is (not a temp folder), same as running from
source; `utils.get_base_dir()` resolves this correctly in both cases.

`--icon icon.ico` embeds the icon as the `.exe`'s own PE resource (shown in
Explorer, and when pinned to the taskbar); `--add-data "icon.ico;."`
additionally bundles it as a runtime data file so `gui.py` can load it via
`utils.get_icon_path()` and call `self.iconbitmap(default=...)` on the main
window — the `default=` form applies it to every dialog (Settings, Restore
Backup) too, not just the main window, so it's consistent across the whole
app rather than needing to be set on each window individually.

Verified for real, not just by inspecting the build log: ran `build.bat`
itself end-to-end (it correctly cleaned prior output and rebuilt), then ran
the resulting `.exe`, confirmed `config.json`/`logs`/`reports`/`backup` were
correctly created next to it, then used the Win32 API directly
(`GetClassLongPtrW` + `GetIconInfo` + `GetObjectW`) to extract the actual
icon bitmap from both the running window and the `.exe`'s embedded PE
resource — both returned valid, non-default icon bitmaps (confirmed
different from vanilla Tk's built-in icon by comparing handle values),
proving the icon genuinely applies at runtime rather than just trusting
that the CLI flags "should" work. Note `build.bat`'s closing `pause` will
sit waiting for a keypress if ever invoked non-interactively (e.g. from
another script) — that's intentional for its real double-click use case,
just something to be aware of if automating it further.

`--windowed` suppresses the console window PyInstaller would otherwise pop
up behind the GUI. Note onefile + windowed builds spawn a short-lived
bootloader process that launches a child process to actually run the app —
expected PyInstaller behavior, not a bug, if you ever go looking for the
running process and see two entries.

## Project Layout

| Module                  | Responsibility                                             |
|--------------------------|-------------------------------------------------------------|
| `app.py`                 | Entry point: sets up logging/config and launches the GUI    |
| `gui.py`                  | Tkinter desktop interface                                   |
| `config.py`               | `AppConfig` dataclass + JSON load/save (`config.json`)       |
| `scanner.py`              | Recursive website scanner (images + source files)            |
| `reference_parser.py`     | Extracts image references from HTML/CSS/JS/JSON/etc. *(Phase 2)* |
| `normalizer.py`           | Normalizes paths/URLs for comparison *(Phase 2)*              |
| `comparison_engine.py`    | Referenced vs. unused vs. missing vs. protected *(Phase 3)*   |
| `report_generator.py`     | CSV + HTML dashboard reports *(Phase 4/5)*                    |
| `backup_manager.py`       | Moves unused images into `backup/YYYY-MM-DD/` *(Phase 6)*      |
| `duplicate_detector.py`   | SHA-256 based duplicate detection *(Phase 7)*                  |
| `liquidweb_importer.py`   | Imports/compares Liquid Web orphan reports *(Phase 8)*         |
| `theme.py`                | Light/dark color palette + ttk style configuration for the GUI |
| `utils.py`                | Shared logging, hashing, path-matching, and icon-resolution helpers |
| `icon.ico`                | Application icon — embedded in the built `.exe` and applied to every window at runtime |
| `ImageCleaner.spec`       | Pre-generated PyInstaller build spec (`pyinstaller ImageCleaner.spec`) |
| `build.bat`                | One-click build script — see "Building a standalone .exe" above |

## Development Status

This project is being built in phases. See the phase notes shared alongside
each update for what is implemented so far.

- **Phase 1:** project structure, configuration, GUI shell, and the website
  scanner (image + file discovery, exclusion rules, progress reporting,
  threaded scanning so the UI stays responsive).
- **Phase 2:** extensible reference parser (`img src`, `srcset`,
  `<picture><source>`, inline `style=` and `<style>` backgrounds, CSS
  `url()`, `og:image`/`twitter:image`, `<link rel="icon"/apple-touch-icon">`,
  inline/standalone JSON-LD `image`, manifest.json icons, JS string literals,
  and plain-text URL fallback) plus the path normalizer that resolves
  relative references (see the Phase 9 bugfix note below for how this is
  actually resolved) and collapses root-relative, `./`, and absolute-URL
  forms of the same path into one comparable key.
- **Phase 3:** comparison engine — classifies every scanned image
  as `Referenced`, `Unused`, or `Protected` (matching
  `excluded_filenames`/`excluded_extensions`/`recent_file_days`), and lists
  `Missing` broken references (referenced images that don't exist on disk).
  Every result carries a confidence score and a human-readable reason. Note:
  protection rules moved out of the scanner and into the comparison engine —
  website files are always scanned for references (skipping one could hide a
  real reference), and protected images now show up in reports instead of
  disappearing silently.
- **Phase 4:** CSV report generation — `ReferencedImages.csv`,
  `UnusedImages.csv`, `ProtectedImages.csv`, `BrokenImages.csv`, and
  `SummaryReport.csv`, written by `report_generator.py`. `Start Scan` in the
  GUI now runs the full scan → parse → compare pipeline, and `Generate
  Reports` writes the CSVs to the selected output folder (both run on
  background threads). `DuplicateImages.csv` and `LiquidWebComparison.csv`
  hooks already exist in `ReportGenerator.generate_all()`, ready for Phases
  7 and 8 to supply data.
- **Phase 5:** `SummaryReport.html` — a self-contained HTML
  dashboard (inline CSS/JS only, no CDN or network calls, works fully
  offline) with summary cards, a CSS-only donut chart, clickable links to
  every CSV report, a searchable/filterable image table with color-coded
  status badges, and a broken-references panel. Generated automatically as
  part of `ReportGenerator.generate_all()`.
- **Phase 6:** `backup_manager.py` — moves unused images into
  `backup/YYYY-MM-DD/` (preserving folder structure, never overwriting an
  existing backup file) and records every move in that session's
  `BackupManifest.csv`. Restore supports both "entire backup" and a
  user-selected subset, updates the manifest's `Status` column in place
  (`Moved` → `Restored`) rather than deleting history, and refuses to
  restore over a file that already exists at the destination. `Move Unused
  Images` and `Restore Backup` in the GUI are now fully wired, including a
  confirmation prompt before moving/restoring and a selection dialog for
  restore.
- **Phase 7:** `duplicate_detector.py` — groups scanned images by
  identical SHA-256 content hash (only computed when `detect_duplicates` is
  enabled), picks a `Referenced` copy as the "primary" to keep when one
  exists, and feeds `DuplicateImages.csv` plus the dashboard's Duplicate
  Images link via `ReportGenerator`'s existing Phase-7 hook. Duplicate
  detection is informational only — it never changes a scanned image's
  Referenced/Unused/Protected status from `comparison_engine.py`, it's a
  separate report meant to be read alongside it. Runs automatically as part
  of `Start Scan` when `detect_duplicates` is on.
- **Phase 8:** `liquidweb_importer.py` — parses a Liquid Web
  orphan-images report (permissive plain-text format: one path per line,
  `#`/`//`/`;` comments and blank lines skipped, trailing comma/tab metadata
  ignored) and cross-checks every entry against this app's own verdicts.
  Flags a disagreement whenever Liquid Web calls an image unused but this
  app found real references to it (a likely false-positive on Liquid Web's
  side, since it can't see references expressed via CSS/JSON/JS the way
  this app's parser does) — and, in keeping with the app's safety-first
  design, treats an image Liquid Web lists but this app's scan can't find at
  all as *uncertain* rather than silently agreeing. Feeds
  `LiquidWebComparison.csv` via `ReportGenerator`'s existing Phase-8 hook,
  and runs automatically during `Start Scan` whenever a Liquid Web report
  path is provided in the GUI.
- **Phase 9 (current):** performance pass, aimed at the spec's 50,000+
  image / 10,000+ page target:
  - `scanner.py` — per-file classification (stat + optional SHA-256 hash)
    now runs on a `ThreadPoolExecutor` (16 workers by default) instead of a
    single-threaded loop; `_classify_file` was made a pure function (no
    shared-state mutation) so results can be safely returned from worker
    threads and folded into `ScanResult` on the main thread.
  - `reference_parser.py` already used a thread pool (Phase 2); it now also
    reports progress via the same `(done, total, current_path)` callback
    shape as the scanner, so 10,000+ page sites show live progress instead
    of appearing to hang.
  - `backup_manager.py` — destination-path assignment (the part that touches
    shared collision-tracking state) stays sequential since it's cheap; the
    actual `shutil.move` calls, which dominate runtime, now run on a thread
    pool for both backup and restore. All manifest-row mutation still
    happens on the main thread as each future completes, so the "never
    overwrite an existing file" and "never lose a manifest row" guarantees
    are unaffected by the added concurrency (re-verified with a dedicated
    conflict test after parallelizing).
  - `report_generator.py` — the three full-image-set CSVs (`Referenced`,
    `Unused`, `Protected` — together covering every scanned image) no longer
    materialize a second full list of row-dicts alongside the existing
    results list; rows are now built lazily via a generator during the
    write itself, meaningfully cutting peak memory at the 50,000-image
    scale.
  - No file is ever read from disk more than once: the scanner reads each
    image's bytes only when hashing for duplicate detection, and the
    reference parser reads each website file's text once and shares it
    in-memory across every applicable extractor rather than re-reading per
    pattern.
  - Verified with a synthetic 2,045-image / 300-page stress test exercising
    every parallelized path (scan, parse, move, restore) end-to-end:
    zero misclassifications, zero lost or duplicated manifest rows, and a
    dedicated restore-conflict check confirmed the no-overwrite safety
    guarantee still holds after parallelization.

### Bugfix: CSS-referenced images incorrectly flagged Unused

`normalizer.py` originally normalized every relative reference by blindly
stripping *all* leading `./`/`../`/`/` markers regardless of where the
reference came from. That's wrong for relative references written from a
non-root file: a CSS file at `assets/css/style.css` containing
`url(../images/logo.png)` refers to `assets/images/logo.png` (resolved
relative to the CSS file's own directory, per CSS/browser semantics), but
the old normalizer collapsed it straight to `images/logo.png`, which never
matched the actual scanned image — so the image was reported `Unused` even
though it was genuinely referenced. Same-folder CSS references
(`url(icon.png)` from within `assets/css/`) were affected even more
severely, since there were no leading dots to strip at all.

Fixed by having `normalize_reference()` accept the referencing file's
directory (`source_dir`) and resolve plain relative references against it
via `posixpath.join` + `posixpath.normpath` — the same resolution a browser
performs — while root-relative (`/images/...`) and absolute URLs continue
to resolve independently of the source file's location, as before.
`comparison_engine.py` now passes each reference's own source directory
(derived from `RawReference.source_file`) when building the reference
index. Verified with a reproduction of the reported bug (nested CSS file,
same-folder CSS reference, root-level CSS reference) plus the full existing
regression suite (Phase 3 protection/unused/referenced/missing, Liquid Web
comparison) — all still pass.

### GUI redesign

`gui.py` was rebuilt around a new `theme.py` design system rather than the
plain default Tk look:

- **Light/dark theme**, toggleable from the header and persisted to
  `config.json` (`theme` field). Built on ttk's `clam` base theme (the only
  bundled theme that honors custom colors on Windows — the native
  `vista`/`xpnative` themes largely ignore `ttk.Style` color overrides) with
  a full custom palette for every widget type: buttons (Primary/Secondary/
  Ghost variants), entries, checkbuttons, progressbar, labelframes,
  scrollbars. Classic `tk` widgets (`Text`, `Listbox`, card border frames)
  aren't covered by `ttk.Style` at all, so they register a small
  "theme refresh" callback that reapplies the current palette — both at
  creation and again whenever the user toggles the theme.
- **Live stats dashboard** embedded directly in the app: six color-coded
  cards (Referenced / Unused / Protected / Missing / Duplicate Groups /
  Space Reclaimable) update immediately after each scan, mirroring the HTML
  report's summary cards so the desktop app and its generated reports read
  as one consistent product.
- **Card-based layout** (folders, stats, activity log) with a header,
  status-bar activity dot that recolors by state (idle/busy/success/error),
  and a color-coded activity log (warnings in amber, errors in red) instead
  of a flat text dump.
- Caught and fixed a real bug during this work, not just a styling issue:
  packing the expanding log console *before* the action-button bar and
  status bar in Tk's `pack()` call order caused those bottom widgets to be
  squeezed out of the window entirely — Tk's packer carves cavity in
  insertion order, and a widget packed with `expand=True` claims whatever
  cavity remains *at the time it's packed*, regardless of what's packed
  after it. Fixed by packing fixed-height edge widgets (status bar, action
  bar, both `side="bottom"`) before the expanding log console. Verified via
  widget-geometry introspection (`winfo_rooty()`/`winfo_height()` for every
  action-bar and status-bar widget, confirming all sit within the window's
  bounds with no overlap) since on-machine screenshot capture was unreliable
  in this environment.

### GUI: "Open Report" button

After a successful `Generate Reports`, the button relabels itself to
`Open Report` and its click handler switches to launching the generated
`SummaryReport.html` dashboard in the system's default browser
(`webbrowser.open()` on a proper `file://` URI). It reverts to
`Generate Reports` whenever the underlying data goes stale — a new scan
completes, or the Output Folder changes — so it can never point at a report
that no longer matches what's on screen. If the report file has been moved
or deleted since generation, clicking it shows a warning and resets the
button rather than silently failing.

### HTML dashboard: paginated data tables

Both dashboard tables (the main Images table and the Broken References
table) are now real paginated data tables instead of dumping every row into
one long scroll: 25 rows per page by default, with a "Rows per page"
dropdown (25/50/100/250/All for Images; 25/50/100/All for Broken
References) and Prev/Next controls showing "Showing X–Y of Z • Page N of M".
Implemented as a single reusable `createPager()` function in the dashboard's
vanilla JS (no CDN/library dependency, consistent with the report's
offline-only design) that both tables' pagination wires into; the Images
table's existing search box and status filter buttons now feed into the
same pager instead of directly toggling row visibility, so search/filter and
pagination interact correctly instead of fighting each other. Verified two
ways: structurally (every row is still present in the raw HTML — pagination
hides rows client-side, it doesn't omit data) and functionally, by rendering
the actual generated report in headless Edge and inspecting the post-JS DOM:
a 60-row Images table correctly showed 25 visible rows, "Page 1 of 3," and a
disabled Prev button; a 40-row Broken References table correctly showed 25
visible rows and "Page 1 of 2."

### HTML dashboard: rebuilt with real Tailwind CSS

At the user's explicit request, `SummaryReport.html` is now styled with
actual Tailwind CSS loaded from the CDN (`cdn.tailwindcss.com`), not
hand-written CSS. **This is a deliberate, flagged exception** to every other
part of this app: everything else — the desktop GUI, all CSV generation,
and the report's own *data* — still works fully offline; only this one HTML
file needs an internet connection to render styled (it degrades to
unstyled-but-readable HTML without one, rather than failing outright).
Given how consistently this project had been built around an offline-first,
CDN-free design, I flagged the tradeoff and asked before proceeding rather
than assuming; the user chose real Tailwind over a hand-rolled
Tailwind-style alternative.

Implementation notes:

- `darkMode: 'media'` is configured inline (`tailwind.config = {...}`)
  before the CDN script runs, so `dark:` variants keep responding to the
  OS's `prefers-color-scheme` automatically, same as before.
- Repeated per-row/per-badge utility classes are composed once via `@apply`
  inside a `<style type="text/tailwindcss">` block (e.g. `.badge-referenced`,
  `.stat-card-unused`, `.filter-btn-active`) rather than writing out the
  full Tailwind utility list on every generated `<td>`/badge/row — at this
  app's 50,000+ image target scale, inlining full class lists per row would
  meaningfully bloat the generated file and slow generation. This is still
  genuine Tailwind (composing utilities via its own `@apply` directive,
  processed by the real Tailwind engine at render time), just not repeated
  literally thousands of times.
- The donut chart's conic-gradient remains an inline `style` attribute since
  its color stops are computed per-report (data-driven, not expressible as
  a static utility class) — but its hex values now match Tailwind's actual
  palette (`emerald-500`, `red-500`, `blue-500`) so it's visually
  indistinguishable from the Tailwind-classed badges next to it.
- Verified for real, not just by reading the generated markup: found
  headless Edge and Chrome available on this machine, rendered the actual
  generated report through `msedge --headless=new --dump-dom` (which fetches
  the live CDN script and executes it), and confirmed a ~25KB stylesheet was
  genuinely injected by Tailwind's runtime containing real generated rules
  for this report's exact class names (`.stat-card-referenced`,
  `.badge-referenced`, `.filter-btn-active`, the indigo brand color) — proof
  the CDN integration actually works end-to-end, not just that the HTML
  looks plausible. Also confirmed 25 correct `@media
  (prefers-color-scheme: dark)` blocks were generated with correct dark
  values, and took an actual rendered screenshot to visually confirm the
  light-mode result.

### Bugfix: fonts and @import-ed stylesheets showing up as "broken images"

`reference_parser.py`'s `_extract_css_urls()` (shared by `CssUrlExtractor`,
`InlineStyleBackgroundExtractor`, and `StyleBlockExtractor`) extracted
*every* CSS `url(...)` value with no filtering — but `url()` is used for far
more than images: `@font-face src: url(...)` (fonts), `@import url(...)`
(other stylesheets), cursors, etc. Since fonts and imported stylesheets are
never scanned as images, any such reference that didn't coincidentally also
exist as a scanned image file landed in `report.missing` and showed up in
`BrokenImages.csv`/the dashboard as a broken *image* — even though it
wasn't an image reference at all.

Fixed by having `_extract_css_urls()` filter matches through the same
`_has_image_extension()` check the JS-string and plain-text extractors
already used, so `.ttf`/`.woff`/`.woff2`/`.eot`/`.css` (and anything else
that isn't a configured image extension) are excluded at extraction time
rather than leaking through to be mismatched later. Verified with a CSS
file containing all four font formats, an `@import`, a real image
reference, and a genuinely missing image in one go: the fonts and the
`@import` no longer appear anywhere, the real image is still correctly
detected as `Referenced`, and the genuinely missing image is still
correctly flagged — confirming this narrows false positives without
losing real detections.

### Bugfix: duplicate rows and video files in Broken Images

Two related reports of the same underlying gap:

1. **Video files (`.mp4`, `.webm`, ...) showing up as broken images.**
   `PictureSourceExtractor` matched any `<source src="...">` tag with no
   filtering — but a regex has no notion of parent element, so it can't
   distinguish `<picture><source src="...">` (responsive images) from the
   identical-looking `<video><source src="movie.mp4"></video>` /
   `<audio><source src="clip.mp3"></audio>` pattern. `SrcsetExtractor` had
   the same gap. Fixed the same way as the CSS/font issue above: both now
   filter through `_has_image_extension()`, so non-image `<source>` targets
   are never extracted as references at all.
2. **The same broken image listed multiple times for the same page.**
   When a page reaches one missing image through more than one mechanism —
   e.g. both `<img src="x.png">` and `<img srcset="x.png 1x">`, or the same
   `<img>` tag simply repeated in the markup — each occurrence was captured
   as a separate reference, producing one `BrokenImages.csv`/dashboard row
   per occurrence. Fixed with a new `ReportGenerator._dedupe_broken_rows()`
   helper (shared by the CSV and the HTML dashboard, so both stay
   consistent) that collapses rows to one per unique (page, missing image)
   pair, combining every reference type that hit it into a single cell
   (e.g. `img_src, srcset`) instead of duplicating the row per type.

Verified with a page referencing the same missing image five different ways
(repeated `img_src`, `srcset`, and a `<picture>` variant) alongside a
`<video>` tag with `.mp4`/`.webm` sources: the video files never appeared
in the extracted references at all, and the five references to the same
missing image collapsed into exactly one `BrokenImages.csv` row combining
`img_src, srcset` — confirmed in both the CSV and the HTML dashboard's
table.

### GUI: Clear Log, phase/percentage progress, resizable Settings

Three usability requests:

- **Clear Log button** next to the "ACTIVITY LOG" header — clears the log
  console without needing to close and reopen the app.
- **Phase + percentage progress display**, replacing the bare progress bar
  with a labeled one: a phase name ("Scanning", "Parsing references",
  "Comparing", "Detecting duplicates", "Generating reports", "Moving to
  backup", "Restoring", ...) on the left and a live percentage on the
  right. Steps with a real incremental count (scanning, parsing, moving,
  restoring) show an actual percentage; steps with no granular progress
  available (comparing, duplicate detection, report generation) switch the
  bar to an animated indeterminate mode instead of leaving it looking
  frozen, via new `_set_progress_phase()` / `_finish_progress()` helpers
  wired into every worker's phase transitions.
- **Settings dialog now actually grows when resized/maximized.** The
  underlying bug: the dialog's scrollable-canvas content was only ever
  sized to its natural minimum — the canvas's `<Configure>` handler synced
  *width* to the viewport but never height, so resizing the dialog just
  left the extra space as blank scrollable area below the (unchanged-size)
  input boxes. Fixed by having the canvas stretch its embedded content to
  `max(content's natural height, canvas viewport height)`, and making each
  list field's row and `Text` widget use `fill="both", expand=True` (not
  just `fill="x"`) so multiple rows sharing `expand=True` divide up
  whatever extra vertical space becomes available, per standard Tk `pack`
  behavior — this is what makes the input boxes actually grow instead of
  just the window around them. Verified precisely via `winfo_height()`
  before/after resizing (not just visual inspection): canvas height
  tracked the dialog's resize exactly (484px→644px→689px across
  640px/800px/845px-tall dialogs on this machine), and the list fields'
  container correctly absorbed the corresponding extra room each time
  while the fixed-height Options section stayed exactly unchanged. Note:
  how much the boxes visibly grow depends on your screen's actual height —
  this sandbox's test display is a constrained 864px tall, which limits
  how much "slack" is available even when maximized; on a normal 1080p+
  screen there's substantially more room for them to expand into.
