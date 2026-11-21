from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_REMOTE_URL = "https://streamcap.jp.insolitude.me"


class RemoteBackendError(RuntimeError):
    """Raised when the remote recording backend returns an HTTP or network error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class RemoteBackendConfig:
    enabled: bool
    base_url: str
    api_key: str
    auto_download: bool

    @classmethod
    def from_user_config(cls, user_config: dict[str, Any] | None) -> RemoteBackendConfig:
        cfg = user_config or {}
        env_enabled = os.getenv("STREAMCAP_REMOTE_ENABLED", "").strip().lower()
        enabled_val = cfg.get("remote_backend_enabled")
        if enabled_val is None and env_enabled:
            enabled = env_enabled in {"1", "true", "yes", "on"}
        else:
            enabled = bool(enabled_val)

        raw_url = str(cfg.get("remote_backend_url") or os.getenv("STREAMCAP_REMOTE_URL") or DEFAULT_REMOTE_URL).strip()
        base_url = raw_url.rstrip("/")

        api_key = str(cfg.get("remote_backend_api_key") or os.getenv("STREAMCAP_API_KEY") or "").strip()

        auto_val = cfg.get("remote_backend_auto_download")
        if auto_val is None:
            env_auto = os.getenv("STREAMCAP_REMOTE_AUTO_DOWNLOAD", "true").strip().lower()
            auto_download = env_auto not in {"0", "false", "no", "off"}
        else:
            auto_download = bool(auto_val)

        return cls(
            enabled=enabled,
            base_url=base_url,
            api_key=api_key,
            auto_download=auto_download,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key)


class RemoteBackendClient:
    """Async HTTP client for the headless StreamCap remote recording server."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.base_url:
            raise RemoteBackendError("Remote backend URL is not configured")
        if not self.api_key:
            raise RemoteBackendError("Remote backend API key is not configured")

        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
                resp = await client.request(
                    method,
                    path,
                    headers=self._headers(),
                    json=json_body,
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise RemoteBackendError(f"Network error contacting remote backend: {exc}") from exc

        if resp.status_code >= 400:
            detail = resp.text
            try:
                payload = resp.json()
                detail = payload.get("detail") or detail
            except Exception:
                pass
            raise RemoteBackendError(str(detail), status_code=resp.status_code)

        return resp.json() if resp.content else {}

    async def get_system(self) -> dict[str, Any]:
        return await self._request("GET", "/api/system")

    async def create_task(
        self,
        url: str,
        quality: str = "OD",
        fmt: str = "ts",
        segment_record: bool = False,
        segment_time: int = 1800,
        overseas: bool = False,
        flv_direct_download: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "url": url,
            "quality": quality,
            "format": fmt,
            "segment_record": segment_record,
            "segment_time": max(30, int(segment_time)),
            "overseas": overseas,
            "flv_direct_download": flv_direct_download,
        }
        return await self._request("POST", "/api/tasks", json_body=payload)

    async def list_tasks(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/tasks")
        return list(data.get("tasks") or [])

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/tasks/{task_id}")

    async def stop_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/tasks/{task_id}/stop")

    async def delete_task(self, task_id: str, force: bool = False) -> dict[str, Any]:
        return await self._request("DELETE", f"/api/tasks/{task_id}", params={"force": str(force).lower()})

    async def sync_cookies(self, cookies: dict[str, str]) -> dict[str, Any]:
        cleaned = {str(k): str(v or "") for k, v in cookies.items() if v}
        return await self._request("PUT", "/api/config/cookies", json_body={"cookies": cleaned})

    async def download_file(
        self,
        download_path_or_url: str,
        dest_path: Path,
        expected_size: int | None = None,
        progress_cb: Callable[[int, int | None], None] | None = None,
    ) -> Path:
        """Download a file from the remote backend with HTTP Range resume."""
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = dest_path.with_name(dest_path.name + ".part")

        if dest_path.is_file() and expected_size is not None and dest_path.stat().st_size == expected_size:
            return dest_path

        existing_size = part_path.stat().st_size if part_path.is_file() else 0
        if expected_size is not None and existing_size > expected_size:
            part_path.unlink(missing_ok=True)
            existing_size = 0

        url = (
            download_path_or_url
            if download_path_or_url.startswith(("http://", "https://"))
            else f"{self.base_url}{download_path_or_url}"
        )

        headers = self._headers()
        mode = "wb"
        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"
            mode = "ab"

        try:
            async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code == 416 and expected_size is not None and existing_size == expected_size:
                        os.replace(part_path, dest_path)
                        return dest_path
                    if resp.status_code == 200 and existing_size > 0:
                        mode = "wb"
                        existing_size = 0
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        raise RemoteBackendError(
                            f"Download failed [{resp.status_code}]: {body.decode(errors='replace')}",
                            status_code=resp.status_code,
                        )

                    written = existing_size
                    with part_path.open(mode) as fp:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            fp.write(chunk)
                            written += len(chunk)
                            if progress_cb is not None:
                                progress_cb(written, expected_size)
        except httpx.HTTPError as exc:
            raise RemoteBackendError(f"Download network error: {exc}") from exc

        if expected_size is not None and written != expected_size:
            raise RemoteBackendError(
                f"Incomplete download for '{dest_path.name}': got {written} of {expected_size} bytes"
            )

        os.replace(part_path, dest_path)
        return dest_path
