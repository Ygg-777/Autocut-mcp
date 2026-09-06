[README.md](https://github.com/user-attachments/files/31889142/README.md)
# Autocut：通用自动剪辑引擎 · MCP Server · Web 人机合剪

Autocut **不是“MCP”本身**，而是一套三合一的本地自动剪辑方案：

1. **剪辑引擎（engine/）**：转写、素材筛选(Triage)、说话人判定(TalkNet)、景别构图、多轨合成、调色、字幕——纯本地 ffmpeg，不依赖任何 MCP。
2. **MCP Server（`autocut_mcp.py`）**：把引擎能力封装成 AI 可调用的工具（MCP 是 Anthropic 提出的开放协议，类似“AI 的 USB-C 接口”：写一次，任何支持 MCP 的 Agent/客户端都能连上使用）。
3. **Web 审片 UI（`ui_app.py`）**：给人用的人机合剪台——AI 出分镜，人在页面里审片/改镜头，增量渲染只重编改动镜头。

不绑定任何素材库：所有工具都接收调用方给定的绝对路径。

> 工作定位：**AI 出分镜 → 人审分镜 → 增量渲染** 的人机合剪（不是“一键全自动出片”的玄学）。
> 它把“从花絮里挑对 take、判断谁在说话、给什么景别、反应镜头放哪”拆成可检查、可修改、可量化的中间步骤。

> 什么是 MCP：MCP(Model Context Protocol) 是一套让 AI 模型与外部工具/数据统一连接的开放标准；`autocut_mcp.py` 就是一个“提供剪辑工具的 MCP Server”，让支持 MCP 的 AI Agent 能直接调用剪辑能力。

## 特性

- **素材筛选（Triage）**：自动识别 NG / 场记口令 / 花絮闲聊 / 同句重拍，按剧本分镜挑出每个节点的可用 take 与精确时间窗。
- **多模态分析**：语音识别(ASR) + 说话人判定(TalkNet-ASD 可选) + 人脸/唇动 + 景别/构图建议（纯几何规则，可单测）。
- **导演分镜**：把剧本(文本)逐句对齐到素材，输出 shot-role 计划（全景/中景/中近景/近景/反应）。
- **可视化审片 UI（页签 06）**：内嵌成片播放器、彩色时间轴(按 beat 着色)、每镜头缩略图/胶片预览、改源窗口/景别、一键换同 beat 其它 take、**增量渲染**（只重编改动的镜头，其余复用缓存拼接）。
- **MCP 工具**：探测 / 转写 / 文本选段 / 多轨时间线 / 调色 / 字幕烧录 / 模板等，任意 agent 可直接调用。
- **本地优先**：全部在本地跑，无云端上传。

## 快速开始

```bash
python -m pip install -r requirements.txt      # 核心依赖
python autocut_mcp.py --test                    # 打印已注册的 MCP 工具
python autocut_mcp.py                           # 以 stdio 启动 MCP 服务
python ui_app.py [端口]                         # Web 控制台 http://127.0.0.1:8001
```

可选能力（按需安装）：
- 语音识别：`pip install faster-whisper`（推荐）或 `openai-whisper`
- 深度说话人判定 TalkNet-ASD（研究用预训练模型，仅运行时本地下载，不随仓库分发）：
  ```bash
  git clone https://github.com/TaoRuijie/TalkNet-ASD third_party/TalkNet-ASD
  # 模型 pretrain_TalkSet.model 下载方式见 docs/AUTODIRECTOR.md / THIRD_PARTY_NOTICES.md
  ```
  > `third_party/`、`*.model` 已在 `.gitignore`，不会进仓库。

环境变量：`AUTOCUT_OUT` 输出目录、`AUTOCUT_RUNS` 工作目录、`AUTOCUT_ROOT`(UI) 素材浏览根目录。

## 3 分钟跑通（导演分镜 → UI 审片）

准备：**素材目录**（放拍摄/多 take 的视频） + **台词文本**（`.txt`，每行一段台词，可选 `角色: 台词`）。

```bash
# 1) 生成分镜计划(triage_report / user_plan / v6_director_plan JSON)
python direct_cut.py --rushes ./my_footage --script ./script.txt

# 已有转写可避免现场 ASR(推荐大素材):
# python direct_cut.py --rushes ./my_footage --script ./script.txt --transcripts ./transcripts.json

# 2) 打开审片 UI
python ui_app.py          # 浏览器 http://127.0.0.1:8001 → [06 审片修片]
```

在 06 里：内嵌成片播放器 + 时间轴色块 + 每镜头缩略图；可改源/入出点/景别、一键换同拍其它 take，
然后“增量渲染”——只重编改动的镜头，其余复用缓存。

## 工作流（分镜 → 审片 → 增量渲染）

```text
剧本(文本) + 原始素材
   │
   ▼ ① 素材筛选 triage         去掉 NG/花絮/场记，每个节点找出可用 take（含精确时间窗）
   ▼ ② 导演分镜 director      脚本逐句 ↔ 素材对齐 → shot-role 计划（句子级，含说话人/景别/反应）
   ▼ ③ Web 审片 UI (06)        内嵌成片 + 时间轴 + 缩略图；可改源/入出点/景别/换候选
   ▼ ④ 增量渲染               只重编改动的镜头，其余复用片段缓存 → 快速出片
```

中间产物都是 JSON（`AUTOCUT_RUNS/`）：
- `triage_report.json`：每段素材分类 + 每个节点的候选 take
- `v6_director_plan.json` / `user_plan.json`：镜头计划（源文件/时间窗/景别/台词）
- 片段渲染缓存：`autocut_runs/segcache/`（按「源+入出点」哈希，改动镜头才重编码）

## MCP 工具

| 工具 | 说明 |
|---|---|
| `probe_media` / `scan_media` | 探测/扫描媒体 |
| `transcribe_media` / `srt_to_segments` | 语音识别 / SRT 解析 |
| `plan_text_cut` / `text_cut` | 文本驱动剪辑规划/成片（AutoCut 式） |
| `burn_subtitles` | SRT → 硬字幕 |
| `clip_concat` / `render_timeline` / `extract_audio` | 拼接 / 多轨合成 / 音频裁切 |
| `triage_rushes` | **素材筛选**：NG/花絮识别 + 按剧本挑可用 take |
| `auto_edit` / `apply_color_grade` | 一键智能精简 / 达芬奇风格调色 |
| `list_templates` / `run_template` | 模板系统（去静音/卡点/四分屏/多机位校验…） |

## 目录

```text
autocut_mcp.py        MCP 服务入口
ui_app.py             Web 审片/控制台后端
autocut_render.py     多轨渲染引擎
engine/               核心（triage/director/cinema/speaker_map/asd_talknet/asr/segments/grade/…）
templates/            内置剪辑模板
ui/                   Web 前端
tests/smoke_test.py   离线单测
docs/AUTODIRECTOR.md  迭代研究与技术路线
```

## 测试 / CI

```bash
python tests/smoke_test.py      # 离线单测（无需 GPU/网络）
```
仓库含 GitHub Actions：`python -m compileall …` + 冒烟测试。

## 限制与诚实说明

- 全自动只到“分镜计划”，成片前建议在 UI 里人工审一遍（审美/反应镜头目前没有可靠的通用模型）。
- 说话人/人物精度受素材约束：宽景小脸、非同条双机位会有上限，工具会检测不到时自动回退到整组镜头而不是硬选人。
- 若拍摄为多机位同 take，可再用多机位对齐/说话人校验做机位切换（见 `docs/AUTODIRECTOR.md`）。

## 许可

Apache-2.0，第三方说明见 `THIRD_PARTY_NOTICES.md`；作者信息见 `NOTICE`。
TalkNet-ASD 代码为 MIT、预训练模型由其作者分发，仅运行时本地使用，不随本仓库再分发。


## 开源版 vs Pro（Open Core 边界）

本仓库为 **开源核心（Apache-2.0）**，提供通用引擎与人机合剪 UI，定位是“AI 出分镜 → 人审分镜 → 增量渲染”。

以下高价值能力在**商业层（pro/ 私有仓库，不在本仓库开源）**迭代，避免污染核心：
- 高级导演决策与成片质量自动评分（需人工成片数据训练）
- 云端批量渲染 / 协作审片 SaaS / 私有化部署
- 行业垂直模板与风格包（短剧 / 口播 / 电商 / 混剪）
- 评测数据集与训练基准

对接方式与边界见 pro/ARCHITECTURE.md（该目录被 .gitignore 排除，不会出现在本开源仓库）。
