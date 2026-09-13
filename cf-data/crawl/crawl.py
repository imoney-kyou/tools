# -*- coding: utf-8 -*-
"""
クラファン手帳 — 12社の募集一覧を読み取って cf-funds.json と案件画像を作る
実行: python crawl.py [--out DIR] [--only key,key]
GitHub Actions からは cf-data/crawl/crawl.py として週1で実行される（repo「tools」の直下が公開URLの /tools/ に当たる）。
"""
import asyncio, json, re, sys, os, hashlib, io, datetime, argparse, traceback
from playwright.async_api import async_playwright
import requests
from PIL import Image

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
JST = datetime.timezone(datetime.timedelta(hours=9))
TODAY = datetime.datetime.now(JST).date()

# ---------------------------------------------------------------- 会社定義
# card: {"link": 正規表現} → その href を持つ a を起点に親をたどってカードを作る
#       {"sel": CSS}      → その要素をカードにする
COMPANIES = {
    "reale":     {"url": "https://reale-fund.jp/fund", "card": {"link": r"/fund/\d+"}},
    "lseed":     {"url": "https://lseed.net/crowdfunding/fund", "card": {"link": r"/crowdfunding/fund/[0-9a-f-]{20,}"}},
    "torches":   {"url": "https://www.torches.fund/projects", "card": {"sel": "a.cassette-link"}},
    "gates":     {"url": "https://funding.gatestokyo.co.jp/investment/fund_list.html", "card": {"link": r"(lottery_application_entry|investment_entry|fund_detail)", "climb_img": True}},
    "cozuchi":   {"url": "https://cozuchi.com/system/funds?locale=ja", "card": {"link": r"/system/funds/\d+"}},
    "fantas":    {"url": "https://www.fantas-funding.com/customers/products", "card": {"link": r"/fund/\d+"}},
    "rimawari":  {"url": "https://rimawari.co.jp/investment/fund_list.html", "card": {"link": r"(investment_entry|fund_detail)", "climb_img": True}},
    "rakutama":  {"url": "https://rakutama.jp/projects", "card": {"sel": "a.cassette-link"}},
    "funds":     {"url": "https://funds.jp/fund/list", "card": {"link": r"/fund/detail/[^/#?]+$"}},
    "capima":    {"url": "https://www.capima.jp/fund", "card": {"link": r"/fund/\d+"}},
    "batsunagu": {"url": "https://batsunagu-funding.com/", "card": {"sel": ".fund-card"}, "cloudflare": True},
    "crowdbank": {"url": "https://crowdbank.jp/funds/search/", "card": {"link": r"/funds/crowd/A\d+", "climb_img": True}},
}

# ---------------------------------------------------------------- 共通ヘルパ
def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def z2h(s):
    return s.translate(str.maketrans("０１２３４５６７８９（）", "0123456789()"))

def first(pattern, text, flags=0, group=1):
    m = re.search(pattern, text, flags)
    return norm(m.group(group)) if m else ""

STATUS_WORDS = [
    (r"まもなく募集開始|募集開始前|募集前|募集予定", "pre"),
    (r"先着募集中|抽選募集中|募集中|募集終了まであと|残り時間", "open"),
    (r"抽選中|抽選結果待ち", "lot"),
    (r"抽選済|運用前|運用中|成立|償還待ち", "run"),
    (r"募集終了|運用終了|償還済|終了|不成立", "done"),
]
def status_from(text):
    for pat, st in STATUS_WORDS:
        if re.search(pat, text):
            return st
    return ""

def term_months(term):
    """'1年6ヶ月' '363日' '約19ヶ月' '1.0ヶ月' → 月数(int) or None"""
    t = z2h(term or "")
    y = re.search(r"(\d+)\s*年", t); m = re.search(r"(\d+(?:\.\d+)?)\s*[かヶヵカ]月", t); d = re.search(r"(\d+)\s*日", t)
    if y or m:
        return (int(y.group(1)) * 12 if y else 0) + (round(float(m.group(1))) if m else 0)
    if d:
        return max(1, round(int(d.group(1)) / 30.4))
    return None

def yen_min(s):
    """'1万円' '¥10,000' '10,000円' '1万〜' '10口 ¥100,000' → 円(int) or None"""
    t = z2h(s or "").replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*万", t)
    if m: return int(float(m.group(1)) * 10000)
    m = re.search(r"[¥￥]?\s*(\d{4,})", t)
    if m: return int(m.group(1))
    return None

def fmt_min(n):
    if not n: return ""
    return (f"{n//10000}万円" if n % 10000 == 0 else f"{n:,}円")

# ---------------------------------------------------------------- 各社パーサ
# 入力 c = {"text","alt","href","img","deadline_visible"} / 出力 dict or None
def p_reale(c):
    t = c["text"]
    name = first(r"(【[^】]+】\S+)", t) or c["alt"]
    return dict(name=name, status=status_from(t), yield_=first(r"想定利回り\s*([\d.]+)%", t),
                term=first(r"予定運用期間\s*([\d.]+\s*[ヵヶか]月|[\d.]+\s*年)", t), min_=first(r"最低募集単位\s*(\S+?)/", t),
                when=first(r"募集期間\s*(.+?)\s*まで", t), url=c["href"])

def p_lseed(c):
    t = c["text"]
    return dict(name=first(r"(LSEED\s*#\s*\d+)", t).replace(" ", ""), status=status_from(t), yield_=first(r"年予定利回り\s*([\d.]+)\s*%", t),
                term=first(r"運用期間\s*(\d+\s*年(?:\s*\d+\s*[ヶか]月)?|\d+\s*[ヶか]月)", t).replace(" ", ""), min_="1万円",
                when=first(r"募集期間\s*(\d{4}/\d{2}/\d{2}.*)$", t), url=c["href"])

def p_cassette(c):  # torches / rakutama
    t = c["text"]
    name = c["alt"] or first(r"^(.+?)\s*応募金額", t)
    applied = first(r"応募金額\s*¥([\d,]+)", t).replace(",", "")
    rate = first(r"(\d+)%\s*想定利回り", t)
    if c.get("deadline_visible"): st = "open"
    elif applied in ("", "0") and rate in ("", "0"): st = "pre"
    else: st = "done"
    return dict(name=name, status=st, yield_=first(r"想定利回り\s*\(年利\)\s*([\d.]+)%", t),
                term=first(r"運用期間\s*([\d.]+\s*[ヶか]月|\d+\s*日|\d+\s*年)", t).replace(" ", ""),
                min_=first(r"最低出資\s*金額\s*\(\d+口\)\s*(¥[\d,]+)", t), when=("あと" + first(r"募集終了まであと\s*(\S+)", t)) if c.get("deadline_visible") else "",
                url=c["href"], method=first(r"募集方式\s*(\S+)", t))

def p_gates(c):
    t = c["text"]
    name = re.sub(r"のファンドイメージ$", "", c["alt"] or "") or first(r"(GATES FUNDING\s*\d+号)", t)
    return dict(name=name, status=status_from(t), yield_=first(r"([\d.]+)%\s*\d+\s*[ヵヶか]月", t),
                term=first(r"[\d.]+%\s*(\d+\s*[ヵヶか]月)", t).replace(" ", ""), min_=first(r"([\d,]+円)\s*\d{4}/\d{2}/\d{2}", t),
                when=first(r"(\d{4}/\d{2}/\d{2}\s*\d{2}:\d{2})", t) + "〜", url="https://funding.gatestokyo.co.jp/investment/fund_list.html", method=first(r"(抽選式|先着式|先着順)", t))

def p_cozuchi(c):
    t = c["text"]
    name = re.sub(r"\s*\|\s*ファンドメイン画像.*$", "", c["alt"] or "")
    kind = "中長期運用型" if "中長期" in t else "短期運用型"
    return dict(name=name, status=status_from(t), yield_=first(r"想定利回り[^\d]*([\d.]+)\s*%", t),
                term=first(r"運用期間\s*(\S+)", t), min_=("10万円" if kind == "中長期運用型" else "1万円"),
                when=first(r"募集期間\s*(\S+)", t).replace("-", ""), url=c["href"], note=kind)

def p_fantas(c):
    t = z2h(c["text"])
    name = first(r"((?:FANTAS|ファンタス)[^ ]*(?:\s*(?:repro|check|development)\s*PJ)?\s*第?\s*\d+\s*号(?:\(※[^)]+\))?)", t) or first(r"(FANTAS\S*)", t)
    st = status_from(t)
    when = first(r"募集開始まで\s*(\S+)", t)
    if when: when = "募集開始まで " + when
    else:
        w2 = first(r"残り募集期間\s*(\S+)", t)
        when = ("残り " + w2) if w2 and "終了" not in w2 else ""
    return dict(name=name, status=st, yield_=first(r"予定分配率\s*([\d.]+)%", t), term=first(r"運用期間\s*(\S+)", t),
                min_=first(r"出資可能金額\s*(\S+?)[〜~]", t), when=when, url=c["href"], method=first(r"(抽選方式|先着方式)", t))

def p_rimawari(c):
    t = c["text"]
    return dict(name=first(r"^(利回り不動産\d+号ファンド（[^）]*）)", t) or first(r"^(\S+ファンド[^ ]*)", t), status=status_from(t.split("詳細はこちら")[-1]) or status_from(t),
                yield_=first(r"予定利回り[^\d]*([\d.]+)%", t), term=first(r"運用期間\s*(\S+)", t), min_=first(r"最低投資金額\s*(¥[\d,]+)", t),
                when=first(r"申込期間\s*(\S+)", t), url=c["href"])

def p_funds(c):
    t = c["text"]
    name = c["alt"] or first(r"^(.+?)\s*(?:投資家限定ファンド|先着|抽選|運用中|募集)", t)
    st = status_from(t)
    term = first(r"予定運用期間.*?(約?\s*\d+\s*[ヶか]月)\s*(?:200万円|\d+万円投資)", t) or first(r"(約\s*\d+\s*[ヶか]月)", t)
    return dict(name=name, status=st, yield_=first(r"予定利回り\s*\(年率税引前\)\s*([\d.]+)%", t), term=term.replace(" ", ""),
                min_="1円", when=first(r"(\d{4}/\d{1,2}/\d{1,2}\s*\([^)]*\)\s*\d{1,2}:\d{2})\s*(?:先着|抽選)?募集終了", t) and ("〜" + first(r"(\d{4}/\d{1,2}/\d{1,2}\s*\([^)]*\)\s*\d{1,2}:\d{2})\s*(?:先着|抽選)?募集終了", t)),
                url=c["href"], method=first(r"(先着|抽選)募集", t) and (first(r"(先着|抽選)募集", t)))

def p_capima(c):
    t = c["text"]
    st = status_from(t)
    name = c["alt"] or first(r"^(?:募集終了|抽選済|運用中|募集中|募集前|運用終了)?\s*(\S+（ファンド\d+号）)", t)
    return dict(name=name, status=st, yield_=first(r"想定利回り\s*([\d.]+)\s*[%％]", t), term=first(r"運用期間\s*(\d+\s*[ヶか]月)", t).replace(" ", ""),
                min_="1万円", when="", url=c["href"])

def p_batsunagu(c):
    t = c["text"]
    name = first(r"^(?:募集終了|運用中|募集中|募集前|運用終了|募集開始前|償還済み?)?\s*(?:償還済み?\s*)?(.+?)(?:【|\s本プロジェクトの特徴)", t)
    return dict(name=name, status=status_from(t.split(" ")[0]) or status_from(t), yield_=first(r"想定利回り\s*([\d.]+)\s*%", t), term=first(r"想定運用期間\s*(\S+)", t),
                min_="1万円", when=(lambda m: (m.group(1) + " " + m.group(2) + "〜") if m else "")(re.search(r"募集開始日\s*(\d{4}/\d{1,2}/(?:3[01]|[12]\d|0?[1-9]))\s*((?:[01]?\d|2[0-3]):\d{2})", t)),
                url=(c["href"] or "https://batsunagu-funding.com/").split("?")[0], method=first(r"【(抽選式|先着式)】", t))

def p_crowdbank(c):
    t = c["text"]
    name = first(r"^(?:日本|カナダ/米国|中国|アジア/オセアニア|欧州|アフリカ/中南米/その他)?\s*(?:建設/不動産事業|太陽光|風力|バイオマス|水力|地熱|物流|宿泊/飲食|エンターテイメント|医療/ヘルスケア|水処理|廃棄物処理|金融/マイクロファイナンス|IT/ソフトウェア|その他)?\s*(.+?)\s*(?:JPY|先着|抽選)", t)
    st = status_from(t)
    return dict(name=name, status=st, yield_=first(r"目標利回り\s*([\d.]+)\s*%", t) or first(r"([\d.]+)%\s*\d+ヶ月", t),
                term=first(r"運用期間\s*(\d+\s*[ヶか]月)", t).replace(" ", "") or first(r"[\d.]+%\s*(\d+ヶ月)", t), min_="1万円",
                when=("残り " + first(r"残り時間\s*(\d+日)", t)) if first(r"残り時間\s*(\d+日)", t) else ("〜" + first(r"募集終了日時\s*(\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2})", t) if first(r"募集終了日時\s*(\d{1,2}月\d{1,2}日)", t) else ""),
                url=c["href"], method=first(r"(先着|抽選)方式", t))

PARSERS = dict(reale=p_reale, lseed=p_lseed, torches=p_cassette, rakutama=p_cassette, gates=p_gates, cozuchi=p_cozuchi,
               fantas=p_fantas, rimawari=p_rimawari, funds=p_funds, capima=p_capima, batsunagu=p_batsunagu, crowdbank=p_crowdbank)

# ---------------------------------------------------------------- 取得
JS_CARDS = """
(cfg)=>{
  const res=[]; const seen=new Set();
  const pack=(el,a)=>{
    const t=(el.innerText||'').replace(/\\s+/g,' ').trim();
    const img=(a&&a.querySelector('img'))||el.querySelector('img'); const dl=el.querySelector('.cassette-deadline');
    let dv=false; if(dl){ const cs=getComputedStyle(dl); dv = cs.display!=='none' && cs.visibility!=='hidden'; }
    return {text:t.slice(0,1200), alt:img?(img.alt||''):'', href:a?a.href:'', img:img?(img.currentSrc||img.src||''):'', deadline_visible:dv};
  };
  if(cfg.sel){
    for(const el of document.querySelectorAll(cfg.sel)){ const a=el.matches('a[href]')?el:el.querySelector('a[href]'); const c=pack(el,a); const k=(c.href||'')+'|'+c.text.slice(0,50); if(seen.has(k))continue; seen.add(k); res.push(c); if(res.length>=cfg.max)break; }
  }else{
    const re=new RegExp(cfg.link);
    for(const a of document.querySelectorAll('a[href]')){
      const h=a.getAttribute('href')||''; if(!re.test(h)) continue;
      let el=a; for(let i=0;i<7;i++){ const ok=(el.innerText||'').length>80 && (!cfg.climb_img || el.querySelector('img')); if(ok) break; el=el.parentElement||el; }
      const c=pack(el,a); const k=c.href.split('#')[0]; if(seen.has(k)) continue; seen.add(k); res.push(c); if(res.length>=cfg.max) break;
    }
  }
  return res;
}
"""

async def fetch_cards(ctx, key, cfg, max_cards=12):
    page = await ctx.new_page()
    try:
        await page.goto(cfg["url"], wait_until="domcontentloaded", timeout=60000)
        if cfg.get("cloudflare"):
            for _ in range(12):
                await page.wait_for_timeout(2000)
                if "Checking" not in (await page.title()): break
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)
        c = dict(cfg["card"]); c["max"] = max_cards
        cards = await page.evaluate(JS_CARDS, c)
        return cards
    finally:
        await page.close()

def save_image(url, key, outdir):
    if not url: return ""
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Referer": COMPANIES[key]["url"]}, timeout=30)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        w, h = im.size
        if w < 120 or h < 80: return ""
        s = min(1.0, 480 / w); im = im.resize((round(w * s), round(h * s)), Image.LANCZOS)
        name = f"{key}-{hashlib.md5(url.encode()).hexdigest()[:10]}.jpg"
        os.makedirs(os.path.join(outdir, "img"), exist_ok=True)
        im.save(os.path.join(outdir, "img", name), quality=80, optimize=True)
        return "img/" + name
    except Exception:
        return ""

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    ap.add_argument("--only", default="")
    ap.add_argument("--max", type=int, default=12)
    args = ap.parse_args()
    keys = [k for k in COMPANIES if not args.only or k in args.only.split(",")]
    result = {"updatedAt": TODAY.isoformat(), "generatedAt": datetime.datetime.now(JST).isoformat(timespec="minutes"),
              "companies": {}, "funds": []}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(user_agent=UA, locale="ja-JP", viewport={"width": 1280, "height": 900})
        for key in keys:
            cfg = COMPANIES[key]; info = {"ok": False, "count": 0, "error": ""}
            try:
                cards = await fetch_cards(ctx, key, cfg, args.max)
                n = 0
                for c in cards:
                    try:
                        d = PARSERS[key](c)
                    except Exception as e:
                        continue
                    if not d or not (d.get("name") or "").strip():
                        continue
                    yl = d.get("yield_", "")
                    fund = {
                        "co": key, "name": norm(d["name"])[:80], "url": d.get("url") or cfg["url"],
                        "yield": (yl + "%") if yl else "", "term": norm(d.get("term", "")), "months": term_months(d.get("term", "")),
                        "min": norm(d.get("min_", "")), "minYen": yen_min(d.get("min_", "")), "when": norm(d.get("when", "")),
                        "status": d.get("status") or "", "method": norm(d.get("method", "")), "note": norm(d.get("note", "")),
                        "img": "", "srcImg": c.get("img", ""),
                    }
                    if fund["min"] and fund["minYen"]: fund["min"] = fmt_min(fund["minYen"])
                    result["funds"].append(fund); n += 1
                info["ok"] = n > 0; info["count"] = n
                if n == 0: info["error"] = "カードを読めませんでした（サイト構造の変更の可能性）"
            except Exception as e:
                info["error"] = (str(e).splitlines() or ["error"])[0][:200]
            result["companies"][key] = info
            print(f"[{key}] ok={info['ok']} count={info['count']} {info['error']}", flush=True)
        # 募集期間が取れなかった募集前/募集中の案件は詳細ページを見に行く（最大8件）
        n_detail = 0
        for f in result["funds"]:
            if f["status"] in ("open", "pre") and not f["when"] and f["url"] and n_detail < 8 and f["co"] in ("torches", "rakutama", "reale", "lseed", "cozuchi", "capima"):
                n_detail += 1
                try:
                    pg = await ctx.new_page()
                    await pg.goto(f["url"], wait_until="domcontentloaded", timeout=45000)
                    await pg.wait_for_timeout(2000)
                    body = norm(await pg.inner_text("body"))
                    await pg.close()
                    m = re.search(r"募集期間[:：]?\s*(\d{4}[/年.]\d{1,2}[/月.]\d{1,2}[^ ]{0,8}\s*\d{0,2}:?\d{0,2}\s*[〜～~-]\s*\d{0,4}[/年.]?\d{1,2}[/月.]\d{1,2}[^ ]{0,8}\s*\d{0,2}:?\d{0,2})", body)
                    if not m:
                        m = re.search(r"募集(?:開始|期間)[^\d]{0,8}(\d{4}[/年.]\d{1,2}[/月.]\d{1,2}[^ ]{0,8}\s*\d{0,2}:?\d{0,2})", body)
                    if m:
                        f["when"] = norm(m.group(1))[:40]
                    if f["co"] == "torches" and f["status"] == "pre" and "募集開始前" not in body and "募集中" in body:
                        f["status"] = "open"
                except Exception:
                    pass
        await browser.close()
    # 画像: 募集中/募集前は全部、それ以外は各社2件まで
    per = {}
    for f in result["funds"]:
        per.setdefault(f["co"], 0)
        if f["status"] in ("open", "pre", "lot") or per[f["co"]] < 2:
            f["img"] = save_image(f["srcImg"], f["co"], args.out)
            per[f["co"]] += 1
    # 古い画像の掃除
    keep = {f["img"].split("/")[-1] for f in result["funds"] if f["img"]}
    imgdir = os.path.join(args.out, "img")
    if os.path.isdir(imgdir):
        for fn in os.listdir(imgdir):
            if fn.endswith(".jpg") and fn not in keep:
                os.remove(os.path.join(imgdir, fn))
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "cf-funds.json"), "w", encoding="utf-8") as fp:
        json.dump(result, fp, ensure_ascii=False, indent=1)
    ok = sum(1 for v in result["companies"].values() if v["ok"])
    print(f"done: {ok}/{len(keys)} companies, {len(result['funds'])} funds -> {args.out}/cf-funds.json")
    if ok == 0:
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
