# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""导演分镜决策(纯规则, 不渲染) —— 把"分析结果"变成"镜头语言"。

输入: 一个 turn 的 meta(是否 beat 开场/强调/时长) + 人物分析(谁在说/听者)
输出: {role, subject, reason}。 role: wide/medium/mcu/closeup(极少数金句用)/reaction。
构图数值在 engine/cinema.py; 这里只做"这段该给什么镜头/给谁"。
参考 docs/AUTODIRECTOR.md 第 2.3 节(shot role 语法)。
"""
from __future__ import annotations

import re

# 强调句触发词(出现则本句值得"推近"): 含金句/冲突/揭穿/笑点/结局
EMPHASIS_TOKENS = {
    "precious", "official", "sure", "fake", "movie", "never", "rowling",
    "j.k.", "scared", "whoa", "amazing", "volcano", "doll", "wizard", "kung",
    "咕噜", "假的", "电影", "官方", "罗琳", "吓", "宝贵",
}


def norm_tokens(text: str) -> set:
    toks = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", str(text or "").lower()))
    return toks


def unit_emphasis(text: str, beat_id: str = "") -> bool:
    """是否强调/关键句: 命中触发词, 或 beat 本身是情绪点(p2 模仿/p5 揭穿/p6 结局)。"""
    toks = norm_tokens(text)
    if toks & EMPHASIS_TOKENS:
        return True
    if beat_id in ("p2", "p5", "p6"):
        return True
    return False


def choose_shot(meta: dict, analysis: dict | None, last_roles=()):
    """给一个 turn 选镜头。meta: {first_of_piece, emphasis, dur_s, beat_id}
    analysis: analyze_unit_faces 的返回(或 None)。
    返回 {role, subject, reason}。"""
    dur_s = float(meta.get("dur_s") or 0.0)
    first = bool(meta.get("first_of_piece"))
    emph = bool(meta.get("emphasis")) or unit_emphasis(
        meta.get("text", ""), meta.get("beat_id", ""))

    if analysis is None or not analysis.get("tracks"):
        return {"role": "wide", "subject": "group", "reason": "no_face_data_wide"}
    sp = analysis.get("speaker")
    nf = int(analysis.get("nfaces", 0))
    n_listen = len(analysis.get("listeners") or [])
    if sp is None:
        # 没人检出在说话 -> 整组/环境(如 Gollum 夸张表演脸太小)
        return {"role": "wide", "subject": "group", "reason": "no_active_speaker_wide"}

    role, subject, reason = "mcu", "speaker", "solo_speaker_mcu"
    if first:
        if nf >= 3:
            role, subject, reason = "wide", "group", "beat_open_establish"
        else:
            role, subject, reason = "mcu", "speaker", "beat_open_small_group"
    elif emph:
        # 强调句: 用中近景即可, 避免过近; 连续强调且有人在听 -> 反应镜头换气
        role, subject, reason = "mcu", "speaker", "emphasis_mcu"
        if n_listen >= 1 and dur_s >= 3.0 and (len(last_roles) >= 1 and last_roles[-1] == "mcu"):
            role, subject, reason = "reaction", "listener", "emphasis_reaction_break"
    elif n_listen >= 1:
        if dur_s >= 4.0:
            role, subject, reason = "medium", "speaker_plus_listener", "dialogue_two_shot"
        else:
            role, subject, reason = "mcu", "speaker", "speaker_mcu_short"
    else:
        role, subject, reason = "mcu", "speaker", "solo_speaker_mcu"

    # 防单调: 同一景别连续 >=3 段 -> 换气(整组可用则给 wide, 否则中景<->中近景交替)
    same = sum(1 for r in reversed(last_roles) if r == role)
    if same >= 2 and role != "wide":
        if nf >= 3:
            role, subject, reason = "wide", "group", "variety_breath_wide"
        else:
            role = "medium" if role == "mcu" else "mcu"
            subject = "speaker" if role == "mcu" else subject
            reason = "variety_switch"
    return {"role": role, "subject": subject, "reason": reason}
