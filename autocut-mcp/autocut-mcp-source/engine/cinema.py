# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""电影化重构图核心(纯函数, 零渲染) —— 解决"景别单一/取景太近/选错人"的根本问题。

参考(GitHub 调研, 详见 docs/AUTODIRECTOR.md):
  * 景别: oximedia-shots(ShotType ECU..ELS, 用脸/皮肤占比); segmo(头顶留白~15%, 目标占比)
  * 取景: Auto-Framing-For-OBS(headroom 预设/组取景/最大放大/死区); Unity Cinemachine 构图
  * 同人: AVZoom centroid tracker / FaceMesh; VideoFace2.0 重识别思想(用归一化特征给同人稳定 ID)

本模块只做"几何/规则"层: 给 人脸框 + 想要景别 -> 返回安全裁窗(留头顶白/不裁太近/超限回退)。
不含 ffmpeg/MediaPipe, 便于单测; 视觉检测由 speaker_map 提供(脸框/唇动)。
"""
from __future__ import annotations

# 人脸占画幅宽的比例 -> 景别(参考电影语言常用档)
SHOT_BANDS = [
    ("closeup", 0.25),   # face_w >= 25% frame_w
    ("mcu", 0.17),       # 中近景
    ("medium", 0.08),    # 中景
    ("wide", 0.0),       # 全景/环境
]
# 各景别重构图时的"目标人脸占比"(输出画幅宽的比例), 越靠后越松(回退顺序)
SHOT_TARGET = {
    "wide": 0.06,
    "medium": 0.13,
    "mcu": 0.20,
    "closeup": 0.28,
}
# 竖向: 人脸中心在裁窗高度的位置(越大越靠下, 顶部留白越多)
SHOT_FCY = {"wide": 0.50, "medium": 0.42, "mcu": 0.40, "closeup": 0.44}
LOOSER_ORDER = ["closeup", "mcu", "medium", "wide"]


def shot_scale_of_face(face_w: float, frame_w: float) -> str:
    """按"脸宽/画幅宽"给出现有取景的景别标签。face_w/frame_w 为像素或归一化皆可。"""
    if frame_w <= 0 or face_w <= 0:
        return "wide"
    frac = float(face_w) / float(frame_w)
    for label, thr in SHOT_BANDS:
        if frac >= thr:
            return label
    return "wide"


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _targets_for(shot: str):
    """从目标景别开始, 依次尝试"更松"档(取不到安全构图就回退, 而不是硬裁到过近)。"""
    try:
        i = LOOSER_ORDER.index(shot)
    except ValueError:
        i = LOOSER_ORDER.index("mcu")
    return [LOOSER_ORDER[k] for k in range(i, len(LOOSER_ORDER))]


def framing_rect(frame_w: float, frame_h: float, face, shot: str = "mcu",
                 out_w: float = 1920.0, out_h: float = 1080.0,
                 headroom: float = 0.14, max_face_frac: float = 0.34,
                 min_crop_frac: float = 0.22):
    """给 人脸框 face=(x,y,w,h)(源像素) + 目标景别, 返回安全裁窗。

    构图原则:
      * 目标景别优先; 若源画幅/脸太小凑不出该景别 -> 依次回退更松一档, 直至全景;
      * 头顶留白 headroom(占裁窗高度比例), 下方留出肩部空间;
      * 最近极限 min_crop_frac(裁窗宽 >= 源宽比例, 防无限放大/画质崩);
      * 最远脸占比上限 max_face_frac(防裁到"太近")。
    返回 dict {x0,y0,w,h, shot, feasible, reason, face_frac} 或全景回退(feasible=False)。
    """
    frame_w, frame_h = float(frame_w), float(frame_h)
    x, y, w, h = (float(v) for v in face)
    cx, cy = x + w / 2, y + h / 2
    aspect = float(out_h) / float(out_w)

    if shot == "wide":
        return {"x0": 0.0, "y0": 0.0, "w": frame_w, "h": frame_h,
                "shot": "wide", "feasible": True, "reason": "wide",
                "face_frac": round(w / frame_w, 4)}

    for target_shot in _targets_for(shot):
        target = SHOT_TARGET[target_shot]
        # 需要的裁窗宽: 让脸占 target 比例
        cw = w / target
        # 不能裁出比源更宽的窗; 也不能小于最近极限(过近/过度放大)
        if cw < frame_w * min_crop_frac:
            cw = frame_w * min_crop_frac
        if cw > frame_w:
            continue                      # 脸已大到该档放不下 -> 回退更松
        ch = cw * aspect
        if ch > frame_h:
            ch = frame_h
            cw = ch / aspect
            if cw > frame_w:
                continue
        # 实际人脸占比(可能因 clamp 而变化)
        actual = w / cw
        if actual > max_face_frac:
            continue                      # 仍太近 -> 回退更松
        # 水平: 人脸居中, 限制在源内
        x0 = _clamp(cx - cw / 2, 0.0, frame_w - cw)
        # 竖向: 人脸中心放在裁窗 fcy 处(fcy 越大脸越靠下 -> 顶部留白越多)
        fcy = SHOT_FCY.get(target_shot, 0.42)
        y0 = _clamp(cy - fcy * ch, 0.0, frame_h - ch)
        head_px = y - y0                  # 头顶到裁窗顶的距离
        if head_px < headroom * ch * 0.25:
            # 顶部没有足够留白(人贴画面上缘) -> 试更松档或干脆更靠下再检
            y0 = _clamp(y - headroom * ch, 0.0, frame_h - ch)
            head_px = y - y0
        if head_px < headroom * ch * 0.25 and target_shot != "wide":
            continue
        return {"x0": round(x0, 1), "y0": round(y0, 1),
                "w": round(cw, 1), "h": round(ch, 1),
                "shot": target_shot, "feasible": True,
                "reason": "framed_%s" % target_shot,
                "face_frac": round(actual, 4), "headroom_px": round(head_px, 1)}

    # 全景回退(绝不硬裁到过近/错误取景)
    return {"x0": 0.0, "y0": 0.0, "w": frame_w, "h": frame_h,
            "shot": "wide", "feasible": False, "reason": "fallback_wide",
            "face_frac": round(w / frame_w, 4)}


def union_rect(boxes, pad_x: float = 0.5, pad_y: float = 0.8, frame_w=None, frame_h=None):
    """多张脸 -> 包含它们的取景窗(两 shot / 组 shot)。
    pad_x/pad_y: 以"最宽脸宽"为单位的左右/上下外扩, 给肩部/呼吸空间。
    返回 (x0,y0,w,h) 并 clamp 到 frame_w/frame_h(若有)。"""
    if not boxes:
        return None
    xs0 = min(b[0] for b in boxes)
    ys0 = min(b[1] for b in boxes)
    xs1 = max(b[0] + b[2] for b in boxes)
    ys1 = max(b[1] + b[3] for b in boxes)
    fw_ref = max(b[2] for b in boxes)
    fh_ref = max(b[3] for b in boxes)
    x0 = xs0 - fw_ref * pad_x
    y0 = ys0 - fh_ref * pad_y * 0.5
    x1 = xs1 + fw_ref * pad_x
    y1 = ys1 + fh_ref * pad_y * 0.5
    if frame_w:
        x0, x1 = _clamp(x0, 0, frame_w), _clamp(x1, 0, frame_w)
    if frame_h:
        y0, y1 = _clamp(y0, 0, frame_h), _clamp(y1, 0, frame_h)
    return (round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1))


# ================================================================ 同人弱特征
def person_descriptor(landmarks, eye_l=None, eye_r=None):
    """由人脸关键点算"尺度/旋转不变"的弱身份特征, 用于跨句/跨镜识别同一人。

    landmarks: [(x,y), ...](像素或归一化均可, 需与 eye_l/eye_r 同一坐标系);
    无眼睛基准时退回"以脸框对角为尺"。返回排序后的归一化距离元组(可哈希比较)。
    """
    pts = list(landmarks)
    if len(pts) < 2:
        return None
    if eye_l and eye_r:
        base = float(((eye_r[0] - eye_l[0]) ** 2 + (eye_r[1] - eye_l[1]) ** 2) ** 0.5)
    else:
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        base = float(((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** 0.5)
    if base <= 1e-9:
        return None
    # 选 "参考点=各点均值", 距离向量 -> 尺度归一 + 排序, 抗平移/旋转
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    dists = sorted(((p[0] - cx) ** 2 + (p[1] - cy) ** 2) ** 0.5 / base for p in pts)
    return tuple(round(d, 4) for d in dists)


def same_person(d1, d2, thr: float = 0.06) -> bool:
    """两个弱特征是否同人(平均归一化距离差 < thr)。"""
    if not d1 or not d2 or len(d1) != len(d2):
        return False
    n = len(d1)
    diff = sum(abs(a - b) for a, b in zip(d1, d2)) / n
    return diff < thr


# ================================================================ 反应镜头启发式
def reaction_score(lip_std: float, audio_r: float, is_speaker: bool) -> float:
    """给"是否作为反应镜头主体"打分(0-1)。
    反应镜头主体 = 不说话的听者(唇动小/与音频无关) 但 脸上有戏(情绪/微动靠嘴外特征, 这里用保守基线)。
    """
    if is_speaker:
        return 0.0
    # 非说话: 唇动越小越"安静听", 但与音频负相关弱即可; 用 1 - 归一化唇动 作基线
    norm = min(1.0, float(lip_std) / 0.15)
    return round(max(0.0, 1.0 - norm), 3) if audio_r <= 0.0 else round(max(0.0, 1.0 - norm) * 0.5, 3)
