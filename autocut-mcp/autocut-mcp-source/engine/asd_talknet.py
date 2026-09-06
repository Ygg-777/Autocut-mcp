# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Autocut MCP Plugin contributors
"""TalkNet-ASD 说话人判定(可选依赖, 运行时外部模型) —— 根治"宽景小脸说话人判定不准"。

来源/许可:
  - 代码: TaoRuijie/TalkNet-ASD (MIT, 见 third_party/TalkNet-ASD/LICENSE.md)
  - 模型: pretrain_TalkSet.model (作者预训练, 运行时从镜像下载, 不并入仓库分发)
接入方式(参考官方 demoTalkNet.evaluate_network):
  输入: 单人脸 crop 视频(25fps, 224x224 彩色, 实际内部裁 112x112 灰度) + 同时间窗 16k wav
  输出: 逐帧原始 logit(>0 判为说话), 多 duration 平均后返回
本模块仅在调用时导入; 缺仓库/模型/依赖时抛带指引的 RuntimeError, 不影响其它工具。
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))           # engine/
_REPO = os.path.normpath(os.path.join(_ROOT, "..", "third_party", "TalkNet-ASD"))
_MODEL = os.path.join(_REPO, "pretrain_TalkSet.model")
_NET = None


def available() -> bool:
    """仓库 + 模型是否就绪。"""
    return os.path.isfile(os.path.join(_REPO, "talkNet.py")) and os.path.isfile(_MODEL)


def model_info() -> dict:
    return {"repo": _REPO, "model": _MODEL,
            "model_exists": os.path.isfile(_MODEL),
            "repo_exists": os.path.isfile(os.path.join(_REPO, "talkNet.py"))}


def _get_net():
    global _NET
    if _NET is not None:
        return _NET
    if not available():
        raise RuntimeError(
            "TalkNet-ASD 未就绪: 需要 third_party/TalkNet-ASD 与 pretrain_TalkSet.model。\n"
            "  安装: git clone https://github.com/TaoRuijie/TalkNet-ASD third_party/TalkNet-ASD\n"
            "  模型: 从 https://hf-mirror.com/AlekseyKorshuk/talknet-asd 下载 pretrain_TalkSet.model")
    try:
        import torch  # noqa: F401
        import python_speech_features  # noqa: F401
        import cv2  # noqa: F401
        import pandas  # noqa: F401
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("TalkNet 依赖缺失(%s): pip install torch torchaudio python_speech_features scenedetect gdown pandas opencv-python scikit-learn tqdm" % e)
    sys.path.insert(0, _REPO)
    try:
        from talkNet import talkNet
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("无法导入 talkNet(需 CUDA): %s" % e)
    net = talkNet()
    net.loadParameters(_MODEL)
    net.eval()
    _NET = net
    return net


def score_crop(avi_path: str, wav_path: str):
    """对单人脸 crop(25fps, 224x224) + 同步 wav(16k) 返回逐帧 logit(>0 说话)。

    移植自 demoTalkNet.evaluate_network(TalkNet-ASD, MIT): 用多 duration 采样并平均,
    提高可靠性。返回 (scores: numpy 1D 长度≈视频帧数, fps=25)。
    """
    import math
    import cv2
    import numpy as np
    import python_speech_features as psf
    import torch
    from scipy.io import wavfile

    net = _get_net()
    _, audio = wavfile.read(wav_path)
    audio = np.asarray(audio, dtype=np.float64)
    af = psf.mfcc(audio, 16000, numcep=13, winlen=0.025, winstep=0.010)
    cap = cv2.VideoCapture(avi_path)
    vf = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (224, 224))
        vf.append(g[56:168, 56:168])
    cap.release()
    if not vf:
        raise RuntimeError("空 crop 视频: %s" % avi_path)
    vf = np.array(vf)
    length = min((af.shape[0] - af.shape[0] % 4) / 100.0, vf.shape[0] / 25.0)
    if length < 0.8:
        raise RuntimeError("crop 太短(%.2fs), TalkNet 需要 >=1s" % length)
    n_frames = int(round(length * 25))
    af = af[:int(round(length * 100)), :]
    vf = vf[:n_frames, :, :]
    duration_set = (1, 1, 1, 2, 2, 2, 3, 3, 4, 5, 6)
    per_dur = []
    with torch.no_grad():
        for d in duration_set:
            batch = int(math.ceil(length / d))
            scores = []
            for i in range(batch):
                a0, a1 = i * d * 100, min((i + 1) * d * 100, af.shape[0])
                v0, v1 = i * d * 25, min((i + 1) * d * 25, n_frames)
                if a1 - a0 < 50 or v1 - v0 < 12:
                    continue
                inputA = torch.FloatTensor(af[a0:a1, :]).unsqueeze(0).cuda()
                inputV = torch.FloatTensor(vf[v0:v1, :, :]).unsqueeze(0).cuda()
                eA = net.model.forward_audio_frontend(inputA)
                eV = net.model.forward_visual_frontend(inputV)
                eA, eV = net.model.forward_cross_attention(eA, eV)
                out = net.model.forward_audio_visual_backend(eA, eV)
                sc = net.lossAV.forward(out, labels=None)
                scores.append(sc)
            if scores:
                per_dur.append(np.concatenate(scores)[:n_frames])
    if not per_dur:
        raise RuntimeError("TalkNet 无有效输出")
    # 对齐到帧: 每个 duration 的分数已按帧序拼接, 取各 duration 的均值
    n = min(n_frames, min(len(x) for x in per_dur))
    scores = np.mean([x[:n] for x in per_dur], axis=0)
    return scores
