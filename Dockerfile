FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    GLOAN_DATA=/data

WORKDIR /app
COPY server.py index.html ./

# Unprivileged user; /data is where Cloud Run mounts the Cloud Storage bucket.
RUN useradd --system --uid 10001 gloan && mkdir /data && chown gloan /data
USER gloan

# Cloud Run supplies $PORT. The client address arrives in X-Forwarded-For, appended by Google's front end (last
# entry); the container is only reachable through it, so any peer may be trusted. ENTRYPOINT is fixed so that
# deploy.sh's --args (which replace CMD) can add flags without redefining the command.
ENTRYPOINT ["python3", "server.py"]
CMD ["--host", "0.0.0.0", "--proxy-header", "X-Forwarded-For", "--proxy-any-peer"]
