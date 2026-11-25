/**
 * Gallery state, server-side pagination/sorting/filtering, sequential image loading and thumbnail/rescan jobs.
 */

import { API, createElement, escapeHtml, isOwner, parseTags, runBackgroundTask, select, toast, withBusy } from './api.js';

// Sort keys must match SORT_KEYS in gloan/query.py; labels are for the dropdown only.
export const SORTS = {
  new: 'Newest first',
  old: 'Oldest first',
  az: 'Title A–Z',
  za: 'Title Z–A',
  big: 'Largest file',
  small: 'Smallest file',
};

export const PAGE_SIZES = [16, 32, 64, 128];

const storedPageSize = Number(localStorage.getItem('gloan-pagesize'));
const initialPageSize = PAGE_SIZES.includes(storedPageSize) ? storedPageSize : 16;

let masonryConfigPromise = null;

/**
 * Load and cache the Masonry viewport mosaic template definitions from `/static/masonry_templates.json`.
 */
async function loadMasonryTemplates() {
  if (!masonryConfigPromise) {
    masonryConfigPromise = fetch('/static/masonry_templates.json')
      .then(res => (res.ok ? res.json() : null))
      .catch(() => null);
  }
  return masonryConfigPromise;
}

/**
 * Assign `photos` to the slots of `template` minimizing total log aspect-ratio distortion.
 * Sorting photos by natural aspectRatio and slots by rendered slot aspectRatio is optimal by rearrangement.
 */
function scoreTemplateForPhotos(photos, template, cols, rows, baseAspect) {
  const slotsWithRatio = template.slots.map((slot, slotIdx) => ({
    slot,
    slotIdx,
    ratio: ((slot.colSpan / cols) * baseAspect) / (slot.rowSpan / rows),
  }));
  const photosWithRatio = photos.map((photo, photoIdx) => ({
    photo,
    photoIdx,
    ratio: Number(photo.aspectRatio) > 0 ? Number(photo.aspectRatio) : 1.3333,
  }));

  const sortedSlots = [...slotsWithRatio].sort((a, b) => a.ratio - b.ratio);
  const sortedPhotos = [...photosWithRatio].sort((a, b) => a.ratio - b.ratio);

  let totalLogDiff = 0;
  let sumLogRatioDelta = 0;
  const assignments = new Array(template.slots.length);
  for (let i = 0; i < sortedSlots.length; i++) {
    const logDelta = Math.log(sortedPhotos[i].ratio / sortedSlots[i].ratio);
    totalLogDiff += Math.abs(logDelta);
    sumLogRatioDelta += logDelta;
    assignments[sortedSlots[i].slotIdx] = {
      photo: sortedPhotos[i].photo,
      slot: sortedSlots[i].slot,
    };
  }
  // Dynamically scale the page frame aspectRatio slightly toward the photos' natural ratios to tighten fit.
  const meanScale = Math.exp(sumLogRatioDelta / Math.max(1, sortedSlots.length));
  const frameAspect = Math.max(0.95, Math.min(1.65, baseAspect * Math.pow(meanScale, 0.55)));
  return {
    template,
    assignments,
    frameAspect: Number(frameAspect.toFixed(3)),
    avgCost: totalLogDiff / Math.max(1, sortedSlots.length),
  };
}

/**
 * Pack an ordered list of photos into viewport-sized Masonry template pages of 4-8 photos each
 * (falling back to 1-3 photo templates for any final remainder).
 */
export function packMasonryPages(photos, config) {
  if (!photos.length) return [];
  if (!config || !Array.isArray(config.templates) || !config.templates.length) {
    return [{ template: null, assignments: photos.map(photo => ({ photo, slot: null })), frameAspect: 1.2 }];
  }
  const cols = config.cols || 12;
  const rows = config.rows || 6;
  const baseAspect = config.baseAspect || 1.15;
  const pages = [];
  let cursor = 0;

  while (cursor < photos.length) {
    const remaining = photos.length - cursor;
    let candidates;
    if (remaining < 4) {
      candidates = (config.smallTemplates || []).filter(tpl => tpl.count === remaining);
    } else {
      // Avoid leaving a lonely 1-photo tail when a slightly smaller/larger template (4..8) avoids it.
      candidates = config.templates.filter(tpl => tpl.count <= remaining && (remaining - tpl.count !== 1 || remaining <= 8));
      if (!candidates.length) {
        candidates = config.templates.filter(tpl => tpl.count <= remaining);
      }
    }
    if (!candidates.length) {
      const slice = photos.slice(cursor, cursor + Math.min(8, remaining));
      pages.push({ template: null, assignments: slice.map(photo => ({ photo, slot: null })), frameAspect: baseAspect });
      cursor += slice.length;
      continue;
    }

    let bestMatch = null;
    for (const tpl of candidates) {
      const slice = photos.slice(cursor, cursor + tpl.count);
      const scored = scoreTemplateForPhotos(slice, tpl, cols, rows, baseAspect);
      if (!bestMatch || scored.avgCost < bestMatch.avgCost) {
        bestMatch = scored;
      }
    }
    pages.push(bestMatch);
    cursor += bestMatch.template.count;
  }
  return pages;
}

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
  masonryPages: [],
  currentMasonryPage: null,
  filteredTotal: 0,
  photoTotal: 0,
  pageCount: 1,
  tagList: [],
  page: 0,
  pageSize: initialPageSize,
  sortKey: localStorage.getItem('gloan-sort') || 'new',
  suggestionCache: new Map(),
};

const TILE_LOAD_CONCURRENCY = 4; // parallel tile worker lanes so one huge un-thumbnailed file never blocks the rest

let imageLoadRun = 0;  // bumped on every render or lightbox open so tile downloads yield immediately
const activeTileImages = new Set(); // all tile <img> elements currently downloading, so openLightbox can abort them
let currentGalleryEl = null;
let refreshRun = 0;    // bumped per fetch so a slow, outdated response cannot overwrite a newer one
let activeFetchController = null; // cancels superseded GET /api/photos calls on rapid filter/page clicks

/**
 * Abort any in-flight gallery tile downloads so a lightbox full-resolution image request never waits in the
 * browser's per-origin TCP socket queue behind a slow un-thumbnailed image.
 */
export function pauseGalleryImageLoads() {
  imageLoadRun++;
  for (const img of activeTileImages) {
    if (!img.complete) {
      const cancelHandler = img.onerror;
      img.onload = img.onerror = null;
      img.removeAttribute('src');
      if (typeof cancelHandler === 'function') cancelHandler();
    }
  }
  activeTileImages.clear();
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
 * True on mobile / foldable phone viewports (`<= 860px`) where only Grid view (2-4 across) is used.
 */
function isMobileViewport() {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    && window.matchMedia('(max-width: 860px)').matches;
}

/**
 * Effective gallery layout: mobile viewports always use Grid (`'uniform'`); desktop uses the user's chosen layout.
 */
function effectiveLayout() {
  return isMobileViewport() ? 'uniform' : state.layout;
}

if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
  const mobileMediaQuery = window.matchMedia('(max-width: 860px)');
  const onViewportChange = () => {
    if (state.ready) {
      state.page = 0;
      refresh();
    }
  };
  if (typeof mobileMediaQuery.addEventListener === 'function') {
    mobileMediaQuery.addEventListener('change', onViewportChange);
  }
}

/**
 * Ask the server for the current page (filter + sort + page number) and store the result in state.
 * In desktop Masonry mode, fetches a batch and dynamically packs photos into 4-8 photo template pages based on
 * their natural aspectRatio; in Grid mode (and on mobile), uses exact multiples of 16 (16/32/64/128).
 */
export async function fetchPage() {
  const runId = ++refreshRun;
  if (activeFetchController) {
    activeFetchController.abort();
    activeFetchController = null;
  }
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  activeFetchController = controller;
  const isMasonry = effectiveLayout() === 'masonry';
  const query = {
    page: isMasonry ? 0 : state.page,
    size: isMasonry ? 128 : state.pageSize,
    sort: state.sortKey,
    match: 'all',
    tags: [...state.activeTags].join(','),
    q: state.searchQuery,
  };
  let reply;
  let masonryConfig;
  try {
    [reply, masonryConfig] = await Promise.all([
      API.list(query, controller?.signal),
      isMasonry ? loadMasonryTemplates() : Promise.resolve(null),
    ]);
  } finally {
    if (runId === refreshRun) activeFetchController = null;
  }
  if (runId !== refreshRun) return false; // a newer request superseded this one

  const removedTags = [...state.activeTags].filter(tag => !reply.tags.some(([name]) => name === tag));
  if (removedTags.length) {
    removedTags.forEach(tag => state.activeTags.delete(tag));
    return fetchPage(); // a filter tag was deleted from all photos; re-query without it
  }

  state.filteredTotal = reply.total;
  state.photoTotal = reply.all;
  state.tagList = reply.tags;
  state.suggestionCache.clear();

  if (isMasonry) {
    state.masonryPages = packMasonryPages(reply.photos, masonryConfig);
    state.pageCount = Math.max(1, state.masonryPages.length);
    state.page = Math.max(0, Math.min(state.page, state.pageCount - 1));
    state.currentMasonryPage = state.masonryPages[state.page] || null;
    state.view = state.currentMasonryPage ? state.currentMasonryPage.assignments.map(item => item.photo) : [];
  } else {
    state.masonryPages = [];
    state.currentMasonryPage = null;
    state.view = reply.photos;
    state.pageCount = reply.pages;
    state.page = reply.page;
  }
  return true;
}

/**
 * Fetch the current page from the server and redraw the gallery.
 * When `silent` is true (background sync after optimistic UI updates), avoids flashing the loading badge.
 */
export async function refresh({ silent = false } = {}) {
  const host = select('#galleryHost');
  if (host && !silent) host.classList.add('loading');
  try {
    if (await fetchPage()) render();
  } catch (error) {
    if (error && error.name === 'AbortError') return; // superseded by a newer click
    toast(error.message);
  } finally {
    if (host && !silent && !activeFetchController) host.classList.remove('loading');
  }
}

/**
 * Build the pagination bar: First/Prev, numbered pages (with … gaps), Next/Last.
 */
export function buildPager() {
  const pagerBox = createElement('div', 'pager');
  const isMasonry = effectiveLayout() === 'masonry';
  let firstIndex;
  let lastIndex;
  if (isMasonry && state.masonryPages.length) {
    let offset = 0;
    for (let i = 0; i < state.page; i++) offset += state.masonryPages[i].assignments.length;
    firstIndex = offset + 1;
    lastIndex = offset + state.view.length;
  } else {
    firstIndex = state.page * state.pageSize + 1;
    lastIndex = Math.min(state.filteredTotal, (state.page + 1) * state.pageSize);
  }
  pagerBox.append(createElement('div', 'info', `Showing ${firstIndex}–${lastIndex} of ${state.filteredTotal}`));
  if (state.pageCount < 2) return pagerBox;

  const goToPage = (targetPage, buttonEl) => {
    if (buttonEl) buttonEl.classList.add('on');
    state.page = targetPage;
    if (isMasonry && state.masonryPages.length) {
      state.currentMasonryPage = state.masonryPages[state.page] || null;
      state.view = state.currentMasonryPage ? state.currentMasonryPage.assignments.map(item => item.photo) : [];
      render();
    } else {
      refresh();
    }
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
 * Fetch the current page's tile images in reading order (top-left first) using bounded concurrency (4 lanes),
 * prioritizing lightweight `/thumbs/` tiles ahead of any un-thumbnailed `/photos/` originals so a single 50 MB
 * un-thumbnailed image can never stall the remaining cards on the page.
 */
export async function loadImagesInOrder(galleryElement) {
  const runId = ++imageLoadRun;
  const cards = [...galleryElement.querySelectorAll('.card')].filter(card => !card.classList.contains('loaded'));
  // Sort lightweight thumbnails before full-size fallback originals, then by visual on-screen (row, column).
  const screenPosition = card => {
    const rect = card.getBoundingClientRect();
    const src = card.querySelector('img')?.dataset.src || '';
    const isFullOriginal = src.startsWith('/photos/') ? 1 : 0;
    return [isFullOriginal, Math.round(rect.top / 20), rect.left];
  };
  cards.sort((cardA, cardB) => {
    const [origA, rowA, colA] = screenPosition(cardA);
    const [origB, rowB, colB] = screenPosition(cardB);
    return origA - origB || rowA - rowB || colA - colB;
  });

  let nextIndex = 0;
  const loadWorker = async () => {
    while (nextIndex < cards.length) {
      if (runId !== imageLoadRun) return; // superseded by a newer render or paused by openLightbox
      const card = cards[nextIndex++];
      if (!card || card.classList.contains('loaded')) continue;
      const image = card.querySelector('img');
      activeTileImages.add(image);
      await new Promise(done => {
        image.onload = () => {
          card.classList.add('loaded');
          done();
        };
        image.onerror = done;
        image.src = image.dataset.src;
      });
      activeTileImages.delete(image);
    }
  };

  const laneCount = Math.min(TILE_LOAD_CONCURRENCY, cards.length);
  await Promise.all(Array.from({ length: laneCount }, () => loadWorker()));
}

/**
 * Parse active comma-separated tag terms from `#tagSearch` (or `state.searchQuery`).
 */
function activeSearchTerms() {
  const searchInput = select('#tagSearch');
  const raw = searchInput ? searchInput.value : state.searchQuery;
  return String(raw || '')
    .split(',')
    .map(term => term.trim())
    .filter(Boolean);
}

/**
 * Toggle a tag chip directly in the `#tagSearch` input box:
 * - Clicking an unused tag appends it with a trailing comma (`"tag1, "`, then `"tag1, tag2, "`).
 * - Clicking an in-use tag removes it from the search bar.
 */
function toggleTagInSearch(tagName) {
  const searchInput = select('#tagSearch');
  const terms = activeSearchTerms();
  const targetLower = tagName.toLowerCase();
  const existingIdx = terms.findIndex(term => term.toLowerCase() === targetLower);
  if (existingIdx >= 0) {
    terms.splice(existingIdx, 1);
  } else {
    terms.push(tagName);
  }
  const formatted = terms.length ? terms.join(', ') + ', ' : '';
  if (searchInput) searchInput.value = formatted;
  state.searchQuery = terms.join(', ');
  state.activeTags.clear();
  state.page = 0;
  // Optimistically highlight active sidebar tags at 0ms while GET /api/photos fetches in the background.
  const nextInUse = new Set(terms.map(term => term.toLowerCase()));
  const sidebarButtons = select('#tagList')?.querySelectorAll('button') || [];
  sidebarButtons.forEach((btn, idx) => {
    if (idx === 0) {
      btn.classList.toggle('on', nextInUse.size === 0);
    } else {
      const label = (btn.querySelector('span')?.textContent || '').toLowerCase();
      btn.classList.toggle('on', nextInUse.has(label));
    }
  });
  refresh();
}

/**
 * Render the desktop left tag sidebar list (hidden on mobile where only `#tagSearch` is shown),
 * sort/grid-size/layout controls, dynamic masonry/grid cards and pager.
 */
export function render() {
  const tags = tagCounts();
  const layout = effectiveLayout();
  const photosStatEl = select('#sPhotos');
  const tagsStatEl = select('#sTags');
  if (photosStatEl) photosStatEl.textContent = state.photoTotal;
  if (tagsStatEl) tagsStatEl.textContent = tags.length;

  // Desktop left sidebar tag list synced directly with `#tagSearch` (hidden on mobile via CSS).
  const tagListEl = select('#tagList');
  const inUseLower = new Set(activeSearchTerms().map(term => term.toLowerCase()));
  if (tagListEl) {
    tagListEl.innerHTML = '';
    const allItem = createElement('li');
    const allBtn = createElement('button', inUseLower.size ? '' : 'on', `<span>All</span><small>${state.photoTotal}</small>`);
    allBtn.onclick = () => {
      state.activeTags.clear();
      state.searchQuery = '';
      const searchInput = select('#tagSearch');
      if (searchInput) searchInput.value = '';
      state.page = 0;
      refresh();
    };
    allItem.append(allBtn);
    tagListEl.append(allItem);

    tags.forEach(([tagName, count]) => {
      const isSelected = inUseLower.has(tagName.toLowerCase());
      const item = createElement('li');
      const btn = createElement('button', isSelected ? 'on' : '', `<span>${escapeHtml(tagName)}</span><small>${count}</small>`);
      btn.onclick = () => toggleTagInSearch(tagName);
      item.append(btn);
      tagListEl.append(item);
    });
  }

  // Top controls bar: Sort dropdown, Grid "X / page" selector (Grid view / mobile), and desktop Masonry | Grid toggle.
  const filtersBar = select('#filters');
  filtersBar.innerHTML = '';
  const modeBar = createElement('div', 'mode');
  const sortOptions = Object.entries(SORTS)
    .map(([key, label]) => `<option value="${key}"${key === state.sortKey ? ' selected' : ''}>${label}</option>`)
    .join('');
  const sizeSelectorHtml = layout === 'uniform'
    ? `<select class="sel" id="sizeSel" title="Photos per page">${
      PAGE_SIZES.map(size => `<option value="${size}"${size === state.pageSize ? ' selected' : ''}>${size} / page</option>`).join('')
    }</select>`
    : '';
  modeBar.innerHTML = `<select class="sel" id="sortSel" title="Sort by">${sortOptions}</select>
   ${sizeSelectorHtml}
   <div class="seg"><button data-l="masonry" class="${layout === 'masonry' ? 'on' : ''}">Masonry</button><button data-l="uniform" class="${layout === 'uniform' ? 'on' : ''}">Grid</button></div>`;

  modeBar.querySelector('#sortSel').onchange = event => {
    state.sortKey = event.target.value;
    localStorage.setItem('gloan-sort', state.sortKey);
    state.page = 0;
    refresh();
  };
  const sizeSel = modeBar.querySelector('#sizeSel');
  if (sizeSel) {
    sizeSel.onchange = event => {
      state.pageSize = Number(event.target.value);
      localStorage.setItem('gloan-pagesize', state.pageSize);
      state.page = 0;
      refresh();
    };
  }
  modeBar.querySelectorAll('[data-l]').forEach(button => {
    button.onclick = () => {
      state.layout = button.dataset.l;
      localStorage.setItem('gloan-layout', state.layout);
      state.page = 0;
      modeBar.querySelectorAll('[data-l]').forEach(btn => btn.classList.toggle('on', btn === button));
      refresh();
    };
  });
  filtersBar.append(modeBar);

  const host = select('#galleryHost');
  host.innerHTML = '';
  if (!state.photoTotal) {
    host.innerHTML = isOwner()
      ? `<div class="empty"><h2>Your gallery is empty</h2><p>Upload your first photos, or load a few generated samples to try the site out.</p>
      <div class="row"><button class="btn primary" id="e1">Upload photos</button><button class="btn" id="e2">Load sample images</button></div></div>`
      : '<div class="empty"><h2>No photos yet</h2><p>Check back soon.</p></div>';
    if (isOwner()) {
      select('#e1').onclick = () => cardHandlers.openUpload();
      select('#e2').onclick = () => cardHandlers.loadDemo();
    }
    return;
  }
  if (!state.filteredTotal) {
    host.innerHTML = '<div class="empty"><h2>No photos match</h2><p>Try fewer tags in the search bar.</p></div>';
    return;
  }

  const isMosaic = layout === 'masonry' && state.currentMasonryPage && state.currentMasonryPage.template;
  const galleryClass = layout === 'uniform'
    ? 'gallery uniform'
    : (isMosaic ? 'gallery masonry-mosaic' : 'gallery');
  const galleryEl = createElement('div', galleryClass);
  if (isMosaic) {
    galleryEl.style.aspectRatio = String(state.currentMasonryPage.frameAspect);
  }

  const renderItems = isMosaic
    ? state.currentMasonryPage.assignments
    : state.view.map(photo => ({ photo, slot: null }));

  renderItems.forEach(({ photo, slot }, index) => {
    const card = createElement('div', 'card in');
    if (slot) {
      card.style.gridColumn = `${slot.col} / span ${slot.colSpan}`;
      card.style.gridRow = `${slot.row} / span ${slot.rowSpan}`;
    }
    const storyBadge = photo.story ? '<div class="sb">Read the story</div>' : '';
    const manualTags = photo.tags || [];
    const manualSet = new Set(manualTags);
    const aiTags = (photo.aiTags || []).filter(tag => !manualSet.has(tag));
    const tagPills = [
      ...manualTags.map(tag => `<span>${escapeHtml(tag)}</span>`),
      ...aiTags.map(tag => `<span class="ai-tag" title="AI tag">${escapeHtml(tag)}</span>`),
    ].join('');
    card.innerHTML = `<div class="card-loader"><span class="spinner" aria-hidden="true"></span><span>Loading image…</span></div><img data-src="${escapeHtml(photo.thumbUrl || photo.url)}" alt="${escapeHtml(photo.title || '')}"><div class="ov"><button class="btn owner-only addtag">+ tags</button><h3>${escapeHtml(photo.title || 'Untitled')}</h3>${storyBadge}<div class="t">${tagPills}</div></div>`;
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
    await runBackgroundTask('Generating thumbnails…', async () => {
      try {
        try {
          await API.call('POST', '/api/thumbnails');
        } catch (error) {
          if (!/already running/.test(error.message)) throw error; // joining an in-progress run is fine
        }
        toast('Generating thumbnails in background…');
        for (;;) {
          await new Promise(done => setTimeout(done, 1500));
          const status = await API.call('GET', '/api/thumbnails');
          if (status.running) continue;
          await refresh({ silent: true });
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
  });
}

/**
 * Trigger a server folder scan to adopt manually copied photos from data/photos/.
 */
export async function rescanFolder() {
  await withBusy(select('#scanBtn'), 'Scanning folder…', async () => {
    await runBackgroundTask('Scanning folder…', async () => {
      try {
        const result = await API.call('POST', '/api/rescan');
        state.page = 0;
        await refresh({ silent: true });
        const aiNote = result.aiTagged ? `, AI tagged ${result.aiTagged}` : '';
        if (result.added) {
          toast(`Added ${result.added} photo${result.added > 1 ? 's' : ''} from the folder${aiNote}`);
        } else if (result.aiTagged) {
          toast(`AI tagged ${result.aiTagged} photo${result.aiTagged > 1 ? 's' : ''}`);
        } else {
          const skippedCount = Array.isArray(result.skipped) ? result.skipped.length : 0;
          let skippedNote = '';
          if (skippedCount === 1) {
            skippedNote = ` (${result.skipped[0]})`;
          } else if (skippedCount > 1) {
            skippedNote = ` (${skippedCount} skipped, e.g. ${result.skipped[0]})`;
          }
          toast(`No new photos found — ${result.indexed} indexed${skippedNote}`);
        }
      } catch (error) {
        toast(error.message);
      }
    });
  });
}

/**
 * Clear all AI-generated tags from `data/photos.json` (manual tags remain untouched).
 */
export async function clearAiTags() {
  if (!confirm('Clear all AI-generated tags from photos.json? Manual tags will be kept.')) return;
  await withBusy(select('#clearAiTagsBtn'), 'Clearing AI tags…', async () => {
    await runBackgroundTask('Clearing AI tags…', async () => {
      try {
        const result = await API.clearAiTags();
        await refresh({ silent: true });
        toast(result.cleared
          ? `Cleared AI tags from ${result.cleared} photo${result.cleared > 1 ? 's' : ''}`
          : 'No AI tags to clear');
      } catch (error) {
        toast(error.message);
      }
    });
  });
}
