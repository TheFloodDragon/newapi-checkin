# 代码优化分析报告

> 分析基准：master @ 2449cec（2026-07-27）。行号以该版本为准。

## 一、代码全景

| 模块 | 规模 | 职责 |
| --- | --- | --- |
| `manage_accounts.py` | 3625 行 | PySide6 管理 GUI（本次重构对象） |
| `accounts_store.py` | ~1300 行 | ACCOUNTS.json 读写 / 文件锁 / 迁移 / OAuth 登录态 / Secret 导出 |
| `run__all_checkin.py` | ~750 行 | 批量签到调度器（ThreadPoolExecutor，max 8 并发 + 同站互斥锁） |
| `providers/` | — | 三维正交组装：site_profile(newapi/sub2api) × auth_method × checkin_action |
| `browser/` | — | Camoufox/Playwright 会话、OAuth 捕获重放、Turnstile、自定义脚本 |
| `tests/` | 12 个文件 | 覆盖存储 / HTTP / 脚本 / 安全输出 / worker 协议；**无 GUI 测试** |

整体架构（三维正交 + 统一入口 `providers.run_checkin` / `query_status`）是清晰的，本次发现的问题集中在**重复逻辑漂移**与 **GUI 单体**两块。

## 二、可优化点

### P0 · 重复逻辑收敛（有正确性风险）

**1. `normalize_base_url` 三份实现**
- [accounts_store.py:455](../accounts_store.py:455)、[checkin.py:52](../checkin.py:52)、[providers/base.py:338](../providers/base.py:338)
- 同名函数三处维护，历史上极易漂移（大小写、末尾斜杠、默认 scheme）。应保留 `providers/base.py` 一份，其余 re-export。

**2. `VERIFICATION_PATTERNS` 五份定义且内容已经漂移**
- [providers/base.py:41](../providers/base.py:41)：`Turnstile/Cloudflare/Just a moment/安全验证/challenge-platform`
- [providers/actions/api.py:31](../providers/actions/api.py:31)：多 `人机/验证/captcha`
- [providers/actions/visit.py:38](../providers/actions/visit.py:38)：多 `人机/验证`，无 `captcha`
- [providers/profiles/newapi.py:65](../providers/profiles/newapi.py:65)：同 base
- [providers/profiles/sub2api.py:48](../providers/profiles/sub2api.py:48)：`captcha/verify/人机/验证`，无 `Just a moment/challenge-platform`
- `contains_any` 已做大小写归一（[providers/base.py:277](../providers/base.py:277)），大小写差异无害；但**词表内容差异是真实的行为差异**：同一段站点报错文本，在接口签到里会归类为 `need_verification`，在保活/另一 profile 里可能落成普通 `error`，GUI 徽标与重试策略随之不同。应在 `providers/base.py` 维护唯一词表（可加 profile 级追加项），其余引用它。

**3. 额度格式化 / detail 提取三方重复**
- CLI：`.4g` 三处 — [browser/session.py:351](../browser/session.py:351)、[providers/actions/_common.py:30](../providers/actions/_common.py:30)、[run__all_checkin.py:406](../run__all_checkin.py:406)
- GUI：`.2f/.4f` 两处 — [manage_accounts.py:456](../manage_accounts.py:456)、[manage_accounts.py:1815](../manage_accounts.py:1815)
- `run__all_checkin.py:411-434` 的 `detail_is_usd/format_quota/extract_*` 与 `_common.usd_str`、GUI 的 `_detail_quota_usd`（[manage_accounts.py:1479](../manage_accounts.py:1479)）解析的是同一个 `detail` 结构。同一余额 CLI 显示 `$246.1`、GUI 显示 `$246.10`。应统一 `format_usd` / `detail_quota_usd` 到 providers 层。

**4. GUI 内部：参数装配 ×3、auth 矫正 ×10（本次重构直接解决）**
- 同一个 ~20 键的任务 params dict 在 `_checkin_current`（[manage_accounts.py:1990](../manage_accounts.py:1990)）、`_checkin_all`（:2105）、`_browser_params`（:2925）三处手写。
- `relogin → auth=oauth`、`browser_script → auth∈{browser,oauth}` 的矫正逻辑散布在 `_rows`(:217)、`_set_combos`(:2295)、`_on_combo_changed`(:2327)、`_sync_type`(:2342)、`_flush`(:2455)、`_rows_snapshot`(:2514)、`_save`(:2824)、`_browser_params`(:2913)、`_checkin_current`(:1978)、`_checkin_all`(:2093) 约十处。改一处漏九处的温床。

### P1 · GUI 架构与可用性（本次重构直接解决）

5. **3625 行单文件**：UI、线程、业务、持久化、QSS 全部耦合在一个模块。
6. **「全部签到」是死代码**：`_checkin_all`（:2013）完整实现了批量签到，但没有任何按钮触发——核心批量能力在 GUI 里不可达，只能靠 CLI。
7. **列表全量重建**：`_render_list`（:1622）在每次搜索键入时 clear + 重建全部 `SiteItemWidget`。
8. **后台任务信号生命周期靠 workaround**：`BatchTask.setAutoDelete(False)` + App 持引用再手动释放（:788-803、:1465-1476）。信号应定义在长寿命 runner 对象上，任务经 runner 转发，整段 workaround 可删。
9. **两条并行的后台执行路径**：`BrowserWorker(QThread)` 与 `BatchTask(QRunnable)` 都会执行 query/checkin（`_test_checkin` 走前者、刷新/签到走后者），行为重复。应统一：线程池跑全部 providers 调用，QThread 仅保留需要交互收尾的 capture/verify。
10. **退出靠 `os._exit`**（:3621）：没有任务取消设计，靠强杀进程兜底（Playwright 子进程/非守护线程会挂住解释器）。短期可保留为最后防线，但排队任务应可清空、浏览器 worker 应可请求收尾（现已部分做到）。
11. **反馈通道弱**：批量结果用阻塞 `QMessageBox` 弹长文本；过程日志只进 stderr（GUI 用户根本看不到）；toast 是 footer 单条 QLabel、4 秒覆盖。
12. **视觉硬编码**：仅浅色主题；侧栏固定 360px、列表项 `setMaximumWidth(328)`；状态徽标依赖 emoji（🎁/○/⚠）在不同字体栈下渲染不一致。
13. **状态缓存双文件合并脆弱**：`_load_cached_status`(:1491) 读 `results/checkin_result.json`，`_merge_gui_status_cache`(:1536) 再按 `saved_at` ISO 字符串比大小合并 `results/gui_status_cache.json`。逻辑正确但分散、无类型；应收敛为一个 `StatusStore`。

### P2 · 其他（暂缓，不在本次范围）

14. `accounts_store.py` 同时负责锁/原子写/迁移/归一化/OAuth/导出，可拆但收益有限，且测试覆盖良好。
15. `ACCOUNTS.json` 明文存凭据 — `.gitignore` 已覆盖（已验证未被 git 跟踪），风险可接受；后续可选 keyring/DPAPI。
16. `.worktrees/login-j3r8RD` 是登记在案的 git worktree，停在旧提交 5aa1501，会污染全仓 grep 结果；确认无用后 `git worktree remove` 清理。
17. `run__all_checkin.py` 调度器本身状态良好（并发 + 同站锁 + 超时看护），不动。

## 三、新管理 UI 设计（实施方案）

**决策**：重做 PySide6 桌面版，直接替换旧版；入口 `manage_accounts.py` 保持不变（变为薄壳），实现迁入 `gui/` 包。

```
gui/
  theme.py     # 设计令牌（浅色/深色两套）+ QSS 生成器 + QSettings 持久化
  core.py      # 纯逻辑层（不依赖 Qt）：行归一化、effective_auth 唯一矫正、
               #   task_params 唯一装配、快照/脏比较、format_usd、StatusStore、
               #   脱敏日志（沿用旧版安全语义）
  workers.py   # TaskRunner：QThreadPool 统一跑 query/checkin，信号挂在 runner 上
               #   （消除 autoDelete workaround）；BrowserWorker 仅 capture/verify
  widgets.py   # Pill/StatCard/SiteItem/Toast/LogPanel/NoWheelComboBox
  dialogs.py   # 新增站点选型对话框等
  app.py       # 主窗口装配 + main()
manage_accounts.py  # from gui.app import main
```

**界面结构**（在保留全部旧功能的前提下新增）：

- **顶栏**：应用标识 · 深/浅色切换 · 保存状态徽标
- **概览条（新增）**：站点总数/启用数 · 今日已签 · 已知总额度 · 异常数，右侧挂**全部查询 / 全部签到**（把死代码 `_checkin_all` 真正接上）
- **左侧站点列表**：搜索、拖拽排序、启用开关、状态点/额度/徽标（沿用交互，组件化重写）
- **右侧编辑区**：汇总卡（大字额度 + 立即签到/刷新/复制/删除）+ 表单卡（站点信息/凭据），显隐联动由 `core.effective_auth` 单点驱动
- **底部日志抽屉（新增）**：把原本只进 stderr 的后台任务日志（已脱敏）实时展示在 GUI 内，可折叠
- **Toast 队列**：非阻塞轻提示；批量结果改为摘要 toast + 日志明细，不再弹长文本模态框

**保留不变**：ACCOUNTS.json 数据格式与 `accounts_store` 读写路径、导出 Secret、剪贴板导入、OAuth 捕获/检测流程、关闭前双重确认、脱敏日志规则、`main()` 末尾 `os._exit` 兜底。

## 四、建议实施顺序

1. ✅ GUI 重构（`gui/` 包 + 薄壳入口）— 覆盖 §二 4-13
2. P0-1/2/3：providers 层词表与格式化收敛（小改动、独立提交）
3. P2 按需

*本报告由代码分析产出；GUI 重构随本次一并落地，其余项待后续提交。*
