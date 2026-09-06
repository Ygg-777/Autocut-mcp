# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""一键智能成片模板 (面向“不会剪辑的用户”: 给一个视频, 自动产出干净成片)

参考架构: OpenCut AI / Descript 式 SmartCut(去静音+删语气词), fragua 的
“timings belong to the edit”(删完再按新时间轴烧字幕), aymind/auto-edit-video 向导。

mode:
  talk  口播智能精简 = 自动去静音 + (可选)删语气词(需词级字幕JSON) + (可选)重排烧录字幕
  clean 仅去静音
  beat  卡点混剪 = 交给 beat_cut (需 BGM)
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess

def _finalize(src: str, dst: str) -> None:
    """Move intermediate file to final output.

    On Windows os.replace cannot move across disk volumes (OSError WinError 17).
    Same volume -> atomic replace; cross volume -> copy + delete.
    """
    same_vol = (os.path.normcase(os.path.splitdrive(os.path.abspath(src))[0]) ==
                os.path.normcase(os.path.splitdrive(os.path.abspath(dst))[0]))
    if same_vol:
        os.replace(src, dst)
    else:
        shutil.copy2(src, dst)
        os.remove(src)


META = {
    "id": "auto_edit",
    "name": "一键智能成片",
    "description": "口播智能精简(去静音+删语气词+字幕重排烧录) / 仅去静音 / 卡点混剪",
}

SCHEMA = [
    {"key": "mode", "label": "模式", "type": "select",
     "options": [{"value": "talk", "label": "口播智能精简(推荐)"},
                 {"value": "clean", "label": "仅去静音"},
                 {"value": "beat", "label": "卡点混剪(需BGM)"}],
     "default": "talk"},
    {"key": "src_video", "label": "视频素材", "type": "video_file"},
    {"key": "segments_json", "label": "词级字幕JSON(可选, 删语气词用)", "type": "text", "required": False,
     "help": "transcribe_media(word_timestamps=true) 生成的 .segments.json 路径"},
    {"key": "fillers", "label": "语气词列表", "type": "text", "default": "嗯,啊,呃,那个,这个,就是,然后,um,uh"},
    {"key": "look", "label": "成片调色(可选)", "type": "select",
     "options": [{"value": "none", "label": "原色(不调)"}, {"value": "vivid", "label": "鲜艳自然"},
                 {"value": "warm", "label": "暖阳"}, {"value": "cool", "label": "冷调"},
                 {"value": "soft", "label": "柔和"}, {"value": "food", "label": "美食暖色"},
                 {"value": "cinema", "label": "电影感(青橙)"}, {"value": "bw", "label": "黑白"}],
     "default": "none", "help": "口播精简/去静音成片后套一层调色滤镜"},
    {"key": "filler_padding", "label": "语气词边界(秒)", "type": "number", "default": 0.05, "step": 0.01},
    {"key": "threshold", "label": "静音阈值(dB)", "type": "number", "default": -35},
    {"key": "min_silence", "label": "最短静音(秒)", "type": "number", "default": 0.3, "step": 0.05},
    {"key": "padding", "label": "静音边界保留(秒)", "type": "number", "default": 0.15, "step": 0.05},
    {"key": "subtitle_srt", "label": "成片字幕SRT(可选, 自动重排后烧录)", "type": "text", "required": False},
    {"key": "bgm_file", "label": "BGM(仅卡点模式)", "type": "audio_file", "required": False},
    {"key": "target_dur", "label": "卡点目标时长(秒)", "type": "number", "default": 30},
    {"key": "beat_unit", "label": "每片段节拍数", "type": "number", "default": 1, "min": 1, "max": 8},
    {"key": "transition", "label": "转场(卡点)", "type": "select",
     "options": [{"value": "none", "label": "硬切(最卡点)"}, {"value": "fade", "label": "交叉淡化"}],
     "default": "none"},
    {"key": "width", "label": "分辨率宽", "type": "number", "default": 1920},
    {"key": "height", "label": "分辨率高", "type": "number", "default": 1080},
    {"key": "fps", "label": "帧率", "type": "number", "default": 25},
    {"key": "output_name", "label": "输出文件名", "type": "text", "default": "auto_edit_result"},
]


def _try_import(pkg, names):
    try:
        mod = __import__(pkg, fromlist=names)
        return tuple(getattr(mod, n) for n in names)
    except ImportError:
        mod = __import__("_".join(pkg.split(".")[-1:]), fromlist=names)
        return tuple(getattr(mod, n) for n in names)


def _video_size(path):
    r = subprocess.run([_ff(), "-hide_banner", "-i", path], capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "Video:" in line:
            m = re.search(r"(\d{3,5})x(\d{3,5})", line)
            if m:
                return int(m.group(1)), int(m.group(2))
    return 1920, 1080


def _ff():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _burn_subs(video, segments, ass_path, output, log):
    from engine.segments import write_ass
    W, H = _video_size(video)
    write_ass(segments, ass_path, width=W, height=H,
              fontname="Microsoft YaHei", fontsize=0)
    r = subprocess.run([_ff(), "-hide_banner", "-loglevel", "error", "-y",
                        "-i", video, "-vf", "ass=subs.ass",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                        "-pix_fmt", "yuv420p", "-c:a", "copy", output],
                       capture_output=True, text=True,
                       cwd=os.path.dirname(ass_path))
    if r.returncode != 0:
        raise RuntimeError("字幕烧录失败: " + (r.stderr or "")[-1500:])
    log("  已烧录 %d 条(重排后)字幕 -> %s" % (len(segments), os.path.basename(output)))


def run(cfg, log=print):
    import imageio_ffmpeg
    SRC = cfg["src_video"]
    WD = cfg["workdir"]
    OUTPUT = cfg["output"]
    W = int(cfg.get("width", 1920)); H = int(cfg.get("height", 1080))
    FPS = int(cfg.get("fps", 25))
    MODE = cfg.get("mode") or "talk"
    os.makedirs(WD, exist_ok=True)
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)

    if MODE == "beat":
        log("= 一键智能成片 · 卡点混剪 (委托 beat_cut) =")
        beat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beat_cut.py")
        spec = importlib.util.spec_from_file_location("beat_cut", beat)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        bcfg = dict(cfg)
        bcfg.setdefault("bgm_file", cfg.get("bgm_file") or "")
        if not bcfg.get("bgm_file"):
            raise RuntimeError("卡点混剪模式需要 bgm_file")
        return mod.run(bcfg, log=log)

    from engine.smartcut import (detect_silences, merge_intervals, removed_to_kept,
                                 render_kept, copy_media, collect_filler_intervals,
                                 load_segments_json, probe_duration, remap_subtitles)
    from engine.segments import read_srt

    log("= 一键智能成片 · %s = 源: %s" % ("口播智能精简" if MODE == "talk" else "仅去静音",
                                        os.path.basename(SRC)))
    removed, notes = [], []
    src_dur = probe_duration(SRC)

    # 1) 静音
    thr = float(cfg.get("threshold", -35)); ms = float(cfg.get("min_silence", 0.3))
    pad = float(cfg.get("padding", 0.15))
    total, silences = detect_silences(SRC, threshold_db=thr, min_silence=ms)
    if silences:
        sil = merge_intervals([(max(0.0, s - pad), min(total, e + pad)) for s, e in silences])
        removed += sil
        notes.append("静音 %d 段" % len(sil))
        log("  静音检测: %d 段 -> %s" % (len(sil), [(round(a, 2), round(b, 2)) for a, b in sil[:6]]))

    # 2) 语气词(仅 talk 且给了词级字幕)
    fillers = str(cfg.get("fillers") or "嗯,啊,呃,那个,这个,就是,然后,um,uh")
    fpad = float(cfg.get("filler_padding", 0.05))
    seg_path = cfg.get("segments_json")
    n_words = 0
    if MODE == "talk" and seg_path:
        segs = load_segments_json(seg_path)
        fhits = collect_filler_intervals(segs, fillers, pad=fpad)
        if fhits:
            removed += fhits
            n_words = len(fhits)
            notes.append("语气词 %d 处" % len(fhits))
            log("  语气词命中: %d 处 -> %s" % (len(fhits), [(round(a, 2), round(b, 2)) for a, b in fhits[:6]]))
        else:
            log("  未在词级字幕中命中语气词(跳过该步骤)")

    if not removed:
        log("  没有需要删除的静音/语气词, 直接复制原片")
        res = copy_media(SRC, OUTPUT, log=log)
        subtitle = cfg.get("subtitle_srt")
        if subtitle and os.path.isfile(subtitle):
            segs = read_srt(subtitle)
            segs2 = [{"t0": x["start"], "t1": x["end"], "text": x["text"]} for x in segs]
            _burn_subs(SRC, segs2, os.path.join(WD, "subs.ass"), OUTPUT + ".tmp.mp4", log)
            _finalize(OUTPUT + ".tmp.mp4", OUTPUT)
        res.update({"mode": MODE, "notes": notes, "src_dur_s": round(src_dur, 3),
                    "out_dur_s": round(probe_duration(OUTPUT), 3), "output": OUTPUT})
        return res

    removed = merge_intervals(removed)
    kept = removed_to_kept(total, removed, min_keep=0.0)
    log("  剪辑蓝图: 保留 %d 段 (%.2fs -> %.2fs, 压缩 %.0f%%)" % (
        len(kept), total, sum(b - a for a, b in kept),
        100 * sum(b - a for a, b in kept) / max(total, 1e-6)))

    # 3) 渲染(先出无字幕成片)
    inter = os.path.join(WD, "clean.mp4")
    log("  渲染精简成片…")
    render_kept(SRC, kept, inter, width=W, height=H, fps=FPS, log=log)

    # 3.5) 可选: 成片调色(达芬奇风格预设)
    look = str(cfg.get("look") or "none")
    if look and look != "none":
        from engine.grade import apply_grade as _grade
        graded = os.path.join(WD, "graded.mp4")
        log("  成片调色: %s" % look)
        _grade(inter, graded, {"preset": look}, workdir=WD, log=log)
        inter = graded
        notes.append("调色(%s)" % look)

    # 4) 可选: 重排字幕并烧录
    subtitle = cfg.get("subtitle_srt")
    if subtitle and os.path.isfile(subtitle):
        entries = read_srt(subtitle)
        src_segs = [{"t0": x["start"], "t1": x["end"], "text": x["text"]} for x in entries]
        new_segs = remap_subtitles(src_segs, kept)
        log("  字幕重排: %d 条 -> %d 条" % (len(src_segs), len(new_segs)))
        _burn_subs(inter, new_segs, os.path.join(WD, "subs.ass"), OUTPUT, log)
    else:
        _finalize(inter, OUTPUT)

    out_dur = probe_duration(OUTPUT)
    res = {"mode": MODE, "notes": notes, "removed_intervals": len(removed),
           "kept_segments": len(kept), "src_dur_s": round(total, 3),
           "out_dur_s": round(out_dur, 3),
           "ratio": round(100 * out_dur / max(total, 1e-6), 1),
           "filler_hits": n_words, "output": OUTPUT}
    log("= DONE: 原 %.2fs -> 成片 %.2fs (%.0f%%) =  %s" % (total, out_dur, 100 * out_dur / max(total, 1e-6), OUTPUT))
    return res
