"""
Runs every 30 min: fetch reviews from Google Places, alert owner via WhatsApp for new reviews.
No posting to Google until approval (stub).
"""
import os
import time
import requests
from supabase import create_client
from twilio.rest import Client as TwilioClient

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")  # e.g. whatsapp:+14155238886

def get_place_reviews(place_id: str):
    """Fetch reviews for a place. Tries New API then legacy."""
    key = GOOGLE_PLACES_API_KEY
    if not key:
        return None, "No GOOGLE_PLACES_API_KEY"
    # New Places API (v1) - place_id in path (hex format URL-encoded)
    place_id_encoded = place_id.replace(":", "%3A")
    url = f"https://places.googleapis.com/v1/places/{place_id_encoded}?fields=reviews,displayName"
    try:
        r = requests.get(url, headers={"X-Goog-Api-Key": key}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return data.get("reviews") or [], None
        # Fallback: legacy Place Details (uses ChIJ place_id; our hex may not work)
        url_legacy = f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&fields=reviews,name&key={key}"
        r2 = requests.get(url_legacy, timeout=10)
        if r2.status_code == 200:
            j = r2.json()
            if j.get("status") == "OK" and "result" in j:
                return j["result"].get("reviews") or [], None
            return [], j.get("status", "unknown")
        return None, f"HTTP {r.status_code}"
    except Exception as e:
        return None, str(e)

def run_review_check():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    businesses = supabase.table("businesses").select("id, name, owner_phone, place_id").not_.is_("place_id", "null").execute()
    if not businesses.data:
        return
    twilio = None
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM:
        twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    for biz in businesses.data:
        place_id = biz.get("place_id")
        if not place_id:
            continue
        reviews, err = get_place_reviews(place_id)
        if err:
            print(f"[review_monitor] {biz.get('name')} place_id={place_id[:20]}... err={err}")
            continue
        if not reviews:
            continue
        for rev in reviews:
            author = (rev.get("authorAttribution") or {}).get("displayName") or "Someone"
            rating = rev.get("rating") or 0
            raw_text = rev.get("text")
            if isinstance(raw_text, dict):
                text = raw_text.get("text") or ""
            else:
                text = str(raw_text or "")
            review_id = f"{place_id}|{author}|{text[:50]}"
            existing = supabase.table("seen_reviews").select("id").eq("review_id", review_id).limit(1).execute()
            if existing.data:
                continue
            supabase.table("seen_reviews").insert({
                "review_id": review_id,
                "business_id": biz["id"],
                "reviewer_name": author,
                "rating": rating,
                "review_text": text,
                "replied": False,
            }).execute()
            # Alert owner via WhatsApp
            to_phone = biz.get("owner_phone") or ""
            if to_phone and not to_phone.startswith("whatsapp:"):
                to_phone = f"whatsapp:{to_phone}"
            if twilio and to_phone:
                urgent = "⚠️ Low rating – " if rating and int(rating) <= 3 else ""
                body = f"{urgent}New review for {biz.get('name', 'your business')}\n\n{author} – {rating} stars\n{text[:300]}"
                try:
                    twilio.messages.create(body=body, from_=TWILIO_WHATSAPP_FROM, to=to_phone)
                except Exception as e:
                    print(f"[review_monitor] Twilio send failed: {e}")
