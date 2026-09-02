from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FONT_BASE = "https://fonts.gstatic.com/s/"
FONTS_DIR = ROOT / "assets" / "fonts"

# 引擎中 fallback 字体条目的列表包裹在这两个字符串之间
_SLICE_START = "Noto Color Emoji 0"
_PAIR_RE = re.compile(r'A\.z\("([^"]+)","([^"]+)"\)')


def locate_main_dart_js() -> Path | None:
    """从本地安装的 flet_web 包定位 web/main.dart.js。"""
    try:
        import flet_web

        pkg_dir = Path(flet_web.__file__).resolve().parent
        candidate = pkg_dir / "web" / "main.dart.js"
        if candidate.is_file():
            return candidate
    except ImportError:
        pass
    return None


def extract_font_paths(source: str) -> list[str]:
    index = source.find(_SLICE_START)
    if index == -1:
        raise SystemExit(f"Cannot locate font manifest slice ('{_SLICE_START}') in main.dart.js")
    # 字体条目集中在一个连续切片里，之后遇到非 A.z(...) 就停止
    seg = source[index:]
    paths: list[str] = []
    for match in _PAIR_RE.finditer(seg):
        path = match.group(2)
        if not path.startswith(("noto", "roboto/")) or "/" not in path:
            break
        paths.append(path)
    if not paths:
        raise SystemExit("No fallback font paths extracted from main.dart.js")
    return paths


def load_paths(args: argparse.Namespace) -> list[str]:
    if args.from_json:
        paths = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        source = None
        mac = locate_main_dart_js()
        if mac:
            source = mac.read_text(encoding="utf-8")
        else:
            raise SystemExit(
                "flet_web not found locally. Pass --from-json <font_paths.json> "
                "or run this script in an environment with flet_web installed."
            )
        paths = extract_font_paths(source)
    if args.only:
        chosen = sorted(p for p in paths if any(p.startswith(f"{prefix}/") for prefix in args.only))
        print(f"Filtered by --only {args.only}: {len(chosen)}/{len(paths)}")
        paths = chosen
    return paths


def download(url: str, destination: Path, retries: int = 3) -> bool:
    """下载单个字体（带重试），返回是否成功。"""
    import time

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            # gstatic 偶发对无 Referer 的批量请求限流，带上 Google 字体页面来源更稳
            "Referer": "https://fonts.googleapis.com/",
        },
    )
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as out:
                out.write(response.read())
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"  attempt {attempt}/{retries} FAILED {url}: {exc}")
            if attempt < retries:
                time.sleep(3 * attempt)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Flutter fallback fonts bundled for no_cdn web mode.")
    parser.add_argument(
        "--from-json",
        help="Path to a JSON array of font paths (e.g. exported from main.dart.js), skipping flet_web discovery.",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        help="Only download font families starting with these directories, e.g. --only notosanssc notosanstc",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be downloaded without writing files.",
    )
    parser.add_argument("--out", default=str(FONTS_DIR), help=f"Output directory (default: {FONTS_DIR})")
    args = parser.parse_args()

    paths = load_paths(args)
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    skipped, downloaded, failed = 0, 0, 0
    for path in paths:
        dest = out_root / path
        if dest.is_file() and dest.stat().st_size > 0:
            skipped += 1
            continue
        if args.dry_run:
            print(f"[dry-run] {path}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = FONT_BASE + path
        if download(url, dest):
            downloaded += 1
        else:
            failed += 1
            # 删除失败残留，避免下次误判为已存在
            if dest.exists():
                dest.unlink()

    print(f"\nDone. skipped={skipped} downloaded={downloaded} failed={failed}")
    if failed:
        raise SystemExit(f"{failed} font(s) failed to download. Re-run to retry.")
    if downloaded == 0:
        print("All fonts already present.")


if __name__ == "__main__":
    main()
