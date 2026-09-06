# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""四分屏Vlog片头模板。

从 abba_v2/quadsplit_auto.py 重构而来，作为"模板系统"的一个内置模板。
核心算法保持不变：
  步骤1 导入BGM，节拍检测取7节点
  步骤2-5 四分屏动画（4层cover+overlay堆叠，蒙版宽度1920→960→640→480左移）
  步骤6 新视频setpts延迟到第7节点铺底，阶梯裁切逐条露出
  步骤7 定格：波峰处放大+饱和度归零
  步骤8 镜面蒙版反转：缝隙最小→完全打开，回忆快闪 + 滴答处3帧照片闪图
"""
import os
import subprocess
import sys

import numpy as np

META = {
    "id": "quadsplit",
    "name": "四分屏Vlog片头",
    "description": "BGM节拍同步 · 蒙版收缩 · 阶梯裁切 · 定格回忆快闪",
}

SCHEMA = [
    {"key": "src_video", "label": "视频素材", "type": "video_file",
     "help": "四分屏画面 + 新视频 + 闪图来源"},
    {"key": "bgm_file", "label": "背景音乐 BGM", "type": "audio_file",
     "help": "用于节拍检测与最终混音"},
    {"key": "new_ss", "label": "新视频起点(秒)", "type": "number", "default": 60,
     "help": "从源视频第几秒截取新视频段"},
    {"key": "tile_ss", "label": "四宫格画面起点(4段,秒)", "type": "textlist",
     "default": "0.0, 1.5, 3.0, 4.5"},
    {"key": "mask_widths", "label": "蒙版宽度序列(px)", "type": "textlist",
     "default": "1920, 960, 640, 480"},
    {"key": "num_nodes", "label": "节点数", "type": "number", "default": 7, "min": 7, "max": 12,
     "help": "新视频铺底与阶梯裁切依赖第7节点，最少7个"},
    {"key": "end_dur", "label": "结尾时长(秒)", "type": "number", "default": 3.7, "step": 0.1},
    {"key": "zoom", "label": "波峰放大比例", "type": "number", "default": 0.15, "step": 0.05,
     "help": "定格后放大 1→1+zoom"},
    {"key": "width", "label": "分辨率宽", "type": "number", "default": 1920},
    {"key": "height", "label": "分辨率高", "type": "number", "default": 1080},
    {"key": "fps", "label": "帧率", "type": "number", "default": 25},
    {"key": "output_name", "label": "输出文件名", "type": "text", "default": "quadsplit_final"},
]

DEFAULT_CFG = {
    "width": 1920, "height": 1080, "fps": 25,
    "new_ss": 60.0, "tile_ss": [0.0, 1.5, 3.0, 4.5],
    "mask_widths": [1920, 960, 640, 480],
    "end_dur": 3.7, "zoom": 0.15, "num_nodes": 7,
}


def _parse_numlist(val):
    """把 '0.0, 1.5, 3.0' 或 [0.0, 1.5] 统一解析为 float 列表。"""
    if isinstance(val, (list, tuple)):
        return [float(x) for x in val]
    if val is None:
        return []
    return [float(x) for x in str(val).replace("，", ",").split(",") if str(x).strip()]


def run(cfg, log=print):
    """执行四分屏模板。cfg 由后端组装（含 workdir/output/src_video/bgm_file）。"""
    W = int(cfg.get("width", 1920))
    H = int(cfg.get("height", 1080))
    FPS = int(cfg.get("fps", 25))
    NEW_SS = float(cfg["new_ss"])
    TILE_SS = _parse_numlist(cfg["tile_ss"])
    MW = _parse_numlist(cfg["mask_widths"])
    END_DUR = float(cfg["end_dur"])
    ZOOM = float(cfg["zoom"])
    NNODES = int(cfg.get("num_nodes", 7))
    SRC_VIDEO = cfg["src_video"]
    BGM_FILE = cfg["bgm_file"]
    WD = cfg["workdir"]
    OUTPUT = cfg["output"]

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
    # 步骤0：提取BGM → 节拍检测 → 节点
    # ----------------------------------------------------------
    log("= 步骤0/8 提取BGM并检测节拍 =")
    BGM_WAV = os.path.join(WD, "bgm.wav")
    run_ff([FF, "-hide_banner", "-y", "-i", BGM_FILE,
            "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1",
            BGM_WAV], "extract bgm")

    import librosa
    y, sr = librosa.load(BGM_WAV, sr=44100)
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, start_bpm=120, units="time")
    tempo = float(np.atleast_1d(tempo)[0])
    beats = [float(x) for x in beats]
    step = beats[1] - beats[0] if len(beats) > 1 else 0.315
    N = [beats[0] + i * step for i in range(NNODES)]
    log(f"  tempo={tempo:.0f}BPM  step={step:.3f}s")
    log(f"  {NNODES}节点: {[round(x, 3) for x in N]}")

    if len(N) < 7:
        raise RuntimeError(f"节点数不足: 检测到{len(N)}个节点, 至少需要7个 (num_nodes 最小为7)")
    N1, N2, N3, N4, N5, N6, N7 = N[:7]
    NEW_END = N7 + 4 * step
    NEW_DUR = 4 * step

    # ----------------------------------------------------------
    # 蒙版宽度动画 cw(t)
    # ----------------------------------------------------------
    def seg(t0, t1, v0, v1):
        return f"({v0}+({v1}-{v0})*((t-{t0:.4f})/{max(t1 - t0, 1e-6):.4f}))"

    m0, m1, m2, m3 = MW[0], MW[1], MW[2], MW[3]
    CW = (
        f"if(lt(t,{N1:.4f}),{W},"
        f"if(lt(t,{N2:.4f}),{seg(N1, N2, W, m1)},"
        f"if(lt(t,{N3:.4f}),{m1},"
        f"if(lt(t,{N4:.4f}),{seg(N3, N4, m1, m2)},"
        f"if(lt(t,{N5:.4f}),{m2},"
        f"if(lt(t,{N6:.4f}),{seg(N5, N6, m2, m3)},{m3}))))))"
    )

    # ----------------------------------------------------------
    # 步骤2-5：四分屏动画 + 新视频铺底
    # ----------------------------------------------------------
    log("= 步骤2-5/8 四分屏动画 + 新视频接入 =")
    COVER = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"

    new_vid = os.path.join(WD, "new_video.mp4")
    run_ff([FF, "-hide_banner", "-y",
            "-ss", f"{NEW_SS:.3f}", "-i", SRC_VIDEO,
            "-vf", COVER,
            "-t", f"{NEW_DUR:.3f}",
            "-r", str(FPS), "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-video_track_timescale", "13824",
            "-an", new_vid], "new video seg")
    log(f"  新视频段 -> {round(NEW_DUR, 2)}s (源{NEW_SS:.0f}s起)")

    WIN_END = [N7, N7 + step, N7 + 2 * step, N7 + 3 * step]

    fc = []
    fc.append(f"color=c=black:s={W}x{H}:r={FPS}[bg]")
    fc.append(f"[4:v]{COVER},setpts=PTS+{N7:.4f}/TB[NW_c]")
    fc.append(f"[bg][NW_c]overlay=0:0[base]")
    for i in range(4):
        fc.append(f"[{i}:v]{COVER}[V{i}_c]")
    prev = "base"
    for i in range(4):
        s = 0 if i < 2 else [N3, N5][i - 2]
        e = WIN_END[i]
        fc.append(
            f"[{prev}][V{i}_c]overlay=x='{i}*({CW})':y=0:eval=frame"
            f":enable='between(t,{s:.4f},{e:.4f})'[V{i}]"
        )
        prev = f"V{i}"

    intro = os.path.join(WD, "intro.mp4")
    intro_inputs = []
    for ss in TILE_SS:
        intro_inputs += ["-ss", str(ss), "-i", SRC_VIDEO]
    intro_inputs += ["-i", new_vid]
    run_ff([FF, "-hide_banner", "-y",
            *intro_inputs,
            "-filter_complex", ";".join(fc),
            "-map", f"[{prev}]",
            "-t", f"{NEW_END:.3f}",
            "-r", str(FPS), "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-video_track_timescale", "13824",
            "-an", intro], "intro quadsplit (stacking) + new video base")
    log(f"  四分屏+新视频 -> {round(NEW_END, 2)}s (tiles@{TILE_SS})")

    # ----------------------------------------------------------
    # 步骤7-8：定格 + 镜面回忆快闪（连续结尾）
    # ----------------------------------------------------------
    log("= 步骤7-8/8 定格 + 镜面回忆快闪 =")
    freeze_png = os.path.join(WD, "freeze.png")
    run_ff([FF, "-hide_banner", "-y",
            "-i", new_vid,
            "-vf", f"select='gte(n,{int(NEW_DUR * FPS) - 1})'", "-frames:v", "1",
            freeze_png], "freeze frame")

    rms = librosa.feature.rms(y=y, hop_length=512)[0]
    rms_t = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=512)
    t0_freeze = NEW_END
    peak_span = t0_freeze + 2.0
    idx0 = int(t0_freeze * sr / 512)
    idx1 = int(min(peak_span, 26.0) * sr / 512)
    if idx1 > idx0 + 1:
        peak_i = idx0 + int(np.argmax(rms[idx0:idx1]))
        PEAK_LOCAL = float(rms_t[peak_i]) - t0_freeze
    else:
        PEAK_LOCAL = 0.6
    log(f"  波峰(定格内): {PEAK_LOCAL:.2f}s")

    end_frames = int(END_DUR * FPS)

    from scipy.signal import find_peaks
    oenv = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512)
    ot = librosa.frames_to_time(np.arange(len(oenv)), sr=sr, hop_length=512)
    mask_o = (ot > t0_freeze) & (ot < t0_freeze + END_DUR)
    seg_o = oenv[mask_o]; tt_o = ot[mask_o]
    pk_o, _ = find_peaks(seg_o, height=seg_o.max() * 0.25, distance=int(0.02 * sr / 512))
    tick_rel = sorted(float(tt_o[q]) - t0_freeze for q in pk_o)
    flashes = []
    for x in tick_rel:
        if 0 <= x < END_DUR and (not flashes or x - flashes[-1] >= 3.0 / FPS):
            flashes.append(x)
    flashes = flashes[:12]
    log(f"  波峰 {PEAK_LOCAL:.2f}s  滴答闪点 {len(flashes)}个: {[round(x, 2) for x in flashes]}")

    NPHOTO = max(len(flashes), 1)
    src_dur = probe_duration(SRC_VIDEO)
    offsets = np.linspace(3.0, max(4.0, src_dur - 3.0), NPHOTO)
    photo_dir = os.path.join(WD, "photos")
    os.makedirs(photo_dir, exist_ok=True)
    photo_paths = []
    for j, off in enumerate(offsets):
        ph = os.path.join(photo_dir, f"p{j:02d}.png")
        run_ff([FF, "-hide_banner", "-y",
                "-ss", f"{off:.3f}", "-i", SRC_VIDEO,
                "-vf", COVER, "-frames:v", "1", ph], f"photo {j}")
        photo_paths.append(ph)

    from PIL import Image, ImageEnhance
    sup = Image.open(freeze_png).convert("RGB").resize((W * 2, H * 2), Image.LANCZOS)
    photos = [Image.open(q).convert("RGB") for q in photo_paths]
    end_dir = os.path.join(WD, "end_frames")
    os.makedirs(end_dir, exist_ok=True)
    for n in range(end_frames):
        tl = n / FPS
        frame = None
        for j, ft in enumerate(flashes):
            if ft <= tl < ft + 3.0 / FPS:
                frame = photos[j % len(photos)].copy()
                break
        if frame is None:
            z = 1.0 + ZOOM * min(tl, PEAK_LOCAL) / max(PEAK_LOCAL, 1e-6)
            sw = int(W * 2 / z); sh = int(H * 2 / z)
            x0 = (W * 2 - sw) // 2; y0 = (H * 2 - sh) // 2
            frame = sup.crop((x0, y0, x0 + sw, y0 + sh)).resize((W, H), Image.LANCZOS)
            if tl >= PEAK_LOCAL:
                frame = ImageEnhance.Color(frame).enhance(0.0)
            h = max(2, int(H * tl / END_DUR))
            y1 = (H - h) // 2
            canvas = Image.new("RGB", (W, H), (0, 0, 0))
            canvas.paste(frame.crop((0, y1, W, y1 + h)), (0, y1))
            frame = canvas
        frame.save(os.path.join(end_dir, f"e{n:03d}.png"))
    ending_vid = os.path.join(WD, "ending.mp4")
    run_ff([FF, "-hide_banner", "-y",
            "-framerate", str(FPS), "-i", os.path.join(end_dir, "e%03d.png"),
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-video_track_timescale", "13824",
            "-an", ending_vid], "ending encode")
    log(f"  统一结尾(缩放+去色+镜面打开+滴答照片快闪) -> {END_DUR:.2f}s")

    # ----------------------------------------------------------
    # 拼接全部段落 + 混入BGM
    # ----------------------------------------------------------
    log("= 拼接与BGM合成 =")
    segs = [intro, ending_vid]
    concat_txt = os.path.join(WD, "concat.txt")
    with open(concat_txt, "w", encoding="utf-8") as f:
        for seg in segs:
            f.write(f"file '{seg.replace(chr(92), '/')}'\n")

    video_noaud = os.path.join(WD, "video_noaud.mp4")
    run_ff([FF, "-hide_banner", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_txt,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-video_track_timescale", "13824",
            "-an", video_noaud], "concat segs")

    total = probe_duration(video_noaud)
    log(f"  视频总时长: {total:.2f}s")

    run_ff([FF, "-hide_banner", "-y",
            "-i", video_noaud, "-i", BGM_FILE,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-t", f"{total:.3f}", "-shortest",
            OUTPUT], "mux bgm")
    log(f"= DONE: {OUTPUT} =")

    import json
    params = {
        "tempo_bpm": tempo, "step_s": step, "nodes": N,
        "new_end_s": NEW_END, "new_video_dur_s": NEW_DUR,
        "t0_freeze_s": t0_freeze, "peak_local_s": PEAK_LOCAL,
        "ending_dur_s": END_DUR, "flashes": flashes,
        "photo_offsets": [float(x) for x in offsets],
        "output": OUTPUT, "total_s": total, "segments": segs,
    }
    with open(os.path.join(WD, "params.json"), "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)
    return params
