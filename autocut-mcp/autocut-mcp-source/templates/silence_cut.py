# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""去静音 / 跳跃剪辑模板。

参考 GitHub 开源项目 Jumpcutter (c0decracker)、qmm-autoedit、ffmpeg_toolkit
(superyngo) 的静音处理思路：用 ffmpeg silencedetect 定位静音区间，然后
  - mode=cut   删除静音段（口播紧凑，总时长缩短）
  - mode=speed 静音段按 silent_speed 倍速播放（保留呼吸感，atempo 链式变速）

变速/原速各小段统一编码参数后 concat，视频音频同步。
"""
import os
import subprocess

META = {
    "id": "silence_cut",
    "name": "去静音/跳跃剪辑",
    "description": "自动检测静音 · 删除或加速 · 口播录屏提效",
}

SCHEMA = [
    {"key": "src_video", "label": "视频素材", "type": "video_file"},
    {"key": "mode", "label": "处理方式", "type": "select",
     "options": [{"value": "cut", "label": "删除静音段(紧凑)"},
                 {"value": "speed", "label": "静音段加速(保留呼吸感)"}],
     "default": "cut"},
    {"key": "threshold", "label": "静音阈值(dB)", "type": "number", "default": -35,
     "help": "音量低于该值视为静音，越接近0越严格"},
    {"key": "min_silence", "label": "最短静音(秒)", "type": "number", "default": 0.3, "step": 0.1,
     "help": "短于此的停顿不处理"},
    {"key": "padding", "label": "边界保留(秒)", "type": "number", "default": 0.15, "step": 0.05,
     "help": "静音边界前后保留的缓冲，避免生硬过渡"},
    {"key": "silent_speed", "label": "静音段倍速", "type": "number", "default": 8.0, "min": 2.0,
     "help": "仅 mode=加速 时生效"},
    {"key": "width", "label": "分辨率宽", "type": "number", "default": 1920},
    {"key": "height", "label": "分辨率高", "type": "number", "default": 1080},
    {"key": "fps", "label": "帧率", "type": "number", "default": 25},
    {"key": "output_name", "label": "输出文件名", "type": "text", "default": "silence_cut_result"},
]


def _atempo_chain(speed):
    """atempo 支持 0.5~2.0，>2 需链式。返回 atempo 滤镜串。"""
    if speed <= 2.0:
        return f"atempo={speed:.4f}"
    chain = "atempo=2.0"
    rem = speed / 2.0
    while rem > 2.0:
        chain += ",atempo=2.0"
        rem /= 2.0
    chain += f",atempo={rem:.4f}"
    return chain


def run(cfg, log=print):
    W = int(cfg.get("width", 1920))
    H = int(cfg.get("height", 1080))
    FPS = int(cfg.get("fps", 25))
    SRC = cfg["src_video"]
    WD = cfg["workdir"]
    OUTPUT = cfg["output"]

    MODE = cfg.get("mode") or "cut"
    THRESH = float(cfg.get("threshold", -35))
    MIN_SIL = float(cfg.get("min_silence", 0.3))
    PAD = float(cfg.get("padding", 0.15))
    SIL_SPEED = float(cfg.get("silent_speed", 8.0))

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
    # 步骤1：silencedetect 定位静音区间
    # ----------------------------------------------------------
    log("= 步骤1/4 静音检测 =")
    r = subprocess.run([FF, "-hide_banner", "-i", SRC,
                        "-af", f"silencedetect=noise={THRESH:g}dB:d={MIN_SIL:.2f}",
                        "-f", "null", "-"],
                       capture_output=True, text=True)
    silences = []  # (start, end)
    cur = None
    for line in r.stderr.splitlines():
        if "silence_start:" in line:
            try:
                cur = float(line.split("silence_start:")[1].split()[0])
            except Exception:
                pass
        elif "silence_end:" in line and cur is not None:
            try:
                end = float(line.split("silence_end:")[1].split()[0])
                silences.append((cur, end))
            except Exception:
                pass
            cur = None
    src_dur = probe_duration(SRC)
    # 去除末尾无意义的静音（视频结束处），并加边界
    keep = []
    for (s, e) in silences:
        if s >= src_dur - MIN_SIL:
            continue
        keep.append((max(0.0, s - PAD), min(src_dur, e + PAD)))
    # 合并重叠
    merged = []
    for (s, e) in keep:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    log(f"  检测到静音段 {len(merged)} 个 (阈值{THRESH:g}dB, 最短{MIN_SIL}s): "
        f"{[ (round(a,2), round(b,2)) for a, b in merged ]}")
    if not merged:
        log("  未检测到可处理静音，直接复制输出")
        run_ff([FF, "-hide_banner", "-y", "-i", SRC, "-c", "copy", OUTPUT], "copy original")
        import json
        with open(os.path.join(WD, "params.json"), "w", encoding="utf-8") as f:
            json.dump({"mode": MODE, "silences": [], "total_s": src_dur, "output": OUTPUT},
                      f, ensure_ascii=False, indent=2)
        return {"mode": MODE, "silences": [], "total_s": src_dur, "output": OUTPUT}

    # ----------------------------------------------------------
    # 步骤2：构建时间轴小段 (start, end, speed)
    # ----------------------------------------------------------
    log("= 步骤2/4 构建时间轴 =")
    segs = []  # (start, end, speed)
    cursor = 0.0
    for (s, e) in merged:
        if s > cursor + 0.05:
            segs.append((cursor, s, 1.0))           # 有声段原速
        if MODE == "speed":
            segs.append((s, e, SIL_SPEED))          # 静音段加速
        # mode=cut 时静音段直接丢弃
        cursor = e
    if cursor < src_dur - 0.05:
        segs.append((cursor, src_dur, 1.0))
    if not segs:
        raise RuntimeError("时间轴为空，请调低静音阈值或检查素材")
    log(f"  生成 {len(segs)} 个小段 (mode={MODE})")

    # ----------------------------------------------------------
    # 步骤3：逐段截取 + 变速编码（参数统一便于 concat）
    # 注意：本机 ffmpeg 构建下 -vf 的 setpts 不生效，必须用 filter_complex + -map
    # ----------------------------------------------------------
    log("= 步骤3/4 逐段处理 =")
    COVER = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    parts = []
    for i, (st, en, sp) in enumerate(segs):
        dur = en - st
        part = os.path.join(WD, f"part{i:03d}.mp4")
        args = [FF, "-hide_banner", "-y",
                "-ss", f"{st:.3f}", "-t", f"{dur:.3f}", "-i", SRC]
        if abs(sp - 1.0) > 1e-3:
            fc = (f"[0:v]{COVER},setpts=PTS/{sp:.4f}[v];"
                  f"[0:a]{_atempo_chain(sp)}[a]")
            args += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
        else:
            args += ["-filter_complex", f"[0:v]{COVER}[v]",
                     "-map", "[v]", "-map", "0:a"]
        args += ["-pix_fmt", "yuv420p",
                 "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                 "-video_track_timescale", "13824",
                 "-c:a", "aac", "-b:a", "192k",
                 part]
        run_ff(args, f"part {i}: {st:.1f}~{en:.1f}s x{sp:g}")
        parts.append(part)
    log(f"  已生成 {len(parts)} 段")

    # ----------------------------------------------------------
    # 步骤4：concat 拼接
    # ----------------------------------------------------------
    log("= 步骤4/4 拼接 =")
    concat_txt = os.path.join(WD, "concat.txt")
    with open(concat_txt, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{p.replace(chr(92), '/')}'\n")
    run_ff([FF, "-hide_banner", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_txt,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-video_track_timescale", "13824",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            OUTPUT], "concat parts")
    total = probe_duration(OUTPUT)
    log(f"  原长 {src_dur:.2f}s -> 输出 {total:.2f}s (压缩比 {total / max(src_dur, 1e-6) * 100:.0f}%)")
    log(f"= DONE: {OUTPUT} =")

    import json
    params = {"mode": MODE, "silences": merged, "n_segments": len(segs),
              "src_dur_s": src_dur, "total_s": total, "output": OUTPUT}
    with open(os.path.join(WD, "params.json"), "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)
    return params
