#!/usr/bin/env python3
"""library.py — 片库总览：扫描 videos/ 下每条片子，生成一页 videos/index.html。

  python3 scripts/library.py [--videos DIR] [--out FILE] [--json]

每条片一张卡片：编号、片名、路线（AI 生成 / HyperFrames / Remotion / Manim）、进度、
分镜版本、缩略图、成片（时长、分辨率、自检结果）和分镜页面链接。两条路线的片子都在里面。
页面自包含（缩略图内嵌、无外部资源），只写相对路径；它含生成的图，不进 git。

只用 Python 标准库，需要 ffmpeg / ffprobe 做缩略图和读成片信息（没有就跳过）。
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "ai"))
import film  # noqa: E402  (shot list helpers: versions, picks, timeline)

VIDEO_DIR_RE = re.compile(r"^\d{3}-[\w.-]+$")
MEDIA_EXT = {".mp4", ".mov", ".webm", ".mkv"}
# stage -> kind (badge colour); order is the AI route's order of work
STAGES = {
    "写分镜": "draft", "分镜": "draft", "出关键帧": "making", "出视频": "making",
    "动态分镜": "making", "待拼接": "making", "制作中": "making", "已成片": "done",
}


# ---------------------------------------------------------------- collect


def brief_rows(vdir: Path) -> dict[str, str]:
    """`| 项 | 内容 |` rows of brief.md (主题, 路线, 时长 …)."""
    p = vdir / "brief.md"
    rows: dict[str, str] = {}
    if not p.is_file():
        return rows
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*$", line)
        if m and not set(m.group(1)) <= set("-: ") and m.group(1) != "项":
            rows[m.group(1)] = m.group(2)
    return rows


def detect_route(vdir: Path, rows: dict[str, str]) -> str:
    src = vdir / "src"
    if (vdir / "prompts" / "shots.json").is_file():
        return "AI 生成"
    if (src / "hyperframes.json").is_file():
        return "代码动画 · HyperFrames"
    pkg = src / "package.json"
    if pkg.is_file() and "remotion" in pkg.read_text(encoding="utf-8", errors="ignore"):
        return "代码动画 · Remotion"
    if src.is_dir() and any("manim" in p.read_text(encoding="utf-8", errors="ignore")
                            for p in list(src.rglob("*.py"))[:50]):
        return "Manim"
    return rows.get("路线") or "未定"


def probe(path: Path) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, check=True).stdout
        d = json.loads(out)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return {}
    s = (d.get("streams") or [{}])[0]
    fps = s.get("r_frame_rate", "0/1").split("/")
    info = {"duration": float(d.get("format", {}).get("duration") or 0)}
    if s.get("width"):
        info.update(width=int(s["width"]), height=int(s["height"]))
    if len(fps) == 2 and float(fps[1] or 0):
        info["fps"] = round(float(fps[0]) / float(fps[1]), 2)
    return info


def video_thumb(path: Path, at: float) -> str | None:
    """One frame of a video as a width-480 JPEG data URI."""
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, at):.2f}", "-i", str(path),
                            "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", "-f", "image2pipe",
                            "-c:v", "mjpeg", "pipe:1"], capture_output=True, check=False)
    except OSError:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(r.stdout).decode("ascii")


def selfcheck_result(vdir: Path) -> str | None:
    """「通过」「不通过」 from renders/frames/selfcheck.txt, None when selfcheck never ran."""
    p = vdir / "renders" / "frames" / "selfcheck.txt"
    if not p.is_file():
        return None
    m = re.search(r"结果：(\S+?)(?:[，（,(]|$)", p.read_text(encoding="utf-8", errors="ignore"), re.M)
    return m.group(1) if m else "读不到"


def ai_progress(vdir: Path, data: dict) -> tuple[str, list[str]]:
    """Stage and detail lines for an AI-route film, from its shot list and versions."""
    shots = data.get("shots") or []
    n = len(shots)
    _, total = film.timeline(data) if shots and all("duration" in s for s in shots) else ([], 0)
    lines = [f"{n} 镜 · 总长 {total:.1f} s"]
    vers = film.load_versions(vdir)
    if vers:
        v = vers[-1]
        dirty = film.storyboard(data) != v["storyboard"]
        lines.insert(0, f"分镜 v{v['version']} · {film.short_time(v.get('time', ''))} · {v.get('note', '')}"
                        + ("（有未保存的改动）" if dirty else ""))
    kf_done = [s for s in shots if (s.get("keyframe") or {}).get("image")]
    kinds = [film.clip_kind((s.get("motion") or {}).get("clip")) for s in shots]
    picked = [k for k in kinds if k]
    lines.append(f"关键帧 {len(kf_done)}/{n}")
    if picked:
        count = {}
        for k in picked:
            label = {"placeholder": "占位", "h3-trial": "h3 试稿", "h3-final": "h3 定稿"}.get(k, "其他")
            count[label] = count.get(label, 0) + 1
        lines.append(f"视频 {len(picked)}/{n}（" + "，".join(f"{k} {v}" for k, v in count.items()) + "）")
    else:
        lines.append(f"视频 0/{n}")
    wants_video = any(film.shot_backend(data, s) == "h3-video" for s in shots)
    if not vers:
        stage = "写分镜"
    elif len(kf_done) < n:
        stage = "出关键帧" if kf_done or film.candidates(vdir, "keyframes", film.IMAGE_EXT) else "分镜"
    elif len(picked) < n:
        stage = "出视频"
    elif wants_video and "placeholder" in kinds:
        stage = "动态分镜"
    else:
        stage = "待拼接"
    return stage, lines


def newest_mtime(vdir: Path) -> float:
    keys = [vdir / "brief.md", vdir / "storyboard.md", vdir / "prompts" / "shots.json"]
    keys += list((vdir / "prompts" / "versions").glob("v*.json"))
    keys += [p for p in (vdir / "renders").glob("*") if p.suffix.lower() in MEDIA_EXT]
    src = vdir / "src"
    if src.is_dir():
        keys += [p for p in src.rglob("*") if p.is_file() and "node_modules" not in p.parts
                 and ".hyperframes" not in p.parts][:500]
    return max((p.stat().st_mtime for p in keys if p.is_file()), default=vdir.stat().st_mtime)


def collect_one(vdir: Path) -> dict:
    rows = brief_rows(vdir)
    route = detect_route(vdir, rows)
    item: dict = {"id": vdir.name, "num": vdir.name[:3], "route": route, "detail": [],
                  "renders": [], "final": None, "selfcheck": selfcheck_result(vdir), "thumb": None,
                  "storyboard_page": None, "storyboard_md": (vdir / "storyboard.md").is_file()}
    data = None
    if route == "AI 生成":
        try:
            data = film.load(vdir)
        except film.FilmError as e:
            item["detail"].append(f"镜头清单读不出：{e}")
    title = (data or {}).get("title") or rows.get("主题") or vdir.name[4:]
    item["title"] = str(title)

    for p in sorted((vdir / "renders").glob("*")):
        if p.suffix.lower() in MEDIA_EXT and p.is_file():
            item["renders"].append({"path": p.relative_to(vdir).as_posix(), "mtime": p.stat().st_mtime,
                                    "mb": round(p.stat().st_size / 1e6, 1), **probe(p)})
    if data is not None:  # AI route: the shot list names the film; other renders (an animatic) are drafts
        out = data.get("output") or f"renders/{vdir.name}.mp4"
        item["final"] = next((r for r in item["renders"] if r["path"] == out), None)
    elif item["renders"]:
        item["final"] = max(item["renders"], key=lambda r: r["mtime"])

    if data is not None:
        stage, lines = ai_progress(vdir, data)
        item["detail"] = lines + item["detail"]
    else:
        stage = "制作中" if (vdir / "src").is_dir() or item["storyboard_md"] else "写分镜"
        if rows.get("时长"):
            item["detail"].append(f"时长 {rows['时长']}")
    if item["final"]:
        if data is not None:
            if stage == "动态分镜":
                item["detail"].append("⚠ 成片里用的是占位视频（动态分镜）")
            if (vdir / film.SHOTS).stat().st_mtime > item["final"]["mtime"]:
                item["detail"].append("⚠ 镜头清单在成片之后改过，可能要重新拼接（film.py assemble）")
        stage = "已成片"
    item["stage"] = stage

    page = vdir / "assets" / "review" / "storyboard.html"
    if page.is_file():
        item["storyboard_page"] = page.relative_to(vdir).as_posix()

    # thumbnail: the finished film, else the first picked keyframe, else the newest render
    f = item["final"]
    if f:
        item["thumb"] = video_thumb(vdir / f["path"], (f.get("duration") or 0) / 3)
    if not item["thumb"] and data is not None:
        img = next(((s.get("keyframe") or {}).get("image") for s in data.get("shots") or []
                    if (s.get("keyframe") or {}).get("image")), None)
        if img:
            item["thumb"] = film.make_thumbnail_data_uri(vdir / img)
    if not item["thumb"] and item["renders"]:
        r = max(item["renders"], key=lambda r: r["mtime"])
        item["thumb"] = video_thumb(vdir / r["path"], (r.get("duration") or 0) / 3)
    item["updated"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(newest_mtime(vdir)))
    return item


def collect(videos: Path) -> list[dict]:
    dirs = [d for d in videos.iterdir() if d.is_dir() and VIDEO_DIR_RE.match(d.name)] if videos.is_dir() else []
    return [collect_one(d) for d in sorted(dirs, key=lambda d: d.name, reverse=True)]


# ---------------------------------------------------------------- render


CSS = """\
:root {
  --bg: #f8f9fa; --fg: #18181b; --muted: #52525b; --card: #ffffff; --subtle: #f1f3f5;
  --border: #e4e4e7; --thumb-bg: #eceef1;
  --draft-bg: #f4f4f5; --draft-fg: #3f3f46;
  --making-bg: #fffbeb; --making-fg: #92400e; --making-border: #d97706;
  --done-bg: #ecfdf5; --done-fg: #065f46; --done-border: #059669;
  --bad-fg: #b91c1c;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111214; --fg: #f4f4f5; --muted: #a1a1aa; --card: #1a1c20; --subtle: #23262c;
    --border: #2e323a; --thumb-bg: #141619;
    --draft-bg: #272a31; --draft-fg: #d4d4d8;
    --making-bg: rgba(217, 119, 6, 0.16); --making-fg: #fcd34d; --making-border: #f59e0b;
    --done-bg: rgba(16, 185, 129, 0.16); --done-fg: #6ee7b7; --done-border: #10b981;
    --bad-fg: #fca5a5;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
  font-family: -apple-system, "PingFang SC", "Hiragino Sans GB", sans-serif; font-size: 14px; line-height: 1.55;
}
.page { max-width: 1200px; margin: 0 auto; }
h1 { margin: 0 0 4px; font-size: 22px; }
.meta { color: var(--muted); font-size: 13px; margin: 0 0 16px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr)); gap: 16px; }
.card {
  min-width: 0; background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  padding: 14px; display: flex; flex-direction: column; gap: 10px;
}
.card.done { border-color: var(--done-border); }
.head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; }
.num { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-weight: 700; color: var(--muted); }
.title { font-weight: 700; font-size: 16px; overflow-wrap: anywhere; }
.pills { display: flex; flex-wrap: wrap; gap: 6px; }
.pill { font-size: 12px; padding: 1px 8px; border-radius: 999px; background: var(--draft-bg); color: var(--draft-fg); }
.stage.making { background: var(--making-bg); color: var(--making-fg); }
.stage.done { background: var(--done-bg); color: var(--done-fg); }
.thumb {
  aspect-ratio: 16 / 9; width: 100%; border-radius: 8px; background: var(--thumb-bg); overflow: hidden;
  display: flex; align-items: center; justify-content: center; color: var(--muted); font-size: 13px;
}
.thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
ul { margin: 0; padding-left: 18px; }
li { overflow-wrap: anywhere; }
.film { font-size: 13px; background: var(--subtle); border-radius: 8px; padding: 8px 10px; overflow-wrap: anywhere; }
.film a { color: inherit; }
.film .bad { color: var(--bad-fg); font-weight: 600; }
.links { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 13px; }
.links a { color: inherit; }
.foot { color: var(--muted); font-size: 12px; }
.empty { color: var(--muted); padding: 24px 0; }
"""


def fmt_duration(sec: float) -> str:
    sec = round(sec, 1)
    return f"{int(sec // 60)}:{sec % 60:04.1f}" if sec >= 60 else f"{sec:.1f} s"


def render(items: list[dict], out: Path, videos: Path) -> str:
    esc = html.escape
    done = sum(1 for i in items if i["stage"] == "已成片")

    def href(item: dict, p: str) -> str:
        """Link relative to the page, so it works wherever the repo lives (never an absolute path)."""
        target = (videos / item["id"] / p).resolve()
        try:
            return esc(Path(target).relative_to(out.parent.resolve()).as_posix(), quote=True)
        except ValueError:
            return esc(f"{item['id']}/{p}", quote=True)

    cards = []
    for it in items:
        kind = STAGES.get(it["stage"], "making")
        thumb = (f'<img src="{it["thumb"]}" alt="{esc(it["title"])}">' if it["thumb"]
                 else "还没有画面")
        detail = "".join(f"<li>{esc(x)}</li>" for x in it["detail"])
        film_html = ""
        if it["final"]:
            f = it["final"]
            spec = " · ".join(x for x in (
                fmt_duration(f["duration"]) if f.get("duration") else "",
                f"{f['width']}×{f['height']}" if f.get("width") else "",
                f"{f['fps']:g} fps" if f.get("fps") else "", f"{f['mb']} MB") if x)
            sc = it["selfcheck"]
            sc_html = ("自检：没跑" if sc is None else
                       f'自检：<span class="{"" if sc == "通过" else "bad"}">{esc(sc)}</span>')
            film_html = (f'<div class="film">成片 <a href="{href(it, f["path"])}">{esc(f["path"])}</a><br>'
                         f"{esc(spec)}<br>{sc_html}</div>")
        elif it["renders"]:
            r = max(it["renders"], key=lambda r: r["mtime"])
            film_html = f'<div class="film">还没成片；最新的渲染：<a href="{href(it, r["path"])}">{esc(r["path"])}</a></div>'
        links = []
        if it["storyboard_page"]:
            links.append(f'<a href="{href(it, it["storyboard_page"])}">分镜页面</a>')
        if it["storyboard_md"]:
            links.append(f'<a href="{href(it, "storyboard.md")}">storyboard.md</a>')
        links.append(f'<a href="{href(it, "brief.md")}">brief.md</a>')
        cards.append(
            f'<section class="card {kind}" data-video="{esc(it["id"], quote=True)}">'
            f'<div class="head"><span class="num">{esc(it["num"])}</span>'
            f'<span class="title">{esc(it["title"])}</span></div>'
            f'<div class="pills"><span class="pill route">{esc(it["route"])}</span>'
            f'<span class="pill stage {kind}">{esc(it["stage"])}</span></div>'
            f'<div class="thumb">{thumb}</div>'
            + (f"<ul>{detail}</ul>" if detail else "") + film_html
            + f'<div class="links">{"".join(links)}</div>'
            f'<div class="foot">{esc(it["id"])} · 更新于 {esc(it["updated"])}</div></section>')
    body = "".join(cards) if cards else '<p class="empty">videos/ 下还没有片子。</p>'
    return ("<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>片库</title><style>\n{CSS}</style></head><body><div class=\"page\">"
            f"<h1>片库</h1><p class=\"meta\">{len(items)} 条 · 已成片 {done} · 制作中 {len(items) - done} · "
            f"生成于 {time.strftime('%Y-%m-%d %H:%M')}</p><div class=\"grid\">{body}</div></div></body></html>\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="library.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--videos", default=str(REPO / "videos"), help="片子目录（默认仓库的 videos/）")
    p.add_argument("--out", help="输出页面（默认 <videos>/index.html）")
    p.add_argument("--json", action="store_true", help="另外把收集到的信息以 JSON 打印出来（不含缩略图）")
    a = p.parse_args(argv)
    videos = Path(a.videos)
    out = Path(a.out) if a.out else videos / "index.html"
    items = collect(videos)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(items, out, videos), encoding="utf-8")
    for it in items:
        print(f"{it['id']:<24} {it['route']:<16} {it['stage']}")
    try:
        shown = out.resolve().relative_to(REPO).as_posix()
    except ValueError:
        shown = out.name
    print(f"\n片库：{shown}（{len(items)} 条）")
    if a.json:
        print(json.dumps([{k: v for k, v in it.items() if k != "thumb"} for it in items],
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
