/**
 * Full-screen lightbox, slideshow playback, story drawer, metadata editor and owner Add-Tags modal.
 */

import { API, createElement, escapeHtml, isOwner, parseTags, select, toast, withBusy } from './api.js';
import { pauseGalleryImageLoads, refresh, resumeGalleryImageLoads, state, suggestionChips } from './gallery.js';

const SLIDE_INTERVAL_MS = 4500;

let currentIndex = 0;
let isPlaying = false;
let slideTimerId = null;
let storyOpen = true;
let taggingPhoto = null;
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
  lightboxEl.classList.add('open');
  document.body.style.overflow = 'hidden';
  buildStrip();
  showSlide(true);
}

/**
 * Close the lightbox, stop any running slideshow, resume any unfinished gallery tile loads and exit fullscreen.
 */
export function closeLightbox() {
  stopSlideshow();
  slideLoadRunId++;
  select('#lb').classList.remove('open', 'show-edit', 'chromeless');
  select('#sbox').classList.remove('zoom', 'loading');
  select('#lbImg').removeAttribute('src');
  document.body.style.overflow = '';
  if (document.fullscreenElement) document.exitFullscreen();
  resumeGalleryImageLoads();
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
  select('#lbTags').innerHTML = photo.tags
    .map(tag => `<span data-t="${escapeHtml(tag)}">${escapeHtml(tag)}</span>`)
    .join('');
  select('#lbTags').querySelectorAll('span').forEach(span => {
    span.onclick = () => {
      closeLightbox();
      state.activeTags = new Set([span.dataset.t]);
      state.page = 0;
      refresh();
      scrollTo({ top: 300, behavior: 'smooth' });
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
 * Open the owner-only quick "Add tags" modal for `photo`.
 */
export function openTagModal(photo) {
  if (!isOwner() || !photo) return;
  taggingPhoto = photo;
  select('#tagTitle').textContent = 'Add tags — ' + (photo.title || 'Untitled');
  select('#tagInput').value = '';
  suggestionChips(select('#tagSug'), select('#tagInput'));
  select('#tagModal').classList.add('open');
  setTimeout(() => select('#tagInput').focus(), 30);
}

/**
 * Close the quick "Add tags" modal.
 */
export function closeTagModal() {
  select('#tagModal').classList.remove('open');
  taggingPhoto = null;
}

/**
 * Append the newly entered tags to `taggingPhoto` and persist via PATCH /api/photos/<id>.
 */
export async function submitTags() {
  const photo = taggingPhoto;
  const addedTags = parseTags(select('#tagInput').value);
  if (!photo || !addedTags.length) {
    closeTagModal();
    return;
  }
  await withBusy(select('#tagGo'), 'Adding…', async () => {
    try {
      const mergedTags = [...new Set([...photo.tags, ...addedTags])];
      await API.update(photo.id, photo.title || '', mergedTags, photo.story || '');
    } catch (error) {
      toast(error.message);
      return;
    }
    closeTagModal();
    await afterPhotoEdit(photo.id, `Added ${addedTags.length} tag${addedTags.length > 1 ? 's' : ''}`);
  });
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
  select('#eSave').onclick = saveEdit;
  select('#delBtn').onclick = deleteCurrentPhoto;
  stageImage.onclick = () => select('#sbox').classList.toggle('zoom');

  let hideChromeTimer = null;
  lightboxEl.addEventListener('mousemove', () => {
    if (isPlaying) {
      lightboxEl.classList.remove('chromeless');
      clearTimeout(hideChromeTimer);
      hideChromeTimer = setTimeout(() => isPlaying && lightboxEl.classList.add('chromeless'), 2200);
    }
  });

  // Touch swipe navigation on mobile/tablet
  let touchStartX = 0;
  select('#sbox').addEventListener('touchstart', event => {
    touchStartX = event.touches[0].clientX;
  }, { passive: true });
  select('#sbox').addEventListener('touchend', event => {
    const deltaX = event.changedTouches[0].clientX - touchStartX;
    if (Math.abs(deltaX) > 50) goSlide(deltaX < 0 ? 1 : -1);
  });
}
