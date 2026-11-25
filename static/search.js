/**
 * Reusable comma-delimited predictive tag autocomplete backed by GET /api/tags, plus header live-search wiring.
 *
 * Behavior (shared by the header search bar, the photo Manage/Suggest Tags modal, and the upload tags input):
 * - Predictive autocomplete always suggests completions for the active (last) comma-delimited segment.
 * - Pressing Tab (or clicking a suggestion) autocompletes the current segment with `<tag>, ` (trailing comma)
 *   and immediately re-opens predictive suggestions for the next comma-delimited segment.
 */

import { API, createElement, select } from './api.js';
import { refresh, state } from './gallery.js';

// Labels shown next to looser matches in the dropdown so they don't surprise the user.
export const MATCH_NOTES = {
  fuzzy: 'did you mean?',
  wildcard: 'pattern',
};

/**
 * Split a comma-delimited input string into `{ completedTerms, activeSegment }`.
 * For example, `"bryce_fall, win"` -> `{ completedTerms: ["bryce_fall"], activeSegment: "win" }`.
 */
export function splitCommaInput(rawValue) {
  const parts = String(rawValue || '').split(',');
  const activeSegment = parts.pop().trim();
  const completedTerms = parts.map(part => part.trim()).filter(Boolean);
  return { completedTerms, activeSegment };
}

/**
 * Attach comma-delimited predictive tag autocomplete to any `(inputEl, listEl)` pair.
 * Reused across the header search box, the Manage/Suggest Tags modal, and the upload modal.
 */
export function attachTagAutocomplete({
  inputEl,
  listEl,
  getExcludedTags = () => [],
  onInput = null,
  onSelect = null,
  onEnter = null,
  onEscape = null,
  debounceMs = 50,
}) {
  if (!inputEl || !listEl) return { update: () => {}, close: () => {}, reset: () => {} };

  let suggestions = [];
  let highlightedIndex = -1;
  let searchTimerId = null;
  let searchRunId = 0;

  const close = () => {
    listEl.classList.remove('open');
    inputEl.setAttribute('aria-expanded', 'false');
  };

  const draw = () => {
    listEl.innerHTML = '';
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
        choose(index);
      };
      listEl.append(row);
    });
    const isOpen = suggestions.length > 0 && document.activeElement === inputEl;
    listEl.classList.toggle('open', isOpen);
    inputEl.setAttribute('aria-expanded', String(isOpen));
  };

  const update = async () => {
    const { completedTerms, activeSegment } = splitCommaInput(inputEl.value);
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
    const excluded = new Set([
      ...getExcludedTags().map(tag => String(tag).toLowerCase()),
      ...completedTerms.map(term => term.toLowerCase()),
    ]);
    suggestions = found.filter(item => !excluded.has(item.tag.toLowerCase()));
    highlightedIndex = suggestions.length ? 0 : -1;
    draw();
  };

  const choose = index => {
    const item = suggestions[index];
    if (!item) return;
    const { completedTerms } = splitCommaInput(inputEl.value);
    completedTerms.push(item.tag);
    inputEl.value = completedTerms.join(', ') + ', ';
    inputEl.focus();
    if (onSelect) onSelect(item.tag);
    update();
  };

  const reset = () => {
    clearTimeout(searchTimerId);
    suggestions = [];
    highlightedIndex = -1;
    draw();
  };

  inputEl.addEventListener('input', () => {
    clearTimeout(searchTimerId);
    const { activeSegment } = splitCommaInput(inputEl.value);
    if (state.suggestionCache.has(activeSegment)) {
      update(); // instant dropdown update when segment is already cached
    } else {
      searchTimerId = setTimeout(update, debounceMs);
    }
    if (onInput) onInput();
  });
  inputEl.addEventListener('focus', update);
  inputEl.addEventListener('blur', close);
  inputEl.addEventListener('keydown', event => {
    if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && suggestions.length) {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      highlightedIndex = (highlightedIndex + step + suggestions.length) % suggestions.length;
      draw();
    } else if (event.key === 'Tab' && suggestions.length) {
      // Tab autofills the highlighted suggestion with a trailing comma and opens predictions for the next segment.
      event.preventDefault();
      choose(highlightedIndex >= 0 ? highlightedIndex : 0);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      close();
      if (onEnter) onEnter();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      if (listEl.classList.contains('open')) {
        reset();
      } else if (onEscape) {
        onEscape();
      }
    }
  });

  return { update, close, reset };
}

/**
 * Sync the header search bar's comma-separated query into `state.searchQuery` and refresh the gallery (AND matching).
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
 * Attach live-filtering tag autocomplete to the header `#tagSearch` combobox.
 */
export function initSearchEvents() {
  const searchInput = select('#tagSearch');
  const suggestList = select('#tagSuggest');
  let filterTimerId = null;

  attachTagAutocomplete({
    inputEl: searchInput,
    listEl: suggestList,
    getExcludedTags: () => [...state.activeTags],
    onInput: () => {
      clearTimeout(filterTimerId);
      filterTimerId = setTimeout(applyLiveSearchFilter, 120);
    },
    onSelect: () => {
      clearTimeout(filterTimerId);
      applyLiveSearchFilter();
    },
    onEnter: () => {
      clearTimeout(filterTimerId);
      applyLiveSearchFilter();
    },
    onEscape: () => {
      clearTimeout(filterTimerId);
      searchInput.value = '';
      applyLiveSearchFilter();
      searchInput.blur();
    },
  });
}
