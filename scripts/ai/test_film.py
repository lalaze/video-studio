#!/usr/bin/env python3
"""Unit tests for storyboard versioning in scripts/ai/film.py (stdlib only, no network, no h3)."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AI_DIR = Path(__file__).resolve().parent
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

import film  # noqa: E402

TEMPLATE_SHOTS = Path(__file__).resolve().parents[2] / "templates" / "ai-film-base" / "prompts" / "shots.json"


class FilmVersionTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vdir = self.root / "001-test"
        (self.vdir / "prompts").mkdir(parents=True)
        shutil.copy2(TEMPLATE_SHOTS, self.vdir / "prompts" / "shots.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = film.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def read_shots(self, vdir: Path | None = None) -> dict:
        return json.loads(((vdir or self.vdir) / "prompts" / "shots.json").read_text(encoding="utf-8"))

    def write_shots(self, data: dict, vdir: Path | None = None) -> None:
        ((vdir or self.vdir) / "prompts" / "shots.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def touch_asset(self, rel_path: str, vdir: Path | None = None) -> str:
        p = (vdir or self.vdir) / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
        return rel_path

    def pick_all_assets(self, data: dict) -> None:
        """Attach dummy existing image/clip/seed picks to keeper and all shots in data."""
        for name, ref in (data.get("references") or {}).items():
            ref["image"] = self.touch_asset(f"assets/keyframes/ref-{name}/trial/ref.png")
            ref["seed"] = 100
        for i, s in enumerate(data["shots"], start=1):
            s["keyframe"]["image"] = self.touch_asset(f"assets/keyframes/{s['id']}/trial/kf.png")
            s["keyframe"]["seed"] = 200 + i
            s["motion"]["clip"] = self.touch_asset(f"assets/clips/{s['id']}/h3-trial/clip.mp4")
            s["motion"]["seed"] = 300 + i

    def version_paths(self, vdir: Path | None = None) -> list[Path]:
        d = (vdir or self.vdir) / "prompts" / "versions"
        return sorted(d.glob("v*.json")) if d.is_dir() else []


class TestPlanAndVersions(FilmVersionTestBase):
    def test_first_write_idempotency_picks_ignored_and_v2(self) -> None:
        # plan without --write before v1
        rc, out, err = self.run_cli("plan", str(self.vdir))
        self.assertEqual(rc, 0, err)
        self.assertIn("版本：还没有（plan --write 存第一版）", out)

        # First plan --write saves v1 with default note "初稿"
        rc, out, err = self.run_cli("plan", str(self.vdir), "--write")
        self.assertEqual(rc, 0, err)
        self.assertIn("已存为 v1（初稿）", out)
        vfiles = self.version_paths()
        self.assertEqual([p.name for p in vfiles], ["v001.json"])

        v1 = json.loads(vfiles[0].read_text(encoding="utf-8"))
        self.assertEqual(v1["version"], 1)
        self.assertEqual(v1["note"], "初稿")
        self.assertRegex(v1["time"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

        # storyboard.md has version header
        sb_md = (self.vdir / "storyboard.md").read_text(encoding="utf-8")
        self.assertIn(f"版本 v1 · {v1['time'].replace('T', ' ')[:16]} · 初稿", sb_md)

        # plan without --write shows v1 clean
        rc, out, err = self.run_cli("plan", str(self.vdir))
        self.assertEqual(rc, 0, err)
        self.assertIn("版本：v1\n", out)
        self.assertNotIn("未保存", out)

        # Running plan --write again with no changes does not save a new version
        rc, out, err = self.run_cli("plan", str(self.vdir), "--write")
        self.assertEqual(rc, 0, err)
        self.assertIn("分镜和 v1 一样，没存新版本", out)
        self.assertEqual(len(self.version_paths()), 1)

        # Changing only pick fields (image/seed/clip) does not save a new version
        data = self.read_shots()
        self.pick_all_assets(data)
        self.write_shots(data)
        rc, out, err = self.run_cli("plan", str(self.vdir), "--write")
        self.assertEqual(rc, 0, err)
        self.assertIn("分镜和 v1 一样，没存新版本", out)
        self.assertEqual(len(self.version_paths()), 1)

        # Changing storyboard content saves v2; no pick fields or absolute paths in version file
        data["shots"][1]["line"] = "他每天天未亮就上楼。"
        self.write_shots(data)

        rc, out, err = self.run_cli("plan", str(self.vdir))
        self.assertEqual(rc, 0, err)
        self.assertIn("版本：v1（有未保存的改动 → plan --write -m 说明）", out)

        rc, out, err = self.run_cli("plan", str(self.vdir), "--write", "-m", "改 s02 台词")
        self.assertEqual(rc, 0, err)
        self.assertIn("已存为 v2（改 s02 台词）：s02 台词", out)
        self.assertEqual([p.name for p in self.version_paths()], ["v001.json", "v002.json"])

        raw_v2 = (self.vdir / "prompts" / "versions" / "v002.json").read_text(encoding="utf-8")
        v2 = json.loads(raw_v2)
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["note"], "改 s02 台词")
        # Ensure pick fields are completely stripped
        for r in v2["storyboard"]["references"].values():
            self.assertNotIn("image", r)
            self.assertNotIn("seed", r)
        for s in v2["storyboard"]["shots"]:
            self.assertNotIn("image", s["keyframe"])
            self.assertNotIn("seed", s["keyframe"])
            self.assertNotIn("clip", s["motion"])
            self.assertNotIn("seed", s["motion"])
        # Ensure no absolute path leaked into version file
        self.assertNotIn(str(self.root), raw_v2)
        self.assertNotIn(str(self.root.resolve()), raw_v2)
        self.assertNotIn("/Volumes/", raw_v2)
        self.assertNotIn("/Users/", raw_v2)

    def test_pick_on_shot_without_motion_does_not_save_version(self) -> None:
        # Placeholder shots may omit "motion"; pick / auto-pick then create "motion": {"clip": …}.
        data = self.read_shots()
        data["backend"] = "placeholder"
        for s in data["shots"]:
            s.pop("motion", None)
        self.write_shots(data)
        rc, out, err = self.run_cli("plan", str(self.vdir), "--write")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.version_paths()), 1)

        clip = self.touch_asset("assets/clips/s01/placeholder-01.mp4")
        rc, out, err = self.run_cli("pick", str(self.vdir), "s01", str(self.vdir / clip))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read_shots()["shots"][0]["motion"], {"clip": clip})

        rc, out, err = self.run_cli("plan", str(self.vdir))
        self.assertEqual(rc, 0, err)
        self.assertNotIn("未保存的改动", out)
        rc, out, err = self.run_cli("plan", str(self.vdir), "--write")
        self.assertEqual(rc, 0, err)
        self.assertIn("分镜和 v1 一样，没存新版本", out)
        self.assertEqual(len(self.version_paths()), 1)

        # A full revert to v1 keeps the pick and does not leave an empty "motion" in the saved version.
        data = self.read_shots()
        data["shots"][1]["line"] = "改一句台词"
        self.write_shots(data)
        rc, out, err = self.run_cli("plan", str(self.vdir), "--write", "-m", "改台词")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cli("revert", str(self.vdir), "v1")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read_shots()["shots"][0]["motion"], {"clip": clip})
        v1, v3 = (json.loads(p.read_text(encoding="utf-8")) for p in (self.version_paths()[0], self.version_paths()[-1]))
        self.assertEqual(v3["storyboard"], v1["storyboard"])
        self.assertNotIn("motion", v3["storyboard"]["shots"][0])

    def test_message_without_write_errors(self) -> None:
        rc, out, err = self.run_cli("plan", str(self.vdir), "-m", "说明")
        self.assertEqual(rc, 1)
        self.assertIn("-m 要和 --write 一起用", err)
        self.assertEqual(self.version_paths(), [])

    def test_template_dir_skips_versions(self) -> None:
        fake_repo = self.root / "fake-repo"
        tpl_dir = fake_repo / "templates" / "ai-film-base"
        (tpl_dir / "prompts").mkdir(parents=True)
        shutil.copy2(TEMPLATE_SHOTS, tpl_dir / "prompts" / "shots.json")

        with mock.patch.object(film, "REPO", fake_repo):
            rc, out, err = self.run_cli("plan", str(tpl_dir), "--write")
            self.assertEqual(rc, 0, err)
            self.assertIn("模板目录不存版本", out)
            self.assertFalse((tpl_dir / "prompts" / "versions").exists())
            sb_md = (tpl_dir / "storyboard.md").read_text(encoding="utf-8")
            self.assertNotIn("版本 v", sb_md)

            rc, out, err = self.run_cli("plan", str(tpl_dir))
            self.assertEqual(rc, 0, err)
            self.assertIn("模板目录不存版本", out)

    def test_plan_write_stale_pick_warning_does_not_modify_shots(self) -> None:
        data = self.read_shots()
        self.pick_all_assets(data)
        self.write_shots(data)
        rc, _, err = self.run_cli("plan", str(self.vdir), "--write", "-m", "初稿")
        self.assertEqual(rc, 0, err)

        # Modify s02 keyframe.prompt and s03 motion.prompt while keeping old picks
        data = self.read_shots()
        data["shots"][1]["keyframe"]["prompt"] = data["shots"][1]["keyframe"]["prompt"].replace("brass oil lantern", "red storm lantern")
        data["shots"][2]["motion"]["prompt"] = data["shots"][2]["motion"]["prompt"].replace("slow speed", "fast speed")
        before = copy.deepcopy(data)
        self.write_shots(data)

        rc, out, err = self.run_cli("plan", str(self.vdir), "--write", "-m", "改 s02 画面和 s03 动作")
        self.assertEqual(rc, 0, err)
        self.assertIn("⚠ s02 画面变了，但还选着按旧分镜出的关键帧 assets/keyframes/s02/trial/kf.png", out)
        self.assertIn("⚠ s02 画面变了，但还选着按旧分镜出的视频 assets/clips/s02/h3-trial/clip.mp4", out)
        self.assertIn("⚠ s03 动作变了，但还选着按旧分镜出的视频 assets/clips/s03/h3-trial/clip.mp4", out)
        self.assertNotIn("⚠ s03 动作变了，但还选着按旧分镜出的关键帧", out)
        self.assertNotIn("⚠ s01", out)

        # shots.json must remain untouched (picks NOT cleared by plan --write)
        after = self.read_shots()
        self.assertEqual(after, before)


class TestVersionsAndDiff(FilmVersionTestBase):
    def test_versions_list_and_diff_all_modes(self) -> None:
        # Before any version
        rc, out, err = self.run_cli("versions", str(self.vdir))
        self.assertEqual(rc, 0, err)
        self.assertIn("plan", out)
        self.assertIn("--write", out)

        # v1
        self.run_cli("plan", str(self.vdir), "--write", "-m", "初稿")

        # v2: edit s02 prompt & duration, edit global video_style, add s04, delete s03, reorder not triggered yet
        data = self.read_shots()
        data["video_style"] = data["video_style"].replace("warm amber", "cool silver")
        data["shots"][1]["keyframe"]["prompt"] = data["shots"][1]["keyframe"]["prompt"].replace("brass oil lantern", "silver lantern")
        data["shots"][1]["duration"] = 6
        data["shots"][1]["line"] = "他每天清晨上楼。"
        s04 = copy.deepcopy(data["shots"][2])
        s04["id"] = "s04"
        s04["keyframe"]["prompt"] = "Sunrise over the quiet ocean seen from the top balcony of the lighthouse"
        data["shots"] = [data["shots"][0], data["shots"][1], s04]
        self.write_shots(data)
        self.run_cli("plan", str(self.vdir), "--write", "-m", "调整中段和结尾")

        # Unsaved workspace change: swap s01 and s02 order
        data["shots"] = [data["shots"][1], data["shots"][0], data["shots"][2]]
        self.write_shots(data)

        # versions shows v1, v2 + summary + unsaved hint
        rc, out, err = self.run_cli("versions", str(self.vdir))
        self.assertEqual(rc, 0, err)
        self.assertIn("v1", out)
        self.assertIn("初稿", out)
        self.assertIn("v2", out)
        self.assertIn("调整中段和结尾", out)
        self.assertIn("s02 画面、台词、时长", out)
        self.assertIn("新增 s04", out)
        self.assertIn("删除 s03", out)
        self.assertIn("全局 视频风格", out)
        self.assertIn("当前有未保存的改动", out)

        # diff default (latest -> now): only order changed
        rc, out, err = self.run_cli("diff", str(self.vdir))
        self.assertEqual(rc, 0, err)
        self.assertIn("v2 → 当前", out)
        self.assertIn("顺序：s01 s02 s04 → s02 s01 s04", out)

        # diff v1 2 (both version styles)
        rc, out, err = self.run_cli("diff", str(self.vdir), "v1", "2")
        self.assertEqual(rc, 0, err)
        self.assertIn("v1 → v2", out)
        self.assertIn("全局", out)
        self.assertIn("视频风格：", out)
        self.assertIn("[-warm amber-]{+cool silver+}", out)
        self.assertIn("s02", out)
        self.assertIn("画面：", out)
        self.assertIn("[-brass oil-]{+silver+}", out)
        self.assertIn("时长：4 → 6", out)
        self.assertIn("+ s04（新增）：Sunrise over the quiet ocean", out)
        self.assertIn("- s03（删除）", out)

        # diff with explicit now
        rc, out, err = self.run_cli("diff", str(self.vdir), "1", "now")
        self.assertEqual(rc, 0, err)
        self.assertIn("v1 → 当前", out)
        self.assertIn("顺序：s01 s02 s03 → s02 s01 s04", out)

        # diff same version -> 没有差别
        rc, out, err = self.run_cli("diff", str(self.vdir), "v2", "v2")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "没有差别")

        # diff non-existent version -> FilmError (rc=1)
        rc, out, err = self.run_cli("diff", str(self.vdir), "v99")
        self.assertEqual(rc, 1)
        self.assertIn("没有版本 v99", err)

    def test_diff_references_and_missing_fields(self) -> None:
        self.run_cli("plan", str(self.vdir), "--write")
        data = self.read_shots()
        data["references"]["lantern"] = {"prompt": "Close-up of a weathered brass oil lantern"}
        data["references"]["keeper"]["prompt"] = data["references"]["keeper"]["prompt"].replace("navy wool", "grey wool")
        data["format"]["fps"] = 30
        data["shots"][0]["motion"]["backend"] = "placeholder"
        self.write_shots(data)

        rc, out, err = self.run_cli("diff", str(self.vdir), "v1", "now")
        self.assertEqual(rc, 0, err)
        self.assertIn("画幅 fps：24 → 30", out)
        self.assertIn("参考图 keeper：", out)
        self.assertIn("[-navy-]{+grey+}", out)
        self.assertIn("+ 参考图 lantern：Close-up of a weathered brass oil lantern", out)
        self.assertIn('后端：（无） → "placeholder"', out)


class TestRevert(FilmVersionTestBase):
    def test_full_revert_pick_invalidation_autosave_and_roundtrip(self) -> None:
        # Save v1
        self.run_cli("plan", str(self.vdir), "--write", "-m", "初稿")

        # Build v2:
        # - s01: only line changed -> keyframe & clip MUST be preserved on revert
        # - s02: keyframe.prompt changed -> BOTH keyframe & clip MUST be cleared on revert
        # - s03: only motion.prompt changed -> ONLY clip cleared, keyframe preserved on revert
        data = self.read_shots()
        data["shots"][0]["line"] = "新版 s01 台词"
        data["shots"][1]["keyframe"]["prompt"] = data["shots"][1]["keyframe"]["prompt"].replace("brass oil lantern", "electric torch")
        data["shots"][2]["motion"]["prompt"] = data["shots"][2]["motion"]["prompt"].replace("pull-out", "push-in") + " Extra motion."
        self.pick_all_assets(data)
        self.write_shots(data)
        self.run_cli("plan", str(self.vdir), "--write", "-m", "第二版")

        # Add an unsaved change to s01 notes before reverting to v1
        data = self.read_shots()
        data["shots"][0]["notes"] = "临时未保存备注"
        self.write_shots(data)

        # Revert whole storyboard to v1
        rc, out, err = self.run_cli("revert", str(self.vdir), "v1")
        self.assertEqual(rc, 0, err)
        self.assertIn("回退前自动保存", out)
        self.assertIn("回退到 v1", out)
        self.assertIn("清掉 s02 关键帧", out)
        self.assertIn("清掉 s02 视频", out)
        self.assertIn("清掉 s03 视频", out)
        self.assertNotIn("清掉 s03 关键帧", out)
        self.assertNotIn("清掉 s01", out)
        self.assertIn("文件还在 assets/", out)

        # Versions should now be v1 (初稿), v2 (第二版), v3 (回退前自动保存), v4 (回退到 v1)
        vers = film.load_versions(self.vdir)
        self.assertEqual([v["version"] for v in vers], [1, 2, 3, 4])
        self.assertEqual(vers[2]["note"], "回退前自动保存")
        self.assertEqual(vers[3]["note"], "回退到 v1")

        # Check reverted shots.json content and picks
        cur = self.read_shots()
        self.assertEqual(film.storyboard(cur), vers[0]["storyboard"])
        # s01: both keyframe & clip kept (only line & notes changed)
        self.assertEqual(cur["shots"][0]["keyframe"]["image"], "assets/keyframes/s01/trial/kf.png")
        self.assertEqual(cur["shots"][0]["keyframe"]["seed"], 201)
        self.assertEqual(cur["shots"][0]["motion"]["clip"], "assets/clips/s01/h3-trial/clip.mp4")
        self.assertEqual(cur["shots"][0]["motion"]["seed"], 301)
        # s02: both keyframe & clip cleared (keyframe.prompt changed)
        self.assertNotIn("image", cur["shots"][1]["keyframe"])
        self.assertNotIn("seed", cur["shots"][1]["keyframe"])
        self.assertNotIn("clip", cur["shots"][1]["motion"])
        self.assertNotIn("seed", cur["shots"][1]["motion"])
        # s03: keyframe kept, clip cleared (only motion.prompt changed)
        self.assertEqual(cur["shots"][2]["keyframe"]["image"], "assets/keyframes/s03/trial/kf.png")
        self.assertEqual(cur["shots"][2]["keyframe"]["seed"], 203)
        self.assertNotIn("clip", cur["shots"][2]["motion"])
        self.assertNotIn("seed", cur["shots"][2]["motion"])
        # Assets still exist on disk
        self.assertTrue((self.vdir / "assets/keyframes/s02/trial/kf.png").is_file())
        self.assertTrue((self.vdir / "assets/clips/s02/h3-trial/clip.mp4").is_file())

        # Reverting again to v1 when already at v1 -> no-op
        rc, out, err = self.run_cli("revert", str(self.vdir), "v1")
        self.assertEqual(rc, 0, err)
        self.assertIn("已经是这个样子，没有改动", out)
        self.assertEqual(len(self.version_paths()), 4)

        # Can revert back to v3 (the auto-saved pre-revert state!)
        rc, out, err = self.run_cli("revert", str(self.vdir), "v3", "-m", "撤销回退")
        self.assertEqual(rc, 0, err)
        vers = film.load_versions(self.vdir)
        self.assertEqual(len(vers), 5)
        self.assertEqual(vers[4]["note"], "撤销回退")
        cur2 = self.read_shots()
        self.assertEqual(cur2["shots"][0]["notes"], "临时未保存备注")
        self.assertEqual(film.storyboard(cur2), vers[2]["storyboard"])

    def test_revert_shots_partial_and_reinsert_deleted(self) -> None:
        # v1 has [s01, s02, s03]
        self.run_cli("plan", str(self.vdir), "--write")

        # v2: modify s01 & s03, delete s02, modify global style
        data = self.read_shots()
        data["title"] = "改过标题"
        data["shots"][0]["line"] = "v2 的 s01 台词"
        data["shots"][2]["line"] = "v2 的 s03 台词"
        data["shots"] = [data["shots"][0], data["shots"][2]]
        self.write_shots(data)
        self.run_cli("plan", str(self.vdir), "--write", "-m", "删掉 s02")

        # Revert only s02 from v1 -> s02 reinserted right after s01 (between s01 and s03); title & s01/s03 untouched
        rc, out, err = self.run_cli("revert", str(self.vdir), "v1", "--shots", "s02")
        self.assertEqual(rc, 0, err)
        self.assertIn("s02 回退到 v1", out)
        cur = self.read_shots()
        self.assertEqual([s["id"] for s in cur["shots"]], ["s01", "s02", "s03"])
        self.assertEqual(cur["title"], "改过标题")
        self.assertEqual(cur["shots"][0]["line"], "v2 的 s01 台词")
        self.assertEqual(cur["shots"][2]["line"], "v2 的 s03 台词")

        # Now delete s01 (the first shot) and revert --shots s01 -> reinserted at index 0
        cur["shots"] = [cur["shots"][1], cur["shots"][2]]
        self.write_shots(cur)
        self.run_cli("plan", str(self.vdir), "--write", "-m", "删掉 s01")

        rc, out, err = self.run_cli("revert", str(self.vdir), "v1", "--shots", "s01")
        self.assertEqual(rc, 0, err)
        cur = self.read_shots()
        self.assertEqual([s["id"] for s in cur["shots"]], ["s01", "s02", "s03"])
        self.assertEqual(cur["shots"][0]["line"], "天还没亮，灯塔已经醒了。")

        # Target version doesn't contain requested shot -> FilmError
        rc, out, err = self.run_cli("revert", str(self.vdir), "v2", "--shots", "s02")
        self.assertEqual(rc, 1)
        self.assertIn("v2 里没有 s02", err)

    def test_revert_validation_failure_writes_nothing(self) -> None:
        # v1: s02 uses ref "keeper"
        self.run_cli("plan", str(self.vdir), "--write")

        # v2: remove ref "keeper" and remove "keeper" from s02.keyframe.refs
        data = self.read_shots()
        del data["references"]["keeper"]
        data["shots"][1]["keyframe"]["refs"] = []
        data["shots"][1]["keyframe"]["prompt"] = "Medium shot inside the lighthouse spiral staircase"
        self.write_shots(data)
        self.run_cli("plan", str(self.vdir), "--write", "-m", "去掉 keeper 参考图")

        # Also make an unsaved edit to prove even auto-save does NOT run when validation fails
        data = self.read_shots()
        data["shots"][0]["notes"] = "未保存"
        self.write_shots(data)
        shots_before = (self.vdir / "prompts" / "shots.json").read_text(encoding="utf-8")
        versions_before = [p.name for p in self.version_paths()]

        # Reverting only s02 to v1 reintroduces refs: ["keeper"] without restoring references.keeper -> validate fails!
        rc, out, err = self.run_cli("revert", str(self.vdir), "v1", "--shots", "s02")
        self.assertEqual(rc, 1)
        self.assertIn("keeper", out)
        self.assertIn("错误", err)
        # Neither shots.json nor prompts/versions changed
        self.assertEqual((self.vdir / "prompts" / "shots.json").read_text(encoding="utf-8"), shots_before)
        self.assertEqual([p.name for p in self.version_paths()], versions_before)

    def test_invalidation_rules_style_ref_video_style_backend(self) -> None:
        # Test ref prompt change invalidates ref + referencing shot (s02), not s01/s03
        data = self.read_shots()
        self.pick_all_assets(data)
        self.write_shots(data)
        self.run_cli("plan", str(self.vdir), "--write")

        v1_sb = film.storyboard(data)
        # 1. Ref prompt change
        d1 = copy.deepcopy(data)
        d1["references"]["keeper"]["prompt"] += ", wearing a yellow raincoat"
        stale = film.stale_picks(v1_sb, d1)
        kinds = {(e["kind"], e["target"]) for e in stale}
        self.assertEqual(kinds, {("ref", "keeper"), ("keyframe", "s02"), ("clip", "s02")})

        # 2. Global style change -> all refs, all keyframes, all clips
        d2 = copy.deepcopy(data)
        d2["style"] = "Watercolor illustration"
        stale = film.stale_picks(v1_sb, d2)
        kinds = {(e["kind"], e["target"]) for e in stale}
        self.assertEqual(kinds, {
            ("ref", "keeper"),
            ("keyframe", "s01"), ("clip", "s01"),
            ("keyframe", "s02"), ("clip", "s02"),
            ("keyframe", "s03"), ("clip", "s03"),
        })

        # 3. Global video_style change -> only clips
        d3 = copy.deepcopy(data)
        d3["video_style"] = "Anime style"
        stale = film.stale_picks(v1_sb, d3)
        kinds = {(e["kind"], e["target"]) for e in stale}
        self.assertEqual(kinds, {("clip", "s01"), ("clip", "s02"), ("clip", "s03")})

        # 4. Effective backend change on s01 only -> only s01 clip
        d4 = copy.deepcopy(data)
        d4["shots"][0]["motion"]["backend"] = "placeholder"
        stale = film.stale_picks(v1_sb, d4)
        kinds = {(e["kind"], e["target"]) for e in stale}
        self.assertEqual(kinds, {("clip", "s01")})

        # 5. Only transition / notes / line changed -> nothing stale
        d5 = copy.deepcopy(data)
        d5["shots"][0]["transition"] = "cut"
        d5["shots"][1]["notes"] = "改备注"
        d5["shots"][2]["line"] = "改台词"
        self.assertEqual(film.stale_picks(v1_sb, d5), [])


if __name__ == "__main__":
    unittest.main()
