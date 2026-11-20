/**
 * Gallery state, server-side pagination/sorting/filtering, sequential image loading and thumbnail/rescan jobs.
 */

import { API, createElement, escapeHtml, isOwner, parseTags, select, toast, withBusy } from './api.js';

// Sort keys must match SORT_KEYS in gloan/query.py; labels are for the dropdown only.
export const SORTS = {
  new: 'Newest first',
  old: 'Oldest first',
  az: 'Title A–Z',
  za: 'Title Z–A',
  big: 'Largest file',
  small: 'Smallest file',
};

export const PAGE_SIZES = [10, 20, 50];

/**
 * Shared gallery state describing the current filter settings and the server's current page reply.
 */
export const state = {
  ready: false,
  activeTags: new Set(),
  searchQuery: '',
  matchAll: true,
  layout: localStorage.getItem('gloan-layout') || 'masonry',
  view: [],
  filteredTotal: 0,
  photoTotal: 0,
  pageCount: 1,
  tagList: [],
  page: 0,
  pageSize: Number(localStorage.getItem('gloan-pagesize')) || 10,
  sortKey: localStorage.getItem('gloan-sort') || 'new',
  suggestionCache: new Map(),
};

let imageLoadRun = 0;  // bumped on every render or lightbox open so a sequential tile load yields immediately
let activeTileImage = null; // the tile <img> currently downloading, so openLightbox can abort it and free sockets
let currentGalleryEl = null;
let refreshRun = 0;    // bumped per fetch so a slow, outdated response cannot overwrite a newer one

/**
 * Abort any in-flight sequential tile download so a lightbox full-resolution image request never waits in the
 * browser's per-origin TCP socket queue behind a slow un-thumbnailed image.
 */
export function pauseGalleryImageLoads() {
  imageLoadRun++;
  if (activeTileImage && !activeTileImage.complete) {
    const cancelHandler = activeTileImage.onerror;
    activeTileImage.onload = activeTileImage.onerror = null;
    activeTileImage.removeAttribute('src');
    if (typeof cancelHandler === 'function') cancelHandler();
  }
  activeTileImage = null;
}

/**
 * Resume sequential loading for any remaining unloaded cards on the current page after the lightbox closes.
 */
export function resumeGalleryImageLoads() {
  if (currentGalleryEl) {
    loadImagesInOrder(currentGalleryEl);
  }
}

let cardHandlers = {
  openLightbox: () => {},
  openTagModal: () => {},
  openUpload: () => {},
  loadDemo: () => {},
};

/**
 * Wire lightbox and upload actions into gallery card and empty-state buttons without circular module imports.
 */
export function registerGalleryActions(handlers) {
  cardHandlers = { ...cardHandlers, ...handlers };
}

/**
 * Return the gallery-wide [[tag, count]] list from the last server reply.
 */
export function tagCounts() {
  return state.tagList;
}

/**
 * Ask the server for the current page (filter + sort + page number) and store the result in state.
 */
export async function fetchPage() {
  const runId = ++refreshRun;
  const query = {
    page: state.page,
    size: state.pageSize,
    sort: state.sortKey,
    match: 'all',
    tags: [...state.activeTags].join(','),
    q: state.searchQuery,
  };
  const reply = await API.list(query);
  if (runId !== refreshRun) return false; // a newer request superseded this one

  const removedTags = [...state.activeTags].filter(tag => !reply.tags.some(([name]) => name === tag));
  if (removedTags.length) {
    removedTags.forEach(tag => state.activeTags.delete(tag));
    return fetchPage(); // a filter tag was deleted from all photos; re-query without it
  }

  state.view = reply.photos;
  state.filteredTotal = reply.total;
  state.photoTotal = reply.all;
  state.pageCount = reply.pages;
  state.page = reply.page;
  state.tagList = reply.tags;
  state.suggestionCache.clear();
  return true;
}

/**
 * Fetch the current page from the server and redraw the gallery.
 * Shows an immediate loading state on `#galleryHost` so filter/sort/page clicks respond without delay.
 */
export async function refresh() {
  const host = select('#galleryHost');
  if (host) host.classList.add('loading');
  try {
    if (await fetchPage()) render();
  } catch (error) {
    toast(error.message);
  } finally {
    if (host) host.classList.remove('loading');
  }
}

/**
 * Build the pagination bar: First/Prev, numbered pages (with … gaps), Next/Last.
 */
export function buildPager() {
  const pagerBox = createElement('div', 'pager');
  const firstIndex = state.page * state.pageSize + 1;
  const lastIndex = Math.min(state.filteredTotal, (state.page + 1) * state.pageSize);
  pagerBox.append(createElement('div', 'info', `Showing ${firstIndex}–${lastIndex} of ${state.filteredTotal}`));
  if (state.pageCount < 2) return pagerBox;

  const goToPage = (targetPage, buttonEl) => {
    if (buttonEl) buttonEl.classList.add('on');
    state.page = targetPage;
    refresh();
    select('#filters').scrollIntoView({ behavior: 'smooth' });
  };
  const addPageButton = (label, targetPage, disabled, isCurrent) => {
    const button = createElement('button', 'btn' + (isCurrent ? ' on' : ''), label);
    button.disabled = !!disabled;
    button.onclick = () => goToPage(targetPage, button);
    pagerBox.append(button);
  };

  addPageButton('«', 0, state.page === 0);
  addPageButton('‹', state.page - 1, state.page === 0);
  let previousPage = -1;
  for (let pageIndex = 0; pageIndex < state.pageCount; pageIndex++) {
    if (pageIndex !== 0 && pageIndex !== state.pageCount - 1 && Math.abs(pageIndex - state.page) > 1) {
      continue; // keep first, last and immediate neighbours of current page
    }
    if (previousPage >= 0 && pageIndex - previousPage > 1) {
      pagerBox.append(createElement('span', 'hint', '…'));
    }
    addPageButton(String(pageIndex + 1), pageIndex, false, pageIndex === state.page);
    previousPage = pageIndex;
  }
  addPageButton('›', state.page + 1, state.page === state.pageCount - 1);
  addPageButton('»', state.pageCount - 1, state.page === state.pageCount - 1);
  return pagerBox;
}

/**
 * Fetch the current page's tile images one at a time in reading order (top-left first).
 * Each image receives its src only after the previous one finishes so off-page images never load.
 */
export async function loadImagesInOrder(galleryElement) {
  const runId = ++imageLoadRun;
  const cards = [...galleryElement.querySelectorAll('.card')];
  // Sort by visual on-screen coordinates (row, then column) because CSS masonry runs down columns first.
  const screenPosition = card => {
    const rect = card.getBoundingClientRect();
    return [Math.round(rect.top / 20), rect.left];
  };
  cards.sort((cardA, cardB) => {
    const [rowA, colA] = screenPosition(cardA);
    const [rowB, colB] = screenPosition(cardB);
    return rowA - rowB || colA - colB;
  });
  for (const card of cards) {
    if (runId !== imageLoadRun) return; // superseded by a newer render or paused by openLightbox
    if (card.classList.contains('loaded')) continue;
    const image = card.querySelector('img');
    activeTileImage = image;
    await new Promise(done => {
      image.onload = () => {
        card.classList.add('loaded');
        done();
      };
      image.onerror = done;
      image.src = image.dataset.src;
    });
    activeTileImage = null;
  }
}

/**
 * Render the stats counters, filter chips, sort/page-size/layout controls, photo cards and pager.
 */
export function render() {
  const tags = tagCounts();
  select('#sPhotos').textContent = state.photoTotal;
  select('#sTags').textContent = tags.length;

  const filtersBar = select('#filters');
  filtersBar.innerHTML = '';
  const allChip = createElement('button', 'chip' + (state.activeTags.size || state.searchQuery ? '' : ' on'), `All<small>${state.photoTotal}</small>`);
  allChip.onclick = () => {
    state.activeTags.clear();
    state.searchQuery = '';
    const searchInput = select('#tagSearch');
    if (searchInput) searchInput.value = '';
    state.page = 0;
    refresh();
  };
  filtersBar.append(allChip);

  tags.forEach(([tagName, count]) => {
    const chip = createElement('button', 'chip' + (state.activeTags.has(tagName) ? ' on' : ''), `${escapeHtml(tagName)}<small>${count}</small>`);
    chip.onclick = () => {
      if (state.activeTags.has(tagName)) state.activeTags.delete(tagName);
      else state.activeTags.add(tagName);
      chip.classList.toggle('on', state.activeTags.has(tagName)); // instant visual toggle before network round-trip
      state.page = 0;
      refresh();
    };
    filtersBar.append(chip);
  });

  const modeBar = createElement('div', 'mode');
  const sortOptions = Object.entries(SORTS)
    .map(([key, label]) => `<option value="${key}"${key === state.sortKey ? ' selected' : ''}>${label}</option>`)
    .join('');
  const sizeOptions = PAGE_SIZES
    .map(size => `<option value="${size}"${size === state.pageSize ? ' selected' : ''}>${size} / page</option>`)
    .join('');
  modeBar.innerHTML = `<select class="sel" id="sortSel" title="Sort by">${sortOptions}</select>
   <select class="sel" id="sizeSel" title="Photos per page">${sizeOptions}</select>
   <div class="seg"><button data-l="masonry" class="${state.layout === 'masonry' ? 'on' : ''}">Masonry</button><button data-l="uniform" class="${state.layout === 'uniform' ? 'on' : ''}">Grid</button></div>`;

  modeBar.querySelector('#sortSel').onchange = event => {
    state.sortKey = event.target.value;
    localStorage.setItem('gloan-sort', state.sortKey);
    state.page = 0;
    refresh();
  };
  modeBar.querySelector('#sizeSel').onchange = event => {
    state.pageSize = Number(event.target.value);
    localStorage.setItem('gloan-pagesize', state.pageSize);
    state.page = 0;
    refresh();
  };
  modeBar.querySelectorAll('[data-l]').forEach(button => {
    button.onclick = () => {
      state.layout = button.dataset.l;
      localStorage.setItem('gloan-layout', state.layout);
      render();
    };
  });
  filtersBar.append(modeBar);

  const host = select('#galleryHost');
  host.innerHTML = '';
  if (!state.photoTotal) {
    host.innerHTML = isOwner()
      ? `<div class="empty"><h2>Your gallery is empty</h2><p>Upload your first photos, or load a few generated samples to try the site out.</p>
      <div class="row"><button class="btn primary" id="e1">＋ Upload photos</button><button class="btn" id="e2">Load sample images</button></div></div>`
      : '<div class="empty"><h2>No photos yet</h2><p>Check back soon.</p></div>';
    if (isOwner()) {
      select('#e1').onclick = () => cardHandlers.openUpload();
      select('#e2').onclick = () => cardHandlers.loadDemo();
    }
    return;
  }
  if (!state.filteredTotal) {
    host.innerHTML = '<div class="empty"><h2>No photos match</h2><p>Try fewer tags or switch to “Any”.</p></div>';
    return;
  }

  const galleryEl = createElement('div', 'gallery' + (state.layout === 'uniform' ? ' uniform' : ''));
  state.view.forEach((photo, index) => {
    const card = createElement('div', 'card in');
    const storyBadge = photo.story ? '<div class="sb">📖 Read the story</div>' : '';
    const tagPills = photo.tags.map(tag => `<span>${escapeHtml(tag)}</span>`).join('');
    card.innerHTML = `<div class="card-loader"><span class="spinner" aria-hidden="true"></span><span>Loading image…</span></div><img data-src="${photo.thumbUrl || photo.url}" alt="${escapeHtml(photo.title || '')}"><div class="ov"><button class="btn owner-only addtag">＋ tags</button><h3>${escapeHtml(photo.title || 'Untitled')}</h3>${storyBadge}<div class="t">${tagPills}</div></div>`;
    card.querySelector('.addtag').onclick = event => {
      event.stopPropagation(); // don't also open the lightbox when clicking "+ tags"
      cardHandlers.openTagModal(photo);
    };
    card.onclick = () => cardHandlers.openLightbox(index);
    card.addEventListener('mousemove', event => {
      const rect = card.getBoundingClientRect();
      const relX = (event.clientX - rect.left) / rect.width - 0.5;
      const relY = (event.clientY - rect.top) / rect.height - 0.5;
      card.style.transform = `perspective(900px) rotateY(${relX * 4}deg) rotateX(${-relY * 4}deg)`;
    });
    card.addEventListener('mouseleave', () => {
      card.style.transform = '';
    });
    galleryEl.append(card);
  });

  currentGalleryEl = galleryEl;
  host.append(galleryEl);
  host.append(buildPager());
  loadImagesInOrder(galleryEl); // intentionally unawaited so cards stream in while UI stays responsive
}

/**
 * Populate popular-tag chips that append to a comma-separated input; shared by upload and add-tags modals.
 */
export function suggestionChips(container, inputElement) {
  container.innerHTML = '';
  tagCounts().slice(0, 12).forEach(([tagName]) => {
    const chip = createElement('button', 'chip', escapeHtml(tagName));
    chip.onclick = () => {
      const chosen = parseTags(inputElement.value);
      if (!chosen.includes(tagName)) chosen.push(tagName);
      inputElement.value = chosen.join(', ');
    };
    container.append(chip);
  });
}

/**
 * Trigger tools/make_thumbnails.py on the server on explicit owner request and poll until completion.
 * Note: Never triggered automatically on upload so Cloud Run CPU time is not consumed unexpectedly.
 */
export async function runThumbnailJob() {
  await withBusy(select('#thumbBtn'), 'Generating thumbnails…', async () => {
    try {
      try {
        await API.call('POST', '/api/thumbnails');
      } catch (error) {
        if (!/already running/.test(error.message)) throw error; // joining an in-progress run is fine
      }
      toast('Generating thumbnails…');
      for (;;) {
        await new Promise(done => setTimeout(done, 1500));
        const status = await API.call('GET', '/api/thumbnails');
        if (status.running) continue;
        await refresh();
        if (status.exitCode === 0) {
          toast(status.missing ? `${status.missing} photo(s) still have no thumbnail` : 'Thumbnails up to date');
        } else {
          const lastLine = status.output.trim().split('\n').pop() || 'unknown error';
          toast('Thumbnail tool failed: ' + lastLine);
        }
        return;
      }
    } catch (error) {
      toast(error.message);
    }
  });
}

/**
 * Trigger a server folder scan to adopt manually copied photos from data/photos/.
 */
export async function rescanFolder() {
  await withBusy(select('#scanBtn'), 'Scanning folder…', async () => {
    try {
      const result = await API.call('POST', '/api/rescan');
      state.page = 0;
      await refresh();
      if (result.added) {
        toast(`Added ${result.added} photo${result.added > 1 ? 's' : ''} from the folder`);
      } else {
        const skippedText = result.skipped.length ? '; skipped: ' + result.skipped.join(', ') : '';
        toast(`No new photos found (${result.indexed} already listed${skippedText})`);
      }
    } catch (error) {
      toast(error.message);
    }
  });
}
