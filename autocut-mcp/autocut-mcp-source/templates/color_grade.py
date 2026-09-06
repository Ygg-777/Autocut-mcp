# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""调色模板: 对整片应用 DaVinci 风格的调色(lift/gamma/gain 色轮·色温·对比度·饱和度·LUT·晕影·颗粒)。
把 Resolve Color 页的核心控制映射到 ffmpeg(engine/grade.py)。"""
import json
import os

META = {
    "id": "color_grade",
    "name": "调色(达芬奇风格)",
    "description": "色温/色调/曝光/对比度/饱和度/伽马/晕影/颗粒 · 一键电影感等预设 · 支持 .cube LUT",
}

_PRESETS = [
    {"value": "none", "label": "原片(不调)"},
    {"value": "vivid", "label": "鲜艳自然"},
    {"value": "warm", "label": "暖阳"},
    {"value": "cool", "label": "冷调"},
    {"value": "soft", "label": "柔和"},
    {"value": "food", "label": "美食暖色"},
    {"value": "cinema", "label": "电影感(青橙)"},
    {"value": "bw", "label": "黑白"},
]

SCHEMA = [
    {"key": "src_video", "label": "视频素材", "type": "video_file"},
    {"key": "preset", "label": "风格预设", "type": "select", "options": _PRESETS, "default": "cinema",
     "help": "预设之外手动调节会自动叠加在预设之上(0/不填=不覆盖)"},
    {"key": "temperature", "label": "色温(K, 0=不动; <6500偏暖 >6500偏冷)", "type": "number", "default": 0},
    {"key": "tint", "label": "色调(-100偏绿 ~ +100偏品红)", "type": "number", "default": 0},
    {"key": "exposure_pct", "label": "曝光(%)", "type": "number", "default": 0},
    {"key": "contrast_pct", "label": "对比度(%)", "type": "number", "default": 0},
    {"key": "saturation_pct", "label": "饱和度(%)", "type": "number", "default": 0},
    {"key": "gamma", "label": "伽马(0.1~2.5, 1=不动)", "type": "number", "default": 1.0, "step": 0.05},
    {"key": "vignette_pct", "label": "晕影(%)", "type": "number", "default": 0},
    {"key": "grain_pct", "label": "胶片颗粒(%)", "type": "number", "default": 0},
    {"key": "lut", "label": "3D LUT(.cube 路径, 可选)", "type": "text", "required": False,
     "help": "例: DaVinci 自带的 Film Looks Rec709 Kodak 2383 D65.cube"},
    {"key": "width", "label": "分辨率宽(仅兜底)", "type": "number", "default": 1920, "required": False},
    {"key": "height", "label": "分辨率高(仅兜底)", "type": "number", "default": 1080, "required": False},
    {"key": "fps", "label": "帧率(仅兜底)", "type": "number", "default": 25, "required": False},
    {"key": "output_name", "label": "输出文件名", "type": "text", "default": "graded"},
]


def run(cfg, log=print):
    try:
        from engine.grade import apply_grade
    except ImportError:
        from grade import apply_grade
    src = cfg["src_video"]
    WD = cfg["workdir"]
    OUTPUT = cfg["output"]
    os.makedirs(WD, exist_ok=True)
    params = {k: cfg.get(k) for k in ("preset", "temperature", "tint", "exposure_pct",
                                      "contrast_pct", "saturation_pct", "gamma",
                                      "vignette_pct", "grain_pct", "lut") if cfg.get(k) is not None}
    log("= 调色 = preset=%s" % (params.get("preset") or "none"))
    res = apply_grade(src, OUTPUT, params, workdir=WD, log=log)
    log("  滤镜: %s" % (res["filter"] or "(未叠加滤镜, 直接复制)"))
    return {"preset": params.get("preset") or "none", "filter": res["filter"],
            "output": OUTPUT}
