#!/usr/bin/env node
'use strict';

const crypto = require('crypto');

const K = new Uint32Array([
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
]);
const W = new Uint32Array(64);

function sha256Fast16(msg) {
  const blk = new Uint8Array(64);
  blk.set(msg);
  blk[16] = 0x80;
  blk[63] = 128;
  for (let i = 0; i < 16; i++) {
    W[i] = (blk[i * 4] << 24) | (blk[i * 4 + 1] << 16) | (blk[i * 4 + 2] << 8) | blk[i * 4 + 3];
    W[i] >>>= 0;
  }
  for (let i = 16; i < 64; i++) {
    const w15 = W[i - 15] >>> 0;
    const w2 = W[i - 2] >>> 0;
    const s0 = (((w15 >>> 7) | (w15 << 25)) ^ ((w15 >>> 18) | (w15 << 14)) ^ (w15 >>> 3)) >>> 0;
    const s1 = (((w2 >>> 17) | (w2 << 15)) ^ ((w2 >>> 19) | (w2 << 13)) ^ (w2 >>> 10)) >>> 0;
    W[i] = (W[i - 16] + s0 + W[i - 7] + s1) >>> 0;
  }
  let a = 0x6a09e667, b = 0xbb67ae85, c = 0x3c6ef372, d = 0xa54ff53a;
  let e = 0x510e527f, f = 0x9b05688c, g = 0x1f83d9ab, h = 0x5be0cd19;
  for (let i = 0; i < 64; i++) {
    const S1 = (((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7))) >>> 0;
    const ch = ((e & f) ^ ((~e) & g)) >>> 0;
    const t1 = (h + S1 + ch + K[i] + W[i]) >>> 0;
    const S0 = (((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10))) >>> 0;
    const mj = ((a & b) ^ (a & c) ^ (b & c)) >>> 0;
    const t2 = (S0 + mj) >>> 0;
    h = g; g = f; f = e; e = (d + t1) >>> 0;
    d = c; c = b; b = a; a = (t1 + t2) >>> 0;
  }
  const out = new Uint8Array(32);
  const H = [a + 0x6a09e667, b + 0xbb67ae85, c + 0x3c6ef372, d + 0xa54ff53a,
             e + 0x510e527f, f + 0x9b05688c, g + 0x1f83d9ab, h + 0x5be0cd19];
  for (let i = 0; i < 8; i++) {
    const v = H[i] >>> 0;
    out[i * 4] = (v >>> 24) & 0xff;
    out[i * 4 + 1] = (v >>> 16) & 0xff;
    out[i * 4 + 2] = (v >>> 8) & 0xff;
    out[i * 4 + 3] = v & 0xff;
  }
  return out;
}

function atobBytes(value) {
  return new Uint8Array(Buffer.from(value, 'base64'));
}

function hexToBytes(value) {
  const out = new Uint8Array(value.length >> 1);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(value.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function bytesToBase64(bytes) {
  return Buffer.from(bytes).toString('base64');
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const text = await response.text();
  let payload;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    throw new Error(`接口返回非 JSON：${text.slice(0, 300)}`);
  }
  if (!response.ok) {
    throw new Error(payload?.message || `HTTP ${response.status}`);
  }
  return payload;
}

async function runVm(baseUrl, challenge) {
  const wasmResponse = await fetch(`${baseUrl}/static/wasm/checkin-vm-v4.wasm`, { cache: 'no-cache' });
  if (!wasmResponse.ok) throw new Error(`获取签到 WASM 失败：${wasmResponse.status}`);
  const wasmBuffer = await wasmResponse.arrayBuffer();
  const instance = await WebAssembly.instantiate(wasmBuffer, {
    env: {
      abort: (_p, line, col, code) => {
        throw new Error(`wasm abort at ${line}:${col}:${code}`);
      }
    }
  });
  const {
    set_bytecode_len, init_sha_k, decrypt_bytecode, decrypt_table, build_inv_op_table,
    run, get_output_lo, get_output_hi, memory
  } = instance.instance.exports;

  const bytecode = atobBytes(challenge.bytecode_b64);
  const mem = new Uint8Array(memory.buffer);
  if (bytecode.length > 4088) throw new Error('bytecode too large');
  init_sha_k();
  mem.set(bytecode, 0);

  const table = challenge.secret_table || [];
  if (table.length !== 256) throw new Error(`table size: ${table.length}`);
  for (let i = 0; i < 256; i++) {
    const item = table[i];
    if (!item || item.length !== 16) throw new Error(`bad table hex at ${i}`);
    for (let j = 0; j < 8; j++) mem[4096 + i * 8 + j] = parseInt(item.slice(j * 2, j * 2 + 2), 16);
  }

  const opTable = challenge.op_table || [];
  for (let i = 0; i < 10; i++) mem[8320 + i] = parseInt(opTable[i] || '00', 16);

  const xorKey = hexToBytes(challenge.xor_key || '');
  if (xorKey.length !== 16) throw new Error(`xor key length ${xorKey.length}`);
  mem.set(xorKey, 8704);

  set_bytecode_len(bytecode.length);
  decrypt_bytecode();
  decrypt_table();
  build_inv_op_table();
  const code = run();
  if (code !== 0) throw new Error(`vm run failed: code=${code}`);

  const lo = get_output_lo() >>> 0;
  const hi = get_output_hi() >>> 0;
  const hex = hi.toString(16).padStart(8, '0') + lo.toString(16).padStart(8, '0');
  const bytes = new Uint8Array(8);
  bytes[0] = lo & 255;
  bytes[1] = (lo >>> 8) & 255;
  bytes[2] = (lo >>> 16) & 255;
  bytes[3] = (lo >>> 24) & 255;
  bytes[4] = hi & 255;
  bytes[5] = (hi >>> 8) & 255;
  bytes[6] = (hi >>> 16) & 255;
  bytes[7] = (hi >>> 24) & 255;
  return { hex, bytes };
}

function solvePow(vmBytes, difficulty) {
  const buf = new Uint8Array(16);
  for (let i = 0; i < 8; i++) buf[i] = vmBytes[i];
  const targetByte = Math.floor(difficulty / 8);
  const targetBit = 8 - (difficulty % 8);
  const targetMask = (0xff << targetBit) & 0xff;
  let nonce = 0n;
  for (;;) {
    const n = nonce;
    buf[8] = Number(n & 0xffn);
    buf[9] = Number((n >> 8n) & 0xffn);
    buf[10] = Number((n >> 16n) & 0xffn);
    buf[11] = Number((n >> 24n) & 0xffn);
    buf[12] = Number((n >> 32n) & 0xffn);
    buf[13] = Number((n >> 40n) & 0xffn);
    buf[14] = Number((n >> 48n) & 0xffn);
    buf[15] = Number((n >> 56n) & 0xffn);
    const hash = sha256Fast16(buf);
    let ok = true;
    for (let j = 0; j < targetByte; j++) {
      if (hash[j] !== 0) { ok = false; break; }
    }
    if (ok && targetByte < 32 && (hash[targetByte] & targetMask) !== 0) ok = false;
    if (ok) return nonce.toString();
    nonce++;
    if (nonce > (1n << 30n)) throw new Error('pow timeout');
  }
}

async function encryptPayload(payload, vmBytes, nonceText) {
  const keyMaterial = new Uint8Array(16);
  for (let i = 0; i < 8; i++) keyMaterial[i] = vmBytes[i];
  const nonce = BigInt(nonceText);
  for (let i = 0; i < 8; i++) keyMaterial[8 + i] = Number((nonce >> BigInt(i * 8)) & 0xffn);
  const digest = await crypto.webcrypto.subtle.digest('SHA-256', keyMaterial);
  const key = await crypto.webcrypto.subtle.importKey('raw', digest, { name: 'AES-GCM' }, false, ['encrypt']);
  const iv = crypto.webcrypto.getRandomValues(new Uint8Array(12));
  const data = new TextEncoder().encode(JSON.stringify(payload));
  const ciphertext = await crypto.webcrypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, data);
  return { iv: bytesToBase64(iv), ciphertext: bytesToBase64(new Uint8Array(ciphertext)) };
}

function panelSize() {
  const winW = Number(process.env.NEWAPI_VIEWPORT_W || 800);
  const width = Math.max(280, Math.min(760, winW - 80));
  return {
    w: Math.round(width),
    h: Math.round(width * 460 / 760),
    dotR: Math.max(12, Math.round(width * 18 / 760)),
  };
}

function makeInteraction(challenge, panel) {
  const dots = challenge.dots || [];
  const trajectory = [];
  const dotClicks = [];
  let t = 120;
  let px = panel.w * 0.18;
  let py = panel.h * 0.22;

  const jitter = (seed) => Math.sin(seed * 12.9898) * 2.2;
  const moveTo = (tx, ty, steps, duration) => {
    const sx = px;
    const sy = py;
    for (let i = 1; i <= steps; i++) {
      const ratio = i / steps;
      const ease = ratio * ratio * (3 - 2 * ratio);
      const x = sx + (tx - sx) * ease + jitter(i + t) * (1 - ratio);
      const y = sy + (ty - sy) * ease + jitter(i + t + 7) * (1 - ratio);
      trajectory.push([x, y, t + duration * ratio]);
    }
    t += duration;
    px = tx;
    py = ty;
  };

  trajectory.push([px, py, t]);
  if ((challenge.type || 'click') === 'swipe') {
    dots.forEach((dot) => {
      const x = dot.x_pct * panel.w;
      const y = dot.y_pct * panel.h;
      moveTo(x, y, 18, 520);
    });
  } else {
    dots.forEach((dot, index) => {
      const x = dot.x_pct * panel.w;
      const y = dot.y_pct * panel.h;
      moveTo(x, y, 16 + index, 480 + index * 45);
      t += 70 + index * 15;
      trajectory.push([x + 0.4, y - 0.3, t - 28]);
      trajectory.push([x, y, t - 8]);
      dotClicks.push({ dot_index: index, click: [x, y, t] });
      t += 120;
    });
  }
  return { trajectory, dotClicks };
}

async function main() {
  const baseUrl = (process.env.NEWAPI_BASE_URL || '').replace(/\/+$/, '');
  const cookie = process.env.NEWAPI_COOKIE || '';
  const accessToken = (process.env.NEWAPI_ACCESS_TOKEN || '').replace(/^Bearer\s+/i, '').trim();
  const userId = process.env.NEWAPI_USER_ID || '';
  const referer = process.env.NEWAPI_REFERER || `${baseUrl}/profile`;
  if (!baseUrl || (!cookie && !accessToken) || !userId) throw new Error('缺少 NEWAPI_BASE_URL / NEWAPI_COOKIE 或 NEWAPI_ACCESS_TOKEN / NEWAPI_USER_ID');

  const headers = {
    'User-Agent': process.env.NEWAPI_USER_AGENT || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Origin': baseUrl,
    'Referer': referer,
    'New-Api-User': userId,
  };
  if (cookie) headers.Cookie = cookie;
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;

  const challengePayload = await fetchJson(`${baseUrl}/api/user/checkin/challenge`, { headers });
  if (challengePayload?.success === false) {
    console.log(JSON.stringify(challengePayload));
    return;
  }
  const challenge = challengePayload?.data;
  if (!challenge?.challenge_id) throw new Error('挑战获取失败');

  const vm = await runVm(baseUrl, challenge);
  const nonce = solvePow(vm.bytes, Number(challenge.pow_difficulty || 0));
  const panel = panelSize();
  const interaction = makeInteraction(challenge, panel);
  const verificationPayload = {
    trajectory: interaction.trajectory,
    dot_clicks: interaction.dotClicks,
    automation_signals: {
      webdriver: false,
      nightmare: false,
      phantom: false,
      playwright: false,
      selenium: false,
      plugins_zero: false,
      languages_zero: false,
      cdp_attached: false,
    },
    viewport: { w: Number(process.env.NEWAPI_VIEWPORT_W || 800), h: Number(process.env.NEWAPI_VIEWPORT_H || 600), dpr: 1 },
    panel_size: { w: panel.w, h: panel.h },
  };
  const encrypted = await encryptPayload(verificationPayload, vm.bytes, nonce);
  const submitPayload = await fetchJson(`${baseUrl}/api/user/checkin/submit`, {
    method: 'POST',
    headers: { ...headers, 'Content-Type': 'application/json;charset=UTF-8', 'X-Client-Fingerprint': '' },
    body: JSON.stringify({
      challenge_id: challenge.challenge_id,
      vm_output: vm.hex,
      pow_nonce: Number(nonce),
      iv: encrypted.iv,
      ciphertext: encrypted.ciphertext,
    }),
  });
  console.log(JSON.stringify(submitPayload));
}

main().catch((error) => {
  console.log(JSON.stringify({ success: false, message: error?.message || String(error) }));
  process.exit(2);
});
