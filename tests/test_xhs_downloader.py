"""
xhs_downloader 纯函数单元测试（不触网）。

覆盖：
  - parse_note_url       URL → (note_id, xsec_token, xsec_source)
  - resolve_note_url     xhslink 短链跟随
  - extract_initial_state  HTML → dict
  - find_note_in_state   __INITIAL_STATE__ 里按 id 取 note
  - extract_streams      note dict → 按分辨率排序的 stream 列表
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from xhs_downloader import (
    parse_note_url,
    resolve_note_url,
    extract_initial_state,
    find_note_in_state,
    extract_streams,
    build_session,
    fetch_note_detail,
    _check_not_blocked,
    XHS_HOME,
    merge_cookies,
    choose_stream,
    build_output_filename,
    parse_cli_arguments,
    DEFAULT_CLI_OUTPUT_DIR,
)


# ─────────────────────────  parse_note_url  ─────────────────────────
class TestParseNoteUrl:
    VALID_ID = "69c7e067000000002202809d"

    def test_explore_with_token(self):
        nid, tok, src = parse_note_url(
            f"https://www.xiaohongshu.com/explore/{self.VALID_ID}?xsec_token=XYZ&xsec_source=pc_feed"
        )
        assert nid == self.VALID_ID
        assert tok == "XYZ"
        assert src == "pc_feed"

    def test_discovery_path(self):
        nid, tok, src = parse_note_url(
            f"https://www.xiaohongshu.com/discovery/item/{self.VALID_ID}"
        )
        assert nid == self.VALID_ID
        assert tok == ""
        assert src == "pc_user"  # 默认值

    def test_trailing_slash(self):
        nid, _, _ = parse_note_url(f"https://www.xiaohongshu.com/explore/{self.VALID_ID}/")
        assert nid == self.VALID_ID

    def test_shell_escaped_backslashes_stripped(self):
        # zsh 下复制粘贴粘来的 URL 常带转义反斜杠
        raw = (
            r"https://www.xiaohongshu.com/explore/69c7e067000000002202809d"
            r"\?xsec_token\=ABZ\=\&xsec_source\=pc_feed"
        )
        nid, tok, src = parse_note_url(raw)
        assert nid == "69c7e067000000002202809d"
        assert tok == "ABZ="
        assert src == "pc_feed"

    def test_invalid_note_id_raises(self):
        with pytest.raises(ValueError, match="note_id"):
            parse_note_url("https://www.xiaohongshu.com/explore/not-a-hex-id")


# ─────────────────────────  resolve_note_url  ───────────────────────
class TestResolveNoteUrl:
    def test_passes_through_full_url(self):
        url = "https://www.xiaohongshu.com/explore/abc"
        assert resolve_note_url(url) == url

    @patch("xhs_downloader.requests.get")
    def test_follows_short_link(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.url = "https://www.xiaohongshu.com/explore/abc?xsec_token=TOK"
        mock_get.return_value = mock_resp

        result = resolve_note_url("https://xhslink.com/ABC")
        assert "explore/abc" in result
        assert mock_get.call_args.kwargs.get("allow_redirects") is True


# ─────────────────────────  extract_initial_state  ──────────────────
class TestExtractInitialState:
    def test_basic_json(self):
        html = '<script>window.__INITIAL_STATE__={"a":1,"b":"test"}</script>'
        assert extract_initial_state(html) == {"a": 1, "b": "test"}

    def test_nested_object(self):
        html = '<script>window.__INITIAL_STATE__={"outer":{"inner":[1,2,3]}}</script>'
        state = extract_initial_state(html)
        assert state["outer"]["inner"] == [1, 2, 3]

    def test_undefined_replaced_with_null(self):
        html = '<script>window.__INITIAL_STATE__={"a":undefined,"b":2}</script>'
        assert extract_initial_state(html) == {"a": None, "b": 2}

    def test_missing_state_raises(self):
        with pytest.raises(RuntimeError, match="INITIAL_STATE"):
            extract_initial_state("<html>no state here</html>")


# ─────────────────────────  find_note_in_state  ─────────────────────
class TestFindNoteInState:
    def test_direct_lookup(self):
        state = {
            "note": {
                "noteDetailMap": {
                    "abc123": {"note": {"type": "video", "title": "t"}}
                }
            }
        }
        note = find_note_in_state(state, "abc123")
        assert note["type"] == "video"
        assert note["title"] == "t"

    def test_entry_without_note_wrapper(self):
        # 某些版本直接把 note 放在 map value 上
        state = {
            "note": {
                "noteDetailMap": {
                    "abc123": {"type": "video", "title": "t"}
                }
            }
        }
        note = find_note_in_state(state, "abc123")
        assert note["type"] == "video"

    def test_missing_raises(self):
        with pytest.raises(RuntimeError, match="未在"):
            find_note_in_state({"note": {"noteDetailMap": {}}}, "abc123")


# ─────────────────────────  extract_streams  ────────────────────────
class TestExtractStreams:
    def test_image_note_raises(self):
        with pytest.raises(ValueError, match="不是视频"):
            extract_streams({"type": "normal"})

    def test_empty_stream_raises(self):
        note = {"type": "video", "video": {"media": {"stream": {}}}}
        with pytest.raises(ValueError, match="视频流"):
            extract_streams(note)

    def test_snake_case_fields(self):
        note = {
            "type": "video",
            "video": {
                "media": {
                    "stream": {
                        "h265": [{
                            "master_url": "https://cdn/hevc.mp4",
                            "backup_urls": ["https://cdn2/hevc.mp4"],
                            "video_codec": "hevc",
                            "width": 1920, "height": 1080,
                            "fps": 30, "avg_bitrate": 2_000_000,
                            "size": 10_000_000, "format": "mp4",
                            "stream_type": 115,
                        }],
                    }
                }
            }
        }
        streams = extract_streams(note)
        assert len(streams) == 1
        s = streams[0]
        assert s["master_url"] == "https://cdn/hevc.mp4"
        assert s["backup_urls"] == ["https://cdn2/hevc.mp4"]
        assert s["codec"] == "hevc"
        assert s["width"] == 1920
        assert s["desc"] == "H265 1080p"

    def test_camel_case_fields_from_initial_state(self):
        # __INITIAL_STATE__ 里的字段可能是 camelCase
        note = {
            "type": "video",
            "video": {
                "media": {
                    "stream": {
                        "h264": [{
                            "masterUrl": "https://cdn/h264.mp4",
                            "backupUrls": [],
                            "videoCodec": "h264",
                            "width": 1280, "height": 720,
                            "fps": 30, "avgBitrate": 1_000_000,
                            "size": 5_000_000, "format": "mp4",
                        }],
                    }
                }
            }
        }
        streams = extract_streams(note)
        assert len(streams) == 1
        assert streams[0]["master_url"] == "https://cdn/h264.mp4"
        assert streams[0]["avg_bitrate"] == 1_000_000

    def test_sorted_by_resolution_desc(self):
        note = {
            "type": "video",
            "video": {
                "media": {
                    "stream": {
                        "h264": [{
                            "master_url": "https://cdn/720.mp4",
                            "video_codec": "h264",
                            "width": 1280, "height": 720,
                            "size": 5_000_000, "format": "mp4",
                        }],
                        "h265": [{
                            "master_url": "https://cdn/1080.mp4",
                            "video_codec": "hevc",
                            "width": 1920, "height": 1080,
                            "size": 10_000_000, "format": "mp4",
                        }],
                    }
                }
            }
        }
        streams = extract_streams(note)
        assert streams[0]["width"] == 1920  # 1080p 在前
        assert streams[1]["width"] == 1280

    # 放到类尾部
    pass


# ─────────────────────────  build_session  ──────────────────────────
class TestBuildSession:
    @patch("xhs_downloader.requests.Session")
    def test_guest_mode_warms_up_homepage(self, mock_sess_cls):
        mock_sess = MagicMock()
        mock_sess_cls.return_value = mock_sess

        build_session(user_cookies={})

        # 应访问首页预热
        mock_sess.get.assert_called_once()
        assert mock_sess.get.call_args.args[0] == XHS_HOME

    @patch("xhs_downloader.requests.Session")
    def test_guest_mode_with_no_cookies_arg(self, mock_sess_cls):
        mock_sess = MagicMock()
        mock_sess_cls.return_value = mock_sess

        build_session()

        mock_sess.get.assert_called_once()

    @patch("xhs_downloader.requests.Session")
    def test_auth_mode_skips_warmup(self, mock_sess_cls):
        mock_sess = MagicMock()
        mock_sess_cls.return_value = mock_sess

        build_session(user_cookies={"a1": "abc", "web_session": "xyz"})

        # 有登录态 cookie → 不预热
        mock_sess.get.assert_not_called()
        # cookie 也被写进 session
        mock_sess.cookies.update.assert_called_once()

    @patch("xhs_downloader.requests.Session")
    def test_empty_cookie_values_treated_as_guest(self, mock_sess_cls):
        mock_sess = MagicMock()
        mock_sess_cls.return_value = mock_sess

        build_session(user_cookies={"a1": "", "web_session": ""})

        # 空字符串视为游客，应走预热
        mock_sess.get.assert_called_once()

    @patch("xhs_downloader.requests.Session")
    def test_warmup_failure_is_swallowed(self, mock_sess_cls):
        import requests as _req
        mock_sess = MagicMock()
        mock_sess.get.side_effect = _req.ConnectionError("boom")
        mock_sess_cls.return_value = mock_sess

        # 预热异常不应抛出
        sess = build_session(user_cookies={})
        assert sess is mock_sess


# ─────────────────────────  风控检测  ────────────────────────────────
class TestCheckNotBlocked:
    def _resp(self, url="https://www.xiaohongshu.com/explore/abc", text="x" * 3000):
        r = MagicMock()
        r.url = url
        r.text = text
        return r

    def test_normal_page_passes(self):
        # 不抛异常即通过
        _check_not_blocked(
            self._resp(text="<html>" + "x" * 3000 + "window.__INITIAL_STATE__={}</html>")
        )

    def test_login_redirect_raises(self):
        with pytest.raises(RuntimeError, match="登录"):
            _check_not_blocked(
                self._resp(url="https://www.xiaohongshu.com/web-login/captcha?redirect=xxx")
            )

    def test_passport_redirect_raises(self):
        with pytest.raises(RuntimeError, match="登录"):
            _check_not_blocked(self._resp(url="https://passport.xiaohongshu.com/login"))

    def test_short_response_raises(self):
        with pytest.raises(RuntimeError, match="响应过短"):
            _check_not_blocked(self._resp(text="tiny"))

    def test_captcha_text_raises(self):
        with pytest.raises(RuntimeError, match="验证"):
            _check_not_blocked(self._resp(text="x" * 3000 + "滑动验证码"))

    def test_captcha_english_raises(self):
        with pytest.raises(RuntimeError, match="验证"):
            _check_not_blocked(self._resp(text="x" * 3000 + "captcha required"))


# ─────────────────────────  fetch 重试  ──────────────────────────────
class TestFetchNoteDetailRetry:
    def _make_ok_response(self, payload='{"ok":1}'):
        resp = MagicMock()
        resp.url = "https://www.xiaohongshu.com/explore/abc"
        resp.text = (
            "<html>" + "x" * 3000
            + "<script>window.__INITIAL_STATE__=" + payload + "</script></html>"
        )
        resp.raise_for_status = MagicMock()
        return resp

    @patch("xhs_downloader.time.sleep")
    @patch("xhs_downloader.build_session")
    def test_retry_then_success(self, mock_build, _mock_sleep):
        import requests as _req
        sess_fail = MagicMock()
        sess_fail.get.side_effect = _req.Timeout("first failed")

        sess_ok = MagicMock()
        sess_ok.get.return_value = self._make_ok_response()

        mock_build.side_effect = [sess_fail, sess_ok]

        state = fetch_note_detail(
            "https://www.xiaohongshu.com/explore/abc", max_retries=3
        )
        assert state == {"ok": 1}
        assert mock_build.call_count == 2  # 第一次失败后重建了 session

    @patch("xhs_downloader.time.sleep")
    @patch("xhs_downloader.build_session")
    def test_exhausted_raises(self, mock_build, _mock_sleep):
        import requests as _req
        sess = MagicMock()
        sess.get.side_effect = _req.Timeout("always bad")
        mock_build.return_value = sess

        with pytest.raises(RuntimeError, match="重试"):
            fetch_note_detail(
                "https://www.xiaohongshu.com/explore/abc", max_retries=2
            )
        assert sess.get.call_count == 2

    @patch("xhs_downloader.time.sleep")
    @patch("xhs_downloader.build_session")
    def test_retry_on_blocked_page(self, mock_build, _mock_sleep):
        # 第一次返回登录墙 → 应重试；第二次拿到正常页
        blocked_resp = MagicMock()
        blocked_resp.url = "https://www.xiaohongshu.com/web-login/xxx"
        blocked_resp.text = "x" * 3000
        blocked_resp.raise_for_status = MagicMock()

        sess1 = MagicMock()
        sess1.get.return_value = blocked_resp

        sess2 = MagicMock()
        sess2.get.return_value = self._make_ok_response('{"ok":2}')

        mock_build.side_effect = [sess1, sess2]

        state = fetch_note_detail(
            "https://www.xiaohongshu.com/explore/abc", max_retries=3
        )
        assert state == {"ok": 2}


class TestExtractStreamsSameResolution:
    def test_same_resolution_hevc_preferred(self):
        note = {
            "type": "video",
            "video": {
                "media": {
                    "stream": {
                        "h264": [{
                            "master_url": "https://cdn/264.mp4",
                            "video_codec": "h264",
                            "width": 1920, "height": 1080,
                            "size": 9_000_000, "format": "mp4",
                        }],
                        "h265": [{
                            "master_url": "https://cdn/265.mp4",
                            "video_codec": "hevc",
                            "width": 1920, "height": 1080,
                            "size": 7_000_000, "format": "mp4",
                        }],
                    }
                }
            }
        }
        streams = extract_streams(note)
        assert streams[0]["codec"] == "hevc"


class TestCookieHelpers:
    def test_merge_cookies_keeps_default_and_runtime_values(self):
        cookies = merge_cookies({"a1": "token-a1", "web_session": "session-token"})
        assert cookies["xsecappid"] == "xhs-pc-web"
        assert cookies["a1"] == "token-a1"
        assert cookies["web_session"] == "session-token"


class TestStreamSelectionHelpers:
    def test_choose_stream_prefers_requested_codec(self):
        streams = [
            {"codec": "h264", "width": 1280, "height": 720, "format": "mp4"},
            {"codec": "hevc", "width": 1280, "height": 720, "format": "mp4"},
        ]
        chosen, index = choose_stream(streams, prefer_codec="hevc", quality_index=0)
        assert chosen["codec"] == "hevc"
        assert index == 0

    def test_choose_stream_falls_back_to_first_candidate_when_index_too_large(self):
        streams = [
            {"codec": "h264", "width": 1280, "height": 720, "format": "mp4"},
        ]
        chosen, index = choose_stream(streams, prefer_codec="hevc", quality_index=99)
        assert chosen["codec"] == "h264"
        assert index == 0

    def test_build_output_filename_matches_legacy_pattern(self):
        filename = build_output_filename(
            "69c7e067000000002202809d",
            {"width": 1280, "height": 720, "codec": "h264", "format": "mp4"},
        )
        assert filename == "xhs_69c7e067000000002202809d_1280x720_h264.mp4"


class TestCliArgumentParsing:
    def test_parse_cli_arguments_uses_dedicated_default_output_dir(self):
        url, output_dir, codec, quality_index = parse_cli_arguments(
            ["xhs_downloader.py", "https://www.xiaohongshu.com/explore/69c7e067000000002202809d"]
        )
        assert url == "https://www.xiaohongshu.com/explore/69c7e067000000002202809d"
        assert output_dir == DEFAULT_CLI_OUTPUT_DIR
        assert codec == "hevc"
        assert quality_index == 0

    def test_parse_cli_arguments_keeps_explicit_output_dir(self):
        _, output_dir, codec, quality_index = parse_cli_arguments(
            [
                "xhs_downloader.py",
                "https://www.xiaohongshu.com/explore/69c7e067000000002202809d",
                "./custom-dir",
                "h264",
                "2",
            ]
        )
        assert output_dir == "./custom-dir"
        assert codec == "h264"
        assert quality_index == 2
