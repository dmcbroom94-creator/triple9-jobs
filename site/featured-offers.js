/* Featured offers for the discounts page. All content is treated as text. */
(function () {
  'use strict';

  function text(value) {
    return typeof value === 'string' ? value.trim() : '';
  }

  // Dates are calendar dates, not timestamps; a blank date is optional.
  function dateValue(value) {
    if (value === undefined || value === null || value === '') return '';
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const date = new Date(value + 'T00:00:00Z');
    return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value ? value : null;
  }

  function todayInUK() {
    const parts = new Intl.DateTimeFormat('en-GB', {
      timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit'
    }).formatToParts(new Date());
    const get = (type) => parts.find((part) => part.type === type).value;
    return get('year') + '-' + get('month') + '-' + get('day');
  }

  function offerURL(value) {
    if (value === undefined || value === null || value === '') return '';
    if (typeof value !== 'string') return null;
    if (!value.trim()) return '';
    try {
      const url = new URL(value.trim());
      return (url.protocol === 'https:' || url.protocol === 'http:') && !url.username && !url.password ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content) node.textContent = content;
    return node;
  }

  function detail(card, label, value) {
    if (!value) return;
    const line = element('p', 'fo-card__detail');
    line.append(element('strong', '', label + ': '), document.createTextNode(value));
    card.appendChild(line);
  }

  function cardFor(offer, expiry, url) {
    const card = element('article', 'fo-card');
    const header = element('div', 'fo-card__meta');
    header.appendChild(element('span', 'fo-card__retailer', text(offer.retailer)));
    if (offer.sponsored === true) header.appendChild(element('span', 'fo-card__ad', 'Advertisement'));
    card.appendChild(header);
    card.appendChild(element('h3', 'fo-card__title', text(offer.title)));
    card.appendChild(element('p', 'fo-card__description', text(offer.description)));
    if (text(offer.code)) {
      const code = element('p', 'fo-card__code');
      code.append(element('span', '', 'Discount code'), element('strong', '', text(offer.code)));
      card.appendChild(code);
    }
    detail(card, 'Eligibility', text(offer.eligibility));
    detail(card, 'Terms', text(offer.terms));
    if (expiry) {
      const formatted = new Intl.DateTimeFormat('en-GB', {
        day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC'
      }).format(new Date(expiry + 'T00:00:00Z'));
      detail(card, 'Expires', formatted);
    }
    if (url) {
      const link = element('a', 'fo-card__link', 'View offer →');
      link.href = url;
      if (offer.sponsored === true) link.rel = 'sponsored';
      card.appendChild(link);
    }
    return card;
  }

  async function loadOffers() {
    const root = document.querySelector('#t9-app .featured-offers');
    if (!root) return;
    const status = root.querySelector('[data-offers-status]');
    const list = root.querySelector('[data-offers-list]');
    status.textContent = 'Loading featured offers…';
    try {
      const response = await fetch('featured-offers.json', { cache: 'no-cache' });
      if (!response.ok) throw new Error('Could not load offers');
      const data = await response.json();
      if (!data || !Array.isArray(data.offers)) throw new Error('Invalid offers data');
      const today = todayInUK();
      const cards = document.createDocumentFragment();
      let shown = 0;
      let invalid = 0;
      data.offers.forEach((offer) => {
        if (!offer || typeof offer !== 'object' || Array.isArray(offer)) { invalid++; return; }
        const start = dateValue(offer.start);
        const expiry = dateValue(offer.expiry);
        const url = offerURL(offer.url);
        if (!text(offer.retailer) || !text(offer.title) || !text(offer.description) ||
            start === null || expiry === null || (start && expiry && start > expiry) || url === null ||
            (offer.sponsored !== undefined && typeof offer.sponsored !== 'boolean')) {
          invalid++;
          return;
        }
        if ((start && today < start) || (expiry && today > expiry)) return;
        cards.appendChild(cardFor(offer, expiry, url));
        shown++;
      });
      list.replaceChildren(cards);
      if (invalid) {
        status.textContent = 'Some featured offers could not be displayed. Please try again later.';
      } else if (!shown) {
        status.textContent = 'No featured offers are available at the moment. Please check back soon.';
      } else {
        status.textContent = '';
      }
    } catch (_) {
      list.replaceChildren();
      status.textContent = 'We couldn’t load featured offers right now. Please try again later.';
    }
  }

  // t9.js registers its DOMContentLoaded handler first and mounts the page before this runs.
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', loadOffers);
  else loadOffers();
})();
