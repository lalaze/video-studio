# hyperframes-base

HyperFrames 新片的起点：片头 → 两个「左文右图」段落（一个配图、一个配动画图形）→ 片尾，11 秒，1920×1080，30 fps，静音。`check` 全过、渲染和 `selfcheck.sh` 都验证过。001-demo 踩过的坑已经写进代码和下面的规矩里。

用不上的场景直接删宿主；讲解类片子通常就是「片头 + N 个段落 + 片尾」。

## 用法

```bash
# 1. 复制（别带上 .hyperframes/，那是本机的预览 / 历史状态）
rsync -a --exclude .hyperframes templates/hyperframes-base/ videos/002-foo/

# 2. 改 src/meta.json 的 id / name（如 002-foo），写 brief.md、storyboard.md

# 3. 写场景，边写边查（在 src/ 里运行）
cd videos/002-foo/src
../../../scripts/hf.sh lint
../../../scripts/hf.sh check
../../../scripts/hf.sh snapshot --at 1,3      # PNG 在 .hyperframes/artifacts/snapshots/

# 4. 渲染 + 自检
../../../scripts/hf.sh render --quality delivery -o ../renders/foo.mp4
../../../scripts/selfcheck.sh ../renders/foo.mp4 --project . --audio no
```

`hf.sh` 固定 CLI 版本、关掉遥测、走 skill 自带的启动器。`selfcheck.sh` 从 `index.html` 读期望的时长 / 分辨率 / 帧率，抽帧到 `renders/frames/`，最后仍要逐张看。

## 文件

| 文件 | 作用 |
|---|---|
| `src/index.html` | 根合成：只放场景宿主。颜色、字体变量和 PingFang 的 `@font-face` 写在这里的内联样式里 |
| `src/compositions/title.html` | 片头示例：文案是变量、SVG 描线、进场和退场分在两个元素上 |
| `src/compositions/section.html` | 段落场景（左文右图），一个文件挂多次，见下文 |
| `src/compositions/graphic-flow.html` | 右栏动画图形的例子（三步流程），叠在段落上 |
| `src/compositions/end.html` | 片尾示例：0.5 s 内落定，之后静止 ≥ 2 s |
| `src/assets/` | 素材、字体文件、共享的布局 CSS；`section-placeholder.svg` 是段落配图的占位图，换成真截图 |
| `src/hyperframes.json`、`src/meta.json` | HyperFrames 工程配置；`meta.json` 的 id / name 改成片子编号 |
| `brief.md`、`storyboard.md` | 仓库规则要求的需求和分镜骨架 |

## 改文案

文案是场景变量，在 `index.html` 的宿主上改 `data-variable-values`，不用动场景文件：

```html
<div id="title" … data-variable-values='{"title":"新片名","subtitle":"新副标题"}'></div>
```

同一个场景文件可以挂多次（每次给不同的 `data-variable-values`）。

## 段落场景（左文右图）

`compositions/section.html`：左边编号 / 标题 / 工具 / 一行说明，右边一张图或一块动画图形。**一个文件挂多次**，每段一个宿主：

```html
<div id="s3" class="clip"
  data-composition-id="section"
  data-composition-src="compositions/section.html"
  data-variable-values='{"num":"03","name":"标题","tools":"工具 / 关键词","desc":"一行说明","accent":"#a98bd8","image":"assets/shot.png"}'
  data-start="8.5" data-duration="3" data-track-index="1" data-track-kind="graphics"
  data-width="1920" data-height="1080"></div>
```

| 变量 | 说明 |
|---|---|
| `num` / `name` / `tools` / `desc` | 四行文字。`name` 一行放不下时自动缩小（104 → 64 px），再长就换成两行 72 px，尽量不超过 20 个字。`desc` 在 3 s 的段落里控制在 8 个词以内（阅读时长下限） |
| `accent` | 编号和工具行的颜色 |
| `image` | `assets/` 里的截图或关键帧，自带卡片和缓慢推近；留空（`""`）则右栏空着，叠一个图形场景 |

- 每段的宿主 `id` 不同（`s1`、`s2`…），`data-composition-id` 一律写 `section`。运行时会给每次挂载分配独立的 id，选择器和时间轴互不干扰。
- 退场按宿主的 `data-duration` 自动定在结束前 0.17 s，段落多长都行（≥ 1.5 s）。读不到时控制台报错，`check` 会失败。
- 场景内部用 class 不用 id（同一页上会有好几份）。

### 右栏放动画图形

`image` 留空，再挂一个图形场景到轨道 2，**`data-start` / `data-duration` 和段落完全一样**，`accent` 也传同一个值（见 `index.html` 里的 `s2` 和 `g2`）。`compositions/graphic-flow.html`（三步流程图，三个标签是变量）是例子。做新图形就复制它，保留三样东西：

- 画布位置 x 1000–1728、y 300–780（和段落右栏同一个框）；
- 0.08 s 从右侧滑入；
- 按槽位时长退场的那段代码。

图形最好一直有东西在动，直到退场（例子里是一个点匀速走完三步），否则长段落会被 `check` 报 `dead_stretch`。

## 加一个场景

1. 复制 `title.html` 为 `compositions/<id>.html`，把根节点的 `data-composition-id`、`window.__timelines["<id>"]` 和所有元素 id 的前缀改成 `<id>`（整页 id 不能重复）。
2. `index.html` 里加宿主：`id`、`data-composition-id` 都是 `<id>`，`data-start` / `data-duration` 接上前一场，`data-track-index="1"`（不重叠的场景共用一条轨）。根节点的 `data-duration` 改成总时长。
3. `lint` → `check` → `snapshot` 看帧。

## 规矩和坑

1. **字体变量必须写在 `index.html` 的内联样式里。** 渲染时编译器只扫内联样式来决定嵌入哪些内置字体；写进外链 CSS 的话，预览截图正常，成片却悄悄退成 PingFang 的西文字形。外链 CSS 只放布局类。
2. **中文用 PingFang（`local()`），只能在 Mac 本机渲染。** lint 的 `font_face_local_only` 是预期警告。要云渲染或换机器，改用 Noto Sans SC（Google Fonts，OFL）。
3. **内置字体直接写名字**（Inter 400/700/900、JetBrains Mono、Montserrat 等，见 `hyperframes` skill 的 `references/format.md`）。编译器会从 Google Fonts 拉其他字重，缓存到 `~/.cache/hyperframes/fonts`（免费，第一次要联网）。
4. **SVG 描线用 `pathLength="1000"`**，dash 偏移 1000 → 0。GSAP 会把 px 值取整，`pathLength="1"` 时线条会一下子整条出现。
5. **进场放子元素，退场放外层 wrapper。** 同一个元素上第二个 `fromTo` 要加 `immediateRender: false`；场景里只用 `fromTo`，不用 `from`。
6. **注释里不要写字面的 style 标签**，lint 会误报 `unbalanced_style_tags`。
7. **文字要停够时间**：`check` 的 `line_unread` 按 0.75 ×（0.5 s + 0.3 s × 词数）算，中文按分词计（「按文字稿定剪点」约 6 个词）；全片最后一行至少停 2 s。
8. **第 0 帧别空着**（它是缩略图）：标题从暗色变亮，而不是从透明淡入。
9. **转场不留空**：下一场的宿主可以比名义节拍提前 0.3 s 开始，上一场在提前点之前的 0.18 s 内退完（001-demo 的做法，见 `videos/001-demo/storyboard.md`）。
10. **预览截图不等于成片，`check` 也不是万能的**：最后一定要看 `selfcheck.sh` 从成片里抽出来的帧。做段落模板时，一个过长的标题压到了右栏图片上，`check` 一条都没报，是看帧才发现的。
11. **CLI 一律走 `scripts/hf.sh`**，不直接 `npx hyperframes`，也不发 `events` / `feedback`。
