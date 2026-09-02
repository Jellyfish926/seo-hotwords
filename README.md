# seo-hotwords · 热词雷达

每日 10:00(北京时间)拉 **Steam(在线榜 100 / 热门新品 100 / 最新发售 100)+ Roblox(Up-and-Coming / Top Trending / Top Playing Now)+ Google Trends 时下流行(美国)**,
和前一日快照对比,按四项权重打分,推送表现出众的热词。所有数据源免 key、只用 Python 标准库。

```
hotwords.py          # 拉取 → 跨日对比 → 评分 → 写 snapshots/ + reports/ → 打印摘要
built-sites.json     # 永久排除:已建站(归一化名)+ 常青巨头(steam:appid)。建成新站当天同步
snapshots/YYYY-MM-DD.json   # 当日全部候选原始数据 + 评分(跨日对比的依据)
reports/YYYY-MM-DD.md       # 当日日报(推送用)
cache/steam-names.json      # appid → 名字缓存
```

## 评分口径(满分 100,每个数字追得到某次调用)

| 项 | 分 | 怎么算 |
|---|---|---|
| 突增 | 40 | 任一榜排名上移 Δ≥50 → 40,≥20 → 30,≥10 → 20,≥5 → 10;新进榜按 100−名次算 Δ;Steam 周榜差(`last_week_rank`)也算。发售 >180 天的老游戏回暖封顶 20(只作事件页候选) |
| 增速 | 25 | 在线数 今/昨:≥2× → 25,≥1.5× → 18,≥1.2× → 10;无昨日快照记 0 + 未获取 |
| 新鲜度 | 15 | 发售 ≤14 天 15,≤45 天 10,≤90 天 4;未发售 8;Roblox Up-and-Coming 10。**评论 >10,000 且发售 <30 天 = 1.0 陷阱,清零** |
| 跨源 | 20 | Trends 时下流行命中 +12;≥2 个榜同现 +8 |

排除:`built-sites.json` 里的已建站与常青巨头;Steam 周榜与日榜都在前 30 且非新作的稳定巨头。
**≥50 分 = 表现出众**,进 seo-xuanci 七步法深评;连续 3 天在榜的写进观察项按 3/7/14/28 天复查。

## 手动跑

```bash
python3 hotwords.py --top 10
```

## 定时任务

由 Claude 的「每日热词雷达」任务在 02:00 UTC 执行:从 Mac 的 `seo-factory/.env` 读 `GITHUB_TOKEN` → 克隆本仓 → 跑脚本 → 提交推送 → 推送前 5 条摘要。
Mac 离线时任务仍跑,但不落盘、不做跨日对比(只有 Steam 周榜差),日报标「未获取(未持久化)」。
