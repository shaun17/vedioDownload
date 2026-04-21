"""
yt_dlp_downloader 纯逻辑测试。

这些测试通过 fake yt-dlp 对象验证参数和结果结构，不触网、不真实下载。
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from yt_dlp_downloader import (
    DEFAULT_YT_DLP_AUDIO_FORMAT,
    DEFAULT_YT_DLP_OUTPUT_DIR,
    DEFAULT_YT_DLP_VIDEO_FORMAT,
    build_generic_task_id,
    build_yt_dlp_options,
    download_yt_dlp_video_result,
    load_yt_dlp_module,
    parse_cli_arguments,
    resolve_downloaded_file,
    to_int,
)


class FakeYoutubeDL:
    """模拟 yt-dlp 的上下文管理器和核心下载 API。"""

    latest_options = None

    def __init__(self, options):
        self.options = options
        FakeYoutubeDL.latest_options = options
        extension = "mp3" if "postprocessors" in options else "mp4"
        self.output_path = Path(options["outtmpl"]["default"].replace("%(ext)s", extension))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, video_url, download=True):
        """模拟下载完成，并写入一个可被定位的 mp4 文件。"""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_bytes(b"video")
        return {
            "webpage_url": video_url,
            "original_url": video_url,
            "vcodec": "h264",
            "acodec": "aac",
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "tbr": 2500,
            "ext": self.output_path.suffix.lstrip("."),
            "requested_downloads": [{"filepath": str(self.output_path)}],
        }

    def sanitize_info(self, info):
        """测试场景不需要额外清洗，直接返回原始元数据。"""
        return info

    def prepare_filename(self, _info):
        """返回 yt-dlp 预估的输出文件名。"""
        return str(self.output_path)


class FakeYtDlpModule:
    """提供和 yt_dlp 模块相同的 YoutubeDL 入口。"""

    YoutubeDL = FakeYoutubeDL


class TestGenericTaskId:
    def test_build_generic_task_id_is_stable(self):
        url = "https://example.com/watch?v=abc"
        assert build_generic_task_id(url) == build_generic_task_id(url)
        assert build_generic_task_id(url).startswith("ytdlp_")

    def test_build_generic_task_id_distinguishes_urls(self):
        assert build_generic_task_id("https://example.com/a") != build_generic_task_id(
            "https://example.com/b"
        )


class TestYtDlpOptions:
    def test_build_audio_options_extracts_mp3_and_uses_stable_filename(self, tmp_path):
        options = build_yt_dlp_options(tmp_path, "ytdlp_abc", media_type="audio", quiet=True)
        assert options["format"] == DEFAULT_YT_DLP_AUDIO_FORMAT
        assert options["postprocessors"][0]["key"] == "FFmpegExtractAudio"
        assert options["postprocessors"][0]["preferredcodec"] == "mp3"
        assert options["postprocessors"][0]["preferredquality"] == "0"
        assert options["noplaylist"] is True
        assert options["quiet"] is True
        assert options["outtmpl"]["default"].endswith("ytdlp_abc.%(ext)s")

    def test_build_video_options_prefers_hls_before_direct_video(self, tmp_path):
        options = build_yt_dlp_options(tmp_path, "ytdlp_abc", media_type="video")
        assert options["format"] == DEFAULT_YT_DLP_VIDEO_FORMAT
        assert "protocol^=m3u8" in options["format"]
        assert options["merge_output_format"] == "mp4"

    def test_build_options_rejects_unknown_media_type(self, tmp_path):
        with pytest.raises(ValueError, match="media_type"):
            build_yt_dlp_options(tmp_path, "ytdlp_abc", media_type="bad")


class TestHelpers:
    def test_to_int_handles_empty_and_numeric_values(self):
        assert to_int(None) == 0
        assert to_int("30.5") == 30
        assert to_int("bad", default=7) == 7

    def test_resolve_downloaded_file_uses_requested_downloads(self, tmp_path):
        output = tmp_path / "ytdlp_abc.mp4"
        output.write_bytes(b"video")
        result = resolve_downloaded_file(
            tmp_path,
            "ytdlp_abc",
            {"requested_downloads": [{"filepath": str(output)}]},
        )
        assert result == output

    @patch("yt_dlp_downloader.importlib.import_module", side_effect=ImportError)
    def test_missing_yt_dlp_dependency_raises_clear_error(self, _mock_import):
        with pytest.raises(RuntimeError, match="pip install"):
            load_yt_dlp_module()


class TestDownloadYtDlpVideoResult:
    @patch("yt_dlp_downloader.load_yt_dlp_module", return_value=FakeYtDlpModule)
    def test_download_returns_pipeline_compatible_result(self, _mock_loader, tmp_path):
        url = "https://example.com/watch?v=abc"
        task_id = "ytdlp_custom"

        result = download_yt_dlp_video_result(
            video_url=url,
            output_dir=tmp_path,
            task_id=task_id,
            quiet=True,
        )

        assert result.note_id == task_id
        assert result.note_url == url
        assert result.resolved_url == url
        assert result.output_path.endswith("ytdlp_custom.mp3")
        assert result.filename == "ytdlp_custom.mp3"
        assert result.codec == "h264"
        assert result.audio_codec == "aac"
        assert result.width == 1920
        assert result.height == 1080
        assert result.fps == 30
        assert result.avg_bitrate == 2_500_000
        assert result.size == 5
        assert result.format == "mp3"
        assert Path(result.output_path).exists()
        assert FakeYoutubeDL.latest_options["quiet"] is True
        assert FakeYoutubeDL.latest_options["format"] == DEFAULT_YT_DLP_AUDIO_FORMAT


class TestCliArguments:
    def test_parse_cli_arguments_uses_default_output_dir(self):
        video_url, output_dir, media_type = parse_cli_arguments([
            "yt_dlp_downloader.py",
            "https://example.com/video",
        ])
        assert video_url == "https://example.com/video"
        assert output_dir == DEFAULT_YT_DLP_OUTPUT_DIR
        assert media_type == "audio"

    def test_parse_cli_arguments_keeps_explicit_output_dir(self):
        _, output_dir, media_type = parse_cli_arguments([
            "yt_dlp_downloader.py",
            "https://example.com/video",
            "./custom",
            "video",
        ])
        assert output_dir == "./custom"
        assert media_type == "video"
