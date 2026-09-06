# AutoDirector 迭代路线(研究结论 + 工具改进计划)

> 背景: v1/v2(单机位长镜)与 v3(全特写)反馈: 镜别太少 / 取景太近 / 人物(镜头主体)选择不准。
> 用户要求: 停止反复渲染, 从 GitHub 找现成方法, 先解决工具层的根本问题。

## 0. 一个必须先讲清的事实(实测)
- 对现有 24 段素材做 A<->C 全片音频互相关, 置信度只有 0.01~0.035
  (同场双机位通常 >0.5)。=> a/* 与 c/* 是**不同 take(重拍)**, 不是同一条表演的双机位。
- 因此"跨 A/C 做同表演的机位切换/正反打"物理上不成立(口型/表演对不上)。
- 现阶段正确路线: **单机位电影化重构图**——从 4K 全景按说话人/听者重取景,
  产出 全景/中景/近景/反应 等不同景别; 真正多机位切换需重拍时 A/C 同条同步开机。

## 0.5 最终技术决策(挖到底)
- 结论: "人物选择不准"的底层根因 = 宽景 4K 里人脸小, 手写唇动-RMS(SyncNet式) 逐句判定噪声大
  (v5 实测: 说话人标错/同一人被拆段)。改进"主体锁定/v6"只能缓解, 不能根治。
- 根治方案(两层):
  A. 离线层(已完成): triage 素材筛选 -> 句子级 shot 分组 -> 置信门槛 + 主体锁定(v6) -> shot-role 分镜表。
  B. 模型层(需联网下载 + 许可评估, 未渲染前先做):
     - 说话人判定改用 **TalkNet-ASD**(TaoRuijie/TalkNet-ASD, AVA/TalkSet 预训练; 研究用途, 仓库无明确 OSS 许可,
       仅作运行时外部依赖下载, 不把权重并入本仓库/Apache 分发; 参考 AVZoom/whisperVideo 的接入方式)。
     - 可选声纹说话人切分 **pyannote/speaker-diarization-3.1**(需 HF token, gated)或 WhisperX 词级时间戳。
     - 注意: 素材本身 A/C 非同条双机位(实测互相关 0.01~0.035), 多机位正反打需重拍同条或仅用于不同 take 的素材选择。

## 1. GitHub 方法地图(已调研)
| 问题 | 参考实现 | 可借鉴点 |
|---|---|---|
| 说话人判定 | pranay241/AVZoom; sieve-community/fast-asd; showlab/whisperVideo | 人脸检测+质心跟踪+FaceMesh 唇动(MAR)+光流+音频 RMS/VAD 融合; TalkNet-ASD 深度模型(可选); **迟滞/指数平滑**防抖 |
| 取景/构图 | moorwp-jpg/Auto-Framing-For-OBS; segmo; Unity Cinemachine | 头顶留白(headroom ~10-15%)、人脸占比目标、安全边距、**组取景(group union)**、最大放大限制、死区+平滑、subject lock |
| 景别(shot scale) | oximedia-shots(ShotType ECU..ELS + CinematicRole); rsomani95/shot-type-classifier | 用人脸/皮肤面积占比等特征把镜头分成 全景/中景/近景/特写; 分类"反应镜头/插入镜头" |
| 同一人识别 | VideoFace2.0(重识别); AVZoom centroid tracker; ByteTrack | 给每个人稳定 ID, 镜头主体连续, 不因漏检乱跳 |
| 剪辑节奏/语法 | theSamPadilla/montaj(camera-vocabulary/select-takes); DirectorSKILL 电影语言 | 建立"shot role 序列"规则: 建立镜头->中景对话->关键句近景->听者反应 |

## 2. 根因与修法(工具层)
### 2.1 取景太近 -> 引入"景别目标 + headroom + 上限保护"
- 当前 `_render_closeup` 固定人脸宽≈输出宽 35%(近特写), 且无景别概念。
- 改为按"shot role"给目标人脸占比带:
    wide    face_w ~ 3-7%  (整组/环境)
    medium  face_w ~ 10-16% (双人中景/对话)
    mcu     face_w ~ 18-24% (中近景, 默认对话首选)
    closeup face_w ~ 26-33% (仅强调句/情绪点使用)
- 构图参数(参考 segmo/Auto-Framing): 头顶留白 10-15%; 目标占比优先, 源分辨率不够则**回退更松一档**, 不硬裁到过近; 人物中心可用三分法/居中, 下方留出肩部空间。
- 4K->1080p/720p 有充足裁切余量, 但必须设"最近极限": 裁窗不小于源宽 25%(约等于 4x 放大), 否则画质/裁切错误。

### 2.2 选错人 -> 稳定的人物轨迹 + 说话人迟滞 + 主体锁
- 每段先把所有人脸连成轨迹(质心+尺寸+FaceMesh 弱特征做同人 re-id), 不打乱 ID。
- 说话人判据融合: 唇开度(MAR)标准差 + 唇动-音频相关 r + (可选)口部光流;
  用**迟滞阈值**切换: 只有当另一张脸持续 >0.4s 且分数超过当前主体 1.3x 才换人, 避免两人间抖动。
- 同一"逻辑镜头"内锁定主体(subject lock); 主体短暂漏检时保持上一个裁窗并平滑, 不跳到别人。

### 2.3 没有镜头语言 -> shot role 决策层(导演语法)
- 以"剧本 beat + 说话人 + 在场人数"驱动:
    beat 开场/转场   -> WIDE(交代人物关系/环境)
    单人多句独白     -> MEDIUM/MCU 为主, 每 2-3 句插一次听者 REACTION(若检测到第 2 张脸)
    问答/冲突句      -> MCU 提问者 -> 切听者反应(被问方) -> MCU 回答者
    金句/笑点/揭穿/结尾 -> CLOSEUP(仅这些点)
- 输出: 每句(或每 1-3 句)一个"shot 指令": {role, subject_id(说话人/听者/组), 时间窗},
  渲染前先出**分镜表**给人审, 而不是直接渲染。

## 3. 落地顺序(不渲染, 先做分析与工具)
1. [x] triage: 去掉 NG/花絮, 每个 beat 找出可用 take(engine/triage.py)
2. [x] v3: 句子级 turn + SyncNet 说话人检测(engine/speaker_map.py)
3. [x] engine/cinema.py: 景别标定 / 构图矩形 / 同人描述子 / 反应镜头判定(纯函数, 可单测)
4. [x] speaker_map: analyze_unit_faces 加置信门槛(r/std/score); v5/v6 句子级说话人分镜+主体锁定(分析产物: autocut_runs/v5_director_plan.json, v6_director_plan.json)
5. [x] director: shot-role 分镜表生成(v4 14turn / v5 句级 / v6 主体锁定) —— 均纯分析未渲染
6. [ ] 渲染层: 按审过的分镜表用 cinema 构图(wide/medium/mcu/closeup) —— 彻底迭代完成前不渲染
7. [~] 模型层: TalkNet-ASD 已下载并接入 engine/asd_talknet.py, 已在真实 4K 素材验证能区分说话/非说话脸; (可选)pyannote 声纹切分待做
8. [ ] (可选, 需重拍) 真双机位同条: 用 polysync 音频互相关对齐 A/C, 再按说话人切机位

## 4. 未采纳/慎用
- TalkNet-ASD / pyannote / VideoMAE shot-scale: 需联网下载模型与许可, 暂作为可选增强;
  基础版先用 MediaPipe(已在仓库)+ 经典信号处理(与 AVZoom classical 基线一致)。
