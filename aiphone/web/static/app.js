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
  let count = box.querySelectorAll('.msg').length;
  const timer = setInterval(async () => {
    try {
      const r = await fetch('/api/calls/' + uuid);
      if (!r.ok) return;
      const c = await r.json();
      if (c.transcript.length !== count) {
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
