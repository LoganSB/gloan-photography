FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    GLOAN_DATA=/data

WORKDIR /app

# Install Flask + Gunicorn for the web server and Pillow for tools/make_thumbnails.py.
COPY requirements.txt ./requirements.txt
COPY tools/requirements.txt ./tools/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r tools/requirements.txt
COPY server.py index.html ./
COPY static ./static
COPY gloan ./gloan
COPY tools ./tools

# Unprivileged user; /data is where Cloud Run mounts the Cloud Storage bucket.
RUN useradd --system --uid 10001 gloan && mkdir /data && chown gloan /data
USER gloan

# Cloud Run supplies $PORT. The client address arrives in X-Forwarded-For, appended by Google's front end (last
# entry); the container is only reachable through it, so any peer may be trusted. ENTRYPOINT is fixed so that
# deploy.sh's --args (which replace CMD) can add flags without redefining the command.
ENTRYPOINT ["python3", "server.py"]
CMD ["--host", "0.0.0.0", "--proxy-header", "X-Forwarded-For", "--proxy-any-peer"]
