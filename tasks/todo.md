# TODO

- [x] 阅读现有仓库结构与核心脚本，确认当前能力边界
- [x] 阅读现有测试，确认已覆盖的核心行为
- [x] 输出 Spec 第 1 段：现状分析
- [x] 输出 Spec 第 2 段：目标功能拆分
- [x] 输出 Spec 第 3 段：风险与关键决策
- [x] 等待用户完成 HARD-GATE 确认
- [x] 确认后再开始实现 Colab 部署、文稿提取与 Drive 保存
- [x] 补充或调整自动化测试
- [x] 执行验证并记录证据

## Review

- 已完成下载器运行时 Cookie 注入、结构化下载结果输出，以及 Colab 转录流水线实现。
- 当前流水线行为已调整为：视频直接下载到 Drive，文稿直接写入 Drive，只有临时音频保留在 Colab 本地。
- 已新增 `requirements-colab.txt`，将 `faster-whisper` 限定为 Colab 运行时依赖，不进入本地基础依赖。
- 已新增 `tasks/lessons.md`，记录“Colab + GPU 场景不把 Whisper 设计成本地默认依赖”的约束。
- 验证证据：`.venv/bin/python -m pytest tests/test_xhs_downloader.py tests/test_xhs_colab_pipeline.py`
- 验证结果：48 个测试全部通过。
- 未验证项：当前环境不是 Colab，未实际执行 Drive 挂载、ffmpeg 抽音频和 GPU 转录。

## 2026-04-21 CLI 默认输出目录

- [x] 确认直接执行 `xhs_downloader.py` 的默认输出路径和 `run_colab_workflow` 的显式输出路径
- [x] 将 `xhs_downloader.py` 的脚本默认输出目录改为独立目录
- [x] 补充测试并执行验证

## Review 2026-04-21

- 已将 `xhs_downloader.py` 直接执行时的默认输出目录改为 `./downloads`，避免视频文件落在仓库根目录。
- 本次只调整了脚本入口的默认参数解析，未改动 `download_xhs_video_result(...)` 的显式 `output_dir` 调用链，因此 `run_colab_workflow` 继续按原逻辑写入 Drive。
- 已新增 CLI 参数解析测试，覆盖“未传输出目录时默认使用独立目录”和“显式输出目录保持不变”两种行为。
- 验证证据：`.venv/bin/python -m pytest tests/test_xhs_downloader.py tests/test_xhs_colab_pipeline.py`
- 验证结果：50 个测试全部通过。

## 2026-04-21 yt-dlp 通用视频下载

- [x] 输出 Spec 第 1 段：现状分析
- [x] 等待用户确认现状分析
- [x] 输出 Spec 第 2 段：功能点与文件级方案
- [x] 等待用户确认功能方案
- [x] 输出 Spec 第 3 段：风险、决策与验证计划
- [x] 等待 HARD-GATE 确认
- [x] 实现 yt-dlp 下载入口和本地测试脚本
- [x] 接入 `xhs_colab_pipeline` 的域名分流下载
- [x] 补充测试并执行验证
- [x] 记录最终 review

## Review 2026-04-21 yt-dlp 通用视频下载

- 已新增 `yt_dlp_downloader.py`，本地可单独执行下载，不触发转录。
- 已在 `xhs_colab_pipeline.py` 中加入域名分流：小红书和 xhslink 继续走现有 `xhs_downloader.py`，其他域名走 yt-dlp。
- 非小红书 URL 使用稳定 hash 任务 ID，继续复用现有 Drive/local 目录、跳过、抽音频、转录和 metadata 写入链路。
- 已在 `requirements.txt` 增加 `yt-dlp` 依赖；转录依赖仍保留在 `requirements-colab.txt`。
- 验证证据：`.venv/bin/python -m pytest tests/test_xhs_downloader.py tests/test_xhs_colab_pipeline.py tests/test_yt_dlp_downloader.py`
- 验证结果：64 个测试全部通过。
- 额外验证：`.venv/bin/python -m py_compile yt_dlp_downloader.py xhs_colab_pipeline.py` 通过。
- 未执行真实下载：当前本地虚拟环境尚未安装 yt-dlp，单元测试通过 fake yt-dlp 验证参数和结果结构。

## Review 2026-04-21 yt-dlp X 超时修复

- 已将 `yt_dlp_downloader.py` 默认模式改为音频下载并转 mp3，对齐用户验证过的 `yt-dlp -x --audio-format mp3 --audio-quality 0`。
- 已保留 `video` 模式：`python yt_dlp_downloader.py <URL> <输出目录> video`，视频模式优先选择 HLS，避免优先落到大文件 HTTPS 直连。
- 已调整 `xhs_colab_pipeline.py` 的非小红书分支使用音频模式，后续仍交给现有 ffmpeg 抽取 WAV 和 faster-whisper 转录链路。
- 已让 skip-existing 支持 `.mp3/.m4a/.webm/.mov` 等媒体文件，避免非小红书音频产物无法跳过。
- 已补充 `.gitignore`，忽略本地下载产物目录和常见音频格式。
- 验证证据：`.venv/bin/python -m pytest tests/test_xhs_downloader.py tests/test_xhs_colab_pipeline.py tests/test_yt_dlp_downloader.py`
- 验证结果：67 个测试全部通过。
- 真实下载验证：`.venv/bin/python yt_dlp_downloader.py https://x.com/dachaoren/status/2033843657258766576 /tmp/vedioDownload-ytdlp-test`
- 真实下载结果：选择 `hls-audio-128000-Audio`，成功输出 `/tmp/vedioDownload-ytdlp-test/ytdlp_23ed38b9d4b40408.mp3`。

## 2026-04-21 yt-dlp MP4 视频模式修复

- [x] 将 yt-dlp 默认模式恢复为 mp4 视频下载
- [x] 保留 audio 模式用于只下载 mp3
- [x] 修复 Twitter/X 视频模式避免选择 `http-2176` 大文件直连
- [x] 补充测试并执行验证
- [x] 记录最终 review

## Review 2026-04-21 yt-dlp MP4 视频模式修复

- 已将 `yt_dlp_downloader.py` 默认模式改为 `video`，默认输出 mp4；传入 `audio` 时仍然只提取 mp3。
- 已将 `xhs_colab_pipeline.py` 的非小红书分支改为 `media_type="video"`，恢复“下载视频后再转录”的主路径。
- 视频模式优先选择 HLS MP4；当 HLS 探测失败时，HTTP 兜底优先低码率/低分辨率 MP4，避免选中 `http-2176`。
- 已增加 yt-dlp 的 socket timeout 和重试次数，降低 m3u8 信息和分片下载偶发超时的失败概率。
- 验证证据：`.venv/bin/python -m pytest tests/test_xhs_downloader.py tests/test_xhs_colab_pipeline.py tests/test_yt_dlp_downloader.py`
- 验证结果：68 个测试全部通过。
- 格式选择验证：X 链接正常路径选择 `hls-553+hls-audio-128000-Audio`；HTTP 兜底路径选择 `http-256`，均未选择 `http-2176`。
