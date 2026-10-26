# AI IDE 每日签到 / 积分自动领取（Qoder CN · Trae CN · WorkBuddy · 库库AI）

一个**零 GUI、纯接口**的每日签到项目：只读取各客户端在本机的登录态文件，解出 token，
直接调用官方同源签到接口领取每日积分。无需打开客户端、无需模拟点击，适合挂计划任务。

思路整理并复刻自 linux.do 帖子
[《Qoder CN、Trae CN、WorkBuddy 免费签到脚本》](https://linux.do/t/topic/2945247)（作者 yellowred / 起风了灬）。
本项目在其基础上补齐依赖、统一入口、幂等日志与 Windows 计划任务脚本。

> ⚠️ 安全声明：所有接口均来自官方客户端的同源调用，脚本**只读登录态、不修改任何文件、不打印 token**。
> 仅供个人在自己电脑上学习研究使用，请勿外传 token 相关代码，勿用于任何商业用途。风险自负。

---

## 与「CDP 模拟点击」方案的差异

| 维度 | 本项目（API 直调） | CDP / UI 自动化（kuku-ai、qoder-cn-daily-checkin） |
|------|------|------|
| 触发方式 | 读本地 token → 调官方 HTTP 接口 | 带调试端口启动客户端 → 模拟点击面板按钮 |
| 是否要开客户端 | 否（只要登录态文件在，token 未过期即可） | 是（客户端必须在运行） |
| 稳定性 | 高（接口契约，幂等） | 中（受 UI/DOM、iframe 残留、动画影响） |
| 依赖 | Python 3 + requests/cryptography/pycryptodome | Node.js（零第三方依赖） |
| 覆盖面 | 一家一套解密，覆盖四家 | 单家单客户端 |

两种方案互补：接口方案更省心，UI 方案在 token 解密路径变更时仍可兜底。

---

## 四家登录态与接口一览

| 产品 | 凭证路径 | 加密方式 | 签到接口 | 关键 Header |
|------|----------|----------|----------|-------------|
| **Qoder CN** | `%APPDATA%\com.qodercn.app.stable\` 下 `Local State` + `auth.v1.dat` | DPAPI 解出 AES-256 key → AES-GCM 解 auth.v1.dat | `GET /sash/api/v1/me/campaigns`；`POST .../campaigns/{id}/claim` | `Authorization: Bearer <token>` + **`Cosy-ClientType: 10`**（缺它 campaigns 返回空数组） |
| **Trae CN / SOLO** | `%APPDATA%\Trae CN\User\globalStorage\storage.json` 的 `iCubeAuthInfo://icube.cloudide` | 自定义：SHA-512 派生 + AES-CBC + 去 64B HMAC | `POST .../checkin_credits/status`；`POST .../checkin_credits/claim` | `Authorization: Cloud-IDE-JWT <token>` + `x-device-id` + `X-User-Region` |
| **WorkBuddy** | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info` | 5.6.0 前明文；5.6.0 起 `$wbEncrypted`（AES-256-GCM，at-rest 密钥派生） | `POST /v2/billing/meter/checkin-status`；`POST .../daily-checkin` | `Authorization: Bearer <accessToken>` + `X-User-Id` + `X-Domain` |
| **库库AI (GenFlowPro)** | `%APPDATA%\baidugenflowpro\Network\Cookies`（默认上下文仓） | Chromium Cookie 仓：DPAPI 解 os_crypt key → AES-GCM 解 BDUSS/STOKEN | `GET /api/genflowpro/freepoint/homenew`；`POST .../taskComplete`（置对话任务为可领）；`POST .../rewardClaim` | 仅 `Cookie: BDUSS=..; STOKEN=..; gfprotpl=genflowpro`（**无 Authorization/签名**） |

四家签到**均为幂等**：已签到时 Qoder 返回 `replayed:true`、Trae 返回 `checked_in:true`、WorkBuddy 返回 `code:10001`、库库任务领取后变 `FINISHED`（重复领 `errno≠0`），重复运行不会重复发放，乱点无害。

> ⚠️ **2026-10-15 活动改版**：库库旧 `freepoint`（`daily_login`/`daily_chat` 各 +50）已**下线**——`homenew` 现返回空 `activities`，本脱机 provider 对库库当前不再产出（`checkin_kuku.py` 安全 no-op）。新活动「金秋厚礼」走 `kuku.baidu.com/comps/view` 且每个请求都带 App 注入的 `window.jsToken`，**脱机无法生成该令牌**，故库库改由姊妹 CDP 项目 `kuku-ai-daily-checkin` 领取（连签送 SVIP + 对话积分+10）。

（以下为改版前 freepoint 脱机方案的**历史说明**，接口现均已下线）库库旧 freepoint 的 `daily_login`/`daily_chat` 曾可纯脱机领满 100：`daily_chat` 先调 `POST .../freepoint/taskComplete`（`task_type=CHAT`）置为可领再 `rewardClaim`（实测服务端曾接受该信号、不校验是否真对话过）；`invite_*` 需真实邀请。

---

## 目录结构

```
ai-ide-daily-checkin/
├── checkin.py              # 统一入口：python checkin.py [all|qoder|trae|workbuddy]，末尾汇总推送飞书（含积分小节）
├── checkin_qoder.py        # Qoder CN：用量汇总 + 签到（DPAPI + AES-GCM）+ 积分余额/过期
├── checkin_trae.py         # Trae CN / SOLO：签到（iCubeAuthInfo 自定义加密）+ 积分余额/过期
├── checkin_workbuddy.py    # WorkBuddy：签到（$wbEncrypted AES-GCM）+ 积分余额/过期
├── checkin_kuku.py         # 库库AI：脱机领积分（freepoint，读 cookie 仓 BDUSS/STOKEN）
├── credits_common.py       # 积分汇总共享工具（时间解析/格式化、临期合计）
├── credits_ledger.json     # Qoder 积分发放单账本（本地累积，被 .gitignore 忽略）
├── feishu_notify.py        # 飞书群机器人推送（curl 主 + requests 兜底）
├── config.json             # 本地配置：feishuWebhook（含私有 webhook，被 .gitignore 忽略）
├── config.example.json     # 配置模板（复制为 config.json 后填入 webhook）
├── requirements.txt        # requests / cryptography / pycryptodome
├── auths.example.json      # 可选：多账号 / 免本地解密（复制为 auths.json）
├── setup.bat               # 建 venv + 装依赖
├── run-all.bat             # 手动跑四家（前台，带 pause）
├── install-schedule.bat    # 注册每日计划任务（默认 10:05；调用下方 ps1）
├── install-schedule.ps1    # 计划任务实现：『不管是否登录都运行』+ StartWhenAvailable
├── uninstall-schedule.bat  # 删除计划任务
├── kuku_auth.json          # 库库登录态缓存（经验证有效后写入，被 gitignore）
├── checkin.log             # 运行日志（自动生成，已被 .gitignore 忽略）
└── README.md
```

---

## 快速开始

前置：三个客户端中你想签到的那几家，**已经在本机登录过**（登录态文件存在、token 未过期）。
Python 3.9+（开发验证于 3.13）。

```bat
:: 1) 装依赖（建 .venv 并安装 requests / cryptography / pycryptodome）
setup.bat

:: 2) 先干跑，确认能解密 + 拉到活动列表（不会真正领取）
.venv\Scripts\python.exe checkin.py qoder claim --dry-run

:: 3) 正式领取四家
.venv\Scripts\python.exe checkin.py all
```

也可以单独跑某一家：

```bat
.venv\Scripts\python.exe checkin.py qoder           :: 用量汇总 + 自动签到
.venv\Scripts\python.exe checkin.py qoder campaigns  :: 打印原始活动 JSON
.venv\Scripts\python.exe checkin_qoder.py claim       :: 直接跑单脚本（等价）
.venv\Scripts\python.exe checkin_trae.py
.venv\Scripts\python.exe checkin_workbuddy.py
.venv\Scripts\python.exe checkin_kuku.py --dry-run  :: 库库：只列可领任务，不领取
```

或直接双击 `run-all.bat`。运行明细写入同目录 `checkin.log`。

---

## 飞书推送（可选）

`checkin.py` 跑完四家后汇总发一条飞书群机器人**交互卡片**（与 trae-daily-checkin 同一个机器人）：

- 卡片头部按成功率着色：全成功绿色、部分成功橙色、全失败红色，标题带 `3/4 成功`
- 每家一段：产品名 + 成功/失败标记 + 最后一条关键日志（灰色小字）
- 「积分与过期」小节：余额、最近到期时间与数量、7 天内到期合计；Qoder 的估算备注同样以灰色小字展示
- 卡片发送失败时自动回退为纯文本消息，信息不丢

卡片内容示例（实际在飞书中渲染为带色卡片）：

```text
AI IDE 每日签到 · 3/4 成功
成功 3/4 家 ｜ 聚合退出码 1
────────────────────────
Qoder CN  成功
没有 CLAIMABLE 的活动，无需签到
Trae CN  成功
(API) 今日已签到，当前积分：100
WorkBuddy  成功
(API) 今日已签到
库库AI  失败
未获取到任何 BDUSS
────────────────────────
积分与过期
Qoder CN  余额 1200 ｜ 最近到期 11-21 02:05（100）
Trae CN  余额 3433.9 ｜ 最近到期 10-23 02:31（150）｜ 7 天内到期 1050
WorkBuddy  余额 6066 ｜ 最近到期 10-22 23:37（34）｜ 7 天内到期 6066
```

webhook 三级优先级：

1. `--feishu <url>` 命令行参数
2. 环境变量 `AICHECKIN_FEISHU_WEBHOOK`
3. 同目录 `config.json` 的 `feishuWebhook` 字段（把 `config.example.json` 复制为 `config.json` 填入；`config.json` 含私有 webhook，已被 `.gitignore` 忽略，勿提交）

- 推送通道：curl 主（Windows 自动加 `--ssl-no-revoke`，规避 schannel 证书吊销离线检查失败）+ requests 兜底；**卡片发送失败自动回退纯文本**；**推送失败仅告警，不影响签到退出码**。
- 临时关闭：`python checkin.py all --no-push`。
- 汇总退出码与推送结果也会写入 `checkin.log`（`pythonw` 丢弃 stdout，计划任务场景靠它排查）。
- 单独跑某个子脚本（如 `python checkin_trae.py`）不推送，推送只挂在统一入口 `checkin.py`。

---

## 积分余额与过期（每日推送小节）

每次汇总推送在签到状态之后附带「积分与过期」小节：各家**剩余积分**、**最近一笔过期时间与数量**、**7 天内到期合计**。示例：

```text
── 积分与过期 ──
Qoder CN：余额 1200｜最近到期 11-21 02:05（100）｜历史约1100积分按日滚动到期（首签估算 10-11，领取后30天有效，未记录的每日约100）
Trae CN：余额 3433.9｜最近到期 10-23 02:31（150）｜7天内到期 1050
WorkBuddy：余额 6066｜最近到期 10-22 23:37（34）｜7天内到期 6066
```

各家数据来源（均为官方同源接口，2026-10-22 实测）：

| 产品 | 余额接口 | 过期时间 | 说明 |
|---|---|---|---|
| Qoder CN | `/api/v2/quota/usage` 的 `addOnQuota.remaining` | claim 重放 | campaigns 无历史翻页；对已领取 campaign 重放 claim（幂等）拿回发放单的 `expiresAt`，逐笔记入本地 `credits_ledger.json`；账本之前的历史按「每日领取、30 天有效」规则**估算**（消息中以 `~` 标注） |
| Trae CN | `/trae/api/v2/pay/ide_user_ent_usage` 的 `usage_summary`（total-consumed） | 同接口权益包列表 | 每笔签到奖励独立成包，`expire_time` = 领取时间 + 30 天，逐笔精确 |
| WorkBuddy | `/v2/billing/meter/get-user-resource` 活跃包 `CapacityRemain` 合计 | 同接口每包 `CycleEndTime` | 必须带 `User-Agent: CodeBuddyCN`（服务端硬校验，否则 403）；调用签名逆向自客户端 `out/main.js` |

查询失败不影响签到退出码，小节中该家显示「查询失败（原因）」。库库AI 暂无积分接口，不占小节。

---

## 注册 Windows 计划任务

```bat
install-schedule.bat
```

- 任务名 `AICheckinDaily`，默认每天 **10:05** 运行（Qoder CN 每日活动 10:00 UTC+8 刷新，安排在其后；Trae / WorkBuddy 同点跑也无害，幂等）。
- 自定义时间：`install-schedule.bat 08:30`
- 用 `pythonw.exe`（venv 优先）无窗口后台执行 `checkin.py all`。
- **默认改为「不管用户是否登录都要运行」**（`install-schedule.bat` → `install-schedule.ps1` 用 `Register-ScheduledTask -User .. -Password ..`）：
  运行脚本时会在**控制台内**提示你输入 Windows 登录密码（隐藏回显，不是 GUI 弹窗；旧版用 `PromptForCredential` 在无 GUI 宿主下会静默不弹窗，已改）。
  Task Scheduler 会用保存的凭据做一次 Batch 登录并加载用户配置文件，**DPAPI 仍能解密**，各家照常工作（已实测：无人值守批处理登录下四家全跑通、`lastResult=0`）；
  另加 `-StartWhenAvailable`，错过时间点（关机/睡眠）后开机尽快补跑。
- ⚠️ **DPAPI 冲突（关键）**：四家都靠 `CryptUnprotectData`（用户主密钥）。「不管是否登录都要运行」**只有『保存了密码』(Password 登录类型) 时才工作**；
  若配成「不保存密码 / S4U / 服务账户」，无明文凭据 → `CryptUnprotectData failed` → 全部失败。
  **改 Windows 登录密码后，需重新运行 `install-schedule.bat` 刷新保存的凭据**（密码由系统 LSA 保管，不写入仓库）。
- 不想保存密码就退回旧的**仅登录时运行**（`/IT`，用户没登录到桌面则不触发）：
  `schtasks /Create /F /SC DAILY /ST 10:05 /TN AICheckinDaily /IT /TR "\"%CD%\.venv\Scripts\pythonw.exe\" \"%CD%\checkin.py\" all"`
- 卸载：`uninstall-schedule.bat`；管理：`taskschd.msc`。

如果你只想给某一家单独排程，可手动加任务，例如：

```bat
schtasks /Create /F /SC DAILY /ST 10:05 /TN "AICheckinQoder" /IT ^
  /TR "\"%CD%\.venv\Scripts\pythonw.exe\" \"%CD%\checkin_qoder.py\" claim"
```

---

## 常见问题（对应原帖「六、排查」）

- **Q1 `CryptUnprotectData failed`** — DPAPI 需要能解锁用户主密钥的登录凭据。用 `install-schedule.bat` 注册时**输入了密码**（Password 登录类型）即可非交互解密；
  若任务被配成「不保存密码 / S4U」，或改了 Windows 密码却没刷新任务凭据，就会失败。排查：`schtasks /Query /TN AICheckinDaily /FO LIST /V` 看登录模式。
- **Q2 campaigns 返回空数组** — 少了 `Cosy-ClientType: 10`；或 `Authorization` 用了裸 token 没加 `Bearer `（会 401 TOKEN_INVALID）。
- **Q3 签到后客户端显示未登录** — 千万别在脚本里自行刷 refresh token。refresh 一次一换，改了不写回会让客户端下次刷新 400/401 → 直接登出。正确做法：脚本只读不动 refresh，token 临期（约 16 天）前打开一次客户端让它自己刷新。
- **Q4 计划任务跑完没日志** — `pythonw` 不写 stdout，脚本内部已显式追加 `checkin.log`；若用命令行重定向需 `>> checkin.log 2>&1`。
- **Q5 WorkBuddy `keyId` 不匹配 / 版本更新** — `_AT_REST_SECRET` 是内嵌 at-rest 密钥，WorkBuddy 大版本升级后可能更换，需重新提取。
- **Q6 accessToken/登录态过期** — Trae / WorkBuddy 会记录状态失败；打开对应客户端登录一次刷新登录态文件即可。
- **Q7 库库 `homenew errno=-6`（未登录）** — 脱机用的 `STOKEN` 陈旧。库库的 genflowpro STOKEN 由客户端启动时经 native 引擎交换得到，落盘在**默认上下文** Cookie 仓；若客户端运行时读盘（DB 被独占）会回退缓存 `kuku_auth.json`；缓存也旧时，**正常打开一次库库再优雅退出**让其把最新 STOKEN 落盘即可（脚本已做“验证有效才写缓存”，陈旧 cookie 不会污染缓存）。
- **Q8 计划任务 `LastResult` = 2147942402（0x80070002 找不到文件）、`checkin.log` 无新记录** — 任务动作里是**裸 `pythonw.exe`**：Task Scheduler 按**机器 PATH**（而非交互用户的 PATH）解析裸命令名，机器 PATH 无 Python 时动作启动即失败，签到根本不会执行。`install-schedule.ps1` 已修复为始终注册**绝对路径**解释器（`.venv` 优先，否则解析系统 `pythonw.exe` 的绝对路径），重新运行 `install-schedule.bat` 即可。
  注意：对已存凭据的现有任务，`Set-ScheduledTask` / `schtasks /Change` 都要求重新输入 Windows 密码才能改写（凭据由系统 LSA 保管），**无法免密修复**；任务若已处于该状态，只能重跑 `install-schedule.bat` 输入密码刷新注册。改 Windows 登录密码后同样需要重跑刷新。

---

## 多账号 / 免本地解密（可选）

把 `auths.example.json` 复制为 `auths.json` 并填入从 `auth.v1.dat` 解出的凭证（对象或对象列表）。
`checkin_qoder.py` 读取优先级：`--auth-file` > 同目录 `auths.json` > 本机客户端解密。
适合把 token 同步到别处（如青龙面板）后跑，但请注意 token 敏感，`auths.json` 已被 `.gitignore` 忽略，勿提交。

---

## 许可与免责声明

本项目仅用于个人学习研究。所用接口均来自官方客户端同源调用，未做任何接口篡改。
因使用本项目导致的任何账号风险、积分异常或其他损失，作者与本项目维护者不承担责任，请自行评估风险。
接口如有变更，以官方客户端实际行为为准。
