from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..utils.logger import logger

DEFAULT_MAX_STORAGE_BYTES = 10 * 1024**3  # 10 GiB
DEFAULT_MIN_FREE_BYTES = 3 * 1024**3  # 3 GiB
DEFAULT_RETENTION_SECONDS = 24 * 3600  # 24 hours
ORPHAN_GRACE_SECONDS = 300


class InsufficientStorageError(RuntimeError):
    """Raised when storage limits cannot be satisfied even after cleanup."""


@dataclass(frozen=True)
class DiskStats:
    used_bytes: int
    free_bytes: int
    total_bytes: int
    max_storage_bytes: int
    min_free_bytes: int

    def to_dict(self) -> dict[str, int]:
        return {
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "total_bytes": self.total_bytes,
            "max_storage_bytes": self.max_storage_bytes,
            "min_free_bytes": self.min_free_bytes,
        }


class StorageManager:
    """Manages task persistence, 24h retention, and 10 GiB / 3 GiB free disk guards."""

    def __init__(
        self,
        recordings_root: Path,
        state_file: Path,
        max_storage_bytes: int = DEFAULT_MAX_STORAGE_BYTES,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
    ) -> None:
        self.recordings_root = Path(recordings_root)
        self.state_file = Path(state_file)
        self.max_storage_bytes = max_storage_bytes
        self.min_free_bytes = min_free_bytes
        self.retention_seconds = retention_seconds
        self._io_lock = threading.Lock()

        self.recordings_root.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def dir_size(path: Path) -> int:
        """Compute total byte size of regular files under ``path`` safely."""
        if not path.exists():
            return 0
        total = 0
        for root, _, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
        return total

    def disk_stats(self) -> DiskStats:
        """Return current recordings usage and underlying filesystem free space."""
        self.recordings_root.mkdir(parents=True, exist_ok=True)
        du = shutil.disk_usage(self.recordings_root)
        return DiskStats(
            used_bytes=self.dir_size(self.recordings_root),
            free_bytes=du.free,
            total_bytes=du.total,
            max_storage_bytes=self.max_storage_bytes,
            min_free_bytes=self.min_free_bytes,
        )

    def load_state(self) -> dict[str, dict[str, Any]]:
        """Load persisted task metadata dictionary from disk."""
        import json

        if not self.state_file.is_file():
            return {}
        try:
            with self.state_file.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.error(f"Failed to load remote task state '{self.state_file}': {exc}")
        return {}

    def save_state(self, serialized_tasks: dict[str, dict[str, Any]]) -> None:
        """Atomically persist serialized task metadata to disk."""
        import json

        payload = json.dumps(serialized_tasks, ensure_ascii=False, indent=2)
        with self._io_lock:
            fd, tmp_path = tempfile.mkstemp(
                suffix=".tmp",
                prefix=".tasks_",
                dir=str(self.state_file.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    fp.write(payload)
                os.replace(tmp_path, self.state_file)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

    def delete_task_dir(self, task_id: str) -> None:
        """Delete a task's output directory if present inside ``recordings_root``."""
        target = (self.recordings_root / task_id).resolve()
        root = self.recordings_root.resolve()
        if target == root or not target.is_relative_to(root):
            return
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)

    def cleanup_expired_and_orphans(
        self,
        tasks_map: dict[str, Any],
        is_active_fn: Callable[[str], bool],
        now: float | None = None,
    ) -> list[str]:
        """Remove tasks past ``expires_at`` and orphaned directories on disk."""
        current = now if now is not None else time.time()
        removed: list[str] = []

        for task_id, task in list(tasks_map.items()):
            if is_active_fn(task_id):
                continue
            expires_at = getattr(task, "expires_at", None)
            if expires_at is not None and expires_at <= current:
                self.delete_task_dir(task_id)
                tasks_map.pop(task_id, None)
                removed.append(task_id)

        if self.recordings_root.is_dir():
            for entry in self.recordings_root.iterdir():
                if not entry.is_dir() or entry.name in tasks_map:
                    continue
                try:
                    age = current - entry.stat().st_mtime
                except OSError:
                    continue
                if age >= ORPHAN_GRACE_SECONDS:
                    shutil.rmtree(entry, ignore_errors=True)
                    logger.info(f"Removed orphaned recording directory: {entry}")

        if removed:
            logger.info(f"Expired tasks cleaned up ({len(removed)}): {removed}")
        return removed

    def evict_oldest_finished(
        self,
        tasks_map: dict[str, Any],
        is_active_fn: Callable[[str], bool],
        target_used_bytes: int | None = None,
        min_free_bytes: int | None = None,
    ) -> list[str]:
        """Delete oldest non-active tasks until storage and free-space caps are satisfied."""
        cap = self.max_storage_bytes if target_used_bytes is None else target_used_bytes
        min_free = self.min_free_bytes if min_free_bytes is None else min_free_bytes

        stats = self.disk_stats()
        if stats.used_bytes < cap and stats.free_bytes >= min_free:
            return []

        candidates = [
            task
            for tid, task in tasks_map.items()
            if not is_active_fn(tid)
        ]
        candidates.sort(key=lambda t: (getattr(t, "finished_at", None) or getattr(t, "created_at", 0.0)))

        evicted: list[str] = []
        for task in candidates:
            stats = self.disk_stats()
            if stats.used_bytes < cap and stats.free_bytes >= min_free:
                break
            tid = task.task_id
            self.delete_task_dir(tid)
            tasks_map.pop(tid, None)
            evicted.append(tid)

        if evicted:
            logger.warning(f"Evicted oldest finished tasks due to storage pressure: {evicted}")
        return evicted

    def prepare_for_new_task(
        self,
        tasks_map: dict[str, Any],
        is_active_fn: Callable[[str], bool],
    ) -> DiskStats:
        """Ensure disk usage and free space allow starting a new recording task."""
        self.cleanup_expired_and_orphans(tasks_map, is_active_fn)
        self.evict_oldest_finished(tasks_map, is_active_fn)
        stats = self.disk_stats()
        if stats.used_bytes >= self.max_storage_bytes:
            raise InsufficientStorageError(
                f"Recording storage cap reached ({stats.used_bytes} / {self.max_storage_bytes} bytes)"
            )
        if stats.free_bytes < self.min_free_bytes:
            raise InsufficientStorageError(
                f"Disk free space too low ({stats.free_bytes} < {self.min_free_bytes} bytes)"
            )
        return stats

    def check_runtime_limits(
        self,
        tasks_map: dict[str, Any],
        active_ids: list[str],
        is_active_fn: Callable[[str], bool],
    ) -> tuple[list[str], str | None]:
        """Run periodic cleanup and return ``(task_ids_to_stop, reason)`` if caps are breached."""
        self.cleanup_expired_and_orphans(tasks_map, is_active_fn)
        stats = self.disk_stats()

        if stats.free_bytes < self.min_free_bytes and active_ids:
            reason = (
                f"Stopped by disk guard: free space {stats.free_bytes / (1024**3):.2f} GiB "
                f"< {self.min_free_bytes / (1024**3):.2f} GiB"
            )
            return list(active_ids), reason

        if stats.used_bytes >= self.max_storage_bytes:
            self.evict_oldest_finished(tasks_map, is_active_fn)
            stats = self.disk_stats()
            if stats.used_bytes >= self.max_storage_bytes and active_ids:
                largest_tid = max(
                    active_ids,
                    key=lambda tid: self.dir_size(self.recordings_root / tid),
                )
                reason = (
                    f"Stopped by storage guard: usage {stats.used_bytes / (1024**3):.2f} GiB "
                    f">= {self.max_storage_bytes / (1024**3):.2f} GiB"
                )
                return [largest_tid], reason

        return [], None
