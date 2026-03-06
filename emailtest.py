import imaplib
import email
from email.header import decode_header
import time
import re
from datetime import datetime
from supabase import create_client, Client

# --- CONFIG ---
EMAIL_ACCOUNT  = "alkresellshoes21@gmail.com"
APP_PASSWORD   = "fikv fdjt szis ozdy"                          # 16-char Gmail App Password
TARGET_SENDER  = "accounts@crepdogcrew.com"
IMAP_SERVER    = "imap.gmail.com"
POLL_INTERVAL  = 30

SUPABASE_URL   = "https://axoxgfbdlaqmaftwqlxp.supabase.co"
SUPABASE_KEY   = "sb_publishable_a9dG4W6EfKvvhvku7Ffbaw_kBp-SsJi"


# --- SUPABASE CLIENT ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def connect():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ACCOUNT, APP_PASSWORD)
    return mail


def decode_str(value):
    decoded, charset = decode_header(value)[0]
    if isinstance(decoded, bytes):
        return decoded.decode(charset or "utf-8", errors="replace")
    return decoded


def get_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition  = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
    else:
        charset = msg.get_content_charset() or "utf-8"
        return msg.get_payload(decode=True).decode(charset, errors="replace")
    return ""


def fetch_latest_from(mail, sender):
    mail.select("INBOX")
    status, data = mail.search(None, f'FROM "{sender}"')

    if status != "OK" or not data[0]:
        return None

    email_ids = data[0].split()
    latest_id = email_ids[-1]

    status, msg_data = mail.fetch(latest_id, "(RFC822)")
    if status != "OK":
        return None

    raw_email = msg_data[0][1]
    msg       = email.message_from_bytes(raw_email)

    return {
        "subject": decode_str(msg.get("Subject", "")),
        "from":    msg.get("From", ""),
        "date":    msg.get("Date", ""),
        "body":    get_body(msg),
        "id":      latest_id.decode()
    }


def parse_payout_email(body):
    # Deduplicate repeated lines from forwarded email copies
    lines = body.splitlines()
    seen, clean = set(), []
    for line in lines:
        stripped = line.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            clean.append(stripped)

    clean_text = "\n".join(clean)

    # Payout date
    date_match  = re.search(r"processed by CDC\* on \*(.+?)\*", clean_text)
    raw_date    = date_match.group(1).strip() if date_match else None
    payout_date = datetime.strptime(raw_date, "%d %b %Y").strftime("%Y-%m-%d") if raw_date else "N/A"

    # Products — "5251230183_Aj1 Low Denim Star Blue -Uk 8 ₹9,100"
    products_raw = re.findall(r"(\d+)_([^\n₹]+?)\s+(₹[\d,]+)", clean_text)

    def to_int(rupee_str):
        return int(rupee_str.replace("₹", "").replace(",", ""))

    products = [
        {
            "id":         pid,
            "name":       name.strip(),
            "amount_str": amt,
            "amount_int": to_int(amt)
        }
        for pid, name, amt in products_raw
    ]

    # Total
    total_match = re.search(r"Total Payout\*\s+\*(₹[\d,]+)\*", clean_text)
    total_str   = total_match.group(1) if total_match else "N/A"
    total_int   = to_int(total_str) if total_str != "N/A" else 0

    print("=" * 42)
    print(f"📅 Payout Date  : {payout_date}")
    print("-" * 42)
    print("📦 Products:")
    for p in products:
        print(f"   ID: {p['id']}  |  {p['amount_str']}")
    print("-" * 42)
    print(f"💰 Total Payout : {total_str}")
    print("=" * 42)

    return {
        "payout_date": payout_date,
        "products":    products,
        "total_int":   total_int,
        "total_str":   total_str
    }


def update_supabase(parsed):
    print("\n🔄 Updating Supabase...")

    for product in parsed["products"]:
        barcode = product["id"]

        # ─── STEP 1: Validate barcode exists in sales table ───────────────
        sales_check = (
            supabase
            .table("sales")
            .select("barcode")
            .eq("barcode", barcode)
            .execute()
        )

        if not sales_check.data:
            print(f"   🚫 Barcode {barcode} NOT found in sales table — invalid, skipping")
            continue  # Skip this product entirely, don't touch payment_trackers

        print(f"   ✔️  Barcode {barcode} validated in sales table")

        # ─── STEP 2: Fetch row from payment_trackers ──────────────────────
        response = (
            supabase
            .table("payment_trackers")
            .select("id, barcode, sale_amount")
            .eq("barcode", barcode)
            .execute()
        )

        if not response.data:
            print(f"   ⚠️  Barcode {barcode} NOT found in payment_trackers — skipping")
            continue

        row         = response.data[0]
        sale_amount = row["sale_amount"]
        row_id      = row["id"]

        # ─── STEP 3: Update payment_trackers ──────────────────────────────
        update_response = (
            supabase
            .table("payment_trackers")
            .update({
                "received_amount": sale_amount,
                "balance":        0,
                "status":         "paid"
            })
            .eq("id", row_id)
            .execute()
        )

        if update_response.data:
            print(f"   ✅ Barcode {barcode} → received_amount={sale_amount}, balance=0, status=paid")
        else:
            print(f"   ❌ Update failed for barcode {barcode}")

    print("✅ Supabase sync complete.\n")


def wait_for_new_email(sender):
    print(f"👀 Watching for emails from: {sender}")
    print(f"🔁 Polling every {POLL_INTERVAL}s\n")
    last_seen_id = None

    while True:
        try:
            mail   = connect()
            result = fetch_latest_from(mail, sender)
            mail.logout()

            if result:
                current_id = result["id"]

                if last_seen_id is None:
                    last_seen_id = current_id
                    print(f"📌 Baseline set. Watching for emails after ID: {current_id}")

                elif current_id != last_seen_id:
                    last_seen_id = current_id
                    print("\n📬 NEW EMAIL RECEIVED!")
                    parsed = parse_payout_email(result["body"])
                    update_supabase(parsed)

        except Exception as e:
            print(f"⚠️  Error: {e}")

        time.sleep(POLL_INTERVAL)


# Run
wait_for_new_email(TARGET_SENDER)