#!/usr/bin/env bash
# Deploy Gloan to Cloud Run with photos and passkey kept in a Cloud Storage bucket.
# Usage: PROJECT=my-project ./deploy.sh
set -euo pipefail

: "${PROJECT:?set PROJECT to your Google Cloud project id}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-gloan}"
BUCKET="${BUCKET:-${PROJECT}-gloan-data}"

gcloud config set project "$PROJECT"
gcloud services enable run.googleapis.com \
    cloudbuild.googleapis.com artifactregistry.googleapis.com

if ! gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access
fi

# The default compute service account runs the service; let it use the bucket.
NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
RUNNER="${NUMBER}-compute@developer.gserviceaccount.com"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$RUNNER" \
    --role=roles/storage.objectUser >/dev/null

# --max-instances=1: photos.json is read-modify-written under an in-process lock, so two instances could
# overwrite each other's edits. One instance easily serves a personal gallery.
gcloud run deploy "$SERVICE" --source . --region "$REGION" --allow-unauthenticated \
    --max-instances=1 --min-instances=0 --cpu=1 --memory=512Mi --concurrency=40 --timeout=60 \
    --set-env-vars="GLOAN_TURNSTILE_SITE_KEY=${GLOAN_TURNSTILE_SITE_KEY:-},GLOAN_TURNSTILE_SECRET_KEY=${GLOAN_TURNSTILE_SECRET_KEY:-},GLOAN_GEMINI_API_KEY=${GLOAN_GEMINI_API_KEY:-}" \
    --add-volume="name=data,type=cloud-storage,bucket=${BUCKET}" \
    --add-volume-mount="volume=data,mount-path=/data" \
    --args="--host,0.0.0.0,--proxy-header,X-Forwarded-For,--proxy-any-peer"
