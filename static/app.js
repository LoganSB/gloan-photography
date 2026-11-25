/**
 * Application entry point: initializes modules, Admin login modal, theme toggle and keyboard shortcuts.
 */

import {
  API,
  createTurnstileController,
  isOwner,
  registerOwnerRenderCallback,
  select,
  setOverlayOpen,
  setOwner,
  setTurnstileSiteKey,
  toast,
  withBusy,
} from './api.js';
import { clearAiTags, fetchPage, registerGalleryActions, render, rescanFolder, runThumbnailJob, state } from './gallery.js';
import {
  closeLightbox,
  closePendingModal,
  closeShareModal,
  closeTagModal,
  currentLightboxPhoto,
  goSlide,
  initLightboxEvents,
  isSlideshowPlaying,
  openLightbox,
  openPendingModal,
  openSharedPhoto,
  openTagModal,
  playSlideshow,
  refreshPendingTagsBadge,
  saveEdit,
  stopSlideshow,
  submitTags,
  updatePendingTagsBadge,
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
const loginTurnstile = createTurnstileController('#turnstileBox');

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
 * True when running on a phone/mobile browser where the passkey already lives on this device
 * (so we should invoke the phone's built-in passkey app directly instead of showing a QR code).
 */
function isPhoneDevice() {
  if (typeof navigator === 'undefined') return false;
  const mobileUa = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent || '');
  const coarseTouch = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    && window.matchMedia('(pointer: coarse)').matches && window.innerWidth <= 900;
  return mobileUa || coarseTouch;
}

/**
 * Verify that the browser exposes WebAuthn (`navigator.credentials` + `PublicKeyCredential`).
 * Browsers hide `navigator.credentials` (making it undefined) on non-HTTPS origins other than localhost,
 * and some in-app webviews omit WebAuthn entirely.
 */
function getWebAuthnUnavailableReason() {
  if (typeof window !== 'undefined' && window.isSecureContext === false) {
    return 'Passkeys require HTTPS (or localhost). Open this site over https:// to sign in.';
  }
  if (typeof navigator === 'undefined' || !navigator.credentials || typeof window === 'undefined' || !window.PublicKeyCredential) {
    return 'Passkeys are not available in this browser. Open this site over HTTPS in Chrome or Safari.';
  }
  return '';
}

/**
 * Open the Admin login / Passkey registration modal.
 * - On desktop: defaults to the QR code (hybrid phone passkey) method.
 * - On a phone: defaults to this device's built-in passkey app (Google Password Manager / iCloud Keychain) directly.
 * Both include a secondary button to open all other authenticator options.
 */
function openLogin(mode = 'auto') {
  const isRegisterMode = mode === 'register' || !hasRegisteredPasskey;
  const onPhone = isPhoneDevice();
  select('#loginErr').textContent = getWebAuthnUnavailableReason();
  select('#loginModalTitle').textContent = isRegisterMode ? 'Register Passkey' : 'Admin login';
  select('#passkeyLoginBtn').textContent = onPhone ? 'Sign in with this device' : 'Sign in with QR code';
  select('#passkeySetupBtn').textContent = onPhone ? 'Register Passkey on this device' : 'Register Passkey with QR code';
  select('#passkeyLoginBtn').style.display = isRegisterMode ? 'none' : 'inline-flex';
  select('#passkeyLoginAltBtn').style.display = isRegisterMode ? 'none' : 'inline-flex';
  select('#passkeySetupBtn').style.display = isRegisterMode ? 'inline-flex' : 'none';
  select('#passkeySetupAltBtn').style.display = isRegisterMode ? 'inline-flex' : 'none';
  if (isRegisterMode && isOwner()) {
    loginTurnstile.hide();
  } else {
    loginTurnstile.ensure();
  }
  setOverlayOpen('#loginModal', true);
}

/**
 * Close the Admin login modal.
 */
function closeLogin() {
  setOverlayOpen('#loginModal', false);
}

/**
 * Register a new WebAuthn Passkey (ES256 / P-256) and save it on the server.
 * `mode`: `'default'` (direct device passkey on phones, QR code on desktop) or `'any'` (all methods).
 */
async function registerPasskey(triggerButton, { mode = 'default' } = {}) {
  select('#loginErr').textContent = '';
  if (!isOwner() && loginTurnstile.isRequired() && !loginTurnstile.getToken()) {
    select('#loginErr').textContent = 'Please complete the CAPTCHA verification first.';
    return;
  }
  const onPhone = isPhoneDevice();
  const preferQr = mode === 'default' && !onPhone;
  const preferDevice = mode === 'default' && onPhone;
  const busyText = preferQr ? 'Opening QR code…' : 'Waiting for Passkey…';
  await withBusy(triggerButton, busyText, async () => {
    try {
      const unavailableReason = getWebAuthnUnavailableReason();
      if (unavailableReason) {
        throw new Error(unavailableReason);
      }
      const options = await API.passkeyRegisterOptions();
      const excludeCredentials = (options.excludeCredentials || []).map(item => ({
        id: base64UrlToBytes(item.id),
        type: item.type,
      }));
      const authenticatorSelection = preferQr
        ? { authenticatorAttachment: 'cross-platform', userVerification: 'preferred' }
        : (preferDevice
          ? { authenticatorAttachment: 'platform', userVerification: 'preferred' }
          : { userVerification: 'preferred' });
      const publicKeyOptions = {
        challenge: base64UrlToBytes(options.challenge),
        rp: options.rp,
        user: {
          id: new TextEncoder().encode(options.user.id),
          name: options.user.name,
          displayName: options.user.displayName,
        },
        pubKeyCredParams: [{ alg: -7, type: 'public-key' }], // ES256 (ECDSA P-256)
        excludeCredentials,
        authenticatorSelection,
        timeout: 60000,
      };
      if (preferQr) {
        publicKeyOptions.hints = ['hybrid'];
      } else if (preferDevice) {
        publicKeyOptions.hints = ['client-device'];
      }
      const credential = await navigator.credentials.create({ publicKey: publicKeyOptions });
      const response = credential.response;
      if (typeof response.getPublicKey !== 'function' || typeof response.getPublicKeyAlgorithm !== 'function') {
        throw new Error('Browser does not support WebAuthn getPublicKey()');
      }
      const reply = await API.passkeyRegister({
        credentialId: bufferToBase64Url(credential.rawId),
        publicKey: bufferToBase64Url(response.getPublicKey()),
        clientDataJSON: bufferToBase64Url(response.clientDataJSON),
        authenticatorData: bufferToBase64Url(response.getAuthenticatorData()),
        captchaToken: loginTurnstile.getToken(),
      });
      hasRegisteredPasskey = true;
      if (reply.csrfToken) {
        API.setCsrfToken(reply.csrfToken);
        setOwner(true);
        refreshPendingTagsBadge();
      }
      closeLogin();
      toast(`Owner Passkey registered (${reply.passkeyCount || 1} device${(reply.passkeyCount || 1) > 1 ? 's' : ''} enrolled)`);
    } catch (error) {
      select('#loginErr').textContent = error.message;
      toast(error.message);
      loginTurnstile.reset();
    }
  });
}

/**
 * Sign in as owner using a registered WebAuthn Passkey and Cloudflare Turnstile token (if enabled).
 * `mode`: `'default'` (opens the phone's built-in passkey app directly on mobile, or QR code on desktop)
 * or `'any'` (omits hints/transports so all authenticators are presented).
 */
async function doPasskeyLogin(triggerButton, { mode = 'default' } = {}) {
  select('#loginErr').textContent = '';
  if (loginTurnstile.isRequired() && !loginTurnstile.getToken()) {
    select('#loginErr').textContent = 'Please complete the CAPTCHA verification first.';
    return;
  }
  const onPhone = isPhoneDevice();
  const preferQr = mode === 'default' && !onPhone;
  const preferDevice = mode === 'default' && onPhone;
  const busyText = preferQr ? 'Opening QR code…' : 'Verifying Passkey…';
  await withBusy(triggerButton, busyText, async () => {
    try {
      const unavailableReason = getWebAuthnUnavailableReason();
      if (unavailableReason) {
        throw new Error(unavailableReason);
      }
      const options = await API.passkeyLoginOptions();
      const publicKeyOptions = {
        challenge: base64UrlToBytes(options.challenge),
        rpId: options.rpId,
        allowCredentials: options.allowCredentials.map(item => (
          preferQr
            ? { id: base64UrlToBytes(item.id), type: item.type, transports: ['hybrid'] }
            : { id: base64UrlToBytes(item.id), type: item.type }
        )),
        userVerification: 'preferred',
        timeout: 60000,
      };
      if (preferQr) {
        publicKeyOptions.hints = ['hybrid'];
      } else if (preferDevice) {
        publicKeyOptions.hints = ['client-device'];
      }
      const assertion = await navigator.credentials.get({ publicKey: publicKeyOptions });
      const response = assertion.response;
      const reply = await API.passkeyLogin({
        credentialId: bufferToBase64Url(assertion.rawId),
        clientDataJSON: bufferToBase64Url(response.clientDataJSON),
        authenticatorData: bufferToBase64Url(response.authenticatorData),
        signature: bufferToBase64Url(response.signature),
        captchaToken: loginTurnstile.getToken(),
      });
      API.setCsrfToken(reply.csrfToken || '');
      closeLogin();
      setOwner(true);
      refreshPendingTagsBadge();
      toast('Signed in with Passkey');
    } catch (error) {
      select('#loginErr').textContent = error.message;
      loginTurnstile.reset();
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
    setTurnstileSiteKey(session.turnstileSiteKey || '');
    API.setCsrfToken(session.csrfToken || '');
    setOwner(session.owner, false);
    updatePendingTagsBadge(session.pendingTagCount || 0);
  } catch (error) {
    host.innerHTML = '<div class="empty"><h2>Can’t reach the gallery server</h2><p>Start it with <code>python3 server.py</code> and open the address it prints.</p></div>';
    return;
  }
  render();

  // If the visitor arrived via a shared photo URL (`/?photo=<id>`), open that photo directly in the lightbox.
  const sharedPhotoId = new URLSearchParams(window.location.search).get('photo');
  if (sharedPhotoId && /^p[0-9a-f]{16}$/.test(sharedPhotoId)) {
    const existingIndex = state.view.findIndex(item => item.id === sharedPhotoId);
    if (existingIndex >= 0) {
      openLightbox(existingIndex);
    } else {
      try {
        const sharedPhoto = await API.getPhoto(sharedPhotoId);
        openSharedPhoto(sharedPhoto);
      } catch (error) {
        toast('Shared photo not found');
      }
    }
  }
}

// Theme toggle
select('#themeBtn').onclick = () => {
  const nextTheme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = nextTheme;
  localStorage.setItem('gloan-theme', nextTheme);
};

// Admin login & logout controls (optimistic 0ms logout; cookie cleared asynchronously in background)
select('#authBtn').onclick = () => {
  if (isOwner()) {
    const previousCsrf = API.csrfToken;
    API.setCsrfToken('');
    setOwner(false);
    updatePendingTagsBadge(0);
    toast('Logged out');
    API.logout(previousCsrf).catch(() => {});
  } else {
    openLogin();
  }
};
select('#passkeyLoginBtn').onclick = () => doPasskeyLogin(select('#passkeyLoginBtn'), { mode: 'default' });
select('#passkeyLoginAltBtn').onclick = () => doPasskeyLogin(select('#passkeyLoginAltBtn'), { mode: 'any' });
select('#passkeySetupBtn').onclick = () => registerPasskey(select('#passkeySetupBtn'), { mode: 'default' });
select('#passkeySetupAltBtn').onclick = () => registerPasskey(select('#passkeySetupAltBtn'), { mode: 'any' });
select('#passkeyRegBtn').onclick = () => openLogin('register');
select('#loginClose').onclick = closeLogin;
select('#loginModal').addEventListener('mousedown', event => {
  if (event.target.id === 'loginModal') closeLogin();
});

// Footer & header owner tools
select('#pendingTagsBtn').onclick = openPendingModal;
select('#pendingTagsLink').onclick = openPendingModal;
select('#thumbBtn').onclick = runThumbnailJob;
select('#scanBtn').onclick = rescanFolder;
select('#clearAiTagsBtn').onclick = clearAiTags;

// Global keyboard shortcuts for modals and the lightbox
document.addEventListener('keydown', event => {
  if (select('#pendingModal').classList.contains('open')) {
    if (event.key === 'Escape') closePendingModal();
    return;
  }
  if (select('#shareModal').classList.contains('open')) {
    if (event.key === 'Escape') closeShareModal();
    return;
  }
  if (event.target.id === 'tagInput') {
    // Enter and Escape on #tagInput are handled by attachTagAutocomplete in lightbox.js.
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
