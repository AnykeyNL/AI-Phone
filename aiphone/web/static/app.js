// Small helpers for the AI-Phone web interface.

async function testKey(key, btn) {
  const out = document.getElementById('test-' + key);
  out.textContent = 'testing…';
  try {
    const r = await fetch('/settings/test/' + key);
    const j = await r.json();
    out.textContent = j.message;
    out.style.color = j.ok ? 'var(--ok)' : 'var(--bad)';
  } catch (e) { out.textContent = String(e); }
}

// Reload the dashboard when something changes (a call starts or ends).
function pollStatus() {
  const el = document.getElementById('status');
  let version = el && el.dataset.version;
  let lines = null;
  setInterval(async () => {
    try {
      const r = await fetch('/api/status');
      if (!r.ok) return;
      const j = await r.json();
      const reg = JSON.stringify(j.lines.map(l => l.registered)) + j.ami;
      if (lines === null) lines = reg;
      if (String(j.version) !== String(version) || reg !== lines) location.reload();
    } catch (e) { /* ignore */ }
  }, 4000);
}

// Live transcript while a call is running.
function pollCall(uuid) {
  const box = document.getElementById('transcript');
  let count = box ? box.querySelectorAll('.msg').length : 0;
  const timer = setInterval(async () => {
    try {
      const r = await fetch('/api/calls/' + uuid);
      if (!r.ok) return;
      const c = await r.json();
      if (box && c.transcript.length !== count) {
        count = c.transcript.length;
        box.innerHTML = '';
        for (const t of c.transcript) {
          const d = document.createElement('div');
          d.className = 'msg ' + t.role;
          d.innerHTML = '<span class="who"></span><span class="t"></span><p></p>';
          d.querySelector('.who').textContent = t.role;
          d.querySelector('.t').textContent = t.t + 's';
          d.querySelector('p').textContent = t.text;
          box.appendChild(d);
        }
      }
      document.getElementById('call-status').textContent = c.status + ' ' + (c.reason || '');
      if (!['ringing', 'connecting', 'active'].includes(c.status)) { clearInterval(timer); setTimeout(() => location.reload(), 500); }
    } catch (e) { /* ignore */ }
  }, 1500);
}

// Show only the schedule fields that belong to the chosen repeat type.
function kindToggle() {
  const sel = document.getElementById('kind');
  const apply = () => document.querySelectorAll('[data-kind]').forEach(el => {
    el.style.display = el.dataset.kind.split(' ').includes(sel.value) ? '' : 'none';
  });
  sel.addEventListener('change', apply);
  apply();
}

// Click a value in the ATA guide to copy it.
document.querySelectorAll('table.copy code').forEach(c => c.addEventListener('click', () => {
  navigator.clipboard && navigator.clipboard.writeText(c.textContent);
  c.style.outline = '2px solid var(--ok)';
  setTimeout(() => c.style.outline = '', 600);
}));

// Replace a <pre> log's text, staying at the bottom if the reader was there.
function setLog(pre, text) {
  if (pre.textContent === text) return;
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.textContent = text;
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

function setLive(on, label) {
  const el = document.getElementById('live');
  if (!el) return;
  el.textContent = label || (on ? 'live' : 'paused');
  el.classList.toggle('off', !on);
}

// Follow the self-update log; the server restarts near the end.
function followUpdate() {
  const pre = document.getElementById('update-log');
  const state = document.getElementById('update-state');
  pre.scrollTop = pre.scrollHeight;
  let restarting = false;
  const poll = async () => {
    try {
      const r = await fetch('/api/update', {cache: 'no-store'});
      if (r.status === 401) { location.href = '/login?next=/settings/update'; return; }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      restarting = false;
      if (j.waiting) {
        state.textContent = 'Starting the update…';
      } else {
        setLog(pre, j.log);
      }
      if (j.finished) {
        state.innerHTML = '<span class="pill ok">Update finished</span> Now running version <code>' +
          (j.installed.commit || '').slice(0, 7) + '</code>.';
        setLive(false, 'done');
        return;
      }
      if (j.failed) {
        state.innerHTML = '<span class="pill bad">The update stopped with an error</span> See the log below.';
        setLive(false, 'failed');
        return;
      }
      if (!j.waiting) state.textContent = 'The update is running. AI-Phone restarts at the end; this page follows along.';
      setLive(true);
    } catch (e) {
      if (!restarting) state.textContent = 'AI-Phone is restarting…';
      restarting = true;
      setLive(true, 'reconnecting');
    }
    setTimeout(poll, 1500);
  };
  setTimeout(poll, 1000);
}

// Follow the system log.
function followLog() {
  const pre = document.getElementById('system-log');
  const pause = document.getElementById('log-pause');
  pre.scrollTop = pre.scrollHeight;
  pause.addEventListener('change', () => setLive(!pause.checked));
  const poll = async () => {
    if (!pause.checked && !document.hidden) {
      try {
        const r = await fetch('/api/logs', {cache: 'no-store'});
        if (r.status === 401) { location.href = '/login?next=/logs'; return; }
        if (r.ok) { setLog(pre, (await r.json()).log); setLive(true); }
        else setLive(false, 'offline');
      } catch (e) { setLive(false, 'offline'); }
    }
    setTimeout(poll, 3000);
  };
  setTimeout(poll, 3000);
}
