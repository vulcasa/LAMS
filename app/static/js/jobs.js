/* 后台任务：入队 + 轮询进度。无第三方依赖。 */
(function () {
  let activeDialog = null;

  function dialogElement() {
    let overlay = document.getElementById('avlib-dialog');
    if (overlay) return overlay;
    overlay = document.createElement('div');
    overlay.id = 'avlib-dialog';
    overlay.className = 'avlib-modal';
    overlay.hidden = true;
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.innerHTML = `
      <div class="avlib-modal-box">
        <div class="avlib-modal-head">
          <b id="avlib-dialog-title">提示</b>
          <button type="button" class="lightbox-close" aria-label="关闭">×</button>
        </div>
        <p id="avlib-dialog-message"></p>
        <div class="avlib-modal-actions">
          <button type="button" class="btn" data-action="cancel">取消</button>
          <button type="button" class="btn primary" data-action="ok">知道了</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    return overlay;
  }

  function closeDialog(result) {
    const overlay = document.getElementById('avlib-dialog');
    if (!overlay || overlay.hidden) return;
    overlay.hidden = true;
    const current = activeDialog;
    activeDialog = null;
    if (current) current(result);
  }

  function showDialog({ title, message, confirm = false }) {
    const overlay = dialogElement();
    if (activeDialog) { const previous = activeDialog; activeDialog = null; previous(false); }
    document.getElementById('avlib-dialog-title').textContent = title || (confirm ? '确认' : '提示');
    document.getElementById('avlib-dialog-message').textContent = message || '';
    const cancel = overlay.querySelector('[data-action="cancel"]');
    const ok = overlay.querySelector('[data-action="ok"]');
    cancel.hidden = !confirm;
    ok.textContent = confirm ? '确定' : '知道了';
    overlay.hidden = false;
    overlay.onclick = (event) => { if (event.target === overlay) closeDialog(false); };
    overlay.querySelector('.avlib-modal-head .lightbox-close').onclick = () => closeDialog(false);
    cancel.onclick = () => closeDialog(false);
    ok.onclick = () => closeDialog(true);
    setTimeout(() => ok.focus(), 20);
    return new Promise((resolve) => {
      activeDialog = resolve;
      overlay.onkeydown = (event) => {
        if (event.key === 'Escape') { event.preventDefault(); closeDialog(false); }
      };
    });
  }

  function dialogAlert(message, title) {
    return showDialog({ title: title || '提示', message: message, confirm: false }).then(() => undefined);
  }

  function dialogConfirm(message, title) {
    return showDialog({ title: title || '确认', message: message, confirm: true });
  }

  async function post(path, payload) {
    const r = await fetch('/api/' + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {}),
    });
    let data = {};
    try { data = await r.json(); } catch (e) { /* ignore */ }
    if (!r.ok) {
      await dialogAlert(data.error || ('HTTP ' + r.status), '操作失败');
      return null;
    }
    return data;
  }

  function toast(message) {
    let el = document.getElementById('avlib-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'avlib-toast';
      el.className = 'avlib-toast';
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.hidden = false;
    clearTimeout(el._timer);
    el._timer = setTimeout(() => { el.hidden = true; }, 3200);
  }

  async function enqueue(path, payload) {
    const data = await post(path, payload);
    if (data && data.job_id) {
      toast('已入队任务 #' + data.job_id + '（' + path + '），可在任务列表或「日志」页查看进度');
      poll();
      setTimeout(poll, 800);
    }
    return data && data.job_id;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function render(jobs) {
    var el = document.getElementById('jobs');
    if (!el) return;
    if (!jobs.length) { el.innerHTML = '<p class="muted">当前没有运行中的后台任务。</p>'; return; }
    el.innerHTML = jobs.map(function (j) {
      var cls = j.status === 'done' ? 'ok' : j.status === 'failed' ? 'fail' : j.status === 'running' ? 'run' : '';
      var pct = j.total ? Math.min(100, Math.round(j.progress * 100 / j.total)) : null;
      var result = '';
      if (j.result) {
        try { result = esc(JSON.stringify(j.result)).slice(0, 420); } catch (e) { /* ignore */ }
      }
      var err = j.error ? '<div class="jerr">' + esc(String(j.error).split('\n')[0]) + '</div>' : '';
      return '<div class="job ' + cls + '">' +
        '<div class="jhead"><span class="jkind">' + esc(j.kind) + '</span>' +
        '<span class="jstatus">' + esc(j.status) + '</span>' +
        '<span class="jid">#' + j.id + '</span></div>' +
        (j.message ? '<div class="jmsg">' + esc(j.message) + '</div>' : '') +
        (pct !== null ? '<div class="bar"><i style="width:' + pct + '%"></i></div>' : '') +
        (result ? '<pre class="jres">' + result + '</pre>' : '') + err +
        '</div>';
    }).join('');
  }

  async function poll() {
    try {
      const r = await fetch('/api/jobs?limit=20&active=1');
      if (!r.ok) return;
      const data = await r.json();
      render(data.jobs || []);
    } catch (e) { /* offline */ }
  }

  window.avlib = {
    enqueue: enqueue, poll: poll,
    alert: dialogAlert, confirm: dialogConfirm,
  };

  document.addEventListener('DOMContentLoaded', function () {
    if (document.getElementById('jobs')) {
      poll();
      setInterval(poll, 3000);
    }
  });
})();
