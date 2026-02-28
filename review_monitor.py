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


def resolve_to_chij_place_id(place_id: str, business_name: str) -> str:
    """If place_id is hex (0x...:0x...), resolve to ChIJ via Places searchText. Else return as-is."""
    if place_id.startswith("ChIJ"):
        return place_id
    key = GOOGLE_PLACES_API_KEY
    if not key or not business_name:
        return place_id
    try:
        url = "https://places.googleapis.com/v1/places:searchText"
        r = requests.post(
            url,
            headers={
                "X-Goog-Api-Key": key,
                "Content-Type": "application/json",
                "X-Goog-FieldMask": "places.id,places.displayName",
            },
            json={"textQuery": business_name},
            timeout=10,
        )
        if r.status_code != 200:
            try:
                err = r.json().get("error", {}).get("message", r.text[:150])
            except Exception:
                err = r.text[:150] if r.text else ""
            print(f"[review_monitor] ChIJ resolve failed (searchText): HTTP {r.status_code} – {err}")
            return place_id
        data = r.json()
        places = data.get("places") or []
        if not places:
            print("[review_monitor] ChIJ resolve: searchText returned no places")
            return place_id
        # id is like "places/ChIJ..."
        raw_id = (places[0].get("id") or "").strip()
        if raw_id.startswith("places/"):
            return raw_id.replace("places/", "", 1)
        return raw_id or place_id
    except Exception as e:
        print(f"[review_monitor] ChIJ resolve error: {e}")
        return place_id


def _fetch_reviews_new_api(place_id: str, key: str):
    """Fetch reviews using Places API (New) – accepts hex place_id in path."""
    place_id_encoded = place_id.replace(":", "%3A")
    url = f"https://places.googleapis.com/v1/places/{place_id_encoded}?fields=reviews,displayName"
    r = requests.get(url, headers={"X-Goog-Api-Key": key}, timeout=10)
    if r.status_code == 200:
        data = r.json()
        if "error" in data:
            msg = data["error"].get("message", data["error"].get("status", ""))
            return None, f"New API error: {msg}"
        return data.get("reviews") or [], None
    try:
        err_body = r.json()
        msg = err_body.get("error", {}).get("message", r.text[:200])
    except Exception:
        msg = r.text[:200] if r.text else ""
    return None, f"HTTP {r.status_code}" + (f" – {msg}" if msg else "")


def get_place_reviews(place_id: str):
    """Fetch reviews. Tries legacy (ChIJ) first; if INVALID_REQUEST and place_id is hex, tries New API."""
    key = GOOGLE_PLACES_API_KEY
    if not key:
        return None, "No GOOGLE_PLACES_API_KEY"
    is_hex = "0x" in place_id and ":" in place_id
    try:
        # Legacy Place Details – works with ChIJ, often fails with hex
        url_legacy = f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&fields=reviews,name&key={key}"
        r2 = requests.get(url_legacy, timeout=10)
        if r2.status_code == 200:
            j = r2.json()
            if j.get("status") == "OK" and "result" in j:
                reviews = j["result"].get("reviews") or []
                return reviews, None
            status = j.get("status", "unknown")
            # If legacy rejects hex, try New API (accepts hex in path)
            if is_hex and status in ("INVALID_REQUEST", "ZERO_RESULTS"):
                reviews, err = _fetch_reviews_new_api(place_id, key)
                if err is None:
                    print("[review_monitor] Used Places API (New) for hex place_id")
                    return reviews, None
                print(f"[review_monitor] New API fallback failed: {err}")
            return [], status
        if is_hex:
            reviews, err = _fetch_reviews_new_api(place_id, key)
            if err is None:
                print("[review_monitor] Used Places API (New) for hex place_id")
                return reviews, None
            print(f"[review_monitor] New API fallback failed: {err}")
        return None, f"HTTP {r2.status_code}"
    except Exception as e:
        return None, str(e)

def run_review_check():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    businesses = supabase.table("businesses").select("id, name, owner_phone, place_id").not_.is_("place_id", "null").execute()
    if not businesses.data:
        print("[review_monitor] No businesses with place_id found – add place_id in Supabase")
        return
    print(f"[review_monitor] Checking {len(businesses.data)} business(es)")
    twilio = None
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM:
        twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    for biz in businesses.data:
        place_id = biz.get("place_id")
        if not place_id:
            continue
        # Hex place_id (from Maps URL) → try to resolve to ChIJ; else we'll try New API in get_place_reviews
        resolved_id = resolve_to_chij_place_id(place_id, biz.get("name") or "")
        if resolved_id != place_id:
            print(f"[review_monitor] Resolved place_id to ChIJ format for {biz.get('name')}")
        elif "0x" in place_id and ":" in place_id:
            print(f"[review_monitor] Using hex place_id – will try New Places API if legacy fails")
        reviews, err = get_place_reviews(resolved_id)
        if err:
            print(f"[review_monitor] {biz.get('name')} place_id={place_id[:30]}... err={err}")
            continue
        if not reviews:
            print(f"[review_monitor] {biz.get('name')} – 0 reviews from Google (or Place ID not valid)")
            continue
        # Only send WhatsApp for reviews that appear *after* we've already backfilled (so we don't alert for old reviews on first run)
        existing = supabase.table("seen_reviews").select("id").eq("business_id", biz["id"]).limit(1).execute()
        is_backfill = not (existing.data and len(existing.data) > 0)
        if is_backfill:
            print(f"[review_monitor] {biz.get('name')} – backfilling {len(reviews)} existing review(s), no WhatsApp")
        else:
            print(f"[review_monitor] {biz.get('name')} – got {len(reviews)} review(s)")
        for rev in reviews:
            # Legacy API: author_name, rating, text
            # New API: authorAttribution.displayName, rating, text (or text.text)
            author = (rev.get("authorAttribution") or {}).get("displayName") or rev.get("author_name") or "Someone"
            rating = rev.get("rating") or 0
            raw_text = rev.get("text")
            if isinstance(raw_text, dict):
                text = raw_text.get("text") or ""
            else:
                text = str(raw_text or "")
            review_id = f"{resolved_id}|{author}|{text[:50]}"
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
            # Alert owner via WhatsApp only for truly new reviews (not during first-time backfill)
            if is_backfill:
                continue
            to_phone = biz.get("owner_phone") or ""
            if to_phone and not to_phone.startswith("whatsapp:"):
                to_phone = f"whatsapp:{to_phone}"
            if twilio and to_phone:
                urgent = "⚠️ Low rating – " if rating and int(rating) <= 3 else ""
                body = f"{urgent}New review for {biz.get('name', 'your business')}\n\n{author} – {rating} stars\n{text[:300]}"
                try:
                    twilio.messages.create(body=body, from_=TWILIO_WHATSAPP_FROM, to=to_phone)
                    print(f"[review_monitor] WhatsApp sent to owner for new review")
                except Exception as e:
                    print(f"[review_monitor] Twilio send failed: {e}")
            else:
                if not twilio:
                    print("[review_monitor] Twilio not configured – set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM")
                elif not to_phone:
                    print(f"[review_monitor] No owner_phone for {biz.get('name')}")
