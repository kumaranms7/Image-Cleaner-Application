"""
normalizer.py
=============

Normalizes image paths and URLs discovered by the scanner and the reference
parser into a single comparable root-relative key.

Relative references (no leading ``/`` and no scheme) are resolved against
the *directory of the file that referenced them* — exactly how a browser
resolves a relative URL in HTML or CSS. This matters most for CSS: a
stylesheet at ``assets/css/style.css`` containing ``url(../images/logo.png)``
refers to ``assets/images/logo.png``, not ``images/logo.png`` — getting this
wrong silently misses real CSS-only references and flags in-use images as
unused. Root-relative references (``/images/logo.png``) and absolute URLs
resolve independently of the referencing file's location, same as in a
browser.

So all of the following resolve to the identical key when referenced *from
a file at the website root*::

    images/logo.png
    /images/logo.png
    ./images/logo.png
    https://domain.com/images/logo.png

...while a reference to ``../images/logo.png`` from ``assets/css/style.css``
correctly resolves to ``assets/images/logo.png`` instead.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import unquote, urlsplit

# Schemes that never point at a local file on this website and should be
# skipped entirely rather than normalized (e.g. embedded image data or
# non-resource links accidentally picked up by generic text scanning).
_NON_RESOURCE_SCHEMES = {"data", "mailto", "tel", "javascript", "blob", "about"}


def _strip_www(host: str) -> str:
    """Strip a literal leading ``www.`` prefix (not arbitrary w/. characters)."""
    return host[4:] if host.startswith("www.") else host


@dataclass(frozen=True)
class NormalizedReference:
    """The result of normalizing a single raw reference string."""

    original: str
    normalized_key: Optional[str]
    scheme: str
    is_external: bool
    is_data_uri: bool
    is_ignored: bool

    @property
    def is_comparable(self) -> bool:
        """Whether this reference can be meaningfully compared to a scanned image."""
        return self.normalized_key is not None and not self.is_ignored


def _clean_rooted_path(path: str) -> str:
    """Normalize a path that is already root-anchored: a root-relative
    reference (leading ``/``), or the path component of an absolute /
    protocol-relative URL. Decodes percent-escapes, normalizes slash
    direction, resolves ``.``/``..`` segments, and lower-cases the result.
    """
    path = unquote(path)
    path = path.replace("\\", "/")
    path = path.lstrip("/")
    if not path:
        return ""
    normalized = posixpath.normpath(path)
    if normalized in (".", "..") or normalized.startswith("../"):
        # Escapes above the site root — not a resolvable local path.
        return ""
    return normalized.strip().lower()


def _resolve_relative_path(source_dir: str, raw_path: str) -> str:
    """Resolve a page/CSS-relative reference against the directory of the
    file that referenced it, mirroring browser URL resolution."""
    raw_path = unquote(raw_path)
    raw_path = raw_path.replace("\\", "/")
    combined = posixpath.join(source_dir, raw_path) if source_dir else raw_path
    normalized = posixpath.normpath(combined)
    if normalized in (".", "..") or normalized.startswith("../"):
        # Escapes above the site root — not a resolvable local path.
        return ""
    return normalized.strip().lower()


def normalize_reference(
    raw_value: str,
    source_dir: str = "",
    site_domains: Optional[Iterable[str]] = None,
) -> NormalizedReference:
    """Normalize a single raw reference (an ``src``, ``url()``, JSON value, etc.).

    Args:
        raw_value: The exact string extracted from a website file.
        source_dir: POSIX-style directory (relative to the website root) of
            the file this reference was found in, e.g. ``"assets/css"``, or
            ``""`` for a file at the root. Used to resolve plain relative
            references the same way a browser would. Ignored for
            root-relative references and absolute URLs, which are already
            self-contained.
        site_domains: Optional hostnames belonging to this website (e.g.
            ``{"example.com", "www.example.com"}``). Absolute URLs pointing at
            one of these hosts are treated as internal/comparable; URLs
            pointing elsewhere are marked external and excluded from
            comparison.

    Returns:
        A :class:`NormalizedReference` describing how (or whether) this value
        can be compared against scanned images.
    """
    text = (raw_value or "").strip()
    if not text:
        return NormalizedReference(raw_value, None, "", False, False, True)

    domains = {_strip_www(d.lower()) for d in (site_domains or [])}

    split = urlsplit(text)
    scheme = split.scheme.lower()

    if scheme in _NON_RESOURCE_SCHEMES:
        is_data = scheme == "data"
        return NormalizedReference(raw_value, None, scheme, not is_data, is_data, True)

    if scheme in ("http", "https"):
        host = _strip_www(split.netloc.lower().split(":")[0])
        is_internal = bool(domains) and host in domains
        normalized = _clean_rooted_path(split.path) if (is_internal or not domains) else None
        return NormalizedReference(
            original=raw_value,
            normalized_key=normalized,
            scheme=scheme,
            is_external=not is_internal,
            is_data_uri=False,
            is_ignored=False,
        )

    if text.startswith("//"):
        # Protocol-relative URL, e.g. "//cdn.example.com/logo.png".
        host = _strip_www(text[2:].split("/", 1)[0].lower().split(":")[0])
        remainder = text[2:].split("/", 1)[1] if "/" in text[2:] else ""
        is_internal = bool(domains) and host in domains
        normalized = _clean_rooted_path(remainder) if (is_internal or not domains) else None
        return NormalizedReference(raw_value, normalized, "", not is_internal, False, False)

    # Plain relative/absolute path (site-relative URL, Unix path, or Windows path).
    raw_path = split.path or text
    is_root_relative = raw_path.replace("\\", "/").startswith("/")
    normalized = (
        _clean_rooted_path(raw_path)
        if is_root_relative
        else _resolve_relative_path(source_dir, raw_path)
    )
    return NormalizedReference(
        original=raw_value,
        normalized_key=normalized if normalized else None,
        scheme="",
        is_external=False,
        is_data_uri=False,
        is_ignored=not normalized,
    )


def normalize_scanned_path(relative_posix_path: str) -> str:
    """Normalize a path already produced by :class:`scanner.ScannedFile`
    (which is always a clean, root-relative POSIX path) into the same
    comparison key space used by :func:`normalize_reference`.
    """
    return _clean_rooted_path(relative_posix_path)
