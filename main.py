import imaplib
import email
from email.header import decode_header
import re
import os
import requests
from datetime import datetime
from supabase import create_client, Client


# --- CONFIG (from GitHub Actions Secrets) ---
EMAIL_ACCOUNT   = os.environ["GMAIL_USER"]
APP_PASSWORD    = os.environ["GMAIL_PASSWORD"]
TARGET_SENDER   = "accounts@crepdogcrew.com"
IMAP_SERVER     = "imap.gmail.com"

SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]

APPS_SCRIPT_URL = os.environ["APPS_SCRIPT_URL"]  # Google Apps Script webhook URL


# --- SUPABASE CLIENT ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ──────────────────────────────────────────
#  SUPABASE STATE  (tracks last seen email)
# ──────────────────────────────────────────

def get_last_seen_id() -> str:
    res = (
        supabase
        .table("email_tracker")
        .select("value")
        .eq("key", "last_seen_id")
        .execute()
    )
    return res.data[0]["value"] if res.data else ""


def set_last_seen_id(email_id: str):
    supabase.table("email_tracker").update({"value": email_id}).eq("key", "last_seen_id").execute()
    print(f"   💾 Saved last_seen_id → {email_id}")


# ──────────────────────────────────────────
#  IMAP HELPERS
# ──────────────────────────────────────────

def connect() -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ACCOUNT, APP_PASSWORD)
    return mail


def decode_str(value: str) -> str:
    decoded, charset = decode_header(value)[0]
    if isinstance(decoded, bytes):
        return decoded.decode(charset or "utf-8", errors="replace")
    return decoded


def get_body(msg) -> str:
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


def fetch_latest_from(mail: imaplib.IMAP4_SSL, sender: str) -> dict | None:
    """Used only on first run to set the baseline ID."""
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


def fetch_all_new_from(mail: imaplib.IMAP4_SSL, sender: str, last_seen_id: str) -> list:
    """Fetches ALL emails newer than last_seen_id — handles multiple new emails per run."""
    mail.select("INBOX")
    status, data = mail.search(None, f'FROM "{sender}"')

    if status != "OK" or not data[0]:
        return []

    email_ids = data[0].split()

    # Only keep IDs greater than last seen
    new_ids = [eid for eid in email_ids if int(eid) > int(last_seen_id)]

    if not new_ids:
        return []

    print(f"   📬 Found {len(new_ids)} new email(s) to process")

    emails = []
    for eid in new_ids:
        status, msg_data = mail.fetch(eid, "(RFC822)")
        if status != "OK":
            continue
        raw_email = msg_data[0][1]
        msg       = email.message_from_bytes(raw_email)
        emails.append({
            "subject": decode_str(msg.get("Subject", "")),
            "from":    msg.get("From", ""),
            "date":    msg.get("Date", ""),
            "body":    get_body(msg),
            "id":      eid.decode()
        })

    return emails


# ──────────────────────────────────────────
#  EMAIL PARSING
# ──────────────────────────────────────────

def parse_payout_email(body: str) -> dict:
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
    date_match  = re.search(r"processed by CDC\* on \*(\d{2} \w+ \d{4})\*", clean_text)
    raw_date    = date_match.group(1).strip() if date_match else None
    payout_date = datetime.strptime(raw_date, "%d %b %Y").strftime("%Y-%m-%d") if raw_date else "N/A"

    def to_int(rupee_str: str) -> int:
        return int(rupee_str.replace("₹", "").replace(",", ""))

    # FORMAT 1: "526010733_Aj1 Low Sail College Grey Rattan -Uk 8₹8,315"
    products_raw = re.findall(r"(\d+)_([^₹]+?)(₹[\d,]+)", clean_text)
    if products_raw:
        print("📋 Detected Format: Detailed (barcode_name₹amount)")
        products = [
            {
                "id":         pid,
                "name":       name.strip(),
                "amount_str": amt,
                "amount_int": to_int(amt)
            }
            for pid, name, amt in products_raw
        ]
    else:
        # FORMAT 2: "5241011275 ₹8,700" (barcode space amount, no name)
        products_raw = re.findall(r"(\d{10})\s+(₹[\d,]+)", clean_text)
        print("📋 Detected Format: Simple (barcode ₹amount)")
        products = [
            {
                "id":         pid,
                "name":       "N/A",
                "amount_str": amt,
                "amount_int": to_int(amt)
            }
            for pid, amt in products_raw
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
        print(f"   ID: {p['id']}  |  {p['name']}  |  {p['amount_str']}")
    print("-" * 42)
    print(f"💰 Total Payout : {total_str}")
    print("=" * 42)

    return {
        "payout_date": payout_date,
        "products":    products,
        "total_int":   total_int,
        "total_str":   total_str
    }


# ──────────────────────────────────────────
#  GOOGLE SHEETS VIA APPS SCRIPT WEBHOOK
# ──────────────────────────────────────────

def write_to_sheets(parsed: dict, validated_barcodes: set):
    print("\n📊 Writing to Google Sheets via Apps Script...")

    rows_written = 0
    for product in parsed["products"]:
        validation = "Validated" if product["id"] in validated_barcodes else "Not Validated"

        payload = {
            "date":        parsed["payout_date"],  # A
            "barcode":     product["id"],           # B
            "description": product["name"],         # C
            "amount":      product["amount_str"],   # D
            "validation":  validation               # E
        }

        response = requests.post(
            APPS_SCRIPT_URL,
            json=payload,
            headers={"Content-Type": "application/json"}
        )

        if response.status_code == 200:
            rows_written += 1
            print(f"   ✅ {product['id']} | {product['name']} | {product['amount_str']} | {validation}")
        else:
            print(f"   ❌ Failed for {product['id']} — {response.status_code}: {response.text}")

    print(f"✅ {rows_written} rows written to Sheets.\n")


# ──────────────────────────────────────────
#  SUPABASE SYNC
# ──────────────────────────────────────────

def update_supabase(parsed: dict) -> set:
    """Updates Supabase and returns a set of validated barcodes for Sheets."""
    print("\n🔄 Updating Supabase...")
    validated_barcodes = set()

    for product in parsed["products"]:
        barcode = product["id"]

        # STEP 1: Validate barcode exists in sales table
        sales_check = (
            supabase
            .table("sales")
            .select("barcode")
            .eq("barcode", barcode)
            .execute()
        )

        if not sales_check.data:
            print(f"   🚫 Barcode {barcode} NOT found in sales — will mark Not Validated in Sheets")
            continue  # not added to validated_barcodes

        print(f"   ✔️  Barcode {barcode} validated in sales table")
        validated_barcodes.add(barcode)

        # STEP 2: Fetch row from payment_trackers
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

        # STEP 3: Update payment_trackers
        update_response = (
            supabase
            .table("payment_trackers")
            .update({
                "received_amount": sale_amount,
                "balance":         0,
                "status":          "paid"
            })
            .eq("id", row_id)
            .execute()
        )

        if update_response.data:
            print(f"   ✅ Barcode {barcode} → received_amount={sale_amount}, balance=0, status=paid")
        else:
            print(f"   ❌ Update failed for barcode {barcode}")

    print("✅ Supabase sync complete.\n")
    return validated_barcodes


# ──────────────────────────────────────────
#  MAIN  (runs once per GitHub Actions job)
# ──────────────────────────────────────────

def main():
    print("🚀 Starting inbox check...")
    print(f"SUPABASE URL: {SUPABASE_URL}")
    
    mail         = connect()
    last_seen_id = get_last_seen_id()

    print(f"📌 Last seen ID : {last_seen_id or 'None (first run)'}")

    # First ever run — set baseline and exit
    if last_seen_id == "":
        result = fetch_latest_from(mail, TARGET_SENDER)
        mail.logout()
        if result:
            set_last_seen_id(result["id"])
            print("📌 First run — baseline set. Will detect new emails from next run.")
        else:
            print("📭 No emails found from target sender.")
        return

    # Fetch all emails newer than last seen
    new_emails = fetch_all_new_from(mail, TARGET_SENDER, last_seen_id)
    mail.logout()

    if not new_emails:
        print("📭 No new emails since last run. Nothing to do.")
        return

    print(f"\n📬 {len(new_emails)} NEW EMAIL(S) DETECTED!")

    for result in new_emails:
        print(f"\n─── Processing Email ID: {result['id']} ───")
        set_last_seen_id(result["id"])
        parsed             = parse_payout_email(result["body"])
        validated_barcodes = update_supabase(parsed)
        write_to_sheets(parsed, validated_barcodes)

    print(f"\n🏁 Done — processed {len(new_emails)} email(s).")


main()

