# newapi-checkin

面向 **New API / Sub2API 系中转站**的自动签到、额度查询与登录态管理工具。

项目把站点差异、认证方式和签到动作拆成三个独立维度，优先使用纯 HTTP；只有在 Token 续期、OAuth 重登、Cloudflare / 阿里云 WAF 或页面交互确实需要时，才启动 Camoufox 浏览器。

- 纯 API 签到、余额查询与访问保活
- New API challenge / legacy 双流程按错误类型条件化回退
- Sub2API Token、Refresh Token、账密与浏览器多级降级
- Linux.do / GitHub OAuth 登录态共享与自动重登
- 仓库内 Python 站点脚本：纯 HTTP 钩子与浏览器钩子
- New API 图形验证码离线识别
- 极速蹬签到后每日答题
- 深浅主题图形管理界面
- GitHub Actions 定时签到、缓存与脱敏报告

> [!WARNING]
> `ACCOUNTS.json`、OAuth 登录态、`browser_state`、Token 和 Cookie 都属于敏感凭据。它们已被 `.gitignore` 忽略，但仍应只保存在本机或 GitHub Secret 中，切勿提交、截图或公开转发。

---

## 1. 选择适合的运行方式

| 场景 | 推荐配置 | 是否启动浏览器 |
|---|---|---:|
| 站点有稳定签到接口，已有 Token / Cookie | `access_token` 或 `cookie` + `api` | 否 |
| New API fork 的签到要求图形验证码 | `access_token` + `api` + `scripts/newapi_captcha.py` | 否 |
| New API 接口被阿里云 WAF 拦截 | `browser` + `api` | 需要，用于过 WAF 和导出 Cookie |
| 站点没有签到接口，只需保活和监控额度 | `access_token` / `cookie` + `visit` | 否 |
| 额度在第三方 OAuth 登录回调时发放 | `oauth` + `relogin` | 需要 |
| 站点只有页面签到按钮或私有交互 | `browser` / `oauth` + `browser_script` | API 可完成时不启动，否则启动 |
| Sub2API Token 可能过期 | 配置 `access_token` + `refresh_token` | 通常不需要 |

浏览器脚本并不等于“每次都开浏览器”。`browser_script` 会先尝试纯 API、Refresh Token 和可用的纯 HTTP 账密登录；只有这些路径都不能给出明确结果时才启动浏览器。

---

## 2. 快速开始

### 2.1 环境要求

- Python **3.11+**
- 推荐使用 [uv](https://docs.astral.sh/uv/)
- 使用 GUI：安装 `gui` extra
- 使用图形验证码脚本或运行测试：安装 `dev` extra（其中包含 Pillow）
- 使用浏览器流程：下载 Camoufox 浏览器
- 使用 New API challenge 流程：本机需要 Node.js；未安装时应安装 Node.js，或把对应站点显式设为 `api_variant=legacy`

### 2.2 安装

```bash
# 基础运行环境
uv sync

# 图形管理界面
uv sync --extra gui

# 测试工具 + Pillow（scripts/newapi_captcha.py 需要）
uv sync --extra dev

# browser / oauth / relogin / browser_script 首次运行前执行一次
uv run python -m camoufox fetch
```

`uv.lock` 固定了实际解析版本。`pyproject.toml` 当前主要依赖如下：

| 用途 | 依赖 |
|---|---|
| 浏览器自动化 | `camoufox[geoip]==0.4.11` |
| CAPTCHA / 数值计算 | `numpy>=2.4.6` |
| CAPTCHA 交互 | `playwright-captcha==0.1.5` |
| GUI extra | `PySide6==6.11.1` |
| dev extra | `pillow>=12.3.0`、`pytest==8.4.2`、`ruff==0.15.21` |

### 2.3 创建配置

Git Bash / Linux / macOS：

```bash
cp ACCOUNTS.example.json ACCOUNTS.json
```

PowerShell：

```powershell
Copy-Item ACCOUNTS.example.json ACCOUNTS.json
```

推荐直接打开管理界面完成配置：

```bash
uv run python manage_accounts.py
```

也可以手工编辑 `ACCOUNTS.json`，然后批量执行：

```bash
uv run python run__all_checkin.py
```

Windows 还可双击 `run_all_checkin.bat`。

---

## 3. 图形管理界面

```bash
uv sync --extra gui
uv run python manage_accounts.py
```

新版管理界面由 `gui/` 包实现，`manage_accounts.py` 只是薄入口。界面采用组件化布局和统一主题令牌：

- **顶栏**：应用标题、深浅主题切换、已保存 / 未保存状态；
- **概览条**：启用站点、今日已签、已知总额度、异常数，以及“全部查询”“全部签到”；
- **站点侧栏**：搜索、拖拽排序、启停、签到状态与额度摘要；
- **站点摘要卡**：当前额度、今日状态、复制、删除、立即签到；
- **双栏编辑区**：站点信息与认证凭据按约 40/60 排列；
- **折叠日志区**：后台日志实时进入主界面，可记忆展开状态；
- **底部操作栏**：重新加载、导出 GitHub Secret、保存全部；
- **交互反馈**：按钮加载状态、列表与统计过渡动画、输入焦点光效，以及 info / success / error Toast。

常用快捷键：

| 快捷键 | 操作 |
|---|---|
| `Ctrl+S` | 保存全部 |
| `Ctrl+N` | 新增站点 |
| `Ctrl+L` | 重新加载 |
| `Delete` | 删除当前站点 |

表单会根据三个运行维度动态显示字段：

- `newapi + api`：显示 `api_variant`；
- `api`：可选填写纯 HTTP 脚本路径；
- `browser_script`：必须填写脚本路径，并显示 `script_args` 与 `script_timeout`；
- `browser`：显示站点级 `browser_state` 和浏览器操作；
- `oauth` / `relogin`：显示共享 OAuth provider / account；
- 支持的场景可额外选择 OAuth fallback；
- Sub2API 的 Access Token / Refresh Token 属于接口凭据，即使认证方式选择 browser / oauth 也仍可编辑。

脚本路径会在保存前校验：只允许仓库内相对 `.py` 文件，拒绝 URL、绝对路径和 `..`，并确认文件真实存在。

---

## 4. 配置模型

每个站点由三个正交字段决定运行方式：

| 维度 | 字段 | 可选值 | 作用 |
|---|---|---|---|
| 站点适配器 | `site_profile` | `newapi` / `sub2api` | 接口路径、请求头、响应解析和额度单位 |
| 认证方式 | `auth_method` | `access_token` / `cookie` / `browser` / `oauth` | 如何获得已认证会话 |
| 签到动作 | `checkin_action` | `api` / `visit` / `relogin` / `browser_script` | 如何触发签到或额度发放 |

### 4.1 常用字段

| 字段 | 说明 |
|---|---|
| `name` | 本地显示名称，同一配置内应唯一 |
| `base_url` | 站点根地址，自动规范化 |
| `enabled` | 是否参与批量签到 |
| `user_id` | New API 的 `New-Api-User` 请求头 |
| `access_token` | Bearer Token；Sub2API 浏览器流程也会优先使用它走纯 API |
| `refresh_token` | Sub2API 长效续期凭据 |
| `cookie` | `auth_method=cookie` 时使用 |
| `api_variant` | New API API 流程：`auto` 或 `legacy` |
| `script` | 仓库内相对 Python 脚本路径 |
| `script_args` | 仅 `browser_script` 的脚本参数对象 |
| `script_timeout` | 仅 `browser_script` 的运行超时 |
| `browser_state` | 站点级 Playwright storage state，gzip + base64 编码 |
| `oauth_provider` | `linuxdo` 或 `github` |
| `oauth_account` | 同一 provider 下的共享登录态名称 |
| `oauth_fallback_provider` | 主凭据失效后的可选 OAuth 兜底 |
| `proxy` | 单站点代理；未填时可回退全局 `CHECKIN_PROXY` |
| `verify_ssl` | 默认 `true`；仅证书异常时谨慎关闭 |
| `referer_path` | New API 请求的 Referer 路径，默认 `/profile` |
| `cookie_file` | 兼容三行凭据文件：Cookie / 用户 ID / Access Token |
| `browser_profile` | 浏览器持久化 Profile 路径前缀 |
| `auto_refresh_cookie` | 是否把去重后的 Cookie 回写凭据文件 |

### 4.2 最小配置示例

```json
{
  "accounts": [
    {
      "name": "普通 New API",
      "base_url": "https://newapi.example.com",
      "site_profile": "newapi",
      "auth_method": "access_token",
      "checkin_action": "api",
      "user_id": "10001",
      "access_token": "<access_token>",
      "enabled": true
    },
    {
      "name": "带签到验证码的 New API fork",
      "base_url": "https://captcha.example.com",
      "site_profile": "newapi",
      "auth_method": "access_token",
      "checkin_action": "api",
      "script": "scripts/newapi_captcha.py",
      "user_id": "10002",
      "access_token": "<access_token>",
      "enabled": true
    },
    {
      "name": "Sub2API 浏览器脚本站",
      "base_url": "https://sub.example.com",
      "site_profile": "sub2api",
      "auth_method": "browser",
      "checkin_action": "browser_script",
      "script": "scripts/checkin/jisudeng.py",
      "script_args": {
        "start_url": "/check-in"
      },
      "script_timeout": 120,
      "access_token": "<auth_token>",
      "refresh_token": "<refresh_token>",
      "browser_state": "<站点登录态>",
      "enabled": true
    },
    {
      "name": "OAuth 登录发额度站",
      "base_url": "https://router.example.com",
      "site_profile": "newapi",
      "auth_method": "oauth",
      "checkin_action": "relogin",
      "oauth_provider": "linuxdo",
      "oauth_account": "default",
      "user_id": "10003",
      "enabled": true
    }
  ],
  "oauth_states": {
    "linuxdo": {
      "accounts": {
        "default": {
          "state": "<共享 Linux.do storage_state>",
          "username": "",
          "updated_at": ""
        }
      }
    }
  }
}
```

支持的顶层形态：

- `{"accounts": [...]}`
- `{"accounts": {"站点名": {...}}}`
- `[...]`

旧版 `type` + `checkin_mode` 会自动迁移为新三维字段，并在安全写回时保留未知顶层元数据和账号自定义字段。旧版 `oauth_states.provider.state` 也会迁移为多账号结构。

---

## 5. 各签到动作的真实行为

### 5.1 `api`：接口签到

通用步骤：

1. 按 `auth_method` 准备 HTTP 凭据；
2. 查询今日签到状态；
3. 已签到则返回 `already_done`；
4. 可选调用站点脚本的 `do_checkin(client, log)`；
5. 脚本返回 `None` 时走 profile 默认签到；
6. 用奖励字段、签到后状态或前后额度差验证签到是否真的成立；
7. 补充当前额度并统一输出美元格式。

New API：

- `api_variant=auto`：challenge 优先，仅在端点不支持或特定网络失败时回退 legacy；
- `api_variant=legacy`：legacy 优先，仅在站点明确提示流程已升级时回退 challenge；
- 登录失败、验证码错误和普通业务拒绝不会被当作“换一种接口再试”；
- challenge 使用 `checkin_challenge.js` 和 Node.js 执行 WASM PoW。

Sub2API：

- 探测 `/api/v1/check-in`、`/api/v1/play/checkin` 等 fork 端点；
- 标准 Sub2API 没有签到接口时，完成登录态验证与余额查询，不伪造奖励；
- Access Token 登录失效时优先使用 Refresh Token 纯 HTTP 续期。

### 5.2 `api + script`：私有 HTTP 签到流程

API 脚本约定：

```python
def do_checkin(client, log=None):
    # 返回 CheckinReward 表示脚本已接管签到
    # 返回 None 表示回退 profile 默认签到
    ...
```

当前内置脚本：

```json
{
  "checkin_action": "api",
  "script": "scripts/newapi_captcha.py"
}
```

`scripts/newapi_captcha.py` 支持两类已知 New API fork 验证码方言：

- `POST /api/user/checkin/captcha` + `captcha_answer`；
- `GET /api/captcha?scene=checkin` + `captcha_code`。

脚本按图片尺寸选择离线识别器。读数不够可信时会重新取图，避免把一次性 `captcha_id` 浪费在硬猜上。详细算法和验收记录见 [`docs/captcha_algorithm.md`](docs/captcha_algorithm.md)。

> 使用该脚本需要 Pillow：执行 `uv sync --extra dev`。

### 5.3 `visit`：访问保活与额度监控

适用于没有签到接口、但访问用户接口可以保持登录或观察额度的站点。

- 请求用户接口并读取额度；
- 与 `.cache-checkin/login_grant_state.json` 中的历史值比较（根目录同名文件只用于旧版兼容读取）；
- 额度增加时返回 `success`；
- 无变化时返回 `already_done`，但不会声称它一定在今天领取；
- `visit` 本身不会触发 OAuth 登录奖励。

### 5.4 `relogin`：OAuth 重登触发发放

适用于“登录即发额度”的站点：

1. 复用顶层 `oauth_states` 中选定的 Linux.do / GitHub 登录态；
2. 优先点击站点前端 OAuth 登录入口；
3. 必要时根据 `/api/status` 和 `/api/oauth/state` 直连授权；
4. 完成回调后读取额度并综合页面成功提示、回跳状态和额度变化判断结果。

中途出现过 Cloudflare 挑战并不会自动否决结果；只要已经成功回到目标站点且存在成功证据，仍会判定成功。真正停在第三方登录页或 WAF 持续阻断时才返回失败状态。

### 5.5 `browser_script`：API 优先的浏览器脚本

浏览器脚本约定：

```python
async def run(page, context, site, helpers):
    return {
        "status": "success",
        "message": "签到成功",
        "detail": {}
    }
```

实际执行顺序：

1. 已保存 `access_token` 纯 HTTP 签到；
2. Token 失效时尝试 `refresh_token` 续期；
3. 脚本提供账密且站点未启用 Turnstile 时，尝试纯 HTTP 登录换 Token；
4. 仍无法完成时恢复站点 `browser_state`；
5. 必要时让脚本自行账密登录并处理 Turnstile；
6. 配置了 OAuth fallback 时，主登录态失效后最多再尝试一次共享 OAuth 登录态；
7. 运行结束后由通用运行器保存新的 storage state、Access Token 和 Refresh Token。

纯 HTTP 阶段成功后，脚本还可定义附加任务：

```python
def run_http_extras(client, log=None):
    return {
        "quiz": {
            "outcome": "submitted",
            "message": "每日答题完成"
        }
    }
```

附加任务结果写入 `detail`，失败不会推翻已经成立的签到结论。

---

## 6. 内置站点脚本

| 路径 | 类型 | 用途 |
|---|---|---|
| `scripts/newapi_captcha.py` | API 脚本 | New API fork 图形验证码签到 |
| `scripts/checkin/100xlabs.py` | 浏览器脚本 | 100xLabs 系页面签到与登录处理 |
| `scripts/checkin/jisudeng.py` | 浏览器脚本 + HTTP extras | 极速蹬签到、登录态刷新和每日答题 |

脚本统一经 `browser/script_loader.py` 加载：

- 只允许仓库内相对路径；
- 只允许 `.py` 文件；
- 禁止 URL、绝对路径和 `..`；
- 每次运行重新加载模块，避免执行旧缓存代码。

### 极速蹬每日答题

`jisudeng.py` 内置离线题库 `ANSWERS`。签到成功或今日已签后，会尝试完成每日 Quiz：

- 已收录题目按题面相似度匹配，再按相似度在选项里定位正确选项文本；
- 站点会在题面末尾追加「（第N题）」序号且同一题序号会变，比较前会剥掉；
- 相似度低于阈值判为新题，选项里定不到唯一答案时同样按新题处理；
- 新题按最长选项猜，记录题面和选项并写入 `.cache-checkin/play_quiz_unknown.json`；
- 结果写入 `detail.quiz`；
- 答题接口不可用或答题失败不会改变签到结论。

---

## 7. 凭据采集与登录态

### 7.1 `collector.js`

在已经登录的站点页面打开开发者工具 Console，粘贴 `collector.js` 并执行。

它会：

- 根据 localStorage / sessionStorage 中的 Token 启发式判断 New API / Sub2API；
- 读取 `user_id`、Access Token 和可见 Cookie；
- Sub2API 额外读取 Refresh Token；
- 探测余额和签到端点；
- 对仅第三方登录的 New API 站建议 `oauth + relogin`；
- 输出可直接导入 GUI 或粘贴进 `ACCOUNTS.json` 的三维配置。

采集结果只是初始建议，仍需人工核对：

- 没有读到 Sub2API Token 时可能被误判为 New API；
- JavaScript 无法读取 httpOnly Cookie；
- 纯第三方登录账号当前默认建议 Linux.do，实际使用 GitHub 时需手工修改 `oauth_provider`；
- 它不采集站点 `browser_state` 或共享 `oauth_states`，这两类状态仍需通过 GUI 或 OAuth CLI 捕获；
- 接口返回 401 / 403 / 500 等非 404 状态时，端点探测可能仍认为该接口存在。

### 7.2 GUI 捕获

管理界面支持：

- 捕获和验证站点级 `browser_state`；
- 按 provider / account 捕获 Linux.do、GitHub 共享 OAuth 登录态；
- 浏览器捕获成功后回填 Access Token / Refresh Token；
- 导出只包含启用站点及其实际引用 OAuth 状态的 GitHub Secret JSON。

### 7.3 OAuth CLI

```bash
# 捕获共享 Linux.do 登录态
uv run python browser/poc_oauth.py setup \
  --oauth-provider linuxdo \
  --oauth-account default

# 捕获共享 GitHub 登录态
uv run python browser/poc_oauth.py setup \
  --oauth-provider github \
  --oauth-account default

# 测试 OAuth 重登
uv run python browser/poc_oauth.py run \
  --base-url https://router.example.com \
  --oauth-provider linuxdo \
  --oauth-account default \
  --user-id 10003
```

`oauth_states` / `browser_state` 是压缩后的 Playwright storage state，**不是加密数据**。

---

## 8. 运行命令

### 8.1 批量运行

```bash
# 执行 ACCOUNTS.json 中所有启用站点
uv run python run__all_checkin.py

# 打印完整原始输出；仍会经过脱敏
uv run python run__all_checkin.py --verbose

# 指定最大并发数
uv run python run__all_checkin.py --workers 4
```

批量调度器会：

- 每个账号启动独立 worker 子进程；
- 同一 `base_url` 下的账号串行，不同站点并发；
- 为 HTTP 和浏览器任务设置不同墙钟超时；
- 用环境变量传递 Cookie、Token、代理、登录态和脚本参数，避免出现在进程命令行；
- 严格校验 worker JSON 协议及退出码；
- 把脱敏结果写入 `.cache-checkin/checkin_result.json`；
- 即使任务成功，也显示 API-first / 浏览器降级等关键阶段日志。

### 8.2 直接读取配置运行

```bash
uv run python checkin.py
```

### 8.3 临时运行单站点

```bash
uv run python checkin.py \
  --base-url https://newapi.example.com \
  --name demo \
  --site-profile newapi \
  --auth-method access_token \
  --checkin-action api \
  --access-token '<token>' \
  --user-id 10001
```

敏感值更推荐通过环境变量传递：

- `CHECKIN_COOKIE`
- `CHECKIN_ACCESS_TOKEN`
- `CHECKIN_REFRESH_TOKEN`
- `CHECKIN_USER_ID`
- `CHECKIN_BROWSER_STATE`
- `CHECKIN_SCRIPT_ARGS`
- `CHECKIN_PROXY`

---

## 9. 运行期缓存

短期 Token、轮换后的 Refresh Token 和浏览器运行态保存在：

```text
.cache-checkin/token_cache.json
```

缓存不会无条件覆盖配置：

- 配置凭据会生成不可逆 SHA-256 basis；
- basis 与当前配置一致时才允许缓存覆盖；
- GUI / CLI 显式输入始终优先，包括显式清空；
- 父进程解析完成后，worker 使用 `CHECKIN_CACHE_POLICY=ignore`，避免二次套用旧缓存；
- 修改凭据时只标记“配置已变”，不立即删除缓存值；
- 把凭据改回原值后，仍匹配 basis 的缓存可以重新命中；
- 缓存身份由规范化 `base_url + name` 组成，修改名称或地址会产生新身份，旧条目虽不会被物理删除，但也不会自动迁移到新身份。

这样既避免旧 GitHub Actions cache 覆盖新 Secret，也不会因为一次凭据误改永久丢失仍然有效的登录态。

---

## 10. 代理、WAF 与验证

### 代理

- HTTP 路径基于标准库 `urllib`，支持 `http://` / `https://` 代理；
- 浏览器路径由 Camoufox 驱动，可使用 HTTP / HTTPS / SOCKS5；
- 站点 `proxy` 优先，未配置时回退 `CHECKIN_PROXY`。

### Cloudflare / Turnstile

- browser / oauth / browser_script 可处理页面挑战、弹窗和部分验证码；
- Sub2API 纯 HTTP 账密登录会先读取公开设置，站点启用 Turnstile 时直接回退浏览器；
- 复杂验证无法自动通过时返回 `need_verification`，不会伪装成登录失效。

### 阿里云 WAF

New API 的 `browser + api` 会让浏览器负责执行 JS 挑战并导出 WAF Cookie，再用 HTTP 完成签到。数据中心和 GitHub Actions 出口 IP 可能因信誉过低持续失败；这种情况应配置住宅代理，或在住宅网络环境运行。

---

## 11. GitHub Actions

工作流：`.github/workflows/auto_checkin.yml`

默认计划：每天 **01:30 UTC（北京时间 09:30）**，也支持手动触发。

运行流程：

1. 恢复 `.cache-checkin`；
2. 使用 Python 3.14 和 `uv sync --locked --extra dev` 安装依赖；
3. 从仓库 Secret `ACCOUNTS` 原子写入 `ACCOUNTS.json` 并设置权限；
4. 解析启用账号，判断是否存在 browser / oauth / relogin / browser_script / OAuth fallback；
5. 仅在需要时安装系统字体、Xvfb 和 Camoufox；
6. 可选从 `CLASH_CONFIG` 启动本地 Clash / mihomo；
7. 运行 `run__all_checkin.py`；
8. 生成脱敏的 `checkin_report.md` 并写入 Step Summary；
9. 保存新的 `.cache-checkin`，供下次复用 Token 与登录态。

> [!CAUTION]
> GitHub Actions 缓存的整个 `.cache-checkin` 可能包含明文运行期 Token、压缩编码的浏览器登录态、失败截图、签到结果和未收录答题内容。请把 Actions cache 视为敏感数据，不要下载后公开转发。

需要配置的 Secret：

| Secret | 必需 | 说明 |
|---|---:|---|
| `ACCOUNTS` | 是 | GUI“导出 Secret”得到的完整 JSON |
| `CHECKIN_PROXY` | 否 | 全局代理地址 |
| `CLASH_CONFIG` | 否 | Clash / mihomo 配置内容 |

浏览器任务在 CI 中通过 Xvfb 有头运行；默认 `CHECKIN_HEADLESS=false`。

---

## 12. 结果状态与排查

| 状态 | 含义 | 常见处理 |
|---|---|---|
| `success` | 本次签到或发放有明确成功证据 | 无需处理 |
| `already_done` | 今日已签，或保活后额度没有变化 | 无需处理 |
| `need_login` | Token、Cookie 或共享 OAuth 状态失效 | 重新采集凭据或登录态 |
| `need_verification` | Turnstile、CAPTCHA、WAF 或出口 IP 风控 | 手工验证、配置脚本或更换代理 |
| `need_config` | 必填字段、脚本路径或凭据缺失 | 检查 GUI 表单和配置 |
| `network_error` | 429、5xx、超时等临时网络失败 | 稍后重试或检查代理 |
| `error` | 业务拒绝、脚本异常或无法确认签到成立 | 查看阶段日志和脱敏原始返回 |

程序不会把“HTTP 200 但没有奖励、已签标记或额度增长证据”的响应直接报成成功。

---

## 13. 安全设计

- `ACCOUNTS.json`、`.cache-checkin/`、`.browser_profile/`、旧版 `login_grant_state.json`、`*.lock` 已被忽略；
- `.gitignore` 只能防止普通提交，不能代替本机磁盘权限、备份保护和 Actions cache 权限控制；
- 配置、结果、状态和报告使用文件锁与原子替换写入；
- 配置损坏时失败关闭，不会静默当作空配置覆盖原文件；
- 批量 worker 的 stdout 只承载单行 JSON 协议，诊断日志写 stderr；
- Cookie、Bearer、JWT、`sk-*`、OAuth state、敏感键和代理 URL 凭据统一脱敏；
- 脚本只能从仓库内安全加载；
- 登录态为压缩编码而非加密，安全性依赖本地权限和 GitHub Secret。

---

## 14. 开发与验证

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run python -m compileall -q .
uv lock --check
```

主要目录：

```text
providers/
  profiles/               # New API / Sub2API 接口适配器
  actions/                # api / visit / relogin / browser_script
browser/
  session.py              # 登录态捕获、验证、OAuth 重登、WAF 混合流程
  script_loader.py        # 安全脚本加载器
  script_runner.py        # browser_script 运行与登录态续存
  script_helpers.py       # 脚本 helper
captcha_ocr/              # CAPTCHA 识别器与模板
scripts/
  newapi_captcha.py       # API 图形验证码脚本
  checkin/                # 浏览器站点脚本
ci/                       # 浏览器检测、代理与报告
gui/                      # PySide6 管理界面
tests/                    # 单元与回归测试
```

验证码逆向与识别细节见 [`docs/captcha_algorithm.md`](docs/captcha_algorithm.md)，历史架构优化记录见 [`docs/OPTIMIZATION.md`](docs/OPTIMIZATION.md)。

---

## License

[MIT License](LICENSE)
