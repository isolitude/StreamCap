from __future__ import annotations

import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dotenv import find_dotenv, load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .remote_auth import AuthConfig
from .remote_recorder import (
    ConcurrencyLimitError,
    RecorderConfigError,
    RecordingTask,
    RemoteRecorderManager,
    StreamOfflineError,
    TaskConflictError,
    TaskNotFoundError,
)
from .remote_storage import (
    DEFAULT_MAX_STORAGE_BYTES,
    DEFAULT_MIN_FREE_BYTES,
    DEFAULT_RETENTION_SECONDS,
    InsufficientStorageError,
    StorageManager,
)
from .video_stream_utils import (
    InvalidByteRangeError,
    InvalidVideoPathError,
    file_sender_range,
    parse_range_header,
)

load_dotenv(find_dotenv())

MEDIA_TYPE_OVERRIDES = {
    ".ts": "video/mp2t",
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".flv": "video/x-flv",
    ".mov": "video/quicktime",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
}


class CreateTaskRequest(BaseModel):
    url: str = Field(..., min_length=5, max_length=2048)
    quality: str = Field(default="OD")
    format: str = Field(default="ts")
    segment_record: bool = Field(default=False)
    segment_time: int = Field(default=1800, ge=30, le=86400)
    overseas: bool = Field(default=False)
    proxy: str | None = Field(default=None)
    flv_direct_download: bool = Field(default=False)


class UpdateCookiesRequest(BaseModel):
    cookies: dict[str, str]


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _build_content_disposition(filename: str) -> str:
    ascii_fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in {'"', "\\"} else "_" for ch in filename)
    ascii_fallback = ascii_fallback or "recording.ts"
    return f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{quote(filename)}'


def _guess_media_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in MEDIA_TYPE_OVERRIDES:
        return MEDIA_TYPE_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _serialize_task(
    task: RecordingTask,
    manager: RemoteRecorderManager,
    auth: AuthConfig,
    request: Request,
    public_base_url: str,
) -> dict[str, Any]:
    files_payload: list[dict[str, Any]] = []
    base = public_base_url.rstrip("/") if public_base_url else str(request.base_url).rstrip("/")

    for item in manager.list_task_files(task.task_id):
        filename = item["filename"]
        exp, sig = auth.sign_download(task.task_id, filename)
        rel_path = f"/api/tasks/{task.task_id}/files/{quote(filename)}"
        signed_path = f"{rel_path}?expires={exp}&sig={sig}"
        files_payload.append(
            {
                **item,
                "download_path": signed_path,
                "download_url": f"{base}{signed_path}",
                "api_path": rel_path,
                "expires_at": exp,
            }
        )

    data = task.to_dict()
    data["files"] = files_payload
    return data


def create_remote_app(
    run_path: str | Path | None = None,
    recordings_root: Path | None = None,
    auth_config: AuthConfig | None = None,
    max_storage_bytes: int | None = None,
    min_free_bytes: int | None = None,
    retention_seconds: int | None = None,
    max_concurrent_tasks: int | None = None,
    nginx_accel_prefix: str | None = None,
    public_base_url: str | None = None,
) -> FastAPI:
    """Create and configure the headless StreamCap FastAPI application."""
    default_root = Path(__file__).resolve().parents[2]
    resolved_run_path = (
        Path(run_path)
        if run_path is not None
        else Path(os.getenv("STREAMCAP_RUN_DIR") or default_root)
    )
    resolved_recordings = (
        Path(recordings_root)
        if recordings_root is not None
        else Path(os.getenv("STREAMCAP_RECORDINGS_DIR") or (resolved_run_path / "downloads" / "remote_tasks"))
    )
    state_file = resolved_recordings / "tasks_state.json"

    max_bytes = (
        max_storage_bytes
        if max_storage_bytes is not None
        else _int_env("STREAMCAP_MAX_STORAGE_BYTES", DEFAULT_MAX_STORAGE_BYTES)
    )
    min_free = (
        min_free_bytes
        if min_free_bytes is not None
        else _int_env("STREAMCAP_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES)
    )
    retention = (
        retention_seconds
        if retention_seconds is not None
        else _int_env("STREAMCAP_RETENTION_SECONDS", DEFAULT_RETENTION_SECONDS)
    )
    concurrency = (
        max_concurrent_tasks
        if max_concurrent_tasks is not None
        else _int_env("STREAMCAP_MAX_CONCURRENT_TASKS", 4)
    )
    resolved_accel_prefix = (
        nginx_accel_prefix
        if nginx_accel_prefix is not None
        else os.getenv("STREAMCAP_NGINX_ACCEL_PREFIX", "")
    ).strip().rstrip("/")
    resolved_public_base = (
        public_base_url
        if public_base_url is not None
        else os.getenv("STREAMCAP_PUBLIC_BASE_URL", "")
    ).strip()

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        auth = auth_config or AuthConfig.from_env()
        storage = StorageManager(
            recordings_root=resolved_recordings,
            state_file=state_file,
            max_storage_bytes=max_bytes,
            min_free_bytes=min_free,
            retention_seconds=retention,
        )
        manager = RemoteRecorderManager(
            run_path=resolved_run_path,
            storage=storage,
            max_concurrent_tasks=concurrency,
        )
        app_instance.state.auth = auth
        app_instance.state.storage = storage
        app_instance.state.manager = manager
        await manager.start_guard_loop()
        try:
            yield
        finally:
            await manager.shutdown()

    api = FastAPI(title="StreamCap Headless Recorder", version="1.0.0", lifespan=lifespan)

    def require_bearer(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> AuthConfig:
        auth: AuthConfig = request.app.state.auth
        if not auth.verify_bearer(authorization):
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return auth

    def get_manager(request: Request) -> RemoteRecorderManager:
        return request.app.state.manager

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/api/system", dependencies=[Depends(require_bearer)])
    async def system_status(request: Request) -> dict[str, Any]:
        manager: RemoteRecorderManager = request.app.state.manager
        storage: StorageManager = request.app.state.storage
        return {
            "disk": storage.disk_stats().to_dict(),
            "active_tasks": len(manager.active_task_ids()),
            "total_tasks": len(manager.tasks),
            "max_concurrent_tasks": manager.max_concurrent_tasks,
            "retention_seconds": storage.retention_seconds,
        }

    @api.post("/api/tasks", status_code=201, dependencies=[Depends(require_bearer)])
    async def create_task(body: CreateTaskRequest, request: Request) -> dict[str, Any]:
        manager = get_manager(request)
        auth: AuthConfig = request.app.state.auth
        try:
            task = await manager.create_task(
                url=body.url,
                quality=body.quality,
                record_format=body.format,
                segment_record=body.segment_record,
                segment_time=body.segment_time,
                overseas=body.overseas,
                proxy=body.proxy,
                flv_direct_download=body.flv_direct_download,
            )
        except InsufficientStorageError as exc:
            raise HTTPException(status_code=507, detail=str(exc)) from exc
        except ConcurrencyLimitError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except RecorderConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except StreamOfflineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        return _serialize_task(task, manager, auth, request, resolved_public_base)

    @api.get("/api/tasks", dependencies=[Depends(require_bearer)])
    async def list_tasks(request: Request) -> dict[str, Any]:
        manager = get_manager(request)
        auth: AuthConfig = request.app.state.auth
        return {
            "tasks": [
                _serialize_task(task, manager, auth, request, public_base_url)
                for task in manager.list_tasks()
            ]
        }

    @api.get("/api/tasks/{task_id}", dependencies=[Depends(require_bearer)])
    async def get_task(task_id: str, request: Request) -> dict[str, Any]:
        manager = get_manager(request)
        auth: AuthConfig = request.app.state.auth
        try:
            task = manager.get_task(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        return _serialize_task(task, manager, auth, request, resolved_public_base)

    @api.post("/api/tasks/{task_id}/stop", dependencies=[Depends(require_bearer)])
    async def stop_task(task_id: str, request: Request) -> dict[str, Any]:
        manager = get_manager(request)
        auth: AuthConfig = request.app.state.auth
        try:
            task = await manager.stop_task(task_id, reason="Stopped via API")
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        return _serialize_task(task, manager, auth, request, resolved_public_base)

    @api.delete("/api/tasks/{task_id}", dependencies=[Depends(require_bearer)])
    async def delete_task(
        task_id: str,
        request: Request,
        force: bool = Query(default=False),
    ) -> dict[str, str]:
        manager = get_manager(request)
        try:
            await manager.delete_task(task_id, force=force)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        except TaskConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"deleted": task_id}

    @api.get("/api/tasks/{task_id}/files/{filename}")
    async def download_task_file(
        task_id: str,
        filename: str,
        request: Request,
        authorization: str | None = Header(default=None),
        expires: int | None = Query(default=None),
        sig: str | None = Query(default=None),
    ):
        auth: AuthConfig = request.app.state.auth
        if not auth.verify_bearer(authorization) and not auth.verify_download(task_id, filename, expires, sig):
            raise HTTPException(status_code=401, detail="Unauthorized")

        manager = get_manager(request)
        try:
            file_path = manager.resolve_task_file(task_id, filename)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        except InvalidVideoPathError as exc:
            raise HTTPException(status_code=400, detail="Invalid file path") from exc

        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        media_type = _guess_media_type(file_path)
        disposition = _build_content_disposition(file_path.name)

        if resolved_accel_prefix:
            accel_uri = quote(f"{resolved_accel_prefix}/{task_id}/{file_path.name}", safe="/")
            return Response(
                status_code=200,
                headers={
                    "X-Accel-Redirect": accel_uri,
                    "Content-Type": media_type,
                    "Content-Disposition": disposition,
                },
            )

        file_size = file_path.stat().st_size
        range_header = request.headers.get("Range")
        if range_header:
            try:
                start, end = parse_range_header(range_header, file_size)
            except InvalidByteRangeError as exc:
                raise HTTPException(
                    status_code=416,
                    detail="Requested range not satisfiable",
                    headers={"Content-Range": f"bytes */{file_size}"},
                ) from exc

            return StreamingResponse(
                file_sender_range(file_path, start, end),
                status_code=206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(end - start + 1),
                    "Content-Type": media_type,
                    "Content-Disposition": disposition,
                },
            )

        if file_size == 0:
            return Response(
                content=b"",
                status_code=200,
                headers={
                    "Content-Length": "0",
                    "Accept-Ranges": "bytes",
                    "Content-Type": media_type,
                    "Content-Disposition": disposition,
                },
            )

        return StreamingResponse(
            file_sender_range(file_path, 0, file_size - 1),
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "Content-Type": media_type,
                "Content-Disposition": disposition,
            },
        )

    @api.get("/api/config/cookies", dependencies=[Depends(require_bearer)])
    async def get_cookies_summary(request: Request) -> dict[str, Any]:
        manager = get_manager(request)
        cookies = manager.config_manager.load_cookies_config() or {}
        return {
            "platforms": {
                key: {"configured": bool(val), "length": len(str(val or ""))}
                for key, val in cookies.items()
            }
        }

    @api.put("/api/config/cookies", dependencies=[Depends(require_bearer)])
    async def update_cookies(body: UpdateCookiesRequest, request: Request) -> dict[str, Any]:
        manager = get_manager(request)
        current = manager.config_manager.load_cookies_config() or {}
        for platform_key, cookie_val in body.cookies.items():
            if cookie_val:
                current[platform_key] = cookie_val
            else:
                current.pop(platform_key, None)
        await manager.config_manager.save_cookies_config(current)
        return {"updated_platforms": sorted(body.cookies.keys())}

    return api


def __getattr__(name: str):
    if name == "app":
        return create_remote_app()
    raise AttributeError(name)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("STREAMCAP_HOST", "127.0.0.1")
    port = _int_env("STREAMCAP_PORT", 8000)
    uvicorn.run(create_remote_app(), host=host, port=port, log_level="info")
