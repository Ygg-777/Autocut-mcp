# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""文本/字幕片段核心层 (零第三方依赖)。

AutoCut 系工具(参考 mli/autocut、FunClip、auto-editor)的共同架构是:
  视频 --ASR--> 带时间戳的文本片段(segments) --文本筛选--> 保留段 --ffmpeg--> 成片
即把「字幕/文本片段」当作剪辑的中间表示, 本模块就是这层。

提供:
  * SRT 解析/写出 (健壮: BOM、逗号/点小数、多行文本、缺失序号、错误行跳过)
  * segments 的 JSON / SRT / ASS 互转 (ASS 用于 libass 烧录进画面)
  * 文本驱动的选段规划 (keep/drop + 关键词/正则/索引, 边界 padding, 重叠合并, 最短时长过滤)
  * 由保留段直接生成可交给 render_timeline 的 timeline JSON

segments 元素结构:
  {"src": 源视频(可选), "t0": 秒, "t1": 秒, "text": str, "speaker": 可选str, "words": 可选}
"""
from __future__ import annotations

import json
import os
import re
from typing import Iterable, Optional, Tuple

# ================================================================ 时间
_SRT_SPLIT = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})")


def ts_to_seconds(ts) -> float:
    """字幕时间戳 -> 秒。支持 'HH:MM:SS,mmm' 'MM:SS.mmm' 'SS.xxx' 及纯数字。"""
    if ts is None:
        raise ValueError("空时间戳")
    s = str(ts).strip().replace("，", ",")
    if not s:
        raise ValueError("空时间戳")
    if ":" in s:
        m = _SRT_SPLIT.search(s)
        if not m:
            raise ValueError("无法解析时间戳: %r" % s)
        hh, mm, ss, ms = m.groups()
        frac = float("0." + ms) if ms else 0.0
        return int(hh or 0) * 3600 + int(mm) * 60 + int(ss) + frac
    return float(s)


def seconds_to_srt_ts(sec: float) -> str:
    """秒 -> 'HH:MM:SS,mmm' (SRT/ASS 通用前段)。"""
    sec = max(0.0, float(sec))
    hh = int(sec // 3600)
    mm = int((sec % 3600) // 60)
    ss = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        ms -= 1000
        ss += 1
        if ss >= 60:
            ss -= 60
            mm += 1
            if mm >= 60:
                mm -= 60
                hh += 1
    return "%02d:%02d:%02d,%03d" % (hh, mm, ss, ms)


def seconds_to_ass_ts(sec: float) -> str:
    """秒 -> 'H:MM:SS.cc' (ASS 时间格式, 百分之一秒)。"""
    s = seconds_to_srt_ts(sec)          # HH:MM:SS,mmm
    hh, rest = s.split(":", 1)
    mm, ss_ms = rest.split(":", 1)
    ss, mmm = ss_ms.split(",")
    cs = str(round(int(mmm) / 10)).zfill(2)
    return "%d:%s:%s.%s" % (int(hh), mm, ss, cs)


# ================================================================ SRT
def parse_srt(text: str) -> list:
    """解析 SRT 文本 -> [{index,start,end,text}]。容错: BOM、乱序号、错误块自动跳过。"""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    entries = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        idx = None
        if lines[0].isdigit():
            idx = int(lines[0])
            lines = lines[1:]
        if not lines:
            continue
        m = re.match(r"^(.*?)\s*-->\s*(.*)$", lines[0])
        if not m:
            continue
        try:
            start = ts_to_seconds(m.group(1))
            end = ts_to_seconds(m.group(2))
        except ValueError:
            continue
        if end <= start:
            continue
        entries.append({"index": idx, "start": start, "end": end,
                        "text": "\n".join(lines[1:])})
    return entries


def read_srt(path) -> list:
    """读取 SRT 文件 -> entries (utf-8 / utf-8-sig / gbk 自动尝试)。"""
    if not os.path.isfile(path):
        raise FileNotFoundError("字幕文件不存在: %s" % path)
    raw = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                raw = f.read()
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if raw is None:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    return parse_srt(raw)


def build_srt(entries) -> str:
    """entries -> SRT 文本。自动重新编号。"""
    out = []
    for i, e in enumerate(entries, 1):
        out.append("%d\n%s --> %s\n%s\n" % (
            i, seconds_to_srt_ts(e["start"]), seconds_to_srt_ts(e["end"]), e["text"]))
    return "\n".join(out)


def write_srt(entries, path) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    text = build_srt(entries)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return text


# ================================================================ segments
def normalize_segments(segments) -> list:
    """统一 segments 字段; 丢弃无效段。"""
    out = []
    for s in segments or []:
        if not isinstance(s, dict):
            continue
        try:
            t0 = float(s.get("t0", 0.0))
            t1 = float(s.get("t1", t0))
        except (TypeError, ValueError):
            continue
        if t1 <= t0:
            continue
        out.append({
            "src": s.get("src"),
            "t0": round(t0, 3),
            "t1": round(t1, 3),
            "text": str(s.get("text") or ""),
            "speaker": s.get("speaker"),
            "words": s.get("words"),
        })
    return out


def segments_from_srt(path, src=None) -> list:
    """SRT 文件 -> segments 列表。"""
    return normalize_segments([
        {"src": src, "t0": e["start"], "t1": e["end"], "text": e["text"]}
        for e in read_srt(path)])


def segments_to_srt_text(segments) -> str:
    return build_srt([{"start": s["t0"], "end": s["t1"], "text": s["text"]}
                      for s in normalize_segments(segments)])


def segments_from_json(text_or_path) -> list:
    """接受 JSON 字符串或文件路径。"""
    s = text_or_path
    if isinstance(s, str) and os.path.isfile(s):
        with open(s, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.loads(s if isinstance(s, str) else json.dumps(s))
    if isinstance(data, dict):
        data = data.get("segments") or data.get("items") or []
    return normalize_segments(data)


# ================================================================ ASS
def segments_to_ass(segments, width=1920, height=1080,
                    fontname="Microsoft YaHei", fontsize=None,
                    primary="#FFFFFF", outline=1.6, shadow=0.8,
                    margin_v=48, alignment=2) -> str:
    """segments -> ASS 字幕文本(可交给 ffmpeg ass 滤镜烧录)。
    样式: 底部居中白字+黑描边, 自动按分辨率缩放字号。"""
    fontsize = fontsize or int(max(32, min(96, height // 22)))
    esc = lambda t: (str(t).replace("\n", "\\N")
                     .replace("{", "(").replace("}", ")")
                     .replace("\\", "\\\\"))
    head = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: %d\nPlayResY: %d\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,%s,%d,&H00%s,&H000000FF,&H00101010,&H80000000,"
        "0,0,0,0,100,100,0,0,1,%s,%s,%d,24,24,%d,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        % (int(width), int(height), fontname, int(fontsize),
           str(primary).lstrip("#").upper(), str(outline), str(shadow),
           int(alignment), int(margin_v)))
    lines = [head]
    for s in normalize_segments(segments):
        lines.append("Dialogue: 0,%s,%s,Default,,0,0,0,,%s" % (
            seconds_to_ass_ts(s["t0"]), seconds_to_ass_ts(s["t1"]), esc(s["text"])))
    return "\n".join(lines)


def write_ass(segments, path, **kw) -> str:
    text = segments_to_ass(segments, **kw)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return text


# ================================================================ 文本选段
def _match_one(seg, match, pattern) -> bool:
    text = str(seg.get("text") or "")
    if match == "keywords":
        return any(k.strip().lower() and k.strip().lower() in text.lower()
                   for k in re.split(r"[|,，、;；]+", pattern) if k.strip())
    if match == "regex":
        return re.search(pattern, text) is not None
    if match == "items":
        wanted = {int(x) for x in re.split(r"[,\s，]+", pattern) if x.strip()}
        return seg.get("_idx") in wanted
    raise ValueError("match 必须是 keywords / regex / items")


def merge_intervals(ivs, tol=0.0) -> list:
    """合并重叠(或间距<=tol)的 (t0,t1) 区间。"""
    ivs = sorted((float(a), float(b)) for a, b in ivs if b > a)
    out = []
    for s, e in ivs:
        if out and s <= out[-1][1] + tol:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def select_segments(segments, *, keep_mode="keep", match="keywords",
                    value="", padding=0.0, min_keep_dur=0.0):
    """文本筛选 -> (kept_clips, dropped_segments, kept_originals)
    keep_mode: 'keep'=保留命中段 / 'drop'=删除命中段
    match: keywords | regex | items(0起索引)
    padding: 命中段前后各扩几秒(合并重叠), 用于剪出口播的呼吸感缓冲
    min_keep_dur: 过滤过短保留区间
    返回 kept_clips 为可直接渲染的 {src,t0,t1,text} 合并区间列表。
    """
    segs = normalize_segments(segments)
    for i, s in enumerate(segs):
        s["_idx"] = i
    hits = [_match_one(s, match, value) for s in segs]
    keep_flags = hits if keep_mode == "keep" else [not h for h in hits]
    kept_orig = [s for s, k in zip(segs, keep_flags) if k]
    dropped = [s for s, k in zip(segs, keep_flags) if not k]
    pad = max(0.0, float(padding))
    ivs = merge_intervals([(s["t0"] - pad, s["t1"] + pad) for s in kept_orig], tol=0.0)
    if min_keep_dur and min_keep_dur > 0:
        ivs = [(a, b) for a, b in ivs if b - a >= float(min_keep_dur)]
    clips = []
    for a, b in ivs:
        txt = " ".join(s["text"] for s in kept_orig
                       if s["t1"] > a and s["t0"] < b).strip()
        clips.append({"src": kept_orig[0].get("src") if kept_orig else None,
                      "t0": round(max(0.0, a), 3), "t1": round(b, 3), "text": txt})
    return clips, dropped, kept_orig


def clips_to_timeline(clips, src=None, width=1920, height=1080, fps=25) -> dict:
    """保留区间 -> 顺序 timeline JSON (供 render_timeline / run_template)。"""
    vclips = []
    t = 0.0
    for c in clips:
        if src is None:
            src = c.get("src")
        if not src:
            raise ValueError("缺少源视频 src")
        dur = float(c["t1"]) - float(c["t0"])
        if dur <= 0:
            continue
        vclips.append({
            "src": src, "start": round(t, 3), "dur": round(dur, 3),
            "src_in": round(float(c["t0"]), 3),
            "x": 0, "y": 0, "scale": 1.0, "opacity": 1.0,
        })
        t += dur
    if not vclips:
        raise ValueError("没有可保留的片段(筛选结果为空)")
    return {"width": int(width), "height": int(height), "fps": int(fps),
            "tracks": [{"id": "V1", "type": "video", "clips": vclips}]}


def make_segments_json(segments) -> str:
    return json.dumps({"segments": normalize_segments(segments)},
                      ensure_ascii=False, indent=2)
