#!/usr/bin/env python3
"""热词雷达 —— 每日拉 Steam / Roblox / Google Trends 三源榜单,跨日对比,综合评分,产出当日快照与日报。

用法:
  python3 hotwords.py            # 拉取 → 评分 → 写 snapshots/YYYY-MM-DD.json + reports/YYYY-MM-DD.md → 打印摘要
  python3 hotwords.py --top 10   # 摘要条数
  python3 hotwords.py --dry      # 不写文件

纪律(与 seo-xuanci 一致):
  * 每个数字都来自某次实测调用;拿不到就标「未获取 + 原因」,绝不估算。
  * 只依赖 Python 标准库;所有数据源免 key。
  * 已建站清单(built-sites.json)里的游戏永久排除;常青巨头(连续两周稳居 Steam 前 30)排除。
"""
import argparse, json, os, re, sys, time, datetime as dt, urllib.request, urllib.parse, xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR, REPORT_DIR, CACHE_DIR = (os.path.join(ROOT, d) for d in ("snapshots", "reports", "cache"))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
TODAY = dt.date.today().isoformat()
NOTES = []  # 未获取记录


def get(url, timeout=25, retries=2, json_=True):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            return json.loads(data) if json_ else data.decode("utf-8", "replace")
        except Exception as e:  # noqa
            last = e
            time.sleep(1.5 * (i + 1))
    NOTES.append(f"未获取: {url[:90]}… ({type(last).__name__}: {str(last)[:60]})")
    return None


def norm(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# ---------- 数据源 ----------
def steam_most_played():
    d = get("https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/")
    if not d:
        return []
    return [{"appid": r["appid"], "rank": r["rank"], "last_week_rank": r.get("last_week_rank"), "peak": r.get("peak_in_game")}
            for r in d["response"]["ranks"]]


def steam_search(filter_=None, sort=None, count=100):
    q = {"query": "", "start": 0, "count": count, "json": 1, "supportedlang": "english", "cc": "us", "l": "english"}
    if filter_:
        q["filter"] = filter_
    if sort:
        q["sort_by"] = sort
    d = get("https://store.steampowered.com/search/results/?" + urllib.parse.urlencode(q))
    out = []
    if not d:
        return out
    for i, it in enumerate(d.get("items", []), 1):
        m = re.search(r"/apps/(\d+)/", it.get("logo", ""))
        if m:
            out.append({"appid": int(m.group(1)), "name": it["name"], "rank": i})
    return out


def steam_ccu(appid):
    d = get(f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={appid}", retries=1)
    return d["response"].get("player_count") if d and d.get("response", {}).get("result") == 1 else None


def steam_details(appid):
    d = get(f"https://store.steampowered.com/api/appdetails?appids={appid}&l=english&cc=us&filters=basic,release_date,genres", retries=1)
    try:
        x = d[str(appid)]["data"]
        return {"name": x.get("name"), "release": x.get("release_date", {}).get("date"), "coming": x.get("release_date", {}).get("coming_soon"),
                "genres": [g["description"] for g in x.get("genres", [])], "type": x.get("type")}
    except Exception:
        return None


def steam_reviews(appid):
    d = get(f"https://store.steampowered.com/appreviews/{appid}?json=1&language=all&purchase_type=all&num_per_page=0", retries=1)
    try:
        s = d["query_summary"]
        return {"total": s["total_reviews"], "positive": s["total_positive"]}
    except Exception:
        return None


def roblox_sorts():
    d = get("https://apis.roblox.com/explore-api/v1/get-sorts?sessionId=1&device=computer&country=us")
    out = {}
    if not d:
        return out
    for s in d.get("sorts", []):
        if s.get("contentType") != "Games":
            continue
        out[s["sortId"]] = [{"universeId": g["universeId"], "name": g["name"], "rank": i, "players": g.get("playerCount"),
                             "up": g.get("totalUpVotes"), "down": g.get("totalDownVotes"), "genre": g.get("genreL1")}
                            for i, g in enumerate(s.get("games", []), 1)]
    return out


def trends_rss():
    x = get("https://trends.google.com/trending/rss?geo=US", json_=False)
    out = []
    if not x:
        return out
    try:
        root = ET.fromstring(x)
        ns = {"ht": "https://trends.google.com/trending/rss"}
        for it in root.iter("item"):
            t = it.findtext("title") or ""
            tr = it.findtext("ht:approx_traffic", namespaces=ns) or ""
            news = [n.findtext("ht:news_item_title", namespaces=ns) or "" for n in it.findall("ht:news_item", ns)]
            out.append({"title": t, "traffic": tr, "news": news})
    except Exception as e:
        NOTES.append(f"未获取: Trends RSS 解析失败 ({e})")
    return out


GAME_RE = re.compile(r"\b(game|games|steam|roblox|codes|wiki|patch|update|dlc|release|trailer|gameplay|xbox|ps5|playstation|nintendo|switch|early access|beta)\b", re.I)


# ---------- 排除 ----------
def load_json(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def parse_release(s):
    if not s:
        return None
    for fmt in ("%d %b, %Y", "%b %d, %Y", "%b %Y", "%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


# ---------- 评分 ----------
def score_all(today, yday, built, days_hist):
    """today: dict 候选(key -> 各源数据)。返回评分后的列表。
    权重(满分 100):突增 40 / 增速 25 / 新鲜度 15 / 跨源 20。全部由实测值算出,缺项记 0 并注明未获取。"""
    rows = []
    for key, c in today.items():
        if key in built or norm(c.get("name")) in built:
            continue
        ev, s_jump, s_growth, s_fresh, s_cross, why = [], 0, 0, 0, 0, []
        y = yday.get(key) if yday else None
        # --- 突增(40):Steam 日排名 / 周排名 / Roblox 排名 / 新进榜
        deltas = []
        for src in ("steam_top", "steam_popularnew", "roblox_up", "roblox_trending", "roblox_top"):
            r = c.get(src, {}).get("rank")
            if r is None:
                continue
            ry = (y or {}).get(src, {}).get("rank") if y else None
            if ry is None and y is not None:
                deltas.append(("新进榜", src, r, 100 - r))
            elif ry is not None:
                deltas.append((f"昨日 {ry}→今 {r}", src, r, ry - r))
        lw = c.get("steam_top", {}).get("last_week_rank")
        r0 = c.get("steam_top", {}).get("rank")
        if lw and r0:
            deltas.append((f"周榜 {lw}→{r0}(Steam 自带 last_week_rank)", "steam_top", r0, lw - r0))
        if deltas:
            best = max(deltas, key=lambda d: d[3])
            if best[3] >= 50:
                s_jump = 40
            elif best[3] >= 20:
                s_jump = 30
            elif best[3] >= 10:
                s_jump = 20
            elif best[3] >= 5:
                s_jump = 10
            why.append(f"突增 {s_jump}:{best[1]} {best[0]}(Δ{best[3]:+d})")
        elif y is None and not yday:
            why.append("突增 0:无昨日快照(首日基线),仅用 Steam 周榜差")
        else:
            why.append("突增 0:各榜排名无明显上移")
        # --- 增速(25):CCU 今/昨,或 Roblox 在线 今/昨
        ccu, ccu_y = c.get("ccu"), (y or {}).get("ccu") if y else None
        rp, rp_y = c.get("roblox_players"), (y or {}).get("roblox_players") if y else None
        ratio = None
        if ccu and ccu_y:
            ratio = ccu / ccu_y
            ev.append(f"Steam CCU {ccu_y}→{ccu}")
        elif rp and rp_y:
            ratio = rp / rp_y
            ev.append(f"Roblox 在线 {rp_y}→{rp}")
        if ratio:
            s_growth = 25 if ratio >= 2 else 18 if ratio >= 1.5 else 10 if ratio >= 1.2 else 0
            why.append(f"增速 {s_growth}:{ev[-1]}(×{ratio:.2f})")
        else:
            why.append("增速 0:未获取(无昨日在线数可比)" if (ccu or rp) else "增速 0:未获取(在线数未取到)")
        # --- 新鲜度(15):发售 ≤45 天;1.0 陷阱(评论 >10000 且发售 <30 天)清零
        rel = parse_release(c.get("release"))
        if rel:
            age = (dt.date.fromisoformat(TODAY) - rel).days
            if 0 <= age <= 14:
                s_fresh = 15
            elif age <= 45:
                s_fresh = 10
            elif age <= 90:
                s_fresh = 4
            rv = c.get("reviews")
            if rv and rv.get("total", 0) > 10000 and age < 30:
                s_fresh = 0
                why.append(f"新鲜度 0:1.0 陷阱(发售 {age} 天但评论 {rv['total']})")
            else:
                why.append(f"新鲜度 {s_fresh}:发售 {rel.isoformat()}({age} 天)")
        elif c.get("coming"):
            s_fresh = 8
            why.append("新鲜度 8:未发售(预告期埋伏候选)")
        elif "roblox_up" in c:
            s_fresh = 10
            why.append("新鲜度 10:Roblox Up-and-Coming 榜(按定义是新作)")
        elif "roblox_trending" in c or "roblox_top" in c:
            s_fresh = 3
            why.append("新鲜度 3:Roblox 无发售日字段,仅 trending/top 在榜")
        else:
            why.append("新鲜度 0:发售日未获取")
        # 老游戏回暖:发售 >180 天且非预告 —— 只能做事件页,突增封顶 20
        if rel and (dt.date.fromisoformat(TODAY) - rel).days > 180 and s_jump > 20:
            s_jump = 20
            why.append("封顶:发售 >180 天的老游戏回暖 → 只作事件页候选,突增封顶 20")
        # --- 跨源(20):出现在 ≥2 个源 / Trends 命中
        srcs = [s for s in ("steam_top", "steam_popularnew", "steam_new", "roblox_up", "roblox_trending", "roblox_top") if s in c]
        n = len(set(s.split("_")[0] for s in srcs)) + (1 if c.get("trends") else 0)
        if c.get("trends"):
            s_cross += 12
            why.append(f"跨源 +12:Trends 时下流行命中「{c['trends']['title']}」({c['trends']['traffic']})")
        if len(srcs) >= 2:
            s_cross += 8
            why.append(f"跨源 +8:同时在 {', '.join(srcs)}")
        s_cross = min(s_cross, 20)
        # --- 常青巨头排除:Steam 周榜与日榜都在前 30 且连续出现 ≥2 周(用 last_week_rank 判)
        if r0 and lw and r0 <= 30 and lw <= 30 and s_fresh == 0:
            why.append("排除:常青巨头(周榜/日榜均前 30 且非新作)")
            continue
        total = s_jump + s_growth + s_fresh + s_cross
        streak = days_hist.get(key, 0) + 1
        rows.append({"key": key, "name": c.get("name"), "appid": c.get("appid"), "universeId": c.get("universeId"),
                     "score": total, "parts": {"突增": s_jump, "增速": s_growth, "新鲜度": s_fresh, "跨源": s_cross},
                     "why": why, "sources": srcs, "ccu": ccu, "roblox_players": rp, "release": c.get("release"),
                     "genres": c.get("genres"), "days_on_radar": streak})
    rows.sort(key=lambda r: (-r["score"], r["name"] or ""))
    return rows


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--max-detail", type=int, default=60, help="取发售日/评论数的候选上限(按初步分)")
    a = ap.parse_args()
    for d in (SNAP_DIR, REPORT_DIR, CACHE_DIR):
        os.makedirs(d, exist_ok=True)
    names = load_json(os.path.join(CACHE_DIR, "steam-names.json"), {})
    built = set(load_json(os.path.join(ROOT, "built-sites.json"), {}).get("normalized", []))
    evergreen = set(load_json(os.path.join(ROOT, "built-sites.json"), {}).get("evergreen", []))
    built |= evergreen

    t0 = time.time()
    top = steam_most_played()
    popnew = steam_search(filter_="popularnew")
    newest = steam_search(sort="Released_DESC")
    rb = roblox_sorts()
    tr = trends_rss()
    print(f"[fetch] steam_top={len(top)} popularnew={len(popnew)} newest={len(newest)} roblox={ {k: len(v) for k, v in rb.items()} } trends={len(tr)} ({time.time()-t0:.0f}s)")

    cand = {}
    def put(key, **kw):
        c = cand.setdefault(key, {})
        for k, v in kw.items():
            if v is not None and k not in c:
                c[k] = v
            elif k in ("steam_top", "steam_popularnew", "steam_new", "roblox_up", "roblox_trending", "roblox_top"):
                c[k] = v
        return c

    # Steam 名字:缓存 + 缺的查 appdetails
    need = [r["appid"] for r in top if str(r["appid"]) not in names]
    for r in popnew + newest:
        names.setdefault(str(r["appid"]), r["name"])
    if need:
        with ThreadPoolExecutor(8) as ex:
            for appid, dd in zip(need, ex.map(steam_details, need)):
                if dd and dd.get("name"):
                    names[str(appid)] = dd["name"]
    for r in top:
        nm = names.get(str(r["appid"]))
        if not nm:
            continue
        put(f"steam:{r['appid']}", name=nm, appid=r["appid"], steam_top=r)
    for r in popnew:
        put(f"steam:{r['appid']}", name=r["name"], appid=r["appid"], steam_popularnew={"rank": r["rank"]})
    for r in newest:
        put(f"steam:{r['appid']}", name=r["name"], appid=r["appid"], steam_new={"rank": r["rank"]})
    for sid, key in (("up-and-coming", "roblox_up"), ("top-trending", "roblox_trending"), ("top-playing-now", "roblox_top")):
        for g in rb.get(sid, []):
            put(f"roblox:{g['universeId']}", name=g["name"], universeId=g["universeId"], roblox_players=g["players"],
                genres=[g["genre"]] if g.get("genre") else None, **{key: {"rank": g["rank"]}})
    # Trends 命中:标题归一化后与候选名互相包含,或新闻标题带游戏词
    nm_index = {norm(c["name"]): k for k, c in cand.items() if c.get("name")}
    for it in tr:
        nt = norm(it["title"])
        hit = None
        for nn, k in nm_index.items():
            if len(nn) >= 5 and (nn in nt or nt in nn):
                hit = k
                break
        if hit:
            cand[hit]["trends"] = it
        elif GAME_RE.search(it["title"] + " " + " ".join(it["news"])):
            put(f"trends:{nt}", name=it["title"], trends=it)

    # CCU:Steam popularnew + newest + top(已有 peak,但取当前)
    steam_ids = [c["appid"] for k, c in cand.items() if k.startswith("steam:")]
    with ThreadPoolExecutor(12) as ex:
        for appid, v in zip(steam_ids, ex.map(steam_ccu, steam_ids)):
            cand[f"steam:{appid}"]["ccu"] = v
    # 昨日快照
    yday, days_hist = None, {}
    snaps = sorted(f for f in os.listdir(SNAP_DIR) if f.endswith(".json") and f[:-5] < TODAY)
    if snaps:
        prev = load_json(os.path.join(SNAP_DIR, snaps[-1]), {})
        yday = prev.get("candidates", {})
        days_hist = prev.get("days_on_radar", {})
        print(f"[compare] 对比快照 {snaps[-1]}")
    else:
        NOTES.append("未获取: 无昨日快照,跨日突增/增速本轮全部为 0(首日基线)")
    # 初评 → 前 N 取发售日 / 评论数
    pre = score_all(cand, yday, built, days_hist)
    detail_ids = [r["appid"] for r in pre if r["appid"]][: a.max_detail]
    with ThreadPoolExecutor(8) as ex:
        for appid, dd, rv in zip(detail_ids, ex.map(steam_details, detail_ids), ex.map(steam_reviews, detail_ids)):
            c = cand[f"steam:{appid}"]
            if dd:
                c.update({k: v for k, v in dd.items() if v is not None and k != "name"})
            if rv:
                c["reviews"] = rv
    rows = score_all(cand, yday, built, days_hist)
    # 落盘
    snap = {"date": TODAY, "generated_at": dt.datetime.utcnow().isoformat() + "Z", "candidates": cand,
            "scores": rows, "days_on_radar": {r["key"]: r["days_on_radar"] for r in rows}, "notes": NOTES,
            "sources": {"steam_top": len(top), "steam_popularnew": len(popnew), "steam_new": len(newest),
                        "roblox": {k: len(v) for k, v in rb.items()}, "trends": len(tr)}}
    report = render(rows, a.top, snap)
    if not a.dry:
        with open(os.path.join(SNAP_DIR, f"{TODAY}.json"), "w") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1, default=str)
        with open(os.path.join(REPORT_DIR, f"{TODAY}.md"), "w") as f:
            f.write(report)
        with open(os.path.join(CACHE_DIR, "steam-names.json"), "w") as f:
            json.dump(names, f, ensure_ascii=False)
    print(report)


def render(rows, top, snap):
    L = [f"# 热词雷达 {TODAY}", "",
         f"来源实测:Steam 在线榜 {snap['sources']['steam_top']} / 热门新品 {snap['sources']['steam_popularnew']} / 最新发售 {snap['sources']['steam_new']};"
         f"Roblox {snap['sources']['roblox']};Trends 时下流行 {snap['sources']['trends']} 条。候选 {len(snap['candidates'])},评分后 {len(rows)}。", ""]
    strong = [r for r in rows if r["score"] >= 50]
    L.append(f"**表现出众(≥50 分):{len(strong)} 个**" if strong else "**今日无表现出众的热词(无 ≥50 分候选)** —— 下面是最高的几个,仅供参考。")
    L += ["", "| # | 游戏 | 分 | 突增/增速/新鲜/跨源 | 在线 | 发售 | 依据 |", "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows[:top], 1):
        p = r["parts"]
        online = f"Steam {r['ccu']}" if r.get("ccu") else (f"Roblox {r['roblox_players']}" if r.get("roblox_players") else "未获取")
        ident = f"[{r['name']}](https://store.steampowered.com/app/{r['appid']})" if r.get("appid") else (f"{r['name']}(Roblox {r['universeId']})" if r.get("universeId") else r["name"])
        L.append(f"| {i} | {ident} | **{r['score']}** | {p['突增']}/{p['增速']}/{p['新鲜度']}/{p['跨源']} | {online} | {r.get('release') or '—'} | {'; '.join(r['why'])} |")
    L += ["", "评分口径:突增 40(日榜/周榜排名上移或新进榜)· 增速 25(在线数今/昨)· 新鲜度 15(发售 ≤45 天;1.0 陷阱清零)· 跨源 20(Trends 命中 +12,≥2 榜同现 +8)。"
          "已建站与常青巨头已排除。每个数字追得到某次调用;缺的写未获取。", ""]
    if snap["notes"]:
        L += ["未获取 / 备注:"] + [f"- {n}" for n in snap["notes"][:12]]
    L += ["", "下一步:≥50 分的候选进 seo-xuanci 七步法深评(先过已建站清单与机制硬门槛);连续 3 天在榜的写进观察项按 3/7/14/28 天复查。"]
    return "\n".join(L)


if __name__ == "__main__":
    main()
