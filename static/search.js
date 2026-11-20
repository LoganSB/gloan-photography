/**
 * Tag search combobox and comma-delimited predictive autocomplete backed by GET /api/tags and GET /api/photos?q=.
 *
 * Behavior:
 * - Typing into `#tagSearch` filters the gallery live using AND matching across all comma-separated terms
 *   (e.g. `*wint` matches `bryce_winter` and `arches_winter`; `*winter, bryce` matches only `bryce_winter`).
 * - Predictive autocomplete always suggests completions for the active (last) comma-delimited segment.
 * - Pressing Tab (or clicking a suggestion) autocompletes the current segment with `<tag>, ` (note the trailing
 *   comma) and immediately re-opens predictive suggestions for the next comma-delimited segment.
 */

import { API, createElement, select } from './api.js';
import { refresh, state } from './gallery.js';

// Labels shown next to looser matches in the dropdown so they don't surprise the user.
export const MATCH_NOTES = {
  fuzzy: 'did you mean?',
  wildcard: 'pattern',
};

let suggestions = [];
let highlightedIndex = -1;
let searchTimerId = null;
let filterTimerId = null;
let searchRunId = 0;

/**
 * Split `#tagSearch.value` into `{ completedTerms, activeSegment }`.
 * For example, `"bryce_fall, win"` -> `{ completedTerms: ["bryce_fall"], activeSegment: "win" }`.
 */
export function splitCommaInput(rawValue) {
  const parts = String(rawValue || '').split(',');
  const activeSegment = parts.pop().trim();
  const completedTerms = parts.map(part => part.trim()).filter(Boolean);
  return { completedTerms, activeSegment };
}

/**
 * Sync the search bar's comma-separated query into `state.searchQuery` and refresh the gallery (AND matching).
 */
function applyLiveSearchFilter() {
  const searchInput = select('#tagSearch');
  const nextQuery = searchInput.value.trim().replace(/,+$/, '').trim();
  if (nextQuery === state.searchQuery) return;
  state.searchQuery = nextQuery;
  state.page = 0;
  refresh();
}

/**
 * Query GET /api/tags for the current comma-delimited segment and update the predictive dropdown.
 */
export async function updateSuggestions() {
  const searchInput = select('#tagSearch');
  const { completedTerms, activeSegment } = splitCommaInput(searchInput.value);
  const runId = ++searchRunId;
  let found = state.suggestionCache.get(activeSegment);
  if (!found) {
    try {
      found = (await API.tags(activeSegment)).suggestions;
    } catch (error) {
      return; // suggestions are optional; stay quiet on transient network errors
    }
    state.suggestionCache.set(activeSegment, found);
  }
  if (runId !== searchRunId) return; // user kept typing; a newer lookup owns the dropdown
  const alreadyUsed = new Set([...state.activeTags, ...completedTerms.map(term => term.toLowerCase())]);
  suggestions = found.filter(item => !alreadyUsed.has(item.tag) && !alreadyUsed.has(item.tag.toLowerCase()));
  highlightedIndex = suggestions.length ? 0 : -1;
  drawSuggestions();
}

/**
 * Render the current `suggestions` array into `#tagSuggest` and sync ARIA attributes.
 */
export function drawSuggestions() {
  const searchInput = select('#tagSearch');
  const suggestList = select('#tagSuggest');
  suggestList.innerHTML = '';
  suggestions.forEach((item, index) => {
    const row = createElement('li', index === highlightedIndex ? 'on' : '');
    row.setAttribute('role', 'option');
    row.append(document.createTextNode(item.tag)); // text node: a tag name is never parsed as HTML
    if (MATCH_NOTES[item.match]) {
      row.append(createElement('span', 'why', MATCH_NOTES[item.match]));
    }
    row.append(createElement('small', '', String(item.count)));
    row.onmousedown = event => {
      event.preventDefault(); // fire before input blur closes the list
      chooseSuggestion(index);
    };
    suggestList.append(row);
  });
  const isOpen = suggestions.length > 0 && document.activeElement === searchInput;
  suggestList.classList.toggle('open', isOpen);
  searchInput.setAttribute('aria-expanded', String(isOpen));
}

/**
 * Complete the active comma-delimited segment with `suggestions[index].tag` followed by a comma (`"tag, "`),
 * refresh the gallery filter, and immediately re-open predictive search for the newly started comma segment.
 */
export function chooseSuggestion(index) {
  const item = suggestions[index];
  if (!item) return;
  const searchInput = select('#tagSearch');
  const { completedTerms } = splitCommaInput(searchInput.value);
  completedTerms.push(item.tag);
  searchInput.value = completedTerms.join(', ') + ', ';
  searchInput.focus();
  applyLiveSearchFilter();
  updateSuggestions();
}

/**
 * Attach input, focus, blur and keyboard (Tab autofill, Enter, ArrowUp/Down, Escape) handlers to `#tagSearch`.
 */
export function initSearchEvents() {
  const searchInput = select('#tagSearch');
  const suggestList = select('#tagSuggest');

  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimerId);
    clearTimeout(filterTimerId);
    const { activeSegment } = splitCommaInput(searchInput.value);
    if (state.suggestionCache.has(activeSegment)) {
      updateSuggestions(); // instant dropdown update when segment is already cached
    } else {
      searchTimerId = setTimeout(updateSuggestions, 50);
    }
    filterTimerId = setTimeout(applyLiveSearchFilter, 120);
  });
  searchInput.addEventListener('focus', updateSuggestions); // empty segment lists popular tags
  searchInput.addEventListener('blur', () => {
    suggestList.classList.remove('open');
    searchInput.setAttribute('aria-expanded', 'false');
  });
  searchInput.addEventListener('keydown', event => {
    if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && suggestions.length) {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      highlightedIndex = (highlightedIndex + step + suggestions.length) % suggestions.length;
      drawSuggestions();
    } else if (event.key === 'Tab' && suggestions.length) {
      // Tab autofills the highlighted suggestion with a trailing comma and opens predictions for the next segment.
      event.preventDefault();
      chooseSuggestion(highlightedIndex >= 0 ? highlightedIndex : 0);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      clearTimeout(filterTimerId);
      applyLiveSearchFilter();
      suggestList.classList.remove('open');
      searchInput.setAttribute('aria-expanded', 'false');
    } else if (event.key === 'Escape') {
      searchInput.value = '';
      suggestions = [];
      drawSuggestions();
      applyLiveSearchFilter();
      searchInput.blur();
    }
  });
}
