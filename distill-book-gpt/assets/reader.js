'use strict';
document.documentElement.classList.add('has-js');
const chapters = [...document.querySelectorAll('details.chapter')];
for (const button of document.querySelectorAll('[data-open]')) {
  button.addEventListener('click', () => {
    for (const chapter of chapters) chapter.open = button.dataset.open === 'true';
  });
}
function revealHash() {
  let key;
  try { key = decodeURIComponent(location.hash.slice(1)); } catch (_) { return; }
  const target = document.getElementById(key);
  if (!target) return;
  let el = target;
  while (el) {
    if (el.tagName === 'DETAILS') el.open = true;
    el = el.parentElement;
  }
  requestAnimationFrame(() => target.scrollIntoView({block: 'start'}));
}
window.addEventListener('hashchange', revealHash);
document.querySelectorAll('a[href^="#"]').forEach(a => {
  a.addEventListener('click', () => { if (a.hash === location.hash) revealHash(); });
});
revealHash();
let printState = null;
window.addEventListener('beforeprint', () => {
  if (!printState) printState = [...document.querySelectorAll('details')].map(el => [el, el.open]);
  for (const [el] of printState) el.open = true;
});
window.addEventListener('afterprint', () => {
  if (printState) for (const [el, open] of printState) el.open = open;
  printState = null;
});
document.getElementById('print-book')?.addEventListener('click', () => window.print());
