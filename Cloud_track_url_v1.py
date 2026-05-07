import time
import pandas as pd
from playwright.sync_api import sync_playwright
from lxml import html
from datetime import datetime, timezone, timedelta
import requests
import json

# --- 1. 設定區域 ---
GSHEET_INPUT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=1643541848&single=true&output=csv"
GAS_OUTPUT_URL = "https://script.google.com/macros/s/AKfycbyLvET6p-l_zxQaoJqk-XLg6PEd--bBu__lqqRcc_LF70cgHmijRRpXSr2Kf50NHlLoiA/exec"
TRACK_URL_DATA_CSV = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=0&single=true&output=csv"

# --- 2. 工具函式 ---

def is_data_complete(row_data):
    """ 檢查 Landing page 與 Cat1-4 相關 18 個欄位 """
    fields_to_check = [
        "Landing page URL", "Link works",
        "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
        "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
        "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
        "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"
    ]
    for field in fields_to_check:
        val = str(row_data.get(field, "")).strip().lower()
        if val in ["", "nan", "undefined", "none", "null"]:
            return False
    return True

def is_intermediate_domain(url):
    if not url: return True
    url_lower = url.lower()
    blacklist = ["yahoo.com", "search.yahoo.com", "shopping.yahoo.com", "affinity.net", "bizrate.com", "ebay.com/rover", "rover.ebay.com", "peakoptions.site"]
    return any(k in url_lower for k in blacklist)

def get_link_status(page, response):
    try:
        current_url = page.url
        page_title = page.title().lower()
        if "ebay.com" in current_url and not is_intermediate_domain(current_url): return "Yes"
        if is_intermediate_domain(current_url): return "Error"
        if not response: return "No"
        not_found_keywords = ["page not found", "404", "dead end", "page cannot be found"]
        visible_text = page.inner_text("body").lower() if page.query_selector("body") else ""
        if any(k in page_title for k in not_found_keywords) or any(k in visible_text for k in not_found_keywords): return "404"
        status = response.status
        if status == 403 or "access denied" in page_title: return "Yes"
        return "Yes" if 200 <= status < 300 else "No"
    except: return "No"

def wait_for_redirect_smart(page, initial_url):
    try:
        resp = page.goto(initial_url, wait_until="commit", timeout=35000)
        for _ in range(5): 
            if not is_intermediate_domain(page.url):
                page.wait_for_timeout(1500)
                return resp
            page.wait_for_timeout(2500) 
        return resp
    except: return None

# --- 3. 核心抓取函式 ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    data = {col: "" for col in column_order}
    data["Retailer"], data["SRP"] = retailer, srp_url
    data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    try:
        page.goto(srp_url, wait_until="load", timeout=30000)
        time.sleep(2)
        tree = html.fromstring(page.content())
        dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
        data["DD Name"] = dd_label[0] if dd_label else "DD cannot be found"
        
        # 即使抓到 DD cannot be found，我們還是要把結果傳回去紀錄
        if dd_label and data["DD Name"].lower() != "dd cannot be found":
            raw_link = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
            if raw_link:
                resp = wait_for_redirect_smart(page, raw_link[0])
                data["Link works"] = get_link_status(page, resp)
                data["Landing page URL"] = page.url
            for i in range(1, 5):
                cat_key = f"Cat{i}"
                if "yahoo.com" not in page.url: page.goto(srp_url, wait_until="domcontentloaded", timeout=20000)
                c_tree = html.fromstring(page.content())
                c_name = c_tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a//img/@alt")
                c_link = c_tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a/@href")
                data[f"{cat_key} Name"] = c_name[0] if c_name else "N/A"
                if c_link:
                    l_val = c_link[0]
                    data[f"{cat_key} Link URL"] = l_val
                    c_resp = wait_for_redirect_smart(page, l_val)
                    data[f"{cat_key} page URL"] = page.url
                    data[f"{cat_key} Link works"] = get_link_status(page, c_resp)
    except: pass
    return data

# --- 4. 主流程 ---

def main():
    print("🚀 啟動任務：執行 DD Name 存在性與完整性檢查...")
    try:
        df_input = pd.read_csv(GSHEET_INPUT_URL)
    except: print("❌ 無法讀取來源名單"); return

    existing_records = {}
    try:
        df_existing = pd.read_csv(TRACK_URL_DATA_CSV)
        for _, r in df_existing[::-1].iterrows():
            name = str(r['Retailer'])
            if name not in existing_records:
                existing_records[name] = r.to_dict()
    except: print("⚠️ 無舊資料，全面啟動爬取。")

    column_order = [
        "Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works",
        "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
        "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
        "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
        "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="en-US", timezone_id="America/New_York")
        page = context.new_page()
        now = datetime.now(timezone.utc)

        for index, row in df_input.iterrows():
            retailer_name = str(row['Retailer'])
            should_crawl = True
            
            if retailer_name in existing_records:
                old = existing_records[retailer_name]
                old_dd = str(old.get('DD Name', '')).strip()
                old_date_str = str(old.get('Update Date', ''))
                
                # --- 修正後的邏輯 ---
                # 只有 DD Name 正確標示為 "DD cannot be found" 時，才算「檢查過」
                # 如果它是空的、nan，則觸發爬蟲 (should_crawl 保持 True)
                has_been_verified = (old_dd.lower() == "dd cannot be found")
                
                within_7_days = False
                if has_been_verified:
                    try:
                        clean_date = old_date_str.replace(" UTC", "")
                        old_date = datetime.strptime(clean_date, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                        if now - old_date < timedelta(days=7):
                            within_7_days = True
                    except: pass
                
                complete = is_data_complete(old)
                
                # 滿足所有條件才 Skip：已確認找不到 DD、且在 7 天內、且資料架構完整
                if has_been_verified and within_7_days and complete:
                    print(f"⏭️  Skip: {retailer_name} (已確認無 DD 且在 7 天內)")
                    should_crawl = False

            if not should_crawl: continue

            print(f"🔍 ({index+1}/{len(df_input)}) Processing: {retailer_name}")
            result = run_retailer_capture(page, row, column_order)
            
            try:
                requests.post(GAS_OUTPUT_URL, json=result, timeout=25)
                print(f"   ✅ {retailer_name} 回傳成功")
            except: print(f"   ❌ {retailer_name} 傳送失敗")

        browser.close()
    print("🎉 任務結束！")

if __name__ == "__main__":
    main()