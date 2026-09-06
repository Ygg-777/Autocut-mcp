# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""智能精简内核 (Descript/SmartCut 式“一键清理”的离线实现)。

供 templates/auto_edit.py 与 MCP auto_edit 复用, 依赖只有 ffmpeg:
  * detect_silences         silencedetect 定位静音区间
  * collect_filler_intervals 基于【词级】字幕段删语气词/填充词(um/uh/嗯/啊…)
  * removed_to_kept        删除区间 -> 保留区间(剪辑蓝图)
  * render_kept            按保留区间裁切拼接渲染成片
  * remap_subtitles        字幕随剪辑“重排时间轴”(fragua: timings belong to the edit)
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import imageio_ffmpeg

_FF = imageio_ffmpeg.get_ffmpeg_exe()


def probe_duration(path: str) -> float:
    r = subprocess.run([_FF, "-hide_banner", "-i", path, "-f", "null", "-"],
                       capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "Duration:" in line:
            try:
                raw = line.split("Duration:")[1].split(",")[0].strip()
                hh, mm, ss = raw.split(":")
                return int(hh) * 3600 + int(mm) * 60 + float(ss)
            except Exception:
                continue
    raise RuntimeError("无法读取时长: %s" % path)


# ---------------------------------------------------------------- 区间工具
def merge_intervals(ivs, tol=0.0):
    out = []
    for s, e in sorted((float(a), float(b)) for a, b in ivs if b > a):
        if out and s <= out[-1][1] + tol:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def removed_to_kept(total: float, removed, min_keep: float = 0.0):
    """删除区间 -> 保留区间列表 [(t0,t1), ...]。min_keep 过滤过短保留段。"""
    rem = merge_intervals(removed)
    kept, cur = [], 0.0
    for s, e in rem:
        if s > cur + 0.02:
            kept.append((cur, s))
        cur = max(cur, e)
    if cur < total - 0.02:
        kept.append((cur, total))
    if min_keep and min_keep > 0:
        kept = [(a, b) for a, b in kept if b - a >= min_keep]
    return kept


def detect_silences(path: str, threshold_db: float = -35.0,
                    min_silence: float = 0.3):
    """返回 (src_dur, silences[(start,end)])。"""
    src_dur = probe_duration(path)
    r = subprocess.run([_FF, "-hide_banner", "-i", path,
                        "-af", "silencedetect=noise=%gdB:d=%.2f" % (threshold_db, min_silence),
                        "-f", "null", "-"], capture_output=True, text=True)
    silences, cur = [], None
    for line in r.stderr.splitlines():
        if "silence_start:" in line:
            try:
                cur = float(line.split("silence_start:")[1].split()[0])
            except Exception:
                pass
        elif "silence_end:" in line and cur is not None:
            try:
                silences.append((cur, float(line.split("silence_end:")[1].split()[0])))
            except Exception:
                pass
            cur = None
    # 去掉结尾无意义静音
    silences = [(s, e) for s, e in silences if s < src_dur - min_silence]
    return src_dur, merge_intervals(silences)


# ---------------------------------------------------------------- 语气词删除
_WORD_RE = re.compile(r"[\w\u4e00-\u9fff]+")


def _norm_token(s: str) -> str:
    return "".join(_WORD_RE.findall(str(s).lower()))


def collect_filler_intervals(segments, fillers: str, pad: float = 0.05):
    """segments(词级, 含 words:[{word,start,end}]) -> 命中语气词的时间区间。
    无词级时间戳时返回空(只做静音清理), 不猜测时间。"""
    filler_set = {_norm_token(x) for x in re.split(r"[|,，、;；\s]+", str(fillers)) if _norm_token(x)}
    hits = []
    for seg in segments or []:
        words = seg.get("words") if isinstance(seg, dict) else None
        if not words:
            continue
        for w in words:
            if _norm_token(w.get("word")) in filler_set:
                try:
                    hits.append((float(w["start"]) - float(pad), float(w["end"]) + float(pad)))
                except (TypeError, ValueError, KeyError):
                    continue
    return merge_intervals(hits)


# ---------------------------------------------------------------- 字幕重排
def remap_subtitles(segments, kept):
    """把原字幕按保留区间映射到成片时间轴。
    返回新字幕段 [{t0,t1,text}]; 被删掉的句子片段自动丢弃, 跨界句子被切分。"""
    out, cur = [], 0.0
    for a, b in kept:
        for seg in segments or []:
            s, e = float(seg["t0"]), float(seg["t1"])
            ov0, ov1 = max(s, a), min(e, b)
            if ov1 - ov0 >= 0.12:            # 至少保留 0.12s 才输出
                out.append({
                    "t0": round(cur + (ov0 - a), 3),
                    "t1": round(cur + (ov1 - a), 3),
                    "text": str(seg.get("text") or ""),
                })
        cur += b - a
    # 相邻同文本合并(被删除段切成两半时恢复)
    merged = []
    for x in out:
        if merged and abs(x["t0"] - merged[-1]["t1"]) < 0.02 and merged[-1]["text"] == x["text"]:
            merged[-1]["t1"] = x["t1"]
        else:
            merged.append(dict(x))
    return merged


# ---------------------------------------------------------------- 渲染
def render_kept(src: str, kept, output: str, width: int = 1920, height: int = 1080,
                fps: int = 25, log=print):
    """把保留区间按顺序裁切拼接渲染成片(复用多轨时间线引擎的纯时序路径)。"""
    if not kept:
        raise RuntimeError("没有可保留的区间(全部被删除?)")
    clips, t = [], 0.0
    for a, b in kept:
        dur = b - a
        clips.append({"src": src, "start": round(t, 3), "dur": round(dur, 3),
                      "src_in": round(a, 3), "x": 0, "y": 0,
                      "scale": 1.0, "opacity": 1.0})
        t += dur
    tl = {"width": int(width), "height": int(height), "fps": int(fps),
          "tracks": [{"id": "V1", "type": "video", "clips": clips}]}
    from autocut_render import run_render
    res = run_render(tl, output)
    res["kept_segments"] = len(kept)
    res["src_dur_s"] = round(float(kept[-1][1] if kept else 0) + (0.0), 3)
    return res


def copy_media(src, output, log=print):
    r = subprocess.run([_FF, "-hide_banner", "-loglevel", "error", "-y",
                        "-i", src, "-c", "copy", output], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("复制失败: " + (r.stderr or "")[-800:])
    return {"mode": "copy", "output": output}


def load_segments_json(path_or_text):
    """读取 engine/segments 兼容的 segments JSON(文件或字符串)。"""
    s = path_or_text
    if isinstance(s, str) and os.path.isfile(s):
        with open(s, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.loads(s if isinstance(s, str) else json.dumps(s))
    if isinstance(data, dict):
        data = data.get("segments") or []
    return data
