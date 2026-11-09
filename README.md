# spark-review harness

一台最小 Agent：**在线模型当 assistant 负责干活，本地 Ollama 的 spark 当 reviewer 负责把关**，
代码硬规则与路径监狱排在模型前面。分层、类名、字段名沿用课程那套骨架（出处见文末「参考」）；
在此之上按本机实际情况和界面要求改了不少，理由不单独列清单——都写在离它最近的地方：对应小节、
源码注释和测试名字里，每一条都有实测数据或用例撑着。

```
前端              TUI: harness.bat（textual，一键进对话；Ctrl+P 里改设置）  CLI: main.py run / sessions / show / serve / doctor / tui
                  WebApi: main.py serve（GET / 有最简 webui）——三个前端只调用 AgentService 的三个方法
应用层 app/       AgentService.chat(session_id, user_message, on_event=None)
    │   组织 Context → model_client.generate → 审核 tool_call → 执行 → 回填 → 循环（≤ max_iterations，撞满强制收尾）
    │   端口：ModelClient / SessionStore（+ ModelSettings{model_id, effort, thinking} / Effort）——接口说"应用需要什么"，实现在 adapters/
    │   审核链：guard(23 条硬规则 + 路径监狱) → reviewer(模型，不带思维链) → verdicts(按对话段的判决缓存)，拒绝的理由也回给模型
    │   依赖倒置：这一层不 import openai / subprocess / pathlib / adapters，测试只塞假客户端（有纪律测试）
领域层 domain/    Session · UserMessage(含 segment)/AssistantMessage(含 usage)/ToolResult · ToolCall · Message · Tool
                  Context(泛型 output_type) · Assistant · ToolCallReviewer · Decision
适配层 adapters/  wire(线格式) · models/OpenAIClient(+Anthropic/Google 空位) · stores/Jsonl(+ session_slug)
                  tools/Shell+Python(+windows 共享细节) · api/WebApi · cli · tui · doctor · locks(工作区互斥)
组装根            main.py：全系统唯一知道所有具体类的地方（端点表 + build + rig）；指令文案 prompts.py
```

## 文件分布（行数为实测）

| 目录/文件 | 层 | 行数 | 放什么 |
|---|---|---|---|
| `domain/session.py` | 领域 | 100 | `Session`（含 `current_segment` / `goal`＝最近一次任务） · `UserMessage`（带 `segment`） · `AssistantMessage`（带 `prompt/completion/cached_tokens`） · `ToolResult` · `ToolCall` · `Message` 别名 · `new_id` · `json_dict` |
| `domain/context.py` | 领域 | 28 | `Context(Generic[T])`：`instruction / tools / messages / output_type` |
| `domain/tool.py` | 领域 | 21 | `Tool`（Protocol）：`name / description / parameters / execute` |
| `domain/roles.py` | 领域 | 59 | `Assistant`（`context(session, with_tools=…)`，收尾时不带工具） · `ToolCallReviewer` · `Decision`（这三个角色划在领域层） |
| `app/ports.py` | 应用 | 59 | `ModelClient` · `SessionStore` · `ModelSettings`（`model_id / effort / thinking`） · `Effort` |
| `app/policy.py` | 应用 | 116 | `DENY_PATTERNS`(23) · `scan_text` · `outside` / `outside_write`（**路径监狱**） · `hard_deny` · `no_guard` · `audit` · `REVIEW_MODES` |
| `app/service.py` | 应用 | 189 | `AgentService`：`new_session / list_sessions / chat(…, on_event)` + 审核链（分段缓存、失败重试与保守拒绝）+ 撞满步数强制收尾 + 同一越界写连拒 3 次就停手 |
| `adapters/wire.py` | 适配 | 71 | 线格式：`to_wire` · `from_wire` · `message_row` · `session_row` · `request_message` · `STORE_ONLY_KEYS`（8 个私有键） |
| `adapters/models/openai_client.py` | 适配 | 93 | `OpenAIClient(ModelClient)` · `EFFORT_TEMPERATURE` · `payload`（`thinking=False` → `reasoning_effort:none`） · `parse`（收 `usage`） · `token_counts` · `parse_decision` |
| `adapters/models/anthropic_client.py` | 适配 | 26 | `AnthropicClient(ModelClient)` 空位：`generate()` 老实抛 `NotImplementedError` |
| `adapters/models/google_client.py` | 适配 | 25 | `GoogleClient(ModelClient)` 空位：同上 |
| `adapters/models/no_review.py` | 适配 | 25 | `NoReviewClient(ModelClient)`：`--review off` 的替身，零网络、永远放行，审计文本写明"没人审" |
| `adapters/stores/jsonl.py` | 适配 | 55 | `JsonlSessionStore(SessionStore)`：Session <-> 一行行 json；`session_slug(workspace)` 决定"一工作区一条轨迹"的文件名 |
| `adapters/locks.py` | 适配 | 141 | `WorkspaceLock` / `WorkspaceLocked` / `process_state`：`runs/<slug>.lock` 互斥。判活看 `GetExitCodeProcess` 是不是 `STILL_ACTIVE`（**开得到句柄不等于在跑**），看不见（`gle==5`）就不抢，读不出内容按 mtime 宽限 30 分钟 |
| `adapters/tools/windows.py` | 适配 | 123 | `PS_PREFIX` · `_strip_clixml` · `conda_env` · `conda_python` · `SubprocessTool`（执行细节；超时先杀树，再给管道 5 s 收尸上限） |
| `adapters/tools/shell.py` | 适配 | 25 | `ShellTool(SubprocessTool, Tool)`：PowerShell |
| `adapters/tools/python.py` | 适配 | 34 | `PythonTool(SubprocessTool, Tool)`：钉死的解释器跑代码片段 |
| `adapters/api/web.py` | 适配 | 91 | `WebApi` + 内联 `PAGE`：3 个路由 |
| `adapters/cli.py` | 适配 | 236 | `Ports`（注入的零件）+ `build_app` + 6 个命令（`run/sessions/show/serve/doctor/tui`）+ `render` + 审核开关；`run` 现在是实时的（`on_event`）并抢工作区锁；`tui` 把档位/步数/闸门交给界面，换工作区或界面里换的档位要落地就重建 service 再开一次 |
| `adapters/tui.py` | 适配 | 1087 | `HarnessApp`（textual）：**功能都在 `Ctrl+P` 面板里**（设置 / 视图 / 跳转 / 文件 / 帮助五类，标题写的就是此刻的真值；面板里**只有这几类中文条目**，textual 白送的 `Theme/Quit/Keys/Maximize/Screenshot` 一律不要），屏幕外面只剩两条状态行（账 + 态）。闸门运行时真能开关（关要确认两次）、步数一次输入到位、轨迹有原文/对话两种只读屏、同工作区切段靠"跳转 · 历史会话"、身份行放不下靠悬停滚动。**只吃鸭子类型的 service**，不 import 组装根，所以 59 个无头用例零网络 |
| `adapters/doctor.py` | 适配 | 142 | `Rig`（注入的体检零件）+ `run_checks` **十项**（多了一项「终端宿主与鼠标可用性」） |
| `main.py` | 组装根 | 127 | `ENDPOINTS` · `endpoint()` · `key_of()` · `reviewer_parts()`（三档都 `thinking=False`，本地 8 s / 在线 20 s 超时） · `build()`（`partial` 把工作目录绑进闸门、算出轨迹名） · `rig()` · `app = build_app(...)` |
| `prompts.py` | 素材 | 60 | `ASSISTANT` / `REVIEWER` 两段指令（`string.Template`，`$workspace` / `$python`）；reviewer 判据已缩到两条（不再判"跑题"） |
| `tests/` | 测试 | 2901 | `conftest.py`(124) + `test_domain.py`(134) + `test_app.py`(413) + `test_adapters.py`(532) + `test_main.py`(334) + **`test_tui.py`(1364，59 个无头界面用例)**，**213 个常跑用例** + 1 个要 `HARNESS_LIVE=1` 才连真 spark |
| `quests/` | 实验 | — | `01_env_report` / `02_pelican_bicycle`（任务 + 判分）+ **`03_review_latency`（审核延迟归因与判决一致性对照，`python check.py` 复现）** |
| `docs/spark-harness-design.md` | 依据 | 228 | spark 能力实测数据：temp、`num_ctx` 造成的 32K 墙、审核链该放哪；开头新增了实验 03 的三条修正 |
| `harness.bat` | 入口 | 47 | 一键进 TUI：父窗口只负责起界面窗口然后立刻退场（关 bat 窗口 ≠ 退界面）。首选 `wt.exe -w new`（新开一扇 WT 窗，鼠标与悬停在那儿才有效；`-N` / `-n` / `--new-window` 在 1.24.12741.0 上实测**被拒**，命令压根不跑），找不到 wt 就回落 conhost（只有键盘，状态行会写明）。界面非 0 退出时那个窗口停住并打出 `runs/tui-last.log` + `runs/tui-error.log`。**纯 ASCII + 整份 CRLF**：cmd 用 OEM 码页按字节偏移回读批处理，中文注释或裸 LF 都会让它从半行开始执行（原因写在下面「一键（推荐）：`harness.bat`」那一节） |
| `screenshots/` | 素材 | 284 + `_tools/` | 19 张真实渲染的界面图（17 张现版 + 06/07 两张真模型、改版前底栏，留着做对照）：`_tools/shot_v2.py` 驱动 `adapters/tui.py` 真跑一遍再 `export_screenshot` 导 SVG，`rsvg-convert` 转 PNG；`README.md` 逐张附了那一屏的**逐字文本**（手机上看图不方便就读文本） |
| `ruff.toml` / `requirements.txt` / `.gitignore` | 规范 | 9 / 10 / 45 | lint（F/E/W/I/UP，120 列）、依赖（含 textual）、挡掉凭据/运行产物/开发期草稿（`summary.md`）/编辑器与 Office 留下的碎文件 |
| `.env.example` | 配置 | 26 | 空值模板：该填的环境变量名全列在里（值一律留空，注释写了默认值）。复制成 `.env` 再填 key |
| `.env` | 配置 | — | 凭据槽：`DASHSCOPE_API_KEY=`（已填，116 字符，本 README 不复述其值）。含密钥：不要提交 git，也不要 `cat` 进聊天记录 |

运行代码合计 2933 行（`domain/` 208 + `app/` 364 + `adapters/` 2174 + 根 `main.py`+`prompts.py` 187），
测试 2901 行。界面占了适配层将近一半（`tui.py` 1087），因为它的规矩多（焦点、面板、二次确认、只读屏），
而每一条都在源码注释里写了"为什么长这样"和"踩过什么坑"。
所有 `__init__.py` 留空，import 一律写深路径（`from domain.session import Session`）：
每个名字都能直接翻到定义，读代码不用猜。

`skill/` 与本项目无依赖关系，也不在重构范围内：`skill/bilibili-reader/`、`skill/deleiqina-tts/`。

### 运行产物（都可删，重跑会自动重建）

| 路径 | 是什么 |
|---|---|
| `sessions/<工作区slug>.jsonl` | 轨迹。**一工作区一条文件**（`<目录名>-<路径哈希>`），旧格式的 uuid 文件照样能读 |
| `runs/doctor.json` | 最近一次体检结果（脱敏，不含 key） |
| `runs/<工作区slug>.lock` | 工作区互斥锁（内容：pid、时间、命令行）。进程正常退出就删掉；崩溃留下的死锁由下一个进程判活回收 |
| `runs/tui-error.log` | `harness.bat` 启动失败时打印的日志 |
| `quests/<名字>/` | `-w` 指定的工作目录，`run` 时自动 mkdir；里面的文件是模型写的产物 |
| `quests/*/.harness_tmp/` | `python` 工具落地的临时脚本 |
| `_tmp/tests-<pid>-<时间戳>/` | 测试用例的工作目录（见「测试为什么自己管临时目录」） |


## 一次 chat 的时间线

```
chat(session_id, "创建 todo.md …", on_event=None)
  ├─ commit UserMessage（自带 segment：一次 chat 就是一次对话）      # 每条消息产生的当下就打 ts 并落盘，崩了也留完整现场
  └─ 循环 ≤ max_iterations：
       Context ← assistant.context(session)    # instruction + tools + 全量历史，output_type=AssistantMessage
       AssistantMessage ← model_client.generate(Context, assistant_model_settings)   # usage 的三个 token 数留在消息上
       commit → 对每个 ToolCall：
             guard(call)：23 条硬正则命中？    → Decision(False, "命中硬规则 …")        source=policy（不劳模型）
                        路径监狱判到越界写？   → Decision(False, "…在工作目录之外…")   source=policy
             verdicts 里同一段落有同样的请求？ → 复用旧判决                              source=缓存里那个
             否则 → reviewer_client.generate(...)（reasoning_effort=none，8 s/20 s 超时，失败重试一次）
             拒绝 → ToolResult("denied by …: 理由")，不执行                              （理由必须回给模型）
             放行 → tool.execute(arguments) → ToolResult({"review": …, "output": …}, review_ms=…)
             同一请求在本次对话第 3 次出现 → output 里追加一句"别再重复"（不参与判决）
       没有 tool_calls → break
  撞满步数 → assistant.context(session, with_tools=False) 收尾一问（只许说话），轨迹不以工具结果结尾
   同一条越界写被硬规则连拒 3 次（或一轮 6 次）→ 不再问模型，直接落一条「已停止这一步…用 `main.py tui -w <目录>` 换过去」
```


## 审核开关（`--review` / `--no-guard`）

```powershell
# 默认：本地 spark 审（免费、不出网，但慢且抖动大）
python main.py run "..." -w ... --review local

# 审核也交给在线端点（同一把 key，模型默认与 assistant 相同，温度压到 LOW 求确定性）
python main.py run "..." -w ... --review online

# 不审：只留代码闸门（23 条硬规则 + 路径监狱）
python main.py run "..." -w ... --review off

# 连硬规则也摘掉（只用于"完全无审核"的对照实验，启动时会打警告）
python main.py run "..." -w ... --review off --no-guard
```

`serve` 同样接受 `--review`。三个档位定义在 `app/policy.py:REVIEW_MODES`（所以不在某个前端里写死），
`main.py:reviewer_parts()` 把档位翻成"用哪个客户端 + 哪个模型设置"，`off` 用
`adapters/models/no_review.py:NoReviewClient` 顶替——它不发请求、永远放行，但审计文本写的是
"审核已关闭（--review off）"，事后仍能从轨迹里区分"没人审"和"审过了没问题"。

实测（同一台机器、同一个"写 note.txt 再读回"任务，各跑一遍；**这三行是审核请求还带思维链时的老口径**）：

| 审核档位 | 单次判决耗时 | 整轮 | 轨迹 |
|---|---|---|---|
| `local`（spark 4B @ 本机 Ollama） | 2.4–42.8 s，抖动大 | 169 s，10 次判决合计 101.5 s（60%） | `9982c547a779` |
| `online`（qwen3.8-flash @ DashScope） | 3.38–4.63 s | 41.9 s，6 次判决合计 22.1 s（53%） | `cb73977da629` |
| `off` | **0.2 ms**（零请求） | 11.7 s | `2ae21198bc3f` |


## 实测数据

都在重构后的这台机器上重跑，不是引用的二手结论。

| 现象 | 数据 | 后果 |
|---|---|---|
| 真实端到端 | `sessions/d63cbc270b30.jsonl`：16 条消息（1 user / 8 assistant / 7 tool）、**92 s**、7 次工具请求全部经 spark 放行 | 模型自己发现 PowerShell 按 GBK 读回乱码，改用带 BOM 的 UTF-8 重写并复核字节——审核链与应用链都在环内工作 |
| 审核耗时（现在直接写进轨迹） | 7 次判决 `review_ms` = 4217 / 9372 / 2100 / 6956 / 8117 / 7180 / 11371 ms，合计 **49.4 s ≈ 整轮的 54%** | "时间花在哪一步"不再需要手测脚本；步数上限 `-s` 要克制 |
| spark 判定会抖动 | 同一句 `Get-Date`（temp=0）docs 里记录过先 `allow` 后 `deny`；今天的 doctor 探针判它"与写文件任务无关"→ 拒绝 | 会话内 `verdicts` 缓存：同一请求只审一次 |
| spark 会看任务判跑题 | 目标是"创建 todo.md"时，`Get-ChildItem` 被判"只读、与任务相关"放行；doctor 里目标换成写文件、命令是 `Get-Date` → 拒绝 | 审核上下文必须带任务目标 + 工作目录（`ToolCallReviewer.context` + `prompts.REVIEWER`） |
| PowerShell 5.1 错误流 | 重定向时输出 CLIXML + `_x005F_` 转义 | `_strip_clixml()` 还原成可读文本且保留退出码（包 `2>&1 \| Out-String` 会吞退出码，已回滚） |
| GBK 控制台 | 模型输出含 ✅ 时 `print` 抛 `UnicodeEncodeError`；`conda run`（4.12，捕获后转印）同样崩 | `cli.py` 起手 `errors="replace"`；调用统一用 `conda run --no-capture-output` 或直接解释器 |
| 上下文预算 | 工具输出截断到 8000 字符。**理由改了**：32K 那道墙是 spark 自己的 `num_ctx`（docs 实测 >32K 才 400），而审核上下文只有"任务目标 + 待审请求"两行、实测最长 3272 token，离墙很远；assistant 侧实测 750,061 prompt token 也不报错 | 这个数字实际是**控成本与控噪声**（一次工具输出顶几千 token 的日志会把上下文冲淡），不是防 400；数字未动，`test_long_output_is_truncated_for_the_context_wall` 仍锁住行为 |
| 思维链是隐藏成本 | `usage.completion_tokens` 里 60–98% 是 `reasoning`；同一条 `Get-Date` 一次 2.2 s、一次 40.2 s | 审核请求 `thinking=False`；`usage` 三个数从此落进轨迹，看得见才量得清 |
| 端到端复跑（旧轨迹原任务、同档位） | 审核 10 次合计 101.5 s / 中位 5.62 s / 最大 42.8 s → 15 次合计 **15.4 s / 中位 1.04 s / 最大 1.38 s** | **整轮 169 s → 179 s 并没有变快**（这次 assistant 多走了 5 步）；变的是审核占比 60% → 8.6%，主要矛盾移到了 assistant 侧 |
| 依赖检查 | `anthropic` / `google-genai` 未装、三个 key 都不在环境里、Ollama 无 Anthropic 兼容端点（404） | 那两个适配器只能留空位 |

体检与测试：`main.py doctor` **十项全绿**（python / powershell / git / python_pinned / tool_execution /
hard_deny / console_host / reviewer_ollama / reviewer_verdict / assistant_online，机器可读结果在
`runs/doctor.json`）；`pytest tests` **213 passed, 1 skipped**（那个 skipped 是要 `HARNESS_LIVE=1` 才连真
spark 的 live 用例；断网时 7 项体检与绝大多数用例照样能跑，`test_doctor_reports_ten_independent_checks`
就是拿打不开的端点验的）；`ruff check .` 全绿。
旧轨迹兼容：`test_store_reads_trajectories_written_by_the_previous_shape` 自己造一份上一版 shape 的 jsonl 读回来验（缺 `segment` 就标「未绑定」），本机真有历史轨迹时顺带每条读一遍——不依赖只存在我这台机器上的文件。

## 用法

### 第一次跑（新克隆的仓库）

`sessions/`、`runs/`、`quests/`、`_tmp/` 都不进版本库（口径见 `.gitignore`），所以刚克隆下来这些目录是空的
——这是设计，不是丢了。第一次按这个顺序：

```powershell
Copy-Item .env.example .env      # 填 DASHSCOPE_API_KEY；不填也能跑，见下面第二条
conda run --no-capture-output -n llm_learn python -m pip install -r requirements.txt
conda run --no-capture-output -n llm_learn python main.py doctor     # 十项体检，机器可读结果落 runs/doctor.json
```

- **没有在线 key 也能整链跑通**：把 assistant 也指到本机 Ollama（`HARNESS_ASSISTANT_URL=http://127.0.0.1:11434/v1`
  加 `HARNESS_ASSISTANT_MODEL=<本机模型名>`，代码一行不改）。这时 `doctor` 如实报
  `FAIL assistant_online   DASHSCOPE_API_KEY 未设置（写进 .env）`，其余九项照样绿——它不装成"全都能跑"。
- `python main.py sessions` 在没有轨迹时会打一句「还没有轨迹：…是空的」并说清怎么让它长出来，
  不再一声不响（界面空屏上那句提示正是让人敲这条命令）。
- 进界面：双击 `harness.bat`，或 `python main.py tui -w <工作目录>`。

### 一键（推荐）：`harness.bat`

```powershell
E:\harness\harness.bat            # 直接进 TUI：选工作区 → 打字 → 看实时审核与折叠的过程
E:\harness\harness.bat E:\harness\quests\t04     # 带工作区进
```

不 activate conda、不 cd、不敲 `python main.py ...`：批处理自己 `cd /d "%~dp0"` 并用死路径
`C:\conda\envs\llm_learn\python.exe`。**它是两扇窗**：父窗口只负责起界面窗口就立刻退场（否则
"关掉 bat 窗口"和"退出界面"是同一件事，现场一起没了）；界面窗口异常退出时会停住，把 `runs\tui-last.log`
（每次退出都写的摘要：工作区、会话、档位、闸门、步数与账——读的是**当时的真值**，不是启动时那份）和
`runs\tui-error.log`（traceback）打在屏上再 pause。

起窗口优先走 **Windows Terminal**：`wt.exe -w new cmd /c ...` 强制新开一扇 WT 窗（不加 `-w new` 会开成
已有窗口的标签页）。**参数只能照本机 wt 实测**：1.24.12741.0 上 `-N` / `-n` / `--new-window` 一律被拒——
wt 起来、抱怨一句、退出，你给的命令压根不跑，双击看过去就是"什么都没发生"（这条踩过）。
理由是鼠标：只有 WT 认 textual 发的 DECSET 1000/1003/1006，conhost 压根不往输入流写鼠标字节
（详见「已知边界」）。`where wt.exe` 找不到就回落 `start "spark harness" cmd /c ...`——同样的界面、只有
键盘，界面本身不提示这件事（状态行那格留给档位/闸门/上下文）——想知道有没有鼠标就跑 `doctor` 第十项。本机装了 wt（1.24.12741.0），但
`HKCU:\Console\%%Startup` 没有 Delegation 值，系统默认宿主仍是 conhost，所以批处理自己指定宿主。

批处理正文**只能 ASCII、行尾必须整份 CRLF**：cmd 用 OEM 代码页按字节偏移回读脚本，中文注释会变成命令
（实测 `'ui' is not recognized`），裸 LF 再碰上 `chcp` 会让偏移错位、从一行的中间开始执行（实测报
`'the' is not recognized` / `'ast.log' is not recognized`），两种症状看着都是"双击就闪退"。还有个老坑：
调子批处理不加 `call` 会接管并终止父脚本，异常分支于是永远走不到。三条现在由
`test_entry_bat_is_ascii_only_crlf_and_forwards_args` 在字节层面钉住；`--child` 分支的参数拼接手验过
（`main.py tui -w quests\t05`）；"真起一扇 WT 窗"这条现在也能在沙箱里验——`wt.exe -w new cmd /c 记录脚本`
确实开了窗并跑了命令（先前那句"沙箱里起不来"是错的，crashpad 的 `CreateFile 拒绝访问` 只是它的崩溃上报器没权限，不影响开窗）。只有**鼠标事件到底落不落地**还得你的双击算数。

### 界面里就这几件事

| 想做的 | 怎么做 |
|---|---|
| 说话 | 直接打字，`Enter` 发送（输入框在状态行上方，鼠标在 WT 里可以直接点） |
| 改设置 / 换工作区 / 看轨迹 / 折叠 | `Ctrl+P` 开面板：打字过滤，`Enter` 执行；改值类执行完**自动回到面板**，接着改下一个 |
| 键盘微调 | `Tab` 把焦点挪到状态行——**挪到了看得见**（背景变、粗体下划线、行首多一个 `▸`）；然后 `+ -` 档位、`[ ]` 步数、`h` 闸门（关要按两次，开一下就行） |
| 写多行任务 | 输入框是多行的：粘贴整段代码或长提示词**一个字不丢**；`Shift+Enter` 换行（终端分不出就 `Ctrl+J`），`Enter` 发送；框随内容长到五行封顶，发完缩回一行 |
| 折叠 / 展开 | 一轮里的**每次思考**各是一个小折叠块（`▶ 思考 2 · shell · 拒`），下一次思考一开始就折上；**最终回答不折**，`Ctrl+B` 收/摊整轮，点标题收/摊单块。切工作区读回来的轨迹只摊开最近一段，更早的收成一行标题 |
| 看键位 | `Ctrl+P` → `帮助 · 键位清单`：单独一屏，`Esc` 返回对话，再执行一次这条就是关掉它 |
| 屏幕外的状态 | 身份行（工作区 · 会话 · 对话段，一个字不压缩，放不下就悬停滚动）+ 两条状态行：`本轮 3/12 · 累计 15 步 · 本轮 4.2 s · tokens 6,680 (5,850+830)` 与 `档位 local · 闸门 开 · 上下文 1.2% · Ctrl+P 命令与设置 · Ctrl+Q 退出` |
| 退出 | `Ctrl+Q`。 |

面板条目长这样（标题写的就是此刻的真值，`设置 · 步数上限 12` 而不是"改步数"）：

```
🔎输入要执行的命令
设置 · 闸门 开                     硬规则 + 路径监狱，当场生效。关要按两次，本段已判过的沿用旧判决
设置 · 审核模式 local              local 本地模型审，串行、免费但别的窗口会排队 · online 在线模型审，花 API 额度 …
设置 · 步数上限 12                 一次输入到位，范围 1 到 40，下一个请求生效
视图 · 折叠全部 / 视图 · 展开全部
跳转 · 换工作区 / 跳转 · 历史会话 2 段 /（有待生效档位时）跳转 · 重启界面 · 应用档位 online
文件 · 查看轨迹对话 / 查看轨迹原文 / 打开 sessions 目录
帮助 · 键位清单                    开一屏中文键位清单，Esc 返回对话；再执行一次这条就把那屏关掉
```


### 命令行（固定 conda llm_learn，或直接用上面的解释器路径）

```powershell
# 0) 依赖（可选，env 里已装好；textual 是 TUI 要的）
conda run --no-capture-output -n llm_learn python -m pip install -r requirements.txt

# 1) 体检：十项彼此独立，没有在线 key 时本地 reviewer 与审核链照样验完
conda run --no-capture-output -n llm_learn python main.py doctor

# 2) 在线 assistant（key 在 .env 里）
conda run --no-capture-output -n llm_learn python main.py run "在工作目录创建 todo.md，写三行待办" -w E:\harness\quests\t04

# 3) 没 key 也能整链跑通：assistant 也指到本地 Ollama（只动环境变量，代码一行不改）
$env:HARNESS_ASSISTANT_URL='http://127.0.0.1:11434/v1'
$env:HARNESS_ASSISTANT_MODEL='spark-x2.6-4b-abliterated:q6'
conda run --no-capture-output -n llm_learn python main.py run "..." -w ...

# 3b) 审核档位：--review local（默认）/ online / off；--no-guard 把代码闸门也关（对照实验用）
conda run --no-capture-output -n llm_learn python main.py run "..." -w ... --review online
conda run --no-capture-output -n llm_learn python main.py run "..." -w ... --review off

# 4) 轨迹：列表 / 回放（带每步审核耗时）/ 续聊
conda run --no-capture-output -n llm_learn python main.py sessions
conda run --no-capture-output -n llm_learn python main.py show t04-1a2b3c4d
conda run --no-capture-output -n llm_learn python main.py run "把刚才的文件再读一遍" -r t04-1a2b3c4d -w ...

# 4b) 同一个工作目录不写 `-r` 也会接上自己的旧轨迹：文件名就是工作区算出来的（一工作区一条）
conda run --no-capture-output -n llm_learn python main.py run "继续" -w E:\harness\quests\t04

# 5) 前端：浏览器打开 http://127.0.0.1:8000（GET / 是最简 webui）；TUI 走 main.py tui
conda run --no-capture-output -n llm_learn python main.py serve --port 8000
conda run --no-capture-output -n llm_learn python main.py tui

# 6) 测试与 lint
conda run --no-capture-output -n llm_learn python -m pytest tests -q
conda run --no-capture-output -n llm_learn python -m ruff check .
```

`conda run` 必须带 `--no-capture-output`：conda 4.12 会捕获子进程输出再用它自己的 GBK 流转印，
模型一回 ✅ 就 `UnicodeEncodeError`，任务其实已经跑完却看着像崩了。嫌啰嗦就直接用环境里的解释器：
`C:\conda\envs\llm_learn\python.exe main.py doctor`。

环境变量：`HARNESS_ASSISTANT_URL/MODEL`、`HARNESS_REVIEWER_URL/MODEL`、`HARNESS_CONDA_ENV`、`HARNESS_PYTHON`、
`HARNESS_CONTEXT_BUDGET`（TUI 成本账的分数分母，默认 128000 token，是假设值不是端点规格）、
`DASHSCOPE_API_KEY`、`HARNESS_LIVE=1`（跑连真 spark 的那一个用例）。
命令：`tui`（进界面）、`run`、`sessions`、`show`、`serve`、`doctor`。
选项：`-w` 工作目录、`-s` 循环上限（默认 12）、`-m/--assistant-model`、
`--reviewer-model`、`--review local|online|off`、`--no-guard`、`-r/--resume` 续聊、`serve --host/--port`。

## 轨迹格式（`sessions/<工作区>.jsonl`）

第 1 行 `{"kind":"session","id":…}`，之后每条消息一行：
`{"kind":"message","type":"UserMessage|AssistantMessage|ToolResult", …}`，行体就是 `to_wire()` 的形状
（`role` / `content` / `tool_calls` / `tool_call_id`）+ 观测字段——jsonl 与 chat/completions 故意共用
一个线格式，换模型协议不动存储；发给模型前 `request_message()` 把 `kind/type/ts/review_ms/segment/
prompt_tokens/completion_tokens/cached_tokens` 全部剥掉（多一个键就多改一次发给模型的字节）。

```json
{"review": "allowed by reviewer: 只读列出工作目录内容，不破坏数据",
 "output": "{\"returncode\": 0, \"output\": \"…\"}"}     ← ToolResult.content
```

`ToolResult` 行带 `review_ms`（本次判决耗时，`show` 里显示成 `[9372 ms 审核]`；**接近 0 就说明这一条
没劳驾模型**——命中缓存、命中闸门，或 `--review off`）；`AssistantMessage` 行带三个 token 数（成本账）；
`UserMessage` 行带 `segment`（同一条轨迹里第几次对话，界面按它折叠）。
被拦时 `review` 是 `"denied by policy: 命中硬规则 …"` / `"denied by policy: …在工作目录之外…"` /
`"denied by reviewer: …"`，assistant 下一轮就读到这条理由并改方案。

## Python 固定 conda llm_learn

`adapters/tools/windows.py:conda_python()` 依次尝试：`HARNESS_PYTHON` 覆盖 → 当前进程 →
`~/.conda/environments.txt` → conda CLI（查找结果按环境名缓存，环境变量每次重读）。`PythonTool` 直接用它；
`ShellTool` 把该环境目录顶在子进程 PATH 最前，所以模型在 shell 里敲 `python` 也只能拿到同一个解释器
（`test_python_is_pinned_to_the_conda_interpreter` 锁住这条）。

## 测试为什么自己管临时目录

`tests/conftest.py` 的 `workdir` 夹具不用 pytest 自带的 `tmp_path`：本机沙箱会给"用过一轮"的临时目录
补上 deny 的 DACL，`tmp_path` 收尾的 `listdir`/`rmtree` 拿到 WinError 5，**整套用例会以 ERROR 收场而不是
真的失败**（实测：把 `--basetemp` 指到系统 TEMP 或一个 PowerShell 建过的目录都会复现）。改成每次运行
换一个全新目录 `_tmp/tests-<pid>-<时间戳>`，实测刚被 PowerShell 子进程当过 cwd 也删得掉。

另外三组用例是"分层纪律"：`test_domain` 与 `test_app` 各自扫源码，确认领域层与应用层没 import
外部世界的库；`test_main` 扫全部三层，确认没有一层反过来 import 组装根；`test_adapters` 的
`test_adapters_declare_every_port_method_themselves` 逐个端口点名测实现——这是显式写
`class JsonlSessionStore(SessionStore)` 换来的那半条保险（Protocol 的方法体是 `...`，漏写会静默返回 None）。

## Bronze 目标对照（myagent-skeleton README）

| 目标 | 状态 |
|---|---|
| 1. 跑通 `doctor` | ✅ 十项全绿 |
| 2. agent 调用一个真实工具完成小任务 | ✅ `d63cbc270b30`：在线 qwen3.8-flash + 本地 spark 审核，7 次工具调用全部经放行并留下理由与耗时 |
| 3. 留下一条 `.jsonl` 轨迹 | ✅ `sessions/<工作区slug>.jsonl`（一工作区一条，多段对话靠 `segment` 分开）+ `sessions` / `show` / TUI 回放，旧 uuid 轨迹兼容（有测试） |
| 4. 报告第八节 3–5 条改进建议 | 交作业那份报告不在仓库里；在这儿能复核的是「实测数据」那张表，5 条结论都挂在它身上 |

## 已知边界

- **审核器现在只看两件事**：不可逆破坏、越界写。它仍然只拿"任务目标 + 待审命令"两行、不带历史，
  但"跑题"这条判据已经删掉（实验 03：关思考后它会把 `New-Item draft.md` 这种正常中间步骤判成跑题而误拦）。
  判决缓存按对话段隔离，旧批条不再跨任务生效。
- **路径监狱只管写**。`Set-Content C:\Windows\...`、`..\out.txt`、`open(..., 'w')` 到目录外，代码直接拒；
  但**读**工作目录之外不拦（`Get-Content C:\Users\...`）——这是故意的：读旧轨迹、读环境里的文件都是
  正当需求，而"越界读"用代码判会误伤一大片。要靠的是 assistant 指令里的"Only touch files under it"
  与模型自己的判断，不是闸门。拒绝理由里现在写清了工作目录到底是哪个；同一条理由连拒 3 次（或一轮 6 次）
  这一轮就停手，不陪模型烧 token；界面向导这条路还会在发问前先提醒一句。
- `session_slug()` 只归一大小写 / `..` / 8.3 短名。**映射盘符与 UNC 互为别名时仍会裂成两条轨迹**
  （`Z:\p\q` 与 `\\host\share\p\q`）——没有查注册表重建映射表，已知残留。
- 本地 Ollama 只有一个实例：TUI 开两个窗口、或 `serve` 并发，审核请求实际都在排队
  （这条现在写在面板 `设置 · 审核模式` 的说明里，不占常驻状态行）。工作区级别的并发写入另有
  `WorkspaceLock` 挡（`save()` 是整体重写，两个进程写同一个文件会互相覆盖，不是顺序问题）。
- **界面上鼠标只在 Windows Terminal 里可用，这是宿主的能力边界，不是本仓库的开关**。textual 发的是
  DECSET 1000/1003/1006（`drivers/linux.py` 那套 SGR 序列在 Windows 上不走），而 Windows 底下：
  conhost 的 `SetConsoleMode` 文档明写鼠标事件"由 ReadFile/ReadConsole 丢弃"、它的 VT 输入序列清单里
  没有鼠标输入条目、microsoft/terminal#15296（conhost 转发鼠标）至今是 Backlog；textual 8.2.8 的
  `drivers/win32.py:179` 设置输入模式是**整体替换**（从不 OR 上 `ENABLE_MOUSE_INPUT`），读循环
  （`win32.py:265-282`）只处理 `KEY_EVENT 0x0001` 与 `WINDOW_BUFFER_SIZE_EVENT 0x0004`，
  `MOUSE_EVENT 0x0002` 静默丢掉——虽然 `MOUSE_EVENT_RECORD` 结构体在 `win32.py:81-89` 已经定义好、
  也接进了 union（`:115`）。结论：conhost 下不是"没接好"，是**这条路没有输入**。所以只做两件事：
  `harness.bat` 优先用 `wt.exe -w new` 起窗、`doctor` 报宿主与鼠标可用性；不 patch site-packages、
  不改系统注册表（改默认宿主是全局副作用，值得你自己决定，不由一个实验仓库替你动）。
  判宿主用 `WT_SESSION` 环境变量（WT 注入，conhost 下没有），比读 `Console\%%Startup` 便宜且不越界。
- **界面里闸门是真开关**（`app/service.py:119` 每次工具调用现读 `guard`），**审核档位不是**：审核客户端在
  `AgentService` 构造时就定死了，界面换档只记新值并挂"待生效"，落地走面板「跳转 · 重启界面」或换工作区
  （两条都由 `cli.py` 重建 service）。同一段里已缓存的判决也不跟着闸门翻脸——本段判过的沿用旧判决，
  新对话段起全部重判，这条在关闸门的提示里写明。
- 上下文占用百分比的分母是**假设值**：`HARNESS_CONTEXT_BUDGET`（默认 128000 token）。真实上限探不到
  （实测 assistant 吃到 750,061 prompt token 仍不报错），所以这个数字用来给"这一轮烧了多少"做刻度，
  不要当成端点规格。
- `thinking=False` 的安全性是靠 20 条命令的对照表换来的（一致率 16/20，漏的 3 条已进代码闸门）。
  **换审核模型时这张表必须重跑**，否则"快"就没有依据。
- 重复提醒只在 `output` 文本里追加一句，**不参与判决**，也不阻断执行：真正的硬止损是 `guard`、`-s`，以及策略拒绝到 `DENY_STOP` / `DENY_TURN_STOP` 那两条（只有代码判的拒绝计数，模型审的不算——它的理由每次不一样）。
- `WebApi` 无鉴权、只 3 个路由、单文件内联 HTML，仅用于演示"前端属于适配层"。它是 threaded server，
  但本地 Ollama 只有一个实例，并发请求实际会排队（实测单请求 2–11 s）。
- `doctor` 的 `assistant_online` 会**真发一次极小补全**（返回 `pong`）：早先只调 `models.list()` 的版本
  会被骗——DashScope 的模型列表不鉴权也返回，塞假 key 也能"通过"。无效 key 现在如实报 401。
- `.env` 里的 key 只被 `load_dotenv` 读取；**已设的同名环境变量优先**，所以残留的 `$env:DASHSCOPE_API_KEY`
  会盖掉文件内容，排查前先 `Remove-Item Env:\DASHSCOPE_API_KEY`。
- 用 PowerShell `Get-Content` 直接看中文文件（含 `.env` 注释、`runs/doctor.json`）会显示乱码，那是控制台
  GBK 解码 UTF-8 的问题，文件本身是 UTF-8；要核对请用 `python -c` 按 UTF-8 读，或先设 `[Console]::OutputEncoding`。
  上面那次端到端里，模型自己也踩到了同一个坑并自己改成了带 BOM 的 UTF-8。
- 未做（视频留的作业 / 骨架的 Silver+Gold）：对话分支、上下文压缩、流式输出、成本限制、
  Anthropic / Gemini 的真实实现（现在是空位）、工具调用参数 schema 校验（坏 JSON 只降级成空参数）。
- assistant 走公网：工具输出会随上下文外发，敏感内容别放进 workspace。

## 参考

分层骨架（assistant + tools + 循环 + jsonl 轨迹这一套）参考课程视频
【Agent 开发第一课：50 行代码实现极简 Agent】https://www.bilibili.com/video/BV1W1nT67EB1/ 。
开发期用来确定架构的那份草稿（`summary.md`）只是本机留档，已经 `.gitignore` 掉、不随仓库分发；
能在这儿复核的实测记录是 `docs/spark-harness-design.md`（spark 的 temp、`num_ctx` 造成的 32K 墙、
审核链该放哪）和上面「实测数据」那张表。
