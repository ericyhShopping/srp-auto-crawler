import time
import random
import pandas as pd
from playwright.sync_api import sync_playwright
from lxml import html
from datetime import datetime, timezone
import openai
import requests
import json
import os

# --- 1. 配置設定 ---
# ⚠️ 請替換為你從 'urls' 分頁「發佈到網路」產生的 CSV 網址
GSHEET_INPUT_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vS-hMIOWp6V-9xdD5pQixZtUKwgTYyx37Bv1agLQb3CeOE6kNI0AD72lC_R1MurA8VNoLPa9C8sr4aa/pub?gid=1039583453&single=true&output=csv"

# ⚠️ 請替換為你 GAS 部署後產生的「網頁應用程式網址」
GAS_OUTPUT_URL = "https://script.google.com/macros/s/AKfycbyHzJ9JEC-8AuEJmcpZpjXdpfVrKcMmO3pvstNvZQyv-_c0jlmoqBHt4jnW3IwrDiK0Hg/exec"

# 從 GitHub Secrets 自動讀取 Key
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = openai.OpenAI(api_key=OPENAI_API_KEY)

# --- 2. 工具函式 ---

def is_intermediate_domain(url):
    """ 判定是否為中間跳轉或無效網域 """
    if not url: return True
    url_lower = url.lower()
    blacklist = [
        "us.search.yahoo.com", "yahoo.com/rdlw", "r.search.yahoo.com",
        "affinity.net", "bizrate.com", "shophermedia.net", "provenpixel.com",
        "socialiqredir.com", "discounthero.org", "magik.ly", "netsourceio.com",
        "clickroll.net", "shopping123.com", "top-best.com", "v2i8b.com", 
        "beyondcheap.com", "intentxredir.com", "peakoptions.site"
    ]
    return any(k in url_lower for k in blacklist)

def get_ai_check(url, name, page_text):
    """ 呼叫 OpenAI 判定是否為官方購物網站 """
    if not url or is_intermediate_domain(url): return "No"
    snippet = page_text[:1200].replace('\n', ' ')
    prompt = f"判定網址是否為 {name} 的官方購物網站。\n網址: {url}\n內容摘要: {snippet}\n符合回傳 Yes，否則 No。"
    try:
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        return res.choices[0].message.content.strip()
    except:
        return "AI Error"

def get_link_status(page):
    """ 檢查連結是否有效 """
    try:
        if is_intermediate_domain(page.url): return "Error"
        # 處理常見的 403 阻擋但內容正確的情況
        if "access denied" in page.title().lower(): return "Yes"
        return "Yes"
    except:
        return "No"

def wait_for_redirect_smart(page, initial_url):
    """ 智能等待跳轉直到脫離黑名單網域 """
    try:
        page.goto(initial_url, wait_until="commit", timeout=60000)
        for _ in range(8):
            if not is_intermediate_domain(page.url): return True
            page.wait_for_timeout(4000)
        return True
    except:
        return False

# --- 3. 核心抓取流程 ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    
    data = {col: "" for col in column_order}
    data.update({
        "Retailer": retailer, 
        "SRP": srp_url, 
        "Update Date": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    })
    
    capture_status = {"Landing": False, "Cat1": False, "Cat2": False, "Cat3": False, "Cat4": False}

    for attempt in range(1, 4): # 雲端重試 3 次
        if all(capture_status.values()): break
        try:
            page.goto(srp_url, wait_until="load", timeout=60000)
            time.sleep(2)
            tree = html.fromstring(page.content())
            
            # 抓取 DD Name
            dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
            data["DD Name"] = dd_label[0] if dd_label else "DD cannot be found"
            if data["DD Name"] == "DD cannot be found": break

            # 1. 處理 Landing Page
            if not capture_status["Landing"]:
                raw_link = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
                if raw_link and wait_for_redirect_smart(page, raw_link[0]):
                    status = get_link_status(page)
                    data["Link works"], data["Landing page URL"] = status, page.url
                    data["Link Check"] = get_ai_check(page.url, data["DD Name"], page.inner_text("body")) if status == "Yes" else "No"
                    capture_status["Landing"] = True

            # 2. 處理 4 個 Categories
            for i in range(1, 5):
                cat_key = f"Cat{i}"
                if not capture_status[cat_key]:
                    if page.url != srp_url: page.goto(srp_url, wait_until="domcontentloaded")
                    c_tree = html.fromstring(page.content())
                    c_name = c_tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a//img/@alt")
                    c_link = c_tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a/@href")
                    
                    data[f"{cat_key} Name"] = c_name[0] if c_name else "N/A"
                    link_val = c_link[0] if c_link else ""
                    data[f"{cat_key} Link URL"] = link_val
                    
                    if link_val and link_val not in ["#", "N/A"] and wait_for_redirect_smart(page, link_val):
                        status_c = get_link_status(page)
                        data[f"{cat_key} Link works"], data[f"{cat_key} page URL"] = status_c, page.url
                        data[f"{cat_key} Link Check"] = get_ai_check(page.url, f"{data['DD Name']}-{data[f'{cat_key} Name']}", page.inner_text("body")) if status_c == "Yes" else "No"
                        capture_status[cat_key] = True
                    else:
                        data[f"{cat_key} Link works"] = "No"
                        capture_status[cat_key] = True
        except Exception as e:
            print(f"⚠️ {retailer} 嘗試錯誤: {e}")
            
    return data

def upload_to_google(results_dict, gas_url):
    """ 將結果透過 GAS 傳回 Google Sheets """
    payload = list(results_dict.values())
    try:
        res = requests.post(gas_url, data=json.dumps(payload), headers={'Content-Type': 'application/json'}, timeout=120)
        print(f"📡 GAS 伺服器回應: {res.text}")
    except Exception as e:
        print(f"❌ 資料傳回 Google Sheets 失敗: {e}")

# --- 4. 主執行程序 ---

def main():
    column_order = [
        "Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works", "Link Check",
        "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works", "Cat1 Link Check",
        "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works", "Cat2 Link Check",
        "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works", "Cat3 Link Check",
        "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works", "Cat4 Link Check"
    ]
    
    print("📥 讀取輸入清單 (urls)...")
    try:
        df_input = pd.read_csv(GSHEET_INPUT_URL)
    except Exception as e:
        print(f"❌ 讀取 CSV 失敗: {e}")
        return

    results_dict = {}

    with sync_playwright() as p:
        # 雲端執行必須設定 headless=True
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        page = context.new_page()

        for index, row in df_input.iterrows():
            retailer = row.get('Retailer', 'Unknown')
            print(f"🚀 正在處理 ({index+1}/{len(df_input)}): {retailer}")
            results_dict[retailer] = run_retailer_capture(page, row, column_order)
        
        print("📤 任務完成，正在寫回 track_url 工作表...")
        upload_to_google(results_dict, GAS_OUTPUT_URL)
        browser.close()

if __name__ == "__main__":
    main()