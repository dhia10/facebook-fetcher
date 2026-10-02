# Real-Time Stream Ingestion & Lead Routing Pipeline (`facebook-fetcher`)

Automated stream ingestion worker and API service designed to capture incoming live comments, extract contact entities and purchase intents, sanitize inputs, and route validated leads to n8n workflows and database storage.

---

## Overview & Architecture

In live commerce streaming (TikTok Live, Facebook Live), prospective buyers frequently comment with phone numbers and product inquiries in high-velocity bursts. This repository provides two complementary ingestion components:

1. **`server.py` (FastAPI Webhook Service):** Asynchronous REST endpoint receiving real-time comment payloads, validating payload schema with Pydantic, extracting entities, and triggering n8n automation workflows.
2. **`worker.py` (Graph API Polling Worker):** Scheduled worker running via GitHub Actions cron to paginate posts, live videos, and comment threads.

```
[ Live Streams / Comments ]
           │
           ▼
  [ Unicode Normalization ]  ──> Converts Arabic-Indic digits (٠-٩) to ASCII (0-9)
           │
           ▼
   [ Entity Extraction ]     ──> Regex matching Tunisian telecom operators (2, 3, 4, 5, 7, 9)
           │
           ▼
  [ Deduplication Window ]   ──> 1-hour in-memory sliding cache per phone number
           │
           ▼
  [ Webhook Dispatcher ]     ──> Dispatches validated lead payload to n8n / CRM
```

### Measured Impact
- **Throughput:** Scaled processing volume from **300 to 700 qualified leads/day (+133%)** at constant audience size.
- **Latency:** Sub-second routing from comment arrival to n8n workflow trigger.
- **Data Quality:** Zero malformed phone numbers or duplicate entries routed to agents.

---

## Data Quality & Governance Rules

- **Unicode Sanitization:** Strips HTML/script tags and converts Arabic-Indic digits (`\u0660`–`\u0669`) to standard integers.
- **Prefix Verification:** Enforces 8-digit lengths with valid operator prefixes (`2x`, `3x`, `4x`, `5x`, `7x`, `9x`).
- **Sliding Deduplication:** In-memory expiration window (`DEDUP_WINDOW_S = 3600`) to prevent duplicate outbound messages if a user posts their phone number multiple times in a live session.
- **Ignore-List Filter:** Immediately excludes customer service numbers or internal test numbers.

---

## Project Structure

```
facebook-fetcher/
├── .github/
│   └── workflows/
│       └── fetch.yml       # Scheduled GitHub Actions cron runner
├── server.py               # FastAPI real-time webhook endpoint & lead extractor
├── worker.py               # Graph API polling and pagination worker
├── requirements.txt        # Dependencies (FastAPI, Uvicorn, Pydantic, Requests)
└── README.md
```

---

## Quick Start

### 1. Installation

```bash
git clone https://github.com/dhia10/facebook-fetcher.git
cd facebook-fetcher

python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Run the Real-Time Ingestion Server

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

- Health Check: `GET http://localhost:8000/health`
- Ingest Endpoint: `POST http://localhost:8000/ingest/comment`

### 3. Run the Meta Graph API Worker

```bash
export FB_GRAPH_VER="v23.0"
export RECEIVER_URL="https://your-n8n-or-sheet-webhook.com"
export RECEIVER_SECRET="your_shared_secret"
export PAGES_JSON='[{"id":"your_page_id","token":"your_page_access_token"}]'

python worker.py
```

---

## Author & Contact

- **Developer:** Dhia Romdhane — Data Science & AI Engineering (ESPRIT)
- **LinkedIn:** [linkedin.com/in/dhia-romdhane-ds](https://www.linkedin.com/in/dhia-romdhane-ds/)
- **GitHub:** [github.com/dhia10](https://github.com/dhia10)

---

## License

MIT License.