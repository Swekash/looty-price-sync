import os, time, re, json, logging
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── CONFIGURATION ──────────────────────────────────────────────────────────
SHEET_ID          = os.environ.get("SHEET_ID")
TAB_NAME          = os.environ.get("TAB_NAME", "Other_Gadgets")
CREDS_JSON        = os.environ.get("GOOGLE_CREDS_JSON_CONTENT")
BOT_TOKEN         = os.environ.get("BOT_TOKEN")
CHAT_ID           = os.environ.get("TELEGRAM_CHAT_ID")
AMAZON_CLIENT_ID  = os.environ.get("AMAZON_CLIENT_ID")
AMAZON_CLIENT_SECRET = os.environ.get("AMAZON_CLIENT_SECRET")
AMAZON_TAG        = os.environ.get("AMAZON_TAG", "looty08-21")

ALERT_DROP_THRESHOLD = 0.20   # Alert if price drops 20%+

TOKEN_URL = "https://api.amazon.co.uk/auth/o2/token"
CATALOG_URL = "https://creatorsapi.amazon/catalog/v1/getItems"

# ─── AMAZON AUTH ─────────────────────────────────────────────────────────────
_access_token = None
_token_expiry = 0

def get_access_token():
    global _access_token, _token_expiry
    if _access_token and time.time() < _token_expiry - 60:
        return _access_token
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "client_credentials",
        "client_id":     AMAZON_CLIENT_ID,
        "client_secret": AMAZON_CLIENT_SECRET,
        "scope":         "creatorsapi::default",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    _access_token = data["access_token"]
    _token_expiry = time.time() + data.get("expires_in", 3600)
    log.info("Amazon token refreshed.")
    return _access_token

# ─── FETCH PRICES ─────────────────────────────────────────────────────────────
def fetch_prices(asins: list[str]) -> dict[str, int]:
    """Returns {asin: price_in_rupees} for a batch of up to 10 ASINs."""
    token = get_access_token()
    payload = {
        "asins": asins,
        "resources": ["offersV2.listings.price", "offersV2.listings.dealDetails"],
        "tag": AMAZON_TAG,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "x-marketplace": "www.amazon.in",
    }
    resp = requests.post(CATALOG_URL, json=payload, headers=headers, timeout=20)
    resp.raise_for_status()
    result = {}
    items = resp.json().get("items") or resp.json().get("itemsResult", {}).get("items", [])
    for item in items:
        asin = item.get("asin")
        try:
            listings = item["offersV2"]["listings"]
            amount = listings[0]["price"]["amount"]
            result[asin] = int(str(amount).split(".")[0].replace(",", ""))
        except (KeyError, IndexError, TypeError):
            pass   # leaves asin absent → treated as no price found
    return result

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def extract_asin(url):
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", str(url))
    return m.group(1) if m else None

def clean_price(raw):
    if not raw: return 0
    digits = re.sub(r"[^\d]", "", str(raw).split(".")[0])
    return int(digits) if digits else 0

def tg_send(text):
    if not BOT_TOKEN or not CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        log.error(f"Telegram send failed: {e}")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    log.info("Starting Looty Price Sync...")

    # 1. GSheet
    creds_data = json.loads(CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SHEET_ID).worksheet(TAB_NAME)

    data = ws.get_all_values()
    if len(data) < 2:
        log.info("No data rows found.")
        return

    headers, rows = data[0], data[1:]
    col = {name: i for i, name in enumerate(headers)}
    idx_link    = col.get("AmazonLink")
    idx_price   = col.get("Price")
    idx_updated = col.get("Updated")
    idx_name    = col.get("Name")

    # 2. Collect ASINs
    asin_to_row = {}
    for i, row in enumerate(rows):
        while len(row) < len(headers): row.append("")
        asin = extract_asin(row[idx_link])
        if asin:
            asin_to_row[asin] = i

    asins = list(asin_to_row.keys())
    log.info(f"Found {len(asins)} products to check.")

    updated_count = 0
    today_str = datetime.now().strftime("%d-%b-%Y")

    # 3. Batch in chunks of 10
    for i in range(0, len(asins), 10):
        batch = asins[i:i+10]
        log.info(f"Batch {i//10 + 1}: {batch}")
        try:
            prices = fetch_prices(batch)
            for asin in batch:
                if asin not in prices:
                    log.warning(f"No price for {asin} — skipping.")
                    continue
                new_price = prices[asin]
                row_idx   = asin_to_row[asin]
                old_price = clean_price(rows[row_idx][idx_price])
                name      = rows[row_idx][idx_name]

                if new_price == old_price:
                    continue   # no change, leave untouched

                # Price drop alert
                if old_price > 0:
                    drop = (old_price - new_price) / old_price
                    if drop >= ALERT_DROP_THRESHOLD:
                        tg_send(
                            f"🚨 *PRICE DROP ALERT*\n\n"
                            f"*{name}*\n"
                            f"Rs.{old_price} → *Rs.{new_price}*\n"
                            f"({int(drop*100)}% drop!)\n\n"
                            f"Check sheet to post."
                        )

                rows[row_idx][idx_price]   = str(new_price)
                rows[row_idx][idx_updated] = today_str
                updated_count += 1
                log.info(f"{asin} | {name[:30]} | {old_price} → {new_price}")

        except Exception as e:
            log.error(f"Batch failed: {e}")

        time.sleep(2)

    # 4. Write back once
    if updated_count > 0:
        log.info(f"Writing {updated_count} updated rows to sheet...")
        ws.update(
            values=[headers] + rows,
            range_name=f"A1:{chr(65 + len(headers) - 1)}{len(rows) + 1}"
        )
        log.info("Done.")
    else:
        log.info("No price changes. Sheet untouched.")

if __name__ == "__main__":
    main()
