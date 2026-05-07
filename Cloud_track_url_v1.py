import time
import random
import pandas as pd
from playwright.sync_api import sync_playwright
from lxml import html
from datetime import datetime, timezone
import os
import requests
import json

# --- 1. 環境變數與設定 ---
# 請確保在 GitHub Settings > Secrets 中設定了這兩個網址
GSHEET_INPUT_URL = os.environ.get('GSHEET_INPUT_URL')
GAS_OUTPUT_URL = os.environ.get('GAS_OUTPUT_URL')

# --- 2. 基礎工具函式 ---

def is_intermediate_domain(url):
    """ 判定是否為中間轉址網域。 """
    if not url: return True
    url_lower = url.lower()
    blacklist = [
        "yahoo.com", "search.yahoo.com", "shopping.yahoo.com",
        "affinity.net", "bizrate.com", "shophermedia.net", "provenpixel.com",
        "socialiqredir.com", "discounthero.org", "magik.ly", "netsourceio.com",
        "clickroll.net", "shopping123.com", "top-best.com",
        "v2i8b.com", "beyondcheap.com", "intentxredir.com",
        "peakoptions.site"
    ]
    return any(k in url_lower for k in blacklist)

def get_link_status(page, response):
    """ 核心判定邏輯：包含 404 關鍵字檢查與轉址判定。 """
    try:
        current_url = page.url
        if is_intermediate_domain(current_url): return "Error"
        if not response: return "No"

        # 404 關鍵字檢查 (標題與內容)
        page_title = page.title().lower()
        page_content = page.content().lower()
        not_found_keywords = ["page not found", "404", "dead end", "page cannot be found"]
        
        if any(k in page_title for k in not_found_keywords) or \
           any(k in page_content for k in not_found_keywords):
            return "404"

        status = response.status
        if status == 403 or "access denied" in page_title: return "Yes"
        if status >= 400: return "No"
        return "Yes" if 200 <= status < 300 else "No"
    except:
        return "No"

def wait_for_redirect_smart(page, initial_url):
    """ 強制追蹤轉址，直到脫離 Yahoo/中間網域。 """
    try:
        resp = page.goto(initial_url, wait_until="commit", timeout=60000)
        for _ in range(12): # 最多等待約 50 秒
            if not is_intermediate_domain(page.url):
                page.wait_for_timeout(2000)
                return resp
            page.mouse.move(random.randint(100, 300), random.randint(100, 300))
            page.wait_for_timeout(4000)
        return resp
    except:
        return None

# --- 3. 核心抓取流程 ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    data = {col: ("N/A" if "Check" in col else "") for col in column_order}
    data["Retailer"], data["SRP"] = retailer, srp_url
    data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    capture_status = {"Landing": False, "Cat1": False, "Cat2": False, "Cat3": False, "Cat4": False}

    for attempt in range(1, 4):
        if all(capture_status.values()): break
        try:
            page.goto(srp_url, wait_until="load", timeout=60000)
            time.sleep(3)
            tree = html.fromstring(page.content())
            dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
            data["DD Name"] = dd_label[0] if dd_label else "DD cannot be found"
            if not dd_label: break 

            # Landing Page
            if not capture_status["Landing"]:
                raw_link = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
                if raw_link:
                    resp = wait_for_redirect_smart(page, raw_link[0])
                    data["Link works"] = get_link_status(page, resp)
                    data["Landing page URL"] = page.url
                    if not is_intermediate_domain(page.url): capture_status["Landing"] = True

            # Categories 1-4
            for i in range(1, 5):
                cat_key = f"Cat{i}"
                if not capture_status[cat_key]:
                    if "yahoo.com" not in page.url: page.goto(srp_url, wait_until="domcontentloaded")
                    c_tree = html.fromstring(page.content())
                    c_name = c_tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a//img/@alt")
                    c_link = c_tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a/@href")
                    data[f"{cat_key} Name"] = c_name[0] if c_name else "N/A"
                    l_val = c_link[0] if c_link else ""
                    data[f"{cat_key} Link URL"] = l_val
                    if l_val and l_val not in ["", "#", "N/A"]:
                        c_resp = wait_for_redirect_smart(page, l_val)
                        data[f"{cat_key} page URL"] = page.url
                        data[f"{cat_key} Link works"] = get_link_status(page, c_resp)
                        if not is_intermediate_domain(page.url) and data[f"{cat_key} Name"] != "N/A":
                            capture_status[cat_key] = True
                    else:
                        data[f"{cat_key} Link works"] = "No"; capture_status[cat_key] = True 
        except: pass
    return data

# --- 4. 主執行與資料傳輸 ---

def main():
    if not GSHEET_INPUT_URL or not GAS_OUTPUT_URL:
        print("❌ 錯誤：找不到 GSHEET_INPUT_URL 或 GAS_OUTPUT_URL 變數")
        return

    # 讀取 Google Sheet 資料
    df_input = pd.read_csv(GSHEET_INPUT_URL)
    column_order = [
        "Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works", "Link Check",
        "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works", "Cat1 Link Check",
        "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works", "Cat2 Link Check",
        "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works", "Cat3 Link Check",
        "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works", "Cat4 Link Check"
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)...")
        page = context.new_page()

        for index, row in df_input.iterrows():
            print(f"🚀 Processing: {row['Retailer']}")
            result_data = run_retailer_capture(page, row, column_order)
            
            # 每一筆抓完立刻透過 GAS 傳回 Google Sheet
            try:
                requests.post(GAS_OUTPUT_URL, json=result_data, timeout=30)
                print(f"✅ {row['Retailer']} 資料已傳回 Sheet")
            except Exception as e:
                print(f"❌ 傳回失敗: {e}")

        browser.close()
    print("🎉 所有任務已完成！")

if __name__ == "__main__":
    main()