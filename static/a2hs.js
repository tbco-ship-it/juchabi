// Add to Home Screen. Shared across the sites — change here, port to all (same file in every repo).
// A bottom pill appears once the visitor has got value (home: after the first result; other pages: after a scroll and a few seconds),
// phones only, never when already opened from the home screen, and not again for 30 days after ✕.
// Android/Chromium: the pill fires the browser's own install dialog (beforeinstallprompt, held until the pill is tapped).
// iOS: Apple has no install API, and the Web Share sheet (navigator.share) deliberately omits Safari's own
// "Add to Home Screen" item (verified on iOS 26, 2026-09-20) — so the pill opens a sheet of steps pointing at Safari's
// own share button: iOS 26+ compact bar = ⋯ next to the address capsule → Share; older iOS = the share button at the bottom.
// position:fixed for everything so nothing in the page moves (no CLS).
(function () {
  const d = document, h = d.documentElement;
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  const phone = matchMedia('(pointer: coarse)').matches && innerWidth < 900;
  if (standalone || !phone) return;
  const KEY = 'a2hs.until';
  try { if (+localStorage.getItem(KEY) > Date.now()) return; } catch (e) {}
  const snooze = days => { try { localStorage.setItem(KEY, String(Date.now() + days * 864e5)); } catch (e) {} };
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const iosMajor = ios ? +((navigator.userAgent.match(/OS (\d+)_/) || [])[1] || 0) : 0; // 0 = iPadOS/desktop UA: assume the new layout
  const name = (d.querySelector('meta[name="apple-mobile-web-app-title"]') || {}).content || d.title;
  const icon = (d.querySelector('link[rel="apple-touch-icon"]') || {}).href || '';
  const T = {
    ko: { pill: '홈 화면에 추가', sub: '다음엔 아이콘 한 번으로', close: '닫기', title: '홈 화면에 추가', lead: '앱처럼 바로 열려요. 설치 없이, 용량 없이.',
          s1new: '주소창 오른쪽 <b>⋯</b> 을 누르고 <b>공유</b>', s1old: '사파리 아래쪽 <b>공유</b> 버튼을 눌러요', s2: '메뉴 아래쪽 <b>홈 화면에 추가</b>를 눌러요', s2n: '안 보이면 <b>더 보기</b> 안에 있어요', s3: '오른쪽 위 <b>추가</b>',
          ok2: '알겠어요', later: '다음에', ok: '설치', add: '추가' },
    ja: { pill: 'ホーム画面に追加', sub: '次からはアイコン1つで', close: '閉じる', title: 'ホーム画面に追加', lead: 'アプリのようにすぐ開けます。インストール不要。',
          s1new: 'アドレスバー右の<b>⋯</b>をタップして<b>共有</b>', s1old: 'Safari下部の<b>共有</b>ボタンをタップ', s2: 'メニュー下の<b>ホーム画面に追加</b>をタップ', s2n: '見当たらなければ<b>その他</b>の中にあります', s3: '右上の<b>追加</b>',
          ok2: 'わかりました', later: 'あとで', ok: 'インストール', add: '追加' },
    en: { pill: 'Add to Home Screen', sub: 'Next time, one tap', close: 'Close', title: 'Add to Home Screen', lead: 'Opens like an app. Nothing to install, no storage used.',
          s1new: 'Tap <b>⋯</b> next to the address bar, then <b>Share</b>', s1old: 'Tap Safari\'s <b>share</b> button at the bottom', s2: 'Tap <b>Add to Home Screen</b> near the bottom', s2n: 'Not there? It is under <b>More</b>', s3: 'Tap <b>Add</b> top right',
          ok2: 'Got it', later: 'Not now', ok: 'Install', add: 'Add' }
  };
  const t = () => T[(h.lang || 'en').slice(0, 2)] || T.en;
  const css = `
.a2hs{position:fixed;left:50%;bottom:calc(14px + env(safe-area-inset-bottom));transform:translate(-50%,120%);z-index:60;display:flex;align-items:center;gap:10px;max-width:calc(100vw - 24px);padding:8px 8px 8px 10px;border:1px solid var(--line);border-radius:999px;background:var(--surface);color:var(--text);box-shadow:0 12px 32px -12px rgba(0,0,0,.35);font:inherit;opacity:0;transition:transform .5s cubic-bezier(.16,1,.3,1),opacity .4s}
.a2hs.on{transform:translate(-50%,0);opacity:1}
.a2hs img{width:30px;height:30px;border-radius:8px;flex:none}
.a2hs .t{display:flex;flex-direction:column;line-height:1.2;text-align:left;min-width:0}
.a2hs .t b{font-size:.92rem;font-weight:700;white-space:nowrap}.a2hs .t span{font-size:.76rem;color:var(--muted);white-space:nowrap}
.a2hs .go,.a2hs .x{appearance:none;border:0;font:inherit;cursor:pointer;flex:none}
.a2hs .go{padding:8px 14px;border-radius:999px;background:var(--blue);color:#fff;font-weight:700;font-size:.88rem}
.a2hs .x{width:30px;height:30px;border-radius:50%;background:transparent;color:var(--muted);font-size:1.1rem;line-height:1}
.a2hs-bg{position:fixed;inset:0;z-index:70;background:rgba(0,0,0,.45);opacity:0;transition:opacity .3s}
.a2hs-sheet{position:fixed;left:0;right:0;bottom:0;z-index:71;padding:20px 20px calc(20px + env(safe-area-inset-bottom));border-radius:var(--r-sheet,24px) var(--r-sheet,24px) 0 0;background:var(--bg);color:var(--text);transform:translateY(100%);transition:transform .45s cubic-bezier(.16,1,.3,1);max-height:88vh;overflow:auto;box-shadow:0 -8px 40px rgba(0,0,0,.25)}
.a2hs-open .a2hs-bg{opacity:1}.a2hs-open .a2hs-sheet{transform:none}
.a2hs-sheet .hd{display:flex;align-items:center;gap:12px;margin-bottom:6px}.a2hs-sheet .hd img{width:44px;height:44px;border-radius:11px}
.a2hs-sheet .hd b{font-size:1.15rem;display:block}.a2hs-sheet .hd span{font-size:.86rem;color:var(--muted)}
.a2hs-sheet ol{list-style:none;margin:14px 0 0;padding:0}.a2hs-sheet li{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--line);font-size:.95rem;line-height:1.45}
.a2hs-sheet li i{flex:none;width:26px;height:26px;border-radius:50%;background:var(--blue-soft);color:var(--blue);font:700 .85rem/26px var(--font);text-align:center;font-style:normal}
.a2hs-sheet li small{display:block;color:var(--muted);font-size:.82rem}
.a2hs-sheet li svg{width:18px;height:18px;vertical-align:-3px;margin:0 2px}
.a2hs-sheet .go{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;margin-top:16px;padding:15px;border:0;border-radius:14px;background:var(--blue);color:#fff;font:700 1rem var(--font);cursor:pointer}
.a2hs-sheet .go svg{width:20px;height:20px}
.a2hs-sheet .later{display:block;width:100%;margin-top:6px;padding:12px;border:0;background:none;color:var(--muted);font:inherit;font-size:.9rem;cursor:pointer}
@media (prefers-reduced-motion:reduce){.a2hs,.a2hs-sheet,.a2hs-bg{transition:none}}`;
  const SHARE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v13"/><path d="M8 7l4-4 4 4"/><path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7"/></svg>';
  const PLUS = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="4"/><path d="M12 8v8M8 12h8"/></svg>';
  let deferred = null, pill = null, shown = false, ready = false;
  addEventListener('beforeinstallprompt', e => { e.preventDefault(); deferred = e; if (ready) show(); });
  addEventListener('appinstalled', () => { snooze(365); hide(); });

  function show() {
    if (shown || (!ios && !deferred)) return; shown = true;
    const s = t();
    d.head.appendChild(Object.assign(d.createElement('style'), { textContent: css }));
    pill = d.createElement('div'); pill.className = 'a2hs'; pill.setAttribute('role', 'dialog'); pill.setAttribute('aria-label', s.pill);
    pill.innerHTML = `${icon ? `<img src="${icon}" alt="">` : ''}<div class="t"><b>${s.pill}</b><span>${s.sub}</span></div><button type="button" class="go">${ios ? s.add : s.ok}</button><button type="button" class="x" aria-label="${s.close}">✕</button>`;
    d.body.appendChild(pill);
    requestAnimationFrame(() => requestAnimationFrame(() => pill.classList.add('on')));
    pill.querySelector('.x').onclick = () => { snooze(30); hide(); };
    pill.querySelector('.go').onclick = ios ? sheet : async () => {
      const p = deferred; deferred = null; hide();
      try { p.prompt(); const r = await p.userChoice; snooze(r.outcome === 'accepted' ? 365 : 30); } catch (e) { snooze(30); }
    };
  }
  function hide() { if (!pill) return; pill.classList.remove('on'); setTimeout(() => pill.remove(), 500); }
  function sheet() {
    const s = t();
    const bg = d.createElement('div'); bg.className = 'a2hs-bg';
    const sh = d.createElement('div'); sh.className = 'a2hs-sheet'; sh.setAttribute('role', 'dialog'); sh.setAttribute('aria-label', s.title);
    sh.innerHTML = `<div class="hd">${icon ? `<img src="${icon}" alt="">` : ''}<div><b>${name}</b><span>${s.lead}</span></div></div>
      <ol><li><i>1</i><div>${iosMajor && iosMajor < 26 ? s.s1old : s.s1new} ${SHARE}</div></li>
      <li><i>2</i><div>${s.s2} ${PLUS}<small>${s.s2n}</small></div></li>
      <li><i>3</i><div>${s.s3}</div></li></ol>
      <button type="button" class="go">${s.ok2}</button><button type="button" class="later">${s.later}</button>`;
    d.body.append(bg, sh); h.classList.add('a2hs-open');
    const close = () => { h.classList.remove('a2hs-open'); setTimeout(() => { bg.remove(); sh.remove(); }, 450); };
    bg.onclick = close; sh.querySelector('.later').onclick = () => { snooze(30); close(); hide(); };
    // Can't observe whether they add it: if they do, the next launch is standalone and we never ask again; if not, don't nag for 30 days.
    sh.querySelector('.go').onclick = () => { snooze(30); close(); hide(); };
  }
  // The moment of value: home page leaves its landing state on the first result; elsewhere a scroll plus a few seconds.
  const arm = () => { if (ready) return; ready = true; show(); };
  if (h.classList.contains('landing')) {
    new MutationObserver((_, o) => { if (!h.classList.contains('landing')) { o.disconnect(); setTimeout(arm, 1600); } }).observe(h, { attributes: true, attributeFilter: ['class'] });
  } else {
    const t0 = Date.now();
    addEventListener('scroll', function f() { if (scrollY > 240) { removeEventListener('scroll', f); setTimeout(arm, Math.max(0, 5000 - (Date.now() - t0))); } }, { passive: true });
  }
})();
