/**
 * Application entry point: initializes modules, Admin login modal, theme toggle and keyboard shortcuts.
 */

import { API, isOwner, registerOwnerRenderCallback, select, setOwner, toast, withBusy } from './api.js';
import { fetchPage, registerGalleryActions, render, rescanFolder, runThumbnailJob, state } from './gallery.js';
import {
  closeLightbox,
  closeTagModal,
  currentLightboxPhoto,
  goSlide,
  initLightboxEvents,
  isSlideshowPlaying,
  openLightbox,
  openTagModal,
  playSlideshow,
  saveEdit,
  stopSlideshow,
  submitTags,
} from './lightbox.js';
import { initSearchEvents } from './search.js';
import { closeUpload, initUploadEvents, loadDemo, openUpload } from './upload.js';

registerOwnerRenderCallback(() => {
  if (state.ready) render();
});

registerGalleryActions({
  openLightbox,
  openTagModal,
  openUpload,
  loadDemo,
});

// Session capability flags returned by GET /api/session
let hasRegisteredPasskey = false;
let turnstileSiteKey = '';
let turnstileToken = '';
let turnstileWidgetId = null;
// DEBUG WORKAROUND FLAG (remove once cloud passkey is verified)
let debugPasswordEnabled = false;

/**
 * Convert a base64url string from the server into a Uint8Array for WebAuthn browser APIs.
 */
function base64UrlToBytes(base64Url) {
  const padded = base64Url.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (base64Url.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let idx = 0; idx < binary.length; idx++) bytes[idx] = binary.charCodeAt(idx);
  return bytes;
}

/**
 * Convert an ArrayBuffer from a WebAuthn browser response into an unpadded base64url string.
 */
function bufferToBase64Url(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let idx = 0; idx < bytes.length; idx++) binary += String.fromCharCode(bytes[idx]);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/**
 * Dynamically load and render the Cloudflare Turnstile widget when `turnstileSiteKey` is configured.
 */
function ensureTurnstileWidget() {
  const box = select('#turnstileBox');
  if (!turnstileSiteKey) {
    box.style.display = 'none';
    return;
  }
  box.style.display = 'flex';
  const renderWidget = () => {
    if (!window.turnstile) return;
    if (turnstileWidgetId !== null) {
      turnstileToken = '';
      window.turnstile.reset(turnstileWidgetId);
      return;
    }
    turnstileWidgetId = window.turnstile.render(box, {
      sitekey: turnstileSiteKey,
      callback: token => { turnstileToken = token; },
      'expired-callback': () => { turnstileToken = ''; },
    });
  };
  if (window.turnstile) {
    renderWidget();
  } else if (!select('#turnstileScript')) {
    const script = document.createElement('script');
    script.id = 'turnstileScript';
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    script.async = true;
    script.onload = renderWidget;
    document.head.append(script);
  }
}

/**
 * Open the Admin login modal, configuring Passkey buttons, Cloudflare Turnstile and optional debug password input.
 */
function openLogin() {
  select('#loginErr').textContent = '';
  select('#pw').value = '';
  select('#passkeyLoginBtn').style.display = hasRegisteredPasskey ? 'inline-flex' : 'none';
  // Allow first-time passkey enrollment directly from the modal when no passkey exists and no debug password is set.
  select('#passkeySetupBtn').style.display = (!hasRegisteredPasskey && !debugPasswordEnabled) ? 'inline-flex' : 'none';
  // DEBUG PASSWORD WORKAROUND: only shown while GLOAN_PASSWORD is set on the server.
  select('#debugPwSection').style.display = debugPasswordEnabled ? 'flex' : 'none';
  ensureTurnstileWidget();
  select('#loginModal').classList.add('open');
  if (debugPasswordEnabled && !hasRegisteredPasskey) {
    setTimeout(() => select('#pw').focus(), 30);
  }
}

/**
 * Close the Admin login modal and clear `#pw` so Chromium/Brave password managers never mistake subsequent
 * form submissions (like Add Tags) for a login submission.
 */
function closeLogin() {
  select('#pw').value = '';
  select('#loginModal').classList.remove('open');
}

/**
 * Register a new WebAuthn Passkey (ES256 / P-256) using the browser's authenticator and save it on the server.
 */
async function registerPasskey(triggerButton) {
  select('#loginErr').textContent = '';
  await withBusy(triggerButton, 'Waiting for Passkey…', async () => {
    try {
      if (!window.PublicKeyCredential) {
        throw new Error('Passkeys (WebAuthn) are not supported in this browser');
      }
      const options = await API.passkeyRegisterOptions();
      const credential = await navigator.credentials.create({
        publicKey: {
          challenge: base64UrlToBytes(options.challenge),
          rp: options.rp,
          user: {
            id: new TextEncoder().encode(options.user.id),
            name: options.user.name,
            displayName: options.user.displayName,
          },
          pubKeyCredParams: [{ alg: -7, type: 'public-key' }], // ES256 (ECDSA P-256)
          authenticatorSelection: { userVerification: 'preferred' },
          timeout: 60000,
        },
      });
      const response = credential.response;
      if (typeof response.getPublicKey !== 'function' || typeof response.getPublicKeyAlgorithm !== 'function') {
        throw new Error('Browser does not support WebAuthn getPublicKey()');
      }
      const reply = await API.passkeyRegister({
        credentialId: bufferToBase64Url(credential.rawId),
        publicKey: bufferToBase64Url(response.getPublicKey()),
        clientDataJSON: bufferToBase64Url(response.clientDataJSON),
        authenticatorData: bufferToBase64Url(response.getAuthenticatorData()),
      });
      hasRegisteredPasskey = true;
      if (reply.token) {
        API.setToken(reply.token);
        setOwner(true);
      }
      closeLogin();
      toast('Owner Passkey registered');
    } catch (error) {
      select('#loginErr').textContent = error.message;
      toast(error.message);
    }
  });
}

/**
 * Sign in as owner using the registered WebAuthn Passkey and Cloudflare Turnstile token (if enabled).
 */
async function doPasskeyLogin() {
  select('#loginErr').textContent = '';
  if (turnstileSiteKey && !turnstileToken) {
    select('#loginErr').textContent = 'Please complete the CAPTCHA verification first.';
    return;
  }
  await withBusy(select('#passkeyLoginBtn'), 'Verifying Passkey…', async () => {
    try {
      const options = await API.passkeyLoginOptions();
      const assertion = await navigator.credentials.get({
        publicKey: {
          challenge: base64UrlToBytes(options.challenge),
          rpId: options.rpId,
          allowCredentials: options.allowCredentials.map(item => ({
            id: base64UrlToBytes(item.id),
            type: item.type,
          })),
          userVerification: 'preferred',
          timeout: 60000,
        },
      });
      const response = assertion.response;
      const reply = await API.passkeyLogin({
        credentialId: bufferToBase64Url(assertion.rawId),
        clientDataJSON: bufferToBase64Url(response.clientDataJSON),
        authenticatorData: bufferToBase64Url(response.authenticatorData),
        signature: bufferToBase64Url(response.signature),
        captchaToken: turnstileToken,
      });
      API.setToken(reply.token);
      closeLogin();
      setOwner(true);
      toast('Signed in with Passkey');
    } catch (error) {
      select('#loginErr').textContent = error.message;
      if (window.turnstile && turnstileWidgetId !== null) {
        turnstileToken = '';
        window.turnstile.reset(turnstileWidgetId);
      }
    }
  });
}

// ============================================================================
// DEBUG PASSWORD WORKAROUND (REMOVE AFTER CLOUD PASSKEY VERIFICATION)
// ============================================================================
async function doDebugPasswordLogin() {
  select('#loginErr').textContent = '';
  if (turnstileSiteKey && !turnstileToken) {
    select('#loginErr').textContent = 'Please complete the CAPTCHA verification first.';
    return;
  }
  await withBusy(select('#loginGo'), 'Logging in…', async () => {
    try {
      const reply = await API.login(select('#pw').value, turnstileToken);
      API.setToken(reply.token);
      closeLogin();
      setOwner(true);
      toast(hasRegisteredPasskey ? 'Logged in (debug password)' : 'Logged in — click "Register Passkey" in the footer');
    } catch (error) {
      select('#loginErr').textContent = error.message;
      if (window.turnstile && turnstileWidgetId !== null) {
        turnstileToken = '';
        window.turnstile.reset(turnstileWidgetId);
      }
    }
  });
}

/**
 * Bootstrap the gallery: check session status, fetch the initial page and render.
 * Shows an immediate loading indicator in `#galleryHost` while the initial API calls are in flight.
 */
async function load() {
  const host = select('#galleryHost');
  host.innerHTML = '<div class="empty"><span class="spinner" aria-hidden="true"></span><p style="margin-top:10px">Loading gallery…</p></div>';
  try {
    const [session] = await Promise.all([API.session(), fetchPage()]);
    state.ready = true;
    hasRegisteredPasskey = !!session.hasPasskey;
    turnstileSiteKey = session.turnstileSiteKey || '';
    debugPasswordEnabled = !!session.debugPasswordEnabled;
    setOwner(session.owner, false);
  } catch (error) {
    host.innerHTML = '<div class="empty"><h2>Can’t reach the gallery server</h2><p>Start it with <code>python3 server.py</code> and open the address it prints.</p></div>';
    return;
  }
  render();
}

// Theme toggle
select('#themeBtn').onclick = () => {
  const nextTheme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = nextTheme;
  localStorage.setItem('gloan-theme', nextTheme);
};

// Admin login & logout controls
select('#authBtn').onclick = () => {
  if (isOwner()) {
    API.setToken('');
    setOwner(false);
    toast('Logged out');
  } else {
    openLogin();
  }
};
select('#passkeyLoginBtn').onclick = doPasskeyLogin;
select('#passkeySetupBtn').onclick = () => registerPasskey(select('#passkeySetupBtn'));
select('#passkeyRegBtn').onclick = () => registerPasskey(select('#passkeyRegBtn'));
select('#loginGo').onclick = doDebugPasswordLogin;
select('#loginClose').onclick = closeLogin;
select('#pw').onkeydown = event => {
  if (event.key === 'Enter') doDebugPasswordLogin();
  else if (event.key === 'Escape') closeLogin();
};
select('#loginModal').addEventListener('mousedown', event => {
  if (event.target.id === 'loginModal') closeLogin();
});

// Footer owner tools
select('#thumbBtn').onclick = runThumbnailJob;
select('#scanBtn').onclick = rescanFolder;

// Global keyboard shortcuts for modals and the lightbox
document.addEventListener('keydown', event => {
  if (event.target.id === 'tagInput') {
    if (event.key === 'Enter') submitTags();
    else if (event.key === 'Escape') closeTagModal();
    return;
  }
  if (event.target.tagName === 'INPUT' || event.target.tagName === 'TEXTAREA') {
    if (event.key === 'Enter' && event.target.tagName === 'INPUT' && event.target.closest('.editrow')) {
      saveEdit();
    }
    if (event.key === 'Escape' && select('#upModal').classList.contains('open')) {
      closeUpload();
    }
    return;
  }
  if (select('#upModal').classList.contains('open')) {
    if (event.key === 'Escape') closeUpload();
    return;
  }
  if (!select('#lb').classList.contains('open')) return;
  if (event.key === 'Escape') closeLightbox();
  else if (event.key === 'ArrowRight') goSlide(1);
  else if (event.key === 'ArrowLeft') goSlide(-1);
  else if (event.key === ' ') {
    event.preventDefault();
    if (isSlideshowPlaying()) stopSlideshow();
    else playSlideshow();
  } else if (event.key === 's' && currentLightboxPhoto()?.story) {
    select('#storyBtn').click();
  } else if (event.key === 'f') {
    select('#fsBtn').click();
  } else if (event.key === 'e') {
    select('#editBtn').click();
  }
});

initLightboxEvents();
initUploadEvents();
initSearchEvents();

document.documentElement.dataset.theme = localStorage.getItem('gloan-theme') || 'dark';
load();
