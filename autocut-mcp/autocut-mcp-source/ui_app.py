# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""插件独立 Web UI 后端 (通用, 不绑定任何素材库)。

提供浏览器控制台, 通过本插件(MCP)的基础剪辑能力完成:
  * /api/probe         探测用户给定路径的媒体  (等价 MCP probe_media)
  * /api/render        提交多轨时间线并渲染成片 (等价 MCP render_timeline)
  * /api/status/<id>   轮询渲染状态/日志/进度
  * /output/<name>     预览/下载渲染结果
  * /media/...         预览任意本地媒体(限本项目输出/素材目录)

素材来源不预设任何项目目录: 用户可在 UI 中填写任意路径, 或配置 AUTOCUT_ROOT 目录
作为文件浏览根(root, 默认本脚本所在目录), 从那里挑选视频/音频。

运行:
    python ui_app.py            # 默认 http://127.0.0.1:8001
环境变量:
    AUTOCUT_ROOT   素材浏览根目录(默认: 本脚本目录)
    AUTOCUT_OUT    输出目录(默认: <脚本目录>/autocut_output)
    AUTOCUT_RUNS   工作目录(默认: <脚本目录>/autocut_runs)
"""
import json
import os
import re
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_from_directory, render_template, Response

from autocut_render import run_render

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("AUTOCUT_ROOT", _HERE)
OUT_DIR = os.environ.get("AUTOCUT_OUT", os.path.join(_HERE, "autocut_output"))
RUNS_DIR = os.environ.get("AUTOCUT_RUNS", os.path.join(_HERE, "autocut_runs"))
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(RUNS_DIR, exist_ok=True)

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mts", ".m2ts"}
AUDIO_EXTS = {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg"}

app = Flask(__name__, static_folder="ui", static_url_path="/ui", template_folder="ui")

TASKS = {}
_LOCK = threading.Lock()


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


def _safe_rel(path):
    """把给定绝对路径转为安全相对路径(去盘符/去通用前缀), 用于 /media/ 预览。"""
    norm = os.path.abspath(path).replace("\\", "/")
    norm = norm.strip("/")
    # 去盘符 d:/  ->  unix 形式
    norm = re.sub(r"^[a-zA-Z]:/", "", norm)
    return re.sub(r"[^\w./\-]", "_", norm)


# ---------------- 页面 ----------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------- 素材浏览 ----------------
@app.route("/api/scan", methods=["POST"])
def api_scan():
    """列出 ROOT 下视频/音频(递归)。body: {root?}. 通用, 可指定任意目录。"""
    body = request.get_json(force=True, silent=True) or {}
    base = body.get("root") or ROOT
    if not os.path.isdir(base):
        return jsonify({"error": f"目录不存在: {base}"})
    videos, audios = [], []
    for root, _, names in os.walk(base):
        for name in sorted(names):
            ext = os.path.splitext(name)[1].lower()
            if ext in VIDEO_EXTS:
                videos.append({"name": name, "rel": os.path.relpath(os.path.join(root, name), base).replace("\\", "/"), "path": os.path.join(root, name)})
            elif ext in AUDIO_EXTS:
                audios.append({"name": name, "rel": os.path.relpath(os.path.join(root, name), base).replace("\\", "/"), "path": os.path.join(root, name)})
    return jsonify({"root": base, "videos": videos, "audios": audios})


@app.route("/api/probe", methods=["POST"])
def api_probe():
    """探测任意路径媒体。body: {path}。"""
    body = request.get_json(force=True, silent=True) or {}
    p = body.get("path")
    if not p or not os.path.isfile(p):
        return jsonify({"error": "path 无效或文件不存在"}), 400
    try:
        from autocut_mcp import probe_media
        return Response(probe_media(p), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------- 模板 ----------------
@app.route("/api/templates")
def api_templates():
    """列出内置剪辑模板(名称/描述/参数schema)。"""
    from autocut_mcp import _load_templates
    tpls = _load_templates()
    out = []
    for tid, mod in sorted(tpls.items()):
        out.append({"id": tid, "name": mod.META.get("name", tid),
                    "description": mod.META.get("description", ""),
                    "params": getattr(mod, "SCHEMA", [])})
    return jsonify({"templates": out})


@app.route("/api/template_render", methods=["POST"])
def api_template_render():
    """运行内置剪辑模板渲染成片。body: {template_id, params, output_name}。"""
    body = request.get_json(force=True, silent=True) or {}
    template_id = body.get("template_id")
    params = body.get("params") or {}
    if not template_id:
        return jsonify({"error": "template_id 必填"}), 400
    out_name = str(body.get("output_name") or (template_id + "_" + time.strftime("%H%M%S"))).strip()
    if not out_name.lower().endswith(".mp4"):
        out_name += ".mp4"
    task_id = uuid.uuid4().hex[:12]
    task_dir = os.path.join(RUNS_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    output = os.path.join(OUT_DIR, out_name)
    with _LOCK:
        TASKS[task_id] = {"id": task_id, "status": "running", "log": [],
                          "progress": 0, "output": out_name, "error": None}

    def _log(msg):
        with _LOCK:
            TASKS[task_id]["log"].append(str(msg))

    def _worker():
        from autocut_mcp import _load_templates
        try:
            tpls = _load_templates()
            if template_id not in tpls:
                raise RuntimeError(f"模板不存在: {template_id}")
            cfg = dict(params)
            cfg["workdir"] = task_dir
            cfg["output"] = output
            cfg["output_name"] = str(params.get("output_name") or out_name)
            if "timeline_json" in cfg and isinstance(cfg["timeline_json"], str):
                try:
                    cfg["timeline_json"] = json.loads(cfg["timeline_json"])
                except Exception:
                    pass
            _log(f"开始: {template_id} -> {out_name}")
            result = tpls[template_id].run(cfg, log=_log)
            with _LOCK:
                TASKS[task_id]["status"] = "done"
                TASKS[task_id]["progress"] = 100
                TASKS[task_id]["log"].append("渲染完成")
                TASKS[task_id]["params"] = result
        except Exception as e:
            import traceback
            with _LOCK:
                TASKS[task_id]["status"] = "error"
                TASKS[task_id]["error"] = str(e)
                TASKS[task_id]["log"].append("[ERROR] " + str(e))
                TASKS[task_id]["log"].append(traceback.format_exc()[-1500:])

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"task_id": task_id})


# ---------------- 渲染 ----------------
@app.route("/api/render", methods=["POST"])
def api_render():
    body = request.get_json(force=True, silent=True) or {}
    tl = body.get("timeline_json")
    if isinstance(tl, str):
        try:
            tl = json.loads(tl)
        except Exception:
            return jsonify({"error": "timeline_json 非法"}), 400
    if not isinstance(tl, dict) or not tl.get("tracks"):
        return jsonify({"error": "timeline_json 必须含 tracks"}), 400

    out_name = str(body.get("output_name") or "edit_" + time.strftime("%H%M%S")).strip()
    if not out_name.lower().endswith(".mp4"):
        out_name += ".mp4"
    task_id = uuid.uuid4().hex[:12]
    task_dir = os.path.join(RUNS_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    output = os.path.join(OUT_DIR, out_name)

    with _LOCK:
        TASKS[task_id] = {"id": task_id, "status": "running", "log": [],
                          "progress": 0, "output": out_name, "error": None}

    def _log(msg):
        with _LOCK:
            TASKS[task_id]["log"].append(str(msg))
            s = str(msg)

    def _worker():
        try:
            cfg = {"timeline_json": tl, "width": int(tl.get("width", 1920)),
                   "height": int(tl.get("height", 1080)), "fps": int(tl.get("fps", 25)),
                   "workdir": task_dir, "output": output}
            with _LOCK:
                TASKS[task_id]["log"].append(f"渲染中: {out_name}")
            _log("开始渲染…")
            run_render(tl, output, int(tl.get("width", 1920)),
                       int(tl.get("height", 1080)), int(tl.get("fps", 25)))
            with _LOCK:
                TASKS[task_id]["status"] = "done"
                TASKS[task_id]["progress"] = 100
                TASKS[task_id]["log"].append("渲染完成")
        except Exception as e:
            import traceback
            with _LOCK:
                TASKS[task_id]["status"] = "error"
                TASKS[task_id]["error"] = str(e)
                TASKS[task_id]["log"].append("[ERROR] " + str(e))
                TASKS[task_id]["log"].append(traceback.format_exc()[-1500:])

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/status/<task_id>")
def api_status(task_id):
    with _LOCK:
        t = TASKS.get(task_id)
    if not t:
        return jsonify({"error": "task not found"}), 404
    return jsonify(t)


@app.route("/output/<filename>")
def output_file(filename):
    return send_from_directory(OUT_DIR, filename, as_attachment=False)


@app.route("/media/<path:rest>")
def media_file(rest):
    """通用媒体预览: /media/<相对路径>. 相对 ROOT 目录。"""
    full = os.path.join(ROOT, rest)
    if os.path.isfile(full):
        return send_from_directory(ROOT, rest)
    return "not found", 404



# ---------------- 可视化审片 / 局部修改 / 增量渲染 ----------------
# 素材目录不再硬编码: 一律以 AUTOCUT_ROOT(ROOT) 或用户给出的绝对路径为准
_SEGCACHE = os.path.join(RUNS_DIR, "segcache")
os.makedirs(_SEGCACHE, exist_ok=True)


def _resolve_src(rel):
    cands = []
    if rel:
        if os.path.isabs(rel):
            cands = [rel]
        else:
            cands = [os.path.join(ROOT, rel)]
    for c in cands:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return None


@app.route("/api/direct_plan", methods=["GET", "POST"])
def api_direct_plan():
    """可视化审片: 优先载入用户当前计划(user_plan.json), 否则 v6; 附备选 take 与成片链接。"""
    import glob
    user_path = os.path.join(RUNS_DIR, "user_plan.json")
    v6_path = os.path.join(RUNS_DIR, "v6_director_plan.json")
    src, plan_path, output_url, plan_source = None, None, None, "v6"
    if os.path.isfile(user_path):
        try:
            up = json.load(open(user_path, encoding="utf-8"))
            if up.get("plan"):
                plan_path, src, plan_source = user_path, up, "user"
                output_url = up.get("output_url") or ""
        except Exception:
            pass
    if plan_path is None and os.path.isfile(v6_path):
        plan_path, plan_source = v6_path, "v6"
    if plan_path is None:
        return jsonify({"error": "缺少导演计划, 请先生成分镜表"}), 400
    if src is None:
        src = json.load(open(plan_path, encoding="utf-8"))
    plan = src["plan"]
    tri_path = os.path.join(RUNS_DIR, "triage_report.json")
    cand_by_beat = {}
    if os.path.isfile(tri_path):
        tri = json.load(open(tri_path, encoding="utf-8"))
        for po in tri.get("pieces", []):
            pid = po["piece_id"]
            cand_by_beat[pid] = [{"file": c["file"], "t0": c["t0"], "t1": c["t1"],
                                  "dur": c["dur"], "clean": c["clean"],
                                  "coverage": c["coverage"],
                                  "text": c.get("window_text", "")[:80]}
                                 for c in (po.get("candidates") or [])[:8]]
    for i, sh in enumerate(plan):
        sh["i"] = i
        sh["src"] = _resolve_src(sh.get("src_rel") or sh.get("src_name") or "")
    return jsonify({"plan": plan, "candidates_by_beat": cand_by_beat,
                    "plan_source": plan_source, "output_url": output_url or "",
                    "video_ext": ".mp4"})


@app.route("/api/dir_frame")
def api_dir_frame():
    """抽一帧 JPEG 用于缩略图/审片: ?src=<rel>&t=<秒>&w=<宽,默认480>"""
    import subprocess, tempfile
    import imageio_ffmpeg
    src = _resolve_src(request.args.get("src", ""))
    if not src:
        return jsonify({"error": "src 无效"}), 400
    try:
        t = max(0.0, float(request.args.get("t", 0.0)))
        w = int(request.args.get("w", 480))
    except Exception:
        return jsonify({"error": "参数非法"}), 400
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    jpg = os.path.join(tempfile.gettempdir(), "dirframe_%s.jpg" % uuid.uuid4().hex[:10])
    vf = "scale=%d:-2" % w if w and w > 0 else None
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-ss", "%.3f" % t, "-i", src,
           "-frames:v", "1", "-q:v", "3"]
    if vf:
        cmd += ["-vf", vf]
    cmd.append(jpg)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(jpg):
        return jsonify({"error": "取帧失败: %s" % (r.stderr or "")[-200:]}), 500
    from flask import send_file
    return send_file(jpg, mimetype="image/jpeg")


@app.route("/api/dir_save", methods=["POST"])
def api_dir_save():
    """保存用户修改后的计划(仅改 JSON, 不渲染)。body: {plan:[...]}"""
    body = request.get_json(force=True, silent=True) or {}
    plan = body.get("plan")
    if not isinstance(plan, list):
        return jsonify({"error": "plan 必须是数组"}), 400
    out = os.path.join(RUNS_DIR, "user_plan.json")
    json.dump({"plan": plan}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return jsonify({"ok": True, "saved": out, "shots": len(plan)})


def _render_shot_seg(src, t0, t1, segkey, w, h):
    """把镜头全幅裁切编码为缓存段(视频+音频)。已存在则跳过。"""
    import subprocess
    import imageio_ffmpeg
    vout = os.path.join(_SEGCACHE, segkey + ".mp4")
    aout = os.path.join(_SEGCACHE, segkey + ".m4a")
    if os.path.isfile(vout) and os.path.isfile(aout):
        return vout, aout, False
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    dur = max(0.3, t1 - t0)
    vf = "scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,format=yuv420p" % (w, h, w, h)
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", "%.3f" % t0, "-t", "%.3f" % dur, "-i", src,
                    "-vf", vf, "-r", "25", "-c:v", "libx264", "-preset", "fast",
                    "-crf", "18", "-video_track_timescale", "13824", "-an", vout], check=True)
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", "%.3f" % t0, "-t", "%.3f" % dur, "-i", src,
                    "-vn", "-ac", "2", "-ar", "44100", "-c:a", "aac", "-b:a", "192k", aout], check=True)
    return vout, aout, True


@app.route("/api/dir_render", methods=["POST"])
def api_dir_render():
    """增量渲染: body {shots:[{src_rel,t0,t1,text}], dirty:[索引]}。
    只重编码 dirty(或缺失缓存)镜头, 其余用缓存拼接 -> 不改全片。"""
    import hashlib, subprocess
    import imageio_ffmpeg
    body = request.get_json(force=True, silent=True) or {}
    shots = body.get("shots") or []
    dirty = set(int(x) for x in (body.get("dirty") or []))
    if not shots:
        return jsonify({"error": "shots 为空"}), 400
    w = int(body.get("width", 1280)); h = int(body.get("height", 720))
    out_name = str(body.get("output_name") or ("ui_edit_" + time.strftime("%H%M%S"))).strip()
    if not out_name.lower().endswith(".mp4"):
        out_name += ".mp4"
    task_id = uuid.uuid4().hex[:12]
    task_dir = os.path.join(RUNS_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    output = os.path.join(OUT_DIR, out_name)
    with _LOCK:
        TASKS[task_id] = {"id": task_id, "status": "running", "log": [],
                          "progress": 0, "output": out_name, "error": None}

    def _log(m):
        with _LOCK:
            TASKS[task_id]["log"].append(str(m))

    def _worker():
        try:
            ff = imageio_ffmpeg.get_ffmpeg_exe()
            segs, auds, changed = [], [], []
            for i, sh in enumerate(shots):
                src = _resolve_src(sh.get("src_rel") or sh.get("src") or "")
                if not src:
                    raise RuntimeError("镜头%d 源文件不存在: %s" % (i, sh.get("src_rel")))
                t0 = max(0.0, float(sh.get("t0", 0.0)))
                t1 = float(sh.get("t1", t0 + 1.0))
                if t1 - t0 < 0.25:
                    raise RuntimeError("镜头%d 时长过短" % i)
                key = hashlib.md5(("%s|%.3f|%.3f" % (src, t0, t1)).encode()).hexdigest()[:20]
                v, a, isnew = _render_shot_seg(src, t0, t1, key, w, h)
                segs.append(v); auds.append(a)
                if isnew:
                    changed.append(i)
                with _LOCK:
                    TASKS[task_id]["progress"] = int(80 * (i + 1) / len(shots))
                _log("镜头%02d %s  %s" % (i, "重编码(修改)" if i in dirty or isnew else "复用缓存",
                                          sh.get("text", "")[:26]))
            # 拼接(仅拷贝, 快)
            def _concat(files, out):
                txt = os.path.join(task_dir, os.path.basename(out) + ".txt")
                with open(txt, "w", encoding="utf-8") as f:
                    for p in files:
                        f.write("file '" + p.replace("\\", "/") + "'" + os.linesep)
                r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                                    "-f", "concat", "-safe", "0", "-i", txt, "-c", "copy", out],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    raise RuntimeError(r.stderr[-1000:])
            joined = os.path.join(task_dir, "joined.mp4"); _concat(segs, joined)
            audio_total = os.path.join(task_dir, "audio_total.m4a"); _concat(auds, audio_total)
            r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                                "-i", joined, "-i", audio_total, "-map", "0:v", "-map", "1:a",
                                "-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart", output],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(r.stderr[-1000:])
            with _LOCK:
                TASKS[task_id]["status"] = "done"
                TASKS[task_id]["progress"] = 100
                TASKS[task_id]["log"].append("完成 -> %s (重编码 %d 段)" % (out_name, len(changed)))
                TASKS[task_id]["result"] = {"output": output, "changed": changed}
        except Exception as e:
            import traceback
            with _LOCK:
                TASKS[task_id]["status"] = "error"
                TASKS[task_id]["error"] = str(e)
                TASKS[task_id]["log"].append("[ERROR] " + str(e))
                TASKS[task_id]["log"].append(traceback.format_exc()[-1200:])

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"task_id": task_id})


# ---------------- 文本/字幕驱动剪辑 (AutoCut 式: 字幕 -> 选句 -> 成片) ----------------
@app.route("/api/textplan", methods=["POST"])
def api_textplan():
    """同步: 文本筛选规划。body: {video, srt, mode, match, value, padding}。"""
    body = request.get_json(force=True, silent=True) or {}
    try:
        from autocut_mcp import _plan_text_cut
        plan, tl = _plan_text_cut(
            body.get("video", ""), body.get("srt", ""),
            str(body.get("mode") or "keep"), str(body.get("match") or "keywords"),
            str(body.get("value") or ""), float(body.get("padding") or 0.0),
            float(body.get("min_keep_dur") or 0.0), 1920, 1080, 25)
        plan["timeline"] = tl
        return jsonify(plan)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def _textjob_worker(task_id, typ, body):
    from autocut_mcp import text_cut, burn_subtitles
    from engine.asr import transcribe as _tr
    try:
        if typ == "transcribe":
            with _LOCK:
                TASKS[task_id]["log"].append("转写中… (首次使用会下载 whisper 模型)")
            res = _tr(body.get("path", ""), model=str(body.get("model") or "small"),
                      language=str(body.get("language") or ""),
                      vad=bool(body.get("vad", True)),
                      word_timestamps=bool(body.get("word_timestamps", False)),
                      output_dir=OUT_DIR)
            with _LOCK:
                TASKS[task_id]["status"] = "done"
                TASKS[task_id]["progress"] = 100
                TASKS[task_id]["output"] = os.path.basename(res["srt"])
                TASKS[task_id]["log"].append("转写完成: %s (后端=%s)" % (res["srt"], res["backend"]))
                TASKS[task_id]["result"] = {"srt": res["srt"], "json": res["json"],
                                            "segments": res["segments_count"], "language": res["language"]}
            return
        out_name = str(body.get("output_name") or (typ + "_" + time.strftime("%H%M%S"))).strip()
        if not out_name.lower().endswith(".mp4"):
            out_name += ".mp4"
        output = os.path.join(OUT_DIR, out_name)
        if typ == "text_cut":
            raw = text_cut(body.get("video", ""), body.get("srt", ""), output,
                           mode=str(body.get("mode") or "keep"),
                           match=str(body.get("match") or "keywords"),
                           value=str(body.get("value") or ""),
                           padding=float(body.get("padding") or 0.0),
                           min_keep_dur=float(body.get("min_keep_dur") or 0.0),
                           width=int(body.get("width") or 1920),
                           height=int(body.get("height") or 1080),
                           fps=int(body.get("fps") or 25))
            d = json.loads(raw)
        else:  # burn
            raw = burn_subtitles(body.get("video", ""), body.get("srt", ""), output,
                                 fontname=str(body.get("fontname") or "Microsoft YaHei"),
                                 fontsize=int(body.get("fontsize") or 0),
                                 primary=str(body.get("primary") or "#FFFFFF"),
                                 outline=float(body.get("outline") or 1.6),
                                 shadow=float(body.get("shadow") or 0.8),
                                 margin_v=int(body.get("margin_v") or 48))
            d = json.loads(raw)
        with _LOCK:
            TASKS[task_id]["status"] = "done"
            TASKS[task_id]["progress"] = 100
            TASKS[task_id]["output"] = out_name
            TASKS[task_id]["log"].append("完成: " + output)
            TASKS[task_id]["result"] = d
    except Exception as e:
        import traceback
        with _LOCK:
            TASKS[task_id]["status"] = "error"
            TASKS[task_id]["error"] = str(e)
            TASKS[task_id]["log"].append("[ERROR] " + str(e))
            TASKS[task_id]["log"].append(traceback.format_exc()[-1200:])


@app.route("/api/textjob", methods=["POST"])
def api_textjob():
    """异步任务。body: {type: transcribe|text_cut|burn, ...参数}。"""
    body = request.get_json(force=True, silent=True) or {}
    typ = body.get("type")
    if typ not in ("transcribe", "text_cut", "burn"):
        return jsonify({"error": "type 必须是 transcribe/text_cut/burn"}), 400
    if typ != "transcribe" and (not body.get("video") or not body.get("srt")):
        return jsonify({"error": "video 与 srt 必填"}), 400
    if typ == "transcribe" and not body.get("path"):
        return jsonify({"error": "path 必填"}), 400
    task_id = uuid.uuid4().hex[:12]
    os.makedirs(RUNS_DIR, exist_ok=True)
    with _LOCK:
        TASKS[task_id] = {"id": task_id, "status": "running", "log": [],
                          "progress": 0, "output": None, "error": None}
    threading.Thread(target=_textjob_worker, args=(task_id, typ, body), daemon=True).start()
    return jsonify({"task_id": task_id})




# ---------------- 一键智能成片 (01 主入口) ----------------
def _autoedit_worker(task_id, body):
    import time as _t
    try:
        from autocut_mcp import _load_templates
        tpl = _load_templates().get("auto_edit")
        if not tpl:
            raise RuntimeError("找不到 auto_edit 模板")
        out_name = str(body.get("output_name") or ("auto_" + time.strftime("%H%M%S"))).strip()
        if not out_name.lower().endswith(".mp4"):
            out_name += ".mp4"
        output = os.path.join(OUT_DIR, out_name)
        task_dir = os.path.join(RUNS_DIR, task_id)
        os.makedirs(task_dir, exist_ok=True)
        cfg = {"src_video": str(body.get("video") or ""), "mode": str(body.get("mode") or "talk"),
               "segments_json": str(body.get("segments_json") or ""),
               "subtitle_srt": str(body.get("subtitle_srt") or ""),
               "fillers": str(body.get("fillers") or "嗯,啊,呃,那个,这个,就是,然后,um,uh"),
               "bgm_file": str(body.get("bgm_file") or ""),
               "target_dur": float(body.get("target_dur") or 30),
               "beat_unit": int(body.get("beat_unit") or 1),
               "threshold": float(body.get("threshold") or -35),
               "min_silence": float(body.get("min_silence") or 0.3),
               "padding": float(body.get("padding") or 0.15),
               "width": int(body.get("width") or 1920),
               "height": int(body.get("height") or 1080),
               "fps": int(body.get("fps") or 25),
               "workdir": task_dir, "output": output, "output_name": out_name}
        if not cfg["src_video"] or not os.path.isfile(cfg["src_video"]):
            raise RuntimeError("video 无效或文件不存在")

        def _log(m):
            with _LOCK:
                TASKS[task_id]["log"].append(str(m))
                TASKS[task_id]["progress"] = min(99, len(TASKS[task_id]["log"]))

        with _LOCK:
            TASKS[task_id]["log"].append("开始一键智能成片 (mode=%s)" % cfg["mode"])
        res = dict(tpl.run(cfg, log=_log) or {})
        with _LOCK:
            TASKS[task_id]["status"] = "done"
            TASKS[task_id]["progress"] = 100
            TASKS[task_id]["output"] = out_name
            TASKS[task_id]["log"].append("完成: %s" % output)
            TASKS[task_id]["result"] = res
    except Exception as e:
        import traceback
        with _LOCK:
            TASKS[task_id]["status"] = "error"
            TASKS[task_id]["error"] = str(e)
            TASKS[task_id]["log"].append("[ERROR] " + str(e))
            TASKS[task_id]["log"].append(traceback.format_exc()[-1200:])


@app.route("/api/autoedit", methods=["POST"])
def api_autoedit():
    """一键智能成片。body: {mode, video, bgm_file?, segments_json?, subtitle_srt?, ...}。"""
    body = request.get_json(force=True, silent=True) or {}
    mode = str(body.get("mode") or "talk")
    if mode not in ("talk", "clean", "beat"):
        return jsonify({"error": "mode 必须是 talk/clean/beat"}), 400
    if not body.get("video"):
        return jsonify({"error": "video 必填"}), 400
    task_id = uuid.uuid4().hex[:12]
    os.makedirs(RUNS_DIR, exist_ok=True)
    with _LOCK:
        TASKS[task_id] = {"id": task_id, "status": "running", "log": [],
                          "progress": 0, "output": None, "error": None}
    threading.Thread(target=_autoedit_worker, args=(task_id, body), daemon=True).start()
    return jsonify({"task_id": task_id})

if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    print(f"UI: http://127.0.0.1:{port}  (root={ROOT})")
    app.run(host="127.0.0.1", port=port, debug=False)