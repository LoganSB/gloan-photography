/**
 * Full-screen lightbox, slideshow playback, story drawer, metadata editor and owner Add-Tags modal.
 */

import {
  API,
  createElement,
  createTurnstileController,
  escapeHtml,
  isOwner,
  parseTags,
  select,
  setOverlayOpen,
  toast,
  withBusy,
} from './api.js';
import { pauseGalleryImageLoads, refresh, resumeGalleryImageLoads, state, suggestionChips } from './gallery.js';
import { attachTagAutocomplete } from './search.js';

const SLIDE_INTERVAL_MS = 4500;
const tagTurnstile = createTurnstileController('#tagTurnstileBox');

let currentIndex = 0;
let isPlaying = false;
let slideTimerId = null;
let storyOpen = true;
let taggingPhoto = null;
let stagedManualTags = [];
let stagedAiTags = [];
let tagInputAutocomplete = null;
let slideLoadRunId = 0; // bumped on each slide change so an earlier full-res load cannot overwrite a newer slide

/**
 * Return the currently displayed photo in the lightbox, if any.
 */
export function currentLightboxPhoto() {
  return state.view[currentIndex];
}

/**
 * Return true when the slideshow is actively advancing.
 */
export function isSlideshowPlaying() {
  return isPlaying;
}

/**
 * Open the lightbox focused on the photo at `index` in the current page view.
 * Immediately pauses any in-flight gallery tile downloads and schedules image loading after the browser paints
 * the open modal + spinner so the popup appears on screen at 0ms.
 */
export function openLightbox(index) {
  pauseGalleryImageLoads();
  currentIndex = index;
  const lightboxEl = select('#lb');
  const stageBox = select('#sbox');
  const stageImage = select('#lbImg');
  stageBox.classList.remove('zoom');
  stageBox.classList.add('loading');
  stageImage.removeAttribute('src');
  setOverlayOpen(lightboxEl, true);
  buildStrip();
  showSlide(true);
}

/**
 * Close the lightbox, stop any running slideshow, resume any unfinished gallery tile loads and exit fullscreen.
 */
export function closeLightbox() {
  stopSlideshow();
  closeShareModal();
  slideLoadRunId++;
  select('#lb').classList.remove('show-edit', 'chromeless');
  setOverlayOpen('#lb', false);
  select('#sbox').classList.remove('zoom', 'loading');
  select('#lbImg').removeAttribute('src');
  if (document.fullscreenElement) document.exitFullscreen();
  // Remove ?photo=<id> from the browser URL when closing the lightbox so refreshing returns to the gallery.
  const currentUrl = new URL(window.location.href);
  if (currentUrl.searchParams.has('photo')) {
    currentUrl.searchParams.delete('photo');
    window.history.replaceState({}, '', currentUrl.pathname + currentUrl.search + currentUrl.hash);
  }
  resumeGalleryImageLoads();
}

/**
 * Open a deep-linked shared photo (`/?photo=<id>`), prepending it to `state.view` if it is on another page.
 */
export function openSharedPhoto(photo) {
  if (!photo || !photo.id) return;
  let idx = state.view.findIndex(item => item.id === photo.id);
  if (idx < 0) {
    state.view = [photo, ...state.view];
    idx = 0;
  }
  openLightbox(idx);
}

/**
 * Build the canonical shareable URL for `photo`.
 */
export function photoShareUrl(photo) {
  const url = new URL('/', window.location.origin);
  url.searchParams.set('photo', photo.id);
  return url.toString();
}

/**
 * Open the share dialog for the currently viewed photo.
 */
export function openShareModal() {
  const photo = currentLightboxPhoto();
  if (!photo) return;
  const shareUrl = photoShareUrl(photo);
  select('#shareTitle').textContent = 'Share — ' + (photo.title || 'Untitled');
  const input = select('#shareUrlInput');
  input.value = shareUrl;
  setOverlayOpen('#shareModal', true);
  setTimeout(() => input.select(), 20);
}

/**
 * Close the share dialog.
 */
export function closeShareModal() {
  setOverlayOpen('#shareModal', false);
}

/**
 * Copy the active photo's share URL to the clipboard (using Clipboard API with textarea fallback).
 */
export async function copyShareLink() {
  const photo = currentLightboxPhoto();
  if (!photo) return;
  const url = photoShareUrl(photo);
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(url);
    } else {
      const input = select('#shareUrlInput');
      input.value = url;
      input.select();
      document.execCommand('copy');
    }
    toast('Link copied to clipboard');
  } catch (error) {
    toast('Copy the link from the box above');
  }
}

/**
 * Open Facebook's standard web share dialog template for the active photo URL.
 */
function shareToFacebook() {
  const photo = currentLightboxPhoto();
  if (!photo) return;
  const fbUrl = 'https://www.facebook.com/sharer/sharer.php?u=' + encodeURIComponent(photoShareUrl(photo));
  window.open(fbUrl, '_blank', 'noopener,noreferrer,width=640,height=520');
}

/**
 * Share via the native Web Share API (Instagram/Messages/AirDrop on mobile) or fall back to copying the link.
 */
async function shareNativeOrInstagram() {
  const photo = currentLightboxPhoto();
  if (!photo) return;
  const url = photoShareUrl(photo);
  if (typeof navigator.share === 'function') {
    try {
      await navigator.share({
        title: photo.title || 'Gloan Photography',
        text: photo.title || 'Check out this photo on Gloan',
        url,
      });
      closeShareModal();
      return;
    } catch (error) {
      if (error.name === 'AbortError') return;
    }
  }
  await copyShareLink();
  toast('Link copied — paste into Instagram or any app to share');
}

/**
 * Render the current photo (`state.view[currentIndex]`) inside the lightbox stage, caption and story panel.
 * Updates the UI chrome and shows the spinning "Loading image…" indicator immediately, then defers starting
 * the full-resolution image network request until after the next paint frame so opening the popup never blocks.
 */
export function showSlide(instant = false) {
  const photo = state.view[currentIndex];
  if (!photo) {
    closeLightbox();
    return;
  }
  const runId = ++slideLoadRunId;
  const lightboxEl = select('#lb');
  const stageBox = select('#sbox');
  const stageImage = select('#lbImg');

  // Update metadata, controls, story drawer and active strip thumbnail immediately on click.
  stageBox.classList.add('loading');
  stageImage.classList.remove('out');
  stageImage.removeAttribute('src');
  select('#lbTitle').textContent = photo.title || 'Untitled';
  select('#lbCnt').textContent = `${currentIndex + 1} / ${state.view.length}`;
  const manualTags = photo.tags || [];
  const manualSet = new Set(manualTags);
  const aiTags = (photo.aiTags || []).filter(tag => !manualSet.has(tag));
  select('#lbTags').innerHTML = [
    ...manualTags.map(tag => `<span data-t="${escapeHtml(tag)}">${escapeHtml(tag)}</span>`),
    ...aiTags.map(tag => `<span class="ai-tag" data-t="${escapeHtml(tag)}" title="AI tag">${escapeHtml(tag)}</span>`),
  ].join('');
  select('#lbTags').querySelectorAll('span').forEach(span => {
    span.onclick = () => {
      closeLightbox();
      const chosenTag = span.dataset.t;
      const searchInput = select('#tagSearch');
      if (searchInput) searchInput.value = chosenTag + ', ';
      state.activeTags.clear();
      state.searchQuery = chosenTag;
      state.page = 0;
      refresh();
      scrollTo({ top: 0, behavior: 'smooth' });
    };
  });
  select('#eTitle').value = photo.title || '';
  select('#eTags').value = photo.tags.join(', ');
  select('#eStory').value = photo.story || '';
  select('#stTitle').textContent = photo.title || 'Untitled';
  select('#stBody').textContent = photo.story || '';
  select('#storyBtn').style.display = photo.story ? '' : 'none';
  lightboxEl.classList.toggle('story-open', !!photo.story && storyOpen);
  select('#strip').querySelectorAll('img').forEach((thumbImg, idx) => {
    thumbImg.classList.toggle('on', idx === currentIndex);
    if (idx === currentIndex) {
      thumbImg.scrollIntoView({ inline: 'center', block: 'nearest', behavior: instant ? 'auto' : 'smooth' });
    }
  });

  // Start the full-resolution download on the next frame so the open modal and "Loading image…" spinner paint first.
  const startFullDownload = () => {
    if (runId !== slideLoadRunId) return;
    const fullLoader = new Image();
    fullLoader.onload = fullLoader.onerror = () => {
      if (runId !== slideLoadRunId) return; // user navigated to another slide before this one finished
      stageImage.src = photo.url;
      stageBox.classList.remove('loading');
      // Only preload neighbours AFTER the current full-size image has finished loading so we never compete for sockets.
      [1, -1].forEach(delta => {
        const neighbour = state.view[(currentIndex + delta + state.view.length) % state.view.length];
        if (neighbour) new Image().src = neighbour.url;
      });
    };
    fullLoader.src = photo.url;
  };
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(startFullDownload);
  } else {
    setTimeout(startFullDownload, 0);
  }

  if (isPlaying) restartProgressBar();
}

/**
 * Advance the lightbox by `delta` slides (+1 for next, -1 for previous), wrapping around the page.
 */
export function goSlide(delta) {
  if (!state.view.length) return;
  currentIndex = (currentIndex + delta + state.view.length) % state.view.length;
  select('#sbox').classList.remove('zoom');
  showSlide();
}

/**
 * Rebuild the bottom thumbnail strip for the photos on the current page.
 * Uses `photo.thumbUrl` when available, or falls back to `photo.url` with `loading="lazy"` so strip tiles still
 * render even before `tools/make_thumbnails.py` has been run on the bucket.
 */
export function buildStrip() {
  const stripEl = select('#strip');
  stripEl.innerHTML = '';
  state.view.forEach((photo, index) => {
    const thumbImg = createElement('img');
    thumbImg.loading = 'lazy';
    thumbImg.decoding = 'async';
    thumbImg.src = photo.thumbUrl || photo.url;
    thumbImg.alt = photo.title || 'Untitled';
    thumbImg.onclick = () => {
      currentIndex = index;
      showSlide();
    };
    stripEl.append(thumbImg);
  });
}

/**
 * Reset and animate the slideshow progress bar for SLIDE_INTERVAL_MS.
 */
function restartProgressBar() {
  const bar = select('#pbar');
  bar.style.transition = 'none';
  bar.style.width = '0';
  void bar.offsetWidth;
  bar.style.transition = `width ${SLIDE_INTERVAL_MS}ms linear`;
  bar.style.width = '100%';
  clearTimeout(slideTimerId);
  slideTimerId = setTimeout(() => goSlide(1), SLIDE_INTERVAL_MS);
}

/**
 * Start automatic slideshow playback inside the open lightbox.
 */
export function playSlideshow() {
  isPlaying = true;
  select('#playBtn').textContent = '❚❚';
  select('#lb').classList.add('chromeless');
  restartProgressBar();
}

/**
 * Pause slideshow playback and restore lightbox controls.
 */
export function stopSlideshow() {
  isPlaying = false;
  clearTimeout(slideTimerId);
  select('#playBtn').textContent = '▶';
  select('#lb').classList.remove('chromeless');
  const bar = select('#pbar');
  bar.style.transition = 'none';
  bar.style.width = '0';
}

/**
 * Open the lightbox at the first photo on the current page and start the slideshow.
 */
export function startSlideshow() {
  if (!state.view.length) {
    toast('Nothing to play yet');
    return;
  }
  openLightbox(0);
  playSlideshow();
}

/**
 * Save title, tags and story edits from the lightbox inline editor.
 */
export async function saveEdit() {
  const photo = state.view[currentIndex];
  if (!photo) return;
  await withBusy(select('#eSave'), 'Saving…', async () => {
    try {
      const updated = await API.update(
        photo.id,
        select('#eTitle').value.trim(),
        parseTags(select('#eTags').value),
        select('#eStory').value
      );
      Object.assign(photo, updated);
    } catch (error) {
      toast(error.message);
      return;
    }
    await afterPhotoEdit(photo.id, 'Saved');
  });
}

/**
 * Reload the current page after a photo's metadata changes and keep the lightbox focused on that photo.
 */
export async function afterPhotoEdit(photoId, message) {
  await refresh();
  const lightboxEl = select('#lb');
  if (!lightboxEl.classList.contains('open')) {
    toast(message);
    return;
  }
  const newIndex = state.view.findIndex(item => item.id === photoId);
  if (newIndex < 0) {
    closeLightbox();
    toast(message + ' — hidden by current filter');
    return;
  }
  currentIndex = newIndex;
  buildStrip();
  showSlide(true);
  toast(message);
}

/**
 * Update the header & footer pending-tag badge counts for the logged-in admin.
 */
export function updatePendingTagsBadge(count) {
  const safeCount = Math.max(0, Number(count) || 0);
  const headerBtn = select('#pendingTagsBtn');
  if (headerBtn) {
    headerBtn.textContent = safeCount > 0 ? `Pending tags (${safeCount})` : 'Pending tags';
    headerBtn.classList.toggle('has-pending', safeCount > 0);
  }
  const footerLink = select('#pendingTagsLink');
  if (footerLink) {
    footerLink.textContent = safeCount > 0 ? `Pending tag suggestions (${safeCount})` : 'Pending tag suggestions';
  }
}

/**
 * Fetch the latest pending visitor tag count for the logged-in admin and update the UI badge.
 */
export async function refreshPendingTagsBadge() {
  if (!isOwner()) {
    updatePendingTagsBadge(0);
    return;
  }
  try {
    const reply = await API.listPendingTags();
    updatePendingTagsBadge(reply.total || 0);
  } catch (error) {
    // Badge refresh is best-effort; stay quiet on transient errors.
  }
}

/**
 * Render the current tags list inside `#tagModal` (removable pills for admins, read-only pills for visitors).
 */
function renderCurrentTagChips() {
  const container = select('#tagCurrentChips');
  if (!container) return;
  container.innerHTML = '';
  const owner = isOwner();
  const manualSet = new Set(stagedManualTags);
  const items = [
    ...stagedManualTags.map(tag => ({ tag, isAi: false })),
    ...stagedAiTags.filter(tag => !manualSet.has(tag)).map(tag => ({ tag, isAi: true })),
  ];
  if (!items.length) {
    container.append(createElement('span', 'hint', 'No tags yet'));
    return;
  }
  items.forEach(({ tag, isAi }) => {
    const pill = createElement('span', isAi ? 'tag-pill ai-tag' : 'tag-pill');
    pill.append(document.createTextNode(tag));
    if (owner) {
      const removeBtn = createElement('button', '', '×');
      removeBtn.type = 'button';
      removeBtn.title = `Remove ${tag}`;
      removeBtn.setAttribute('aria-label', `Remove ${tag}`);
      removeBtn.onclick = () => {
        if (isAi) {
          stagedAiTags = stagedAiTags.filter(item => item !== tag);
        } else {
          stagedManualTags = stagedManualTags.filter(item => item !== tag);
        }
        renderCurrentTagChips();
        tagInputAutocomplete?.update();
      };
      pill.append(removeBtn);
    }
    container.append(pill);
  });
}

/**
 * Render pending visitor tag suggestions for `photoId` inside `#tagModal` (admin only).
 */
async function renderPhotoPendingTags(photoId) {
  const wrap = select('#tagPendingWrap');
  const list = select('#tagPendingList');
  if (!wrap || !list) return;
  if (!isOwner()) {
    wrap.style.display = 'none';
    list.innerHTML = '';
    return;
  }
  try {
    const reply = await API.listPendingTags();
    updatePendingTagsBadge(reply.total || 0);
    const entry = (reply.items || []).find(item => item.photoId === photoId);
    const pending = entry ? entry.pendingTags : [];
    if (!pending.length) {
      wrap.style.display = 'none';
      list.innerHTML = '';
      return;
    }
    wrap.style.display = 'flex';
    list.innerHTML = '';
    pending.forEach(tag => {
      const row = createElement('div', 'pending-row');
      const tagLabel = createElement('span');
      tagLabel.textContent = tag;
      row.append(tagLabel);
      const actions = createElement('div', 'actions');
      const acceptBtn = createElement('button', 'btn primary', 'Accept');
      const rejectBtn = createElement('button', 'btn danger', 'Reject');
      const moderate = async (action, btn) => {
        await withBusy(btn, '…', async () => {
          const res = await API.moderatePendingTag(photoId, tag, action);
          updatePendingTagsBadge(res.pendingTagCount);
          if (action === 'accept' && !stagedManualTags.includes(tag)) {
            stagedManualTags.push(tag);
            renderCurrentTagChips();
          }
          await afterPhotoEdit(photoId, action === 'accept' ? `Accepted tag "${tag}"` : `Rejected tag "${tag}"`);
          await renderPhotoPendingTags(photoId);
        });
      };
      acceptBtn.onclick = () => moderate('accept', acceptBtn);
      rejectBtn.onclick = () => moderate('reject', rejectBtn);
      actions.append(acceptBtn, rejectBtn);
      row.append(actions);
      list.append(row);
    });
  } catch (error) {
    wrap.style.display = 'none';
  }
}

/**
 * Open the Tag modal for `photo`:
 * - Admin: add or delete manual/AI tags and accept/reject pending visitor tags.
 * - Visitor: suggest comma-separated tags (existing or custom) for admin approval.
 */
export function openTagModal(photo) {
  if (!photo) return;
  const owner = isOwner();
  taggingPhoto = photo;
  stagedManualTags = [...(photo.tags || [])];
  stagedAiTags = [...(photo.aiTags || [])];
  select('#tagTitle').textContent = (owner ? 'Manage tags — ' : 'Suggest tags — ') + (photo.title || 'Untitled');
  select('#tagCurrentLabel').textContent = owner ? 'Current tags (click × to remove)' : 'Current tags';
  select('#tagInput').value = '';
  select('#tagInput').placeholder = owner
    ? 'Add tags (comma separated, Tab to autocomplete)'
    : 'Suggest tags (comma separated, Tab to autocomplete)';
  select('#tagHint').textContent = owner
    ? 'Type comma-separated tags to add, or click × on any tag above to delete it.'
    : 'Suggested tags are sent to the admin for approval before appearing on the site.';
  select('#tagGo').textContent = owner ? 'Save tags' : 'Submit suggestion';
  if (owner) {
    tagTurnstile.hide();
  } else {
    tagTurnstile.ensure();
  }
  renderCurrentTagChips();
  renderPhotoPendingTags(photo.id);
  suggestionChips(select('#tagSug'), select('#tagInput'));
  setOverlayOpen('#tagModal', true);
  setTimeout(() => select('#tagInput').focus(), 30);
}

/**
 * Close the Manage/Suggest Tags modal.
 */
export function closeTagModal() {
  tagInputAutocomplete?.close();
  setOverlayOpen('#tagModal', false);
  taggingPhoto = null;
}

/**
 * Save tag additions/deletions (admin) or submit tag suggestions for admin approval (visitor).
 */
export async function submitTags() {
  const photo = taggingPhoto;
  if (!photo) {
    closeTagModal();
    return;
  }
  const enteredTags = parseTags(select('#tagInput').value);
  if (!isOwner()) {
    if (!enteredTags.length) {
      toast('Enter one or more tags to suggest');
      return;
    }
    if (tagTurnstile.isRequired() && !tagTurnstile.getToken()) {
      toast('Please complete the CAPTCHA verification first.');
      return;
    }
    await withBusy(select('#tagGo'), 'Submitting…', async () => {
      try {
        const reply = await API.suggestTags(photo.id, enteredTags, tagTurnstile.getToken());
        tagTurnstile.reset();
        closeTagModal();
        if (reply.added && reply.added.length) {
          toast(`Submitted ${reply.added.length} tag suggestion${reply.added.length > 1 ? 's' : ''} for admin approval`);
        } else {
          toast('Those tags are already on this photo or awaiting approval');
        }
      } catch (error) {
        tagTurnstile.reset();
        toast(error.message);
      }
    });
    return;
  }

  const mergedManualTags = [...new Set([...stagedManualTags, ...enteredTags])];
  const origManual = (photo.tags || []).join(',');
  const origAi = (photo.aiTags || []).join(',');
  if (mergedManualTags.join(',') === origManual && stagedAiTags.join(',') === origAi) {
    closeTagModal();
    return;
  }
  await withBusy(select('#tagGo'), 'Saving…', async () => {
    try {
      await API.update(photo.id, photo.title || '', mergedManualTags, photo.story || '', stagedAiTags);
    } catch (error) {
      toast(error.message);
      return;
    }
    closeTagModal();
    await afterPhotoEdit(photo.id, 'Tags updated');
  });
}

/**
 * Open the global Admin Pending Tag Suggestions moderation modal.
 */
export async function openPendingModal() {
  if (!isOwner()) return;
  const body = select('#pendingModalBody');
  body.innerHTML = '<div class="hint">Loading pending suggestions…</div>';
  setOverlayOpen('#pendingModal', true);
  try {
    const reply = await API.listPendingTags();
    updatePendingTagsBadge(reply.total || 0);
    renderPendingQueue(reply.items || []);
  } catch (error) {
    body.innerHTML = `<div class="hint">${escapeHtml(error.message)}</div>`;
  }
}

/**
 * Render the list of photos with pending visitor tag suggestions inside `#pendingModal`.
 */
function renderPendingQueue(items) {
  const body = select('#pendingModalBody');
  body.innerHTML = '';
  if (!items.length) {
    body.append(createElement('div', 'hint', 'No pending visitor tag suggestions.'));
    return;
  }
  items.forEach(item => {
    const card = createElement('div', 'pending-card');
    const thumb = createElement('img');
    thumb.src = item.thumbUrl || item.url;
    thumb.alt = item.title || 'Photo';
    const info = createElement('div', 'tag-section');
    info.append(createElement('h3', '', escapeHtml(item.title || 'Untitled')));
    const list = createElement('div', 'pending-tag-list');
    item.pendingTags.forEach(tag => {
      const row = createElement('div', 'pending-row');
      const tagLabel = createElement('span');
      tagLabel.textContent = tag;
      row.append(tagLabel);
      const actions = createElement('div', 'actions');
      const acceptBtn = createElement('button', 'btn primary', 'Accept');
      const rejectBtn = createElement('button', 'btn danger', 'Reject');
      const moderate = async (action, btn) => {
        await withBusy(btn, '…', async () => {
          try {
            const res = await API.moderatePendingTag(item.photoId, tag, action);
            updatePendingTagsBadge(res.pendingTagCount);
            if (action === 'accept') await refresh();
            const updated = await API.listPendingTags();
            renderPendingQueue(updated.items || []);
            toast(action === 'accept' ? `Accepted "${tag}"` : `Rejected "${tag}"`);
          } catch (error) {
            toast(error.message);
          }
        });
      };
      acceptBtn.onclick = () => moderate('accept', acceptBtn);
      rejectBtn.onclick = () => moderate('reject', rejectBtn);
      actions.append(acceptBtn, rejectBtn);
      row.append(actions);
      list.append(row);
    });
    info.append(list);
    card.append(thumb, info);
    body.append(card);
  });
}

/**
 * Close the Admin Pending Tag Suggestions moderation modal.
 */
export function closePendingModal() {
  setOverlayOpen('#pendingModal', false);
}

/**
 * Delete the currently viewed photo after owner confirmation.
 */
export async function deleteCurrentPhoto() {
  const photo = state.view[currentIndex];
  if (!photo || !confirm(`Delete “${photo.title || 'Untitled'}”?`)) return;
  await withBusy(select('#delBtn'), '…', async () => {
    try {
      await API.remove(photo.id);
    } catch (error) {
      toast(error.message);
      return;
    }
    await refresh();
    if (!state.view.length) {
      closeLightbox();
      return;
    }
    currentIndex = Math.min(currentIndex, state.view.length - 1);
    buildStrip();
    showSlide(true);
  });
}

/**
 * Attach DOM event listeners for lightbox controls, touch swipe gestures and the add-tags dialog.
 */
export function initLightboxEvents() {
  const lightboxEl = select('#lb');
  const stageImage = select('#lbImg');
  select('#slideBtn').onclick = startSlideshow;
  select('#prev').onclick = () => goSlide(-1);
  select('#next').onclick = () => goSlide(1);
  select('#closeBtn').onclick = closeLightbox;
  select('#shareBtn').onclick = openShareModal;
  select('#shareClose').onclick = closeShareModal;
  select('#shareCopyBtn').onclick = copyShareLink;
  select('#shareFbBtn').onclick = shareToFacebook;
  select('#shareNativeBtn').onclick = shareNativeOrInstagram;
  select('#shareModal').addEventListener('mousedown', event => {
    if (event.target.id === 'shareModal') closeShareModal();
  });
  select('#playBtn').onclick = () => (isPlaying ? stopSlideshow() : playSlideshow());
  select('#fsBtn').onclick = () => (
    document.fullscreenElement ? document.exitFullscreen() : lightboxEl.requestFullscreen?.()
  );
  const toggleStory = () => {
    storyOpen = !storyOpen;
    lightboxEl.classList.toggle('story-open', storyOpen && !!state.view[currentIndex]?.story);
  };
  select('#storyBtn').onclick = toggleStory;
  select('#storyX').onclick = toggleStory;
  select('#editBtn').onclick = () => lightboxEl.classList.toggle('show-edit');
  select('#addTagBtn').onclick = () => openTagModal(state.view[currentIndex]);
  select('#tagGo').onclick = submitTags;
  select('#tagClose').onclick = closeTagModal;
  select('#tagModal').addEventListener('mousedown', event => {
    if (event.target.id === 'tagModal') closeTagModal();
  });
  tagInputAutocomplete = attachTagAutocomplete({
    inputEl: select('#tagInput'),
    listEl: select('#tagInputSuggest'),
    getExcludedTags: () => [...stagedManualTags, ...stagedAiTags],
    onEnter: submitTags,
    onEscape: closeTagModal,
  });
  select('#pendingClose').onclick = closePendingModal;
  select('#pendingModal').addEventListener('mousedown', event => {
    if (event.target.id === 'pendingModal') closePendingModal();
  });
  select('#eSave').onclick = saveEdit;
  select('#delBtn').onclick = deleteCurrentPhoto;
  const stageBox = select('#sbox');
  const swipePrevBar = select('#swipePrev');
  const swipeNextBar = select('#swipeNext');

  let hideChromeTimer = null;
  lightboxEl.addEventListener('mousemove', () => {
    if (isPlaying) {
      lightboxEl.classList.remove('chromeless');
      clearTimeout(hideChromeTimer);
      hideChromeTimer = setTimeout(() => isPlaying && lightboxEl.classList.add('chromeless'), 2200);
    }
  });

  // Pull-to-swipe buffer zone on mobile/tablet:
  // - Shows a translucent side bar that grows as you drag until it hits SWIPE_CONFIRM_PX ("confirmed").
  // - When zoomed in (`.zoom`), only activates AFTER hitting the horizontal scroll edge of `#sbox`.
  const SWIPE_DEADZONE_PX = 10;
  const SWIPE_CONFIRM_PX = 90;
  const SWIPE_BAR_MAX_WIDTH_PX = 84;

  let touchStartX = 0;
  let touchStartY = 0;
  let pullStartX = null;
  let pullDirection = 0; // -1 = swipe right -> previous, +1 = swipe left -> next
  let pullConfirmed = false;
  let gestureLockedVertical = false;
  let didSwipeDrag = false;

  // Tapping (mobile) or clicking (desktop) the image toggles zoom; ignored if the user just dragged to swipe.
  stageImage.onclick = () => {
    if (didSwipeDrag) {
      didSwipeDrag = false;
      return;
    }
    stageBox.classList.toggle('zoom');
  };

  const resetSwipeBars = () => {
    for (const bar of [swipePrevBar, swipeNextBar]) {
      if (!bar) continue;
      bar.classList.remove('active', 'ready');
      bar.style.width = '0px';
      bar.style.opacity = '0';
    }
    pullStartX = null;
    pullDirection = 0;
    pullConfirmed = false;
    gestureLockedVertical = false;
  };

  stageBox.addEventListener('touchstart', event => {
    resetSwipeBars();
    didSwipeDrag = false;
    if (event.touches.length !== 1) return;
    touchStartX = event.touches[0].clientX;
    touchStartY = event.touches[0].clientY;
  }, { passive: true });

  stageBox.addEventListener('touchmove', event => {
    if (event.touches.length !== 1) {
      resetSwipeBars();
      return;
    }
    const currentX = event.touches[0].clientX;
    const currentY = event.touches[0].clientY;
    const totalDx = currentX - touchStartX;
    const totalDy = currentY - touchStartY;

    if (pullStartX === null) {
      if (gestureLockedVertical) return;
      if (Math.abs(totalDy) > SWIPE_DEADZONE_PX && Math.abs(totalDy) > Math.abs(totalDx) * 1.15) {
        gestureLockedVertical = true;
        return;
      }
      if (Math.abs(totalDx) < SWIPE_DEADZONE_PX) return;

      const intendedDir = totalDx < 0 ? 1 : -1; // dragging left -> next (+1), dragging right -> prev (-1)
      const isZoomed = stageBox.classList.contains('zoom');
      const maxScrollLeft = Math.max(0, stageBox.scrollWidth - stageBox.clientWidth);

      // When zoomed in, allow natural horizontal panning until the image hits its horizontal scroll edge.
      if (isZoomed && maxScrollLeft > 2) {
        const atRightEdge = stageBox.scrollLeft >= maxScrollLeft - 2;
        const atLeftEdge = stageBox.scrollLeft <= 2;
        if (intendedDir === 1 && !atRightEdge) return;
        if (intendedDir === -1 && !atLeftEdge) return;
        pullDirection = intendedDir;
        pullStartX = currentX;
      } else {
        pullDirection = intendedDir;
        // Anchor just past the deadzone so pullDistance is already > 0 on the very first active frame.
        pullStartX = touchStartX + (intendedDir === 1 ? -SWIPE_DEADZONE_PX : SWIPE_DEADZONE_PX);
      }
    }

    // Track pull distance from the point where the buffer zone engaged.
    const pullDistance = pullDirection === 1
      ? (pullStartX - currentX)
      : (currentX - pullStartX);

    // Strictly `< 0` (not `<= 0`) so the boundary frame where pullStartX === currentX does not immediately reset!
    if (pullDistance < 0) {
      resetSwipeBars();
      return;
    }

    didSwipeDrag = true;
    if (event.cancelable) event.preventDefault();
    const progress = Math.min(1, pullDistance / SWIPE_CONFIRM_PX);
    const activeBar = pullDirection === 1 ? swipeNextBar : swipePrevBar;
    const otherBar = pullDirection === 1 ? swipePrevBar : swipeNextBar;
    if (otherBar) {
      otherBar.classList.remove('active', 'ready');
      otherBar.style.width = '0px';
      otherBar.style.opacity = '0';
    }
    if (activeBar) {
      activeBar.classList.add('active');
      activeBar.style.width = Math.max(8, Math.round(progress * SWIPE_BAR_MAX_WIDTH_PX)) + 'px';
      activeBar.style.opacity = (0.22 + progress * 0.72).toFixed(2);
      const nowConfirmed = progress >= 1;
      activeBar.classList.toggle('ready', nowConfirmed);
      if (nowConfirmed && !pullConfirmed && typeof navigator.vibrate === 'function') {
        navigator.vibrate(12);
      }
      pullConfirmed = nowConfirmed;
    }
  }, { passive: false });

  const finishSwipe = () => {
    const shouldGo = pullConfirmed ? pullDirection : 0;
    resetSwipeBars();
    if (shouldGo !== 0) goSlide(shouldGo);
  };
  stageBox.addEventListener('touchend', finishSwipe);
  stageBox.addEventListener('touchcancel', resetSwipeBars);
}
