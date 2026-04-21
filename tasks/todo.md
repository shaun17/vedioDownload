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
