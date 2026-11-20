#!/usr/bin/env python3
"""Gloan - photo-gallery backend (Flask + Gunicorn).

Public:  GET  /                  the site
         GET  /static/<file>     CSS and native ES modules
         GET  /api/photos        list of photos
         GET  /api/tags          tag search suggestions
         GET  /photos/<id>.<ext> image files
         GET  /thumbs/<id>.jpg   thumbnail tiles
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
