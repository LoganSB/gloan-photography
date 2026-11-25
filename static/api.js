/**
 * Backend HTTP client, authentication state and shared DOM/tag utilities.
 */

let onOwnerChangedCallback = null;

/**
 * Register a callback invoked whenever Admin login state changes so the gallery can re-render.
 */
export function registerOwnerRenderCallback(callback) {
  onOwnerChangedCallback = callback;
}

/**
 * Query a single DOM element by CSS selector.
 */
export function select(selector) {
  return document.querySelector(selector);
}

/**
 * Open or close a modal/lightbox overlay and lock background page scrolling
 * whenever at least one `.modal.open` or `.lb.open` overlay is visible.
 * Centralizing this prevents nested modals (e.g. Share/Tags over Lightbox) from unlocking scroll early.
 */
export function setOverlayOpen(overlayOrSelector, isOpen) {
  const overlayEl = typeof overlayOrSelector === 'string' ? select(overlayOrSelector) : overlayOrSelector;
  if (!overlayEl) return;
  overlayEl.classList.toggle('open', Boolean(isOpen));
  const anyOverlayOpen = Boolean(document.querySelector('.modal.open, .lb.open'));
  document.documentElement.classList.toggle('scroll-locked', anyOverlayOpen);
  document.body.classList.toggle('scroll-locked', anyOverlayOpen);
}

/**
 * Create a DOM element with optional className and innerHTML.
 */
export function createElement(tagName, className, innerHtml) {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  if (innerHtml != null) element.innerHTML = innerHtml;
  return element;
}

/**
 * Escape user-controlled text before interpolating into HTML templates to prevent XSS.
 */
export function escapeHtml(rawValue) {
  return String(rawValue).replace(/[&<>"']/g, char => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]
  ));
}

/**
 * Normalize a single tag string to lowercase hyphenated form, matching server clean_tags().
 */
export function normalizeTag(rawTag) {
  return rawTag.trim().toLowerCase().replace(/\s+/g, '-');
}

/**
 * Parse a comma-separated tag input string into unique normalized tags.
 */
export function parseTags(commaSeparated) {
  return [...new Set(commaSeparated.split(',').map(normalizeTag).filter(Boolean))];
}

let toastTimerId = null;

/**
 * Show a temporary status message pill at the bottom of the screen.
 */
export function toast(message) {
  const toastEl = select('#toast');
  toastEl.textContent = message;
  toastEl.classList.add('show');
  clearTimeout(toastTimerId);
  toastTimerId = setTimeout(() => toastEl.classList.remove('show'), 2200 + message.length * 50);
}

/**
 * Run an async action while showing a spinning wheel and optional temporary label on `element`.
 * Prevents duplicate clicks and guarantees the user sees instant visual feedback on press.
 */
export async function withBusy(element, busyLabel, action) {
  if (!element || element.classList.contains('busy')) return;
  const originalHtml = element.innerHTML;
  const wasDisabled = 'disabled' in element ? element.disabled : false;
  element.classList.add('busy');
  if ('disabled' in element) element.disabled = true;
  if (busyLabel) {
    element.innerHTML = `<span class="spinner" aria-hidden="true"></span> <span>${escapeHtml(busyLabel)}</span>`;
  }
  try {
    return await action();
  } finally {
    element.classList.remove('busy');
    if ('disabled' in element) element.disabled = wasDisabled;
    if (busyLabel) element.innerHTML = originalHtml;
  }
}

/**
 * Return true when the page is currently in authenticated owner mode.
 */
export function isOwner() {
  return document.body.classList.contains('owner');
}

/**
 * Toggle owner UI controls and update the login/logout button label.
 */
export function setOwner(enabled, shouldRender = true) {
  document.body.classList.toggle('owner', enabled);
  select('#authBtn').innerHTML = enabled
    ? '<span class="lbl">Log out</span>'
    : '<span class="lbl">Admin login</span>';
  const tagBtn = select('#addTagBtn');
  if (tagBtn) tagBtn.textContent = enabled ? 'Tags' : 'Suggest tags';
  if (shouldRender && onOwnerChangedCallback) {
    onOwnerChangedCallback();
  }
}

let configuredTurnstileSiteKey = '';
let turnstileScriptPromise = null;

/**
 * Store the server's configured Cloudflare Turnstile site key (empty string when CAPTCHA is disabled).
 */
export function setTurnstileSiteKey(siteKey) {
  configuredTurnstileSiteKey = String(siteKey || '').trim();
}

/**
 * Create a reusable Cloudflare Turnstile widget controller for `boxSelector`.
 * Shared by the Admin login modal (`#turnstileBox`) and public visitor tag suggestions (`#tagTurnstileBox`).
 */
export function createTurnstileController(boxSelector) {
  let widgetId = null;
  let token = '';

  const loadScript = () => {
    if (window.turnstile) return Promise.resolve();
    if (!turnstileScriptPromise) {
      turnstileScriptPromise = new Promise(resolve => {
        const existing = select('#turnstileScript');
        if (existing) {
          existing.addEventListener('load', resolve, { once: true });
          return;
        }
        const script = document.createElement('script');
        script.id = 'turnstileScript';
        script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
        script.async = true;
        script.onload = resolve;
        document.head.append(script);
      });
    }
    return turnstileScriptPromise;
  };

  return {
    getToken: () => token,
    isRequired: () => Boolean(configuredTurnstileSiteKey),
    hide() {
      const box = select(boxSelector);
      if (box) box.style.display = 'none';
      token = '';
    },
    reset() {
      token = '';
      if (window.turnstile && widgetId !== null) {
        window.turnstile.reset(widgetId);
      }
    },
    async ensure() {
      const box = select(boxSelector);
      if (!box) return;
      if (!configuredTurnstileSiteKey) {
        box.style.display = 'none';
        return;
      }
      box.style.display = 'flex';
      await loadScript();
      if (!window.turnstile) return;
      if (widgetId !== null) {
        token = '';
        window.turnstile.reset(widgetId);
        return;
      }
      widgetId = window.turnstile.render(box, {
        sitekey: configuredTurnstileSiteKey,
        callback: nextToken => { token = nextToken; },
        'expired-callback': () => { token = ''; },
      });
    },
  };
}

// Clear any legacy localStorage token left over from earlier revisions before HttpOnly cookie sessions.
try {
  localStorage.removeItem('gloan-token');
} catch (error) {
  // Ignore storage restrictions in private browsing modes.
}

/**
 * Backend API wrapper using HttpOnly SameSite=Strict session cookies + in-memory X-CSRF-Token header.
 */
export const API = {
  csrfToken: '',

  /**
   * Store or clear the session-bound CSRF token in memory (never written to localStorage).
   */
  setCsrfToken(tokenValue) {
    this.csrfToken = String(tokenValue || '');
  },

  /**
   * Perform a JSON or binary request against the server and unwrap errors cleanly.
   */
  async call(method, path, { body, headers } = {}) {
    const requestHeaders = { ...headers };
    if (this.csrfToken && (method === 'POST' || method === 'PATCH' || method === 'DELETE')) {
      requestHeaders['X-CSRF-Token'] = this.csrfToken;
    }
    const response = await fetch(path, {
      method,
      headers: requestHeaders,
      body,
      credentials: 'same-origin',
    });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401 && isOwner() && !path.startsWith('/api/passkey/')) {
      this.setCsrfToken('');
      setOwner(false);
      toast('Session expired — log in again');
    }
    if (!response.ok) throw new Error(payload.error || response.statusText);
    return payload;
  },

  logout() {
    return this.call('POST', '/api/logout');
  },

  list(query) {
    return this.call('GET', '/api/photos?' + new URLSearchParams(query));
  },

  getPhoto(photoId) {
    return this.call('GET', '/api/photos/' + encodeURIComponent(photoId));
  },

  clearAiTags() {
    return this.call('DELETE', '/api/aitags');
  },

  session() {
    return this.call('GET', '/api/session');
  },

  tags(query, limit = 8) {
    return this.call('GET', '/api/tags?' + new URLSearchParams({ q: query, limit }));
  },

  passkeyRegisterOptions() {
    return this.call('POST', '/api/passkey/register/options');
  },

  passkeyRegister(payload) {
    return this.call('POST', '/api/passkey/register', {
      body: JSON.stringify(payload),
      headers: { 'Content-Type': 'application/json' },
    });
  },

  passkeyLoginOptions() {
    return this.call('POST', '/api/passkey/login/options');
  },

  passkeyLogin(payload) {
    return this.call('POST', '/api/passkey/login', {
      body: JSON.stringify(payload),
      headers: { 'Content-Type': 'application/json' },
    });
  },

  upload(blob, title, tags) {
    return this.call('POST', '/api/photos', {
      body: blob,
      headers: {
        'Content-Type': blob.type || 'application/octet-stream',
        'X-Title': encodeURIComponent((title || '').slice(0, 200)),
        'X-Tags': encodeURIComponent(tags.join(',')),
      },
    });
  },

  update(photoId, title, tags, story, aiTags) {
    const payload = { title, tags, story };
    if (Array.isArray(aiTags)) payload.aiTags = aiTags;
    return this.call('PATCH', '/api/photos/' + photoId, {
      body: JSON.stringify(payload),
      headers: { 'Content-Type': 'application/json' },
    });
  },

  suggestTags(photoId, tags, captchaToken = '') {
    return this.call('POST', '/api/photos/' + encodeURIComponent(photoId) + '/suggest-tags', {
      body: JSON.stringify({ tags, captchaToken }),
      headers: { 'Content-Type': 'application/json' },
    });
  },

  listPendingTags() {
    return this.call('GET', '/api/tag-suggestions');
  },

  moderatePendingTag(photoId, tag, action) {
    return this.call('POST', '/api/photos/' + encodeURIComponent(photoId) + '/pending-tags', {
      body: JSON.stringify({ tag, action }),
      headers: { 'Content-Type': 'application/json' },
    });
  },

  remove(photoId) {
    return this.call('DELETE', '/api/photos/' + photoId);
  },
};
