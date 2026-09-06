# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""离线冒烟测试: 验证文本/字幕驱动剪辑与字幕烧录整条链路。
运行: python tests/smoke_test.py  (无需网络/GPU)
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import imageio_ffmpeg
FF = imageio_ffmpeg.get_ffmpeg_exe()

from engine import segments as seg
from engine import smartcut as sc
from engine import grade as gr
import autocut_mcp

SAMPLE_SRT = """1
00:00:00,200 --> 00:00:01,400
大家好，这是第一句。

2
00:00:01,800 --> 00:00:03,000
第二句要保留。

3
00:00:03,400 --> 00:00:04,600
这段是不要的内容。

4
00:00:05,000 --> 00:00:06,200
最后一句也要。
"""


def make_video(path, dur=8.0):
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=duration=%s:size=640x360:rate=25" % dur,
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=%s" % dur,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                    "-c:a", "aac", path], check=True)
    return path


class TestSegments(unittest.TestCase):
    def test_time_convert(self):
        self.assertAlmostEqual(seg.ts_to_seconds("00:00:01,500"), 1.5)
        self.assertAlmostEqual(seg.ts_to_seconds("00:01:02,000"), 62.0)
        self.assertAlmostEqual(seg.ts_to_seconds("2.5"), 2.5)
        self.assertEqual(seg.seconds_to_srt_ts(62.5), "00:01:02,500")

    def test_srt_roundtrip(self):
        entries = seg.parse_srt(SAMPLE_SRT)
        self.assertEqual(len(entries), 4)
        self.assertAlmostEqual(entries[0]["start"], 0.2)
        self.assertEqual(entries[1]["text"], "第二句要保留。")
        text2 = seg.build_srt(entries)
        entries2 = seg.parse_srt(text2)
        self.assertEqual([e["text"] for e in entries2],
                         [e["text"] for e in entries])
        self.assertAlmostEqual(entries2[3]["end"], 6.2)

    def test_ass_escape_and_style(self):
        segs = [{"t0": 0.0, "t1": 1.0, "text": "第一行\n第二行 {ok}", "src": None}]
        ass = seg.segments_to_ass(segs, width=640, height=360)
        self.assertIn("[Script Info]", ass)
        self.assertIn("PlayResX: 640", ass)
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,", ass)
        self.assertIn("\\N", ass)
        self.assertNotIn("{ok}", ass)

    def test_select_keep_drop(self):
        src = [{"src": "v.mp4", "t0": i, "t1": i + 1, "text": t}
               for i, t in enumerate(["a 保留 b", "c", "d 保留 e", "f"])]
        clips, dropped, kept = seg.select_segments(
            src, keep_mode="keep", match="keywords", value="保留", padding=0.0)
        self.assertEqual(len(clips), 2)
        self.assertEqual(len(kept), 2)
        self.assertEqual(len(dropped), 2)
        clips2, _, kept2 = seg.select_segments(
            src, keep_mode="drop", match="keywords", value="保留", padding=0.0)
        self.assertEqual(len(clips2), 2)   # c / f 两段彼此不相邻, 不合并
        self.assertEqual(len(kept2), 2)

    def test_padding_merge(self):
        src = [{"src": "v.mp4", "t0": 1.0, "t1": 2.0, "text": "x"},
               {"src": "v.mp4", "t0": 2.6, "t1": 3.0, "text": "y"}]
        clips, _, _ = seg.select_segments(src, keep_mode="keep",
                                          match="items", value="0,1", padding=0.4)
        self.assertEqual(len(clips), 1)
        self.assertAlmostEqual(clips[0]["t0"], 0.6)
        self.assertAlmostEqual(clips[0]["t1"], 3.4)


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="autocut_smoke_")
        cls.video = make_video(os.path.join(cls.tmp, "src.mp4"))
        cls.srt = os.path.join(cls.tmp, "src.srt")
        with open(cls.srt, "w", encoding="utf-8") as f:
            f.write(SAMPLE_SRT)

    def test_plan_text_cut(self):
        out = autocut_mcp.plan_text_cut(
            self.video, self.srt, mode="keep", match="keywords",
            value="要保留|也要", padding=0.0)
        d = json.loads(out)
        self.assertEqual(d["kept_segments"], 2)
        self.assertEqual(d["dropped_segments"], 2)
        self.assertIsNotNone(d["timeline"])
        self.assertEqual(len(d["timeline"]["tracks"][0]["clips"]), 2)

    def test_text_cut_render(self):
        out_path = os.path.join(self.tmp, "cut.mp4")
        out = autocut_mcp.text_cut(
            self.video, self.srt, out_path, mode="keep", match="keywords",
            value="要保留|也要", padding=0.0, width=320, height=180, fps=15)
        d = json.loads(out)
        self.assertTrue(os.path.isfile(out_path), out)
        self.assertEqual(d["kept_segments"], 2)
        probe = json.loads(autocut_mcp.probe_media(out_path))
        self.assertGreater(probe["duration"], 0.5)
        self.assertTrue(any(st.get("codec_type") == "audio" for st in probe.get("streams", [])),
                        "成片必须保留源音轨")

    def test_burn_subtitles(self):
        out_path = os.path.join(self.tmp, "burned.mp4")
        out = autocut_mcp.burn_subtitles(self.video, self.srt, out_path,
                                         fontname="Arial", fontsize=48)
        d = json.loads(out)
        self.assertTrue(os.path.isfile(out_path), out)
        self.assertEqual(d["segments"], 4)

    def test_srt_to_segments_tool(self):
        out = autocut_mcp.srt_to_segments(self.srt, src=self.video)
        d = json.loads(out)
        self.assertEqual(len(d["segments"]), 4)
        self.assertEqual(d["segments"][1]["text"], "第二句要保留。")


class TestAutoEdit(unittest.TestCase):
    """一键智能成片: 去静音 / 删语气词 / 字幕重排(全部离线)。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="autocut_auto_")
        # 5s: 音-静-音-静-音 (两段 1s 静音)
        a = os.path.join(cls.tmp, "a.wav")
        subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1",
                        "-f", "lavfi", "-i", "sine=frequency=660:duration=1",
                        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1",
                        "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
                        "-filter_complex", "[0:a][1:a][2:a][3:a][4:a]concat=n=5:v=0:a=1[a]",
                        "-map", "[a]", "-c:a", "pcm_s16le", a], check=True)
        cls.sil_video = os.path.join(cls.tmp, "sil.mp4")
        subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=duration=5:size=320x180:rate=15",
                        "-i", a, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-preset", "ultrafast", "-c:a", "aac", "-shortest", cls.sil_video], check=True)
        # 3s 连续音(无静音)用于语气词删除
        cls.tone_video = os.path.join(cls.tmp, "tone.mp4")
        subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=duration=3:size=320x180:rate=15",
                        "-f", "lavfi", "-i", "sine=frequency=500:duration=3",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                        "-c:a", "aac", cls.tone_video], check=True)
        # 词级字幕 JSON(含 um/uh)
        cls.seg_json = os.path.join(cls.tmp, "f.segments.json")
        import json as _j
        segs = [{"t0": 0.0, "t1": 3.0, "text": "keep um talk uh end", "words": [
            {"word": "keep", "start": 0.0, "end": 0.5},
            {"word": "um", "start": 1.0, "end": 1.35},
            {"word": "talk", "start": 1.35, "end": 2.0},
            {"word": "uh", "start": 2.2, "end": 2.5},
            {"word": "end", "start": 2.5, "end": 3.0}]}]
        with open(cls.seg_json, "w", encoding="utf-8") as f:
            f.write(_j.dumps({"segments": segs}, ensure_ascii=False))

    def test_auto_clean_silence(self):
        out = os.path.join(self.tmp, "clean.mp4")
        d = json.loads(autocut_mcp.auto_edit(self.sil_video, out, mode="clean",
                                             width=320, height=180, fps=15))
        self.assertTrue(os.path.isfile(out), d)
        self.assertGreaterEqual(d.get("removed_intervals", 0), 1)
        self.assertLess(d["out_dur_s"], d["src_dur_s"])
        self.assertLess(d["out_dur_s"], 4.3)

    def test_auto_talk_filler(self):
        out = os.path.join(self.tmp, "talk.mp4")
        d = json.loads(autocut_mcp.auto_edit(self.tone_video, out, mode="talk",
                                             segments_json=self.seg_json,
                                             width=320, height=180, fps=15))
        self.assertTrue(os.path.isfile(out), d)
        self.assertGreaterEqual(d.get("filler_hits", 0), 2)
        self.assertLess(d["out_dur_s"], d["src_dur_s"])

    def test_auto_talk_look(self):
        out = os.path.join(self.tmp, "talk_look.mp4")
        d = json.loads(autocut_mcp.auto_edit(self.tone_video, out, mode="talk",
                                             segments_json=self.seg_json, look="cinema",
                                             width=320, height=180, fps=15))
        self.assertTrue(os.path.isfile(out), d)
        self.assertTrue(any("调色" in str(n) for n in d.get("notes", [])), d.get("notes"))

    def test_smartcut_pure_fns(self):
        segs = [{"t0": 0.0, "t1": 3.0, "text": "x", "words": [
            {"word": "um", "start": 1.0, "end": 1.3}, {"word": "ok", "start": 1.3, "end": 2.0}]}]
        hits = sc.collect_filler_intervals(segs, "um,uh", pad=0.05)
        self.assertEqual(len(hits), 1)
        self.assertAlmostEqual(hits[0][0], 0.95, places=2)
        kept = [(0.0, 1.0), (2.0, 3.0)]
        src = [{"t0": 0.2, "t1": 0.8, "text": "a"}, {"t0": 0.9, "t1": 2.5, "text": "b"}]
        out = sc.remap_subtitles(src, kept)
        # a 完整映射到成片 0.2-0.8; b 被切成 0.9-1.0 与 2.0-2.5 两段
        self.assertGreaterEqual(len(out), 2)
        self.assertAlmostEqual(out[0]["t0"], 0.2, places=2)


class TestGrade(unittest.TestCase):
    """调色(达芬奇风格): 滤镜链 / MCP 工具 / 音轨保留。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="autocut_grade_")
        cls.src = os.path.join(cls.tmp, "src.mp4")
        subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=duration=1:size=160x90:rate=10",
                        "-f", "lavfi", "-i", "sine=frequency=400:duration=1",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                        "-c:a", "aac", cls.src], check=True)

    def test_build_filter(self):
        self.assertIn("colorbalance", gr.build_filter({"preset": "cinema"}))
        self.assertIn("colortemperature", gr.build_filter({"temperature": 5600}))
        self.assertEqual(gr.build_filter({"preset": "none"}), "")

    def test_apply_tool(self):
        out = os.path.join(self.tmp, "graded.mp4")
        d = json.loads(autocut_mcp.apply_color_grade(self.src, out, preset="cinema"))
        self.assertTrue(os.path.isfile(out), d)
        self.assertIn("colorbalance", d.get("filter", ""))
        probe = json.loads(autocut_mcp.probe_media(out))
        self.assertTrue(any(st.get("codec_type") == "audio" for st in probe.get("streams", [])))




class TestTriage(unittest.TestCase):
    """素材筛选引擎: 场记前缀/重拍/花絮标记 + 剧本节点对齐窗口。"""

    def _segs(self, items):
        return [{"start": s, "end": e, "text": t} for s, e, t in items]

    def test_director_prefix_trim(self):
        import engine.triage as T
        rushes = {"shot.mp4": {"lang": "en", "segs": self._segs([
            (0.0, 2.0, "3, 2, 1, GO!"),
            (2.0, 4.0, "Whoa, dude, you scared me."),
            (4.0, 5.0, "Sorry."),
        ])}}
        t = T.triage(rushes, pieces=[])
        a = t["clips"][0]
        self.assertEqual(a["verdict"], "prefix_ng")
        tk = a["takes"][0]
        self.assertTrue(tk["has_director_prefix"])
        self.assertEqual(tk["content_t0"], 2.0)
        self.assertTrue(tk["clean"])

    def test_dup_retake_detected(self):
        import engine.triage as T
        a = T.analyze_clip("dup.mp4", self._segs([
            (0.0, 3.0, "I really love the Chronicles of Narnia."),
            (4.0, 7.0, "I really love the Chronicles of Narnia."),
        ]))
        self.assertEqual(a["verdict"], "retake_mess")
        self.assertTrue(a["dup_retake"])

    def test_blooper_flagged(self):
        import engine.triage as T
        rushes = {"ng.mp4": {"lang": "en", "segs": self._segs([
            (0.0, 3.0, "What's wrong?"),
            (3.0, 6.0, "Sorry, I forgot my line."),
        ])}}
        t = T.triage(rushes, pieces=[])
        a = t["clips"][0]
        self.assertEqual(a["verdict"], "ng_included")
        self.assertFalse(a["takes"][0]["clean"])

    def test_unmatched_talk(self):
        import engine.triage as T
        rushes = {"out.mp4": {"lang": "en", "segs": self._segs([
            (0.3, 2.3, "I'm sorry"), (6.0, 8.0, "No, just")])}}
        pieces = [{"id": "p1", "title": "x", "lines": [], "keywords": ["porcelain", "doll"]}]
        t = T.triage(rushes, pieces=pieces)
        self.assertEqual(t["clips"][0]["verdict"], "unmatched_talk")

    def test_align_window_ignores_stray_kw(self):
        import engine.triage as T
        segs = self._segs([
            (0.0, 2.0, "Harry once traveled to china, whatever."),
            (10.0, 13.0, "Harry travels to China for the Porcelain Doll."),
            (13.5, 16.0, "He meets Chinese wizards who know Kung Fu."),
            (16.5, 19.0, "They fight villains near a volcano!"),
        ])
        piece = {"id": "p4", "title": "story", "lines": [], "keywords":
                 ["harry", "china", "porcelain", "doll", "chinese", "wizards", "kung", "fu", "villains", "volcano"]}
        a = T.analyze_clip("a.mp4", segs, duration=20.0)
        cands = T.align_piece(piece, [a])
        self.assertTrue(cands, "should find a candidate")
        self.assertGreaterEqual(cands[0]["t0"], 9.0,
                                "window must not include stray early china line")
        self.assertIn("Porcelain Doll", cands[0]["window_text"])



class TestCinema(unittest.TestCase):
    """电影化构图核心: 景别标定 / 安全裁窗 / 同人弱特征 / 反应镜头启发式。"""

    def test_shot_scale_bands(self):
        import engine.cinema as CIN
        self.assertEqual(CIN.shot_scale_of_face(120, 3840), "wide")     # 3%
        self.assertEqual(CIN.shot_scale_of_face(400, 3840), "medium")   # 10%
        self.assertEqual(CIN.shot_scale_of_face(800, 3840), "mcu")      # 21%
        self.assertEqual(CIN.shot_scale_of_face(1200, 3840), "closeup") # 31%

    def test_framing_medium_not_too_close(self):
        import engine.cinema as CIN
        # 4K 全景里的中等人脸, 请求 medium -> 应得到 medium 裁窗且不越界
        r = CIN.framing_rect(3840, 2160, (1700, 600, 400, 560), shot="medium")
        self.assertTrue(r["feasible"])
        self.assertEqual(r["shot"], "medium")
        self.assertGreaterEqual(r["x0"], 0); self.assertLessEqual(r["x0"] + r["w"], 3840)
        self.assertGreaterEqual(r["y0"], 0); self.assertLessEqual(r["y0"] + r["h"], 2160)
        self.assertLessEqual(r["face_frac"], 0.20)

    def test_framing_closeup_request_falls_looser_when_source_too_tight(self):
        import engine.cinema as CIN
        # 人脸已占 31% 源宽 -> 源本身没有更松的上下文, 必须回退全景(不硬裁/不放大)
        r = CIN.framing_rect(3840, 2160, (1300, 500, 1200, 900), shot="closeup")
        self.assertFalse(r["feasible"])
        self.assertEqual(r["shot"], "wide")
        self.assertEqual((r["w"], r["h"]), (3840, 2160))

    def test_framing_closeup_with_room(self):
        import engine.cinema as CIN
        r = CIN.framing_rect(3840, 2160, (2000, 700, 700, 900), shot="closeup")
        self.assertTrue(r["feasible"])
        self.assertEqual(r["shot"], "closeup")
        self.assertLess(r["w"], 3840)

    def test_union_rect_contains_both(self):
        import engine.cinema as CIN
        r = CIN.union_rect([(100, 200, 300, 400), (1500, 250, 260, 380)], frame_w=3840, frame_h=2160)
        self.assertLessEqual(r[0], 100)
        self.assertGreaterEqual(r[0] + r[2], 1500 + 260)
        self.assertLessEqual(r[1], 200)
        self.assertGreaterEqual(r[1] + r[3], 250 + 380)

    def test_same_person_descriptor(self):
        import engine.cinema as CIN
        a = [(100, 100), (120, 130), (90, 140), (110, 150), (80, 120)]
        b = [(200, 200), (240, 260), (180, 280), (220, 300), (160, 240)]  # 2x 缩放同形
        d1 = CIN.person_descriptor(a)
        d2 = CIN.person_descriptor(b)
        self.assertTrue(CIN.same_person(d1, d2))
        c = [(100, 100), (500, 130), (90, 140), (110, 150), (80, 120)]    # 一点远偏 -> 不同
        d3 = CIN.person_descriptor(c)
        self.assertFalse(CIN.same_person(d1, d3, thr=0.05))

    def test_reaction_score(self):
        import engine.cinema as CIN
        self.assertAlmostEqual(CIN.reaction_score(0.01, -0.1, is_speaker=False), 0.933, places=2)  # 安静听者
        self.assertEqual(CIN.reaction_score(0.05, 0.2, is_speaker=True), 0.0)    # 说话人不算反应
        self.assertLess(CIN.reaction_score(0.12, 0.1, is_speaker=False), 0.4)    # 嘴动大 -> 更像在说




class TestDirector(unittest.TestCase):
    """导演分镜决策(纯规则): 强调句/beat 开场/对话双人/反应镜头/防单调。"""

    def _an(self, nf=1, speaker=True, n_listen=0):
        tracks = [{"score": 1.0, "cx_px": 100, "cy_px": 100, "fw": 300, "fh": 400}] if speaker else []
        for i in range(n_listen):
            tracks.append({"score": 0.0, "cx_px": 200 + i * 50, "cy_px": 120, "fw": 260, "fh": 360})
        return {"nfaces": nf, "tracks": tracks,
                "speaker": tracks[0] if speaker else None,
                "listeners": tracks[1:] if speaker else tracks}

    def test_emphasis_detection(self):
        import engine.director as D
        self.assertTrue(D.unit_emphasis("My precious."))
        self.assertTrue(D.unit_emphasis("are you sure this is the official book?"))
        self.assertTrue(D.unit_emphasis("plain line", beat_id="p5"))
        self.assertFalse(D.unit_emphasis("an enormous world full of danger", beat_id="p1"))

    def test_no_faces_wide(self):
        import engine.director as D
        r = D.choose_shot({"first_of_piece": True, "emphasis": False, "dur_s": 3}, None)
        self.assertEqual((r["role"], r["subject"]), ("wide", "group"))

    def test_no_active_speaker_wide(self):
        import engine.director as D
        an = self._an(nf=2, speaker=False, n_listen=0)
        an["tracks"] = [dict(t, score=0.0) for t in an["tracks"]]
        an["listeners"] = an["tracks"]
        r = D.choose_shot({"first_of_piece": False, "emphasis": False, "dur_s": 3}, an)
        self.assertEqual(r["role"], "wide")

    def test_beat_open_establish_wide(self):
        import engine.director as D
        r = D.choose_shot({"first_of_piece": True, "emphasis": False, "dur_s": 3},
                          self._an(nf=3, n_listen=2))
        self.assertEqual((r["role"], r["subject"]), ("wide", "group"))

    def test_dialogue_two_shot(self):
        import engine.director as D
        r = D.choose_shot({"first_of_piece": False, "emphasis": False, "dur_s": 5},
                          self._an(nf=2, n_listen=1))
        self.assertEqual((r["role"], r["subject"]), ("medium", "speaker_plus_listener"))

    def test_emphasis_reaction_break(self):
        import engine.director as D
        r = D.choose_shot({"first_of_piece": False, "emphasis": True, "dur_s": 3.5},
                          self._an(nf=2, n_listen=1), last_roles=("mcu",))
        self.assertEqual(r["role"], "reaction")

    def test_variety_after_repeat(self):
        import engine.director as D
        an = self._an(nf=1, speaker=True, n_listen=0)
        r = D.choose_shot({"first_of_piece": False, "emphasis": False, "dur_s": 2},
                          an, last_roles=("mcu", "mcu"))
        self.assertEqual(r["role"], "medium")
        self.assertIn("variety", r["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

