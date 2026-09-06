# Autocut MCP Plugin

通用自动剪辑 · 人机合剪：AI 出分镜 → 人在 UI 审片/改镜头 → 增量渲染。
MCP 工具 + Web 控制台（06 审片修片），本地优先、不上传素材。Apache-2.0。

## 快速开始
pip install -r requirements.txt

# 生成分镜计划（素材目录 + 台词文本）
python direct_cut.py --rushes ./my_footage --script ./script.txt

# 打开审片 UI
python ui_app.py   # http://127.0.0.1:8001 → 06 审片修片

## 说明
- 详细使用见 README.md 完整版（zip 内自带）
- 第三方声明见 THIRD_PARTY_NOTICES.md
