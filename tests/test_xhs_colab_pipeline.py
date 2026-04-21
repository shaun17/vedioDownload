"""
Colab 流水线的纯逻辑测试。

这些测试不依赖 Colab、网络或 faster-whisper，
只验证路径、配置解析和跳过逻辑是否正确。
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from xhs_downloader import VideoDownloadResult

from xhs_colab_pipeline import (
    TranscriptionResult,
    build_generic_task_id,
    build_workflow_paths,
    find_existing_media_output,
    has_existing_drive_outputs,
    is_xiaohongshu_url,
    normalize_note_urls,
    parse_cookie_json,
    render_plain_transcript,
    resolve_workflow_identity,
    resolve_transcription_runtime,
    run_single_workflow,
)


class TestParseCookieJson:
    def test_empty_cookie_json_returns_empty_dict(self):
        assert parse_cookie_json(None) == {}

    def test_valid_cookie_json_returns_dict(self):
        result = parse_cookie_json('{"a1":"token","web_session":"session"}')
        assert result == {"a1": "token", "web_session": "session"}

    def test_non_object_cookie_json_raises(self):
        with pytest.raises(ValueError, match="JSON 对象"):
            parse_cookie_json('["not", "object"]')


class TestNormalizeNoteUrls:
    def test_blank_and_duplicate_urls_are_removed(self):
        urls = normalize_note_urls([
            "",
            " https://www.xiaohongshu.com/explore/69c7e067000000002202809d ",
            "https://www.xiaohongshu.com/explore/69c7e067000000002202809d",
        ])
        assert urls == ["https://www.xiaohongshu.com/explore/69c7e067000000002202809d"]


class TestResolveTranscriptionRuntime:
    def test_gpu_runtime_uses_cuda_float16(self):
        assert resolve_transcription_runtime(True) == ("cuda", "float16")

    def test_cpu_runtime_uses_cpu_int8(self):
        assert resolve_transcription_runtime(False) == ("cpu", "int8")


class TestBuildWorkflowPaths:
    def test_paths_are_grouped_by_note_id(self, tmp_path):
        paths = build_workflow_paths(
            note_id="69c7e067000000002202809d",
            local_root=tmp_path / "local",
            drive_root=tmp_path / "drive",
        )
        assert paths.local_task_dir.endswith("/69c7e067000000002202809d")
        assert paths.drive_task_dir.endswith("/69c7e067000000002202809d")
        assert paths.drive_video_dir.endswith("/69c7e067000000002202809d")
        assert paths.local_audio_path.endswith("/audio.wav")
        assert paths.drive_transcript_path.endswith("/transcript.txt")
        assert paths.drive_metadata_path.endswith("/metadata.json")


class TestExistingDriveOutputs:
    def test_only_full_output_set_counts_as_existing(self, tmp_path):
        task_dir = tmp_path / "drive" / "69c7e067000000002202809d"
        task_dir.mkdir(parents=True)
        assert has_existing_drive_outputs(task_dir) is False

        (task_dir / "video.mp4").write_bytes(b"video")
        (task_dir / "transcript.txt").write_text("hello", encoding="utf-8")
        (task_dir / "metadata.json").write_text("{}", encoding="utf-8")
        assert has_existing_drive_outputs(task_dir) is True

    def test_audio_output_also_counts_as_existing_media(self, tmp_path):
        task_dir = tmp_path / "drive" / "ytdlp_abc"
        task_dir.mkdir(parents=True)
        (task_dir / "audio.mp3").write_bytes(b"audio")
        (task_dir / "transcript.txt").write_text("hello", encoding="utf-8")
        (task_dir / "metadata.json").write_text("{}", encoding="utf-8")
        assert has_existing_drive_outputs(task_dir) is True
        assert find_existing_media_output(task_dir).name == "audio.mp3"


class TestRenderPlainTranscript:
    def test_segments_are_joined_by_newline(self):
        text = render_plain_transcript([
            {"text": " 第一行 "},
            {"text": ""},
            {"text": "第二行"},
        ])
        assert text == "第一行\n第二行"


class TestRunSingleWorkflowSkip:
    def test_existing_drive_outputs_are_skipped(self, tmp_path):
        note_id = "69c7e067000000002202809d"
        drive_task_dir = tmp_path / "drive" / note_id
        drive_task_dir.mkdir(parents=True)
        (drive_task_dir / "video.mp4").write_bytes(b"video")
        (drive_task_dir / "transcript.txt").write_text("现有文稿", encoding="utf-8")
        (drive_task_dir / "metadata.json").write_text("{}", encoding="utf-8")

        result = run_single_workflow(
            note_url=f"https://www.xiaohongshu.com/explore/{note_id}",
            drive_root=tmp_path / "drive",
            local_root=tmp_path / "local",
            skip_existing=True,
        )
        assert result.status == "skipped"
        assert result.transcript_preview == "现有文稿"

    def test_existing_generic_outputs_are_skipped(self, tmp_path):
        url = "https://example.com/watch?v=abc"
        task_id = build_generic_task_id(url)
        drive_task_dir = tmp_path / "drive" / task_id
        drive_task_dir.mkdir(parents=True)
        (drive_task_dir / "audio.mp3").write_bytes(b"audio")
        (drive_task_dir / "transcript.txt").write_text("现有通用文稿", encoding="utf-8")
        (drive_task_dir / "metadata.json").write_text("{}", encoding="utf-8")

        result = run_single_workflow(
            note_url=url,
            drive_root=tmp_path / "drive",
            local_root=tmp_path / "local",
            skip_existing=True,
        )
        assert result.status == "skipped"
        assert result.note_id == task_id
        assert result.video_path.endswith("audio.mp3")
        assert result.transcript_preview == "现有通用文稿"


class TestWorkflowIdentity:
    def test_xiaohongshu_domains_use_xhs_downloader(self):
        assert is_xiaohongshu_url("https://www.xiaohongshu.com/explore/abc") is True
        assert is_xiaohongshu_url("https://xhslink.com/a/b") is True
        assert is_xiaohongshu_url("https://example.com/watch?v=abc") is False

    def test_generic_identity_uses_stable_hash_id(self):
        url = "https://example.com/watch?v=abc"
        resolved_url, task_id, is_xhs = resolve_workflow_identity(url)
        assert resolved_url == url
        assert task_id == build_generic_task_id(url)
        assert is_xhs is False


class TestRunSingleWorkflowRouting:
    @patch("xhs_colab_pipeline.transcribe_with_faster_whisper")
    @patch("xhs_colab_pipeline.extract_audio_track")
    @patch("xhs_colab_pipeline.download_xhs_video_result")
    @patch("xhs_colab_pipeline.download_yt_dlp_video_result")
    def test_generic_url_uses_yt_dlp_then_existing_transcription_flow(
        self,
        mock_yt_dlp_download,
        mock_xhs_download,
        mock_extract_audio,
        mock_transcribe,
        tmp_path,
    ):
        url = "https://example.com/watch?v=abc"
        task_id = build_generic_task_id(url)
        video_path = tmp_path / "drive" / task_id / f"{task_id}.mp3"
        mock_yt_dlp_download.return_value = VideoDownloadResult(
            note_id=task_id,
            note_url=url,
            resolved_url=url,
            output_path=str(video_path),
            filename=video_path.name,
            codec="h264",
            audio_codec="aac",
            width=1920,
            height=1080,
            fps=30,
            avg_bitrate=1_000_000,
            size=1024,
            format="mp3",
            quality_index=0,
        )
        mock_extract_audio.return_value = str(tmp_path / "local" / task_id / "audio.wav")
        mock_transcribe.return_value = TranscriptionResult(
            text="通用视频文稿",
            language="zh",
            language_probability=1.0,
            model_name="turbo",
            device="cpu",
            compute_type="int8",
            segments=[{"text": "通用视频文稿"}],
        )

        result = run_single_workflow(
            note_url=url,
            drive_root=tmp_path / "drive",
            local_root=tmp_path / "local",
            skip_existing=False,
        )

        assert result.status == "success"
        assert result.note_id == task_id
        assert result.transcript_preview == "通用视频文稿"
        mock_xhs_download.assert_not_called()
        mock_yt_dlp_download.assert_called_once()
        assert mock_yt_dlp_download.call_args.kwargs["video_url"] == url
        assert mock_yt_dlp_download.call_args.kwargs["task_id"] == task_id
        assert mock_yt_dlp_download.call_args.kwargs["output_dir"].endswith(task_id)
        assert mock_yt_dlp_download.call_args.kwargs["media_type"] == "video"
        mock_extract_audio.assert_called_once_with(str(video_path), str(tmp_path / "local" / task_id / "audio.wav"))
        mock_transcribe.assert_called_once()

    @patch("xhs_colab_pipeline.transcribe_with_faster_whisper")
    @patch("xhs_colab_pipeline.extract_audio_track")
    @patch("xhs_colab_pipeline.download_xhs_video_result")
    @patch("xhs_colab_pipeline.download_yt_dlp_video_result")
    def test_xiaohongshu_url_keeps_existing_downloader(
        self,
        mock_yt_dlp_download,
        mock_xhs_download,
        mock_extract_audio,
        mock_transcribe,
        tmp_path,
    ):
        note_id = "69c7e067000000002202809d"
        url = f"https://www.xiaohongshu.com/explore/{note_id}"
        video_path = tmp_path / "drive" / note_id / f"xhs_{note_id}.mp4"
        mock_xhs_download.return_value = VideoDownloadResult(
            note_id=note_id,
            note_url=url,
            resolved_url=url,
            output_path=str(video_path),
            filename=video_path.name,
            codec="h264",
            audio_codec="aac",
            width=1280,
            height=720,
            fps=30,
            avg_bitrate=1_000_000,
            size=1024,
            format="mp4",
            quality_index=0,
        )
        mock_extract_audio.return_value = str(tmp_path / "local" / note_id / "audio.wav")
        mock_transcribe.return_value = TranscriptionResult(
            text="小红书文稿",
            language="zh",
            language_probability=1.0,
            model_name="turbo",
            device="cpu",
            compute_type="int8",
            segments=[{"text": "小红书文稿"}],
        )

        result = run_single_workflow(
            note_url=url,
            drive_root=tmp_path / "drive",
            local_root=tmp_path / "local",
            skip_existing=False,
        )

        assert result.status == "success"
        assert result.note_id == note_id
        mock_yt_dlp_download.assert_not_called()
        mock_xhs_download.assert_called_once()
        assert mock_xhs_download.call_args.kwargs["note_url"] == url
        assert mock_xhs_download.call_args.kwargs["output_dir"].endswith(note_id)
