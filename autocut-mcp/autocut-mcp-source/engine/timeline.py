# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""多轨时间线合成模板 (可视化多轨编辑器后端)
  视频: 多条视频轨, 每条轨按时间叠加; 主轨(V1)铺满, 其余轨为画中画,
        支持位置(x,y,0-1归一化)、缩放(scale)、透明度(opacity)、循环(dur)。
  音频: 多轨 amix 混合, 支持音量(volume)、淡入(fade_in)、淡出(fade_out)、延时(start)。

timeline JSON 示例:
{
  "width": 1920, "height": 1080, "fps": 25,
  "tracks": [
    {"id": "V1", "type": "video", "clips": [
       {"src": "d:/a.mp4", "start": 0, "dur": 5, "x": 0, "y": 0,
        "scale": 1.0, "opacity": 1.0, "audio": true}
    ]},
    {"id": "A1", "type": "audio", "clips": [
       {"src": "d:/bgm.mp3", "start": 0, "dur": 4, "volume": 0.5,
        "fade_in": 1.0, "fade_out": 1.0}
    ]}
  ]
}
"""
import os
import subprocess

META = {
    "id": "timeline",
    "name": "多轨时间线合成",
    "description": "可视化多轨编辑器: 多视频轨(画中画/位置/缩放/透明度) + 多音频轨(音量/淡入淡出)",
}

SCHEMA = [
    {"key": "timeline_json", "label": "时间线JSON", "type": "text",
     "help": "前端多轨编辑器导出的时间线JSON"},
    {"key": "width", "label": "分辨率宽", "type": "number", "default": 1920},
    {"key": "height", "label": "分辨率高", "type": "number", "default": 1080},
    {"key": "fps", "label": "输出帧率", "type": "number", "default": 25},
    {"key": "output_name", "label": "输出文件名", "type": "text", "default": "timeline_result"},
]


def _esc(s):
    return s


def _fs(c):
    """是否为全屏片段 (x=y=0, scale≈1, opacity≈1)。"""
    return (abs(float(c.get("x", 0.0))) < 1e-6 and abs(float(c.get("y", 0.0))) < 1e-6
            and float(c.get("scale", 1.0)) >= 0.999 and float(c.get("opacity", 1.0)) >= 0.999)


_HAS_AUDIO_CACHE = {}


def _has_audio(src):
    """src 是否含音轨 (进程内缓存; 用 ffmpeg -i 解析, 不依赖 ffprobe)。"""
    if src in _HAS_AUDIO_CACHE:
        return _HAS_AUDIO_CACHE[src]
    import imageio_ffmpeg
    FF = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        r = subprocess.run([FF, "-hide_banner", "-i", src], capture_output=True, text=True)
        out = (r.stderr or "") + (r.stdout or "")
        ok = "Audio:" in out
    except Exception:
        ok = False
    _HAS_AUDIO_CACHE[src] = ok
    return ok


def _build_track_stream(W, H, fps, total_len, parts_v, ti, clips):
    """合成单条视频轨 -> 返回其流标签。
    若轨内全部全屏、按时间非重叠(允许间隙) → 用 concat(黑填充间隙),
    避免大量 overlay 层导致的内存爆炸; 否则按片段 pad + 轨内 overlay。"""
    lab = f"vt{ti}"

    def fullscreen(m):
        return _fs(m)

    lclips = sorted(clips, key=lambda s: s[2])  # by start
    if all(fullscreen(c) for (_, c, _, _) in lclips):
        ok = True
        cur = 0.0
        for (_, _, start, dur) in lclips:
            if start < cur - 0.02:
                ok = False
                break
            cur = max(cur, start + dur)
        if ok:
            segs = []
            n = 0
            cur = 0.0
            for (src, c, start, dur) in lclips:
                gap = start - cur
                if gap > 0.02:
                    parts_v.append(
                        f"color=c=black:s={W}x{H}:r={fps},trim=duration={gap:.3f},"
                        f"setpts=PTS-STARTPTS,fps={fps},settb=AVTB,setsar=1[bf{n}]")
                    segs.append(f"[bf{n}]"); n += 1
                sin = float(c.get("src_in", 0.0))
                parts_v.append(
                    f"[{src}:v]trim=start={sin:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS,"
                    f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                    f"format=yuv420p,fps={fps},settb=AVTB,setsar=1[cf{n}]")
                segs.append(f"[cf{n}]"); n += 1
                cur = start + dur
            parts_v.append("".join(segs) + f"concat=n={n}:v=1:a=0[{lab}]")
            return lab

    # 兜底: 轨内逐片段 pad(铺到绝对时间) 再轨内 overlay
    subs = []
    for (src, c, start, dur) in lclips:
        scc = max(0.001, float(c.get("scale", 1.0)))
        x = float(c.get("x", 0.0)); y = float(c.get("y", 0.0))
        op = max(0.0, min(float(c.get("opacity", 1.0)), 1.0))
        tw = max(32, int(W * scc)); th = max(32, int(H * scc))
        px = int(x * W); py = int(y * H)
        sin = float(c.get("src_in", 0.0))
        tag = f"pt{ti}_{len(subs)}"
        chain = (f"[{src}:v]trim=start={sin:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS,"
                 f"scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},format=rgba" +
                 (f",colorchannelmixer=aa={op:.3f}" if op < 1.0 else "") +
                 (f",tpad=start_duration={start:.3f}" if start > 0 else "") +
                 (f",tpad=stop_duration={total_len - start - dur:.3f}" if total_len > start + dur else "") +
                 f",pad=w={W}:h={H}:x={px}:y={py}:color=black@0,format=yuv420p,fps={fps}[{tag}]")
        parts_v.append(chain); subs.append(tag)
    base = subs[0]
    for i in range(1, len(subs)):
        nn = len(parts_v)
        parts_v.append(f"[{base}][{subs[i]}]overlay=0:0:eof_action=pass:format=auto[v{nn}]")
        base = f"v{nn}"
    parts_v.append(f"[{base}]format=yuv420p[{lab}]")
    return lab


def _build_filter(av, tl):
    """构建 ffmpeg filter_complex。返回 (inputs, filtergraph, has_audio)。"""
    W = int(tl.get("width", 1920)); H = int(tl.get("height", 1080))
    fps = int(tl.get("fps", 25))
    tracks = tl.get("tracks", [])

    # ---- 输入去重: 相同 (src, src_in) 只输入一次
    in_idx = {}
    real_inputs = []
    clip_idx = []   # strip_map 平行 -> 实际输入号
    for tr in tracks:
        for c in tr.get("clips", []):
            src_in = c.get("src_in", 0.0)
            key = (c["src"], round(src_in, 4))
            if key not in in_idx:
                in_idx[key] = len(real_inputs)
                real_inputs.append(c["src"])
            clip_idx.append(in_idx[key])

    parts_v = []   # filter 片段(视频链)
    parts_a = []   # filter 片段(音频链)
    vid_tracks = []  # 按轨收集视频 (src_tag, c, start, dur)
    aud_labels = []  # 每个音频流(延时+音量)标签
    nclip = 0
    total_len = 0.0

    k = 0
    for tr in tracks:
        tr_type = tr["type"]
        vclips = []
        for c in tr.get("clips", []):
            in_i = clip_idx[k]; k += 1
            src_tag = f"{in_i}"
            start = float(c.get("start", 0.0))
            dur = float(c.get("dur", 1.0))
            total_len = max(total_len, start + dur)

            src_in = float(c.get("src_in", 0.0))
            if tr_type == "video":
                vclips.append((src_tag, c, start, dur))
                # 视频片段自动携带源音轨(可在 clip 里设 audio=false 关闭)
                if c.get("audio", True) is not False and _has_audio(c["src"]):
                    alab = "ac%d" % nclip
                    nclip += 1
                    vol = float(c.get("volume", 1.0))
                    a = ("[%s:a]atrim=start=%.3f:duration=%.3f,asetpts=PTS-STARTPTS"
                         ",volume=%.3f" % (src_tag, src_in, dur, vol))
                    if start > 0:
                        a += ",adelay=%d" % int(start * 1000)
                    a += "[%s]" % alab
                    parts_a.append(a)
                    aud_labels.append(alab)
            else:  # audio
                alab = f"ac{nclip}"
                vol = float(c.get("volume", 1.0))
                fin = float(c.get("fade_in", 0.0))
                fout = float(c.get("fade_out", 0.0))
                a = f"[{src_tag}:a]atrim=start={src_in:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS"
                if fin > 0:
                    a += f",afade=t=in:st=0:d={fin:.3f}"
                if fout > 0:
                    a += f",afade=t=out:st={max(0, dur - fout):.3f}:d={fout:.3f}"
                a += f",volume={vol:.3f}"
                if start > 0:
                    a += f",adelay={int(start * 1000)}"
                a += f"[{alab}]"
                parts_a.append(a)
                aud_labels.append(alab)
                nclip += 1
        if vclips:
            vid_tracks.append(vclips)

    # ---- 视频: 逐轨合成(concat/pad)后按主轨在下、其余轨在上依次 overlay
    if vid_tracks:
        vstreams = []
        for ti, clips in enumerate(vid_tracks):
            vstreams.append(_build_track_stream(W, H, fps, total_len, parts_v, ti, clips))
        base = vstreams[0]
        for lab in vstreams[1:]:
            nn = len(parts_v)
            parts_v.append(f"[{base}][{lab}]overlay=0:0:eof_action=pass:format=auto[v{nn}]")
            base = f"v{nn}"
        # 保证总长度 = 时间线长度
        parts_v.append(f"[{base}]trim=duration={total_len:.3f},setpts=PTS-STARTPTS,fps={fps}[vout]")
    else:
        # 无视频: 铺纯黑
        parts_v.append(f"color=c=black:s={W}x{H}:r={fps}[vout]")

    # ---- 音频: amix 混合所有音频流
    if aud_labels:
        if len(aud_labels) == 1:
            parts_a.append(f"[{aud_labels[0]}]anull[aout]")
        else:
            args = "".join(f"[{l}]" for l in aud_labels) + f"amix=inputs={len(aud_labels)}:normalize=0[aout]"
            parts_a.append(args)
        has_audio = True
    else:
        has_audio = False

    filtergraph = ";".join(parts_v) + (";" + ";".join(parts_a) if has_audio else "")

    return real_inputs, filtergraph, has_audio


def _is_pure_sequential(tl):
    """是否为纯时序主轨: 恰一条视频轨、全部全屏、按时间非重叠(允许间隙)。
    这类时间线(自动剪辑导入)用"分段渲染+拼接"方式, 峰值内存极低。"""
    vids = [t for t in tl["tracks"] if t["type"] == "video"]
    if len(vids) != 1:
        return False
    clips = vids[0]["clips"]
    if not clips or not all(_fs(c) for c in clips):
        return False
    sc = sorted(clips, key=lambda c: c["start"])
    cur = 0.0
    for c in sc:
        if c["start"] < cur - 0.02:
            return False
        cur = max(cur, c["start"] + c["dur"])
    return True


def _audio_only_filter(tl):
    """构建纯音频 amix filtergraph。返回 (inputs, filtergraph)。
    音频来源 = 音频轨片段 + 视频轨片段自带的源音轨(文件无音轨则自动跳过)。"""
    parts_a, labels, items = [], [], []
    for tr in tl.get("tracks", []):
        for c in tr.get("clips", []):
            if tr["type"] == "audio":
                items.append({"src": c["src"], "src_in": c.get("src_in", 0.0),
                              "dur": c.get("dur", 1.0), "start": c.get("start", 0.0),
                              "volume": c.get("volume", 1.0),
                              "fin": c.get("fade_in", 0.0), "fout": c.get("fade_out", 0.0)})
            elif tr["type"] == "video" and c.get("audio", True) is not False:
                items.append({"src": c["src"], "src_in": c.get("src_in", 0.0),
                              "dur": c.get("dur", 1.0), "start": c.get("start", 0.0),
                              "volume": 1.0, "fin": 0.0, "fout": 0.0})
    uniq, key2idx = [], {}
    for it in items:
        if not _has_audio(it["src"]):
            continue
        key = (it["src"], round(float(it["src_in"]), 4))
        if key not in key2idx:
            key2idx[key] = len(uniq)
            uniq.append(it["src"])
        src_in = float(it["src_in"]); dur = float(it["dur"]); start = float(it["start"])
        vol = float(it["volume"]); fin = float(it["fin"]); fout = float(it["fout"])
        alab = "ai%d" % len(labels)
        a = "[%d:a]atrim=start=%.3f:duration=%.3f,asetpts=PTS-STARTPTS" % (key2idx[key], src_in, dur)
        if fin > 0:
            a += ",afade=t=in:st=0:d=%.3f" % fin
        if fout > 0:
            a += ",afade=t=out:st=%.3f:d=%.3f" % (max(0, dur - fout), fout)
        a += ",volume=%.3f" % vol
        if start > 0:
            a += ",adelay=%d" % int(start * 1000)
        a += "[%s]" % alab
        parts_a.append(a)
        labels.append(alab)
    if not labels:
        return [], ""
    if len(labels) == 1:
        end = "[%s]anull[aout]" % labels[0]
    else:
        end = "".join("[%s]" % l for l in labels) + "amix=inputs=%d:normalize=0[aout]" % len(labels)
    return uniq, ";".join(parts_a) + ";" + end


def _run_sequential(FF, WD, OUTPUT, tl, W, H, fps, log):
    """两段式渲染: 逐段裁切(每次仅解码1个输入) → concat demuxer 拼视频 → 混音 → 封装。"""
    vclips = sorted([t for t in tl["tracks"] if t["type"] == "video"][0]["clips"],
                    key=lambda c: c["start"])
    parts, idx, cur = [], 0, 0.0
    for c in vclips:
        gap = c["start"] - cur
        if gap > 0.02:
            pf = os.path.join(WD, f"part{idx:03d}.mp4"); idx += 1
            r = subprocess.run([FF, "-hide_banner", "-y", "-f", "lavfi", "-i",
                                f"color=c=black:s={W}x{H}:r={fps}:d={gap:.3f}",
                                "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                                "-pix_fmt", "yuv420p", "-video_track_timescale", "13824",
                                "-an", pf], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError("gap render failed: " + r.stderr[-800:])
            parts.append(pf)
        pf = os.path.join(WD, f"part{idx:03d}.mp4"); idx += 1
        sin = float(c.get("src_in", 0.0)); dur = float(c.get("dur", 1.0))
        log(f"  段 {len(parts)}: {os.path.basename(c['src'])} in={sin:.2f}+{dur:.2f}s")
        vf = (f"trim=start={sin:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS,"
              f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"fps={fps},setsar=1,format=yuv420p")
        r = subprocess.run([FF, "-hide_banner", "-y", "-i", c["src"], "-vf", vf,
                            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                            "-video_track_timescale", "13824", "-an", pf],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("seg render failed: " + r.stderr[-800:])
        parts.append(pf)
        cur = c["start"] + dur

    lst = os.path.join(WD, "alist.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '" + p.replace("\\", "/") + "'\n")
    vidc = os.path.join(WD, "video_total.mp4")
    log(f"  拼接 {len(parts)} 个片段 (concat demuxer, -c copy)")
    r = subprocess.run([FF, "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                        "-c", "copy", "-video_track_timescale", "13824", vidc],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("concat failed: " + r.stderr[-800:])

    ainputs, afg = _audio_only_filter(tl)
    if ainputs:
        audw = os.path.join(WD, "audio_total.m4a")
        log(f"  混音 {len(ainputs)} 路音频 (amix)")
        cmd = [FF, "-hide_banner", "-y"]
        for s in ainputs:
            cmd += ["-i", s]
        cmd += ["-filter_complex", afg, "-map", "[aout]", "-c:a", "aac", "-b:a", "192k", audw]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("audio mix failed: " + r.stderr[-800:])
        r = subprocess.run([FF, "-hide_banner", "-y", "-i", vidc, "-i", audw,
                            "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                            "-c:a", "aac", "-b:a", "192k", "-shortest", OUTPUT],
                           capture_output=True, text=True)
    else:
        r = subprocess.run([FF, "-hide_banner", "-y", "-i", vidc, "-c", "copy", OUTPUT],
                           capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("mux failed: " + r.stderr[-800:])
    return {"mode": "sequential_concat", "segments": len(vclips), "output": OUTPUT}


def run(cfg, log=print):
    import json
    import imageio_ffmpeg
    FF = imageio_ffmpeg.get_ffmpeg_exe()
    WD = cfg["workdir"]
    OUTPUT = cfg["output"]
    os.makedirs(WD, exist_ok=True)

    tl = cfg.get("timeline_json")
    if isinstance(tl, str):
        tl = json.loads(tl)
    if not isinstance(tl, dict) or not tl.get("tracks"):
        raise RuntimeError("时间线JSON无效或为空")

    W = int(cfg.get("width", 1920)); H = int(cfg.get("height", 1080))
    fps = int(cfg.get("fps", 25))

    # 纯时序主轨 → 低内存两段式渲染
    if _is_pure_sequential(tl):
        log("纯时序主轨, 采用低内存两段式渲染(逐段+拼接)")
        return _run_sequential(FF, WD, OUTPUT, tl, W, H, fps, log)

    inputs, filtergraph, has_audio = _build_filter(None, tl)
    if not inputs:
        # 空时间线则产出单帧黑
        subprocess.run([FF, "-hide_banner", "-y", "-f", "lavfi", "-i",
                        f"color=c=black:s={W}x{H}:r={fps}:d=1",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", OUTPUT],
                       capture_output=True, text=True)
        log("= DONE (空时间线, 单帧黑) =")
        return {"input_count": 0, "output": OUTPUT}

    cmd = [FF, "-hide_banner", "-y"]
    for src in inputs:
        cmd += ["-i", src]
    cmd += ["-filter_complex", filtergraph, "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "[aout]"]
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-video_track_timescale", "13824",
            "-shortest", OUTPUT]

    log("多轨合成")
    log(f"  inputs={len(inputs)}  buildfilter done")
    # 写 graph 便于调试
    gpath = os.path.join(WD, "timeline_filtergraph.txt")
    with open(gpath, "w", encoding="utf-8") as f:
        f.write(filtergraph)
    log(f"  filtergraph -> {gpath}")

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"[FFERR] timeline\n{r.stderr[-3000:]}")
    log(f"= DONE: {OUTPUT} =")
    return {"input_count": len(inputs), "width": W, "height": H, "output": OUTPUT}