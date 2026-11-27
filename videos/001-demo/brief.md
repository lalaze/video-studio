# 001-demo · brief

| 项 | 内容 |
|---|---|
| 主题 | Claude 做视频的四条路 |
| 路线 | 代码动画（HyperFrames，HTML + GSAP） |
| 时长 | 15 秒 |
| 画幅 | 1920×1080，30 fps，16:9 |
| 声音 | 无配音、无音乐（静音片） |
| 受众 | 第一次打开本仓库的人：一眼看懂这个工作台能做哪四类视频 |
| 用途 | 跑通 HyperFrames 全流程（装 skill → 写 composition → 渲染 → 自检）的 demo |
| 付费服务 | 不用：不调 TTS、视频模型，不开 `h3` |

## 风格

- 深色暖底（`#141413`）、米白字（`#F3EFE6`），四条路各一个强调色：陶土橙 / 天蓝 / 淡紫 / 草绿。
- 中文用系统自带 PingFang SC（`@font-face` + `local()`），西文用 HyperFrames 内置的 Inter / JetBrains Mono。
- 动效克制：标题落定后不再动；每条路的图形在自己那 2.5 秒里讲完一个小动作（打字→逐帧渲染、按字剪掉口癖、草图→关键帧→视频、画出正弦曲线）。
- 一条贯穿的顶栏：左边片名，右边四段进度条，标题里的四个色块飞上去变成进度条，结尾再飞回来。

## 内容

1. 片头：「Claude 做视频的四条路」
2. 代码动画 —— HyperFrames / Remotion
3. 剪已有素材 —— 转写 + ffmpeg
4. AI 生成画面 —— 分镜 → 关键帧 → 图生视频
5. 讲解动画 —— Manim
6. 片尾：「video-studio」

## 交付

- 成片：`renders/demo.mp4`（不进 git）
- 自检抽帧：`renders/frames/`（不进 git）
