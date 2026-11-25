/**
 * Photo upload staging modal, client-side canvas downscaling, drag-and-drop and procedural demo images.
 */

import { API, createElement, escapeHtml, isOwner, parseTags, select, setOverlayOpen, toast, withBusy } from './api.js';
import { refresh, state, suggestionChips } from './gallery.js';
import { attachTagAutocomplete } from './search.js';

let stagedFiles = [];

/**
 * Open the upload modal and render any currently staged files.
 */
export function openUpload() {
  setOverlayOpen('#upModal', true);
  renderStaged();
}

/**
 * Close the upload modal and release all object-URL preview blobs.
 */
export function closeUpload() {
  setOverlayOpen('#upModal', false);
  stagedFiles.forEach(item => URL.revokeObjectURL(item.preview));
  stagedFiles = [];
  select('#allTags').value = '';
}

/**
 * Stage one or more user-selected or dropped File objects for upload.
 */
export function stageFiles(fileList) {
  if (!isOwner()) return;
  const images = [...fileList].filter(file => file.type.startsWith('image/'));
  if (!images.length) {
    toast('No image files found');
    return;
  }
  images.forEach(file => {
    stagedFiles.push({
      file,
      preview: URL.createObjectURL(file),
      title: file.name.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' '),
      tags: '',
    });
  });
  openUpload();
}

/**
 * Render the list of staged images with per-photo title and tag inputs inside the upload modal.
 */
export function renderStaged() {
  const stagedBox = select('#staged');
  stagedBox.innerHTML = '';
  stagedFiles.forEach(item => {
    const row = createElement('div', 'stage');
    row.innerHTML = `<img src="${escapeHtml(item.preview)}"><div class="f"><input class="field" type="search" autocomplete="off" data-lpignore="true" data-1p-ignore="true" data-bwignore="true" data-form-type="other" value="${escapeHtml(item.title)}" placeholder="Title"><input class="field" type="search" autocomplete="off" data-lpignore="true" data-1p-ignore="true" data-bwignore="true" data-form-type="other" value="${escapeHtml(item.tags)}" placeholder="Tags for this photo (optional)"></div>`;
    const [titleInput, tagsInput] = row.querySelectorAll('input');
    titleInput.oninput = () => { item.title = titleInput.value; };
    tagsInput.oninput = () => { item.tags = tagsInput.value; };
    stagedBox.append(row);
  });
  select('#saveUp').disabled = !stagedFiles.length;
  select('#stCount').textContent = stagedFiles.length ? `${stagedFiles.length} selected` : '';
  suggestionChips(select('#sugAll'), select('#allTags'));
}

/**
 * Downscale large images on a canvas to `maxEdgePx` on the longest side before uploading.
 */
async function resizeImage(file, maxEdgePx = 2400) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, maxEdgePx / Math.max(bitmap.width, bitmap.height));
  if (scale === 1 && file.size < 2.5e6) return file;
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  return new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.9));
}

/**
 * Upload all staged photos sequentially, updating the progress bar.
 * Note: Does NOT trigger server-side thumbnail generation automatically so Cloud Run CPU time is never spent
 * unexpectedly on upload; thumbnails are generated on the local computer or via the explicit footer link.
 */
export async function saveStaged() {
  await withBusy(select('#saveUp'), 'Uploading…', async () => {
    const sharedTags = parseTags(select('#allTags').value);
    const progressEl = select('#prog');
    progressEl.style.display = 'block';
    let completed = 0;
    for (const item of stagedFiles) {
      try {
        const blob = await resizeImage(item.file);
        const mergedTags = [...new Set([...sharedTags, ...parseTags(item.tags)])];
        await API.upload(blob, item.title.trim(), mergedTags);
      } catch (error) {
        console.error(error);
        toast(`${item.file.name}: ${error.message}`);
      }
      progressEl.firstElementChild.style.width = (++completed / stagedFiles.length * 100) + '%';
    }
    state.page = 0;
    await refresh(); // newest first lands on page 1
    progressEl.style.display = 'none';
    progressEl.firstElementChild.style.width = '0';
    const count = stagedFiles.length;
    closeUpload();
    toast(`Added ${count} photo${count > 1 ? 's' : ''}`);
  });
}

/**
 * Deterministic pseudo-random number generator for procedural sample images.
 */
function seededRandom(seed) {
  let stateValue = seed;
  return () => {
    stateValue = Math.imul(stateValue ^ (stateValue >>> 15), stateValue | 1);
    stateValue ^= stateValue + Math.imul(stateValue ^ (stateValue >>> 7), stateValue | 61);
    return ((stateValue ^ (stateValue >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Paint a procedural layered mountain landscape onto a canvas for the sample gallery option.
 */
function generateLandscapeCanvas(seed, palette) {
  const rand = seededRandom(seed);
  const width = 1800;
  const height = 1200;
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  const skyGrad = ctx.createLinearGradient(0, 0, 0, height);
  palette.sky.forEach((color, idx) => skyGrad.addColorStop(idx / (palette.sky.length - 1), color));
  ctx.fillStyle = skyGrad;
  ctx.fillRect(0, 0, width, height);

  const sunX = width * (0.25 + rand() * 0.5);
  const sunY = height * (0.28 + rand() * 0.15);
  const sunGlow = ctx.createRadialGradient(sunX, sunY, 0, sunX, sunY, 260);
  sunGlow.addColorStop(0, palette.sun);
  sunGlow.addColorStop(1, 'transparent');
  ctx.fillStyle = sunGlow;
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = palette.sun;
  ctx.beginPath();
  ctx.arc(sunX, sunY, 28, 0, 7);
  ctx.fill();

  palette.layers.forEach((color, layerIndex) => {
    const base = height * (0.5 + layerIndex * 0.11);
    const amplitude = 170 - layerIndex * 28;
    const freq = 0.002 + rand() * 0.002;
    const phase = rand() * 100;
    ctx.beginPath();
    ctx.moveTo(0, height);
    for (let xPos = 0; xPos <= width; xPos += 6) {
      const wave = Math.sin(xPos * freq + phase) * 0.5
        + Math.sin(xPos * freq * 2.3 + phase * 2) * 0.3
        + Math.sin(xPos * freq * 5.1 + phase) * 0.12;
      const yPos = base - amplitude * wave - amplitude * 0.4;
      ctx.lineTo(xPos, yPos);
    }
    ctx.lineTo(width, height);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
  });

  const mistGrad = ctx.createLinearGradient(0, height * 0.7, 0, height);
  mistGrad.addColorStop(0, 'rgba(255,255,255,0)');
  mistGrad.addColorStop(1, 'rgba(0,0,0,.35)');
  ctx.fillStyle = mistGrad;
  ctx.fillRect(0, 0, width, height);
  return canvas;
}

/**
 * Paint a procedural macro flower study onto a canvas for the sample gallery option.
 */
function generateMacroCanvas(seed, hue, petals) {
  const rand = seededRandom(seed);
  const width = 1400;
  const height = (1400 * (rand() > 0.5 ? 1.25 : 0.8)) | 0;
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  const bgGrad = ctx.createRadialGradient(width / 2, height / 2, 50, width / 2, height / 2, width);
  bgGrad.addColorStop(0, `hsl(${hue},30%,22%)`);
  bgGrad.addColorStop(1, `hsl(${hue + 20},35%,6%)`);
  ctx.fillStyle = bgGrad;
  ctx.fillRect(0, 0, width, height);

  for (let idx = 0; idx < 46; idx++) {
    const bokehX = rand() * width;
    const bokehY = rand() * height;
    const bokehR = 20 + rand() * 90;
    const bokehGrad = ctx.createRadialGradient(bokehX, bokehY, bokehR * 0.6, bokehX, bokehY, bokehR);
    bokehGrad.addColorStop(0, `hsla(${hue + rand() * 60},70%,65%,.0)`);
    bokehGrad.addColorStop(0.85, `hsla(${hue + rand() * 60},70%,65%,.22)`);
    bokehGrad.addColorStop(1, 'transparent');
    ctx.fillStyle = bokehGrad;
    ctx.beginPath();
    ctx.arc(bokehX, bokehY, bokehR, 0, 7);
    ctx.fill();
  }

  const centerX = width / 2;
  const centerY = height / 2;
  for (let ring = 0; ring < 3; ring++) {
    for (let idx = 0; idx < petals; idx++) {
      const angle = (idx / petals) * Math.PI * 2 + ring * 0.35;
      const length = 300 - ring * 70;
      const petalWidth = 70 - ring * 10;
      ctx.save();
      ctx.translate(centerX, centerY);
      ctx.rotate(angle);
      const petalGrad = ctx.createLinearGradient(0, 0, length, 0);
      petalGrad.addColorStop(0, `hsl(${hue + 40},80%,${30 + ring * 14}%)`);
      petalGrad.addColorStop(1, `hsl(${hue},85%,${62 + ring * 8}%)`);
      ctx.fillStyle = petalGrad;
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.bezierCurveTo(length * 0.3, -petalWidth, length * 0.8, -petalWidth * 0.8, length, 0);
      ctx.bezierCurveTo(length * 0.8, petalWidth * 0.8, length * 0.3, petalWidth, 0, 0);
      ctx.fill();
      ctx.restore();
    }
  }

  const coreGrad = ctx.createRadialGradient(centerX, centerY, 0, centerX, centerY, 50);
  coreGrad.addColorStop(0, '#ffe9a8');
  coreGrad.addColorStop(1, '#b8741a');
  ctx.fillStyle = coreGrad;
  ctx.beginPath();
  ctx.arc(centerX, centerY, 48, 0, 7);
  ctx.fill();

  for (let idx = 0; idx < 14; idx++) {
    const dropX = centerX + (rand() - 0.5) * 600;
    const dropY = centerY + (rand() - 0.5) * 600;
    const dropR = 6 + rand() * 14;
    const dropGrad = ctx.createRadialGradient(dropX - dropR * 0.3, dropY - dropR * 0.3, 1, dropX, dropY, dropR);
    dropGrad.addColorStop(0, 'rgba(255,255,255,.95)');
    dropGrad.addColorStop(0.5, 'rgba(190,220,255,.35)');
    dropGrad.addColorStop(1, 'rgba(255,255,255,.05)');
    ctx.fillStyle = dropGrad;
    ctx.beginPath();
    ctx.arc(dropX, dropY, dropR, 0, 7);
    ctx.fill();
  }
  return canvas;
}

/**
 * Generate and upload a set of sample landscape and macro images when the gallery is empty.
 */
export async function loadDemo() {
  await withBusy(select('#e2'), 'Generating samples…', async () => {
    const sampleSpecs = [
      ['Alpine Dawn', ['mountain', 'sunrise'], generateLandscapeCanvas(3, { sky: ['#2c3a5a', '#d98a6c', '#f6d8a8'], sun: '#fff1d0', layers: ['#5d5a74', '#443f58', '#2f2b40', '#1c1a28'] })],
      ['Blue Hour Ridge', ['mountain', 'dusk'], generateLandscapeCanvas(8, { sky: ['#0f1a33', '#3b5a8c', '#9fb6d8'], sun: '#e8f0ff', layers: ['#4c6285', '#364764', '#243049', '#141b2c'] })],
      ['Desert Light', ['desert', 'sunset'], generateLandscapeCanvas(14, { sky: ['#7a3a46', '#e07a4f', '#f7c97a'], sun: '#fff0c0', layers: ['#a65a3d', '#833f2f', '#5c2a24', '#331816'] })],
      ['Misty Valley', ['forest', 'fog'], generateLandscapeCanvas(21, { sky: ['#8fa3a8', '#c8d3d3', '#eef0ec'], sun: '#ffffff', layers: ['#9db0b0', '#738c8c', '#4d6666', '#2e4444'] })],
      ['Coastal Evening', ['coast', 'sunset'], generateLandscapeCanvas(33, { sky: ['#352a52', '#b5578a', '#f6a97a'], sun: '#ffe2b8', layers: ['#6a4676', '#4b3257', '#33223e', '#1d1426'] })],
      ['Dahlia Study', ['flower', 'macro'], generateMacroCanvas(5, 330, 18)],
      ['Dewdrop Bloom', ['flower', 'macro', 'water'], generateMacroCanvas(11, 200, 14)],
      ['Golden Petals', ['flower', 'macro'], generateMacroCanvas(17, 35, 22)],
      ['Violet Detail', ['flower', 'macro', 'abstract'], generateMacroCanvas(26, 275, 12)],
    ];
    toast('Generating samples…');
    for (const [title, tags, canvas] of sampleSpecs) {
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.88));
      try {
        await API.upload(blob, title, tags);
      } catch (error) {
        toast(error.message);
        break;
      }
    }
    state.page = 0;
    await refresh();
    toast('Sample images added — delete them any time');
  });
}

/**
 * Attach DOM event listeners for the upload modal and page-wide file drag-and-drop.
 */
export function initUploadEvents() {
  select('#upBtn').onclick = openUpload;
  select('#pick').onclick = () => select('#file').click();
  select('#file').onchange = event => {
    stageFiles(event.target.files);
    event.target.value = '';
  };
  select('#saveUp').onclick = saveStaged;
  document.querySelectorAll('[data-close]').forEach(button => {
    button.onclick = closeUpload;
  });
  select('#upModal').addEventListener('mousedown', event => {
    if (event.target.id === 'upModal') closeUpload();
  });
  attachTagAutocomplete({
    inputEl: select('#allTags'),
    listEl: select('#allTagsSuggest'),
  });

  // Page-wide drag & drop upload for logged-in owner
  let dragDepth = 0;
  addEventListener('dragenter', event => {
    if (isOwner() && event.dataTransfer?.types?.includes('Files')) {
      dragDepth++;
      document.body.classList.add('dropping');
    }
  });
  addEventListener('dragleave', () => {
    if (--dragDepth <= 0) {
      dragDepth = 0;
      document.body.classList.remove('dropping');
    }
  });
  addEventListener('dragover', event => event.preventDefault());
  addEventListener('drop', event => {
    event.preventDefault();
    dragDepth = 0;
    document.body.classList.remove('dropping');
    if (event.dataTransfer.files.length) stageFiles(event.dataTransfer.files);
  });
}
