import time
import random
import pandas as pd
from playwright.sync_api import sync_playwright
from lxml import html
from datetime import datetime, timezone
import os
import requests
import json

# --- 1. 設定區域 ---
GSHEET_INPUT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRYb03CvtlCo6M_xaOC9SfReKwH7CNGcMabekpH9y0tHN_0tM7GP13qfNDovd_XekUbkzU4Q6dgS8xr/pub?gid=1643541848&single=true&output=csv"
GAS_OUTPUT_URL = "https://script.google.com/macros/s/AKfycbyLvET6p-l_zxQaoJqk-XLg6PEd--bBu__lqqRcc_LF70cgHmijRRpXSr2Kf50NHlLoiA/exec"

# --- 2. 工具函式 ---

def is_intermediate_domain(url):
    """ 判定是否為 Yahoo 或中間轉址網域 """
    if not url: return True
    url_lower = url.lower()
    blacklist = [
        "yahoo.com", "search.yahoo.com", "shopping.yahoo.com",
        "affinity.net", "bizrate.com", "ebay.com/rover", "rover.ebay.com", 
        "peakoptions.site", "clickroll.net"
    ]
    return any(k in url_lower for k in blacklist)

def get_link_status(page, response):
    """ 精確判斷：只檢查標題與可見文字內容，避免誤判 Amazon """
    try:
        current_url = page.url
        page_title = page.title().lower()
        
        # Ebay 快速通關：一旦進入 ebay 且脫離跳轉網域就給 Yes
        if "ebay.com" in current_url and not is_intermediate_domain(current_url):
            return "Yes"
            
        if is_intermediate_domain(current_url): return "Error"
        if not response: return "No"

        # 404 關鍵字檢查 (標題與畫面上看得到的文字)
        not_found_keywords = ["page not found", "404", "dead end", "page cannot be found"]
        visible_text = page.inner_text("body").lower() if page.query_selector("body") else ""
        
        if any(k in page_title for k in not_found_keywords) or \
           any(k in visible_text for k in not_found_keywords):
            return "404"

        status = response.status
        # 針對部分電商 403 阻擋但內容存在的處理
        if status == 403 or "access denied" in page_title: return "Yes"
        
        return "Yes" if 200 <= status < 300 else "No"
    except:
        return "No"

def wait_for_redirect_smart(page, initial_url):
    """ 轉址追蹤加速版：防止單一網址卡死 """
    try:
        # 只要伺服器回應 (commit) 就開始追蹤
        resp = page.goto(initial_url, wait_until="commit", timeout=35000)
        
        # 最多等待 5 次循環 (約 15 秒)
        for _ in range(5): 
            if not is_intermediate_domain(page.url):
                page.wait_for_timeout(1500) # 到達後穩定 1.5 秒
                return resp
            
            # 模擬微小捲動與隨機等待
            page.mouse.wheel(0, 100)
            page.wait_for_timeout(2500) 
            
        return resp
    except:
        return None

# --- 3. 核心抓取函式 ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    
    # 初始化資料 (無 Link Check)
    data = {col: "" for col in column_order}
    data["Retailer"], data["SRP"] = retailer, srp_url
    data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    try:
        # 造訪 Yahoo SRP 頁面
        page.goto(srp_url, wait_until="load", timeout=30000)
        time.sleep(2)
        tree = html.fromstring(page.content())
        
        # 檢查 DD 名稱
        dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
        data["DD Name"] = dd_label[0] if dd_label else "DD cannot be found"
        
        if dd_label:
            # --- 1. 抓取 Landing Page ---
            raw_link = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
            if raw_link:
                resp = wait_for_redirect_smart(page, raw_link[0])
                data["Link works"] = get_link_status(page, resp)
                data["Landing page URL"] = page.url

            # --- 2. 抓取 Categories 1-4 ---
            for i in range(1, 5):
                cat_key = f"Cat{i}"
                # 如果跳轉走了，就回 SRP 頁面抓下一個分類
                if "yahoo.com" not in page.url:
                    page.goto(srp_url, wait_until="domcontentloaded", timeout=20000)
                
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
    except Exception as e:
        print(f"      ⚠️ {retailer} 抓取部分逾時: {str(e)[:50]}")
    
    return data

# --- 4. 主程式流程 ---

def main():
    print("🚀 啟動優化任務：美國環境模擬 + 欄位精簡版")
    
    try:
        df_input = pd.read_csv(GSHEET_INPUT_URL)
    except Exception as e:
        print(f"❌ 無法讀取 CSV 資料來源: {e}")
        return

    # 定義對齊 GAS 的 22 個欄位
    column_order = [
        "Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works",
        "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
        "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
        "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
        "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"
    ]

    with sync_playwright() as p:
        # 啟動瀏覽器
        browser = p.chromium.launch(headless=True)
        
        # 模擬美國地區環境設定
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/New_York",
            viewport={'width': 1920, 'height': 1080}
        )
        
        page = context.new_page()

        for index, row in df_input.iterrows():
            retailer_name = row['Retailer']
            print(f"🔍 ({index+1}/{len(df_input)}) Processing: {retailer_name}")
            
            # 執行抓取邏輯
            result_data = run_retailer_capture(page, row, column_order)
            
            # 即時傳回 GAS
            try:
                headers = {'Content-Type': 'application/json'}
                r = requests.post(GAS_OUTPUT_URL, data=json.dumps(result_data), headers=headers, timeout=25)
                if "Success" in r.text or "成功" in r.text:
                    print(f"   ✅ {retailer_name} 完成並回傳")
                else:
                    print(f"   ⚠️ {retailer_name} 回傳異常: {r.text}")
            except Exception as e:
                print(f"   ❌ {retailer_name} 網路傳送失敗: {e}")

        browser.close()
    print("🎉 所有任務已完成！")

if __name__ == "__main__":
    main()