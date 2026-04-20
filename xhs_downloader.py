"""
小红书视频下载器
原理：
  1. 携带 Cookie 请求小红书 Web API 获取 note 详情（含各码率视频流信息）
  2. 从响应中提取带签名 token 的 CDN masterUrl（格式为 MP4 + sign/t 参数）
  3. 使用 HTTP Range 请求分片拉取，写入本地文件

使用方式：
  python xhs_downloader.py <小红书笔记URL>

依赖：pip install requests tqdm
"""

import re
import sys
import time
import json
import random
import requests
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from tqdm import tqdm


# ─────────────────────────────────────────────
# 配置区：填入浏览器 Cookie（F12 → Network → 任意请求 → Cookie 头）
# ─────────────────────────────────────────────
DEFAULT_COOKIES = {
    # 默认游客模式：留空即可，首次请求会自动访问小红书首页获取匿名 a1/webId/gid。
    # 如需登录态下载（更稳定、可绕过部分风控），填入浏览器已登录状态下的 a1 + web_session。
    # "a1": "",
    # "web_session": "",
    "xsecappid": "xhs-pc-web",
}

XHS_HOME = "https://www.xiaohongshu.com/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.xiaohongshu.com/",
    "Origin": "https://www.xiaohongshu.com",
}


@dataclass
class VideoDownloadResult:
    """
    下载结果对象。

    这个结构专门给上层流水线使用，避免后续转录和存储阶段
    还要重新从文件名里反解元数据。
    """

    note_id: str
    note_url: str
    resolved_url: str
    output_path: str
    filename: str
    codec: str
    audio_codec: str
    width: int
    height: int
    fps: int
    avg_bitrate: int
    size: int
    format: str
    quality_index: int

    def to_dict(self) -> dict:
        """将结果对象转换为普通字典，便于写入 JSON 元数据。"""
        return asdict(self)


def merge_cookies(user_cookies: dict | None = None) -> dict:
    """
    合并默认 Cookie 与运行时传入 Cookie。

    这里保留 `xsecappid` 这类基础字段，同时允许上层在 Colab
    或命令行里动态注入登录态 Cookie，而不需要改源码。
    """
    cookies = dict(DEFAULT_COOKIES)
    if user_cookies:
        cookies.update({k: v for k, v in user_cookies.items() if v})
    return cookies


# ─────────────────────────────────────────────
# 1. 短链解析 + note_id / xsec_token 提取
# ─────────────────────────────────────────────
def resolve_note_url(url: str) -> str:
    """
    跟随 xhslink.com 短链重定向，返回最终的 explore/ 或 discovery/ URL；
    非短链直接原样返回。
    """
    if "xhslink.com" in url:
        resp = requests.get(
            url,
            headers=HEADERS,
            allow_redirects=True,
            timeout=10,
        )
        return resp.url
    return url


NOTE_ID_RE = re.compile(r"^[0-9a-f]{24}$")


def parse_note_url(url: str) -> tuple[str, str, str]:
    """
    支持格式：
      https://www.xiaohongshu.com/explore/{note_id}?xsec_token=...
      https://www.xiaohongshu.com/discovery/item/{note_id}
    同时吃掉 zsh 等 shell 粘贴时插入的转义反斜杠。
    """
    # 去掉 shell 粘贴带入的 \? \= \& 反斜杠
    url = url.replace("\\", "")
    parsed = urlparse(url)
    note_id = parsed.path.rstrip("/").split("/")[-1]
    if not NOTE_ID_RE.match(note_id):
        raise ValueError(
            f"无法从 URL 中提取有效的 note_id（应为 24 位十六进制），实际: {note_id!r}"
        )
    params = parse_qs(parsed.query)
    xsec_token = params.get("xsec_token", [""])[0]
    xsec_source = params.get("xsec_source", ["pc_user"])[0]
    return note_id, xsec_token, xsec_source


# ─────────────────────────────────────────────
# 2. 获取笔记详情（HTML + __INITIAL_STATE__ 提取，绕开 API 签名反爬）
# ─────────────────────────────────────────────
def build_session(user_cookies: dict = None) -> requests.Session:
    """
    构建请求会话：
      - 若 user_cookies 含有效 a1 或 web_session：直接使用（登录态路径）
      - 否则先 GET 小红书首页让服务端 Set-Cookie 下发匿名 a1/webId/gid（游客路径）
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    cookies = merge_cookies(user_cookies)
    has_auth = bool(cookies.get("a1") or cookies.get("web_session"))
    if cookies:
        session.cookies.update(cookies)

    if not has_auth:
        try:
            session.get(XHS_HOME, timeout=10, allow_redirects=True)
        except requests.RequestException:
            pass  # 预热失败也继续，部分笔记匿名直连仍可成功
    return session


BLOCK_KEYWORDS = ("captcha", "滑动验证", "人机验证", "访问受限", "请稍后再试")


def _check_not_blocked(resp) -> None:
    """
    识别风控/登录墙/验证码响应；识别到即抛 RuntimeError，让外层重试。
    """
    url_lower = (resp.url or "").lower()
    if "login" in url_lower or "passport" in url_lower or "captcha" in url_lower:
        raise RuntimeError(f"被重定向到登录/验证页: {resp.url}")

    text = resp.text or ""
    if len(text) < 1000:
        raise RuntimeError(f"响应过短（{len(text)} 字节），疑似风控拦截页")

    head = text[:5000]
    head_lower = head.lower()
    for kw in BLOCK_KEYWORDS:
        if kw in head or kw in head_lower:
            raise RuntimeError(f"响应含风控/验证码关键词: {kw!r}")


def fetch_note_detail(
    note_url: str,
    session: requests.Session = None,
    max_retries: int = 3,
    cookies: dict | None = None,
) -> dict:
    """
    请求笔记 Web 页面，从 window.__INITIAL_STATE__ 提取笔记状态 dict。

    重试策略：
      - 最多 max_retries 次，指数退避 (1.5^n + jitter)
      - 每次失败换一个全新的 session（重新预热匿名 a1），降低被标记 IP 的命中率
      - 下列任一情况都会触发重试：网络异常 / 4xx-5xx / 登录墙 / 验证码 / 响应过短 / 解析失败
    """
    last_err = None
    current_session = session
    for attempt in range(1, max_retries + 1):
        try:
            sess = current_session or build_session(cookies)
            resp = sess.get(note_url, timeout=15, allow_redirects=True)
            resp.raise_for_status()
            _check_not_blocked(resp)
            return extract_initial_state(resp.text)

        except (requests.RequestException, RuntimeError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            if attempt >= max_retries:
                break
            wait = 1.5 ** (attempt - 1) + random.uniform(0, 0.5)
            print(f"  ⚠ 第 {attempt}/{max_retries} 次请求失败: {type(e).__name__}: {e}")
            print(f"    {wait:.1f}s 后重建会话重试...")
            time.sleep(wait)
            current_session = None  # 强制重建 session，重新预热 a1

    raise RuntimeError(
        f"重试 {max_retries} 次仍无法获取笔记详情。最后错误: "
        f"{type(last_err).__name__}: {last_err}"
    )


def extract_initial_state(html: str) -> dict:
    """
    用正则从 HTML 中抽取 window.__INITIAL_STATE__ 并解析为 dict。
    小红书前端会把 undefined 直接写进 JSON，先替换为 null 再解析。
    """
    m = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>",
        html,
        re.DOTALL,
    )
    if not m:
        raise RuntimeError(
            "未能从页面提取 __INITIAL_STATE__，可能未登录 / cookie 失效 / 页面已改版"
        )
    raw = re.sub(r":\s*undefined\b", ":null", m.group(1))
    return json.loads(raw)


def find_note_in_state(state: dict, note_id: str) -> dict:
    """
    在 __INITIAL_STATE__.note.noteDetailMap 中按 id 取出笔记主体。
    兼容两种结构：map[id].note 或 map[id] 本身即 note。
    """
    note_map = state.get("note", {}).get("noteDetailMap", {})
    entry = note_map.get(note_id)
    if isinstance(entry, dict):
        note = entry.get("note") if isinstance(entry.get("note"), dict) else entry
        if note and "type" in note:
            return note
    raise RuntimeError(
        f"未在 __INITIAL_STATE__.note.noteDetailMap 中找到 {note_id}，"
        f"可能未登录 / cookie 失效 / 笔记不存在或被风控"
    )


# ─────────────────────────────────────────────
# 3. 解析视频流，选择最优码率
# ─────────────────────────────────────────────

# streamType 映射（从逆向分析得到）
STREAM_TYPE_DESC = {
    114: "H265 720p",
    115: "H265 1080p",
    108: "H265 1440p (2K)",
    109: "H265 4K",
    259: "H264 720p",
}

def _field(d: dict, snake: str, default=None):
    """
    兼容读取字段：优先 snake_case，回退 camelCase。
    edith API 响应使用 snake_case；__INITIAL_STATE__ 使用 camelCase。
    """
    if snake in d:
        return d[snake]
    parts = snake.split("_")
    camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
    return d.get(camel, default)


def extract_streams(note_card: dict) -> list[dict]:
    """
    从笔记主体中提取所有视频流，按分辨率降序、同分辨率 H265 优先。
    """
    note_type = note_card.get("type")
    if note_type != "video":
        raise ValueError(f"该笔记类型为 {note_type!r}，不是视频笔记，无法下载")

    video_info = note_card.get("video", {})
    media = video_info.get("media", {})
    stream_data = media.get("stream", {})

    if not stream_data:
        raise ValueError("笔记未包含视频流信息（video.media.stream 为空）")

    streams = []
    for codec_key in ("h265", "h264", "av1"):
        for s in stream_data.get(codec_key, []) or []:
            mu = _field(s, "master_url")
            if not mu:
                continue
            streams.append({
                "codec":        _field(s, "video_codec", codec_key),
                "audio_codec":  _field(s, "audio_codec", "aac"),
                "stream_type":  _field(s, "stream_type"),
                "width":        s.get("width", 0),
                "height":       s.get("height", 0),
                "fps":          s.get("fps", 0),
                "avg_bitrate":  _field(s, "avg_bitrate", 0),
                "size":         s.get("size", 0),
                "duration_ms":  s.get("duration", 0),
                "format":       s.get("format", "mp4"),
                "quality_type": _field(s, "quality_type", ""),
                "master_url":   mu,
                "backup_urls":  _field(s, "backup_urls", []) or [],
                "desc":         STREAM_TYPE_DESC.get(_field(s, "stream_type"), "unknown"),
            })

    streams.sort(
        key=lambda s: (s["width"] * s["height"], s["codec"] == "hevc"),
        reverse=True,
    )
    return streams


def choose_stream(
    streams: list[dict],
    prefer_codec: str = "hevc",
    quality_index: int = 0,
) -> tuple[dict, int]:
    """
    按编码偏好和画质索引选择最终下载流。

    返回值除了选中的流，还会返回其在候选列表中的最终索引，
    便于上层把真实选择结果写进元数据。
    """
    preferred = [s for s in streams if prefer_codec in s["codec"].lower()]
    fallback = [s for s in streams if prefer_codec not in s["codec"].lower()]
    candidate_list = preferred + fallback
    if not candidate_list:
        raise ValueError("没有可用的视频流可供选择")

    final_index = quality_index if quality_index < len(candidate_list) else 0
    return candidate_list[final_index], final_index


def build_output_filename(note_id: str, stream: dict) -> str:
    """
    根据笔记 ID 和所选视频流生成输出文件名。

    文件名格式保持兼容旧版本，避免影响已有使用习惯。
    """
    return (
        f"xhs_{note_id}_{stream['width']}x{stream['height']}"
        f"_{stream['codec']}.{stream['format']}"
    )


# ─────────────────────────────────────────────
# 4. 分片下载（HTTP Range 请求）
# ─────────────────────────────────────────────
CHUNK_SIZE = 4 * 1024 * 1024  # 每次请求 4MB

def download_video(stream: dict, output_path: str, cookies: dict | None = None) -> None:
    """
    通过 HTTP Range 请求分片拉取 MP4 视频，写入 output_path
    小红书 CDN 使用 sign + t 参数鉴权，masterUrl 已包含
    """
    url = stream["master_url"]
    backup_urls = stream["backup_urls"]
    total_size = stream["size"]

    print(f"\n▶ 开始下载")
    print(f"  编码: {stream['codec'].upper()} / {stream['audio_codec'].upper()}")
    print(f"  分辨率: {stream['width']}×{stream['height']} @ {stream['fps']}fps")
    print(f"  平均码率: {stream['avg_bitrate'] // 1000} Kbps")
    print(f"  文件大小: {total_size / 1024 / 1024:.1f} MB")
    print(f"  输出路径: {output_path}\n")

    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.update(merge_cookies(cookies))

    content_length, supports_range = _probe_content_length(
        session, url, backup_urls, fallback_size=total_size,
    )
    if not content_length:
        raise RuntimeError("无法获取文件大小：CDN 未返回 Content-Length / Content-Range")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    # 断点续传：如果文件已存在且未下载完，从断点继续
    if output.exists():
        downloaded = output.stat().st_size
        if downloaded >= content_length:
            print("✓ 文件已存在且完整，跳过下载")
            return
        print(f"  断点续传，已下载 {downloaded / 1024 / 1024:.1f} MB")

    mode = "ab" if downloaded > 0 else "wb"

    with open(output, mode) as f, tqdm(
        total=content_length,
        initial=downloaded,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc="下载进度",
        ncols=80,
    ) as pbar:

        if supports_range:
            # 分片 Range 请求
            while downloaded < content_length:
                end = min(downloaded + CHUNK_SIZE - 1, content_length - 1)
                range_header = f"bytes={downloaded}-{end}"

                resp = _request_with_fallback(
                    session, "GET", url, backup_urls,
                    headers={"Range": range_header},
                    stream=True,
                )

                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        pbar.update(len(chunk))
        else:
            # 不支持 Range，直接流式下载
            resp = _request_with_fallback(session, "GET", url, backup_urls, stream=True)
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    pbar.update(len(chunk))

    print(f"\n✓ 下载完成：{output_path}")


def _probe_content_length(session, primary_url, backup_urls, fallback_size=0):
    """
    优先 HEAD 取 Content-Length + Accept-Ranges；
    HEAD 不可用 / 未返回大小时，回退到 GET Range: bytes=0-0 读 Content-Range。
    """
    try:
        resp = _request_with_fallback(session, "HEAD", primary_url, backup_urls)
        cl = int(resp.headers.get("Content-Length", 0))
        ar = resp.headers.get("Accept-Ranges", "")
        if cl > 0:
            return cl, "bytes" in ar
    except Exception:
        pass

    resp = _request_with_fallback(
        session, "GET", primary_url, backup_urls,
        headers={"Range": "bytes=0-0"}, stream=True,
    )
    try:
        cr = resp.headers.get("Content-Range", "")
        if "/" in cr:
            total = int(cr.rsplit("/", 1)[-1])
            return total, True
        cl = int(resp.headers.get("Content-Length", fallback_size or 0))
        return cl, False
    finally:
        resp.close()


def _request_with_fallback(session, method, primary_url, backup_urls, **kwargs):
    """带重试和备用 URL 的请求"""
    urls = [primary_url] + backup_urls
    last_err = None
    for url in urls:
        for attempt in range(3):
            try:
                resp = session.request(method, url, timeout=30, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                last_err = e
                if attempt < 2:
                    time.sleep(1.5 ** attempt)
    raise RuntimeError(f"所有 CDN 节点均请求失败: {last_err}")


# ─────────────────────────────────────────────
# 辅助：生成 traceid
# ─────────────────────────────────────────────
def _gen_traceid() -> str:
    return "".join(random.choices("0123456789abcdef", k=32))


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────
def download_xhs_video_result(
    note_url: str,
    output_dir: str = ".",
    prefer_codec: str = "hevc",
    quality_index: int = 0,
    cookies: dict | None = None,
) -> VideoDownloadResult:
    """
    下载小红书视频并返回结构化结果。

    这是给 Colab 流水线使用的主入口，保留原有下载逻辑，
    但把下载阶段的重要元数据一起返回。
    """
    runtime_cookies = merge_cookies(cookies)

    print("🔗 解析笔记 URL...")
    full_url = resolve_note_url(note_url)
    note_id, _, _ = parse_note_url(full_url)
    print(f"   note_id: {note_id}")

    mode = "登录态" if runtime_cookies.get("web_session") or runtime_cookies.get("a1") else "游客"
    print(f"🪪 Cookie 模式: {mode}")

    print("📡 拉取笔记页面并解析 __INITIAL_STATE__...")
    session = build_session(runtime_cookies)
    state = fetch_note_detail(full_url, session=session, cookies=runtime_cookies)
    note_card = find_note_in_state(state, note_id)

    print("🎬 解析可用视频流...")
    streams = extract_streams(note_card)

    print(f"\n可用视频流（共 {len(streams)} 条）：")
    for i, s in enumerate(streams):
        size_mb = s["size"] / 1024 / 1024
        marker = " ←" if i == quality_index else ""
        print(
            f"  [{i}] {s['codec'].upper():6} {s['width']}×{s['height']} "
            f"@ {s['fps']}fps  {s['avg_bitrate']//1000:5} Kbps  "
            f"{size_mb:6.1f} MB  {s['desc']}{marker}"
        )

    chosen, final_quality_index = choose_stream(
        streams=streams,
        prefer_codec=prefer_codec,
        quality_index=quality_index,
    )
    filename = build_output_filename(note_id, chosen)
    output_path = str(Path(output_dir) / filename)

    download_video(chosen, output_path, cookies=runtime_cookies)
    return VideoDownloadResult(
        note_id=note_id,
        note_url=note_url,
        resolved_url=full_url,
        output_path=output_path,
        filename=filename,
        codec=chosen["codec"],
        audio_codec=chosen["audio_codec"],
        width=chosen["width"],
        height=chosen["height"],
        fps=chosen["fps"],
        avg_bitrate=chosen["avg_bitrate"],
        size=chosen["size"],
        format=chosen["format"],
        quality_index=final_quality_index,
    )


def download_xhs_video(
    note_url: str,
    output_dir: str = ".",
    prefer_codec: str = "hevc",
    quality_index: int = 0,   # 0=最高画质，1=次高，以此类推
    cookies: dict | None = None,
) -> str:
    """
    兼容旧版本的主入口。

    参数：
      note_url     - 小红书笔记完整 URL
      output_dir   - 视频保存目录（默认当前目录）
      prefer_codec - 优先编码 "hevc"（H265）或 "h264"
      quality_index- 0 为最高画质
      cookies      - 运行时注入的 Cookie 字典

    返回：下载文件路径
    """
    return download_xhs_video_result(
        note_url=note_url,
        output_dir=output_dir,
        prefer_codec=prefer_codec,
        quality_index=quality_index,
        cookies=cookies,
    ).output_path


# ─────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python xhs_downloader.py <小红书笔记URL> [输出目录] [hevc|h264] [画质索引]")
        print("示例: python xhs_downloader.py 'https://www.xiaohongshu.com/explore/xxxx?xsec_token=...' ./videos hevc 0")
        sys.exit(1)

    url        = sys.argv[1]
    out_dir    = sys.argv[2] if len(sys.argv) > 2 else "."
    codec      = sys.argv[3] if len(sys.argv) > 3 else "hevc"
    q_index    = int(sys.argv[4]) if len(sys.argv) > 4 else 0

    try:
        result_path = download_xhs_video(url, out_dir, codec, q_index)
        print(f"\n🎉 视频已保存至: {result_path}")
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        sys.exit(1)
