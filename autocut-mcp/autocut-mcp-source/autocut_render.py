# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""薄封装: 优先使用随包自带的多轨合成引擎渲染成片(engine/timeline.py)。
供 autocut_mcp.py 调用, 不持有任何项目素材库逻辑, 可独立分发。
"""
import os
import sys


def _find_engine():
    """优先取随包自带引擎(engine/timeline.py), 再退回外部 autocut_gui / 环境变量。"""
    here = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(here, "engine")
    if os.path.isfile(os.path.join(local, "timeline.py")):
        return local
    cand = os.environ.get("AUTOCUT_GUI")
    if cand:
        return cand
    d = here
    for _ in range(5):
        for sub in (os.path.join(d, "autocut_gui"), d):
            if os.path.isfile(os.path.join(sub, "templates", "timeline.py")):
                return sub
        d = os.path.dirname(d)
    return None


def run_render(tl, output, width=None, height=None, fps=None):
    """调用多轨合成引擎渲染。tl 为 timeline dict, output 为输出绝对路径。"""
    engine = _find_engine()
    if not engine:
        # 无外部引擎时, 用本地最小实现(单视频轨纯时序 + 音频 amix)
        return _render_local(tl, output, width, height, fps)

    if engine not in sys.path:
        sys.path.insert(0, engine)
    if os.path.basename(engine) == "engine":
        from timeline import run as tl_run
    else:
        from templates.timeline import run as tl_run

    W = int(width or tl.get("width", 1920))
    H = int(height or tl.get("height", 1080))
    f = int(fps or tl.get("fps", 25))
    workdir = os.path.join(os.path.dirname(output), "_runs", os.path.splitext(os.path.basename(output))[0])
    os.makedirs(workdir, exist_ok=True)

    cfg = {"timeline_json": tl, "width": W, "height": H, "fps": f,
           "workdir": workdir, "output": output}
    return tl_run(cfg, log=lambda *a, **k: None)


def _render_local(tl, output, width, height, fps):
    """极简本地渲染兜底(无 autocut_gui 时): 仅支持单条视频轨纯时序 + 可选单音频轨。"""
    import subprocess
    import imageio_ffmpeg

    W = int(width or tl.get("width", 1920))
    H = int(height or tl.get("height", 1080))
    f = int(fps or tl.get("fps", 25))
    FF = imageio_ffmpeg.get_ffmpeg_exe()
    os.makedirs(os.path.dirname(output), exist_ok=True)

    vtr = next((t for t in tl["tracks"] if t["type"] == "video"), None)
    cl = sorted((vtr or {"clips": []}).get("clips", []), key=lambda c: c["start"])
    if not cl:
        # 无视频 -> 纯黑
        subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", f"color=c=black:s={W}x{H}:r={f}:d=1",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", output],
                       capture_output=True, text=True)
        return {"mode": "black", "output": output}

    segs = []
    for i, c in enumerate(cl):
        pf = os.path.join(os.path.dirname(output), f"_runs_p{i}.mp4")
        sin = float(c.get("src_in", 0.0)); dur = float(c.get("dur", 1.0))
        vf = (f"trim=start={sin:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS,"
              f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"fps={f},format=yuv420p,setsar=1")
        subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-i", c["src"],
                        "-vf", vf, "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                        "-an", pf], capture_output=True, text=True)
        segs.append(pf)

    lst = os.path.join(os.path.dirname(output), "_runs_alist.txt")
    with open(lst, "w", encoding="utf-8") as fh:
        for p in segs:
            fh.write("file '" + p.replace("\\", "/") + "'\n")
    vid = os.path.join(os.path.dirname(output), "_runs_total.mp4")
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                    "-safe", "0", "-i", lst, "-c", "copy", vid],
                   capture_output=True, text=True)

    artr = next((t for t in tl["tracks"] if t["type"] == "audio"), None)
    acl = (artr or {"clips": []}).get("clips", [])
    if acl:
        a = acl[0]
        af = f"atrim=start={float(a.get('src_in',0)):.3f}:duration={float(a.get('dur',1)):.3f},asetpts=PTS-STARTPTS"
        if float(a.get("volume", 1.0)) != 1.0:
            af += f",volume={float(a['volume']):.3f}"
        audf = os.path.join(os.path.dirname(output), "_runs_audio.m4a")
        subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-i", a["src"],
                        "-vn", "-af", af, "-c:a", "aac", "-b:a", "192k", audf],
                       capture_output=True, text=True)
        subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-i", vid,
                        "-i", audf, "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                        "-c:a", "aac", "-shortest", output], capture_output=True, text=True)
    else:
        subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-i", vid,
                        "-c", "copy", output], capture_output=True, text=True)
    return {"mode": "local_basic", "segments": len(cl), "output": output}