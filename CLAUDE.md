# video-studio

用 Claude Code 做视频的工作台。可复用的流程、模板、脚本放这里；每条视频一个目录。

## 四条路线

1. **代码动画**：HyperFrames（默认，Apache 2.0，HTML + GSAP）或 Remotion（React）。讲解、产品宣传、数据动画、片头。
2. **剪已有素材**：转写拿词级时间戳 → 按文字稿定剪点 → ffmpeg 执行。口播、访谈、录屏。
3. **AI 生成画面**：剧本 → 分镜 → 每镜关键帧 → 图生视频（Veo / Kling / Seedance 等）→ 配音 → 拼接。
4. **数学 / 算法讲解**：Manim。

## 目录约定

```
videos/<三位编号>-<短名>/     例：videos/001-demo/
  brief.md         需求：主题、时长、画幅、风格、受众
  storyboard.md    分镜表：每镜时长、画面、文字、转场
  src/             工程源码（HyperFrames / Remotion / Manim）
  prompts/         生成参数：prompt、模型、种子、参考图
  assets/          素材（不进 git）
  renders/         渲染输出（不进 git）
templates/         可复用模板（hyperframes-base：HyperFrames 新片起点）
scripts/           通用脚本（hf.sh 跑 HyperFrames CLI、selfcheck.sh 渲染自检；以后加转写、去静音、拼接、字幕…）
```

## 规则

- 先写 `brief.md` 和 `storyboard.md`；正式片要等用户确认分镜再渲染或生成。
- 会花钱的操作（视频模型、TTS、`h3` 开机）必须先问用户。关键帧图片用 `h3`，规则见全局 CLAUDE.md。
- 大文件不进 git：素材、渲染结果、模型输出。成片要归档时问用户，放 NAS 视频共享（`/Volumes/video`）。
- **本仓库是公开的**：不要复制没有开源协议的第三方 skill（例如 `remotion-dev/skills`）进来；不要写入密钥、内网地址或个人信息。
- 第三方 skill 装进 `.claude/skills/` 前先读一遍代码，并在 README 记录来源和版本。
- HyperFrames 新片从 `templates/hyperframes-base/` 复制（`rsync -a --exclude .hyperframes`），先读它的 README；CLI 一律用 `scripts/hf.sh`。
- 每次渲染完自检：运行 `scripts/selfcheck.sh <成片> --project <src>`（ffprobe 核对时长、分辨率、帧率，扫黑帧 / 空帧 / 静止段，抽帧到 `renders/frames/`），然后逐张亲眼看抽帧（文字溢出、重叠、乱码、黑帧、动画提前结束）。
