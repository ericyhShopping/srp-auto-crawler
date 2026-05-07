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
        "viglink.com", "sovrn.com", "linkbux.com",
        "affinity.net", "bizrate.com", "ebay.com/rover", "rover.ebay.com", "peakoptions.site", "clickroll.net"
    ]
    return any(k in url_lower for k in blacklist)

def get_link_status(page, response):
    """ 重新找回 Title 判定邏輯，並強化 Amazon/eBay 特徵偵測 """
    try:
        curr_url = page.url
        page_title = page.title().lower()
        
        # 1. 黑名單判定 (轉址中介或瀏覽器錯誤)
        if is_intermediate_domain(curr_url): return "Error"
        
        # 2. 標題關鍵字判定 (通用 404 偵測)
        # 即使伺服器回傳 200，只要標題符合這些字眼，就判定為 404
        not_found_keywords = ["page not found", "404", "dead end", "can't find that page", "dogs of amazon", "doesn't exist"]
        if any(k in page_title for k in not_found_keywords):
            return "404"
        
        # 3. 內容特徵偵測 (針對 Amazon 等不改標題的頑強頁面)
        content = page.content().lower()
        if "amazon.com" in curr_url and "images-na.ssl-images-amazon.com/images/g/01/error/" in content:
            return "404"

        # 4. 高難度網站特許通行 (只要沒被擋 IP 就算 Yes)
        if any(k in curr_url for k in ["hotels.com", "jcpenney.com", "lowes.com", "robinhood.com", "zappos.com"]):
            return "Yes"
            
        # 5. 標準 HTTP 狀態碼判定
        if not response: return "No"
        status = response.status
        if status == 404: return "404"
        if status == 403 or "access denied" in page_title: return "Yes" # 403 視為連結存在但拒絕機房
        
        return "Yes" if 200 <= status < 300 else "No"
    except: 
        return "No"

def wait_for_redirect_smart(page, initial_url, retailer_name):
    try:
        hard_list = ["Hotels.com", "JCPenney", "Lowe's", "Robinhood", "Zappos", "Amazon", "Hilton"]
        timeout = 45000 if any(k in retailer_name for k in hard_list) else 25000
        
        resp = page.goto(initial_url, wait_until="commit", timeout=timeout, referer="https://search.yahoo.com/")
        page.mouse.wheel(0, random.randint(300, 600))
        
        last_url = ""
        for _ in range(12):
            curr_url = page.url
            if curr_url == last_url and not is_intermediate_domain(curr_url):
                return resp
            last_url = curr_url
            page.wait_for_timeout(2500)
        return resp
    except:
        return None

# --- run_retailer_capture 保持不變 (包含 last_captured_url 比對邏輯) ---
def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    data = {col: "" for col in column_order}
    data["Retailer"], data["SRP"] = retailer, srp_url
    expected_no_dd = ["LifeLock", "REI", "Nordstrom", "Nordstrom Rack"]
    
    try:
        page.goto(srp_url, wait_until="domcontentloaded", timeout=40000)
        time.sleep(4) 
        tree = html.fromstring(page.content())
        dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
        
        if not dd_label:
            if any(k in retailer for k in expected_no_dd):
                data["DD Name"] = "DD cannot be found"
                data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                return data
            return None
            
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

        last_captured_url = ""
        if landing_raw:
            resp = wait_for_redirect_smart(page, landing_raw[0], retailer)
            curr_url = page.url
            if not is_intermediate_domain(curr_url):
                data["Landing page URL"] = curr_url
                data["Link works"] = get_link_status(page, resp)
                last_captured_url = curr_url

        for i, cat in enumerate(cat_links):
            cat_key = f"Cat{i+1}"
            data[f"{cat_key} Name"] = cat["name"]
            if cat["link"]:
                data[f"{cat_key} Link URL"] = cat["link"]
                time.sleep(2)
                c_resp = wait_for_redirect_smart(page, cat["link"], retailer)
                curr_actual_url = page.url
                if not is_intermediate_domain(curr_actual_url) and curr_actual_url != last_captured_url:
                    data[f"{cat_key} page URL"] = curr_actual_url
                    data[f"{cat_key} Link works"] = get_link_status(page, c_resp)
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

# --- main 邏輯保持不變 (包含首次初始化與資料保護) ---
def main():
    print("🚀 啟動任務：找回 Title 判定與強化 404 偵測版")
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
            viewport={"width": 1920, "height": 1080}
        )
        page = context.new_page()
        if stealth_sync: stealth_sync(page)

        now = datetime.now(timezone.utc)

        for index, row in df_input.iterrows():
            retailer_name = str(row['Retailer']).strip()
            srp_url = str(row.get('SRP', ''))
            should_crawl = True
            is_new_entry = retailer_name not in existing_records

            if not is_new_entry:
                old = existing_records[retailer_name]
                old_dd = str(old.get('DD Name', '')).strip()
                old_date_str = str(old.get('Update Date', '')).strip()
                if not old_dd or old_dd.lower() == 'nan':
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
            
            if result and (str(result.get("DD Name")).lower() == "dd cannot be found" or is_data_complete(result)):
                try:
                    requests.post(GAS_OUTPUT_URL, json=result, timeout=25)
                    print(f"   ✅ {retailer_name} 覆蓋更新成功")
                except: print(f"   ❌ {retailer_name} 傳送失敗")
            else:
                if is_new_entry:
                    print(f"   ⚠️ {retailer_name} 為新項目且抓取受阻，執行首次初始化...")
                    init_payload = {col: "" for col in column_order}
                    init_payload["Retailer"] = retailer_name
                    init_payload["SRP"] = srp_url
                    try:
                        requests.post(GAS_OUTPUT_URL, json=init_payload, timeout=25)
                    except: pass
                else:
                    print(f"   ⚠️ {retailer_name} 抓取受阻，保留舊有完整資料。")
            
            time.sleep(random.uniform(5, 10))

        browser.close()
    print("🎉 任務完成！")

if __name__ == "__main__":
    main()