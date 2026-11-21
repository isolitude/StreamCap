from __future__ import annotations

import asyncio
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..core.config.config_manager import ConfigManager
from ..core.media import ffmpeg_builders
from ..core.media.direct_downloader import DirectStreamDownloader
from ..core.platforms import platform_handlers
from ..core.platforms.platform_handlers import StreamData
from ..core.recording.stream_manager import LiveStreamRecorder
from ..models.media.audio_format_model import AudioFormat
from ..models.media.video_quality_model import VideoQuality
from ..utils import utils
from ..utils.logger import logger
from .remote_storage import InsufficientStorageError, StorageManager
from .video_stream_utils import InvalidVideoPathError, resolve_video_path

SUPPORTED_FORMATS = ("ts", "mp4", "mkv", "flv", "mov", "nut", "mp3", "m4a", "aac", "wav", "wma")
ACTIVE_STATUSES = {"checking", "recording", "stopping"}
TERMINAL_STATUSES = {"completed", "stopped", "error"}


class RecorderConfigError(ValueError):
    """Raised on unsupported platform, format, or quality arguments."""


class StreamOfflineError(RuntimeError):
    """Raised when the requested live room is currently offline."""


class ConcurrencyLimitError(RuntimeError):
    """Raised when maximum concurrent active recordings is reached."""


class TaskNotFoundError(KeyError):
    """Raised when a task ID does not exist."""


class TaskConflictError(RuntimeError):
    """Raised when an operation conflicts with the task's current state."""


@dataclass
class RecordingTask:
    task_id: str
    url: str
    platform: str
    platform_key: str
    anchor_name: str
    live_title: str | None
    quality: str
    record_format: str
    segment_record: bool
    segment_time: int
    overseas: bool
    status: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    expires_at: float | None = None
    error_message: str | None = None
    stop_reason: str | None = None
    save_path: str | None = None
    # Non-persisted runtime handles
    stop_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)
    runner_task: asyncio.Task[None] | None = field(default=None, repr=False, compare=False)
    process: asyncio.subprocess.Process | None = field(default=None, repr=False, compare=False)
    direct_downloader: DirectStreamDownloader | None = field(default=None, repr=False, compare=False)
    stop_requested: bool = field(default=False, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "url": self.url,
            "platform": self.platform,
            "platform_key": self.platform_key,
            "anchor_name": self.anchor_name,
            "live_title": self.live_title,
            "quality": self.quality,
            "record_format": self.record_format,
            "segment_record": self.segment_record,
            "segment_time": self.segment_time,
            "overseas": self.overseas,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "expires_at": self.expires_at,
            "error_message": self.error_message,
            "stop_reason": self.stop_reason,
            "save_path": self.save_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecordingTask:
        return cls(
            task_id=str(data["task_id"]),
            url=str(data["url"]),
            platform=str(data.get("platform") or ""),
            platform_key=str(data.get("platform_key") or ""),
            anchor_name=str(data.get("anchor_name") or "LiveRoom"),
            live_title=data.get("live_title"),
            quality=str(data.get("quality") or VideoQuality.OD),
            record_format=str(data.get("record_format") or "ts"),
            segment_record=bool(data.get("segment_record", False)),
            segment_time=int(data.get("segment_time") or 1800),
            overseas=bool(data.get("overseas", False)),
            status=str(data.get("status") or "stopped"),
            created_at=float(data.get("created_at") or time.time()),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            expires_at=data.get("expires_at"),
            error_message=data.get("error_message"),
            stop_reason=data.get("stop_reason"),
            save_path=data.get("save_path"),
        )


class RemoteRecorderManager:
    """Headless on-demand live stream recorder backed by StreamCap core builders."""

    def __init__(
        self,
        run_path: str | Path,
        storage: StorageManager,
        max_concurrent_tasks: int = 4,
        guard_interval_seconds: int = 15,
    ) -> None:
        self.run_path = str(run_path)
        self.config_manager = ConfigManager(self.run_path)
        self.storage = storage
        self.max_concurrent_tasks = max(1, max_concurrent_tasks)
        self.guard_interval_seconds = max(5, guard_interval_seconds)

        self.tasks: dict[str, RecordingTask] = {}
        self._state_lock = asyncio.Lock()
        self._guard_task: asyncio.Task[None] | None = None

        self._restore_state()

    def _restore_state(self) -> None:
        raw = self.storage.load_state()
        now = time.time()
        changed = False
        for tid, item in raw.items():
            try:
                task = RecordingTask.from_dict(item)
            except Exception as exc:
                logger.warning(f"Skipping corrupted task record '{tid}': {exc}")
                changed = True
                continue
            if task.status in ACTIVE_STATUSES:
                task.status = "stopped"
                task.finished_at = task.finished_at or now
                task.expires_at = task.expires_at or (task.finished_at + self.storage.retention_seconds)
                task.stop_reason = task.stop_reason or "Server restarted while task was active"
                changed = True
            self.tasks[tid] = task

        self.storage.cleanup_expired_and_orphans(self.tasks, self.is_task_active, now=now)
        if changed:
            self._persist_state_sync()

    def _persist_state_sync(self) -> None:
        serialized = {tid: task.to_dict() for tid, task in self.tasks.items()}
        self.storage.save_state(serialized)

    def is_task_active(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if task is None:
            return False
        if task.runner_task is not None and not task.runner_task.done():
            return True
        return task.status in ACTIVE_STATUSES

    def active_task_ids(self) -> list[str]:
        return [tid for tid in self.tasks if self.is_task_active(tid)]

    async def start_guard_loop(self) -> None:
        if self._guard_task is not None and not self._guard_task.done():
            return
        self._guard_task = asyncio.create_task(self._guard_loop())

    async def _guard_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.guard_interval_seconds)
                await self.run_storage_guard()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Storage guard loop error: {exc}")

    async def run_storage_guard(self) -> None:
        async with self._state_lock:
            to_stop, reason = self.storage.check_runtime_limits(
                self.tasks,
                self.active_task_ids(),
                self.is_task_active,
            )
            self._persist_state_sync()

        for tid in to_stop:
            logger.warning(f"Stopping task {tid}: {reason}")
            try:
                await self.stop_task(tid, reason=reason)
            except Exception as exc:
                logger.error(f"Failed stopping task {tid} during guard: {exc}")

    async def shutdown(self) -> None:
        if self._guard_task is not None and not self._guard_task.done():
            self._guard_task.cancel()
            await asyncio.gather(self._guard_task, return_exceptions=True)

        active = [self.tasks[tid] for tid in self.active_task_ids() if tid in self.tasks]
        for task in active:
            task.stop_requested = True
            task.stop_reason = task.stop_reason or "Server shutting down"
            task.stop_event.set()

        runners = [t.runner_task for t in active if t.runner_task is not None]
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)

        async with self._state_lock:
            self._persist_state_sync()

    def _resolve_proxy(self, platform_key: str, override_proxy: str | None) -> str | None:
        if override_proxy:
            return utils.handle_proxy_addr(override_proxy)
        user_cfg = self.config_manager.load_user_config() or {}
        if not user_cfg.get("enable_proxy"):
            return None
        default_proxy_platform = str(user_cfg.get("default_platform_with_proxy", ""))
        proxy_list = default_proxy_platform.replace("，", ",").replace(" ", "").split(",")
        if platform_key in proxy_list:
            return utils.handle_proxy_addr(user_cfg.get("proxy_address"))
        return None

    @staticmethod
    def _select_record_url(stream_info: StreamData, platform_key: str, prefer_https: bool = False) -> str | None:
        url = stream_info.record_url
        if platform_key in {"douyin", "tiktok"} and stream_info.flv_url:
            codec = utils.get_query_params(stream_info.flv_url, "codec")
            if not (codec and codec[0] == "h265"):
                url = stream_info.flv_url

        if not url:
            return None

        if prefer_https and url.startswith("http://") and platform_key not in {"shopee", "migu"}:
            url = url.replace("http://", "https://", 1)
        elif platform_key in {"shopee", "migu"}:
            url = url.replace("https://", "http://", 1)
        return url

    @staticmethod
    def _resolve_format_and_mode(
        requested_format: str,
        stream_info: StreamData,
        platform_key: str,
        flv_direct_download: bool,
    ) -> tuple[str, bool]:
        fmt = requested_format.lower()
        if stream_info.flv_url:
            if platform_key == "shopee" or flv_direct_download:
                return "flv", True
            if fmt == "flv":
                codec = utils.get_query_params(stream_info.flv_url, "codec")
                if codec and codec[0] == "h265":
                    logger.warning("FLV is not supported for h265 codec, falling back to TS")
                    return "ts", False
        return fmt, False

    async def resolve_live_stream(
        self,
        url: str,
        quality: str,
        proxy: str | None = None,
    ) -> tuple[str, str, StreamData, str | None]:
        platform, platform_key = platform_handlers.get_platform_info(url)
        if not platform_key:
            raise RecorderConfigError(f"Unsupported live URL platform: {url}")

        cookies_cfg = self.config_manager.load_cookies_config() or {}
        accounts_cfg = self.config_manager.load_accounts_config() or {}
        resolved_proxy = self._resolve_proxy(platform_key, proxy)
        account = accounts_cfg.get(platform_key, {})

        handler = platform_handlers.get_platform_handler(
            live_url=url,
            proxy=resolved_proxy,
            cookies=cookies_cfg.get(platform_key),
            record_quality=quality,
            platform=platform,
            username=account.get("username"),
            password=account.get("password"),
            account_type=account.get("account_type"),
        )
        if not handler:
            raise RecorderConfigError(f"No handler available for platform URL: {url}")

        stream_info = await handler.get_stream_info(url)
        if not stream_info or not stream_info.is_live:
            raise StreamOfflineError(f"Stream is currently offline: {url}")

        return platform or platform_key, platform_key, stream_info, resolved_proxy

    async def create_task(
        self,
        url: str,
        quality: str = VideoQuality.OD,
        record_format: str = "ts",
        segment_record: bool = False,
        segment_time: int = 1800,
        overseas: bool = False,
        proxy: str | None = None,
        flv_direct_download: bool = False,
    ) -> RecordingTask:
        fmt = record_format.lower().strip()
        if fmt not in SUPPORTED_FORMATS:
            raise RecorderConfigError(f"Unsupported format '{record_format}'. Supported: {', '.join(SUPPORTED_FORMATS)}")

        qual = quality.upper().strip()
        if qual not in VideoQuality.get_qualities():
            raise RecorderConfigError(
                f"Unsupported quality '{quality}'. Supported: {', '.join(VideoQuality.get_qualities())}"
            )

        if segment_time < 30:
            raise RecorderConfigError("segment_time must be at least 30 seconds")

        async with self._state_lock:
            self.storage.prepare_for_new_task(self.tasks, self.is_task_active)
            if len(self.active_task_ids()) >= self.max_concurrent_tasks:
                self._persist_state_sync()
                raise ConcurrencyLimitError(
                    f"Maximum concurrent recordings ({self.max_concurrent_tasks}) reached"
                )

        platform, platform_key, stream_info, resolved_proxy = await self.resolve_live_stream(
            url=url,
            quality=qual,
            proxy=proxy,
        )

        user_cfg = self.config_manager.load_user_config() or {}
        record_url = self._select_record_url(
            stream_info,
            platform_key,
            prefer_https=bool(user_cfg.get("force_https_recording")),
        )
        if not record_url:
            raise StreamOfflineError(f"No playable stream URL returned for: {url}")

        actual_format, use_direct = self._resolve_format_and_mode(
            fmt, stream_info, platform_key, flv_direct_download
        )

        async with self._state_lock:
            # Re-verify limits after network resolution before spawning subprocess.
            self.storage.prepare_for_new_task(self.tasks, self.is_task_active)
            if len(self.active_task_ids()) >= self.max_concurrent_tasks:
                self._persist_state_sync()
                raise ConcurrencyLimitError(
                    f"Maximum concurrent recordings ({self.max_concurrent_tasks}) reached"
                )

            task_id = uuid.uuid4().hex[:12]
            task_dir = self.storage.recordings_root / task_id
            task_dir.mkdir(parents=True, exist_ok=True)

            anchor = utils.clean_name(stream_info.anchor_name, "LiveRoom") or "LiveRoom"
            title = utils.clean_name(stream_info.title, None)
            timestamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
            base_name = "_".join(part for part in (anchor, title[:30] if title else None, timestamp) if part)
            suffix = f"_%03d.{actual_format}" if (segment_record and not use_direct) else f".{actual_format}"
            save_path = str((task_dir / (base_name + suffix).replace(" ", "_")).as_posix())

            now = time.time()
            task = RecordingTask(
                task_id=task_id,
                url=url,
                platform=platform,
                platform_key=platform_key,
                anchor_name=anchor,
                live_title=stream_info.title,
                quality=qual,
                record_format=actual_format,
                segment_record=segment_record and not use_direct,
                segment_time=segment_time,
                overseas=overseas,
                status="recording",
                created_at=now,
                started_at=now,
                save_path=save_path,
            )
            self.tasks[task_id] = task

            if use_direct:
                task.runner_task = asyncio.create_task(
                    self._run_direct_task(task, record_url, resolved_proxy)
                )
            else:
                ffmpeg_cmd = self._build_ffmpeg_command(
                    task=task,
                    stream_info=stream_info,
                    record_url=record_url,
                    proxy=resolved_proxy,
                )
                task.runner_task = asyncio.create_task(self._run_ffmpeg_task(task, ffmpeg_cmd))

            self._persist_state_sync()
            return task

    def _build_ffmpeg_command(
        self,
        task: RecordingTask,
        stream_info: StreamData,
        record_url: str,
        proxy: str | None,
    ) -> list[str]:
        audio_formats = {fmt.lower() for fmt in AudioFormat.get_formats()}
        if task.record_format in audio_formats:
            metadata = {
                "title": stream_info.title,
                "artist": task.anchor_name,
                "album": f"{task.platform} {task.anchor_name}",
                "date": date.today().isoformat(),
                "genre": "Live Stream",
                "comment": task.url,
            }
        else:
            metadata = {
                "title": stream_info.title,
                "artist": task.anchor_name,
                "date": date.today().isoformat(),
                "comment": task.url,
                "network": task.platform,
            }

        builder = ffmpeg_builders.create_builder(
            task.record_format,
            record_url=record_url,
            is_overseas=task.overseas,
            proxy=proxy,
            segment_record=task.segment_record,
            segment_time=str(task.segment_time),
            full_path=task.save_path,
            headers=LiveStreamRecorder.get_headers_params(task.url, task.platform_key),
            platform_key=task.platform_key,
            video_bitrate=None,  # Never transcode on resource-constrained remote hosts.
            metadata=metadata,
        )
        return builder.build_command()

    async def _run_ffmpeg_task(self, task: RecordingTask, ffmpeg_cmd: list[str]) -> None:
        stderr_task: asyncio.Task[bytes] | None = None
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            task.process = process
            stderr_task = asyncio.create_task(LiveStreamRecorder._capture_stream_tail(process.stderr))

            wait_proc = asyncio.create_task(process.wait())
            wait_stop = asyncio.create_task(task.stop_event.wait())
            done, pending = await asyncio.wait(
                {wait_proc, wait_stop},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if wait_stop in done and process.returncode is None:
                await self._terminate_ffmpeg(process)
                await wait_proc
            else:
                wait_stop.cancel()

            stderr_bytes = await stderr_task
            code = process.returncode
            if task.stop_requested:
                task.status = "stopped"
            elif code in {0, 255}:
                task.status = "completed"
            else:
                tail = stderr_bytes.decode(errors="replace").strip()
                last_lines = "\n".join(tail.splitlines()[-5:]) if tail else f"ffmpeg exited with code {code}"
                task.status = "error"
                task.error_message = last_lines
                logger.error(f"Task {task.task_id} ffmpeg failed (code={code}): {last_lines}")
        except FileNotFoundError:
            task.status = "error"
            task.error_message = "ffmpeg executable not found on server"
            logger.error(f"Task {task.task_id}: ffmpeg not found")
        except Exception as exc:
            task.status = "error"
            task.error_message = str(exc)
            logger.error(f"Task {task.task_id} unexpected error: {exc}")
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            if stderr_task is not None and not stderr_task.done():
                await asyncio.gather(stderr_task, return_exceptions=True)
            task.process = None
            await self._finalize_task(task)

    @staticmethod
    async def _terminate_ffmpeg(process: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "nt":
                if process.stdin:
                    process.stdin.write(b"q")
                    await process.stdin.drain()
            else:
                process.send_signal(signal.SIGINT)
            if process.stdin:
                process.stdin.close()
            await asyncio.wait_for(process.wait(), timeout=12.0)
        except asyncio.TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        except ProcessLookupError:
            pass

    async def _run_direct_task(
        self,
        task: RecordingTask,
        record_url: str,
        proxy: str | None,
    ) -> None:
        headers: dict[str, str] = {}
        header_param = LiveStreamRecorder.get_headers_params(task.url, task.platform_key)
        if header_param and ":" in header_param:
            key, val = header_param.split(":", 1)
            headers[key] = val

        downloader = DirectStreamDownloader(
            record_url=record_url,
            save_path=task.save_path or "",
            headers=headers,
            proxy=proxy,
        )
        task.direct_downloader = downloader
        try:
            await downloader.start_download()
            dl_task = downloader.download_task
            if dl_task is None:
                raise RuntimeError("Direct downloader failed to create download task")

            wait_stop = asyncio.create_task(task.stop_event.wait())
            done, _ = await asyncio.wait(
                {dl_task, wait_stop},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wait_stop in done:
                await downloader.stop_download()
            else:
                wait_stop.cancel()

            task.status = "stopped" if task.stop_requested else "completed"
        except Exception as exc:
            task.status = "error"
            task.error_message = str(exc)
            logger.error(f"Task {task.task_id} direct download failed: {exc}")
        finally:
            task.direct_downloader = None
            await self._finalize_task(task)

    async def _finalize_task(self, task: RecordingTask) -> None:
        now = time.time()
        task.finished_at = now
        task.expires_at = now + self.storage.retention_seconds
        async with self._state_lock:
            self._persist_state_sync()
        logger.info(
            f"Task {task.task_id} finished with status={task.status}, expires_at={task.expires_at:.0f}"
        )

    async def stop_task(self, task_id: str, reason: str | None = None) -> RecordingTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if not self.is_task_active(task_id):
            return task

        task.stop_requested = True
        if reason:
            task.stop_reason = reason
        task.status = "stopping"
        task.stop_event.set()
        async with self._state_lock:
            self._persist_state_sync()
        return task

    async def delete_task(self, task_id: str, force: bool = False) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)

        if self.is_task_active(task_id):
            if not force:
                raise TaskConflictError(f"Task '{task_id}' is active; stop it first or pass force=true")
            await self.stop_task(task_id, reason="Deleted with force=true")
            if task.runner_task is not None:
                await asyncio.gather(task.runner_task, return_exceptions=True)

        async with self._state_lock:
            self.storage.delete_task_dir(task_id)
            self.tasks.pop(task_id, None)
            self._persist_state_sync()

    def get_task(self, task_id: str) -> RecordingTask:
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    def list_tasks(self) -> list[RecordingTask]:
        return sorted(self.tasks.values(), key=lambda t: t.created_at, reverse=True)

    def list_task_files(self, task_id: str) -> list[dict[str, Any]]:
        task_dir = self.storage.recordings_root / task_id
        if not task_dir.is_dir():
            return []

        files: list[dict[str, Any]] = []
        for entry in sorted(task_dir.iterdir(), key=lambda p: p.name):
            if not entry.is_file() or entry.name.startswith("."):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            files.append(
                {
                    "filename": entry.name,
                    "size_bytes": stat.st_size,
                    "modified_at": stat.st_mtime,
                }
            )
        return files

    def resolve_task_file(self, task_id: str, filename: str) -> Path:
        if task_id not in self.tasks:
            raise TaskNotFoundError(task_id)
        if "/" in task_id or "\\" in task_id or task_id in {".", ".."}:
            raise InvalidVideoPathError("Invalid task ID")
        return resolve_video_path(self.storage.recordings_root, filename, subfolder=task_id)
