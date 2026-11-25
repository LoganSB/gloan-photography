"""Command-line entry point: flags, owner password and Flask / Gunicorn server start-up."""

import argparse
import contextlib
import logging
import os
from pathlib import Path
import signal
import ssl
from typing import Any

from werkzeug.serving import make_server

from gloan.auth import Auth
from gloan.config import DATA, LOGGER, PHOTO_DIR
from gloan.handler import Handler, app
from gloan.storage import scan_folder


def stop_on_sigterm(signum: int, frame: Any) -> None:
    """Raise KeyboardInterrupt so the server loop unwinds cleanly on SIGTERM (e.g. Cloud Run shutdown)."""
    raise KeyboardInterrupt


def resolve_ssl_files(
    cert_path: str | None,
    key_path: str | None,
    auto_dirs: tuple[Path, ...] = (),
) -> tuple[str, str] | None:
    """Validate optional TLS certificate and private key paths for direct HTTPS serving.

    Returns ``(cert_path, key_path)`` when both are configured and exist on disk, ``None`` when neither
    is set (plain HTTP), or raises ``ValueError`` for partial/missing explicit configuration.
    When neither explicit path is set and ``auto_dirs`` is provided, checks each directory for a matching
    ``cert.pem`` + ``key.pem`` pair (used by local Docker debug so F5 enables HTTPS automatically when certs exist).
    """
    cert = (cert_path or '').strip()
    key = (key_path or '').strip()
    if not cert and not key:
        for directory in auto_dirs:
            auto_cert = directory / 'cert.pem'
            auto_key = directory / 'key.pem'
            if auto_cert.is_file() and auto_key.is_file():
                return str(auto_cert), str(auto_key)
        return None
    if not cert or not key:
        raise ValueError('Both --ssl-cert and --ssl-key (or GLOAN_SSL_CERT and GLOAN_SSL_KEY) must be set together')
    if not Path(cert).is_file():
        raise ValueError(f'SSL certificate file not found: {cert}')
    if not Path(key).is_file():
        raise ValueError(f'SSL private key file not found: {key}')
    return cert, key


def build_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """Create a TLS 1.2+ server SSLContext (disabling legacy TLS 1.0/1.1) for direct HTTPS serving."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return ctx


def _run_with_gunicorn(host: str, port: int, ssl_files: tuple[str, str] | None = None) -> bool:
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

    options: dict[str, Any] = {
        'bind': f'{host}:{port}',
        'workers': 1,
        'threads': 8,
        'timeout': 30,
        'graceful_timeout': 15,
        'keepalive': 2,
        'max_requests': 1000,
        'max_requests_jitter': 100,
        'limit_request_line': 4096,
        'limit_request_fields': 64,
        'limit_request_field_size': 8190,
        'accesslog': '-',
    }
    if ssl_files:
        options['certfile'], options['keyfile'] = ssl_files

        def _tls12_ssl_context(_conf: Any, default_factory: Any) -> ssl.SSLContext:
            context = default_factory()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            return context

        options['ssl_context'] = _tls12_ssl_context
    StandaloneGunicornApp(app, options).run()
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
    parser.add_argument('--ssl-cert', default=os.environ.get('GLOAN_SSL_CERT', '').strip() or None, metavar='PATH',
                        help='path to a PEM TLS certificate file to serve HTTPS directly (also GLOAN_SSL_CERT)')
    parser.add_argument('--ssl-key', default=os.environ.get('GLOAN_SSL_KEY', '').strip() or None, metavar='PATH',
                        help='path to the matching PEM TLS private key file (also GLOAN_SSL_KEY)')
    parser.add_argument('--https-redirect', action='store_true',
                        default=os.environ.get('GLOAN_HTTPS_REDIRECT', '').strip().lower() in ('1', 'true', 'yes'),
                        help='redirect plain-HTTP requests to HTTPS when a trusted proxy sends X-Forwarded-Proto: http')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

    auto_ssl_dirs = (Path('/certs'), Path('certs')) if os.environ.get('GLOAN_SSL_AUTO', '').strip().lower() in ('1', 'true', 'yes') else ()
    try:
        ssl_files = resolve_ssl_files(args.ssl_cert, args.ssl_key, auto_dirs=auto_ssl_dirs)
    except ValueError as exc:
        parser.error(str(exc))

    Handler.proxy_header = args.proxy_header
    Handler.proxy_hops = max(1, args.proxy_hops)
    Handler.proxy_any_peer = args.proxy_any_peer
    Handler.https_redirect = args.https_redirect
    Handler.auth = Auth()
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    adopted = scan_folder()
    if adopted:
        LOGGER.info('Added %d photo(s) found in %s', adopted, PHOTO_DIR)

    scheme = 'https' if ssl_files else 'http'
    LOGGER.info('Gloan on %s://%s:%s (data: %s)', scheme, args.host, args.port, DATA)

    if not _run_with_gunicorn(args.host, args.port, ssl_files=ssl_files):
        ssl_ctx = build_ssl_context(ssl_files[0], ssl_files[1]) if ssl_files else None
        server = make_server(args.host, args.port, app, threaded=True, ssl_context=ssl_ctx)
        signal.signal(signal.SIGTERM, stop_on_sigterm)
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()
