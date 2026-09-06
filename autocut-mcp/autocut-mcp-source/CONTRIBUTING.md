# Contributing（贡献指南）

感谢你想参与。本项目为开源核心（Apache-2.0），本地优先、不上传任何素材。

## 报告问题 / 提需求
- 在 Issues 里使用“使用反馈 / Bug 报告”模板。
- **只贴日志/截图，不要附原始视频或素材**（隐私：你的素材只在你本机）。

## 开发与测试
```bash
pip install -r requirements.txt
python tests/smoke_test.py        # 离线单测
```

## 提交约定（DCO）
本仓库要求每个提交带开发者签名（Developer Certificate of Origin）：
```bash
git commit -s -m "your message"
```
提交即表示你同意：你拥有/有权提交该改动，并同意其以本项目 Apache-2.0 许可分发。
不带 `Signed-off-by` 的提交会被要求补上。

## 不要提交什么
- 原始素材 / 成片 / 模型权重（`*.model`、`third_party/`）
- 任何密钥、Token、个人资料
- 商业层功能（pro/ 是私有仓库，不进本仓库）
