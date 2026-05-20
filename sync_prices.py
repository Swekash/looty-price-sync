import os, time, re, json, logging
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
import requests
from amazon_creatorsapi import AmazonCreatorsApi, Country

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── CONFIGURATION ──────────────────────────────────────────────────────────
SHEET_ID = os.environ.get("SHEET_ID")
TAB_NAME = os.environ.get("TAB_NAME", "Other_Gadgets")
CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON_CONTENT")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
AMAZON_CLIENT_ID = os.environ.get("AMAZON_CLIENT_ID")
AMAZON_CLIENT_SECRET = os.environ.get("AMAZON_CLIENT_SECRET")
AMAZON_TAG = os.environ.get("AMAZON_TAG", "looty08-21")

ALERT_DROP_THRESHOLD = 0.20  # Alert if price drops by 20% or more

# ─── HELPERS ────────────────────────────────────────────────────────────────
def extract_asin(url):
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", str(url))
    return m.group(1) if m else None

def clean_price(raw):
    return int(re.sub(r"[^\d]", "", str(raw).split(".")[0])) if raw else 0

def tg_send(text):
    if not BOT_TOKEN or not CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        log.error(f"Telegram send failed: {e}")

# ─── MAIN LOGIC ─────────────────────────────────────────────────────────────
def main():
    log.info("Starting Price Sync Bot...")
    
    # 1. Init GSheet Client
    creds_data = json.loads(CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(TAB_NAME)
    
    # Fetch all data
    data = ws.get_all_values()
    if len(data) < 2:
        log.info("No data to process.")
        return
    
    headers = data[0]
    rows = data[1:]
    
    # Map column indexes
    col_idx = {name: i for i, name in enumerate(headers)}
    idx_link = col_idx.get("AmazonLink")
    idx_price = col_idx.get("Price")
    idx_updated = col_idx.get("Updated")
    idx_name = col_idx.get("Name")
    
    # 2. Init Amazon API
    api = AmazonCreatorsApi(
        credential_id=AMAZON_CLIENT_ID,
        credential_secret=AMAZON_CLIENT_SECRET,
        version="2.3",
        tag=AMAZON_TAG,
        country=Country.IN,
    )
    
    # 3. Extract ASINs and build a map
    asin_to_row = {}
    for i, row in enumerate(rows):
        # Ensure row has enough columns
        while len(row) < len(headers): row.append("")
        asin = extract_asin(row[idx_link])
        if asin:
            asin_to_row[asin] = i
    
    asins = list(asin_to_row.keys())
    log.info(f"Found {len(asins)} products to check.")
    
    updated_count = 0
    today_str = datetime.now().strftime("%d-%b-%Y")
    
    # 4. Batch query Amazon API (10 items at a time)
    for i in range(0, len(asins), 10):
        batch_asins = asins[i:i+10]
        log.info(f"Checking batch: {batch_asins}")
        
        try:
            items = api.get_items(batch_asins)
            
            for item in items:
                asin = item.asin
                row_idx = asin_to_row[asin]
                old_price_str = rows[row_idx][idx_price]
                old_price = clean_price(old_price_str)
                name = rows[row_idx][idx_name]
                
                new_price = 0
                
                # Extract new price
                if item.offers_v2 and item.offers_v2.listings:
                    listing = item.offers_v2.listings[0]
                    new_price = clean_price(listing.price.money.amount)
                
                # Check conditions and update
                if new_price == 0:
                    rows[row_idx][idx_price] = "0"
                    rows[row_idx][idx_updated] = today_str
                    updated_count += 1
                elif new_price != old_price:
                    # Check for massive drop
                    if old_price > 0:
                        drop_ratio = (old_price - new_price) / old_price
                        if drop_ratio >= ALERT_DROP_THRESHOLD:
                            tg_send(f"🚨 **MASSIVE PRICE DROP** 🚨\n\n{name}\nDropped from Rs.{old_price} to **Rs.{new_price}**!\n\nCheck sheet to post.")
                    
                    rows[row_idx][idx_price] = str(new_price)
                    rows[row_idx][idx_updated] = today_str
                    updated_count += 1
                    
        except Exception as e:
            log.error(f"Failed to fetch batch {batch_asins}: {e}")
        
        # Sleep to respect API rate limits
        time.sleep(2)
        
    # 5. Write back to Google Sheets in one single API call
    if updated_count > 0:
        log.info(f"Updating {updated_count} rows in GSheet...")
        ws.update(values=[headers] + rows, range_name=f"A1:{chr(65+len(headers)-1)}{len(rows)+1}")
        log.info("Sheet updated successfully.")
    else:
        log.info("No prices changed.")

if __name__ == "__main__":
    main()