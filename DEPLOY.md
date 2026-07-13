# Deploying to Google Cloud Run

Cloud Run is the right fit: one container, HTTP, scale-to-zero, no cluster to manage.
Cloud Build compiles the image in the cloud, so **you do not need Docker locally**.

---

## 0. One-time setup

Install the CLI (you don't have it yet):

```bash
brew install --cask google-cloud-sdk
```

Then authenticate and pick a project:

```bash
gcloud init                                   # login + choose/create a project
gcloud config set project YOUR_PROJECT_ID

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com
```

Billing must be enabled on the project (Cloud Run has a free tier, but the
project still needs a billing account attached).

---

## 1. Put the Groq key in Secret Manager

Never bake the key into the image — `.dockerignore` already excludes `.env`.

```bash
# rotate the old key first at console.groq.com — it was exposed in a screenshot
printf 'gsk_YOUR_NEW_KEY' | gcloud secrets create groq-api-key --data-file=-
```

Grant Cloud Run's runtime service account access to it:

```bash
PROJECT_NUMBER=$(gcloud projects describe $(gcloud config get-value project) --format='value(projectNumber)')

gcloud secrets add-iam-policy-binding groq-api-key \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor
```

---

## 2. Deploy

From the repo root (where the `Dockerfile` is):

```bash
gcloud run deploy jewellery-bot \
  --source . \
  --region asia-south1 \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 300 \
  --min-instances 1 \
  --max-instances 1 \
  --set-secrets GROQ_API_KEY=groq-api-key:latest
```

First deploy takes ~8-12 min (Chromium is a big layer). Later ones are faster.
It prints a `https://jewellery-bot-xxxx.run.app` URL when done.

### Why each flag matters — do not trim these

| Flag | Reason |
|---|---|
| `--memory 2Gi` | Playwright/Chromium OOMs at the 512Mi default. |
| `--cpu 2` | Parsing hundreds of product pages is CPU-bound. |
| `--timeout 300` | A hard site takes up to 150s to scrape; the 60s default would kill it. |
| `--max-instances 1` | **Sessions are in-memory.** With 2+ instances, the request that creates a session and the request that chats can land on different containers, and chat 404s. |
| `--min-instances 1` | Scale-to-zero destroys the container, wiping every session. Keeps one warm. |
| `--region asia-south1` | Mumbai — closest to Indian jewellery sites, so scrapes are faster. |

---

## 3. Verify

```bash
URL=$(gcloud run services describe jewellery-bot --region asia-south1 --format='value(status.url)')

curl "$URL/api/health"          # -> {"status":"ok","active_sessions":0}
open "$URL"                     # the app
```

Logs:

```bash
gcloud run services logs tail jewellery-bot --region asia-south1
```

---

## Known limitation: sessions are in-memory

`--max-instances 1` is a workaround, not a fix. It means the app cannot scale
horizontally, and a container restart (deploy, crash, or Cloud Run recycling it)
drops every active session — users see "session expired".

To actually scale, `session_store.py` needs to move to Redis (Memorystore) or
Firestore. That is a real change, not a config flag. Fine to defer for a demo;
not fine for real traffic.

## Cost

With `--min-instances 1` the container never scales to zero, so you pay for one
always-on instance (roughly $15-25/month at 2 vCPU / 2Gi in asia-south1).
Dropping to `--min-instances 0` is much cheaper but sessions die on every cold
start. That trade-off disappears once the session store is external.
