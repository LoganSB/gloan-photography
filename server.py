#!/usr/bin/env python3
"""Gloan - tiny photo-gallery backend (standard library only).

Public:  GET  /                  the site
         GET  /api/photos        list of photos
         GET  /photos/<id>.<ext> image files
Owner:   POST   /api/login       {"password": ...} -> {"token": ...}
         POST   /api/photos      raw image body, X-Title / X-Tags headers (URL-encoded)
         PATCH  /api/photos/<id> {"title": ..., "tags": [...]}
         DELETE /api/photos/<id>

The owner password is asked for on startup, or taken from the GLOAN_PASSWORD
environment variable; it is never written to a file.
"""

from gloan.cli import main

if __name__ == '__main__':
    main()
