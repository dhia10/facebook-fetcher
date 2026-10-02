"""
FastAPI Stream Ingestion & Webhook Dispatcher
Author: Dhia Romdhane
"""

import os
import re
import time
import logging
from typing import List, Optional
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI(
    title="Real-Time Stream Ingestion & Lead Routing Engine",
    description="Captures live comments, extracts phone numbers and purchase intent, validates input, and routes to n8n / PostgreSQL.",
    version="1.1.0"
)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "prod_secret_token_2026")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")

# Regular expression for 8-digit phone numbers with Tunisian operator prefixes (2, 3, 4, 5, 7, 9)
PHONE_REGEX = re.compile(r'(?:^|[^\d])([234579](?:[\s.-]?\d){7})(?:[^\d]|$)')
INTENT_KEYWORDS = re.compile(r'\b(prix|combien|livraison|commande|commander|interesse|dispo|taille|acheter|svp)\b', re.IGNORECASE)

# In-memory deduplication cache: phone -> last_seen_timestamp
dedup_cache = {}
DEDUP_EXPIRY_SECONDS = 3600


class LiveCommentPayload(BaseModel):
    source: str = Field(..., example="tiktok_live")
    sender_id: str = Field(..., example="usr_98124")
    sender_name: str = Field(..., example="Mohamed Ben Ali")
    message: str = Field(..., example="svp prix livraison 98123456 disponible ?")
    timestamp: Optional[str] = None


class ExtractedLead(BaseModel):
    source: str
    sender_name: str
    phone: str
    intent_detected: bool
    raw_message: str
    created_at: str


def sanitize_input(text: str) -> str:
    """Strips dangerous characters, script tags, and normalizes Arabic-indic digits."""
    if not text:
        return ""
    # Convert Arabic-Indic digits (٠-٩) to standard (0-9)
    cleaned = ''.join(chr(ord(c) - 0x0660 + ord('0')) if '\u0660' <= c <= '\u0669' else c for c in text)
    cleaned = re.sub(r'[<>{}\[\]\\]', '', cleaned)
    return cleaned.strip()


def forward_to_n8n(lead: ExtractedLead):
    """Dispatches validated lead payload to n8n webhook for automated messaging & CRM."""
    if not N8N_WEBHOOK_URL:
        logging.info(f"[ROUTER] (Simulated n8n dispatch) Lead: {lead.phone} | Intent: {lead.intent_detected}")
        return

    try:
        import requests
        resp = requests.post(N8N_WEBHOOK_URL, json=lead.dict(), timeout=3.0)
        logging.info(f"[ROUTER] Dispatched to n8n: {resp.status_code}")
    except Exception as e:
        logging.error(f"[ROUTER] Failed to dispatch to n8n: {e}")


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "stream-lead-ingestion",
        "cached_leads": len(dedup_cache),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@app.post("/ingest/comment", status_code=202)
def ingest_comment(
    payload: LiveCommentPayload,
    background_tasks: BackgroundTasks,
    x_secret_token: Optional[str] = Header(None)
):
    if WEBHOOK_SECRET and x_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid authorization token")

    clean_message = sanitize_input(payload.message)
    match = PHONE_REGEX.search(clean_message)

    if not match:
        return {"processed": True, "lead_created": False, "reason": "no_valid_phone_number"}

    phone = re.sub(r'\D', '', match.group(1))
    now = time.time()

    # Deduplication check
    last_seen = dedup_cache.get(phone, 0)
    if now - last_seen < DEDUP_EXPIRY_SECONDS:
        return {"processed": True, "lead_created": False, "reason": "duplicate_within_cooldown_window"}

    dedup_cache[phone] = now
    has_intent = bool(INTENT_KEYWORDS.search(clean_message))

    lead = ExtractedLead(
        source=payload.source,
        sender_name=payload.sender_name,
        phone=phone,
        intent_detected=has_intent,
        raw_message=clean_message,
        created_at=datetime.now(timezone.utc).isoformat()
    )

    logging.info(f"[INGEST] New qualified lead: {lead.phone} ({lead.sender_name}) - Intent: {lead.intent_detected}")
    background_tasks.add_task(forward_to_n8n, lead)

    return {"processed": True, "lead_created": True, "phone": phone, "intent": has_intent}
