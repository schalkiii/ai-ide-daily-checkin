# AGENTS.md — AI IDE 每日签到（脱机 API 直调 · 四家）

## 定位
零 GUI、纯接口：只读各客户端在本机的登录态文件，解出 token，直调官方同源签到接口领每日积分。
覆盖 Qoder CN / Trae CN / WorkBuddy / 库库AI(GenFlowPro) 四家。Python 3.9+（验证于 3.13）。

## 怎么跑
```bat
setup.bat                                            :: 建 .venv + 装 requests/cryptography/pycryptodome
.venv\Scripts\python.exe checkin.py all              :: 领取四家
.venv\Scripts\python.exe checkin.py qoder claim --dry-run  :: 干跑（不真正领取）
install-schedule.bat                                 :: 注册 AICheckinDaily（默认每天 10:05）
```
统一入口 `checkin.py`，`all` 执行顺序 = qoder → trae → workbuddy → kuku。

## 技术栈与约定
- 每家一个 `checkin_<name>.py`，解密路径与接口各异（详见 README 一览表）。
- 计划任务注册走 `install-schedule.ps1`：**必须纯 ASCII**（WinPS5.1 按 GBK 读无 BOM 的 UTF-8 脚本会崩）；
  密码用控制台 `Read-Host -AsSecureString` 采集（`PromptForCredential` 在纯 console 宿主里不弹窗）。
- 库库脱机读**默认上下文** Cookie 仓 `%APPDATA%\baidugenflowpro\Network\Cookies` 优先；
  `Partitions\genflow-auth` 分区仓的 STOKEN 常陈旧。
- 运行明细写 `checkin.log`；库库登录态**经 homenew 验证有效后**才写缓存 `kuku_auth.json`（防陈旧污染）。

## 关键事实（已实地验证）
- 四家均幂等：已签到重复跑不会重复发放。
- 计划任务「不管是否登录都运行」**只有『保存了密码』(Password 登录类型) 时 DPAPI 才能非交互解密**；
  S4U / 服务账户 → `CryptUnprotectData failed` 全失败。改 Windows 密码后需重跑 `install-schedule.bat` 刷新凭据。
- 库库 `daily_login` 与 `daily_chat` 现在都可纯脱机领满 100 分：`daily_chat` 先调 `/freepoint/taskComplete`（`task_type=CHAT`）客户端完成信号置为可领再 `rewardClaim`（实测服务端不校验真实对话）；`invite_*` 需真实邀请不自助完成。
- 库库 `errno=-6` = STOKEN 陈旧：正常打开库库再优雅退出让其落盘，或改用 CDP 项目领取。

## 分工 / 安全红线
- 与姊妹项目 `kuku-ai-daily-checkin`（CDP 模拟 UI）互补：本项目免开客户端；库库对话项依赖 CDP 或手动。
- 只读登录态、不打印 token/cookie 明文、**绝不改 refresh token**（改了不写回会让客户端下次刷新被登出）。
- `kuku_auth.json` / `auths.json` / `checkin.log` 含敏感登录态，已被 `.gitignore` 忽略，勿提交。
