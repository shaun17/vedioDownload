"""
Colab 流水线的纯逻辑测试。

这些测试不依赖 Colab、网络或 faster-whisper，
只验证路径、配置解析和跳过逻辑是否正确。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from xhs_colab_pipeline import (
    build_workflow_paths,
    has_existing_drive_outputs,
    normalize_note_urls,
    parse_cookie_json,
    render_plain_transcript,
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
