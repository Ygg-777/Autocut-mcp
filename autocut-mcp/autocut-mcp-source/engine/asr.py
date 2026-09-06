# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""语音识别(ASR)封装: 视频/音频 -> 带时间戳文本片段。

后端策略(与 editorvideo-ai "缺依赖的工具会提示怎么装" 的做法一致):
  优先 faster-whisper (CPU 下 int8 更快), 缺失时退回 openai-whisper;
  都没有则抛带安装指引的 RuntimeError, 不崩溃。

输出:
  * .srt   句级字幕 (标准格式)
  * .json  segments: [{src,t0,t1,text,speaker,words:[{word,start,end}]}]
  二者都作为文本剪辑(engine/segments.py)的输入中间表示。

依赖均为可选: pip install faster-whisper  或  pip install openai-whisper
模型首次使用需联网下载到缓存目录 (可用环境变量 WHISPER_CACHE_DIR 或
HF_HOME 指定; faster-whisper 支持 WHISPER_MODELS_DIR)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import imageio_ffmpeg

_FF = imageio_ffmpeg.get_ffmpeg_exe()


def _extract_wav(path: str, wav_path: str) -> None:
    r = subprocess.run([_FF, "-hide_banner", "-loglevel", "error", "-y", "-i", path,
                        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", wav_path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("提取音频失败: " + (r.stderr or "")[-800:])


def available_backends() -> list:
    """检测已安装的 ASR 后端。"""
    out = []
    try:
        import faster_whisper  # noqa: F401
        out.append("faster_whisper")
    except Exception:
        pass
    try:
        import whisper  # noqa: F401
        out.append("whisper")
    except Exception:
        pass
    return out


def _resolve_device(device):
    if device and device != "auto":
        return device
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _run_faster_whisper(path, model, language, vad, word_timestamps, device):
    from faster_whisper import WhisperModel
    compute = "float16" if device == "cuda" else "int8"
    m = WhisperModel(model, device=device, compute_type=compute)
    seg_iter, info = m.transcribe(
        path, language=language or None, vad_filter=bool(vad),
        word_timestamps=bool(word_timestamps), beam_size=5)
    segs, words_all = [], []
    for s in seg_iter:
        words = None
        if word_timestamps and getattr(s, "words", None):
            words = [{"word": w.word, "start": round(w.start, 3),
                      "end": round(w.end, 3)} for w in s.words]
            words_all.extend(words)
        segs.append({"start": round(s.start, 3), "end": round(s.end, 3),
                     "text": s.text.strip(), "words": words})
    return segs, (info.language if info else None), (info.duration if info else None)


def _run_whisper(path, model, language, vad, word_timestamps, device):
    import whisper
    m = whisper.load_model(model, device=device)
    opts = {"language": language or None, "word_timestamps": bool(word_timestamps)}
    if vad:
        try:
            from whisper.vad import load_vad  # 部分版本有
            opts["vad_filter"] = True
        except Exception:
            pass
    res = m.transcribe(path, **opts)
    segs = []
    for s in res.get("segments", []):
        words = None
        if word_timestamps and s.get("words"):
            words = [{"word": w.get("word"), "start": round(w.get("start", 0), 3),
                      "end": round(w.get("end", 0), 3)} for w in s["words"]]
        segs.append({"start": round(s.get("start", 0), 3),
                     "end": round(s.get("end", 0), 3),
                     "text": (s.get("text") or "").strip(), "words": words})
    return segs, res.get("language"), res.get("duration")


def transcribe(path: str, model: str = "small", language: str = "",
               vad: bool = True, word_timestamps: bool = False,
               output_dir: str = "", backend: str = "auto",
               device: str = "auto") -> dict:
    """对媒体文件做语音识别, 返回 dict{srt,json,segments,...}; 同时写 .srt/.json。"""
    src = os.path.abspath(path)
    if not os.path.isfile(src):
        raise FileNotFoundError("文件不存在: %s" % src)

    backends = available_backends()
    if backend == "auto":
        backend = backends[0] if backends else ""
    if backend not in backends:
        raise RuntimeError(
            "未安装可用的语音识别后端。请先安装其一:\n"
            "  pip install faster-whisper      (推荐, CPU 快)\n"
            "  pip install openai-whisper\n"
            "已检测: %s" % (backends or "无"))

    dev = _resolve_device(device)
    tmp_wav = os.path.join(tempfile.gettempdir(),
                           "_asr_%s.wav" % os.path.splitext(os.path.basename(src))[0])
    try:
        _extract_wav(src, tmp_wav)
        if backend == "faster_whisper":
            segs, lang, dur = _run_faster_whisper(tmp_wav, model, language, vad,
                                                  word_timestamps, dev)
        else:
            segs, lang, dur = _run_whisper(tmp_wav, model, language, vad,
                                           word_timestamps, dev)
    finally:
        try:
            os.remove(tmp_wav)
        except OSError:
            pass

    # 写文件
    base = os.path.splitext(os.path.basename(src))[0]
    out_dir = os.path.abspath(output_dir) if output_dir else os.path.dirname(src)
    os.makedirs(out_dir, exist_ok=True)
    srt_path = os.path.join(out_dir, base + ".srt")
    json_path = os.path.join(out_dir, base + ".segments.json")

    try:
        from engine.segments import write_srt, make_segments_json, normalize_segments
    except ImportError:
        from segments import write_srt, make_segments_json, normalize_segments
    write_srt([{"start": s["start"], "end": s["end"], "text": s["text"]} for s in segs],
              srt_path)
    segs_out = normalize_segments([
        {"t0": s["start"], "t1": s["end"], "text": s["text"], "words": s.get("words")}
        for s in segs])
    with open(json_path, "w", encoding="utf-8") as f:
        f.write(make_segments_json(segs_out))

    return {
        "path": src, "backend": backend, "model": model,
        "language": lang or "", "duration": round(float(dur or 0.0), 3),
        "segments_count": len(segs_out),
        "srt": srt_path, "json": json_path,
        "segments": segs_out,
    }
