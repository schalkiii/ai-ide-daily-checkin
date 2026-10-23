# AGENTS.md — AI IDE 每日签到（脱机 API 直调 · 五家）

## 定位
零 GUI、纯接口：只读各客户端在本机的登录态文件，解出 token，直调官方同源签到接口领每日积分。
覆盖 Qoder CN / Trae CN / WorkBuddy / 库库AI(GenFlowPro) / 豆包PC 五家。Python 3.9+（验证于 3.13）。

## 怎么跑
```bat
setup.bat                                            :: 建 .venv + 装 requests/cryptography/pycryptodome
.venv\Scripts\python.exe checkin.py all              :: 领取五家
.venv\Scripts\python.exe checkin.py qoder claim --dry-run  :: 干跑（不真正领取）
.venv\Scripts\python.exe checkin.py doubao --dry-run       :: 豆包只查状态
install-schedule.bat                                 :: 注册 AICheckinDaily（默认每天 10:05）
```
统一入口 `checkin.py`，`all` 执行顺序 = qoder → trae → workbuddy → kuku → doubao。

## 技术栈与约定
- 每家一个 `checkin_<name>.py`，解密路径与接口各异（详见 README 一览表）。
- 飞书推送：统一入口 `checkin.py` 汇总各家状态，经 `feishu_notify.py` 发群机器人（curl 主 + requests 兜底）；webhook 优先级 `--feishu` > 环境变量 `AICHECKIN_FEISHU_WEBHOOK` > 同目录 `config.json`（gitignore，本地与 trae-daily-checkin 同 webhook）；推送失败不影响退出码。
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
- 计划任务动作**必须注册绝对路径解释器**：Task Scheduler 按机器 PATH（非交互用户 PATH）解析裸命令名，裸 `pythonw.exe` 会以 0x80070002 启动失败、签到不执行；`install-schedule.ps1` 已保证（venv 优先，否则系统 pythonw 绝对路径）。改现有任务需重输密码（LSA 凭据不可免密改写）。
- 五家均幂等：已签到重复跑不会重复发放。
- 豆包活动 group = `pc_incentive_task_mvp_v3`，每日签到 +200 额度；状态以
  `POST /alice/ug/campaign/v3/detail/` 的 `daily_signin.daily_checkin.checked_in_today` 为准，
  签到接口 `POST /alice/ug/campaign/landing/checkin` 只传 `scene`。
- **豆包 710010702 是服务端问题（2026-10-22 实测）**：官方 PC 客户端弹窗点「立即签到」同样返回
  `710010702 系统错误`，请求报文与脚本完全一致；脚本如实报失败，服务端恢复后自动成功。
- 计划任务「不管是否登录都运行」**只有『保存了密码』(Password 登录类型) 时 DPAPI 才能非交互解密**；
  S4U / 服务账户 → `CryptUnprotectData failed` 全失败。改 Windows 密码后需重跑 `install-schedule.bat` 刷新凭据。
- 库库旧 `freepoint`（daily_login/daily_chat）于 2026-10-15 已下线（homenew 返回空 activities），`checkin_kuku.py` 现对库库安全 no-op；新活动「金秋厚礼」走 `comps/view` 且每请求需 App 注入的 `window.jsToken`，脱机无法生成 → 库库改由姊妹 CDP 项目 `kuku-ai-daily-checkin` 领取。
- 库库 `errno=-6` = STOKEN 陈旧：正常打开库库再优雅退出让其落盘，或改用 CDP 项目领取。

## 当前状态（2026-10-22 14:32）
- **AICheckinDaily 已修复并验证**：用户已于 08:2x 重跑 `install-schedule.bat` 重新注册——动作为绝对路径 `C:\Users\Schal\AppData\Local\Programs\Python\Python313\pythonw.exe`，LogonType=Password 凭据已刷新。08:27:59 手动触发实测：DPAPI 非交互解密正常，Qoder 已签 / Trae CN 已签(100) / WorkBuddy 已签，飞书汇总推送送达；`LastResult=1` **仅因库库本机未安装**（baidugenflowpro 不在机内，预期失败，由 CDP 姊妹项目负责）——这是本机的稳态退出码，不是故障。
- 今日（10-22）08:28 计划任务跑出的即为当日签到结果，无需再手动补跑。
- **豆包 provider 已加入（2026-10-22 12:04）**：`checkin_doubao.py` 完成 cookie 解密、状态查询、
  缓存回退，并接入 `checkin.py all`。本机用真实 Cookie 验证 `landing/get` / `v3/detail` 正常
  （今日未签、+200），实际签到被服务端 `710010702` 阻塞——已抓包确认官方 PC 弹窗同样失败，
  故 `checkin.py doubao` 当前退出码 1，属**预期（服务端问题）**。`doubao_auth.json` 已建立并被
  gitignore，豆包运行时可走缓存。
- 计划任务下一次起会自动包含豆包；服务端修复前聚合退出码仍为 1（库库未安装 + 豆包服务端错误），
  飞书汇总会如实显示失败，不是调度/解密故障。
- **14:30 复测**：`/landing/checkin` 仍返回 `710010702`；详情 `checked_in_today=false`、
  `last_checkin_date=2026-10-21`，额度流水只有 10-21 的 `daily_signin +200` 和
  `select_work_fields +100`，**今日确实未签、服务端未恢复**。App 里的签到入口是「探索」页的
  每日气泡（最多每天展示一次，localStorage key `agent_explore_checkin_bubble_shown_at`），
  10:42 复现时已展示/点击过，故今天不再出现——不代表已签到。
- 本机无 `.venv`；系统 Python313 已装齐 requests/cryptography/pycryptodome，手动运行直接用系统 python 即可（上方「怎么跑」的 venv 路径是标准工作流）。

## 分工 / 安全红线
- 与姊妹项目 `kuku-ai-daily-checkin`（CDP 模拟 UI）互补：本项目免开客户端；库库对话项依赖 CDP 或手动。
- 只读登录态、不打印 token/cookie 明文、**绝不改 refresh token**（改了不写回会让客户端下次刷新被登出）。
- `kuku_auth.json` / `auths.json` / `checkin.log` 含敏感登录态，已被 `.gitignore` 忽略，勿提交。
