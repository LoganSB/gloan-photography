# AGENTS.md: Instructions for AI assistants working on Gloan

Read this file and [SYSTEMS.md](SYSTEMS.md) before changing anything.

## Coding standards

1. **Comment for humans.** Explain *why*, not just what. Every function gets a short docstring/comment. Comment tricky logic, security-relevant checks, and anything a person debugging would wonder about.
2. **Meaningful names.** No single-letter or vague names (`x`, `tmp`, `data2`) except trivial loop indices. Names should say what the value is, including units where relevant (`timeout_seconds`).
3. **Efficiency vs. readability.** Prefer efficient solutions, but readability is weighted heavily. Don't use a clever or obscure approach for a marginal speedup. If you do, comment why.
4. **Keep it simple.** The web server uses Flask + Gunicorn (`requirements.txt`); Pillow is used only by `tools/make_thumbnails.py`; the frontend is `index.html` + `static/app.css` + native ES modules in `static/*.js` (no bundler or build step). Do not add dependencies or build steps without asking the user.
5. **Match existing style** in the file you edit. Don't reformat unrelated code.
6. **Use Git Branches** for Significant changes or new features. Avoid directly modifying while on origin main except for small changes.
7. **No emojis without asking.** Do not use emojis or decorative icon symbols in UI text, buttons, badges, or messages — only the slideshow play/pause symbol (`▶` / `❚❚`) is permitted. Whenever adding or changing UI text where an emoji might be used, prompt the user first to ask whether they want an emoji or plain text.

## Tool & language review (every request)

For each request, briefly consider whether another language, framework, library or tool would make the result more robust than extending the current stack, e.g. Flask (+ gunicorn) for the server, native ES modules or a small JS library for the frontend, SQLite, Playwright for browser tests.
- Weigh robustness, security and readability against new dependencies, build steps and deploy changes (Cloud Run, one instance, data on a Cloud Storage FUSE mount).
- State the conclusion in the response (one or two sentences is enough when the current stack is the right fit).
- Only adopt something that adds a dependency, a build step or a rewrite after the user approves it; otherwise implement within the current stack and list the alternative as a suggestion.

Current decisions (from the Nov 2026 project review; update when the user decides):
- **Approved & implemented:** Flask + gunicorn (1 worker, 8 threads) replacing `http.server`; `index.html` split into `static/app.css` + native ES modules (`static/*.js`, no build step) with `Content-Security-Policy: script-src 'self'`; in-memory, `mtime`/generation-keyed `RowsCache` of `photos.json` rows and file sizes in `gloan/storage.py`.
- **Rejected:** Generating thumbnails automatically at upload (owner runs thumbnail generation locally on demand to avoid Cloud Run CPU charges); jQuery (modern DOM APIs already cover it); React/Vue/Svelte or any bundler (build step not worth it at this size); SQLite as the store while data lives on Cloud Storage FUSE (no reliable file locking there).


## Interconnected systems (see [SYSTEMS.md](SYSTEMS.md))

SYSTEMS.md maps how files and components depend on each other (API contract, env vars, Docker, deploy, docs).

For every code change:
1. Look up the changed component in SYSTEMS.md and check each system linked to it.
2. Update linked systems that the change affects (e.g. new endpoint → frontend `API` object, README API table, SYSTEMS.md).
3. Test the change and the affected linked systems (see **Testing & verification (required after every change)** below), and report what was and wasn't tested.
4. If you add, remove, or rename a component or link, update SYSTEMS.md in the same change.

## Testing & verification (required after every change)

Never consider a change complete until all of the following pass:
1. **Unit & syntax test suite (`tests/test_gloan.py`):**
   - Run `python3 -m unittest discover -s tests -v`.
   - Every new feature, route, bug fix, or frontend change MUST have unit test coverage in `tests/test_gloan.py`.
   - `FrontendSyntaxTest` validates the `<script>` block inside `index.html` (catching unescaped newlines in string literals, unbalanced braces/quotes, and running `node --check` when Node is available) so a JS syntax error can never silently break all page scripts and buttons.
2. **Full Docker build & runtime check:**
   - Bring up the debug container (`docker compose -f docker-compose.debug.yml up -d --build`), run the unit test suite *inside* the container (`docker exec gloan-debug python3 -m unittest discover -s tests -v`, which exercises Pillow and `tools/make_thumbnails.py`), and verify the live HTTP endpoints (`GET /`, `GET /api/session`, `GET /api/photos`) return `200`.
   - Verify the production image builds cleanly (`docker build -t gloan-test .`).
3. **Synthetic & randomly generated test data for troubleshooting:**
   - When building or troubleshooting search, filtering, pagination, sorting, or storage behavior, generate realistic randomized/combinatorial test datasets inside `tests/test_gloan.py` (or a scratch directory under `TEST_DATA_DIR`) to exercise edge cases, multi-tag combinations, wildcards, typos, and pagination boundaries.
   - Keep generated datasets reasonable in size (e.g. tens to a few hundred synthetic photos/rows and tiny valid image headers, never gigabytes or terabytes of files/text) and always clean up temporary test directories in `tearDown` / `tearDownModule`.

**Avoid infinite improvement loops:**
- Follow links **one hop only**. Updates made *because of* a link are mechanical sync fixes; do not then re-trigger the link check from those files.
- Each file is checked **at most once per task**. Keep a list of files already visited.
- Only change linked files when the original change actually breaks or invalidates them. Do not refactor or "improve" them opportunistically.
- Noticed unrelated problems? Note them to the user as suggestions rather than fixing them in the same task.

## Security

Treat this as a public, internet-facing site with an Admin login and file uploads.
- When reading or writing code, watch for: auth/session flaws, path traversal, unsafe upload handling (type, size, filenames), XSS/HTML injection in the frontend, header/proxy trust (`X-Forwarded-For`), secrets in code or logs, missing rate limits, and plain-HTTP exposure.
- **If you spot a potential flaw, tell the user** with where it is, the risk, and a suggested fix, and ask before making sweeping security changes. Do not silently ignore it, and do not silently expand scope.
- Never commit real passwords or secrets. `debugpassword` is for local Docker debugging only.

## Local workflow

- Debug: F5 in VS Code ("Docker: Debug Website (attach)") → http://localhost:8000. Debug owner password: `debugpassword`.
- Deploy script (`deploy.sh`) targets Cloud Run; never run it without the user's explicit request.
