import time
import pandas as pd
from playwright.sync_api import sync_playwright
try:
    from playwright_stealth import stealth_sync
except ImportError:
    stealth_sync = None

from lxml import html
from datetime import datetime, timezone, timedelta
import requests
import json
import random
from collections import Counter

# --- 1. 設定區域 ---
GSHEET_INPUT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=1643541848&single=true&output=csv"
GAS_OUTPUT_URL = "https://script.google.com/macros/s/AKfycbwfA2vP2hdACwsEei73OSQUEojmXFRyyKqu_NcsDFi0Mp7oU2_fTUB1DCM2x4oFMWt7tA/exec"
TRACK_URL_DATA_CSV = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=0&single=true&output=csv"

# --- 2. 工具函式 ---

def is_data_complete(row_data):
    """ 檢查網址與 works 欄位是否都有有效資料 """
    fields_to_check = [
        "Landing page URL", "Link works",
        "Cat1 page URL", "Cat1 Link works",
        "Cat2 page URL", "Cat2 Link works",
        "Cat3 page URL", "Cat3 Link works",
        "Cat4 page URL", "Cat4 Link works"
    ]
    for field in fields_to_check:
        val = str(row_data.get(field, "")).strip().lower()
        if val in ["", "nan", "undefined", "none", "null", "n/a", "same", "crawler failed"]:
            return False
    return True

def is_intermediate_domain(url):
    """ 黑名單過濾：排除中介廣告頁與錯誤頁面 """
    if not url: return True
    url_lower = url.lower()
    blacklist = [
        "yahoo.com", "search.yahoo.com", "shopping.yahoo.com", 
        "chrome-error://", "chromewebdata", "access-denied", "accessdenied", 
        "viglink.com", "sovrn.com", "linkbux.com", "financebuzz.com"
    ]
    return any(k in url_lower for k in blacklist)

def get_link_status(page, response):
    """ 精準狀態偵測：結合狀態碼與標題特徵 """
    try:
        curr_url = page.url
        page_title = page.title().lower()
        if is_intermediate_domain(curr_url): return "Error"
        
        # 通用 404/狗狗頁面偵測
        not_found_keywords = ["page not found", "404", "dead end", "can't find that page", "dogs of amazon", "doesn't exist"]
        if any(k in page_title for k in not_found_keywords): return "404"
        
        # 高難度網站強制通行 (只要沒被擋 IP)
        hard_sites = ["lifelock.com", "booking.com", "hotels.com", "jcpenney.com", "lowes.com", "robinhood.com", "zappos.com", "hilton.com", "kohls.com", "statefarm.com"]
        if any(k in curr_url for k in hard_sites): return "Yes"
        
        if not response: return "No"
        status = response.status
        if status == 404: return "404"
        if status == 403 or "access denied" in page_title: return "Yes"
        return "Yes" if 200 <= status < 300 else "No"
    except: return "No"

def wait_for_redirect_smart(page, initial_url, retailer_name):
    """ 智慧跳轉等待邏輯 """
    try:
        hard_list = ["LifeLock", "Booking", "Hotels", "JCPenney", "Lowe", "Robinhood", "Zappos", "Hilton", "Amazon", "Kohls", "State Farm"]
        timeout = 55000 if any(k in retailer_name for k in hard_list) else 30000
        resp = page.goto(initial_url, wait_until="commit", timeout=timeout, referer="https://search.yahoo.com/")
        page.mouse.wheel(0, random.randint(300, 600))
        last_url = ""
        for _ in range(12):
            curr_url = page.url
            if curr_url == last_url and not is_intermediate_domain(curr_url):
                page.wait_for_timeout(1000)
                return resp
            last_url = curr_url
            page.wait_for_timeout(2500)
        return resp
    except: return None

# --- 3. 核心抓取函式 ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    data = {col: "" for col in column_order}
    data["Retailer"], data["SRP"] = retailer, srp_url
    titles_map = {}

    try:
        # Step 1: 加載 Yahoo SRP 頁面
        page.goto(srp_url, wait_until="domcontentloaded", timeout=40000)
        time.sleep(4) 
        tree = html.fromstring(page.content())
        
        # Step 2: 判定 DD (第一真相)
        dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
        has_dd_on_srp = False
        if not dd_label:
            data["DD Name"] = "DD cannot be found"
        else:
            data["DD Name"] = dd_label[0]
            has_dd_on_srp = True

        # Step 3: 抓取 Landing Page & Categories
        landing_raw = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
        if landing_raw:
            resp = wait_for_redirect_smart(page, landing_raw[0], retailer)
            if not is_intermediate_domain(page.url):
                data["Landing page URL"] = page.url
                data["Link works"] = get_link_status(page, resp)
                titles_map["Landing"] = page.title().strip()
            else: data["Link works"] = "Error"

        for i in range(1, 5):
            c_name = tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a//img/@alt")
            c_link = tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a/@href")
            cat_key = f"Cat{i}"
            data[f"{cat_key} Name"] = c_name[0] if c_name else "N/A"
            if c_link:
                data[f"{cat_key} Link URL"] = c_link[0]
                time.sleep(1.5)
                c_resp = wait_for_redirect_smart(page, c_link[0], retailer)
                if not is_intermediate_domain(page.url):
                    data[f"{cat_key} page URL"] = page.url
                    data[f"{cat_key} Link works"] = get_link_status(page, c_resp)
                    titles_map[cat_key] = page.title().strip()
                else: data[f"{cat_key} Link works"] = "Error"
            else: data[f"{cat_key} Link URL"] = "N/A"

        # Step 4: 標題重複性偵測 (same)
        title_counts = Counter([t for t in titles_map.values() if t])
        same_count = 0
        for key, title in titles_map.items():
            if title_counts[title] > 2:
                same_count += 1
                if key == "Landing": data["Link works"] = "same"
                else: data[f"{key} Link works"] = "same"

        # 💡 Step 5: 終極校驗邏輯
        # 只要 Yahoo 有 DD，但後續「網址抓取失敗」或「5 same 攔截」，一律標記為 crawler failed
        if has_dd_on_srp:
            if not data["Landing page URL"] or same_count >= 5 or not is_data_complete(data):
                print(f"   ⚠️ {retailer}: Yahoo 有 DD 但官網抓取異常，修正為 crawler failed")
                data["DD Name"] = "crawler failed"
                # 清空失敗的網址欄位以保持乾淨
                for col in ["Landing page URL", "Link works", "Cat1 page URL", "Cat1 Link works", "Cat2 page URL", "Cat2 Link works", "Cat3 page URL", "Cat3 Link works", "Cat4 page URL", "Cat4 Link works"]:
                    data[col] = ""

        data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        return data

    except Exception as e:
        print(f"   ❌ SRP 階段失敗: {e}")
        data["DD Name"] = "crawler failed"
        return data

# --- 4. 主流程 ---

def main():
    print("🚀 啟動優化版：解耦 DD 判定與官網完整性強制校驗")
    try:
        df_input = pd.read_csv(GSHEET_INPUT_URL)
    except: return

    existing_records = {}
    try:
        cb = random.randint(1000, 9999)
        fresh_url = f"{TRACK_URL_DATA_CSV}&t={int(time.time())}&cb={cb}"
        df_existing = pd.read_csv(fresh_url)
        for _, r in df_existing[::-1].iterrows():
            name = str(r['Retailer']).strip()
            if name not in existing_records: existing_records[name] = r.to_dict()
    except: pass

    column_order = ["Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works", 
                    "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
                    "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
                    "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
                    "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", viewport={"width": 1920, "height": 1080})
        page = context.new_page()
        if stealth_sync: stealth_sync(page)

        for index, row in df_input.iterrows():
            retailer_name = str(row['Retailer']).strip()
            srp_url = str(row.get('SRP', ''))
            should_crawl = True
            
            if retailer_name in existing_records:
                old = existing_records[retailer_name]
                old_dd = str(old.get('DD Name', '')).strip().lower()
                # 判定重爬：空值、標記失敗、或標記 DD cannot be found，皆強制每小時重試
                if old_dd == "" or "crawler failed" in old_dd or "dd cannot be found" in old_dd:
                    should_crawl = True
                else:
                    try:
                        old_date = datetime.strptime(str(old.get('Update Date', '')).replace(" UTC", ""), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                        # 資料完整且在 7 天內，則 Skip
                        if (datetime.now(timezone.utc) - old_date < timedelta(days=7)) and is_data_complete(old):
                            print(f"⏭️  Skip: {retailer_name}")
                            should_crawl = False
                    except: pass

            if not should_crawl: continue

            print(f"🔍 Processing: {retailer_name}")
            result = run_retailer_capture(page, row, column_order)
            
            if result:
                try:
                    requests.post(GAS_OUTPUT_URL, json=result, timeout=25)
                    print(f"   ✅ {retailer_name} 資料傳送成功 (DD: {result['DD Name']})")
                except: print(f"   ❌ {retailer_name} 傳送失敗")
            
            time.sleep(random.uniform(5, 10))
        browser.close()

if __name__ == "__main__":
    main()