# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""direct_cut: 从 素材目录 + 台词文本 -> 生成导演分镜计划(JSON), 供 Web UI(06 审片)审阅/增量渲染。

用法:
  python direct_cut.py --rushes <素材目录> --script <script.txt|script.json>
  # script.txt: 每行一段台词/分镜描述(空行跳过), 或 "角色: 台词"
  # script.json: [{id,title,lines:[..],keywords:[..]}, ...]
  # 可选: --transcripts <已有 transcripts.json {rel:{lang,segs:[{start,end,text}]}}>, 避免现场转写
  # 可选: --model small (现场转写用的 whisper 模型), --out <输出目录,默认 <本脚本目录>/autocut_runs>
输出(到 out/autocut_runs):
  triage_report.json   素材筛选(含每节点的候选 take)
  user_plan.json       可直接进 UI 06 的镜头计划
  v6_director_plan.json 同 user_plan(兼容 UI 回退读取)
之后: python ui_app.py  -> 打开 06 审片修片 查看/修改/增量渲染。
"""
import argparse, io, json, os, re, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from engine import triage as T

STOP = set("""the a an and or but if then so to of in on for with at by from as is are was were be been being
it its this that these those i you he she we they my your his her our their me him us them what which who
oh well just really about do did does can could would should will have has had not no yes one two""".split())


def norm_keywords(text):
    out = []
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        if tok not in STOP and len(tok) > 1:
            out.append(tok)
    # 中文: 整行作为短语, 保留 2+ 字片段
    cn = re.sub(r"[^\u4e00-\u9fff]", "", text)
    if cn:
        out.append(cn)
        for i in range(0, max(1, len(cn) - 1)):
            big = cn[i:i + 2]
            if len(big) >= 2:
                out.append(big)
    return list(dict.fromkeys(out))


def load_script(path):
    if path.endswith(".json"):
        return T.load_script_pieces(path)
    pieces, lines = [], [ln.strip() for ln in io.open(path, encoding="utf-8") if ln.strip()]
    for i, ln in enumerate(lines, 1):
        txt = ln.split(":", 1)[1].strip() if ":" in ln[:12] else ln
        pieces.append({"id": "p%d" % i, "title": ln[:40], "lines": [txt], "keywords": norm_keywords(txt)})
    return pieces


def scan_videos(d):
    vids = []
    for root, _, names in os.walk(d):
        for nm in sorted(names):
            if os.path.splitext(nm)[1].lower() in {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mts"}:
                vids.append(os.path.join(root, nm))
    return sorted(vids)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rushes", required=True, help="原始素材目录(会递归扫视频)")
    ap.add_argument("--script", required=True, help="台词文本(.txt 每行一段) 或 分镜 JSON")
    ap.add_argument("--transcripts", default="", help="已有 transcripts.json {rel:{lang,segs}}; 缺省则现场转写")
    ap.add_argument("--model", default="small", help="现场转写模型(默认 small)")
    ap.add_argument("--out", default=os.path.join(ROOT, "autocut_runs"), help="输出目录(默认仓库 autocut_runs)")
    a = ap.parse_args()

    if not os.path.isdir(a.rushes):
        sys.exit("素材目录不存在: " + a.rushes)
    vids = scan_videos(a.rushes)
    if not vids:
        sys.exit("目录里没有视频: " + a.rushes)

    outdir = os.path.abspath(a.out)
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(os.path.join(outdir, "analysis"), exist_ok=True)

    # 1) 转写
    trans = {}
    if a.transcripts and os.path.isfile(a.transcripts):
        trans = json.load(io.open(a.transcripts, encoding="utf-8"))
    rushes, missing = {}, []
    for v in vids:
        rel = os.path.relpath(v, a.rushes).replace("\\", "/")
        hit = trans.get(rel) or trans.get(v) or trans.get(os.path.basename(v))
        if hit:
            rushes[rel] = {"lang": hit.get("lang", ""), "segs": hit.get("segs", [])}
        else:
            missing.append(v)
    if missing:
        print("现场转写 %d 个文件 (模型 %s) ..." % (len(missing), a.model))
        from engine.asr import transcribe as _tr
        for v in missing:
            rel = os.path.relpath(v, a.rushes).replace("\\", "/")
            res = _tr(v, model=a.model, vad=True, word_timestamps=False)
            rushes[rel] = {"lang": res.get("language", ""), "segs": res.get("segments", [])}
        with io.open(os.path.join(outdir, "analysis", "transcripts.json"), "w", encoding="utf-8") as f:
            json.dump(rushes, f, ensure_ascii=False, indent=1)

    pieces = load_script(a.script)
    if not pieces:
        sys.exit("没有读到任何剧本分镜")

    # 2) triage + 分镜计划(每个节点取 best 窗口为一个镜头)
    report = T.build_report(rushes, pieces=pieces)
    with io.open(os.path.join(outdir, "triage_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    plan = []
    for po in report["pieces"]:
        b = po["best"]
        if not b:
            print("[skip] %s 无可用 take" % po["piece_id"])
            continue
        plan.append({"beat": po["piece_id"], "text": b.get("window_text", ""),
                     "src_rel": b["file"], "t0": b["t0"], "t1": b["t1"],
                     "role": "wide", "reason": "auto:%s" % (po.get("title") or po["piece_id"])})
    up = {"plan": plan, "output_url": None}
    for name in ("user_plan.json", "v6_director_plan.json"):
        with io.open(os.path.join(outdir, name), "w", encoding="utf-8") as f:
            json.dump(up, f, ensure_ascii=False, indent=1)

    print("\n完成: %d 个镜头 -> %s" % (len(plan), outdir))
    print("triage_report.json / user_plan.json / v6_director_plan.json 已写入")
    print("下一步: 在此目录运行  python ui_app.py  然后打开浏览器进入 [06 审片修片]")


if __name__ == "__main__":
    main()
