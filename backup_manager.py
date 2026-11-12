"""
backup_manager.py
==================

Moves unused images into a dated backup folder — never deletes them — and
restores them back to the website on request.

Layout on disk::

    backup/
      2026-07-07/
        BackupManifest.csv
        images/logo-old.png      <- same relative path as in the website

Every move and restore is recorded in that session's ``BackupManifest.csv``
(``Original Path``, ``Backup Path``, ``File Size (Bytes)``, ``Moved At``,
``Status``, ``Restored At``). Restoring updates the manifest's ``Status``
column in place rather than deleting rows, so the manifest stays a complete
audit trail of everything that was ever moved and restored.

Restore supports both "entire backup" (:meth:`BackupManager.restore_all`)
and a caller-selected subset (:meth:`BackupManager.restore_entries`) per the
spec's Restore Manager requirements — both live here since the project
structure defines a single ``backup_manager.py`` module, not a separate
restore module.

Performance: destination-path assignment (which touches the shared
collision-tracking set) happens sequentially first, since it's cheap
pure-Python bookkeeping; the actual ``shutil.move`` calls — the slow,
I/O-bound part — then run on a thread pool. Manifest-row mutation always
happens back on the main thread as each future completes, so nothing shared
is ever written from two threads at once.
"""

from __future__ import annotations

import csv
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Set, Tuple

from scanner import ImageFile
from utils import human_readable_size

logger = logging.getLogger(__name__)

#: Thread pool size for concurrent file moves. Kept more conservative than
#: the scanner's read-only workers since moves involve writes on both ends.
DEFAULT_BACKUP_WORKERS = 8

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MANIFEST_FILENAME = "BackupManifest.csv"
_MANIFEST_FIELDNAMES = [
    "Original Path",
    "Backup Path",
    "File Size (Bytes)",
    "Moved At",
    "Status",
    "Restored At",
]

ProgressCallback = Callable[[int, int, str], None]


@dataclass
class BackupManifestEntry:
    """One row of a session's ``BackupManifest.csv``."""

    original_relative_path: str
    backup_relative_path: str
    file_size: int
    moved_at: datetime
    status: str = "Moved"  # "Moved" or "Restored"
    restored_at: Optional[datetime] = None


@dataclass
class BackupSession:
    """A single dated backup folder (``backup/YYYY-MM-DD/``)."""

    session_folder: Path
    manifest_path: Path
    date_label: str


@dataclass
class MoveOutcome:
    """Result of attempting to move one image into backup."""

    image: ImageFile
    success: bool
    backup_path: Optional[Path]
    error: Optional[str]


@dataclass
class BackupRunResult:
    """Aggregate result of :meth:`BackupManager.move_unused_images`."""

    session: BackupSession
    moved: List[MoveOutcome]
    failed: List[MoveOutcome]
    total_bytes_moved: int


@dataclass
class BackupEntry:
    """A restorable item: one manifest row resolved to an absolute backup path."""

    session_date: str
    original_relative_path: str
    backup_absolute_path: Path
    file_size: int
    moved_at: datetime
    manifest_path: Path


@dataclass
class RestoreOutcome:
    """Result of attempting to restore one backup entry."""

    entry: BackupEntry
    success: bool
    restored_path: Optional[Path]
    error: Optional[str]


@dataclass
class RestoreRunResult:
    """Aggregate result of a restore operation."""

    restored: List[RestoreOutcome]
    failed: List[RestoreOutcome]


class BackupManager:
    """Moves unused images into dated backup folders and restores them on request."""

    def __init__(self, backup_root: Path) -> None:
        self.backup_root = Path(backup_root)

    # ------------------------------------------------------------------ #
    # Backup (move out of the website)
    # ------------------------------------------------------------------ #
    def move_unused_images(
        self,
        images: List[ImageFile],
        progress_callback: Optional[ProgressCallback] = None,
        max_workers: int = DEFAULT_BACKUP_WORKERS,
    ) -> BackupRunResult:
        """Move the given images into today's dated backup session folder.

        Preserves each image's relative folder structure under the session
        folder. Never overwrites an existing backup file — on the rare
        collision (e.g. the same relative path backed up twice in one day)
        the new copy is disambiguated with a `` (1)``-style suffix, and the
        manifest still records enough information to trace it back.

        Destination paths are assigned sequentially first (cheap bookkeeping
        against the shared collision set), then the actual file moves — the
        slow, I/O-bound part — run concurrently on a thread pool.
        """
        session = self._get_or_create_session()
        manifest_rows = self._read_manifest(session.manifest_path)
        used_backup_relpaths: Set[str] = {row.backup_relative_path for row in manifest_rows}

        assignments: List[Tuple[ImageFile, str, Path]] = []
        for image in images:
            backup_rel = self._resolve_unique_backup_relpath(image.relative_path, used_backup_relpaths)
            used_backup_relpaths.add(backup_rel)
            assignments.append((image, backup_rel, session.session_folder / backup_rel))

        moved: List[MoveOutcome] = []
        failed: List[MoveOutcome] = []
        total_bytes = 0
        total = len(assignments)
        completed = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_assignment = {
                executor.submit(self._move_one_file, image, dest_path): (image, backup_rel, dest_path)
                for image, backup_rel, dest_path in assignments
            }
            for future in as_completed(future_to_assignment):
                image, backup_rel, dest_path = future_to_assignment[future]
                completed += 1
                try:
                    future.result()
                    manifest_rows.append(
                        BackupManifestEntry(
                            original_relative_path=image.relative_path,
                            backup_relative_path=backup_rel,
                            file_size=image.size_bytes,
                            moved_at=datetime.now(),
                            status="Moved",
                        )
                    )
                    moved.append(MoveOutcome(image=image, success=True, backup_path=dest_path, error=None))
                    total_bytes += image.size_bytes
                except (OSError, PermissionError, shutil.Error) as exc:
                    logger.warning("Failed to move %s to backup: %s", image.relative_path, exc)
                    failed.append(
                        MoveOutcome(image=image, success=False, backup_path=None, error=str(exc))
                    )

                if progress_callback:
                    progress_callback(completed, total, image.relative_path)

        self._write_manifest(session.manifest_path, manifest_rows)
        logger.info(
            "Backup complete: %d moved, %d failed, %s total, session=%s",
            len(moved),
            len(failed),
            human_readable_size(total_bytes),
            session.date_label,
        )
        return BackupRunResult(session=session, moved=moved, failed=failed, total_bytes_moved=total_bytes)

    @staticmethod
    def _move_one_file(image: ImageFile, dest_path: Path) -> None:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(image.absolute_path), str(dest_path))

    def _get_or_create_session(self) -> BackupSession:
        date_label = datetime.now().strftime("%Y-%m-%d")
        session_folder = self.backup_root / date_label
        session_folder.mkdir(parents=True, exist_ok=True)
        return BackupSession(
            session_folder=session_folder,
            manifest_path=session_folder / MANIFEST_FILENAME,
            date_label=date_label,
        )

    @staticmethod
    def _resolve_unique_backup_relpath(original_rel: str, used: Set[str]) -> str:
        if original_rel not in used:
            return original_rel
        path = PurePosixPath(original_rel)
        stem, suffix, parent = path.stem, path.suffix, path.parent
        counter = 1
        while True:
            name = f"{stem} ({counter}){suffix}"
            candidate = str(parent / name) if str(parent) != "." else name
            if candidate not in used:
                return candidate
            counter += 1

    # ------------------------------------------------------------------ #
    # Restore
    # ------------------------------------------------------------------ #
    def list_backup_entries(self, only_pending: bool = True) -> List[BackupEntry]:
        """Scan every dated session folder under ``backup_root`` for restorable entries.

        Args:
            only_pending: If True (default), already-restored entries are
                excluded from the result.
        """
        entries: List[BackupEntry] = []
        if not self.backup_root.is_dir():
            return entries

        for session_folder in sorted(p for p in self.backup_root.iterdir() if p.is_dir()):
            manifest_path = session_folder / MANIFEST_FILENAME
            if not manifest_path.exists():
                continue
            for row in self._read_manifest(manifest_path):
                if only_pending and row.status == "Restored":
                    continue
                entries.append(
                    BackupEntry(
                        session_date=session_folder.name,
                        original_relative_path=row.original_relative_path,
                        backup_absolute_path=session_folder / row.backup_relative_path,
                        file_size=row.file_size,
                        moved_at=row.moved_at,
                        manifest_path=manifest_path,
                    )
                )
        return entries

    def restore_all(
        self, website_root: Path, progress_callback: Optional[ProgressCallback] = None
    ) -> RestoreRunResult:
        """Restore every pending backup entry back into ``website_root``."""
        return self.restore_entries(self.list_backup_entries(only_pending=True), website_root, progress_callback)

    def restore_entries(
        self,
        entries: List[BackupEntry],
        website_root: Path,
        progress_callback: Optional[ProgressCallback] = None,
        max_workers: int = DEFAULT_BACKUP_WORKERS,
    ) -> RestoreRunResult:
        """Restore a caller-selected list of backup entries into ``website_root``.

        Never overwrites an existing file at the destination — if the
        website already has something at that path, the restore for that
        entry is marked failed rather than risking data loss, and the user
        can resolve the conflict manually. The existence check and the move
        happen together inside the worker for a given entry, so this safety
        guarantee holds even when entries are restored concurrently.
        """
        website_root = Path(website_root)
        restored: List[RestoreOutcome] = []
        failed: List[RestoreOutcome] = []

        entries_by_manifest: Dict[Path, List[BackupEntry]] = {}
        for entry in entries:
            entries_by_manifest.setdefault(entry.manifest_path, []).append(entry)

        manifest_rows_by_path = {
            manifest_path: self._read_manifest(manifest_path) for manifest_path in entries_by_manifest
        }
        rows_by_key_by_manifest = {
            manifest_path: {(r.original_relative_path, r.backup_relative_path): r for r in rows}
            for manifest_path, rows in manifest_rows_by_path.items()
        }

        tasks = [(entry, website_root / entry.original_relative_path) for entry in entries]
        total = len(tasks)
        completed = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task = {
                executor.submit(self._restore_one_entry, entry, dest_path): (entry, dest_path)
                for entry, dest_path in tasks
            }
            for future in as_completed(future_to_task):
                entry, dest_path = future_to_task[future]
                completed += 1
                try:
                    future.result()
                    restored.append(
                        RestoreOutcome(entry=entry, success=True, restored_path=dest_path, error=None)
                    )
                    session_folder = entry.manifest_path.parent
                    backup_rel = entry.backup_absolute_path.relative_to(session_folder).as_posix()
                    row = rows_by_key_by_manifest[entry.manifest_path].get(
                        (entry.original_relative_path, backup_rel)
                    )
                    if row:
                        row.status = "Restored"
                        row.restored_at = datetime.now()
                except (OSError, PermissionError, shutil.Error) as exc:
                    logger.warning("Failed to restore %s: %s", entry.original_relative_path, exc)
                    failed.append(
                        RestoreOutcome(entry=entry, success=False, restored_path=None, error=str(exc))
                    )

                if progress_callback:
                    progress_callback(completed, total, entry.original_relative_path)

        for manifest_path, rows in manifest_rows_by_path.items():
            self._write_manifest(manifest_path, rows)

        logger.info("Restore complete: %d restored, %d failed.", len(restored), len(failed))
        return RestoreRunResult(restored=restored, failed=failed)

    @staticmethod
    def _restore_one_entry(entry: BackupEntry, dest_path: Path) -> None:
        if dest_path.exists():
            raise FileExistsError(f"Destination already exists: {dest_path}")
        if not entry.backup_absolute_path.exists():
            raise FileNotFoundError(f"Backup file missing: {entry.backup_absolute_path}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry.backup_absolute_path), str(dest_path))

    # ------------------------------------------------------------------ #
    # Manifest persistence
    # ------------------------------------------------------------------ #
    @staticmethod
    def _read_manifest(manifest_path: Path) -> List[BackupManifestEntry]:
        if not manifest_path.exists():
            return []
        entries: List[BackupManifestEntry] = []
        try:
            with manifest_path.open("r", newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    entries.append(
                        BackupManifestEntry(
                            original_relative_path=row["Original Path"],
                            backup_relative_path=row["Backup Path"],
                            file_size=int(row.get("File Size (Bytes)") or 0),
                            moved_at=_parse_date(row.get("Moved At")) or datetime.now(),
                            status=row.get("Status") or "Moved",
                            restored_at=_parse_date(row.get("Restored At")),
                        )
                    )
        except (OSError, csv.Error, ValueError) as exc:
            logger.warning("Could not read backup manifest %s: %s", manifest_path, exc)
        return entries

    @staticmethod
    def _write_manifest(manifest_path: Path, entries: List[BackupManifestEntry]) -> None:
        try:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            with manifest_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=_MANIFEST_FIELDNAMES)
                writer.writeheader()
                for entry in entries:
                    writer.writerow(
                        {
                            "Original Path": entry.original_relative_path,
                            "Backup Path": entry.backup_relative_path,
                            "File Size (Bytes)": entry.file_size,
                            "Moved At": entry.moved_at.strftime(DATE_FORMAT),
                            "Status": entry.status,
                            "Restored At": entry.restored_at.strftime(DATE_FORMAT) if entry.restored_at else "",
                        }
                    )
        except OSError as exc:
            logger.error("Failed to write backup manifest %s: %s", manifest_path, exc)
            raise


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, DATE_FORMAT)
    except ValueError:
        return None
