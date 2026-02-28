from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
from supabase import create_client

# =========================
# KEYS – use env vars (set in Railway / .env locally)
# =========================

import os
import time

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://znrvyoriduvtfbjdlhmm.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# =========================
# CLIENTS
# =========================

# Longer timeout so Railway can reach Anthropic (avoids APIConnectionError)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)

# =========================
# WHATSAPP WEBHOOK
# =========================

@app.route("/webhook/whatsapp", methods=["POST"])
def webhook():

    from_number = request.form.get("From")
    user_message = request.form.get("Body")

    # Normalize phone: Twilio sends "whatsapp:+971564556554", Supabase may have "+971564556554" or "971564556554"
    owner_phone = from_number.replace("whatsapp:", "").strip() if from_number else ""

    print("FROM:", from_number)
    print("MESSAGE:", user_message)

    resp = MessagingResponse()

    # -------------------------
    # Get business instructions (match normalized phone)
    # -------------------------
    result = (
        supabase.table("businesses")
        .select("custom_instructions, name")
        .eq("owner_phone", owner_phone)
        .limit(1)
        .execute()
    )

    # Debug: see what Supabase returned (remove these prints once it works)
    print("[DEBUG] owner_phone used for query:", repr(owner_phone))
    print("[DEBUG] result.data:", result.data)

    if result.data and len(result.data) > 0:
        row = result.data[0]
        custom_instructions = row["custom_instructions"] or "You are a helpful business assistant."
        business_name = row.get("name", "this business")
        custom_instructions = f"You are Wavy AI, the operations manager for {business_name}. {custom_instructions}"
        print("[DEBUG] system prompt (first 200 chars):", custom_instructions[:200])
    else:
        custom_instructions = "You are a helpful business assistant."
        print("[DEBUG] No business row found for this phone – using generic prompt.")

    # -------------------------
    # AI RESPONSE (with retry for connection errors)
    # -------------------------
    reply_text = None
    last_error = None
    for attempt in range(3):
        try:
            ai_response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=500,
                system=custom_instructions,
                messages=[
                    {
                        "role": "user",
                        "content": user_message
                    }
                ]
            )
            reply_text = ai_response.content[0].text
            break
        except anthropic.APIConnectionError as e:
            last_error = e
            if attempt < 2:
                print(f"[RETRY] Attempt {attempt + 1} failed: {e}. Retrying in 2s...")
                time.sleep(2)
            else:
                import traceback
                print("[ERROR]", type(e).__name__, str(e))
                print(traceback.format_exc())
                reply_text = "Sorry, I couldn't reach the AI right now. Please try again in a moment."
        except Exception as e:
            import traceback
            print("[ERROR]", type(e).__name__, str(e))
            print(traceback.format_exc())
            reply_text = f"Sorry, something went wrong: {str(e)}. Please try again later."
            break

    if reply_text is None and last_error:
        reply_text = "Sorry, I couldn't reach the AI right now. Please try again in a moment."

    resp.message(reply_text)

    return str(resp)


# =========================
# REVIEW MONITOR (every 30 min)
# =========================

def start_review_scheduler():
    import schedule
    import threading
    from review_monitor import run_review_check
    schedule.every(30).minutes.do(run_review_check)
    run_review_check()  # run once on start
    def run():
        while True:
            schedule.run_pending()
            time.sleep(60)
    t = threading.Thread(target=run, daemon=True)
    t.start()

# =========================
# RUN SERVER
# =========================

# Start review monitor (runs in background; works when run as script or by gunicorn)
start_review_scheduler()

if __name__ == "__main__":
    app.run(port=5000, debug=True)