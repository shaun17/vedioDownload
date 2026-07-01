"""
基于 yt-dlp 的通用视频下载器。

这个脚本只负责下载公开可访问的视频，不包含转录逻辑，
方便在本地单独验证下载能力。
"""

from __future__ import annotations

import hashlib
import importlib
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from xhs_downloader import VideoDownloadResult


# 本地直接执行脚本时的默认输出目录，和 xhs_downloader.py 保持一致。
DEFAULT_YT_DLP_OUTPUT_DIR = "downloads"

# 通用链接默认下载 MP4 视频；audio 模式用于只提取 mp3。
DEFAULT_YT_DLP_AUDIO_FORMAT = "bestaudio/best"

# 视频模式优先走 HLS；如果 HLS 探测超时，HTTP 兜底只选低码率 MP4，
# 避免 Twitter/X 回退到 `http-2176` 这类超大 HTTPS 直连文件。
DEFAULT_YT_DLP_VIDEO_FORMAT = (
    "bv*[protocol^=m3u8][ext=mp4]+ba[protocol^=m3u8]/"
    "b[protocol^=m3u8][ext=mp4]/"
    "b[ext=mp4][height<=360][tbr<=400]/"
    "b[ext=mp4][height<=360]/"
    "b[ext=mp4][tbr<=1000]/"
    "w[ext=mp4]/"
    "b[ext=mp4]/best"
)
DEFAULT_YT_DLP_SOCKET_TIMEOUT = 60
DEFAULT_YT_DLP_RETRIES = 10
YT_DLP_COOKIE_BASE_DOMAINS = ("douyin.com", "iesdouyin.com", "amemv.com")


def build_generic_task_id(video_url: str) -> str:
    """根据 URL 生成稳定任务 ID，保证同一链接多次运行落到同一目录。"""
    normalized_url = video_url.strip()
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:16]
    return f"ytdlp_{digest}"


def load_yt_dlp_module():
    """
    延迟加载 yt-dlp。

    这样导入 Colab 流水线或运行单元测试时，不会因为本地尚未安装
    yt-dlp 而直接失败；只有真正下载通用视频时才要求依赖存在。
    """
    try:
        return importlib.import_module("yt_dlp")
    except ImportError as error:
        raise RuntimeError(
            "未找到 yt-dlp，请先执行 `pip install -r requirements.txt` "
            "或单独执行 `pip install yt-dlp`"
        ) from error


def normalize_cookie_file_field(value: Any) -> str:
    """清理 cookie 文件字段，避免 tab 或换行破坏 Netscape cookie 格式。"""
    return str(value).replace("\t", " ").replace("\n", " ").strip()


def resolve_cookie_domain(video_url: str) -> str:
    """根据视频 URL 选择 cookie domain，抖音类域名统一写到主域。"""
    host = (urlparse(video_url.replace("\\", "").strip()).hostname or "").lower()
    if not host:
        raise ValueError("无法从视频 URL 解析 cookie 域名")

    for domain in YT_DLP_COOKIE_BASE_DOMAINS:
        if host == domain or host.endswith(f".{domain}"):
            return f".{domain}"
    return host


def build_netscape_cookie_file_text(video_url: str, cookies: dict | None) -> str:
    """把运行时 cookie 字典转换成 yt-dlp 可读取的 Netscape cookie 文件内容。"""
    domain = resolve_cookie_domain(video_url)
    lines = ["# Netscape HTTP Cookie File", ""]
    for raw_name, raw_value in (cookies or {}).items():
        name = normalize_cookie_file_field(raw_name)
        value = normalize_cookie_file_field(raw_value)
        if not name or not value:
            continue
        lines.append(f"{domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}")
    return "\n".join(lines) + "\n"


def write_temporary_cookie_file(video_url: str, cookies: dict | None) -> Path | None:
    """将 cookie 写入临时文件；没有有效 cookie 时返回 None。"""
    valid_cookies = {
        normalize_cookie_file_field(name): normalize_cookie_file_field(value)
        for name, value in (cookies or {}).items()
        if normalize_cookie_file_field(name) and normalize_cookie_file_field(value)
    }
    if not valid_cookies:
        return None

    cookie_text = build_netscape_cookie_file_text(video_url, valid_cookies)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="yt_dlp_cookies_",
        suffix=".txt",
        delete=False,
    ) as cookie_file:
        cookie_file.write(cookie_text)
        return Path(cookie_file.name)


def build_yt_dlp_options(
    output_dir: str | Path,
    task_id: str,
    media_type: str = "video",
    quiet: bool = False,
    cookiefile: str | Path | None = None,
) -> dict[str, Any]:
    """构建 yt-dlp 下载参数，统一输出文件名和音视频下载模式。"""
    if media_type not in {"audio", "video"}:
        raise ValueError("media_type 必须是 'audio' 或 'video'")

    output_path_template = str(Path(output_dir) / f"{task_id}.%(ext)s")
    options: dict[str, Any] = {
        "format": DEFAULT_YT_DLP_AUDIO_FORMAT if media_type == "audio" else DEFAULT_YT_DLP_VIDEO_FORMAT,
        "outtmpl": {"default": output_path_template},
        "noplaylist": True,
        "restrictfilenames": True,
        "socket_timeout": DEFAULT_YT_DLP_SOCKET_TIMEOUT,
        "retries": DEFAULT_YT_DLP_RETRIES,
        "fragment_retries": DEFAULT_YT_DLP_RETRIES,
        "extractor_retries": DEFAULT_YT_DLP_RETRIES,
        "file_access_retries": DEFAULT_YT_DLP_RETRIES,
        "quiet": quiet,
        "no_warnings": quiet,
    }
    if cookiefile:
        options["cookiefile"] = str(cookiefile)

    if media_type == "audio":
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0",
        }]
        return options

    options["merge_output_format"] = "mp4"
    return options


def to_int(value: Any, default: int = 0) -> int:
    """把 yt-dlp 元数据里的数字字段安全转换为 int。"""
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def resolve_downloaded_file(
    output_dir: str | Path,
    task_id: str,
    info: dict[str, Any],
    prepared_path: str | Path | None = None,
) -> Path:
    """
    从 yt-dlp 元数据和输出目录中定位最终下载文件。

    合并音视频后，最终文件名可能和 prepare_filename 的中间结果不同，
    因此优先看 requested_downloads，再回退到目录扫描。
    """
    candidate_paths: list[Path] = []
    for item in info.get("requested_downloads") or []:
        for key in ("filepath", "filename"):
            raw_path = item.get(key)
            if raw_path:
                candidate_paths.append(Path(raw_path))

    for key in ("filepath", "filename", "_filename"):
        raw_path = info.get(key)
        if raw_path:
            candidate_paths.append(Path(raw_path))

    if prepared_path:
        candidate_paths.append(Path(prepared_path))

    for candidate in candidate_paths:
        if candidate.exists() and candidate.is_file():
            return candidate

    output_root = Path(output_dir)
    existing_files = sorted(
        output_root.glob(f"{task_id}.*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if existing_files:
        return existing_files[0]

    raise RuntimeError(f"yt-dlp 下载完成后未找到输出文件: {output_root / task_id}")


def build_download_result(
    video_url: str,
    task_id: str,
    info: dict[str, Any],
    output_path: str | Path,
) -> VideoDownloadResult:
    """把 yt-dlp 元数据转换为流水线复用的下载结果结构。"""
    output = Path(output_path)
    output_size = output.stat().st_size if output.exists() else to_int(
        info.get("filesize") or info.get("filesize_approx")
    )
    return VideoDownloadResult(
        note_id=task_id,
        note_url=video_url,
        resolved_url=str(info.get("webpage_url") or info.get("original_url") or video_url),
        output_path=str(output),
        filename=output.name,
        codec=str(info.get("vcodec") or "unknown"),
        audio_codec=str(info.get("acodec") or "unknown"),
        width=to_int(info.get("width")),
        height=to_int(info.get("height")),
        fps=to_int(info.get("fps")),
        avg_bitrate=to_int(info.get("tbr"), default=0) * 1000,
        size=output_size,
        format=output.suffix.lstrip(".") or str(info.get("ext") or "mp4"),
        quality_index=0,
    )


def download_yt_dlp_video_result(
    video_url: str,
    output_dir: str | Path = DEFAULT_YT_DLP_OUTPUT_DIR,
    task_id: str | None = None,
    media_type: str = "video",
    quiet: bool = False,
    cookies: dict | None = None,
) -> VideoDownloadResult:
    """使用 yt-dlp 下载媒体，并返回兼容 Colab 流水线的结果对象。"""
    runtime_task_id = task_id or build_generic_task_id(video_url)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    yt_dlp = load_yt_dlp_module()
    cookiefile = write_temporary_cookie_file(video_url, cookies)
    try:
        options = build_yt_dlp_options(
            output_dir,
            runtime_task_id,
            media_type=media_type,
            quiet=quiet,
            cookiefile=cookiefile,
        )
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(video_url, download=True)
            sanitized_info = ydl.sanitize_info(info)
            prepared_path = ydl.prepare_filename(info)
    finally:
        if cookiefile:
            cookiefile.unlink(missing_ok=True)

    output_path = resolve_downloaded_file(
        output_dir=output_dir,
        task_id=runtime_task_id,
        info=sanitized_info,
        prepared_path=prepared_path,
    )
    return build_download_result(video_url, runtime_task_id, sanitized_info, output_path)


def parse_cli_arguments(argv: list[str]) -> tuple[str, str, str]:
    """解析本地下载脚本参数。"""
    video_url = argv[1]
    output_dir = argv[2] if len(argv) > 2 else DEFAULT_YT_DLP_OUTPUT_DIR
    media_type = argv[3] if len(argv) > 3 else "video"
    return video_url, output_dir, media_type


def main(argv: list[str] | None = None) -> int:
    """命令行入口，只执行下载，不触发转录。"""
    runtime_argv = argv or sys.argv
    if len(runtime_argv) < 2:
        print("用法: python yt_dlp_downloader.py <视频URL> [输出目录] [audio|video]")
        print(f"默认输出目录: ./{DEFAULT_YT_DLP_OUTPUT_DIR}")
        print("默认下载模式: video（下载 mp4 视频）；传 audio 可只提取 mp3")
        return 1

    video_url, output_dir, media_type = parse_cli_arguments(runtime_argv)
    try:
        result = download_yt_dlp_video_result(
            video_url,
            output_dir=output_dir,
            media_type=media_type,
        )
        print(f"\n🎉 媒体已保存至: {result.output_path}")
        return 0
    except Exception as error:
        print(f"\n❌ 错误: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
