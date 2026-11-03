# Gloan

This is currently in stage one and is entirely AI generated with only minor human edits. You have been warned.

Landscape and macro photo gallery: static page (`index.html`) plus a small
standard-library Python backend (`server.py`, no installs needed).

## Run

In Terminal:

```bash
cd ~/Documents/Gloan
python3 server.py
```

It asks you to choose an owner password (8+ characters, not shown as you type).
When it prints `Gloan on http://127.0.0.1:8000`, open that address in
your browser, click **Owner login**, enter the password, and upload. Visitors
can browse, filter and play the slideshow but cannot change anything.
Stop the server with `Ctrl+C`; the password is asked for again next start.

To skip the prompt, set `GLOAN_PASSWORD` in the environment (from a password
manager, for example) before starting.

Flags: `--host 0.0.0.0` to listen on your network, `--port N`.
Env: `GLOAN_DATA` moves the storage folder (default `./data`).

## Who can log in

Anyone who knows the owner password. Login attempts are limited (5 wrong tries from one address locks it out for
a minute) and sessions expire after 12 hours, but the password is the only protection, so make it long (12+
characters).

Optionally restrict owner login to specific addresses with `--owner-ip IP` (repeatable). Then the **Owner login**
button is hidden from everyone else, the login route answers 404, and a stolen token is rejected from other
addresses. Behind a proxy, also pass `--proxy-header X-Forwarded-For`; it is trusted only from the same machine
(or from any peer with `--proxy-any-peer`), and the last entry is used.

## Data

`data/photos/` holds the images and `data/photos.json` holds each photo's title, tags and story.
Back up the `data/` folder and you have everything.

## Putting it online

The server speaks plain HTTP. Put it behind a TLS reverse proxy (Caddy, nginx,
Cloudflare Tunnel) before exposing it, or the password and token cross the
network in the clear.

## API

| Method | Path | Auth |
|---|---|---|
| GET | `/api/photos`, `/photos/<id>.<ext>`, `/api/session` | public |
| POST | `/api/login` `{"password"}` -> `{"token"}` | public (5 failures = 60s lockout) |
| POST | `/api/photos` raw image body, `X-Title` / `X-Tags` URL-encoded headers | owner |
| PATCH | `/api/photos/<id>` `{"title","tags"}` | owner |
| DELETE | `/api/photos/<id>` | owner |

Owner calls send `Authorization: Bearer <token>`; tokens last 12 hours.

## Deploying to Google Cloud Run

Cloud Run's disk is wiped whenever an instance stops, so photos and `photos.json` live in a Cloud Storage
bucket mounted at `/data`. `deploy.sh` creates the bucket, stores the owner password in Secret Manager and
deploys from the `Dockerfile`:

```bash
PROJECT=my-gcp-project ./deploy.sh
```

It needs the `gcloud` CLI, signed in (`gcloud auth login`). The first run asks for the owner password.

Things to know:

- **One instance only** (`--max-instances=1`). The metadata file is edited under an in-process lock, so two
  instances could overwrite each other. It scales to zero when idle, so the first visit after a quiet spell is
  a little slower.
- **Client address.** Cloud Run puts the real client address last in `X-Forwarded-For`; the server reads that
  entry (for the login lockout, and for `--owner-ip` if you set it by running `OWNER_IP=1.2.3.4 ./deploy.sh`).
  If you later put a load balancer in front, add `--proxy-hops 2`.
- **Your domain.** `gcloud beta run domain-mappings create --service gloan --domain gloan.example.com --region REGION`
  prints the DNS records to add at your registrar; Google issues the HTTPS certificate itself.
- **HTTPS** is provided by Cloud Run, which is what the owner password needs on the public internet.
- **Adding photos by hand:** upload files to the bucket's `photos/` folder (console or
  `gcloud storage cp *.jpg gs://BUCKET/photos/`), then use "Scan folder for new photos" on the site.
- **Size limit:** Cloud Run rejects requests over 32 MB; uploads are capped at 25 MB anyway.
- **Changing the password:** add a new Secret Manager version
  (`gcloud secrets versions add gloan-password --data-file=-`) and redeploy.
- **Cost:** a personal gallery typically stays within the free tier; the bucket is the only always-on piece.

Flags added for this: `--proxy-hops N` and `--proxy-any-peer` (trust the proxy header from any peer, only safe
when the container is reachable solely through a managed proxy). `PORT` is honored as the default port.
