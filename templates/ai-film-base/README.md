# ai-film-base

AI 生成路线的片子起点：剧本 → 分镜 → 参考图 / 关键帧（h3 出图）→ 动态分镜（免费）→ 图生视频（h3 出视频，MiniMax-H3）→ 审片挑条 → 拼接 → 自检。Claude 当导演，流程和审片标准在 skill [`ai-director`](../../.claude/skills/ai-director/SKILL.md) 里。

## 用法

```bash
rsync -a templates/ai-film-base/ videos/002-foo/
python3 scripts/ai/film.py plan videos/002-foo --write     # 校验镜头清单，生成 storyboard.md 和分镜页面
python3 scripts/ai/film.py keyframes videos/002-foo         # dry run：打印 h3 命令和 CU 预算
# 用户确认预算后加 --yes；之后 review → pick → clips（先 placeholder 动态分镜，再 h3-video）→ assemble → check，见 ai-director
```

## 文件

| 文件 | 作用 |
|---|---|
| `prompts/shots.json` | **镜头清单，唯一的事实来源**：画幅、统一风格、参考图（人物 / 道具设定）、每镜的时长、转场、关键帧提示词、运镜、台词，以及选中的图和视频。脚本读它，也写回它 |
| `prompts/versions/` | 每一版分镜（`v001.json`、`v002.json`…），`plan --write` 自动存，只增不改，进 git |
| `storyboard.md` | 由 `film.py plan --write` 从 `shots.json` 生成，给人看、给用户确认用；不要手改 |
| `assets/review/storyboard.html` | 分镜页面，`plan --write` / `revert` 生成，不进 git |
| `brief.md` | 需求、风格设定、角色设定、预算 |
| `prompts/runs/*.jsonl` | 每次 h3 出图 / 出视频的记录（提示词、seed、尺寸），进 git |
| `assets/` | 生成的参考图、关键帧、视频条（`clips/<镜头>/placeholder-*.mp4`、`h3-trial/`、`h3-final/`）、审片图（不进 git） |
| `renders/` | 成片和字幕（不进 git） |

示例 `shots.json` 是一条 12.5 秒、3 个镜头的小片「灯塔的清晨」，用到了参考图（守塔人）、叠化转场、三种运镜和每镜的环境音，可以直接拿来改。

## shots.json 字段

| 字段 | 说明 |
|---|---|
| `format` | `width` / `height` / `fps` / `aspect`；参考图、关键帧和视频都按这个画幅出。用 h3 视频时 fps 用 24 |
| `backend` | 图生视频默认后端：`h3-video`（MiniMax-H3 首帧生视频，带环境音，付费）或 `placeholder`（免费，关键帧加运镜，做动态分镜）；单镜可用 `motion.backend` 覆盖 |
| `style` | 关键帧统一风格，自动接在每条关键帧提示词后面 |
| `video_style` | 给视频模型的统一风格（如 `Live-action, cinematic, …`），自动放在每条 `motion.prompt` 前面 |
| `references.<名字>` | `desc`：设定图说明（中文，给人看，不发给模型）；`prompt`：设定图提示词（英文）；`image`：选中的设定图（`pick ref:名字` 写入）；`seed` |
| `shots[].duration` | 秒（h3-video 一条 4–15 s，短于 4 s 的出 4 s 再裁）；`transition`：进入下一镜的方式，`cut` 或 `fade`（0.5 s 叠化） |
| `shots[].keyframe` | `desc`：画面说明（中文，给人看，不发给模型）；`prompt`（英文）；`refs`：用到的参考图名，提示词里用 `<Picture 1>`… 指代；`image` / `seed`：选中的关键帧（`pick` 写入） |
| `shots[].motion` | `desc`：动作说明（中文，给人看，不发给模型）；`camera`：placeholder 的运镜（`static`、`push-in`、`pull-out`、`pan-left`、`pan-right`、`tilt-up`、`tilt-down`）；`prompt`：给视频模型的画面描述（英文，小写开头接在 `video_style` 后，用 `<Picture 1>` 指代首帧）；`sound`：环境音和动作声（英文，不写对白和音乐）；`clip` / `seed`：选中的视频条（`pick` 写入） |
| `shots[].line` / `notes` | 台词（出字幕）/ 导演备注（重拍原因等） |
