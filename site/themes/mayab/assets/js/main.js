(() => {
  'use strict';
  const initTheme = () => {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    const getTheme = () => { try { return localStorage.getItem('mayab-theme'); } catch (_) { return null; } };
    const setTheme = (t) => { try { localStorage.setItem('mayab-theme', t); } catch (_) {} };
    const apply = (t) => {
      if (t === 'dark' || t === 'light') {
        document.documentElement.setAttribute('data-theme', t);
      } else {
        document.documentElement.removeAttribute('data-theme');
      }
      const isDark = document.documentElement.getAttribute('data-theme') === 'dark' ||
        (!document.documentElement.hasAttribute('data-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches);
      btn.setAttribute('aria-pressed', isDark ? 'true' : 'false');
      btn.setAttribute('aria-label', isDark ? 'Cambiar a modo claro' : 'Cambiar a modo oscuro');
      btn.innerHTML = isDark
        ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg><span class="theme-btn-label">Claro</span>'
        : '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg><span class="theme-btn-label">Oscuro</span>';
    };
    apply(getTheme() || 'system');
    btn.addEventListener('click', () => {
      const isDark = document.documentElement.getAttribute('data-theme') === 'dark' ||
        (!document.documentElement.hasAttribute('data-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches);
      const next = isDark ? 'light' : 'dark';
      setTheme(next);
      apply(next);
    });
  };

  const initStickyOffset = () => {
    const header = document.querySelector('.site-header');
    if (!header) return;
    const updateHeight = (h) => {
      document.documentElement.style.setProperty('--header-h', `${Math.ceil(h)}px`);
    };
    updateHeight(header.offsetHeight);
    if (typeof ResizeObserver !== 'undefined') {
      const ro = new ResizeObserver((entries) => {
        for (const entry of entries) {
          const h = entry.borderBoxSize?.[0]?.blockSize ?? header.offsetHeight;
          updateHeight(h);
        }
      });
      ro.observe(header);
    }
  };

  const initPopovers = () => {
    const popovers = document.querySelectorAll('details.popover-details');
    if (!popovers.length) return;
    popovers.forEach((det) => {
      det.addEventListener('toggle', () => {
        if (det.open) {
          popovers.forEach((other) => {
            if (other !== det && other.open) other.removeAttribute('open');
          });
        }
      });
    });
    document.addEventListener('click', (e) => {
      popovers.forEach((det) => {
        if (det.open && !det.contains(e.target)) det.removeAttribute('open');
      });
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        popovers.forEach((det) => {
          if (det.open) det.removeAttribute('open');
        });
      }
    });
  };

  const initFilter = () => {
    const bar = document.getElementById('category-filter-bar');
    if (!bar) return;
    const chips = bar.querySelectorAll('.cat-chip-btn');
    const cards = document.querySelectorAll('.event-card[data-cat]');
    if (!chips.length || !cards.length) return;

    const filter = (slug) => {
      let count = 0;
      cards.forEach((card) => {
        const cats = (card.getAttribute('data-cat') || '').split(' ');
        const ok = !slug || slug === 'todas' || cats.includes(slug);
        card.classList.toggle('hidden', !ok);
        if (ok) count++;
      });
      chips.forEach((c) => {
        const s = c.getAttribute('data-cat-slug');
        const active = (!slug && s === 'todas') || s === slug;
        c.setAttribute('aria-pressed', active ? 'true' : 'false');
        c.classList.toggle('active', active);
      });
      const counter = document.getElementById('cat-results-counter');
      if (counter) counter.textContent = `${count} eventos mostrados`;
    };

    chips.forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        const slug = btn.getAttribute('data-cat-slug');
        if (slug === 'todas') {
          history.replaceState(null, '', window.location.pathname);
          filter('todas');
        } else {
          window.location.hash = slug;
          filter(slug);
        }
      });
    });

    const onHash = () => {
      const h = window.location.hash.replace('#', '').trim();
      filter(h || 'todas');
    };
    window.addEventListener('hashchange', onHash);
    onHash();
  };

  document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    initStickyOffset();
    initFilter();
    initPopovers();
  });
})();
