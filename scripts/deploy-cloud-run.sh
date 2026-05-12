#!/usr/bin/env bash
# 一鍵：啟用 API → Artifact Registry → Cloud Build 建映像 → Cloud Run 部署
# 前置：已安裝 gcloud、已 gcloud auth login、GCP 已建立專案並啟用帳單
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-ig-reel-translate}"
REGION="${GCP_REGION:-asia-east1}"
REPO="${ARTIFACT_REPO:-ig-api}"
SERVICE="${CLOUD_RUN_SERVICE:-instagram-reel-api}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/instagram-reel-api:latest"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

echo "==> Project: ${PROJECT_ID}  Region: ${REGION}  Service: ${SERVICE}"
gcloud config set project "${PROJECT_ID}"
gcloud projects describe "${PROJECT_ID}" >/dev/null

echo "==> Enable APIs"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com --project="${PROJECT_ID}"

echo "==> Artifact Registry: ${REPO}"
if ! gcloud artifacts repositories describe "${REPO}" --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --description="instagram-reel API images"
fi

echo "==> Docker auth for Artifact Registry"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" -q

echo "==> Cloud Build (Docker build + push)"
gcloud builds submit "${ROOT}" \
  --project="${PROJECT_ID}" \
  --tag "${IMAGE}"

echo "==> Cloud Run deploy"
# 勿在 set-env-vars 設定 PORT；Cloud Run 會自動注入
gcloud run deploy "${SERVICE}" \
  --project="${PROJECT_ID}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --memory 4Gi \
  --cpu 2 \
  --timeout 3600 \
  --set-env-vars "HOST=0.0.0.0,CORS_ORIGINS=https://dg-family.github.io,WHISPER_MODEL=base"

URL="$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')"
echo ""
echo "=========================================="
echo "Cloud Run 網址: ${URL}"
echo "=========================================="
echo ""
echo "GitHub Actions：到 repo → Settings → Secrets → Actions"
echo "新增或更新 PUBLIC_API_URL = ${URL}"
echo "再手動執行 workflow「Deploy GitHub Pages」讓 app.html 指向此 API。"
