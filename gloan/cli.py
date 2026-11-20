"""Command-line entry point: flags, owner password and server start-up."""

import argparse
import contextlib
import getpass
import ipaddress
import logging
import os
import signal
import sys
from http.server import ThreadingHTTPServer
from typing import Any

from gloan.auth import Auth, normalize_ip
from gloan.config import DATA, LOGGER, MIN_PASSWORD, PHOTO_DIR
from gloan.handler import Handler
from gloan.storage import scan_folder


def get_password() -> str:
    """Owner password from GLOAN_PASSWORD if set, otherwise prompted for on the terminal (not echoed)."""
    password = os.environ.get('GLOAN_PASSWORD', '')
    while len(password) < MIN_PASSWORD:
        if not sys.stdin.isatty():
            sys.exit(f'Set GLOAN_PASSWORD to a password of at least {MIN_PASSWORD} characters.')
        if password:
            print(f'That was only {len(password)} characters - please use at least {MIN_PASSWORD}.')
        password = getpass.getpass('Choose an owner password (you will use it to log in on the site): ')
    return password


def stop_on_sigterm(signum: int, frame: Any) -> None:
    """Raise KeyboardInterrupt so serve_forever() unwinds the same way as for Ctrl+C."""
    raise KeyboardInterrupt


def main() -> None:
    """Parse flags, require the owner password, and serve until interrupted."""
    parser = argparse.ArgumentParser(description='Gloan photo gallery server')
    # Loopback by default: exposing the site is a deliberate choice (--host 0.0.0.0 or a reverse proxy).
    parser.add_argument('--host', default='127.0.0.1')
    # Cloud Run (and most PaaS) hand the listening port over in $PORT.
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8000')))
    parser.add_argument('--owner-ip', action='append', metavar='IP',
                        help='only allow owner login from this address; repeatable (default: any address)')
    parser.add_argument('--proxy-header', metavar='NAME',
                        help='trust this header (e.g. X-Forwarded-For) for the client IP when behind a local proxy')
    parser.add_argument('--proxy-hops', type=int, default=1, metavar='N',
                        help='trusted proxies in front of us; the Nth entry from the right of the header is the client')
    parser.add_argument('--proxy-any-peer', action='store_true',
                        help='trust the proxy header from any peer, not just loopback (only if the container is '
                             'reachable solely through a managed proxy such as Cloud Run)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

    owner_ips = [normalize_ip(ip) for ip in (args.owner_ip or [])]
    for ip in owner_ips:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            sys.exit(f'--owner-ip {ip!r} is not a valid IP address.')
    Handler.owner_ips = frozenset(owner_ips)
    Handler.proxy_header = args.proxy_header
    Handler.proxy_hops = max(1, args.proxy_hops)
    Handler.proxy_any_peer = args.proxy_any_peer
    Handler.auth = Auth(get_password())
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    adopted = scan_folder()
    if adopted:
        LOGGER.info('Added %d photo(s) found in %s', adopted, PHOTO_DIR)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    LOGGER.info('Gloan on http://%s:%s (data: %s)', args.host, args.port, DATA)
    if Handler.owner_ips:
        LOGGER.info('Owner login allowed only from: %s', ', '.join(sorted(Handler.owner_ips)))
    # Cloud Run stops instances with SIGTERM; turn it into the same clean exit as Ctrl+C.
    signal.signal(signal.SIGTERM, stop_on_sigterm)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
