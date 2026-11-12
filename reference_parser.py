"""
reference_parser.py
====================

Extracts every image *reference* (as opposed to image *file*) from a
website's HTML, CSS, JS, JSON, XML and plain-text files.

The design is deliberately extensible: each extraction rule lives in its own
:class:`BaseExtractor` subclass with a narrow, well-tested regex or parser,
registered with :class:`ReferenceParser`. Adding support for a new pattern
(e.g. a framework-specific attribute) means adding one small class and one
``register_extractor`` call — nothing else in the pipeline needs to change.

All extractors return raw, un-normalized strings; turning those into a
canonical comparison key is :mod:`normalizer`'s job.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from scanner import WebsiteFile

logger = logging.getLogger(__name__)

# Progress callback signature: (files_parsed, total_estimate, current_path)
ProgressCallback = Callable[[int, int, str], None]


@dataclass
class RawReference:
    """A single, not-yet-normalized image reference found in a website file."""

    raw_value: str
    reference_type: str
    source_file: str  # POSIX relative path of the file the reference was found in
    line_number: Optional[int] = None


def _line_number(text: str, index: int) -> int:
    """Return the 1-based line number of a character offset within ``text``."""
    return text.count("\n", 0, index) + 1


def _has_image_extension(value: str, image_extensions: Set[str]) -> bool:
    """Check whether ``value`` (ignoring query string/anchor) ends with a
    known image extension."""
    cleaned = value.split("?", 1)[0].split("#", 1)[0].strip().lower()
    return any(cleaned.endswith("." + ext) for ext in image_extensions)


# --------------------------------------------------------------------------- #
# Shared low-level helpers (used by multiple extractors)
# --------------------------------------------------------------------------- #

_TAG_RE_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _find_tags(text: str, tag_name: str) -> List[Tuple[str, int]]:
    """Find all ``<tag_name ...>`` opening tags (self-closed or not).

    Returns a list of ``(full_tag_text, start_index)`` tuples.
    """
    pattern = _TAG_RE_CACHE.get(tag_name)
    if pattern is None:
        pattern = re.compile(rf"<{tag_name}\b[^>]*>", re.IGNORECASE)
        _TAG_RE_CACHE[tag_name] = pattern
    return [(m.group(0), m.start()) for m in pattern.finditer(text)]


_ATTR_RE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))"""
)


def _parse_attributes(tag_text: str) -> Dict[str, str]:
    """Parse an HTML tag's attributes into a lower-cased-key dict.

    Handles double-quoted, single-quoted, and bare (unquoted) attribute
    values, in any attribute order.
    """
    attrs: Dict[str, str] = {}
    for match in _ATTR_RE.finditer(tag_text):
        name = match.group(1).lower()
        value = match.group(2)
        if value is None:
            value = match.group(3)
        if value is None:
            value = match.group(4)
        attrs[name] = value or ""
    return attrs


_CSS_URL_RE = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s'"]+))\s*\)""", re.IGNORECASE)


def _extract_css_urls(text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
    """Extract ``url(...)`` values from CSS-like text that point at an image.

    CSS's ``url()`` function is used for far more than images — fonts
    (``@font-face src``), other stylesheets (``@import``), cursors, etc. —
    so matches are filtered down to known image extensions here, the same
    way :func:`_extract_js_image_strings` and the plain-text extractor
    already do. Without this, a font or ``@import``-ed stylesheet that
    doesn't happen to also exist as a scanned image would incorrectly show
    up as a "broken image" reference (it isn't an image reference at all).
    """
    results = []
    for match in _CSS_URL_RE.finditer(text):
        value = match.group(1) or match.group(2) or match.group(3) or ""
        if value and _has_image_extension(value, image_extensions):
            results.append((value, match.start()))
    return results


_QUOTED_STRING_RE = re.compile(r'"([^"\n]{1,2048})"|\'([^\'\n]{1,2048})\'')


def _extract_js_image_strings(text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
    """Extract quoted JavaScript string literals that look like image paths."""
    results = []
    for match in _QUOTED_STRING_RE.finditer(text):
        value = match.group(1) or match.group(2) or ""
        if value and _has_image_extension(value, image_extensions):
            results.append((value, match.start()))
    return results


def _collect_json_ld_images(node: Any) -> List[str]:
    """Recursively collect ``"image"`` values from a parsed JSON-LD structure.

    An ``image`` value may be a plain URL string, a list of URL strings, or
    an ``ImageObject`` dict with a ``url`` key.
    """
    found: List[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            image_value = value.get("image")
            if isinstance(image_value, str):
                found.append(image_value)
            elif isinstance(image_value, list):
                for item in image_value:
                    if isinstance(item, str):
                        found.append(item)
                    elif isinstance(item, dict) and isinstance(item.get("url"), str):
                        found.append(item["url"])
            elif isinstance(image_value, dict) and isinstance(image_value.get("url"), str):
                found.append(image_value["url"])
            for child in value.values():
                _walk(child)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(node)
    return found


_PLAIN_URL_RE = re.compile(r"""(?<![\w"'=])((?:https?:)?//?[^\s"'<>()]+)""", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Extractors
# --------------------------------------------------------------------------- #


class BaseExtractor(ABC):
    """Base class for a single image-reference extraction rule."""

    #: Reference type label used in reports (e.g. ``"img_src"``).
    reference_type: str = "generic"

    #: File extensions (with leading dot, lower-case) this extractor applies
    #: to. An empty frozenset means "applies to every scanned file type".
    applicable_extensions: FrozenSet[str] = frozenset()

    def applies_to(self, extension: str) -> bool:
        return not self.applicable_extensions or extension in self.applicable_extensions

    @abstractmethod
    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        """Return a list of ``(raw_value, char_index)`` tuples found in ``text``."""


class ImgSrcExtractor(BaseExtractor):
    """``<img src="...">``"""

    reference_type = "img_src"
    applicable_extensions = frozenset({".html", ".htm"})
    _re = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE)

    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        results = []
        for match in self._re.finditer(text):
            value = match.group(1) or match.group(2) or match.group(3) or ""
            if value:
                results.append((value, match.start()))
        return results


class SrcsetExtractor(BaseExtractor):
    """``<img srcset="...">`` and ``<source srcset="...">`` (responsive images)."""

    reference_type = "srcset"
    applicable_extensions = frozenset({".html", ".htm"})
    _re = re.compile(
        r"""<(?:img|source)\b[^>]*?\bsrcset\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE
    )

    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        results = []
        for match in self._re.finditer(text):
            candidate_set = match.group(1) or match.group(2) or ""
            for candidate in candidate_set.split(","):
                url = candidate.strip().split()[0] if candidate.strip() else ""
                if url and _has_image_extension(url, image_extensions):
                    results.append((url, match.start()))
        return results


class PictureSourceExtractor(BaseExtractor):
    """``<source src="...">`` inside ``<picture>`` (rare, but seen in some polyfills).

    A regex has no notion of parent element, so ``<source src="...">`` can't
    be distinguished from the identical-looking tag used inside ``<video>``/
    ``<audio>`` (e.g. ``<video><source src="movie.mp4"></video>``) — filtering
    to known image extensions is what keeps video/audio files out.
    """

    reference_type = "picture_source"
    applicable_extensions = frozenset({".html", ".htm"})
    _re = re.compile(r"""<source\b[^>]*?\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)

    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        results = []
        for match in self._re.finditer(text):
            url = match.group(1) or match.group(2) or ""
            if url and _has_image_extension(url, image_extensions):
                results.append((url, match.start()))
        return results


class InlineStyleBackgroundExtractor(BaseExtractor):
    """``style="background-image: url(...)"`` on any HTML element."""

    reference_type = "inline_style_background"
    applicable_extensions = frozenset({".html", ".htm"})
    _style_attr_re = re.compile(r"""\bstyle\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)

    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        results = []
        for match in self._style_attr_re.finditer(text):
            style_value = match.group(1) or match.group(2) or ""
            for url, _offset in _extract_css_urls(style_value, image_extensions):
                results.append((url, match.start()))
        return results


class StyleBlockExtractor(BaseExtractor):
    """``<style>...</style>`` blocks embedded in HTML."""

    reference_type = "css_url"
    applicable_extensions = frozenset({".html", ".htm"})
    _re = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)

    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        results = []
        for match in self._re.finditer(text):
            block = match.group(1)
            offset = match.start(1)
            for url, local_offset in _extract_css_urls(block, image_extensions):
                results.append((url, offset + local_offset))
        return results


class CssUrlExtractor(BaseExtractor):
    """Image ``url(...)`` references in ``.css`` files — ``background``,
    ``background-image``, list-style-image, etc. Non-image ``url()`` targets
    (fonts via ``@font-face``, other stylesheets via ``@import``, ...) are
    filtered out by :func:`_extract_css_urls`."""

    reference_type = "css_url"
    applicable_extensions = frozenset({".css"})

    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        return _extract_css_urls(text, image_extensions)


class MetaImageExtractor(BaseExtractor):
    """``<meta property="og:image">`` and ``<meta name="twitter:image">``."""

    reference_type = "meta_image"
    applicable_extensions = frozenset({".html", ".htm"})
    _target_names = {"og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"}

    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        results = []
        for tag_text, start in _find_tags(text, "meta"):
            attrs = _parse_attributes(tag_text)
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key in self._target_names and attrs.get("content"):
                results.append((attrs["content"], start))
        return results


class LinkIconExtractor(BaseExtractor):
    """``<link rel="icon">``, ``rel="shortcut icon"``, ``rel="apple-touch-icon">``, ``rel="mask-icon">``."""

    reference_type = "link_icon"
    applicable_extensions = frozenset({".html", ".htm"})

    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        results = []
        for tag_text, start in _find_tags(text, "link"):
            attrs = _parse_attributes(tag_text)
            rel = attrs.get("rel", "").lower()
            if "icon" in rel and attrs.get("href"):
                results.append((attrs["href"], start))
        return results


_SCRIPT_TAG_RE = re.compile(r'<script\b([^>]*)>(.*?)</script>', re.IGNORECASE | re.DOTALL)


class InlineJsonLdExtractor(BaseExtractor):
    """``<script type="application/ld+json">`` blocks embedded in HTML."""

    reference_type = "json_ld_image"
    applicable_extensions = frozenset({".html", ".htm"})

    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        results = []
        for match in _SCRIPT_TAG_RE.finditer(text):
            attrs = _parse_attributes(match.group(1))
            if "ld+json" not in attrs.get("type", "").lower():
                continue
            try:
                parsed = json.loads(match.group(2))
            except (json.JSONDecodeError, ValueError):
                continue
            offset = match.start(2)
            for url in _collect_json_ld_images(parsed):
                results.append((url, offset))
        return results


class InlineScriptStringExtractor(BaseExtractor):
    """Non-JSON-LD inline ``<script>`` blocks: quoted strings that look like image paths."""

    reference_type = "javascript_string"
    applicable_extensions = frozenset({".html", ".htm"})

    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        results = []
        for match in _SCRIPT_TAG_RE.finditer(text):
            attrs = _parse_attributes(match.group(1))
            if "ld+json" in attrs.get("type", "").lower():
                continue
            offset = match.start(2)
            for url, local_offset in _extract_js_image_strings(match.group(2), image_extensions):
                results.append((url, offset + local_offset))
        return results


class ManifestJsonExtractor(BaseExtractor):
    """Web app manifest ``icons`` / ``screenshots`` arrays (``manifest.json``)."""

    reference_type = "manifest_icon"
    applicable_extensions = frozenset({".json"})

    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return []
        results = []
        for key in ("icons", "screenshots"):
            entries = data.get(key) if isinstance(data, dict) else None
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and isinstance(entry.get("src"), str):
                        results.append((entry["src"], 0))
        return results


class JsonLdImageExtractor(BaseExtractor):
    """Standalone ``.json`` files containing JSON-LD ``image`` fields."""

    reference_type = "json_ld_image"
    applicable_extensions = frozenset({".json"})

    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return []
        return [(url, 0) for url in _collect_json_ld_images(data)]


class JavaScriptStringExtractor(BaseExtractor):
    """Quoted string literals in ``.js`` files that look like image paths."""

    reference_type = "javascript_string"
    applicable_extensions = frozenset({".js"})

    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        return _extract_js_image_strings(text, image_extensions)


class PlainTextUrlExtractor(BaseExtractor):
    """Fallback: bare URL-like tokens ending in a known image extension.

    Applies to plain-text and XML files (e.g. sitemaps, ``robots.txt``) where
    there is no structured markup to key off of.
    """

    reference_type = "plain_text_url"
    applicable_extensions = frozenset({".txt", ".xml"})

    def extract(self, text: str, image_extensions: Set[str]) -> List[Tuple[str, int]]:
        results = []
        for match in _PLAIN_URL_RE.finditer(text):
            value = match.group(1)
            if _has_image_extension(value, image_extensions):
                results.append((value, match.start()))
        return results


# --------------------------------------------------------------------------- #
# Parser orchestration
# --------------------------------------------------------------------------- #


class ReferenceParser:
    """Runs every registered :class:`BaseExtractor` over each website file.

    Extensible: call :meth:`register_extractor` to add support for a new
    reference pattern without modifying this class.
    """

    def __init__(self, image_extensions: Iterable[str]) -> None:
        self.image_extensions: Set[str] = {ext.lower().lstrip(".") for ext in image_extensions}
        self._extractors: List[BaseExtractor] = []
        self._register_default_extractors()

    def register_extractor(self, extractor: BaseExtractor) -> None:
        """Add a new extraction rule to the pipeline."""
        self._extractors.append(extractor)

    def _register_default_extractors(self) -> None:
        for extractor in (
            ImgSrcExtractor(),
            SrcsetExtractor(),
            PictureSourceExtractor(),
            InlineStyleBackgroundExtractor(),
            StyleBlockExtractor(),
            CssUrlExtractor(),
            MetaImageExtractor(),
            LinkIconExtractor(),
            InlineJsonLdExtractor(),
            InlineScriptStringExtractor(),
            ManifestJsonExtractor(),
            JsonLdImageExtractor(),
            JavaScriptStringExtractor(),
            PlainTextUrlExtractor(),
        ):
            self.register_extractor(extractor)

    def parse_file(self, website_file: WebsiteFile) -> List[RawReference]:
        """Extract every reference found in a single website file."""
        text = self._read_text(website_file.absolute_path)
        if text is None:
            return []

        extension = Path(website_file.relative_path).suffix.lower()
        references: List[RawReference] = []

        for extractor in self._extractors:
            if not extractor.applies_to(extension):
                continue
            try:
                matches = extractor.extract(text, self.image_extensions)
            except Exception as exc:  # noqa: BLE001 - one bad extractor must not break the scan
                logger.warning(
                    "Extractor %s failed on %s: %s",
                    type(extractor).__name__,
                    website_file.relative_path,
                    exc,
                )
                continue

            for raw_value, char_index in matches:
                cleaned = raw_value.strip()
                if not cleaned:
                    continue
                references.append(
                    RawReference(
                        raw_value=cleaned,
                        reference_type=extractor.reference_type,
                        source_file=website_file.relative_path,
                        line_number=_line_number(text, char_index),
                    )
                )
        return references

    def parse_all(
        self,
        website_files: Iterable[WebsiteFile],
        max_workers: int = 8,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> List[RawReference]:
        """Parse many website files concurrently and aggregate all references.

        Uses a thread pool since parsing is I/O-bound (reading files) plus
        CPU-light regex work, which benefits from concurrency without the
        complexity of multiprocessing. Each file is read from disk exactly
        once (in :meth:`parse_file`) and shared in memory across every
        applicable extractor, rather than re-reading it per extractor.

        Args:
            website_files: Files to parse (typically ``scan_result.website_files``).
            max_workers: Thread pool size.
            progress_callback: Optional callable invoked periodically with
                ``(files_parsed, total_estimate, current_path)`` so a GUI can
                show progress across 10,000+ page sites instead of appearing
                to hang.
        """
        all_references: List[RawReference] = []
        files = list(website_files)
        total = len(files)
        if not files:
            return all_references

        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_file = {executor.submit(self.parse_file, wf): wf for wf in files}
            for future in as_completed(future_to_file):
                website_file = future_to_file[future]
                completed += 1
                try:
                    all_references.extend(future.result())
                except Exception as exc:  # noqa: BLE001 - keep scanning remaining files
                    logger.warning("Failed to parse %s: %s", website_file.relative_path, exc)

                if progress_callback and (completed % 25 == 0 or completed == total):
                    progress_callback(completed, total, website_file.relative_path)

        return all_references

    @staticmethod
    def _read_text(path: Path) -> Optional[str]:
        """Read a file as text, tolerating invalid UTF-8 by replacing bad bytes."""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError) as exc:
            logger.warning("Could not read %s: %s", path, exc)
            return None
