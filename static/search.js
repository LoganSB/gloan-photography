/**
 * Tag search combobox and typo-tolerant autocomplete dropdown backed by GET /api/tags.
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
let searchRunId = 0;

/**
 * Query GET /api/tags (or the per-page suggestionCache) and update the autocomplete list.
 */
export async function updateSuggestions() {
  const searchInput = select('#tagSearch');
  const query = searchInput.value.trim();
  const runId = ++searchRunId;
  let found = state.suggestionCache.get(query);
  if (!found) {
    try {
      found = (await API.tags(query)).suggestions;
    } catch (error) {
      return; // suggestions are optional; stay quiet on transient network errors
    }
    state.suggestionCache.set(query, found);
  }
  if (runId !== searchRunId) return; // user kept typing; a newer lookup owns the dropdown
  suggestions = found.filter(item => !state.activeTags.has(item.tag)); // hide tags already active
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
 * Add the suggestion at `index` to the active tag filters and refresh the gallery.
 */
export function chooseSuggestion(index) {
  const item = suggestions[index];
  if (!item) return;
  const searchInput = select('#tagSearch');
  state.activeTags.add(item.tag);
  state.page = 0;
  searchInput.value = '';
  suggestions = [];
  drawSuggestions();
  refresh();
}

/**
 * Attach input, focus, blur and keyboard navigation handlers to the tag search box.
 */
export function initSearchEvents() {
  const searchInput = select('#tagSearch');
  const suggestList = select('#tagSuggest');

  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimerId);
    const query = searchInput.value.trim();
    if (state.suggestionCache.has(query)) {
      updateSuggestions(); // instant dropdown update when query is already cached
    } else {
      searchTimerId = setTimeout(updateSuggestions, 50); // short debounce for uncached keystrokes
    }
  });
  searchInput.addEventListener('focus', updateSuggestions); // empty query lists most-used tags
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
    } else if (event.key === 'Enter') {
      event.preventDefault();
      chooseSuggestion(highlightedIndex);
    } else if (event.key === 'Escape') {
      searchInput.value = '';
      suggestions = [];
      drawSuggestions();
      searchInput.blur();
    }
  });
}
