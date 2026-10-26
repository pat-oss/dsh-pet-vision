# 小鲸鱼 · 桌面 AI 伴侣 🐋

> 小鲸鱼视觉与语音版 —— 会看屏幕、会主动开口的桌面桌宠

[![platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6.svg)](#环境要求)
[![python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](#环境要求)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![release](https://img.shields.io/github/v/release/pat-oss/dsh-pet-vision.svg)](https://github.com/pat-oss/dsh-pet-vision/releases/latest)

一只住在你电脑里的桌宠：蓝头发、鲸鱼尾巴的女仆鱼。

![小鲸鱼的三种状态：待机 / 说话 / 思考](docs/preview.png)

她会**自己看你的屏幕**、挑时机主动跟你搭话；喊她名字就能聊天，回答用**本机合成的声音**说出来。
只有"看屏幕判断该不该说话"这一步会调用 DeepSeek 的视觉 API，语音合成完全在你自己的电脑上跑。

| | |
|---|---|
| 仓库 | <https://github.com/pat-oss/dsh-pet-vision> |
| 平台 | Windows 10 / 11（用到窗口枚举、全屏检测与 SAPI 保底，暂不支持 macOS / Linux） |
| 语言 | Python 3.11+（实测 3.13） |
| 视觉 | DeepSeek `deepseek-v4-flash-vision-exp`（OpenAI 兼容接口，需自备 API Key） |
| 语音 | MOSS-TTS-Nano（ONNX，本地 CPU 推理，零样本音色克隆） |
| 唤醒 | Vosk 离线中文小模型，喊「小鲸鱼」即可（可改） |

> ⚠️ **角色动画素材来自 dsh-pet，允许开源使用但禁止商用**，二次创作在介绍/展示/分发处必须附原作者地址。
> 详见文末 [声明与致谢](#声明与致谢)。

---

## 目录

- [它能做什么](#它能做什么)
- [它是怎么工作的](#它是怎么工作的)
- [环境要求](#环境要求)
- [下载即用（推荐）](#下载即用推荐)
- [快速开始](#快速开始)
- [模型下载](#模型下载)
- [首次设置](#首次设置)
- [日常使用](#日常使用)
- [主动互动频率](#主动互动频率)
- [关于音色](#关于音色)
- [打包成 exe（可选）](#打包成-exe可选)
- [配置速查](#配置速查)
- [常见问题](#常见问题)
- [目录结构](#目录结构)
- [隐私说明](#隐私说明)
- [声明与致谢](#声明与致谢)

---

## 它能做什么

- **主动搭话**：按你设定的频率（5–30 次/小时可调）挑时机说一句话。视觉开着时她会带上当前屏幕的截图，
  所以会评论你在写什么代码、在追什么番；视觉关了就纯文字陪你闲聊。
- **画面有大变动时也会开口**：比如突然弹了个报错窗、切到一个有意思的页面。
- **喊名字对话**：说「小鲸鱼」，她先应一声，你接着说问题就行；3 秒内没下文就结束本轮，不会一直挂着麦克风。
- **打字聊天**：右键托盘 →「跟它说话」，弹输入框。
- **声音可换成任何人**：给 3–10 秒干净人声，零样本克隆（本地推理）。
- **有分寸**：专注模式（免打扰）、窗口标题黑名单（网银/密码类标题自动闭嘴）、每日调用预算、全屏可静音。
- **单进程、无服务**：不起 HTTP/WebSocket，一个 `python main.py` 就是一个完整的桌宠。

## 它是怎么工作的

```
   每 N 秒截屏 ──► dHash 比较画面变化 ──►【值得说吗？】──► DeepSeek 视觉 API
                        │                                    │
                 到达设定频率 ──────────►（带上截图）──────────┤
                                                             ▼
                                          句子切分 ──► MOSS-TTS-Nano 逐句合成 ──► 播放线程
                                                             │
                                                    气泡同步显示在头顶
```

关键取舍（也是踩过的坑）：

- **按句合成、整句交出**：本机合成慢于实时（RTF 0.7–0.9x），所以不能等整段说完再播。
- **唤醒应答走预热缓存**：三条应答语在启动时预合成，命中缓存后出声几乎 0 延迟。
- **全屏下的画面变动一律忽略**：视频画面一直在动，那不是"值得开口"的信号，还会每 5 秒烧一次 API；
  但"到点搭话"照常（可以在设置里关掉）。

## 环境要求

- Windows 10 / 11（x64）
- Python 3.11 及以上
- 一个 DeepSeek 开放平台的 API Key（`sk-` 开头，[申请地址](https://platform.deepseek.com/)）
- 磁盘：源码约 40 MB + 模型约 800 MB + 运行时数据几 MB（完整包解压后 1119 MB）
- 内存：模型常驻，实际占用约 2 GB
- 麦克风（用唤醒词功能时；不插麦克风也能打字对话）

## 下载即用（推荐）

不想折腾 Python 环境的话，直接下载打好包的完整版 —— **模型权重都在里面**，解压双击就能用：

**⬇ [little-whale-pet-win64-v1.0.0.zip](https://github.com/pat-oss/dsh-pet-vision/releases/download/v1.0.0/little-whale-pet-win64-v1.0.0.zip)**

压缩包 **554 MB**，解压后 **1119 MB**；内含 MOSS-TTS-Nano 642 MB + 音频编解码 87 MB + Vosk 唤醒模型 66 MB。
去 [Releases 页面](https://github.com/pat-oss/dsh-pet-vision/releases/latest) 可以看更新记录和 SHA256。

1. 下载并解压到任意目录 —— **要把整个 `小鲸鱼` 文件夹一起解出来**，不要只把 exe 拖走：
   `_internal`、`models`、`pet` 必须和 `小鲸鱼.exe` 在同一层目录，否则起不来。
2. 双击 `小鲸鱼.exe`。
3. 首次启动会弹设置窗，填上 DeepSeek API Key（`sk-` 开头）→ 点保存。
4. 系统询问麦克风 / 网络权限时选「允许」（麦克风用于喊名字，不给也能打字聊天）。

> 打包版没有做代码签名，火绒 / 腾讯电脑管家 / Defender 可能拦一下，加白名单即可。
> 不放心的话所有源码都在本仓库，按下面的步骤自己跑、自己打包完全等价。

## 快速开始

```bat
:: 1. 取代码（或页面右上角 Code → Download ZIP 解压）
git clone https://github.com/pat-oss/dsh-pet-vision.git
cd dsh-pet-vision

:: 2. 建虚拟环境并装依赖
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

:: 3. 下模型（约 800 MB，只需一次）
pip install huggingface_hub
python scripts/download_models.py

:: 4. 启动
python main.py
```

第一次启动会弹设置窗，填上 DeepSeek API Key 就能用了。之后每次启动都静默读配置，不再询问。

> 只想跑打包好的 exe？
> **想省事** → 直接用上面的 [下载即用](#下载即用推荐) 完整包，模型已经打进去了。
> **想自己打包** → 先按上面步骤下模型，再 `pip install pyinstaller`，
> 执行 `python scripts/build_exe.py`，产物在 `dist\小鲸鱼\小鲸鱼.exe`（约 1.1 GB）。

## 模型下载

仓库不包含模型权重（体积太大），首次使用必须自己下。用脚本最省事：

```bat
python scripts/download_models.py            :: 一次下三个
python scripts/download_models.py --only vosk :: 只下唤醒词模型
```

脚本失败时可以手动下，放到对应目录即可：

| 模型 | 体积 | 放到 | 来源 |
|---|---|---|---|
| MOSS-TTS-Nano-100M-ONNX | 641 MB | `models/moss-tts-nano-onnx/` | [HuggingFace](https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX) |
| MOSS-Audio-Tokenizer-Nano-ONNX | 86 MB | `models/moss-audio-tokenizer-nano-onnx/` | [HuggingFace](https://huggingface.co/OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX) |
| vosk-model-small-cn-0.22 | 65 MB | `models/vosk-small-cn/`（解压后的 `am/ conf/ graph/ …` 直接放进去） | [Vosk](https://alphacephei.com/vosk/models) |

放好后目录应该长这样：

```
models/
├── moss-tts-nano-onnx/            # 里面有 moss_tts_prefill.onnx 等
├── moss-audio-tokenizer-nano-onnx/
└── vosk-small-cn/                 # 里面有 am/ conf/ graph/ ivector/
```

> 没有 TTS 模型也能启动：会自动降级到 Windows 自带的 SAPI5 语音（机器人音），
> 但**唤醒词模型缺失就等于喊不醒**，所以 `vosk-small-cn` 建议一定下。

## 首次设置

启动后（或托盘右键 →「重新设置…」）会看到这些项，保存后立即生效，无需重启：

| 设置项 | 说明 |
|---|---|
| **API 密钥** | DeepSeek 的 `sk-` 密钥。也可以改用环境变量 `DEEPSEEK_API_KEY`（优先级更高） |
| **唤醒词（她的称呼）** | 默认「小鲸鱼」，就是你要喊的那个词。2 字词容错差，3 字以上更稳 |
| **她这样称呼你** | 默认「主人」，会影响人设语气和应答语 |
| **音色** | 已注册的音色列表；一个都没有时用 SAPI 保底 |
| **主动互动** | 不主动互动 / 低频 5–10 / 中频 10–20 / 高频 20–30 / 自定义（自己填最低、最高） |
| **全屏时** | 勾上 = 全屏看视频/玩游戏时一句不说；默认不勾（全屏也按频率陪聊） |
| **最大输出 token** | 留空 = 不限制（推荐）。填了数字才受限；**填太小会出现"想满了却没正文"** |
| **麦克风** | 「测试麦克风（2 秒）」会报峰值能量，帮确认唤醒词能不能听到你 |

## 日常使用

**托盘图标右键**：

| 菜单项 | 作用 |
|---|---|
| 重新设置… | 打开上面那张设置窗 |
| 视觉功能（让她看屏幕） | 取消勾选 = 完全不看屏幕，也不发截图，省 API；专注模式下会被强制关闭 |
| 专注模式（免打扰） | 相当于"别烦我"，只回应你主动跟她说话 |
| 跟它说话 | 弹输入框打字聊 |
| 退出 | 退出程序 |

**喊她**：

1. 说「小鲸鱼」（或你改的唤醒词）→ 她应一声「主人，怎么啦？」
2. **3 秒内**接着把问题说完，她会回答；没下文就当本轮结束，不会一直占着麦克风
3. 说话过程中她会显示「思考中…」，API 一般 1–3 秒

**其他**：鼠标左键拖动她可以换位置；单击有反应动画。

**她在哪个目录存东西？**

- 源码运行：项目里的 `data/`
- 打包后的 exe：`%LOCALAPPDATA%\little-whale-pet\`（`run.log` 就在里面，出问题先看它）

## 主动互动频率

| 档位 | 每小时 | 大致间隔 |
|---|---|---|
| 不主动互动 | 0 | 不开口，也不截屏（省电省 API） |
| 低频 | 5–10 | 6–12 分钟 |
| 中频 | 10–20 | 3–6 分钟 |
| 高频 | 20–30 | 2–3 分钟 |
| 自定义 | 你填 | 按你填的区间随机 |

- 间隔是**在区间内随机取**的，说完一句重新抽，所以不会像节拍器一样规律。
- **全屏看视频 / 玩游戏**时：默认照常按频率搭话（会带上截图，所以她能评论视频内容）；
  勾上「全屏时也不要说话」才会完全安静。无论勾不勾，**全屏下的"画面变动"都不会触发开口**
  ——视频画面一直在动，那是噪声不是信号。
- 每小时有硬上限（= 你选的最高值），加上每日调用预算，不会失控刷 API。

## 关于音色

**本仓库故意不带任何参考音频**——音色样本属于第三方声音，权属不清，不便随代码分发。
所以克隆音色前，她会先用 Windows SAPI 的机器人音说话。

换成你自己的声音（或任何你**已获授权**的声音）：

```bat
:: 3–10 秒干净人声，wav / flac / ogg 都行，越干净越像
python scripts/register_voice.py --audio my_voice.wav --name 我的音色
:: 看看已注册了哪些
python scripts/register_voice.py --list
```

然后在托盘 →「重新设置…」→ 音色里选它。旧的语音缓存会按音色名自动隔离，换回去也不会串音。

> 合规提醒（MOSS-TTS-Nano 模型卡的约定）：**只克隆自己或已获明确授权的声音**；
> 合成音频在对外场合应标注为合成产物。

## 打包成 exe（可选）

```bat
pip install pyinstaller
python scripts/build_exe.py --no-shortcut      :: 只打包，不建桌面快捷方式
:: 附带的其它参数：
::   --fast        跳过模型复制（模型已在 dist 里时用，省几分钟）
::   --clean       先清 build/dist
```

产物是 `dist\小鲸鱼\`（onedir，约 1.1 GB，含模型）。**打包前请先退出正在运行的实例**，
否则 `dist\小鲸鱼` 被占用会报权限错误。

想改角色尺寸/帧素材，从官方 alpha 视频重新生成：

```bat
python scripts/prepare_assets.py --src _assets_src/webm --out pet/assets/frames --size 320x180
```

## 配置速查

`config.yaml` 是全部默认值，改行为优先改这里（不用碰代码）。想只改几个值又不污染默认配置时，
可以写进 `data/user_prefs.json`，启动时会深合并覆盖。

| 键 | 默认 | 含义 |
|---|---|---|
| `wake.phrase` | 小鲸鱼 | 唤醒词，也可以直接输入文字时的称呼 |
| `wake.silence_ms` | 3000 | 喊名字后多久没下文就结束本轮 |
| `wake.energy_threshold` | 0.006 | 麦克风能量门，环境吵可调高 |
| `chat.idle_finish_sec` | 8 | 会话空闲多久自动收尾（不收尾会挂住唤醒） |
| `observe.diff_threshold` | 12 | dHash 汉明距离阈值，越大越不敏感 |
| `observe.interval_idle_sec` | 10 | 静止时的截屏间隔（秒） |
| `proactive.mode` | low | off / low / mid / high / custom |
| `gate.dnd_fullscreen` | false | 全屏时是否完全安静 |
| `gate.daily_call_budget` | 300 | 每日 API 调用上限（观察+会话） |
| `gate.title_blacklist` | 网银/银行/密码… | 前台标题命中就闭嘴 |
| `model.detail` | low | 截图降采样等级（每图 ≤384 token） |
| `model.max_tokens_chat` | null | 输出上限，null = 不限制 |
| `voice.threads` | 6 | ONNX 线程数（实测 6 最快，别乱调） |

## 常见问题

**Q：喊她没反应？**
① 确认唤醒词跟设置里一致，且**说的是那个词**（2 字词识别容错小，可以改成 3 字以上）；
② 托盘里「视觉功能」关掉不影响唤醒，但「专注模式」开着会让她只听不说；
③ 跑一下 `python scripts/verify_wake.py` 看麦克风与识别链路；
④ 没下 `vosk-small-cn` 模型，唤醒功能等于没有。

**Q：有气泡、没声音？**
① 模型没下全 → 看 `run.log` 里有没有 `TTS 引擎加载失败`；
② 没注册音色时是 SAPI 机器人音，如果连机器人音都没有，检查系统音量/输出设备；
③ 正在放音乐/视频时，她的声音会被盖住（合成音量固定）。

**Q：她不主动说话？**
① 主动互动是不是选了「不主动互动」；② 「专注模式」开着；
③ 全屏中且勾了「全屏时也不要说话」；④ 当天 API 预算用完了（余量≤60 次时观察循环主动挂起，
留给你的对话）；⑤ 模型认为"没什么值得说的"——她有权沉默。

**Q：一直显示「思考中…」然后没了？**
通常是 API Key 失效、欠费或被限流。看 `run.log` / `data/run.log`，里面有 `[permanent]`、`401`
之类的错误分类。托盘 →「重新设置…」换成有效 Key 即可。

**Q：杀毒软件报毒 / 拦截？**
PyInstaller 打的未签名 exe 常被误判。把整个 `小鲸鱼\` 目录加进白名单；打包版还需要允许它访问网络
（DeepSeek API）和麦克风。

**Q：下好的完整包解压后双击没反应？**
① 必须**整个 `小鲸鱼` 文件夹一起解压**——`_internal`、`models`、`pet` 要和 `小鲸鱼.exe` 同层，
单独把 exe 拖出来一定起不来；
② 解压路径尽量短、别带特殊符号，`D:\小鲸鱼\` 这种最稳；
③ 看同目录有没有生成 `run.log`：**没有日志说明进程根本没起来**，基本是被杀软拦了；
有日志就按里面的报错走。

**Q：内存占 2 GB 正常吗？**
正常。三个 ONNX 模型常驻内存，其中主模型 641 MB。

**Q：能不能放到 Mac / Linux 上用？**
不行。窗口标题读取、全屏检测、SAPI 保底都依赖 `pywin32`；窗口透明置顶也按 Windows 调过。
真要移植，`observe/screen.py`、`voice/` 下的保底路径都得换实现。

## 目录结构

```
.
├── main.py                  # 装配入口：截屏 → DeepSeek → TTS → 窗口 → 唤醒，全在这里接线
├── config.yaml              # 声明式配置（改行为先改这里）
├── requirements.txt
├── NOTICE                   # 第三方素材与组件声明
├── chat/                    # 会话执行器（会话线程、通道、空闲收尾）
├── core/                    # 配置/状态/节流门/prompt/路径/密钥
├── observe/                 # 截屏、dHash、观察循环、DeepSeek 客户端
├── voice/                   # MOSS-TTS-Nano 引擎、播放器、缓存、唤醒监听
├── pet/                     # 窗口、动画、托盘、设置弹窗、assets/frames（角色帧）
├── vendor/moss_tts_nano/    # vendored 的 MOSS 运行时（Apache-2.0，含本地化改动）
├── scripts/                 # 下模型 / 注册音色 / 生成帧素材 / 打包 / 唤醒自检
├── tests/                   # pytest 用例（65 项）
├── docs/                    # 架构设计文档
└── _assets_src/webm/        # 角色动画的官方 VP9-alpha 源视频（溯源与再生成用）
```

## 隐私说明

- **会离开你电脑的只有"截图"**：判断要不要说话时，当前屏幕画面（默认降到 512×512）会发给
  DeepSeek API。不想这样就把托盘里的「视觉功能」关掉，她就完全看不见屏幕。
- **语音不上云**：TTS 全程本地推理。
- **唤醒词识别在本地**：Vosk 离线模型，音频不出本机。
- **API Key 存在本机**：`%LOCALAPPDATA%\little-whale-pet\secrets.json`，**明文 JSON**
  （桌面程序的威胁模型是"别每次重启都输密码"，不是对抗同机恶意程序）。
  建议用**有额度上限**的 Key；也可以只用环境变量 `DEEPSEEK_API_KEY` 不落盘。
- **窗口标题黑名单**：`config.yaml` 里 `gate.title_blacklist` 命中的前台窗口（网银、密码管理器等）
  会直接跳过，不截图、不发请求。

## 声明与致谢

这个项目站在别人肩膀上，逐项说明如下（完整版见 [`NOTICE`](NOTICE)）：

### 1. 角色动画素材 — dsh-pet（有使用限制，请务必阅读）

- 来源：<https://github.com/PC2005-cloud/dsh-pet>
- 本仓库 `pet/assets/frames/` 下的 PNG 帧序列，以及 `_assets_src/webm/` 里的源视频，
  均来自 dsh-pet 的 VP9-alpha 动画，经 `scripts/prepare_assets.py` 预解码/缩放生成。
- 许可（依 dsh-pet 仓库「许可」一节）：
  - **代码：MIT**
  - **素材（动画 / 提示词 / 源视频）：允许开源使用，禁止商用**
  - **二次创作作品在介绍、展示、分发处必须附原作者 GitHub 地址**
    （<https://github.com/PC2005-cloud/dsh-pet>）
- 因此：**本项目（含帧素材）不得用于商业用途**；任何形式的再分发请保留本段与 `NOTICE`。
  角色形象版权归 dsh-pet 原作者所有，本项目只做了格式转换。

### 2. 语音合成 — MOSS-TTS-Nano（Apache-2.0）

- 模型：[MOSS-TTS-Nano-100M-ONNX](https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX)、
  [MOSS-Audio-Tokenizer-Nano-ONNX](https://huggingface.co/OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX)
- 原始项目：<https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Nano>
- `vendor/moss_tts_nano/` 是上述 ONNX 仓库浏览器参考实现（Apache-2.0）的本地化副本，
  为在 Windows + Python 下跑通做了三处改动：`tokenizer.model` 改走 `model_proto` 加载
  （绕开中文路径 NOT_FOUND）、`torchaudio` 换成 `soundfile + numpy` 重采样、
  `WeTextProcessing`（pynini）换成最小文本规整实现。许可证随目录保留。
- 合规：**只克隆自己或已获明确授权的声音**；合成音频在对外场合应标注为合成产物。

### 3. 语音识别 — Vosk（Apache-2.0）

- <https://github.com/alphacep/vosk-api>，模型 `vosk-model-small-cn-0.22`（Apache-2.0）。

### 4. 视觉与对话 — DeepSeek API

- 通过 [DeepSeek 开放平台](https://platform.deepseek.com/) 的 OpenAI 兼容接口调用
  `deepseek-v4-flash-vision-exp`。这是**收费的在线服务**，需要你自己的 Key，
  调用量与费用由你自己承担。
- 本项目与其无隶属关系，不是官方项目。

### 5. 参考实现（只做工程对照，未拷贝代码）

- dsh-pet-indesktop（同角色的 PySide6 独立桌宠）：<https://github.com/MerZlin/dsh-pet-indesktop>

### 6. 其它依赖（各归其原作者）

`PySide6`（LGPLv3，动态链接、未修改）、`onnxruntime`、`numpy`、`soundfile`、`sentencepiece`、
`scipy`、`mss`、`openai`（Python 客户端）、`sounddevice`、`PyYAML`、`pywin32`、`pypinyin`、
`PyInstaller`（仅打包时需要）。完整列表见 `requirements.txt`。

### 7. 本项目的许可

除上述第三方内容外，本人编写的代码以 **MIT** 发布（见 [`LICENSE`](LICENSE)）。
**MIT 仅覆盖代码，不覆盖**：

- `pet/assets/`（角色素材，见第 1 条，禁止商用）
- `vendor/`（MOSS 运行时，Apache-2.0）
- `_assets_src/webm/`（原始动画，见第 1 条）

换句话说：拿这份代码随便改随便用，但**别拿角色素材去卖钱，也别删掉这里的署名**。

---

项目地址：<https://github.com/pat-oss/dsh-pet-vision>

如果这个项目让你桌上多了点乐子，欢迎 Star；发现 bug 或想加功能，开 Issue 就好。
