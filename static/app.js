/**
 * Application entry point: initializes modules, owner login modal, theme toggle and keyboard shortcuts.
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

/**
 * Open the owner password modal and focus the password input.
 */
function openLogin() {
  select('#loginErr').textContent = '';
  select('#pw').value = '';
  select('#loginModal').classList.add('open');
  setTimeout(() => select('#pw').focus(), 30);
}

/**
 * Close the owner password modal.
 */
function closeLogin() {
  select('#loginModal').classList.remove('open');
}

/**
 * Exchange the password for a bearer token via POST /api/login.
 */
async function doLogin() {
  select('#loginErr').textContent = '';
  await withBusy(select('#loginGo'), 'Logging in…', async () => {
    try {
      const reply = await API.login(select('#pw').value);
      API.setToken(reply.token);
      closeLogin();
      setOwner(true);
      toast('Logged in');
    } catch (error) {
      select('#loginErr').textContent = error.message;
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
    // Owner login button is hidden completely when --owner-ip excludes the visitor's address.
    select('#authBtn').style.display = (session.owner || session.canLogin) ? '' : 'none';
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

// Owner login & logout controls
select('#authBtn').onclick = () => {
  if (isOwner()) {
    API.setToken('');
    setOwner(false);
    toast('Logged out');
  } else {
    openLogin();
  }
};
select('#loginGo').onclick = doLogin;
select('#loginClose').onclick = closeLogin;
select('#pw').onkeydown = event => {
  if (event.key === 'Enter') doLogin();
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
