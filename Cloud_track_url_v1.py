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

# --- 1. 設定區域 ---
GSHEET_INPUT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=1643541848&single=true&output=csv"
GAS_OUTPUT_URL = "https://script.google.com/macros/s/AKfycbwfA2vP2hdACwsEei73OSQUEojmXFRyyKqu_NcsDFi0Mp7oU2_fTUB1DCM2x4oFMWt7tA/exec"
TRACK_URL_DATA_CSV = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=0&single=true&output=csv"

# --- 2. 工具函式 ---

def is_data_complete(row_data):
    fields_to_check = [
        "Landing page URL", "Link works",
        "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
        "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
        "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
        "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"
    ]
    for field in fields_to_check:
        val = str(row_data.get(field, "")).strip().lower()
        if val in ["", "nan", "undefined", "none", "null", "n/a"]:
            return False
    return True

def is_intermediate_domain(url):
    if not url: return True
    url_lower = url.lower()
    blacklist = [
        "yahoo.com", "search.yahoo.com", "shopping.yahoo.com", 
        "chrome-error://", "chromewebdata", "access-denied", "accessdenied",
        "affinity.net", "bizrate.com", "ebay.com/rover", "rover.ebay.com", "peakoptions.site", "clickroll.net"
    ]
    return any(k in url_lower for k in blacklist)

def get_link_status(page, response):
    try:
        curr_url = page.url
        if is_intermediate_domain(curr_url): return "Error"
        # 針對難抓網站，只要能進去就給 Yes
        if any(k in curr_url for k in ["ebay.com", "hotels.com", "jcpenney.com", "lowes.com", "rei.com", "zappos.com", "robinhood.com", "lifelock.com"]): 
            return "Yes"
        if not response: return "No"
        status = response.status
        if status == 403 or "access denied" in page.title().lower(): return "Yes"
        return "Yes" if 200 <= status < 300 else "No"
    except: return "No"

def wait_for_redirect_smart(page, initial_url, retailer_name):
    try:
        hard_list = ["Hotels.com", "JCPenney", "Lowe's", "REI", "Zappos", "Robinhood", "LifeLock"]
        timeout = 40000 if any(k in retailer_name for k in hard_list) else 25000
        resp = page.goto(initial_url, wait_until="commit", timeout=timeout)
        page.mouse.wheel(0, random.randint(300, 600))
        for _ in range(8): 
            curr_url = page.url
            if "ebay.com" in curr_url and "rover" not in curr_url: return resp
            if not is_intermediate_domain(curr_url): return resp
            page.wait_for_timeout(2000) 
        return resp
    except: return None

# --- 3. 核心抓取函式 ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    data = {col: "" for col in column_order}
    data["Retailer"], data["SRP"] = retailer, srp_url
    
    # 💡 預期找不到 DD 的清單
    expected_no_dd = ["LifeLock", "REI", "Nordstrom", "Nordstrom Rack"]
    
    try:
        page.goto(srp_url, wait_until="domcontentloaded", timeout=35000)
        time.sleep(3) 
        tree = html.fromstring(page.content())
        
        dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
        
        # 💡 處理找不到 DD 的情況
        if not dd_label:
            if any(k in retailer for k in expected_no_dd):
                data["DD Name"] = "DD cannot be found"
                data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                return data
            return None # 非預期無 DD 網站，回傳 None 以便觸發「保留舊資料」邏輯
            
        data["DD Name"] = dd_label[0]

        if data["DD Name"].lower() == "dd cannot be found":
            data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            return data

        landing_raw = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
        cat_links = []
        for i in range(1, 5):
            c_name = tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a//img/@alt")
            c_link = tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a/@href")
            cat_links.append({"name": c_name[0] if c_name else "N/A", "link": c_link[0] if c_link else None})

        # 處理 Landing Page
        last_captured_url = ""
        if landing_raw:
            resp = wait_for_redirect_smart(page, landing_raw[0], retailer)
            curr_url = page.url
            if not is_intermediate_domain(curr_url):
                data["Link works"] = get_link_status(page, resp)
                data["Landing page URL"] = curr_url
                last_captured_url = curr_url

        # 處理 Cat 1-4
        for i, cat in enumerate(cat_links):
            cat_key = f"Cat{i+1}"
            data[f"{cat_key} Name"] = cat["name"]
            if cat["link"]:
                data[f"{cat_key} Link URL"] = cat["link"]
                c_resp = wait_for_redirect_smart(page, cat["link"], retailer)
                curr_actual_url = page.url
                # 💡 防止 URL 重複
                if not is_intermediate_domain(curr_actual_url) and curr_actual_url != last_captured_url:
                    data[f"{cat_key} page URL"] = curr_actual_url
                    data[f"{cat_key} Link works"] = get_link_status(page, c_resp)
                    last_captured_url = curr_actual_url
                else:
                    data[f"{cat_key} page URL"] = ""
                    data[f"{cat_key} Link works"] = "Error"
            else:
                data[f"{cat_key} Link URL"] = "N/A"

        if not data["Landing page URL"] or is_intermediate_domain(data["Landing page URL"]):
            return None

        data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        return data
    except: return None

# --- 4. 主流程 ---

def main():
    print("🚀 啟動強化任務：資料保護與強制 NF 標記版")
    try:
        df_input = pd.read_csv(GSHEET_INPUT_URL)
    except: return

    existing_records = {}
    try:
        cache_breaker = random.randint(1000, 9999)
        fresh_url = f"{TRACK_URL_DATA_CSV}&t={int(time.time())}&cb={cache_breaker}"
        df_existing = pd.read_csv(fresh_url)
        for _, r in df_existing[::-1].iterrows():
            name = str(r['Retailer']).strip()
            if name not in existing_records: existing_records[name] = r.to_dict()
    except: pass

    column_order = [
        "Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works",
        "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
        "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
        "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
        "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US", timezone_id="America/New_York",
            viewport={"width": 1920, "height": 1080},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Upgrade-Insecure-Requests": "1"
            }
        )
        page = context.new_page()
        if stealth_sync: stealth_sync(page)

        now = datetime.now(timezone.utc)

        for index, row in df_input.iterrows():
            retailer_name = str(row['Retailer']).strip()
            srp_url = str(row.get('SRP', ''))
            
            should_crawl = True
            if retailer_name in existing_records:
                old = existing_records[retailer_name]
                old_dd = str(old.get('DD Name', '')).strip()
                if old_dd.lower() == 'nan': old_dd = ""
                old_date_str = str(old.get('Update Date', '')).strip()
                if old_date_str.lower() == 'nan': old_date_str = ""
                
                if not old_dd:
                    should_crawl = True
                else:
                    within_7_days = False
                    try:
                        old_date = datetime.strptime(old_date_str.replace(" UTC", ""), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                        if now - old_date < timedelta(days=7): within_7_days = True
                    except: pass
                    
                    if old_dd.lower() == "dd cannot be found" and within_7_days:
                        print(f"⏭️  Skip: {retailer_name}")
                        should_crawl = False
                    elif old_dd != "" and within_7_days and is_data_complete(old):
                        print(f"⏭️  Skip: {retailer_name}")
                        should_crawl = False

            if not should_crawl: continue

            print(f"🔍 ({index+1}/{len(df_input)}) Processing: {retailer_name}")
            result = run_retailer_capture(page, row, column_order)
            
            # 💡 判斷是否要發送寫入請求
            if result and (str(result.get("DD Name")).lower() == "dd cannot be found" or is_data_complete(result)):
                try:
                    requests.post(GAS_OUTPUT_URL, json=result, timeout=25)
                    print(f"   ✅ {retailer_name} 覆蓋更新成功")
                except: print(f"   ❌ {retailer_name} 傳送失敗")
            else:
                # 💡 資料失敗時，不發送任何 Post 請求給 GAS，保留原有試算表內容
                print(f"   ⚠️ {retailer_name} 抓取受阻，保留舊資料不變。")
            
            time.sleep(random.uniform(5, 8))

        browser.close()
    print("🎉 任務完成！")

if __name__ == "__main__":
    main()