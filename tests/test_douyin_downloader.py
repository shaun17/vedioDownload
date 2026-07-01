"""
抖音下载器纯逻辑测试。

这些测试不触网，通过 fake session 验证 share 页面解析和媒体下载链路。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from douyin_downloader import (
    build_douyin_share_url,
    download_douyin_video_result,
    extract_douyin_aweme,
    extract_douyin_video_id,
    extract_router_data,
)


SAMPLE_ROUTER_HTML = """
<html><body>
<script>window._ROUTER_DATA = {"loaderData":{"video_(id)\\u002Fpage":{"videoInfoRes":{"item_list":[{"aweme_id":"7646960381037995690","desc":"抖音文案","video":{"play_addr":{"uri":"v0200","url_list":["https:\\u002F\\u002Faweme.snssdk.com\\u002Faweme\\u002Fv1\\u002Fplaywm\\u002F?video_id=v0200"]},"width":886,"height":1920,"duration":250310}}]}}},"errors":null}</script>
</body></html>
"""


class FakeResponse:
    """模拟 requests 响应对象。"""

    def __init__(self, text="", body=b"", url="https://example.com/video.mp4"):
        self.text = text
        self.body = body
        self.url = url
        self.headers = {"Content-Length": str(len(body))} if body else {}

    def raise_for_status(self):
        """测试响应默认都是成功状态。"""

    def iter_content(self, chunk_size=65536):
        """按 requests 流式接口返回内容。"""
        yield self.body


class FakeSession:
    """模拟抖音 share 页面请求和媒体下载请求。"""

    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        """根据 URL 返回页面或媒体内容。"""
        self.calls.append((url, kwargs))
        if "share/video" in url:
            return FakeResponse(text=SAMPLE_ROUTER_HTML, url=url)
        return FakeResponse(body=b"video-bytes", url=url)


class TestDouyinIdentity:
    def test_extract_video_id_from_video_url(self):
        assert (
            extract_douyin_video_id("https://www.douyin.com/video/7646960381037995690")
            == "7646960381037995690"
        )

    def test_extract_video_id_from_query_value(self):
        assert (
            extract_douyin_video_id("https://www.douyin.com/?modal_id=7646960381037995690")
            == "7646960381037995690"
        )

    def test_invalid_url_raises_clear_error(self):
        with pytest.raises(ValueError, match="抖音视频 ID"):
            extract_douyin_video_id("https://www.douyin.com/")

    def test_share_url_is_built_from_video_url(self):
        assert build_douyin_share_url(
            "https://www.douyin.com/video/7646960381037995690"
        ) == "https://www.douyin.com/share/video/7646960381037995690/"


class TestRouterData:
    def test_extract_router_data_from_share_html(self):
        data = extract_router_data(SAMPLE_ROUTER_HTML)
        assert "loaderData" in data

    def test_extract_aweme_from_router_data(self):
        aweme = extract_douyin_aweme(SAMPLE_ROUTER_HTML)
        assert aweme["aweme_id"] == "7646960381037995690"
        assert aweme["desc"] == "抖音文案"
        assert aweme["video"]["play_addr"]["url_list"][0].startswith("https://aweme.snssdk.com")


class TestDownloadDouyinVideoResult:
    def test_download_uses_share_page_play_addr_without_browser_cookie(self, tmp_path):
        session = FakeSession()

        result = download_douyin_video_result(
            video_url="https://www.douyin.com/video/7646960381037995690",
            output_dir=tmp_path,
            session=session,
        )

        assert result.note_id == "douyin_7646960381037995690"
        assert result.note_url == "https://www.douyin.com/video/7646960381037995690"
        assert result.resolved_url == "https://www.douyin.com/share/video/7646960381037995690/"
        assert result.output_path.endswith("douyin_7646960381037995690.mp4")
        assert result.width == 886
        assert result.height == 1920
        assert result.size == len(b"video-bytes")
        assert Path(result.output_path).read_bytes() == b"video-bytes"
        assert session.calls[0][0] == "https://www.douyin.com/share/video/7646960381037995690/"
        assert session.calls[1][0].startswith("https://aweme.snssdk.com/aweme/v1/playwm/")
