#!/usr/bin/env python3
"""Tests for scripts/library.py (stdlib unittest, no network, no h3).

Run: python3 -m unittest discover -s scripts -p 'test_library.py'
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import library  # noqa: E402

TEMPLATE = SCRIPTS.parent / "templates" / "ai-film-base"
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def make_mp4(path: Path, seconds: float = 2.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=24:d={seconds}",
                    "-pix_fmt", "yuv420p", str(path)], check=True)


class LibraryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.videos = Path(self._tmp.name) / "videos"
        self.videos.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_lib(self) -> tuple[str, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = library.main(["--videos", str(self.videos)])
        self.assertEqual(rc, 0)
        return out.getvalue(), (self.videos / "index.html").read_text(encoding="utf-8")

    def ai_film(self, name: str) -> Path:
        v = self.videos / name
        shutil.copytree(TEMPLATE, v)
        return v

    def by_id(self) -> dict[str, dict]:
        return {i["id"]: i for i in library.collect(self.videos)}

    def test_empty_and_ignored_dirs(self) -> None:
        (self.videos / "notes").mkdir()
        (self.videos / "12-short").mkdir()
        out, page = self.run_lib()
        self.assertIn("还没有片子", page)
        self.assertIn("（0 条）", out)

    def test_brief_only_film(self) -> None:
        v = self.videos / "005-idea"
        v.mkdir()
        (v / "brief.md").write_text("# 005 · brief\n\n| 项 | 内容 |\n|---|---|\n| 主题 | 纸飞机 <b>x</b> |\n"
                                    "| 路线 | 代码动画（Manim） |\n| 时长 | 25 秒 |\n", encoding="utf-8")
        it = self.by_id()["005-idea"]
        self.assertEqual(it["title"], "纸飞机 <b>x</b>")
        self.assertEqual(it["route"], "代码动画（Manim）")  # falls back to the brief's 路线 row
        self.assertEqual(it["stage"], "写分镜")
        self.assertIn("时长 25 秒", it["detail"])
        _, page = self.run_lib()
        self.assertIn("纸飞机 &lt;b&gt;x&lt;/b&gt;", page)
        self.assertNotIn("<b>x</b>", page)

    def test_ai_stages(self) -> None:
        v = self.ai_film("003-ai")
        self.assertEqual(self.by_id()["003-ai"]["stage"], "写分镜")  # no version saved yet
        F = library.film
        with contextlib.redirect_stdout(io.StringIO()):
            F.main(["plan", str(v), "--write", "-m", "初稿"])
        it = self.by_id()["003-ai"]
        self.assertEqual(it["stage"], "分镜")
        self.assertTrue(it["detail"][0].startswith("分镜 v1"))
        self.assertEqual(it["storyboard_page"], "assets/review/storyboard.html")

        data = F.load(v)
        for s in data["shots"]:
            img = v / "assets" / "keyframes" / s["id"] / "trial" / "a.png"
            img.parent.mkdir(parents=True, exist_ok=True)
            img.write_bytes(b"")
            s["keyframe"]["image"] = img.relative_to(v).as_posix()
        data["shots"][0]["motion"]["clip"] = "x/placeholder-01.mp4"
        (v / "x").mkdir()
        (v / "x" / "placeholder-01.mp4").write_bytes(b"")
        F.save(v, data)
        it = self.by_id()["003-ai"]
        self.assertEqual(it["stage"], "出视频")
        self.assertIn("关键帧 3/3", it["detail"])
        self.assertTrue(any(d.startswith("视频 1/3（占位 1）") for d in it["detail"]))

        for s in data["shots"]:  # all placeholder while the film wants h3-video: an animatic
            s["motion"]["clip"] = "x/placeholder-01.mp4"
        F.save(v, data)
        self.assertEqual(self.by_id()["003-ai"]["stage"], "动态分镜")

        for s in data["shots"]:
            p = v / "assets" / "clips" / s["id"] / "h3-final" / "c.mp4"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"")
            s["motion"]["clip"] = p.relative_to(v).as_posix()
        F.save(v, data)
        it = self.by_id()["003-ai"]
        self.assertEqual(it["stage"], "待拼接")
        self.assertNotIn("（有未保存的改动）", it["detail"][0])  # picks are not storyboard edits

        data["shots"][0]["line"] = "改过"
        F.save(v, data)
        self.assertIn("（有未保存的改动）", self.by_id()["003-ai"]["detail"][0])

    @unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg")
    def test_finished_films(self) -> None:
        hf = self.videos / "001-hf"
        (hf / "src").mkdir(parents=True)
        (hf / "src" / "hyperframes.json").write_text("{}", encoding="utf-8")
        (hf / "brief.md").write_text("| 项 | 内容 |\n|---|---|\n| 主题 | 演示片 |\n", encoding="utf-8")
        self.assertEqual(self.by_id()["001-hf"]["stage"], "制作中")
        make_mp4(hf / "renders" / "demo.mp4")
        (hf / "renders" / "frames").mkdir()
        (hf / "renders" / "frames" / "selfcheck.txt").write_text("…\n结果：通过，2 条警告需要人看一眼\n",
                                                                encoding="utf-8")
        it = self.by_id()["001-hf"]
        self.assertEqual((it["route"], it["stage"], it["selfcheck"]), ("代码动画 · HyperFrames", "已成片", "通过"))
        self.assertAlmostEqual(it["final"]["duration"], 2.0, places=1)
        self.assertEqual((it["final"]["width"], it["final"]["height"]), (640, 360))
        self.assertTrue(it["thumb"].startswith("data:image/jpeg;base64,"))

        # AI film: only the shot list's output counts as the finished film; an animatic does not
        ai = self.ai_film("002-ai")
        make_mp4(ai / "renders" / "animatic.mp4")
        it = self.by_id()["002-ai"]
        self.assertNotEqual(it["stage"], "已成片")
        self.assertIsNone(it["final"])
        make_mp4(ai / "renders" / "film.mp4")
        os.utime(ai / "prompts" / "shots.json", (time.time() - 60, time.time() - 60))
        it = self.by_id()["002-ai"]
        self.assertEqual(it["stage"], "已成片")
        self.assertIsNone(it["selfcheck"])
        self.assertFalse(any("重新拼接" in d for d in it["detail"]))
        os.utime(ai / "prompts" / "shots.json", None)  # edited after the film was made
        os.utime(ai / "renders" / "film.mp4", (time.time() - 30, time.time() - 30))
        self.assertTrue(any("重新拼接" in d for d in self.by_id()["002-ai"]["detail"]))

        (hf / "renders" / "frames" / "selfcheck.txt").write_text("结果：不通过（1 项失败）\n", encoding="utf-8")
        out, page = self.run_lib()
        self.assertIn("不通过", page)
        self.assertIn('class="bad"', page)
        self.assertLess(out.index("002-ai"), out.index("001-hf"))  # newest number first
        for bad in (self._tmp.name, "http://", "https://", "/Users/", "/Volumes/"):
            self.assertNotIn(bad, page)
        self.assertIn('href="001-hf/renders/demo.mp4"', page)

    def test_json_output_has_no_thumbnails(self) -> None:
        self.ai_film("002-ai")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            library.main(["--videos", str(self.videos), "--json"])
        payload = json.loads(out.getvalue()[out.getvalue().index("["):])
        self.assertEqual(payload[0]["id"], "002-ai")
        self.assertNotIn("thumb", payload[0])


if __name__ == "__main__":
    unittest.main()
