// Scroll reveal: blocks trail the scroll (rise + blur→sharp, staggered), once, skipped under reduced motion.
// __reveal(root, force, base): arm root's direct block children; force=true also animates blocks already in view (used after the first result on the home page).
(function () {
  const hdr = document.querySelector('header.top');
  if (hdr) document.documentElement.style.setProperty('--hdr', hdr.offsetHeight + 'px');
  if (matchMedia('(prefers-reduced-motion: reduce)').matches || !('IntersectionObserver' in window)) return;
  const SEL = ':scope > section:not(:first-of-type), :scope > .grid2, :scope > h2, :scope > .tbl, :scope > .prose, :scope > .sec, :scope > .sheet:not(:first-of-type), :scope > .week, :scope > .legend, :scope > .card, :scope > .list';
  const io = new IntersectionObserver(entries => {
    for (const e of entries) if (e.isIntersecting) { e.target.classList.add('is-in'); io.unobserve(e.target); }
  }, { rootMargin: '0px 0px -12% 0px', threshold: 0 });
  window.__reveal = (root, force, base) => {
    if (!root) return;
    root.querySelectorAll(SEL).forEach(t => {
      if (!force && t.getBoundingClientRect().top <= innerHeight) return;
      // Sections with several blocks (heading, card, note) rise one after another; single blocks rise alone.
      const items = t.matches('.sec, .prose, .grid2') && t.children.length > 1 ? Array.from(t.children) : [t];
      items.forEach((el, i) => { el.classList.add('rv'); el.style.setProperty('--d', ((base || 0) + i * 110) + 'ms'); });
      t.classList.add('reveal'); io.observe(t);
    });
  };
  window.__reveal(document.querySelector('main'), false, 0);
})();
