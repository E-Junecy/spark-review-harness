# TUI 截图（`harness.bat` / `python main.py tui` 的真实画面）

这些 PNG 不是示意图：由 `adapters/tui.py` 真渲染出来后，用 textual 的 `App.export_screenshot()` 导出 SVG，
再 `rsvg-convert` 转成 PNG。尺寸是 `run_test(size=...)` 指定的假终端（大多 112×30，21 那张故意 76×26
就是为了逼出"放不下"的情形），真机上会跟随窗口缩放。

**给手机看的说明**：下面每张都抄了它那一屏的**逐字文本**（用 `_tools/svg2txt.py` 从 SVG 的文本片段还原，
不是手抄），打不开图也能核对界面。

## 现在这一版（改版后，17 张）

| 文件 | 数据来源 | 看什么 |
|---|---|---|
| `10-live-turn-new-status-lines.png` | 假 service | 一轮**进行中**：命令、审核结论、工具输出逐条落地；状态就两行（账 / 态） |
| `11-turn-sealed.png` | 假 service | **每次思考折一块**（`▶ 思考 2 · shell · 拒`），最终回答摊在屏上；`本轮 3/12` 的上限就在第一行 |
| `12-two-segments-and-seconds.png` | 假 service | 同工作区第二次发问：`── 新对话 ──` 分段，屏幕不清空，**上一整轮到这一步才收起** |
| `13-ctrl-p-palette.png` | 假 service | `Ctrl+P` 面板全量条目（分类只靠标题前缀，8.2.8 的 `SystemCommand` 没有 category） |
| `22-ctrl-p-settings.png` | 假 service | 面板里输"设置"只剩三条，**标题写的就是此刻的真值**：`闸门 开` / `审核模式 local` / `步数上限 12` |
| `14-guard-armed.png` | 假 service | 闸门二次确认的第一下：只是武装，`闸门 开 · 再执行一次才真关` |
| `15-guard-off-red.png` | 假 service | 真关之后：`闸门 关 · 模型说什么就干什么` 一直亮着，`service.guard` 当场被换掉 |
| `16-workspace-picker.png` | 假 service | 换工作区（入口在面板里，`Ctrl+N` 已撤——WT 把它绑成"新建窗口"）；`　◀ 当前` 标在 **service 真绑着**的那个目录上，不是候选列表第一个 |
| `17-step-value-prompt.png` | 假 service | 步数上限**一次输入到位**（`±1` 逐格那条按你的要求删了） |
| `18-segment-list.png` | 假 service | `跳转 · 历史会话`：列当前工作区这条轨迹的对话段，选中就滚到那一轮并展开 |
| `19-trajectory-raw.png` | 假 service | `文件 · 查看轨迹原文`：jsonl 一行一条，`review` / `review_ms` 这些审计字段就在这儿 |
| `20-status-focus-marker.png` | 假 service | 焦点在状态行时**看得见**：背景变、粗体下划线、行首 `▸ 本轮 2/12` |
| `23-help-screen.png` | 假 service | `帮助 · 键位清单`：单独一屏中文键位，`Esc` 回对话，状态行一个字不改 |
| `24-recent-segment-opened.png` | 假 service | 换工作区起来后读回那两条轨迹：**最近一段摊开**（里面仍是一块一块折着），更早的收成一行标题 |
| `25-outside-write-warning.png` | 假 service | 任务里写了工作区之外的路径：发问之前就提醒会被硬规则拦，并给出 `换工作区` / `-w` 两条出路 |
| `26-multiline-paste.png` | 假 service | 四行提示词贴在多行输入框里：粘贴不再只留第一行（旧 `Input` 的 `_input.py:758` 会砍） |
| `21-marquee-hover.png` | 假 service | 身份行放不下（76 格窗口，文本 88 格）：悬停才滚动，抓到的瞬间已滚 2 个字 |

10–26 全部用 `_tools/shot_v2.py` 里的假 service 驱动：画面稳定、零网络、不花 token。身份行里的路径与
段号是脚本喂的；`闸门 开` 不是硬编码——脚本给假 service 挂了 `partial(hard_deny, workspace=...)`，
界面读的是对象本身，所以这个"开"和真机上一样。

## 逐屏文本

### 10 一轮进行中（状态只有两行）

```
 spark harness 工作区 E:\harness\quests\03_review_latency · 会话 03_review_latency-9f3c21aa · 对话 6a1f0001
▼ 第 1 轮 · 进行中
＃ 把工作目录里的基准脚本跑 5 轮，把中位数写进 runs\ab.md
▼ 思考 1 · 进行中                                   ← 正在做的这一次思考，摊着
先测一轮延迟，再和上次的数据对齐。
  → shell {"command": "python bench\\review_latency.py --runs 5"}
  审核 放行 0.8s
  ↳ median 0.82 s  p95 1.31 s
本轮 1/12 · 累计 1 步 · tokens 6,030 (5,850+180) · 进行中
档位 local · 闸门 开 · 上下文 4.6% · Ctrl+P 命令与设置 · Ctrl+Q 退出
```

（改版前这里还挂着第三、四行——`本地审核串行，其他窗口会排队` 与 `Tab 焦点到底栏 · +/- 档位 · …`
那串键位表。前者挪进面板 `设置 · 审核模式` 的说明，后者整条删掉：屏幕上真生效的键在
`帮助 · 键位清单` 那一屏里能看到（单独一屏，`Esc` 回对话），不需要一份会过期的抄本。）

第一行是"账"：`本轮 x/上限 · 累计 · 本轮秒数（跑完才有）· tokens 总(输入+输出)`，正在跑时末尾多 ` · 进行中`；
第二行是"态"：`档位 · 闸门 · 上下文% · Ctrl+P 命令与设置 · Ctrl+Q 退出`（有待生效档位时多一格黄色 `待生效：…`）。
屏幕上**不再提宿主与鼠标**：那一格本来该给档位/闸门/上下文，是加错了才删的（宿主与鼠标可用性归 `doctor` 第十项）。
`N tok/s` 与 `缓存 x%` 都撤了：前者算的是 (输入+输出)÷整轮墙钟，**不是解码速度**（实测真解码约 51–53
tk/s），挂上去就是一直在误导；后者没人看，轨迹文件里还在。内存不看（你定的）。

### 11 / 12 折叠与多段

11（一轮跑完）：

```
▼ 第 1 轮 · 3 步 · 1.2s · 拒 1
＃ 把工作目录里的基准脚本跑 5 轮，把中位数写进 runs\ab.md
▶ 思考 1 · shell · 放行 0.8s                          ← 想完、审完，这一块自己折了
▶ 思考 2 · shell · 拒                                  ← 拒的理由整条在块里，标题只留一个字
中位数从 2.69 s 降到 0.82 s，结论写在 runs\ab.md。      ← 最终回答：不折，摊在屏幕上
本轮 3/12 · 累计 3 步 · 本轮 1.2 s · tokens 18,389 (18,050+339)
档位 local · 闸门 开 · 上下文 4.8% · Ctrl+P 命令与设置 · Ctrl+Q 退出
```

12（同工作区再问一次，上一整轮这时才收起）：

```
▶ 第 1 轮 · 3 步 · 1.2s · 拒 1                          ← 上一轮：整轮收成一行
── 新对话 ──                                             ← 同工作区换对话段：画分隔，不清屏
▼ 第 2 轮 · 2 步 · 0.8s · 拒 0
＃ 顺便把 p95 也补上
▶ 思考 1 · shell · 放行 0.8s
中位数从 2.69 s 降到 0.82 s，结论写在 runs\ab.md。
```

折叠的规矩是你这条要求定的：**想一次、审一次、折一次**，最后那句要给人看的留在屏上；等下一轮开始才把
上一**整轮**折起来。旧版是"跑完一整轮 → 整轮折成一行标题"，连最终回答一起折掉了。
另一件必须记的事：`Collapsible` 本体**不是容器**，内容住在它内部的 `Contents` 里（隐藏规则
`&.-collapsed > Contents {display:none}`）。思考块起初直接挂在 `Round`（也是个 `Collapsible`）身上，
于是 `collapsed=True` 只改了标题上的 `▶/▼`，里面的东西照样在屏上——这张截图就是把它抓出来的现行犯。
现在挂进 `Contents`，并且用例断言的是**几何**（`region.height` 归零），不再只看标志位。

### 13 / 22 `Ctrl+P` 面板

```
🔎设置
设置 · 闸门 开
硬规则 + 路径监狱，当场生效。关要按两次，本段已判过的沿用旧判决
设置 · 审核模式 local
local 本地模型审，串行、免费但别的窗口会排队 · online 在线模型审，花 API 额度 · off 不审，只留闸门。换档要点跳转 · 重启界面才落地
设置 · 步数上限 12
一次输入到位，范围 1 到 40，下一个请求生效
```

13 是没过滤的全量，条目**只有** `设置/视图/跳转/文件/帮助` 五类中文——`App.get_system_commands` 白送的
`Theme / Quit / Keys / Maximize / Screenshot` 一条都没要过，所以这里不调 `super()`；搜索框那句
`Search for commands…` 也用 `Palette` 子类换成了中文。唯一残留的英文是 `command.py:889` 那条
`No matches found`：它在私有方法里现建 Option，子类接不上，只有乱打字（搜不到东西）时才看得见。排序按模糊匹配分数，不带分类字段——8.2.8 的 `SystemCommand` 只有 `(title, help, callback,
discover)`，所以分类只能写进标题前缀，这是实测出来的边界。改值类命令执行完面板会自动重开、停在原位。

### 14 / 15 闸门两下才真关

```
档位 local · 闸门 开 · 再执行一次才真关 · 上下文 4.8% · Ctrl+P 命令与设置 · Ctrl+Q 退出
闸门：再执行一次才真关——关 = 硬规则与路径监狱全部摘掉，再执行一次「设置 · 闸门」才真关

档位 local · 闸门 关 · 模型说什么就干什么 · 上下文 4.8% · Ctrl+P 命令与设置 · Ctrl+Q 退出
闸门已关：硬规则与路径监狱都不拦了 · 模型说什么就干什么。本段已判过的命令沿用旧判决，新对话段起全部重判
```

这两张是**真换对象**的结果（`service.guard = no_guard`），旧版按 `h` 只会打一句"换它得重启 harness"。
开回来装的是启动时装配好的原物；`--no-guard` 起的界面不自己合成闸门，只会说"启动时就没开，要开得重启"。

### 20 焦点在状态行（这版的关键修补）

```
▸ 本轮 2/12 · 累计 5 步 · 本轮 0.8 s · tokens 30,663 (30,080+583)
档位 local · 闸门 开 · 上下文 4.8% · Ctrl+P 命令与设置 · Ctrl+Q 退出
```

"步数上限根本调不了"的真相是焦点：焦点还在输入框时 `]` 就是打字。现在 `Tab` 挪过去之后**看得见**——
背景 + 粗体下划线 + 行首 `▸`。本来先试的 `outline` 被截图否掉了：30 行高的窗口里它画在控件外的那两行
会把第一行那行账直接挤出屏幕（焦点看得见了，步数反而没了）。另外 `StatusBar:focus` 那条会被 `#status`
的 ID 选择器压住（ID 优先级更高），所以样式最后写成 `#status:focus`。

### 21 身份行放不下时（76 格窗口）

```
park harness 工作区 E:\harness\quests\03_review_latency · 会话        ← 滚动中的那一行，已滚 2 个字
```

平时一个字不动；只有内容真放不下（`cell_len` > 宽度）才在鼠标悬停时以 0.22 s 一格轮动，手离开 1.5 s
回到原位，控件下场（`on_unmount`）一定停表。同一屏还能看见悬停弹出的 tooltip 把全文折成四行显示——
那是 overlay，不是身份行本身被折行。

### 19 轨迹原文（只读，`Esc` 返回）

```
  轨迹原文 · _tmp\shots2\base\sessions\03_review_latency-9f3c21aa.jsonl · Esc 返回对话
{"kind": "session", "id": "03_review_latency-9f3c21aa"}
{"kind": "message", "type": "UserMessage", "role": "user", "content": "把工作目录里的基准脚本跑 5 轮, …
{"kind": "message", "type": "AssistantMessage", …, "prompt_tokens": 5850, "completion_tokens": 180, …
{"kind": "message", "type": "ToolResult", …, "content": "{\"review\": \"allowed by reviewer\", …", "review_ms": 820.0}
```

文件是真 `JsonlSessionStore` 写到真文件再读回来的（截图写到 `_tmp/shots2/base/`，不碰仓库的 `sessions/`）。
交报告时数消息条数、核对审计字段就用这一屏。

### 23 帮助是单独一屏（`Esc` 返回）

```
  键位 · Esc 返回对话
23-帮助单独一屏
Ctrl+P       命令面板：闸门、档位、步数、工作区、历史会话、轨迹、帮助都在里面
Ctrl+Q       退出
Ctrl+B       折叠或展开全部轮次
Enter        发送输入框里那段话
Shift+Enter  输入框里换行。有些终端分不出它，那就按 Ctrl+J；粘贴多行本来就完整
Tab          把焦点挪到下面那两条状态行。挪过去之后：
+ 或 -        换审核档位，换完要点「跳转 · 重启界面」才落地
[ 或 ]        加减步数上限，下一个请求生效
h            开关闸门，关要按两次
滚轮           翻对话
```

前一版我把这份清单写进状态行下面那格，结果"出现了快捷键却消不掉"——临时说明挂进常驻区，就得再造一个
撤销动作。现在借用轨迹只读屏那个类（`TrajectoryScreen`）：多一屏，不多一套机制；再执行一次「帮助 · 键位清单」
就是把那屏关掉，状态行全程一个字没动（用例 `test_the_help_entry_opens_its_own_screen_and_closes_again` 钉着）。

### 24 切到别的工作区，读回那段对话

```
 spark harness 工作区 E:\harness\quests\03_review_latency · 会话 03_review_latency-9f3c21aa · 对话 6a1d0002
▶ 第 1 轮 · 1 步 · 12.0s · 拒 0
── 新对话 ──
▼ 第 2 轮 · 2 步 · 4.2s · 拒 0
＃ 上次：thinking off 再跑一遍
▶ 思考 1 · shell · 放行 0.8s                           ← 读回来的旧思考，同样是一块一块折着
▶ 思考 2 · shell · 放行 0.8s
0.82 s，结论写在 runs\ab.md。                            ← 这段最后说了什么，一眼就在
本轮 3/12 · 累计 3 步 · tokens 16,281 (16,200+81)
档位 local · 闸门 开 · 上下文 4.4% · Ctrl+P 命令与设置 · Ctrl+Q 退出
```

界面一起来就把这条轨迹读回来（`_load_history`），**只有最近一个对话段摊开**，更早的段收成 `▶` 一行：
全折叠看着像"对话没了"，全展开会被旧内容占满屏。一条消息都没有时不是一片空白，而是一行
`这个工作区还没有对话 · 全部轨迹：python main.py sessions`，第一次真发问它就消失。

### 25 任务里写了别的目录，一开口就提醒

```
 spark harness 工作区 E:\harness\quests\03_review_latency · 会话 03_review_latency-9f3c21aa · 对话 6a1f0001
▼ 第 1 轮 · 3 步 · 0.0s · 拒 1
＃ 把 C:\Users\pu\Documents\实验三\README.md 按提纲写好
▶ 思考 1 · shell · 放行 0.8s
▶ 思考 2 · shell · 拒
中位数从 2.69 s 降到 0.82 s，结论写在 runs\ab.md。
本轮 3/12 · 累计 3 步 · 本轮 0.0 s · tokens 18,389 (18,050+339)
档位 local · 闸门 开 · 上下文 4.8% · Ctrl+P 命令与设置 · Ctrl+Q 退出
注意：任务里写了 C:\Users\pu\Documents\实验三\README.md，工作区是 E:\harness\quests\03_review_latency，
往那儿写的命令会被硬规则拦下。要用那个目录就「跳转 · 换工作区」，或退出后 main.py tui -w 指过去
```

最后那行提醒是**按下回车那一刻**挂到状态行下面的（模型还没开口），这张截图里连过程一起拍完了。起因是一条真轨迹：
`sessions/experiment_3-4ded7f5f.jsonl` 35 行里 7 行是策略拒绝，同一个 `README.md` 被连交 4 次——
每一次都是一整轮模型请求，纯烧 token。现在三处一起堵：这一行提前提醒；拒绝理由里写清工作目录到底是哪个；
同一条理由连拒 3 次（或一轮 6 次）`service` 直接停手（`DENY_STOP` / `DENY_TURN_STOP`）。
拦本身是对的：工作区外就是不许写，要改那个目录请用 `-w` 指过去或者换工作区。

### 26 四行提示词躺在输入框里（还没发送）

```
 spark harness 工作区 E:\harness\quests\03_review_latency · 会话 03_review_latency-9f3c21aa · 对话 未绑定
这个工作区还没有对话 · 全部轨迹：python main.py sessions
按下述提纲写实验报告：
一、环境与版本
二、步骤与命令
三、结论与抖动
本轮 0/12 · 累计 0 步 · 未启用计量
档位 local · 闸门 开 · 上下文 — · Ctrl+P 命令与设置 · Ctrl+Q 退出
```

中间那四行**全在输入框内**（框自己长到四行高，上下边框另算）。旧界面用的是 textual 单行 `Input`，
它处理粘贴时先做 `event.text.splitlines()[0]`——多行贴进去只剩第一行，剩下的静默丢掉，所以你贴的长
提示词从来没完整送出去过。换 `PromptArea`（`TextArea` 子类）之后：粘贴整段保留、`Shift+Enter` 换行、
`Ctrl+J` 是兜底换行（传统 conhost 分不出修饰键 + Enter），`Enter` 发送，框最多长到五行正文。
发出去之后屏幕上摊开的是整段问题（`＃` 那几行），超过六行才折成"……还有 N 行，全文在这条轨迹里"。

## 06 / 07 真模型跑的两帧（**改版前的底栏**，留着做对照）

```
 spark harness 工作区 _tmp\t04real · 会话 t04real-e5a078f5 · 对话 c72fce5f
▼ 第 1 轮 · 进行中
＃ 在工作目录写 note.txt，内容是 hello，然后读回来确认，不要做别的事
（这一步没说话，直接要执行命令）
  → python {"code": "with open(\"note.txt\", \"w\", encoding=\"utf-8\") as f: …
档位 local · 步数上限 6 · 硬规则：开（改动需重启）
本轮 1 步 · 累计 1 步 · tokens 825（635+190） · 缓存 0.0% · 上下文 0.5% · — tok/s

▶ 第 1 轮 · 5 步 · 20.3s · 拒 1          ← 07：整轮结束后
tokens 6,680（5,850+830） · 缓存 35.0% · 上下文 1.2% · 329 tok/s
```

数据来自真链路：在线 assistant + 本地 spark 审核，会话 `t04real-e5a078f5`，轨迹还在
`sessions/t04real-e5a078f5.jsonl`（截图用的工作目录 `_tmp\t04real` 是临时产物，会被删，轨迹不会）。
`（这一步没说话，直接要执行命令）` 是刻意的：模型经常返回"空 text + 一个工具调用"，界面不许把它显示成
一片空白。**注意这两张的底栏已经不代表现在的界面**：`硬规则：开（改动需重启）` 那句是当时的事实错误
（闸门其实可切），`329 tok/s` 是虚荣指标，现在都改掉了。旧版 01–05 五张（同一套旧底栏的假 service 画面）
已从本目录删除，避免和现版混读。

## 已知不完美（截图时撞到的，都记在案）

1. **10–26 的数字是脚本喂的假 usage**，只有 06/07 的 `6,680 / 35.0% / 329 tok/s` 来自真实 `usage`。
   画面本身（布局、文案、交互态）是真渲染出来的。
2. **21 只能拍到滚动的某一瞬间**，PNG 不会动。要看真滚动请在 Windows Terminal 里把鼠标放到身份行上
   （conhost 里没有鼠标可用，见仓库 README「已知边界」）。
3. **13 那张面板被窗口高度截断**：面板是列表，条目多时下半屏看不见，用打字过滤（22）就能看全我们要的三条。
4. 截图里已经没有"宿主/鼠标"那一格了（本来就不该有）。想知道自己这扇窗有没有鼠标，跑 `main.py doctor` 看第十项。
5. 旧版界面的截图脚本 `_tools/shot.py` 已删（它产的 01–05 随旧界面一起作废）；真模型那两张出自 `_tools/shot_real.py`。

## 重跑这些图

```powershell
# 现版 17 张：假 service，零网络，不花 token（工作目录写到 _tmp\shots2\base，不碰 sessions/）
C:\conda\envs\llm_learn\python.exe E:\harness\screenshots\_tools\shot_v2.py
# 06/07 那两张真模型的（会发真请求，约 25s）
C:\conda\envs\llm_learn\python.exe E:\harness\screenshots\_tools\shot_real.py
# SVG 都在 _tmp\shots2\（或 _tmp\shots\），转 PNG：
rsvg-convert -w 1568 -b "#101010" -o screenshots\13-ctrl-p-palette.png "_tmp\shots2\13-Ctrl-P-面板标题写当前值.svg"
# 核对逐字文本（本文件那些代码块就是这么来的）：
$env:SHOTS_DIR='_tmp/shots2'; C:\conda\envs\llm_learn\python.exe E:\harness\screenshots\_tools\svg2txt.py 22-Ctrl-P-输入设置只剩三条控制项
```
