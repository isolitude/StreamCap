from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api.remote_auth import AuthConfig
from app.api.remote_recorder import (
    RecordingTask,
    RemoteRecorderManager,
)
from app.api.remote_server import create_remote_app
from app.api.remote_storage import InsufficientStorageError, StorageManager
from app.core.platforms.platform_handlers import StreamData
from app.core.recording.remote_client import RemoteBackendClient, RemoteBackendConfig
from app.models.recording.recording_model import Recording


class TestRemoteAuth(unittest.TestCase):
    def test_bearer_and_signed_download(self) -> None:
        auth = AuthConfig(api_key="super-secret-key", signing_secret="signing-secret", download_ttl_seconds=300)
        self.assertTrue(auth.verify_bearer("Bearer super-secret-key"))
        self.assertTrue(auth.verify_bearer("bearer super-secret-key"))
        self.assertFalse(auth.verify_bearer("Bearer wrong"))
        self.assertFalse(auth.verify_bearer(None))

        exp, sig = auth.sign_download("task123", "video.ts")
        self.assertTrue(auth.verify_download("task123", "video.ts", exp, sig))
        self.assertFalse(auth.verify_download("task123", "other.ts", exp, sig))
        self.assertFalse(auth.verify_download("task123", "video.ts", exp - 1000, sig))
        self.assertFalse(auth.verify_download("task123", "video.ts", int(time.time()) - 1, sig))


class TestRemoteStorage(unittest.TestCase):
    def test_24h_expiry_and_10g_eviction_and_min_free_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "recs"
            state = root / "tasks_state.json"
            storage = StorageManager(
                recordings_root=root,
                state_file=state,
                max_storage_bytes=1000,
                min_free_bytes=100,
                retention_seconds=24 * 3600,
            )

            now = time.time()
            t_expired = RecordingTask(
                task_id="expired1",
                url="https://example.com/1.m3u8",
                platform="custom",
                platform_key="custom",
                anchor_name="A",
                live_title=None,
                quality="OD",
                record_format="ts",
                segment_record=False,
                segment_time=1800,
                overseas=False,
                status="completed",
                created_at=now - 90000,
                finished_at=now - 87000,
                expires_at=now - 600,
            )
            t_old = RecordingTask(
                task_id="old1",
                url="https://example.com/2.m3u8",
                platform="custom",
                platform_key="custom",
                anchor_name="B",
                live_title=None,
                quality="OD",
                record_format="ts",
                segment_record=False,
                segment_time=1800,
                overseas=False,
                status="completed",
                created_at=now - 5000,
                finished_at=now - 4000,
                expires_at=now + 80000,
            )
            t_active = RecordingTask(
                task_id="active1",
                url="https://example.com/3.m3u8",
                platform="custom",
                platform_key="custom",
                anchor_name="C",
                live_title=None,
                quality="OD",
                record_format="ts",
                segment_record=False,
                segment_time=1800,
                overseas=False,
                status="recording",
                created_at=now - 100,
            )

            for tid, size in [("expired1", 300), ("old1", 600), ("active1", 500)]:
                d = root / tid
                d.mkdir(parents=True, exist_ok=True)
                (d / "clip.ts").write_bytes(b"x" * size)

            tasks = {"expired1": t_expired, "old1": t_old, "active1": t_active}
            is_active = lambda tid: tid == "active1"

            # Expired task is cleaned, then old1 (600) is evicted so used (500) < 1000.
            storage.prepare_for_new_task(tasks, is_active)
            self.assertNotIn("expired1", tasks)
            self.assertNotIn("old1", tasks)
            self.assertIn("active1", tasks)

            # If active task alone exceeds max_storage_bytes, new task is rejected and guard stops it.
            (root / "active1" / "clip.ts").write_bytes(b"x" * 1200)
            with self.assertRaises(InsufficientStorageError):
                storage.prepare_for_new_task(tasks, is_active)

            to_stop, reason = storage.check_runtime_limits(tasks, ["active1"], is_active)
            self.assertEqual(to_stop, ["active1"])
            self.assertIn("storage guard", reason)

            # Min free space breach stops all active tasks.
            storage_low_free = StorageManager(
                recordings_root=root,
                state_file=state,
                max_storage_bytes=10 * 1024**3,
                min_free_bytes=10**18,  # impossibly high so disk free is always below
            )
            to_stop_free, reason_free = storage_low_free.check_runtime_limits(tasks, ["active1"], is_active)
            self.assertEqual(to_stop_free, ["active1"])
            self.assertIn("free space", reason_free)


class TestRemoteServerAPI(unittest.TestCase):
    def test_full_api_lifecycle_and_range_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            rec_dir = Path(tmp) / "recs"
            (run_dir / "config").mkdir(parents=True, exist_ok=True)
            auth = AuthConfig(api_key="test-token-123", signing_secret="test-sign-456", download_ttl_seconds=3600)

            app = create_remote_app(
                run_path=run_dir,
                recordings_root=rec_dir,
                auth_config=auth,
                max_storage_bytes=100 * 1024 * 1024,
                min_free_bytes=1024,
                retention_seconds=24 * 3600,
                max_concurrent_tasks=2,
                nginx_accel_prefix="",
                public_base_url="",
            )

            headers = {"Authorization": "Bearer test-token-123"}

            with TestClient(app) as client:
                # Health open, system requires auth
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/api/system").status_code, 401)
                self.assertEqual(client.get("/api/system", headers=headers).status_code, 200)

                # Offline stream returns 409
                offline_stream = StreamData(platform="Custom", anchor_name="TestRoom", is_live=False)
                with patch.object(
                    RemoteRecorderManager,
                    "resolve_live_stream",
                    new=AsyncMock(return_value=("Custom", "custom", offline_stream, None)),
                ):
                    r_off = client.post("/api/tasks", json={"url": "https://example.com/live.m3u8"}, headers=headers)
                    self.assertEqual(r_off.status_code, 409)

                # Online stream spawns command; mock builder command with tiny python writer script
                live_stream = StreamData(
                    platform="Custom",
                    anchor_name="StreamerA",
                    title="LiveShow",
                    is_live=True,
                    record_url="https://example.com/live.m3u8",
                )

                def fake_build_cmd(self_mgr, task, stream_info, record_url, proxy):
                    script = (
                        "import pathlib, time, sys; "
                        "p = pathlib.Path(sys.argv[1]); "
                        "p.write_bytes(b'0123456789ABCDEF'); "
                        "time.sleep(0.2)"
                    )
                    return [sys.executable, "-c", script, task.save_path]

                with (
                    patch.object(
                        RemoteRecorderManager,
                        "resolve_live_stream",
                        new=AsyncMock(return_value=("Custom", "custom", live_stream, None)),
                    ),
                    patch.object(RemoteRecorderManager, "_build_ffmpeg_command", new=fake_build_cmd),
                ):
                    r_create = client.post(
                        "/api/tasks",
                        json={"url": "https://example.com/live.m3u8", "format": "ts"},
                        headers=headers,
                    )
                    self.assertEqual(r_create.status_code, 201, r_create.text)
                    task_id = r_create.json()["task_id"]

                # Wait briefly for python dummy recorder to finish
                task_data = None
                for _ in range(30):
                    r_get = client.get(f"/api/tasks/{task_id}", headers=headers)
                    self.assertEqual(r_get.status_code, 200)
                    task_data = r_get.json()
                    if task_data["status"] == "completed":
                        break
                    time.sleep(0.1)

                self.assertIsNotNone(task_data)
                self.assertEqual(task_data["status"], "completed")
                self.assertIsNotNone(task_data["expires_at"])
                self.assertEqual(len(task_data["files"]), 1)

                file_info = task_data["files"][0]
                self.assertEqual(file_info["size_bytes"], 16)

                # Download using signed URL (no Bearer header)
                r_dl = client.get(file_info["download_path"])
                self.assertEqual(r_dl.status_code, 200)
                self.assertEqual(r_dl.content, b"0123456789ABCDEF")

                # Range download using Bearer auth
                r_range = client.get(
                    file_info["api_path"],
                    headers={**headers, "Range": "bytes=4-9"},
                )
                self.assertEqual(r_range.status_code, 206)
                self.assertEqual(r_range.content, b"456789")
                self.assertEqual(r_range.headers["Content-Range"], "bytes 4-9/16")

                # Path traversal blocked
                r_trav = client.get(f"/api/tasks/{task_id}/files/..%2Ftasks_state.json", headers=headers)
                self.assertIn(r_trav.status_code, (400, 404))

                # Delete task
                r_del = client.delete(f"/api/tasks/{task_id}", headers=headers)
                self.assertEqual(r_del.status_code, 200)
                self.assertEqual(client.get(f"/api/tasks/{task_id}", headers=headers).status_code, 404)

    def test_remote_backend_config_and_recording_model_serialization(self) -> None:
        cfg = RemoteBackendConfig.from_user_config(
            {
                "remote_backend_enabled": True,
                "remote_backend_url": "https://streamcap.jp.insolitude.me/",
                "remote_backend_api_key": "abc123",
                "remote_backend_auto_download": False,
            }
        )
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.base_url, "https://streamcap.jp.insolitude.me")
        self.assertEqual(cfg.api_key, "abc123")
        self.assertFalse(cfg.auto_download)
        self.assertTrue(cfg.is_configured)

        rec = Recording(
            rec_id="r1",
            url="https://live.bilibili.com/1",
            streamer_name="Tester",
            record_format="TS",
            quality="OD",
            segment_record=False,
            segment_time="1800",
            monitor_status=True,
            scheduled_recording=False,
            scheduled_start_time="",
            monitor_hours="",
            recording_dir=None,
            enabled_message_push=False,
            only_notify_no_record=False,
            flv_use_direct_download=False,
            recording_backend="remote",
        )
        restored = Recording.from_dict(rec.to_dict())
        self.assertEqual(restored.recording_backend, "remote")


if __name__ == "__main__":
    unittest.main()
