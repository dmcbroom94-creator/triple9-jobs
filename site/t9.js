/* Triple 9 Careers – small page runtime. Renders each page's template with live data. No tracking, no cookies, nothing stored. */
(function () {
  'use strict';
  window.DCLogic = class {
    constructor(props) { this.props = props || {}; this.state = {}; }
    setState(patch) {
      const next = typeof patch === 'function' ? patch(this.state) : patch;
      this.state = Object.assign({}, this.state, next);
      if (this.__render) this.__render();
    }
  };

  const HOLE = /\{\{([^}]*)\}\}/g;
  const lookup = (scope, expr) => {
    expr = expr.trim();
    if (expr === 'true') return true;
    if (expr === 'false') return false;
    if (/^-?\d+(\.\d+)?$/.test(expr)) return Number(expr);
    return expr.split('.').reduce((o, k) => (o == null ? undefined : o[k]), scope);
  };
  const interp = (scope, str) => str.replace(HOLE, (_, e) => { const v = lookup(scope, e); return v == null ? '' : String(v); });
  const single = (str) => { const m = /^\s*\{\{([^}]*)\}\}\s*$/.exec(str || ''); return m ? m[1] : null; };
  const listOf = (scope, attr) => { const e = single(attr); const l = e ? lookup(scope, e) : []; return Array.isArray(l) ? l : []; };

  function dispatch(ev) { const f = this.__h && this.__h[ev.type]; if (typeof f === 'function') f(ev); }
  function listen(el, type) { el.__ev = el.__ev || {}; if (!el.__ev[type]) { el.__ev[type] = true; el.addEventListener(type, dispatch); } }

  function renderNodes(nodes, scope, out) { for (const n of nodes) renderNode(n, scope, out); }

  function renderNode(node, scope, out) {
    if (node.nodeType === 3) { out.appendChild(document.createTextNode(interp(scope, node.nodeValue))); return; }
    if (node.nodeType !== 1) return;
    const tag = node.tagName.toLowerCase();
    if (tag === 'sc-for') {
      const as = node.getAttribute('as') || 'item';
      listOf(scope, node.getAttribute('list')).forEach((item, i) => {
        const s = Object.create(scope); s[as] = item; s.$index = i;
        renderNodes(node.childNodes, s, out);
      });
      return;
    }
    if (tag === 'sc-if') {
      const e = single(node.getAttribute('value'));
      if (e && lookup(scope, e)) renderNodes(node.childNodes, scope, out);
      return;
    }
    if (node.hasAttribute('data-if')) {
      const e = single(node.getAttribute('data-if'));
      if (!(e && lookup(scope, e))) return;
    }
    const svg = node.namespaceURI === 'http://www.w3.org/2000/svg';
    const el = svg ? document.createElementNS(node.namespaceURI, node.tagName) : document.createElement(node.tagName);
    for (const a of Array.from(node.attributes)) {
      const name = a.name;
      if (name.indexOf('hint-') === 0 || name === 'data-if' || name === 'data-for' || name === 'data-as') continue;
      if (/^on[a-z]+$/i.test(name)) {
        const e = single(a.value);
        const fn = e ? lookup(scope, e) : null;
        if (typeof fn === 'function') { el.__h = el.__h || {}; const t = name.slice(2).toLowerCase(); el.__h[t] = fn; listen(el, t); }
        continue;
      }
      el.setAttribute(name, interp(scope, a.value));
    }
    if (node.hasAttribute('data-for')) {
      const as = node.getAttribute('data-as') || 'item';
      listOf(scope, node.getAttribute('data-for')).forEach((item, i) => {
        const s = Object.create(scope); s[as] = item; s.$index = i;
        renderNodes(node.childNodes, s, el);
      });
    } else {
      renderNodes(node.childNodes, scope, el);
    }
    out.appendChild(el);
  }

  /* Update the page in place, so only what changed is touched (animations do not replay). */
  function same(a, b) { return a.nodeType === b.nodeType && (a.nodeType !== 1 || a.tagName === b.tagName); }
  function patch(oldEl, newEl) {
    if (oldEl.nodeType === 3) { if (oldEl.nodeValue !== newEl.nodeValue) oldEl.nodeValue = newEl.nodeValue; return; }
    for (const a of Array.from(oldEl.attributes)) if (!newEl.hasAttribute(a.name)) oldEl.removeAttribute(a.name);
    for (const a of Array.from(newEl.attributes)) if (oldEl.getAttribute(a.name) !== a.value) oldEl.setAttribute(a.name, a.value);
    oldEl.__h = newEl.__h;
    if (newEl.__h) Object.keys(newEl.__h).forEach((t) => listen(oldEl, t));
    patchChildren(oldEl, newEl);
  }
  function patchChildren(oldP, newP) {
    const o = Array.from(oldP.childNodes), n = Array.from(newP.childNodes);
    n.forEach((nn, i) => {
      const oo = o[i];
      if (!oo) oldP.appendChild(nn);
      else if (same(oo, nn)) patch(oo, nn);
      else oldP.replaceChild(nn, oo);
    });
    for (let i = n.length; i < o.length; i++) o[i].remove();
  }

  function boot() {
    const host = document.getElementById('t9-tpl') || document.querySelector('x-dc');
    if (!host) return;
    const root = host.content || host;
    const helmet = root.querySelector && root.querySelector('helmet');
    if (helmet) { Array.from(helmet.childNodes).forEach((c) => document.head.appendChild(document.importNode(c, true))); helmet.remove(); }
    const tpl = Array.from(root.childNodes);
    let app = document.getElementById('t9-app');
    const pre = !!app;
    if (pre) host.remove(); else { app = document.createElement('div'); app.id = 't9-app'; host.replaceWith(app); }
    const src = document.querySelector('script[data-dc-script]');
    let comp = { renderVals: () => ({}) };
    if (src) {
      const Component = new Function(src.textContent + '\n;return Component;')();
      comp = new Component({});
    }
    let queued = false, first = !pre;
    const render = () => {
      queued = false;
      const box = document.createElement('div');
      renderNodes(tpl, comp.renderVals(), box);
      if (first) { app.replaceChildren(...box.childNodes); first = false; } else patchChildren(app, box);
    };
    comp.__render = () => { if (!queued) { queued = true; requestAnimationFrame(render); } };
    render();
    if (location.hash) { const t = document.getElementById(location.hash.slice(1)); if (t) t.scrollIntoView(); }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
