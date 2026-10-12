import os
import time
import pandas as pd
from playwright.sync_api import sync_playwright
# 容錯匯入 stealth：相容新版(2.x 的 Stealth)，套件缺失/版本不符時優雅略過，
# 絕不讓反偵測套件的問題拖垮整個爬蟲啟動。
try:
    from playwright_stealth import Stealth
    _STEALTH = Stealth()
except Exception:
    _STEALTH = None
from lxml import html
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, unquote, parse_qs
import re
import base64
import requests
import json
import random
from collections import Counter

# --- 1. 設定區域 ---
# 三個網址一律從環境變數讀，程式碼裡不留預設值。這個 repo 會公開，而 GAS 端點外洩
# 等於讓任何人都能對試算表寫入；一旦 commit 進 git 歷史就清不掉（只能換端點）。
#   本機執行       ：在本檔同層放一個 .env，一行一組 KEY=VALUE（.env 已列入 .gitignore）
#   GitHub Actions ：設在 repo Settings → Secrets，再由 workflow 的 env: 映射進來
# 需要的三個變數：GSHEET_INPUT_URL / GAS_OUTPUT_URL / TRACK_URL_DATA_CSV
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.isfile(_ENV_FILE):
    # 極簡 .env 解析（不引入 python-dotenv 依賴）：一行一組 KEY=VALUE，# 開頭是註解。
    # 用 setdefault，讓已存在的環境變數（如 CI 注入的 Secrets）優先，不被 .env 蓋掉。
    with open(_ENV_FILE, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

_REQUIRED_ENV = ("GSHEET_INPUT_URL", "GAS_OUTPUT_URL", "TRACK_URL_DATA_CSV")
_MISSING_ENV = [_k for _k in _REQUIRED_ENV if not os.environ.get(_k)]
if _MISSING_ENV:
    raise SystemExit(
        "❌ 缺少必要的環境變數：" + ", ".join(_MISSING_ENV) + "\n"
        "   本機：在程式同層建一個 .env，一行一組 KEY=VALUE\n"
        "   CI  ：在 repo Settings → Secrets and variables → Actions 設定，"
        "並在 workflow 的 env: 區塊映射過來"
    )

GSHEET_INPUT_URL = os.environ["GSHEET_INPUT_URL"]
GAS_OUTPUT_URL = os.environ["GAS_OUTPUT_URL"]
TRACK_URL_DATA_CSV = os.environ["TRACK_URL_DATA_CSV"]

# 中繼/失敗的「真實網域」黑名單：只比對 hostname，避免落地頁追蹤參數
# (force_referer=search.yahoo.com、pname-Yahoo 等) 把真正落地頁誤判成中繼。
INTERMEDIATE_DOMAIN_BLACKLIST = [
    "yahoo.com", "viglink.com", "sovrn.com", "linkbux.com",
    "financebuzz.com", "validclick.net", "shophermedia.net",
    "clickroll.net", "rover.ebay.com", "ampxdirect.com", "rd.bizrate.com",
    "top-best.com",   # 搜尋套利/廣告轉址落地頁（RSOC，非真實零售商本站）
    "digidip.net",    # 聯盟行銷連結變現/追蹤跳板（同 viglink/sovrn 性質）
    "pureleads.com",  # 潛在客戶開發/搜尋套利服務（非真實零售商本站）
    "affinity.net",   # Yahoo adslib 點擊轉址器(ww2.affinity.net/fly1)；有時會往下跳、有時卡住
    # SRVTRCK（前 Yieldkit，德國漢堡）的聯盟追蹤跳板，同 viglink/sovrn/digidip 性質。
    # 2026-09-17 加入：曾兩次贏下多數決被當成 retailer 網域（Temu 4:3、L.L. Bean 4:1），
    # 導致分類誤標 Yes / 正確落地被誤判。根路徑回 200 不轉址，只能靠網域比對。
    "linksprf.com",
    # 搜尋套利落地頁（RSOC），同 top-best.com 性質。2026-09-22 實測確認是死路：
    # /monetize?b=<brand> 302 回它自己首頁，渲染出通用優惠目錄
    # （title "Best offers | shoppingwaves.net"），不會往零售商本站跳。
    "shoppingwaves.net",
    # 同一個廣告商的轉址基礎設施，Temu / Maurices 的落地都曾停在上面。
    # 2026-10-01 復驗 Temu 三次各抽到不同轉址器（birgi-zqq → tre9 → temu.com 直達），
    # 證實是聯盟商輪替。加黑名單讓它觸發 LANDING_RETRY 自動重抽，當輪就有機會救回。
    "birgi-zqq.com",  # /zokvisitor/<uuid>/... 轉址器
    "tre9.com"        # zp.tre9.com/<uuid>?target=alpha-low-... 轉址器
]
# 錯誤/非正常頁面標記：比對整條 URL（scheme 或錯誤字串）
INTERMEDIATE_URL_MARKERS = ["chrome-error://", "chromewebdata", "access-denied"]

DD_WAIT_TIMEOUT = 8000          # 等 DD 元素出現的毫秒上限
DD_DETECT_ATTEMPTS = 2          # DD 抓不到時的重載重試總次數
# 等待跳轉時的輪詢次數。窗口 = ROUNDS × INTERVAL，且是「page.goto 返回之後」才開始算。
# 2026-09-09 Kohl's 實測：goto 在 shophermedia click.track 返回(4.4~5.7s)，該頁要跑完 IPQS
# 風控才往下跳，kohls.com 要到第 6~7 輪才 commit，提早返回還要多一輪確認 → 恰好第 7~8 輪。
# 舊值 6（9 秒）根本不夠，Kohl's 會停在 click.track 被標成中繼待判。
# 代價：真死的連結（affinity fly1 404）每次都會把窗口燒滿，每條 +9 秒。
REDIRECT_POLL_ROUNDS = 12       # 一般廠商的輪詢次數（18 秒窗口）
REDIRECT_POLL_ROUNDS_EBAY = 3   # eBay 另有 ebay.com 斷路器，維持極速不動
REDIRECT_POLL_INTERVAL = 1500   # 每輪等待毫秒
SRP_SETTLE_RANGE = (2.5, 5.0)   # 進 SRP 後等待頁面穩定的隨機秒數
LOAD_SETTLE_TIMEOUT = 8000      # 讀 title 前等待導航穩定的毫秒上限
GAS_POST_RETRIES = 3            # 發射回 GAS 的重試次數
LANDING_RETRY = 1               # 落地跳轉停在中繼網域時的重試次數（設 0 可關閉，只對失敗筆加時間）
LANDING_RETRY_PAUSE = 3         # 落地重試前的暫停秒數
CAT_RETRY = 3                   # 單一分類判 same/Error 時，重新點擊 re-roll 聯盟商的重試次數（設 0 可關閉）
CAT_RETRY_PAUSE = 2             # 分類重試前的暫停秒數
RETAILER_GAP_RANGE = (5, 12)    # 廠商之間的隨機間隔秒數（限流綁 IP 與間隔無關，故用較短值省時間）
# 例外廠商（關鍵字，小寫比對）：來源 DD 連結本身就重複/錯誤（人工點擊也導到同一頁），
# 非反爬造成，重試無用。列此清單者直接跳過抓取，避免浪費重試與誤標，也不覆蓋既有資料。
EXCEPTION_RETAILERS = ["aarp"]
WARMUP_URL = "https://www.yahoo.com/"  # 開跑前暖機訪問，取得 cookie/consent 讓 session 更像真人

# 模擬美國使用者的瀏覽器地區指紋（IP 仍以實際執行環境為準，這層只統一語言/時區）
US_LOCALE = "en-US"
US_TIMEZONE = "America/New_York"
US_ACCEPT_LANGUAGE = "en-US,en;q=0.9"

# --- 2. 工具函式 ---

def is_data_complete(row_data):
    """ 觸發爬蟲的核心判斷：Landing page URL 與 Link works 必須一起看 """
    landing_status = str(row_data.get("Landing page URL", "")).strip()
    if landing_status.lower() in ["", "nan", "crawler failed", "dd cannot be found"]:
        return False
    # 2026-10-01 補上：整筆旗標已改寫在 Link works（不再覆寫 Landing page URL，
    # 以保住「實際停住的位置」）。若只看 Landing page URL，需重跑 / crawler failed
    # 的列會因為該欄是個真網址而被當成「已完成」，7 天內都不會被重跑 ——
    # 明明標記需重跑卻不重跑，正好相反。
    if str(row_data.get("Link works", "")).strip() in ("需重跑", "crawler failed"):
        return False
    return True

def is_intermediate_domain(url):
    """ 黑名單：排除廣告跳轉網域（只比對 hostname，避免落地頁追蹤參數誤殺） """
    if not url: return True
    url_lower = str(url).lower()
    if any(m in url_lower for m in INTERMEDIATE_URL_MARKERS):
        return True
    host = urlparse(url_lower).hostname or ""
    return any(host == d or host.endswith("." + d) for d in INTERMEDIATE_DOMAIN_BLACKLIST)

def _safe_title(page):
    """ 先等導航穩定再抓 title 並包 try，避免頁面跳轉中呼叫 title() 觸發
        'Execution context was destroyed' 而讓整筆失敗。 """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=LOAD_SETTLE_TIMEOUT)
    except Exception:
        pass
    try:
        return page.title().strip().split('|')[0].split('-')[0].strip().lower()
    except Exception:
        return ""

def wait_for_redirect_smart(page, initial_url, retailer_name):
    """ 專對 eBay 優化的極速跳轉邏輯 """
    is_ebay = "ebay" in retailer_name.lower()
    timeout = 15000 if is_ebay else 35000
    resp = None
    try:
        # goto 逾時也不放棄：跳轉鏈長的站即使沒在時限內完成，
        # 當下 page.url 通常已抵達落地頁，後續靠輪詢確認穩定即可。
        resp = page.goto(initial_url, wait_until="domcontentloaded", timeout=timeout, referer="https://search.yahoo.com/")
    except Exception:
        resp = None

    last_url = ""
    # 💡 eBay 只要跳轉一次就檢查，不進入長時間循環
    max_loops = REDIRECT_POLL_ROUNDS_EBAY if is_ebay else REDIRECT_POLL_ROUNDS
    for _ in range(max_loops):
        try:
            curr_url = page.url
        except Exception:
            break
        # 💡 eBay 核心斷路器：只要進到 ebay.com 且脫離 rover 追蹤，立刻回傳
        if is_ebay and "ebay.com" in curr_url and "rover.ebay" not in curr_url:
            return resp
        if curr_url == last_url and not is_intermediate_domain(curr_url):
            return resp
        last_url = curr_url
        page.wait_for_timeout(REDIRECT_POLL_INTERVAL)
    return resp

def _url_key(url):
    """ 取 hostname + 路徑(去尾斜線、忽略 query) 當作頁面識別。
        用來判斷分類是否導到與落地相同的頁面——比標題可靠（標題常被品牌名/
        反爬頁如 'Bot or Not?' 汙染，使不同分類頁看起來相同）。 """
    try:
        u = urlparse(str(url).lower())
        return (u.hostname or "") + (u.path or "").rstrip("/")
    except Exception:
        return str(url).lower()

def _reg_domain(url):
    """ 取「可註冊網域」(hostname 最後兩段，如 www.fanatics.com → fanatics.com)。
        用於「同網域規則」：分類必須落在 retailer 自己的網域才算真 Yes，
        落在外部網域(套利/追蹤站)一律視為未落地 → 觸發重試/推測/中繼待判。 """
    try:
        host = urlparse(str(url).lower()).hostname or ""
    except Exception:
        return ""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host

# 中繼/追蹤網址常把「真正的目的地」藏在這些 query 參數裡（URL 編碼或 Base64）：
#   t=(bizrate)、lp=(boostclscale)、product_url=(peakoptions)、eu=(digidip) 等
EMBEDDED_TARGET_PARAMS = [
    "t", "lp", "product_url", "url", "u", "murl", "ru",
    "dest", "destination", "target", "redirect", "r", "eu"
]

def _try_b64_url(s):
    """ 試著把字串當 Base64/base64url 解碼，看裡面是不是網址（或含網址的 JSON） """
    try:
        pad = s + "=" * (-len(s) % 4)
        dec = base64.urlsafe_b64decode(pad).decode("utf-8", "ignore")
    except Exception:
        return ""
    if dec.lower().startswith("http"):
        return dec
    m = re.search(r'https?://[^\s"\\]+', dec)
    return m.group(0) if m else ""

def extract_embedded_target(url):
    """ 從中繼/追蹤網址的 query 參數，盡量還原它「本來要導去」的真實網址。
        支援 URL 百分比編碼（含雙重編碼）與 Base64；還原不出來就回空字串。 """
    try:
        qs = parse_qs(urlparse(str(url)).query)
    except Exception:
        return ""
    for key in EMBEDDED_TARGET_PARAMS:
        if key not in qs or not qs[key]:
            continue
        raw = qs[key][0]
        for _ in range(2):
            if raw.lower().startswith("http"):
                return raw
            dec = unquote(raw)
            if dec == raw:
                break
            raw = dec
        if raw.lower().startswith("http"):
            return raw
        cand = _try_b64_url(qs[key][0])
        if cand:
            return cand
    return ""

# --- 3. 核心抓取函式 ---

def run_retailer_capture(page, row, column_order):
    retailer = str(row.get('Retailer', 'N/A'))
    srp_url = str(row.get('SRP', ''))
    data = {col: "" for col in column_order}
    data["Retailer"], data["SRP"] = retailer, srp_url
    titles_map = {}
    landing_domain = ""  # retailer 落地頁的可註冊網域；分類必須落在此網域才算真 Yes（同網域規則）
    # 例外廠商：來源 DD 連結本身重複(人工點擊亦同)。繞過 same 限制、照抓重複資料，不判失敗。
    is_exception = any(x in retailer.lower() for x in EXCEPTION_RETAILERS)

    try:
        # Step 1: Yahoo SRP
        page.goto(srp_url, wait_until="domcontentloaded", timeout=30000)

        # 🔁 等 DD 模組真的出現再抓；抓不到就重載重試，避免「零等待直接抓」抓到
        #    尚未載入完成的頁面而誤報 DD cannot be found。
        tree, dd_label = None, []
        for attempt in range(DD_DETECT_ATTEMPTS):
            try:
                page.wait_for_selector("div[class*='TopNavCommerce'] h3 a[aria-label]", timeout=DD_WAIT_TIMEOUT)
            except Exception:
                pass
            time.sleep(random.uniform(*SRP_SETTLE_RANGE))
            tree = html.fromstring(page.content())
            dd_label = tree.xpath("//div[contains(@class,'TopNavCommerce')]//h3/a/@aria-label")
            if dd_label:
                break
            if attempt < DD_DETECT_ATTEMPTS - 1:
                page.reload(wait_until="domcontentloaded", timeout=30000)

        if not dd_label:
            data["Landing page URL"] = "DD cannot be found"
            data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            return data

        data["DD Name"] = dd_label[0]

        # Step 2: Landing Page
        landing_raw = tree.xpath("//div[contains(@class,'compTitle')]/h3/a/@href")
        # 落地停在黑名單中繼站時重試逃離（限流多為瞬時，重跑常能成功）
        landing_url = ""
        if landing_raw:
            for attempt in range(LANDING_RETRY + 1):
                wait_for_redirect_smart(page, landing_raw[0], retailer)
                if not is_intermediate_domain(page.url):
                    break
                if attempt < LANDING_RETRY:
                    print(f"   ↻ {retailer} 落地停在中繼網域，{LANDING_RETRY_PAUSE}s 後重試跳轉...")
                    time.sleep(LANDING_RETRY_PAUSE)
            landing_url = page.url or ""

        # 先收集各分類最終網址與推測目的地（網域判定延後到「多數決」之後）。
        # 逃離條件只看「非黑名單中繼站 且 非與落地完全相同頁」；真正的 retailer 網域稍後才決定。
        landing_key0 = _url_key(landing_url) if (landing_url and not is_intermediate_domain(landing_url)) else None
        cats = []
        for i in range(1, 5):
            c_link = tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a/@href")
            c_name = tree.xpath(f"//div[contains(@class,'TopNavCommerce')]/div[3]//li[{i}]//a//img/@alt")
            name = c_name[0] if c_name else "N/A"
            if not c_link:
                cats.append({"key": f"Cat{i}", "name": name, "link": None, "url": "", "intended": ""})
                continue
            curr_url = ""
            for attempt in range(CAT_RETRY + 1):
                wait_for_redirect_smart(page, c_link[0], retailer)
                curr_url = page.url
                # 逃離中繼站/同落地頁；例外廠商連結重複，接受 same 不再重試逃離
                if not is_intermediate_domain(curr_url) and (is_exception or not (landing_key0 and _url_key(curr_url) == landing_key0)):
                    break
                if attempt < CAT_RETRY:
                    print(f"   ↻ {retailer} Cat{i} 停在中繼/同落地頁，{CAT_RETRY_PAUSE}s 後重抽聯盟商重試 ({attempt+1}/{CAT_RETRY})...")
                    time.sleep(CAT_RETRY_PAUSE)
            cats.append({"key": f"Cat{i}", "name": name, "link": c_link[0],
                         "url": curr_url, "intended": extract_embedded_target(curr_url)})
            page.wait_for_timeout(1000)

        # 🧭 多數決決定 retailer 可註冊網域：候選 = 落地 + 各分類最終網址 + 各分類推測目的地（排除黑名單中繼站）。
        #    「落地被套利站/內容農場洗掉、但分類一致指向本站」時，多數會落在真正的 retailer 網域；
        #    離群的內容農場落地不會被採信，真分類也不再被同網域規則誤殺。
        domain_votes = Counter()
        for u in [landing_url] + [c["url"] for c in cats] + [c["intended"] for c in cats]:
            if u and not is_intermediate_domain(u):
                d = _reg_domain(u)
                if d:
                    domain_votes[d] += 1
        retailer_domain = domain_votes.most_common(1)[0][0] if domain_votes else ""
        landing_domain = retailer_domain
        if len(domain_votes) > 1:
            print(f"   🧭 {retailer} retailer 網域(多數決)={retailer_domain} 票數={dict(domain_votes)}")

        def _judge(u):
            # 回傳 (verdict, 要記錄的網址)。verdict: Yes / 需重跑 / Error
            #
            # 2026-09-11：移除 Yes(推測) 分支（與 _CL 版同步）。原本會從中繼站的 query 參數
            # 挖出「它本來要去的網址」當落點回報，但那是**宣告值**不是實際落點 —— 若宣告值
            # 可採信，這支程式就不必存在：rdlw 的 custData base64 離線解一次就有落地 + 4 個
            # 分類的全部網址，不必開瀏覽器。而它存在的理由恰好是宣告值 ≠ 實際落點
            # （zappos 宣告 /kids、實際停在 /null/OgLWL-ICAQc.zso）。
            # 原則：沒真的到過那一頁，就不能標成 Yes。
            if u and not is_intermediate_domain(u) and retailer_domain and _reg_domain(u) == retailer_domain:
                return "Yes", u
            is_err = any(m in str(u).lower() for m in INTERMEDIATE_URL_MARKERS)
            if u and str(u).lower().startswith("http") and not is_err:
                # 沒抵達零售商本站：記錄「實際停住的位置」並標記需重跑。
                # 網址照留是為了下次查得出上次卡在哪一站，但標籤明示這不是有效落點。
                return "需重跑", u
            return "Error", ""

        # 判定落地
        landing_needs_rerun = False
        if landing_url:
            lv, lstore = _judge(landing_url)
            if lv == "Error":
                data["Landing page URL"] = "crawler failed"
            else:
                data["Landing page URL"] = lstore
                data["Link works"] = "Yes" if lv == "Yes" else lv
                titles_map["Landing"] = _url_key(lstore)
                if lv != "Yes":
                    landing_needs_rerun = (lv == "需重跑")
                    print(f"   🔁 {retailer} 落地={lv}: {lstore[:140]}")
        else:
            data["Landing page URL"] = "crawler failed"

        # 💡 額度斷路器：Landing 失敗則不跑 Categories 判定
        if data["Landing page URL"] == "crawler failed":
            data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            return data

        # 判定各分類（用多數決出來的 retailer_domain）
        has_cat_failed = False
        has_rerun = landing_needs_rerun
        for c in cats:
            cat_key, name = c["key"], c["name"]
            data[f"{cat_key} Name"] = name
            if c["link"] is None:
                if name != "N/A":
                    has_cat_failed = True
                continue
            data[f"{cat_key} Link URL"] = c["link"]
            u = c["url"]
            if u and not is_intermediate_domain(u) and _url_key(u) == titles_map.get("Landing"):
                cv, cstore = "same", u  # 導回與落地完全相同頁
            else:
                cv, cstore = _judge(u)
            if cv == "Yes":
                data[f"{cat_key} page URL"] = cstore
                data[f"{cat_key} Link works"] = "Yes"
            elif cv == "需重跑":
                data[f"{cat_key} page URL"] = cstore
                data[f"{cat_key} Link works"] = "需重跑"
                has_rerun = True
                print(f"   🔁 {retailer} {cat_key} 沒抵達最後一頁，記錄停住位置待重跑: {cstore[:140]}")
            elif cv == "same" and is_exception:
                # 例外廠商：來源連結重複，繞過 same 限制、照抓重複資料，不判整筆失敗
                data[f"{cat_key} page URL"] = cstore
                data[f"{cat_key} Link works"] = "same"
            else:  # same(非例外) 或 Error
                data[f"{cat_key} Link works"] = cv
                has_cat_failed = True

        # 整筆旗標一律寫在 Link works，不覆寫 Landing page URL。
        # 2026-09-14：has_cat_failed 原本會把 Landing page URL 蓋成 "crawler failed"，
        # 連已經抓成功的落地網址一起擦掉，於是「被機器人驗證擋住」和「DD 設定重複」
        # 在 sheet 上長得一模一樣。保住落地網址才有得區分。
        if has_cat_failed:
            data["Link works"] = "crawler failed"
            print(f"   ⛔ {retailer} 整筆標記 crawler failed（有分類判 same/Error）")
        elif has_rerun:
            # 只要有任一格沒抵達最後一頁，這一列就不算完成。
            data["Link works"] = "需重跑"
            print(f"   🔁 {retailer} 整筆標記需重跑（有格子沒抵達最後一頁）")

        data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        return data
    except Exception as e:
        print(f"   ❌ 擷取異常: {e}")
        data["Landing page URL"] = "crawler failed"
        data["Update Date"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        return data

def post_to_gas(result):
    """ 發射回 GAS，加重試 + backoff，避免一次網路抖動就讓整個任務崩潰中止。 """
    for attempt in range(1, GAS_POST_RETRIES + 1):
        try:
            requests.post(GAS_OUTPUT_URL, json=result, timeout=20)
            return True
        except Exception as e:
            print(f"   ⚠️ GAS 發射失敗 ({attempt}/{GAS_POST_RETRIES}): {e}")
            if attempt < GAS_POST_RETRIES:
                time.sleep(2 * attempt)
    return False

# --- 4. 主流程 ---

def _print_geo():
    # 印出對外 IP 與國家，方便在 GitHub Actions log 確認是否從美國 IP 執行
    try:
        j = requests.get("https://ipinfo.io/json", timeout=10).json()
        print(f"🌐 對外 IP: {j.get('ip')} | 國家: {j.get('country')} | 地區: {j.get('region')}")
        if j.get("country") != "US":
            print("⚠️ 注意：目前非美國 IP，結果可能與美國使用者不同！")
    except Exception as e:
        print(f"🌐 IP 國家檢查略過: {e}")

def main():
    print(f"🚀 極速模式啟動 | 本月剩餘額度預估支援中")
    _print_geo()
    try:
        df_input = pd.read_csv(GSHEET_INPUT_URL)
    except Exception as e:
        print(f"❌ 讀取來源清單失敗: {e}")
        return

    existing_records = {}
    try:
        cb = random.randint(1000, 9999)
        fresh_url = f"{TRACK_URL_DATA_CSV}&t={int(time.time())}&cb={cb}"
        df_existing = pd.read_csv(fresh_url)
        for _, r in df_existing[::-1].iterrows():
            name = str(r['Retailer']).strip()
            if name not in existing_records: existing_records[name] = r.to_dict()
    except Exception:
        pass

    column_order = ["Retailer", "SRP", "Update Date", "DD Name", "Landing page URL", "Link works", 
                    "Cat1 Name", "Cat1 Link URL", "Cat1 page URL", "Cat1 Link works",
                    "Cat2 Name", "Cat2 Link URL", "Cat2 page URL", "Cat2 Link works",
                    "Cat3 Name", "Cat3 Link URL", "Cat3 page URL", "Cat3 Link works",
                    "Cat4 Name", "Cat4 Link URL", "Cat4 page URL", "Cat4 Link works"]

    with sync_playwright() as p:
        # CI(GitHub Actions / ubuntu-latest)只裝了 chromium，不可加 channel="chrome"
        browser = p.chromium.launch(headless=True)
        try:
            # 縮小 Viewport 減少渲染壓力
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1024, "height": 768},
                locale=US_LOCALE,
                timezone_id=US_TIMEZONE,
                extra_http_headers={"Accept-Language": US_ACCEPT_LANGUAGE}
            )
            # 套用 stealth（新版 2.x API）；容錯：失敗不影響爬蟲
            if _STEALTH:
                try:
                    _STEALTH.apply_stealth_sync(context)
                except Exception:
                    pass
            page = context.new_page()

            # 🔥 暖機：開跑前先訪問 Yahoo 首頁，取得 cookie/consent，讓後續請求更像正常使用者
            try:
                page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(random.randint(2000, 4000))
                print("🔥 暖機完成，開始爬取")
            except Exception:
                pass

            for index, row in df_input.iterrows():
                retailer_name = str(row['Retailer']).strip()
                srp_url = str(row.get('SRP', ''))

                # 跳過來源表裡的空白列：pandas 會把空儲存格讀成 NaN，str() 之後變字串
                # "nan"，於是每天都被當成一個叫 nan 的廠商去抓，必然 DD cannot be found。
                # 2026-10-01 實測 GitHub 近 20 輪有 4 輪在跑這個空列，純浪費額度。
                if retailer_name.lower() in ("", "nan") or not srp_url.startswith("http"):
                    continue

                if retailer_name in existing_records:
                    old = existing_records[retailer_name]
                    if is_data_complete(old):
                        try:
                            old_date = datetime.strptime(str(old.get('Update Date', '')).replace(" UTC", ""), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                            if (datetime.now(timezone.utc) - old_date < timedelta(days=7)):
                                print(f"⏭️  Skip: {retailer_name}")
                                continue
                        except Exception:
                            pass

                print(f"🔍 ({index+1}/{len(df_input)}) Processing: {retailer_name}")
                result = run_retailer_capture(page, row, column_order)
                if result:
                    # 與 _CL 一致：帶 mode 讓 GAS 走「依 Retailer 就地覆蓋」分支
                    # （GAS 的無 mode else 分支是空的，不帶 mode 會寫不進去）
                    result["mode"] = "update_by_retailer"
                    print(f"   → 結果: {result.get('Landing page URL','')}")
                    post_to_gas(result)

                # 拉長 Retailer 之間的隨機間隔，大幅降低被限流的機率
                time.sleep(random.uniform(*RETAILER_GAP_RANGE))
        finally:
            browser.close()
    print("🎉 任務結束！")

if __name__ == "__main__":
    main()