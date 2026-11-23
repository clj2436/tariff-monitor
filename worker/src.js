/* ============================================================
 * 资费监控站 · 配置代理 Worker  【构建源文件，勿直接部署】
 * 部署请用 index.js（本文件内联加密库后的成品）
 * ------------------------------------------------------------
 * 作用：网页不接触 GitHub PAT，只把「密码 + 要改的配置」发给本
 * Worker；Worker 校验密码后用自己持有的 PAT 写入 GitHub。
 *
 * 部署：Cloudflare Workers，粘贴本文件全部内容为 Worker 代码。
 * 需在 Worker 的 Settings → Variables and Secrets 配 3 个 secret：
 *   SITE_PASSWORD  —— 你设定的配置密码（ Worker 端校验，前端绕不过）
 *   GITHUB_PAT     —— 细粒度 PAT，需本仓库 Actions: write + Contents: write
 *   REPO           —— 仓库，格式 owner/repo，如 clj2436/tariff-monitor
 * 可选 secret：
 *   ALLOWED_ORIGIN —— 额外允许的页面来源，逗号分隔；内置已放行
 *                     https://zf.chenlijun.eu.org 与 https://clj2436.github.io
 * 可选绑定：
 *   AUTH_KV        —— Cloudflare KV 绑定名，跨节点持久化密码失败计数/锁定状态
 * 安全：密码按 IP 计数，连续 5 次错误锁定 15 分钟；同 IP 最小请求间隔 400ms；
 *       5 分钟内全局失败 30 次则整体冷却 5 分钟；请求体上限 64KB；恒定时间比较。
 * ============================================================ */

/* ---------- 内联依赖：tweetnacl + sealedbox（GitHub Secrets 加密用） ---------- */
const __NACL__ = null; // 占位，构建时替换为 nacl.min.js + sealedbox.web.js
/* ------------------------------------------------------------------------- */

const API = 'https://api.github.com';
const json = (obj, status = 200, extraHeaders = {}) => new Response(
  JSON.stringify(obj),
  { status, headers: { 'Content-Type': 'application/json; charset=utf-8', ...extraHeaders } }
);

// 内置放行两个站点域名（GitHub Pages 默认域 + 自定义域名）；ALLOWED_ORIGIN 可追加其他来源，逗号分隔
const BUILTIN_ORIGINS = ['https://zf.chenlijun.eu.org', 'https://clj2436.github.io'];
function allowedOrigins(env) {
  const extra = String(env.ALLOWED_ORIGIN || '')
    .split(/[,\s]+/).map(s => s.trim().replace(/\/$/, '')).filter(Boolean);
  return Array.from(new Set(BUILTIN_ORIGINS.concat(extra)));
}
function corsHeaders(env, request) {
  const list = allowedOrigins(env);
  const reqOrigin = request && request.headers ? (request.headers.get('Origin') || '') : '';
  const allow = reqOrigin && list.indexOf(reqOrigin.replace(/\/$/, '')) >= 0
    ? reqOrigin.replace(/\/$/, '') : list[0];
  return {
    'Access-Control-Allow-Origin': allow,
    'Vary': 'Origin',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
  };
}

// 恒定时间比较，降低时序侧信道风险
function safeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const s = typeof TextEncoder !== 'undefined' ? new TextEncoder() : null;
  const ba = s ? s.encode(a) : Uint8Array.from(a.split(''), c => c.charCodeAt(0));
  const bb = s ? s.encode(b) : Uint8Array.from(b.split(''), c => c.charCodeAt(0));
  if (ba.length !== bb.length) return false;
  let diff = 0;
  for (let i = 0; i < ba.length; i++) diff |= ba[i] ^ bb[i];
  return diff === 0;
}

/* ---------- 防爆破：按来源 IP 的失败计数 / 临时锁定 / 最小请求间隔 ---------- */
const _authFails = new Map();          // 未绑定 KV 时退化为当前 isolate 内存计数
const MAX_FAILS = 5;                  // 连续 5 次错误即锁定
const LOCK_MS = 15 * 60 * 1000;       // 锁定 15 分钟
const MIN_INTERVAL_MS = 400;          // 同 IP 两次尝试最小间隔，压低爆破速率
const MAX_BODY_BYTES = 64 * 1024;     // 请求体上限，避免超大 JSON 消耗资源
// 全局熔断：单 IP 锁定挡不住换 IP 的分布式爆破，全局短时间内失败过多则整体冷却
const GLOBAL_WINDOW_MS = 5 * 60 * 1000;
const GLOBAL_FAIL_LIMIT = 30;
const GLOBAL_LOCK_MS = 5 * 60 * 1000;
let _globalFails = 0, _globalStart = 0, _globalLockUntil = 0;
function globalLockSeconds(now) {
  if (now - _globalStart > GLOBAL_WINDOW_MS) { _globalFails = 0; _globalStart = now; }
  if (_globalLockUntil > now) return Math.ceil((_globalLockUntil - now) / 1000);
  return 0;
}
function noteGlobalFail(now) {
  _globalFails++;
  if (_globalFails >= GLOBAL_FAIL_LIMIT) {
    _globalLockUntil = now + GLOBAL_LOCK_MS;
    _globalFails = 0;
  }
}

function clientIP(request) {
  const h = request && request.headers;
  if (!h) return 'unknown';
  const v = h.get('CF-Connecting-IP') || h.get('X-Forwarded-For') || '';
  return String(v).split(',')[0].trim() || 'unknown';
}
async function readAuthState(env, ip) {
  if (env.AUTH_KV) {
    try {
      const v = await env.AUTH_KV.get('pwfail:' + ip, 'json');
      if (v) return v;
    } catch (e) {}
  }
  return _authFails.get(ip) || { fails: 0, lockedUntil: 0, lastAt: 0 };
}
async function writeAuthState(env, ip, st) {
  if (env.AUTH_KV) {
    try {
      await env.AUTH_KV.put('pwfail:' + ip, JSON.stringify(st), { expirationTtl: 1800 });
      return;
    } catch (e) {}
  }
  if (_authFails.size > 5000) {
    const cutoff = Date.now() - 3600000;
    for (const [k, v] of _authFails) if ((v.lastAt || 0) < cutoff) _authFails.delete(k);
  }
  _authFails.set(ip, st);
}
async function checkPassword(request, env, password, cors) {
  const ip = clientIP(request);
  const st = await readAuthState(env, ip);
  const now = Date.now();
  if (st.lockedUntil > now) {
    const sec = Math.ceil((st.lockedUntil - now) / 1000);
    return json({ ok: false, error: '密码错误次数过多，请 ' + sec + ' 秒后重试' },
                429, Object.assign({}, cors, { 'Retry-After': String(sec) }));
  }
  if (now - (st.lastAt || 0) < MIN_INTERVAL_MS) {
    return json({ ok: false, error: '请求过于频繁，请稍后再试' },
                429, Object.assign({}, cors, { 'Retry-After': '1' }));
  }
  st.lastAt = now;
  if (!safeEqual(String(password || ''), env.SITE_PASSWORD)) {
    st.fails = (st.fails || 0) + 1;
    let status = 401;
    let msg = '配置密码不正确（剩余 ' + Math.max(0, MAX_FAILS - st.fails) + ' 次尝试）';
    if (st.fails >= MAX_FAILS) {
      st.lockedUntil = now + LOCK_MS;
      st.fails = 0;
      status = 429;
      msg = '密码错误次数过多，已锁定 15 分钟';
    }
    await writeAuthState(env, ip, st);
    noteGlobalFail(now);
    return json({ ok: false, error: msg }, status, cors);
  }
  // 成功也要写回 lastAt，否则首次成功后紧接着的第二次请求会绕过最小间隔
  await writeAuthState(env, ip, { fails: 0, lockedUntil: 0, lastAt: now });
  return null;
}

function ghHeaders(pat) {
  return {
    'Authorization': 'Bearer ' + pat,
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'tariff-monitor-config-worker',
  };
}

async function gh(repo, pat, path, method = 'GET', body) {
  const res = await fetch(API + '/repos/' + repo + path, {
    method,
    headers: { ...ghHeaders(pat), ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) {}
  return { res, data };
}

// GitHub Secrets 加密：取仓库公钥 → libsodium sealed box → base64
async function setSecret(repo, pat, name, value) {
  const pk = await gh(repo, pat, '/actions/secrets/public-key');
  if (!pk.res.ok || !pk.data || !pk.data.key || !pk.data.key_id) {
    throw new Error('获取仓库公钥失败 HTTP ' + pk.res.status);
  }
  const pub = Uint8Array.from(atob(pk.data.key), c => c.charCodeAt(0));
  const box = sealedBox.seal(new TextEncoder().encode(value), pub);
  let bin = '';
  for (let i = 0; i < box.length; i++) bin += String.fromCharCode(box[i]);
  const put = await gh(repo, pat, '/actions/secrets/' + encodeURIComponent(name), 'PUT', {
    encrypted_value: btoa(bin), key_id: pk.data.key_id,
  });
  if (!put.res.ok) throw new Error('写入 Secret ' + name + ' 失败 HTTP ' + put.res.status + '：' + ((put.data && put.data.message) || ''));
  return name;
}

// Variables 明文：先探测存在性，404 → POST 新建，否则 PATCH 更新
async function setVariable(repo, pat, name, value) {
  const base = '/actions/variables/' + encodeURIComponent(name);
  const probe = await gh(repo, pat, base);
  let r;
  if (probe.res.status === 404) r = await gh(repo, pat, '/actions/variables', 'POST', { name, value });
  else if (probe.res.ok) r = await gh(repo, pat, base, 'PATCH', { name, value });
  else throw new Error('读取 Variable ' + name + ' 失败 HTTP ' + probe.res.status);
  if (!r.res.ok) throw new Error('写入 Variable ' + name + ' 失败 HTTP ' + r.res.status + '：' + ((r.data && r.data.message) || ''));
  return name;
}

// 更新 watch_config.json：先 GET 拿 sha，再 PUT（保留未管理的字段）
const CONFIG_PATH = 'data/watch_config.json';
async function setConfig(repo, pat, patch) {
  const apiPath = '/contents/' + CONFIG_PATH;
  const cur = await gh(repo, pat, apiPath);
  let sha = null, existing = null;
  if (cur.res.ok && cur.data && cur.data.content) {
    sha = cur.data.sha;
    try {
      const raw = atob(cur.data.content.replace(/\s/g, ''));
      const bytes = Uint8Array.from(raw, c => c.charCodeAt(0));
      existing = JSON.parse(new TextDecoder().decode(bytes));
    } catch (e) {}
  } else if (cur.res.status !== 404) {
    throw new Error('读取配置失败 HTTP ' + cur.res.status);
  }
  const cfg = {
    version: 1,
    updated_at: new Date().toISOString().slice(0, 19).replace('T', ' '),
    note: existing && existing.note ? existing.note : '',
    focus_rules: existing && existing.focus_rules ? existing.focus_rules : '',
    rules: Array.isArray(patch.rules) ? patch.rules : (existing && existing.rules) || [],
    max_items: Number.isFinite(patch.max_items) ? patch.max_items : ((existing && existing.max_items) || 30),
    site_url: typeof patch.site_url === 'string' ? patch.site_url : ((existing && existing.site_url) || ''),
  };
  const content = btoa(unescape(encodeURIComponent(JSON.stringify(cfg, null, 2) + '\n')));
  const body = { message: 'chore: 更新订阅配置（配置代理）', content, branch: 'main' };
  if (sha) body.sha = sha;
  const put = await gh(repo, pat, apiPath, 'PUT', body);
  if (!put.res.ok) throw new Error('写入配置失败 HTTP ' + put.res.status + '：' + ((put.data && put.data.message) || ''));
  return 'watch_config.json';
}

/* ---------- 配置回读：Secrets 不可回读真值，只能读元数据；Variables/规则文件可读真值 ---------- */
const SNAPSHOT_KEY = 'tariff:botcfg';
let _snapshotMem = {};                 // 未绑定 KV 时用 isolate 内存兜底（重启丢失，前端会提示）
const SECRET_KEYS = [
  'DINGTALK_WEBHOOK', 'DINGTALK_SECRET',
  'DINGTALK_WEBHOOK_FOCUS', 'DINGTALK_SECRET_FOCUS',
  'DINGTALK_WEBHOOK_OTHER', 'DINGTALK_SECRET_OTHER',
];
const _tail = v => String(v || '').slice(-8);

async function readSnapshot(env) {
  if (env.CONFIG_KV) {
    try { const v = await env.CONFIG_KV.get(SNAPSHOT_KEY, 'json'); if (v) return v; } catch (e) {}
  }
  return _snapshotMem;
}
async function writeSnapshot(env, snap) {
  if (env.CONFIG_KV) {
    try { await env.CONFIG_KV.put(SNAPSHOT_KEY, JSON.stringify(snap)); return true; } catch (e) {}
  }
  _snapshotMem = snap;
  return false;
}
async function saveSnapshot(env, input) {
  const snap = await readSnapshot(env);
  const map = {
    webhook: 'DINGTALK_WEBHOOK', secret: 'DINGTALK_SECRET',
    webhook_focus: 'DINGTALK_WEBHOOK_FOCUS', secret_focus: 'DINGTALK_SECRET_FOCUS',
    webhook_other: 'DINGTALK_WEBHOOK_OTHER', secret_other: 'DINGTALK_SECRET_OTHER',
  };
  Object.keys(map).forEach(k => {
    const v = String(input[k] || '').trim();
    if (v) snap[map[k]] = { tail: _tail(v), at: new Date().toISOString().slice(0, 19).replace('T', ' ') };
  });
  return writeSnapshot(env, snap);
}
async function getVariable(repo, pat, name) {
  const r = await gh(repo, pat, '/actions/variables/' + encodeURIComponent(name));
  return r.res.ok && r.data ? String(r.data.value || '') : '';
}
async function readWatchConfig(repo, pat) {
  const r = await gh(repo, pat, '/contents/' + CONFIG_PATH);
  if (!r.res.ok || !r.data || !r.data.content) return {};
  try {
    const raw = atob(r.data.content.replace(/\s/g, ''));
    return JSON.parse(new TextDecoder().decode(Uint8Array.from(raw, c => c.charCodeAt(0))));
  } catch (e) { return {}; }
}
async function readConfig(repo, pat, env) {
  const [secretsRes, channels, siteUrlVar, cfg] = await Promise.all([
    gh(repo, pat, '/actions/secrets'),
    getVariable(repo, pat, 'NOTIFY_CHANNELS'),
    getVariable(repo, pat, 'SITE_URL'),
    readWatchConfig(repo, pat),
  ]);
  const meta = {};
  if (secretsRes.res.ok && secretsRes.data && Array.isArray(secretsRes.data.secrets)) {
    secretsRes.data.secrets.forEach(s => { meta[s.name] = String(s.updated_at || '').replace('T', ' ').slice(0, 16); });
  }
  const snap = await readSnapshot(env);
  const bots = {};
  SECRET_KEYS.forEach(n => {
    bots[n] = { set: !!meta[n], updated: meta[n] || '', tail: (snap[n] && snap[n].tail) || '' };
  });
  return {
    bots,
    channels: channels || 'dingtalk',
    site_url: siteUrlVar || cfg.site_url || '',
    max_items: Number.isFinite(cfg.max_items) ? cfg.max_items : 30,
    rules: Array.isArray(cfg.rules) ? cfg.rules : [],
    updated_at: cfg.updated_at || '',
    snapshot_persistent: !!(env.CONFIG_KV),
  };
}

// 白名单：只接受已知字段，逐个校验类型
function sanitize(input) {
  const out = { secrets: {}, variables: {}, config: {} };
  const clean = v => (typeof v === 'string' ? v.trim() : '');
  const webhook = clean(input.webhook);
  const secret = clean(input.secret);
  const webhookFocus = clean(input.webhook_focus);
  const secretFocus = clean(input.secret_focus);
  const webhookOther = clean(input.webhook_other);
  const secretOther = clean(input.secret_other);
  const channels = clean(input.channels);
  const siteUrl = clean(input.site_url);
  // Webhook 必须是钉钉地址，防止被用来塞任意机密
  const isDing = v => /^https:\/\/oapi\.dingtalk\.com\/robot\/send\?access_token=[\w-]+/.test(v);
  if (webhook && !isDing(webhook)) throw new Error('DINGTALK_WEBHOOK 必须是钉钉机器人地址（https://oapi.dingtalk.com/robot/send?access_token=...）');
  if (webhookFocus && !isDing(webhookFocus)) throw new Error('重点机器人 Webhook 必须是钉钉机器人地址');
  if (webhookOther && !isDing(webhookOther)) throw new Error('其他机器人 Webhook 必须是钉钉机器人地址');
  if (webhook) out.secrets.DINGTALK_WEBHOOK = webhook;
  if (secret) out.secrets.DINGTALK_SECRET = secret.slice(0, 200);
  if (webhookFocus) out.secrets.DINGTALK_WEBHOOK_FOCUS = webhookFocus;
  if (secretFocus) out.secrets.DINGTALK_SECRET_FOCUS = secretFocus.slice(0, 200);
  if (webhookOther) out.secrets.DINGTALK_WEBHOOK_OTHER = webhookOther;
  if (secretOther) out.secrets.DINGTALK_SECRET_OTHER = secretOther.slice(0, 200);
  if (channels === 'dingtalk' || channels === 'all') out.variables.NOTIFY_CHANNELS = channels;
  else if (channels) throw new Error('NOTIFY_CHANNELS 只能是 dingtalk 或 all');
  if (siteUrl) {
    if (!/^https?:\/\//.test(siteUrl)) throw new Error('SITE_URL 必须以 http:// 或 https:// 开头');
    out.variables.SITE_URL = siteUrl.slice(0, 500);
  }
  if (Object.prototype.hasOwnProperty.call(input, 'rules')) {
    if (!Array.isArray(input.rules) || input.rules.length > 40) throw new Error('rules 格式不正确');
    const ops = { mobile: 1, telecom: 1, unicom: 1, gb: 1 };
    const rules = input.rules.map(r => {
      if (!r || !ops[r.op] || !Array.isArray(r.provinces)) throw new Error('rules 含非法运营商或省份列表');
      return { op: r.op, provinces: r.provinces.filter(p => typeof p === 'string').slice(0, 40) };
    }).slice(0, 4);
    out.config.rules = rules;
  }
  if (Object.prototype.hasOwnProperty.call(input, 'max_items')) {
    const n = Number(input.max_items);
    if (!Number.isInteger(n) || n < 1 || n > 200) throw new Error('max_items 必须是 1-200 的整数');
    out.config.max_items = n;
  }
  if (Object.prototype.hasOwnProperty.call(input, 'site_url')) out.config.site_url = siteUrl.slice(0, 500);
  return out;
}

async function handle(request, env) {
  const cors = corsHeaders(env, request);
  const reqOrigin = request.headers && request.headers.get('Origin');
  if (reqOrigin && allowedOrigins(env).indexOf(reqOrigin.replace(/\/$/, '')) < 0) {
    return json({ ok: false, error: '来源不在允许列表' }, 403, cors);
  }
  if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });
  if (request.method !== 'POST') return json({ ok: false, error: '仅支持 POST' }, 405, cors);

  let input;
  try {
    const raw = await request.text();
    if (raw.length > MAX_BODY_BYTES) return json({ ok: false, error: '请求内容过大' }, 413, cors);
    input = JSON.parse(raw);
  } catch (e) { return json({ ok: false, error: '请求体不是合法 JSON' }, 400, cors); }
  if (!input || typeof input !== 'object') return json({ ok: false, error: '请求体格式错误' }, 400, cors);

  if (!env.GITHUB_PAT || !env.REPO) {
    return json({ ok: false, error: 'Worker 未配置完整（需 GITHUB_PAT / REPO）' }, 500, cors);
  }

  // read 免密码：只返回配置元数据（密钥仅尾号，无明文），便于公开查看机器人状态。
  // 写操作（保存修改）仍需密码，安全边界不变。
  if (input.action === 'read') {
    let config;
    try { config = await readConfig(env.REPO, env.GITHUB_PAT, env); }
    catch (e) { return json({ ok: false, error: '读取配置失败：' + e.message }, 500, cors); }
    return json({ ok: true, config }, 200, cors);
  }

  // 密码校验：Worker 端 secrets，前端无法绕过（仅写操作）
  if (!env.SITE_PASSWORD) {
    return json({ ok: false, error: 'Worker 未配置完整（需 SITE_PASSWORD）' }, 500, cors);
  }
  const gsec = globalLockSeconds(Date.now());
  if (gsec) return json({ ok: false, error: '失败请求过多，请 ' + gsec + ' 秒后重试' },
                        429, Object.assign({}, cors, { 'Retry-After': String(gsec) }));
  const denied = await checkPassword(request, env, input.password, cors);
  if (denied) return denied;

  let job;
  try { job = sanitize(input); } catch (e) { return json({ ok: false, error: e.message }, 400, cors); }

  const done = [], errors = [];
  const run = async (label, fn) => { try { await fn(); done.push(label); } catch (e) { errors.push(label + '：' + e.message); } };

  for (const [name, value] of Object.entries(job.secrets)) await run(name, () => setSecret(env.REPO, env.GITHUB_PAT, name, value));
  for (const [name, value] of Object.entries(job.variables)) await run(name, () => setVariable(env.REPO, env.GITHUB_PAT, name, value));
  if (Object.keys(job.config).length) await run('watch_config.json', () => setConfig(env.REPO, env.GITHUB_PAT, job.config));
  if (done.length) await saveSnapshot(env, input).catch(() => {});

  if (!done.length && !errors.length) return json({ ok: false, error: '没有需要保存的内容（留空表示不修改）' }, 400, cors);
  return json({ ok: errors.length === 0, done, errors }, errors.length ? 207 : 200, cors);
}

export default { fetch: handle };
