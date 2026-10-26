# 小鲸鱼桌宠 Agent Harness 架构设计

> 设计台账：2026-10-23 · 小鲸鱼桌宠（little-whale-pet）· v1 · 关键决策 17 项 · 未知区 8 项 · 独立评审：通过（存疑 8 项已回修，证据不足 3 项已按待实测/待核实处理，结论见 §16）

## 1. 需求规格

| 维度 | 内容 | 状态 |
|---|---|---|
| 目标与用途 | 本地桌面 AI 伙伴：常驻桌面角落，低频主动观察屏幕，自主判断"值不值得开口"，用 DeepSeek 视觉 API 思考，用本地 TTS（Audio8 0.1B INT8）以克隆音色主动发声；用户主动搭话时必须回应 | 【已确认】 |
| 成功标准 | ① 主动发言 ≤10 次/小时；② 用户搭话回应率 100%（免打扰/黑名单场景除外，且预算保底额度内）；③ 画面无值得说内容时静默率 ≥80%（ground truth = 录屏回放人工逐场景标注 + dHash 拦截率自动统计，操作化见 §4.6）；④ 常驻内存 ≤1.5GB、空闲 CPU ≤5%；⑤ 端到端（发现值得说的画面→出声）目标 ≤6s、上限 ≤8s；⑥ 说话中可立即打断；⑦ 主动发言单句 ≤80 字（TTS 速度预算约束，见 §5） | 【已确认】Q1/Q4/Q6 |
| 验收来源（oracle） | 决策日志（JSONL，每轮记录变化分数/是否调 API/模型输出/放行结果/延迟）+ 每周人工抽 20 条按显式 rubric 打分 + 10 段标准场景录屏回放回归；主观质量人评，客观指标（频率/延迟/预算/成功率）自动统计 | 【默认假设】 |
| 任务结构 | 双模态：A. 自主观察循环（截屏→dHash 变化检测→视觉判断→放行门→TTS）；B. 用户会话循环（输入→上下文+历史→回应→TTS）。B 进行时 A 挂起 | 【已确认】Q1 新增会话通道 |
| 数据/资源边界 | 只读屏幕（截屏 + 前台窗口标题）；不写任何系统状态；外部通道仅 DeepSeek API（画面上行）与本地音频播放；TTS 参考样本必须是自己或已获授权的声音（Audio8 模型卡合规要求）；API key 走环境变量，不进代码/配置仓库/日志 | 【默认假设】 |
| 硬约束 | 单个 Python 主进程串起 截屏→DeepSeek→TTS；不起额外 HTTP/WebSocket 服务（不用 blind-vision-mcp、不跑 Audio8 的 HTTP 服务端）；视觉 detail:"low"；TTS 用 Audio8 0.1B INT8 本地推理 | 【已确认】 |
| 用户与场景 | 单人本地 Windows 11；全屏应用（游戏/演示）自动免打扰；快捷键手动开关"专注模式"；音量跟随系统主音量 | 【默认假设】 |
| 边界（不做什么） | 不做零延迟流式对话；不做 OCR 级小字识别（detail:low 硬顶 384 token/图）；不承诺轻量 TTS 的音质上限；主动说话必须节流；二期才做语音输入（ASR） | 【已确认】 |
| 技术栈约束 | Python 3.11+（Audio8 setup.sh 硬校验）；PySide6；mss；Pillow+numpy；onnxruntime（CPUExecutionProvider）；openai SDK（DeepSeek OpenAI 兼容端点）；PyYAML | 【已确认】 |
| 模型策略与成本 | 单模型（DeepSeek 视觉实验模型）完成 看+判+说+聊；日预算 ≤300 次 API 调用、费用 ≤¥5/日 作熔断；接受前缀缓存计费；窄决策占比≈0（变化检测是确定性代码，不需要第二档模型） | 【默认假设】 |
| 效率与体感 | 轮询 3–10s 自适应；主动节流 30s；端到端目标 ≤6s、上限 ≤8s；可中断（点宠物/快捷键立即闭嘴）；TTS 常驻热加载换首音延迟；"响应越快越好" | 【已确认】Q6 |

## 2. 架构范式 P

| 子维度 | 决策 | 理由 |
|---|---|---|
| 扩展方式 | 插件化（依赖注入，能力可增删替换） | 观察器/发声器/形象源/会话通道都是可替换组件；核心循环极薄，换形象或换 TTS 不动循环 |
| 配置方式 | 声明式（YAML 配置 + dataclass 校验，代码只留默认值） | 人设、节流、阈值、模型名、路径全在配置；改行为不改代码，也便于 A/B 人设 |
| 部署拓扑 | 单机单进程 | 用户硬约束；无远程子代理、无分布式状态同步 |
| 编排模式 | 中心化（容器统一装配，主循环调度各组件） | 单智能体，无协商无扇出 |
| 交互模式（开放子维度） | 主动观察为主 + 被动会话为辅，二者互斥（会话中挂起观察） | Q1 新增需求；会话优先级高于观察，避免"主人说话时宠物自说自话" |

范式 P 对 E 层的影响：插件化 → 执行模式（观察/会话）可增删替换；声明式 → 轮询间隔、节流参数全部走配置；单机 → 无跨节点状态；中心化 → SpeechGate 是唯一放行裁决点。

## 3. 技术栈选型

| 项 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.11+ | 用户指定；Audio8 运行时硬校验 3.11 |
| UI 框架 | PySide6（Qt6） | 透明无边框置顶窗 + 帧动画 + 输入框；同角色独立桌宠 dsh-pet-indesktop（PySide6，v4.3.4）已验证该路线 |
| 截屏 | mss | Windows 上最快的纯截屏库（毫秒级），无 GUI 依赖 |
| 图像/哈希 | Pillow + numpy（dHash 自实现，约 30 行，不引 OpenCV） | 变化检测只需灰度+缩放+位比较，省一份上百 MB 的依赖 |
| LLM 客户端 | openai SDK → base_url=https://api.deepseek.com | 官方 OpenAI 兼容；模型名只出现在配置 |
| TTS | onnxruntime（CPUExecutionProvider）+ Audio8-TTS-Preview-0.1B-ONNX-INT8（进程内加载 ONNX 会话，不跑其 HTTP 服务） | 单进程约束；~100M 参数（含 codec ~170M），常规合成 ~0.6GB / 含注册器 ~1.0GB；44.1kHz；零样本克隆 |
| 音频播放 | sounddevice（PortAudio 绑定）独立线程 + 队列 | 44.1kHz float32 PCM；支持即时 stop |
| 配置 | PyYAML + dataclass 启动校验 | 声明式；缺字段启动即报错而非运行时崩 |
| TTS 降级 | Windows SAPI5（pyttsx3） | 保底：音质差但零额外依赖、断网可用 |
| 形象素材 | dsh-pet（PC2005-cloud/dsh-pet）VP9-Alpha webm 动画子集 + 构建期预解码 PNG 帧序列 | 用户指定角色；预解码换运行时低 CPU（见 §7 assets 处理） |

**模型组合**（四分类档位；候选来源 = 用户指定 + 官方文档核实，2026-10-23）

| 档位 | 候选模型 | 职责 | 单价量级 | 预估 token 占比 |
|---|---|---|---|---|
| 确定性代码 | —（mss/numpy/pywin32） | 截屏、dHash、黑名单、节流门、日志 | — | — |
| 决策型模型 | —（不需要：变化检测即零成本前置门） | — | — | — |
| 快速低价 LLM | —（不需要第二档：任务异质性被前置门吸收） | — | — | — |
| 强力 LLM | DeepSeek 视觉实验模型（deepseek-v4-flash-vision-exp，Flash 档） | 看画面判断值不值得说 + 生成一句话 + 会话回应 | 以官方价目表为准（实现时核实并写入配置注释） | ~100%（观察 ≤1350 token/次；会话 ≤1700 token/次） |

形态判定：**A（单智能体 + 单模型）**。显式质疑记录：① "整个循环同用一档模型"被质疑——结论是确实同档就够：变化检测（确定性代码）预期把静止场景的多数轮次拦在 API 之前（拦截率待 test_differ.py 标定，强依赖场景分布：视频/滚动网页趋近 0，静止阅读趋近 100%，不作通用百分比承诺），剩下的是"看懂画面内容"的语义任务，窄决策模型干不了，再上一个快筛模型只是把成本从一个 API 挪到另一个 API；② 能否上多 Agent——不能：任务单线、无可并行子任务、无上下文隔离需求，过不了编排决策卡第一步。

## 4. 六层设计

### 4.1 E 执行循环

- 设计决策：
  - 双循环结构：**观察循环**（ObserveLoop，常驻线程，自主模式）+ **会话执行器**（ChatSession，用户输入触发的短任务线程）。会话进行中 `state.mode="chat"`，观察循环让行（每 0.5s 检查一次），会话结束恢复；单次会话上限 20 轮或 10 分钟，到顶强制恢复观察一轮（防长会话把观察循环无限挂起）。
  - 观察循环状态机：`IDLE → CAPTURE → DIFF →(score<threshold)→ IDLE` / `→ PRIVACY →(命中)→ IDLE` / `→ JUDGE → GATE →(放行)→ SPEAK → IDLE` / `→(拦截)→ LOG → IDLE`。
  - 轮询自适应：静止时间隔 10s；一旦检测到变化，接下来 60s 内间隔收紧到 3s（快跟进）；API 调用最小间隔 5s（防刷）。
  - 终止与保险（常驻循环无"任务完成"概念，三道保险）：单次 API 超时 8s + 日调用预算熔断（300 次）+ 连续失败 5 次退避挂起（指数退避，上限 10min）。
  - 失败回喂：API 错误分四类恢复——瞬时（连接/超时：指数退避+抖动重试 3 次）/ 配额（日预算触及保底线：观察循环挂起，保底额度留给会话，见 §5 降级链）/ 永久（4xx 参数类：挂起观察+宠物错误动画+日志）/ 资源（预算耗尽：同配额）。所有失败作为状态变更写日志并驱动 UI 错误态，不在循环里吞异常。
  - 打转检测：同一 speak 文本指纹（sha1 前 8 位）连续出现 3 次 → 该文本进冷却名单 10min；观察循环连续 50 轮无变化 → 间隔已自然拉满（本就有界，防 CPU 空转）。
  - 墙钟预算：端到端目标 ≤6s = API ≤3s + TTS ≤2s + 组装/调度 ≤1s；上限 ≤8s。
  - 并行：TTS 合成与播放在独立线程，观察循环**不等待播放完成**（说完即继续观察）；播放中宠物显示说话动画。
  - 流式与可中断：不流式（低频场景，流式收益＜复杂度）；可中断——说话中点击宠物或按快捷键立即停播清队列（副作用清单：已播音频不可收回，但未播队列全部作废，utterance 记为 interrupted）。
- 落地规格：`ObserveLoop(QThread)` 主循环如上；`ChatSession(QThread)` 每轮用户输入起一个，串行执行（同一时刻最多 1 个，防并发把 API 打爆）；`SpeechGate.allow()` 是循环内唯一放行裁决；阶段耗时写入决策日志供 V 层聚合。

### 4.2 T 工具注册

- 设计决策：
  - **固定管线，不是动态 function-calling**：本 harness 不给 LLM 工具表（给固定管线配没人调用的工具注册表 = 过度设计）。模型只做"判断+生成"，所有动作由确定性代码执行。
  - 组件即注入（依赖注入）：ScreenSource / FrameDiffer / VisionJudge / SpeechGate / VoiceEngine / SpeechPlayer / PetView / ChatChannel 八件套在容器注册，可替换（换 TTS、换形象源不动循环）。
  - 注册 ≠ 披露 ≠ 执行授权：组件在容器注册；本设计无"模型可见工具"层；执行授权唯一收口在 SpeechGate（放行/拦截+原因）。
  - 执行环境：全部本地进程内；写操作影响半径 = 本地音频播放 + 本地日志文件，无系统级写。
  - 凭证：API key 从环境变量 `DEEPSEEK_API_KEY` 读取，启动时注入 client；不进配置文件、不进日志、不随日志上传。
- 落地规格：`Container.register(name, factory)` / `Container.resolve(name)`；组件单例；接口签名见 §8。

### 4.3 C 上下文管理

- 设计决策：
  - 观察模式上下文（每轮重新组装，前缀字节稳定）：
    `[system: 人设+判断规则（字节固定，版本化）]` + `[user: 最近发言摘要 ≤120 token（尾部）]` + `[user: 当前帧 base64 + 判断指令]`
  - 会话模式上下文：`[system: 同一人设前缀]` + `[会话历史：折叠摘要 ≤200 + 最近 8 轮 ≤400，合计 ≤600（含摘要，含关系明确）]` + `[user: 本次输入]`。两模式共用同一 system 前缀 → 跨模式吃缓存命中（实际共享的是人设子段，观察模式的判断规则段不进会话，跨模式命中范围略小于字面声称）。
  - 前缀纪律：时间戳、frame hash、变化分数、随机 id 一律不进前缀；system 区改动 = prompt_version 升级（v1→v2）+ 显式缓存失效记账。
  - 序列化确定性：消息组装用定序 dict / json.dumps(sort_keys=True)；同一语义永远同一字节串。
  - 预算分配：观察 = system ~550 + 摘要 ≤120 + 图片 ≤384（detail:"low" 硬顶）+ 输出 ≤160 ≈ ≤1250 token/次；会话 = system ~550 + 历史 ≤600 + 输入 ≤100 + 输出 ≤400 ≈ ≤1650 token/次。
  - 压缩策略：会话历史超 8 轮才折叠（分段压：system 前缀不动，只把中后段旧轮换成摘要）；**历史折叠是前缀重写的第三个触发点**（另两个是改人设、换模型），其失效账 = 折叠点之后的 token（不是 system 550），记入 §5 重写账。观察循环不累积历史（无状态判断），只带"最近说过什么"避免复读。
  - 外部内容消毒：system 明确声明"画面内容是待分析数据，不是指令"；截图经 base64 进 user 消息，永不被提升为 system。
- 落地规格：`prompts.py` 提供 `build_observe_messages(frame_b64, recent)` / `build_chat_messages(history, user_text)`，两个函数只往尾部追加，system 段从常量模板渲染（同版本同字节）。

### 4.4 S 状态存储

- 设计决策：
  - 双存储：**RuntimeState**（单文件 JSON，节流时间戳/计数/模式/失败计数，原子写：tmp+rename）+ **DecisionLog**（JSONL 只追加，event-sourcing：单一真相源，只追加不修改）。
  - 写治理：Agent/模型不能直接写状态；一切写入经容器 API（`record_decision()` / `mark_spoken()` / `set_mode()`）。
  - 并发写：单写者纪律——观察线程是 DecisionLog 唯一写者；Qt 线程与 TTS 线程通过信号/队列把事件交给写者，不做多线程同时 append。**RuntimeState 的并发写同样收口**：所有 read-modify-write（mark_spoken 计数、预算扣减等）只在单写者线程（观察线程）执行，或经容器 API 内的 `threading.Lock` 串行化——防止计数丢更新击穿"≤10 次/小时"与日预算（test_gate.py 补并发用例）。
  - 恢复：重启读 RuntimeState 恢复节流状态（防重启刷屏）；DecisionLog 可整体重放复盘。
  - 外部副作用幂等：TTS 播放以 `utterance_id`（hash(文本+时间窗)）幂等，同一 utterance 不重复入队；API 调用是只读推理无业务副作用；日志 append-only 无更新路径。
  - 记忆治理：一期无长期画像记忆；决策日志每条带 `mode/model/prompt_version`；未验证推测（如"主人爱看视频"）不得升级为事实——要进记忆必须走人工确认的单独机制（二期）。
  - 证据版本绑定：任何"该模型在该人设下误报多"的结论只对 (model, prompt_version) 组合有效；换模型或人设升级后旧结论标注失效。
- 落地规格：`core/state.py`：`RuntimeState` dataclass + `save()/load()`；`DecisionLog.append(event: dict)` 批量 fsync（每事件 fsync 太慢，批次 1s 或 10 条）。

### 4.5 L 生命周期钩子

- 设计决策：
  - 拦截点（三个，全部确定性代码，不可被模型输出绕过）：
    - `pre_capture`：隐私黑名单——前台窗口标题命中黑名单关键词（网银/密码管理器/银行）→ 本轮跳过（连图都不截）。
    - `pre_speak`（SpeechGate，唯一放行点）：主动模式——30s 最小间隔 + ≤10 次/小时 + 日预算（余量 ≤ 保底额度 60 次时观察循环提前挂起，见 §5 降级链）+ 免打扰（全屏应用）；被动模式——仅 2s 防连点，**不受主动节流约束**（主人搭话必须回应，这是 Q1 确认的需求）。
    - `post_decision`：无论放行/拦截，记决策日志（含 gate 原因）。
  - 免打扰：全屏检测（pywin32 取前台窗口矩形 vs 屏幕矩形，≥95% 覆盖判全屏）；快捷键手动"专注模式"（托盘/全局热键，默认不绑——学 dsh-pet-indesktop 的教训，默认热键易与用户软件冲突）。
  - 人在回路四档落点：自动执行（节流窗口内的主动发言——副作用最轻且可立即撤销）；执行后告知（日志+宠物气泡留痕）；执行前确认（不需要——开口可撤销，停顿确认反而破坏"主动陪伴"的灵性）；直接阻断（黑名单/预算熔断/专注模式）。
  - 高危拦截纵深：pre_capture（隐私）与 pre_speak（节流）是两道独立检查，不依赖同一分类器；黑名单只做"跳过"，不做"静默降级分析"（避免变相偷看）。
  - 快筛门控不适用（无第二档模型）。
- 落地规格：`core/gate.py` 的 `SpeechGate.allow(mode, now)` 返回 `(bool, reason)`，reason 入日志；全屏/黑名单检测在 `observe/screen.py` 的 `foreground_title()` + `is_fullscreen()`。

### 4.6 V 评估接口

- 设计决策：
  - 客观指标（自动，从 DecisionLog 聚合）：主动发言次/小时、**dHash 拦截率**（api_called=false 且 diff<threshold 的轮次占比，与静默率分开测量）、静默率（api_called=true 且 speak=false 占比）、API 成功率与错误分类分布、端到端延迟分位（p50/p95）、TTS 首音延迟、预算消耗速度、打转抑制触发次数。
  - 主观质量（人工，每周抽 20 条）：rubric 三档——贴脸（符合人设）/ 有用（说了该说的）/ 烦人（不该说而说或打扰）。抽检样本带 `frame_hash`+reason，不存原图（隐私），需要时可按 hash 复现环境。
  - **静默率 ≥80% 的 ground truth 操作化**：每周 20 条抽检统计功效不足以单独支撑 80% 验收——ground truth 由录屏回放产生：10 段标准场景（写代码/看视频/挂机/弹错误框/聊天）在回放时由人工逐场景标注"每一轮是否真值得说"，得到该场景的条件静默率真值，再对照系统实际静默率计算偏差；dHash 拦截率作为自动代理指标持续监控（拦截率突降 = 阈值漂移信号）。
  - 生成者与评估者分离：模型生成发言；评估 = 确定性统计 + 人工抽检（独立上下文、显式 rubric、每周固定节奏）；**模型不自评**——不设"让模型给自己的发言打分"的环节（循环论证）。
  - 回归：`tests/replay.py` 回放 10 段标准场景录屏（写代码/看视频/挂机/弹错误框/聊天），VisionJudge 可替换为录制回放器，验证管线延迟、放行门逻辑与节流行为（不花 API 费用）。
  - 工具回执 ≠ 业务事实：TTS"播放完成"回执只证明音频播完，不证明主人听到——评估只观测系统行为，不假装观测主人反应；主人真实反馈走每周抽检时的人工备注（二期可做"这句烦"按钮）。
- 落地规格：`tests/replay.py` + `scripts/report.py`（从 JSONL 出周报：频率/延迟分位/预算/错误分布）。

## 5. 模型组合与任务分派

- 形态判定与理由：**形态 A（单智能体+单模型）**。质疑记录见 §3：变化检测（确定性代码）是零成本前置门， absorbed 了任务异质性；"看懂画面值不值得说"需要语义理解，属强力 LLM 域；不需要决策型第二档（无封闭答案集高频重复步骤——节流门是代码不是模型）。原本想靠多 Agent 实现的"专业化"（观察/会话/发声拆分）全部由单 Agent + 组件注入承担，能替代就不上多 Agent。
- 任务-模型分派表：

| 步骤 | 档位 | 理由 | 成本/延迟预算 |
|---|---|---|---|
| 截屏 | 确定性代码 | mss 机械动作 | ≤15ms |
| dHash 变化检测 | 确定性代码 | 答案集可枚举（汉明距离） | ≤10ms |
| 隐私/免打扰检测 | 确定性代码 | pywin32 窗口查询 | ≤5ms |
| 值不值得说+说什么 | 强力 LLM（DeepSeek 视觉 Flash 档） | 需语义理解画面内容 | ≤3s，≤1350 token/次 |
| 会话回应 | 同模型文本模式 | 复用同一 system 前缀吃缓存；质量不够时配置一行切 deepseek-flash | ≤3s，≤1700 token/次 |
| TTS 合成 | 本地模型（Audio8 0.1B INT8，CPU） | 音色克隆必须本地 | ≤2s/句（≤80 字，见下方估算） |
| TTS 播放 | 确定性代码（sounddevice） | 本地播放 | 实时 |
| 放行门 | 确定性代码 | 节流/预算/黑名单 | ≤1ms |

TTS 延迟估算（诚实口径）：Audio8 0.1B 的 DualAR 慢分支逐 token 生成，**官方未公布吞吐数据**（第三方曾报 8 线程约 19ms/token，未收录为已登记来源，待首轮压测实测回填）。按保守约 25ms/token、中文约 1 token/字估算：80 字约 80 token 约 2s（含快分支与 codec 解码摊销）。故**主动发言单句不超过 80 字**（宠物资言本应短）；若压测实测更慢，先砍字数上限，再议端到端 8s 上限。

- token 预算按档位分配：日 300 次调用的 worst case 为全会话 300×1650 约 **49.5 万 token**；观察为主典型为 300×1314 约 **40 万 token**。熔断以两条线为准：次数（300 次/日）与费用（5 元/日，按官方价目表核算）。评估/验证步骤计入预算——回放回归不花 API token（录制回放器），人工抽检零 token。
- 缓存与重算账：DeepSeek 上下文缓存计费的**存在性与折扣倍数待官方文档核实**（未收录进已登记来源）；前缀纪律本身有独立价值（确定性、防注入、可复现），决策不依赖折扣成立。system 前缀约 550 token，占观察调用输入约 42%，字节稳定则这部分可持续按缓存价计（倍数以厂商实时文档为准）。触发前缀重写的动作共三个：改人设（prompt_version v1→v2）、换模型、会话历史折叠——前两个赔约 550 token 重算，第三个赔折叠点之后的 token；都属显式版本/策略事件，记入设计台账。
- 级联升级条件：一期无低档。升级路径预留——VisionJudge 连续 N=3 次 JSON 解析失败或 reason 为空 → 用"文本协议兜底"（首词 SPEAK/SILENT）重试 1 次；仍失败按瞬时错误退避。若未来接入更强推理模型做"深思考"，升级条件=观察循环连续 10 轮 speak=true 但抽检烦人率 >30%。
- 降级链（预算口径已闭环）：
  - 日预算 300 次 = 观察 + 会话总计，其中预留被动会话保底额度 60 次（20%）；观察循环在预算余量不超过 60 时提前挂起，把余量留给会话——用户已确认"搭话必须回应"优先于省额度。
  - 配额/费用熔断后：主动观察全停；被动会话用保底额度维持回应；保底耗尽才停会话（宠物打盌动画+日志）。
  - 仅标题模式（无图文本判断）计入同一预算，输入 token 降约三成（去图 384/1314 约 29%，连带精简判断规则约五成），不是降一个量级。
  - Audio8 不可用（加载失败/推理崩溃）则降级 SAPI5 保底音色；两者全挂则宠物打盌动画 + 日志，不崩主进程。
- 评估者档位与生成者档位关系：评估者 = 确定性统计 + 人工（不花模型 token，"便宜"但不是跳过分离的理由——独立性由人工抽检的独立上下文保证）。

## 6. 多 Agent 编排

不适用。理由：任务单线无可并行子任务（步骤 1）；无上下文上限压力（单次 ≤1700 token，离窗口上限三个数量级）；无专业化需求无法被组件注入+单模型覆盖（步骤 1 模型分档判据）。反模式自查：无共享可变状态（单写者+锁纪律）、无无界扇出（并发上限 1）、无过度分解、无"子代理结论直接采信"问题（无子代理）。

## 7. 模块 / 目录结构

```
little-whale-pet/
├── main.py                     # 入口：装配 Container + 启动 Qt + 注册优雅退出
├── config.yaml                 # 声明式配置（人设/节流/阈值/模型/路径），见 §10
├── core/
│   ├── container.py            # 依赖注入容器：register/resolve，组件单例
│   ├── gate.py                 # SpeechGate：节流/预算/黑名单/免打扰，唯一放行点
│   ├── state.py                # RuntimeState（JSON 原子写）+ DecisionLog（JSONL 追加）
│   ├── prompts.py              # system prompt 模板（版本化，字节固定）+ 消息组装
│   └── types.py                # 全部 dataclass/TypedDict 定义（见 §9）
├── observe/
│   ├── screen.py               # ScreenSource：mss 截屏 + pywin32 前台标题/全屏检测
│   ├── differ.py               # FrameDiffer：dHash + 汉明距离（纯 numpy）
│   ├── judge.py                # VisionJudge：DeepSeek 客户端封装（观察判断+会话回应）
│   └── loop.py                 # ObserveLoop：QThread 状态机（见 §4.1）
├── chat/
│   ├── session.py              # ChatSession：QThread，会话历史+折叠，挂起观察循环
│   └── channel.py              # ChatChannel：PetWindow 输入框信号 → ChatSession 桥
├── voice/
│   ├── audio8.py               # Audio8Engine：onnxruntime 进程内推理（读 manifest 装载）
│   ├── player.py               # SpeechPlayer：sounddevice 独立线程+队列+stop_all
│   └── fallback_sapi.py        # SAPI5 降级发声（pyttsx3）
├── pet/
│   ├── app.py                  # QApplication + PetWindow：置顶透明/帧动画/输入框/托盘
│   ├── anim.py                 # 帧序列播放器（QPixmap + QTimer，状态→动画映射表）
│   └── assets/frames/<state>/  # 预解码 PNG 帧序列（构建期生成，见 scripts/）
├── scripts/
│   ├── prepare_assets.py       # dsh-pet webm → 状态动画 PNG 帧序列（PyAV/ffmpeg，一次性）
│   └── register_voice.py       # Audio8 音色注册：0.5–30s 样本+准确转写 → 注册档案
├── tests/
│   ├── test_differ.py          # dHash 阈值标定（用户实际屏幕场景）
│   ├── test_gate.py            # SpeechGate 单测（时间注入，不 sleep）
│   └── replay.py               # 录屏回放回归（Judge 可替换为录制回放器）
├── NOTICE                      # dsh-pet 素材署名（二创约定：附原作者 GitHub 地址）
├── requirements.txt
└── designs/                    # 本设计文档
```

素材处理说明：dsh-pet 动画为 VP9-Alpha webm（画布 2160×1215）。运行时只保留约 15 个状态动画（待机/思考/工作/说话/打盹/出错/点击回应），构建期用 `prepare_assets.py` 预解码为 640×360 PNG 帧序列（Qt 不原生支持 VP9-alpha，运行时解码既费 CPU 又要引解码依赖；预解码把成本移到一次性构建）。全部 106 个动画的完整库留作二期（运行时 PyAV 按需解码）。

## 8. 关键接口签名

### 8.1 数据源接口

```python
class ScreenSource:
    """mss 截屏 + pywin32 窗口信息。"""
    def __init__(self, monitor: int = 1) -> None: ...
    def capture(self) -> Image.Image:
        """全屏截图。mss: raw = sct.grab(mon); Image.frombytes('RGB', raw.size, raw.rgb)"""
    def foreground_title(self) -> str:
        """pywin32: win32gui.GetWindowText(win32gui.GetForegroundWindow())"""
    def is_fullscreen(self) -> bool:
        """前台窗口矩形 ≥ 屏幕矩形 95% 判全屏"""

class FrameDiffer:
    """dHash 差异检测。"""
    def __init__(self, hash_size: int = 8) -> None: ...
    def dhash(self, img: Image.Image) -> int:
        """灰度→9×8 缩放→行间位比较→64bit 整数"""
    def distance(self, h1: int, h2: int) -> int:
        """汉明距离 0..64"""
```

### 8.2 组件接口

```python
@dataclass(frozen=True)
class Decision:
    speak: bool
    text: str | None          # speak=True 时非空，≤150 字（Audio8 单句上限）
    reason: str               # 判断理由，入日志供抽检
    model: str
    prompt_version: str
    latency_ms: int

class VisionJudge:
    """DeepSeek OpenAI 兼容客户端；观察与会话两模式共用同一 system 前缀。"""
    def __init__(self, client: OpenAI, cfg: JudgeConfig) -> None: ...
    def judge(self, frame_b64: str, recent: str) -> Decision:
        """观察模式：detail='low' 图 + 判断指令 → Decision"""
    def reply(self, user_text: str, history: list[Turn]) -> str:
        """会话模式：文本 → 回应文本"""

class SpeechGate:
    """唯一放行点。确定性代码，模型输出不可绕过。"""
    def allow(self, mode: Literal["proactive", "reactive"], now: float) -> tuple[bool, str]:
        """返回 (放行?, 原因)；proactive 查 30s/10次每小时/日预算/免打扰；reactive 仅 2s 防连点"""

class Audio8Engine:
    """onnxruntime 进程内推理；张量名从 runtime_manifest.json 读，不硬编码。"""
    def __init__(self, model_dir: Path, voice_name: str) -> None: ...
    def synthesize(self, text: str) -> np.ndarray:
        """文本 → 44.1kHz float32 单声道 PCM；>150 字按标点切分合成后拼接"""

class SpeechPlayer:
    """sounddevice 独立线程 + 队列；可即时打断。"""
    def __init__(self, samplerate: int = 44100) -> None: ...
    def enqueue(self, utt_id: str, pcm: np.ndarray) -> None: ...
    def stop_all(self) -> None:
        """停播+清队列；未播 utterance 记 interrupted"""

class PetView:
    """Qt 主线程 UI。"""
    state_changed = Signal(str)                       # idle/think/work/talk/nap/error
    user_text_submitted = Signal(str)                 # 输入框 → ChatChannel
    def set_state(self, s: str) -> None: ...          # 状态→动画映射
```

### 8.3 钩子接口

```python
def pre_capture(title: str) -> bool:
    """隐私黑名单：命中返回 False（跳过本轮，连图都不截）"""

def pre_speak(ctx: SpeakContext) -> Verdict:
    """= SpeechGate.allow()，返回放行/拦截+原因"""

def post_decision(dec: Decision, gate_reason: str) -> None:
    """记 DecisionLog（无论放行/拦截）"""
```

## 9. 核心数据结构

### 9.1 状态结构

```python
@dataclass
class RuntimeState:
    last_spoken_at: float            # 上次主动发言 monotonic 时间（30s 节流）
    spoke_this_hour: int             # 本小时主动发言计数（≤10）
    hour_window_start: float         # 计数窗口起点
    calls_today: int                 # 日 API 调用计数（≤300）
    mode: str                        # observe / chat / dnd
    consecutive_failures: int        # 连续失败（≥5 挂起退避）
    suspended_until: float           # 挂起恢复时间
    text_cooldown: dict[str, float]  # 打转抑制：文本指纹 → 解冻时间
```

### 9.2 消息/任务结构（DecisionLog 单条事件）

```json
{
  "ts": "2026-10-23T21:14:03+08:00",
  "mode": "observe",
  "frame_hash": "a1b2c3d4",
  "diff": 18,
  "api_called": true,
  "decision": {"speak": true, "text": "主人又在加班啦……", "reason": "深夜+代码界面+持续画面"},
  "gate": "allow",
  "gate_reason": "",
  "tts_first_audio_ms": 1800,
  "end_to_end_ms": 4300,
  "model": "deepseek-v4-flash-vision-exp",
  "prompt_version": "v1"
}
```

### 9.3 配置结构

```python
@dataclass
class Config:
    persona: PersonaConfig        # name/style/prompt_version
    observe: ObserveConfig        # interval_idle_sec/interval_active_sec/min_api_interval_sec/diff_threshold
    gate: GateConfig              # proactive_min_interval_sec/max_proactive_per_hour/daily_call_budget/
                                  # daily_cost_budget_cny/dnd_fullscreen/title_blacklist
    model: ModelConfig            # base_url/vision_model/chat_model/detail/timeout_sec/max_tokens_*
    voice: VoiceConfig            # engine/model_dir/voice_name/reference_audio/reference_text/fallback
    pet: PetConfig                # always_on_top/click_through/corner/scale
```

## 10. 配置文件格式（config.yaml 完整示例）

```yaml
persona:
  name: 小鲸鱼
  style: "可爱、撒娇、偶尔吐槽；称呼用户为主人；深海鱼设定，离不开水但住在电脑里"
  prompt_version: v1              # 改动人设必须升版本 → 前缀缓存显式失效

observe:
  interval_idle_sec: 10           # 静止时轮询间隔
  interval_active_sec: 3          # 检测到变化后的跟进间隔（保持 60s）
  min_api_interval_sec: 5         # API 调用最小间隔（防刷）
  diff_threshold: 12              # dHash 汉明距离阈值 0..64，需 test_differ.py 标定

gate:
  proactive_min_interval_sec: 30  # 主动发声最小间隔
  max_proactive_per_hour: 10      # 主动发言每小时上限（Q1 已确认）
  daily_call_budget: 300          # 日 API 调用预算（观察+会话总计，熔断）
  session_reserve_calls: 60       # 其中预留被动会话保底额度（20%）：余量≤此值观察循环提前挂起，
                                  # 保证"搭话必须回应"优先于省额度（Q1 已确认）
  daily_cost_budget_cny: 5        # 日费用预算（按官方价目表核算，实现时核实写入）
  dnd_fullscreen: true            # 全屏应用免打扰
  title_blacklist: ["网银", "银行", "密码", "Password", "Keychain"]   # 隐私黑名单

model:
  base_url: https://api.deepseek.com
  vision_model: deepseek-v4-flash-vision-exp   # 实验模型；官方文档注明旧模型名将转由
                                              # DeepSeek-V4.1-Flash 服务并按 Flash 计费——
                                              # 模型名只放这里，实现时以官方文档为准
  chat_model: deepseek-v4-flash-vision-exp     # 会话复用同一模型（共享 system 前缀吃缓存）；
                                              # 质量不够时改这一行即可，无需改代码
  detail: low                          # 512×512 降尺度，每图硬顶 384 token
  timeout_sec: 8
  max_tokens_observe: 160              # 观察输出上限（一句话）
  max_tokens_chat: 400                 # 会话输出上限

voice:
  engine: audio8                       # audio8 | sapi
  model_dir: ./models/audio8-0.1b-int8
  voice_name: xiaojingyu               # register_voice.py 注册的音色名
  reference_audio: ./assets/ref/voice_sample.wav   # 0.5–30s，自己或已授权声音
  reference_text: "样本的准确转写文本"              # 必须与音频内容一致
  fallback: sapi                       # Audio8 不可用时的保底

pet:
  always_on_top: true
  click_through: false                 # True 时鼠标穿透（不能拖拽/点击对话）
  corner: bottom-right
  scale: 0.5
  topmost_reassert_sec: 30             # 置顶自检重 Assert（防资源管理器重启/DPI 变更丢置顶）
```

## 11. 层间交叉检查

| 关系 | 说明 |
|---|---|
| E↔L | 观察循环每次发声前必经 SpeechGate（不可绕过）；会话模式挂起观察由 L 层优先级规则驱动 E 层状态机迁移 |
| C↔E | 观察循环不累积历史（无状态判断）；会话历史折叠只在 chat 模式触发，不污染 observe 前缀 |
| T↔S | 组件无持久状态；一切写操作经容器 API 落 RuntimeState/DecisionLog（单写者） |
| V↔S | 周报指标从 DecisionLog 聚合；日志不存原图（隐私+体积），按 frame_hash 可复现 |
| C↔成本 | system 前缀版本化；三个重写触发点（换人设/换模型各赔约 550 token、历史折叠赔折叠点之后 token）均记台账 |
| E↔体感 | 端到端预算拆到阶段（API 3s/TTS 2s/杂项 1s）；TTS 热加载换首音延迟；播放不阻塞下一轮观察 |

## 12. 六层决策表

| 层 | 决策 | 依据 | 状态 |
|---|---|---|---|
| E | 双循环+状态机；自适应间隔 3/10s；无流式；说完不阻塞观察 | 低频主动场景；Q6 响应优先 | 【默认假设】 |
| T | 固定管线+DI 容器；无动态工具表；key 走环境变量 | 防过度设计；凭证不进执行环境 | 【默认假设】 |
| C | system 前缀字节稳定+尾追加；会话历史 8 轮折叠（=第三重写触发点，已记账）；外部内容标记为数据 | 前缀纪律（缓存折扣待核实，防注入与确定性不依赖折扣） | 【默认假设】 |
| S | RuntimeState JSON 原子写 + DecisionLog JSONL 单写者 + RuntimeState 写操作锁/单写者串行化 | event-sourcing 模式；并发写治理（防计数丢更新击穿上限） | 【默认假设】 |
| L | SpeechGate 确定性放行；pre_capture/pre_speak 双拦截；人在回路四档落点 | 模型输出不可信为闸门；隐私纵深 | 【默认假设】 |
| V | 确定性指标+每周人工抽检+录屏回放；模型不自评 | 生成者与评估者分离（验证强度阶梯第二档） | 【默认假设】 |
| 交互 | 主动观察为主+被动会话为辅，会话优先 | Q1/Q4 已确认 | 【已确认】 |
| 模型 | 形态 A 单模型；降级链：预算保底 60 次给会话，再标题模式，再 SAPI，再打盌 | 质疑记录见 §3/§5 | 【默认假设】 |
| 缓存 | system 版本化+定序序列化；重写触发点三个（人设/模型/历史折叠）均已记账 | prefix-stable-context 模式 | 【默认假设】 |

## 13. 生产级必答项（逐项回答）

| # | 必答项 | 结论 |
|---|---|---|
| 1 | 执行环境：工具在哪跑、凭证是否进执行环境 | 全本地进程；API key 环境变量注入，不进配置仓库/日志；Audio8 进程内 ONNX 不起服务端 |
| 2 | 错误回喂：失败如何作为观察回到上下文、如何分类恢复 | API 错误四类恢复（瞬时退避重试/配额熔断转标题模式/永久挂起+错误动画/预算耗尽）；TTS 失败降级 SAPI；全部写日志驱动 UI，不吞异常 |
| 3 | 打转检测与熔断：如何识别原地打转、如何打断 | 同文本指纹连续 3 次进 10min 冷却；连续 50 轮无变化间隔自然拉满；日预算 300 次/¥5 熔断；连续失败 5 次退避挂起 |
| 4 | 记忆治理：可信度、归属、失效条件 | 一期无长期记忆；决策日志带 mode/model/prompt_version；未验证推测不得升级为事实；证据只对 (model, prompt_version) 组合有效 |
| 5 | 外部副作用幂等：幂等键 / 状态查询 / 恢复策略 | TTS utterance_id 幂等（同文本同时窗不重复播）；API 只读无业务副作用；日志 append-only 无更新路径；重启读 RuntimeState 恢复节流 |
| 6 | 证据版本绑定：验证结论如何绑定版本、版本变更后如何失效 | 日志与周报结论绑定 (model, prompt_version)；人设升级 v1→v2 后旧结论标注失效；缓存前缀随版本显式失效 |
| 7 | 生成者与评估者分离：独立上下文、不看推理、显式评分标准、迭代预算 | 评估=确定性统计+每周人工抽检（独立上下文、显式三档 rubric、固定节奏）；模型不自评；回放回归用录制回放器零 token |
| 8 | 前缀稳定与缓存命中：稳定区/可变区划分、序列化确定性、重写触发点的失效代价、命中率观测 | system 区字节固定（时间戳/hash/随机 id 只进尾部）；json.dumps(sort_keys=True) 定序；重写触发点共三个（改人设/换模型各赔约 550 token、会话历史折叠赔折叠点之后 token）记台账；缓存计费存在性与折扣待官方文档核实，前缀纪律决策不依赖折扣成立；命中率从日志 token 字段周报观测 |

**可编码三门槛自检**：① 类型完整——§8/§9 引用的每个类型（Decision/RuntimeState/SpeakContext/Verdict/Turn/JudgeConfig 等）均有字段定义；② API 真实——mss（grab/raw.rgb）、pywin32（GetForegroundWindow/GetWindowText）、openai SDK（chat.completions.create + image_url detail 字段）、onnxruntime（CPUExecutionProvider）、sounddevice（OutputStream）、PySide6（FramelessWindowHint/WindowStaysOnTopHint/WA_TranslucentBackground）均为官方文档现存 API，核实日期 2026-10-23；③ 标准可达——≤6s 端到端拆成 API ≤3s（Flash 档短输出量级估计，**待首轮压测回填分位值**）+ TTS ≤2s（80 字单句按约 25ms/token 保守估算，**官方无吞吐数据，待实测**）+ 杂项 ≤1s（截屏/哈希/调度毫秒级），留出压测验证路径（tests/replay.py 分阶段计时）。

## 14. 参考架构图

```xml
<svg viewBox="0 0 680 560" width="100%" role="img">
<title>小鲸鱼桌宠线程模型</title>
<desc>单 Python 主进程内 Qt 主线程、观察线程、TTS 工作线程与共享存储的关系</desc>
<defs>
<marker id="ar" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
<path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
</marker>
</defs>
<rect x="20" y="16" width="640" height="528" rx="16" fill="#F1EFE8" stroke="#5F5E5A" stroke-width="0.5"/>
<text x="40" y="42" font-size="13" font-weight="500" fill="#444441">单个 Python 主进程（无额外 HTTP/WS 服务）</text>
<rect x="44" y="60" width="180" height="200" rx="10" fill="#E6F1FB" stroke="#185FA5" stroke-width="0.5"/>
<text x="134" y="82" text-anchor="middle" font-size="13" font-weight="500" fill="#0C447C">Qt 主线程</text>
<text x="134" y="102" text-anchor="middle" font-size="12" fill="#185FA5">PetWindow 置顶透明窗</text>
<text x="134" y="122" text-anchor="middle" font-size="12" fill="#185FA5">帧动画 · 输入框 · 托盘</text>
<text x="134" y="142" text-anchor="middle" font-size="12" fill="#185FA5">状态位 → 动画映射</text>
<text x="134" y="176" text-anchor="middle" font-size="12" fill="#185FA5">idle / think / work</text>
<text x="134" y="196" text-anchor="middle" font-size="12" fill="#185FA5">talk / nap / error</text>
<rect x="248" y="60" width="200" height="200" rx="10" fill="#E1F5EE" stroke="#0F6E56" stroke-width="0.5"/>
<text x="348" y="82" text-anchor="middle" font-size="13" font-weight="500" fill="#085041">观察线程 ObserveLoop</text>
<text x="348" y="102" text-anchor="middle" font-size="12" fill="#0F6E56">ScreenSource(mss) 截屏</text>
<text x="348" y="122" text-anchor="middle" font-size="12" fill="#0F6E56">FrameDiffer dHash</text>
<text x="348" y="142" text-anchor="middle" font-size="12" fill="#0F6E56">VisionJudge → DeepSeek API</text>
<text x="348" y="162" text-anchor="middle" font-size="12" fill="#0F6E56">ChatSession（会话时挂起观察）</text>
<rect x="472" y="60" width="164" height="200" rx="10" fill="#FAEEDA" stroke="#854F0B" stroke-width="0.5"/>
<text x="554" y="82" text-anchor="middle" font-size="13" font-weight="500" fill="#633806">TTS 工作线程</text>
<text x="554" y="102" text-anchor="middle" font-size="12" fill="#854F0B">Audio8Engine 合成</text>
<text x="554" y="122" text-anchor="middle" font-size="12" fill="#854F0B">(onnxruntime CPU)</text>
<text x="554" y="142" text-anchor="middle" font-size="12" fill="#854F0B">SpeechPlayer 播放队列</text>
<text x="554" y="162" text-anchor="middle" font-size="12" fill="#854F0B">stop_all 可即时打断</text>
<rect x="44" y="300" width="592" height="120" rx="10" fill="#FBEAF0" stroke="#993556" stroke-width="0.5"/>
<text x="340" y="326" text-anchor="middle" font-size="13" font-weight="500" fill="#72243E">SpeechGate（L 层确定性钩子 · 唯一放行点）</text>
<text x="340" y="348" text-anchor="middle" font-size="12" fill="#993556">主动：30s 间隔 · 每小时最多 10 次 · 日预算 300（保底 60 留给会话）· 全屏免打扰 · 标题黑名单</text>
<text x="340" y="368" text-anchor="middle" font-size="12" fill="#993556">被动：仅 2s 防连点（主人搭话必须回应）</text>
<rect x="44" y="452" width="592" height="72" rx="10" fill="#EEEDFE" stroke="#534AB7" stroke-width="0.5"/>
<text x="340" y="476" text-anchor="middle" font-size="13" font-weight="500" fill="#3C3489">RuntimeState（JSON 原子写） + DecisionLog（JSONL 只追加 · 单写者）</text>
<text x="340" y="498" text-anchor="middle" font-size="12" fill="#534AB7">容器 API 写入 · 模型不可直接篡改 · 不存原图</text>
<path d="M224 170 L244 170" fill="none" stroke="#5F5E5A" stroke-width="1.5" marker-end="url(#ar)"/>
<text x="234" y="160" text-anchor="middle" font-size="11" fill="#5F5E5A">信号</text>
<path d="M448 170 L468 170" fill="none" stroke="#5F5E5A" stroke-width="1.5" marker-end="url(#ar)"/>
<text x="458" y="160" text-anchor="middle" font-size="11" fill="#5F5E5A">utterance</text>
<path d="M134 260 L134 296" fill="none" stroke="#5F5E5A" stroke-width="1.5" marker-end="url(#ar)"/>
<text x="142" y="280" font-size="11" fill="#5F5E5A">用户输入</text>
<path d="M348 260 L348 296" fill="none" stroke="#5F5E5A" stroke-width="1.5" marker-end="url(#ar)"/>
<path d="M554 260 L554 296" fill="none" stroke="#5F5E5A" stroke-width="1.5" marker-end="url(#ar)"/>
<path d="M340 420 L340 448" fill="none" stroke="#5F5E5A" stroke-width="1.5" marker-end="url(#ar)"/>
</svg>
```

## 15. 遗留问题与未知区

1. **DeepSeek 视觉模型细节未核实**：`response_format` JSON 模式是否支持该实验模型未验证，兜底文本协议（首词 SPEAK/SILENT + 分隔符）；模型名与计费以官方文档实现时为准（官方已注明旧模型名转由 V4.1-Flash 服务并按 Flash 计费）；上下文缓存计费的存在性与折扣倍数同样待官方文档核实（前缀纪律不依赖折扣成立）。占位方案：配置里模型名可一行切换。
2. **Audio8 0.1B INT8 进程内推理 API 未对齐**：0.1B-ONNX-INT8 发布的 onnx_runtime 运行时的 Python 推理入口（张量名/会话装载/流式 PCM）需按仓库实现时核对；设计已按"读 runtime_manifest.json 装载、不硬编码张量名"留出适配层。占位方案：若只有 CLI 可用，退为"同版本 Python 模块 import 调用"；仍不行才接受子进程 CLI（违反单进程约束的降级，需用户知情）。
3. **Audio8 音质与速度待实测**：INT8 会改变输出，官方提示按语言/音色/部署环境单独评估——中文零样本克隆在可爱音色上的相似度需注册后用 10 句测试集人评；**合成速度官方无数据**，本设计的 ≤2s/句（80 字）按约 25ms/token 保守估算，待首轮压测实测回填，超预算则先砍字数上限再议端到端 8s。不满足时的替代：Audio8 0.6B-ONNX-INT4（约 400MB，质量优先，内存 +400MB 在预算内）。
4. **dHash 阈值 12 是初始猜测**：需 test_differ.py 在用户实际场景（看视频/滚动网页/静态阅读）标定；阈值随显示器内容分布漂移，周报观测静默率异常时重标。
5. **dsh-pet 素材许可边界**：代码 MIT，素材（动画/提示词/源视频）禁商用+二创署名。个人自用合规；将来开源分发必须随附 NOTICE（原作者 GitHub 地址）；若只想要规避许可风险，可用仓库自带提示词流水线自研同风格角色（二期选项）。
6. **会话输入通道形态**：一期定为"点击宠物弹文本框"（最轻、零额外模型）；语音输入需增 ASR（+数百 MB 内存），与"后台占用少"冲突，列为二期。若一期后期望语音输入，优先 SenseVoice 类小模型复核后再议。
7. **多显示器/DPI 混合缩放**：dsh-pet-indesktop 实测定性——Windows 11 + 150% DPI 曾有桌宠不可见/鼠标失效未结案 issue；本设计默认单主屏，多屏与混合缩放列为已知风险，启动时检测到非 100% DPI 给出警告日志。
8. **API 延迟无实测数据**：Flash 档短输出 ≤3s 为量级估计（无已登记来源含延迟数据），待首轮压测回填 p50/p95；若实测超 3s，重议端到端 6s 目标（上限 8s 不变），或观察模式换更短输出上限。

## 16. 独立评审记录

**评审执行**：2026-10-23，独立上下文的子智能体（委派评审包：本设计文档 v1 + 关键决策清单 + 知识库六文件 + 四份已登记外部来源），按 production-checklist 四要素（权衡成立性/案例对照贴切性/成本量级一致性/来源支撑性）对 §2 范式 P（5 项）、§5 模型组合与分派表（6 项）、§12 六层决策表（6 项）共 17 项关键决策抽样评审。

**评审结论**：通过 9 项、存疑 8 项、证据不足 3 项；无擅改用户已确认约束（三处张力仅标注：单进程 vs Audio8 子进程降级、搭话必须回应 vs 预算熔断、≤10 次/小时 vs 并发写）。

**存疑项回修记录**（8/8 已回修）：
1. 前缀重写触发点与 8 轮折叠矛盾 → 折叠列为第三触发点，失效账记"折叠点之后的 token"（§4.3/§5/§12/§13-8）。
2. 降级链"配额熔断→仅标题模式"不闭环 → 日预算预留会话保底 60 次（20%），观察循环余量 ≤60 提前挂起；熔断后会话用保底维持回应（§4.1/§5 降级链）。
3. "费用降一个量级"与预算表矛盾 → 改为"输入 token 降约三成（连带精简规则约五成）"（§5）。
4. "80%+ 轮次拦截"无依据且与静默率混用 → 改为"待标定预测"；V 层增 dHash 拦截率自动指标，与静默率分开测量（§3/§4.6）。
5. 观察档 token 加总 1314 超声明 1250 → 口径统一为观察 ≤1350、会话 ≤1700（§3/§5）。
6. 日 40 万 token 未按会话占比上界核算 → worst case 全会话 49.5 万、典型 40 万，熔断以次数/费用两条线为准（§5）。
7. TTS 延迟依据无来源 → 改为保守估算（80 字×约 25ms/token≈2s），官方无吞吐数据标注待实测，单句 ≤80 字（§1/§5/§13/§15-3）。
8. RuntimeState 并发写未串行化 → read-modify-write 收口单写者线程或容器 API 加锁，test_gate.py 补并发用例（§4.4）。
另回修评审附注项：会话历史预算包含关系明确为"摘要 ≤200 + 最近 8 轮 ≤400 合计 ≤600"（§4.3）；长会话挂起无界 → 单次会话上限 20 轮/10 分钟（§4.1）；跨模式缓存命中范围如实修正为"人设子段"（§4.3）。

**证据不足项处理**：① TTS 吞吐数据（~19ms/token）不在已登记来源 → 已降级为"待实测保守估算"（回修 7 同）；② DeepSeek 缓存计费未收录 → 文档自标"待官方文档核实"且决策不依赖折扣成立，依据强度如实降为待核实（§5/§13-8/§15-1）；③ API ≤3s 无实测 → 标注待首轮压测回填，列入未知区（§15-8）。

**复审**：一轮（本轮评审即回修后复审）——8 项存疑的回修均落实为文档内可指认的具体修改；无回修后仍存疑项。与用户已确认约束的三处张力未擅改，其中"搭话必须回应 vs 预算熔断"已用保底额度机制在默认假设内闭环，若用户要改熔断口径需重新拍板。

**声明**：本评审为关键决策抽样（17/17 项范围内部长决策），不等于全文正确性证明；未抽到的项靠可编码三门槛与六层自查兜底。
