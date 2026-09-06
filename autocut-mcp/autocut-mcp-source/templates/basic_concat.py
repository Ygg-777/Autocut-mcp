# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""基础拼接模板：按用户指定的片段列表从视频中裁剪并拼接，可选转场 + BGM + 结尾淡出。

通用性示例：这是与"四分屏"完全不同的剪辑方式，验证模板系统可扩展任意剪辑逻辑。
  - clips: 片段列表 "start-duration | start-duration ..."
  - transition: 无 / 交叉淡化 / 叠化
  - bgm + 结尾淡出 + 音量
"""
import os
import subprocess

META = {
    "id": "basic_concat",
    "name": "基础拼接剪辑",
    "description": "按指定片段裁剪拼接 · 转场 · 配BGM · 结尾淡出",
}

SCHEMA = [
    {"key": "src_video", "label": "视频素材", "type": "video_file"},
    {"key": "clips", "label": "片段列表(起始秒-时长秒)", "type": "text", "default": "0-3 | 5-4 | 10-3",
     "help": "格式：起始秒-时长秒，多个用 | 分隔，如 0-3 | 5-4 | 10-3"},
    {"key": "transition", "label": "转场", "type": "select",
     "options": [{"value": "none", "label": "无转场(硬切)"},
                 {"value": "fade", "label": "交叉淡化"}],
     "default": "fade"},
    {"key": "transition_dur", "label": "转场时长(秒)", "type": "number", "default": 0.4, "step": 0.1},
    {"key": "bgm_file", "label": "背景音乐(可选)", "type": "audio_file", "required": False},
    {"key": "bgm_volume", "label": "BGM音量", "type": "number", "default": 1.0, "step": 0.1},
    {"key": "fadeout", "label": "结尾淡出(秒)", "type": "number", "default": 0.5, "step": 0.1},
    {"key": "width", "label": "分辨率宽", "type": "number", "default": 1920},
    {"key": "height", "label": "分辨率高", "type": "number", "default": 1080},
    {"key": "fps", "label": "帧率", "type": "number", "default": 25},
    {"key": "output_name", "label": "输出文件名", "type": "text", "default": "basic_concat"},
]


def run(cfg, log=print):
    W = int(cfg.get("width", 1920))
    H = int(cfg.get("height", 1080))
    FPS = int(cfg.get("fps", 25))
    SRC = cfg["src_video"]
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

    # 解析片段列表 "0-3 | 5-4 | 10-3"
    raw_clips = cfg.get("clips") or "0-3 | 5-4 | 10-3"
    clips = []
    for token in str(raw_clips).replace("，", ",").replace("|", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            s, _, d = token.partition("-")
            clips.append((float(s.strip()), float(d.strip())))
        else:
            clips.append((float(token), 3.0))
    if not clips:
        raise RuntimeError("未解析到有效片段，格式应为 起始-时长 | 起始-时长，如 0-3 | 5-4")
    log(f"  解析片段 {len(clips)}段: {clips}")

    trans = cfg.get("transition") or "none"
    trans_dur = float(cfg.get("transition_dur", 0.4))

    # 逐段裁剪为统一尺寸/帧率的片段
    segs = []
    for i, (ss, dur) in enumerate(clips):
        seg = os.path.join(WD, f"seg{i:02d}.mp4")
        run_ff([FF, "-hide_banner", "-y",
                "-ss", f"{ss:.3f}", "-i", SRC,
                "-t", f"{dur:.3f}",
                "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}",
                "-r", str(FPS), "-pix_fmt", "yuv420p",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-video_track_timescale", "13824",
                "-an", seg], f"clip {i} @{ss}s")
        segs.append(seg)
        log(f"  片段{i+1}: {ss}s→{ss+dur}s ({dur:.2f}s)")

    # 拼接（支持交叉淡化）
    if trans == "fade" and len(segs) >= 2:
        # xfade 链：必须为每级 xfade 显式指定 offset（= 前段累计时长 - 转场时长），
        # 否则默认 offset=duration 会让后面的转场在开头重叠，导致总时长被截短。
        inputs = []
        for seg in segs:
            inputs += ["-i", seg]
        durs = [probe_duration(s) for s in segs]
        acc = durs[0] + durs[1] - trans_dur
        fc = [f"[0:v][1:v]xfade=transition=fade:duration={trans_dur:.3f}:offset={max(0.0, durs[0] - trans_dur):.3f}[v1]"]
        label = "v1"
        for k in range(2, len(segs)):
            offset = max(0.0, acc - trans_dur)
            fc.append(f"[v{k-1}][{k}:v]xfade=transition=fade:duration={trans_dur:.3f}:offset={offset:.3f}[v{k}]")
            acc = acc + durs[k] - trans_dur
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
    total = probe_duration(joined)
    log(f"  拼接完成: {total:.2f}s")

    # 结尾淡出 + 可选BGM
    fadeout = float(cfg.get("fadeout", 0.5))
    vf = f"fade=t=out:st={max(0.0, total - fadeout):.3f}:d={fadeout:.3f}:alpha=1"
    tmp = os.path.join(WD, "video_faded.mp4")
    run_ff([FF, "-hide_banner", "-y", "-i", joined,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-video_track_timescale", "13824",
            "-an", tmp], "fade out")

    bgm = cfg.get("bgm_file")
    if bgm and os.path.isfile(bgm):
        vol = float(cfg.get("bgm_volume", 1.0))
        run_ff([FF, "-hide_banner", "-y",
                "-i", tmp, "-i", bgm,
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy",
                "-af", f"volume={vol:.2f}",
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{total:.3f}", "-shortest",
                OUTPUT], "mux bgm")
    else:
        run_ff([FF, "-hide_banner", "-y", "-i", tmp,
                "-c:v", "copy",
                "-movflags", "+faststart",
                OUTPUT], "finalize")
    log(f"= DONE: {OUTPUT} =")

    import json
    params = {"clips": clips, "transition": trans, "total_s": total, "output": OUTPUT}
    with open(os.path.join(WD, "params.json"), "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)
    return params
