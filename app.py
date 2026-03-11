from flask import Flask, render_template, jsonify
import json, os, re, urllib.request, urllib.parse, urllib.error
from datetime import datetime
import time
import threading

app = Flask(__name__)

# ── 路徑（Railway 持久磁碟掛載到 /data）────────────────
DATA_DIR     = os.environ.get("DATA_DIR", ".")
RATES_FILE   = os.path.join(DATA_DIR, "rates.json")
DEPOSIT_FILE = os.path.join(DATA_DIR, "deposits.json")

# ── BOCHK 設定（從環境變數讀取）──────────────────────────
CLIENT_ID     = os.environ.get("BOCHK_CLIENT_ID",     "l7a0112f738b784f3ba3425c5af576f5bd")
CLIENT_SECRET = os.environ.get("BOCHK_CLIENT_SECRET", "6917800727d64b9980fbdf3509130cf4")
TOKEN_URL     = "https://apigateway.bochk.com/auth/oauth/v2/token"
RATE_URL      = "https://apigateway.bochk.com/fx/hkdrate/v1"
DEPOSIT_URL   = "https://apigateway.bochk.com/deposits/interest/timedeposit/v1"
BOCHK_PROMO_URL = "https://www.bochk.com/en/deposits/promotion/timedeposits.html"

_token_cache = {"token": None, "expires_at": 0}

PERIOD_MAP = {
    "D001":"1日","D007":"7日","D014":"14日",
    "M001":"1個月","M002":"2個月","M003":"3個月",
    "M006":"6個月","M009":"9個月","M012":"12個月"
}

FALLBACK = {
    "usd_new_fund":   {"3個月":"2.80%","6個月":"2.70%","3個月_PW":"3.00%","6個月_PW":"2.90%"},
    "hkd_new_fund":   {"3個月":"2.10%","6個月":"1.90%","3個月_PW":"2.10%","6個月_PW":"1.90%"},
    "usd_fx_promo":   {"7日":"8.8%","1個月":"4.0%"},
    "hkd_exch_promo": {"7日":"5.00%","1個月":"2.00%"},
    "new_fund_updated":"2026-03-07"
}

# ── 讀寫 ──────────────────────────────────────────────
def load_json(path):
    if not os.path.exists(path): return []
    try:
        with open(path,"r") as f:
            c = f.read().strip()
            return json.loads(c) if c else []
    except: return []

def save_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp,"w") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_rates():    return load_json(RATES_FILE)
def load_deposits(): return load_json(DEPOSIT_FILE)

def save_rate(sell, buy, timestamp, last_update=None):
    data = load_rates()
    data.append({"time":timestamp,"sell":round(sell,4),"buy":round(buy,4),
                 "spread":round(sell-buy,4),"last_update":last_update or timestamp})
    save_json(RATES_FILE, data[-500:])

def save_deposit(record):
    data = load_deposits()
    data.append(record)
    save_json(DEPOSIT_FILE, data[-200:])

# ── OAuth Token ───────────────────────────────────────
def get_access_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 30:
        return _token_cache["token"]
    try:
        body = urllib.parse.urlencode({
            "grant_type":"client_credentials",
            "client_id":CLIENT_ID,"client_secret":CLIENT_SECRET
        }).encode("utf-8")
        req = urllib.request.Request(TOKEN_URL, data=body, method="POST",
              headers={"Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            td = json.loads(resp.read())
        token = td.get("access_token")
        if token:
            _token_cache["token"]      = token
            _token_cache["expires_at"] = now + int(td.get("expires_in",3600))
            print(f"  [TOKEN] 有效期 {td.get('expires_in')} 秒")
            return token
    except urllib.error.HTTPError as e:
        print(f"  [TOKEN 錯誤] HTTP {e.code}: {e.read().decode('utf-8',errors='ignore')}")
    except Exception as e:
        print(f"  [TOKEN 錯誤] {e}")
    return None

# ── 抓取匯率 ──────────────────────────────────────────
def fetch_usd_rates():
    token = get_access_token()
    if not token: return None, None, None
    try:
        body = urllib.parse.urlencode({"lang":"en-US","Currency":"USD"}).encode("utf-8")
        req  = urllib.request.Request(RATE_URL, data=body, method="POST", headers={
            "Authorization":f"Bearer {token}",
            "Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        for item in data.get("products",[]):
            if str(item.get("Currency","")).upper() == "USD":
                sell = round(float(item["BankSell"])/100, 4)
                buy  = round(float(item["BankBuy"]) /100, 4)
                lu   = item.get("LastUpdateTime","")
                print(f"  [FX] Sell:{sell} Buy:{buy} {lu}")
                return sell, buy, lu
    except urllib.error.HTTPError as e:
        print(f"  [FX 錯誤] HTTP {e.code}")
        if e.code == 401: _token_cache["token"] = None
    except Exception as e:
        print(f"  [FX 錯誤] {e}")
    return None, None, None

# ── 爬取官網新資金利率 ────────────────────────────────
def scrape_new_fund_rates():
    try:
        req = urllib.request.Request(BOCHK_PROMO_URL, headers={
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept":"text/html,application/xhtml+xml,*/*"
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        usd_nf, hkd_nf, usd_fx, hkd_exch = {}, {}, {}, {}
        promo_updated = datetime.now().strftime("%Y-%m-%d")
        section = None; usd_col_idx = 6

        dm = re.search(r'published on (\d+ \w+ \d{4})', html)
        if dm:
            try: promo_updated = datetime.strptime(dm.group(1),"%d %B %Y").strftime("%Y-%m-%d")
            except: pass

        for line in html.replace("\r","").split("\n"):
            ls = line.strip()
            if "Preferential RMB & FX" in ls:            section = "fx_promo"
            elif "Preferential HKD Time Deposit" in ls:  section = "hkd_exch"
            elif "New Fund Preferential Time Deposit" in ls: section = "new_fund"

            if "|" not in ls or re.match(r'^[\|\-\s]+$', ls): continue
            cells = [re.sub(r'\*+','',c).strip() for c in ls.strip("|").split("|")]

            if len(cells) >= 4:
                cur = cells[0].upper().strip(); acc = cells[1] if len(cells)>1 else ""
                c3  = re.search(r'(\d+\.\d+)%', cells[2]) if len(cells)>2 else None
                c6  = re.search(r'(\d+\.\d+)%', cells[3]) if len(cells)>3 else None
                if "USD" == cur:
                    if any(x in acc for x in ["Enrich","i-Free","Other"]):
                        if c3: usd_nf["3個月"]=c3.group(1)+"%"
                        if c6: usd_nf["6個月"]=c6.group(1)+"%"
                    elif any(x in acc for x in ["Private","Wealth"]):
                        if c3: usd_nf["3個月_PW"]=c3.group(1)+"%"
                        if c6: usd_nf["6個月_PW"]=c6.group(1)+"%"
                elif "HKD" == cur:
                    if any(x in acc for x in ["Enrich","i-Free","Other"]):
                        if c3: hkd_nf["3個月"]=c3.group(1)+"%"
                        if c6: hkd_nf["6個月"]=c6.group(1)+"%"
                    elif any(x in acc for x in ["Private","Wealth"]):
                        if c3: hkd_nf["3個月_PW"]=c3.group(1)+"%"
                        if c6: hkd_nf["6個月_PW"]=c6.group(1)+"%"

            if section == "fx_promo":
                if "AUD" in cells and "USD" in cells and "GBP" in cells:
                    usd_col_idx = cells.index("USD"); continue
                tenor = cells[0] if cells else ""
                if ("7-day" in tenor or "1-month" in tenor) and len(cells)>usd_col_idx:
                    r = re.search(r'(\d+\.\d+)%', cells[usd_col_idx])
                    if r: usd_fx["7日" if "7" in tenor else "1個月"] = r.group(1)+"%"

            if section == "hkd_exch":
                tenor = cells[0] if cells else ""
                r = re.search(r'(\d+\.\d+)%', cells[1]) if len(cells)>1 else None
                if ("7-day" in tenor or "1-month" in tenor) and r:
                    hkd_exch["7日" if "7" in tenor else "1個月"] = r.group(1)+"%"

        print(f"  [SCRAPE] USD:{usd_nf} HKD:{hkd_nf} FX:{usd_fx}")

        if usd_nf:
            return {"usd_new_fund":usd_nf,"hkd_new_fund":hkd_nf,
                    "usd_fx_promo":usd_fx,"hkd_exch_promo":hkd_exch,
                    "new_fund_updated":promo_updated,
                    "scraped_at":datetime.now().strftime("%Y-%m-%d %H:%M")}
    except Exception as e:
        print(f"  [SCRAPE 錯誤] {e}")

    print("  [SCRAPE] ⚠️ 使用備用硬編碼值")
    return {**FALLBACK,"scraped_at":datetime.now().strftime("%Y-%m-%d %H:%M")+" (備用)"}

# ── 抓取存款利率 ──────────────────────────────────────
def fetch_deposit_rates():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    result    = {"time":timestamp,"usd":{},"hkd":{}}

    promo = scrape_new_fund_rates()
    if promo and promo.get("usd_new_fund"):
        result.update(promo)
    else:
        prev = load_deposits()
        last_real = next((r for r in reversed(prev)
                         if r.get("usd_new_fund") and "(備用)" not in r.get("scraped_at","")), None)
        if last_real:
            for k in ["usd_new_fund","hkd_new_fund","usd_fx_promo","hkd_exch_promo","new_fund_updated"]:
                result[k] = last_real.get(k, FALLBACK.get(k,{}))
            result["scraped_at"] = last_real.get("scraped_at","")+" (快取)"
        else:
            result.update({**FALLBACK,"scraped_at":timestamp+" (備用)"})

    token = get_access_token()
    if token:
        try:
            for cur_param, target in [("USD","usd"),("HKD","hkd")]:
                body = urllib.parse.urlencode({"lang":"en-US","Currency":cur_param}).encode("utf-8")
                req  = urllib.request.Request(DEPOSIT_URL, data=body, method="POST", headers={
                    "Authorization":f"Bearer {token}",
                    "Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())
                products = data.get("products",[])
                if not isinstance(products,list): products=[products]
                for product in products:
                    tier    = product.get("TierDetails",product)
                    details = tier.get("RateDetails",[]) if isinstance(tier,dict) else []
                    for item in details:
                        p=item.get("Period",""); rv=item.get("FixedRate","")
                        if p and rv:
                            label=PERIOD_MAP.get(p,p)
                            try:
                                r=float(rv)
                                if r>0: result[target][label]=f"{r:.4f}%"
                            except: pass
        except urllib.error.HTTPError as e:
            print(f"  [DEPOSIT API 錯誤] HTTP {e.code}")
            if e.code==401: _token_cache["token"]=None
        except Exception as e:
            print(f"  [DEPOSIT API 錯誤] {e}")

    return result

# ── 存款建議 ──────────────────────────────────────────
def get_deposit_advice(deposit_data):
    if not deposit_data:
        return {"signal":"neutral","label":"⚪ 尚無數據","color":"#888888",
                "score":0,"max_score":5,"signals":[]}
    latest=deposit_data[-1]; usd_nf=latest.get("usd_new_fund",{}); hkd_nf=latest.get("hkd_new_fund",{})
    nf_date=latest.get("new_fund_updated","")
    def pr(d,*keys):
        for k in keys:
            v=d.get(k,"")
            if v:
                try: return float(str(v).replace("%",""))
                except: pass
        return 0.0
    usd_3m=pr(usd_nf,"3個月"); usd_6m=pr(usd_nf,"6個月"); hkd_3m=pr(hkd_nf,"3個月")
    score,signals=0,[]; note=f"（官網更新：{nf_date}）"
    if usd_3m>=3.0:   score+=1; signals.append(f"💵 USD 新資金 3個月 {usd_3m}%，達 3.0% 以上 {note}")
    elif usd_3m>=2.0: signals.append(f"💵 USD 新資金 3個月 {usd_3m}%，中等水平 {note}")
    else:             signals.append(f"💵 USD 新資金 3個月：{usd_3m if usd_3m else '未取得'}{'%' if usd_3m else ''} {note}")
    if usd_6m>=2.5:   score+=1; signals.append(f"📈 USD 新資金 6個月 {usd_6m}%，中期鎖定理想")
    elif usd_6m>0:    signals.append(f"📊 USD 新資金 6個月 {usd_6m}%，一般水平")
    if usd_3m>0 and usd_6m>0:
        if usd_3m>=usd_6m: score+=1; signals.append(f"⬇️ USD 倒掛（3M:{usd_3m}% ≥ 6M:{usd_6m}%），短存為佳")
        else:              signals.append(f"⬆️ USD 向上（3M:{usd_3m}% → 6M:{usd_6m}%），可鎖定 6 個月")
    if hkd_3m>=2.0:   score+=1; signals.append(f"🇭🇰 HKD 新資金 3個月 {hkd_3m}%，具吸引力")
    elif hkd_3m>0:    signals.append(f"🇭🇰 HKD 新資金 3個月 {hkd_3m}%，一般水平")
    else:             signals.append(f"🇭🇰 HKD 新資金 3個月：未取得")
    if usd_3m>0 and hkd_3m>0:
        diff=round(usd_3m-hkd_3m,2)
        if diff>=0.5:   score+=1; signals.append(f"⚖️ USD（{usd_3m}%）比 HKD（{hkd_3m}%）高 {diff}%，美元更划算")
        elif diff>0:    signals.append(f"⚖️ USD 略高於 HKD {diff}%")
        else:           signals.append(f"⚖️ HKD（{hkd_3m}%）不低於 USD，可優先港元")
    if score>=4:   v={"signal":"deposit_now",  "label":"🟢 現在是存款好時機","color":"#4ade80"}
    elif score>=2: v={"signal":"deposit_watch","label":"🟡 可考慮存入",       "color":"#facc15"}
    else:          v={"signal":"deposit_wait", "label":"🔴 利率偏低，建議觀望","color":"#f87171"}
    return {**v,"score":score,"max_score":5,"signals":signals,
            "usd_new_fund":usd_nf,"hkd_new_fund":hkd_nf,
            "usd_regular":latest.get("usd",{}),"hkd_regular":latest.get("hkd",{}),
            "usd_fx_promo":latest.get("usd_fx_promo",{}),"hkd_exch_promo":latest.get("hkd_exch_promo",{}),
            "new_fund_updated":nf_date,"scraped_at":latest.get("scraped_at",""),"time":latest.get("time","")}

# ── 匯率技術分析 ──────────────────────────────────────
def calculate_rsi(rates,period=14):
    if len(rates)<period+1: return None
    gains,losses=[],[]
    for i in range(1,period+1):
        d=rates[-i]-rates[-i-1]
        (gains if d>0 else losses).append(abs(d))
    ag=sum(gains)/period if gains else 0; al=sum(losses)/period if losses else 0.0001
    return round(100-(100/(1+ag/al)),1)

def get_fx_advice(data):
    if len(data)<20:
        return {"signal":"neutral","label":"⚪ 數據不足","color":"#888888",
                "score":0,"max_score":4,"signals":["需要 20 筆以上數據。"],"stats":{}}
    sells=[d["sell"] for d in data]; current=sells[-1]; n=len(sells)
    ma5=round(sum(sells[-5:])/5,4); ma20=round(sum(sells[-20:])/20,4)
    ma30=round(sum(sells[-min(30,n):])/min(30,n),4); rsi=calculate_rsi(sells)
    rec=sells[-50:] if n>=50 else sells; hi,lo=max(rec),min(rec)
    rng=hi-lo if hi!=lo else 0.0001; pos=round((current-lo)/rng*100,1)
    score,signals=0,[]
    if ma5<ma20:   score+=1; signals.append(f"📉 MA5（{ma5}）< MA20（{ma20}），短期下跌，換匯成本較低")
    else:          signals.append(f"📈 MA5（{ma5}）> MA20（{ma20}），短期上升，成本偏高")
    if rsi is not None:
        if rsi<35:   score+=1; signals.append(f"💡 RSI={rsi}（<35），超賣，可能反彈")
        elif rsi>65: signals.append(f"⚠️ RSI={rsi}（>65），偏熱，不宜追高")
        else:        signals.append(f"📊 RSI={rsi}，中性區間（35–65）")
    else: signals.append("📊 RSI：數據不足")
    if current<ma30: score+=1; signals.append(f"✅ 當前（{current}）低於30筆均值（{ma30}）")
    else:            signals.append(f"❌ 當前（{current}）高於30筆均值（{ma30}）")
    if pos<15:   score+=1; signals.append(f"🟢 位於近50筆最低{pos}%，接近低位")
    elif pos>85: signals.append(f"🔴 位於近50筆最高（{pos}%），不宜買入")
    else:        signals.append(f"⚪ 位於中間位置（{pos}%）")
    if score>=3:   v={"signal":"buy",  "label":"🟢 建議買入美金",  "color":"#4ade80"}
    elif score==2: v={"signal":"watch","label":"🟡 可考慮分批買入","color":"#facc15"}
    else:          v={"signal":"wait", "label":"🔴 建議等待更佳時機","color":"#f87171"}
    return {**v,"score":score,"max_score":4,"signals":signals,
            "stats":{"current":current,"ma5":ma5,"ma20":ma20,"ma30":ma30,
                     "rsi":rsi,"low_50":round(lo,4),"high_50":round(hi,4),"position_pct":pos}}

# ── Flask 路由 ─────────────────────────────────────────
@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/rates")
def api_rates(): return jsonify(load_rates())

@app.route("/api/advice")
def api_advice(): return jsonify(get_fx_advice(load_rates()))

@app.route("/api/deposits")
def api_deposits(): return jsonify(load_deposits())

@app.route("/api/deposit_advice")
def api_deposit_advice(): return jsonify(get_deposit_advice(load_deposits()))

@app.route("/api/fetch_now")
def fetch_now():
    sell,buy,lu = fetch_usd_rates()
    dep         = fetch_deposit_rates()
    result      = {"success":False}
    if sell and buy:
        ts=datetime.now().strftime("%Y-%m-%d %H:%M")
        save_rate(sell,buy,ts,lu)
        result.update({"success":True,"sell":sell,"buy":buy,
                       "spread":round(sell-buy,4),"last_update":lu,"time":ts})
    save_deposit(dep)
    result["deposit"]={"usd_new_fund":dep.get("usd_new_fund",{}),
                       "hkd_new_fund":dep.get("hkd_new_fund",{}),
                       "new_fund_updated":dep.get("new_fund_updated",""),
                       "scraped_at":dep.get("scraped_at","")}
    if not result["success"]: result["error"]="無法取得匯率，請查看 Log"
    return jsonify(result)

# ── 背景執行緒 ────────────────────────────────────────
def background_fetch():
    print("  [啟動] 正在抓取初始數據...")
    sell,buy,lu=fetch_usd_rates()
    if sell and buy:
        ts=datetime.now().strftime("%Y-%m-%d %H:%M")
        save_rate(sell,buy,ts,lu)
        print(f"  [{ts}] FX Sell:{sell} Buy:{buy}")
    dep=fetch_deposit_rates()
    if dep:
        save_deposit(dep)
        print(f"  [啟動] USD新資金:{dep.get('usd_new_fund',{})} HKD新資金:{dep.get('hkd_new_fund',{})}")
    while True:
        time.sleep(600)
        sell,buy,lu=fetch_usd_rates()
        if sell and buy:
            ts=datetime.now().strftime("%Y-%m-%d %H:%M")
            save_rate(sell,buy,ts,lu)
            print(f"  [{ts}] FX Sell:{sell} Buy:{buy}")
        dep=fetch_deposit_rates()
        if dep: save_deposit(dep)

if __name__ == "__main__":
    threading.Thread(target=background_fetch, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
