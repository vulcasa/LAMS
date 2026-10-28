/* 客户端解码播放器：movi-player + 片库播放列表 / 最近播放。 */
(function () {
  const page = document.getElementById('clientPage');
  const stage = document.getElementById('clientStage');
  const player = document.getElementById('clientPlayer');
  if (!page) return;
  const els = {
    code: document.getElementById('clientCode'), title: document.getElementById('clientTitle'),
    file: document.getElementById('clientFileName'), size: document.getElementById('clientSize'),
    current: document.getElementById('clientCurrent'), recent: document.getElementById('clientRecent'),
    toggle: document.getElementById('clientPlaylistToggle'), backdrop: document.getElementById('clientBackdrop'),
  };
  let recentLoaded = false;
  const mobile = window.matchMedia('(max-width: 900px)');
  const sessionId = (crypto.randomUUID && crypto.randomUUID()) || ('s-' + Date.now());

  function esc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  function setPlaylist(open) {
    const on = typeof open === 'boolean' ? open : !page.classList.contains('playlist-open');
    if (mobile.matches) {
      page.classList.toggle('playlist-open', on);
      if (els.backdrop) els.backdrop.hidden = !on;
    } else {
      page.classList.toggle('playlist-collapsed', !on);
    }
  }
  function recordPlay(titleId) {
    if (!titleId) return;
    fetch('/api/player/recent', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title_id:Number(titleId)})}).catch(()=>{});
  }
  function sendSession() {
    if (!stage || !player) return;
    const titleId = stage.dataset.titleId, fileId = stage.dataset.fileId;
    if (!titleId || !fileId) return;
    fetch('/api/player/session', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      session_id: sessionId, title_id:Number(titleId), file_id:Number(fileId),
      position:player.currentTime || 0, duration:player.duration || 0, paused:!!player.paused,
    })}).catch(()=>{});
  }
  function load(item) {
    if (!item || !stage || !player) return;
    els.code.textContent = item.dataset.code || '';
    els.title.textContent = item.dataset.title || '';
    els.file.textContent = item.dataset.name || '';
    els.size.textContent = ((Number(item.dataset.size) || 0) / 1048576).toFixed(1) + ' MB';
    stage.dataset.fileId = item.dataset.fileId;
    stage.dataset.titleId = item.dataset.titleId;
    document.querySelectorAll('#clientCurrent .playlist-item').forEach(n => n.classList.toggle('active', n === item));
    player.src = item.dataset.src;
    player.play().catch(() => {});
    recordPlay(item.dataset.titleId);
    const url = new URL(location.href);
    url.searchParams.set('file_id', item.dataset.fileId);
    history.replaceState(null, '', url.toString());
    if (mobile.matches) setPlaylist(false);
    sendSession();
  }
  async function loadRecent() {
    recentLoaded = true;
    try {
      const data = await (await fetch('/api/player/recent')).json();
      const items = data.items || [];
      els.recent.innerHTML = items.length ? items.map(item =>
        `<button type="button" class="playlist-item" data-url="${esc(item.url)}">
          <span class="playlist-cover">${item.cover ? `<img src="${esc(item.cover)}" alt="" loading="lazy">` : `<span>${esc((item.dvd_id||'').slice(0,2))}</span>`}</span>
          <span class="playlist-info"><b>${esc(item.dvd_id)}</b><span>${esc(item.title)}</span><small>播放 ${Number(item.play_count||1)} 次</small></span>
        </button>`).join('') : '<p class="muted" style="padding:16px">还没有播放记录。</p>';
      els.recent.querySelectorAll('[data-url]').forEach(btn => btn.addEventListener('click', () => { location.href = btn.dataset.url; }));
    } catch (e) { els.recent.innerHTML = '<p class="muted" style="padding:16px">最近播放加载失败。</p>'; }
  }
  function tab(name) {
    document.querySelectorAll('[data-client-tab]').forEach(b => b.classList.toggle('active', b.dataset.clientTab === name));
    els.current.hidden = name !== 'current';
    els.recent.hidden = name !== 'recent';
    if (name === 'recent' && !recentLoaded) loadRecent();
  }
  document.querySelectorAll('#clientCurrent .playlist-item').forEach(item => item.addEventListener('click', () => load(item)));
  document.querySelectorAll('[data-client-tab]').forEach(btn => btn.addEventListener('click', () => tab(btn.dataset.clientTab)));
  if (els.toggle) els.toggle.addEventListener('click', () => setPlaylist());
  if (els.backdrop) els.backdrop.addEventListener('click', () => setPlaylist(false));
  if (player) {
    player.addEventListener('play', sendSession);
    player.addEventListener('pause', sendSession);
    player.addEventListener('seeked', sendSession);
    player.addEventListener('timeupdate', () => { if (Math.floor((player.currentTime || 0)) % 10 === 0) sendSession(); });
    setInterval(sendSession, 10000);
  }
  // 保持之前的键盘操作：左右 15 秒，上下音量
  document.addEventListener('keydown', function (e) {
    const tag = (e.target && e.target.tagName) || '';
    if (!player || tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); player.currentTime = Math.max(0, (player.currentTime || 0) - 15); }
    if (e.key === 'ArrowRight') { e.preventDefault(); player.currentTime = (player.currentTime || 0) + 15; }
    if (e.key === 'ArrowUp') { e.preventDefault(); player.volume = Math.min(1, (player.volume || 1) + 0.05); }
    if (e.key === 'ArrowDown') { e.preventDefault(); player.volume = Math.max(0, (player.volume || 1) - 0.05); }
  });
  function leave() { try { if (player) player.pause(); } catch (e) {} }
  window.addEventListener('pagehide', leave);
  window.addEventListener('beforeunload', leave);
  if (stage) recordPlay(stage.dataset.titleId);
  sendSession();
})();
