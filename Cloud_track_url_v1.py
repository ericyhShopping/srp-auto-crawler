import os
import time
import pandas as pd
from playwright.sync_api import sync_playwright
from lxml import html
from datetime import datetime, timezone, timedelta
import requests
import random
from collections import Counter

# --- 1. 設定區域 (透過 GitHub Secrets 安全取得環境變數) ---
GSHEET_INPUT_URL = os.environ.get("GSHEET_INPUT_URL")
GAS_OUTPUT_URL = os.environ.get("GAS_OUTPUT_URL")
TRACK_URL_DATA_CSV = os.environ.get("TRACK_URL_DATA_CSV")

# --- 2. 工具函式 ---

def is_data_complete(row_data):
    """ 
    🔥 超嚴格欄位完備性判定：
    必須所有關鍵欄位都有健全的資料才算完成。任何一個欄位有缺失或異常字眼，一律重爬！
    """
    try:
        # 1. 檢查 Landing Page 基礎狀況
        landing_status = str(row_data.get("Landing page URL", "")).strip().lower()
        if landing_status in ["", "nan", "crawler failed", "dd cannot be found", "error", "same"]:
            return False
        
        # 2. 檢查 Link works 狀態
        link_works = str(row_data.get("Link works", "")).strip().lower()
        if link_works in ["", "nan", "no", "error", "same"]:
            return False

        # 3. 💡 逐一地毯式檢查 Cat1 到 Cat4 的所有欄位（只要一個欄位不及格就重爬）
        for i in range(1, 5):
            name_val = str(row_data.get(f"Cat{i} Name", "")).strip().lower()
            link_url_val = str(row_data.get(f"Cat{i} Link URL", "")).strip().lower()
            page_url_val = str(row_data.get(f"Cat{i} page URL", "")).strip().lower()
            works_val = str(row_data.get(f"Cat{i} Link works", "")).strip().lower()
            
            # 如果名字是 N/A，且連結相關欄位都是空的，代表 Yahoo 本身就沒這個分類，這算「正常完工」
            if name_val == "n/a" and page_url_val in ["", "nan"] and link_url_val in ["", "nan"]:
                continue
                
            # 如果有分類，但任何一個欄位出現空值、錯誤或 same，判定資料不足
            if page_url_val in ["", "nan", "error"] or link_url_val in ["", "nan"] or works_val in ["", "nan", "error", "same"]:
                return False
                
        return True
    except:
        return False # 有任何異常，保險起見一律重爬

def is_intermediate_domain(url):
    if not url: return True
    url_lower = url.lower()
    blacklist = [
        "yahoo.com", "search.yahoo.com", "shopping.yahoo.com", 
        "chrome-error://", "chromewebdata", "access-denied", "accessdenied", 
        "viglink.com", "sovrn.com", "linkbux.com", "financebuzz.com",
        "validclick.net", "shophermedia.net", "rd.bizrate.com"
    ]
    return any(k in url_lower for k in blacklist)

def wait_for_redirect_smart(page, initial_url, retailer_name):
    try:
        is_slow = any(k in retailer_name.lower() for k in ["ebay", "booking", "statefarm", "kohls"])
        timeout_ms = 15000 if is_slow else 30000
        
        resp = page.goto(initial_url, wait_until="domcontentloaded", timeout=timeout_ms, referer="https://search.yahoo.com/")
        
        last_url = ""
        max_loops = 3 if is_slow else 6
        for _ in range(max_loops):
            curr_url = page.url
            if is_slow and "ebay.com" in curr_url and "rover.ebay" not in curr_url:
                return resp
            if curr_url == last_url and not is_intermediate_domain(curr_url):
                return resp
            last_url = curr_url
            page.wait_for_timeout(1500)
        return resp
    except:
        return None

# --- 3. 核心抓取函式 ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    data = {col: "" for col in column_order}
    data["Retailer"], data["SRP"] = retailer, srp_url
    titles_map = {}

    try:
        page.goto(srp_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        tree = html.fromstring(page.content())
        
        dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
        if not dd_label:
            data["Landing page URL"] = "DD cannot be found"
            data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            return data
        data["DD Name"] = dd_label[0]

        landing_raw = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
        if landing_raw:
            resp = wait_for_redirect_smart(page, landing_raw[0], retailer)
            if not is_intermediate_domain(page.url):
                data["Landing page URL"] = page.url
                data["Link works"] = "Yes" if page.url and len(page.url) > 10 else "No"
                t_raw = page.title().strip().split('|')[0].split('-')[0].strip().lower()
                titles_map["Landing"] = t_raw
            else:
                data["Landing page URL"] = "crawler failed"

        if data["Landing page URL"] == "crawler failed":
            data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            return data

        for i in range(1, 5):
            c_link = tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a/@href")
            c_name = tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a//img/@alt")
            cat_key = f"Cat{i}"
            data[f"{cat_key} Name"] = c_name[0] if c_name else "N/A"
            
            if c_link:
                data[f"{cat_key} Link URL"] = c_link[0]
                wait_for_redirect_smart(page, c_link[0], retailer)
                if not is_intermediate_domain(page.url):
                    data[f"{cat_key} page URL"] = page.url
                    data[f"{cat_key} Link works"] = "Yes"
                    curr_t = page.title().strip().split('|')[0].split('-')[0].strip().lower()
                    if curr_t == titles_map.get("Landing") and len(curr_t) > 3:
                        data[f"{cat_key} Link works"] = "same"
                        break
                else:
                    data[f"{cat_key} Link works"] = "Error"
            time.sleep(1)

        data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        return data
    except:
        data["Landing page URL"] = "crawler failed"
        data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        return data

# --- 4. 主流程 ---

def main():
    print("🚀 安全、隱身、全功能完美版排程核心啟動中...", flush=True)
    
    if not GSHEET_INPUT_URL or not GAS_OUTPUT_URL or not TRACK_URL_DATA_CSV:
        print("❌ 錯誤: 缺少必要的 GitHub Secrets 設定。")
        return

    try:
        df_input = pd.read_csv(GSHEET_INPUT_URL, storage_options={"timeout": 15})
    except Exception as e:
        print(f"❌ 輸入清單拉取失敗: {e}")
        return

    existing_records = {}
    try:
        cb = random.randint(1000, 9999)
        fresh_url = f"{TRACK_URL_DATA_CSV}&t={int(time.time())}&cb={cb}"
        df_existing = pd.read_csv(fresh_url, storage_options={"timeout": 15})
        for _, r in df_existing[::-1].iterrows():
            name = str(r['Retailer']).strip()
            if name not in existing_records: existing_records[name] = r.to_dict()
    except Exception as e:
        print(f"ℹ️ 歷史紀錄未成功讀取: {e}")

    column_order = ["Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works", 
                    "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
                    "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
                    "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
                    "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            bypass_csp=True
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        print(f"📋 開始處理清單，總計: {len(df_input)} 筆", flush=True)

        for index, row in df_input.iterrows():
            retailer_name = str(row['Retailer']).strip()
            
            should_skip = False
            if retailer_name in existing_records:
                old = existing_records[retailer_name]
                
                # 💡 只有當「is_data_complete」嚴格檢驗過關（True）時，才允許去比對 7 天保護期
                if is_data_complete(old):
                    try:
                        old_date_str = str(old.get('Update Date', '')).replace(" UTC", "").strip()
                        old_date = datetime.strptime(old_date_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                        
                        if (datetime.now(timezone.utc) - old_date < timedelta(days=7)):
                            print(f"箱箱 ⏭️  Skip: {retailer_name} (所有欄位完全健全，且在 7 天保護期內)", flush=True)
                            should_skip = True
                    except Exception as e:
                        pass
            
            if should_skip: 
                continue

            print(f"🔍 ({index+1}/{len(df_input)}) Processing: {retailer_name} (欄位不齊全或過期，啟動重爬)", flush=True)
            result = run_retailer_capture(page, row, column_order)
            
            if result:
                try: 
                    requests.post(GAS_OUTPUT_URL, json=result, timeout=15)
                    print(f"   ✅ {retailer_name} 更新成功 (Status: {result['Landing page URL']})", flush=True)
                except: 
                    print(f"   ❌ {retailer_name} 送出失敗", flush=True)
            
            time.sleep(random.uniform(2, 4))
            
        browser.close()
    print("🎉 所有任務圓滿結束！", flush=True)

if __name__ == "__main__":
    main()
