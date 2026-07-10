# checkin —— 公益站自动签到

轻量自动签到调度器。

## 快速开始

```bash
# 1. 安装依赖（推荐 uv）
uv sync
# 首次运行浏览器路径前需拉取 Camoufox 浏览器
uv run python -m camoufox fetch

# 2. 从示例配置生成本地凭据文件（ACCOUNTS.json 已被 .gitignore，不会入库）
cp ACCOUNTS.example.json ACCOUNTS.json
# 按需编辑 ACCOUNTS.json，填入各站点的 access_token / cookie / oauth_states

# 3. 运行
uv run python run__all_checkin.py
```

> ⚠️ **凭据安全**：`ACCOUNTS.json`、`oauth_states`、`browser_state` 等含敏感登录态，已在 `.gitignore` 中忽略，**切勿提交进仓库**。GitHub Actions 部署时改为存入仓库 Secret `ACCOUNTS`（见文末「GitHub Actions 部署」）。

本项目以 [MIT License](LICENSE) 开源。

---

核心为纯标准库（Python），按**三个正交维度**组装 provider。新版 challenge 签到用 Node 内置能力；`browser` / `oauth` 登录方式与 `relogin` 签到方式使用 **Camoufox**（Firefox 反检测浏览器），突破 Cloudflare / 阿里云 WAF 限制。

## 🔥 新架构：Camoufox + 反检测绕过

浏览器路径（`browser` / `oauth` 登录方式，及 `relogin` 签到方式）已从 Playwright+Chromium 迁移到 **Camoufox**（基于 Firefox 的反检测浏览器），集成以下绕过能力：

- **Camoufox**：隐藏 `navigator.webdriver`、随机化指纹、人类化行为模拟。
- **Cloudflare cf_clearance**：自动破解 Cloudflare Interstitial 验证（playwright-captcha ClickSolver）。
- **阿里云 WAF cookies**：预加载页面获取 `acw_tc` / `cdn_sec_tc` / `acw_sc__v2`。
- **阿里云滑块**：mouse 模拟拖拽，带人类化延迟和抖动（手速仿真）。

依赖：
```bash
# 使用 uv（推荐，快速依赖管理）
uv sync

# 或 pip
pip install camoufox[geoip]>=0.4.11 playwright-captcha>=0.1.0

# 安装 Camoufox 浏览器（首次运行必须）
python -m camoufox fetch
```

> **为什么换 Camoufox？** Chromium 的自动化特征（`webdriver` / `chrome.runtime` 等）易被 Cloudflare 和阿里云 WAF 识别。Camoufox 基于 Firefox，原生隐藏 webdriver 特征，反检测通过率更高。

## 数据模型：三个正交维度

签到能力被拆成三个互相独立、可自由组合的维度：

| 维度 | 字段 | 含义 | 可选值 |
|------|------|------|--------|
| **站点适配器** | `site_profile` | 接口长什么样（路径 / 请求头 / 响应解析 / 额度换算） | `newapi` / `sub2api` |
| **登录方式** | `auth_method` | 如何获得已认证会话 | `access_token` / `cookie` / `browser` / `oauth` |
| **签到方式** | `checkin_action` | 如何触发发额度 | `api` / `relogin` / `visit` / `browser_script` |

辅助字段：

- `api_variant`（仅 `newapi` + `api`）：接口变体偏好，`auto`（challenge 优先，默认）/ `legacy`（旧接口优先）。两种变体互为失败兜底。
- `oauth_provider` + `oauth_account`（`auth_method=oauth` 或 `checkin_action=relogin`）：选择共享 OAuth 登录态，支持同一 provider 下多个账号。
- `script` / `script_args` / `script_timeout`（仅 `checkin_action=browser_script`）：仓库内相对 Python 脚本路径、脚本参数 JSON 对象、脚本超时秒数。

**有意义的组合**：

| site_profile | auth_method | checkin_action | 适用场景 |
|--------------|-------------|----------------|----------|
| newapi | access_token / cookie | api | 普通 New API 站签到（challenge / legacy） |
| newapi | browser | api | 阿里云 WAF 站点混合签到：先用站点级 `browser_state` 启浏览器过 WAF 导出 cookie，再用 HTTP 发签到请求（仿 millylee，比 relogin 快、比纯 cookie 能过 WAF） |
| sub2api | access_token | api | Sub2API 前端登录 token / 可选 API Key：优先试 `GET /v1/usage`；若返回 `INVALID_API_KEY`，说明该接口要专用 API Key，会回退前端登录态接口 |
| sub2api | browser | api | Sub2API 余额查询 / 可选签到（每次先用站点级 `browser_state` 刷新 auth_token，再按上面顺序查询） |
| sub2api | oauth | api | Sub2API 余额查询 / 可选签到（每次先用选定 OAuth 账号刷新 auth_token，再按上面顺序查询） |
| newapi | access_token / cookie | visit | 无签到接口、登录即发额度站点的「保活 + 余额监控」 |
| newapi | oauth | relogin | 浏览器自动重放第三方 OAuth 登录，**真正触发发额度** |
| newapi / sub2api | oauth / browser | browser_script | 恢复浏览器登录态后执行仓库内自定义 Python 脚本，适合只有前端按钮的站点 |

代码组织（`providers/` 包）：

- `base.py`        —— 共用模型（`SiteConfig` / `ProfileClient` / `CheckinResult` 等）、HTTP 与文本工具；
- `auth.py`        —— 登录方式：把凭据加载/规范化为统一 `AuthInfo`；
- `profiles/`      —— 站点适配器（`newapi.py` / `sub2api.py`），只管「接口长什么样」；
- `actions/`       —— 签到方式（`api.py` / `relogin.py` / `visit.py`），只管「如何触发发额度」；
- `__init__.py`    —— 组装入口：`run_checkin` 按 `site_profile` 选 profile，按 `checkin_action` 执行动作，动作内部按 `auth_method` 准备认证。

> **旧字段自动迁移**：旧配置的 `type` + `checkin_mode` 会在读取时自动映射为新三维字段并**写回** `ACCOUNTS.json`（`legacy/challenge → api`，`login_grant → visit`，`browser_oauth → oauth+relogin`；`challenge/legacy` 顺带写入 `api_variant`）。旧版 `oauth_states.provider.state` 会迁移为 `oauth_states.provider.accounts.default.state`。

provider 路由由 `providers/__init__.py` 完成（profile × auth × action 三维组装）。

## 配置文件（ACCOUNTS.json）

**三维字段全集**：

```json5
{
  "accounts": [
    {
      "name": "某 New API 站",
      "base_url": "https://example.com",
      "site_profile": "newapi",              // 站点适配器：newapi / sub2api
      "auth_method": "cookie",               // 登录方式：access_token / cookie / browser / oauth
      "checkin_action": "api",               // 签到方式：api / visit / relogin
      "api_variant": "auto",                 // 接口变体偏好（仅 newapi + api）：auto / legacy
      "enabled": true,                       // 启用状态
      "cookie": "session=xxx",               // Cookie（auth_method=cookie/access_token 时必填）
      "access_token": "eyJ...",              // Bearer token（auth_method=access_token 时必填）
      "user_id": "1234",                     // newapi 的 New-Api-User 请求头
      "proxy": "http://user:pass@host:port"  // 可选：代理，支持 http/https/socks5
    },
    {
      "name": "Sub2API",
      "base_url": "https://sub.100xlabs.space",
      "site_profile": "sub2api",
      "auth_method": "access_token",         // Sub2API 用浏览器 localStorage 的 auth_token/access_token；若你有专用 API Key 也可填这里
      "checkin_action": "api",               // 优先查 /v1/usage；INVALID_API_KEY 时回退前端登录态接口；若站点开放 check-in 扩展则顺便签到
      "access_token": "eyJhbGc...",
      "user_id": "19653"                     // 可选：Sub2API 的 user_id，旧接口回退时辅助定位
    },
    {
      "name": "AgentRouter 站",
      "base_url": "https://agentrouter.org",
      "site_profile": "newapi",
      "auth_method": "oauth",
      "checkin_action": "relogin",           // OAuth 重登签到（真正触发发额度）
      "oauth_provider": "linuxdo",           // linuxdo / github
      "oauth_account": "default",            // provider 下的账号名；登录态存顶层 oauth_states
      "proxy": ""
    },
    {
      "name": "百倍示例",
      "base_url": "https://example.100xlabs.com",
      "site_profile": "newapi",
      "auth_method": "oauth",                // 使用顶层 oauth_states，也可用 browser + 站点 browser_state
      "checkin_action": "browser_script",
      "script": "scripts/checkin/100xlabs.py",
      "script_args": { "start_path": "/check-in", "checkin_text": "签到" },
      "script_timeout": 120,
      "oauth_provider": "linuxdo",
      "oauth_account": "default"
    }
  ],
  "oauth_states": {
    "linuxdo": {
      "accounts": {
        "default": { "state": "eyJvcmlnaW4iOi...", "username": "", "updated_at": "2026-07-05T12:00:00" },
        "work":    { "state": "eyJvcmlnaW4iOi...", "username": "", "updated_at": "2026-07-05T12:30:00" }
      }
    },
    "github": {
      "accounts": {
        "default": { "state": "eyJvcmlnaW4iOi...", "username": "", "updated_at": "2026-07-05T12:00:00" }
      }
    }
  }
}
```

**简化版（只写必填字段）**：

```json5
[
  { "name": "简单站", "base_url": "https://elysiver.h-e.top",
    "site_profile": "newapi", "auth_method": "access_token", "checkin_action": "api",
    "access_token": "eyJ...", "user_id": "14573" },
  { "name": "Sub2API", "base_url": "https://sub.100xlabs.space",
    "site_profile": "sub2api", "auth_method": "access_token", "checkin_action": "api",
    "access_token": "eyJh..." }
]
```

> 旧字段 `type` + `checkin_mode` 仍能识别并自动迁移（见「数据模型」一节）。

## 登录即发额度类站点（如 AgentRouter）

部分中转站（如 [AgentRouter](https://agentrouter.org)）**没有任何独立签到接口**（`/api/user/checkin`、`/sign_in`、`/daily` 等均返回「无权进行此操作」），额度是在**第三方 OAuth 登录（Linux.do / GitHub 等）回调时发放**。针对这类站点有两种签到方式：

### `visit`：保活 + 余额监控（不触发发放）

纯标准库 HTTP，不引入浏览器：

1. 用已保存的 session / access_token 调 `/api/user/self` 保活并读取额度；
2. 额度持久化到 `login_grant_state.json`（已 `.gitignore`），跨次对比增量；
3. 额度增长 → `success`；无变化 → `already_done`；session 失效 → `need_login`。

> 它**不触发发放**，真正领取仍需你在浏览器手动登录一次。

### `relogin`：浏览器自动重放 OAuth（真正触发发放）

用 **Camoufox**（Firefox 反检测浏览器）复用顶层共享的第三方登录态，优先走站点前端 `/login` / `/register` 的 OAuth 按钮（如 AgentRouter 需在登录/注册面板间切换后点击“使用 LinuxDO 继续”），失败时再按站点 `/api/status` + `/api/oauth/state` 直连拼出 Linux.do / GitHub 授权 URL，完成 OAuth 回调，真正触发额度发放，再读 `/api/user/self` 对比前后额度。

核心浏览器逻辑集中在 `browser/` 子包（CLI 与 GUI 共享）：
- `browser/session.py` —— capture / verify / oauth checkin 共享逻辑（**async**，基于 Camoufox）
- `browser/state.py`   —— 登录态编码（storage_state → base64(gzip(json))）/ 解码
- `browser/bypass.py`  —— 绕过引擎（Camoufox 启动、Cloudflare cf_clearance、WAF cookies、滑块拖拽）
- `browser/popups.py`  —— 弹窗自动关闭守卫（MutationObserver 注入，关闭遮挡公告）
- `browser/poc_oauth.py` —— 命令行入口（setup / run，用 run_sync 桥接）
- `collector.js` —— F12 控制台凭据采集

**登录态格式**：Playwright storage_state（**跨平台 JSON**，含 cookies + localStorage），经 gzip + base64 压缩，无需加密（与其它凭据同级，靠 .gitignore / Secret 保护）。

**⚠️ 关键：relogin 站点不再保存站点级 `browser_state`。** Linux.do / GitHub 登录态统一保存在 `ACCOUNTS.json` 顶层 `oauth_states[provider].accounts[oauth_account]`，多个站点可通过各自的 `oauth_provider` + `oauth_account` 复用同一份第三方登录态。若共享态过期，OAuth 重放会停在第三方登录页并报 `need_login`。

OAuth 重放流程（`run_oauth_checkin`）：
1. 注入共享 `oauth_states[oauth_provider].accounts[oauth_account].state` → 访问站点（自动解阿里云 WAF）；
2. 优先进入 `{origin}/login`，找不到 OAuth 按钮时自动切到 `{origin}/register` 或点击登录/注册切换入口；
3. 点击站点前端的「使用 LinuxDO/GitHub 继续」，让站点自己请求 `/api/oauth/state` 并打开授权页；
4. 若前端入口未触发，再回退到 `GET {origin}/api/status` + `GET {origin}/api/oauth/state`，直连 provider 授权 URL；
5. 已有第三方登录态时点击授权按钮，带 code 回跳站点 → 触发发额度 → 读 `/api/user/self` 对比前后额度。

**登录态来源**：
- `oauth`：顶层 `oauth_states.<provider>.accounts.<account>`（共享，推荐用 GUI「捕获 OAuth 登录态」或 `browser/poc_oauth.py setup --oauth-provider ... --oauth-account ...` 生成）；
- `browser`：站点条目内的 `browser_state`（仅当前站点使用，如 sub2api 站点级刷新）。

### `browser` + `api`：过 WAF 拿 cookie + HTTP 签到（混合式）

针对**有独立签到接口、但被阿里云 WAF 挡住纯 HTTP 请求**的 New API 站点，`newapi + browser + api` 组合提供一条比 `relogin` 更快、比纯 `cookie` 更能过 WAF 的路径（思路仿 [millylee/anyrouter-check-in](https://github.com/millylee/anyrouter-check-in)）：

1. 用站点级 `browser_state`（站点登录态，GUI「浏览器登录捕获」生成）启动 Camoufox，访问站点执行阿里云 WAF 的 JS 挑战，拿到 `acw_tc` 等 WAF cookie；
2. 浏览器只负责「过 WAF + 导出 cookie」，拿到「WAF cookie + 站点 session cookie」后**立刻关闭浏览器**；
3. 把导出的 cookie 交给现有 `api` 签到逻辑，用轻量 HTTP 请求完成签到（读状态 → 签到 → 读余额）。

与 `relogin` 的区别：
- `relogin` 走完整 OAuth 重登（点登录 → 跳第三方 → 授权 → 回跳），慢且依赖第三方共享登录态；适合**没有签到接口、靠登录回调发额度**的站点；
- `browser + api` 只用浏览器过 WAF，签到本身是 HTTP，低风险 IP 上从分钟级降到秒级；适合**有签到接口但被 WAF 挡**的站点。

WAF 持续风控（数据中心/CI IP 信誉过低）时快速失败并返回 `need_verification`（提示配置住宅代理），不误报 `need_login`。刷新出的 storage_state 会回写 `browser_state` 供下次复用。

### `browser_script`：仓库内自定义浏览器脚本

`browser_script` 适合没有稳定接口、但页面上有“签到/领取”按钮的站点。程序会：

1. 按 `auth_method=oauth` 读取顶层共享 OAuth 登录态，或按 `auth_method=browser` 读取站点级 `browser_state`；
2. 启动 Camoufox 并恢复 cookies / localStorage；
3. 只允许加载仓库内相对路径的 Python 文件（禁止绝对路径、`..`、URL）；
4. 调用脚本中的 `run(page, context, site, helpers)`，返回 `{status, message, detail}`。

内置示例：`scripts/checkin/100xlabs.py`。脚本可使用 Playwright 的 `page/context`，也可用 `helpers.goto()`、`helpers.click_text()`、`helpers.screenshot()`、`helpers.success()` / `helpers.already_done()` 等便捷方法。`helpers.goto()` 支持绝对 URL 和相对路径，例如 `helpers.goto("/check-in")` 会基于当前站点 `base_url` 访问签到页。百倍示例默认首访 `/check-in`，也可用 `script_args.start_url` 或 `script_args.start_path` 覆盖。

> **依赖**：Camoufox（已在 pyproject.toml 中声明，启用 `os=macos` 指纹 + `forceScopeAccess`）。登录态会过期（站点态较短，linux.do 态较长约数周），过期后报 `need_login`，需重新捕获。
> **反检测**：集成 playwright-captcha 自动解 Cloudflare Interstitial、阿里云 WAF JS 挑战 + 滑块（`#nocaptcha .btn_slide`）。

## 配置：ACCOUNTS.json 统一管理

`ACCOUNTS.json` 统一保存站点配置、启用状态与凭据，已被 `.gitignore` 忽略，仅本地或 GitHub Secret 中保存。

```json
{
  "accounts": [
    {
      "name": "Elysiver",
      "base_url": "https://elysiver.h-e.top",
      "type": "newapi",
      "checkin_mode": "legacy",
      "enabled": true,
      "user_id": "12345",
      "access_token": "xxxx",
      "cookie": ""
    },
    {
      "name": "Sub2API",
      "base_url": "https://sub.100xlabs.space",
      "type": "sub2api",
      "enabled": true,
      "access_token": "浏览器 localStorage 的 auth_token"
    },
    {
      "name": "AgentRouter",
      "base_url": "https://agentrouter.org",
      "site_profile": "newapi",
      "auth_method": "oauth",
      "checkin_action": "relogin",
      "oauth_provider": "linuxdo",
      "oauth_account": "default",
      "enabled": true,
      "user_id": "68124"
    }
  ],
  "oauth_states": {
    "linuxdo": {
      "accounts": {
        "default": { "state": "gAAAA…（共享 Linux.do 登录态）", "username": "", "updated_at": "" }
      }
    },
    "github": {
      "accounts": {
        "default": { "state": "gAAAA…（共享 GitHub 登录态）", "username": "", "updated_at": "" }
      }
    }
  }
}
```

兼容旧对象格式：`{"accounts": {"站点名": { ... }}}`。

## 图形界面

```bash
# 需要安装 GUI 依赖
uv sync --extra gui
# 或
pip install PySide6>=6.8.0

# 启动 GUI
python manage_accounts.py
```
- 左侧站点列表，右侧编辑「站点配置 + 凭据」：选择**站点类型**、**登录方式**与**签到方式**（三维字段）；
- 选 `auth_method=oauth` 或 `checkin_action=relogin` 时会出现 **OAuth 提供商 + OAuth 账号** 控件：
  - **捕获 OAuth 登录态**：弹出浏览器登录所选 provider/account，完成后写入 `ACCOUNTS.json` 顶层 `oauth_states`；
  - **检测 OAuth 登录态**：检查当前 provider/account 是否已有 state；
  - relogin 站点不会保存站点级 `browser_state`；
- 选 `auth_method=browser` 时出现站点级「浏览器登录态」输入区；
- 选 `checkin_action=browser_script` 时会出现 **脚本路径 / 脚本参数 JSON / 超时** 控件；脚本路径必须是仓库内相对路径；
- **测试签到**：按当前三维字段跑一次完整签到，当场看结果；
- **sub2api 三种常用登录方式**：
  - `access_token`：直接填浏览器 localStorage 的 `auth_token` / `access_token`；它会先尝试 `/v1/usage`，若返回 `INVALID_API_KEY` 则视为“不是专用 API Key”并回退前端登录态接口；过期后不会隐式读取 OAuth；
  - `browser`：填站点级 `browser_state`，每次先刷新 `auth_token` 再执行余额/登录态查询；
  - `oauth`：选择 OAuth provider/account，每次先用该共享登录态刷新 `auth_token` 再执行余额/登录态查询；
  - Sub2API 的 check-in 接口属于站点 fork/扩展能力；若站点未开放，程序会返回“余额查询成功但未开放签到接口”，不会再误判为 `need_login`；
- **代理**：支持在「认证凭据」区填写 proxy 字段（如 `http://user:pass@host:port`），浏览器操作会自动使用。
- `保存全部` 写回 `ACCOUNTS.json`；`导出 Secret` 把整份 JSON 复制到剪贴板。

## 浏览器一键采集凭据（newapi / sub2api）

在已登录的站点页面打开 F12 → Console，粘贴 `collector.js` 内容回车。脚本自动识别站点类型，输出可直接粘贴到 `ACCOUNTS.json` 的账号配置块。

Sub2API 站点会从 localStorage 读取 `auth_token` / `access_token` / `token`，并尝试调用 `GET /v1/usage` 探测余额。若返回 `INVALID_API_KEY`，通常表示 `/v1/usage` 需要站点生成的专用 API Key，而不是前端登录 token；主程序会继续回退到 `/api/v1/auth/me`、check-in 状态等前端登录态接口，避免误判登录失效。

## CLI：捕获与测试浏览器登录态

**browser/poc_oauth.py** CLI 工具，用于首次捕获登录态或本地测试：

```bash
# 首次捕获：有头浏览器人工登录第三方 provider/account，完成后打印共享 oauth_state
python browser/poc_oauth.py setup --oauth-provider linuxdo --oauth-account default
python browser/poc_oauth.py setup --oauth-provider github --oauth-account default

# 测试：注入共享态后自动重放 OAuth，观察额度变化
# Windows PowerShell 示例：$env:CHECKIN_BROWSER_STATE="<oauth_states.linuxdo.accounts.default.state>"
python browser/poc_oauth.py run --base-url https://agentrouter.org --oauth-provider linuxdo --oauth-account default --user-id 68124

# 可选参数：
#   --proxy http://user:pass@host:port   使用代理（支持 http/https/socks5）
```

**代理支持**：
- Camoufox 支持 http/https/socks5 代理。
- 在 ACCOUNTS.json 中为站点添加 `"proxy": "http://user:pass@host:port"` 字段。
- CLI 用 `--proxy` 参数；GUI 在「认证凭据」区填写 proxy 字段。

**CI 里用 Clash 配置文件起本地代理（CLASH_CONFIG）**：

如果你手上是 mihomo/Clash 的配置文件（含 proxies 节点、proxy-groups、rules），而不是订阅 URL，可以让 CI 起一个本地 mihomo 服务，再让需要过 WAF 的站点走它。

1. 把**完整配置文件内容**放进仓库 Secret `CLASH_CONFIG`（Settings → Secrets and variables → Actions）。配置里含节点密码，属敏感信息，必须用 Secret，切勿提交进仓库。
2. workflow 会在签到前执行 `ci/setup_proxy.sh`：下载 mihomo、写入你的配置、**剥离配置里的顶层监听/管控项**（`mixed-port`/`port`/`socks-port`/`allow-lan`/`bind-address`/`external-controller` 等）再统一注入 `mixed-port: 7897` 并关闭 external-controller，后台启动并做健康检查。签到结束后 `stop_proxy.sh` 会关闭它。
   - 之所以要剥离：mihomo 严格拒绝重复的顶层 key，你配置里若已有 `mixed-port` 会直接报 `mapping key "mixed-port" already defined` 校验失败。脚本只删**行首无缩进**的同名 key，`proxies`/`rules` 里缩进的同名字段（如节点的 `port`）不受影响。
3. 在 ACCOUNTS.json 里，给需要过 WAF 的站点填 `"proxy": "http://127.0.0.1:7897"`；其它站点留空即直连。端口固定为 `7897`（mixed-port，http/socks5 通用）。

合法的 `CLASH_CONFIG` 最小样例（顶层监听项可留可不留，脚本都会覆盖；重点是 `proxies` + `proxy-groups` + `rules` 三段齐全）：

```yaml
# 顶层监听/管控项可省略，脚本会强制注入 mixed-port: 7897 并关闭 external-controller
mode: rule
log-level: warning

proxies:
  - name: "residential-hk"
    type: ss                       # 按你的节点类型：ss / vmess / trojan / hysteria2 等
    server: your.host.example.com
    port: 12345                    # 注意这是节点端口（缩进在 proxies 下，不会被剥离）
    cipher: aes-256-gcm
    password: "your-node-password"
    udp: true

proxy-groups:
  - name: "checkin"                # 供 rules 指向；select 默认用列表第一个节点
    type: select
    proxies:
      - "residential-hk"

rules:
  - MATCH,checkin                  # 所有流量都走 checkin 组（最简单：全量走代理）
```

> 单节点直接把 `rules` 写成 `- MATCH,residential-hk` 也可以，省掉 proxy-groups。多节点想固定用某一个，就用上面的 `select` 组并把目标节点放在列表第一位（CI 无 GUI，运行时不能交互切换，靠配置顺序决定）。

行为说明：
- 未设置 `CLASH_CONFIG` → 脚本打印跳过并正常退出，不影响直连站点。
- 代理起不来时默认仅告警并跳过（站点回退直连）；若希望「代理失败即让整个 job 失败」，给该步骤加 `PROXY_REQUIRED: 'true'`。
- 可选 `MIHOMO_VERSION` 覆盖回退版本（默认脚本先取 mihomo 最新 release，失败再回退固定版本）。

> ⚠️ **节点 IP 质量决定成败**：阿里云 WAF 按出口 IP 信誉风控，数据中心节点照样过不了，必须使用住宅/家宽（优先中国大陆/亚太）节点。

**阿里云 WAF 与出口 IP 信誉（重要）**：
- 新版阿里云 WAF（`aliyun_waf_aa`/`aliyun_waf_bb` 挑战）不是可离线复现的纯 JS 算法，而是「客户端环境信号 + 出口 IP 信誉」的联合风控。数据中心/CI 机房 IP（如 GitHub Actions）信誉分极低，即使用真实有头浏览器也常年过不了 JS 挑战。
- 表现：日志出现 `WAF 挑战求解失败` 反复重试、`status=200 ... waf=True` 且响应体含 `aliyun_waf_aa`。此时签到会返回 `need_verification`，提示「出口 IP 被持续风控」——**这不是登录态失效，无需重新捕获登录态**。
- 代码内置 WAF 熔断：连续 2 次整轮求解失败即判定 IP 被风控，短路后续所有重复求解，单账号从约 6 分钟空耗降到约 1 分钟内快速失败。
- 解法：为该账号配置**住宅代理**（proxy 字段，优先中国大陆/亚太住宅 IP），或把签到挪到住宅 IP 环境（自托管 runner / 家宽机器）运行。纯换 UA/header 对阿里云 WAF 的 IP 信誉判定无效。

**Playwright Firefox 驱动崩溃补丁（CI 自动执行）**：
- 现象：站点前端 JS 抛未捕获错误（如 `获取公告失败`）时，Playwright 1.60.0 的驱动在处理该 pageError 时因 `pageError.location` 为 undefined 直接崩溃整个 Node 进程，日志出现 `TypeError: Cannot read properties of undefined (reading 'url')` 与 `Connection closed while reading from the driver`，导致整轮签到中断。
- 这是 Playwright 自身的 bug，无法从页面层拦截。`ci/patch_playwright.py` 会把驱动 `coreBundle.js` 里的 `pageError.location.url` 等改为可选链（`pageError.location?.url`）使其空安全。
- workflow 在装完浏览器、跑签到前自动执行该补丁。脚本幂等（已打过就跳过）、best-effort（找不到文件也不报错）。由于 `uv sync` 重装会还原驱动，该步骤每次都运行、不加缓存条件。
- 本地手动跑浏览器签到若遇同样崩溃，执行一次 `python ci/patch_playwright.py` 即可（重装 playwright 后需重跑）。

## 运行

```bash
python run__all_checkin.py            # 执行所有启用站点；每个站点独立计算结果并写入 results/checkin_result.json
python run__all_checkin.py --verbose  # 额外打印每个任务的完整原始输出（已脱敏）
python checkin.py                     # 直接读 ACCOUNTS.json（按三维字段路由）
# 临时签到单站点（三维字段 + 凭据）
python checkin.py --base-url https://x --site-profile newapi --auth-method access_token --checkin-action api --access-token xxx --user-id 123
python checkin.py --base-url https://x --site-profile sub2api --auth-method access_token --checkin-action api --access-token xxx
python checkin.py --base-url https://x --site-profile newapi --auth-method oauth --checkin-action browser_script --script scripts/checkin/100xlabs.py --script-args '{"checkin_text":"签到"}'
```
Windows 可双击 `run_all_checkin.bat`。

## 额度显示

所有额度统一显示为 USD：
- `newapi`：站点返回内部 quota，按 `/500000` 换算为美元；
- `sub2api`：优先使用 `GET /v1/usage`，按 `remaining ?? quota.remaining ?? balance` 识别余额，单位取 `unit ?? quota.unit ?? "USD"`；若该接口不可用，再回退到旧接口。Sub2API provider 标记 `quota_is_usd`，不会再按 New API quota 规则二次换算。

## GitHub Actions 部署

1. 本地用 GUI「捕获 OAuth 登录态」或 CLI `browser/poc_oauth.py setup --oauth-provider linuxdo/github --oauth-account default` 捕获共享态，写入 `ACCOUNTS.json` 顶层 `oauth_states.<provider>.accounts.<account>`；`browser` 登录方式仍填站点级 `browser_state`；
2. `导出 Secret`（GUI 或手动复制），把整份 JSON 存为仓库 Secret：
   - 名称：`ACCOUNTS`
   - 值：`ACCOUNTS.json` 完整内容
3. `.github/workflows/auto_checkin.yml` 运行时自动：
   - 用 **ubuntu-latest** runner + **xvfb 虚拟显示**跑**有头模式**（`CHECKIN_HEADLESS=false`）；
   - **为什么有头**：阿里云 WAF 的 JS 挑战对无头浏览器敏感，无头会被识别拒绝放行，必须有头才能通过；
   - 用 **uv** 快速安装依赖（pyproject.toml）；
   - 安装 **Camoufox 浏览器**（`camoufox fetch`，有缓存）；
   - 把 Secret 还原为 `ACCOUNTS.json` 后用 `xvfb-run` 执行签到。

> **注意**：
> - 登录态（`oauth_states` / `browser_state`）会过期，过期后报 `need_login`，需重新捕获并更新 Secret；
> - relogin 站点只保存 `oauth_provider` + `oauth_account`，不要再内联保存站点级 `browser_state`；共享态需包含对应 provider（linux.do 或 GitHub）的登录 Cookie。
> - 浏览器默认有头运行；只有设置 `CHECKIN_HEADLESS=true` 才启用无头。

## 安全要点

- `ACCOUNTS.json` / `results/` / `.browser_profile/` / `login_grant_state.json` 均已被 `.gitignore` 忽略；
- `oauth_states.*.accounts.*.state` / `browser_state` 为未加密的 base64（storage_state JSON），其保护依赖 ACCOUNTS.json 不入库 + GitHub Secret 加密存储（与 cookie/access_token 同级）；
- 控制台与 CI 日志对 `session` / `Bearer` / `cf_clearance` 等脱敏（`mask_utils.py`）；
- Cloudflare / 阿里云 WAF 自动绕过（cf_clearance / WAF cookies），若失败报 `need_verification`。滑块验证自动拖拽，复杂验证码需人工完成后重新捕获。
