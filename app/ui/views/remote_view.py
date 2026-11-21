from __future__ import annotations

import asyncio
import time
from pathlib import Path

import flet as ft

from ...core.recording.remote_client import RemoteBackendClient, RemoteBackendConfig, RemoteBackendError
from ...models.media.video_quality_model import VideoQuality
from ...utils.logger import logger
from ..base_page import PageBase


class RemotePage(PageBase):
    """Web/Desktop UI page for controlling the headless remote recording server."""

    def __init__(self, app):
        super().__init__(app)
        self.page_name = "remote"
        self.app.language_manager.add_observer(self)
        self._: dict[str, str] = {}
        self.load_language()

        self.status_banner = ft.Column(spacing=6)
        self.tasks_column = ft.Column(spacing=10)
        self.loading_ring = ft.ProgressRing(width=24, height=24, stroke_width=2.5, visible=False)
        self.url_field: ft.TextField | None = None
        self.quality_dropdown: ft.Dropdown | None = None
        self.format_dropdown: ft.Dropdown | None = None
        self.segment_switch: ft.Switch | None = None
        self.segment_time_field: ft.TextField | None = None

    def load_language(self):
        language = self.app.language_manager.language
        for key in ("remote_page", "video_quality", "base", "recording_dialog"):
            self._.update(language.get(key, {}))

    def _get_remote_config(self) -> RemoteBackendConfig:
        return RemoteBackendConfig.from_user_config(self.app.settings.user_config)

    async def load(self):
        self.content_area.controls.clear()
        self.load_language()
        is_mobile = self.app.is_mobile

        header_row = ft.Row(
            [
                ft.Text(self._["title"], theme_style=ft.TextThemeStyle.TITLE_LARGE, weight=ft.FontWeight.BOLD),
                ft.Container(expand=True),
                self.loading_ring,
                ft.IconButton(
                    icon=ft.Icons.REFRESH,
                    icon_color=ft.Colors.PRIMARY,
                    tooltip=self._["refresh"],
                    on_click=self.on_refresh_click,
                ),
            ],
            alignment=ft.MainAxisAlignment.START,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        self.url_field = ft.TextField(
            label=self._["live_url_label"],
            hint_text=self._["live_url_hint"],
            expand=True,
            border_radius=6,
        )
        self.quality_dropdown = ft.Dropdown(
            label=self._["quality_label"],
            options=[ft.dropdown.DropdownOption(q, text=self._.get(q, q)) for q in VideoQuality.get_qualities()],
            value=self.app.settings.get_config_value("record_quality", VideoQuality.OD),
            width=150 if not is_mobile else None,
            expand=True if is_mobile else None,
        )
        self.format_dropdown = ft.Dropdown(
            label=self._["format_label"],
            options=[ft.dropdown.DropdownOption(fmt) for fmt in ("ts", "mp4", "mkv", "flv", "m4a", "mp3")],
            value="ts",
            width=110 if not is_mobile else None,
            expand=True if is_mobile else None,
        )
        self.segment_switch = ft.Switch(
            label=self._["segment_label"],
            value=bool(self.app.settings.get_config_value("segmented_recording_enabled", False)),
        )
        self.segment_time_field = ft.TextField(
            label=self._["segment_time_label"],
            value=str(self.app.settings.get_config_value("video_segment_time", "1800")),
            width=130 if not is_mobile else None,
            expand=True if is_mobile else None,
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        start_btn = ft.Button(
            content=self._["start_remote_record"],
            icon=ft.Icons.FIBER_MANUAL_RECORD,
            on_click=self.on_start_remote_click,
        )

        if is_mobile:
            form_controls = [
                self.url_field,
                ft.Row([self.quality_dropdown, self.format_dropdown], spacing=10),
                ft.Row([self.segment_switch, self.segment_time_field], spacing=10),
                start_btn,
            ]
        else:
            form_controls = [
                ft.Row([self.url_field, start_btn], spacing=10),
                ft.Row(
                    [self.quality_dropdown, self.format_dropdown, self.segment_switch, self.segment_time_field],
                    spacing=12,
                ),
            ]

        create_card = ft.Card(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(self._["manual_start_title"], size=15, weight=ft.FontWeight.W_600),
                        *form_controls,
                    ],
                    spacing=10,
                ),
                padding=14,
            )
        )

        status_card = ft.Card(
            content=ft.Container(
                content=self.status_banner,
                padding=14,
            )
        )

        page_column = ft.Column(
            [
                header_row,
                status_card,
                create_card,
                ft.Text(self._["task_list_title"], size=16, weight=ft.FontWeight.W_600),
                self.tasks_column,
            ],
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

        self.content_area.controls.append(ft.Container(content=page_column, expand=True, padding=10))
        self.content_area.update()
        await self.refresh_data()

    async def on_refresh_click(self, _):
        await self.refresh_data()

    async def refresh_data(self):
        cfg = self._get_remote_config()
        self.status_banner.controls.clear()
        self.tasks_column.controls.clear()

        if not cfg.is_configured:
            self.status_banner.controls.extend(
                [
                    ft.Text(self._["not_configured"], color=ft.Colors.ORANGE),
                    ft.Button(
                        content=self._["go_settings"],
                        icon=ft.Icons.SETTINGS,
                        on_click=lambda _: self.page.go("/settings"),
                    ),
                ]
            )
            self.content_area.update()
            return

        self.loading_ring.visible = True
        self.content_area.update()

        client = RemoteBackendClient(cfg.base_url, cfg.api_key)
        try:
            sys_info, tasks = await asyncio.gather(client.get_system(), client.list_tasks())
            self._render_system_status(cfg, sys_info)
            self._render_tasks(cfg, tasks)
        except RemoteBackendError as exc:
            self.status_banner.controls.append(
                ft.Text(self._["request_failed"].format(error=str(exc)), color=ft.Colors.RED)
            )
        except Exception as exc:
            logger.error(f"Failed refreshing remote backend page: {exc}")
            self.status_banner.controls.append(
                ft.Text(self._["request_failed"].format(error=str(exc)), color=ft.Colors.RED)
            )
        finally:
            self.loading_ring.visible = False
            self.content_area.update()

    def _render_system_status(self, cfg: RemoteBackendConfig, sys_info: dict):
        disk = sys_info.get("disk") or {}
        used_gb = disk.get("used_bytes", 0) / (1024**3)
        max_gb = max(1e-6, disk.get("max_storage_bytes", 10 * 1024**3) / (1024**3))
        free_gb = disk.get("free_bytes", 0) / (1024**3)
        min_free_gb = disk.get("min_free_bytes", 3 * 1024**3) / (1024**3)
        ratio = min(1.0, used_gb / max_gb)

        active = sys_info.get("active_tasks", 0)
        max_tasks = sys_info.get("max_concurrent_tasks", 4)

        server_info = ft.Row(
            [
                ft.Icon(ft.Icons.CLOUD_DONE, color=ft.Colors.GREEN, size=18),
                ft.Text(
                    f"{self._['server_url']}: {cfg.base_url}",
                    weight=ft.FontWeight.W_600,
                    selectable=True,
                ),
            ],
            spacing=8,
            tight=True,
        )
        retention_label = ft.Text(self._["retention_hours"], size=12, color=ft.Colors.GREY)

        if self.app.is_mobile:
            top_row = ft.Column([server_info, retention_label], spacing=4)
            stats_row = ft.Column(
                [
                    ft.Text(f"{self._['storage_usage']}: {used_gb:.2f} / {max_gb:.1f} GB", size=13),
                    ft.Text(f"{self._['disk_free']}: {free_gb:.2f} GB (min {min_free_gb:.1f} GB)", size=13),
                    ft.Text(f"{self._['active_tasks']}: {active}/{max_tasks}", size=13),
                ],
                spacing=4,
            )
        else:
            top_row = ft.Row(
                [server_info, retention_label],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            stats_row = ft.Row(
                [
                    ft.Text(f"{self._['storage_usage']}: {used_gb:.2f} / {max_gb:.1f} GB", size=13),
                    ft.Text(f"{self._['disk_free']}: {free_gb:.2f} GB (min {min_free_gb:.1f} GB)", size=13),
                    ft.Text(f"{self._['active_tasks']}: {active}/{max_tasks}", size=13),
                ],
                spacing=20,
            )

        self.status_banner.controls.extend(
            [
                top_row,
                ft.ProgressBar(value=ratio, bar_height=8),
                stats_row,
            ]
        )

    def _render_tasks(self, cfg: RemoteBackendConfig, tasks: list[dict]):
        if not tasks:
            self.tasks_column.controls.append(
                ft.Container(
                    content=ft.Text(self._["no_tasks"], color=ft.Colors.GREY),
                    padding=16,
                )
            )
            return

        now = time.time()
        status_colors = {
            "checking": ft.Colors.BLUE,
            "recording": ft.Colors.GREEN,
            "stopping": ft.Colors.ORANGE,
            "completed": ft.Colors.TEAL,
            "stopped": ft.Colors.GREY,
            "error": ft.Colors.RED,
        }

        for task in tasks:
            tid = task["task_id"]
            status = task.get("status", "unknown")
            status_label = self._.get(f"status_{status}", status)
            badge = ft.Container(
                content=ft.Text(status_label, size=12, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                bgcolor=status_colors.get(status, ft.Colors.GREY),
                border_radius=4,
                padding=ft.Padding.symmetric(horizontal=8, vertical=3),
            )

            files = task.get("files") or []
            total_mb = sum(int(f.get("size_bytes") or 0) for f in files) / (1024 * 1024)
            expiry_text = ""
            expires_at = task.get("expires_at")
            if expires_at:
                rem = max(0, int(expires_at - now))
                if rem == 0:
                    expiry_text = self._["expired"]
                else:
                    expiry_text = self._["expires_in"].format(hours=rem // 3600, minutes=(rem % 3600) // 60)

            subtitle_parts = [
                f"ID: {tid}",
                task.get("platform") or "",
                f"{len(files)} file(s) · {total_mb:.1f} MB",
            ]
            if expiry_text:
                subtitle_parts.append(expiry_text)
            if task.get("error_message"):
                subtitle_parts.append(task["error_message"][:120])

            actions: list[ft.Control] = []
            if status in {"checking", "recording", "stopping"}:
                actions.append(
                    ft.Button(
                        content=self._["stop_btn"],
                        icon=ft.Icons.STOP,
                        on_click=lambda _, t_id=tid: self.page.run_task(self.on_stop_task, t_id),
                    )
                )
            if files:
                actions.append(
                    ft.Button(
                        content=self._["pull_btn"],
                        icon=ft.Icons.DOWNLOAD,
                        on_click=lambda _, t=task: self.page.run_task(self.on_pull_task, t),
                    )
                )
                for f_item in files:
                    dl_url = f_item.get("download_url") or f"{cfg.base_url}{f_item['download_path']}"
                    label = self._["download_btn"] if len(files) == 1 else f_item["filename"]
                    actions.append(
                        ft.TextButton(
                            content=label,
                            icon=ft.Icons.OPEN_IN_NEW,
                            on_click=lambda _, url=dl_url: self.page.run_task(self.page.launch_url, url),
                        )
                    )
            if status not in {"checking", "recording", "stopping"}:
                actions.append(
                    ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE,
                        icon_color=ft.Colors.RED,
                        tooltip=self._["delete_btn"],
                        on_click=lambda _, t_id=tid: self.page.run_task(self.on_delete_task, t_id),
                    )
                )

            card = ft.Card(
                content=ft.Container(
                    content=ft.Column(
                        [
                            ft.Row(
                                [
                                    ft.Text(
                                        f"{task.get('anchor_name') or 'Live'} - {task.get('live_title') or task.get('url')}",
                                        weight=ft.FontWeight.W_600,
                                        expand=True,
                                        max_lines=1,
                                        overflow=ft.TextOverflow.ELLIPSIS,
                                        selectable=True,
                                    ),
                                    badge,
                                ],
                                spacing=8,
                            ),
                            ft.Text(" | ".join(p for p in subtitle_parts if p), size=12, color=ft.Colors.GREY),
                            ft.Row(actions, spacing=8, scroll=ft.ScrollMode.AUTO),
                        ],
                        spacing=6,
                    ),
                    padding=12,
                )
            )
            self.tasks_column.controls.append(card)

    async def on_start_remote_click(self, _):
        cfg = self._get_remote_config()
        if not cfg.is_configured:
            await self.app.snack_bar.show_snack_bar(self._["remote_config_missing"], bgcolor=ft.Colors.RED)
            return

        url = (self.url_field.value or "").strip() if self.url_field else ""
        if not url:
            return

        try:
            seg_time = int((self.segment_time_field.value or "1800").strip())
        except ValueError:
            seg_time = 1800

        client = RemoteBackendClient(cfg.base_url, cfg.api_key)
        try:
            task = await client.create_task(
                url=url,
                quality=self.quality_dropdown.value or VideoQuality.OD,
                fmt=self.format_dropdown.value or "ts",
                segment_record=bool(self.segment_switch.value),
                segment_time=max(30, seg_time),
            )
            if self.url_field:
                self.url_field.value = ""
            await self.app.snack_bar.show_snack_bar(
                self._["task_started"].format(task_id=task["task_id"]),
                bgcolor=ft.Colors.PRIMARY,
            )
            await self.refresh_data()
        except RemoteBackendError as exc:
            await self.app.snack_bar.show_snack_bar(
                self._["request_failed"].format(error=str(exc)),
                bgcolor=ft.Colors.RED,
            )

    async def on_stop_task(self, task_id: str):
        cfg = self._get_remote_config()
        client = RemoteBackendClient(cfg.base_url, cfg.api_key)
        try:
            await client.stop_task(task_id)
            await self.app.snack_bar.show_snack_bar(self._["task_stopped"].format(task_id=task_id))
            await self.refresh_data()
        except RemoteBackendError as exc:
            await self.app.snack_bar.show_snack_bar(
                self._["request_failed"].format(error=str(exc)),
                bgcolor=ft.Colors.RED,
            )

    async def on_delete_task(self, task_id: str):
        cfg = self._get_remote_config()
        client = RemoteBackendClient(cfg.base_url, cfg.api_key)
        try:
            await client.delete_task(task_id)
            await self.app.snack_bar.show_snack_bar(self._["task_deleted"].format(task_id=task_id))
            await self.refresh_data()
        except RemoteBackendError as exc:
            await self.app.snack_bar.show_snack_bar(
                self._["request_failed"].format(error=str(exc)),
                bgcolor=ft.Colors.RED,
            )

    async def on_pull_task(self, task: dict):
        cfg = self._get_remote_config()
        files = task.get("files") or []
        if not files:
            await self.app.snack_bar.show_snack_bar(self._["no_files"])
            return

        task_id = task["task_id"]
        save_root = Path(self.app.settings.get_video_save_path())
        target_dir = save_root / "remote" / task_id
        client = RemoteBackendClient(cfg.base_url, cfg.api_key)

        await self.app.snack_bar.show_snack_bar(self._["pull_started"].format(task_id=task_id))
        try:
            for item in files:
                dl_url = item.get("download_url") or item.get("api_path") or f"/api/tasks/{task_id}/files/{item['filename']}"
                await client.download_file(dl_url, target_dir / item["filename"], expected_size=item.get("size_bytes"))
            await self.app.snack_bar.show_snack_bar(
                self._["pull_success"].format(path=str(target_dir)),
                bgcolor=ft.Colors.PRIMARY,
                duration=4000,
            )
        except Exception as exc:
            await self.app.snack_bar.show_snack_bar(
                self._["pull_failed"].format(error=str(exc)),
                bgcolor=ft.Colors.RED,
                duration=4000,
            )
