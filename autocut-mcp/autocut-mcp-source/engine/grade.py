# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""调色内核: 把 DaVinci Resolve Color 页的核心控制映射到 ffmpeg 滤镜链。

对照 DaVinci(理解自其 Color 页 + 官方脚本/LUT/CTL):
  * 色轮 Lift/Gamma/Gain(每通道) -> ffmpeg colorbalance (shadows/midtones/highlights)
  * 白平衡 Temperature/Tint      -> colortemperature(实测: <6500K 偏暖, >6500K 偏冷) + colorbalance(midtones) 近似 Tint
  * 曝光/对比度/饱和度/伽马      -> eq (brightness/contrast/saturation/gamma)
  * 3D LUT(支持 .cube, 如 DaVinci 自带 Film Looks) -> lut3d
  * 晕影/颗粒                    -> vignette / noise

应用方式: 后处理整片(与 Resolve 对整轨 ApplyGradeFromDRX 后 Deliver 渲染一致)。
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess

import imageio_ffmpeg

_FF = imageio_ffmpeg.get_ffmpeg_exe()

# 预设: DaVinci 风格 "look"。每个字段缺省即不动该项。
PRESETS = {
    "none":   {},
    "vivid":  {"saturation_pct": 18, "contrast_pct": 8},
    "warm":   {"temperature": 5600, "contrast_pct": 6, "saturation_pct": 6},
    "cool":   {"temperature": 8200, "contrast_pct": 4, "saturation_pct": 2},
    "soft":   {"contrast_pct": -8, "saturation_pct": -12, "gamma": 1.04, "vignette_pct": 14},
    "food":   {"temperature": 5400, "saturation_pct": 20, "contrast_pct": 7, "tint": 6},
    "cinema": {"contrast_pct": 16, "saturation_pct": -8, "vignette_pct": 22,
               "lift_r": -0.05, "lift_g": 0.03, "lift_b": 0.07,
               "gain_r": 0.09, "gain_g": 0.03, "gain_b": -0.05},
    "bw":     {"saturation_pct": -100, "contrast_pct": 14, "gamma": 1.03},
}

PRESET_NAMES = list(PRESETS)


def preset_params(name):
    return dict(PRESETS.get(name or "none") or {})


def _active(p):
    """过滤出“需要生效”的参数。"""
    out = {}
    for k, v in (p or {}).items():
        if k == "lut" and v:
            out[k] = v
        elif k == "temperature":
            if v not in (None, 0): out[k] = float(v)
        elif k == "gamma":
            if v not in (None, 1.0): out[k] = float(v)
        elif k in ("tint", "exposure_pct", "contrast_pct", "saturation_pct",
                   "vignette_pct", "grain_pct"):
            if v not in (None, 0): out[k] = float(v)
        elif k in ("lift_r", "lift_g", "lift_b", "gamma_r", "gamma_g", "gamma_b",
                   "gain_r", "gain_g", "gain_b"):
            if v not in (None, 0): out[k] = float(v)
    return out


def _clip(x, lo, hi):
    return max(lo, min(hi, float(x)))


def build_filter(params) -> str:
    """params -> 单条 -vf 滤镜串。无任何调整时返回空串。"""
    merged = preset_params(params.get("preset"))
    merged.update(_active({k: v for k, v in (params or {}).items() if k != "preset"}))
    p = _active(merged)
    if not p:
        return ""
    chain = []

    # 基础: 曝光/对比度/饱和度/伽马 (eq 数值范围: brightness -1..1, contrast 0..2, saturation 0..3, gamma 0.1..10)
    eqs = []
    if "exposure_pct" in p:
        eqs.append("brightness=%.3f" % _clip(p["exposure_pct"] / 100.0, -1, 1))
    if "contrast_pct" in p:
        eqs.append("contrast=%.3f" % _clip(1 + p["contrast_pct"] / 100.0, 0.01, 3))
    if "saturation_pct" in p:
        eqs.append("saturation=%.3f" % _clip(1 + p["saturation_pct"] / 100.0, 0, 3))
    if "gamma" in p:
        eqs.append("gamma=%.3f" % _clip(p["gamma"], 0.1, 10))
    if eqs:
        chain.append("eq=%s" % ":".join(eqs))

    # 白平衡: 温度(colortemperature) + 色调(Tint, 用中调色轮近似: 正=偏品红)
    if "temperature" in p:
        chain.append("colortemperature=temperature=%d:pl=1" % _clip(int(p["temperature"]), 1000, 40000))
    if "tint" in p:
        t = _clip(p["tint"], -100, 100) / 100.0
        cb = "colorbalance=rm=%.3f:bm=%.3f:gm=%.3f" % (0.25 * t, 0.20 * t, -0.22 * t)
        chain.append(cb)

    # 色轮: Lift=shadows, Gamma=midtones, Gain=highlights (每通道)
    cb = []
    for ch, name in (("r", "r"), ("g", "g"), ("b", "b")):
        lk, gk, hk = "lift_" + ch, "gamma_" + ch, "gain_" + ch
        if lk in p: cb.append("%ss=%.3f" % (name, _clip(p[lk], -1, 1)))
        if gk in p: cb.append("%sm=%.3f" % (name, _clip(p[gk], -1, 1)))
        if hk in p: cb.append("%sh=%.3f" % (name, _clip(p[hk], -1, 1)))
    if cb:
        chain.append("colorbalance=%s:pl=1" % ":".join(cb))

    # 3D LUT (.cube)
    if p.get("lut"):
        chain.append("lut3d=file=%s" % p["lut"])

    # 晕影
    if p.get("vignette_pct"):
        v = _clip(p["vignette_pct"], 0, 100) / 100.0
        chain.append("vignette=a=%.4f" % (0.35 + 1.6 * v))

    # 颗粒
    if p.get("grain_pct"):
        g = _clip(p["grain_pct"], 0, 100)
        chain.append("noise=alls=%.1f:allf=t+u" % (g * 0.25))

    return ",".join(chain)


def apply_grade(video: str, output: str, params, workdir: str = "", log=print) -> dict:
    """对整片应用调色并重编码输出(mp4, 保留音轨)。"""
    src = os.path.abspath(video)
    if not os.path.isfile(src):
        raise FileNotFoundError("文件不存在: %s" % src)
    out = os.path.abspath(output)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    wd = os.path.abspath(workdir) if workdir else os.path.dirname(out)
    os.makedirs(wd, exist_ok=True)

    merged = preset_params(params.get("preset"))
    merged.update(_active({k: v for k, v in (params or {}).items() if k != "preset"}))
    p = _active(merged)
    if p.get("lut"):
        lut = os.path.abspath(p["lut"])
        if not os.path.isfile(lut):
            raise FileNotFoundError("LUT 文件不存在: %s" % lut)
        dst = os.path.join(wd, "look.cube")
        shutil.copyfile(lut, dst)
        p["lut"] = "look.cube"          # 相对名, 规避滤镜路径转义

    vf = build_filter(p)
    cmd = [_FF, "-hide_banner", "-loglevel", "error", "-y", "-i", src]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-map", "0:v?", "-map", "0:a?", "-c:v", "libx264", "-preset", "fast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", out]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=wd if vf and p.get("lut") else None)
    if r.returncode != 0:
        raise RuntimeError("调色失败: " + (r.stderr or "")[-1500:])
    log("  已应用调色: %s -> %s" % (os.path.basename(src), os.path.basename(out)))
    return {"preset": params.get("preset") or "custom", "params": p,
            "filter": vf, "output": out}


def describe():
    return {"presets": {k: preset_params(k) for k in PRESET_NAMES},
            "controls": ["temperature", "tint", "exposure_pct", "contrast_pct",
                         "saturation_pct", "gamma", "lift_r/g/b", "gamma_r/g/b",
                         "gain_r/g/b", "vignette_pct", "grain_pct", "lut"]}
