#!/usr/bin/env python3
"""film.py — AI 生成路线的导演工具：分镜表 → 关键帧（h3）→ 图生视频 → 审片 → 拼接 → 自检。

所有步骤都由片子目录里的 prompts/shots.json（镜头清单）驱动，结果写回这份清单：

  film.py plan      DIR [--write [-m 说明]] 校验清单、列出每镜进度；--write 存版本并生成 storyboard.md 和分镜页面
  film.py versions  DIR                     列出分镜版本历史和每次改动摘要
  film.py diff      DIR [A] [B]             按镜头对比两版分镜（默认最新版 → 当前）
  film.py revert    DIR VER [--shots ...] [-m 说明]
                                            整版或单镜回退到指定版本，自动清理失效的选片
  film.py keyframes DIR [--stage trial|final] [--shots s01,s02] [--yes] ...
                                            用 h3 出参考图和关键帧。默认只打印计划和 CU 预算，
                                            --yes 才开机；跑完一定 h3 down（--keep-vm 除外）
  film.py pick      DIR TARGET PATH         选定一张关键帧 / 一条视频（TARGET: s01 或 ref:名字）
  film.py clips     DIR [--backend placeholder|h3-video] [--stage trial|final] [--shots ...] [--yes]
                                            图生视频。placeholder：关键帧 + 运镜（免费、本机，做动态分镜）；
                                            h3-video：MiniMax-H3 首帧生视频（带环境音），默认只打印计划和预算
  film.py review    DIR [--what board|keyframes|clips]
                                            拼审片图到 assets/review/
  film.py assemble  DIR [-o renders/x.mp4] [--subs] [--no-audio]
                                            按清单顺序裁剪、转场、拼接；台词另存 SRT
  film.py check     DIR [VIDEO]             用 scripts/selfcheck.sh 核对成片

只用 Python 标准库；需要 ffmpeg / ffprobe；关键帧和 h3-video 需要 h3（见全局 CLAUDE.md）。
"""
from __future__ import annotations

import argparse
import base64
import copy
import difflib
import html
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SELFCHECK = REPO / "scripts" / "selfcheck.sh"
SHOTS = Path("prompts") / "shots.json"
VERSIONS = Path("prompts") / "versions"

FADE = 0.5  # crossfade length for transition "fade", seconds
ASPECTS = {"1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16", "21:9"}
CAMERAS = ("static", "push-in", "pull-out", "pan-left", "pan-right", "tilt-up", "tilt-down")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv"}

# h3 cost model (from `h3 --help`): a boot costs ~1.6 CU and ~11 min; an A100 burns ~8.9 CU/h.
H3_BOOT_CU = 1.6
H3_CU_PER_HOUR = 8.9
USD_PER_CU = 0.10
MIN_PER_IMAGE = {4: 0.5, 16: 2.5}  # rough, from h3's own timings
STAGES = {"trial": {"mp": 4, "n": 2}, "final": {"mp": 16, "n": 1}}

# h3 video = MiniMax-H3 first-frame image-to-video with native audio (colab-use `h3 video`).
BACKENDS = ("placeholder", "h3-video")
VIDEO_FPS = 24
VIDEO_SHORT = 768
VIDEO_MAX_PIXELS = 1344 * 768
VIDEO_SECONDS = (4, 15)
# Per 5 s 1344x768 I2V clip on an A100, from colab-use's README (measured 2026-11-27):
# 20 steps ~5.7 min / ~1.05 CU, --turbo (8 steps) ~2.3 min / ~0.4 CU.
CLIP_STAGES = {"trial": {"turbo": True, "n": 1, "cu": 0.4, "min": 2.3},
               "final": {"turbo": False, "n": 1, "cu": 1.05, "min": 5.7}}
I2V_HEAD = "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."


class FilmError(Exception):
    pass


# ---------------------------------------------------------------- shot list


def load(vdir: Path) -> dict:
    path = vdir / SHOTS
    if not path.is_file():
        raise FilmError(f"找不到 {path}（片子目录里要有 prompts/shots.json）")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise FilmError(f"{path} 不是合法 JSON：{e}") from e


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def save(vdir: Path, data: dict) -> None:
    _write_json(vdir / SHOTS, data)


def rel(vdir: Path, p: Path | str) -> str:
    """Path relative to the video dir (never an absolute path: the repo is public)."""
    p = Path(p).resolve()
    try:
        return p.relative_to(vdir.resolve()).as_posix()
    except ValueError:
        return p.name


def fmt(data: dict) -> tuple[int, int, int, str]:
    f = data.get("format", {})
    return int(f.get("width", 1920)), int(f.get("height", 1080)), int(f.get("fps", 30)), f.get("aspect", "16:9")


def timeline(data: dict) -> tuple[list[float], float]:
    """Start time of every shot and the total length, crossfades overlapping."""
    starts, t = [], 0.0
    shots = data.get("shots", [])
    for i, s in enumerate(shots):
        starts.append(round(t, 3))
        t += float(s["duration"])
        if s.get("transition") == "fade" and i < len(shots) - 1:
            t -= FADE
    return starts, round(t, 3)


def pad(text: str, width: int) -> str:
    """Left-justify by display width (CJK characters take two columns)."""
    used = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(1, width - used)


def md(text: str) -> str:
    """Escape for a Markdown table cell: keep <Picture 1> visible and | from splitting cells."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|")


def md_inline(text: str) -> str:
    """Escape HTML-like placeholders such as <Picture 1> for Markdown prose."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _or_unwritten(val: object) -> str:
    s = (str(val) if val is not None else "").strip()
    return s if s else "（未写）"


def reading_words(text: str) -> int:
    """Rough word count for the reading-time floor: CJK chars / 2 + Latin words."""
    cjk = len(re.findall(r"[㐀-鿿]", text))
    latin = len(re.findall(r"[A-Za-z0-9]+", text))
    return round(cjk / 2) + latin


def validate(vdir: Path, data: dict) -> tuple[list[str], list[str]]:
    errors, warns = [], []
    if data.get("version") != 1:
        errors.append("version 必须是 1")
    w, h, fps, aspect = fmt(data)
    if aspect not in ASPECTS:
        errors.append(f"format.aspect {aspect!r} 不在 {sorted(ASPECTS)} 里")
    else:
        a, b = (float(x) for x in aspect.split(":"))
        if abs(w / h - a / b) > 0.02:
            errors.append(f"format {w}x{h} 和 aspect {aspect} 对不上")
    refs = data.get("references", {}) or {}
    for name, r in refs.items():
        if not re.fullmatch(r"[a-z0-9_-]+", name):
            errors.append(f"参考图名 {name!r} 只能用小写字母、数字、- _")
        if not (r.get("prompt") or r.get("image")):
            errors.append(f"参考图 {name} 既没有 prompt 也没有 image")
        if not (r.get("desc") or "").strip():
            warns.append(f"参考图 {name}: 没写说明（desc）")
        if r.get("image") and not (vdir / r["image"]).is_file():
            errors.append(f"参考图 {name} 的 image 不存在：{r['image']}")
    shots = data.get("shots") or []
    if not shots:
        errors.append("shots 是空的")
    seen, uses_video = set(), False
    for i, s in enumerate(shots):
        sid = s.get("id", f"#{i + 1}")
        if not re.fullmatch(r"[a-z0-9_-]+", str(sid)):
            errors.append(f"镜头 id {sid!r} 只能用小写字母、数字、- _")
        if sid in seen:
            errors.append(f"镜头 id {sid} 重复")
        seen.add(sid)
        d = s.get("duration")
        if not isinstance(d, (int, float)) or d <= 0:
            errors.append(f"{sid}: duration 要是正数")
            continue
        if d < 2 or d > 10:
            warns.append(f"{sid}: {d} s 的镜头偏{'短' if d < 2 else '长'}（AI 镜头通常 3–6 s）")
        tr = s.get("transition", "cut")
        if tr not in ("cut", "fade"):
            errors.append(f"{sid}: transition 只能是 cut 或 fade")
        if tr == "fade" and d < 2 * FADE + 0.5:
            errors.append(f"{sid}: 镜头太短，放不下 {FADE} s 的叠化")
        kf = s.get("keyframe") or {}
        if not kf.get("prompt") and not kf.get("image"):
            errors.append(f"{sid}: keyframe.prompt 是空的")
        if not (kf.get("desc") or "").strip():
            warns.append(f"{sid}: 没写画面说明（keyframe.desc，中文，给用户看）")
        for r in kf.get("refs") or []:
            if r not in refs:
                errors.append(f"{sid}: refs 里的 {r!r} 没在 references 里定义")
        if kf.get("refs") and "<Picture" not in (kf.get("prompt") or ""):
            warns.append(f"{sid}: 用了参考图，但提示词里没写 <Picture 1>…（h3 edit 靠它指代参考图）")
        if kf.get("image") and not (vdir / kf["image"]).is_file():
            errors.append(f"{sid}: keyframe.image 不存在：{kf['image']}")
        mo = s.get("motion") or {}
        if not (mo.get("desc") or "").strip():
            warns.append(f"{sid}: 没写动作说明（motion.desc）")
        if mo.get("camera", "static") not in CAMERAS:
            errors.append(f"{sid}: motion.camera 只能是 {', '.join(CAMERAS)}")
        if mo.get("clip") and not (vdir / mo["clip"]).is_file():
            errors.append(f"{sid}: motion.clip 不存在：{mo['clip']}")
        be = shot_backend(data, s)
        if be not in BACKENDS:
            errors.append(f"{sid}: 不认识的后端 {be!r}（可选 {', '.join(BACKENDS)}）")
        elif be == "h3-video":
            uses_video = True
            lo, hi = VIDEO_SECONDS
            if d > hi:
                errors.append(f"{sid}: h3-video 一条最长 {hi} s，这镜 {d} s（拆成两镜）")
            elif d < lo:
                warns.append(f"{sid}: h3-video 最短出 {lo} s，拼接时裁到 {d} s")
            vp = (mo.get("prompt") or "").strip()
            if not vp:
                errors.append(f"{sid}: 用 h3-video 要写 motion.prompt（首帧之后画面怎么动）")
            elif "<Picture 1>" not in vp:
                warns.append(f"{sid}: motion.prompt 里没有 <Picture 1>（官方写法 the … shown in <Picture 1> …，"
                             "帮模型对齐首帧）")
            if not (mo.get("sound") or "").strip():
                warns.append(f"{sid}: 没写 motion.sound，h3-video 会按完全无声（N/A）生成")
        line = s.get("line") or ""
        if line:
            visible = d - (FADE if tr == "fade" else 0)
            need = 0.75 * (0.5 + 0.3 * reading_words(line))
            if need > visible:
                warns.append(f"{sid}: 台词「{line}」约需 {need:.1f} s 才读得完，镜头只有 {visible:.1f} s")
    if uses_video and fps % VIDEO_FPS:
        warns.append(f"h3 视频是 {VIDEO_FPS} fps，片子是 {fps} fps：拼接时会重复帧，运镜处轻微顿挫。"
                     f"主要用 h3 视频的片子把 format.fps 设成 {VIDEO_FPS}")
    return errors, warns


def require_valid(vdir: Path, data: dict) -> None:
    errors, warns = validate(vdir, data)
    for w_ in warns:
        print(f"  ⚠ {w_}")
    if errors:
        for e in errors:
            print(f"  ✗ {e}")
        raise FilmError(f"{vdir / SHOTS} 有 {len(errors)} 处错误")


def select(data: dict, ids: str | None) -> list[dict]:
    shots = data["shots"]
    if not ids:
        return shots
    want = [x.strip() for x in ids.split(",") if x.strip()]
    known = {s["id"] for s in shots}
    missing = [x for x in want if x not in known]
    if missing:
        raise FilmError(f"没有这些镜头：{', '.join(missing)}")
    return [s for s in shots if s["id"] in want]


def full_prompt(data: dict, prompt: str) -> str:
    style = (data.get("style") or "").strip()
    return f"{prompt.strip().rstrip('.')}. {style}" if style else prompt.strip()


def shot_backend(data: dict, shot: dict, override: str | None = None) -> str:
    return override or (shot.get("motion") or {}).get("backend") or data.get("backend") or "placeholder"


def clip_kind(clip: str | None) -> str | None:
    """Where a picked clip came from, by its path: h3-final / h3-trial / placeholder / other."""
    if not clip:
        return None
    for k in ("h3-final", "h3-trial"):
        if f"/{k}/" in clip:
            return k
    return "placeholder" if Path(clip).name.startswith("placeholder-") else "other"


def video_prompt(data: dict, shot: dict) -> str:
    """MiniMax-H3 I2V prompt laid out as the official guide asks: the first-frame line, then the three
    core fields. motion.prompt continues video_style the way the guide's examples do
    ("Live-action, cinematic, the woman shown in <Picture 1> …"). Music stays N/A per clip: it would jump
    at every cut."""
    mo = shot.get("motion") or {}
    style = (data.get("video_style") or "").strip().rstrip(".,; ")
    body = (mo.get("prompt") or "").strip()
    desc = f"{style}, {body}" if style else body[:1].upper() + body[1:]
    sound = (mo.get("sound") or "").strip() or "N/A"
    return (f"{I2V_HEAD}\n\nintegrated_multimodal_description: [Shot 1] {desc}\n\n"
            f"overall_soundscape: {sound}\n\nnon_diegetic_music: N/A")


def video_frames(seconds: float) -> int:
    """MiniMax-H3 renders 17k+5 frames at 24 fps, rounding up (same rule as colab-use's video_length)."""
    n = max(5, round(seconds * VIDEO_FPS))
    return n + (5 - n % 17) % 17


def video_canvas(aspect: str) -> tuple[int, int]:
    """h3 video's canvas for an aspect: short edge 768, area <= 1344x768, sides multiples of 32.

    Same rule as colab-use remote/h3_comfy.py video_canvas. h3 stretches the first frame onto this
    canvas, so the first frame is cropped to exactly this size before upload."""
    rw, rh = (float(x) for x in aspect.split(":"))
    ratio = rw / rh
    nw, nh = (VIDEO_SHORT * ratio, float(VIDEO_SHORT)) if ratio >= 1 else (float(VIDEO_SHORT), VIDEO_SHORT / ratio)
    if nw * nh > VIDEO_MAX_PIXELS:
        k = math.sqrt(VIDEO_MAX_PIXELS / (nw * nh))
        nw, nh = nw * k, nh * k

    def near(x: float) -> set[int]:
        f, c = math.floor(x / 32) * 32, math.ceil(x / 32) * 32
        return {max(32, v) for v in (f - 32, f, c)}

    cands = [(w, h) for w in near(nw) for h in near(nh) if w * h <= VIDEO_MAX_PIXELS]
    if not cands:
        cands = [(max(32, math.floor(nw / 32) * 32), max(32, math.floor(nh / 32) * 32))]
    err = lambda c: abs((c[0] / c[1]) / ratio - 1.0)  # noqa: E731
    within = [c for c in cands if err(c) <= 0.03]
    if within:
        return min(within, key=lambda c: (abs(c[0] * c[1] - nw * nh), err(c), -(c[0] * c[1])))
    return min(cands, key=lambda c: (err(c), abs(c[0] * c[1] - nw * nh)))


# ---------------------------------------------------------------- media helpers


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


def ffprobe_json(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def image_size(path: Path) -> tuple[int, int]:
    for s in ffprobe_json(path).get("streams", []):
        if s.get("width"):
            return int(s["width"]), int(s["height"])
    raise FilmError(f"读不到图片尺寸：{path}")


def has_audio(path: Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in ffprobe_json(path).get("streams", []))


def audio_levels(path: Path) -> str:
    """Mean / peak loudness, so a director who can't listen still catches silent or clipped clips."""
    if not has_audio(path):
        return "无音轨"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0", "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True)
    mean = re.search(r"mean_volume: (-?[\d.]+) dB", r.stderr)
    peak = re.search(r"max_volume: (-?[\d.]+) dB", r.stderr)
    if not (mean and peak):
        return "音量读不出"
    m, p = float(mean.group(1)), float(peak.group(1))
    flag = "，几乎无声" if m < -50 else ("，可能削波" if p >= -0.1 else "")
    return f"音量 平均 {m:.0f} dB / 峰值 {p:.0f} dB{flag}"


def sidecar(path: Path) -> dict:
    """The .json next to a generated file (h3 writes seed / prompt there; film.py adds the keyframe)."""
    meta = path.with_suffix(".json")
    try:
        return json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def candidates(vdir: Path, sub: str, exts: set[str]) -> list[Path]:
    root = vdir / "assets" / sub
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.suffix.lower() in exts)


def tile(images: list[Path], out: Path, cols: int = 4, size: tuple[int, int] = (480, 270)) -> None:
    """Contact sheet, every cell letterboxed to `size`. Each image is its own input: the concat demuxer
    rebuilds the filter graph when the size changes (1344x768 next to 1920x1080) and drops cells."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = max(1, min(cols, len(images)))
    rows = (len(images) + cols - 1) // cols
    cw, ch = size
    inputs, chains = [], []
    for i, p in enumerate(images):
        inputs += ["-i", str(p)]
        chains.append(f"[{i}:v]scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
                      f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:white,pad=iw+4:ih+4:2:2:white,setsar=1,format=rgb24[c{i}]")
    graph = ";".join(chains) + ";" + "".join(f"[c{i}]" for i in range(len(images))) + \
        f"concat=n={len(images)}:v=1:a=0,tile={cols}x{rows}:color=white"
    run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-frames:v", "1", str(out)])


def cell(data: dict, width: int = 480) -> tuple[int, int]:
    w, h, _, _ = fmt(data)
    return width, round(width * h / w / 2) * 2


# ---------------------------------------------------------------- plan


def cmd_plan(a) -> int:
    if a.message is not None and not a.write:
        raise FilmError("-m 要和 --write 一起用")
    vdir = Path(a.dir)
    data = load(vdir)
    print(f"{data.get('title', vdir.name)} · {vdir / SHOTS}")
    require_valid(vdir, data)
    w, h, fps, aspect = fmt(data)
    starts, total = timeline(data)
    refs = data.get("references", {}) or {}
    print(f"\n{w}x{h} · {fps} fps · {aspect} · {len(data['shots'])} 镜 · 总长 {total:.2f} s\n")
    if refs:
        print("参考图")
        for name, r in refs.items():
            n = len(candidates(vdir, f"keyframes/ref-{name}", IMAGE_EXT))
            print(f"  ref:{name:<10} {'✓ ' + r['image'] if r.get('image') else f'未选（候选 {n} 张）'}")
        print()
    print(pad("镜头", 6) + pad("起点", 8) + pad("时长", 6) + pad("转场", 6) + pad("运镜", 11)
          + pad("关键帧", 16) + pad("视频", 12) + "台词")
    todo = {"keyframe": 0, "clip": 0, "video": []}
    for s, st in zip(data["shots"], starts):
        kf, mo = s.get("keyframe") or {}, s.get("motion") or {}
        nk = len(candidates(vdir, f"keyframes/{s['id']}", IMAGE_EXT))
        nc = len(candidates(vdir, f"clips/{s['id']}", VIDEO_EXT))
        k = "✓ 已选" + ("(试稿)" if "/trial/" in (kf.get("image") or "") else "") if kf.get("image") else f"候选 {nk}"
        ck = clip_kind(mo.get("clip"))
        c = ({"h3-final": "✓ h3 定稿", "h3-trial": "✓ h3 试稿", "placeholder": "✓ 占位"}.get(ck, "✓ 已选")
             if ck else f"候选 {nc}")
        todo["keyframe"] += not kf.get("image")
        todo["clip"] += not mo.get("clip")
        if shot_backend(data, s) == "h3-video" and ck not in ("h3-final", "h3-trial"):
            todo["video"].append(s["id"])
        print(pad(s["id"], 6) + pad(f"{st:.2f}", 8) + pad(f"{s['duration']}", 6)
              + pad(s.get("transition", "cut"), 6) + pad(mo.get("camera", "static"), 11)
              + pad(k, 16) + pad(c, 12) + s.get("line", ""))
    print()
    if todo["keyframe"]:
        print(f"下一步：{todo['keyframe']} 镜还没选关键帧 → film.py keyframes {vdir}（先 dry-run 看预算）")
    elif todo["clip"]:
        print(f"下一步：{todo['clip']} 镜还没选视频 → film.py clips {vdir}"
              + ("（先 --backend placeholder 出免费的动态分镜；h3-video 先 dry-run 看预算）" if todo["video"] else ""))
    elif todo["video"]:
        print(f"下一步：{', '.join(todo['video'])} 还是占位视频（动态分镜）。用户看过后 → film.py clips {vdir}"
              "（h3-video，先 dry-run 看预算）；或者直接 assemble 出动态分镜")
    else:
        print(f"下一步：film.py assemble {vdir}")
    sb = storyboard(data)
    vers = load_versions(vdir)
    latest = vers[-1] if vers else None
    if a.write:
        if is_template(vdir):
            print("模板目录不存版本")
            write_storyboard(vdir, data)
            print(f"已重写 {vdir / 'storyboard.md'}")
        else:
            if latest is None or sb != latest["storyboard"]:
                note = (a.message or "").strip() or ("初稿" if latest is None else "（未写说明）")
                ver = save_version(vdir, sb, note)
                if latest is None:
                    print(f"已存为 v{ver['version']}（{ver['note']}）：共 {len(data['shots'])} 镜")
                else:
                    d = diff_storyboards(latest["storyboard"], sb)
                    print(f"已存为 v{ver['version']}（{ver['note']}）：{diff_summary(d)}")
                    for w_ in desc_sync_warnings(latest["storyboard"], sb):
                        print(f"  ⚠ {w_}")
                    warn_stale_picks(stale_picks(latest["storyboard"], data))
            else:
                ver = latest
                print(f"分镜和 v{latest['version']} 一样，没存新版本")
            write_storyboard(vdir, data, ver=ver)
            print(f"已重写 {vdir / 'storyboard.md'}")
            print("分镜页面：assets/review/storyboard.html")
    else:
        if is_template(vdir) and latest is None:
            print("版本：模板目录不存版本")
        elif latest is None:
            print("版本：还没有（plan --write 存第一版）")
        elif sb == latest["storyboard"]:
            print(f"版本：v{latest['version']}")
        else:
            print(f"版本：v{latest['version']}（有未保存的改动 → plan --write -m 说明）")
    return 0


def keyframe_status(image: str | None) -> str:
    if not image:
        return "未选"
    if "/trial/" in image:
        return "试稿"
    if "/final/" in image:
        return "定稿"
    return "已选"


def clip_status(clip: str | None) -> str:
    ck = clip_kind(clip)
    if ck is None:
        return "未选"
    return {"placeholder": "占位", "h3-trial": "h3 试稿", "h3-final": "h3 定稿"}.get(ck, "已选")


def _safe_rel(vdir: Path, p: str | None) -> str:
    """Return a relative POSIX path for display; never leak a local absolute path."""
    if not p:
        return ""
    pp = Path(p)
    return rel(vdir, pp) if pp.is_absolute() else pp.as_posix()


def make_thumbnail_data_uri(path: Path) -> str | None:
    """Scale an image to width 480 JPEG and return a base64 data URI, or None on failure."""
    if not path.is_file():
        return None
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=480:-2",
             "-frames:v", "1", "-q:v", "4", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"],
            capture_output=True, check=False,
        )
    except OSError:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(r.stdout).decode("ascii")


def _prev_version(vdir: Path, ver: dict | None) -> dict | None:
    if ver is None:
        return None
    target_n = ver.get("version")
    prev: dict | None = None
    for v in load_versions(vdir):
        if v.get("version") == target_n:
            return prev
        prev = v
    return None


_PROMPT_DETAIL_FIELDS = {
    "keyframe.prompt", "keyframe.refs",
    "motion.camera", "motion.prompt", "motion.sound", "motion.backend",
}

_STORYBOARD_CSS = """\
:root {
  --bg: #f8f9fa;
  --fg: #18181b;
  --muted: #52525b;
  --card: #ffffff;
  --subtle: #f1f3f5;
  --border: #e4e4e7;
  --thumb-bg: #eceef1;
  --changed-bg: #fffbeb;
  --changed-border: #d97706;
  --changed-fg: #92400e;
  --new-bg: #ecfdf5;
  --new-border: #059669;
  --new-fg: #065f46;
  --del-bg: #fef2f2;
  --del-border: #dc2626;
  --del-fg: #991b1b;
  --pill-bg: #f4f4f5;
  --pill-fg: #3f3f46;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111214;
    --fg: #f4f4f5;
    --muted: #a1a1aa;
    --card: #1a1c20;
    --subtle: #23262c;
    --border: #2e323a;
    --thumb-bg: #141619;
    --changed-bg: rgba(217, 119, 6, 0.16);
    --changed-border: #f59e0b;
    --changed-fg: #fcd34d;
    --new-bg: rgba(16, 185, 129, 0.16);
    --new-border: #10b981;
    --new-fg: #6ee7b7;
    --del-bg: rgba(239, 68, 68, 0.16);
    --del-border: #ef4444;
    --del-fg: #fca5a5;
    --pill-bg: #272a31;
    --pill-fg: #d4d4d8;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 16px;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, "PingFang SC", "Hiragino Sans GB", sans-serif;
  font-size: 14px;
  line-height: 1.55;
  overflow-x: hidden;
}
.page {
  max-width: 1200px;
  margin: 0 auto;
}
.header {
  margin-bottom: 18px;
}
h1 {
  margin: 0 0 6px;
  font-size: 22px;
  line-height: 1.3;
  overflow-wrap: anywhere;
}
h2 {
  margin: 22px 0 12px;
  font-size: 16px;
  font-weight: 600;
}
.version-line {
  color: var(--muted);
  font-size: 13px;
  margin: 0 0 6px;
}
.meta-line {
  color: var(--muted);
  font-size: 13px;
  margin: 0 0 10px;
}
.change-banner {
  margin: 10px 0 12px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
  background: var(--subtle);
  border: 1px solid var(--border);
  overflow-wrap: anywhere;
}
.change-banner.has-diff {
  background: var(--changed-bg);
  border-color: var(--changed-border);
  color: var(--changed-fg);
}
details {
  margin-top: 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--subtle);
  padding: 6px 10px;
}
details > summary {
  cursor: pointer;
  font-size: 13px;
  color: var(--muted);
  font-weight: 500;
  list-style-position: outside;
}
details > summary.changed {
  color: var(--changed-fg);
  border-left: none;
  background: transparent;
  padding-left: 0;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr));
  gap: 16px;
}
.card {
  min-width: 0;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.card.is-changed {
  border-color: var(--changed-border);
}
.card.is-new {
  border-color: var(--new-border);
}
.card-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
}
.shot-id {
  font-weight: 700;
  font-size: 15px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.time-range {
  font-size: 12px;
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.meta-pill {
  font-size: 12px;
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--pill-bg);
  color: var(--pill-fg);
}
.meta-pill.changed {
  border-left: none;
  padding: 1px 7px;
  background: var(--changed-bg);
  color: var(--changed-fg);
  border: 1px solid var(--changed-border);
}
.badge {
  display: inline-block;
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
  max-width: 100%;
  overflow-wrap: anywhere;
}
.badge.changed {
  border-left: none;
  background: var(--changed-bg);
  color: var(--changed-fg);
  border: 1px solid var(--changed-border);
}
.badge.new {
  background: var(--new-bg);
  color: var(--new-fg);
  border: 1px solid var(--new-border);
}
.thumb {
  width: 100%;
  border-radius: 8px;
  overflow: hidden;
  background: var(--thumb-bg);
  border: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: center;
}
.thumb img {
  width: 100%;
  height: 100%;
  max-width: 100%;
  object-fit: contain;
  display: block;
}
.thumb.placeholder {
  color: var(--muted);
  font-size: 13px;
}
.fields {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.field {
  padding: 4px 6px;
  border-radius: 6px;
  overflow-wrap: anywhere;
}
.field .label,
.prompt-row .label {
  display: inline-block;
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  margin-right: 6px;
}
.field.changed,
.prompt-row.changed {
  border-left: 3px solid var(--changed-border);
  background: var(--changed-bg);
  padding-left: 8px;
  border-radius: 4px;
}
.status-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  font-size: 12px;
  color: var(--muted);
  padding-top: 6px;
  border-top: 1px solid var(--border);
}
.pick-path {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  overflow-wrap: anywhere;
  color: var(--muted);
}
.prompt-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}
.prompt-row {
  font-size: 12px;
  padding: 4px 6px;
  border-radius: 4px;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.deleted-section {
  margin-top: 20px;
  padding: 10px 12px;
  border-radius: 8px;
  font-size: 13px;
  background: var(--del-bg);
  border: 1px solid var(--del-border);
  color: var(--del-fg);
}
"""


def _render_thumb(vdir: Path, img_rel: str | None, alt: str, aspect_css: str) -> str:
    esc = html.escape
    style_attr = f' style="aspect-ratio: {aspect_css};"'
    if not img_rel:
        return f'<div class="thumb placeholder empty"{style_attr}>还没出图</div>'
    uri = make_thumbnail_data_uri(vdir / img_rel)
    if uri is None:
        return f'<div class="thumb placeholder missing"{style_attr}>图片读不到</div>'
    return f'<div class="thumb"{style_attr}><img src="{uri}" alt="{esc(alt)}"></div>'


def write_storyboard_html(vdir: Path, data: dict, ver: dict | None = None) -> Path:
    """Write a self-contained read-only storyboard HTML review page under assets/review/."""
    esc = html.escape
    w, h, fps, aspect = fmt(data)
    starts, total = timeline(data)
    shots = data.get("shots") or []
    refs = data.get("references") or {}
    title = str(data.get("title") or vdir.name)
    aspect_css = f"{w} / {h}"

    prev_ver = _prev_version(vdir, ver)
    diff: dict | None = None
    if prev_ver is not None:
        cur_sb = ver["storyboard"] if (ver is not None and "storyboard" in ver) else storyboard(data)
        diff = diff_storyboards(prev_ver["storyboard"], cur_sb)

    global_items = diff["global"] if diff else []
    mod_map: dict[str, list[tuple[str, object, object]]] = {sid: ch for sid, ch in diff["modified"]} if diff else {}
    added_ids: set[str] = {sid for sid, _ in diff["added"]} if diff else set()
    deleted_ids: list[str] = list(diff["deleted"]) if diff else []
    reordered = diff["reordered"] if diff else None

    style_changed = any(k == "field" and lbl == "关键帧风格" for k, lbl, _, _ in global_items)
    video_style_changed = any(k == "field" and lbl == "视频风格" for k, lbl, _, _ in global_items)

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(title)} · 分镜</title>",
        f"<style>\n{_STORYBOARD_CSS}</style>",
        "</head>",
        "<body>",
        '<main class="page">',
        '<header class="header">',
        f"<h1>{esc(title)}</h1>",
    ]
    if ver is not None:
        parts.append(
            f'<p class="version-line">v{esc(str(ver["version"]))} · '
            f'{esc(short_time(ver.get("time", "")))} · {esc(str(ver.get("note", "")))}</p>'
        )
    parts.append(
        f'<p class="meta-line">总长 {total:.1f} s · {w}×{h} · {fps} fps · {esc(aspect)} · {len(shots)} 镜</p>'
    )

    if diff is None:
        parts.append('<div class="change-banner initial" data-change="initial">初稿</div>')
    else:
        banner_parts: list[str] = []
        if global_items:
            g_labels = list(dict.fromkeys(lbl for _, lbl, _, _ in global_items))
            banner_parts.append("、".join(g_labels))
        if reordered:
            ids_a, ids_b = reordered
            banner_parts.append(f"顺序变了（{' '.join(ids_a)} → {' '.join(ids_b)}）")
        shot_parts: list[str] = []
        for sid, ch in diff["modified"]:
            labels = list(dict.fromkeys(shot_label(p) for p, _, _ in ch))
            shot_parts.append(f"{sid} {'、'.join(labels)}")
        if diff["added"]:
            shot_parts.append(f"新增 {'、'.join(sid for sid, _ in diff['added'])}")
        if diff["deleted"]:
            shot_parts.append(f"删除 {'、'.join(diff['deleted'])}")
        if shot_parts:
            banner_parts.append("；".join(shot_parts))
        banner_text = f"本版改动（相对 v{prev_ver['version']}）：{'；'.join(banner_parts) if banner_parts else '无改动'}"
        cls = "change-banner has-diff" if diff_has_changes(diff) else "change-banner"
        parts.append(f'<div class="{cls}" data-change="diff">{esc(banner_text)}</div>')

    if data.get("style") or data.get("video_style") or style_changed or video_style_changed:
        any_style_ch = style_changed or video_style_changed
        sum_cls = ' class="changed"' if any_style_ch else ""
        sum_suffix = "（本版有改动）" if any_style_ch else ""
        parts.append('<details class="style-details">')
        parts.append(f"<summary{sum_cls}>风格设定{sum_suffix}</summary>")
        parts.append('<div class="prompt-list">')
        st_cls = "prompt-row changed" if style_changed else "prompt-row"
        parts.append(
            f'<div class="{st_cls}"><span class="label">关键帧风格</span>'
            f'<span class="val">{esc(_or_unwritten(data.get("style")))}</span></div>'
        )
        if data.get("video_style") or video_style_changed:
            vst_cls = "prompt-row changed" if video_style_changed else "prompt-row"
            parts.append(
                f'<div class="{vst_cls}"><span class="label">视频风格</span>'
                f'<span class="val">{esc(_or_unwritten(data.get("video_style")))}</span></div>'
            )
        parts.append("</div></details>")

    parts.append("</header>")

    if refs:
        parts.append('<section class="section">')
        parts.append("<h2>参考图</h2>")
        parts.append('<div class="grid ref-grid">')
        for name, r in refs.items():
            r = r or {}
            ref_added = any(k == "ref_add" and lbl == f"+ 参考图 {name}" for k, lbl, _, _ in global_items)
            ref_desc_ch = any(k == "field" and lbl == f"参考图 {name} 说明" for k, lbl, _, _ in global_items)
            ref_prompt_ch = any(k == "field" and lbl == f"参考图 {name}" for k, lbl, _, _ in global_items)
            ref_ch_labels: list[str] = []
            if ref_desc_ch:
                ref_ch_labels.append("说明")
            if ref_prompt_ch:
                ref_ch_labels.append("提示词")
            card_cls = "card ref-card"
            badge_html = ""
            if ref_added:
                card_cls += " is-new"
                badge_html = '<span class="badge new">新增</span>'
            elif ref_ch_labels:
                card_cls += " is-changed"
                badge_html = f'<span class="badge changed">本版改动：{esc("、".join(ref_ch_labels))}</span>'
            parts.append(f'<article class="{card_cls}" data-ref="{esc(name)}">')
            parts.append(f'<div class="card-head"><span class="shot-id">{esc(name)}</span>{badge_html}</div>')
            parts.append(_render_thumb(vdir, r.get("image"), f"参考图 {name}", aspect_css))
            d_cls = "field changed" if ref_desc_ch else "field"
            parts.append(
                f'<div class="fields"><div class="{d_cls}" data-field="desc">'
                f'<span class="label">说明</span><span class="val">{esc(_or_unwritten(r.get("desc")))}</span>'
                f"</div></div>"
            )
            img_rel = _safe_rel(vdir, r.get("image"))
            img_stat = keyframe_status(r.get("image"))
            path_html = f' <code class="pick-path">{esc(img_rel)}</code>' if img_rel else ""
            parts.append(f'<div class="status-bar"><span>参考图：{esc(img_stat)}{path_html}</span></div>')
            p_sum_cls = ' class="changed"' if ref_prompt_ch else ""
            p_sum_sfx = "（本版有改动）" if ref_prompt_ch else ""
            p_row_cls = "prompt-row changed" if ref_prompt_ch else "prompt-row"
            parts.append("<details>")
            parts.append(f"<summary{p_sum_cls}>给模型的提示词{p_sum_sfx}</summary>")
            parts.append(
                f'<div class="prompt-list"><div class="{p_row_cls}" data-field="prompt">'
                f'<span class="label">提示词</span><span class="val">{esc(_or_unwritten(r.get("prompt")))}</span>'
                f"</div></div></details>"
            )
            parts.append("</article>")
        parts.append("</div></section>")

    parts.append('<section class="section">')
    parts.append("<h2>分镜</h2>")
    parts.append('<div class="grid shot-grid">')
    for s, st in zip(shots, starts):
        sid = str(s["id"])
        kf, mo = s.get("keyframe") or {}, s.get("motion") or {}
        dur = float(s["duration"])
        tr = str(s.get("transition", "cut"))
        ch_list = mod_map.get(sid, [])
        ch_fields = {p for p, _, _ in ch_list}
        is_new = sid in added_ids

        card_cls = "card shot-card"
        badge_html = ""
        if is_new:
            card_cls += " is-new"
            badge_html = '<span class="badge new">新增</span>'
        elif ch_list:
            card_cls += " is-changed"
            ch_labels = list(dict.fromkeys(shot_label(p) for p, _, _ in ch_list))
            badge_html = f'<span class="badge changed">本版改动：{esc("、".join(ch_labels))}</span>'

        dur_cls = "meta-pill changed" if "duration" in ch_fields else "meta-pill"
        tr_cls = "meta-pill changed" if "transition" in ch_fields else "meta-pill"
        parts.append(f'<article class="{card_cls}" data-shot="{esc(sid)}">')
        parts.append(
            '<div class="card-head">'
            f'<span class="shot-id">{esc(sid)}</span>'
            f'<span class="time-range">{st:.1f}–{st + dur:.1f} s</span>'
            f'<span class="{dur_cls}">{dur:g} s</span>'
            f'<span class="{tr_cls}">{esc(tr)}</span>'
            f"{badge_html}"
            "</div>"
        )
        parts.append(_render_thumb(vdir, kf.get("image"), f"{sid} 关键帧", aspect_css))

        parts.append('<div class="fields">')
        for fkey, flabel, fval in (
            ("keyframe.desc", "画面说明", kf.get("desc")),
            ("motion.desc", "动作说明", mo.get("desc")),
            ("line", "台词", s.get("line")),
            ("notes", "备注", s.get("notes")),
        ):
            f_cls = "field changed" if fkey in ch_fields else "field"
            parts.append(
                f'<div class="{f_cls}" data-field="{esc(fkey)}">'
                f'<span class="label">{esc(flabel)}</span>'
                f'<span class="val">{esc(_or_unwritten(fval))}</span></div>'
            )
        parts.append("</div>")

        kf_rel = _safe_rel(vdir, kf.get("image"))
        cl_rel = _safe_rel(vdir, mo.get("clip"))
        kf_path_html = f' <code class="pick-path">{esc(kf_rel)}</code>' if kf_rel else ""
        cl_path_html = f' <code class="pick-path">{esc(cl_rel)}</code>' if cl_rel else ""
        parts.append(
            '<div class="status-bar">'
            f'<span>关键帧：{esc(keyframe_status(kf.get("image")))}{kf_path_html}</span>'
            f'<span>视频：{esc(clip_status(mo.get("clip")))}{cl_path_html}</span>'
            "</div>"
        )

        prompt_changed = bool(ch_fields & _PROMPT_DETAIL_FIELDS)
        p_sum_cls = ' class="changed"' if prompt_changed else ""
        p_sum_sfx = "（本版有改动）" if prompt_changed else ""
        parts.append("<details>")
        parts.append(f"<summary{p_sum_cls}>给模型的提示词{p_sum_sfx}</summary>")
        parts.append('<div class="prompt-list">')
        prompt_rows: list[tuple[str, str, str]] = [
            ("keyframe.prompt", "画面", _or_unwritten(kf.get("prompt"))),
        ]
        if kf.get("refs") or "keyframe.refs" in ch_fields:
            refs_text = "、".join(str(r) for r in (kf.get("refs") or [])) or "（无）"
            prompt_rows.append(("keyframe.refs", "参考图", refs_text))
        prompt_rows.append(("motion.camera", "运镜", str(mo.get("camera", "static"))))
        prompt_rows.append(("motion.prompt", "动作", _or_unwritten(mo.get("prompt"))))
        prompt_rows.append(("motion.sound", "声音", _or_unwritten(mo.get("sound"))))
        if mo.get("backend") or "motion.backend" in ch_fields:
            prompt_rows.append(("motion.backend", "后端", str(mo.get("backend") or "（默认）")))
        for pkey, plabel, pval in prompt_rows:
            p_cls = "prompt-row changed" if pkey in ch_fields else "prompt-row"
            parts.append(
                f'<div class="{p_cls}" data-field="{esc(pkey)}">'
                f'<span class="label">{esc(plabel)}</span>'
                f'<span class="val">{esc(pval)}</span></div>'
            )
        parts.append("</div></details>")
        parts.append("</article>")
    parts.append("</div></section>")

    if deleted_ids:
        parts.append(
            f'<section class="deleted-section" data-deleted="true">'
            f'本版删除：{esc("、".join(deleted_ids))}</section>'
        )

    parts.append("</main></body></html>\n")
    out = vdir / "assets" / "review" / "storyboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text("\n".join(parts), encoding="utf-8")
    tmp.replace(out)
    return out


def write_storyboard(vdir: Path, data: dict, ver: dict | None = None) -> None:
    w, h, fps, aspect = fmt(data)
    starts, total = timeline(data)
    lines = [
        f"# {data.get('title', vdir.name)} · storyboard",
        "",
        "<!-- 由 scripts/ai/film.py plan --write 从 prompts/shots.json 生成；改分镜请改 shots.json 再重新生成。 -->",
        "",
    ]
    if ver is not None:
        lines += [f"版本 v{ver['version']} · {short_time(ver.get('time', ''))} · {ver.get('note', '')}", ""]
    lines += [
        f"{total:.1f} s · {w}×{h} · {fps} fps · {aspect} · {len(data['shots'])} 镜。正式片：分镜确认后再出关键帧。",
        "",
        f"关键帧风格：{data.get('style', '')}",
        "",
    ]
    if data.get("video_style"):
        lines += [f"视频风格：{data['video_style']}", ""]
    refs = data.get("references", {}) or {}
    if refs:
        lines += ["## 参考图（人物 / 风格设定）", "", "| 名字 | 说明 | 提示词 |", "|---|---|---|"]
        lines += [
            f"| `{n}` | {md(_or_unwritten(r.get('desc')))} | {md(_or_unwritten(r.get('prompt')))} |"
            for n, r in refs.items()
        ]
        lines.append("")
    lines += ["## 分镜", "", "| # | 时间 | 画面说明 | 动作说明 | 台词 | 转场 |",
              "|---|---|---|---|---|---|"]
    for s, st in zip(data["shots"], starts):
        kf, mo = s.get("keyframe") or {}, s.get("motion") or {}
        lines.append(
            f"| {md(s['id'])} | {st:.1f} – {st + s['duration']:.1f} | "
            f"{md(_or_unwritten(kf.get('desc')))} | {md(_or_unwritten(mo.get('desc')))} | "
            f"{md(_or_unwritten(s.get('line')))} | {md(s.get('transition', 'cut'))} |"
        )
    lines += ["", "## 给模型的提示词", ""]
    for s in data["shots"]:
        kf, mo = s.get("keyframe") or {}, s.get("motion") or {}
        lines.append(f"### {s['id']}")
        lines.append("")
        lines.append(f"- 画面：{md_inline(_or_unwritten(kf.get('prompt')))}")
        if kf.get("refs"):
            lines.append(f"- 参考图：{', '.join(f'`{r}`' for r in kf['refs'])}")
        lines.append(f"- 运镜：{mo.get('camera', 'static')}")
        lines.append(f"- 动作：{md_inline(_or_unwritten(mo.get('prompt')))}")
        lines.append(f"- 声音：{md_inline(_or_unwritten(mo.get('sound')))}")
        lines.append("")
    (vdir / "storyboard.md").write_text("\n".join(lines), encoding="utf-8")
    if not is_template(vdir):
        write_storyboard_html(vdir, data, ver=ver)


# ---------------------------------------------------------------- versions


_MISSING = object()

SHOT_LABELS = {
    "keyframe.desc": "画面说明",
    "keyframe.prompt": "画面",
    "keyframe.refs": "参考图",
    "motion.desc": "动作说明",
    "motion.camera": "运镜",
    "motion.prompt": "动作",
    "motion.sound": "声音",
    "motion.backend": "后端",
    "line": "台词",
    "duration": "时长",
    "transition": "转场",
    "notes": "备注",
}
_SHOT_RANK = {k: i for i, k in enumerate(SHOT_LABELS)}

GLOBAL_LABELS = {
    "title": "标题",
    "output": "输出",
    "backend": "后端",
    "style": "关键帧风格",
    "video_style": "视频风格",
}
_GLOBAL_RANK = {k: i for i, k in enumerate(GLOBAL_LABELS)}

_WORD_RE = re.compile(r"\s+|[A-Za-z0-9_'-]+|[\u3400-\u9fff]|.", re.DOTALL)


def storyboard(data: dict) -> dict:
    """Deep copy of shots.json with production-pick fields removed, for versioning and diffing."""
    sb = copy.deepcopy(data)
    for r in (sb.get("references") or {}).values():
        if isinstance(r, dict):
            r.pop("image", None)
            r.pop("seed", None)
    for s in sb.get("shots") or []:
        if isinstance(s, dict):
            for key, picks in (("keyframe", ("image", "seed")), ("motion", ("clip", "seed"))):
                part = s.get(key)
                if isinstance(part, dict):
                    for k in picks:
                        part.pop(k, None)
                # `pick` / placeholder auto-pick create "motion": {"clip": …} on shots that had no motion;
                # once the picks are gone an empty or null section is the same as no section.
                if key in s and not s[key]:
                    del s[key]
    return sb


def is_template(vdir: Path) -> bool:
    """True when vdir sits inside `<REPO>/templates/` (templates must not accumulate version history)."""
    return vdir.resolve().is_relative_to((REPO / "templates").resolve())


def version_files(vdir: Path) -> list[tuple[int, Path]]:
    d = vdir / VERSIONS
    if not d.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for p in d.iterdir():
        m = re.fullmatch(r"v(\d+)\.json", p.name)
        if m and p.is_file():
            found.append((int(m.group(1)), p))
    return sorted(found, key=lambda x: x[0])


def load_versions(vdir: Path) -> list[dict]:
    out: list[dict] = []
    for _, p in version_files(vdir):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError as e:
            raise FilmError(f"{p} 不是合法 JSON：{e}") from e
    return out


def parse_version_num(spec: str) -> int:
    m = re.fullmatch(r"v?(\d+)", spec.strip(), re.IGNORECASE)
    if not m or int(m.group(1)) <= 0:
        raise FilmError(f"不认识的版本号：{spec!r}（用 v1、1 这类写法）")
    return int(m.group(1))


def load_version(vdir: Path, num: int) -> dict:
    files = version_files(vdir)
    for n, p in files:
        if n == num:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise FilmError(f"{p} 不是合法 JSON：{e}") from e
    if not files:
        raise FilmError(f"没有版本 v{num}（还没有存过版本）")
    raise FilmError(f"没有版本 v{num}（现有 v{files[0][0]}–v{files[-1][0]}）")


def save_version(vdir: Path, sb: dict, note: str) -> dict:
    files = version_files(vdir)
    n = (files[-1][0] + 1) if files else 1
    rec = {
        "version": n,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": note,
        "storyboard": sb,
    }
    _write_json(vdir / VERSIONS / f"v{n:03d}.json", rec)
    return rec


def short_time(ts: str) -> str:
    return ts.replace("T", " ")[:16]


def global_label(path: str) -> str:
    if path in GLOBAL_LABELS:
        return GLOBAL_LABELS[path]
    if path.startswith("format."):
        return f"画幅 {path[7:]}"
    return path


def shot_label(path: str) -> str:
    return SHOT_LABELS.get(path, path)


def _flatten_nonempty(d: dict, prefix: str) -> list[tuple[str, object]]:
    items: list[tuple[str, object]] = []
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and v:
            items.extend(_flatten_nonempty(v, path))
        else:
            items.append((path, v))
    return items


def _same_val(a: object, b: object) -> bool:
    if a is _MISSING or b is _MISSING:
        return a is b
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def _diff_dicts(da: dict, db: dict, prefix: str = "",
                *, skip: set[str] | None = None) -> list[tuple[str, object, object]]:
    keys: list[str] = []
    seen: set[str] = set()
    for k in (*db.keys(), *da.keys()):
        if k not in seen and (not skip or k not in skip):
            seen.add(k)
            keys.append(k)
    changes: list[tuple[str, object, object]] = []
    for k in keys:
        path = f"{prefix}.{k}" if prefix else k
        va, vb = da.get(k, _MISSING), db.get(k, _MISSING)
        if _same_val(va, vb):
            continue
        if isinstance(va, dict) and isinstance(vb, dict):
            changes.extend(_diff_dicts(va, vb, path))
        elif isinstance(va, dict) and vb is _MISSING and va:
            for sub_p, sub_v in _flatten_nonempty(va, path):
                changes.append((sub_p, sub_v, _MISSING))
        elif va is _MISSING and isinstance(vb, dict) and vb:
            for sub_p, sub_v in _flatten_nonempty(vb, path):
                changes.append((sub_p, _MISSING, sub_v))
        else:
            changes.append((path, va, vb))
    return changes


def diff_storyboards(sb_a: dict, sb_b: dict) -> dict:
    """Structured diff between two storyboards: global + references, per-shot changes, add/del, order."""
    g_raw = _diff_dicts(sb_a, sb_b, skip={"shots", "references"})
    g_raw.sort(key=lambda item: _GLOBAL_RANK.get(item[0], len(_GLOBAL_RANK)))
    global_items: list[tuple[str, str, object, object]] = [
        ("field", global_label(p), va, vb) for p, va, vb in g_raw
    ]

    refs_a, refs_b = sb_a.get("references") or {}, sb_b.get("references") or {}
    if isinstance(refs_a, dict) and isinstance(refs_b, dict):
        for name in refs_b:
            if name not in refs_a:
                prompt = (refs_b[name] or {}).get("prompt", "") if isinstance(refs_b[name], dict) else ""
                global_items.append(("ref_add", f"+ 参考图 {name}", _MISSING, prompt))
            else:
                ra, rb = refs_a[name], refs_b[name]
                if isinstance(ra, dict) and isinstance(rb, dict):
                    for sub_p, va, vb in _diff_dicts(ra, rb):
                        if sub_p == "desc":
                            lbl = f"参考图 {name} 说明"
                        elif sub_p == "prompt":
                            lbl = f"参考图 {name}"
                        else:
                            lbl = f"references.{name}.{sub_p}"
                        global_items.append(("field", lbl, va, vb))
                elif not _same_val(ra, rb):
                    global_items.append(("field", f"references.{name}", ra, rb))
        for name in refs_a:
            if name not in refs_b:
                global_items.append(("ref_del", f"- 参考图 {name}", _MISSING, _MISSING))
    elif not _same_val(sb_a.get("references", _MISSING), sb_b.get("references", _MISSING)):
        global_items.append(("field", "references", sb_a.get("references", _MISSING), sb_b.get("references", _MISSING)))

    list_a, list_b = sb_a.get("shots") or [], sb_b.get("shots") or []
    map_a = {s["id"]: s for s in list_a}
    map_b = {s["id"]: s for s in list_b}
    ids_a = [s["id"] for s in list_a]
    ids_b = [s["id"] for s in list_b]

    modified: list[tuple[str, list[tuple[str, object, object]]]] = []
    added: list[tuple[str, dict]] = []
    for s in list_b:
        sid = s["id"]
        if sid not in map_a:
            added.append((sid, s))
        else:
            ch = _diff_dicts(map_a[sid], s, skip={"id"})
            if ch:
                ch.sort(key=lambda item: _SHOT_RANK.get(item[0], len(_SHOT_RANK)))
                modified.append((sid, ch))
    deleted = [sid for sid in ids_a if sid not in map_b]

    common_a = [sid for sid in ids_a if sid in map_b]
    common_b = [sid for sid in ids_b if sid in map_a]
    reordered = (ids_a, ids_b) if common_a != common_b else None

    return {
        "global": global_items,
        "modified": modified,
        "added": added,
        "deleted": deleted,
        "reordered": reordered,
    }


def _trim_tokens(tokens: list[str], *, at_start: bool, at_end: bool, keep: int = 30) -> str:
    text = "".join(tokens)
    if at_start and at_end:
        return text
    if at_start and len(text) > keep + 10:
        acc: list[str] = []
        n = 0
        for t in reversed(tokens):
            if n >= keep and not t.isspace():
                break
            acc.append(t)
            n += len(t)
        return "…" + "".join(reversed(acc)).lstrip()
    if at_end and len(text) > keep + 10:
        acc = []
        n = 0
        for t in tokens:
            if n >= keep and not t.isspace():
                break
            acc.append(t)
            n += len(t)
        return "".join(acc).rstrip() + "…"
    if not at_start and not at_end and len(text) > keep * 2 + 10:
        left = _trim_tokens(tokens, at_start=False, at_end=True, keep=keep)
        right = _trim_tokens(tokens, at_start=True, at_end=False, keep=keep)
        return f"{left[:-1]} … {right[1:]}"
    return text


def _coalesce_ops(ta: list[str], ops: list[tuple[str, int, int, int, int]]) -> list[tuple[str, int, int, int, int]]:
    merged: list[tuple[str, int, int, int, int]] = []
    i = 0
    while i < len(ops):
        tag, i1, i2, j1, j2 = ops[i]
        if (tag == "equal" and merged and i + 1 < len(ops)
                and merged[-1][0] != "equal" and ops[i + 1][0] != "equal"
                and "".join(ta[i1:i2]).isspace()):
            ptag, pi1, _, pj1, _ = merged[-1]
            ntag, _, ni2, _, nj2 = ops[i + 1]
            merged[-1] = (ptag if ptag == ntag else "replace", pi1, ni2, pj1, nj2)
            i += 2
            continue
        merged.append((tag, i1, i2, j1, j2))
        i += 1
    return merged


def word_diff(old: str, new: str) -> str:
    ta, tb = _WORD_RE.findall(old), _WORD_RE.findall(new)
    ops = _coalesce_ops(ta, difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes())
    out: list[str] = []
    for idx, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            out.append(_trim_tokens(ta[i1:i2], at_start=(idx == 0), at_end=(idx == len(ops) - 1)))
        else:
            a_s, b_s = "".join(ta[i1:i2]), "".join(tb[j1:j2])
            if tag == "replace":
                out.append(f"[-{a_s}-]{{+{b_s}+}}")
            elif tag == "delete":
                out.append(f"[-{a_s}-]")
            elif tag == "insert":
                out.append(f"{{+{b_s}+}}")
    return "".join(out)


def format_val(v: object) -> str:
    if v is _MISSING:
        return "（无）"
    return json.dumps(v, ensure_ascii=False)


def format_change(old: object, new: object) -> str:
    if isinstance(old, str) and isinstance(new, str):
        return word_diff(old, new)
    return f"{format_val(old)} → {format_val(new)}"


def diff_has_changes(d: dict) -> bool:
    return bool(d["global"] or d["modified"] or d["added"] or d["deleted"] or d["reordered"])


def diff_summary(d: dict) -> str:
    parts: list[str] = []
    for sid, ch in d["modified"]:
        labels = list(dict.fromkeys(shot_label(p) for p, _, _ in ch))
        parts.append(f"{sid} {'、'.join(labels)}")
    if d["added"]:
        parts.append(f"新增 {'、'.join(sid for sid, _ in d['added'])}")
    if d["deleted"]:
        parts.append(f"删除 {'、'.join(d['deleted'])}")
    if d["reordered"]:
        parts.append("顺序变了")
    if d["global"]:
        g_labels = list(dict.fromkeys(lbl for _, lbl, _, _ in d["global"]))
        parts.append(f"全局 {'、'.join(g_labels)}")
    return "；".join(parts) if parts else "无改动"


def format_diff(label_a: str, label_b: str, d: dict) -> str:
    if not diff_has_changes(d):
        return "没有差别"
    lines = [f"{label_a} → {label_b}"]
    if d["global"]:
        lines.append("全局")
        for kind, lbl, va, vb in d["global"]:
            if kind == "field":
                lines.append(f"  {lbl}：{format_change(va, vb)}")
            elif kind == "ref_add":
                snippet = str(vb).strip()
                if len(snippet) > 60:
                    snippet = snippet[:60] + "…"
                lines.append(f"  {lbl}：{snippet}" if snippet else f"  {lbl}")
            elif kind == "ref_del":
                lines.append(f"  {lbl}")
    for sid, ch in d["modified"]:
        lines.append(sid)
        for p, va, vb in ch:
            lines.append(f"  {shot_label(p)}：{format_change(va, vb)}")
    for sid, s in d["added"]:
        prompt = ((s.get("keyframe") or {}).get("prompt") or "").strip()
        snippet = (prompt[:60] + "…") if len(prompt) > 60 else prompt
        lines.append(f"+ {sid}（新增）：{snippet}" if snippet else f"+ {sid}（新增）")
    for sid in d["deleted"]:
        lines.append(f"- {sid}（删除）")
    if d["reordered"]:
        ids_a, ids_b = d["reordered"]
        lines.append(f"顺序：{' '.join(ids_a)} → {' '.join(ids_b)}")
    return "\n".join(lines)


def desc_sync_warnings(old_sb: dict, new_sb: dict) -> list[str]:
    """Remind when Chinese desc and English prompt (or motion fields) were not updated together."""
    warns: list[str] = []
    old_refs, new_refs = old_sb.get("references") or {}, new_sb.get("references") or {}
    if isinstance(old_refs, dict) and isinstance(new_refs, dict):
        for name, new_r in new_refs.items():
            old_r = old_refs.get(name)
            if not (isinstance(old_r, dict) and isinstance(new_r, dict)):
                continue
            d_ch = (old_r.get("desc") or "") != (new_r.get("desc") or "")
            p_ch = (old_r.get("prompt") or "") != (new_r.get("prompt") or "")
            if d_ch != p_ch:
                which = "说明" if d_ch else "英文提示词"
                warns.append(f"参考图 {name} 说明和英文提示词（prompt）要一起改，这次只改了{which}")

    old_shots = {s["id"]: s for s in old_sb.get("shots") or []}
    for new_s in new_sb.get("shots") or []:
        sid = new_s["id"]
        old_s = old_shots.get(sid)
        if old_s is None:
            continue
        old_kf, new_kf = old_s.get("keyframe") or {}, new_s.get("keyframe") or {}
        kfd_ch = (old_kf.get("desc") or "") != (new_kf.get("desc") or "")
        kfp_ch = (old_kf.get("prompt") or "") != (new_kf.get("prompt") or "")
        if kfd_ch != kfp_ch:
            which = "画面说明" if kfd_ch else "英文提示词"
            warns.append(f"{sid} 画面说明和英文画面提示词（keyframe.prompt）要一起改，这次只改了{which}")

        old_mo, new_mo = old_s.get("motion") or {}, new_s.get("motion") or {}
        mod_ch = (old_mo.get("desc") or "") != (new_mo.get("desc") or "")
        moe_ch = (
            (old_mo.get("prompt") or "") != (new_mo.get("prompt") or "")
            or (old_mo.get("sound") or "") != (new_mo.get("sound") or "")
            or (old_mo.get("camera") or "static") != (new_mo.get("camera") or "static")
        )
        if mod_ch != moe_ch:
            which = "动作说明" if mod_ch else "英文提示词"
            warns.append(f"{sid} 动作说明和英文动作提示词（motion.prompt / sound / camera）要一起改，这次只改了{which}")
    return warns


def stale_picks(old_sb: dict, new_data: dict) -> list[dict]:
    """Find picks currently on new_data that no longer match new_data's storyboard relative to old_sb."""
    new_sb = storyboard(new_data)
    style_changed = (old_sb.get("style") or "") != (new_sb.get("style") or "")
    format_changed = old_sb.get("format") != new_sb.get("format")
    video_style_changed = (old_sb.get("video_style") or "") != (new_sb.get("video_style") or "")

    old_refs = old_sb.get("references") or {}
    new_refs = new_data.get("references") or {}
    changed_refs: set[str] = set()
    for name, old_r in old_refs.items():
        if name not in new_refs:
            changed_refs.add(name)
        elif ((old_r or {}).get("prompt") or "") != ((new_refs[name] or {}).get("prompt") or ""):
            changed_refs.add(name)

    stale: list[dict] = []

    for name, ref in new_refs.items():
        if name not in old_refs or not isinstance(ref, dict):
            continue
        reasons: list[str] = []
        if style_changed:
            reasons.append("关键帧风格变了")
        if format_changed:
            reasons.append("画幅变了")
        if name in changed_refs:
            reasons.append("参考图提示词变了")
        if reasons and (ref.get("image") or ref.get("seed") is not None):
            stale.append({"kind": "ref", "target": name, "path": ref.get("image"), "reasons": reasons})

    old_shots = {s["id"]: s for s in old_sb.get("shots") or []}
    for s in new_data.get("shots") or []:
        sid = s["id"]
        old_s = old_shots.get(sid)
        if old_s is None:
            continue
        old_kf, new_kf = old_s.get("keyframe") or {}, s.get("keyframe") or {}
        old_mo, new_mo = old_s.get("motion") or {}, s.get("motion") or {}

        kf_reasons: list[str] = []
        if style_changed:
            kf_reasons.append("关键帧风格变了")
        if format_changed:
            kf_reasons.append("画幅变了")
        if (old_kf.get("prompt") or "") != (new_kf.get("prompt") or ""):
            kf_reasons.append("画面变了")
        if (old_kf.get("refs") or []) != (new_kf.get("refs") or []):
            kf_reasons.append("参考图变了")
        bad_refs = [r for r in (new_kf.get("refs") or []) if r in changed_refs]
        if bad_refs:
            kf_reasons.append(f"参考图 {'、'.join(bad_refs)} 变了")

        if kf_reasons and (new_kf.get("image") or new_kf.get("seed") is not None):
            stale.append({"kind": "keyframe", "target": sid, "path": new_kf.get("image"), "reasons": kf_reasons})

        video_reasons: list[str] = list(kf_reasons)
        if video_style_changed:
            video_reasons.append("视频风格变了")
        if shot_backend(old_sb, old_s) != shot_backend(new_sb, s):
            video_reasons.append("后端变了")
        if old_s.get("duration") != s.get("duration"):
            video_reasons.append("时长变了")
        if old_mo.get("camera", "static") != new_mo.get("camera", "static"):
            video_reasons.append("运镜变了")
        if (old_mo.get("prompt") or "") != (new_mo.get("prompt") or ""):
            video_reasons.append("动作变了")
        if (old_mo.get("sound") or "") != (new_mo.get("sound") or ""):
            video_reasons.append("声音变了")

        if video_reasons and (new_mo.get("clip") or new_mo.get("seed") is not None):
            stale.append({"kind": "clip", "target": sid, "path": new_mo.get("clip"),
                          "reasons": list(dict.fromkeys(video_reasons))})

    return stale


def clear_stale_picks(data: dict, stale: list[dict]) -> None:
    refs = data.get("references") or {}
    shots = {s["id"]: s for s in data.get("shots") or []}
    for e in stale:
        if e["kind"] == "ref":
            r = refs.get(e["target"])
            if isinstance(r, dict):
                r.pop("image", None)
                r.pop("seed", None)
        elif e["kind"] == "keyframe":
            kf = (shots.get(e["target"]) or {}).get("keyframe")
            if isinstance(kf, dict):
                kf.pop("image", None)
                kf.pop("seed", None)
        elif e["kind"] == "clip":
            mo = (shots.get(e["target"]) or {}).get("motion")
            if isinstance(mo, dict):
                mo.pop("clip", None)
                mo.pop("seed", None)


def warn_stale_picks(stale: list[dict]) -> None:
    for e in stale:
        if not e["path"]:
            continue
        reason = "、".join(e["reasons"])
        if e["kind"] == "ref":
            print(f"  ⚠ 参考图 {e['target']} {reason}，但还选着按旧分镜出的图片 {e['path']}：重出或重新 pick")
        elif e["kind"] == "keyframe":
            print(f"  ⚠ {e['target']} {reason}，但还选着按旧分镜出的关键帧 {e['path']}：重出或重新 pick")
        elif e["kind"] == "clip":
            print(f"  ⚠ {e['target']} {reason}，但还选着按旧分镜出的视频 {e['path']}：重出或重新 pick")


def print_cleared_picks(stale: list[dict]) -> None:
    shown = [e for e in stale if e["path"]]
    if not shown:
        return
    for e in shown:
        reason = "、".join(e["reasons"])
        if e["kind"] == "ref":
            print(f"  · 清掉参考图 {e['target']} {e['path']}（{reason}）")
        elif e["kind"] == "keyframe":
            print(f"  · 清掉 {e['target']} 关键帧 {e['path']}（{reason}）")
        elif e["kind"] == "clip":
            print(f"  · 清掉 {e['target']} 视频 {e['path']}（{reason}）")
    print("文件还在 assets/，想用回原来的可以 film.py pick")


def _copy_shot_picks(src: dict, dst: dict) -> None:
    src_kf = src.get("keyframe") or {}
    if src_kf.get("image") is not None or src_kf.get("seed") is not None:
        dst_kf = dst.setdefault("keyframe", {})
        if src_kf.get("image") is not None:
            dst_kf["image"] = src_kf["image"]
        if src_kf.get("seed") is not None:
            dst_kf["seed"] = src_kf["seed"]
    src_mo = src.get("motion") or {}
    if src_mo.get("clip") is not None or src_mo.get("seed") is not None:
        dst_mo = dst.setdefault("motion", {})
        if src_mo.get("clip") is not None:
            dst_mo["clip"] = src_mo["clip"]
        if src_mo.get("seed") is not None:
            dst_mo["seed"] = src_mo["seed"]


def _resolve_diff_side(vdir: Path, data: dict, spec: str | None, *, default_latest: bool) -> tuple[str, dict]:
    if spec is None:
        if default_latest:
            vers = load_versions(vdir)
            if not vers:
                raise FilmError("还没有存过版本（先运行 plan --write）")
            return f"v{vers[-1]['version']}", vers[-1]["storyboard"]
        return "当前", storyboard(data)
    if spec.strip().lower() == "now":
        return "当前", storyboard(data)
    rec = load_version(vdir, parse_version_num(spec))
    return f"v{rec['version']}", rec["storyboard"]


def cmd_versions(a) -> int:
    vdir = Path(a.dir)
    data = load(vdir)
    vers = load_versions(vdir)
    if not vers:
        print(f"还没有版本（先运行 film.py plan {a.dir} --write 存第一版）")
        return 0
    w = max(len(str(v["version"])) for v in vers)
    prev: dict | None = None
    for v in vers:
        print(f"v{v['version']:<{w}}  {short_time(v.get('time', ''))}  {v.get('note', '')}")
        if prev is not None:
            d = diff_storyboards(prev["storyboard"], v["storyboard"])
            print(f"  {diff_summary(d)}")
        prev = v
    if storyboard(data) != vers[-1]["storyboard"]:
        print(f"\n当前有未保存的改动（film.py diff {a.dir} 查看）")
    return 0


def cmd_diff(a) -> int:
    vdir = Path(a.dir)
    data = load(vdir)
    label_a, sb_a = _resolve_diff_side(vdir, data, a.a, default_latest=True)
    label_b, sb_b = _resolve_diff_side(vdir, data, a.b, default_latest=False)
    print(format_diff(label_a, label_b, diff_storyboards(sb_a, sb_b)))
    return 0


def cmd_revert(a) -> int:
    vdir = Path(a.dir)
    cur_data = load(vdir)
    cur_sb = storyboard(cur_data)
    ver_num = parse_version_num(a.ver)
    target_rec = load_version(vdir, ver_num)
    target_sb = target_rec["storyboard"]

    want: list[str] | None = None
    if a.shots is not None:
        want = list(dict.fromkeys(x.strip() for x in a.shots.split(",") if x.strip()))
        if not want:
            raise FilmError("--shots 不能为空")
        target_shots = {s["id"]: s for s in target_sb.get("shots") or []}
        missing = [sid for sid in want if sid not in target_shots]
        if missing:
            raise FilmError(f"v{ver_num} 里没有 {', '.join(missing)}")

        new_data = copy.deepcopy(cur_data)
        new_shots = new_data.setdefault("shots", [])
        cur_idx = {s["id"]: i for i, s in enumerate(new_shots)}
        for sid in want:
            if sid in cur_idx:
                rep = copy.deepcopy(target_shots[sid])
                _copy_shot_picks(new_shots[cur_idx[sid]], rep)
                new_shots[cur_idx[sid]] = rep

        target_order = [s["id"] for s in target_sb.get("shots") or []]
        want_set = set(want)
        for pos, sid in enumerate(target_order):
            if sid in want_set and sid not in cur_idx:
                rep = copy.deepcopy(target_shots[sid])
                present = {s["id"]: i for i, s in enumerate(new_shots)}
                insert_at = 0
                for prev_sid in reversed(target_order[:pos]):
                    if prev_sid in present:
                        insert_at = present[prev_sid] + 1
                        break
                new_shots.insert(insert_at, rep)
    else:
        new_data = copy.deepcopy(target_sb)
        for name, ref in (new_data.get("references") or {}).items():
            cur_ref = (cur_data.get("references") or {}).get(name)
            if isinstance(ref, dict) and isinstance(cur_ref, dict):
                if cur_ref.get("image") is not None:
                    ref["image"] = cur_ref["image"]
                if cur_ref.get("seed") is not None:
                    ref["seed"] = cur_ref["seed"]
        cur_shots_by_id = {s["id"]: s for s in cur_data.get("shots") or []}
        for s in new_data.get("shots") or []:
            cur_s = cur_shots_by_id.get(s["id"])
            if cur_s is not None:
                _copy_shot_picks(cur_s, s)

    new_sb = storyboard(new_data)
    if new_sb == cur_sb:
        print("已经是这个样子，没有改动")
        return 0

    stale = stale_picks(cur_sb, new_data)
    clear_stale_picks(new_data, stale)
    require_valid(vdir, new_data)

    vers = load_versions(vdir)
    if vers and cur_sb != vers[-1]["storyboard"]:
        auto_ver = save_version(vdir, cur_sb, "回退前自动保存")
        print(f"当前有未保存的改动，已先自动存为 v{auto_ver['version']}（回退前自动保存）")

    save(vdir, new_data)
    if (a.message or "").strip():
        note = a.message.strip()
    elif want is not None:
        note = f"{'、'.join(want)} 回退到 v{ver_num}"
    else:
        note = f"回退到 v{ver_num}"
    ver = save_version(vdir, new_sb, note)
    write_storyboard(vdir, new_data, ver=ver)

    d = diff_storyboards(cur_sb, new_sb)
    print(f"已存为 v{ver['version']}（{ver['note']}）：{diff_summary(d)}")
    print_cleared_picks(stale)
    print(f"已重写 {vdir / 'storyboard.md'}")
    if not is_template(vdir):
        print("分镜页面：assets/review/storyboard.html")
    return 0


# ---------------------------------------------------------------- keyframes (h3)


def h3_bin() -> str:
    return os.environ.get("H3_BIN", "h3")


H3_RESULT_FIELDS = ("seed", "width", "height", "frames", "fps", "duration", "steps", "turbo", "seconds")


def h3_batch(vdir: Path, a, what: str, jobs: list[dict]) -> tuple[int, Path]:
    """h3 up → every job → h3 down. The down always runs, also on failure or Ctrl-C (unless --keep-vm).

    A job is {"target", "cmd": argv, "log": fields for the run log, "annotate": fields merged into each
    result's .json sidecar}. Results go to prompts/runs/<what>-<time>-<stage>.jsonl, paths relative."""
    if not shutil.which(h3_bin()):
        raise FilmError(f"找不到 {h3_bin()}（见全局 CLAUDE.md 的 h3 说明）")
    log_dir = vdir / "prompts" / "runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"{what}-{time.strftime('%Y%m%d-%H%M%S')}-{a.stage}.jsonl"
    failed = 0
    try:
        if not a.vm_up:
            up = [h3_bin(), "up", "--models", a.models]
            print(f"\n$ {shlex.join(up)}", flush=True)
            if subprocess.run(up, cwd=vdir).returncode != 0:
                raise FilmError("h3 up 失败")
        for j in jobs:
            print(f"\n$ {j['target']}: h3 {j['cmd'][1]} …", flush=True)
            proc = subprocess.run(j["cmd"], cwd=vdir, stdout=subprocess.PIPE, text=True)
            results = []
            for line in proc.stdout.splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event") in ("image", "video") and ev.get("ok"):
                    results.append({"path": rel(vdir, ev["path"]), **{k: ev[k] for k in H3_RESULT_FIELDS if k in ev}})
                    meta = Path(ev.get("meta") or Path(ev["path"]).with_suffix(".json"))
                    if j.get("annotate") and meta.is_file():
                        try:
                            m = json.loads(meta.read_text(encoding="utf-8"))
                        except json.JSONDecodeError:
                            m = {}
                        meta.write_text(json.dumps({**m, **j["annotate"]}, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
                elif ev.get("event") in ("image", "video", "error") and not ev.get("ok", False):
                    print(f"  ✗ {ev.get('error')}")
            failed += proc.returncode != 0
            entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "stage": a.stage, "target": j["target"],
                     **j["log"], "results": results, "exit": proc.returncode}
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            for r in results:
                print(f"  ✓ {r['path']}  seed={r.get('seed')}")
    finally:
        if a.keep_vm:
            print(f"\n⚠ --keep-vm：VM 还开着，按时长扣 CU（约 {H3_CU_PER_HOUR} CU/h）。用完一定运行 {h3_bin()} down")
        else:
            print(f"\n$ {h3_bin()} down", flush=True)
            subprocess.run([h3_bin(), "down"], cwd=vdir)
    return failed, log


def keyframe_jobs(vdir: Path, data: dict, stage: str, shots: list[dict], redo: bool, takes: int | None):
    w, h, fps, aspect = fmt(data)
    mp, n = STAGES[stage]["mp"], takes or STAGES[stage]["n"]
    refs = data.get("references", {}) or {}
    jobs, skipped = [], []

    def has_candidates(out: str) -> bool:
        d = vdir / out
        return d.is_dir() and any(p.suffix.lower() in IMAGE_EXT for p in d.iterdir())

    def needs(image: str | None, out: str, label: str) -> bool:
        """Spend only on what is still missing: a stage with candidates waits for a pick."""
        if redo:
            return True
        if stage == "trial" and image:
            return False
        if stage == "final" and image and "/trial/" not in image:
            return False  # already has its final
        if has_candidates(out):
            skipped.append(f"{label}（{out} 里已有候选，先 pick；要再出加 --redo）")
            return False
        return True

    used_refs = {r for s in shots for r in (s.get("keyframe") or {}).get("refs") or []}
    if stage == "trial":  # references are inputs only: a picked trial is good enough, no 16 MP final
        for name in sorted(used_refs):
            r = refs[name]
            out = f"assets/keyframes/ref-{name}/{stage}"
            if r.get("prompt") and needs(r.get("image"), out, f"ref:{name}"):
                jobs.append({"target": f"ref:{name}", "kind": "gen", "prompt": full_prompt(data, r["prompt"]),
                             "aspect": aspect, "mp": mp, "n": n, "seed": r.get("seed"), "out": out})
    for s in shots:
        kf = s.get("keyframe") or {}
        out = f"assets/keyframes/{s['id']}/{stage}"
        if not needs(kf.get("image"), out, s["id"]):
            continue
        if stage == "final" and not kf.get("image"):
            print(f"  ⚠ {s['id']}: 还没选试稿，直接出定稿（seed {kf.get('seed') or '随机'}）")
        # final: the picked trial's seed keeps the look; trial: an author-set seed, or random on a redo
        seed = kf.get("seed") if stage == "final" or not kf.get("image") else None
        job = {"target": s["id"], "prompt": full_prompt(data, kf["prompt"]), "mp": mp, "n": n,
               "seed": seed, "out": out}
        if kf.get("refs"):
            missing = [r for r in kf["refs"] if not refs[r].get("image")]
            if missing:
                skipped.append(f"{s['id']}（先选参考图 {', '.join(missing)}）")
                continue
            images = [refs[r]["image"] for r in kf["refs"]]
            iw, ih = image_size(vdir / images[0])
            a_, b_ = (float(x) for x in aspect.split(":"))
            if abs(iw / ih - a_ / b_) > 0.03:
                print(f"  ⚠ {s['id']}: <Picture 1> 是 {iw}x{ih}，h3 edit 的输出会跟它同比例，"
                      f"不是 {aspect}；参考图请按片子画幅出")
            job.update(kind="edit", images=images)
        else:
            job.update(kind="gen", aspect=aspect)
        jobs.append(job)
    return jobs, skipped


def h3_cmd(job: dict) -> list[str]:
    if job["kind"] == "gen":
        cmd = [h3_bin(), "gen", job["prompt"], "--aspect", job["aspect"]]
    else:
        cmd = [h3_bin(), "edit", job["prompt"], *job["images"]]
    cmd += ["--mp", str(job["mp"]), "-n", str(job["n"]), "-o", job["out"], "--json"]
    if job.get("seed") is not None:
        cmd += ["--seed", str(job["seed"])]
    return cmd


def cmd_keyframes(a) -> int:
    vdir = Path(a.dir).resolve()
    data = load(vdir)
    require_valid(vdir, data)
    jobs, skipped = keyframe_jobs(vdir, data, a.stage, select(data, a.shots), a.redo, a.takes)
    images = sum(j["n"] for j in jobs)
    minutes = images * MIN_PER_IMAGE[STAGES[a.stage]["mp"]]
    cu = (0 if a.vm_up else H3_BOOT_CU) + minutes * H3_CU_PER_HOUR / 60
    print(f"关键帧 · {a.stage}（{STAGES[a.stage]['mp']} MP）· {len(jobs)} 个任务 · {images} 张")
    for s in skipped:
        print(f"  · 跳过 {s}")
    if not jobs:
        print("没有要出的图。")
        return 0
    print()
    if not a.vm_up:
        print(f"  {h3_bin()} up --models {a.models}")
    for j in jobs:
        print("  " + shlex.join(h3_cmd(j)))
    if not a.keep_vm:
        print(f"  {h3_bin()} down")
    print(f"\n预算：约 {cu:.1f} CU（≈ ${cu * USD_PER_CU:.2f}）"
          f" = {'' if a.vm_up else f'开机 {H3_BOOT_CU} + '}出图约 {minutes:.0f} 分钟 × {H3_CU_PER_HOUR} CU/h（粗估）")
    if a.stage == "final":
        if any(shot_backend(data, s) == "h3-video" for s in data["shots"]):
            print("· 这片子用 h3-video：视频只有 768p，选中的 4 MP 试稿当首帧已经够用，一般不用出 16 MP 定稿。")
        if images < 4:
            print("⚠ 不到 4 张定稿：按 h3 的规则先问用户，是现在开机还是攒成一批再出。")
    if not a.yes:
        print("\n这是 dry run，没有开机。用户确认预算后加 --yes 运行（开机约 11 分钟，放后台跑）。")
        return 0

    failed, log = h3_batch(vdir, a, "keyframes", [
        {"target": j["target"], "cmd": h3_cmd(j),
         "log": {"kind": j["kind"], "prompt": j["prompt"], "mp": j["mp"], "seed": j.get("seed"),
                 "images": j.get("images")}} for j in jobs])
    print(f"\n记录：{rel(vdir, log)}")
    print(f"下一步：film.py review {a.dir} --what keyframes，看图后 film.py pick {a.dir} <镜头> <图片>")
    return 1 if failed else 0


# ---------------------------------------------------------------- pick


def cmd_pick(a) -> int:
    vdir = Path(a.dir).resolve()
    data = load(vdir)
    path = Path(a.path)
    path = path if path.is_absolute() else (Path.cwd() / path)
    if not path.is_file():
        alt = vdir / a.path
        if not alt.is_file():
            raise FilmError(f"找不到文件 {a.path}")
        path = alt
    rp = rel(vdir, path)
    if not (vdir.resolve() in path.resolve().parents):
        raise FilmError("选中的文件要放在片子目录里（一般是 assets/ 下）")
    meta = sidecar(path)
    seed = meta.get("seed")
    ext = path.suffix.lower()
    if a.target.startswith("ref:"):
        name = a.target[4:]
        ref = (data.get("references") or {}).get(name)
        if ref is None:
            raise FilmError(f"references 里没有 {name}")
        if ext not in IMAGE_EXT:
            raise FilmError("参考图要是图片")
        ref["image"] = rp
        if seed is not None:
            ref["seed"] = seed
    else:
        shot = next((s for s in data["shots"] if s["id"] == a.target), None)
        if shot is None:
            raise FilmError(f"没有镜头 {a.target}")
        if ext in IMAGE_EXT:
            kf = shot.setdefault("keyframe", {})
            kf["image"] = rp
            if seed is not None:
                kf["seed"] = seed
            if (shot.get("motion") or {}).get("clip"):
                print("  · 关键帧换了，原来选的视频已清空，要重新出 clips")
                shot["motion"]["clip"] = None
        elif ext in VIDEO_EXT:
            kf_image = (shot.get("keyframe") or {}).get("image")
            if meta.get("keyframe") and kf_image and meta["keyframe"] != kf_image:
                print(f"  ⚠ 这条视频是用 {meta['keyframe']} 出的，现在的关键帧是 {kf_image}")
            mo = shot.setdefault("motion", {})
            mo["clip"] = rp
            if seed is not None:
                mo["seed"] = seed  # an h3 trial's seed, reused by --stage final
        else:
            raise FilmError(f"不认识的文件类型 {ext}")
    save(vdir, data)
    print(f"✓ {a.target} ← {rp}" + (f"（seed {seed}）" if seed is not None else ""))
    return 0


# ---------------------------------------------------------------- clips (image to video)


def camera_exprs(camera: str, frames: int) -> tuple[str, str, str]:
    """zoompan z/x/y expressions; p eases 0→1 over the shot (smoothstep)."""
    p = f"(on/{max(1, frames - 1)})"
    e = f"({p}*{p}*(3-2*{p}))"
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    span_x, span_y = "(iw-iw/zoom)", "(ih-ih/zoom)"
    return {
        "static": ("1", cx, cy),
        "push-in": (f"1+0.12*{e}", cx, cy),
        "pull-out": (f"1.12-0.12*{e}", cx, cy),
        "pan-left": ("1.12", f"{span_x}*(1-{e})", cy),
        "pan-right": ("1.12", f"{span_x}*{e}", cy),
        "tilt-up": ("1.12", cx, f"{span_y}*(1-{e})"),
        "tilt-down": ("1.12", cx, f"{span_y}*{e}"),
    }[camera]


def backend_placeholder(vdir: Path, data: dict, shot: dict, image: Path, out: Path) -> dict:
    """Free stand-in for a video model: the keyframe with an eased camera move (Ken Burns)."""
    w, h, fps, _ = fmt(data)
    frames = round(float(shot["duration"]) * fps)
    camera = (shot.get("motion") or {}).get("camera", "static")
    z, x, y = camera_exprs(camera, frames)
    # Work on a 2x canvas so zoompan's integer offsets don't make the move judder.
    vf = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={fps},format=yuv420p,setsar=1")
    run(["ffmpeg", "-v", "error", "-y", "-i", str(image), "-vf", vf, "-frames:v", str(frames),
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-movflags", "+faststart", str(out)])
    return {"backend": "placeholder", "camera": camera, "frames": frames}


def clips_placeholder(vdir: Path, data: dict, shots: list[dict], redo: bool) -> None:
    todo = [s for s in shots if redo or not (s.get("motion") or {}).get("clip")]
    print(f"图生视频 · placeholder（免费，本机）· {len(todo)} 镜" + (f"：{', '.join(s['id'] for s in todo)}" if todo else ""))
    if not todo:
        print("  都已经选好视频了（要重出加 --redo）")
        return
    for s in todo:
        out_dir = vdir / "assets" / "clips" / s["id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        k = 1 + len(list(out_dir.glob("placeholder-*.mp4")))
        out = out_dir / f"placeholder-{k:02d}.mp4"
        info = backend_placeholder(vdir, data, s, vdir / s["keyframe"]["image"], out)
        out.with_suffix(".json").write_text(json.dumps(
            {**info, "keyframe": s["keyframe"]["image"], "duration": s["duration"]}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"  ✓ {s['id']}: {rel(vdir, out)}")
        if not (s.get("motion") or {}).get("clip"):
            s.setdefault("motion", {})["clip"] = rel(vdir, out)  # deterministic and free: auto-pick
    save(vdir, data)


def clip_jobs(vdir: Path, data: dict, stage: str, shots: list[dict], redo: bool, takes: int | None):
    """h3-video jobs for the shots that still need this stage (same spend-only-on-what's-missing rule as keyframes)."""
    aspect = fmt(data)[3]
    st = CLIP_STAGES[stage]
    jobs, skipped = [], []
    for s in shots:
        kf, mo = s["keyframe"], s.get("motion") or {}
        kind = clip_kind(mo.get("clip"))
        out = f"assets/clips/{s['id']}/h3-{stage}"
        if not redo:
            if kind == "h3-final" or (stage == "trial" and kind == "h3-trial"):
                continue
            fresh = [p for p in candidates(vdir, f"clips/{s['id']}/h3-{stage}", VIDEO_EXT)
                     if sidecar(p).get("keyframe") == kf["image"]]
            if fresh:
                skipped.append(f"{s['id']}（{out} 里已有 {len(fresh)} 条候选，先 pick；要再出加 --redo）")
                continue
        if stage == "final" and kind != "h3-trial":
            print(f"  ⚠ {s['id']}: 还没选 turbo 试稿，直接出定稿（seed {mo.get('seed') or '随机'}）")
        # final: the picked trial's seed; trial: an author-set seed, or random on a redo
        seed = mo.get("seed") if stage == "final" or kind not in ("h3-trial", "h3-final") else None
        secs = min(max(float(s["duration"]), VIDEO_SECONDS[0]), VIDEO_SECONDS[1])
        jobs.append({"target": s["id"], "prompt": video_prompt(data, s), "keyframe": kf["image"],
                     "first": f"assets/clips/{s['id']}/first-frame.jpg", "seconds": secs,
                     "frames": video_frames(secs), "aspect": aspect, "turbo": st["turbo"],
                     "n": takes or st["n"], "seed": seed, "out": out})
    return jobs, skipped


def h3_video_cmd(job: dict) -> list[str]:
    cmd = [h3_bin(), "video", job["prompt"], "--image", job["first"], "--seconds", f"{job['seconds']:g}",
           "--aspect", job["aspect"], "-n", str(job["n"]), "-o", job["out"], "--json"]
    if job["turbo"]:
        cmd.append("--turbo")
    if job.get("seed") is not None:
        cmd += ["--seed", str(job["seed"])]
    return cmd


def make_first_frame(vdir: Path, data: dict, keyframe: str, out: Path) -> None:
    """Keyframe → the video canvas exactly (h3 stretches a mismatched first frame), as a small JPEG:
    colab uploads above ~1 MB are slow, and the model only sees 768p anyway."""
    cw, ch = video_canvas(fmt(data)[3])
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-v", "error", "-y", "-i", str(vdir / keyframe), "-vf",
         f"scale={cw}:{ch}:force_original_aspect_ratio=increase:flags=lanczos,crop={cw}:{ch}",
         "-frames:v", "1", "-q:v", "2", str(out)])


def clips_h3(vdir: Path, data: dict, shots: list[dict], a) -> int:
    jobs, skipped = clip_jobs(vdir, data, a.stage, shots, a.redo, a.takes)
    st = CLIP_STAGES[a.stage]
    cw, ch = video_canvas(fmt(data)[3])
    print(f"图生视频 · h3-video · {a.stage}（{'turbo 8 步' if st['turbo'] else '20 步'}）· {cw}x{ch} · "
          f"{VIDEO_FPS} fps · {len(jobs)} 个任务 · {sum(j['n'] for j in jobs)} 条")
    for s in skipped:
        print(f"  · 跳过 {s}")
    if not jobs:
        print("没有要出的视频。")
        return 0
    cu, minutes = (0.0, 0.0) if a.vm_up else (H3_BOOT_CU, 11.0)
    print()
    if not a.vm_up:
        print(f"  {h3_bin()} up --models {a.models}")
    for j in jobs:
        # 20-step time grows faster than the frame count (attention); ^1.5 keeps long shots from being under-budgeted
        k = (j["frames"] / video_frames(5)) ** 1.5 * j["n"]
        cu, minutes = cu + st["cu"] * k, minutes + st["min"] * k
        print("  " + shlex.join(h3_video_cmd({**j, "prompt": "PROMPT"})))
        print(f"      {j['target']}：镜头 {j['seconds']:g} s → 出 {j['frames']} 帧（{j['frames'] / VIDEO_FPS:.2f} s），"
              f"seed {j['seed'] if j['seed'] is not None else '随机'}，约 {st['cu'] * k:.2f} CU / {st['min'] * k:.0f} 分钟")
        print("      PROMPT =")
        for line in j["prompt"].splitlines():
            print(f"        {line}")
    if not a.keep_vm:
        print(f"  {h3_bin()} down")
    print(f"\n预算：约 {cu:.1f} CU（≈ ${cu * USD_PER_CU:.2f}）= {'' if a.vm_up else f'开机 {H3_BOOT_CU} + '}"
          f"{len(jobs)} 个视频任务（按 A100 实测折算，粗估）；预计用时约 {minutes:.0f} 分钟")
    print("一次开机把这批视频全做完；跑得久，放后台运行。")
    if not a.yes:
        print("\n这是 dry run，没有开机。用户确认预算后加 --yes 运行。")
        return 0

    for j in jobs:  # local and free: do it before paying for a VM
        make_first_frame(vdir, data, j["keyframe"], vdir / j["first"])
    failed, log = h3_batch(vdir, a, "clips", [
        {"target": j["target"], "cmd": h3_video_cmd(j),
         "annotate": {"film_shot": j["target"], "keyframe": j["keyframe"], "stage": a.stage},
         "log": {"kind": "i2v", "prompt": j["prompt"], "keyframe": j["keyframe"], "seconds": j["seconds"],
                 "turbo": j["turbo"], "seed": j["seed"]}} for j in jobs])
    print(f"\n记录：{rel(vdir, log)}")
    print(f"下一步：film.py review {a.dir} --what clips，看完 film.py pick {a.dir} <镜头> <视频>")
    return 1 if failed else 0


def cmd_clips(a) -> int:
    vdir = Path(a.dir).resolve()
    data = load(vdir)
    require_valid(vdir, data)
    groups: dict[str, list[dict]] = {}
    for s in select(data, a.shots):
        if not (s.get("keyframe") or {}).get("image"):
            print(f"  · 跳过 {s['id']}（还没选关键帧）")
            continue
        groups.setdefault(shot_backend(data, s, a.backend), []).append(s)
    if not groups:
        print("没有要出的视频。")
        return 0
    if "placeholder" in groups:
        clips_placeholder(vdir, data, groups["placeholder"], a.redo)
    if "h3-video" in groups:
        return clips_h3(vdir, data, groups["h3-video"], a)
    return 0


# ---------------------------------------------------------------- review


def cmd_review(a) -> int:
    vdir = Path(a.dir).resolve()
    data = load(vdir)
    out_dir = vdir / "assets" / "review"
    made = []
    size = cell(data)
    if a.what in ("board", "all"):
        imgs = [vdir / s["keyframe"]["image"] for s in data["shots"] if (s.get("keyframe") or {}).get("image")]
        if imgs:
            tile(imgs, out_dir / "board.png", cols=min(4, len(imgs)), size=size)
            made.append(("board.png", "分镜板：已选关键帧，按镜头顺序 " + " → ".join(
                s["id"] for s in data["shots"] if (s.get("keyframe") or {}).get("image"))))
    if a.what in ("keyframes", "all"):
        targets = [f"ref-{n}" for n in (data.get("references") or {})] + [s["id"] for s in data["shots"]]
        for t in targets:
            imgs = candidates(vdir, f"keyframes/{t}", IMAGE_EXT)
            if imgs:
                tile(imgs, out_dir / f"keyframes-{t}.png", size=size)
                made.append((f"keyframes-{t}.png", "  ".join(f"{i + 1}={rel(vdir, p)}" for i, p in enumerate(imgs))))
    if a.what in ("clips", "all"):
        for s in data["shots"]:
            clips = candidates(vdir, f"clips/{s['id']}", VIDEO_EXT)
            if not clips:
                continue
            frames = []
            for c in clips:
                dur = float(ffprobe_json(c).get("format", {}).get("duration") or s["duration"])
                for frac in (0.1, 0.5, 0.9):
                    f = out_dir / "tmp" / f"{s['id']}-{c.stem}-{int(frac * 100):02d}.png"
                    f.parent.mkdir(parents=True, exist_ok=True)
                    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * frac:.3f}", "-i", str(c), "-frames:v", "1",
                         str(f)])
                    frames.append(f)
            tile(frames, out_dir / f"clips-{s['id']}.png", cols=3, size=size)
            made.append((f"clips-{s['id']}.png", "每行一条（10% / 50% / 90%）：\n" + "\n".join(
                f"    {i + 1}={rel(vdir, c)}（{audio_levels(c)}）" for i, c in enumerate(clips))))
        shutil.rmtree(out_dir / "tmp", ignore_errors=True)
    if not made:
        print("还没有可审的图 / 视频。")
        return 0
    for name, note in made:
        print(f"{rel(vdir, out_dir / name)}\n  {note}")
    print("\n逐张看：构图对不对分镜、人物前后是否一致、手和脸、画面里的乱码文字、运动是否变形 / 闪烁。")
    return 0


# ---------------------------------------------------------------- assemble


def cmd_assemble(a) -> int:
    vdir = Path(a.dir).resolve()
    data = load(vdir)
    require_valid(vdir, data)
    shots = data["shots"]
    missing = [s["id"] for s in shots if not (s.get("motion") or {}).get("clip")]
    if missing:
        raise FilmError(f"这些镜头还没选视频：{', '.join(missing)}")
    w, h, fps, _ = fmt(data)
    starts, total = timeline(data)
    out = vdir / (a.output or data.get("output") or f"renders/{vdir.name}.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    work = vdir / "assets" / "tmp" / "assemble"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    clips = [vdir / s["motion"]["clip"] for s in shots]
    with_audio = not a.no_audio and any(has_audio(c) for c in clips)
    print(f"拼接 {len(shots)} 镜 → {rel(vdir, out)}（{total:.2f} s，{'有' if with_audio else '无'}音轨）")

    # 1. normalise every clip: exact shot length, frame size, fps, pixel format; silence if needed.
    norm = []
    for s, c in zip(shots, clips):
        d = float(s["duration"])
        n = work / f"{s['id']}.mp4"
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={fps},"
              f"tpad=stop_mode=clone:stop_duration={d},format=yuv420p,setsar=1")
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(c)]
        if with_audio and not has_audio(c):
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        cmd += ["-vf", vf]
        if with_audio:
            if has_audio(c):
                cmd += ["-map", "0:v:0", "-map", "0:a:0", "-af", "aresample=48000,aformat=channel_layouts=stereo,apad"]
            else:
                cmd += ["-map", "0:v:0", "-map", "1:a:0"]
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-an"]
        cmd += ["-t", f"{d:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "16", str(n)]
        run(cmd)
        norm.append(n)

    # 2. chain: crossfade where a shot says "fade", plain concat otherwise.
    inputs: list[str] = []
    for n in norm:
        inputs += ["-i", str(n)]
    parts, v, au, length = [], "[0:v]", "[0:a]", float(shots[0]["duration"])
    for i in range(1, len(norm)):
        prev, d = shots[i - 1], float(shots[i]["duration"])
        nv, na = f"[v{i}]", f"[a{i}]"
        if prev.get("transition") == "fade":
            parts.append(f"{v}[{i}:v]xfade=transition=fade:duration={FADE}:offset={length - FADE:.3f}{nv}")
            if with_audio:
                parts.append(f"{au}[{i}:a]acrossfade=d={FADE}{na}")
            length += d - FADE
        else:
            parts.append(f"{v}[{i}:v]concat=n=2:v=1:a=0{nv}")
            if with_audio:
                parts.append(f"{au}[{i}:a]concat=n=2:v=0:a=1{na}")
            length += d
        v, au = nv, na
    cmd = ["ffmpeg", "-v", "error", "-y", *inputs]
    if parts:
        cmd += ["-filter_complex", ";".join(parts), "-map", v]
        if with_audio:
            cmd += ["-map", au]
    else:
        cmd += ["-map", "0:v"] + (["-map", "0:a"] if with_audio else [])
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(fps),
            "-movflags", "+faststart"]
    if with_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]  # AAC frame padding would outlast the picture
    body = work / "body.mp4"
    run(cmd + [str(body)])

    # 3. subtitles from the shots' lines: an .srt next to the film, optionally muxed as a soft track.
    srt = out.with_suffix(".srt")
    cues = []
    for i, (s, st) in enumerate(zip(shots, starts)):
        if not s.get("line"):
            continue
        end = st + float(s["duration"]) - (FADE if s.get("transition") == "fade" and i < len(shots) - 1 else 0)
        cues.append((st, end, s["line"]))
    if cues:
        def ts(t: float) -> str:
            ms = round(t * 1000)
            return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
        srt.write_text("".join(f"{k + 1}\n{ts(s_)} --> {ts(e_)}\n{t_}\n\n" for k, (s_, e_, t_) in enumerate(cues)),
                       encoding="utf-8")
        print(f"  字幕 {rel(vdir, srt)}（{len(cues)} 条）")
    if cues and a.subs:
        run(["ffmpeg", "-v", "error", "-y", "-i", str(body), "-i", str(srt), "-map", "0", "-map", "1", "-c", "copy",
             "-c:s", "mov_text", "-metadata:s:s:0", "language=chi", str(out)])
    else:
        shutil.move(str(body), out)
    shutil.rmtree(work, ignore_errors=True)
    print(f"✓ {rel(vdir, out)}\n下一步：film.py check {a.dir}")
    return 0


# ---------------------------------------------------------------- check


def cmd_check(a) -> int:
    vdir = Path(a.dir).resolve()
    data = load(vdir)
    w, h, fps, _ = fmt(data)
    starts, total = timeline(data)
    video = Path(a.video) if a.video else vdir / (data.get("output") or f"renders/{vdir.name}.mp4")
    if not video.is_file():
        raise FilmError(f"找不到成片 {video}（先 film.py assemble）")
    mids = [st + float(s["duration"]) / 2 for s, st in zip(data["shots"], starts)]
    cmd = ["bash", str(SELFCHECK), str(video), "--duration", f"{total:.3f}", "--size", f"{w}x{h}",
           "--fps", str(fps), "--at", ",".join(f"{t:.2f}" for t in mids)]
    print("$ " + shlex.join(cmd), flush=True)
    return subprocess.run(cmd).returncode


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="film.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="校验镜头清单、看进度")
    sp.add_argument("dir")
    sp.add_argument("--write", action="store_true", help="存分镜版本并生成 storyboard.md 和分镜页面")
    sp.add_argument("-m", "--message", help="存版时的说明（和 --write 一起用）")
    sp.set_defaults(fn=cmd_plan)

    sp = sub.add_parser("versions", help="列出分镜版本历史和改动摘要")
    sp.add_argument("dir")
    sp.set_defaults(fn=cmd_versions)

    sp = sub.add_parser("diff", help="按镜头对比两版分镜（默认最新版 → 当前）")
    sp.add_argument("dir")
    sp.add_argument("a", nargs="?", help="旧版本（v1、1 或 now，默认最新版）")
    sp.add_argument("b", nargs="?", help="新版本（v2、2 或 now，默认 now）")
    sp.set_defaults(fn=cmd_diff)

    sp = sub.add_parser("revert", help="整版或单镜回退到指定分镜版本")
    sp.add_argument("dir")
    sp.add_argument("ver", help="目标版本（如 v2 或 2）")
    sp.add_argument("--shots", help="只回退这些镜头，逗号分隔")
    sp.add_argument("-m", "--message", help="本次回退的版本说明")
    sp.set_defaults(fn=cmd_revert)

    sp = sub.add_parser("keyframes", help="用 h3 出参考图和关键帧（默认 dry run）")
    sp.add_argument("dir")
    sp.add_argument("--stage", choices=STAGES, default="trial", help="trial=4MP 试稿（默认每镜 2 张），final=16MP 定稿")
    sp.add_argument("--shots", help="只做这些镜头，逗号分隔")
    sp.add_argument("--takes", type=int, help="每个任务出几张（覆盖 stage 默认）")
    sp.add_argument("--redo", action="store_true", help="已选过的也重出")
    sp.add_argument("--yes", action="store_true", help="真的开机（用户确认预算之后）")
    sp.add_argument("--keep-vm", action="store_true", help="跑完不关机（马上还要接着出图时）")
    sp.add_argument("--vm-up", action="store_true", help="VM 已经开着（上一次用了 --keep-vm），不再 up")
    sp.add_argument("--models", choices=("image", "both"), default="image",
                    help="h3 up 装哪套权重；both = 同一次开机接着出视频")
    sp.set_defaults(fn=cmd_keyframes)

    sp = sub.add_parser("pick", help="选定关键帧 / 视频 / 参考图")
    sp.add_argument("dir")
    sp.add_argument("target", help="镜头 id，或 ref:参考图名")
    sp.add_argument("path", help="图片或视频文件")
    sp.set_defaults(fn=cmd_pick)

    sp = sub.add_parser("clips", help="图生视频")
    sp.add_argument("dir")
    sp.add_argument("--backend", choices=BACKENDS, help="默认用镜头的 motion.backend，再是 shots.json 的 backend，"
                                                        "再没有就 placeholder")
    sp.add_argument("--stage", choices=CLIP_STAGES, default="trial",
                    help="h3-video：trial=turbo 8 步试稿（默认），final=20 步定稿（沿用选中试稿的 seed）")
    sp.add_argument("--shots", help="只做这些镜头，逗号分隔")
    sp.add_argument("--takes", type=int, help="h3-video 每镜出几条（默认 1）")
    sp.add_argument("--redo", action="store_true", help="已选过的也重出")
    sp.add_argument("--yes", action="store_true", help="允许付费后端运行（用户确认预算之后）")
    sp.add_argument("--keep-vm", action="store_true", help="h3-video：跑完不关机")
    sp.add_argument("--vm-up", action="store_true", help="h3-video：VM 已经开着且装了视频权重，不再 up")
    sp.add_argument("--models", choices=("video", "both"), default="video", help="h3 up 装哪套权重")
    sp.set_defaults(fn=cmd_clips)

    sp = sub.add_parser("review", help="拼审片图")
    sp.add_argument("dir")
    sp.add_argument("--what", choices=("all", "board", "keyframes", "clips"), default="all")
    sp.set_defaults(fn=cmd_review)

    sp = sub.add_parser("assemble", help="拼接成片")
    sp.add_argument("dir")
    sp.add_argument("-o", "--output", help="相对片子目录，默认 shots.json 的 output")
    sp.add_argument("--subs", action="store_true", help="把台词作为软字幕轨封进 mp4")
    sp.add_argument("--no-audio", action="store_true", help="不要视频自带的声音（h3 视频带环境音）")
    sp.set_defaults(fn=cmd_assemble)

    sp = sub.add_parser("check", help="自检成片")
    sp.add_argument("dir")
    sp.add_argument("video", nargs="?")
    sp.set_defaults(fn=cmd_check)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except FilmError as e:
        print(f"film.py: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"film.py: 命令失败（{e.returncode}）：{shlex.join(str(x) for x in e.cmd)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
