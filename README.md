# video-studio

用 Claude Code 做视频：代码动画（HyperFrames / Remotion）、按文字稿剪素材（转写 + ffmpeg）、调度 AI 视频模型、Manim 讲解动画。

- 流程和规则：[CLAUDE.md](CLAUDE.md)
- 每条视频一个目录：`videos/<编号>-<短名>/`
- 素材和渲染结果不进 git

## 依赖

- Node.js 22+
- ffmpeg
- Chrome / Chromium（HyperFrames、Remotion 渲染用）

## 第三方 skill

| skill | 来源 | 版本 | 协议 |
|---|---|---|---|
| HyperFrames skill 包（9 个）：`hyperframes`、`hyperframes-cli`、`hyperframes-registry`、`media-use`、`reverse-any-video`、`style-sampling`、`figma`、`remotion-to-hyperframes`、`slideshow` → `.claude/skills/` | [heygen-com/hyperframes](https://github.com/heygen-com/hyperframes)，`npx skills add heygen-com/hyperframes -a claude-code --skill '*' --copy` | commit `4fca68b23561e3f1271b876ed4715dce9c7cf208`（2026-11-27 安装；各 skill 的哈希见 `skills-lock.json`）；渲染用 CLI 固定为 `hyperframes@0.9.102` | Apache-2.0（全文：`.claude/skills/LICENSE-hyperframes.txt`） |

说明：

- 提交前读过 skill 内容：脚本只连 HeyGen / Google / Openverse 等可选服务的 API（需要各自的 key，本仓库不配），`.ssh`、凭据相关的路径只出现在禁止读取的名单里。
- `media-use/audio/assets/sfx/*.mp3`（19 个音效）是 Pixabay Content License，不是 Apache-2.0，也被本仓库的 `*.mp3` 规则忽略，所以**没有提交**。clone 后需要这些音效时运行 `npx skills experimental_install --force -a claude-code`，按 `skills-lock.json` 里的 commit 重新拷贝整个 skill 包。
- `hyperframes/assets/frame-presets/code-editorial/fonts/` 里的 woff2 是 SIL OFL 1.1，许可证原文就在同目录。
- 调用 CLI 一律走 skill 自带的启动器，并关掉遥测：

  ```bash
  export HYPERFRAMES_NO_TELEMETRY=1
  node .claude/skills/hyperframes/scripts/plugin-cli.mjs --cli-version 0.9.102 <command>
  ```

## 示例

- [`videos/001-demo`](videos/001-demo/)：15 秒「Claude 做视频的四条路」，HyperFrames 代码动画。在 `videos/001-demo/src` 里运行 `… check`、`… render --quality delivery -o ../renders/demo.mp4`（`…` 即上面的启动器命令）。中文用 macOS 自带的 PingFang SC（`local()`），所以只能在 macOS 本机渲染。
