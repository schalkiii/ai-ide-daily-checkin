# AI IDE 每日签到 / 积分自动领取（Qoder CN · Trae CN · WorkBuddy）

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
| 覆盖面 | 一家一套解密，覆盖三家 | 单家单客户端 |

两种方案互补：接口方案更省心，UI 方案在 token 解密路径变更时仍可兜底。

---

## 三家登录态与接口一览

| 产品 | 凭证路径 | 加密方式 | 签到接口 | 关键 Header |
|------|----------|----------|----------|-------------|
| **Qoder CN** | `%APPDATA%\com.qodercn.app.stable\` 下 `Local State` + `auth.v1.dat` | DPAPI 解出 AES-256 key → AES-GCM 解 auth.v1.dat | `GET /sash/api/v1/me/campaigns`；`POST .../campaigns/{id}/claim` | `Authorization: Bearer <token>` + **`Cosy-ClientType: 10`**（缺它 campaigns 返回空数组） |
| **Trae CN / SOLO** | `%APPDATA%\Trae CN\User\globalStorage\storage.json` 的 `iCubeAuthInfo://icube.cloudide` | 自定义：SHA-512 派生 + AES-CBC + 去 64B HMAC | `POST .../checkin_credits/status`；`POST .../checkin_credits/claim` | `Authorization: Cloud-IDE-JWT <token>` + `x-device-id` + `X-User-Region` |
| **WorkBuddy** | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info` | 5.6.0 前明文；5.6.0 起 `$wbEncrypted`（AES-256-GCM，at-rest 密钥派生） | `POST /v2/billing/meter/checkin-status`；`POST .../daily-checkin` | `Authorization: Bearer <accessToken>` + `X-User-Id` + `X-Domain` |

三家签到**均为幂等**：已签到时 Qoder 返回 `replayed:true`、Trae 返回 `checked_in:true`、WorkBuddy 返回 `code:10001`，重复运行不会重复发放，乱点无害。

---

## 目录结构

```
ai-ide-daily-checkin/
├── checkin.py              # 统一入口：python checkin.py [all|qoder|trae|workbuddy]
├── checkin_qoder.py        # Qoder CN：用量汇总 + 签到（DPAPI + AES-GCM）
├── checkin_trae.py         # Trae CN / SOLO：签到（iCubeAuthInfo 自定义加密）
├── checkin_workbuddy.py    # WorkBuddy：签到（$wbEncrypted AES-GCM）
├── requirements.txt        # requests / cryptography / pycryptodome
├── auths.example.json      # 可选：多账号 / 免本地解密（复制为 auths.json）
├── setup.bat               # 建 venv + 装依赖
├── run-all.bat             # 手动跑三家（前台，带 pause）
├── install-schedule.bat    # 注册每日计划任务（默认 10:05）
├── uninstall-schedule.bat  # 删除计划任务
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

:: 3) 正式领取三家
.venv\Scripts\python.exe checkin.py all
```

也可以单独跑某一家：

```bat
.venv\Scripts\python.exe checkin.py qoder           :: 用量汇总 + 自动签到
.venv\Scripts\python.exe checkin.py qoder campaigns  :: 打印原始活动 JSON
.venv\Scripts\python.exe checkin_qoder.py claim       :: 直接跑单脚本（等价）
.venv\Scripts\python.exe checkin_trae.py
.venv\Scripts\python.exe checkin_workbuddy.py
```

或直接双击 `run-all.bat`。运行明细写入同目录 `checkin.log`。

---

## 注册 Windows 计划任务

```bat
install-schedule.bat
```

- 任务名 `AICheckinDaily`，默认每天 **10:05** 运行（Qoder CN 每日活动 10:00 UTC+8 刷新，安排在其后；Trae / WorkBuddy 同点跑也无害，幂等）。
- 自定义时间：`install-schedule.bat 08:30`
- 用 `pythonw.exe`（venv 优先）无窗口后台执行 `checkin.py all`。
- **`/IT`（仅登录时运行）是必须的**：Qoder 的 DPAPI 主密钥只在用户交互会话可用，
  选「不管用户是否登录都要运行」会 `CryptUnprotectData failed`。
- 卸载：`uninstall-schedule.bat`；管理：`taskschd.msc`。

如果你只想给某一家单独排程，可手动加任务，例如：

```bat
schtasks /Create /F /SC DAILY /ST 10:05 /TN "AICheckinQoder" /IT ^
  /TR "\"%CD%\.venv\Scripts\pythonw.exe\" \"%CD%\checkin_qoder.py\" claim"
```

---

## 常见问题（对应原帖「六、排查」）

- **Q1 `CryptUnprotectData failed`** — 计划任务没带 `/IT`，或运行时该用户未登录。DPAPI 依赖交互登录态。
- **Q2 campaigns 返回空数组** — 少了 `Cosy-ClientType: 10`；或 `Authorization` 用了裸 token 没加 `Bearer `（会 401 TOKEN_INVALID）。
- **Q3 签到后客户端显示未登录** — 千万别在脚本里自行刷 refresh token。refresh 一次一换，改了不写回会让客户端下次刷新 400/401 → 直接登出。正确做法：脚本只读不动 refresh，token 临期（约 16 天）前打开一次客户端让它自己刷新。
- **Q4 计划任务跑完没日志** — `pythonw` 不写 stdout，脚本内部已显式追加 `checkin.log`；若用命令行重定向需 `>> checkin.log 2>&1`。
- **Q5 WorkBuddy `keyId` 不匹配 / 版本更新** — `_AT_REST_SECRET` 是内嵌 at-rest 密钥，WorkBuddy 大版本升级后可能更换，需重新提取。
- **Q6 accessToken/登录态过期** — Trae / WorkBuddy 会记录状态失败；打开对应客户端登录一次刷新登录态文件即可。

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
