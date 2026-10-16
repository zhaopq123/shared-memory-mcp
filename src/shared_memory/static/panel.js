/* ══════════════════════════════════════════════════════════════
   共享记忆面板 · 前端逻辑

   三个标签页：
     · 记忆库 —— 工作区树/时间脊柱 + 阅读器（主界面）
     · 体系   —— 视图入口（决策/踩坑/待办/时效告警…）
     · 设置   —— 给 agent 的指令 + 写入规则 + 运行参数 + 凭据管理

   ━━━ 与后端的关系 ━━━
   面板**只读记忆、可写配置**。记忆的写入只能走 MCP 工具 ——
   那条路径有队列串行 + git 提交 + 软删除保护，在面板开写入口等于绕过它们。

   配置可以写，但**走同一套 service/队列**（后端保证），所以并发安全。

   ━━━ 安全要点（两条，都很关键）━━━

   ① **本文件里没有 token 变量。**

      旧做法是从地址栏读 `?token=` —— 那等于把钥匙贴在门牌上
      （浏览器历史、书签、Referer、截图、代理日志里全都有）。
      实测中真的因此泄露过一次。

      新做法：凭据只存在 **httpOnly cookie** 里 ——
        · JS 读不到（所以 XSS 也偷不走）
        · URL 里没有（所以链接可以安全地分享/收藏）
      同源 fetch 会自动带上它，前端不需要知道凭据长什么样。
      无 cookie 时显示登录框，用长期 token 换一次性票据。

   ② **所有来自后端的文本在插入 DOM 前都过 esc()。**
      Markdown 渲染的顺序是「先整体转义，再套标记」—— 反了就是 XSS。
   ══════════════════════════════════════════════════════════════ */

/* ─────────────── 基础工具 ─────────────── */

/** HTML 转义。**所有**服务端文本进 DOM 前都必须过它。 */
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

/** 作者色：确定性哈希。
 *  同一个 agent 永远同一个颜色 —— 多 agent 场景下，
 *  "这条是谁写的"是最重要的线索之一，但又不该占版面。 */
function authorColor(name){
  let h = 0; const s = String(name || '?');
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  const palette = ['#d9a441','#7fa88a','#c96a5c','#8f9ec9','#c58fb0','#8fb8c9','#b9a06a'];
  return palette[h % palette.length];
}
function avatar(name){
  const c = authorColor(name);
  const ch = (String(name||'?').trim()[0] || '?').toUpperCase();
  return `<span class="who"><span class="av" style="background:${c}">${esc(ch)}</span>${esc(name)}</span>`;
}

/** 查询串构造。
 *  ⚠️ 注意里面**没有 token** —— 凭据走 cookie，URL 保持干净。 */
const qs = (o) => Object.entries(o).filter(([,v]) => v !== '' && v != null)
      .map(([k,v]) => k + '=' + encodeURIComponent(v)).join('&');

/** 统一的 GET。凭据靠同源 cookie 自动携带。
 *
 *  401 时自动弹登录框（而不是静默失败）—— 否则用户看到的是
 *  一个空面板，不知道发生了什么。
 *
 *  ⚠️ 登录前的那次探测**不打 error 日志**：面板加载时会立刻调
 *  `/api/state`，而此时还没有 cookie（用户还没登录）—— 那是
 *  正常流程，不是故障。但如果它进了 console.error，
 *  收尾验收脚本就会看到"有错误"，把正常状态误判成有问题
 *  （实测踩过：为此白查了一轮）。
 */
const api = (p, o = {}) => {
  const q = qs(o);
  return fetch(p + (q ? '?' + q : ''), { credentials: 'same-origin' })
    .then(async r => {
      if (r.status === 401) return handle401();
      return r.json();
    })
    .catch(e => ({ok:false, message:String(e)}));
};

/** 统一的 POST。同样不带凭据参数。 */
const apiPost = (p, body) => fetch(p, {
  method: 'POST',
  credentials: 'same-origin',
  headers: {'Content-Type':'application/json'},
  body: JSON.stringify(body),
}).then(async r => {
  /* ⚠️ 登录接口的失败原因必须原样透出。
     实测踩过：401 一律吞成 needLogin
     → 服务端明明回了“凭据无效”，
     界面却只能显示笼统的“用户名或密码不正确”：
     用户看不出到底是密码错还是账号未设密码。
     其他接口的 401 仍走 showLogin（会话过期＝要重新登录）。 */
  if (r.status === 401 && p !== '/api/auth/login') return handle401();
  return r.json().catch(() => ({ok:false, message:'HTTP ' + r.status}));
}).catch(e => ({ok:false, message:String(e)}));

/* ─────────────── 登录 ─────────────── */

/** 显示登录遮罩。
 *
 *  为什么需要它：服务端现在**强制鉴权**（绑非 loopback 时），
 *  没有凭据就什么都拿不到。与其显示一堆空列表（用户不知道是坏了
 *  还是没登录），不如明确要求登录。
 *
 *  输入的是**长期 token**，但只在 POST body 里出现一次 ——
 *  换来一次性票据，再由服务端换成 httpOnly cookie。
 *  所以 token **不会进入地址栏/历史**。
 */
/** 登录遮罩 —— 账号密码（给**人**用的正经登录）。
 *
 *  ━━━ 为什么不是 token 输入框（用户明确的反馈）━━━
 *
 *  第一版是"贴一长串 token"。技术上能跑，但**产品上是错的**：
 *
 *  · token 是给 **agent** 用的（它不会记密码，只能存配置文件）
 *  · 人用的是**账号密码** —— 好记、能改、能认出是谁
 *  · 让人类贴 64 位 hex 字符串，等于让用户替架构的缺陷买单
 *
 *  账号密码的后端一直都有（scrypt 哈希 + `/api/auth/login` 收
 *  username/password），只是面板 UI 抄了近路。现在补上。
 *
 *  ━━━ 保留的降级入口 ━━━
 *  底部留了一行"用 token 登录"的小字链接 —— 给"刚拿到账号还没设密码"
 *  或"密码忘了"的场景兜底，不作为主路径。
 */
let refreshTimer = null;

/** 401 的统一处理 —— **不要**无条件糊上全屏登录遮罩。
 *
 *  ━━━ 为什么（2026-10-15 用户实测暴案）━━━
 *
 *  服务端重启 → 会话（只存内存，刻意不落盘）全失效 → 面板的 15s 轮询
 *  拿到 401 → `showLogin()` 把遮罩**追加到 body 末尾** →
 *  用户明明已经登录、页面上数据都还在，却看到底部挂着一个"需要登录"。
 *  用户的原话是"我他妈都登上了这还有个需要登录在最底下"。
 *
 *  判据很简单：**页面上已经有数据** → 那只是会话过期，用非阻断横幅
 *  （数据留着、能继续看、点一下重新登录）；真的什么都没有 → 才需要遮罩。
 *
 *  ⚠️ 顺带停掉轮询：不停的话每 15s 都会再 401 一次，横幅反复弹。
 */
function handle401(){
  if (STATE.notes && STATE.notes.length){
    if (refreshTimer){ clearInterval(refreshTimer); refreshTimer = null; }
    showSessionBanner();
    return {ok:false, needLogin:true, expired:true};
  }
  showLogin();
  return {ok:false, needLogin:true};
}

/** 会话过期横幅（替代"全屏遮罩盖住已有内容"）。 */
function showSessionBanner(){
  if (document.getElementById('sess-banner')) return;
  const b = document.createElement('div');
  b.id = 'sess-banner';
  b.innerHTML = `
    <span>${LANG === 'zh'
      ? '会话已过期（服务端重启或登录超时）—— 下面是最后一次取到的数据。'
      : 'Session expired (server restarted or timed out). Below is the last snapshot.'}</span>
    <button class="btn primary" id="sess-relogin">${
      LANG === 'zh' ? '重新登录' : 'Sign in again'}</button>`;
  document.body.appendChild(b);
  document.getElementById('sess-relogin').onclick =
    () => location.replace(location.pathname);
}

/** 登录入口：先问服务端"要哪种凭据"，再渲染对应表单。
 *
 *  ━━━ 为什么要先问 ━━━
 *
 *  PIN 与账号密码是两套完全不同的表单，猜错会让用户看到不相干的东西。
 *  `GET /api/auth/pin` 是**免鉴权**的极窄接口（就是为登录屏准备的）：
 *  只回 enabled / locked / wait_seconds / panel_open 四个字段，不含任何数据。
 */
function showLogin(msg){
  if (document.getElementById('login-mask')) return;
  fetch('/api/auth/pin', {headers: {'Accept': 'application/json'}})
    .then(r => r.json())
    .then(info => {
      const d = info || {};
      // 服务端说"这个面板不用登录"（非公开绑定 + 未设 PIN）→ 直接重载进面板
      if (d.panel_open){ location.replace(location.pathname); return; }
      if (d.enabled){ renderPinLogin(d, msg); } else { renderFormLogin(msg); }
    })
    .catch(() => renderFormLogin(msg));   // 探不到就回到最保守的账号密码
}

/** PIN 登录屏：**6 个数字盒**。
 *
 *  每个细节都是有意的：
 *   · 输入即前进（不用点下一个盒子）；退格回上一个
 *   · 填满 6 位**自动提交**（少一次点击）
 *   · **回车提交当前长度** —— PIN 允许 4~8 位，6 位只是最常见形态；
 *     硬等 6 位会让设了 4 位 PIN 的人永远进不去
 *   · 支持直接粘贴整串
 *   · 锁定期把剩余秒数显示出来（干等会让人以为坏了）
 */
function renderPinLogin(info, msg){
  if (document.getElementById('login-mask')) return;
  const n = 6;
  const boxes = Array.from({length: n}, () =>
    '<input class="pinbox" type="password" inputmode="numeric" maxlength="1" ' +
    'autocomplete="off" aria-label="PIN">').join('');
  const mask = document.createElement('div');
  mask.id = 'login-mask';
  mask.innerHTML = `
    <div class="loginbox">
      <div class="seal" style="margin:0 auto 14px">记</div>
      <h2>${t('login.title')}</h2>
      <p class="sectip" style="text-align:center;margin-bottom:18px">
        ${LANG === 'zh' ? '输入 PIN 进入面板' : 'Enter your PIN to unlock'}
      </p>
      <div id="login-err" class="errbox" style="display:none"></div>
      <div id="pinwrap">${boxes}</div>
      <p class="sectip" id="pin-wait" style="text-align:center;display:none"></p>
      <button class="btn primary" id="login-btn" style="width:100%;margin-top:16px">
        ${t('login.button')}
      </button>
      <p class="sectip" style="text-align:center;margin:14px 0 0;font-size:12px">
        <a href="javascript:void 0" id="login-pw-link">${
          LANG === 'zh' ? '用账号密码登录' : 'Use username & password'}</a>
      </p>
    </div>`;
  document.body.appendChild(mask);

  const els = [...mask.querySelectorAll('.pinbox')];
  const btn = document.getElementById('login-btn');
  const errBox = document.getElementById('login-err');
  const waitEl = document.getElementById('pin-wait');
  let busy = false;

  if (msg){ errBox.style.display = ''; errBox.textContent = msg; }
  if (info && info.locked){
    errBox.style.display = '';
    errBox.textContent = (LANG === 'zh' ? 'PIN 已锁定，请 ' : 'PIN locked, retry in ')
      + (info.wait_seconds || 0) + (LANG === 'zh' ? ' 秒后再试' : 's');
  }
  els[0].focus();

  const value = () => els.map(e => e.value).join('');
  const clear = () => {
    els.forEach(e => { e.value = ''; });
    els[0].focus();
  };

  const submit = async () => {
    const pin = value();
    if (busy || pin.length < 4) return;
    busy = true;
    btn.disabled = true; btn.textContent = t('login.submitting');
    const r = await apiPost('/api/auth/login', {pin});
    btn.disabled = false; btn.textContent = t('login.button');
    busy = false;
    if (!r.ok){
      errBox.style.display = '';
      errBox.textContent = r.message || t('login.invalid');
      clear();
      if (r.error === 'pin_locked'){
        waitEl.style.display = '';
        waitEl.textContent = r.message || '';
      }
      return;
    }
    location.replace(r.login_url);
  };

  els.forEach((el, i) => {
    el.oninput = () => {
      el.value = el.value.replace(/\D/g, '').slice(0, 1);
      if (el.value && i < n - 1) els[i + 1].focus();
      if (value().length === n) submit();
    };
    el.onkeydown = (e) => {
      if (e.key === 'Backspace' && !el.value && i > 0){
        e.preventDefault();
        els[i - 1].focus();
        els[i - 1].value = '';
      } else if (e.key === 'Enter'){
        e.preventDefault();
        submit();
      }
    };
    el.onpaste = (e) => {
      const txt = ((e.clipboardData || window.clipboardData || {})
        .getData('text') || '').replace(/\D/g, '').slice(0, n);
      if (!txt) return;
      e.preventDefault();
      els.forEach(x => { x.value = ''; });
      [...txt].forEach((ch, k) => { els[k].value = ch; });
      els[Math.min(txt.length, n - 1)].focus();
      if (txt.length === n) submit();
    };
  });

  btn.onclick = submit;
  document.getElementById('login-pw-link').onclick = () => {
    const m = document.getElementById('login-mask');
    if (m) m.remove();
    renderFormLogin();
  };
}

function renderFormLogin(msg){
  if (document.getElementById('login-mask')) return;
  const mask = document.createElement('div');
  mask.id = 'login-mask';
  mask.innerHTML = `
    <div class="loginbox">
      <div class="seal" style="margin:0 auto 14px">记</div>
      <h2>${t('login.title')}</h2>
      <p class="sectip" style="text-align:center;margin-bottom:20px">
        ${t('login.hint')}
      </p>
      <div id="login-err" class="errbox" style="display:none"></div>
      <div class="field" style="margin-bottom:14px">
        <label for="login-user">${t('login.username')}</label>
        <input type="text" id="login-user" placeholder="${t('login.usernamePh')}"
               autocomplete="username" spellcheck="false" style="width:100%">
      </div>
      <div class="field" style="margin-bottom:6px">
        <label for="login-pw">${t('login.password')}</label>
        <input type="password" id="login-pw" placeholder="${t('login.passwordPh')}"
               autocomplete="current-password" style="width:100%">
      </div>
      <button class="btn primary" id="login-btn" style="width:100%;margin-top:14px">
        ${t('login.button')}
      </button>
      <p class="sectip" style="text-align:center;margin:14px 0 0;font-size:12px">
        <a href="javascript:void 0" id="login-token-link">用 token 登录</a>
        <span style="color:var(--muted)"> · 仅用于账号还没设密码时</span>
      </p>
    </div>`;
  document.body.appendChild(mask);

  const userEl = document.getElementById('login-user');
  const pwEl = document.getElementById('login-pw');
  const btn = document.getElementById('login-btn');
  const errBox = document.getElementById('login-err');
  if (msg) { errBox.style.display = ''; errBox.textContent = msg; }
  userEl.focus();

  /** 提交登录。username/password 任一为空就不发（省一次无谓请求）。 */
  const doLogin = async () => {
    const username = userEl.value.trim();
    const pw = pwEl.value;
    if (!username || !pw) {
      errBox.style.display = '';
      errBox.textContent = LANG === 'zh' ? '用户名和密码都要填' : 'Both fields are required';
      return;
    }
    btn.disabled = true; btn.textContent = t('login.submitting');
    const r = await apiPost('/api/auth/login', {username, password: pw});
    btn.disabled = false; btn.textContent = t('login.button');
    if (!r.ok){
      errBox.style.display = '';
      errBox.textContent = r.message || t('login.invalid');
      pwEl.select();
      return;
    }
    /* 用拿到的票据走一次「URL 换 cookie」流程：
       服务端会 set-cookie 并 302 到干净 URL。
       票据是一次性 + 2 分钟过期，即使泄露也几乎无价值。 */
    location.replace(r.login_url);
  };
  btn.onclick = doLogin;
  // 回车在用户名框里 → 跳到密码框（不是提交，因为密码还空着）
  userEl.onkeydown = (e) => {
    if (e.key === 'Enter'){ e.preventDefault(); pwEl.focus(); }
  };
  pwEl.onkeydown = (e) => { if (e.key === 'Enter') doLogin(); };

  // 降级入口：切成 token 输入框（密码忘了/账号未设密码时用）
  document.getElementById('login-token-link').onclick = () => {
    mask.innerHTML = `
      <div class="loginbox">
        <div class="seal" style="margin:0 auto 14px">记</div>
        <h2>${t('login.title')}</h2>
        <p class="sectip" style="text-align:center;margin-bottom:18px">
          ${LANG === 'zh'
            ? '贴入 token 登录（它只用于换取会话，不会出现在地址栏）：'
            : 'Paste a token to sign in (it only exchanges for a session and never appears in the URL):'}
        </p>
        <div id="login-err" class="errbox" style="display:none"></div>
        <input type="password" id="login-token" placeholder="token"
               autocomplete="off" spellcheck="false">
        <button class="btn primary" id="login-btn" style="width:100%;margin-top:12px">
          ${t('login.button')}
        </button>
        <p class="sectip" style="text-align:center;margin:14px 0 0;font-size:12px">
          <a href="javascript:void 0" id="login-back-link">← 用账号密码登录</a>
        </p>
      </div>`;
    const input = document.getElementById('login-token');
    const btn2 = document.getElementById('login-btn');
    const err2 = document.getElementById('login-err');
    input.focus();
    const go = async () => {
      const tok = input.value.trim();
      if (!tok) return;
      btn2.disabled = true; btn2.textContent = t('login.submitting');
      const r = await apiPost('/api/auth/login', {token: tok});
      btn2.disabled = false; btn2.textContent = t('login.button');
      if (!r.ok){
        err2.style.display = '';
        err2.textContent = r.message || t('login.invalid');
        return;
      }
      location.replace(r.login_url);
    };
    btn2.onclick = go;
    input.onkeydown = (e) => { if (e.key === 'Enter') go(); };
    document.getElementById('login-back-link').onclick = () => {
      const m = document.getElementById('login-mask');
      if (m){ m.remove(); }
      showLogin();
    };
  };
}

/** 退出登录：清 cookie 后回到登录态。 */
async function logout(){
  if (!confirm(t('account.logoutConfirm'))) return;
  const r = await apiPost('/api/auth/logout', {});
  // 无论后端结果如何，本地都回到登录态（cookie 可能已失效）
  location.href = location.pathname;
}

/** 相对时间。超过 30 天就显示日期 —— "93 天前"没人能换算。 */
function when(s){
  if (!s) return '–';
  const d = new Date(s); if (isNaN(d)) return s;
  const m = Math.floor((Date.now() - d) / 60000);
  if (m < 1) return '刚刚';
  if (m < 60) return m + ' 分钟前';
  const h = Math.floor(m / 60); if (h < 24) return h + ' 小时前';
  const dd = Math.floor(h / 24); if (dd < 30) return dd + ' 天前';
  return s.slice(0, 10);
}
const dayKey = (s) => (s || '').slice(0, 10);
function dayLabel(k){
  const today = new Date().toISOString().slice(0, 10);
  const y = new Date(Date.now() - 864e5).toISOString().slice(0, 10);
  if (k === today) return '今天 · ' + k;
  if (k === y) return '昨天 · ' + k;
  return k;
}

/* ─────────────── Markdown 渲染 ───────────────
   顺序关键：**先整体转义，再套标记**。反了就是 XSS。
   代码块先切出来占位，避免内部的 # * | 被后续规则误伤。 */
function md(src){
  const raw = String(src || '');
  const fences = [];
  let h = esc(raw).replace(/```(\w*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    fences.push('<pre><code>' + code.replace(/\n$/, '') + '</code></pre>');
    return '\x00F' + (fences.length - 1) + '\x00';
  });
  h = h
    .replace(/(?:^\|.*\|[ \t]*\n?)+/gm, (tbl) => {
      const rows = tbl.trim().split('\n').map(r =>
        r.replace(/^\||\|$/g, '').split('|').map(c => c.trim()));
      if (rows.length < 2) return tbl;
      const isSep = (r) => r.every(c => /^:?-{2,}:?$/.test(c));
      const head = rows[0];
      const body = rows.slice(isSep(rows[1]) ? 2 : 1);
      /* 外层 .tablewrap 负责横向滚动；表格 width:max-content 由内容决定列宽。
         这样中文长表头不会把列压塌（实测踩过，见 panel.css 注释）。 */
      return '<div class="tablewrap"><table><thead><tr>' +
             head.map(c => '<th>' + c + '</th>').join('') +
             '</tr></thead><tbody>' + body.map(r =>
             '<tr>' + r.map(c => '<td>' + c + '</td>').join('') + '</tr>').join('') +
             '</tbody></table></div>';
    })
    .replace(/^#### (.*)$/gm, '<h4>$1</h4>')
    .replace(/^### (.*)$/gm, '<h3>$1</h3>')
    .replace(/^## (.*)$/gm, '<h2>$1</h2>')
    .replace(/^# (.*)$/gm, '<h1>$1</h1>')
    .replace(/^&gt; (.*)$/gm, '<blockquote>$1</blockquote>')
    .replace(/^(?:---|\*\*\*|___)[ \t]*$/gm, '<hr>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*(?=[^*]|$)/g, '$1<em>$2</em>')
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\[\[([^\[\]]+?)\]\]/g, '<code title="记忆引用">$1</code>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/^[ \t]*[-*] (.*)$/gm, '<li>$1</li>')
    .replace(/(?:<li>[\s\S]*?<\/li>\n?)+/g, m => '<ul>' + m + '</ul>')
    .replace(/(?:<blockquote>[\s\S]*?<\/blockquote>\n?)+/g,
             m => m.replace(/<\/blockquote>\s*<blockquote>/g, '<br>'));
  h = h.split(/\n{2,}/).map(p =>
      /^\s*<(h\d|ul|ol|pre|table|blockquote|hr)/.test(p) ? p : '<p>' + p + '</p>')
    .join('');
  return h.replace(/\x00F(\d+)\x00/g, (m, i) => fences[i]);
}

/** 从正文抽标题目录 */
function tocOf(src){
  const out = [];
  String(src || '').replace(/^(#{1,3}) (.+)$/gm, (m, hashes, t) => {
    if (hashes.length <= 3) out.push({lvl: hashes.length, text: t.trim()});
    return m;
  });
  return out;
}
/** 标题 → 锚点 id */
function slugify(s){
  return String(s).trim().toLowerCase()
    .replace(/[^\w\u4e00-\u9fa5]+/g, '-').replace(/^-|-$/g, '').slice(0, 60);
}
/** 阅读时长估算（中日韩字符按字计，其它按词计） */
function readingTime(src){
  const s = String(src || '');
  const cjk = (s.match(/[\u4e00-\u9fa5]/g) || []).length;
  const words = s.replace(/[\u4e00-\u9fa5]/g, ' ').split(/\s+/).filter(Boolean).length;
  return {mins: Math.max(1, Math.round((cjk + words * 1.3) / 400)), chars: s.length};
}

/** 关键词高亮（搜索时用）。先转义再插入 mark，防 XSS。 */
function highlight(text, q){
  const t = esc(text);
  if (!q) return t;
  let out = t;
  for (const p of q.trim().split(/\s+/).filter(Boolean)){
    out = out.replace(new RegExp('(' + p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi'),
                      '<span class="mk">$1</span>');
  }
  return out;
}

/* ─────────────── 全局状态 ─────────────── */
/** 面板构建指纹（由 `<script src="panel.js?v=xxx">` 带进来）。
 *
 *  为什么要显示它：用户实测反馈过"改了但一点没动"—— 根因是浏览器在跑
 *  缓存的旧 panel.js。页面上一旦有指纹，"你看的是哪个版本"就不需要猜，
 *  截个图就能判断。
 */
const PANEL_VER = (() => {
  try {
    const el = document.querySelector('script[src*="panel.js"]');
    const m = el && el.getAttribute('src').match(/v=([0-9a-f]+)/);
    return m ? m[1] : '';
  } catch (e) { return ''; }
})();

const STATE = {
  tab: 'library',              // library | admin | settings
  groupBy: 'workspace',        // workspace（默认）| time | type
  //: 生效域（v2 · S-7.2）：long-term（默认，与 agent 的闸一致）| task | all
  scope: 'long-term',
  notes: [], tags: [], workspaces: [],
  activeTag: '', activeWs: '', q: '', cur: null,
  view: 'overview', viewData: null,
  config: null, cfgDirty: false,
  backlinks: null,
  /* ── 多租户状态 ──
     me    当前身份（用户名/角色/组）—— 顶栏显示"我是谁"
     libs  可读库列表（含 writable 标记）—— 左栏顶部做库筛选
     activeLib 当前选中的库（'' = 全部库）
     这三个由 /api/state 返回，后端已就绪（见 dashboard.py 的 state 路由）。 */
  me: null, libs: [], activeLib: '',
};

/** 当前身份是否管理员（决定是否显示用户/token 管理入口）。 */
const isAdmin = () => !!(STATE.me && STATE.me.user && STATE.me.user.is_admin);

/* ═══════════════ 标签页 1：记忆库 ═══════════════ */

/* ═══════════════ 左栏分组：工作区树 / 时间脊柱 ═══════════════

   ━━━ 为什么默认按工作区分组 ━━━

   这是**多项目共用的库**。第一眼该回答的问题是「这个项目记了什么」，
   而不是「今天记了什么」——后者是时间视图要回答的第二个问题。

   所以两个视图可切换，默认工作区。

   ━━━ 层级从哪来 ━━━

   工作区名里的 ``-`` 是分层分隔符：``乱七八糟-前端`` 属于 ``乱七八糟``
   之下。用户给的两个例子（``乱七八糟-xxx`` / ``个人工作中枢-xxxx``）
   都是这个形态，所以直接支持，不做额外配置。

   没有 ``-`` 的名字就是顶层（如 ``乱七八糟`` 本身）。
*/

/** 把工作区名拆成层级路径。
 *  ``"乱七八糟-前端-web"`` → ``["乱七八糟", "前端", "web"]``
 *  ``"乱七八糟"`` → ``["乱七八糟"]``
 */
function wsPath(name){
  return String(name || '').split('-').map(s => s.trim()).filter(Boolean);
}

/** 把扁平的记忆列表构造成工作区树。
 *
 *  返回形如：
 *    { name, full, count, items: [...], children: [ ... ] }
 *
 *  count 是**子树总数**（点击父节点筛选时应包含所有子项）——
 *  这个数字比只算直接子项有用得多。
 */
function buildWsTree(notes){
  const root = {name: '', full: '', count: 0, items: [], children: new Map()};

  for (const n of notes){
    const ws = n.workspace || '';
    const node = ws ? ensurePath(root, wsPath(ws)) : root;
    if (!ws) (root.unassigned = root.unassigned || []).push(n);
    else node.items.push(n);
    node.count += 1;
    // 沿路径累加计数（祖先也 +1）
    if (ws){
      let cur = root;
      for (const seg of wsPath(ws)){
        cur = cur.children.get(seg);
        cur.count += 1;
      }
    }
  }

  const finalize = (node) => {
    node.childList = [...node.children.values()].map(finalize)
      .sort((a, b) => b.count - a.count);   // 条目多的排前面
    return node;
  };
  finalize(root);
  return root;
}

function ensurePath(root, segments){
  let cur = root;
  for (const seg of segments){
    if (!cur.children.has(seg)){
      cur.children.set(seg, {
        name: seg,
        full: (cur.full ? cur.full + '-' : '') + seg,
        count: 0, items: [], children: new Map(),
      });
    }
    cur = cur.children.get(seg);
  }
  return cur;
}

/** 记忆条目 HTML。
 *  带**时效与可信度标记** —— 读取端要能一眼看出"这条已过期/这是推断"，
 *  不必点进去读了才知道。这是"不幻觉"落到 UI 上的那一步。 */
function itemHtml(n, indent){
  const badges = [];
  const ex = n.expiry || {};
  if (ex.state === 'expired') badges.push('<span class="badge expired" title="' + esc(ex.detail||'') + '">已过期</span>');
  else if (ex.state === 'review') badges.push('<span class="badge review" title="' + esc(ex.detail||'') + '">待复查</span>');
  else if (ex.state === 'superseded') badges.push('<span class="badge superseded" title="' + esc(ex.detail||'') + '">已取代</span>');
  if (n.claim === 'inference') badges.push('<span class="badge claim-inference" title="未核实的推断，引用前当心">推断</span>');
  else if (n.claim === 'proposal') badges.push('<span class="badge claim-proposal">建议</span>');
  else if (n.claim === 'fact') badges.push('<span class="badge claim-fact">事实</span>');
  if (n.type && n.type !== 'note') badges.push('<span class="badge">' + esc(n.type) + '</span>');

  /* ── 共享档位徽标（v2 · S-10.4）──
     显示的是**生效档位**：条目没显式标 share 时后端已经按空间默认档
     解析好放在 share_effective 里了。前端不再自己判一遍继承 ——
     同一件事的口径只允许有一处（后端 effective_share）。
     没有档位（旧后端 / 内部视图没给）就不显示，不猜。 */
  const share = n.share_effective || n.share || '';
  if (share){
    const zh2 = LANG === 'zh';
    const SH = {
      mine:   [zh2 ? '仅自己' : 'Private',  'badge share-mine',
               zh2 ? '只有你（作者）能看见这条' : 'Visible to the author only'],
      space:  [zh2 ? '本空间' : 'Space',    'badge share-space',
               zh2 ? '同空间（同库）的读者都能看见' : 'Visible to readers of this space'],
      shared: [zh2 ? '已共享' : 'Shared',   'badge share-shared',
               zh2 ? '已授权的空间可跨空间检索到它' : 'Authorized spaces may retrieve it'],
    }[share];
    /* ── 徽标可点改档（v2 · S-7.5）──
       面板是阅览室，正文/标题/标签不能在这里改 —— 但**档位是权限**，
       "这条给不给人看"恰好是看完之后马上想做的事。
       点下去走 /api/share/set → TenantHub.set_share，
       与 agent 调 memory_remember 是同一条写通道（队列 + 校验 + 快照）。 */
    if (SH) badges.push('<span class="' + SH[1] + ' share-edit" data-share="' + esc(share) +
                        '" data-ident="' + esc(n.title || n.id || '') +
                        '" data-lib="' + esc(n._lib || STATE.activeLib || '') +
                        '" role="button" tabindex="0" title="' + esc(SH[2]) +
                        (zh2 ? '（点击改档位）' : '') + '">' + esc(SH[0]) + '</span>');
  }

  /* 按工作区分组时不重复显示工作区标签（组名已经说明了）；
     按时间分组时要显示，否则看不出这条属于哪个项目。 */
  const showWs = STATE.groupBy === 'time' && n.workspace;

  /* ── 来源库标记（多租户关键）──
     跨库搜索时结果来自多个库，而**来源决定可信度**：
     公司库的结论比某个人的个人笔记更权威。
     只在"全部库"视图下显示（选了具体库时已经知道来源了，不必重复）。
     后端在每个结果里带了 _lib_label（见 tenancy.search_all）。 */
  const showLib = !STATE.activeLib && n._lib_label;

  /* ── 搜索命中说明（可读性关键）──
     搜索是**全文匹配**：正文命中也会返回。但用户只看到标题时
     会困惑"这条跟我的搜索词有什么关系？"—— 所以标出命中位置。
     这不是装饰：它让"为什么这条在结果里"变得自明。 */
  let hitNote = '';
  if (STATE.q && n.title && !n.title.toLowerCase().includes(STATE.q.toLowerCase().trim())){
    const where = [];
    if ((n.tags||[]).some(t => t.toLowerCase().includes(STATE.q.toLowerCase().trim())))
      where.push('标签');
    if ((n.workspace||'').toLowerCase().includes(STATE.q.toLowerCase().trim()))
      where.push('工作区');
    if (where.length === 0) where.push('正文');
    hitNote = `<div class="hitnote">命中：${where.join('/')}</div>`;
  }

  return `
    <div class="item${STATE.cur === n.title ? ' on' : ''}${indent ? ' indented' : ''}"
         data-id="${esc(n.title)}"${n._lib ? ` data-lib="${esc(n._lib)}"` : ''}>
      <div class="t">${highlight(n.title, STATE.q)}</div>
      <div class="sub">
        <span>${when(n.updated || n.created)}</span>
        ${showLib ? `<span class="libtag">${esc(n._lib_label)}</span>` : ''}
        ${showWs ? `<span class="wstag">${esc(n.workspace)}</span>` : ''}
        ${n.author ? avatar(n.author) : ''}
        ${badges.join('')}
      </div>
      ${hitNote}
      ${(n.tags||[]).filter(t => !String(t).startsWith('ws/')).length
        ? '<div class="tags-inline">' + n.tags
            .filter(t => !String(t).startsWith('ws/'))
            .map(t => `<span class="tag" data-tag="${esc(t)}">${esc(t)}</span>`).join('') + '</div>'
        : ''}
    </div>`;
}

/** 左栏的「空」提示 —— 分情况说清是**哪种**空。
 *
 *  ━━━ 为什么值得单独一个函数 ━━━
 *  原来只有一句「没有匹配的记忆」，但实测发现两种完全不同的场景
 *  被混成一句：
 *
 *    · 筛选用光了（搜索词/标签/库）→ 用户该改条件
 *    · 库本来就是空的（如公司库刚建、测试数据已清）→ 用户该往里写
 *
 *  第二种尤其重要：切到公司库看到**一片空白**，用户会以为面板坏了，
 *  而不是"这个库还没内容"。说清楚能省掉一次误报。
 */
function emptyListHtml(){
  const reasons = [];
  if (STATE.q) reasons.push('搜索「' + STATE.q + '」');
  if (STATE.activeTag) reasons.push('标签「' + STATE.activeTag + '」');
  if (STATE.activeWs) reasons.push('工作区「' + STATE.activeWs + '」');
  if (STATE.activeLib){
    const lib = (STATE.libs || []).find(l => l.id === STATE.activeLib);
    reasons.push('库「' + (lib ? (lib.label || lib.id) : STATE.activeLib) + '」');
  }

  if (reasons.length){
    return `<div class="empty"><span class="big">◌</span>
      ${reasons.join(' + ')} 下没有记忆<br>
      <span style="font-size:12px">清掉筛选条件（点库 chip 的「全部」或 Esc）看看</span>
    </div>`;
  }
  return `<div class="empty"><span class="big">◌</span>
    ${t('list.emptyLib')}<br>
    <span style="font-size:12px">写一条进来吧 —— 落到这个库就会显示在这里</span>
  </div>`;
}

/* ── 折叠状态持久化 ──
   用户在哪个组上折过，刷新/切页后应该记住 —— 否则每次回来都要重折一遍。 */
function collapsedSet(){
  try {
    return new Set(JSON.parse(localStorage.getItem('shm-collapsed') || '[]'));
  } catch { return new Set(); }
}
function toggleCollapsed(key){
  const s = collapsedSet();
  if (s.has(key)) s.delete(key); else s.add(key);
  try { localStorage.setItem('shm-collapsed', JSON.stringify([...s])); } catch {}
}

function renderList(){
  const box = document.getElementById('list');
  if (!STATE.notes.length){
    /* 用 emptyListHtml() 而不是原来的一句「没有匹配的记忆」——
       它分得清「筛选用光了」和「库本来是空的」，用户才知道下一步
       该改条件还是该写内容。实测发现两种场景被混成一句时，
       切到空库的用户会以为面板坏了。 */
    box.innerHTML = emptyListHtml();
    return;
  }
  box.innerHTML = (STATE.groupBy === 'time')
    ? renderByTime()
    : (STATE.groupBy === 'type' ? renderByType() : renderByWorkspace());
  wireListEvents(box);
}

/** 按工作区分组（默认）：可折叠的树 */
function renderByWorkspace(){
  const tree = buildWsTree(STATE.notes);
  const collapsed = collapsedSet();

  // 递归渲染一个节点
  const nodeHtml = (node, depth) => {
    const key = 'ws:' + (node.full || '(未分类)');
    const isCollapsed = collapsed.has(key);
    const hasKids = node.childList.length > 0;
    const own = node.items.length;

    // 组头：折叠三角 + 名字 + 计数
    const head = `
      <div class="wshead" data-key="${esc(key)}" data-ws="${esc(node.full)}"
           style="--depth:${depth}">
        <span class="caret">${hasKids ? (isCollapsed ? '▶' : '▼') : '<span class="dot-s"></span>'}</span>
        <span class="wsname">${esc(node.name)}</span>
        <span class="wscount">${node.count}</span>
      </div>`;

    if (isCollapsed) return `<div class="wsgroup">${head}</div>`;

    // 先渲染直接的条目，再渲染子组
    const items = node.items.map(n => itemHtml(n, depth > 0)).join('');
    const kids = node.childList.map(c => nodeHtml(c, depth + 1)).join('');
    return `<div class="wsgroup">${head}${items}${kids}</div>`;
  };

  let html = '';

  // 有工作区的树
  html += tree.childList.map(c => nodeHtml(c, 0)).join('');

  // 未分类（无工作区）单独放最后 —— 它是"待归类"的信号，不该混在项目里
  if (tree.unassigned && tree.unassigned.length){
    const key = 'ws:(未分类)';
    const isCollapsed = collapsed.has(key);
    html += `<div class="wsgroup">
      <div class="wshead unassigned" data-key="${esc(key)}" data-ws="" style="--depth:0">
        <span class="caret">${isCollapsed ? '▶' : '▼'}</span>
        <span class="wsname">未分类</span>
        <span class="wscount">${tree.unassigned.length}</span>
      </div>
      ${isCollapsed ? '' : tree.unassigned.map(n => itemHtml(n, false)).join('')}
    </div>`;
  }

  return html;
}

/** 按时间分组：原来的时间脊柱 */
function renderByTime(){
  const groups = new Map();
  for (const n of STATE.notes){
    const k = dayKey(n.updated || n.created);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(n);
  }
  const keys = [...groups.keys()].sort().reverse();
  const collapsed = collapsedSet();
  return keys.map(k => {
    const ckey = 'day:' + k;
    const isCollapsed = collapsed.has(ckey);
    return `
    <div class="day">
      <div class="daylabel" data-key="${esc(ckey)}" style="cursor:pointer">
        <span class="caret">${isCollapsed ? '▶' : '▼'}</span>${esc(dayLabel(k))}
        <span class="wscount">${groups.get(k).length}</span>
      </div>
      ${isCollapsed ? '' : groups.get(k).map(n => itemHtml(n, false)).join('')}
    </div>`;
  }).join('');
}

/** 按**六型**分组（v2 · S-7.2）。
 *
 *  ━━━ 为什么 type 视图要单独给一个 ━━━
 *
 *  v2 把记忆收敛成六型，其中 ``workflow`` / ``constraint`` 是**可复用资产** ——
 *  "下次开工直接能用"的东西，价值远高于一条普通结论。按工作区分组时它们
 *  被埋在几十条里，等于没有。
 *
 *  所以这个视图把两类资产**置顶并打徽标**，其余按 踩坑→习惯→结论→任务 排。
 *  日常用法：切到类型视图扫一眼置顶区，就知道"这个库里有什么是我该遵守/
 *  该复用的"。
 */
const TYPE_ORDER = ['workflow', 'constraint', 'lesson', 'habit', 'fact', 'task'];
const TYPE_LABEL_ZH = {
  workflow: '工作流', constraint: '约束', lesson: '踩坑',
  habit: '习惯', fact: '结论', task: '任务',
};
//: 可复用资产 —— 置顶 + 打徽标
const ASSET_TYPES = ['workflow', 'constraint'];

function renderByType(){
  const groups = new Map();
  for (const n of STATE.notes){
    const ty = String(n.type || '').trim() || '(未标注)';
    if (!groups.has(ty)) groups.set(ty, []);
    groups.get(ty).push(n);
  }
  const known = [...TYPE_ORDER, '(未标注)'];
  // 已知类型按固定序（workflow/constraint 天然在最前）；未知类型排在后面，
  // 但**不丢**（旧数据里可能有 v1 的 type，看不见比排序乱更糟）
  const keys = known.filter(k => groups.has(k))
    .concat([...groups.keys()].filter(k => !known.includes(k)));
  const collapsed = collapsedSet();
  return keys.map(k => {
    const ckey = 'type:' + k;
    const isCollapsed = collapsed.has(ckey);
    const isAsset = ASSET_TYPES.includes(k);
    const label = LANG === 'zh' ? (TYPE_LABEL_ZH[k] || k) : k;
    const assetTip = LANG === 'zh' ? '可复用资产' : 'reusable asset';
    return `
    <div class="typegroup">
      <div class="wshead tyhead${isAsset ? ' asset' : ''}" data-key="${esc(ckey)}">
        <span class="caret">${isCollapsed ? '▶' : '▼'}</span>
        <span class="wsname">${esc(label)}</span>
        ${isAsset ? `<span class="assetbadge" title="${assetTip}">${
          LANG === 'zh' ? '资产' : 'asset'}</span>` : ''}
        <span class="wscount">${groups.get(k).length}</span>
      </div>
      ${isCollapsed ? '' : groups.get(k).map(n => itemHtml(n, false)).join('')}
    </div>`;
  }).join('');
}

/** 列表里的事件接线（条目点击、标签点击、组头折叠） */
function wireListEvents(box){
  box.querySelectorAll('.item').forEach(el => el.onclick = (e) => {
    if (e.target.classList.contains('tag')) return;   // 标签自己处理
    if (e.target.classList.contains('share-edit')) {   // 徽标改档，不打开阅读器
      openShareMenu(e.target, () => load());
      return;
    }
    select(el.dataset.id);
  });
  box.querySelectorAll('.tag').forEach(el => el.onclick = (e) => {
    e.stopPropagation(); toggleTag(el.dataset.tag);
  });
  box.querySelectorAll('.share-edit').forEach(el => el.onclick = (e) => {
    e.stopPropagation();
    openShareMenu(el, () => load());
  });
  // 折叠：点组头
  box.querySelectorAll('.wshead, .daylabel').forEach(el => el.onclick = (e) => {
    // 点组内的"筛选"按钮时不折叠
    if (e.target.classList.contains('wsfilter')) return;
    toggleCollapsed(el.dataset.key);
    renderList();
  });
}

/* ── 档位改档菜单（v2 · S-7.5）──
 *
 * 这是整个面板**唯一**能写记忆的入口，而且只写一个字段：档位。
 * 说清楚它为什么算例外：正文/标题/标签由 agent 写，人在阅读器里改
 * 会把并发覆盖的门推开；而"这条给不给别人看"是权限，看完就想点，
 * 并且它走的仍是后端那条写通道（/api/share/set → TenantHub.set_share：
 * 过读闸、过写闸、进写队列、存改前快照），**不是**面板直改文件。
 *
 * ⚠️ 看不见就改不了：菜单只出现在已经渲染出来的条目上，而后端
 * set_share 会再判一次 —— 别人的「仅自己」条目对它来说就是不存在。
 */
let SHARE_MENU = null;

function closeShareMenu(){
  if (SHARE_MENU){ SHARE_MENU.remove(); SHARE_MENU = null; }
}

function openShareMenu(anchor, onDone){
  closeShareMenu();
  const zh = LANG === 'zh';
  const cur = anchor.dataset.share || '';
  const ident = anchor.dataset.ident || '';
  const lib = anchor.dataset.lib || '';
  if (!ident) return;
  const opts = [
    ['mine',   zh ? '仅自己' : 'Private',
     zh ? '只有作者能看见' : 'Author only'],
    ['space',  zh ? '本空间' : 'Space',
     zh ? '同库读者都能看见' : 'Readers of this space'],
    ['shared', zh ? '已共享' : 'Shared',
     zh ? '授权过的空间可跨空间检索' : 'Authorized spaces may search it'],
    ['',       zh ? '跟随空间默认' : 'Follow space default',
     zh ? '撤掉这条的显式档位，回到空间设的默认档' : 'Clear this note’s tier and inherit the space default'],
  ];
  const m = document.createElement('div');
  m.className = 'share-menu';
  m.innerHTML = `<div class="sm-head">${zh ? '谁能看见这条' : 'Who can see this note'}</div>` +
    opts.map(([v, l, d]) =>
      `<button class="sm-opt${cur === v ? ' on' : ''}" data-v="${esc(v)}">` +
      `<span>${esc(l)}</span><em>${esc(d)}</em></button>`).join('') +
    `<div class="sm-hint">${zh
      ? '只改可见范围，正文一字不动。改动仍走写队列 —— 与 agent 写记忆同一条路。'
      : 'Only the tier changes; the body stays byte-identical. Goes through the same write queue as the agent.'}</div>`;
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  const w = m.offsetWidth || 230;
  m.style.top = (r.bottom + window.scrollY + 4) + 'px';
  m.style.left = Math.max(6, Math.min(r.left + window.scrollX, window.innerWidth - w - 10)) + 'px';
  SHARE_MENU = m;

  m.querySelectorAll('.sm-opt').forEach(btn => btn.onclick = async (e) => {
    e.stopPropagation();
    const v = btn.dataset.v;
    if (v === cur) { closeShareMenu(); return; }
    btn.disabled = true;
    const res = await apiPost('/api/share/set', {ident, lib, share: v});
    closeShareMenu();
    if (!res || res.ok === false){
      alert((res && (res.message || res.code)) || (zh ? '改档失败' : 'Failed'));
      return;
    }
    if (onDone) onDone();
  });
}

/* 点别处 / Esc 就收起来 —— 菜单浮在内容上，不关掉会挡住下一条 */
document.addEventListener('click', (e) => {
  if (SHARE_MENU && !SHARE_MENU.contains(e.target)) closeShareMenu();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeShareMenu();
});

function renderChips(){
  /* 分组方式切换（工作区 / 时间）。
     为什么不放在顶栏：它只影响左栏，放在左栏顶部离作用对象最近。
     选择记进 localStorage —— 用户的偏好该跨刷新保留。 */
  const gb = document.getElementById('groupby');
  gb.innerHTML = `
    <span class="rowlabel" style="margin:0 8px 0 0">分组</span>
    <div class="seg">
      <button class="segbtn${STATE.groupBy === 'workspace' ? ' on' : ''}" data-g="workspace">工作区</button>
      <button class="segbtn${STATE.groupBy === 'time' ? ' on' : ''}" data-g="time">时间</button>
      <button class="segbtn${STATE.groupBy === 'type' ? ' on' : ''}" data-g="type">类型</button>
    </div>`;
  gb.querySelectorAll('.segbtn').forEach(el => el.onclick = () => {
    STATE.groupBy = el.dataset.g;
    try { localStorage.setItem('shm-groupby', STATE.groupBy); } catch {}
    renderChips(); renderList();
  });

  /* 这里原来是"14 个标签 chips" —— 用户实测反馈左栏太乱（13 个标签
     + 7 个库 chips 挤在顶部），要求"只按工作空间分类"。所以标签不再
     铺一排，改成**只在有筛选时**显示一条状态条（见 filterBarHtml）。 */
  const c = document.getElementById('chips');
  c.innerHTML = filterBarHtml();
  c.querySelectorAll('[data-clear]').forEach(el => el.onclick = () => {
    const k = el.dataset.clear;
    const qi = document.getElementById('q');
    if (k === 'all'){
      STATE.q = ''; STATE.activeTag = ''; STATE.activeWs = ''; STATE.activeLib = '';
      if (qi) qi.value = '';
    } else if (k === 'q'){ STATE.q = ''; if (qi) qi.value = ''; }
    else if (k === 'tag') STATE.activeTag = '';
    else if (k === 'ws') STATE.activeWs = '';
    else if (k === 'lib') STATE.activeLib = '';
    load();
  });
}

/** 当前筛选条件的可见提示 —— 有筛选就**必须**显示。
 *
 *  ━━━ 为什么必须（2026-10-15 用户实测暴案）━━━
 *
 *  用户点了一个标签（小红书）→ 列表只剩 4 条 → 顶栏"4 条记忆"、
 *  左栏工作区树只剩两个组 → 用户的结论是"我的数据全没了，有 bug"。
 *  实际库里 70 条、乱七八糟 31 条，一条没丢。
 *
 *  原来的实现只在**列表为空**时才解释筛选条件（见 emptyListHtml）——
 *  有 4 条命中时界面上一个字的提示都没有。这是纯可用性缺陷。
 */
function filterBarHtml(){
  const parts = [];
  if (STATE.q) parts.push(['q', '搜索「' + STATE.q + '」']);
  if (STATE.activeTag) parts.push(['tag', '标签「' + STATE.activeTag + '」']);
  if (STATE.activeWs) parts.push(['ws', '工作区「' + STATE.activeWs + '」']);
  if (STATE.activeLib){
    const l = (STATE.libs || []).find(x => x.id === STATE.activeLib);
    parts.push(['lib', '库「' + (l ? (l.label || l.id) : STATE.activeLib) + '」']);
  }
  if (!parts.length) return '';
  return `
    <div class="filterbar">
      <span class="rowlabel" style="margin:0">筛选中</span>
      ${parts.map(([k, txt]) =>
        `<button class="fchip" data-clear="${k}">${esc(txt)}<span class="x">×</span></button>`).join('')}
      ${parts.length > 1 ? '<button class="fclear" data-clear="all">全部清除</button>' : ''}
    </div>`;
}

function toggleTag(t){
  STATE.activeTag = STATE.activeTag === t ? '' : t;
  load();
}

/** 自动上下文条 + 生效域三 chip + 会话卡（v2 · S-7.2）。
 *
 *  ━━━ 为什么这些要摆在记忆页最上面 ━━━
 *
 *  它们回答的是同一个问题："**现在这一屏，是谁的角度、看的是什么范围**"。
 *   · 上下文条：agent 开工会被自动喂什么（约束 N 条 + 在做什么任务）
 *   · scope 三 chip：我现在看的是长期口径、任务过程态、还是全部（考古）
 *   · 会话卡：这个任务的可见性是谁能看见
 *
 *  这三件事此前全是"隐形的自动行为"。越是自动的东西越要能看见 ——
 *  否则它喂错东西的时候，用户连该从哪儿查都不知道。
 */
async function loadContextBar(){
  const bar = document.getElementById('ctxbar');
  const chips = document.getElementById('scopechips');
  const card = document.getElementById('sesscard');
  if (!bar) return;
  const zh = LANG === 'zh';
  const r = await api('/api/context');
  if (!r || !r.ok){
    bar.innerHTML = '';
    if (chips) chips.innerHTML = '';
    if (card) card.innerHTML = '';
    return;
  }
  const sess = r.session;
  const cs = r.constraints || [];

  bar.innerHTML = `
    <div class="ctxbar">
      <span class="ctxdot${sess ? ' on' : ''}"></span>
      <span>${zh ? '自动上下文' : 'Auto context'}：
        ${sess
          ? (zh ? '正在做「' + esc(sess.task) + '」' : 'on ' + esc(sess.task))
          : (zh ? '无活跃会话' : 'no active session')}
        · ${zh ? '约束' : 'constraints'} ${cs.length}
      </span>
    </div>`;

  /* 生效域三档：默认 long-term —— 与 agent 的默认闸一致。
     切到"全部"是**考古模式**（会看到别的会话的任务过程态与已归档旧账），
     所以标签里直说"考古"，别让人以为那是常态。 */
  if (chips){
    const cur = STATE.scope || 'long-term';
    const items = [
      ['long-term', zh ? '长期' : 'Long-term'],
      ['task', zh ? '任务域' : 'Task'],
      ['all', zh ? '全部 · 考古' : 'All · archive'],
    ];
    chips.innerHTML = items.map(([v, label]) =>
      `<button class="chip${cur === v ? ' on' : ''}" data-scope="${v}">${
        esc(label)}</button>`).join('');
    chips.querySelectorAll('[data-scope]').forEach(el => el.onclick = () => {
      STATE.scope = el.dataset.scope;
      load();
    });
  }

  if (!card) return;
  if (!sess){
    card.innerHTML = '';
    return;
  }
  const vs = [
    ['private', zh ? '仅自己' : 'Private'],
    ['space', zh ? '本空间' : 'Space'],
    ['shared', zh ? '已共享' : 'Shared'],
  ];
  card.innerHTML = `
    <div class="sesscard">
      <div class="sesshead">${zh ? '当前会话' : 'Session'} · ${esc(sess.task)}</div>
      <div class="sessrow">
        ${vs.map(([v, l]) =>
          `<button class="chip${(sess.visibility || 'private') === v ? ' on' : ''}" data-vis="${v}">${
            esc(l)}</button>`).join('')}
      </div>
      <div class="sesshint">${zh
        ? '会话可见性只管「这个任务的存在别人能不能看见」，与库权限无关。'
        : 'Session visibility is about the task, not library permissions.'}</div>
    </div>`;
  card.querySelectorAll('[data-vis]').forEach(el => el.onclick = async () => {
    const v = el.dataset.vis;
    const r2 = await apiPost('/api/session/visibility',
      {task: sess.task, visibility: v});
    if (r2.ok) loadContextBar();
  });
}

/** 治理抽屉（v2 · S-7.4）：体检报告。
 *
 *  ━━━ 这里**没有**任何"一键清理"━━━
 *
 *  不是还没做，是不做。自动清理是最容易把人的东西弄丢的一类功能：
 *  它在你没看着的时候跑，出错了也不会有报错弹给你。
 *  所以抽屉里只有三样东西：**数字**（现状）、**候选**（哪几条像重复/过期）、
 *  **建议**（走取代链而不是删除）。真要动，由人或 agent 显式执行。
 */
async function openGovernance(){
  const d = document.getElementById('gov-drawer');
  if (!d) return;
  const zh = LANG === 'zh';
  d.style.display = '';
  d.innerHTML = `<div class="drawer"><div class="empty">${t('common.loading')}</div></div>`;
  const r = await api('/api/governance');
  if (!r || !r.ok){
    d.innerHTML = `<div class="drawer"><div class="empty">${
      esc((r && r.message) || (zh ? '体检失败' : 'failed'))}</div></div>`;
    return;
  }
  const dups = r.duplicates || [];
  const pct = (v) => `${Math.round((v || 0) * 100)}%`;
  d.innerHTML = `
    <div class="drawer">
      <div class="drawerhead">
        <h3>${zh ? '体检' : 'Checkup'}</h3>
        <button class="btn" id="gov-close">${zh ? '关闭' : 'Close'}</button>
      </div>
      <div class="gaugegrid">
        <div class="gauge"><div class="gv">${r.total ?? '–'}</div>
          <div class="gk">${zh ? '总条数' : 'Total'}</div></div>
        <div class="gauge"><div class="gv">${r.growth_30d ?? '–'}</div>
          <div class="gk">${zh ? '近 30 天新增' : 'Added 30d'}</div></div>
        <div class="gauge"><div class="gv">${pct(r.duplicate_rate)}</div>
          <div class="gk">${zh ? '重复率' : 'Dup rate'}</div></div>
        <div class="gauge"><div class="gv">${r.expired_count ?? 0}</div>
          <div class="gk">${zh ? '已过期' : 'Expired'}</div></div>
        <div class="gauge"><div class="gv">${r.superseded_count ?? 0}</div>
          <div class="gk">${zh ? '已被取代' : 'Superseded'}</div></div>
        <div class="gauge"><div class="gv">${r.low_reference_count ?? 0}</div>
          <div class="gk">${zh ? '低引用候选' : 'Low-ref'}</div></div>
      </div>
      ${dups.length ? `
        <h4 class="sechead" style="font-size:13px;margin-top:18px">${
          zh ? '疑似重复（' + r.duplicate_clusters + ' 簇）' : 'Possible duplicates'}</h4>
        ${dups.map(c => `
          <div class="dupcard">
            <div class="dupwhy">${esc(c.reason)}</div>
            <ul>${(c.titles || []).map(t => `<li>${esc(t)}</li>`).join('')}</ul>
            <div class="dupsug">${esc(c.suggestion || '')}</div>
          </div>`).join('')}` : `
        <p class="sectip" style="margin-top:16px">${
          zh ? '没有检测到疑似重复。' : 'No duplicates detected.'}</p>`}
      ${(r.expired_sample || []).length ? `
        <h4 class="sechead" style="font-size:13px;margin-top:16px">${
          zh ? '已过期（默认检索不再返回，考古可查）' : 'Expired'}</h4>
        <ul class="govlist">${r.expired_sample.map(t => `<li>${esc(t)}</li>`).join('')}</ul>` : ''}
      <p class="redline">${
        zh ? '面板是阅览室：这里只出报告与建议，<strong>没有任何自动删除或改写</strong>。'
             + '真要清理，由你或 agent 显式执行。'
           : 'The panel is a reading room: reports and suggestions only. Nothing is auto-deleted.'}</p>
    </div>`;
  document.getElementById('gov-close').onclick = () => { d.style.display = 'none'; };
}

async function load(){
  const r = await api('/api/state',
    {q: STATE.q, tag: STATE.activeTag, workspace: STATE.activeWs,
     lib: STATE.activeLib, scope: STATE.scope || 'long-term'});
  const dot = document.getElementById('dot');
  if (!r.ok){ dot.classList.add('bad'); return; }
  dot.classList.remove('bad');

  STATE.notes = r.notes || [];
  STATE.tags = r.tags || [];
  STATE.workspaces = r.workspaces || [];
  STATE.stats = r.stats || {};
  /* 身份与库列表：后端已返回，前端用来做库筛选与权限提示。
     注意 me/libs 是**服务端按你的权限算出来的**，前端只负责显示。 */
  STATE.me = r.me || null;
  STATE.libs = r.libs || [];

  /* ⚠️ 数字必须说清是"筛后"还是"全部"。
     实测暴案：用户点了一个标签 → 顶栏显示"4 条记忆" → 结论是
     "我这两天做的全没了，是不是有 bug"。库里其实 70 条一条没丢。
     有筛选时显示 "4 / 70"。 */
  const nAll = r.stats.notes_all;
  document.getElementById('s-notes').textContent =
    (nAll != null && nAll !== r.stats.notes)
      ? (r.stats.notes + ' / ' + nAll) : r.stats.notes;
  document.getElementById('s-libs').textContent = r.stats.libs;
  /* 「个工作区」统计**真实的项目数**（排除"(无工作区)"这个分类桶）。
     注意显示 0 是合法且有意义的：说明库里还没有按项目归类的记忆 ——
     这正是"该给记忆标工作区了"的信号，不该被藏起来。 */
  const named = (STATE.workspaces || []).filter(([n]) => n !== '(无工作区)');
  document.getElementById('s-ws').textContent = named.length;
  document.getElementById('s-tags').textContent = r.stats.tags;
  const pend = (r.stats.write_queue && r.stats.write_queue.pending) || 0;
  document.getElementById('s-queue').textContent = pend;
  document.getElementById('st-q').classList.toggle('warn', pend > 0);

  /* 视图 chip（v2 · S-7.2）：跟着记忆页一起出现，不用先切 tab。
     失败不阻断 —— 视图条拉不到不该让整个列表打不开。 */
  try { await loadViewBar(STATE.view || 'overview'); } catch (e) {}
  /* 上下文条 / scope chip / 会话卡：同样是"拉不到不影响看记忆"的辅助块 */
  try { await loadContextBar(); } catch (e) {}

  /* 判故障要看是否**用过**：写者循环惰性创建，新服务里 worker_alive
     本来就是 false —— 那不是故障。判定逻辑必须与服务端 /health 一致，
     否则会出现"面板说坏了但 /health 说健康"的矛盾。 */
  const q = r.stats.write_queue || {};
  const used = (q.processed > 0) || (q.pending > 0) || (q.outstanding > 0);
  if (q.worker_alive === false && used) dot.classList.add('bad');

  const bad = r.stats.unreadable || 0;
  const sb = document.getElementById('s-bad');
  if (sb){
    sb.parentElement.style.display = bad ? '' : 'none';
    sb.textContent = bad;
  }

  /* 顶栏显示"我是谁" —— 多租户下这是最基本的信息：
     不知道自己的身份与权限，就无法判断"为什么看不到某个库"。 */
  const who = document.getElementById('s-who');
  const role = document.getElementById('s-role');
  if (STATE.me && STATE.me.user){
    const u = STATE.me.user;
    const displayName = u.display || u.name;
    const roleText = u.is_admin ? t('role.admin') : t('role.member');
    const g = (u.groups || []).join('/');
    /* ⚠️ 显示名与角色**相同时不重复显示**（实测踩过）：
     *
     * 用户名 admin 的显示名被设成「管理员」，而角色也是 admin →
     * 顶栏出现「管理员 管理员」，看起来像渲染 bug。
     *
     * 判据：显示名已经等于角色名时，第二段就不再显示角色
     * （信息重复，而且会让人以为界面坏了）。 */
    who.textContent = displayName;
    role.textContent = (displayName === roleText)
      ? (g || '')
      : roleText + (g ? ' · ' + g : '');
  } else {
    who.textContent = '–'; role.textContent = '';
  }

  const rootEl = document.getElementById('root');
  if (rootEl) rootEl.textContent = (STATE.me && STATE.me.user)
    ? (STATE.me.user.name + ' @ ' + STATE.libs.length + ' ' + t('stat.libs')
       + (PANEL_VER ? ' · build ' + PANEL_VER : ' · build ?'))
    : '';

  renderLibChips();
  renderChips(); renderList();

  // 首次/筛选后自动选中：否则右栏永远是空的等待提示，看着像坏了
  if (STATE.notes.length && !STATE.notes.some(n => n.title === STATE.cur)){
    select(STATE.notes[0].title);
  } else if (STATE.cur){
    // 保持选中项高亮同步（重新渲染后 class 会丢）
    const el = document.querySelector(`.item[data-id="${CSS.escape(STATE.cur)}"]`);
    if (el) el.classList.add('on');
  }
}

/** 渲染库筛选条（左栏顶部）。
 *
 *  ━━━ 为什么这是多租户下最重要的一块 UI ━━━
 *
 * 记忆分散在多个库里（公司 / 小组 / 个人），而**默认是跨库搜索**。
 * 用户看到的每条记忆都带 `_lib_label` 来源标注，但如果没有"按库筛选"，
 * 就无法回答"这个库里到底有什么"。
 *
 * 只读库（如公司库对 member）会带一个"只读"标记 —— 让用户**提前知道**
 * 写不进去，而不是写完了才拿到 403。
 */
function renderLibChips(){
  const box = document.getElementById('libchips');
  if (!box) return;
  const libs = STATE.libs || [];
  if (!libs.length){ box.innerHTML = ''; return; }

  /* 库 = **权限边界**（谁能读、谁能写），必须能一眼看到自己在哪 ——
     但 7 个 chip 横铺在左栏顶部，加上标签 chips 一共 20 个按钮，
     视觉噪音太大（用户实测反馈"这一块太乱了"）。收成一个下拉：
     信息一个不少，版面只剩一行；只读库直接在下拉项里标出来，
     省得用户写进去才吃到 403。 */
  const note = (l) => {
    let n = l.owner;
    if (l.owner === 'company') {
      n = t('share.company') + ' / ' + t('share.companyDesc');
    } else if (String(l.owner).startsWith('group:')) {
      const g = l.owner.slice(6);
      n = t('share.group', {g}) + ' / ' + t('share.groupDesc', {g});
    } else if (String(l.owner).startsWith('user:')) {
      n = t('share.self') + ' / ' + t('share.selfDesc');
    }
    if (!l.writable) n += ' · ' + t('badge.readonly');
    return n;
  };

  box.innerHTML = `
    <div class="rowlabel">库（${libs.length}）</div>
    <select id="lib-select">
      <option value="">全部库</option>
      ${libs.map(l => `<option value="${esc(l.id)}" title="${esc(note(l))}"${
        STATE.activeLib === l.id ? ' selected' : ''}>${esc(l.label || l.id)}${
        l.writable ? '' : '（只读）'}</option>`).join('')}
    </select>`;

  const sel = document.getElementById('lib-select');
  sel.onchange = () => { STATE.activeLib = sel.value || ''; load(); };
}

async function select(title){
  STATE.cur = title;
  /* 从"视图模式"切回"读某条"（v2 · S-7.2）——视图与条目共用右栏，
     不切回来会看到视图内容盖在条目上。 */
  const vp = document.getElementById('kbody');
  if (vp) vp.style.display = 'none';
  const nd2 = document.getElementById('notedoc');
  if (nd2) nd2.style.display = '';
  /* 记住这条来自哪个库 —— 同名条目可能存在于多个库，
     不传 lib 时后端会按可读库顺序返回第一个，未必是用户点的那条。
     所以从列表项上取 data-lib 传回去，保证"点哪条开哪条"。 */
  const el = document.querySelector(`.item[data-id="${CSS.escape(title)}"]`);
  const lib = (el && el.dataset.lib) || STATE.activeLib || '';

  const reader = document.getElementById('reader');
  reader.innerHTML = `<div class="doc"><div class="empty">${t('common.loading')}</div></div>`;
  renderList();
  const r = await api('/api/note', {ident: title, lib});
  if (!r.ok){
    reader.innerHTML = `<div class="doc"><div class="empty">${
      esc(r.message || t('doc.readFail'))}</div></div>`;
    return;
  }
  /* ⚠️ `_lib` / `_lib_label` 可能在**响应外层**，也可能在 `note` 里。
   *
   * 实测踩过：后端把这两个字段提到外层（为了和 list/search 的条目
   * 结构一致），但前端还在读 `note._lib_label` —— 结果详情页的
   * 「来源库」标记一直是空的（列表里有、详情没有）。
   *
   * 教训：**改字段位置时两端都要改**，或者像这样两边都兼容。
   * 只改一端就是静默失效。 */
  const note = r.note || {};
  if (note._lib === undefined && r._lib) note._lib = r._lib;
  if (note._lib_label === undefined && r._lib_label) note._lib_label = r._lib_label;

  // 反向引用单独拉（不阻塞正文渲染）。同样带上库 ——
  // 引用是**库内**概念，跨库统计会泄露"另一库存在某个标题"。
  STATE.backlinks = null;
  api('/api/backlinks', {title, lib}).then(b => {
    STATE.backlinks = b;
    const holder = document.getElementById('relwrap');
    if (holder && b.ok) holder.innerHTML = backlinksHtml(b);
    wireRelItems();
  });
  render(note);
  reader.scrollTop = 0;
}

function backlinksHtml(b){
  const parts = [];
  if ((b.backlinks||[]).length){
    parts.push('<h3>谁引用了它</h3><div class="rel">' + b.backlinks.map(t =>
      `<div class="relitem" data-id="${esc(t)}"><span style="color:${authorColor('')}">↩</span>
       <span>${esc(t)}</span></div>`).join('') + '</div>');
  }
  if ((b.outgoing||[]).length){
    parts.push('<h3>它引用了谁</h3><div class="rel">' + b.outgoing.map(t =>
      `<div class="relitem" data-id="${esc(t)}"><span style="color:${authorColor('')}">↪</span>
       <span>${esc(t)}</span></div>`).join('') + '</div>');
  }
  return parts.join('');
}

function wireRelItems(){
  document.querySelectorAll('.relitem').forEach(el => el.onclick = () => select(el.dataset.id));
}

function render(n){
  const reader = document.getElementById('reader');
  const rt = readingTime(n.body);
  const toc = tocOf(n.body);
  const related = STATE.notes
    .filter(x => x.title !== n.title &&
                 (x.tags || []).some(t => (n.tags || []).includes(t)))
    .slice(0, 5);
  /* 分享链接：**不带任何凭据**。
   *
   * 旧版是 `?token=<长期凭据>` —— 那等于把钥匙贴在门牌上：
   * 复制给谁、发到哪里，都等于把访问权一起给出去了（而且会进
   * 浏览器历史/书签/截图）。实测正是这样泄露过一次。
   *
   * 现在链接只有 id，**对方必须自己有凭据**才能打开 ——
   * 这既保住了"能分享位置"，又不泄漏访问权。 */
  const shareUrl = location.origin + location.pathname +
                   '?id=' + encodeURIComponent(n.title);

  /* 正文里如果第一行就是与标题同名的 H1，去掉它 ——
     否则页面标题、目录、正文各出现一次，同一句话读三遍（实测发现）。 */
  let bodySrc = String(n.body || '');
  const firstLine = (bodySrc.trim().split('\n')[0] || '');
  const stripTitle = /^\s*#\s/.test(firstLine) &&
        firstLine.replace(/^\s*#\s+/, '').trim() === String(n.title).trim();
  if (stripTitle) bodySrc = bodySrc.replace(/^\s*#\s+.+?\s*$/m, '').replace(/^\s+/, '');

  /* 给标题加锚点 id，让目录能跳 */
  let bodyHtml = md(bodySrc);
  const seen = new Map();
  bodyHtml = bodyHtml.replace(/<(h[1-3])>(.*?)<\/\1>/g, (m, tag, inner) => {
    let id = slugify(inner.replace(/<[^>]+>/g, ''));
    if (seen.has(id)) { seen.set(id, seen.get(id) + 1); id += '-' + seen.get(id); }
    else seen.set(id, 0);
    return `<${tag} id="${id}">${inner}</${tag}>`;
  });
  // 正文里的 H1 与页面标题重复 → 从目录剔除
  const tocItems = stripTitle ? toc.filter(t => t.lvl > 1) : toc;

  // 体系信息条：类型/可信度/时效/工作区
  const metaBadges = [];
  if (n.type && n.type !== 'note') metaBadges.push(`<span class="badge">${esc(n.type)}</span>`);
  if (n.claim) metaBadges.push(`<span class="badge claim-${esc(n.claim)}">${
      esc({fact:'✓ 事实', inference:'? 推断', proposal:'◇ 建议'}[n.claim] || n.claim)}</span>`);
  if (n['superseded-by']) metaBadges.push(
    `<span class="badge superseded">已被「${esc(n['superseded-by'])}」取代</span>`);
  if (n['valid-until']) metaBadges.push(`<span class="badge">有效期至 ${esc(n['valid-until'])}</span>`);
  if (n['review-after']) metaBadges.push(`<span class="badge review">复查：${esc(n['review-after'])}</span>`);
  if (n.workspace) metaBadges.push(`<span class="wstag">${esc(n.workspace)}</span>`);
  /* 共享档位（v2 S-10.4）：读单条时必须看到"这条能被谁看到" ——
     它决定这条能不能直接引用给别人。取后端算好的生效档位。 */
  const dShare = n.share_effective || n.share || '';
  if (dShare){
    const dZh = LANG === 'zh';
    const dLabel = {mine: dZh ? '仅自己' : 'Private',
                    space: dZh ? '本空间' : 'Space',
                    shared: dZh ? '已共享' : 'Shared'}[dShare];
    if (dLabel) metaBadges.push(`<span class="badge share-${esc(dShare)} share-edit" data-share="${
        esc(dShare)}" data-ident="${esc(n.title || n.id || '')}" data-lib="${
        esc(n._lib || STATE.activeLib || '')}" role="button" tabindex="0" title="${
        esc(dZh ? '点击改档位' : 'Click to change tier')}">${esc(dLabel)}</span>`);
  }
  /* 来源库：读单条时也要显示 —— 用户需要知道"这条属于哪个库"，
     因为它决定了这条能被谁看到、以及是否权威（见 dashboard 的来源标注设计）。 */
  if (n._lib_label) metaBadges.push(`<span class="libtag">${esc(n._lib_label)}</span>`);

  reader.innerHTML = `
  <div class="doc">
    <h1 class="dtitle">${esc(n.title)}</h1>
    <div class="dmeta">
      <span>${esc((n.updated || n.created || '').replace('T', ' ').slice(0, 16))}</span>
      ${n.author ? avatar(n.author) : ''}
      <span>${rt.chars} 字 · 约 ${rt.mins} 分钟</span>
      <div class="actions">
        <button class="abtn" id="cp-link">🔗 复制链接</button>
        <button class="abtn" id="cp-md">⧉ 复制原文</button>
      </div>
    </div>
    ${metaBadges.length ? '<div class="dtags">' + metaBadges.join('') + '</div>' : ''}
    ${(n.tags||[]).length ? '<div class="dtags">' + n.tags.map(t =>
      `<span class="tag" data-tag="${esc(t)}">${esc(t)}</span>`).join('') + '</div>' : ''}
    ${tocItems.length > 1 ? '<div class="tocwrap"><h3>目录</h3><div class="toc">' +
      tocItems.map(t =>
        `<a data-goto="${esc(slugify(t.text))}" class="${t.lvl > 1 ? 'l2' : ''}">${esc(t.text)}</a>`
      ).join('') + '</div></div>' : ''}
    <div class="body">${bodyHtml}</div>
    <div id="relwrap"></div>
    ${related.length ? '<div class="sect"><h3>共享标签的相关记忆</h3><div class="rel">' +
      related.map(x => `<div class="relitem" data-id="${esc(x.title)}">
        <span style="color:${authorColor(x.author)}">•</span>
        <span>${esc(x.title)}</span>
        <span class="n">${(x.tags||[]).filter(t => (n.tags||[]).includes(t)).join(' · ')}</span>
      </div>`).join('') + '</div></div>' : ''}
    <div class="sect" id="verwrap">
      <h3>${t('doc.verhistory')}</h3>
      <div id="ver-body"><p class="sectip" style="margin:0">${t('doc.verloading')}</p></div>
    </div>
  </div>`;

  reader.querySelectorAll('.tag').forEach(el => el.onclick = () => toggleTag(el.dataset.tag));
  /* 阅读器里的档位徽标同样可点（v2 · S-7.5）—— 改完重读这一条，
     否则徽标还停在旧档位，用户会以为没生效。 */
  reader.querySelectorAll('.share-edit').forEach(el => el.onclick = (e) => {
    e.stopPropagation();
    openShareMenu(el, () => select(el.dataset.ident));
  });
  reader.querySelectorAll('a[data-goto]').forEach(el => el.onclick = () => {
    const t = document.getElementById(el.dataset.goto);
    if (t) t.scrollIntoView({behavior:'smooth', block:'start'});
  });
  wireRelItems();

  const linkBtn = reader.querySelector('#cp-link');
  linkBtn.onclick = () => copyText(shareUrl, linkBtn, '🔗 复制链接');
  const mdBtn = reader.querySelector('#cp-md');
  mdBtn.onclick = () => copyText(n.body || '', mdBtn, '⧉ 复制原文');
  loadVersions(n);
}

/** 版本历史（v2 · S-7.8）：打开哪条才查哪条，不阻塞正文。
 *
 *  ━━━ 为什么不再是"整库 git log" ━━━
 *
 *  S-8 之后普通写入不进版本库（只有标了 versioned 的条目镜像进共享版本库、
 *  破坏性动作才提库）。原来那种"每条都铺同一份整库日志"的区块，
 *  既看不出**这条**改过几次，又要在每次 /api/state 里 fork 一次 git。
 *  现在按条取：git 版本序列（有则列）+ `.history/` 改前快照（人人有）。
 *
 *  ⚠️ 只列不写：退回某一版是写动作，走 agent 的 memory_restore，
 *  这里一个按钮都不放 —— "面板是阅览室"那条契约就靠这个守住。
 */
async function loadVersions(n){
  const ident = n.title || n.id || '';
  const lib = n._lib || STATE.activeLib || '';
  const r = await api('/api/versions', {ident, lib});
  /* 异步回来时用户可能已经切到另一条了。往"当前 id 固定的容器"里填
     异步结果的串条 bug 实测踩过（反向引用那处同类），所以先验身份。 */
  if (STATE.cur !== ident) return;
  const box = document.getElementById('ver-body');
  if (!box) return;
  if (!r.ok){
    box.innerHTML = `<p class="sectip" style="margin:0">${
      esc(r.message || t('doc.verFail'))}</p>`;
    return;
  }
  const vs = r.versions || [], sn = r.snapshots || [];
  const parts = [];
  if (vs.length){
    parts.push(`<p class="sectip" style="margin:0 0 10px">${t('doc.verRepoOn')}</p>`);
    parts.push('<div class="hist">' + vs.map(v => `<div class="c">
        <span class="h">${esc(v.short || (v.sha || '').slice(0, 7))}</span>
        <span class="d">${esc(String(v.when || '').slice(0, 16).replace('T', ' '))}</span>
        <span class="s">${esc(v.subject || '')}</span></div>`).join('') + '</div>');
  } else {
    parts.push(`<p class="sectip" style="margin:0 0 10px">${t('doc.verRepoOff')}</p>`);
  }
  parts.push(sn.length
    ? `<h4 class="versub">${t('doc.verSnapshots', {n: sn.length})}</h4>
       <div class="hist">` + sn.map(s => `<div class="c">
        <span class="d">${esc(String(s.when || '').slice(0, 19).replace('T', ' '))}</span>
        <span class="s">${esc(s.path || '')}</span>
        <span class="n">${(s.bytes || 0)} B</span></div>`).join('') + '</div>'
    : `<p class="sectip" style="margin:0">${t('doc.verNone')}</p>`);
  parts.push(`<p class="vershint">${t('doc.verRestoreHint')}</p>`);
  box.innerHTML = parts.join('');
}

async function copyText(text, btn, label){
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); ta.remove();
  }
  btn.textContent = '✓ 已复制'; btn.classList.add('done');
  setTimeout(() => { btn.textContent = label; btn.classList.remove('done'); }, 1600);
}

/* ═══════════════ 标签页 2：体系（视图） ═══════════════ */

const VIEW_META = {
  overview:  {icon:'◫', hint:'库的整体状况'},
  decisions: {icon:'⚖', hint:'定过的口径与取舍'},
  lessons:   {icon:'⚠', hint:'踩过的坑，避免重复'},
  todos:     {icon:'☐', hint:'未完成的事'},
  expiring:  {icon:'⏳', hint:'已过期 / 该复查 / 已被取代'},
  inferences:{icon:'?', hint:'未核实的推断 —— 引用前当心'},
  projects:  {icon:'▤', hint:'按工作区分组'},
  recent:    {icon:'◷', hint:'按时间倒序'},
};

/** 视图 chip 条（v2 · S-7.2：从「体系」页搬到记忆页左栏）。
 *
 *  ━━━ 为什么视图要和记忆挤在同一页 ━━━
 *
 *  「视图」（决策/踩坑/待办/时效告警）与「记忆列表」本来就是同一批数据的
 *  两种看法。做成两个顶级 tab 的后果是：想扫一眼待办得切走，
 *  而当前正在读的那条就没了 —— 来回切三次以后人就不用了。
 *
 *  幂等：列表只拉一次；但**每次都按当前 kind 更新高亮** ——
 *  否则从别的入口切回来时高亮会停在上一个视图上。
 */
async function loadViewBar(kind){
  const bar = document.getElementById('viewbar');
  if (!bar) return;
  if (!bar.dataset.done){
    const list = await api('/api/views');
    bar.innerHTML = (list.views || []).map(v =>
      `<button class="chip" data-view="${esc(v.kind)}"
               title="${esc(v.desc)}">${VIEW_META[v.kind]?.icon || '·'} ${
         t('view.' + v.kind, {})}</button>
      `).join('');
    bar.querySelectorAll('.chip').forEach(el => el.onclick = () => {
      STATE.view = el.dataset.view;
      loadKnowledge();
    });
    bar.dataset.done = '1';
  }
  bar.querySelectorAll('.chip').forEach(x =>
    x.classList.toggle('on', x.dataset.view === kind));
}

async function loadKnowledge(){
  const kind = STATE.view || 'overview';
  const holder = document.getElementById('kbody');
  // 视图显示在**记忆页的右栏**（原「体系」页已并入记忆页）：
  // 把"读某条记忆"的面板让出来，不然两块内容会叠在一起。
  const nd = document.getElementById('notedoc');
  if (nd) nd.style.display = 'none';
  holder.style.display = '';
  holder.innerHTML = `<div class="empty">${t('common.loading')}</div>`;
  const r = await api('/api/view', {kind, limit: 200});
  if (!r.ok){
    holder.innerHTML = `<div class="empty">${
      esc(r.message || t('view.loadFail'))}</div>`;
    return;
  }
  STATE.viewData = r;

  await loadViewBar(kind);

  /* 视图标题与说明：优先用语言包（i18n），后端 label 作兜底。
   *
   * ⚠️ 为什么不直接用后端的 r.label：
   * 后端返回的是**中文**（knowledge.VIEWS 里的硬编码），
   * 而用户切到英文时这里就必须变。所以视图名走 t('view.<kind>')。 */
  const _lbl = t('view.' + kind, {});
  const _desc = t('view.' + kind + '.desc', {});
  let html = `<h2 class="sechead">${esc(_lbl || r.label || '')}</h2>
              <p class="sectip">${esc(_desc || r.desc || '')}</p>`;

  // 总览：先给统计卡片（"库里什么状况"一眼看完）
  if (kind === 'overview' && r.stats){
    const st = r.stats;
    html += '<div class="statgrid">';
    html += `<div class="statbox"><div class="k">${t('view.total')}</div><div class="v">${r.total}</div></div>`;
    for (const [tp, n] of (st.types||[])){
      html += `<div class="statbox"><div class="k">${esc(tp)}</div><div class="v">${n}</div></div>`;
    }
    const ex = st.expiry || {};
    if (ex.expired) html += `<div class="statbox"><div class="k">${t('badge.expired')}</div><div class="v" style="color:var(--rust)">${ex.expired}</div></div>`;
    if (ex.review) html += `<div class="statbox"><div class="k">${t('badge.review')}</div><div class="v" style="color:var(--brass)">${ex.review}</div></div>`;
    if (ex.superseded) html += `<div class="statbox"><div class="k">${t('badge.superseded')}</div><div class="v">${ex.superseded}</div></div>`;
    for (const [cp, n] of (st.claims||[])){
      html += `<div class="statbox"><div class="k">${esc(cp)}</div><div class="v">${n}</div></div>`;
    }
    html += '</div>';
    if ((st.workspaces||[]).length > 1){
      html += '<h3 class="sectip" style="font-weight:650;color:var(--ink)">按工作区</h3><div class="statgrid">';
      for (const [w, n] of st.workspaces){
        html += `<div class="statbox"><div class="k">${esc(w)}</div><div class="v">${n}</div></div>`;
      }
      html += '</div>';
    }
  }

  // 条目列表
  if ((r.items||[]).length){
    html += '<div class="rel" style="margin-top:18px">' + r.items.map(n => {
      const badges = [];
      const ex = n.expiry || {};
      if (ex.state === 'expired') badges.push('<span class="badge expired">已过期</span>');
      else if (ex.state === 'review') badges.push('<span class="badge review">待复查</span>');
      else if (ex.state === 'superseded') badges.push('<span class="badge superseded">已取代</span>');
      if (n.claim === 'inference') badges.push('<span class="badge claim-inference">推断</span>');
      return `<div class="relitem" data-id="${esc(n.title)}">
        <span>${esc(n.title)}</span>
        ${n.workspace ? `<span class="wstag">${esc(n.workspace)}</span>` : ''}
        ${badges.join('')}
        <span class="n">${when(n.updated || n.created)}</span>
      </div>`;
    }).join('') + '</div>';
  } else if (kind !== 'overview'){
    html += '<div class="empty"><span class="big">◌</span>' +
      '这个视图是空的。<br>写入相应类型的记忆后，它们会自动出现在这里 —— ' +
      '视图是按规则实时算的，不需要维护。</div>';
  }

  holder.innerHTML = html;
  holder.querySelectorAll('.relitem').forEach(el => el.onclick = () => {
    switchTab('library');
    select(el.dataset.id);
  });
}

/* ═══════════════ 标签页 3：设置 ═══════════════ */

/** 新机器要粘进 mcp.json 的那段配置。
 *
 *  ━━━ 地址为什么不能"一把梭 location.origin"━━━
 *
 *  用户实测反馈："每个电脑上的地址他不一样啊"。分两种情况：
 *
 *    · 从**别的机器**打开面板（http://192.168.3.35:8200）→
 *      origin 就是那台机器能用的地址，直接用它生成的配置是对的。
 *    · 在**服务器本机**打开面板（http://127.0.0.1:8200）→
 *      origin 是回环地址，复制给别的机器等于给了它一个指向它自己的地址
 *      （那个 agent 会连自己的 8200，然后一脸懵）。这种情况必须留占位符。
 *
 *  所以：回环地址 → 占位符；否则 → 用你当前访问用的那个地址。
 */
/** 给定 token，生成可直接粘贴的 `.mcp.json`。
 *
 *  与 accessSnippet() 的区别：这里的 token 是**现成的**（刚签发、明文只出现
 *  这一次），所以能给出完整可用的配置 —— 用户不用把 token 再手抄一遍。
 *  （2026-10-15 用户实测原话："mcp 为什么要手动配置？？这不就是配置的JSON吗？
 *  配完这个不就得了？？"—— 对，就该这样。）
 *
 *  地址规则同 accessSnippet：回环地址 → 占位符（127.0.0.1 发给别的机器没用）。
 */
function mcpSnippet(token){
  const h = location.hostname;
  const loopback = (h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '');
  const url = loopback
    ? 'http://<这台服务器的局域网地址>:8200/mcp'
    : location.origin + '/mcp';
  return JSON.stringify({
    mcpServers: {
      'shared-memory': {
        type: 'streamableHttp',
        url,
        headers: {Authorization: 'Bearer ' + token},
        timeout: 240000,
      },
    },
  }, null, 2);
}

function accessSnippet(){
  const h = location.hostname;
  const loopback = (h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '');
  const url = loopback
    ? 'http://<这台服务器的局域网地址>:8200/mcp'
    : location.origin + '/mcp';
  return JSON.stringify({
    mcpServers: {
      'shared-memory': {
        type: 'streamableHttp',
        url,
        headers: {Authorization: 'Bearer <在这里粘 token>'},
        timeout: 240000,
      },
    },
  }, null, 2);
}

/** 复制到剪贴板。
 *
 *  ⚠️ 面板是 http://192.168.x.x:8200（不是安全上下文），
 *  navigator.clipboard **取不到**（实测），必须留 execCommand 兜底。
 */
function copyText(s, btn){
  const done = () => {
    if (!btn) return;
    const old = btn.textContent;
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = old; }, 1400);
  };
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = s;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (e) {}
    ta.remove();
  };
  if (navigator.clipboard && window.isSecureContext){
    navigator.clipboard.writeText(s).then(done).catch(fallback);
  } else fallback();
}

/** 接入与安全块（v2 · S-7.3 第②块「接入」）。
 *
 *  ━━━ 为什么先做这一块 ━━━
 *
 *  主密钥 / PIN / 接入码三个能力在后端已经齐了（S-6），但**面板里够不着** ——
 *  等于只有 API，用户得去翻文档调 curl。这一块把它们变成"点一下就成"。
 *
 *  ⚠️ 主密钥明文**只在生成/轮换那一次出现**（盘上只有哈希）。所以：
 *   · 生成后当场展示 + 一键复制，并明说"关掉就再也看不到"
 *   · 接入码也**只在那一刻拼得出来**（它内含主密钥），平时显示占位说明
 */
function securityBlockHtml(){
  const zh = LANG === 'zh';
  // 标题由管理页的区块提供（这里只出内容）—— 两个标题会显得像两组设置
  return `
    <p class="sectip">${zh
      ? '两把钥匙各管一摊：<strong>主密钥</strong>给 agent（MCP），<strong>PIN</strong> 给人（面板）。两者都只存哈希 —— 忘了只能重置，找不回来。'
      : 'A master key for agents, a PIN for humans. Only hashes are stored.'}</p>

    <div class="tocwrap">
      <div class="saverow" style="padding-top:0;border-top:none;flex-wrap:wrap">
        <span class="badge" id="mk-state">…</span>
        <button class="btn primary" id="mk-gen">${zh ? '生成主密钥' : 'Generate'}</button>
        <button class="btn" id="mk-rotate">${zh ? '轮换' : 'Rotate'}</button>
        <span class="sectip" style="margin:0;font-size:12px">${zh
          ? '轮换后旧密钥有 60 秒宽限 —— 正在跑的任务不会被打断。'
          : 'The old key stays valid for 60s after rotation.'}</span>
      </div>
      <pre id="mk-key" style="display:none;margin:10px 0 0;font-size:12px;white-space:pre-wrap"></pre>
      <div id="mk-msg" style="font-size:12.5px;margin-top:8px"></div>
    </div>

    <div class="tocwrap" style="margin-top:12px">
      <div class="saverow" style="padding-top:0;border-top:none;flex-wrap:wrap">
        <span class="badge" id="pin-state">…</span>
        <input type="password" id="pin-input" inputmode="numeric" maxlength="8"
               autocomplete="new-password"
               placeholder="${zh ? '4~8 位数字' : '4-8 digits'}"
               style="width:150px;letter-spacing:.2em;text-align:center">
        <button class="btn primary" id="pin-set">${zh ? '设置 PIN' : 'Set PIN'}</button>
        <button class="btn" id="pin-off">${zh ? '关闭 PIN' : 'Disable PIN'}</button>
      </div>
      <div id="pin-msg" style="font-size:12.5px;margin-top:8px"></div>
    </div>

    <div class="tocwrap" style="margin-top:12px">
      <div style="font-size:12.5px;color:var(--muted)">${zh
        ? '接入码（新机器一条命令接上）：' : 'Connect code:'}</div>
      <pre id="conn-code" style="margin:6px 0 0;font-size:12px;white-space:pre-wrap"></pre>
      <div class="saverow" style="margin-top:8px;padding-top:0;border-top:none">
        <button class="btn" id="conn-copy">${zh ? '复制接入码' : 'Copy code'}</button>
        <button class="btn" id="conn-cmd">${zh ? '复制 connect 命令' : 'Copy command'}</button>
      </div>
      <div id="conn-msg" style="font-size:12.5px;margin-top:6px"></div>
    </div>`;
}

/** 接线：主密钥 / PIN / 接入码。 */
function wireSecurityBlock(){
  const zh = LANG === 'zh';
  const $ = (id) => document.getElementById(id);
  /* 生成那一刻才拿得到明文；平时只显示占位（哈希存盘，拼不出完整码） */
  let shownKey = '';

  const connCode = (key) => {
    const host = location.hostname || '127.0.0.1';
    const port = location.port || '8200';
    return `sm://${host}:${port}?k=${key || '<主密钥>'}`;
  };
  const connCmd = (key) =>
    `npx shared-memory-connect "${connCode(key)}" --project-dir .`;

  const refresh = async () => {
    const mk = await api('/api/auth/master-key');
    if (mk && mk.ok){
      $('mk-state').textContent = mk.enabled
        ? (zh ? `主密钥已启用（主人：${mk.owner}）` : `Master key on (owner: ${mk.owner})`)
        : (zh ? '主密钥未设置 —— agent 目前用旧 token' : 'No master key yet');
      $('mk-state').className = 'badge' + (mk.enabled ? ' claim-fact' : '');
    }
    const pin = await api('/api/auth/pin');
    if (pin && pin.ok){
      $('pin-state').textContent = pin.enabled
        ? (zh ? '面板 PIN 已开' : 'Panel PIN on')
        : (zh ? '面板 PIN 未设（用账号密码）' : 'No PIN (username/password)');
      if (pin.locked) $('pin-state').textContent +=
        zh ? `（锁 ${pin.wait_seconds}s）` : ` (locked ${pin.wait_seconds}s)`;
    }
    if (!shownKey){
      $('conn-code').textContent = connCode('');
      $('conn-msg').innerHTML = zh
        ? '⚠️ 接入码里含主密钥，<strong>只在生成/轮换那一次能拿到完整的</strong>；平时这里只显示占位。'
        : '⚠️ The code embeds the master key and is only complete right after generating.';
    } else {
      $('conn-code').textContent = connCode(shownKey);
    }
  };

  const showKey = (key, action) => {
    shownKey = key;
    $('mk-key').style.display = '';
    $('mk-key').textContent = key;
    $('mk-msg').innerHTML = zh
      ? `<strong>这是唯一一次显示明文</strong>（盘上只存哈希）：请立刻复制存进客户端配置。`
      : 'Shown once only — copy it now.';
    $('conn-code').textContent = connCode(key);
    $('conn-msg').innerHTML = `<span style="color:var(--verdigris)">${
      zh ? '接入码已可用（含新密钥）' : 'Connect code is ready'}</span>`;
    refresh();
  };

  $('mk-gen').onclick = async () => {
    if (!confirm(zh ? '生成主密钥？已接入的客户端需要把新密钥填进配置。'
                    : 'Generate a master key?')) return;
    const r = await apiPost('/api/auth/master-key', {action: 'generate'});
    if (!r.ok){ $('mk-msg').textContent = r.message || 'fail'; return; }
    showKey(r.key, 'generate');
  };
  $('mk-rotate').onclick = async () => {
    if (!confirm(zh ? '轮换主密钥？旧密钥 60 秒后失效。' : 'Rotate the master key?')) return;
    const r = await apiPost('/api/auth/master-key', {action: 'rotate'});
    if (!r.ok){ $('mk-msg').textContent = r.message || 'fail'; return; }
    showKey(r.key, 'rotate');
  };
  $('pin-set').onclick = async () => {
    const pin = $('pin-input').value.trim();
    const r = await apiPost('/api/auth/pin', {action: 'set', pin});
    $('pin-msg').textContent = r.ok
      ? (zh ? 'PIN 已设置（下次打开面板就要输它了）' : 'PIN set')
      : (r.message || 'fail');
    if (r.ok) $('pin-input').value = '';
    refresh();
  };
  $('pin-off').onclick = async () => {
    if (!confirm(zh ? '关闭面板 PIN？之后改用账号密码登录。' : 'Disable panel PIN?')) return;
    const r = await apiPost('/api/auth/pin', {action: 'disable'});
    $('pin-msg').textContent = r.ok ? (r.message || 'PIN 已关闭') : (r.message || 'fail');
    refresh();
  };
  $('conn-copy').onclick = (e) =>
    copyText(connCode(shownKey), e.target);
  $('conn-cmd').onclick = (e) =>
    copyText(connCmd(shownKey), e.target);

  refresh();
}

async function loadSettings(){
  const holder = document.getElementById('sbody');
  holder.innerHTML = `<div class="empty">${t('common.loading')}</div>`;
  const r = await api('/api/config');
  if (!r.ok){
    holder.innerHTML = `<div class="empty">${
    esc(r.message || t('cfg.saveFail'))}</div>`;
    return;
  }
  STATE.config = r;
  const rules = r.rules || {}, settings = r.settings || {};

  holder.innerHTML = `
  <div class="form">
    <h3 class="sechead">接入新机器 / 新 agent</h3>
    <p class="sectip">${LANG === 'zh'
      ? '任何机器上的任何 agent 都只认两行：<strong>服务地址 + token</strong>。不用搬数据、不用同步 —— 全平台只有服务端这一份数据。'
      : 'Any agent on any machine needs just two lines: service URL + token. Nothing to sync.'}</p>
    <div class="tocwrap">
      <pre id="access-snippet" style="margin:0;font-size:12px;line-height:1.5;white-space:pre-wrap">${
        esc(accessSnippet())}</pre>
      <div class="saverow" style="margin-top:12px;padding-top:0;border-top:none">
        <button class="btn primary" id="access-gen">生成 token 并填进配置</button>
        <button class="btn" id="access-copy">复制配置</button>
        <span class="sectip" style="margin:0;font-size:12px">${LANG === 'zh'
          ? '点「生成」当场签发一条新 token 并<strong>直接填进上面的 JSON</strong> —— 复制出去存成项目根的 <code>.mcp.json</code> 就能用，不用再去别处找 token。'
          : 'Click Generate: a fresh token is issued and filled into the JSON above.'}</span>
      </div>
      <div id="access-gen-msg" style="font-size:12.5px;margin-top:8px"></div>
    </div>

    ${r.instructions_is_default ? `
      <div class="warnbox">${t('cfg.defaultInstructions')}</div>` : ''}
    ${r.instructions_disabled ? `
      <div class="errbox">${t('cfg.disabledInstructions')}</div>` : ''}

    <h3 class="sechead">${t('cfg.writeRules')}</h3>
    <p class="sectip">${t('cfg.writeRulesHint')}</p>
    <div class="field">
      <label>${t('cfg.policy')}</label>
      <div class="hint">${t('cfg.policyHint')}</div>
      <input type="text" id="cfg-policy" value="${esc(rules.missing_field_policy || 'fill')}">
    </div>
    <div class="field">
      <label>${t('cfg.maxTitle')}</label>
      <div class="hint">${t('cfg.maxTitleHint')}</div>
      <input type="number" id="cfg-maxtitle" value="${esc(rules.max_title_chars ?? 120)}">
    </div>
    <div class="field">
      <label>${t('cfg.requireWs')}</label>
      <div class="hint">${t('cfg.requireWsHint')}</div>
      <input type="text" id="cfg-reqws" value="${rules.require_workspace ? 'true' : 'false'}">
    </div>
    <div class="field">
      <label>${t('cfg.inferType')}</label>
      <div class="hint">${t('cfg.inferTypeHint')}</div>
      <textarea id="cfg-infertype" spellcheck="false" style="min-height:150px">${
        esc(JSON.stringify(rules.infer_type || {}, null, 2))}</textarea>
    </div>

    <h3 class="sechead">${t('cfg.runParams')}</h3>
    <p class="sectip">${t('cfg.runParamsHint')}</p>
    <div class="field">
      <label>${t('cfg.debounce')}</label>
      <div class="hint">${t('cfg.debounceHint')}</div>
      <input type="number" id="cfg-debounce" value="${esc(settings.commit_debounce_seconds ?? 8)}">
    </div>
    <div class="field">
      <label>${t('cfg.searchLimit')}</label>
      <input type="number" id="cfg-limit" value="${esc(settings.search_default_limit ?? 20)}">
    </div>
    <div class="field">
      <label>${t('cfg.refresh')}</label>
      <div class="hint">${t('cfg.refreshHint')}</div>
      <input type="number" id="cfg-refresh" value="${esc(settings.panel_refresh_seconds ?? 15)}">
    </div>

    <div class="saverow">
      <button class="btn primary" id="cfg-save">${t('cfg.save')}</button>
      <button class="btn ghost" id="cfg-reload">${t('cfg.reload')}</button>
      <span id="cfg-msg" style="font-size:13px;color:var(--muted)"></span>
    </div>

    <h3 class="sechead">${t('cfg.source')}</h3>
    <p class="sectip">
      ${t('cfg.sourceHint')}
      <code>${esc(r.dir || '')}</code><br>
      ${t('cfg.isDefault')} / ${t('cfg.changed')}：
      ${r.rules_is_default ? t('cfg.isDefault') : t('cfg.changed')} ·
      ${r.settings_is_default ? t('cfg.isDefault') : t('cfg.changed')}
    </p>

    ${isAdmin() ? `
      <h3 class="sechead">${t('admin.users')}</h3>
      <p class="sectip">${t('admin.usersHint')}</p>
      <div id="admin-users"><div class="empty">${t('common.loading')}</div></div>

      <h3 class="sechead">${t('admin.tokens')}</h3>
      <p class="sectip">${t('admin.tokensHint')}</p>
      <div id="admin-tokens"><div class="empty">${t('common.loading')}</div></div>
    ` : `
      <!-- 成员身份：不显示管理区，但要说清「为什么看不到」——
           直接隐藏会让成员以为功能坏了（这类困惑实测反馈过）。 -->
      <h3 class="sechead">${t('admin.users')}</h3>
      <p class="sectip">${t('admin.onlyAdmin')}</p>
    `}

    <h3 class="sechead">${t('account.title')}</h3>
    <p class="sectip">${t('account.hint')}</p>
    <div class="field">
      <label>${t('account.curPw')}</label>
      <input type="password" id="ac-cur" placeholder="${t('account.curPwPh')}"
             autocomplete="current-password">
    </div>
    <div class="saverow" style="margin-top:0;padding-top:0;border-top:none">
      <input type="password" id="ac-new" placeholder="${t('account.newPwPh')}"
             autocomplete="new-password" style="flex:1;min-width:150px">
      <input type="password" id="ac-new2" placeholder="${t('account.newPw2Ph')}"
             autocomplete="new-password" style="flex:1;min-width:150px">
      <button class="btn" id="ac-change">${t('account.change')}</button>
      <button class="btn ghost" id="ac-logout">${t('account.logout')}</button>
    </div>
    <div id="ac-msg" style="font-size:13px;margin-top:8px"></div>
    <p class="sectip" style="margin-top:14px;font-size:12px">${LANG === 'zh'
      ? '忘了密码没法自助重置（服务端不会把哈希还原成明文）—— 在服务器上跑 '
        + '<code>./venv/bin/python scripts/set_password.py admin</code> 重设一个。'
      : 'Password reset must be done on the server: '
        + '<code>./venv/bin/python scripts/set_password.py admin</code>'}</p>
  </div>`;

  /* ⚠️ 这里**不能**调 wireSecurityBlock()：接入与安全块 S-7.3 已搬到
     「管理 → 接入」，设置页里没有 mk-*、pin-*、conn-* 这些元素，
     调用会在 `$('mk-gen').onclick` 上抛 TypeError，把紧随其后的
     `cfg-save` 接线一起打断 —— 症状是设置页照常渲染、
     「保存配置」按钮点不动（静默失效，静态检查看不出来）。 */
  document.getElementById('cfg-save').onclick = saveSettings;
  document.getElementById('cfg-reload').onclick = () => { loadSettings(); };
  /* 「生成 token 并填进配置」—— 标准管理系统的「Generate API key」形态：
     当场签发、当场填进 JSON、当场可复制。用户不需要去任何别的地方找 token。
     （2026-10-15 用户实测原话："token你直接给他写在这里面就完事了呗
     还非得让用户再找"） */
  const gen = document.getElementById('access-gen');
  if (gen) gen.onclick = async () => {
    const pre = document.getElementById('access-snippet');
    const gmsg = document.getElementById('access-gen-msg');
    gen.disabled = true;
    const oldLabel = gen.textContent;
    gen.textContent = '签发中…';
    const res = await apiPost('/api/auth/token/create', {
      user: (STATE.me && STATE.me.user && STATE.me.user.name) || 'admin',
      label: '面板接入 ' + new Date().toISOString().slice(0, 16).replace('T', ' '),
      scope: 'readwrite',
    });
    gen.disabled = false;
    gen.textContent = oldLabel;
    if (!res.ok){
      if (gmsg) gmsg.innerHTML =
        `<span style="color:var(--rust)">${esc(res.message || '签发失败')}</span>`;
      return;
    }
    const full = mcpSnippet(res.token);
    if (pre) pre.textContent = full;
    if (gmsg) gmsg.innerHTML = `<span style="color:var(--verdigris)">${LANG === 'zh'
      ? '✓ 已签发并填入（readwrite，label「面板接入 …」）。token 明文只显示这一次，复制走即可。'
      : '✓ Issued and filled in. The token is shown only this once.'}</span>`;
    /* 下面的「凭据」列表同步刷新（新 token 要立刻可见、可吊销） */
    if (typeof loadAdminTokens === 'function') loadAdminTokens();
  };

  const accCopy = document.getElementById('access-copy');
  if (accCopy) accCopy.onclick = () => {
    const pre = document.getElementById('access-snippet');
    copyText(pre ? pre.textContent : accessSnippet(), accCopy);
  };
  if (isAdmin()) { loadAdminUsers(); loadAdminTokens(); }
  loadWorkspaces();
  bindAccount();
}

/** 「我的账号」：改密码 + 退出登录。
 *
 *  ━━━ 产品视角 ━━━
 *  改密码是**每个人**都会用到的功能（不是管理员专属），所以放在
 *  设置页末尾且不需要管理员权限。
 *
 *  退出登录也放这里 —— 而不是藏在某个角落菜单里。
 *  面板是个单页应用，没有导航栏空间，放设置页最直观。
 */
function bindAccount(){
  const change = document.getElementById('ac-change');
  const out = document.getElementById('ac-logout');
  const msg = document.getElementById('ac-msg');
  if (!change || !out) return;

  change.onclick = async () => {
    const cur = document.getElementById('ac-cur').value;
    const nw = document.getElementById('ac-new').value;
    const nw2 = document.getElementById('ac-new2').value;
    msg.textContent = '';
    // 前端先做 cheapest 的校验（省一次请求），但**真正的校验在后端**
    if (!cur){ msg.innerHTML = `<span style="color:var(--rust)">${t('account.needCur')}</span>`; return; }
    if (nw.length < 8){ msg.innerHTML = `<span style="color:var(--rust)">${t('account.tooShort')}</span>`; return; }
    if (nw !== nw2){ msg.innerHTML = `<span style="color:var(--rust)">${t('account.mismatch')}</span>`; return; }
    change.disabled = true; change.textContent = t('account.changing');
    const r = await apiPost('/api/auth/password', {current: cur, password: nw, confirm: nw2});
    change.disabled = false; change.textContent = t('account.change');
    if (!r.ok){
      msg.innerHTML = `<span style="color:var(--rust)">${esc(r.message || '')}</span>`;
      return;
    }
    msg.innerHTML = `<span style="color:var(--verdigris)">${t('account.changed')}</span>`;
    document.getElementById('ac-cur').value = '';
    document.getElementById('ac-new').value = '';
    document.getElementById('ac-new2').value = '';
  };

  out.onclick = async () => {
    if (!confirm(t('account.logoutConfirm'))) return;
    await apiPost('/api/auth/logout', {});
    location.href = location.pathname;   // 清掉 query，回到登录态
  };
}

/** 工作空间与共享范围（核心 UI）。
 *
 *  ━━━ 设计要点 ━━━
 *
 * 1. **能管的排前面** —— 用户最关心"我能改哪些"
 * 2. **共享范围用下拉直接改**（不用进详情页）—— 这是高频操作
 * 3. **改之前显示当前状态**，改完显示"现在谁能读"
 * 4. 不可管理的库**明确说明原因**（"不是你的"），而不是把按钮藏起来
 *    —— 藏起来会让人以为功能坏了
 */
async function loadWorkspaces(){
  const box = document.getElementById('ws-body');
  if (!box) return;
  const r = await api('/api/workspaces');
  if (!r.ok){
    box.innerHTML = `<div class="errbox">${esc(r.message || '读取失败')}</div>`;
    return;
  }
  const libs = r.libraries || [];
  const targets = r.share_targets || [];
  const mine = libs.filter(l => l.can_manage);
  const others = libs.filter(l => !l.can_manage);

  /* 共享范围的选项文字：直接用后端返回的 label/desc。
   *
   * ⚠️ 为什么不在这里翻译：这些 label 是**后端按当前用户算出来的**
   * （比如"小组：研发"只对研发组的人出现），而且带业务语义。
   * 前端再翻译一遍会出现"两处文案不一致"的问题。
   * 后端已经支持按语言返回（见 workspace.share_targets）。 */
  const shareOptions = (cur) => targets.map(t =>
    `<option value="${esc(t.owner)}"${t.owner === cur ? ' selected' : ''}>
       ${esc(t.label)}
     </option>`).join('');

  const row = (l, manageable) => `
    <tr>
      <td>
        <strong>${esc(l.label)}</strong>
        ${l.is_workspace
            ? `<span class="badge">${t('badge.workspace')}</span>` : ''}
        <br><code style="font-size:10.5px">${esc(l.lib)}</code>
      </td>
      <td>
        ${manageable
          ? `<select class="ws-share" data-lib="${esc(l.lib)}"
                     style="padding:5px 8px;border-radius:7px;font-size:12.5px;
                            background:var(--panel2);border:1px solid var(--line);
                            color:var(--ink)">
               ${shareOptions(l.owner)}
             </select>`
          : `<span class="badge">${esc(l.share_label)}</span>`}
      </td>
      <td>${l.readable
            ? `<span class="badge claim-fact">${t('badge.readable')}</span>`
            : `<span class="badge superseded">${t('badge.unreadable')}</span>`}</td>
      <td>${l.writable
            ? `<span class="badge claim-fact">${t('badge.writable')}</span>`
            : `<span class="badge superseded">${t('badge.readonly')}</span>`}</td>
      <td style="font-size:11.5px;color:var(--muted)">
        ${manageable ? t('ws.ownerYou') : '—'}
      </td>
    </tr>`;

  box.innerHTML = `
    ${mine.length ? `
      <h4 style="font-size:13px;margin:0 0 8px;color:var(--muted)">
        ${t('ws.mine', {n: mine.length})}
      </h4>
      <div class="tablewrap"><table>
        <thead><tr>
          <th>${t('ws.col.space')}</th><th>${t('ws.col.share')}</th>
          <th>${t('ws.col.read')}</th><th>${t('ws.col.write')}</th><th></th>
        </tr></thead>
        <tbody>${mine.map(l => row(l, true)).join('')}</tbody>
      </table></div>` : ''}

    ${others.length ? `
      <h4 style="font-size:13px;margin:18px 0 8px;color:var(--muted)">
        ${t('ws.others', {n: others.length})}
      </h4>
      <div class="tablewrap"><table>
        <thead><tr>
          <th>${t('ws.col.space')}</th><th>${t('ws.col.share')}</th>
          <th>${t('ws.col.read')}</th><th>${t('ws.col.write')}</th><th></th>
        </tr></thead>
        <tbody>${others.map(l => row(l, false)).join('')}</tbody>
      </table></div>` : ''}

    <h4 style="font-size:13px;margin:20px 0 8px;color:var(--muted)">${t('ws.new')}</h4>
    <div class="saverow" style="margin-top:0;padding-top:0;border-top:none">
      <input type="text" id="ws-name" placeholder="${t('ws.namePlaceholder')}"
             style="flex:1;min-width:200px">
      <select id="ws-share-new" style="padding:8px;border-radius:8px;
              background:var(--panel2);border:1px solid var(--line);color:var(--ink)">
        ${targets.map(t => `<option value="${esc(t.owner)}">${esc(t.label)}</option>`).join('')}
      </select>
      <button class="btn primary" id="ws-add">建库</button>
    </div>
    <div id="ws-msg" style="font-size:13px;margin-top:8px"></div>
    <p class="sectip" style="margin-top:14px;font-size:12.5px">${t('ws.footHint')}</p>`;

  // 改共享范围
  box.querySelectorAll('.ws-share').forEach(sel => sel.onchange = async () => {
    const lib = sel.dataset.lib;
    const share = sel.value;
    const newLabel = sel.options[sel.selectedIndex].text.trim();
    if (!confirm(t('ws.shareConfirm', {lib, to: newLabel}))) {
      loadWorkspaces();   // 撤销选择
      return;
    }
    sel.disabled = true;
    const res = await apiPost('/api/workspace/share', {lib, share});
    sel.disabled = false;
    if (!res.ok){
      alert(res.message || t('ws.shareFail'));
      loadWorkspaces();
      return;
    }
    /* ⚠️ 顺序很重要：先重渲染表格，**再**写提示。
     *
     * 反过来的话提示会被重渲染冲掉 —— 用户改完什么都看不到，
     * 不知道成没成功（实测踩过：脚本抓提示抓不到，因为被冲了）。
     *
     * 这类"操作成功但没反馈"的问题在 UI 上很致命：
     * 用户会重复点、或者以为没生效。 */
    await loadWorkspaces();
    const msg = document.getElementById('ws-msg');
    if (msg){
      msg.innerHTML = `<div class="okbox" style="margin-top:10px">
        <strong>${t('ws.shareChanged', {from: esc(res.before), to: esc(res.after)})}</strong><br>
        <span style="font-size:12.5px">${esc(res.note || '')}</span></div>`;
      // 滚到提示处，确保用户看到（表格可能很长）
      msg.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    }
  });

  // 建库
  const add = document.getElementById('ws-add');
  add.onclick = async () => {
    const name = document.getElementById('ws-name').value.trim();
    if (!name){
      const m = document.getElementById('ws-msg');
      m.innerHTML = `<span style="color:var(--rust)">${t('ws.nameRequired')}</span>`;
      return;
    }
    add.disabled = true; add.textContent = t('ws.creating');
    const res = await apiPost('/api/workspace/create', {
      workspace: name,
      share: document.getElementById('ws-share-new').value,
    });
    add.disabled = false; add.textContent = t('ws.create');
    if (!res.ok){
      const m = document.getElementById('ws-msg');
      m.innerHTML = `<span style="color:var(--rust)">${
        esc(res.message || t('ws.shareFail'))}</span>`;
      return;
    }
    document.getElementById('ws-name').value = '';
    // 同上：先重渲染，再写提示
    await loadWorkspaces();
    const msg = document.getElementById('ws-msg');
    if (msg){
      msg.innerHTML = `<div class="okbox" style="margin-top:10px">
        <strong>${t('ws.created', {l: esc(res.label)})}</strong>（${esc(res.share_label)}）<br>
        <span style="font-size:12.5px">${
          t('ws.writeHint', {lib: esc(res.lib)})}</span>
      </div>`;
      msg.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    }
    load();
  };
}

/** 加载用户与库列表（管理员）。
 *
 *  ━━━ 为什么把"库"和"用户"放一起 ━━━
 *
 * 因为它们是**同一条链**：加人 → 自动给他个人库 → 发 token → 他接入。
 * 分成两个页面会让管理员在两处来回跳。
 */
async function loadAdminUsers(){
  const box = document.getElementById('admin-users');
  if (!box) return;
  const r = await api('/api/admin/users');
  if (!r.ok){
    box.innerHTML = `<div class="errbox">${esc(r.message || t('admin.readFail'))}</div>`;
    return;
  }

  const kindLabel = (o) => o === 'company' ? t('share.company') :
    String(o).startsWith('group:') ? t('share.group', {g: o.slice(6)}) :
    String(o).startsWith('user:') ? t('share.self') + '：' + o.slice(5) : o;

  box.innerHTML = `
    <div class="tablewrap"><table>
      <thead><tr>
        <th>${t('admin.col.user')}</th>
        <th>${t('admin.col.role')}</th>
        <th>${t('admin.col.groups')}</th>
        <th>${t('admin.col.status')}</th>
      </tr></thead>
      <tbody>
        ${(r.users || []).map(u => `
          <tr>
            <td><strong>${esc(u.display || u.name)}</strong>
                <code style="font-size:11px">${esc(u.name)}</code></td>
            <td>${u.role === 'admin' ? t('role.admin') : t('role.member')}</td>
            <td>${esc((u.groups || []).join('、') || '—')}</td>
            <td>${u.disabled
                  ? `<span class="badge expired">${t('badge.disabled')}</span>`
                  : `<span class="badge claim-fact">${t('badge.normal')}</span>`}</td>
          </tr>`).join('')}
      </tbody>
    </table></div>

    <h3 class="sechead" style="font-size:15px">${t('admin.libs', {n: (r.libs || []).length})}</h3>
    <div class="tablewrap"><table>
      <thead><tr>
        <th>${t('admin.col.lib')}</th>
        <th>${t('admin.col.owner')}</th>
        <th>${t('admin.col.memberWrite')}</th>
      </tr></thead>
      <tbody>
        ${(r.libs || []).map(l => `
          <tr>
            <td><strong>${esc(l.label || l.id)}</strong>
                <code style="font-size:11px">${esc(l.id)}</code></td>
            <td>${esc(kindLabel(l.owner))}</td>
            <td>${l.writable_by_members
                  ? `<span class="badge claim-fact">${t('badge.writable')}</span>`
                  : `<span class="badge superseded">${t('badge.readonly')}</span>`}</td>
          </tr>`).join('')}
      </tbody>
    </table></div>

    <div class="saverow">
      <input type="text" id="nu-name" placeholder="${t('admin.userName')}"
             style="flex:1;min-width:150px">
      <input type="text" id="nu-display" placeholder="${t('admin.displayName')}"
             style="flex:1;min-width:120px">
      <input type="text" id="nu-groups" placeholder="${t('admin.groups')}"
             style="flex:1;min-width:130px">
      <select id="nu-role" title="${t('admin.role')}"
              style="padding:8px;border-radius:8px;
              background:var(--panel2);border:1px solid var(--line);color:var(--ink)">
        <option value="member">${t('role.member')}</option>
        <option value="admin">${t('role.admin')}</option>
      </select>
      <button class="btn primary" id="nu-add">${t('admin.addUser')}</button>
    </div>
    <div id="nu-msg" style="font-size:13px;margin-top:8px"></div>`;

  const btn = document.getElementById('nu-add');
  btn.onclick = async () => {
    const msg = document.getElementById('nu-msg');
    const name = document.getElementById('nu-name').value.trim();
    if (!name){
      msg.innerHTML = `<span style="color:var(--rust)">${t('admin.nameRequired')}</span>`;
      return;
    }
    const groups = document.getElementById('nu-groups').value.split(',')
      .map(s => s.trim()).filter(Boolean);
    btn.disabled = true; btn.textContent = t('admin.creating');
    const res = await apiPost('/api/admin/user/add', {
      name,
      display: document.getElementById('nu-display').value.trim(),
      groups,
      role: document.getElementById('nu-role').value,
    });
    btn.disabled = false; btn.textContent = t('admin.addUser');
    if (!res.ok){
      msg.innerHTML = `<span style="color:var(--rust)">${
        esc(res.message || t('admin.createFail'))}</span>`;
      return;
    }
    /* ⚠️ 密码只返回一次 —— 必须显眼提示用户立刻复制，
       否则关掉页面就再也拿不到了（只能重置）。 */
    msg.innerHTML = `
      <div class="okbox" style="margin-top:10px">
        <strong>${t('admin.userCreated', {u: esc(res.user)})}</strong><br>
        ${t('admin.pwOnce')}
        <code style="font-size:15px;user-select:all">${esc(res.password)}</code>
      </div>`;
    loadAdminUsers(); loadAdminTokens();
  };
}

/** 加载 token 列表（管理员）。**永不显示明文** —— 只显示指纹。 */
async function loadAdminTokens(){
  const box = document.getElementById('admin-tokens');
  if (!box) return;
  const r = await api('/api/auth/tokens');
  if (!r.ok){ box.innerHTML = `<div class="errbox">${esc(r.message || '读取失败')}</div>`; return; }

  const fmt = (ts) => ts ? new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ') : '—';

  box.innerHTML = `
    <div class="tablewrap"><table>
      <thead><tr>
        <th>${t('admin.col.fingerprint')}</th>
        <th>${t('admin.col.tokenUser')}</th>
        <th>${t('admin.col.label')}</th>
        <th>${t('admin.col.scope')}</th>
        <th>${t('admin.col.lastUsed')}</th>
        <th>${t('admin.col.status')}</th>
        <th>${t('admin.col.actions')}</th>
      </tr></thead>
      <tbody>
        ${(r.tokens || []).map(tk => `
          <tr>
            <td><code>${esc(tk.fingerprint)}</code></td>
            <td>${esc(tk.user)}</td>
            <td>${esc(tk.label || '—')}</td>
            <td>${tk.scope === 'read'
                  ? `<span class="badge review">${t('badge.readonly')}</span>`
                  : `<span class="badge claim-fact">${t('admin.scopeRW')}</span>`}</td>
            <td>${esc(fmt(tk.last_used))}</td>
            <td>${tk.active
                  ? t('badge.active')
                  : `<span class="badge superseded">${t('badge.revoked')}</span>`}</td>
            <td>${tk.active
                  ? `<button class="abtn rvk" data-t="${esc(tk.fingerprint)}">${t('admin.revoke')}</button>`
                  : ''}</td>
          </tr>`).join('')}
      </tbody>
    </table></div>

    <div class="saverow">
      <input type="text" id="nt-user" placeholder="${t('admin.tokenUser')}"
             style="flex:1;min-width:150px">
      <input type="text" id="nt-label" placeholder="${t('admin.tokenLabel')}"
             style="flex:1;min-width:160px">
      <select id="nt-scope" style="padding:8px;border-radius:8px;
              background:var(--panel2);border:1px solid var(--line);color:var(--ink)">
        <option value="readwrite">${t('admin.scopeRW')}</option>
        <option value="read">${t('admin.scopeRO')}</option>
      </select>
      <button class="btn primary" id="nt-add">${t('admin.issueToken')}</button>
    </div>
    <div id="nt-msg" style="font-size:13px;margin-top:8px"></div>`;

  box.querySelectorAll('.rvk').forEach(el => el.onclick = async () => {
    if (!confirm(t('admin.revokeConfirm', {t: el.dataset.t}))) return;
    const res = await apiPost('/api/auth/token/revoke', {target: el.dataset.t});
    if (res.ok) loadAdminTokens();
    else alert(res.message || t('admin.revokeFail'));
  });

  const add = document.getElementById('nt-add');
  add.onclick = async () => {
    const msg = document.getElementById('nt-msg');
    const user = document.getElementById('nt-user').value.trim();
    if (!user){
      msg.innerHTML = `<span style="color:var(--rust)">${t('admin.needUser')}</span>`;
      return;
    }
    add.disabled = true; add.textContent = t('admin.issuing');
    const res = await apiPost('/api/auth/token/create', {
      user,
      label: document.getElementById('nt-label').value.trim(),
      scope: document.getElementById('nt-scope').value,
    });
    add.disabled = false; add.textContent = t('admin.issueToken');
    if (!res.ok){
      msg.innerHTML = `<span style="color:var(--rust)">${
        esc(res.message || t('admin.createFail'))}</span>`;
      return;
    }
    /* token 明文**只返回这一次**（后端刻意不再提供读取接口）。

       ⚠️ 两处都不能省（2026-10-15 实测）：

       ① **必须先刷新列表、再写提示**。原来反着来：先写提示、
          再 loadAdminTokens() 重渲染整个容器 —— 提示框刚画出来就被冲掉，
          **用户根本看不到刚发的 token**。

       ② **必须同时给出可直接粘贴的 `.mcp.json`**。token 明文只有这一次，
          这里不给配置，用户就得"抄 token → 找地方贴 → 手写 JSON"。 */
    await loadAdminTokens();
    const box2 = document.getElementById('nt-msg');
    if (!box2) return;
    box2.innerHTML = `
      <div class="okbox" style="margin-top:10px">
        <strong>${t('admin.tokenIssued', {u: esc(res.user), s: esc(res.scope)})}</strong><br>
        ${t('admin.tokenOnce')}
        <div style="margin-top:6px">
          <code style="font-size:12px;user-select:all;word-break:break-all">${esc(res.token)}</code>
        </div>
        <pre style="margin:10px 0 0;font-size:12px;line-height:1.5;
             white-space:pre-wrap;user-select:all">${esc(mcpSnippet(res.token))}</pre>
        <div class="saverow" style="margin-top:8px;padding-top:0;border-top:none">
          <button class="btn primary" id="nt-copy">复制整段配置</button>
          <span style="font-size:12px;color:var(--muted)">${LANG === 'zh'
            ? '存成项目根的 <code>.mcp.json</code>；含凭据，<strong>别提交进 git</strong>。'
            : 'Save as <code>.mcp.json</code> in the project root; do not commit it.'}</span>
        </div>
      </div>`;
    const cp = document.getElementById('nt-copy');
    if (cp) cp.onclick = () => copyText(mcpSnippet(res.token), cp);
  };
}

/** 管理页（v2 · S-7.2/S-7.3）：备份 / 接入 / 空间 / 指令。
 *
 *  ━━━ 与「设置」的分界（这条分界是刻意的）━━━
 *
 *   · **管理** = 这个库怎么活下去：备份、谁能进（接入）、空间、给 agent 的指令
 *   · **设置** = 这个服务怎么跑：写入规则、账号、token、外观
 *
 *  混在一起的后果很具体：用户找"备份"会先在设置页翻半天，
 *  而写规则和备份根本不是一类操作。
 */
async function loadAdmin(){
  const zh = LANG === 'zh';

  // ① 接入（主密钥 / PIN / 接入码）
  const acc = document.getElementById('adm-access');
  if (acc){
    acc.innerHTML = securityBlockHtml();
    wireSecurityBlock();
  }

  // ② 空间（沿用既有渲染器，它按 id 找 #ws-body）
  loadWorkspaces();

  // ③ 备份与恢复
  const bk = document.getElementById('adm-backup');
  if (bk){
    bk.innerHTML = `
      <div class="saverow" style="padding-top:0;border-top:none;flex-wrap:wrap">
        <button class="btn primary" id="bk-export">${zh
          ? '一键备份（导出可读的全部库）' : 'Back up now'}</button>
        <button class="btn" id="bk-refresh">${zh ? '刷新记录' : 'Refresh'}</button>
      </div>
      <div id="bk-list" style="margin-top:10px"></div>
      <div id="bk-msg" style="font-size:12.5px;margin-top:8px"></div>`;
    document.getElementById('bk-export').onclick = async (e) => {
      const btn = e.target;
      btn.disabled = true;
      const old = btn.textContent;
      btn.textContent = zh ? '备份中…' : 'Working…';
      const r = await apiPost('/api/backup', {});
      btn.disabled = false; btn.textContent = old;
      document.getElementById('bk-msg').innerHTML = r.ok
        ? `<span style="color:var(--verdigris)">${
            zh ? '已导出 ' + (r.count ?? '?') + ' 条到 ' : 'Exported to '}${esc(r.bundle || '')}</span>`
        : `<span style="color:var(--rust)">${esc(r.message || 'fail')}</span>`;
      loadBackups();
    };
    document.getElementById('bk-refresh').onclick = () => loadBackups();
    loadBackups();
  }

  // ④ 给 agent 的指令（从设置页搬来；契约七条由服务端保证，这里是空间级补充）
  const instr = document.getElementById('adm-instr');
  if (instr){
    const r = await api('/api/config');
    STATE.config = r.ok ? r : STATE.config;
    instr.innerHTML = `
      ${r.ok && r.instructions_is_default ? `<div class="warnbox">${
        t('cfg.defaultInstructions')}</div>` : ''}
      ${r.ok && r.instructions_disabled ? `<div class="errbox">${
        t('cfg.disabledInstructions')}</div>` : ''}
      <div class="field">
        <textarea id="cfg-instructions" spellcheck="false">${
          esc((r.ok ? r.instructions : '') || '')}</textarea>
      </div>
      <div class="saverow">
        <button class="btn primary" id="instr-save">${zh ? '保存指令' : 'Save'}</button>
        <span id="instr-msg" style="font-size:12.5px"></span>
      </div>`;
    document.getElementById('instr-save').onclick = async () => {
      const msg = document.getElementById('instr-msg');
      msg.textContent = zh ? '保存中…' : 'Saving…';
      const r2 = await apiPost('/api/config', {
        instructions: document.getElementById('cfg-instructions').value,
      });
      msg.innerHTML = r2.ok
        ? `<span style="color:var(--verdigris)">${
            zh ? '已保存 —— 下次握手生效' : 'Saved — effective on next handshake'}</span>`
        : `<span style="color:var(--rust)">${esc(r2.message || 'fail')}</span>`;
    };
  }
}

/** 备份记录列表（管理页「备份与恢复」用）。 */
async function loadBackups(){
  const box = document.getElementById('bk-list');
  if (!box) return;
  const zh = LANG === 'zh';
  const r = await api('/api/backups');
  const items = (r && r.items) || [];
  if (!items.length){
    box.innerHTML = `<div class="sectip" style="margin:0">${
      zh ? '还没有备份记录。' : 'No backups yet.'}</div>`;
    return;
  }
  box.innerHTML = items.map(it => `
    <div class="tocwrap" style="margin-top:8px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <code style="font-size:12px">${esc(it.name)}</code>
        <span class="sectip" style="margin:0;font-size:12px">${
          esc(it.when || '')} · ${esc(String(it.count ?? '?'))} ${
          zh ? '条' : 'notes'} · ${esc(String(it.size_kb ?? '?'))} KB</span>
        <button class="btn" data-restore="${esc(it.name)}">${
          zh ? '恢复到…' : 'Restore…'}</button>
      </div>
    </div>`).join('');
  box.querySelectorAll('[data-restore]').forEach(el => el.onclick = async () => {
    const name = el.dataset.restore;
    const target = prompt(zh
      ? '恢复到哪个库？（留空 = 你的个人库；导入不覆盖同名条目，默认 skip）'
      : 'Restore into which lib? (empty = personal)', '');
    if (target === null) return;
    const r2 = await apiPost('/api/restore', {
      bundle: name, target_lib: target.trim(), on_conflict: 'skip',
    });
    const msg = document.getElementById('bk-msg');
    msg.innerHTML = r2.ok
      ? `<span style="color:var(--verdigris)">${
          zh ? '已恢复：新增 ' + (r2.added ?? '?') + '，跳过 ' :
               'Restored: added '}${esc(String(r2.skipped ?? ''))}</span>`
      : `<span style="color:var(--rust)">${esc(r2.message || 'fail')}</span>`;
    loadBackups();
  });
}

async function saveSettings(){
  const msg = document.getElementById('cfg-msg');
  const btn = document.getElementById('cfg-save');
  msg.textContent = '';

  // 先在前端把 JSON 解析好 —— 让用户看到"哪一行写错了"，
  // 而不是等后端返回一个笼统的 400
  let inferType;
  try {
    const raw = document.getElementById('cfg-infertype').value.trim();
    inferType = raw ? JSON.parse(raw) : {};
    if (typeof inferType !== 'object' || Array.isArray(inferType))
      throw new Error('必须是对象：{"类型": ["关键词"]}');
  } catch (e){
    msg.innerHTML = `<span style="color:var(--rust)">${
      t('cfg.inferJsonErr')}${esc(e.message)}</span>`;
    return;
  }

  const payload = {
    /* ⚠️ 指令编辑器已搬到「管理」页（v2 · S-7.2）：它不在这个页面里了，
       所以这里**不能**无条件读 —— 读不到就当没改过（不传该字段），
       否则一次"保存规则"会把指令悄悄清空。 */
    ...(document.getElementById('cfg-instructions')
        ? {instructions: document.getElementById('cfg-instructions').value}
        : {}),
    rules: {
      ...(STATE.config.rules || {}),
      missing_field_policy: document.getElementById('cfg-policy').value.trim(),
      max_title_chars: Number(document.getElementById('cfg-maxtitle').value) || 0,
      require_workspace: document.getElementById('cfg-reqws').value.trim() === 'true',
      infer_type: inferType,
    },
    settings: {
      ...(STATE.config.settings || {}),
      commit_debounce_seconds: Number(document.getElementById('cfg-debounce').value) || 0,
      search_default_limit: Number(document.getElementById('cfg-limit').value) || 20,
      panel_refresh_seconds: Number(document.getElementById('cfg-refresh').value) || 0,
    },
  };

  btn.disabled = true; btn.textContent = t('cfg.saving');
  const r = await apiPost('/api/config', payload);
  btn.disabled = false; btn.textContent = t('cfg.save');

  if (!r.ok){
    // 后端会指明是哪一项不合法 —— 直接展示，不要吞掉
    msg.innerHTML = `<span style="color:var(--rust)">${
      t('cfg.saveFail')}${esc(r.message || '')}</span>`;
    return;
  }
  msg.innerHTML = `<span style="color:var(--verdigris)">${t('cfg.saved')}</span>`;
  loadSettings();
  load();
}

/* ═══════════════ 标签页切换 ═══════════════ */

function switchTab(tab){
  STATE.tab = tab;
  document.querySelectorAll('.navtab').forEach(el =>
    el.classList.toggle('on', el.dataset.tab === tab));
  for (const t of ['library','admin','settings']){
    document.getElementById('page-' + t).style.display = (t === tab) ? '' : 'none';
  }
  if (tab === 'admin') loadAdmin();
  if (tab === 'settings') loadSettings();
}

/* ═══════════════ 键盘操作 ═══════════════ */

function move(delta){
  if (STATE.tab !== 'library') return;
  const idx = STATE.notes.findIndex(n => n.title === STATE.cur);
  const next = STATE.notes[Math.max(0, Math.min(STATE.notes.length - 1,
               (idx === -1 ? 0 : idx) + delta))];
  if (next) select(next.title);
}

document.addEventListener('keydown', (e) => {
  const tag = (document.activeElement.tagName || '');
  const typing = /^(INPUT|TEXTAREA)$/.test(tag);
  // 在设置页打字时不要触发全局快捷键
  if (typing) return;

  if (e.key === '/'){
    e.preventDefault();
    switchTab('library');
    document.getElementById('q').focus();
    return;
  }
  if (e.key === 'Escape'){
    const q = document.getElementById('q');
    q.value = ''; STATE.q = ''; q.blur(); load();
    return;
  }
  if (e.key === 'j' || e.key === 'ArrowDown'){ e.preventDefault(); move(1); }
  if (e.key === 'k' || e.key === 'ArrowUp'){ e.preventDefault(); move(-1); }
  if (e.key === '1') switchTab('library');
  if (e.key === '2') switchTab('admin');
  if (e.key === '3') switchTab('settings');
});

/* ═══════════════ 启动 ═══════════════ */

/* ─────────────── 语言初始化 ───────────────
 *
 * 顺序很重要：
 *   ① detectLang()      决定用哪个语言（存过 > URL > 浏览器 > 默认中文）
 *   ② applyStaticText() 把 HTML 里的 data-i18n 文案换掉
 *   ③ 绑定下拉框        之后用户手动切换
 *
 * 必须在 load() **之前**做：否则首屏先用中文渲染一遍再被替换，
 * 用户会看到一闪而过的语言跳变。 */
LANG = detectLang();
document.documentElement.lang = (LANG === 'zh' ? 'zh-CN' : LANG);
applyStaticText();

const langSel = document.getElementById('lang-select');
if (langSel) {
  langSel.value = LANG;
  langSel.onchange = () => {
    setLang(langSel.value);
    document.documentElement.lang = (LANG === 'zh' ? 'zh-CN' : LANG);
    paintThemeBtn();          // 按钮文案是这里拼的，切语言要重画
  };
}

/* ── 主题开关（v2 · S-7.7）──
   三档循环：跟随系统 → 深色 → 浅色。

   分工要说清楚：**CSS 只认 `<html data-theme>`**（深浅各一处，见 panel.css
   顶部注释），"跟随系统"由这里翻译成具体值。而**首帧**的颜色不由这里管 ——
   那是 dashboard.py 里那段内联脚本的活（panel.js 在 body 末尾，等它跑起来
   再设色，浅色用户会先看到一帧深色）。这里只做"与首帧对齐 + 接开关"。

   手动选定后就不再跟系统走：用户显式点了深色，操作系统到点切浅色
   不该把他的选择改掉。 */
const THEME_MODES = ['auto', 'dark', 'light'];

function themeMode(){
  try { return localStorage.getItem('shm-theme') || 'auto'; } catch { return 'auto'; }
}

function applyTheme(mode){
  const light = mode === 'light' ||
    (mode !== 'dark' && matchMedia('(prefers-color-scheme: light)').matches);
  document.documentElement.dataset.theme = light ? 'light' : 'dark';
  paintThemeBtn(mode);
}

function paintThemeBtn(mode){
  const btn = document.getElementById('theme-btn');
  if (!btn) return;
  const zh = LANG === 'zh';
  const m = mode || themeMode();
  const L = {auto: zh ? '自动' : 'Auto', dark: zh ? '深色' : 'Dark',
             light: zh ? '浅色' : 'Light'}[m] || m;
  const I = {auto: '◐', dark: '☾', light: '☀'}[m] || '◐';
  btn.textContent = I + ' ' + L;
  btn.title = zh ? '主题：跟随系统 → 深色 → 浅色（点击循环）'
                 : 'Theme: auto → dark → light (click to cycle)';
}

const themeBtn = document.getElementById('theme-btn');
if (themeBtn) {
  applyTheme(themeMode());    // 与首帧对齐，并把按钮文案画出来
  themeBtn.onclick = () => {
    const next = THEME_MODES[(THEME_MODES.indexOf(themeMode()) + 1) % THEME_MODES.length];
    try { localStorage.setItem('shm-theme', next); } catch {}
    applyTheme(next);
  };
  const mq = matchMedia('(prefers-color-scheme: light)');
  // 只有"跟随系统"档才跟着系统动
  if (mq.addEventListener) mq.addEventListener('change', () => {
    if (themeMode() === 'auto') applyTheme('auto');
  });
}

let searchTimer;
const qi = document.getElementById('q');
qi.addEventListener('input', (e) => {
  STATE.q = e.target.value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(load, 200);   // 防抖：别每个字符都打一次请求
});
document.querySelectorAll('.navtab').forEach(el =>
  el.onclick = () => switchTab(el.dataset.tab));

/* 顶栏「体检」按钮：治理抽屉（v2 · S-7.4）。按钮可能在 boot 时才存在，
   所以判空后再接 —— 让一个可选按钮把整页脚本打断不值得。 */
const _govBtn = document.getElementById('gov-btn');
if (_govBtn) _govBtn.onclick = () => openGovernance();

/* 深链接支持：?tab=knowledge / ?id=某标题
   用途：把"这个视图"或"这条记忆"直接分享出去 ——
   面板是给人看的，能分享的链接比"你去点第二个标签"有用得多。 */
const bootParams = new URLSearchParams(location.search);
const bootId = bootParams.get('id');
const bootTab = bootParams.get('tab');

if (bootId) STATE.cur = bootId;
switchTab(['admin', 'settings', 'library'].includes(bootTab) ? bootTab : 'library');

load().then(() => { if (bootId) select(bootId); });

/** 启动时恢复用户偏好（分组方式） */
try {
  const saved = localStorage.getItem('shm-groupby');
  if (saved === 'time' || saved === 'workspace' || saved === 'type') STATE.groupBy = saved;
} catch {}

/* 自动刷新：只在记忆库页且没在搜索时刷新。
   两个"不刷"的理由：
     · 用户在搜索框里打字时刷新会打断输入
     · 设置页有未保存的改动，被刷新冲掉会很恼人 */
refreshTimer = setInterval(() => {
  if (!STATE.q && STATE.tab === 'library') load();
}, 15000);
