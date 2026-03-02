from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
import anthropic
from supabase import create_client
import requests
import threading
import time
import os
import json
from datetime import datetime, timedelta
import schedule

# =========================
# KEYS – all from env (set in Railway / .env). Never commit secrets.
# =========================
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
GOOGLE_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://znrvyoriduvtfbjdlhmm.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
DEFAULT_SHEET_URL = os.environ.get("SHEET_URL", "")

# Google Service Account JSON (full JSON string). Required for Stock Guard.
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# =========================
# CLIENTS
# =========================
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)


# =========================
# HELPERS
# =========================
def send_whatsapp(to, message):
    if not twilio_client or not TWILIO_WHATSAPP_NUMBER:
        print("[WARN] Twilio not configured, skipping WhatsApp send")
        return
    to = to if str(to).startswith("whatsapp:") else f"whatsapp:{to}"
    twilio_client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=to, body=message)


def get_business(sender):
    """Normalize Twilio 'whatsapp:+971...' to match owner_phone in DB."""
    phone = (sender or "").replace("whatsapp:", "").strip()
    if not phone:
        return None
    result = supabase.table("businesses").select("*").eq("owner_phone", phone).limit(1).execute()
    return result.data[0] if result.data else None


def get_google_creds():
    """Load Google service account from env JSON. Returns None if not set or google-auth not installed."""
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None
    try:
        from google.oauth2.service_account import Credentials
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        return Credentials.from_service_account_info(info, scopes=scopes)
    except ImportError:
        return None  # google-auth not installed – Stock Guard disabled
    except Exception as e:
        print(f"[WARN] Google service account load failed: {e}")
        return None


# =========================
# STOCK GUARD (Sheets API v4 via requests + google-auth, no gspread)
# =========================
def _sheet_id_from_url(url):
    if not url or "/d/" not in url:
        return None
    try:
        start = url.index("/d/") + 3
        end = url.index("/", start) if "/" in url[start:] else len(url)
        return url[start:end].split("?")[0].strip()
    except Exception:
        return None


def read_stock_sheet(business):
    sheet_url = business.get("sheets_url") or DEFAULT_SHEET_URL
    sid = _sheet_id_from_url(sheet_url)
    if not sid:
        return []
    creds = get_google_creds()
    if not creds:
        return []
    try:
        try:
            from google.auth.transport.requests import Request
        except ImportError:
            return []
        creds.refresh(Request())
        headers = {"Authorization": f"Bearer {creds.token}"}
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/Sheet1!A:Z"
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"Sheets API error: {r.status_code} {r.text[:200]}")
            return []
        data = r.json()
        values = data.get("values") or []
        if len(values) < 2:
            return []
        headers_row = [str(h).strip() for h in values[0]]
        records = []
        for row in values[1:]:
            row = list(row) + [""] * (len(headers_row) - len(row))
            records.append(dict(zip(headers_row, row[: len(headers_row)])))
        return records
    except Exception as e:
        print(f"Sheet read error: {e}")
        return []


def sync_stock_to_supabase(business, rows):
    for row in rows:
        name = (row.get("Item Name") or "").strip()
        if not name:
            continue
        try:
            qty = float(row.get("Current Quantity") or 0)
            threshold = float(row.get("Reorder Threshold") or 0)
            reorder = float(row.get("Reorder Quantity") or 0)
        except (TypeError, ValueError):
            continue
        unit = row.get("Unit", "")
        supplier_name = row.get("Supplier Name", "")
        supplier_wa = row.get("Supplier WhatsApp", "")

        existing = supabase.table("stock_items").select("*").eq("business_id", business["id"]).eq("name", name).execute()

        if existing.data:
            old_qty = existing.data[0].get("current_quantity", 0)
            supabase.table("stock_items").update({
                "current_quantity": qty,
                "reorder_threshold": threshold,
                "reorder_quantity": reorder,
                "unit": unit,
                "last_updated": datetime.now().isoformat(),
            }).eq("id", existing.data[0]["id"]).execute()
            if qty != old_qty:
                supabase.table("stock_movements").insert({
                    "business_id": business["id"],
                    "item_name": name,
                    "quantity_change": qty - old_qty,
                    "new_quantity": qty,
                    "type": "sheet_update",
                    "recorded_at": datetime.now().isoformat(),
                }).execute()
        else:
            supabase.table("stock_items").insert({
                "business_id": business["id"],
                "name": name,
                "current_quantity": qty,
                "unit": unit,
                "reorder_threshold": threshold,
                "reorder_quantity": reorder,
                "supplier_name": supplier_name,
                "supplier_whatsapp": supplier_wa,
                "last_updated": datetime.now().isoformat(),
            }).execute()


def predict_stockout(business_id, item_name, current_qty):
    try:
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        movements = (
            supabase.table("stock_movements")
            .select("*")
            .eq("business_id", business_id)
            .eq("item_name", item_name)
            .lt("quantity_change", 0)
            .gte("recorded_at", week_ago)
            .execute()
        )
        if not movements.data:
            return None
        total_used = sum(abs(m["quantity_change"]) for m in movements.data)
        daily_usage = total_used / 7
        return round(current_qty / daily_usage, 1) if daily_usage > 0 else None
    except Exception:
        return None


def check_stock_levels(business, rows):
    alerts = []
    for row in rows:
        name = (row.get("Item Name") or "").strip()
        if not name:
            continue
        try:
            qty = float(row.get("Current Quantity") or 0)
            threshold = float(row.get("Reorder Threshold") or 0)
            reorder = float(row.get("Reorder Quantity") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= threshold:
            days_left = predict_stockout(business["id"], name, qty)
            alerts.append({
                "name": name,
                "qty": qty,
                "unit": row.get("Unit", ""),
                "threshold": threshold,
                "reorder": reorder,
                "supplier_name": row.get("Supplier Name", ""),
                "supplier_wa": row.get("Supplier WhatsApp", ""),
                "days_left": days_left,
            })
    return alerts


def send_stock_alerts(business, alerts):
    for item in alerts:
        four_hours_ago = (datetime.now() - timedelta(hours=4)).isoformat()
        recent = (
            supabase.table("pending_actions")
            .select("*")
            .eq("business_id", business["id"])
            .eq("action_type", "stock_alert")
            .gte("created_at", four_hours_ago)
            .execute()
        )
        if recent.data and any(r.get("item_name") == item["name"] for r in recent.data):
            continue
        days_str = f"\n⏰ Estimated stockout in *{item['days_left']} days*" if item.get("days_left") else ""
        msg = f"""📦 *Stock Alert — {item['name']}*

Current: *{item['qty']} {item['unit']}*
Reorder point: {item['threshold']} {item['unit']}{days_str}
Supplier: {item['supplier_name']}

Reply *ORDER {item['name']}* to draft a purchase order
Reply *IGNORE {item['name']}* to snooze"""
        send_whatsapp(business["owner_phone"], msg)
        supabase.table("pending_actions").insert({
            "business_id": business["id"],
            "owner_phone": business["owner_phone"],
            "action_type": "stock_alert",
            "item_name": item["name"],
            "item_data": json.dumps(item),
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        }).execute()


def run_stock_monitor():
    print(f"[{datetime.now()}] Running stock monitor...")
    for row in supabase.table("businesses").select("*").execute().data or []:
        try:
            rows = read_stock_sheet(row)
            if not rows:
                continue
            sync_stock_to_supabase(row, rows)
            alerts = check_stock_levels(row, rows)
            if alerts:
                send_stock_alerts(row, alerts)
        except Exception as e:
            print(f"Stock error for {row.get('name', '?')}: {e}")


def handle_order_command(business, item_name):
    items = (
        supabase.table("stock_items")
        .select("*")
        .eq("business_id", business["id"])
        .ilike("name", f"%{item_name}%")
        .execute()
    )
    if not items.data:
        return f"I couldn't find '{item_name}' in your stock list."
    item = items.data[0]
    order_msg = f"""Hi {item.get('supplier_name', 'there')},

We'd like to place an order:
• {item['name']}: {item['reorder_quantity']} {item.get('unit', '')}

Please confirm availability and delivery time.

Thanks,
{business['name']}"""
    supabase.table("pending_actions").insert({
        "business_id": business["id"],
        "owner_phone": business["owner_phone"],
        "action_type": "purchase_order",
        "item_name": item["name"],
        "item_data": json.dumps(item),
        "draft_reply": order_msg,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
    }).execute()
    return f"""📋 *Purchase Order Draft*
Item: {item['name']} — {item['reorder_quantity']} {item.get('unit', '')}
Supplier: {item.get('supplier_name', 'Unknown')}

Message:
"{order_msg}"

Reply *SEND ORDER* to send to supplier via WhatsApp"""


def handle_send_order(business):
    pending = (
        supabase.table("pending_actions")
        .select("*")
        .eq("business_id", business["id"])
        .eq("action_type", "purchase_order")
        .eq("status", "pending")
        .execute()
    )
    if not pending.data:
        return "No pending purchase orders found."
    action = pending.data[0]
    item = json.loads(action.get("item_data") or "{}")
    supplier_wa = item.get("supplier_whatsapp", "")
    if not supplier_wa:
        return f"No WhatsApp number for {item.get('supplier_name')}. Add it to your Google Sheet."
    if not str(supplier_wa).startswith("whatsapp:"):
        supplier_wa = f"whatsapp:{supplier_wa}"
    send_whatsapp(supplier_wa, action.get("draft_reply", ""))
    supabase.table("pending_actions").update({"status": "completed"}).eq("id", action["id"]).execute()
    return f"✅ Order sent to {item.get('supplier_name')}!"


def handle_check_stock(business):
    items = supabase.table("stock_items").select("*").eq("business_id", business["id"]).execute()
    if not items.data:
        return "No stock items yet. Type *sync stock* to load from your Google Sheet."
    low = [i for i in items.data if (i.get("current_quantity") or 0) <= (i.get("reorder_threshold") or 0)]
    ok = [i for i in items.data if (i.get("current_quantity") or 0) > (i.get("reorder_threshold") or 0)]
    msg = f"📦 *Stock — {business['name']}*\n\n"
    if low:
        msg += "🔴 *Needs Reordering:*\n"
        for i in low:
            msg += f"• {i['name']}: {i.get('current_quantity')} {i.get('unit', '')}\n"
        msg += "\n"
    if ok:
        msg += "✅ *OK:*\n"
        for i in ok:
            msg += f"• {i['name']}: {i.get('current_quantity')} {i.get('unit', '')}\n"
    return msg


# =========================
# REVIEW SHIELD
# =========================
def get_reviews(place_id):
    if not place_id or not GOOGLE_API_KEY:
        return None
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {"place_id": place_id, "fields": "name,rating,reviews,user_ratings_total", "key": GOOGLE_API_KEY}
    try:
        data = requests.get(url, params=params, timeout=10).json()
        return data.get("result")
    except Exception:
        return None


def draft_review_reply(business_name, reviewer_name, rating, review_text):
    try:
        response = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": f"Write a professional reply for {business_name} to this {rating}/5 star Google review from {reviewer_name}: '{review_text}'. Under 100 words, warm and specific. Just the reply."
            }],
        )
        return response.content[0].text
    except Exception:
        return "Thank you for your feedback. We appreciate you taking the time to share your experience."


def check_reviews_for_all_businesses():
    print(f"[{datetime.now()}] Running review check...")
    for business in (supabase.table("businesses").select("*").execute().data or []):
        place_id = business.get("place_id") or business.get("google_place_id")
        if not place_id:
            continue
        place_data = get_reviews(place_id)
        if not place_data:
            continue
        for review in place_data.get("reviews") or []:
            review_id = f"{place_id}_{review.get('time')}"
            if supabase.table("seen_reviews").select("id").eq("review_id", review_id).limit(1).execute().data:
                continue
            reviewer = review.get("author_name", "Someone")
            rating = review.get("rating", 0)
            text = review.get("text", "")
            reply_draft = draft_review_reply(business["name"], reviewer, rating, text)
            supabase.table("seen_reviews").insert({
                "review_id": review_id,
                "business_id": business["id"],
                "reviewer_name": reviewer,
                "rating": rating,
                "review_text": text,
                "reply_draft": reply_draft,
                "replied": False,
            }).execute()
            urgent = "🚨 *URGENT*\n\n" if rating <= 3 else "⭐ *New Review*\n\n"
            send_whatsapp(
                business["owner_phone"],
                f"{urgent}*{reviewer}* {'⭐' * int(rating)}\n\"{text[:300]}\"\n\n*Suggested reply:*\n\"{reply_draft}\"\n\nReply *YES* to approve."
            )
            supabase.table("pending_actions").insert({
                "business_id": business["id"],
                "owner_phone": business["owner_phone"],
                "action_type": "review_reply",
                "review_id": review_id,
                "draft_reply": reply_draft,
                "status": "pending",
                "created_at": datetime.now().isoformat(),
            }).execute()


def monday_stock_summary():
    """Send stock report to each business every Monday 8am."""
    for business in (supabase.table("businesses").select("*").execute().data or []):
        try:
            msg = handle_check_stock(business)
            send_whatsapp(business["owner_phone"], msg)
        except Exception as e:
            print(f"Monday summary error for {business.get('name')}: {e}")


# =========================
# WEBHOOK
# =========================
@app.route("/webhook/whatsapp", methods=["POST"])
def webhook():
    incoming_msg = (request.form.get("Body") or "").strip()
    sender = request.form.get("From", "")
    msg_lower = incoming_msg.lower()
    business = get_business(sender)
    resp = MessagingResponse()

    if not business:
        resp.message("Welcome to Wavy AI! You're not registered yet.")
        return str(resp)

    reply = None

    # Stock Guard commands
    if msg_lower.startswith("order "):
        reply = handle_order_command(business, incoming_msg[6:].strip())
    elif msg_lower in ["send order", "yes send", "send it"]:
        reply = handle_send_order(business)
    elif any(x in msg_lower for x in ["check stock", "stock levels", "my stock", "show stock"]):
        reply = handle_check_stock(business)
    elif msg_lower == "sync stock":
        rows = read_stock_sheet(business)
        if rows is None:
            rows = []
        sync_stock_to_supabase(business, rows)
        reply = f"✅ Synced {len(rows)} items from your Google Sheet." if rows else "Couldn't read sheet. Add sheets_url to your business or set SHEET_URL."

    # Review Shield: check reviews (manual)
    elif any(x in msg_lower for x in ["check reviews", "my reviews"]):
        place_id = business.get("place_id") or business.get("google_place_id")
        place_data = get_reviews(place_id) if place_id else None
        if place_data:
            reviews = place_data.get("reviews") or []
            msg = f"⭐ {place_data.get('rating')}/5 ({place_data.get('user_ratings_total')} total)\n\n"
            for r in reviews[:5]:
                msg += f"*{r.get('author_name')}* {'⭐' * int(r.get('rating', 0))}\n\"{(r.get('text') or '')[:100]}\"\n\n"
            reply = msg
        else:
            reply = "Google Reviews not connected (set place_id on your business)."

    # Review reply approval
    elif msg_lower in ["yes", "approve", "post it"]:
        pending = (
            supabase.table("pending_actions")
            .select("*")
            .eq("business_id", business["id"])
            .eq("action_type", "review_reply")
            .eq("status", "pending")
            .limit(1)
            .execute()
        )
        if pending.data:
            action = pending.data[0]
            supabase.table("pending_actions").update({"status": "completed"}).eq("id", action["id"]).execute()
            reply = f'✅ Reply saved:\n"{action.get("draft_reply")}"\n\nPaste this into Google Reviews.'
        else:
            reply = None  # fall through to AI

    # Default: Claude chat
    if reply is None:
        system = f"""You are Wavy AI for {business['name']}.
Commands: "check stock", "sync stock", "order [item]", "send order", "check reviews"
Be concise, under 150 words."""
        try:
            response = claude_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=400,
                system=system,
                messages=[{"role": "user", "content": incoming_msg}],
            )
            reply = response.content[0].text
        except Exception as e:
            reply = f"Sorry, I couldn't reach the AI: {e}"

    resp.message(reply)
    return str(resp)


@app.route("/health", methods=["GET"])
def health():
    return "Wavy AI — Stock Guard + Review Shield", 200


@app.route("/check-reviews", methods=["GET"])
def check_reviews_route():
    try:
        check_reviews_for_all_businesses()
        return "Review check ran. See logs for output.", 200
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return f"Error: {e}", 500


# =========================
# SCHEDULER
# =========================
def run_scheduler():
    schedule.every(4).hours.do(run_stock_monitor)
    schedule.every(30).minutes.do(check_reviews_for_all_businesses)
    schedule.every().monday.at("08:00").do(monday_stock_summary)
    run_stock_monitor()
    check_reviews_for_all_businesses()
    while True:
        schedule.run_pending()
        time.sleep(60)


def start_scheduler():
    try:
        t = threading.Thread(target=run_scheduler, daemon=True)
        t.start()
        print("✅ Wavy AI — Stock Guard + Review Shield active (stock every 4h, reviews every 30 min)")
    except Exception as e:
        print("[WARN] Scheduler not started:", e)


start_scheduler()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
