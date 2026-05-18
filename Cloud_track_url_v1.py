import os
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
import threading

# --- 1. 設定區域 (透過 GitHub Secrets 安全取得環境變數) ---
GSHEET_INPUT_URL = os.environ.get("GSHEET_INPUT_URL")
GAS_OUTPUT_URL = os.environ.get("GAS_OUTPUT_URL")
TRACK_URL_DATA_CSV = os.environ.get("TRACK_URL_DATA_CSV")

# --- 2. 工具函式 ---

def is_data_complete(row_data):
    """ 觸發爬蟲的核心基礎判斷：檢查主要網址是否健全 """
    landing_status = str(row_data.get("Landing page URL", "")).strip()
    if landing_status.lower() in ["", "nan", "crawler failed", "dd cannot be found"]:
        return False
        
    fields_to_check = ["Cat1 page URL", "Cat2 page URL", "Cat3 page URL", "Cat4 page URL"]
    for field in fields_to_check:
        val = str(row_data.get(field, "")).strip().lower()
        if val in ["", "nan", "same", "error"]:
            return False
    return True

def is_intermediate_domain(url):
    """ 中轉與廣告黑名單：若落在這些網域則判定為無效或失敗 """
    if not url: return True
    url_lower = url.lower()
    blacklist = [
        "yahoo.com", "search.yahoo.com", "shopping.yahoo.com", 
        "chrome-error://", "chromewebdata", "access-denied", "accessdenied", 
        "viglink.com", "sovrn.com", "linkbux.com", "financebuzz.com",
        "validclick.net", "shophermedia.net", "rd.bizrate.com"
    ]
    return any(k in url_lower for k in blacklist)

def get_link_status(page, response):
    """ 檢查連結的存活回應狀態碼與關鍵字 """
    try:
        curr_url = page.url
        page_title = page.title().lower()
        if is_intermediate_domain(curr_url): return "Error"
        
        not_found_keywords = ["page not found", "404", "dead end", "can't find that page", "dogs of amazon", "doesn't exist"]
        if any(k in page_title for k in not_found_keywords): return "404"
        
        hard_sites = ["lifelock.com", "booking.com", "hotels.com", "jcpenney.com", "lowes.com", "robinhood.com", "zappos.com", "hilton.com", "kohls.com", "statefarm.com"]
        if any(k in curr_url for k in hard_sites): return "Yes"
        
        if not response: return "No"
        status = response.status
        if status == 404: return "404"
        if status == 403 or "access denied" in page_title: return "Yes"
        return "Yes" if 200 <= status < 300 else "No"
    except: return "No"

def wait_for_redirect_smart(page, initial_url, retailer_name):
    """ 模擬真人智慧跳轉等待 """
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
        # Step 1: 加載 Yahoo SRP
        page.goto(srp_url, wait_until="domcontentloaded", timeout=40000)
        time.sleep(4) 
        tree = html.fromstring(page.content())
        
        # Step 2: DD 判定 (保留原貌，不隨意寫入 N/A)
        dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
        if not dd_label:
            data["Landing page URL"] = "DD cannot be found"
            data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            return data
        data["DD Name"] = dd_label[0]

        # Step 3: 抓取 Landing Page
        landing_raw = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
        if landing_raw:
            resp = wait_for_redirect_smart(page, landing_raw[0], retailer)
            if not is_intermediate_domain(page.url):
                data["Landing page URL"] = page.url
                data["Link works"] = get_link_status(page, resp)
                # 標題淨化 (切除尾綴解決 State Farm 攔截誤判)
                raw_title = page.title().strip()
                clean_title = raw_title.split('|')[0].split('-')[0].strip().lower()
                if len(clean_title) > 3: titles_map["Landing"] = clean_title
            else:
                data["Landing page URL"] = "crawler failed"

        # 斷路保護：如果 Landing 失敗，就不抓 Cat1~4，直接結束
        if data["Landing page URL"] == "crawler failed":
            data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            return data

        # Step 4: 抓取 Categories
        for i in range(1, 5):
            c_link = tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a/@href")
            c_name = tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a//img/@alt")
            cat_key = f"Cat{i}"
            data[f"{cat_key} Name"] = c_name[0] if c_name else "N/A"
            if c_link:
                data[f"{cat_key} Link URL"] = c_link[0]
                time.sleep(random.uniform(2, 4))
                c_resp = wait_for_redirect_smart(page, c_link[0], retailer)
                if not is_intermediate_domain(page.url):
                    data[f"{cat_key} page URL"] = page.url
                    data[f"{cat_key} Link works"] = get_link_status(page, c_resp)
                    raw_t = page.title().strip()
                    clean_t = raw_t.split('|')[0].split('-')[0].strip().lower()
                    if len(clean_t) > 3: titles_map[cat_key] = clean_t
                else:
                    data[f"{cat_key} Link works"] = "Error"

        # Step 5: same 偵測
        title_counts = Counter([t for t in titles_map.values()])
        same_count = 0
        for key, title in titles_map.items():
            if title_counts[title] > 2:
                same_count += 1
                if key == "Landing": data["Link works"] = "same"
                else: data[f"{key} Link works"] = "same"

        # 5 same 覆寫
        if same_count >= 5:
            data["Landing page URL"] = "crawler failed"
            for i in range(1, 5): 
                data[f"Cat{i} page URL"] = ""
                data[f"Cat{i} Link works"] = ""

        data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        return data

    except Exception as e:
        data["Landing page URL"] = "crawler failed"
        data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        return data

# --- 4. 主流程 ---

def main():
    print(f"🚀 極速防護版啟動 | 安全環境變數讀取與強制重爬修正")
    
    # 檢查驗證密鑰
    if not GSHEET_INPUT_URL or not GAS_OUTPUT_URL or not TRACK_URL_DATA_CSV:
        print("❌ 錯誤: 偵測到缺少 GitHub Secrets 設定，請檢查專案變數配置。")
        return

    try:
        df_input = pd.read_csv(GSHEET_INPUT_URL)
    except Exception as e:
        print(f"❌ 無法成功拉取輸入清單 (請確認 GSHEET_INPUT_URL 密鑰網址後綴是否包含 &output=csv): {e}")
        return

    existing_records = {}
    try:
        cb = random.randint(1000, 9999)
        fresh_url = f"{TRACK_URL_DATA_CSV}&t={int(time.time())}&cb={cb}"
        df_existing = pd.read_csv(fresh_url)
        for _, r in df_existing[::-1].iterrows():
            name = str(r['Retailer']).strip()
            if name not in existing_records: existing_records[name] = r.to_dict()
    except Exception as e:
        print(f"ℹ️ 歷史紀錄庫無資料，將以全新品項執行: {e}")

    column_order = ["Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works", 
                    "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
                    "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
                    "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
                    "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1024, "height": 768}
        )
        
        for index, row in df_input.iterrows():
            retailer_name = str(row['Retailer']).strip()
            srp_url = str(row.get('SRP', ''))
            
            should_skip = False
            
            if retailer_name in existing_records:
                old = existing_records[retailer_name]
                
                # 💡 關鍵校正點：抓取前次的狀態文字，只要包含失敗字眼，不論時間多新，一律強制不跳過
                landing_url_str = str(old.get("Landing page URL", "")).strip().lower()
                is_failed_status = any(k in landing_url_str for k in ["failed", "error", "found", "nan", "same", ""])
                
                if is_data_complete(old) and not is_failed_status:
                    try:
                        old_date = datetime.strptime(str(old.get('Update Date', '')).replace(" UTC", ""), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                        if (datetime.now(timezone.utc) - old_date < timedelta(days=7)):
                            print(f"⏭️  Skip: {retailer_name} (前次成功且在 7 天保護期內)")
                            should_skip = True
                    except Exception as e:
                        print(f"⚠️ 日期解析格式異常，將對 {retailer_name} 進行重新檢測: {e}")
            
            if should_skip:
                continue

            print(f"🔍 ({index+1}/{len(df_input)}) Processing: {retailer_name}")
            
            result = None
            page = context.new_page()
            if stealth_sync: stealth_sync(page)
            
            # 使用獨立執行緒實作單一品項 3 分鐘卡死截斷保護
            def worker():
                nonlocal result
                result = run_retailer_capture(page, row, column_order)

            t = threading.Thread(target=worker)
            t.daemon = True
            t.start()
            t.join(timeout=180.0) # ⏳ 單一零售商 3 分鐘強制上限

            if t.is_alive():
                print(f"   🛑 警告: {retailer_name} 執行耗時已達 3 分鐘上限，實施強制截斷！")
                result = {col: "" for col in column_order}
                result.update({
                    "Retailer": retailer_name,
                    "SRP": srp_url,
                    "Landing page URL": "crawler failed",
                    "Update Date": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                })
            
            try: page.close()
            except: pass

            if result:
                try: 
                    requests.post(GAS_OUTPUT_URL, json=result, timeout=20)
                except: 
                    print(f"   ❌ {retailer_name} 經由 GAS API 回傳雲端失敗")
            
            time.sleep(random.uniform(2, 4))
            
        browser.close()
    print("🎉 任務執行完畢！")

if __name__ == "__main__":
    main()
