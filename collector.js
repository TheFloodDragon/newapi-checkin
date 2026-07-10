/**
 * 公益站信息采集脚本（支持 New API / Sub2API）
 *
 * 用法：在已登录的站点页面打开 F12 开发者工具 → Console，粘贴本脚本后回车。
 *
 * 自动识别站点类型：
 *   - sub2api：localStorage 有 auth_token，或存在 /api/v1 接口
 *   - newapi ：其余（New API 系，使用 /api/user/self）
 *
 * 采集内容：站点名称、base_url、用户 ID、Access Token、Cookie
 * 输出可直接粘贴到 ACCOUNTS.json 的统一账号配置块。
 */

(async () => {
  const baseUrl = location.origin.replace(/\/+$/, '');
  const H1 = 'font-weight:bold;font-size:14px;color:#4CAF50';
  const H2 = 'color:#2196F3;font-weight:bold';
  const H3 = 'color:#9C27B0;font-weight:bold';
  const WARN = 'color:#FF9800;font-weight:bold';

  // ── 站点类型识别 ──────────────────────────────────────────────────────────
  const sub2apiToken = localStorage.getItem('auth_token') || localStorage.getItem('access_token') || localStorage.getItem('token') || '';
  let siteType = sub2apiToken ? 'sub2api' : 'newapi';

  let siteName = '';
  let userId = '';
  let accessToken = '';
  let usageInfo = null;
  let checkinMode = 'legacy';
  let isLoginGrantCandidate = false;
  const warnings = [];

  if (siteType === 'sub2api') {
    // ── Sub2API：Bearer auth_token + /api/v1 ───────────────────────────────
    accessToken = sub2apiToken;

    // 站点名称：__APP_CONFIG__.site_name → <title>
    try {
      siteName = (window.__APP_CONFIG__ && window.__APP_CONFIG__.site_name) || '';
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

    // 用户信息 / 余额：标准 Sub2API 源码路由 /api/v1/user/profile + /api/v1/auth/me
    for (const path of ['/api/v1/user/profile', '/api/v1/auth/me']) {
      try {
        const r = await fetch(`${baseUrl}${path}`, {
          headers: { 'Authorization': `Bearer ${accessToken}`, 'Accept': 'application/json' },
        });
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

    if (!siteName) {
      try {
        const stored = JSON.parse(localStorage.getItem('auth_user') || '{}');
        siteName = stored.site_name || '';
      } catch (_) { /* 静默 */ }
    }

    // 用量列表：标准 Sub2API 源码路由 /api/v1/usage，items[].user.balance 可取余额。
    if (accessToken && !usageInfo) {
      try {
        const r = await fetch(`${baseUrl}/api/v1/usage?page=1&page_size=1&sort_by=created_at&sort_order=desc`, {
          headers: { 'Authorization': `Bearer ${accessToken}`, 'Accept': 'application/json' },
        });
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

    // API Key 网关接口：/v1/usage 通常要求 sk-*，前端 auth_token 返回 INVALID_API_KEY 属正常。
    if (accessToken && !usageInfo) {
      try {
        const r = await fetch(`${baseUrl}/v1/usage`, {
          headers: { 'Authorization': `Bearer ${accessToken}`, 'Accept': 'application/json, text/plain, */*' },
        });
        if (r.ok) {
          const j = await r.json();
          const remaining = j?.remaining ?? j?.quota?.remaining ?? j?.balance;
          const unit = j?.unit ?? j?.quota?.unit ?? 'USD';
          const isValid = j?.is_active ?? j?.isValid ?? true;
          if (remaining !== undefined && remaining !== null) {
            usageInfo = { remaining, unit, isValid, source: '/v1/usage' };
            warnings.push(`✅ /v1/usage API Key 余额查询可用：${remaining} ${unit}${isValid === false ? '（标记为无效）' : ''}`);
          }
        } else if (r.status === 401) {
          const text = await r.text().catch(() => '');
          if (/INVALID_API_KEY|Invalid API key/i.test(text)) {
            warnings.push('ℹ️  /v1/usage 需要专用 API Key；当前 localStorage token 是前端登录态，程序会使用 /api/v1/* 接口');
          }
        }
      } catch (_) { /* 静默 */ }
    }

    checkinMode = '';  // sub2api 无 checkin_mode

    if (!accessToken) warnings.push('⚠️  未获取到 auth_token / access_token（可能未登录）');
    warnings.push('ℹ️  Sub2API localStorage token 是前端登录态；/v1/usage 可能需要另行生成的 sk-* API Key。token 过期后需重新从浏览器导出或用 browser/oauth 刷新');
  } else {
    // ── New API：Cookie / Access token + /api/user/self ───────────────────
    // 站点名称：/api/status
    try {
      const r = await fetch(`${baseUrl}/api/status`, { credentials: 'include' });
      if (r.ok) {
        const j = await r.json();
        const d = (j && (j.data || j)) || {};
        siteName = (d.system_name || d.name || d.site_name || d.title || '').trim();
      }
    } catch (_) { /* 静默 */ }

    // 用户信息：/api/user/self
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
          // 检测「登录即发额度」类站点：无本地密码 + 仅第三方 OAuth 登录
          const thirdParty = d.linux_do_id || d.oidc_id || d.github_id || d.wechat_id || d.telegram_id;
          if ((d.password === '' || d.password == null) && thirdParty) {
            isLoginGrantCandidate = true;
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

    if (!userId) warnings.push('⚠️  未获取到 user_id（可能未登录，或 /api/user/self 被拦截）');
    if (!accessToken) warnings.push('ℹ️  未获取到 access_token —— 需在站点「个人设置→Access Token」页面生成后填入');
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

  // 「登录即发额度」类站点提示（如 AgentRouter：无签到接口，靠 OAuth 登录发放）
  if (isLoginGrantCandidate) {
    warnings.push('💡 检测到本站为第三方 OAuth 登录（无本地密码）。');
    warnings.push('   若该站「没有签到按钮、登录即发额度」（如 AgentRouter），请把下方 checkin_mode 改为：');
    warnings.push('   · "login_grant"  ：仅保活 + 余额监控，真正发放仍需你在浏览器重新登录一次；');
    warnings.push('   · "browser_oauth"：用 Playwright 自动重放 OAuth 登录、真正触发发额度（需另配 browser_state）。');
  }

  // ── 组装输出 ──────────────────────────────────────────────────────────────
  const accountEntry = { name: siteName, base_url: baseUrl, type: siteType, enabled: true };
  if (siteType === 'newapi') accountEntry.checkin_mode = checkinMode || 'legacy';
  if (userId) accountEntry.user_id = userId;
  if (accessToken) accountEntry.access_token = accessToken;
  if (visibleCookie) accountEntry.cookie = visibleCookie;

  // login_grant 候选：额外准备一个「登录即发额度」版本的配置块（newapi + checkin_mode）
  let loginGrantEntry = null;
  if (isLoginGrantCandidate) {
    loginGrantEntry = { name: siteName, base_url: baseUrl, type: 'newapi', checkin_mode: 'login_grant', enabled: true };
    if (userId) loginGrantEntry.user_id = userId;
    if (accessToken) loginGrantEntry.access_token = accessToken;
    if (visibleCookie) loginGrantEntry.cookie = visibleCookie;
  }

  // ── 打印 ──────────────────────────────────────────────────────────────────
  console.log('');
  console.log('%c╔══ 公益站信息采集结果 ══╗', H1);
  console.log(`  站点类型 : ${siteType}`);
  console.log(`  站点名称 : ${siteName}`);
  console.log(`  站点地址 : ${baseUrl}`);
  console.log(`  用户 ID  : ${userId || '（未获取）'}`);
  console.log(`  Access T : ${accessToken ? accessToken.slice(0, 8) + '…（已截断）' : '（未获取）'}`);
  if (usageInfo) console.log(`  余额接口 : ${usageInfo.source || '/api/v1/*'} → ${usageInfo.remaining} ${usageInfo.unit}${usageInfo.isValid === false ? '（无效）' : ''}`);
  console.log(`  Cookie   : ${visibleCookie
    ? visibleCookie.slice(0, 80) + (visibleCookie.length > 80 ? '…' : '')
    : '（空）'}`);
  console.log('');

  if (warnings.length) {
    console.log('%c── 提示 ──────────────────────────────', WARN);
    warnings.forEach(w => console.log(w));
    console.log('');
  }

  console.log('%c── 粘贴到 ACCOUNTS.json（"accounts" 数组内） ──────────────', H2);
  console.log('  示例结构：{ "accounts": [ { ... } ] }');
  console.log(JSON.stringify(accountEntry, null, 2));

  if (loginGrantEntry) {
    console.log('');
    console.log('%c── 或：若本站无签到接口、登录即发额度，请改用以下配置（newapi + checkin_mode=login_grant）──', WARN);
    console.log('   想真正自动领取额度，可把 checkin_mode 改为 "browser_oauth"（需 Playwright + 导出 browser_state）。');
    console.log(JSON.stringify(loginGrantEntry, null, 2));
  }

  console.log('');
  console.log('%c── 完整字段备份（供手动核对或直接复制）─────────────────────────', 'color:#607D8B;font-weight:bold');
  console.log(JSON.stringify(accountEntry, null, 2));

  return accountEntry;
})();
