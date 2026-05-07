import time
import pandas as pd
from playwright.sync_api import sync_playwright
from lxml import html
from datetime import datetime, timezone, timedelta
import requests
import json
import random

# --- 1. 設定區域 ---
# 來源名單
GSHEET_INPUT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=1643541848&single=true&output=csv"
# 你最新的 GAS API (支援覆蓋模式)
GAS_OUTPUT_URL = "https://script.google.com/macros/s/AKfycbwfA2vP2hdACwsEei73OSQUEojmXFRyyKqu_NcsDFi0Mp7oU2_fTUB1DCM2x4oFMWt7tA/exec"
# 讀取用的 track_url CSV 連結
TRACK_URL_DATA_CSV = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=0&single=true&output=csv"

# --- 2. 工具函式 ---

def is_data_complete(row_data):
    """ 檢查 18 個關鍵欄位是否都有實質內容 """
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
    
    try:
        page.goto(srp_url, wait_until="load", timeout=30000)
        time.sleep(2)
        tree = html.fromstring(page.content())
        dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
        
        if not dd_label:
            return None 

        data["DD Name"] = dd_label[0]
        
        if data["DD Name"].lower() == "dd cannot be found":
            data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            return data

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

        if not data["Landing page URL"] or data["Landing page URL"] == "":
            return None 

        data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        return data

    except:
        return None

# --- 4. 主流程 ---

def main():
    print("🚀 任務啟動：覆蓋模式 + 破解快取比對中...")
    
    # 讀取 Input
    try:
        df_input = pd.read_csv(GSHEET_INPUT_URL)
    except: print("❌ 無法讀取 Input CSV"); return

    # 讀取歷史紀錄 (加入隨機參數破解 Google CSV 快取)
    existing_records = {}
    try:
        fresh_url = f"{TRACK_URL_DATA_CSV}&t={int(time.time())}"
        df_existing = pd.read_csv(fresh_url)
        # 反轉讀取，確保保留的是最新的一筆記錄
        for _, r in df_existing[::-1].iterrows():
            name = str(r['Retailer']).strip()
            if name not in existing_records:
                existing_records[name] = r.to_dict()
        print(f"📊 成功加載歷史紀錄，共 {len(existing_records)} 筆零售商。")
    except: print("⚠️ 無歷史紀錄，將視為全新品項。")

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
            locale="en-US", timezone_id="America/New_York"
        )
        page = context.new_page()
        now = datetime.now(timezone.utc)

        for index, row in df_input.iterrows():
            retailer_name = str(row['Retailer']).strip()
            should_crawl = True
            
            # --- 比對邏輯 ---
            if retailer_name in existing_records:
                old = existing_records[retailer_name]
                old_dd = str(old.get('DD Name', '')).strip()
                old_date_str = str(old.get('Update Date', ''))
                
                # 只有標記為 "DD cannot be found" 才視為已經驗證過找不到
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
                
                # 同時滿足：已確認無DD、且在7天內、且資料全滿，才 Skip
                if has_been_verified and within_7_days and complete:
                    print(f"⏭️  Skip: {retailer_name}")
                    should_crawl = False
                else:
                    # 如果有資料但不符合以上，我們也要重新跑以取代舊資料
                    pass

            if not should_crawl: continue

            print(f"🔍 ({index+1}/{len(df_input)}) Processing: {retailer_name}")
            result = run_retailer_capture(page, row, column_order)
            
            if result:
                try:
                    # 使用 json.dumps 確保格式正確
                    requests.post(GAS_OUTPUT_URL, data=json.dumps(result), headers={'Content-Type': 'application/json'}, timeout=25)
                    print(f"   ✅ {retailer_name} 覆蓋更新成功")
                except: print(f"   ❌ {retailer_name} 傳送失敗")
            else:
                print(f"   ⚠️ {retailer_name} 抓取不全，不執行更新")

        browser.close()
    print("🎉 任務全部結束！")

if __name__ == "__main__":
    main()