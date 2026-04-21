"""
Colab 小红书视频下载与转录流水线。

设计目标：
  1. 复用现有下载器，不重写小红书抓取逻辑
  2. 所有中间处理都在 Colab 本地临时目录完成
  3. 最终只把视频、文稿、元数据保存到 Google Drive
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from xhs_downloader import (
    VideoDownloadResult,
    download_xhs_video_result,
    merge_cookies,
    parse_note_url,
    resolve_note_url,
)
from yt_dlp_downloader import build_generic_task_id, download_yt_dlp_video_result


DEFAULT_LOCAL_ROOT = Path("/content/xhs_workspace")
DEFAULT_DRIVE_ROOT = Path("/content/drive/MyDrive/xhs_outputs")
XIAOHONGSHU_DOMAINS = ("xiaohongshu.com", "xhslink.com")
MEDIA_OUTPUT_PATTERNS = ("*.mp4", "*.mp3", "*.m4a", "*.webm", "*.mov")


@dataclass
class WorkflowPaths:
    """描述单条笔记任务在本地和 Drive 中的路径布局。"""

    note_id: str
    local_task_dir: str
    drive_task_dir: str
    drive_video_dir: str
    local_audio_path: str
    drive_transcript_path: str
    drive_metadata_path: str


@dataclass
class TranscriptionResult:
    """封装转录阶段的结果，便于后续统一写入元数据。"""

    text: str
    language: str
    language_probability: float
    model_name: str
    device: str
    compute_type: str
    segments: list[dict]


@dataclass
class WorkflowResult:
    """封装单条 URL 在整条流水线中的最终执行结果。"""

    note_id: str
    note_url: str
    status: str
    video_path: str
    transcript_path: str
    metadata_path: str
    transcript_preview: str
    error: str = ""


def parse_cookie_json(cookies_json: str | None) -> dict:
    """
    解析运行时传入的 Cookie JSON。

    这里不直接要求用户修改源码，而是允许在 Colab 里把 Cookie
    作为字符串或字典参数传入。
    """
    if not cookies_json:
        return {}

    parsed = json.loads(cookies_json)
    if not isinstance(parsed, dict):
        raise ValueError("cookies_json 必须解析为 JSON 对象")
    return {str(key): str(value) for key, value in parsed.items() if value}


def normalize_note_urls(note_urls: Iterable[str]) -> list[str]:
    """
    规范化 URL 列表。

    会移除空字符串和重复项，保持原始顺序不变。
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_url in note_urls:
        url = raw_url.strip()
        if not url or url in seen:
            continue
        normalized.append(url)
        seen.add(url)
    return normalized


def probe_cuda_available() -> bool:
    """
    检测当前运行环境是否存在可用 CUDA。

    Colab 上通常已经内置了 `torch`，这里做防御式探测，
    避免在本地或 CPU 运行时直接报导入错误。
    """
    try:
        import torch
    except ImportError:
        return False
    return bool(torch.cuda.is_available())


def resolve_transcription_runtime(has_cuda: bool) -> tuple[str, str]:
    """
    根据是否有 GPU 决定转录设备和计算精度。

    Colab 有 GPU 时默认使用 `cuda + float16`；
    没有 GPU 时退回 `cpu + int8`。
    """
    if has_cuda:
        return "cuda", "float16"
    return "cpu", "int8"


def is_xiaohongshu_url(video_url: str) -> bool:
    """判断 URL 是否应使用小红书专用下载器处理。"""
    host = urlparse(video_url.replace("\\", "")).hostname or ""
    normalized_host = host.lower()
    return any(
        normalized_host == domain or normalized_host.endswith(f".{domain}")
        for domain in XIAOHONGSHU_DOMAINS
    )


def resolve_workflow_identity(video_url: str) -> tuple[str, str, bool]:
    """
    解析流水线任务身份。

    小红书使用真实 note_id；其他站点使用 URL hash，避免非小红书链接
    在 parse_note_url 阶段失败。
    """
    normalized_url = video_url.strip()
    if is_xiaohongshu_url(normalized_url):
        resolved_url = resolve_note_url(normalized_url)
        note_id, _, _ = parse_note_url(resolved_url)
        return resolved_url, note_id, True

    return normalized_url, build_generic_task_id(normalized_url), False


def build_workflow_paths(
    note_id: str,
    local_root: str | Path = DEFAULT_LOCAL_ROOT,
    drive_root: str | Path = DEFAULT_DRIVE_ROOT,
) -> WorkflowPaths:
    """
    为单条笔记构建本地暂存目录和 Drive 目标目录。

    目录按 `note_id` 分组，避免所有结果平铺到同一层。
    """
    local_task_dir = Path(local_root) / note_id
    drive_task_dir = Path(drive_root) / note_id
    return WorkflowPaths(
        note_id=note_id,
        local_task_dir=str(local_task_dir),
        drive_task_dir=str(drive_task_dir),
        drive_video_dir=str(drive_task_dir),
        local_audio_path=str(local_task_dir / "audio.wav"),
        drive_transcript_path=str(drive_task_dir / "transcript.txt"),
        drive_metadata_path=str(drive_task_dir / "metadata.json"),
    )


def has_existing_drive_outputs(drive_task_dir: str | Path) -> bool:
    """
    检查 Drive 中是否已经存在完整产物。

    只有同时存在媒体、文稿和元数据时，才认为这一条任务可以跳过。
    """
    task_dir = Path(drive_task_dir)
    if not task_dir.exists():
        return False

    has_video = any(
        path.exists()
        for pattern in MEDIA_OUTPUT_PATTERNS
        for path in task_dir.glob(pattern)
    )
    has_transcript = (task_dir / "transcript.txt").exists()
    has_metadata = (task_dir / "metadata.json").exists()
    return has_video and has_transcript and has_metadata


def find_existing_media_output(task_dir: str | Path) -> Path:
    """按支持的媒体扩展名查找已有输出文件。"""
    root = Path(task_dir)
    for pattern in MEDIA_OUTPUT_PATTERNS:
        existing_file = next(root.glob(pattern), None)
        if existing_file:
            return existing_file
    return root / ""


def is_running_in_colab() -> bool:
    """判断当前代码是否运行在 Colab 内核中。"""
    try:
        import google.colab  # noqa: F401
    except ImportError:
        return False
    return True


def mount_google_drive(
    mount_point: str = "/content/drive",
    force_remount: bool = False,
) -> str:
    """
    在 Colab 中挂载 Google Drive。

    这个函数只负责挂载，不负责创建业务目录。
    """
    if not is_running_in_colab():
        raise RuntimeError("当前环境不是 Colab，无法调用 google.colab.drive.mount")

    from google.colab import drive

    drive.mount(mount_point, force_remount=force_remount)
    return mount_point


def ensure_ffmpeg_available() -> str:
    """
    确认系统中存在 `ffmpeg` 命令。

    这里不自动安装系统依赖，避免本地环境被脚本直接改动。
    Colab 中如果未安装，会给出明确报错。
    """
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("未找到 ffmpeg，请先在 Colab 中安装 ffmpeg")
    return ffmpeg_path


def extract_audio_track(video_path: str | Path, audio_path: str | Path) -> str:
    """
    从视频中抽取 16k 单声道 WAV 音频。

    显式抽音频可以让转录阶段的输入更稳定，问题也更容易定位。
    """
    ensure_ffmpeg_available()
    target = Path(audio_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(target),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return str(target)


def render_plain_transcript(segments: list[dict]) -> str:
    """
    将分段结果渲染为纯文本。

    第 1 版只保存纯文稿，后续如果需要再扩展 SRT。
    """
    lines = [segment["text"].strip() for segment in segments if segment["text"].strip()]
    return "\n".join(lines).strip()


def transcribe_with_faster_whisper(
    audio_path: str | Path,
    model_name: str = "turbo",
    language: str | None = None,
    beam_size: int = 5,
    word_timestamps: bool = False,
    vad_filter: bool = True,
    device: str | None = None,
    compute_type: str | None = None,
) -> TranscriptionResult:
    """
    使用 faster-whisper 对音频执行转录。

    依赖在函数内部导入，这样本地开发时不会因为未安装
    faster-whisper 而导致整个模块无法导入。
    """
    from faster_whisper import WhisperModel

    resolved_device, resolved_compute_type = resolve_transcription_runtime(probe_cuda_available())
    resolved_device = device or resolved_device
    resolved_compute_type = compute_type or resolved_compute_type

    model = WhisperModel(
        model_name,
        device=resolved_device,
        compute_type=resolved_compute_type,
    )
    segments, info = model.transcribe(
        str(audio_path),
        beam_size=beam_size,
        language=language,
        word_timestamps=word_timestamps,
        vad_filter=vad_filter,
    )

    segment_payloads: list[dict] = []
    for segment in segments:
        payload = {
            "start": segment.start,
            "end": segment.end,
            "text": segment.text,
        }
        if word_timestamps and getattr(segment, "words", None):
            payload["words"] = [
                {
                    "start": word.start,
                    "end": word.end,
                    "word": word.word,
                    "probability": getattr(word, "probability", None),
                }
                for word in segment.words
            ]
        segment_payloads.append(payload)

    transcript_text = render_plain_transcript(segment_payloads)
    return TranscriptionResult(
        text=transcript_text,
        language=info.language,
        language_probability=info.language_probability,
        model_name=model_name,
        device=resolved_device,
        compute_type=resolved_compute_type,
        segments=segment_payloads,
    )


def write_text_file(file_path: str | Path, content: str) -> str:
    """将文本内容写入文件，并确保父目录存在。"""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def write_json_file(file_path: str | Path, payload: dict) -> str:
    """将 JSON 数据写入文件，并确保输出格式稳定。"""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return str(path)


def build_workflow_metadata(
    download_result: VideoDownloadResult,
    transcription_result: TranscriptionResult,
    paths: WorkflowPaths,
) -> dict:
    """构建单条任务的元数据文件内容。"""
    return {
        "note_id": download_result.note_id,
        "note_url": download_result.note_url,
        "resolved_url": download_result.resolved_url,
        "video": download_result.to_dict(),
        "transcription": asdict(transcription_result),
        "outputs": {
            "drive_task_dir": paths.drive_task_dir,
            "drive_video_path": download_result.output_path,
            "drive_transcript_path": paths.drive_transcript_path,
            "drive_metadata_path": paths.drive_metadata_path,
        },
    }


def download_video_for_workflow(
    video_url: str,
    paths: WorkflowPaths,
    is_xiaohongshu: bool,
    cookies: dict | None = None,
    prefer_codec: str = "hevc",
    quality_index: int = 0,
) -> VideoDownloadResult:
    """
    按来源选择下载器。

    小红书继续复用现有签名流解析；其他域名交给 yt-dlp，
    但输出目录仍然使用流水线已有 Drive 目录。
    """
    if is_xiaohongshu:
        return download_xhs_video_result(
            note_url=video_url,
            output_dir=paths.drive_video_dir,
            prefer_codec=prefer_codec,
            quality_index=quality_index,
            cookies=cookies,
        )

    return download_yt_dlp_video_result(
        video_url=video_url,
        output_dir=paths.drive_video_dir,
        task_id=paths.note_id,
        media_type="video",
    )


def build_failed_workflow_result(note_url: str, error: Exception) -> WorkflowResult:
    """构建失败结果，确保非小红书 URL 失败时也不会再次触发 note_id 解析错误。"""
    try:
        _, task_id, _ = resolve_workflow_identity(note_url)
    except Exception:
        task_id = build_generic_task_id(note_url)

    return WorkflowResult(
        note_id=task_id,
        note_url=note_url,
        status="failed",
        video_path="",
        transcript_path="",
        metadata_path="",
        transcript_preview="",
        error=str(error),
    )


def run_single_workflow(
    note_url: str,
    cookies: dict | None = None,
    drive_root: str | Path = DEFAULT_DRIVE_ROOT,
    local_root: str | Path = DEFAULT_LOCAL_ROOT,
    prefer_codec: str = "hevc",
    quality_index: int = 0,
    model_name: str = "turbo",
    language: str | None = None,
    beam_size: int = 5,
    word_timestamps: bool = False,
    vad_filter: bool = True,
    skip_existing: bool = True,
) -> WorkflowResult:
    """
    执行单条 URL 的完整工作流。

    流程固定为：
      1. 解析 note_id
      2. 检查 Drive 中是否已有结果
      3. 在本地临时目录下载视频
      4. 抽取音频并执行转录
      5. 将最终结果复制到 Drive
    """
    resolved_url, note_id, is_xiaohongshu = resolve_workflow_identity(note_url)
    runtime_cookies = merge_cookies(cookies) if is_xiaohongshu else {}
    paths = build_workflow_paths(note_id=note_id, local_root=local_root, drive_root=drive_root)

    if skip_existing and has_existing_drive_outputs(paths.drive_task_dir):
        drive_dir = Path(paths.drive_task_dir)
        existing_video = find_existing_media_output(drive_dir)
        preview = ""
        transcript_path = drive_dir / "transcript.txt"
        if transcript_path.exists():
            preview = transcript_path.read_text(encoding="utf-8")[:120]
        return WorkflowResult(
            note_id=note_id,
            note_url=note_url,
            status="skipped",
            video_path=str(existing_video),
            transcript_path=str(transcript_path),
            metadata_path=str(drive_dir / "metadata.json"),
            transcript_preview=preview,
        )

    Path(paths.local_task_dir).mkdir(parents=True, exist_ok=True)
    Path(paths.drive_task_dir).mkdir(parents=True, exist_ok=True)

    download_result = download_video_for_workflow(
        video_url=resolved_url,
        paths=paths,
        is_xiaohongshu=is_xiaohongshu,
        cookies=runtime_cookies,
        prefer_codec=prefer_codec,
        quality_index=quality_index,
    )
    extract_audio_track(download_result.output_path, paths.local_audio_path)
    transcription_result = transcribe_with_faster_whisper(
        audio_path=paths.local_audio_path,
        model_name=model_name,
        language=language,
        beam_size=beam_size,
        word_timestamps=word_timestamps,
        vad_filter=vad_filter,
    )

    write_text_file(paths.drive_transcript_path, transcription_result.text)
    metadata = build_workflow_metadata(download_result, transcription_result, paths)
    write_json_file(paths.drive_metadata_path, metadata)

    # 音频只是中间文件，复制完成后删除可减少 Colab 临时盘占用。
    Path(paths.local_audio_path).unlink(missing_ok=True)

    return WorkflowResult(
        note_id=note_id,
        note_url=note_url,
        status="success",
        video_path=download_result.output_path,
        transcript_path=paths.drive_transcript_path,
        metadata_path=paths.drive_metadata_path,
        transcript_preview=transcription_result.text[:120],
    )


def run_colab_workflow(
    note_urls: Iterable[str],
    cookies: dict | None = None,
    drive_root: str | Path = DEFAULT_DRIVE_ROOT,
    local_root: str | Path = DEFAULT_LOCAL_ROOT,
    prefer_codec: str = "hevc",
    quality_index: int = 0,
    model_name: str = "turbo",
    language: str | None = None,
    beam_size: int = 5,
    word_timestamps: bool = False,
    vad_filter: bool = True,
    skip_existing: bool = True,
    mount_drive: bool = True,
    force_remount: bool = False,
) -> list[WorkflowResult]:
    """
    执行批量 Colab 工作流。

    每条 URL 的失败彼此隔离，单条失败不会中断整批任务。
    """
    urls = normalize_note_urls(note_urls)
    if not urls:
        raise ValueError("至少需要提供一条小红书 URL")

    if mount_drive:
        mount_google_drive(force_remount=force_remount)

    results: list[WorkflowResult] = []
    for note_url in urls:
        try:
            result = run_single_workflow(
                note_url=note_url,
                cookies=cookies,
                drive_root=drive_root,
                local_root=local_root,
                prefer_codec=prefer_codec,
                quality_index=quality_index,
                model_name=model_name,
                language=language,
                beam_size=beam_size,
                word_timestamps=word_timestamps,
                vad_filter=vad_filter,
                skip_existing=skip_existing,
            )
        except Exception as error:
            result = build_failed_workflow_result(note_url, error)
        results.append(result)
    return results


def load_urls_from_file(file_path: str | Path) -> list[str]:
    """从文本文件中读取 URL 列表，每行一条。"""
    return normalize_note_urls(Path(file_path).read_text(encoding="utf-8").splitlines())


def parse_args() -> argparse.Namespace:
    """解析命令行参数，方便在 Colab 里直接 `!python` 运行。"""
    parser = argparse.ArgumentParser(description="Colab 小红书视频下载与转录流水线")
    parser.add_argument("--url", action="append", default=[], help="小红书笔记 URL，可重复传入")
    parser.add_argument("--url-file", help="包含多个 URL 的文本文件，每行一条")
    parser.add_argument("--cookies-json", help="运行时注入的 Cookie JSON 字符串")
    parser.add_argument("--drive-root", default=str(DEFAULT_DRIVE_ROOT), help="Drive 输出根目录")
    parser.add_argument("--local-root", default=str(DEFAULT_LOCAL_ROOT), help="Colab 本地暂存根目录")
    parser.add_argument("--prefer-codec", default="hevc", help="下载时优先选择的视频编码")
    parser.add_argument("--quality-index", type=int, default=0, help="画质索引，0 表示最高画质")
    parser.add_argument("--model-name", default="turbo", help="faster-whisper 模型名")
    parser.add_argument("--language", help="显式指定转录语言，例如 zh")
    parser.add_argument("--beam-size", type=int, default=5, help="转录 beam size")
    parser.add_argument("--word-timestamps", action="store_true", help="是否输出词级时间戳")
    parser.add_argument("--disable-vad-filter", action="store_true", help="禁用静音过滤")
    parser.add_argument("--no-skip-existing", action="store_true", help="即使已有结果也重新执行")
    parser.add_argument("--no-mount-drive", action="store_true", help="跳过 Drive 挂载")
    parser.add_argument("--force-remount", action="store_true", help="强制重新挂载 Drive")
    return parser.parse_args()


def main() -> int:
    """命令行入口，打印批量任务执行摘要。"""
    args = parse_args()
    urls = list(args.url)
    if args.url_file:
        urls.extend(load_urls_from_file(args.url_file))

    results = run_colab_workflow(
        note_urls=urls,
        cookies=parse_cookie_json(args.cookies_json),
        drive_root=args.drive_root,
        local_root=args.local_root,
        prefer_codec=args.prefer_codec,
        quality_index=args.quality_index,
        model_name=args.model_name,
        language=args.language,
        beam_size=args.beam_size,
        word_timestamps=args.word_timestamps,
        vad_filter=not args.disable_vad_filter,
        skip_existing=not args.no_skip_existing,
        mount_drive=not args.no_mount_drive,
        force_remount=args.force_remount,
    )

    for result in results:
        print(f"[{result.status}] {result.note_id}")
        if result.error:
            print(f"  error: {result.error}")
        if result.video_path:
            print(f"  video: {result.video_path}")
        if result.transcript_path:
            print(f"  transcript: {result.transcript_path}")
        if result.transcript_preview:
            print(f"  preview: {result.transcript_preview[:80]}")

    return 0 if all(result.status != "failed" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
