# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""通用自动剪辑 MCP 插件 —— 供任何 agent(MCP client)直接调用。

封装"基础自动剪辑"能力, 不绑定任何特定素材库/脚本/项目记录:
  * probe_media       探测任意媒体文件的时长/分辨率/帧率/编码
  * clip_concat       把多个视频片段按顺序裁剪拼接为一个文件
                       (clips = [{src, src_in, dur}], 每个片段来自任意路径)
  * render_timeline   提交多轨时间线 JSON, 渲染成片
                       (视频: 全屏/画中画/位置/缩放/透明度; 音频: 音量/淡入淡出/延时)
  * extract_audio     提取/裁切任意文件的音频段, 可加音量/淡入淡出
  * transcribe_media  语音识别: 视频/音频 -> SRT 字幕 + 带时间戳片段JSON
  * srt_to_segments   已有 SRT -> segments JSON (文本剪辑中间表示)
  * plan_text_cut     文本驱动剪辑·规划: 按字幕关键词/正则/索引筛段, 产出 timeline JSON
  * text_cut          文本驱动剪辑·成片: 筛段并顺序拼接渲染 (AutoCut 式)
  * burn_subtitles    SRT -> ASS -> ffmpeg libass 硬字幕烧录
  * auto_edit         一键智能成片: 口播精简/仅去静音/卡点混剪(可选套调色预设)
  * apply_color_grade 调色(达芬奇风格): 色温/色调/Lift-Gamma-Gain 色轮/对比度/饱和度/LUT/晕影/颗粒
所有输入均为调用方给出的绝对路径, 插件自身不预设素材库。

技术栈: 官方 MCP SDK 的 FastMCP + 多轨合成引擎(ffmpeg, 由 imageio_ffmpeg 提供)。
运行(stdio, 供 MCP client / harness 连接):
    python autocut_mcp.py
调试(打印注册的工具清单):
    python autocut_mcp.py --test
环境变量(可覆盖默认值, 均非必需):
    AUTOCUT_OUT   渲染输出目录     (默认: <脚本目录>/autocut_output)
    AUTOCUT_RUNS  临时工作目录     (默认: <脚本目录>/autocut_runs)
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid

import imageio_ffmpeg
from engine.segments import (segments_from_srt, select_segments, clips_to_timeline,
                             write_ass, make_segments_json, normalize_segments,
                             write_srt, segments_to_srt_text)
from engine.asr import transcribe as _transcribe_asr
from engine.grade import apply_grade as _apply_grade
from engine.triage import build_report as _triage_build_report
from engine.triage import load_script_pieces as _triage_load_pieces

from mcp.server.fastmcp import FastMCP

# ---------- 目录(仅输出/工作目录, 无任何素材库预设) ----------
_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("AUTOCUT_OUT", os.path.join(_HERE, "autocut_output"))
RUNS_DIR = os.environ.get("AUTOCUT_RUNS", os.path.join(_HERE, "autocut_runs"))

mcp = FastMCP(
    "autocut",
    instructions=(
        "通用自动剪辑工具集。任意 agent 传入具体文件路径即可完成: 媒体探测、"
        "按序裁剪拼接多段视频、多轨时间线合成(画中画/位置/缩放/透明度/音量/淡入淡出)、"
        "音频提取/裁切、语音识别转字幕(transcribe_media)、字幕解析(srt_to_segments)、"
        "文本驱动剪辑(plan_text_cut/text_cut: 按字幕关键词/正则/索引筛段->拼片)、"
        "字幕烧录(burn_subtitles)、一键智能成片(auto_edit: 去静音/删语气词/字幕重排/卡点混剪)、以及运行内置剪辑模板"
        "(去静音/卡点混剪/四分屏/基础拼接/多机位说话人校验)，调色(apply_color_grade：达芬奇风格"
        "预设 + 手动色温/色轮/LUT)。"
        "所有工具均接受调用方给定的绝对路径, 不依赖任何预设素材库。推荐剪辑流程:"
        "transcribe_media 生成字幕 -> plan_text_cut 预览筛选结果 -> text_cut 渲染成片。"
    ),
)


def _ffmpeg():
    return imageio_ffmpeg.get_ffmpeg_exe()


def _ffprobe():
    exe = _ffmpeg()
    d = os.path.dirname(exe)
    cand = os.path.join(d, "ffprobe.exe")
    if os.path.exists(cand):
        return cand
    import shutil
    return shutil.which("ffprobe") or "ffprobe"


def _clean_path(path: str) -> str:
    """校验并规范化绝对路径。"""
    if not path or not isinstance(path, str):
        raise ValueError("path 必须是非空字符串")
    p = path.strip().strip('"').strip("'")
    if not os.path.isfile(p):
        raise FileNotFoundError(f"文件不存在: {p}")
    return os.path.abspath(p)


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"命令失败(code={r.returncode}): {cmd[:4]}...\n{r.stderr[-2000:]}")
    return r


def _time(path: str) -> float:
    r = subprocess.run([_ffprobe(), "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        raise RuntimeError(f"无法读取时长: {path}")


# ---------------------------------------------------------------- 工具定义

@mcp.tool()
def probe_media(path: str) -> str:
    """探测任意媒体(视频/音频)信息: 时长、分辨率、帧率、编码、大小。
    参数 path 为调用方给定的绝对路径。"""
    p = _clean_path(path)
    fp = _ffprobe()
    r = subprocess.run(
        [fp, "-v", "error",
         "-show_entries", "format=duration,size,bit_rate:stream=codec_type,codec_name,width,height,r_frame_rate,channels,sample_rate",
         "-of", "json", p],
        capture_output=True, text=True)
    if r.returncode != 0:
        return json.dumps({"path": p, "error": r.stderr[-500:]}, ensure_ascii=False)
    info = json.loads(r.stdout or "{}")
    info["path"] = p
    info["duration"] = round(_time(p), 3)
    return json.dumps(info, ensure_ascii=False, indent=2)


@mcp.tool()
def clip_concat(clips_json: str, output: str,
                width: int = 1920, height: int = 1080, fps: int = 25) -> str:
    """把若干视频片段按顺序裁剪拼接为一个成片。

    clips_json 形如: [{"src": "<绝对路径>", "src_in": 10.0, "dur": 3.0}, ...]
      src      源视频绝对路径
      src_in   从该视频的第几秒开始取
      dur      截取长度(秒)
    片段之间可按顺序无缝拼接。output 为输出 mp4 的绝对路径。
    拼接采用低内存两段式(逐段裁剪+拼接), 可处理 4K 长素材。
    """
    try:
        clips = json.loads(clips_json)
    except Exception:
        raise ValueError("clips_json 必须是合法 JSON 数组")
    if not isinstance(clips, list) or not clips:
        raise ValueError("clips_json 必须是非空数组")
    for c in clips:
        _clean_path(c["src"])
        c["dur"] = float(c["dur"])

    out = os.path.abspath(output)
    tl = {
        "width": int(width), "height": int(height), "fps": int(fps),
        "tracks": [{"id": "V1", "type": "video", "clips": [
            {"src": c["src"], "start": round(sum(x["dur"] for x in clips[:i]), 3),
             "dur": round(c["dur"], 3), "src_in": round(float(c.get("src_in", 0)), 3),
             "x": 0, "y": 0, "scale": 1.0, "opacity": 1.0}
            for i, c in enumerate(clips)
        ]}],
    }
    return render_timeline(json.dumps(tl, ensure_ascii=False), output=out)
    # NOTE: render_timeline 内部已负责 workdir/渲染; 上述调用以 stdout 返回结果


@mcp.tool()
def render_timeline(timeline_json: str, output: str) -> str:
    """渲染一个多轨时间线为成片 mp4。

    timeline_json 形如:
      {"tracks":[{"type":"video","clips":[
          {"src":"d:/a.mp4","start":0,"dur":5,"src_in":1.0,"x":0,"y":0,"scale":1.0,"opacity":1.0}]},
        {"type":"audio","clips":[
          {"src":"d:/bgm.mp3","start":0,"dur":5,"volume":0.6,"fade_in":1.0,"fade_out":1.0}]}]}
    顶层可选: width,height,fps(默认1920/1080/25)。
    视频剪辑字段: src 绝对路径, start 出场时间, dur 时长, src_in 源内起点,
                 x,y 归一化位置(0-1), scale 缩放, opacity 透明度。全屏=画中画=可选。
    音频剪辑字段: src, start, dur, src_in, volume(0-1), fade_in, fade_out。
    output 为输出 mp4 的绝对路径。"""
    try:
        tl = json.loads(timeline_json) if isinstance(timeline_json, str) else timeline_json
    except Exception:
        raise ValueError("timeline_json 必须是合法 JSON")
    if not isinstance(tl, dict) or not tl.get("tracks"):
        raise ValueError("timeline_json 必须含 tracks")
    for tr in tl["tracks"]:
        for c in tr.get("clips", []):
            _clean_path(c["src"])

    out = os.path.abspath(output)
    from autocut_render import run_render  # 见下方同目录实现, 复用多轨引擎
    res = run_render(tl, out)
    return json.dumps(res, ensure_ascii=False, indent=2)


@mcp.tool()
def extract_audio(input_path: str, src_in: float = 0.0, dur: float | None = None,
                  output: str | None = None, volume: float = 1.0,
                  fade_in: float = 0.0, fade_out: float = 0.0) -> str:
    """从任意媒体中提取一段音频(可裁切/调音量/淡入淡出), 输出 aac/m4a。
    参数:
      input_path  源文件绝对路径
      src_in      提取起点(秒), 默认0
      dur         提取长度(秒); 省略则到文件末尾
      output      输出绝对路径(建议 .m4a); 省略则自动放在输出目录
      volume      音量(0-1, 默认1)
      fade_in/fade_out  淡入/淡出秒数
    """
    import math
    src = _clean_path(input_path)
    total = _time(src)
    src_in = max(0.0, float(src_in))
    if dur is not None:
        dur = min(float(dur), max(0.0, total - src_in))
    else:
        dur = max(0.0, total - src_in)
    if dur <= 0:
        raise ValueError("无效的时长/起点: 段长为0")

    if output is None:
        out = os.path.join(OUT_DIR, f"extract_{os.path.splitext(os.path.basename(src))[0]}.m4a")
    else:
        out = os.path.abspath(output)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    vf = f"atrim=start={src_in:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS"
    if fade_in > 0:
        vf += f",afade=t=in:st=0:d={fade_in:.3f}"
    if fade_out > 0:
        vf += f",afade=t=out:st={max(0.0, dur - fade_out):.3f}:d={fade_out:.3f}"
    vf += f",volume={volume:.3f}"

    _run([_ffmpeg(), "-hide_banner", "-y", "-i", src, "-vn", "-af", vf,
          "-c:a", "aac", "-b:a", "192k", out])
    return json.dumps({
        "output": out, "duration": round(dur, 3),
        "src_abs": src, "src_in": round(src_in, 3), "volume": volume,
    }, ensure_ascii=False, indent=2)



# ---------------------------------------------------------------- 文本/字幕驱动剪辑

def _video_size(path: str):
    """返回 (width,height) 主视频流尺寸。"""
    r = subprocess.run([_ffprobe(), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height",
                        "-of", "csv=s=x:p=0", path], capture_output=True, text=True)
    try:
        w, h = r.stdout.strip().split("x")
        return int(w), int(h)
    except Exception:
        return 1920, 1080


def _plan_text_cut(video: str, srt_path: str, mode: str, match: str, value: str,
                   padding: float, min_keep_dur: float, width: int, height: int, fps: int):
    """文本筛选规划 -> (plan_dict, timeline_or_None)。"""
    v = _clean_path(video)
    if not os.path.isfile(srt_path):
        raise FileNotFoundError("字幕文件不存在: %s" % srt_path)
    if not str(value or "").strip():
        raise ValueError("value 不能为空 (关键词/正则/索引列表)")
    segs = segments_from_srt(srt_path, src=v)
    if not segs:
        raise ValueError("SRT 中没有有效字幕段: %s" % srt_path)
    clips, dropped, kept_orig = select_segments(
        segs, keep_mode=mode, match=match, value=value,
        padding=float(padding or 0.0), min_keep_dur=float(min_keep_dur or 0.0))
    plan = {
        "video": v, "srt": os.path.abspath(srt_path),
        "mode": mode, "match": match, "value": value,
        "padding": round(float(padding or 0.0), 3),
        "total_segments": len(segs),
        "kept_segments": len(kept_orig), "dropped_segments": len(dropped),
        "kept_seconds": round(sum(c["t1"] - c["t0"] for c in clips), 3),
    }
    if not clips:
        plan["note"] = "没有命中任何字幕段, 请调整 mode/match/value 或 padding"
        return plan, None
    tl = clips_to_timeline(clips, src=v, width=int(width), height=int(height), fps=int(fps))
    return plan, tl


@mcp.tool()
def transcribe_media(path: str, model: str = "small", language: str = "",
                     vad: bool = True, word_timestamps: bool = False,
                     output_dir: str = "") -> str:
    """语音识别: 把任意视频/音频转成带时间戳的字幕(SRT)+片段JSON(文本剪辑的输入)。
    path          媒体绝对路径
    model         whisper 模型: tiny/base/small/medium/large(-v3) (首次需联网下载)
    language      语言代码(如 zh/en); 留空自动检测
    vad           是否过滤静音(仅 faster-whisper)
    word_timestamps 是否输出词级时间戳(写入 JSON)
    output_dir    输出目录(留空=与源文件同目录); 生成 <名>.srt 与 <名>.segments.json
    返回 {srt, json, segments_count, ...}; segments 即 [{t0,t1,text,words}]。
    """
    res = _transcribe_asr(os.path.abspath(path), model=model, language=language,
                          vad=bool(vad), word_timestamps=bool(word_timestamps),
                          output_dir=output_dir)
    return json.dumps(res, ensure_ascii=False, indent=2)


@mcp.tool()
def srt_to_segments(srt_path: str, src: str = "") -> str:
    """把已有 SRT 字幕解析成 segments JSON(文本剪辑的中间表示)。
    src 可填关联视频路径, 之后才能直接用于剪辑。"""
    if not os.path.isfile(srt_path):
        raise FileNotFoundError("字幕文件不存在: %s" % srt_path)
    segs = segments_from_srt(srt_path, src=os.path.abspath(src) if src else None)
    return make_segments_json(segs)


@mcp.tool()
def plan_text_cut(video: str, srt_path: str, mode: str = "keep",
                  match: str = "keywords", value: str = "",
                  padding: float = 0.2, min_keep_dur: float = 0.0) -> str:
    """文本驱动剪辑·规划(不渲染): 按字幕文本筛出要保留的段, 生成可直接交给
    render_timeline 的 timeline JSON + 统计, 供 agent 先看再决定渲染。
    video   源视频绝对路径
    srt_path SRT 字幕(可由 transcribe_media 生成)
    mode    keep=保留命中段 / drop=删除命中段
    match   keywords=关键词(用 | 或 , 分隔, 任一命中) / regex=正则 / items=段索引(0起,逗号分隔)
    value   匹配内容
    padding 命中段前后各扩的秒数(口播缓冲, 相邻段自动合并)
    min_keep_dur 过滤短于该秒数的保留段
    返回 {kept_segments, dropped_segments, kept_seconds, timeline:{...}}。
    """
    plan, tl = _plan_text_cut(video, srt_path, mode, match, value, padding,
                              min_keep_dur, 1920, 1080, 25)
    plan["timeline"] = tl
    return json.dumps(plan, ensure_ascii=False, indent=2)


@mcp.tool()
def text_cut(video: str, srt_path: str, output: str, mode: str = "keep",
             match: str = "keywords", value: str = "",
             padding: float = 0.2, min_keep_dur: float = 0.0,
             width: int = 1920, height: int = 1080, fps: int = 25) -> str:
    """文本驱动剪辑·成片: 一步完成「按字幕筛段->顺序拼接渲染」。
    参数同 plan_text_cut, 另加 output(输出 mp4 绝对路径)与分辨率/帧率。
    返回 {output, kept/dropped 统计, elapsed_s, log}。
    """
    plan, tl = _plan_text_cut(video, srt_path, mode, match, value, padding,
                              min_keep_dur, width, height, fps)
    if tl is None:
        raise ValueError("没有命中任何字幕段: 请调整 mode/match/value/padding")
    from autocut_render import run_render
    t0 = time.time()
    out = os.path.abspath(output)
    task_dir = os.path.join(RUNS_DIR, uuid.uuid4().hex[:12])
    os.makedirs(task_dir, exist_ok=True)
    res = run_render(tl, out)
    res = dict(res or {})
    res.update(plan)
    res["elapsed_s"] = round(time.time() - t0, 1)
    return json.dumps(res, ensure_ascii=False, indent=2)


@mcp.tool()
def burn_subtitles(video: str, srt_path: str, output: str,
                   fontname: str = "Microsoft YaHei", fontsize: int = 0,
                   primary: str = "#FFFFFF", outline: float = 1.6,
                   shadow: float = 0.8, margin_v: int = 48) -> str:
    """把 SRT 字幕烧录(硬字幕)进画面, 输出带字幕的 mp4。
    内部把 SRT 转成带样式的 ASS 再用 ffmpeg libass 渲染; 需要 ffmpeg 带 libass
    (本项目随包 imageio_ffmpeg 已含)。fontsize=0 表示按分辨率自动缩放。
    """
    v = _clean_path(video)
    if not os.path.isfile(srt_path):
        raise FileNotFoundError("字幕文件不存在: %s" % srt_path)
    segs = segments_from_srt(srt_path)
    if not segs:
        raise ValueError("SRT 中没有有效字幕段: %s" % srt_path)
    W, H = _video_size(v)
    ass_name = "subs.ass"
    out = os.path.abspath(output)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    task_dir = os.path.join(RUNS_DIR, uuid.uuid4().hex[:12])
    os.makedirs(task_dir, exist_ok=True)
    ass_path = os.path.join(task_dir, ass_name)
    write_ass(segs, ass_path, width=W, height=H, fontname=fontname,
              fontsize=int(fontsize or 0), primary=primary,
              outline=float(outline), shadow=float(shadow), margin_v=int(margin_v))
    vf = "ass=%s" % ass_name
    r = subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                        "-i", v, "-vf", vf,
                        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                        "-pix_fmt", "yuv420p", "-c:a", "copy", out],
                       capture_output=True, text=True, cwd=task_dir)
    if r.returncode != 0:
        raise RuntimeError("字幕烧录失败(ffmpeg 需带 libass): " + (r.stderr or "")[-1500:])
    return json.dumps({
        "output": out, "video": v, "segments": len(segs),
        "resolution": "%dx%d" % (W, H), "font": fontname,
    }, ensure_ascii=False, indent=2)




@mcp.tool()
def auto_edit(video: str, output: str, mode: str = "talk",
              segments_json: str = "", subtitle_srt: str = "",
              fillers: str = "嗯,啊,呃,那个,这个,就是,然后,um,uh",
              bgm_file: str = "", target_dur: float = 30.0, beat_unit: int = 1,
              threshold: float = -35.0, min_silence: float = 0.3,
              padding: float = 0.15, width: int = 1920, height: int = 1080,
              fps: int = 25, look: str = "none") -> str:
    """一键智能成片 —— 面向不会剪辑的用户: 给一个视频, 自动产出干净的成片。
    mode:
      talk   口播智能精简(推荐): 自动去静音; 若给 segments_json(词级字幕)则同时删语气词;
             若给 subtitle_srt 则按新时间轴重排后烧录字幕
      clean  仅去静音
      beat   卡点混剪(需 bgm_file, 可选 target_dur/beat_unit)
    segments_json: transcribe_media(word_timestamps=true) 生成的 .segments.json
    fillers:      要删除的语气词(逗号/竖线分隔)
    look:         成片调色预设(可选: none/vivid/warm/cool/soft/food/cinema/bw)
    threshold/min_silence/padding: 静音检测参数
    返回 {mode, notes, src_dur_s, out_dur_s, ratio, output, log}。
    """
    params = {
        "mode": mode, "src_video": video, "fillers": fillers,
        "target_dur": float(target_dur), "beat_unit": int(beat_unit),
        "threshold": float(threshold), "min_silence": float(min_silence),
        "padding": float(padding), "width": int(width), "height": int(height),
        "fps": int(fps), "look": str(look or "none"),
    }
    for k, v in (("segments_json", segments_json), ("subtitle_srt", subtitle_srt),
                 ("bgm_file", bgm_file)):
        if v and str(v).strip():
            params[k] = v
    return run_template("auto_edit", json.dumps(params, ensure_ascii=False), output)




@mcp.tool()
def apply_color_grade(video: str, output: str, preset: str = "cinema",
                      temperature: float = 0.0, tint: float = 0.0,
                      exposure_pct: float = 0.0, contrast_pct: float = 0.0,
                      saturation_pct: float = 0.0, gamma: float = 1.0,
                      lift_r: float = 0.0, lift_g: float = 0.0, lift_b: float = 0.0,
                      gamma_r: float = 0.0, gamma_g: float = 0.0, gamma_b: float = 0.0,
                      gain_r: float = 0.0, gain_g: float = 0.0, gain_b: float = 0.0,
                      vignette_pct: float = 0.0, grain_pct: float = 0.0,
                      lut: str = "") -> str:
    """调色(达芬奇风格): 色温/色调/曝光/对比度/饱和度/伽马 + Lift/Gamma/Gain 三色轮 + LUT/晕影/颗粒。
    video   源视频绝对路径
    preset  风格预设: none/vivid/warm/cool/soft/food/cinema/bw (cinema=电影感青橙)
    temperature 色温K(0=不动; <6500偏暖 >6500偏冷)
    tint    -100偏绿 ~ +100偏品红
    exposure_pct / contrast_pct / saturation_pct  百分比
    gamma   0.1~2.5 (1=不动)
    lift_r/g/b   阴影色轮(-1~1)  gamma_r/g/b 中间调  gain_r/g/b 高光
    vignette_pct 晕影  grain_pct 胶片颗粒  lut 3D LUT(.cube) 路径(可选)
    返回 {preset, params, filter, output}。
    """
    params = {"preset": preset}
    for k, v in (("temperature", temperature), ("tint", tint),
                 ("exposure_pct", exposure_pct), ("contrast_pct", contrast_pct),
                 ("saturation_pct", saturation_pct), ("gamma", gamma),
                 ("lift_r", lift_r), ("lift_g", lift_g), ("lift_b", lift_b),
                 ("gamma_r", gamma_r), ("gamma_g", gamma_g), ("gamma_b", gamma_b),
                 ("gain_r", gain_r), ("gain_g", gain_g), ("gain_b", gain_b),
                 ("vignette_pct", vignette_pct), ("grain_pct", grain_pct)):
        if v not in (None, 0) and not (k == "gamma" and float(v) == 1.0):
            params[k] = float(v)
    if lut and str(lut).strip():
        params["lut"] = lut
    out = os.path.abspath(output)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    task_dir = os.path.join(RUNS_DIR, uuid.uuid4().hex[:12])
    os.makedirs(task_dir, exist_ok=True)
    t0 = time.time()
    logs = []

    def _log(m):
        logs.append(str(m)); print(str(m), flush=True)

    res = _apply_grade(video, out, params, workdir=task_dir, log=_log)
    res = dict(res)
    res["elapsed_s"] = round(time.time() - t0, 1)
    res["log"] = logs
    return json.dumps(res, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 模板注册
_TEMPLATES = {}


def _templates_dir():
    return os.path.join(_HERE, "templates")


def _load_templates():
    """扫描插件自带 templates/ 目录, 加载每个导出 META/SCHEMA/run 的模板模块;
    同时注册 engine/timeline.py 提供的 timeline 模板。"""
    if _TEMPLATES:
        return _TEMPLATES
    tdir = _templates_dir()
    if os.path.isdir(tdir):
        for fn in sorted(os.listdir(tdir)):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            mod_name = fn[:-3]
            try:
                spec = importlib.util.spec_from_file_location("tpl_" + mod_name, os.path.join(tdir, fn))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                meta = getattr(mod, "META", None)
                if not meta or "id" not in meta or not callable(getattr(mod, "run", None)):
                    continue
                if not isinstance(getattr(mod, "SCHEMA", None), list):
                    continue
                _TEMPLATES[meta["id"]] = mod
            except Exception as e:  # noqa: BLE001
                print(f"[templates] 加载失败 {fn}: {e}", file=sys.stderr)
    # timeline 模板: engine/timeline.py
    tl_path = os.path.join(_HERE, "engine", "timeline.py")
    if os.path.isfile(tl_path) and "timeline" not in _TEMPLATES:
        try:
            spec = importlib.util.spec_from_file_location("tpl_timeline", tl_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if getattr(mod, "META", {}).get("id") and callable(getattr(mod, "run", None)):
                _TEMPLATES[mod.META["id"]] = mod
        except Exception as e:  # noqa: BLE001
            print(f"[templates] 加载失败 timeline: {e}", file=sys.stderr)
    return _TEMPLATES


@mcp.tool()
def list_templates() -> str:
    """列出插件内置的所有剪辑模板(名称/描述/参数schema), 供选择后调用 run_template。"""
    tpls = _load_templates()
    out = []
    for tid, mod in sorted(tpls.items()):
        meta = mod.META
        out.append({
            "id": tid,
            "name": meta.get("name", tid),
            "description": meta.get("description", ""),
            "params": getattr(mod, "SCHEMA", []),
        })
    return json.dumps({"templates": out}, ensure_ascii=False, indent=2)


@mcp.tool()
def run_template(template_id: str, params_json: str, output: str) -> str:
    """运行一个内置剪辑模板并渲染成片。

    template_id: 模板ID(见 list_templates)。
    params_json: JSON 对象, 键为模板参数(key 见 SCHEMA)。
                 通用字段: width/height/fps/output_name; 各模板另有专属字段, 例如:
                   basic_concat:  src_video, clips("0-3 | 5-4 | 10-3"), transition, transition_dur, bgm_file, bgm_volume, fadeout
                   silence_cut:   src_video, mode(cut/speed), threshold, min_silence, padding, silent_speed
                   beat_cut:      src_video, bgm_file, target_dur, beat_unit, transition, start_ss, gap
                   quadsplit:     src_video, bgm_file, new_ss, tile_ss, mask_widths, num_nodes, end_dur, zoom
                   multicam_speaker: video_files(逗号分隔机位), segments(JSON段落表), ref_index, lip_fps, subtitle_srt
                   timeline:      timeline_json(多轨时间线JSON)
    output: 输出 mp4 的绝对路径。
    """
    tpls = _load_templates()
    if template_id not in tpls:
        raise ValueError(f"模板不存在: {template_id} (可用: {sorted(tpls)})")
    try:
        params = json.loads(params_json) if isinstance(params_json, str) else params_json
    except Exception:
        raise ValueError("params_json 必须是合法 JSON 对象")
    if not isinstance(params, dict):
        raise ValueError("params_json 必须是 JSON 对象")

    out = os.path.abspath(output)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    task_dir = os.path.join(RUNS_DIR, uuid.uuid4().hex[:12])
    os.makedirs(task_dir, exist_ok=True)

    cfg = dict(params)
    cfg["workdir"] = task_dir
    cfg["output"] = out
    cfg["output_name"] = str(params.get("output_name") or os.path.basename(out))
    if "timeline_json" in cfg and isinstance(cfg["timeline_json"], str):
        try:
            cfg["timeline_json"] = json.loads(cfg["timeline_json"])
        except Exception:
            pass

    logs = []

    def _log(msg):
        logs.append(str(msg))
        print(str(msg), flush=True)

    t0 = time.time()
    result = tpls[template_id].run(cfg, log=_log)
    result = dict(result or {})
    result["template_id"] = template_id
    result["elapsed_s"] = round(time.time() - t0, 1)
    result["log"] = logs
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def scan_media(root: str) -> str:
    """递归扫描指定目录, 返回其中的视频/音频素材列表(供素材选择)。"""
    base = os.path.abspath(root)
    if not os.path.isdir(base):
        raise ValueError(f"目录不存在: {base}")
    video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mts", ".m2ts"}
    audio_exts = {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg"}
    videos, audios = [], []
    for root_dir, _, names in os.walk(base):
        for name in sorted(names):
            ext = os.path.splitext(name)[1].lower()
            p = os.path.join(root_dir, name)
            rel = os.path.relpath(p, base).replace("\\", "/")
            if ext in video_exts:
                videos.append({"name": name, "rel": rel, "path": p})
            elif ext in audio_exts:
                audios.append({"name": name, "rel": rel, "path": p})
    return json.dumps({"root": base, "videos": videos, "audios": audios},
                      ensure_ascii=False, indent=2)



@mcp.tool()
def triage_rushes(rushes_dir: str = "", files_json: str = "",
                  transcripts_json: str = "", script_json: str = "",
                  output: str = "", probe: bool = True) -> str:
    """素材筛选(Triage): 从含花絮/NG 的原始素材里, 为每个剧本节点找出可用 take。

    输入:
      rushes_dir        原始素材目录(递归扫描视频, mp4/mov/mkv...)
      files_json        JSON 数组: 显式给出素材文件绝对路径(与 rushes_dir 二选一)
      transcripts_json  已有 ASR 结果: {文件相对或绝对路径: {lang,segs:[{start,end,text}]}}
                        留空则现场转写(较慢; 建议先 transcribe_media 汇总成本文件)
      script_json       剧本分镜 JSON: [{id,title,scene,lines,keywords}]
                        留空则只做素材可用性分类, 不做台词对齐
      output            报告输出路径(默认 <runs>/triage_report.json)
    返回: 摘要 JSON(每段素材的 verdict + 每个剧本节点 best take 窗口); 完整报告已写盘。
    不做任何拼接/渲染 —— 这是拼接前的筛选步骤, 供 agent/人在 GUI 里先审查。
    """
    files = []
    base = ""
    if str(rushes_dir or "").strip():
        base = os.path.abspath(rushes_dir)
        if not os.path.isdir(base):
            raise ValueError("rushes_dir 不存在: %s" % base)
        for root_dir, _, names in os.walk(base):
            for name in sorted(names):
                if os.path.splitext(name)[1].lower() in {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mts", ".m2ts"}:
                    files.append(os.path.join(root_dir, name))
    elif str(files_json or "").strip():
        arr = json.loads(files_json) if isinstance(files_json, str) else files_json
        if not isinstance(arr, list) or not arr:
            raise ValueError("files_json 必须是素材绝对路径数组")
        for it in arr:
            f = it.get("path", it) if isinstance(it, dict) else it
            files.append(_clean_path(str(f)))
    else:
        raise ValueError("需要提供 rushes_dir 或 files_json 之一")
    files = sorted({os.path.abspath(f) for f in files if os.path.isfile(f)})
    if not files:
        raise ValueError("没有找到任何视频素材")

    trans = {}
    if str(transcripts_json or "").strip() and os.path.isfile(str(transcripts_json)):
        with open(str(transcripts_json), encoding="utf-8") as fh:
            trans = json.load(fh)

    rushes, duration_map = {}, {}
    missing = []
    for f in files:
        rel = os.path.relpath(f, base).replace("\\", "/") if base else f
        hit = None
        for k in (rel, f, os.path.basename(f)):
            if k in trans:
                hit = trans[k]
                break
        if hit is not None:
            rushes[rel] = {"lang": hit.get("lang") or "", "segs": hit.get("segs") or []}
        else:
            missing.append(f)
    if missing:
        # 现场对缺失文件转写(默认 small, 快)
        for f in missing:
            rel = os.path.relpath(f, base).replace("\\", "/") if base else f
            res = _transcribe_asr(f, model="small", vad=True, word_timestamps=False)
            rushes[rel] = {"lang": res.get("language") or "",
                           "segs": res.get("segments") or []}
    if probe:
        for f in files:
            rel = os.path.relpath(f, base).replace("\\", "/") if base else f
            try:
                duration_map[rel] = _time(f)
            except RuntimeError:
                pass

    pieces = []
    if str(script_json or "").strip() and os.path.isfile(str(script_json)):
        pieces = _triage_load_pieces(str(script_json))

    rep = _triage_build_report(rushes, pieces=pieces, duration_map=duration_map or None)
    out = os.path.abspath(output) if str(output or "").strip() else os.path.join(RUNS_DIR, "triage_report.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rep, ensure_ascii=False, indent=1))

    from collections import Counter
    vc = Counter(c["verdict"] for c in rep["clips"])
    bests = []
    for po in rep["pieces"]:
        b = po["best"]
        bests.append({
            "piece_id": po["piece_id"], "title": po["title"],
            "n_candidates": po["n_candidates"],
            "best": ({"file": b["file"], "t0": b["t0"], "t1": b["t1"],
                      "dur": b["dur"], "clean": b["clean"],
                      "coverage": b["coverage"]} if b else None),
        })
    return json.dumps({"report": out, "n_clips": len(rep["clips"]),
                       "verdicts": dict(vc), "pieces": bests},
                      ensure_ascii=False, indent=2)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--test", "tools"):
        import asyncio

        async def _list():
            return await mcp.list_tools()

        print("注册的工具:")
        for t in asyncio.run(_list()):
            nm = getattr(t, "name", "?")
            desc = (getattr(t, "description", "") or "").splitlines()[0] or ""
            print(f"  - {nm}: {desc}")
        print("\n启动: python autocut_mcp.py  (stdio MCP server)")
    else:
        mcp.run()