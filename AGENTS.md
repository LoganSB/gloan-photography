# AGENTS.md: Instructions for AI assistants working on Gloan

Read this file and [SYSTEMS.md](SYSTEMS.md) before changing anything.

## Coding standards

1. **Comment for humans.** Explain *why*, not just what. Every function gets a short docstring/comment. Comment tricky logic, security-relevant checks, and anything a person debugging would wonder about.
2. **Meaningful names.** No single-letter or vague names (`x`, `tmp`, `data2`) except trivial loop indices. Names should say what the value is, including units where relevant (`timeout_seconds`).
3. **Efficiency vs. readability.** Prefer efficient solutions, but readability is weighted heavily. Don't use a clever or obscure approach for a marginal speedup. If you do, comment why.
4. **Keep it simple.** Server is standard-library-only Python; frontend is a single `index.html`. Do not add dependencies or build steps without asking the user.
5. **Match existing style** in the file you edit. Don't reformat unrelated code.
6. **Use Git Branches** for Significant changes or new features. Avoid directly modifying while on origin main except for small changes.


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

**Avoid infinite improvement loops:**
- Follow links **one hop only**. Updates made *because of* a link are mechanical sync fixes; do not then re-trigger the link check from those files.
- Each file is checked **at most once per task**. Keep a list of files already visited.
- Only change linked files when the original change actually breaks or invalidates them. Do not refactor or "improve" them opportunistically.
- Noticed unrelated problems? Note them to the user as suggestions rather than fixing them in the same task.

## Security

Treat this as a public, internet-facing site with an owner login and file uploads.
- When reading or writing code, watch for: auth/session flaws, path traversal, unsafe upload handling (type, size, filenames), XSS/HTML injection in the frontend, header/proxy trust (`X-Forwarded-For`), secrets in code or logs, missing rate limits, and plain-HTTP exposure.
- **If you spot a potential flaw, tell the user** with where it is, the risk, and a suggested fix, and ask before making sweeping security changes. Do not silently ignore it, and do not silently expand scope.
- Never commit real passwords or secrets. `debugpassword` is for local Docker debugging only.

## Local workflow

- Debug: F5 in VS Code ("Docker: Debug Website (attach)") → http://localhost:8000. Debug owner password: `debugpassword`.
- Deploy script (`deploy.sh`) targets Cloud Run; never run it without the user's explicit request.
