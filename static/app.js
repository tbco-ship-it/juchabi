(async function () {
  const cssHref = document.querySelector('link[href*="static/style.css"]').getAttribute('href');
  const v = (cssHref.match(/\?v=([^&]+)/) || [])[1] || '';
  const base = cssHref.replace(/static\/style\.css.*$/, '');
  const $ = id => document.getElementById(id);
  const won = n => n.toLocaleString('ko-KR') + '원';
  // [name, sido idx, sigungu, locality, slug|0, lat, lng, fee(0 무료/1 유료/2 혼합), basic_min, basic_won, add_min, add_won, day_won, month_won, free_open, spaces]
  const FEE = ['무료', '유료', '혼합'];
  const L = S => a => ({ name: a[0], sido: S[a[1]][1], sigungu: a[2], loc: a[3], path: `${S[a[1]][0]}/${a[2]}/${a[4] || a[0].replace(/\s+/g, '')}/`, lat: a[5], lng: a[6], fee: FEE[a[7]], bmin: a[8], bwon: a[9], amin: a[10], awon: a[11], day: a[12], month: a[13], fo: a[14], spaces: a[15] });
  // Same rule as scripts/build.py cost(): basic block, then ceil(extra/unit)*unit fee, capped by the day ticket.
  function cost(l, min) {
    if (l.fee === '무료') return 0;
    if ((!l.bwon && !l.awon) || l.bwon == null || (l.bmin == null && l.amin == null)) return null;
    const b = l.bmin || 0; let t;
    if (min <= b) t = l.bwon; else if (l.amin && l.awon != null) t = l.bwon + Math.ceil((min - b) / l.amin) * l.awon; else return null;
    if (l.day) t = Math.min(t, l.day);
    return t;
  }
  const feeLine = l => l.fee === '무료' ? '무료' : (l.bwon != null && l.bmin ? `기본 ${l.bmin}분 ${won(l.bwon)}${l.amin && l.awon != null ? ` · 추가 ${l.amin}분당 ${won(l.awon)}` : ''}` : (l.bwon != null && l.amin && l.awon != null ? `${l.amin}분당 ${won(l.awon)}` : '요금 미기재'));

  // ── lot page: 시간 × 감면 toggles drive the big number ──
  const sheet = document.querySelector('.sheet[data-lot]');
  if (sheet && $('dur')) {
    const num = $('fee-num'), lab = $('fee-for');
    const base1 = JSON.parse(sheet.dataset.costs || '{}');
    let h = 1, pct = 0;
    const paint = () => { const b = base1[h]; if (b == null) { num.textContent = '—'; lab.textContent = `${h}시간 · 계산 불가`; return; } const c = Math.round(b * (100 - pct) / 100 / 10) * 10; num.textContent = won(c); lab.textContent = `${h}시간${pct ? ` · ${pct}% 감면` : ''}`; };
    const seg = (id, key, set) => $(id).addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; $(id).querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', x === b)); set(+b.dataset[key]); paint(); });
    seg('dur', 'h', x => h = x); seg('disc', 'pct', x => pct = x);
    return;
  }

  // ── home: typeahead + geolocation over the compact index ──
  const input = $('q'); if (!input) return;
  const IDX = await (await fetch(base + 'static/index.json?v=' + v)).json();
  const D = IDX.items.map(L(IDX.sidos));
  const out = $('result'), menu = $('q-menu');
  const norm = s => (s || '').toLowerCase().replace(/\s+/g, '');
  function card(l, extra = '') {
    const c1 = cost(l, 60), c2 = cost(l, 120), c3 = cost(l, 180);
    const cls = l.fee === '무료' ? 'balanced' : (c1 == null ? 'quiet' : '');
    const head = l.fee === '무료' ? '무료' : (c1 == null ? l.fee : won(c1));
    const sub = l.fee === '무료' ? '' : (c1 == null ? '' : '1시간');
    const line = l.fee === '무료' ? `무료 주차장${l.fo ? ' · 무료 개방 문구 있음' : ''}${l.spaces ? ` · ${l.spaces}면` : ''}` : `${feeLine(l)}${c2 != null ? ` · 2시간 ${won(c2)} · 3시간 ${won(c3)}` : ''}${l.day ? ` · 일주차 ${won(l.day)}` : ''}${l.month ? ` · 월정기 ${won(l.month)}` : ''}`;
    return `<section class="sheet ${cls}"><p class="sheet-label">${l.sido} ${l.sigungu}${l.loc ? ' ' + l.loc : ''}${extra}</p><div class="sheet-num"><span class="num${head.length > 6 ? ' small-num' : ''}">${head}</span>${sub ? `<span class="pct">${sub}</span>` : ''}</div><p class="sheet-title">${l.name}</p><p class="sheet-text">${line}</p><p class="sheet-actions"><a class="next" href="${base}${l.path.split('/').map(encodeURIComponent).join('/')}">요금표·감면·운영시간</a>${l.lat ? `<a class="next" href="https://map.naver.com/p/search/${encodeURIComponent(l.name + ' ' + l.sigungu)}" target="_blank" rel="noopener">네이버 지도</a>` : ''}</p></section>`;
  }
  let items = [], active = -1;
  function open(q) {
    const nq = norm(q);
    items = nq ? D.filter(l => norm(l.name).includes(nq) || norm(l.sigungu + l.loc).includes(nq) || norm(l.sido + l.sigungu).includes(nq)).slice(0, 8) : [];
    menu.innerHTML = items.length ? items.map((l, i) => `<li role="option" data-i="${i}" ${i === active ? 'aria-selected="true"' : ''}>${l.name}<small class="muted"> ${l.sido} ${l.sigungu}${l.loc ? ' ' + l.loc : ''} · ${l.fee === '무료' ? '무료' : (cost(l, 60) != null ? '1시간 ' + won(cost(l, 60)) : l.fee)}</small></li>`).join('') : (nq ? '<li class="empty">이 이름의 공영주차장이 없어요. 동네나 역 이름으로도 찾아보세요.</li>' : '<li class="empty">주차장 이름, 동네, 역 이름을 입력하세요.</li>');
    menu.hidden = false; input.setAttribute('aria-expanded', 'true');
  }
  function close() { menu.hidden = true; active = -1; input.setAttribute('aria-expanded', 'false'); }
  function leaveLanding() {
    const html = document.documentElement; if (!html.classList.contains('landing')) return;
    const stage = $('stage'), hero = stage.firstElementChild;
    const y0 = hero.getBoundingClientRect().top;
    html.classList.remove('landing');
    const dy = y0 - hero.getBoundingClientRect().top;
    if (dy > 0 && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
      stage.style.transition = 'none'; stage.style.transform = `translateY(${dy}px)`; void stage.offsetHeight;
      stage.style.transition = 'transform 1s cubic-bezier(.16,1,.3,1)'; stage.style.transform = 'translateY(0)';
      stage.addEventListener('transitionend', () => { stage.style.transition = ''; stage.style.transform = ''; }, { once: true });
    }
    if (window.__reveal) window.__reveal($('below'), true, 500);
  }
  function show(html) {
    leaveLanding(); out.innerHTML = html;
    out.classList.remove('is-in'); out.classList.add('reveal');
    let i = 0; out.querySelectorAll(':scope > *').forEach(c => { [c, ...c.children].forEach(el => { el.classList.add('rv'); el.style.setProperty('--d', (i++ * 90) + 'ms'); }); });
    void out.offsetHeight; out.classList.add('is-in');
  }
  function choose(l) { input.value = l.name; close(); show(card(l)); localStorage.setItem('juchabi.lot', l.path); }
  input.addEventListener('focus', () => { setTimeout(() => input.select(), 0); open(input.value); });
  input.addEventListener('input', () => { active = -1; open(input.value); });
  input.addEventListener('keydown', e => {
    if (menu.hidden) return;
    if (e.key === 'ArrowDown') { active = Math.min(active + 1, items.length - 1); open(input.value); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { active = Math.max(active - 1, 0); open(input.value); e.preventDefault(); }
    else if (e.key === 'Enter') { const it = items[active >= 0 ? active : 0]; if (it) choose(it); e.preventDefault(); }
    else if (e.key === 'Escape') close();
  });
  menu.addEventListener('mousedown', e => { const li = e.target.closest('li[data-i]'); if (li) { choose(items[+li.dataset.i]); e.preventDefault(); } });
  input.addEventListener('blur', () => setTimeout(close, 120));

  const geoBtn = $('geo'), geoIdle = geoBtn.textContent;
  const busy = on => { geoBtn.disabled = on; geoBtn.classList.toggle('busy', on); geoBtn.textContent = on ? '위치를 확인하는 중…' : geoIdle; };
  geoBtn.addEventListener('click', () => {
    const msg = $('geo-msg'); msg.hidden = true;
    if (!navigator.geolocation) { msg.hidden = false; msg.textContent = '이 브라우저는 위치를 지원하지 않아요. 이름으로 찾아 주세요.'; return; }
    busy(true);
    navigator.geolocation.getCurrentPosition(pos => {
      busy(false); msg.hidden = false;
      const { latitude: la, longitude: lo } = pos.coords;
      const dist = l => { const dLat = (l.lat - la) * Math.PI / 180, dLng = (l.lng - lo) * Math.PI / 180; const a = Math.sin(dLat / 2) ** 2 + Math.cos(la * Math.PI / 180) * Math.cos(l.lat * Math.PI / 180) * Math.sin(dLng / 2) ** 2; return 6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a)); };
      const near = D.filter(l => l.lat).map(l => ({ l, d: dist(l) })).sort((a, b) => a.d - b.d).slice(0, 5);
      msg.textContent = `가까운 공영주차장 ${near.length}곳`;
      show(near.map(({ l, d }) => card(l, ` · ${d < 1 ? Math.round(d * 1000) + ' m' : d.toFixed(1) + ' km'}`)).join(''));
    }, () => { busy(false); msg.hidden = false; msg.textContent = '위치 권한이 없어요. 이름으로 찾아 주세요.'; }, { timeout: 8000 });
  });
  const remembered = D.find(l => l.path === localStorage.getItem('juchabi.lot'));
  if (remembered) { $('last-name').textContent = remembered.name; $('last').hidden = false; $('last').addEventListener('click', () => choose(remembered)); }
})();
