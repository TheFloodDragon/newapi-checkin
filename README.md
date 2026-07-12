# checkin —— 公益站自动签到

轻量、以标准库为主的自动签到调度器，覆盖 New API / Sub2API 系公益站。支持接口签到、访问保活、OAuth 重登发额度，以及自定义浏览器脚本；浏览器路径使用 [Camoufox](https://camoufox.com)（Firefox 反检测浏览器）突破 Cloudflare / 阿里云 WAF。

本项目以 [MIT License](LICENSE) 开源。

## 快速开始

```bash
# 1. 安装依赖（推荐 uv，使用锁定版本）
uv sync
# 浏览器路径（browser / oauth / relogin / browser_script）首次运行前拉取 Camoufox
uv run python -m camoufox fetch

# 2. 从示例生成本地凭据文件（ACCOUNTS.json 已被 .gitignore，不会入库）
cp ACCOUNTS.example.json ACCOUNTS.json
# 按需编辑 ACCOUNTS.json，填入各站点的 access_token / cookie / oauth_states

# 3. 运行所有启用站点
uv run python run__all_checkin.py
```

> ⚠️ **凭据安全**：`ACCOUNTS.json`、`oauth_states`、`browser_state` 等含敏感登录态，已在 `.gitignore` 中忽略，**切勿提交进仓库**。GitHub Actions 部署时改为存入仓库 Secret `ACCOUNTS`（见「GitHub Actions 部署」）。

## 依赖与版本锁定

依赖在 `pyproject.toml` 中被**精确锁定**，并生成 `uv.lock`，保证本地与 CI 完全一致：

| 用途 | 包 | 版本 |
|------|----|------|
| 运行时 | `camoufox[geoip]` | `0.4.11` |
| 运行时 | `playwright-captcha` | `0.1.5` |
| GUI（可选 `--extra gui`） | `PySide6` | `6.11.1` |
| 开发（可选 `--extra dev`） | `pytest` | `8.4.2` |
| 开发（可选 `--extra dev`） | `ruff` | `0.15.21` |

```bash
uv sync                 # 运行时依赖
uv sync --extra gui     # 额外安装 GUI（PySide6）
uv sync --extra dev     # 额外安装测试/静态检查工具
uv sync --locked        # 严格按 uv.lock 安装（CI 使用）
```

challenge 新版签到需要本机 **Node.js**（执行 WASM PoW，见 `checkin_challenge.js`）；缺失时会退回 legacy 接口或提示安装。

## 数据模型：三个正交维度

签到能力拆成三个互相独立、可自由组合的维度：

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
| newapi | browser | api | 阿里云 WAF 站点混合签到：站点级 `browser_state` 启浏览器过 WAF 导出 cookie，再用 HTTP 发签到请求 |
| sub2api | access_token | api | Sub2API 前端登录 token / 可选 API Key；优先试 `/v1/usage`，`INVALID_API_KEY` 时回退前端登录态接口 |
| sub2api | browser | api | Sub2API 每次先用站点级 `browser_state` 刷新 auth_token 再查询 |
| sub2api | oauth | api | Sub2API 每次先用选定 OAuth 账号刷新 auth_token 再查询 |
| newapi | access_token / cookie | visit | 无签到接口、登录即发额度站点的「保活 + 余额监控」 |
| newapi | oauth | relogin | 浏览器自动重放第三方 OAuth 登录，**真正触发发额度** |
| newapi / sub2api | oauth / browser | browser_script | 恢复浏览器登录态后执行仓库内自定义 Python 脚本 |

代码组织（`providers/` 包）：

- `base.py`     —— 共用模型（`SiteConfig` / `ProfileClient` / `CheckinResult` 等）、HTTP 与文本工具；
- `auth.py`     —— 登录方式：把凭据加载/规范化为统一 `AuthInfo`；
- `profiles/`   —— 站点适配器（`newapi.py` / `sub2api.py`），只管「接口长什么样」；
- `actions/`    —— 签到方式（`api.py` / `relogin.py` / `visit.py` / `browser_script.py`），只管「如何触发发额度」；
- `__init__.py` —— 组装入口：`run_checkin` 按 `site_profile` 选 profile，按 `checkin_action` 执行动作，动作内部按 `auth_method` 准备认证。

`SiteConfig` 的构造统一收敛到 `accounts_store.site_config_from_mapping()`，CLI、批量调度与 GUI 共用同一条规范化路径。

> **旧字段自动迁移**：旧配置的 `type` + `checkin_mode` 会在读取时自动映射为新三维字段并**写回** `ACCOUNTS.json`（`legacy/challenge → api`，`login_grant → visit`，`browser_oauth → oauth+relogin`；`challenge/legacy` 顺带写入 `api_variant`）。旧版 `oauth_states.provider.state` 会迁移为 `oauth_states.provider.accounts.default.state`。

## 配置文件（ACCOUNTS.json）

`ACCOUNTS.json` 统一保存站点配置、启用状态与凭据，已被 `.gitignore` 忽略，仅本地或 GitHub Secret 中保存。参见 `ACCOUNTS.example.json`。

**三维字段全集**：

```json5
{
  "accounts": [
    {
      "name": "某 New API 站",
      "base_url": "https://example.com",
      "site_profile": "newapi",              // 站点适配器：newapi / sub2api
      "auth_method": "cookie",               // 登录方式：access_token / cookie / browser / oauth
      "checkin_action": "api",               // 签到方式：api / visit / relogin / browser_script
      "api_variant": "auto",                 // 接口变体偏好（仅 newapi + api）：auto / legacy
      "enabled": true,
      "cookie": "session=xxx",               // Cookie（auth_method=cookie 时必填）
      "access_token": "eyJ...",              // Bearer token（auth_method=access_token 时必填）
      "user_id": "1234",                     // newapi 的 New-Api-User 请求头
      "proxy": "http://user:pass@host:port"  // 可选：HTTP 签到仅支持 http/https 代理
    },
    {
      "name": "Sub2API",
      "base_url": "https://sub.100xlabs.space",
      "site_profile": "sub2api",
      "auth_method": "access_token",
      "checkin_action": "api",
      "access_token": "eyJhbGc...",
      "user_id": "19653"
    },
    {
      "name": "AgentRouter 站",
      "base_url": "https://agentrouter.org",
      "site_profile": "newapi",
      "auth_method": "oauth",
      "checkin_action": "relogin",           // OAuth 重登签到（真正触发发额度）
      "oauth_provider": "linuxdo",           // linuxdo / github
      "oauth_account": "default"             // provider 下的账号名；登录态存顶层 oauth_states
    },
    {
      "name": "浏览器脚本站",
      "base_url": "https://example.100xlabs.com",
      "site_profile": "newapi",
      "auth_method": "oauth",
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
        "default": { "state": "eyJvcmlnaW4iOi...", "username": "", "updated_at": "2026-07-05T12:00:00" }
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

支持的顶层形态：`{"accounts": [...]}`、`{"accounts": {"站点名": {...}}}`、`[...]`。旧对象格式与 `type` + `checkin_mode` 仍能识别并自动迁移。

**配置读写的健壮性约定**：

- **损坏即失败**：`ACCOUNTS.json` 存在但不是合法 JSON（例如上次写入被中断）时，读取会抛 `ConfigError` 并附带清晰提示，而**不会**静默当作空配置或回退——避免下次保存覆盖真实数据。
- **原子写入 + 文件锁**：账号、共享 OAuth 登录态、额度状态（`login_grant_state.json`）、汇总结果与 CI 报告的写入均采用「同目录临时文件 + `fsync` + `os.replace`」并持有跨进程文件锁，并发签到不会互相覆盖（lost update）。
- **保留未知元数据**：写回时保留 `ACCOUNTS.json` 顶层未知字段与账号条目里的自定义字段，不会丢失。
- **唯一身份更新**：刷新 token / 登录态回写时要求 `name` + `base_url` 唯一定位账号；出现歧义会拒绝写入而非误改。

## 代理与安全模型

**代理**：

- **HTTP 签到路径**（`api` / `visit`）使用标准库 `urllib`，仅支持 **http/https 代理**；填入 `socks5://` 会被明确拒绝并提示。代理通过显式 `ProxyHandler` 注入，不隐式继承进程级环境代理。
- **浏览器路径**（`browser` / `oauth` / `relogin` / `browser_script`）由 Camoufox 驱动，支持 http/https/socks5 代理。
- 站点未配 `proxy` 时回退到全局 `CHECKIN_PROXY`（CI 可从 Secret 注入住宅代理）。

**凭据与脱敏**：

- 批量调度器把 cookie / access_token / user_id / proxy 等敏感值通过**环境变量**传给子进程，**不出现在命令行参数**中（避免进程列表泄露）。
- 子进程 worker 的 **stdout 是机器协议通道**：只输出单行紧凑 JSON；所有诊断/日志改走 stderr。
- 打印到控制台、写入结果 JSON 与 CI Markdown 报告前，统一经 `mask_utils` 脱敏，覆盖 Cookie、`Bearer`、`Authorization`、JWT、`sk-*`、OAuth `state`、以及 URL 中的 `user:password@` 凭据；结果结构会递归清理敏感键。

## 运行

```bash
# 批量执行所有启用站点；每站点独立子进程，结果写入 results/checkin_result.json
uv run python run__all_checkin.py
uv run python run__all_checkin.py --verbose   # 额外打印每个任务的完整原始输出（已脱敏）

# 直接读 ACCOUNTS.json（按三维字段路由）
uv run python checkin.py

# 临时签到单站点（三维字段 + 凭据）
uv run python checkin.py --base-url https://x --site-profile newapi --auth-method access_token --checkin-action api --access-token xxx --user-id 123
uv run python checkin.py --base-url https://x --site-profile sub2api --auth-method access_token --checkin-action api --access-token xxx
```

Windows 可双击 `run_all_checkin.bat`。

> **worker 协议**：`run__all_checkin.py` 为每个站点启动 `checkin.py --worker` 子进程，读取其 stdout 的单行 JSON 结果，并严格校验字段、状态合法性与退出码一致性；协议不符或退出码与状态矛盾会被判为失败，而不是根据「退出码 0」猜成功。

## 额度显示

所有额度统一显示为 USD：

- `newapi`：站点返回内部 quota，按 `/500000` 换算为美元；
- `sub2api`：优先使用 `GET /v1/usage`，按 `remaining ?? quota.remaining ?? balance` 识别余额，单位取 `unit ?? quota.unit ?? "USD"`；不可用时回退前端登录态接口。`quota_is_usd` 标记确保不会二次换算。

## 登录即发额度类站点（如 AgentRouter）

部分中转站**没有任何独立签到接口**，额度是在**第三方 OAuth 登录（Linux.do / GitHub）回调时发放**。两种应对方式：

### `visit`：保活 + 余额监控（不触发发放）

纯标准库 HTTP，不引入浏览器：调 `/api/user/self` 保活并读额度，持久化到 `login_grant_state.json`（原子写 + 锁）跨次对比增量；额度增长 → `success`，无变化 → `already_done`，登录失效 → `need_login`。它**不触发发放**，真正领取仍需在浏览器手动登录一次。

### `relogin`：浏览器自动重放 OAuth（真正触发发放）

用 Camoufox 复用顶层共享的第三方登录态，优先走站点前端 `/login` / `/register` 的 OAuth 按钮，失败时按 `/api/status` + `/api/oauth/state` 直连拼出 Linux.do / GitHub 授权 URL，完成 OAuth 回调触发发额度，再读 `/api/user/self` 对比前后额度。

核心浏览器逻辑集中在 `browser/` 子包（CLI 与 GUI 共享）：

- `browser/session.py` —— capture / verify / oauth checkin 共享逻辑（**async**，基于 Camoufox）；`run_sync()` 在已有运行中事件循环时会切到独立线程执行，避免死锁；
- `browser/state.py`   —— 登录态编码/解码（storage_state → base64(gzip(json))），带严格 base64 校验、限长流式解压（防 zip bomb）与 schema 校验；
- `browser/bypass.py`  —— 绕过引擎（Camoufox 启动、Cloudflare cf_clearance、WAF cookies、滑块拖拽）；
- `browser/popups.py`  —— 弹窗自动关闭守卫（MutationObserver 注入）；
- `browser/script_runner.py` / `browser/script_helpers.py` —— browser_script 运行器与脚本便捷 helper；
- `browser/poc_oauth.py` —— 命令行入口（setup / run）。

**登录态格式**：Playwright storage_state（跨平台 JSON，含 cookies + localStorage），经 gzip + base64 压缩、未加密，保护依赖 `.gitignore` + GitHub Secret（与 cookie/access_token 同级）。

**⚠️ relogin 站点不保存站点级 `browser_state`。** Linux.do / GitHub 登录态统一保存在顶层 `oauth_states[provider].accounts[oauth_account]`，多站点可通过各自的 `oauth_provider` + `oauth_account` 复用。共享态过期时，OAuth 重放会停在第三方登录页并报 `need_login`。

### `browser` + `api`：过 WAF 拿 cookie + HTTP 签到

针对**有签到接口、但被阿里云 WAF 挡住纯 HTTP** 的 New API 站点：用站点级 `browser_state` 启动 Camoufox 过 WAF、导出 `acw_tc` 等 WAF cookie 与 session cookie，立刻关闭浏览器，再把 cookie 交给 HTTP `api` 逻辑完成签到。比 `relogin` 快、比纯 `cookie` 更能过 WAF。WAF 持续风控时快速失败并返回 `need_verification`（提示配置住宅代理），不误报 `need_login`；刷新出的 storage_state 会回写 `browser_state` 供复用。

### `browser_script`：仓库内自定义浏览器脚本

适合没有稳定接口、但页面上有「签到/领取」按钮的站点。程序按 `auth_method` 恢复登录态、启动 Camoufox、**只允许加载仓库内相对路径 Python 文件**（禁止绝对路径、`..`、URL），调用脚本的 `run(page, context, site, helpers)` 并返回 `{status, message, detail}`。内置示例：`scripts/checkin/100xlabs.py`。脚本可直接用 Playwright 的 `page/context`，也可用 `helpers.goto()` / `helpers.click_text()` / `helpers.screenshot()` / `helpers.success()` 等便捷方法。

`auth_method=browser` 时可额外选择 `oauth_fallback_provider` + `oauth_fallback_account`，也可保持“不使用”：程序始终优先使用站点级 `browser_state`，登录态缺失或脚本明确返回 `need_login` 时，最多用共享 OAuth 登录态自动完成一次站点登录并重试脚本；若未选择 OAuth，则直接报告站点登录态失效、签到失败。OAuth 登录期间会在目标站点自动关闭公告、协议、守则、须知等遮挡弹窗，但不会在 Linux.do / GitHub 授权页启用该规则。

## 图形界面（GUI）

```bash
uv sync --extra gui
uv run python manage_accounts.py
```

- 左侧站点列表，右侧编辑「站点配置 + 凭据」：选择站点类型、登录方式与签到方式（三维字段）；
- `auth_method=oauth` 或 `checkin_action=relogin` 时出现 OAuth 提供商 + 账号控件，可**捕获 / 检测 OAuth 登录态**；捕获结果先加入当前内存配置并显示“未保存”，点击 `保存全部` 后写入顶层 `oauth_states`；relogin 站点不保存站点级 `browser_state`；
- `auth_method=browser` 时出现站点级「浏览器登录态」输入区；自定义浏览器脚本还会显示「可选 OAuth」，直接列出顶层共享 `oauth_states` 中已有的账号；
- `checkin_action=browser_script` 时出现脚本路径 / 参数 JSON / 超时控件（路径必须是仓库内相对路径）；
- **测试签到 / 查询额度**：按当前三维字段跑一次，当场看结果；
- **代理**：在「认证凭据」区填 `proxy`；
- `保存全部` 写回 `ACCOUNTS.json`（原子写 + 锁）；`导出 Secret` 把整份 JSON 复制到剪贴板。

## 浏览器一键采集凭据（collector.js）

在已登录的站点页面打开 F12 → Console，粘贴 `collector.js` 内容回车。脚本自动识别站点类型（newapi / sub2api），输出可粘贴到 `ACCOUNTS.json` 的账号配置块。Sub2API 会从 localStorage 读取 `auth_token` 并探测 `/v1/usage`；返回 `INVALID_API_KEY` 属正常（表示需要专用 API Key，而非前端 token）。

## CLI：捕获与测试浏览器登录态

```bash
# 首次捕获：有头浏览器人工登录第三方 provider/account，完成后打印共享 oauth_state
uv run python browser/poc_oauth.py setup --oauth-provider linuxdo --oauth-account default
uv run python browser/poc_oauth.py setup --oauth-provider github  --oauth-account default

# 测试：注入共享态后自动重放 OAuth，观察额度变化
uv run python browser/poc_oauth.py run --base-url https://agentrouter.org --oauth-provider linuxdo --oauth-account default --user-id 68124

# 可选：--proxy http://user:pass@host:port（浏览器路径支持 http/https/socks5）
```

## GitHub Actions 部署

`.github/workflows/auto_checkin.yml` 每日定时运行：

1. 用 `astral-sh/setup-uv` 固定 uv 版本 + `actions/setup-python` **Python 3.12**；
2. `uv sync --locked --extra dev` 按 `uv.lock` 严格安装；
3. **质量门**：`ruff check .` + `pytest` + `compileall`，任一失败即让 job 失败；
4. 把 Secret `ACCOUNTS` **原子写入** `ACCOUNTS.json` 并设 `0600` 权限；
5. `ci/detect_browser.py`（`python -m ci.detect_browser`）判断是否存在浏览器任务；配置解析失败会直接失败，不静默判为「无需浏览器」；
6. **仅当需要浏览器任务**时才安装 xvfb / Camoufox、执行 Playwright Firefox 驱动补丁、并用 `xvfb-run` 有头运行；纯 HTTP 任务直接运行；
7. 可选 Clash/mihomo 本地代理（Secret `CLASH_CONFIG`，见 `ci/setup_proxy.sh`）；
8. 生成脱敏后的 `checkin_report.md` 输出到 Step Summary。

部署步骤：本地用 GUI「捕获 OAuth 登录态」或 `browser/poc_oauth.py setup` 生成共享态 → `导出 Secret` → 存为仓库 Secret `ACCOUNTS`（值为 `ACCOUNTS.json` 完整内容）。

> **注意**：
> - 登录态（`oauth_states` / `browser_state`）会过期，过期后报 `need_login`，需重新捕获并更新 Secret；
> - relogin 站点只保存 `oauth_provider` + `oauth_account`，不要内联站点级 `browser_state`；
> - 浏览器默认有头运行（阿里云 WAF 对无头敏感）；设 `CHECKIN_HEADLESS=true` 才无头。

### 阿里云 WAF 与出口 IP 信誉（重要）

新版阿里云 WAF（`aliyun_waf_aa` / `aliyun_waf_bb` 挑战）按「客户端信号 + 出口 IP 信誉」联合风控。数据中心 / CI 机房 IP（如 GitHub Actions）信誉极低，即使真实有头浏览器也常年过不了。表现为日志反复 `WAF 挑战求解失败`、`status=200 ... waf=True`。此时签到返回 `need_verification`（**不是登录态失效**）。代码内置 WAF 熔断：连续 2 次整轮失败即判定 IP 被风控，短路后续求解，快速失败。解法：为该账号配置**住宅代理**（优先中国大陆 / 亚太），或改在住宅 IP 环境运行。

### Playwright Firefox 驱动崩溃补丁

Playwright 1.6x Firefox 驱动在处理缺少 `location` 的 pageError，或在请求结束事件中拿到空的 `_existingResponse()` 时，可能崩溃整个 Node 进程。`ci/patch_playwright.py`（`python -m ci.patch_playwright`）会同时修复 `pageError.location` 与 `response2.setTransferSize` 空引用；补丁幂等、best-effort，并会在本地启动 Camoufox 前自动尝试一次。`uv sync` 重装会还原驱动，故 CI 每次运行前仍会执行。

## 开发与验证

```bash
uv sync --extra dev
uv run pytest                       # 单元/回归测试（tests/）
uv run ruff check .                 # 静态检查
uv run python -m compileall -q .    # 语法编译
uv lock --check                     # 校验 uv.lock 与 pyproject 同步
```

`tests/` 覆盖 worker 协议、HTTP 代理与幂等重试、原子存储与并发、browser state 校验、脱敏与报告安全等关键路径。

## 安全要点

- `ACCOUNTS.json` / `results/` / `.browser_profile/` / `login_grant_state.json` / `*.lock` 均已被 `.gitignore` 忽略；
- `oauth_states.*.accounts.*.state` / `browser_state` 为未加密 base64（storage_state JSON），保护依赖不入库 + GitHub Secret 加密存储；
- 控制台、结果文件与 CI 报告统一脱敏（`mask_utils.py`）；
- Cloudflare / 阿里云 WAF 自动绕过，失败报 `need_verification`；滑块自动拖拽，复杂验证码需人工完成后重新捕获。
