"""Command-line entry point: flags, owner password and Flask / Gunicorn server start-up."""

import argparse
import contextlib
import logging
import os
import signal
from typing import Any

from werkzeug.serving import make_server

from gloan.auth import Auth
from gloan.config import DATA, LOGGER, PHOTO_DIR
from gloan.handler import Handler, app
from gloan.storage import scan_folder


def stop_on_sigterm(signum: int, frame: Any) -> None:
    """Raise KeyboardInterrupt so the server loop unwinds cleanly on SIGTERM (e.g. Cloud Run shutdown)."""
    raise KeyboardInterrupt


def _run_with_gunicorn(host: str, port: int) -> bool:
    """Run the Flask app under Gunicorn (1 worker, 8 threads) when Gunicorn is available on Unix.

    Why 1 worker: INDEX_LOCK, the login-failure map (FAILS), the thumbnail-job state and the in-memory caches
    live in process memory, so a single multi-threaded worker keeps state consistent while serving concurrent
    requests. When running under debugpy (VS Code F5 debugging) or on Windows, returns False so the caller
    runs the single-process Werkzeug threaded server where breakpoints attach directly.
    """
    if 'debugpy' in os.environ.get('PYTHONPATH', '') or 'debugpy' in str(os.environ.get('_', '')) or os.name == 'nt':
        return False
    try:
        from gunicorn.app.base import BaseApplication  # pylint: disable=import-outside-toplevel
    except ImportError:
        return False

    class StandaloneGunicornApp(BaseApplication):
        """Embeds Gunicorn so `python3 server.py` and deploy.sh CLI flags work without a separate wrapper."""

        def __init__(self, wsgi_app: Any, options: dict[str, Any]) -> None:
            self.options = options
            self.application = wsgi_app
            super().__init__()

        def load_config(self) -> None:
            for key, value in self.options.items():
                if key in self.cfg.settings and value is not None:
                    self.cfg.set(key.lower(), value)

        def load(self) -> Any:
            return self.application

    StandaloneGunicornApp(app, {
        'bind': f'{host}:{port}',
        'workers': 1,
        'threads': 8,
        'timeout': 30,
        'keepalive': 2,
        'max_requests': 1000,
        'max_requests_jitter': 100,
        'accesslog': '-',
    }).run()
    return True


def main() -> None:
    """Parse flags and serve until interrupted."""
    parser = argparse.ArgumentParser(description='Gloan photo gallery server')
    # Loopback by default: exposing the site is a deliberate choice (--host 0.0.0.0 or a reverse proxy).
    parser.add_argument('--host', default='127.0.0.1')
    # Cloud Run (and most PaaS) hand the listening port over in $PORT.
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8000')))
    parser.add_argument('--proxy-header', metavar='NAME',
                        help='trust this header (e.g. X-Forwarded-For) for the client IP when behind a local proxy')
    parser.add_argument('--proxy-hops', type=int, default=1, metavar='N',
                        help='trusted proxies in front of us; the Nth entry from the right of the header is the client')
    parser.add_argument('--proxy-any-peer', action='store_true',
                        help='trust the proxy header from any peer, not just loopback (only if the container is '
                             'reachable solely through a managed proxy such as Cloud Run)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

    Handler.proxy_header = args.proxy_header
    Handler.proxy_hops = max(1, args.proxy_hops)
    Handler.proxy_any_peer = args.proxy_any_peer
    Handler.auth = Auth()
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    adopted = scan_folder()
    if adopted:
        LOGGER.info('Added %d photo(s) found in %s', adopted, PHOTO_DIR)

    LOGGER.info('Gloan on http://%s:%s (data: %s)', args.host, args.port, DATA)

    if not _run_with_gunicorn(args.host, args.port):
        server = make_server(args.host, args.port, app, threaded=True)
        signal.signal(signal.SIGTERM, stop_on_sigterm)
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()
