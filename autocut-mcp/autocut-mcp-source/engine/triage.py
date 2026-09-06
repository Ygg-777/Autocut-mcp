# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""素材筛选(Triage)核心层 —— 从"含花絮/NG 的原始素材(rushes)"里找出每个剧本节点可用的 take。

背景(参考 montaj 的 select-takes 思想, 见 THIRD_PARTY_NOTICES):
  * 导演素材 != 成片: 一个镜头文件里可能含场记板/倒计时/导演口令/NG重来/花絮闲聊,
    也可能同一句台词拍了多条(take1/take2...)。
  * 本层不做拼接/渲染, 只做"筛选+报告":
      输入: rushes 的语音识别结果(segments) + 可选"剧本分镜"(script pieces)
      输出: 每段素材的分类(可用/NG/花絮/场记前缀/未匹配闲聊...), 以及每个剧本节点命中哪些
            take(含精确时间窗、命中关键词、干净度评分) —— 供 agent/人在拼接前审查。

本模块零第三方依赖(纯文本/时间处理)。ASR 结果格式沿用 engine/asr.py:
  segments: [{start,end,text}]  (秒)
"""
from __future__ import annotations

import json
import re

# ================================================================ 文本归一化
_WORD_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def norm_tokens(text: str) -> list:
    """小写 + 只留字母数字/中文 -> token 列表(英文按词, 中文按字)。"""
    return _WORD_RE.findall(str(text or "").lower())


def norm_flat(text: str) -> str:
    """小写 + 只留字母数字/中文 + 空格连接, 用于短语匹配。"""
    return " ".join(norm_tokens(text))


def has_phrase(flat_text: str, phrase: str) -> bool:
    """在空格连接的归一化文本里查短语(短语也先归一化)。"""
    p = norm_flat(phrase)
    return bool(p) and (" " + p + " " in " " + flat_text + " ")


# ================================================================ NG / 花絮 词典
# 注意: 只收录"明显出戏/场外"信号, 避免误伤台词里的 sorry/again 等正常对白。
DIRECTOR_CUES = [  # 场记板 / 导演口令 / 倒计时
    "action", "and action", "cut", "cut it", "that's a take", "that's a wrap",
    "rolling", "speed", "quiet on set", "quiet on the set", "stand by",
    "places", "take one", "take two", "take three", "take four", "from the top",
    "one more time", "again please", "3 2 1", "three two one", "one two three",
    "clapper", "slate", "mark it",
    "开拍", "开始", "准备", "三二一", "三 二 一", "321", "咔", "卡", "再来", "重来",
    "场记", "打板", "第几条",
]
BLOOPER_META = [  # 花絮: 忘词/笑场/导演提词/演员闲聊/抱怨 (避免误伤剧本里的 sorry)
    "i forgot", "forgot my", "my line", "what's my line", "i'll say like",
    "you're gonna say", "say like", "am i a legend", "dehydrated", "stupid asl",
    "i messed up", "messed up", "that was bad", "let me try", "from the top",
    "sorry sorry", "wait wait", "hold on", "no no no", "i can't",
    "can we do", "let's do it again", "one more time", "stop stop",
    "忘词", "词忘了", "说错了", "搞错了", "再来一次", "不好意思", "导演",
]

# 判定"素材属于剧本内容"的最低关键词覆盖率(低于则多为泛词/闲聊)
MATCH_COV_MIN = 0.15


# ================================================================ 剧本分镜结构
def load_script_pieces(path: str) -> list:
    """读取剧本分镜 JSON: [{id,title,lines:[...],keywords:[...]}, ...]"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    pieces = []
    for i, p in enumerate(data or []):
        kw = [norm_flat(k) for k in (p.get("keywords") or [])]
        kw = [k for k in kw if k]
        pieces.append({
            "id": p.get("id") or ("p%d" % (i + 1)),
            "title": p.get("title") or "",
            "scene": p.get("scene") or "",
            "lines": [str(x) for x in (p.get("lines") or [])],
            "keywords": kw,
        })
    return pieces


# ================================================================ take 切分
def split_takes(segs, merge_gap: float = 1.5, min_take_dur: float = 0.4):
    """把句级字幕按静音间隙切成连续拍摄段(take)。返回 [{t0,t1,segs,text}]"""
    ordered = sorted((s for s in (segs or [])
                      if float(s.get("end", 0)) > float(s.get("start", 0))),
                     key=lambda x: float(x["start"]))
    takes, cur, prev_end = [], [], None
    for s in ordered:
        if cur and (float(s["start"]) - prev_end) > merge_gap:
            takes.append(cur)
            cur = []
        cur.append(s)
        prev_end = float(s["end"])
    if cur:
        takes.append(cur)
    out = []
    for tk in takes:
        t0, t1 = float(tk[0]["start"]), float(tk[-1]["end"])
        if t1 - t0 < min_take_dur:
            continue
        out.append({
            "t0": round(t0, 2), "t1": round(t1, 2),
            "segs": [{"start": round(float(x["start"]), 2),
                      "end": round(float(x["end"]), 2),
                      "text": str(x["text"])} for x in tk],
            "text": " ".join(str(x["text"]) for x in tk),
        })
    return out


def _flags_in(flat_text: str):
    """在归一化文本中找场记/花絮信号 -> {director:[...], blooper:[...]}"""
    found = {"director": [], "blooper": []}
    for ph in DIRECTOR_CUES:
        if has_phrase(flat_text, ph):
            found["director"].append(ph)
    for ph in BLOOPER_META:
        if has_phrase(flat_text, ph):
            found["blooper"].append(ph)
    return found


def _dup_sentences(segs):
    """同一句台词在文件里出现>=2次(归一化后) -> 文件内含重拍。"""
    seen, dups = {}, set()
    for s in segs:
        k = norm_flat(s["text"])
        if len(k) < 4:
            continue
        key = " ".join(k.split()[:6])
        if key in seen:
            dups.add(key)
        seen[key] = True
    return sorted(dups)


# ================================================================ 单素材分析
def analyze_clip(rel: str, segs, duration: float | None = None,
                 min_take_dur: float = 0.4) -> dict:
    """分析一段素材: 语音覆盖 + take 列表 + NG/花絮/重拍标记。"""
    segs = [s for s in (segs or []) if float(s.get("end", 0)) > float(s.get("start", 0))]
    segs.sort(key=lambda x: float(x["start"]))
    speech = sum(float(s["end"]) - float(s["start"]) for s in segs)
    dup = _dup_sentences(segs)
    total = float(duration) if duration else (float(segs[-1]["end"]) if segs else 0.0)
    takes = split_takes(segs, min_take_dur=min_take_dur)
    takes_out = []
    for i, tk in enumerate(takes, 1):
        flat = norm_flat(tk["text"])
        flags = _flags_in(flat)
        first_flat = norm_flat(tk["segs"][0]["text"])
        prefix_ph = _flags_in(first_flat)["director"]
        mid_flags = {"director": [], "blooper": list(flags["blooper"])}
        for ph in flags["director"]:
            if ph not in prefix_ph:
                mid_flags["director"].append(ph)
        content_t0 = float(tk["segs"][0]["start"])
        if prefix_ph and len(tk["segs"]) > 1:   # 前缀是场记口令 -> 内容从第二句起
            content_t0 = float(tk["segs"][1]["start"])
        clean = not (mid_flags["director"] or mid_flags["blooper"])
        takes_out.append({
            "index": i,
            "t0": round(tk["t0"], 2), "t1": round(tk["t1"], 2),
            "dur": round(tk["t1"] - tk["t0"], 2),
            "content_t0": round(content_t0, 2),
            "content_t1": round(tk["t1"], 2),
            "text": tk["text"], "segs": tk["segs"],
            "flags": flags, "mid_flags": mid_flags,
            "has_director_prefix": bool(prefix_ph),
            "director_prefix": prefix_ph, "clean": clean, "n_segs": len(tk["segs"]),
        })
    return {
        "file": rel, "path": rel, "n_segs": len(segs),
        "speech_s": round(speech, 2), "duration": round(total, 2),
        "speech_ratio": round(speech / total, 3) if total > 0 else 0.0,
        "lang": "", "dup_retake": dup, "takes": takes_out,
        "verdict": "no_speech" if not segs else ("retake_mess" if dup else "material"),
    }


# ================================================================ 剧本对齐
def _kw_hits(flat_text: str, keywords: list):
    return [kw for kw in keywords if has_phrase(flat_text, kw)]


def _best_span(segs, keywords: list, pad: float = 0.35, max_gap_segs: int = 2):
    """在 take 的句级字幕里, 找"关键词最密的一整段"作为该剧本节点的精确窗口。

    避免把"命中同一泛词(如 china/book)但属于上一节点"的句子一起圈进来:
      逐句打分 -> 命中句按"允许 max_gap_segs 句间隔"聚成块 -> 取累计分最高块。
    返回 (w0, w1, span_text, hit_segs) 或 None。
    """
    scored = []
    for s in segs:
        flat = norm_flat(s["text"])
        hits = _kw_hits(flat, keywords)
        scored.append({"seg": s, "score": len(hits), "hits": hits})
    blocks, cur, last_hit_idx = [], None, None
    for i, sc in enumerate(scored):
        if sc["score"] > 0:
            if cur is None or (last_hit_idx is not None and (i - last_hit_idx - 1) > max_gap_segs):
                if cur:
                    blocks.append(cur)
                cur = {"idxs": [], "score": 0, "hit_segs": []}
            cur["idxs"].append(i)
            cur["score"] += sc["score"]
            cur["hit_segs"].append(sc["seg"])
            last_hit_idx = i
    if cur:
        blocks.append(cur)
    if not blocks:
        return None
    best = max(blocks, key=lambda b: (b["score"], len(b["hit_segs"])))
    w0 = max(0.0, float(best["hit_segs"][0]["start"]) - pad)
    w1 = min(float(best["hit_segs"][-1]["end"]) + pad, float(segs[-1]["end"]))
    span_text = " ".join(str(s["text"]) for s in best["hit_segs"])
    return (round(w0, 2), round(w1, 2), span_text, best["hit_segs"])


def align_piece(piece: dict, clips: list, pad: float = 0.35):
    """在一个素材分析结果集上找命中该剧本节点的候选窗口。"""
    cands = []
    for cl in clips:
        for tk in cl["takes"]:
            sp = _best_span(tk["segs"], piece["keywords"], pad=pad)
            if not sp:
                continue
            w0, w1, span_text, hit_segs = sp
            win_flat = norm_flat(span_text)
            win_flags = _flags_in(win_flat)
            window_clean = not (win_flags["director"] or win_flags["blooper"])
            hits = _kw_hits(win_flat, piece["keywords"])
            cands.append({
                "file": cl["file"], "take_index": tk["index"],
                "t0": w0, "t1": w1, "dur": round(w1 - w0, 2),
                "kw_hits": hits, "kw_count": len(hits),
                "coverage": round(len(hits) / len(piece["keywords"]), 3),
                "window_text": span_text, "flags": win_flags,
                "clean": window_clean and tk["clean"],
                "take_clean": tk["clean"],
                "dup_in_file": bool(cl.get("dup_retake")),
            })
    cands.sort(key=lambda c: (c["clean"], not c["dup_in_file"],
                              c["coverage"], -c["dur"]), reverse=True)
    return cands


def triage(rushes, pieces=None, duration_map=None):
    """主入口。rushes: {rel_path: {lang,segs}}; pieces: 剧本分镜列表(可空)。
    返回 {clips, pieces:[{piece,candidates,best,matched_clips}]}"""
    clips = []
    if isinstance(rushes, dict):
        for rel, v in rushes.items():
            a = analyze_clip(rel, v.get("segs") or [],
                             duration=(duration_map or {}).get(rel))
            a["lang"] = v.get("lang") or ""
            clips.append(a)
    else:
        clips = list(rushes)
    clips.sort(key=lambda c: c["file"])

    pieces_out = []
    clip_matched = {c["file"]: 0.0 for c in clips}   # file -> 最高覆盖率
    for p in pieces or []:
        cands = align_piece(p, clips)
        best = cands[0] if cands else None
        for c in cands:
            if c["coverage"] > clip_matched[c["file"]]:
                clip_matched[c["file"]] = c["coverage"]
        pieces_out.append({"piece": p, "candidates": cands, "best": best,
                           "matched_clips": sorted({c["file"] for c in cands
                                                    if c["coverage"] >= MATCH_COV_MIN})})

    # 综合判定(优先级: 无语音 > 重拍 > NG/花絮 > 场记前缀 > 未匹配闲聊 > 干净可用)
    for c in clips:
        if not c["takes"]:
            c["verdict"] = "no_speech"
            continue
        if c.get("dup_retake"):
            c["verdict"] = "retake_mess"
            continue
        any_ng = any((t.get("mid_flags", {}).get("director") or
                    t.get("mid_flags", {}).get("blooper")) for t in c["takes"])
        any_prefix = any(t["has_director_prefix"] for t in c["takes"])
        if any_ng:
            c["verdict"] = "ng_included"
        elif any_prefix:
            c["verdict"] = "prefix_ng"
        elif pieces and clip_matched[c["file"]] < MATCH_COV_MIN:
            c["verdict"] = "unmatched_talk"
        elif pieces:
            c["verdict"] = "clean_material"
        else:
            c["verdict"] = "material"
    return {"clips": clips, "pieces": pieces_out}


def build_report(rushes, pieces=None, duration_map=None) -> dict:
    """生成可复用 JSON 报告(会写入 runs 目录供 GUI/agent 审查)。"""
    t = triage(rushes, pieces=pieces, duration_map=duration_map)
    clips_sum = [{
        "file": c["file"], "lang": c.get("lang", ""), "n_segs": c["n_segs"],
        "speech_s": c["speech_s"], "duration": c["duration"],
        "speech_ratio": c["speech_ratio"], "verdict": c["verdict"],
        "dup_retake": c["dup_retake"],
        "takes": [{"index": x["index"], "t0": x["t0"], "t1": x["t1"],
                   "content_t0": x["content_t0"], "content_t1": x["content_t1"],
                   "clean": x["clean"],
                   "has_director_prefix": x["has_director_prefix"],
                   "flags": x["flags"], "text": x["text"][:300]}
                  for x in c["takes"]],
    } for c in t["clips"]]
    pieces_sum = []
    for po in t["pieces"]:
        p = po["piece"]
        cands = [{"file": c["file"], "t0": c["t0"], "t1": c["t1"], "dur": c["dur"],
                  "clean": c["clean"], "coverage": c["coverage"],
                  "kw_hits": c["kw_hits"], "flags": c["flags"],
                  "window_text": c["window_text"][:220]}
                 for c in po["candidates"][:10]]
        pieces_sum.append({
            "piece_id": p["id"], "title": p["title"],
            "n_candidates": len(po["candidates"]),
            "matched_clips": po["matched_clips"],
            "best": po["best"], "candidates": cands,
        })
    return {"clips": clips_sum, "pieces": pieces_sum}


