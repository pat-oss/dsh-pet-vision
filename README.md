# 小鲸鱼桌宠（little-whale-pet）

一个跑在 Windows 本地的桌面 AI 伙伴：**自己看屏幕、自己判断值不值得开口**，
用 DeepSeek 视觉 API 思考，用本地 MOSS-TTS-Nano 以可爱音色主动发声，
还能**像手机助手一样一直听着**——喊一声她的名字就醒来陪你聊天。
形象来自开源桌宠素材 [dsh-pet](https://github.com/PC2005-cloud/dsh-pet)。

单个 Python 进程串起 **截屏 → DeepSeek → TTS → 宠物窗口 → 唤醒监听**，
不起额外服务、不开端口。

```
┌────────────┐   dHash 变化门   ┌──────────────┐   放行   ┌─────────────┐
│ 截屏(mss)  │ ───────────────► │ DeepSeek 视觉 │ ───────► │ 节流门      │
└────────────┘   画面没变就跳过  │ 判断要不要说  │ JSON决策 │ 30s/10次/日  │
                                └──────────────┘         └──────┬──────┘
                                                               ▼
                              ┌─────────────┐    PCM 流   ┌──────────────┐
                              │ 宠物窗口动画 │ ◄───────── │ MOSS-TTS-Nano│
                              └─────────────┘            │ (本地 ONNX)  │
                                                         └──────────────┘
   ┌────────────┐  能量门+Vosk   ┌───────────────┐  指令   ┌──────────────┐
   │ 麦克风 16k │ ─────────────► │ 唤醒词监听      │ ──────► │ DeepSeek 会话 │
   └────────────┘  静音不喂识别   │ 「小鲸鱼！」    │         └──────────────┘
                                 └───────────────┘
```

## 快速开始

### 方式 A：下载即用（推荐）

用打包好的 exe，不用装 Python：

1. 拿到 `dist/小鲸鱼/` 整个文件夹（约 950 MB），放到任意位置（如 `D:\小鲸鱼`）
2. 双击 `小鲸鱼.exe`，或桌面上的"小鲸鱼"快捷方式
3. **第一次启动会弹设置窗**：填 DeepSeek API 密钥、给她起个称呼（唤醒词）、
   选个音色。之后启动静默读取，不再询问

### 方式 B：源码运行

```bash
cd little-whale-pet
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt          # 慢就用 -i https://pypi.tuna.tsinghua.edu.cn/simple
python main.py                           # 首次启动同样会弹设置窗
python main.py --self-test               # 离线自检：不起 GUI，不花 API 额度
python main.py --setup                   # 强制打开设置窗（改密钥/唤醒词/音色）
```

> 也支持老办法：设 `DEEPSEEK_API_KEY` 环境变量，优先级高于设置窗里存的密钥。

### 交互方式

| 操作 | 行为 |
|------|------|
| **喊唤醒词** | 她应一声"我在"，然后听你继续说，说完就思考回答（被动回应，不受频率限制）；思考开始时她会显示"思考中…"气泡 |
| 左键点击 | 播放"点击回应"动画，并打开输入框；**输入框上方同时显示用量面板**（本次启动 / 今天 / 本月 / 总计 的调用次数、token、缓存命中率） |
| 左键双击 / 托盘"跟它说话" | 打开输入框，打字聊天 |
| 输入框里按 `Esc` | 收起输入框与用量面板 |
| 拖拽 | 移动位置（记住位置），用量面板跟着走 |
| **右键宠物窗口** | 重新设置… / 视觉功能（让她看屏幕）/ 专注模式（免打扰）/ 跟它说话 / 退出 |
| 右键托盘图标 | 同一套菜单（Windows 11 可能把托盘图标收进溢出区˄，右键宠物更稳） |

> 启动时会用气泡提示一次入口（右键改设置、唤醒词叫她）。托盘图标如果找不到，
> 按 `Win+B` 聚焦托盘区，或在 Windows 设置里把常用图标改为"始终显示"。

### 它会自己记住你

不用每次都说"记住我喜欢什么"——聊完天后她会自己把要点整理进长期记忆：

1. **聊完自动提炼**：一次对话结束，她会把「你本人相关、以后还会起作用」的内容
   （身份、喜好、在忙的事、明确让你别碰的话题）提炼出来，并**和旧笔记合并**——
   同义的两条会合成一条更完整的表述，而不是越堆越多。
2. **不记的东西**：一次性的闲聊寒暄、密码/账号/验证码这类凭据、
   她自己说过的话。凭据即使被说出来也会被关键词规则拦在门外。
3. **明文可查可删**：笔记存在 `notes.jsonl`，纯文本一行一条。
   设置窗「记忆」区可以**查看全部笔记**、**一键清除**。
   嫌整理多花钱，也可以在那儿关掉"对话结束后自己整理记忆"（`chat.auto_consolidate`），
   关掉后仍保留"记住X"这类显式指令的提取。
4. **它只喂提示词尾部**：笔记不进 system 区，这样才能保住提示词前缀缓存——
   用量面板上的「命中」一栏就是这件事是否成立的证据。

### 4. 验证自检

```bash
python main.py --self-test
```

依次检查：① 截屏 + dHash（同帧距离应为 0）② 节流门时间注入（30s 内主动应被拒）
③ MOSS-TTS-Nano 真实合成一段话并存到 `data/selftest_tts.wav` ④ DeepSeek 客户端装配。

## 目录结构

```
little-whale-pet/
├── main.py                  # 装配入口（单进程）
├── config.yaml              # 全部可调参数（改行为不改代码）
├── core/                    # 状态、节流门、提示词、类型
│   ├── gate.py              #   SpeechGate：唯一确定性放行点
│   ├── prompts.py           #   system 前缀字节固定（吃 prompt 缓存）
│   ├── memory.py            #   持久笔记（notes.jsonl，明文可查可删 + 合并整理）
│   ├── usage.py             #   用量统计（小时/天/月/总计 + 本次启动，内存态不落盘）
│   └── state.py             #   JSONL 决策日志 + 原子状态写
├── observe/                 # 看屏幕
│   ├── screen.py            #   mss 截屏 + 全屏/前台窗口检测
│   ├── differ.py            #   dHash 变化检测（省 API 的前置门）
│   ├── judge.py             #   DeepSeek 视觉调用 + JSON 解析
│   └── loop.py              #   观察线程状态机（让行/退避/打转检测）
├── chat/                    # 用户搭话的会话通道
├── voice/
│   ├── moss.py              #   MOSS-TTS-Nano 进程内推理（48kHz 立体声）
│   ├── cache.py             #   短句 PCM 缓存 + 分句（与引擎无关）
│   ├── router.py            #   MOSS 优先 / SAPI5 保底的统一发声接口
│   ├── player.py            #   播放线程（按句合成、整句播出）
│   ├── fallback_sapi.py     #   MOSS 不可用时的保底（Windows 内置语音）
│   └── wake.py              #   唤醒词监听（Vosk + 能量门 + 同音容错）
├── pet/                     # PySide6 桌宠窗口 + 动画 + 托盘 + 首次设置弹窗
├── scripts/
│   ├── prepare_assets.py    #   官方 VP9-alpha webm → 清噪点+预乘缩放 → 320×180@12fps PNG 帧
│   ├── _tts_probe.py        #   离线验证 TTS 链路（预热缓存 + 按句连续播出）
│   ├── register_voice.py    #   音色注册（零样本克隆，无需转写文本）
│   └── build_exe.py         #   打包成 exe（PyInstaller）+ 桌面快捷方式
├── core/
│   ├── paths.py             #   运行路径（开发/打包/数据目录解析）
│   ├── secrets.py           #   API 密钥本地持久化（原子写）
│   └── prefs.py             #   用户偏好覆盖层（user_prefs.json）
├── vendor/moss_tts_nano/    # MOSS-TTS-Nano 官方 Apache 2.0 运行时（vendored，含本地化改动）
├── models/moss-tts-nano-onnx/        # TTS 模型（约 642MB）
├── models/moss-audio-tokenizer-nano-onnx/  # 音频解码器（约 87MB）
├── models/moss-voices/      # 注册音色（<名字>/reference.wav）
├── models/vosk-small-cn/    # 唤醒词识别模型（约 66MB）
└── pet/assets/frames/       # 1019 张动画帧（9 个状态，RGBA 已抠透明）
```

## 配置（config.yaml）

| 区块 | 关键项 | 说明 |
|------|--------|------|
| `observe` | `interval_idle_sec` / `interval_active_sec` | 静止时 10s 一轮；检测到变化后 3s 跟进（保持 60s） |
| | `min_api_interval_sec` | API 调用最小间隔，硬防刷 |
| | `diff_threshold` | dHash 汉明距离阈值（0–64），越大越不灵敏 |
| `gate` | `proactive_min_interval_sec` | 主动发声最小间隔（默认 30s） |
| | `max_proactive_per_hour` | 主动发言每小时上限（默认 10 次） |
| | `daily_call_budget` / `session_reserve_calls` | 日 API 预算 300 次，其中 60 次留给被动会话 |
| | `dnd_fullscreen` / `title_blacklist` | 全屏免打扰 + 隐私黑名单（网银/密码类窗口不截屏分析） |
| `wake` | `enabled` / `phrase` | 唤醒监听开关；**唤醒词 = 你对她的称呼**（设置窗可改，热生效） |
| | `energy_threshold` | RMS 能量门：低于视为静音不喂识别器（省 CPU；环境吵就调大） |
| | `silence_ms` / `command_timeout_sec` | 唤醒后静默多久算说完 / 等待指令的最长时间 |
| | `cooldown_sec` | 唤醒后冷却，防连续触发 |
| `chat` | `idle_finish_sec` | 会话空闲多久自动收尾（默认 8s）；不收尾会一直挂起唤醒监听 |
| `model` | `vision_model` / `chat_model` | 模型名只放这里；会话复用同一模型共享 system 前缀吃缓存 |
| | `detail: low` | 512×512 降尺度，每图硬顶 384 token |
| `voice` | `voice_name` | 当前音色（见下节） |
| | `fallback` | MOSS-TTS-Nano 加载失败时的保底引擎（`sapi` = Windows 内置语音；`none` = 静默） |
| `pet` | `scale` / `corner` / `fps` | 窗口缩放、停靠角落、帧率 |

人设在 `persona` 段（`name` / `style` / `user_address` = 她怎么称呼你）。
**改 `prompt_version` 会让 system 前缀缓存显式失效**——
调整人设后顺手 +1，避免新旧人格串味。

设置窗里改的东西写进 `data/user_prefs.json`，启动时深合并覆盖 `config.yaml`
默认值——所以两份文件都别删：前者是默认模板，后者是你的个人设置。

## 唤醒词（像手机助手一样叫她）

链路：麦克风 16kHz → **能量门**（静音不喂识别器，省 CPU）→ Vosk 流式识别 →
滑动窗口匹配唤醒词（**字符级 + 拼音级双重同音容错**，实测"小鲸鱼"会被识别成
"小金鱼"、"幼楚"被识别成"标出"也能叫醒）→ 她应一声 → 继续说指令 →
**静默 3s 收尾**（她开口播应答的时间不计入这 3s）→ 交给 DeepSeek 回答，
思考开始时宠物显示"思考中…"气泡。

几个刻意的设计：

- **她说话时不拾音**：播放中 / 会话进行中 / 专注模式都会暂停监听，
  否则她会听到自己的声音然后自己接话。两类暂停语义不同——
  **会话/专注模式**（接管类）会丢弃唤醒后没说完的半个指令；
  **播放中**（播放类）不丢：唤醒后要先播应答再听指令，
  且暂停期间指令的截止时间顺延（等应答播完再接着等 3s）。
  曾经这里出过 bug：应答播完只释放了"ack"暂停原因，漏放"speaking"，
  泄漏一个原因 = 唤醒监听永久假死——表现为"第一次叫有反应，
  之后再叫什么都没反应，但打字可以"。现在有回归测试钉住
  （`tests/test_wake.py` / `tests/test_wiring.py`）。
- **麦克风设备自愈合**：MME 输入流会被瞬时故障打断（别的程序抢占麦克风、
  音频栈重初始化、本机挂的麦克风 AI 降噪、驱动抽风）——早期版本 `read()`
  一抛异常就 `break` 退出监听线程，结果是"第一次叫有反应、之后永远不应答"，
  日志里留着「麦克风读取中断，唤醒监听停止。」。现在改为：读异常 / 读卡死
  （`read_available` 长时间不增长）都走**退避重开流**（1s→10s 封顶），
  恢复后重置捕获状态当新的一轮听，绝不永久退出。回归测试在
  `tests/test_wake_resilience.py`。
- **唤醒后 0.4s 内的尾音不算指令**（滤掉"小鲸鱼"三个字的残留），
  少于 2 个有效字的识别结果当杂音丢弃。
- **冷却 8 秒**：连续喊不会连续触发。
- 匹配只取长度 n 与 n+1 的滑窗：放行 n-1 会把"小鱼"这种正常词误判成"小鲸鱼"。
- **会话 8s 空闲自动收尾**：不说话也会关掉会话通道，恢复唤醒监听
  （曾经的 bug：会话挂起 10 分钟，期间再喊名字毫无反应）。
- **唤醒应答开机就预热**：本机合成只有 ~0.7x 实时（实测 RTF 0.67–0.90，
  threads=6 最快），真等到唤醒那一刻再合成要干等 3–5 秒。所以引擎加载完
  就在后台把 3 条应答语合成好存进 `tts_cache.npz`（按引擎+音色隔离，
  换音色自动作废），之后命中缓存约 0.04 秒就出声。日志里会打
  `[tts] 已预热 3 条唤醒应答`。
- **防合成跑偏重试**：MOSS 短文本有概率不预测结束、继续重复
  （实测"我在，主人。"偶发产出 4.9s 的"我在主人这，主人这…"，无标点短句
  如"好的"单次跑偏率高达 42%）。三重防护：每次合成都换随机种子
  （避开 vendor 固定种子 1234 的必现陷阱）、句尾补标点锚定结束预测
  （无标点文本 MOSS 系统性不预测 EOS，句号不发声）、超过时长上限就
  重新合成（5 次尝试全挂概率约 1.3%）。

唤醒词选择建议：

- **3 字以上更稳**（如「小鲸鱼」）。2 字词（如「幼楚」）两个音节都听错时，
  只能靠拼音级匹配兜底，容错空间小；设置窗会对 2 字词给出橙色提示。
- 读起来顺口、和环境日常用语区分度大的词效果好。
- 改唤醒词：托盘「重新设置…」，立即热生效，不用重启。

排障："喊了没反应"时看运行日志 `%LOCALAPPDATA%\little-whale-pet\run.log`——
能量门之上听到的内容会限流打印（`[log] 听到: …`），唤醒命中也有
`[wake] 唤醒词命中` 一行，可直接判断是没听到还是听错了。
叫了她但 3s 内没说话会打 `唤醒后 Ns 内没有听到指令，本轮结束`；
合成跑偏重试会打 `[tts] …超上限…（采样跑偏），换种子重试`。

唤醒模型是 Vosk 中文小模型（约 66MB，离线）。如果 `models/vosk-small-cn/` 不存在，
启动会提示"唤醒监听启动失败"但**不影响点击/打字对话**。
下载：<https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip>
解压后把 `vosk-model-small-cn-0.22` 改名成 `vosk-small-cn`。

> 已知坑：Vosk 底层是 Kaldi 的 C++ 代码，按 ANSI 窄字节开文件，
> 含中文的绝对路径会加载失败。程序会自动建一个纯 ASCII 的目录联接
> （NTFS junction，免管理员权限）绕过，无需手动处理。

## 换音色（零样本克隆）

MOSS-TTS-Nano 支持 3–10 秒干净人声样本复刻音色，**不需要转写文本**：

```bash
# 用自己的样本（wav/flac/ogg 均可，建议单句、无噪音）
python scripts/register_voice.py --audio assets/ref/my_voice.wav --name myvoice

# 查看已注册音色
python scripts/register_voice.py --list
```

然后重开设置窗（托盘「重新设置…」）选 `myvoice`，或改 `config.yaml` 的
`voice.voice_name`。

已注册的音色在 `models/moss-voices/<名字>/`（reference.wav + meta.json）。
仓库自带 `xiaojingyu`（软糯萝莉音色）。

> 合规提醒（MOSS-TTS-Nano 模型卡）：只克隆自己或已获明确授权的声音；
> 合成音频应在适当场合标注为合成产物。参考音频太长（>10s）、太吵都会明显
> 拉低相似度与稳定性。注册后重新打包 exe 才会随包分发
> （`scripts/build_exe.py --fast` 也会同步音色目录）。

## 它怎么决定要不要说话

1. **变化门**：每轮先截屏算 dHash，和上一帧汉明距离小于阈值就直接跳过——
   画面没变不花一分钱。
2. **隐私门**：全屏应用（游戏/视频）或前台窗口标题命中黑名单 → 本轮不分析。
3. **视觉判断**：把当前帧（`detail: low`，512×512）+ 最近几轮对话摘要发给
   DeepSeek，要求按固定 JSON 返回 `{speak, text, reason}`。
4. **节流门**：30 秒内最多主动说一次、每小时最多 10 次、日预算 300 次
   （含被动会话，且始终留 60 次保底给用户搭话）。任一不满足就静默。
5. **合成播放**：放行后 MOSS-TTS-Nano 按句合成、整句播出（本机合成 ~0.7x
   实时，同一句内边合成边播会断流）；句长超 32 字会按标点切分。

所有决策（含被门拦下的）都追加写入 `data/decisions.jsonl`，
可回放审计：`python tests/replay.py`。

## 测试

```bash
python -m pytest tests/ -q          # dHash 阈值标定 + 节流门单测
python main.py --self-test          # 端到端冒烟（含真实 TTS）
```

## 资源占用

| 项 | 典型值 |
|----|--------|
| 常驻内存 | 约 1.0–1.3 GB（TTS ONNX 0.6–1.0 GB + PySide6） |
| 静止时 CPU | < 1%（10s 一轮截屏 + dHash） |
| 分析时 CPU | TTS 合成瞬间可吃满 `voice.threads` 个核，持续约 2–5s |
| 磁盘 | 模型 730MB + 动画帧 104MB |

想更省：把 `voice.threads` 调小、`observe.interval_idle_sec` 调大、
或用托盘"专注模式"直接挂起观察循环。

MOSS-TTS-Nano 模型加载失败时（如模型目录被移走）会自动降级到 SAPI5 内置语音
（音质差但零依赖），启动日志会打印 `[voice] MOSS-TTS-Nano 加载失败…已降级`；
不想要保底就把 `voice.fallback` 改成 `none`。

## 打包成 exe

```bash
python scripts/build_exe.py                # 构建 + 创建桌面快捷方式
python scripts/build_exe.py --clean        # 先清掉旧的 build/dist
python scripts/build_exe.py --no-shortcut  # 只构建不建快捷方式
```

产物在 `dist/小鲸鱼/`：主程序 `小鲸鱼.exe` + `pet/assets`（帧/图标）+
`models`（TTS 730MB + 唤醒 66MB）+ `vendor` + `config.yaml`，整包约 950 MB。
用 `--onedir` 模式：启动不用解压，整个文件夹拷走即用。

运行期数据（密钥/偏好/状态/日志）写 `%LOCALAPPDATA%\little-whale-pet`，
所以 dist 文件夹放哪都行、甚至只读也没事。
打包版没有控制台窗口，日志镜像到 `%LOCALAPPDATA%\little-whale-pet\run.log`，
出问题先看这个文件。

> 想把 exe 挪到别处/发给别人：整个 `dist/小鲸鱼` 文件夹一起拷，
> 不要只拷 exe——模型和动画帧都在同一个目录里。

## 合规与致谢

- **形象素材**：来自 [dsh-pet](https://github.com/PC2005-cloud/dsh-pet)（作者 PC2005-cloud），
  代码 MIT、**素材禁止商用，二创需署名**。本项目沿用该许可，请勿将桌宠形象用于商业用途。
  帧素材由 `scripts/prepare_assets.py` 从官方插件包 `assets/webm/` 的 **VP9-alpha**
  webm 直接解码生成（官方素材链：AI 绿幕视频 → PR 手工抠像 → 640×360 透明 webm），
  alpha 是官方人工修过的连续通道，本管线只清 VP9 压缩噪点 + 预乘线性光缩放到
  320×180，**不做任何抠像**。官方源素材归档在 `_assets_src/webm/`。
  曾经的弯路：拿到过黑幕合成且丢 alpha 的副本，自建"黑幕方程反解"抠像，
  暗部细节被系统性吃掉（角色边缘全是破洞）——已整套废弃，见脚本文档字符串。
- **TTS**：[MOSS-TTS-Nano](https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Nano)
  （OpenMOSS 团队 / MOSI.AI），Apache 2.0；ONNX 导出
  `OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX` + `MOSS-Audio-Tokenizer-Nano-ONNX`。
  运行时已 vendored 到 `vendor/moss_tts_nano/`（含本地化改动说明，见 `NOTICE`）。
- **唤醒识别**：[Vosk](https://alphacephei.com/vosk/)（Apache 2.0）+
  中文小模型 `vosk-model-small-cn-0.22`（Apache 2.0）。
- **声音克隆伦理**：只克隆自己或已获明确授权的声音；对外播放合成音频时应说明是合成产物。
- **隐私**：截屏只发当前帧给 DeepSeek（`detail: low` 降尺度），不做本地持久化；
  隐私黑名单窗口不会被分析。麦克风音频**只在本地做唤醒词匹配**，不上传、
  不落盘（Vosk 是离线模型）。所有外发网络请求只有 DeepSeek 这一条通道，
  可自行在防火墙层面审计。

## 已知边界

- 主动发声是**低频自主开口**，不是实时流式对话——"响应越快越好"的场景请用唤醒词或双击搭话。
- 唤醒词监听常驻一个 CPU 核的一部分：安静环境下接近 0（能量门拦住不喂识别器），
  有人说话时约 10–30%。不想要就把 `wake.enabled` 改成 `false`。
- MOSS-TTS-Nano 0.1B 是轻量模型，音质有取舍；换更大模型需改 `voice/moss.py` 的运行时装配。
- `detail: low` 只适合粗粒度场景判断（"在看代码/在看视频/在聊天"），
  不要指望它读清屏幕上的小字。
- 只验证了 Windows 10/11；截屏与窗口检测依赖 win32 API，不跨平台。
