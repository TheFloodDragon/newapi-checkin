/**
 * 公益站凭据采集脚本（New API / Sub2API）
 *
 * 用法：在已登录的站点页面打开 F12 开发者工具 → Console，粘贴本脚本后回车。
 *
 * 自动识别站点类型：
 *   - sub2api：localStorage 有 auth_token / access_token，或存在 /api/v1 接口
 *   - newapi ：其余（New API 系，使用 /api/user/self）
 *
 * 输出直接使用当前的三维字段（site_profile / auth_method / checkin_action），
 * 可原样粘贴进 ACCOUNTS.json 的 accounts 数组。
 *
 * Sub2API 会额外采集 refresh_token：access_token 是短期 JWT，有了 refresh_token
 * 程序就能纯 HTTP 自行续期，不必为「token 过期」拉起浏览器。
 */

(async () => {
  const baseUrl = location.origin.replace(/\/+$/, '');
  const H1 = 'font-weight:bold;font-size:14px;color:#4CAF50';
  const H2 = 'color:#2196F3;font-weight:bold';
  const WARN = 'color:#FF9800;font-weight:bold';
  const DIM = 'color:#607D8B;font-weight:bold';

  const readStore = (key) => {
    try {
      return (localStorage.getItem(key) || sessionStorage.getItem(key) || '').trim();
    } catch (_) {
      return '';
    }
  };

  // ── 站点类型识别 ──────────────────────────────────────────────────────────
  const sub2apiToken =
    readStore('auth_token') || readStore('access_token') || readStore('token') || readStore('jwt');
  const siteType = sub2apiToken ? 'sub2api' : 'newapi';

  let siteName = '';
  let userId = '';
  let accessToken = '';
  let refreshToken = '';
  let usageInfo = null;
  // 三维字段：登录方式 / 签到方式
  let authMethod = 'cookie';
  let checkinAction = 'api';
  let checkinEndpoint = '';
  const warnings = [];

  if (siteType === 'sub2api') {
    // ── Sub2API：Bearer auth_token + /api/v1 ───────────────────────────────
    accessToken = sub2apiToken;
    refreshToken = readStore('refresh_token');
    authMethod = 'access_token';

    try {
      siteName = (window.__APP_CONFIG__ && window.__APP_CONFIG__.site_name) || '';
    } catch (_) { /* 静默 */ }

    // 站点公开设置（Sub2API 系通用）：站点名 + 是否启用 Turnstile
    try {
      const r = await fetch(`${baseUrl}/api/v1/settings/public`, { headers: { Accept: 'application/json' } });
      if (r.ok) {
        const j = await r.json();
        const d = (j && (j.data || j)) || {};
        if (!siteName) siteName = String(d.site_name || '').trim();
        if (d.turnstile_enabled) {
          warnings.push('ℹ️  该站登录页启用了 Cloudflare Turnstile；登录态过期后需重新捕获，或在 script_args 配置 email/password 由脚本自动登录');
        }
      }
    } catch (_) { /* 静默 */ }

    const pickBalance = (obj) => {
      if (!obj || typeof obj !== 'object') return undefined;
      for (const key of ['balance', 'remaining', 'credit', 'credits', 'quota']) {
        const v = obj[key];
        if (typeof v === 'number' || (typeof v === 'string' && v.trim() !== '' && !Number.isNaN(Number(v)))) return v;
      }
      if (obj.user) {
        const v = pickBalance(obj.user);
        if (v !== undefined) return v;
      }
      if (Array.isArray(obj.items)) {
        for (const item of obj.items) {
          const v = pickBalance(item);
          if (v !== undefined) return v;
        }
      }
      return undefined;
    };

    const authHeaders = { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' };

    // 用户信息 / 余额：标准 Sub2API 源码路由 /api/v1/user/profile + /api/v1/auth/me
    for (const path of ['/api/v1/user/profile', '/api/v1/auth/me']) {
      try {
        const r = await fetch(`${baseUrl}${path}`, { headers: authHeaders });
        if (!r.ok) continue;
        const j = await r.json();
        const d = (j && (j.data || j)) || {};
        if (!userId) userId = String(d.id ?? d.user_id ?? '');
        const balance = pickBalance(d);
        if (balance !== undefined && !usageInfo) {
          usageInfo = { remaining: balance, unit: 'USD', isValid: true, source: path };
          warnings.push(`✅ ${path} 登录态可用，余额：${balance} USD`);
        }
        break;
      } catch (_) { /* 静默 */ }
    }

    // 用量列表兜底：/api/v1/usage 的 items[].user.balance
    if (accessToken && !usageInfo) {
      try {
        const r = await fetch(
          `${baseUrl}/api/v1/usage?page=1&page_size=1&sort_by=created_at&sort_order=desc`,
          { headers: authHeaders },
        );
        if (r.ok) {
          const j = await r.json();
          const d = (j && (j.data || j)) || {};
          const balance = pickBalance(d);
          if (balance !== undefined) {
            usageInfo = { remaining: balance, unit: 'USD', isValid: true, source: '/api/v1/usage' };
            warnings.push(`✅ /api/v1/usage 登录态可用，余额：${balance} USD`);
          }
        }
      } catch (_) { /* 静默 */ }
    }

    // 探测签到端点：各 fork 不统一（100xLabs 用 /check-in，极速蹬用 /play/checkin）。
    // 用 GET 状态接口探测存在性：404 表示该 fork 没有这个端点。
    for (const [postPath, statusPath] of [
      ['/api/v1/check-in', '/api/v1/check-in/status'],
      ['/api/v1/play/checkin', '/api/v1/play/checkin/status'],
    ]) {
      try {
        const r = await fetch(`${baseUrl}${statusPath}`, { headers: authHeaders });
        if (r.status === 404) continue;
        checkinEndpoint = postPath;
        if (r.ok) {
          const j = await r.json();
          const d = (j && (j.data || j)) || {};
          const checked = d.checked_in_today ?? d.checked_in ?? d.today_checked;
          if (checked !== undefined) {
            warnings.push(`ℹ️  签到状态接口 ${statusPath} 可用，今日${checked ? '已' : '未'}签到`);
          }
        }
        break;
      } catch (_) { /* 静默 */ }
    }

    if (checkinEndpoint) {
      checkinAction = 'api';
      warnings.push(`✅ 检测到签到接口 ${checkinEndpoint}，可用纯 API 签到（无需浏览器）`);
    } else {
      // 标准 Sub2API 源码没有每日签到接口。有签到按钮的站点通常是页面级交互，
      // 需要自定义浏览器脚本；没有则只能保活监控余额。
      checkinAction = 'api';
      warnings.push('⚠️  未探测到签到接口；若该站页面上有「签到」按钮，请改用 checkin_action="browser_script" 并指定 script 脚本路径');
    }

    if (!accessToken) warnings.push('⚠️  未获取到 auth_token / access_token（可能未登录）');
    if (refreshToken) {
      warnings.push(`✅ 已采集 refresh_token（${refreshToken.length} 字符）：access_token 过期后程序可纯 HTTP 自动续期`);
    } else {
      warnings.push('⚠️  未找到 refresh_token；access_token 过期后需重新采集或由浏览器脚本重新登录');
    }
  } else {
    // ── New API：Cookie / Access token + /api/user/self ───────────────────
    let thirdPartyOnly = false;

    try {
      const r = await fetch(`${baseUrl}/api/status`, { credentials: 'include' });
      if (r.ok) {
        const j = await r.json();
        const d = (j && (j.data || j)) || {};
        siteName = (d.system_name || d.name || d.site_name || d.title || '').trim();
      }
    } catch (_) { /* 静默 */ }

    try {
      const r = await fetch(`${baseUrl}/api/user/self`, {
        credentials: 'include',
        headers: { 'New-Api-User': '-1' },
      });
      if (r.ok) {
        const j = await r.json();
        if (j && j.success) {
          const d = j.data || {};
          userId = String(d.id ?? '');
          accessToken = (d.access_token || '').trim();
          // 无本地密码 + 仅第三方 OAuth 登录 → 多为「登录即发额度」站点
          const thirdParty = d.linux_do_id || d.oidc_id || d.github_id || d.wechat_id || d.telegram_id;
          if ((d.password === '' || d.password == null) && thirdParty) {
            thirdPartyOnly = true;
          }
        }
      }
    } catch (_) { /* 静默 */ }

    if (!userId) {
      try {
        const stored = JSON.parse(localStorage.getItem('user') || '{}');
        userId = String(stored.id ?? stored.user_id ?? '');
      } catch (_) { /* 静默 */ }
    }

    // 探测是否存在签到接口：有则 api，无则按是否第三方登录给出 visit / relogin 建议
    let hasCheckin = false;
    try {
      const month = new Date().toISOString().slice(0, 7);
      const r = await fetch(`${baseUrl}/api/user/checkin?month=${month}`, {
        credentials: 'include',
        headers: { 'New-Api-User': userId || '-1', Accept: 'application/json' },
      });
      hasCheckin = r.status !== 404;
      if (r.ok) {
        const j = await r.json();
        const stats = ((j && j.data) || {}).stats || {};
        if (stats.checked_in_today !== undefined) {
          warnings.push(`ℹ️  签到状态接口可用，今日${stats.checked_in_today ? '已' : '未'}签到`);
        }
      }
    } catch (_) { /* 静默 */ }

    authMethod = accessToken ? 'access_token' : 'cookie';
    if (hasCheckin) {
      checkinAction = 'api';
    } else if (thirdPartyOnly) {
      checkinAction = 'relogin';
      warnings.push('💡 未探测到签到接口，且本站仅支持第三方 OAuth 登录（无本地密码）。');
      warnings.push('   已建议 checkin_action="relogin"：用浏览器自动重放 OAuth 登录来真正触发发放额度。');
      warnings.push('   需先在管理界面「捕获 OAuth 登录态」；若只想保活+监控余额，可改为 "visit"。');
    } else {
      checkinAction = 'visit';
      warnings.push('💡 未探测到签到接口，已建议 checkin_action="visit"（保活 + 余额监控，不触发发放）。');
    }

    if (!userId) warnings.push('⚠️  未获取到 user_id（可能未登录，或 /api/user/self 被拦截）');
    if (!accessToken) warnings.push('ℹ️  未获取到 access_token —— 可在站点「个人设置 → Access Token」生成后填入（比 Cookie 更持久）');
  }

  // 通用 fallback：<title> 去后缀
  if (!siteName) {
    siteName = document.title
      .replace(/[-–|].*$/, '')
      .replace(/\s*(首页|Home|Dashboard|控制台)\s*$/i, '')
      .trim();
  }
  if (!siteName) siteName = baseUrl.replace(/^https?:\/\//, '');

  // Cookie（仅 JS 可读部分；httpOnly 的 session 读不到属正常现象）
  const visibleCookie = document.cookie.trim();
  if (!visibleCookie && !accessToken) {
    warnings.push('⚠️  Cookie 和 access_token 均为空，签到将无法完成认证');
  }
  if (authMethod === 'cookie' && !visibleCookie) {
    warnings.push('⚠️  登录方式为 cookie，但读不到可用 Cookie（session 多为 httpOnly）；建议改用 access_token');
  }

  // ── 组装输出（三维字段）──────────────────────────────────────────────────
  const entry = {
    name: siteName,
    base_url: baseUrl,
    site_profile: siteType,
    auth_method: authMethod,
    checkin_action: checkinAction,
    enabled: true,
  };
  if (userId) entry.user_id = userId;
  if (accessToken) entry.access_token = accessToken;
  if (refreshToken) entry.refresh_token = refreshToken;
  if (authMethod === 'cookie' && visibleCookie) entry.cookie = visibleCookie;
  if (checkinAction === 'relogin') {
    entry.auth_method = 'oauth';
    entry.oauth_provider = 'linuxdo';
    entry.oauth_account = 'default';
  }

  // ── 打印 ──────────────────────────────────────────────────────────────────
  const mask = (v) => (v ? `${v.slice(0, 8)}…（${v.length} 字符）` : '（未获取）');

  console.log('');
  console.log('%c╔══ 公益站凭据采集结果 ══╗', H1);
  console.log(`  站点适配器 : ${siteType}`);
  console.log(`  站点名称   : ${siteName}`);
  console.log(`  站点地址   : ${baseUrl}`);
  console.log(`  登录方式   : ${entry.auth_method}`);
  console.log(`  签到方式   : ${checkinAction}${checkinEndpoint ? `（${checkinEndpoint}）` : ''}`);
  console.log(`  用户 ID    : ${userId || '（未获取）'}`);
  console.log(`  Access T   : ${mask(accessToken)}`);
  if (siteType === 'sub2api') console.log(`  Refresh T  : ${mask(refreshToken)}`);
  if (usageInfo) {
    console.log(`  余额       : ${usageInfo.remaining} ${usageInfo.unit}（来源 ${usageInfo.source}）`);
  }
  console.log(`  Cookie     : ${visibleCookie ? `${visibleCookie.slice(0, 60)}${visibleCookie.length > 60 ? '…' : ''}` : '（空）'}`);
  console.log('');

  if (warnings.length) {
    console.log('%c── 提示 ──────────────────────────────', WARN);
    warnings.forEach((w) => console.log(w));
    console.log('');
  }

  console.log('%c── 粘贴到 ACCOUNTS.json 的 "accounts" 数组内 ──────────────', H2);
  console.log(JSON.stringify(entry, null, 2));
  console.log('');
  console.log('%c── 说明 ─────────────────────────────────────────────', DIM);
  console.log('  · 三维字段：site_profile（接口长什么样）/ auth_method（如何认证）/ checkin_action（如何触发发放）');
  console.log('  · 也可以直接在管理界面（uv run python manage_accounts.py）里「从剪贴板导入」');
  console.log('  · 凭据请勿提交进 Git：ACCOUNTS.json 已被 .gitignore 忽略');

  return entry;
})();
