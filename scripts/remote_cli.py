#!/usr/bin/env python3
"""Zero-dependency local CLI for controlling the remote StreamCap headless backend."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://streamcap.jp.insolitude.me"


def _load_dotenv_simple() -> None:
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"):
        if not candidate.is_file():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
        break


def _get_config(args: argparse.Namespace) -> tuple[str, str]:
    _load_dotenv_simple()
    base_url = (args.server or os.getenv("STREAMCAP_REMOTE_URL") or DEFAULT_BASE_URL).rstrip("/")
    api_key = (args.api_key or os.getenv("STREAMCAP_API_KEY") or "").strip()
    if not api_key:
        print("Error: set STREAMCAP_API_KEY or pass --api-key", file=sys.stderr)
        sys.exit(2)
    return base_url, api_key


def _request_json(
    base_url: str,
    api_key: str,
    method: str,
    path: str,
    payload: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    url = f"{base_url}{path}"
    headers = {"Authorization": f"Bearer {api_key}"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        print(f"Error [{exc.code}]: {err_body}", file=sys.stderr)
        sys.exit(1)


def _download_file(
    url: str,
    dest_path: Path,
    expected_size: int | None = None,
    api_key: str | None = None,
) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest_path.with_name(dest_path.name + ".part")

    if dest_path.is_file() and expected_size is not None and dest_path.stat().st_size == expected_size:
        print(f"Already downloaded: {dest_path}")
        return

    existing_size = part_path.stat().st_size if part_path.is_file() else 0
    if expected_size is not None and existing_size > expected_size:
        part_path.unlink(missing_ok=True)
        existing_size = 0

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    mode = "wb"
    if existing_size > 0:
        headers["Range"] = f"bytes={existing_size}-"
        mode = "ab"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and expected_size is not None and existing_size == expected_size:
            os.replace(part_path, dest_path)
            print(f"Completed from partial file: {dest_path}")
            return
        raise

    with resp:
        if resp.status == 200 and existing_size > 0:
            mode = "wb"
            existing_size = 0

        written = existing_size
        with part_path.open(mode) as fp:
            while chunk := resp.read(65536):
                fp.write(chunk)
                written += len(chunk)

    if expected_size is not None and written != expected_size:
        raise RuntimeError(f"Incomplete download for {dest_path.name}: got {written} of {expected_size} bytes")

    os.replace(part_path, dest_path)
    print(f"Downloaded: {dest_path} ({written} bytes)")


def _download_task_files(base_url: str, api_key: str, task: dict, out_dir: Path) -> None:
    files = task.get("files", [])
    if not files:
        print(f"No files available for task {task['task_id']}")
        return
    for item in files:
        url = item.get("download_url") or f"{base_url}{item['download_path']}"
        target = out_dir / task["task_id"] / item["filename"]
        _download_file(url, target, expected_size=item["size_bytes"], api_key=api_key)


def cmd_record(args: argparse.Namespace, base_url: str, api_key: str) -> None:
    payload = {
        "url": args.url,
        "quality": args.quality,
        "format": args.format,
        "segment_record": args.segment,
        "segment_time": args.segment_time,
        "overseas": args.overseas,
    }
    task = _request_json(base_url, api_key, "POST", "/api/tasks", payload=payload)
    print(json.dumps(task, ensure_ascii=False, indent=2))

    if not (args.wait or args.download):
        return

    task_id = task["task_id"]
    while task["status"] in {"checking", "recording", "stopping"}:
        time.sleep(args.poll_interval)
        task = _request_json(base_url, api_key, "GET", f"/api/tasks/{task_id}")
        total_bytes = sum(f["size_bytes"] for f in task.get("files", []))
        print(f"[{task_id}] status={task['status']} files={len(task.get('files', []))} bytes={total_bytes}")

    if args.download:
        _download_task_files(base_url, api_key, task, Path(args.output))


def cmd_status(args: argparse.Namespace, base_url: str, api_key: str) -> None:
    path = f"/api/tasks/{args.task_id}" if args.task_id else "/api/tasks"
    data = _request_json(base_url, api_key, "GET", path)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_stop(args: argparse.Namespace, base_url: str, api_key: str) -> None:
    data = _request_json(base_url, api_key, "POST", f"/api/tasks/{args.task_id}/stop")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_delete(args: argparse.Namespace, base_url: str, api_key: str) -> None:
    qs = urllib.parse.urlencode({"force": str(args.force).lower()})
    data = _request_json(base_url, api_key, "DELETE", f"/api/tasks/{args.task_id}?{qs}")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_download(args: argparse.Namespace, base_url: str, api_key: str) -> None:
    task = _request_json(base_url, api_key, "GET", f"/api/tasks/{args.task_id}")
    _download_task_files(base_url, api_key, task, Path(args.output))


def cmd_system(_args: argparse.Namespace, base_url: str, api_key: str) -> None:
    data = _request_json(base_url, api_key, "GET", "/api/system")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_cookies(args: argparse.Namespace, base_url: str, api_key: str) -> None:
    data = _request_json(
        base_url,
        api_key,
        "PUT",
        "/api/config/cookies",
        payload={"cookies": {args.platform: args.cookie}},
    )
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="StreamCap Remote Recorder CLI")
    parser.add_argument(
        "--server",
        help="Remote server base URL (default: STREAMCAP_REMOTE_URL or https://streamcap.jp.insolitude.me)",
    )
    parser.add_argument("--api-key", help="Bearer API key (default: STREAMCAP_API_KEY)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_rec = sub.add_parser("record", help="Start recording a live URL")
    p_rec.add_argument("url", help="Live room URL")
    p_rec.add_argument("--quality", default="OD", help="Video quality (OD/UHD/HD/SD/LD/AD)")
    p_rec.add_argument("--format", default="ts", help="Container format (default: ts)")
    p_rec.add_argument("--segment", action="store_true", help="Enable segmented recording")
    p_rec.add_argument("--segment-time", type=int, default=1800, help="Segment duration in seconds")
    p_rec.add_argument("--overseas", action="store_true", help="Use overseas ffmpeg timeout profile")
    p_rec.add_argument("--wait", action="store_true", help="Wait until recording completes")
    p_rec.add_argument("--download", action="store_true", help="Wait and download recorded files locally")
    p_rec.add_argument("--poll-interval", type=int, default=10, help="Polling interval in seconds")
    p_rec.add_argument("-o", "--output", default="./downloads", help="Local download directory")

    p_st = sub.add_parser("status", help="Show task status or list all tasks")
    p_st.add_argument("task_id", nargs="?", help="Optional task ID")

    p_stop = sub.add_parser("stop", help="Stop an active recording task")
    p_stop.add_argument("task_id", help="Task ID")

    p_del = sub.add_parser("delete", help="Delete a task and its remote files")
    p_del.add_argument("task_id", help="Task ID")
    p_del.add_argument("--force", action="store_true", help="Force stop before deleting")

    p_dl = sub.add_parser("download", help="Download files for a task")
    p_dl.add_argument("task_id", help="Task ID")
    p_dl.add_argument("-o", "--output", default="./downloads", help="Local download directory")

    sub.add_parser("system", help="Check remote storage and concurrency status")

    p_ck = sub.add_parser("cookies", help="Update platform cookies on remote server")
    p_ck.add_argument("platform", help="Platform key, e.g. douyin / bilibili")
    p_ck.add_argument("cookie", help="Cookie header string")

    args = parser.parse_args()
    base_url, api_key = _get_config(args)

    dispatch = {
        "record": cmd_record,
        "status": cmd_status,
        "stop": cmd_stop,
        "delete": cmd_delete,
        "download": cmd_download,
        "system": cmd_system,
        "cookies": cmd_cookies,
    }
    dispatch[args.command](args, base_url, api_key)


if __name__ == "__main__":
    main()
