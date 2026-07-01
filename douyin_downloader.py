"""
抖音视频下载器。

核心路径：
  1. 把 /video/{id} 转成 /share/video/{id}/
  2. 从 share 页的 window._ROUTER_DATA 提取 video.play_addr.url_list
  3. 直接下载 play_addr 指向的公开媒体文件
"""

from __future__ import annotations

import json
import hashlib
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from tqdm import tqdm

from xhs_downloader import VideoDownloadResult


DEFAULT_DOUYIN_OUTPUT_DIR = "downloads"
DOUYIN_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 Mobile/15E148"
)
DOUYIN_HEADERS = {
    "User-Agent": DOUYIN_MOBILE_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
DOUYIN_VIDEO_QUERY_KEYS = ("modal_id", "aweme_id", "item_id", "video_id")


def extract_douyin_video_id(video_url: str) -> str:
    """从常见抖音 URL 中提取视频 ID，无法提取时抛出明确错误。"""
    normalized_url = video_url.replace("\\", "").strip()
    parsed_url = urlparse(normalized_url)
    query_values = parse_qs(parsed_url.query)

    for key in DOUYIN_VIDEO_QUERY_KEYS:
        values = query_values.get(key) or []
        for value in values:
            candidate = value.strip()
            if candidate:
                return candidate

    path_parts = [part for part in parsed_url.path.split("/") if part]
    for marker in ("video", "note"):
        if marker not in path_parts:
            continue
        marker_index = path_parts.index(marker)
        if marker_index + 1 < len(path_parts):
            return path_parts[marker_index + 1].strip()

    raise ValueError(f"无法从 URL 中提取抖音视频 ID: {video_url!r}")


def build_douyin_task_id(video_url: str) -> str:
    """构建抖音任务 ID，保证同一视频多次运行落到同一目录。"""
    try:
        return f"douyin_{extract_douyin_video_id(video_url)}"
    except ValueError:
        digest = hashlib.sha256(video_url.replace("\\", "").strip().encode("utf-8")).hexdigest()[:16]
        return f"douyin_{digest}"


def build_douyin_share_url(video_url: str) -> str:
    """把任意抖音视频 URL 转为更适合脚本解析的 share 页 URL。"""
    video_id = extract_douyin_video_id(video_url)
    return f"https://www.douyin.com/share/video/{video_id}/"


def extract_router_data(html: str) -> dict[str, Any]:
    """从 share 页 HTML 中提取 window._ROUTER_DATA。"""
    match = re.search(
        r"window\._ROUTER_DATA\s*=\s*(\{[\s\S]*?\})\s*</script>",
        html,
    )
    if not match:
        raise RuntimeError("未能从抖音 share 页提取 _ROUTER_DATA")
    return json.loads(match.group(1))


def extract_douyin_aweme(html: str) -> dict[str, Any]:
    """从 share 页路由数据中提取第一条 aweme 视频信息。"""
    router_data = extract_router_data(html)
    loader_data = router_data.get("loaderData") or {}
    for page_data in loader_data.values():
        if not isinstance(page_data, dict):
            continue
        video_info = page_data.get("videoInfoRes") or {}
        item_list = video_info.get("item_list") or []
        if item_list:
            return item_list[0]
    raise RuntimeError("抖音 share 页中没有可下载的视频信息")


def extract_douyin_play_url(aweme: dict[str, Any]) -> str:
    """从 aweme 结构中提取真实播放 URL。"""
    video = aweme.get("video") or {}
    play_addr = video.get("play_addr") or {}
    url_list = play_addr.get("url_list") or []
    for url in url_list:
        if isinstance(url, str) and url.startswith("http"):
            return url
    raise RuntimeError("抖音视频信息中没有 play_addr 下载地址")


def build_douyin_download_result(
    video_url: str,
    share_url: str,
    task_id: str,
    aweme: dict[str, Any],
    output_path: str | Path,
) -> VideoDownloadResult:
    """把抖音页面元数据转换成流水线复用的下载结果结构。"""
    output = Path(output_path)
    video = aweme.get("video") or {}
    return VideoDownloadResult(
        note_id=task_id,
        note_url=video_url,
        resolved_url=share_url,
        output_path=str(output),
        filename=output.name,
        codec="unknown",
        audio_codec="unknown",
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=0,
        avg_bitrate=0,
        size=output.stat().st_size if output.exists() else 0,
        format=output.suffix.lstrip(".") or "mp4",
        quality_index=0,
    )


def download_file(session: requests.Session, media_url: str, output_path: str | Path) -> str:
    """流式下载媒体文件到本地路径。"""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    response = session.get(media_url, stream=True, timeout=30)
    response.raise_for_status()

    total = int(response.headers.get("Content-Length", 0))
    with target.open("wb") as file_obj, tqdm(
        total=total or None,
        unit="B",
        unit_scale=True,
        desc=f"下载 {target.name}",
    ) as progress:
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            file_obj.write(chunk)
            progress.update(len(chunk))
    return str(target)


def build_session(session: requests.Session | None = None) -> requests.Session:
    """构建抖音下载请求会话，并统一使用移动端 UA。"""
    runtime_session = session or requests.Session()
    runtime_session.headers.update(DOUYIN_HEADERS)
    return runtime_session


def download_douyin_video_result(
    video_url: str,
    output_dir: str | Path = DEFAULT_DOUYIN_OUTPUT_DIR,
    task_id: str | None = None,
    session: requests.Session | None = None,
) -> VideoDownloadResult:
    """下载抖音视频并返回结构化结果，不依赖浏览器或手动 cookie。"""
    runtime_task_id = task_id or build_douyin_task_id(video_url)
    share_url = build_douyin_share_url(video_url)
    runtime_session = build_session(session)

    page_response = runtime_session.get(share_url, timeout=30)
    page_response.raise_for_status()
    aweme = extract_douyin_aweme(page_response.text)
    media_url = extract_douyin_play_url(aweme)

    output_path = Path(output_dir) / f"{runtime_task_id}.mp4"
    download_file(runtime_session, media_url, output_path)
    return build_douyin_download_result(
        video_url=video_url,
        share_url=share_url,
        task_id=runtime_task_id,
        aweme=aweme,
        output_path=output_path,
    )


def parse_cli_arguments(argv: list[str]) -> tuple[str, str]:
    """解析本地下载脚本参数。"""
    video_url = argv[1]
    output_dir = argv[2] if len(argv) > 2 else DEFAULT_DOUYIN_OUTPUT_DIR
    return video_url, output_dir


def main(argv: list[str] | None = None) -> int:
    """命令行入口，只执行抖音下载，不触发转录。"""
    runtime_argv = argv or sys.argv
    if len(runtime_argv) < 2:
        print("用法: python douyin_downloader.py <抖音视频URL> [输出目录]")
        print(f"默认输出目录: ./{DEFAULT_DOUYIN_OUTPUT_DIR}")
        return 1

    video_url, output_dir = parse_cli_arguments(runtime_argv)
    try:
        result = download_douyin_video_result(video_url=video_url, output_dir=output_dir)
        print(f"\n媒体已保存至: {result.output_path}")
        return 0
    except Exception as error:
        print(f"\n错误: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
