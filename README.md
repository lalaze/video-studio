# video-studio

用 Claude Code 做视频：代码动画（HyperFrames / Remotion）、按文字稿剪素材（转写 + ffmpeg）、调度 AI 视频模型、Manim 讲解动画。

- 流程和规则：[CLAUDE.md](CLAUDE.md)
- 每条视频一个目录：`videos/<编号>-<短名>/`
- 素材和渲染结果不进 git

## 依赖

- Node.js 22+
- ffmpeg / ffprobe
- Chrome / Chromium（HyperFrames、Remotion 渲染用；HyperFrames 也可以 `scripts/hf.sh browser ensure` 下载固定版本）
- perl、awk（`selfcheck.sh` 用，macOS 自带）
- Python 3（`scripts/ai/film.py` 用，只用标准库）
- AI 生成路线出关键帧和视频要 `h3`（[colab-use](https://github.com/lalaze/colab-use)：MiniMax-H3-Image 出图、MiniMax-H3 出视频，跑在 Colab 上，按开机时长计费）

## 第三方 skill

| skill | 来源 | 版本 | 协议 |
|---|---|---|---|
| HyperFrames skill 包（9 个）：`hyperframes`、`hyperframes-cli`、`hyperframes-registry`、`media-use`、`reverse-any-video`、`style-sampling`、`figma`、`remotion-to-hyperframes`、`slideshow` → `.claude/skills/` | [heygen-com/hyperframes](https://github.com/heygen-com/hyperframes)，`npx skills add heygen-com/hyperframes -a claude-code --skill '*' --copy` | commit `4fca68b23561e3f1271b876ed4715dce9c7cf208`（2026-11-27 安装；各 skill 的哈希见 `skills-lock.json`）；渲染用 CLI 固定为 `hyperframes@0.9.102` | Apache-2.0（全文：`.claude/skills/LICENSE-hyperframes.txt`） |

说明：

- 提交前读过 skill 内容：脚本只连 HeyGen / Google / Openverse 等可选服务的 API（需要各自的 key，本仓库不配），`.ssh`、凭据相关的路径只出现在禁止读取的名单里。
- `media-use/audio/assets/sfx/*.mp3`（19 个音效）是 Pixabay Content License，不是 Apache-2.0，也被本仓库的 `*.mp3` 规则忽略，所以**没有提交**。clone 后需要这些音效时运行 `npx skills experimental_install --force -a claude-code`，按 `skills-lock.json` 里的 commit 重新拷贝整个 skill 包。
- `hyperframes/assets/frame-presets/code-editorial/fonts/` 里的 woff2 是 SIL OFL 1.1，许可证原文就在同目录。
- 调用 CLI 一律用 [`scripts/hf.sh`](scripts/hf.sh)：走 skill 自带的启动器、固定版本、关掉遥测（见下）。

## 模板和脚本

| 路径 | 用途 |
|---|---|
| [`templates/hyperframes-base/`](templates/hyperframes-base/) | HyperFrames 新片起点：片头 → 「左文右图」段落（一个文件挂多次，右栏放图或叠动画图形）→ 片尾；字体 / 配色变量、brief 和分镜骨架都备好了，001-demo 的坑写在它的 README 里 |
| [`templates/ai-film-base/`](templates/ai-film-base/) | AI 生成路线的片子起点：镜头清单 `prompts/shots.json`（唯一事实来源）、示例小片、brief 骨架；流程和审片标准在自有 skill [`ai-director`](.claude/skills/ai-director/SKILL.md) |
| [`scripts/ai/film.py`](scripts/ai/film.py) | AI 片导演工具：`plan` 校验并生成分镜 → `keyframes` 用 h3 出参考图和关键帧（默认 dry run 报 CU 预算，`--yes` 才开机，跑完必关机）→ `pick` → `clips` 图生视频（免费的 placeholder 运镜做动态分镜；`h3-video` 用 MiniMax-H3 首帧生视频、带环境音，turbo 试稿 / 20 步定稿，同样默认 dry run）→ `review` 审片图 → `assemble` 拼接 + SRT → `check` |
| [`scripts/hf.sh`](scripts/hf.sh) | 跑 HyperFrames CLI：`cd <工程>/src && ../../../scripts/hf.sh check`。版本在脚本里固定（`HF_CLI_VERSION` 可临时覆盖），不发 `events` / `feedback` |
| [`scripts/selfcheck.sh`](scripts/selfcheck.sh) | 渲染后自检：ffprobe 核对时长 / 分辨率 / 帧率，整片扫黑帧、空底色帧、静止段，抽帧并拼总览图，报告写进 `renders/frames/selfcheck.txt`。`--help` 看全部选项 |

```bash
# 新片
rsync -a --exclude .hyperframes templates/hyperframes-base/ videos/002-foo/
cd videos/002-foo/src
../../../scripts/hf.sh check
../../../scripts/hf.sh render --quality delivery -o ../renders/foo.mp4
../../../scripts/selfcheck.sh ../renders/foo.mp4 --project . --audio no
```

## 示例

- [`videos/001-demo`](videos/001-demo/)：15 秒「Claude 做视频的四条路」，HyperFrames 代码动画。在 `videos/001-demo/src` 里运行 `../../../scripts/hf.sh render --quality delivery -o ../renders/demo.mp4`，再 `../../../scripts/selfcheck.sh ../renders/demo.mp4 --project . --audio no --at 0.5,2,4.5,7,9.5,12,14.5`。中文用 macOS 自带的 PingFang SC（`local()`），所以只能在 macOS 本机渲染。
