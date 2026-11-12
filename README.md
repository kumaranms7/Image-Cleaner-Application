# Image Cleaner

A Windows desktop app that audits a static website and safely finds the images it no longer uses.

Image Cleaner scans your site's HTML, CSS, JS, JSON and other source files, works out which images are actually referenced, and reports the rest as unused. **It never deletes anything.** Unused images are only moved to a dated backup folder, and you can restore them at any time.

## Features

- **Reference detection:** finds images used in `<img src>`, `srcset`, `<picture><source>`, inline `style=` and `<style>` backgrounds, CSS `url()`, `og:image` / `twitter:image`, favicons and `apple-touch-icon`, JSON-LD, `manifest.json` icons, JS string literals, and plain-text URLs.
- **Path resolution:** relative, root-relative, `./`, `../` and absolute-URL forms of the same path resolve to one key, the way a browser would.
- **Classification:** each image is marked **Referenced**, **Unused** or **Protected**. Each result has a confidence score and a plain-English reason.
- **Broken references:** lists images that pages reference but that don't exist on disk.
- **Duplicate detection:** groups identical images by SHA-256 hash and suggests which copy to keep.
- **Liquid Web cross-check:** imports a Liquid Web orphan-images report and flags where it disagrees with this app's findings.
- **Reports:** writes CSVs (`ReferencedImages`, `UnusedImages`, `ProtectedImages`, `BrokenImages`, `DuplicateImages`, `LiquidWebComparison`, `SummaryReport`) and an HTML dashboard with search, filters and pagination.
- **Safe backup and restore:** moves unused images to `backup/YYYY-MM-DD/`, keeps the folder structure, never overwrites a file, and records every move in `BackupManifest.csv`. You can restore everything or just a selection.
- **Built for large sites:** multithreaded scanning, parsing and file moves, aimed at 50,000+ images and 10,000+ pages.
- **GUI:** Tkinter interface with light and dark themes, live stats cards, a colour-coded activity log, and phase and percentage progress.

## Requirements

- Windows
- Python 3.11 or later (standard library only, no `pip install` needed to run)

## Quick start

```
python app.py
```

1. Choose your website folder and an output folder.
2. Optionally add a Liquid Web orphan report.
3. Click **Start Scan**.
4. Click **Generate Reports**, then **Open Report** to view the dashboard.
5. Review the results, then click **Move Unused Images** to move them to the backup folder.
6. Use **Restore Backup** to undo any move.

## Configuration

Settings are stored in `config.json`, which is created on first run. You can edit it in the in-app **Settings** dialog.

| Setting | Purpose |
|---|---|
| `excluded_folders` | Folders to skip, for example `.git` and `node_modules` |
| `excluded_filenames` / `excluded_extensions` | Images to protect from being flagged unused (wildcards `*` and `?` supported) |
| `recent_file_days` | Protect images modified in the last N days (`0` turns this off) |
| `supported_image_extensions` | File types treated as images |
| `supported_scan_extensions` | Source files searched for references |
| `detect_duplicates`, `detect_missing_images`, `detect_broken_references` | Turn individual checks on or off |
| `backup_enabled` | Enable moving unused images to backup |
| `theme` | `light` or `dark` |

## Building a standalone .exe

Double-click `build.bat`. It installs PyInstaller if needed and writes `dist/ImageCleaner.exe`, a single portable file. To build by hand:

```
pip install pyinstaller
pyinstaller ImageCleaner.spec
```

The `.exe` creates `config.json`, `reports/`, `logs/` and `backup/` next to itself on first run.

## Project layout

| Module | Responsibility |
|---|---|
| `app.py` | Entry point |
| `gui.py`, `theme.py` | Tkinter interface and light/dark theme |
| `config.py` | Settings load and save |
| `scanner.py` | Recursive scan of images and source files |
| `reference_parser.py` | Extracts image references from source files |
| `normalizer.py` | Normalizes paths and URLs for comparison |
| `comparison_engine.py` | Referenced, unused, missing and protected verdicts |
| `duplicate_detector.py` | SHA-256 duplicate grouping |
| `liquidweb_importer.py` | Liquid Web report import and comparison |
| `report_generator.py` | CSV and HTML dashboard output |
| `backup_manager.py` | Backup, restore and manifest |
| `utils.py` | Logging, hashing, path and icon helpers |

## Notes

- The HTML dashboard loads Tailwind CSS from a CDN, so it needs an internet connection to look styled. The data stays readable offline, and the rest of the app works fully offline.
- Detection is based on static analysis. Images referenced only through dynamically built paths (for example `"img/" + name + ".png"` in JavaScript) can't be seen, so check the Unused list before moving files.

See [DEVELOPMENT.md](DEVELOPMENT.md) for the detailed development history and bugfix notes.
