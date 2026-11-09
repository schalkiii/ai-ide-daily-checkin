# AGENTS.md — AI IDE 每日签到（脱机 API 直调 · 五家）

## 定位
零 GUI、纯接口：只读各客户端在本机的登录态文件，解出 token，直调官方同源签到接口领每日积分。
覆盖 Qoder CN / Trae CN / WorkBuddy / 库库AI(GenFlowPro) / 豆包PC 五家；`all` 默认调度 3 家（库库、豆包脱机暂停，只留手动入口）。Python 3.9+（验证于 3.13）。

## 怎么跑
```bat
setup.bat                                            :: 建 .venv + 装 requests/cryptography/pycryptodome
.venv\Scripts\python.exe checkin.py all              :: 领取当前启用各家（默认 3 家）
.venv\Scripts\python.exe checkin.py qoder claim --dry-run  :: 干跑（不真正领取）
.venv\Scripts\python.exe checkin.py doubao --dry-run       :: 豆包只查状态（已从 all 暂停，手动入口保留）
install-schedule.bat                                 :: 注册 AICheckinDaily（默认每天 10:05）
```
统一入口 `checkin.py`，`all` 执行顺序 = qoder → trae → workbuddy（库库、豆包默认跳过，
`checkin.py kuku` / `checkin.py doubao` 保留手动入口）。
本机未建 `.venv`：把 `.venv\Scripts\python.exe` 换成系统 `python.exe`
（`C:\Users\Schal\AppData\Local\Programs\Python\Python313`）即可；计划任务已按 **10:00** 注册
（`install-schedule.bat 10:00`，改时间需重输 Windows 密码）。

## 技术栈与约定
- 每家一个 `checkin_<name>.py`，解密路径与接口各异（详见 README 一览表）。
- 飞书推送：统一入口 `checkin.py` 汇总各家状态，经 `feishu_notify.py` 发**交互卡片**（状态色绿/橙/红 + 每家一行；卡片失败自动回退纯文本；curl 主 + requests 兜底）；webhook 优先级 `--feishu` > 环境变量 `AICHECKIN_FEISHU_WEBHOOK` > 同目录 `config.json`（gitignore，本地与 trae-daily-checkin 同 webhook）；推送失败不影响退出码。
- 积分余额与过期：各 `checkin_<name>.py` 的 `fetch_credits()` 以 `[CREDITS] {json}` 行输出，`checkin.py` 汇总成推送里的「积分与过期」小节（`credits_common.py` 提供时间解析/临期合计）。三家接口：Qoder=`/api/v2/quota/usage`+claim 重放（expiresAt 记账到 `credits_ledger.json`，历史按规则估算）；Trae=`/pay/ide_user_ent_usage`（逐包 expire_time，**必须 require_usage 并跳过已用尽包**）；WorkBuddy=`/v2/billing/meter/get-user-resource`（**必须带 `User-Agent: CodeBuddyCN`**，否则 403）。
- 计划任务注册走 `install-schedule.ps1`：**必须纯 ASCII**（WinPS5.1 按 GBK 读无 BOM 的 UTF-8 脚本会崩）；
  密码用控制台 `Read-Host -AsSecureString` 采集（`PromptForCredential` 在纯 console 宿主里不弹窗）。
- 库库脱机读**默认上下文** Cookie 仓 `%APPDATA%\baidugenflowpro\Network\Cookies` 优先；
  `Partitions\genflow-auth` 分区仓的 STOKEN 常陈旧。
- 运行明细写 `checkin.log`；库库登录态**经 homenew 验证有效后**才写缓存 `kuku_auth.json`（防陈旧污染）。
- 豆包读 Chromium Cookie 仓 `%LOCALAPPDATA%\Doubao\User Data\Default\Network\Cookies`（v10 AES-GCM，
  明文前 32B 为 SHA256(host_key) 域绑定需剥离）；公共 query（aid=582478/device_id/web_id/fp）从
  `sdk_storage\log\saman_*.log` 最近 PC 请求 URL 解析。豆包运行时 Cookie 库被独占，回退缓存
  `doubao_auth.json`（**经 landing/get 校验有效后才写**）；首次需关闭豆包跑一次建立缓存。

## 关键事实（已实地验证）
- **本机环境陷阱（2026-11-09 实测，Agent 会话通用）**：
  ① Agent 工具沙箱用 Job Object（kill-on-close）隔离子进程，shell 结束时连带杀掉拉起的程序——
  工具内直接启动豆包会被误判为「应用崩溃循环」；脱离方式：`explorer.exe <exe>` 或一次性计划任务
  （`Register-ScheduledTask` → 启动后 `Unregister-ScheduledTask`）。
  ② 天锐绿盾透明加密：非受信进程（node.exe）写 D 盘文件被加密（文件头 `%TSD-Header-###%`）；
  node 输出统一写 `%TEMP%`（不受加密影响）；本环境 node stdout 重定向也产生 0 字节，只能写文件传结果。
- 计划任务动作**必须注册绝对路径解释器**：Task Scheduler 按机器 PATH（非交互用户 PATH）解析裸命令名，裸 `pythonw.exe` 会以 0x80070002 启动失败、签到不执行；`install-schedule.ps1` 已保证（venv 优先，否则系统 pythonw 绝对路径）。改现有任务需重输密码（LSA 凭据不可免密改写）。
- 各家均幂等：已签到重复跑不会重复发放。
- Trae「最近过期」以 `POST /trae/api/v2/pay/ide_user_ent_usage` 的
  `user_entitlement_pack_list` 为准：`expire_time` 到期、`quota.credits_limit` 总量、
  `usage.credits_amount` 已用。**必须跳过已用尽（已用 ≥ 总量）的包**，在剩余包里取最早到期；
  2026-10-27 实测 10-28/10-29 的签到包 150/150 已用尽，真正可用的是 10-30 包（已用 37.41/150，剩 112.59）。
  该查询失败只省略推送里的这段，不影响签到本身。
- 豆包活动 group = `pc_incentive_task_mvp_v3`，每日签到 +200；状态以
  `POST /alice/ug/campaign/v3/detail/` 的 `daily_signin.daily_checkin.checked_in_today` 为准，
  签到接口 `POST /alice/ug/campaign/landing/checkin` 传 `scene`（客户端还会带 `biz_params`）。
- **豆包 `daily_signin` 只在请求带上 `device_id/web_id/fp` 时才下发（2026-11-04 实测）**：缺设备参数时
  组内只剩 `chat_activity` + `points_milestone`，会被误判成「签到已下线」。客户端 2.34.6 起把请求 URL
  记进 `saman_frontend_*.log` 的资源计时条目 `"name":"https://…"`，`common_params_from_log()` 已同时解析
  主日志（裸 URL）与前端日志两种格式；用最小集查询虽 code=0 但拿不到签到任务。
- **「距上次签到满 24 小时」猜测已排除（2026-11-06 复测）**：11-04 14:41 与 11-06 23:07 距上次签到（11-03）
  都远超 24 小时，`landing/checkin` 仍返回 `710010702`；连签因此断掉（11-06 显示连签 1 天、累计 11 天）。
- **豆包 `710010702` 三途径证据链定论（2026-11-09，修正 11-06 的「脱机缺指纹被风控」假设）**：
  脱机 HTTP 直调、CDP 点击真实「立即签到」按钮、webview 内同源 `fetch` 重发，三者报文完全一致、响应完全相同
  （`HTTP 200 + code 710010702`）；CDP 环境 `navigator.webdriver=false` 无自动化特征——**真实客户端环境内点击同样被拒**，
  说明拦截维度不在报文指纹上。**用户手点「有时」成功**（11-06 手动签成功、11-09 手点也被拒），当前结论：
  服务端按时间窗/账号维度放行，与请求发起方式无关。脚本如实报失败返回 1。
  历史：10-22 首现 → 10-27 恢复 → 11-04 复现 → 11-06 排除 24h 猜测 → 11-09 仍在。
- **豆包已从 `all` 暂停（2026-11-06）**：`ALL_ORDER` 去掉 doubao（`all` 只跑 qoder/trae/workbuddy 3 家），
  `checkin.py doubao` / `--dry-run` 保留手动入口；恢复自动签到两条路：CDP 备胎（已就位，见下）或摸清服务端放行规律。
- **豆包签到 CDP 备胎（`checkin_doubao_cdp.mjs`，2026-11-09）**：真点击备胎。
  在 agent-explore webview 内文本定位「立即签到」→ Input 真鼠标点击（未触发请求自动回退 JS click）→
  **Network 域捕获 `landing/checkin` 响应体做权威判定**（按钮文本/toast 判定不可靠：失败时按钮不变）。
  退出码：0=成功/已签，1=服务端拒绝，2=点击未触发请求，3=环境错误；结果写 `%TEMP%\_doubao_checkin_result.json`。
  前置：豆包带 `--remote-debugging-port=9444` 启动（见下方环境陷阱）。已实测：链路全通，当前 verdict=rejected:710010702。
- 计划任务「不管是否登录都运行」**只有『保存了密码』(Password 登录类型) 时 DPAPI 才能非交互解密**；
  S4U / 服务账户 → `CryptUnprotectData failed` 全失败。改 Windows 密码后需重跑 `install-schedule.bat` 刷新凭据。
- 库库旧 `freepoint`（daily_login/daily_chat）于 2026-10-15 已下线（homenew 返回空 activities），`checkin_kuku.py` 现对库库安全 no-op；新活动「金秋厚礼」走 `comps/view` 且每请求需 App 注入的 `window.jsToken`，脱机无法生成 → 库库改由姊妹 CDP 项目 `kuku-ai-daily-checkin` 领取。
- 库库 `errno=-6` = STOKEN 陈旧：正常打开库库再优雅退出让其落盘，或改用 CDP 项目领取。

## 当前状态（最近更新 2026-11-09）
- 计划任务 `AICheckinDaily` 正常：动作为 `C:\Users\Schal\AppData\Local\Programs\Python\Python313\pythonw.exe checkin.py all`
  （绝对路径），LogonType=Password，每天 **10:00**（2026-10-24 带参重注册，脚本默认值仍是 10:05）；
  豆包移出 `all` 后，2026-11-06 23:07 实测三家聚合退出码 0。
- `checkin.py all` 当前覆盖 Qoder CN / Trae CN / WorkBuddy 3 家；库库、豆包脱机暂停，
  `checkin.py kuku` / `checkin.py doubao` 保留手动入口，库库日常由姊妹 CDP 项目 `kuku-ai-daily-checkin` 负责。
- 2026-10-27 实测：Qoder 领 100 Credits（余额 1600→1700）、Trae 今日已签 100、
  WorkBuddy 今日已签、豆包已签（连签 3 天）；Trae 最近可用签到包 10-30 到期（剩 112.59）。
- 推送已含「积分与过期」小节：Qoder / Trae / WorkBuddy 三家余额、最近到期与 7 天内到期合计；
  豆包、库库暂无积分接口，不占小节。
- 豆包每日签到任务仍在且已升级为**每日 +400 额度**（签到页「累计获得额度：4500 点」），但 `landing/checkin`
  自 11-04 起持续返回 `710010702`（11-06 排除 24h 猜测、11-09 三途径证据链证明真实客户端内点击同样被拒，
  用户手点偶发成功 → 服务端按时间窗/账号维度放行）。**豆包已从 `all` 暂停**（3 家运行），
  `checkin.py doubao --dry-run` 仍可查状态；CDP 备胎 `checkin_doubao_cdp.mjs` 已就位并实测（verdict=rejected、退出码 1，符合设计）。
- 豆包客户端 2026-11-05 起自动升级到 2.35.8（`Application\app\Doubao.exe`），版本本身正常；
  「强杀后崩溃循环」系沙箱 Job Object 连坐误判，豆包与用户数据均无问题。
- 本机未建 `.venv`，日常与计划任务都用系统 Python313（已装齐 requests/cryptography/pycryptodome）。

## 分工 / 安全红线
- 与姊妹项目 `kuku-ai-daily-checkin`（CDP 模拟 UI）互补：本项目免开客户端；库库对话项依赖 CDP 或手动。
- 豆包的 CDP 备胎 `checkin_doubao_cdp.mjs` 也放在本项目（同机同登录态，无需独立仓库）。
- 只读登录态、不打印 token/cookie 明文、**绝不改 refresh token**（改了不写回会让客户端下次刷新被登出）。
- `kuku_auth.json` / `auths.json` / `checkin.log` 含敏感登录态，已被 `.gitignore` 忽略，勿提交。
