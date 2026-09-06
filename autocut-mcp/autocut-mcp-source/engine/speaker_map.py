# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""自动剪辑升级核心 v2: 说话人-机位自动校验/修正

v2 相对 v1 的关键修复(基于多机位素材实测失败定位的根因):
  1. 统一时间轴校准: 各机位起录时间不同(偏移可达±15s), v1 直接用源机位的时间窗
     到其它机位测量 -> 量错窗口 -> 分数全是噪声。
     v2: 逐段用【源机位台词窗口音频】在【目标机位全音频】上做 FFT 互相关定位,
         得到每段每机位的局部对齐起点(local alignment), 不再依赖全局偏移。
  2. 音频参考: v1 用 camera[0] 的音频做全机位共享参考 -> 该机位麦若拾音差则全错。
     v2: 每台机位用自己的音频RMS做 SyncNet 相关(本机位麦克风最靠近本机位主体)。
  3. 预检缓存: 每台机位只抽帧/检测一次(帧+人脸+唇部缓存到磁盘), 各段复用,
     避免 v1 每段每机位重复抽帧的 24 分钟耗时。
  4. 内容镜头回退: 若某段没有任何机位检出"正在说话的脸"(如火山大战/反应镜头),
     保留原剪辑(不强行改到错误机位)。

原理(SyncNet式, 参考 360-autocam step3b):
  说话的脸 = lip_std 大(嘴在动) 且 r>0(唇动与自身音频RMS同步)
"""
import os, sys, json, subprocess, tempfile, wave
import numpy as np
import cv2, imageio_ffmpeg
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision

FF = imageio_ffmpeg.get_ffmpeg_exe()
MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "face_landmarker.task")
SR = 16000

_LANDMARKER = None
_HAAR = None
_AUDIO_CACHE = {}

UPPER = [13, 312]; LOWER = [14, 317]
EYE_L = [33, 133]; EYE_R = [362, 263]


def _get_models():
    global _LANDMARKER, _HAAR
    if _LANDMARKER is None:
        opts = mp_vision.FaceLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=MODEL),
            running_mode=mp_vision.RunningMode.IMAGE, num_faces=6,
            min_face_detection_confidence=0.25, min_tracking_confidence=0.25)
        _LANDMARKER = mp_vision.FaceLandmarker.create_from_options(opts)
    if _HAAR is None:
        _HAAR = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return _LANDMARKER, _HAAR


# ---------------------------------------------------------------- 音频
def cam_audio(cam):
    """整条机位音频(16k mono float), 缓存。wav 用 md5 稳定命名, 跨进程复用。"""
    if cam not in _AUDIO_CACHE:
        import hashlib
        wav = os.path.join(tempfile.gettempdir(), "_spk_%s.wav" % hashlib.md5(cam.encode()).hexdigest()[:16])
        if not os.path.isfile(wav):
            subprocess.run([FF, "-hide_banner", "-y", "-i", cam, "-vn", "-ac", "1",
                            "-ar", str(SR), wav], capture_output=True, text=True)
        with wave.open(wav, "rb") as w:
            n = w.getnframes()
            _AUDIO_CACHE[cam] = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768
    return _AUDIO_CACHE[cam]


def _audio_rms(audio, t0, t1, fps):
    """音频 t0..t1 的 RMS 序列(每帧 = 1/fps 秒)。"""
    i0, i1 = max(0, int(t0 * SR)), min(len(audio), int(t1 * SR))
    seg = audio[i0:i1]
    hop = int(SR / fps)
    n = int(len(seg) / hop)
    if n < 4:
        return np.zeros(0)
    return np.array([np.sqrt(np.mean(seg[i * hop:(i + 1) * hop] ** 2) + 1e-12) for i in range(n)])


def find_lag_in(src_win, cam_audio_full, thr=0.22, topk=8):
    """src_win: 源机位台词窗口音频(短). cam_audio_full: 目标机位全音频(长).
    原始波形互相关取 top-K 候选峰, 再按归一化皮尔逊r选最优,
    避免高能量区假峰; 对麦克风拾音差异鲁棒。
    返回 (start_sec, conf=r) 或 None(该机位未录到这段台词)。
    """
    n = len(src_win)
    if n < SR * 0.8 or n >= len(cam_audio_full):
        return None
    try:
        from scipy.signal import correlate
    except Exception:
        return None
    cc = correlate(cam_audio_full, src_win, mode="valid")
    if cc.size == 0:
        return None
    k = min(topk, cc.size)
    idx = np.argpartition(np.abs(cc), -k)[-k:]
    best_r, best_start = -1.0, None
    for p in idx:
        seg = cam_audio_full[p:p + n]
        if np.std(seg) < 1e-9 or np.std(src_win) < 1e-9:
            continue
        r = float(np.corrcoef(src_win, seg)[0, 1])
        if r > best_r:
            best_r, best_start = r, p
    if best_start is None or best_r < thr:
        return None
    return best_start / SR, best_r


# ---------------------------------------------------------------- 帧检测
def _detect_frame(img):
    """检测一帧内的脸: 返回 [(x,y,w,h,lip,cx)]。Haar 粗检 + MediaPipe 唇部。"""
    lm, haar = _get_models()
    H, W = img.shape[:2]
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(g, None, fx=0.25, fy=0.25)
    faces = haar.detectMultiScale(small, 1.1, 4, minSize=(20, 20))
    out = []
    for (sx, sy, sw, sh) in faces:
        x, y, w, h = sx * 4, sy * 4, sw * 4, sh * 4
        pad = int(w * 1.2)
        x0, y0 = max(0, x - pad // 2), max(0, y - pad)
        x1, y1 = min(W, x + w + pad // 2), min(H, y + h + pad * 2)
        crop = img[y0:y1, x0:x1]
        if crop.shape[0] < 40 or crop.shape[1] < 40:
            continue
        # 提速: 把裁剪区缩小到 <=256 宽再送 MediaPipe
        sc = 1.0
        if crop.shape[1] > 256:
            sc = 256.0 / crop.shape[1]
            crop = cv2.resize(crop, None, fx=sc, fy=sc)
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        if not res.face_landmarks:
            continue
        fl = res.face_landmarks[0]
        u = np.array([(fl[i].x * crop.shape[1], fl[i].y * crop.shape[0]) for i in UPPER]).mean(0)
        l = np.array([(fl[i].x * crop.shape[1], fl[i].y * crop.shape[0]) for i in LOWER]).mean(0)
        el = np.array([(fl[i].x * crop.shape[1], fl[i].y * crop.shape[0]) for i in EYE_L]).mean(0)
        er = np.array([(fl[i].x * crop.shape[1], fl[i].y * crop.shape[0]) for i in EYE_R]).mean(0)
        fw = np.linalg.norm(el - er)
        lip = float(np.linalg.norm(u - l) / max(fw, 1e-6))
        cx = (x + w / 2) / W
        out.append((x, y, w, h, lip, cx))
    return out


def precompute_frames(cam, lo, hi, fps, cache_dir):
    """抽帧+检测一次, 结果缓存为 npz: times[], dets[](每帧 dets)。
    返回 (times, dets) 并写入 cache_dir/camX.npz。"""
    if not os.path.isdir(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
    key = os.path.join(cache_dir, "det_%s_%dfps.npz" % (os.path.splitext(os.path.basename(cam))[0], int(fps)))
    if os.path.exists(key):
        z = np.load(key, allow_pickle=True)
        return z["times"].tolist(), z["dets"].tolist()
    dur = max(0.2, hi - lo)
    d = tempfile.mkdtemp(prefix="spkpre_")
    subprocess.run([FF, "-hide_banner", "-y", "-ss", f"{lo:.3f}", "-t", f"{dur:.3f}",
                    "-i", cam, "-r", str(fps), os.path.join(d, "f%04d.png")],
                   capture_output=True, text=True)
    fns = sorted(os.listdir(d))
    times, dets = [], []
    for k, fn in enumerate(fns):
        img = cv2.imread(os.path.join(d, fn))
        if img is None:
            continue
        dets.append(_detect_frame(img))
        times.append(lo + k / fps)
        if (k + 1) % 40 == 0:
            print("    %s frames %d/%d" % (os.path.basename(cam), k + 1, len(fns)), flush=True)
    for fn in fns:
        try:
            os.remove(os.path.join(d, fn))
        except Exception:
            pass
    try:
        os.rmdir(d)
    except Exception:
        pass
    np.savez(key, times=np.array(times), dets=np.array(dets, dtype=object))
    return times, dets


# ---------------------------------------------------------------- 评分
def _track_faces(dets_window):
    """把窗口内逐帧脸框按位置连成轨迹: 返回 [ {cx, lips:[len(dets_window)]} ]。
    用绝对帧索引, 轨迹统一重采样到窗口全长(缺失帧用中值填充)。"""
    n_frames = len(dets_window)
    tracks = []   # {cx, idx:[frame], lips:[lip]}
    for fi, dets in enumerate(dets_window):
        used = [False] * len(dets)
        for tr in tracks:
            best, bd = None, 0.25
            for i, dd in enumerate(dets):
                if used[i]:
                    continue
                x, y, w, h, lip, cx = dd
                if abs(tr["cx"] - cx) < bd:
                    bd = abs(tr["cx"] - cx); best = i
            if best is not None:
                x, y, w, h, lip, cx = dets[best]
                tr["idx"].append(fi); tr["lips"].append(lip); tr["cx"] = cx; used[best] = True
        for i, dd in enumerate(dets):
            if not used[i]:
                x, y, w, h, lip, cx = dd
                tracks.append({"cx": cx, "idx": [fi], "lips": [lip]})
    out = []
    for tr in tracks:
        full = np.full(n_frames, np.nan)
        for fi, lp in zip(tr["idx"], tr["lips"]):
            full[fi] = lp
        vals = full[~np.isnan(full)]
        med = float(np.median(vals)) if len(vals) else 0.0
        full = np.where(np.isnan(full), med, full)
        out.append({"cx": tr["cx"], "lips": full.tolist()})
    return out


def _best_r(lips, rms_c, maxlag=2):
    """唇动序列与音频RMS序列在 ±maxlag 帧内搜索最大正相关。
    说话人唇动领先/滞后音频数十毫秒, 固定对齐易得负相关。"""
    best = 0.0
    L = len(lips)
    for lag in range(-maxlag, maxlag + 1):
        if lag >= 0:
            a, b = lips[lag:], rms_c[:L - lag] if lag else rms_c
        else:
            a, b = lips[:lag], rms_c[-lag:]
        n = min(len(a), len(b))
        if n < 6:
            continue
        if np.std(a[:n]) < 1e-9 or np.std(b[:n]) < 1e-9:
            continue
        r = float(np.corrcoef(a[:n], b[:n])[0, 1])
        if not np.isnan(r):
            best = max(best, r)
    return best


def _score_window(cam, t0, t1, fps, cam_times, cam_dets, cam_audio_data):
    """对某机位在 [t0,t1] 打分(自身音频 SyncNet)。返回 (score, best_std, best_r, nfaces)。"""
    i0 = int(np.searchsorted(cam_times, t0))
    i1 = int(np.searchsorted(cam_times, t1))
    dets_window = cam_dets[i0:i1]
    if len(dets_window) < 4:
        return 0.0, 0.0, 0.0, 0
    tracks = _track_faces(dets_window)
    rms = _audio_rms(cam_audio_data, t0, t1, fps)
    if len(rms) < 4:
        return 0.0, 0.0, 0.0, 0
    n = min(len(rms), len(dets_window))
    rms_c = rms[:n]
    faces = []
    for tr in tracks:
        lips = np.asarray(tr["lips"], float)[:n]
        if len(lips) < 6:
            continue
        std = float(np.std(lips))
        r = _best_r(lips, rms_c)
        faces.append({"cx": round(tr["cx"], 3), "lip_std": round(std, 4), "r": round(r, 3)})
    if not faces:
        return 0.0, 0.0, 0.0, 0
    speaking = [f for f in faces if f["r"] > 0.0]
    if not speaking:
        best = max(faces, key=lambda f: f["lip_std"])
        return 0.0, best["lip_std"], best["r"], len(faces)
    best = max(speaking, key=lambda f: f["lip_std"] * (0.3 + f["r"]))
    return best["lip_std"] * (0.3 + best["r"]), best["lip_std"], best["r"], len(faces)


# ---------------------------------------------------------------- 主入口
def verify_segments(cutpoints, lip_fps=6, workdir=None, ref_video=None):
    """对每个台词段, 局部对齐后在所有机位打分, 选出正在说话的机位。"""
    cameras = cutpoints["cameras"]
    segs = cutpoints["segments"]
    WD = workdir or tempfile.mkdtemp(prefix="spkmap_")
    os.makedirs(WD, exist_ok=True)
    cache_dir = os.path.join(WD, "det")
    print("预加载机位音频...", flush=True)
    audio = {ci: cam_audio(cam) for ci, cam in enumerate(cameras)}
    print("计算每段局部对齐...", flush=True)
    # 逐段: 源窗口 -> 各机位局部起点
    align = []   # 每段: {src_ci, wins:[{ci, c_t0, c_t1, conf} or None]}
    src_audio_cache = {}
    for si, seg in enumerate(segs):
        t0, t1 = seg["t0"], seg["t1"]
        src = seg.get("src", "")
        src_ci = None
        for i, cam in enumerate(cameras):
            if os.path.basename(cam) == os.path.basename(src):
                src_ci = i
                break
        dur = t1 - t0
        wins = [None] * len(cameras)
        if src_ci is not None:
            # 源机位窗口音频(加 0.6s 上下文, 提升互相关鲁棒性)
            ctx = 0.6
            win_audio = _audio_rms_win(audio[src_ci], max(0, t0 - ctx), t1 + ctx)
            for ci, cam in enumerate(cameras):
                if ci == src_ci:
                    wins[ci] = {"c_t0": t0, "c_t1": t1, "conf": 1.0}
                    continue
                found = find_lag_in(win_audio, audio[ci])
                if found is None:
                    continue
                start, conf = found
                # 对齐起点对应源窗口起点 -> 目标窗口 = start + ctx .. start + ctx + dur
                c_t0 = max(0, start + ctx)
                c_t1 = c_t0 + dur
                wins[ci] = {"c_t0": c_t0, "c_t1": c_t1, "conf": conf}
        align.append({"src_ci": src_ci, "src": src, "t0": t0, "t1": t1, "note": seg.get("note", ""), "wins": wins})

    # 每机位需要覆盖的时间范围 = 各段对齐窗口的并集
    lo_hi = {}
    for a in align:
        for ci, w in enumerate(a["wins"]):
            if w is None:
                continue
            lo, hi = w["c_t0"], w["c_t1"]
            if ci not in lo_hi:
                lo_hi[ci] = [lo, hi]
            else:
                lo_hi[ci][0] = min(lo_hi[ci][0], lo)
                lo_hi[ci][1] = max(lo_hi[ci][1], hi)

    print("逐机位抽帧检测(仅一次, 缓存)...", flush=True)
    cam_data = {}
    for ci, cam in enumerate(cameras):
        if ci not in lo_hi:
            continue
        lo, hi = lo_hi[ci]
        print("  检测 %s  %.2f-%.2fs" % (os.path.basename(cam), lo, hi), flush=True)
        times, dets = precompute_frames(cam, lo, hi, lip_fps, cache_dir)
        cam_data[ci] = (times, dets)

    print("逐段评分...", flush=True)
    out = []
    for si, a in enumerate(align):
        cam_scores = []
        for ci, w in enumerate(a["wins"]):
            if w is None or ci not in cam_data:
                continue
            times, dets = cam_data[ci]
            score, std, r, nf = _score_window(cameras[ci], w["c_t0"], w["c_t1"], lip_fps,
                                              times, dets, audio[ci])
            cam_scores.append({"cam": os.path.basename(cameras[ci]),
                               "path": cameras[ci],
                               "score": score, "std": std, "r": r,
                               "nfaces": nf, "conf": w["conf"],
                               "c_t0": round(w["c_t0"], 3), "c_t1": round(w["c_t1"], 3)})
        cam_scores.sort(key=lambda c: -c["score"])
        best = cam_scores[0] if cam_scores else None
        src_base = os.path.basename(a["src"]) if a["src"] else None
        src_score = next((c["score"] for c in cam_scores if c["cam"] == src_base), 0.0)
        src_std = next((c["std"] for c in cam_scores if c["cam"] == src_base), 0.0)
        # 阈值: 分数下限 / 相对优势 / 唇动下限 / 音频对齐置信度下限
        MIN_SCORE, MARGIN, STD_FLOOR, CONF_FLOOR = 0.04, 0.015, 0.04, 0.40
        # 保守切换: 推荐机位证据强(std大+r) 且 原机位无说话信号(std小)
        #           且 目标机位音频对齐置信度足够高(conf低=窗口可能没对齐, 唇检结果不可信)。
        # 原因: SyncNet唇检对小/远脸不敏感, 原机位std小不代表说话人不在,
        #       只代表"检测不到"; 此时不得仅凭他机位微弱信号切换(防误报)。
        #       conf 低时目标机位的时间窗可能根本没对上这台机位录到的这段台词,
        #       此时测出的唇动属于其它时刻, 切过去就是错切。
        switch = bool(best and best["cam"] != src_base
                      and best["score"] >= MIN_SCORE
                      and best["std"] >= STD_FLOOR
                      and best["conf"] >= CONF_FLOOR
                      and src_std < STD_FLOOR
                      and best["score"] >= src_score + MARGIN)
        if switch:
            best_cam, best_score = best["cam"], best["score"]
        else:
            best_cam, best_score = src_base, src_score
        is_original = bool(src_base and best_cam == src_base)
        has_speaker = bool(src_score > 0.0 or switch)
        out.append({
            "index": si, "t0": a["t0"], "t1": a["t1"], "note": a["note"],
            "src": a["src"], "best_cam": best_cam, "best_score": best_score,
            "original_ok": is_original, "has_speaker": has_speaker, "cams": cam_scores,
        })
        mark = "OK " if is_original else ("FIX" if has_speaker else "KEEP")
        print("[%02d] %s t=%s %s 原:%s -> %s (score=%.3f, speaker=%s)" %
              (si, mark, "%.2f-%.2f" % (a["t0"], a["t1"]), a["note"][:10],
               os.path.basename(a["src"]) if a["src"] else "-", best_cam, best_score, has_speaker), flush=True)
    return out


# ---------------------------------------------------------------- 说话人逐帧轨迹(dynamic closeup 用)
def _probe_size(cam):
    r = subprocess.run([FF, "-i", cam], capture_output=True, text=True)
    import re
    for l in r.stderr.splitlines():
        if "Video:" in l:
            m = re.search(r"(\d{3,5})x(\d{3,5})", l)
            if m:
                return int(m.group(1)), int(m.group(2))
    return 3840, 2160


def speaker_track(cam, t0, t1, fps=6, workdir=None):
    """返回 (src_w, src_h, pts)。
    pts = 说话人逐帧中心(像素) + 框尺寸: (t, cx, cy, fw, fh), 按时间升序。
    说话人判据 = 唇动-音频相关最高的轨迹(r>0 且 lip_std 大), 无则返回 []。
    复用 precompute_frames 磁盘缓存, 与 verify_segments 共享检测结果。"""
    # 每段用独立缓存目录, 避免 precompute_frames 缓存 key 不含时间窗导致同机位多段互相污染
    cache_dir = os.path.join(workdir, "det_%d_%d" % (int(t0 * 10), int(t1 * 10))) if workdir else tempfile.mkdtemp(prefix="spkdc_")
    times, dets = precompute_frames(cam, t0, t1, fps, cache_dir)
    W, H = _probe_size(cam)
    n = len(dets)
    # 组装轨迹(保留像素中心)
    tracks = []
    for fi, detlist in enumerate(dets):
        used = [False] * len(detlist)
        for tr in tracks:
            best, bd = None, 0.25
            for i, dd in enumerate(detlist):
                if used[i]:
                    continue
                x, y, w, h, lip, c = dd
                if abs(tr["cx"] - c) < bd:
                    bd = abs(tr["cx"] - c); best = i
            if best is not None:
                x, y, w, h, lip, c = detlist[best]
                tr["idx"].append(fi); tr["lips"].append(lip); tr["cx"] = c
                tr["px"].append((x + w / 2, y + h / 2, w, h)); used[best] = True
        for i, dd in enumerate(detlist):
            if not used[i]:
                x, y, w, h, lip, c = dd
                tracks.append({"cx": c, "idx": [fi], "lips": [lip],
                               "px": [(x + w / 2, y + h / 2, w, h)]})
    if not tracks:
        return W, H, []
    # 每条轨迹: 缺失帧用中值填充 -> 算 std 与 r
    audio = cam_audio(cam)
    rms = _audio_rms(audio, t0, t1, fps)
    best, best_score = None, 0.0
    for tr in tracks:
        full = np.full(n, np.nan)
        for fi, lp in zip(tr["idx"], tr["lips"]):
            full[fi] = lp
        vals = full[~np.isnan(full)]
        med = float(np.median(vals)) if len(vals) else 0.0
        full = np.where(np.isnan(full), med, full)
        nr = min(len(full), len(rms))
        r = _best_r(full[:nr], rms[:nr]) if nr >= 6 else 0.0
        std = float(np.std(full[:nr])) if nr else 0.0
        score = std * (0.3 + r) if r > 0 else 0.0
        if score > best_score:
            best_score = score; best = tr
    if best is None or best_score <= 0:
        return W, H, []
    # 将说话人轨迹按时间对齐到检测帧, 缺失帧线性插值
    pts = []
    for k, t in enumerate(times):
        px = [p for p, fi in zip(best["px"], best["idx"]) if fi == k]
        if px:
            pts.append((t, px[0][0], px[0][1], px[0][2], px[0][3]))
        elif pts:
            pts.append((t, pts[-1][1], pts[-1][2], pts[-1][3], pts[-1][4]))
    return W, H, pts


def _audio_rms_win(audio, t0, t1):
    i0, i1 = max(0, int(t0 * SR)), min(len(audio), int(t1 * SR))
    return audio[i0:i1]


# ================================================================ turn 级人物分析(构图用, 不渲染)
def _track_faces_full(dets_window):
    """把窗口内逐帧脸框连成轨迹, 保留 唇动序列 + 中心像素 + 脸尺寸。
    与 _track_faces 相同的位置连轨逻辑, 但额外保留 px/size 供取景。"""
    n_frames = len(dets_window)
    tracks = []
    for fi, dets in enumerate(dets_window):
        used = [False] * len(dets)
        for tr in tracks:
            best, bd = None, 0.25
            for i, dd in enumerate(dets):
                if used[i]:
                    continue
                x, y, w, h, lip, cx = dd
                if abs(tr["cx"] - cx) < bd:
                    bd = abs(tr["cx"] - cx); best = i
            if best is not None:
                x, y, w, h, lip, cx = dets[best]
                tr["idx"].append(fi); tr["lips"].append(float(lip)); tr["cx"] = cx
                tr["px"].append((x + w / 2.0, y + h / 2.0, float(w), float(h)))
                used[best] = True
        for i, dd in enumerate(dets):
            if not used[i]:
                x, y, w, h, lip, cx = dd
                tracks.append({"cx": cx, "idx": [fi], "lips": [float(lip)],
                               "px": [(x + w / 2.0, y + h / 2.0, float(w), float(h))]})
    out = []
    for tr in tracks:
        full = np.full(n_frames, np.nan)
        for fi, lp in zip(tr["idx"], tr["lips"]):
            full[fi] = lp
        vals = full[~np.isnan(full)]
        med = float(np.median(vals)) if len(vals) else 0.0
        full = np.where(np.isnan(full), med, full)
        out.append({"cx": tr["cx"], "lips": full.tolist(), "px": tr["px"]})
    return out


def analyze_unit_faces(cam, t0, t1, times, dets, audio, fps=5,
                         min_r=0.08, min_std=0.02, min_score=0.005):
    """分析一个 turn 窗口里"谁在说话/谁是听者", 供导演分镜(不渲染)。

    返回 dict:
      nfaces: 检测到的脸轨迹数
      tracks: [{cx_px,cy_px,fw,fh,lip_std,r,score,n_obs}](按 score 降序)
      speaker: score>0 的最高分轨迹(正在说话), 无则 None
      listeners: 其余轨迹(非说话/微弱信号)
    判据与 verify_segments 一致(SyncNet式): r>0 且 lip_std 大 -> 在说话。
    """
    i0 = int(np.searchsorted(np.asarray(times), t0))
    i1 = int(np.searchsorted(np.asarray(times), t1))
    dets_window = list(dets)[i0:i1]
    if len(dets_window) < 4:
        return None
    tracks = _track_faces_full(dets_window)
    rms = _audio_rms(audio, t0, t1, fps)
    if len(rms) < 4:
        return None
    n = min(len(rms), len(dets_window))
    rms_c = rms[:n]
    scored = []
    for tr in tracks:
        lips = np.asarray(tr["lips"], float)[:n]
        if len(lips) < 6:
            continue
        std = float(np.std(lips))
        r = _best_r(lips, rms_c)
        score = std * (0.3 + r) if r > 0.0 else 0.0
        xs = [p[0] for p in tr["px"]]; ys = [p[1] for p in tr["px"]]
        ws = [p[2] for p in tr["px"]]; hs = [p[3] for p in tr["px"]]
        scored.append({
            "cx_px": round(float(np.median(xs)), 1), "cy_px": round(float(np.median(ys)), 1),
            "fw": round(float(np.median(ws)), 1), "fh": round(float(np.median(hs)), 1),
            "lip_std": round(std, 4), "r": round(r, 3), "score": round(score, 4),
            "n_obs": len(tr["px"]),
        })
    scored.sort(key=lambda t: -t["score"])
    # ????: ?? r/??/????????"???", ????? None(????????)
    speaker = next((t for t in scored
                    if t["r"] >= min_r and t["lip_std"] >= min_std
                    and t["score"] >= min_score), None)
    listeners = [t for t in scored if t is not speaker]
    return {"nfaces": len(scored), "tracks": scored, "speaker": speaker,
            "listeners": listeners}

