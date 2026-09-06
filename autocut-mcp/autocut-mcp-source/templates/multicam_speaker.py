# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""多机位说话人自动校验模板 (自动剪辑升级核心)

背景: 原 _legacy_single_take 靠人工逐段核对说话人, 仍出现"切到没在说话的人"的错切。
本模板把 360-autocam 的 SyncNet式 唇动-音频同步校验 + multicam-edit 的 FFT 音频对齐
集成进模板系统:
  步骤1  音频对齐: 以参考机位为基准, FFT互相关求各机位偏移, 统一到同一时间轴
  步骤2  逐段校验: 对每个台词段在所有机位抽帧测量"归一化唇开度"序列,
         与音频RMS求皮尔逊r, 选出正在说话(唇动大且r为正)的机位
  步骤3  自动修正: 生成修正后的段落表(机位/时间/说话人)
  步骤4  渲染: 按修正表裁切拼接 + 可选字幕

判据(SyncNet): 说话的脸 = lip_std 大(嘴在动) 且 r>0(唇动与语音同步);
               lip_std≈0 或 r≤0 → 此机位没有在说话。

输入: video_files(逗号分隔机位) + segments(JSON: [{src,t0,t1,note}...]) + 参考机位
输出: 修正后的段落映射表 + 拼接成品
"""
import os
import subprocess
import json

META = {
    "id": "multicam_speaker",
    "name": "多机位说话人自动校验",
    "description": "SyncNet式唇动-音频校验 · FFT对齐 · 自动修正切错说话人的镜头",
}

SCHEMA = [
    {"key": "video_files", "label": "机位视频(逗号分隔)", "type": "text",
     "help": "逗号分隔的多个机位视频路径, 如 d:/a.mp4,d:/b.mp4"},
    {"key": "segments", "label": "段落表JSON", "type": "text",
     "help": "JSON: [{\"src\":\"视频路径\",\"t0\":秒,\"t1\":秒,\"note\":\"说明\"}]"},
    {"key": "ref_index", "label": "参考机位序号(0起)", "type": "number", "default": 0,
     "help": "以哪个机位为时间基准做音频对齐"},
    {"key": "lip_fps", "label": "唇检帧率", "type": "number", "default": 8, "min": 4, "max": 15},
    {"key": "width", "label": "分辨率宽", "type": "number", "default": 1920},
    {"key": "height", "label": "分辨率高", "type": "number", "default": 1080},
    {"key": "fps", "label": "输出帧率", "type": "number", "default": 25},
    {"key": "subtitle_srt", "label": "字幕SRT(可选)", "type": "text", "default": ""},
    {"key": "output_name", "label": "输出文件名", "type": "text", "default": "multicam_speaker"},
]

import importlib.util
_ENGINE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "engine")
_sm_path = os.path.join(_ENGINE_DIR, "speaker_map.py")
spec = importlib.util.spec_from_file_location("speaker_map", _sm_path)
_asm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_asm)


def _parse_json_list(val, field="segments"):
    if isinstance(val, (list, tuple)):
        return list(val)
    import json as _j
    return _j.loads(str(val))


def _load_srt(srt_path):
    """解析SRT -> [(start,end,text)]"""
    entries = []
    with open(srt_path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    cur = None
    for line in lines:
        line = line.strip()
        if "-->" in line:
            a, _, b = line.partition("-->")
            cur = [_ts(a.strip()), _ts(b.strip())]
        elif cur and line:
            cur.append(line)
        elif cur and not line:
            if len(cur) >= 3:
                entries.append((cur[0], cur[1], cur[2]))
            cur = None
    return entries


def _ts(s):
    h, m, rest = s.split(":")
    sec, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000.0


def _render_closeup(seg, src, W, H, FPS, out, FF, frac=0.35, fpy=0.56):
    """人物特写智能筛选渲染。
    逐段跟踪说话人脸: 只有能稳定检出【足够大/清晰】的说话人脸时才裁特写,
    否则返回 None(由调用方回退为全景)。避免之前"乱取景/人脸过小"的问题。
    frac: 特写面宽占输出画面比例(越小越需放大镜越远/画面越松, 露出手部动作)。
    fpy:  人脸在输出画面中距顶部的高度比例(0.56≈人脸略低于居中, 越接近0人脸越靠上, 下方空间越大越能露出手部)。"""
    try:
        SW, SH, pts = _asm.speaker_track(src, seg["t0"], seg["t1"], fps=6)
    except Exception:
        return None
    if not pts:
        return None
    boxes = [(cx, cy, fw) for (t, cx, cy, fw, fh) in pts if fw > 8 and fh > 8]
    if len(boxes) < 4:
        return None
    n = len(boxes)
    cx = sorted(b[0] for b in boxes)[n // 2]
    cy = sorted(b[1] for b in boxes)[n // 2]
    fw = sorted(b[2] for b in boxes)[n // 2]
    # 目标: 特写面宽占输出画面约 frac
    cw = fw / frac
    ch = cw * H / W
    # 源帧不足以达到该放大(脸太小/画面太远) -> 非真实特写, 回退全景
    if cw >= SW * 0.98 or ch >= SH * 0.98:
        return None
    # 人脸位于输出画面 fpy 高度处(fpy 越小, 脸越靠上, 下方保留更多空间露出手部)
    x0 = max(0, min(SW - cw, cx - cw / 2))
    y0 = max(0, min(SH - ch, cy - fpy * ch))
    x0, y0, cw, ch = int(x0), int(y0), int(cw), int(ch)
    dur = max(0.3, seg["t1"] - seg["t0"])
    vf = (f"crop={cw}:{ch}:{x0}:{y0},scale={W}:{H}:sws_flags=bilinear,format=yuv420p")
    r = subprocess.run([FF, "-hide_banner", "-y", "-ss", f"{seg['t0']:.3f}", "-t", f"{dur:.3f}",
                        "-i", src, "-vf", vf, "-r", str(FPS), "-c:v", "libx264",
                        "-preset", "fast", "-crf", "18", "-video_track_timescale", "13824",
                        "-an", out], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return {"cw": cw, "ch": ch, "x": x0, "y": y0, "face_w": fw}


def _render_static(seg, src, W, H, FPS, out, FF):
    """普通段: 整源缩放裁剪到画布。"""
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},format=yuv420p"
    dur = max(0.3, seg["t1"] - seg["t0"])
    subprocess.run([_asm.FF, "-hide_banner", "-y",
                    "-ss", f"{seg['t0']:.3f}", "-t", f"{dur:.3f}", "-i", src,
                    "-vf", vf, "-r", str(FPS), "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    "-video_track_timescale", "13824", "-an", out],
                   capture_output=True, text=True)


def run(cfg, log=print):
    WD = cfg["workdir"]
    OUTPUT = cfg["output"]
    os.makedirs(WD, exist_ok=True)
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)

    videos = [v.strip() for v in str(cfg["video_files"]).replace("；", ",").split(",") if v.strip()]
    if len(videos) < 2:
        raise RuntimeError("至少需要2个机位视频")
    segments = _parse_json_list(cfg["segments"])
    if not segments:
        raise RuntimeError("段落表不能为空")
    ref_index = int(cfg.get("ref_index", 0))
    lip_fps = float(cfg.get("lip_fps", 8))
    W = int(cfg.get("width", 1920)); H = int(cfg.get("height", 1080))
    FPS = int(cfg.get("fps", 25))

    log(f"= 步骤1/4 音频对齐(FFT互相关, 参考机位#{ref_index}) =")
    offsets = [0.0] * len(videos)
    ref = videos[ref_index]
    ref_wav = os.path.join(WD, "ref.wav")
    subprocess.run([_asm.FF, "-hide_banner", "-y", "-i", ref, "-vn", "-ac", "1",
                    "-ar", "16000", ref_wav], capture_output=True, text=True)
    import numpy as np, wave
    with wave.open(ref_wav, "rb") as w:
        n = w.getnframes()
        ref_data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768
    for ci, cam in enumerate(videos):
        if ci == ref_index:
            continue
        wav = os.path.join(WD, f"cam{ci}.wav")
        subprocess.run([_asm.FF, "-hide_banner", "-y", "-i", cam, "-vn", "-ac", "1",
                        "-ar", "16000", wav], capture_output=True, text=True)
        with wave.open(wav, "rb") as w:
            n2 = w.getnframes()
            d2 = np.frombuffer(w.readframes(n2), dtype=np.int16).astype(np.float32) / 32768
        # FFT 互相关求时延
        N = min(len(ref_data), len(d2))
        a = ref_data[:N]; b = d2[:N]
        fa = np.fft.rfft(a); fb = np.fft.rfft(b)
        cc = np.fft.irfft(fa * np.conj(fb))
        lag = int(np.argmax(np.abs(cc)))
        if lag > N // 2:
            lag -= N
        offsets[ci] = round(lag / 16000.0, 3)
        log(f"  机位{ci} 偏移 {offsets[ci]:+.3f}s")
    log(f"  偏移表: {offsets}")

    log("= 步骤2/4 逐段说话人校验(SyncNet唇动-音频) =")
    cutpoints = {"cameras": videos, "segments": segments}
    report = _asm.verify_segments(cutpoints, workdir=os.path.join(WD, "frames"))
    ok = sum(1 for r in report if r["original_ok"])
    log(f"  校验 {len(report)} 段: 原切正确 {ok} 段, 建议修正 {len(report) - ok} 段")

    log("= 步骤3/4 生成修正段落表 =")
    corrected = []
    for r in report:
        # 推荐机位映射回完整路径
        best_path = None
        for v in videos:
            if os.path.basename(v) == r["best_cam"]:
                best_path = v
                break
        if best_path is None:
            best_path = segments[r["index"]]["src"]
        seg = {
            "index": r["index"], "src": best_path,
            "t0": r["t0"], "t1": r["t1"],
            "note": r["note"], "score": r["best_score"],
            "original_ok": r["original_ok"],
        }
        corrected.append(seg)
        mark = "OK " if r["original_ok"] else "FIX"
        log(f"  [{r['index']:02d}] {mark} {os.path.basename(best_path)} "
            f"t={r['t0']:.2f}-{r['t1']:.2f}s {r['note']} (score={r['best_score']:.3f})")

    map_path = os.path.join(WD, "corrected_segments.json")
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump({"offsets": offsets, "segments": corrected}, f, ensure_ascii=False, indent=1)
    log(f"  修正表 -> {map_path}")

    log("= 步骤4/4 渲染(裁切+拼接+字幕) =")
    seg_files = []
    audio_files = []   # 每段对白音频, 最后拼接进成品
    for i, seg in enumerate(corrected):
        out = os.path.join(WD, f"seg{i:02d}.mp4")
        dur = max(0.3, seg["t1"] - seg["t0"])
        # 人物特写智能筛选: 说话人脸足够大/清晰 -> 特写, 否则回退全景
        crop = _render_closeup(seg, seg["src"], W, H, FPS, out, _asm.FF)
        if crop is None:
            _render_static(seg, seg["src"], W, H, FPS, out, _asm.FF)
            mode = "全景"
            crop = None
        else:
            mode = f"特写(crop {crop['cw']}x{crop['ch']}@{crop['x']},{crop['y']})"
        seg["mode"] = mode
        log(f"  seg{i:02d} [{mode}] ({dur:.2f}s, {os.path.basename(seg['src'])})")
        seg_files.append(out)
        # 每段对白音频(与视频同源同时窗, 保证音画同步), 统一 aac/44100 便于 concat
        aseg = os.path.join(WD, f"sega{i:02d}.m4a")
        _ra = subprocess.run([_asm.FF, "-hide_banner", "-y", "-ss", f"{seg['t0']:.3f}", "-t", f"{dur:.3f}",
                              "-i", seg["src"], "-vn", "-ac", "2", "-ar", "44100",
                              "-c:a", "aac", "-b:a", "192k", aseg],
                             capture_output=True, text=True)
        if _ra.returncode != 0:
            raise RuntimeError(f"[FFERR] audio seg{i:02d}\n{_ra.stderr[-1500:]}")
        audio_files.append(aseg)

    # 渲染后回写含取景(mode/特写裁剪)的修正表, 供时间线导入/复查
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump({"offsets": offsets, "segments": corrected}, f, ensure_ascii=False, indent=1)

    concat_txt = os.path.join(WD, "concat.txt")
    with open(concat_txt, "w", encoding="utf-8") as f:
        for p in seg_files:
            f.write(f"file '{p.replace(chr(92), '/')}'\n")
    joined = os.path.join(WD, "joined.mp4")
    r = subprocess.run([_asm.FF, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
                        "-i", concat_txt, "-c", "copy", joined],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"[FFERR] concat\n{r.stderr[-2500:]}")

    # 拼接所有段对白音频, 成品必须带音轨(整片为对白驱动)
    aconcat_txt = os.path.join(WD, "aconcat.txt")
    with open(aconcat_txt, "w", encoding="utf-8") as f:
        for p in audio_files:
            f.write(f"file '{p.replace(chr(92), '/')}'\n")
    audio_total = os.path.join(WD, "audio_total.m4a")
    _ra = subprocess.run([_asm.FF, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
                          "-i", aconcat_txt, "-c", "copy", audio_total],
                         capture_output=True, text=True)
    if _ra.returncode != 0:
        raise RuntimeError(f"[FFERR] aconcat\n{_ra.stderr[-2000:]}")
    log(f"  对白音频拼接 -> {os.path.basename(audio_total)} ({len(audio_files)}段)")

    srt = cfg.get("subtitle_srt") or ""
    if srt and os.path.isfile(srt):
        import shutil
        srt_local = os.path.join(WD, os.path.basename(srt))
        shutil.copy(srt, srt_local)
        subprocess.run([_asm.FF, "-hide_banner", "-y", "-i", joined, "-i", audio_total,
                        "-vf", f"subtitles={os.path.basename(srt_local)}:force_style=FontName=Microsoft YaHei\\,FontSize=16\\,Outline=1\\,Shadow=0\\,MarginV=32",
                        "-map", "0:v", "-map", "1:a",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-c:a", "copy",
                        OUTPUT], cwd=WD, capture_output=True, text=True)
    else:
        subprocess.run([_asm.FF, "-hide_banner", "-y", "-i", joined, "-i", audio_total,
                        "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "copy",
                        "-movflags", "+faststart", OUTPUT],
                       capture_output=True, text=True)
    log(f"= DONE: {OUTPUT} =")

    return {"offsets": offsets, "corrected_count": len(corrected),
            "original_ok": ok, "map": map_path, "output": OUTPUT}
