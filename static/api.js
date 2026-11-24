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
  if (shouldRender && onOwnerChangedCallback) {
    onOwnerChangedCallback();
  }
}

/**
 * Backend API wrapper handling bearer token attachment and 401 session expiry recovery.
 */
export const API = {
  token: localStorage.getItem('gloan-token') || '',

  /**
   * Persist or clear the owner bearer token in localStorage.
   */
  setToken(tokenValue) {
    this.token = tokenValue;
    try {
      if (tokenValue) localStorage.setItem('gloan-token', tokenValue);
      else localStorage.removeItem('gloan-token');
    } catch (error) {
      // Private browsing modes may block localStorage writes; in-memory token still works for this tab.
    }
  },

  /**
   * Perform a JSON or binary request against the server and unwrap errors cleanly.
   */
  async call(method, path, { body, headers } = {}) {
    const requestHeaders = { ...headers };
    if (this.token) requestHeaders.Authorization = 'Bearer ' + this.token;
    const response = await fetch(path, { method, headers: requestHeaders, body });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401 && this.token && path !== '/api/login') {
      this.setToken('');
      setOwner(false);
      toast('Session expired — log in again');
    }
    if (!response.ok) throw new Error(payload.error || response.statusText);
    return payload;
  },

  list(query) {
    return this.call('GET', '/api/photos?' + new URLSearchParams(query));
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

  update(photoId, title, tags, story) {
    return this.call('PATCH', '/api/photos/' + photoId, {
      body: JSON.stringify({ title, tags, story }),
      headers: { 'Content-Type': 'application/json' },
    });
  },

  remove(photoId) {
    return this.call('DELETE', '/api/photos/' + photoId);
  },
};
