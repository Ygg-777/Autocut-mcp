# Third-Party Notices

本项目(autocut_mcp_plugin)以 Apache-2.0 开源。以下为随项目分发或
被本项目参考/依赖的第三方组件的版权与许可说明。所有信息以各上游
仓库的 LICENSE 为准; 如有出入, 以上游为准。

## 1. 随仓库分发的文件 (Bundled)

### engine/models/face_landmarker.task
- 来源: Google MediaPipe Face Landmarker 模型
- 许可证: Apache License 2.0
- 版权: Copyright Google LLC
- 上游: https://github.com/google-ai-edge/mediapipe
- 模型地址: https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task
- 用途: 多机位说话人校验模板(speaker_map.py)的人脸/唇部关键点检测

## 1.5 运行时外部模型/代码(下载后本地使用, 不并入仓库分发)

### TalkNet-ASD (代码 MIT; 预训练模型由作者分发)
- 代码: TaoRuijie/TalkNet-ASD —— https://github.com/TaoRuijie/TalkNet-ASD
- 代码许可: MIT (见 third_party/TalkNet-ASD/LICENSE.md)
- 用途: engine/asd_talknet.py 说话人判定(可选); 作者预训练模型 pretrain_TalkSet.model
  (TalkSet 预训练, 用于 AVA/Columbia 之外的"真实拍摄"素材) 由运行时从外部镜像下载,
  权重版权归作者, 仅作本地研究/实验, 不随本仓库(Apache-2.0)再分发。
- 依赖: torch/torchaudio/python_speech_features/scenedetect/gdown/pandas 等(见其 requirement.txt)

## 2. 设计参考(仅借鉴架构思路, 未复制其代码)
- mli/autocut —— "用文本编辑器剪视频"工作流 (https://github.com/mli/autocut)
- modelscope/FunClip —— ASR 片段 + 文本选段 (https://github.com/modelscope/FunClip)
- WyattBlue/auto-editor —— timeline 作为剪辑结果范式 (https://github.com/WyattBlue/auto-editor)
- KyaniteLabs/kinocut —— Agent 用 MCP 视频工具约定 (https://github.com/KyaniteLabs/kinocut)
- metiu1/editorvideo-ai / @makemyclip/editor —— MCP/Web/引擎共用核心 (https://github.com/metiu1/editorvideo-ai)
- 360-autocam / multicam-edit —— 说话人-机位校验与多机位 FFT 音频对齐思路(经 engine/speaker_map.py、templates/multicam_speaker.py 借鉴)
- pranay241/AVZoom、showlab/whisperVideo —— 说话人判定/平滑取景/身份一致性思路
- oximedia-shots、segmo、moorwp-jpg/Auto-Framing-For-OBS —— 景别/构图(headroom/人脸占比/取景)规则参考


## 3. 运行时依赖 (通过 pip 安装, 不随仓库分发)
| 依赖 | 许可证 |
|---|---|
| mcp | MIT |
| flask | BSD-3-Clause |
| imageio-ffmpeg | BSD-2-Clause |
| numpy | BSD-3-Clause |
| opencv-python | Apache-2.0 |
| mediapipe | Apache-2.0 |
| scipy | BSD-3-Clause |
| librosa | ISC |
| faster-whisper / openai-whisper | MIT (可选) |

## 4. 特别说明: FFmpeg
本项目通过 imageio-ffmpeg 随包调用 FFmpeg 二进制。FFmpeg 本身为
LGPL/GPL 许可(取决于编译选项, 例如是否启用 libass/GPL 组件)。
在分发/商业化本项目前, 请核对所用 imageio-ffmpeg 附带的 FFmpeg
构建配置, 并在发行说明中注明其许可条款。本仓库自身代码不包含
FFmpeg 源码。
