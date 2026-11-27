---
name: ai-director
description: 导演一条 AI 生成的片子（剧本 → 分镜 → 参考图 / 关键帧 → 动态分镜 → 图生视频 → 审片挑条 → 拼接 → 自检），用本仓库的 scripts/ai/film.py 和 h3（MiniMax-H3 出图和出视频）。用户要「AI 生成视频」「用生成的素材做一条片」「你当导演」时先读这个。
---

# AI 片导演手册

你是导演：写剧本和镜头清单、出图、审片、决定重拍、剪成片。用户是制片人：确认分镜、确认每一笔花销。

工具：`scripts/ai/film.py`（读写片子目录里的 `prompts/shots.json`）、`h3`（Colab 上的 MiniMax-H3-Image 出关键帧、MiniMax-H3 出视频；规则见全局 CLAUDE.md 和 `h3 --help`）、`scripts/selfcheck.sh`。

## 必须停下来问用户的地方

1. **分镜**：正式片写完 `shots.json`、生成 `storyboard.md` 后，等用户确认再出图（demo 用户说不用等才例外）。
2. **动态分镜**：关键帧选好后，先用免费的 placeholder 出一版拼起来给用户看，确认后再花钱出视频。
3. **每个付费步骤**：先跑 dry run 拿到预算（`film.py keyframes` / `film.py clips` 不加 `--yes` 就只打印命令、提示词和 CU 估算），报给用户，确认后才加 `--yes`。
4. **h3 定稿图不到 4 张**：问用户现在开机还是攒成一批。用 h3-video 时一般不出定稿图，见下文。

## 流程

```bash
F=scripts/ai/film.py; V=videos/00N-name
rsync -a templates/ai-film-base/ $V/                 # 0. 新片
# 1. 写 brief.md；写 prompts/shots.json（见下文「写镜头清单」）
python3 $F plan $V --write                           # 2. 校验 + 生成 storyboard.md → 给用户确认
python3 $F keyframes $V                              # 3. dry run：关键帧试稿计划和预算 → 用户确认
python3 $F keyframes $V --yes --keep-vm              #    试稿（4 MP，每镜 2 张；参考图先出）
python3 $F review $V --what keyframes                # 4. 审图（逐张看 assets/review/*.png）
python3 $F pick $V ref:keeper assets/keyframes/ref-keeper/trial/xxx.png
python3 $F keyframes $V --yes --vm-up                #    参考图选好后，再出用到它的镜头，跑完关机
python3 $F pick $V s01 assets/keyframes/s01/trial/xxx.png    # 每镜选一张
python3 $F clips $V --backend placeholder            # 5. 免费动态分镜（关键帧 + 运镜）
python3 $F assemble $V -o renders/animatic.mp4       #    拼起来 → 用户看节奏和画面
python3 $F clips $V                                  # 6. dry run：h3 视频试稿（turbo）的提示词和预算 → 用户确认
python3 $F clips $V --yes --keep-vm                  #    试稿：turbo 8 步，每镜 1 条
python3 $F review $V --what clips                    # 7. 审片（三帧 + 音量），每镜 pick 一条
python3 $F pick $V s01 assets/clips/s01/h3-trial/xxx.mp4
python3 $F clips $V --stage final --yes --vm-up      # 8. 定稿：20 步，沿用选中试稿的 seed，跑完关机
python3 $F review $V --what clips                    #    再审一遍，pick 定稿
python3 $F assemble $V --subs                        # 9. 拼接（带 h3 的环境音）；台词出 SRT 并封成软字幕
python3 $F check $V                                  # 10. 自检，然后逐张看抽帧
```

- **开机和出视频都很久**（开机约 11 分钟，一条 5 s 定稿约 6 分钟），超过 Bash 前台 10 分钟上限：带 `--yes` 的命令一律放后台运行（Bash `run_in_background`），用完成通知接着做，不要轮询。
- `keyframes` / `clips` 默认 `h3 up` → 生成 → `h3 down`，失败或中断也会关机。`--keep-vm` 只在接下来几分钟就要接着用时加：VM 闲着照样扣 CU（A100 约 8.9 CU/h，闲 10 分钟约等于重开一次机）。审得慢就直接关机，下一批再开。
- 关键帧用 `--models image`（默认），视频用 `--models video`（`clips` 默认）。同一台 VM 上 `h3 up` 会复用并补装另一套权重；想一次开机做完关键帧和视频，就用 `keyframes --models both --keep-vm`，之后 `clips --vm-up`。
- 结束前确认 VM 已关（`h3 status`）；没关就 `h3 down`。
- 生成记录写在 `prompts/runs/*.jsonl`（提示词、seed、尺寸），进 git；图和视频在 `assets/`，不进 git。

### 用 h3-video 时省钱的地方

- **不用出 16 MP 定稿图**：视频只有 768p（1344×768），选中的 4 MP 试稿当首帧已经够；`clips` 会把关键帧裁成视频画布大小的 JPEG 再上传。
- **turbo 试稿看得过眼就直接用**：pick 了 turbo 试稿也能拼成片；要更好的动作和声音再出 20 步定稿。
- 视频试稿和定稿尽量在一次开机里做完（试稿加 `--keep-vm`，审完马上 `--stage final --vm-up`）。

## 写镜头清单（prompts/shots.json）

- **先定故事**：一句话故事 → 3–5 个节拍 → 每个节拍 1–2 个镜头。每镜只有一个主体、一个动作、一个运镜，4–6 s（h3-video 一条 4–15 s，短于 4 s 的会出 4 s 再裁掉）。
- **景别有起伏**：定场（远景）→ 中景 → 特写，别连着几镜同一景别。
- **连贯**：主角朝向和运动方向前后一致（不越轴），时间和光线按剧情推进（清晨就一路清晨）。
- **人物一致**：要在多镜出现的人 / 物先写进 `references`，出一张设定图（正面 + 侧面并排，纯色背景）。**参考图按片子画幅出**：`h3 edit` 的输出和 `<Picture 1>` 同比例。镜头的 `keyframe.refs` 列出用到的参考图，提示词里用 `<Picture 1>`、`<Picture 2>` 指代。
- **少用 AI 不擅长的**：画面里的文字、复杂手部动作、多人互动、细小的机械结构。必须出现的文字用英文引号括起来。
- `style` 写关键帧统一的质感、光线和色调，会自动接在每条关键帧提示词后面；`video_style` 是给视频模型的同一套风格（「Live-action, cinematic, …」这种开头，不要写 film still）。
- `format.fps` 用 24：h3 视频是 24 fps，30 fps 会重复帧，运镜时顿挫。
- 台词写在 `line`，只做字幕（第一版不配音）；`plan` 会提醒读不完的台词。

### 关键帧提示词（keyframe.prompt，h3 出图）

英文、具体，按「景别和角度 → 主体和动作 → 环境 → 光线和色调」写，例如 *Medium shot, low angle: the old keeper from <Picture 1> climbs a spiral iron staircase holding a brass lantern…*。这里的 `<Picture 1>` 是**参考图**（设定图）。h3 不支持 negative prompt：只写想要的，不写「不要」。

### 视频提示词（motion.prompt / motion.sound，h3 出视频）

`film.py` 按 MiniMax-H3 官方 I2V 格式拼最终提示词（`clips` 的 dry run 会完整打印出来，花钱前先读一遍）：

```text
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

integrated_multimodal_description: [Shot 1] {video_style}, {motion.prompt}

overall_soundscape: {motion.sound}

non_diegetic_music: N/A
```

- **motion.prompt**：小写开头，接在 `video_style` 后面。顺序是「首帧锚定 → 动作开始 → 持续变化 → 结果」：先用 *the … shown in <Picture 1>* 点出首帧里的主体并说保持什么（脸、衣服、道具、位置），再写一个动作和一个运镜。这里的 `<Picture 1>` 是**首帧**（选中的关键帧），和关键帧提示词里的不是同一张。
- **运镜**写成句子里的动作：运镜类型 + 幅度 + 速度，例如 *The camera pushes in with small amplitude at slow speed toward …*。可用：push in / pull out、zoom in / out、pan left / right、truck left / right、tilt up / down、pedestal up / down、arc shot、tracking shot、static shot；幅度 *with small / large amplitude*，速度 *at slow / fast speed*（中等就不写）。
- **motion.sound**：1–4 句英文，写环境音和动作声（海浪、风、脚步、金属吱呀）。不写对白、不写音乐。不写的话会按完全无声生成。
- **不写对白和音乐**：第一版不配音，台词只做字幕；配乐每镜一段会在剪切点断开，所以固定 N/A，以后整片统一加。
- `motion.camera` 只给 placeholder 用（`static`、`push-in`、`pull-out`、`pan-left`、`pan-right`、`tilt-up`、`tilt-down`），尽量和 motion.prompt 里的运镜一致，动态分镜才像成片。
- 官方指南：<https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md>

## 审片标准

**参考图 / 关键帧**（看 `review --what keyframes` 的总览图，再打开原图看细节）
- 构图、景别、主体动作和分镜一致；画幅对。
- 人物和参考图一致：脸、发型、衣服、体型。
- 手、脸、肢体没有畸形；没有乱码文字和水印。
- 和前后镜头的光线、色调、时间对得上（`review --what board` 看整组）。

**视频条**（`review --what clips`：每条一行，10% / 50% / 90% 三帧，后面附音量）
- 第一帧和关键帧一致；人物没有中途变脸、换衣服。
- 没有物体融化、闪烁、穿模、违反物理的运动。
- 运镜和动作是分镜写的那个。
- 声音：你听不到，只能看音量。平均低于 -50 dB 是几乎无声；峰值到 0 dB 可能削波。成片交付时请用户自己听一遍。

**成片**：`check` 通过后逐张看抽帧；转场处没有跳帧；台词字幕时间对得上。

## 重拍规则

- 一镜最多重出 2 次（`--redo`）。还不行就改镜头设计（简化动作、换景别、去掉难画的东西），而不是一直抽卡。
- 每次重拍都在该镜的 `notes` 里写一句为什么（「手指畸形，改成手插口袋」），下次写提示词时避开。
- 换了关键帧，`pick` 会清空这镜选过的视频；旧关键帧出的视频条不会再挡住新的试稿。

## 现在的限制

- 视频只有 768p（1344×768，24 fps），拼接时放大到片子分辨率；turbo 试稿的动作和声音会软一些。
- 声音只有 h3 生成的环境音：没有配音、没有配乐。不想要声音就 `assemble --no-audio`。
- 本机 ffmpeg 没有 libass / drawtext，字幕不能烧进画面：出 SRT，`--subs` 封成软字幕轨。需要烧进画面时，用 HyperFrames 做字幕层。
- MiniMax-H3-Image 和 MiniMax-H3 的许可证都不覆盖美国、欧盟、英国、韩国，而 Colab 的 VM 常在美国；`h3 up` 会显示 VM 所在地，合规问题以用户的判断为准。MiniMax-H3 商用时产品界面要标明 "MiniMax H3"（见 colab-use 的 README）。
