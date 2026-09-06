# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""音乐卡点混剪模板。

参考 GitHub 开源项目 AutoCutV2 (PapaPandroni) 与 Auto-action-video-editor
(nicholsbw77) 的 beat-synced 思路：用 librosa 检测 BGM 节拍，
把源视频切成「节拍间隔整数倍」的片段，硬切/转场正好落在节拍网格上，最后混入 BGM。

  - 每段时长 D = beat_unit × 节拍间隔 step，因此拼接边界 = k×step 恰为节拍时刻 → 完美卡点
  - transition=none 硬切最卡点；fade 转场将跨越节拍附近
"""
import os
import subprocess
import random

import numpy as np

META = {
    "id": "beat_cut",
    "name": "音乐卡点混剪",
    "description": "节拍检测 · 按节拍切分 · 硬切/转场踩点 · 配BGM",
}

SCHEMA = [
    {"key": "src_video", "label": "视频素材", "type": "video_file"},
    {"key": "bgm_file", "label": "背景音乐 BGM", "type": "audio_file",
     "help": "用于节拍检测与最终混音"},
    {"key": "target_dur", "label": "目标总时长(秒)", "type": "number", "default": 30, "min": 5,
     "help": "实际时长 = 片段数×每段时长，会取接近值"},
    {"key": "beat_unit", "label": "每片段节拍数", "type": "number", "default": 1, "min": 1, "max": 8,
     "help": "1=每1拍切一次(快节奏)，2=每2拍(舒缓)"},
    {"key": "transition", "label": "转场", "type": "select",
     "options": [{"value": "none", "label": "硬切(最卡点)"},
                 {"value": "fade", "label": "交叉淡化(跨节拍)"}],
     "default": "none"},
    {"key": "transition_dur", "label": "转场时长(秒)", "type": "number", "default": 0.25, "step": 0.05},
    {"key": "start_ss", "label": "源视频起始(秒)", "type": "number", "default": 5,
     "help": "从源视频第几秒开始取材"},
    {"key": "gap", "label": "片段间源跳跃(秒)", "type": "number", "default": 0, "step": 0.5,
     "help": "相邻片段在源视频上跳过多少秒(避免镜头重复)"},
    {"key": "width", "label": "分辨率宽", "type": "number", "default": 1920},
    {"key": "height", "label": "分辨率高", "type": "number", "default": 1080},
    {"key": "fps", "label": "帧率", "type": "number", "default": 25},
    {"key": "output_name", "label": "输出文件名", "type": "text", "default": "beat_cut_result"},
]


def run(cfg, log=print):
    W = int(cfg.get("width", 1920))
    H = int(cfg.get("height", 1080))
    FPS = int(cfg.get("fps", 25))
    SRC = cfg["src_video"]
    BGM = cfg["bgm_file"]
    WD = cfg["workdir"]
    OUTPUT = cfg["output"]

    TARGET = float(cfg.get("target_dur", 30))
    BEAT_UNIT = max(1, int(cfg.get("beat_unit", 1)))
    TRANS = cfg.get("transition") or "none"
    TRANS_DUR = float(cfg.get("transition_dur", 0.25))
    START_SS = float(cfg.get("start_ss", 5))
    GAP = float(cfg.get("gap", 0))

    os.makedirs(WD, exist_ok=True)
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)

    import imageio_ffmpeg
    FF = imageio_ffmpeg.get_ffmpeg_exe()

    def run_ff(args, label=""):
        log(f"[cmd] {label}")
        try:
            r = subprocess.run(args, capture_output=True, text=True)
        except Exception as e:
            raise RuntimeError(f"[EXC] {label}: {e}")
        if r.returncode != 0:
            raise RuntimeError(f"[FFERR] {label} rc={r.returncode}\n{r.stderr[-3000:]}")
        return r

    def probe_duration(media):
        r = subprocess.run([FF, "-hide_banner", "-i", media, "-f", "null", "-"],
                           capture_output=True, text=True)
        for line in r.stderr.splitlines():
            if "Duration:" in line:
                try:
                    raw = line.split("Duration:")[1].split(",")[0].strip()
                    hh, mm, ss = raw.split(":")
                    return int(hh) * 3600 + int(mm) * 60 + float(ss)
                except Exception:
                    continue
        return 0.0

    # ----------------------------------------------------------
    # 步骤1：提取 BGM → 节拍检测
    # ----------------------------------------------------------
    log("= 步骤1/4 提取BGM并检测节拍 =")
    bgm_wav = os.path.join(WD, "bgm.wav")
    run_ff([FF, "-hide_banner", "-y", "-i", BGM,
            "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1",
            bgm_wav], "extract bgm")

    import librosa
    y, sr = librosa.load(bgm_wav, sr=44100)
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, start_bpm=120, units="time")
    tempo = float(np.atleast_1d(tempo)[0])
    beats = [float(x) for x in beats]
    if len(beats) < 2:
        step = 0.315
    else:
        diffs = np.diff(beats)
        step = float(np.median(diffs))
    D = BEAT_UNIT * step  # 每段时长 = 节拍间隔整数倍 → 边界落在节拍网格上
    n = max(2, int(round(TARGET / D)))
    total = n * D
    log(f"  tempo={tempo:.0f}BPM  节拍间隔={step:.3f}s  每段={D:.3f}s  共{n}段")
    log(f"  输出总时长≈{total:.2f}s  边界步进=节拍网格(每{BEAT_UNIT}拍一切)")

    # ----------------------------------------------------------
    # 步骤2：从源视频顺序截取 n 段（每段 D 秒）
    # ----------------------------------------------------------
    log("= 步骤2/4 截取片段 =")
    COVER = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    src_dur = probe_duration(SRC)
    segs = []
    for i in range(n):
        ss = START_SS + i * (D + GAP)
        if ss + D > src_dur:  # 源视频不够，回绕
            ss = max(0.0, src_dur - D - 1.0)
        seg = os.path.join(WD, f"seg{i:02d}.mp4")
        run_ff([FF, "-hide_banner", "-y",
                "-ss", f"{ss:.3f}", "-i", SRC,
                "-t", f"{D:.3f}",
                "-vf", COVER,
                "-r", str(FPS), "-pix_fmt", "yuv420p",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-video_track_timescale", "13824",
                "-an", seg], f"seg {i} @{ss:.1f}s")
        segs.append(seg)
    log(f"  已截取 {n} 段 -> 源视频起始{START_SS}s, 段长{D:.2f}s")

    # ----------------------------------------------------------
    # 步骤3：拼接（硬切 / 交叉淡化）
    # ----------------------------------------------------------
    log("= 步骤3/4 拼接 =")
    if TRANS == "fade" and n >= 2:
        inputs = []
        for seg in segs:
            inputs += ["-i", seg]
        durs = [probe_duration(s) for s in segs]
        acc = durs[0] + durs[1] - TRANS_DUR
        fc = [f"[0:v][1:v]xfade=transition=fade:duration={TRANS_DUR:.3f}:offset={max(0.0, durs[0] - TRANS_DUR):.3f}[v1]"]
        label = "v1"
        for k in range(2, len(segs)):
            off = max(0.0, acc - TRANS_DUR)
            fc.append(f"[v{k-1}][{k}:v]xfade=transition=fade:duration={TRANS_DUR:.3f}:offset={off:.3f}[v{k}]")
            acc = acc + durs[k] - TRANS_DUR
            label = f"v{k}"
        fc.append(f"[{label}]fps={FPS},format=yuv420p[vout]")
        joined = os.path.join(WD, "joined.mp4")
        run_ff([FF, "-hide_banner", "-y", *inputs,
                "-filter_complex", ";".join(fc),
                "-map", "[vout]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-video_track_timescale", "13824",
                "-an", joined], "concat with xfade")
    else:
        concat_txt = os.path.join(WD, "concat.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            for seg in segs:
                f.write(f"file '{seg.replace(chr(92), '/')}'\n")
        joined = os.path.join(WD, "joined.mp4")
        run_ff([FF, "-hide_banner", "-y",
                "-f", "concat", "-safe", "0", "-i", concat_txt,
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-video_track_timescale", "13824",
                "-an", joined], "concat clips")
    total_actual = probe_duration(joined)
    log(f"  拼接完成: {total_actual:.2f}s")

    # ----------------------------------------------------------
    # 步骤4：混入 BGM（循环补齐时长）
    # ----------------------------------------------------------
    log("= 步骤4/4 混入BGM =")
    run_ff([FF, "-hide_banner", "-y",
            "-i", joined, "-stream_loop", "-1", "-i", BGM,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy",
            "-af", "atrim=duration=%.3f,asetpts=PTS-STARTPTS" % total_actual,
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{total_actual:.3f}",
            OUTPUT], "mux bgm (looped)")
    log(f"= DONE: {OUTPUT} =")

    import json
    params = {
        "tempo_bpm": tempo, "step_s": step, "beat_unit": BEAT_UNIT,
        "seg_dur_s": D, "n_segments": n, "total_s": total_actual,
        "transition": TRANS, "output": OUTPUT, "segments": segs,
    }
    with open(os.path.join(WD, "params.json"), "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)
    return params
