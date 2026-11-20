#!/usr/bin/env bash
# Deploy Gloan to Cloud Run with photos and passkey kept in a Cloud Storage bucket.
# Usage: PROJECT=my-project ./deploy.sh
set -euo pipefail

: "${PROJECT:?set PROJECT to your Google Cloud project id}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-gloan}"
BUCKET="${BUCKET:-${PROJECT}-gloan-data}"
SECRET="${SECRET:-gloan-password}"

gcloud config set project "$PROJECT"
gcloud services enable run.googleapis.com secretmanager.googleapis.com \
    cloudbuild.googleapis.com artifactregistry.googleapis.com

if ! gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access
fi

# DEBUG PASSWORD WORKAROUND: remove the secret creation and --set-secrets line once your Passkey is registered in /data/passkey.json.
if ! gcloud secrets describe "$SECRET" >/dev/null 2>&1; then
    echo "Choose a temporary debug password to register your Passkey (8+ characters, not echoed):"
    read -r -s PASSWORD
    printf '%s' "$PASSWORD" | gcloud secrets create "$SECRET" --data-file=-
    unset PASSWORD
fi

# The default compute service account runs the service; let it read the secret and use the bucket.
NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
RUNNER="${NUMBER}-compute@developer.gserviceaccount.com"
gcloud secrets add-iam-policy-binding "$SECRET" --member="serviceAccount:$RUNNER" \
    --role=roles/secretmanager.secretAccessor >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$RUNNER" \
    --role=roles/storage.objectUser >/dev/null

# --max-instances=1: photos.json is read-modify-written under an in-process lock, so two instances could
# overwrite each other's edits. One instance easily serves a personal gallery.
gcloud run deploy "$SERVICE" --source . --region "$REGION" --allow-unauthenticated \
    --max-instances=1 --min-instances=0 --cpu=1 --memory=512Mi --concurrency=40 --timeout=120 \
    --set-secrets="GLOAN_PASSWORD=${SECRET}:latest" \
    --set-env-vars="GLOAN_TURNSTILE_SITE_KEY=${GLOAN_TURNSTILE_SITE_KEY:-},GLOAN_TURNSTILE_SECRET_KEY=${GLOAN_TURNSTILE_SECRET_KEY:-}" \
    --add-volume="name=data,type=cloud-storage,bucket=${BUCKET}" \
    --add-volume-mount="volume=data,mount-path=/data" \
    --args="--host,0.0.0.0,--proxy-header,X-Forwarded-For,--proxy-any-peer"
