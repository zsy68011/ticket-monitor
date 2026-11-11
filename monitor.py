#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国家大剧院 & 北京艺术中心 开票监控

数据源（官方、免登录、无签名）:
  1) 演出日历  https://www.chncpa.org/was5/web/search?channelid=266971&ycid=73&month=YYYY-MM
     返回 JSONP: getPerfInfos([...])，每行一个场次，覆盖大剧院本部 + 北京艺术中心 + 台湖
  2) 票务详情  https://openapi.chncpa.org/product/detail?productId={id}&channel=pc
     data.productSaleStatus: 售卖中(990134) / 即将开票(990118) / 已结束(990135)
  3) 中央民族乐团·民族音乐厅  https://www.cnto.net.cn/ccno/ycyg/yanchuzixun_list.shtml
     「演出预告」列表页，新开票公告标题带【开票】前缀，按文章 URL 去重

开票判定:
  - 日历中新出现且状态为「售卖中」的演出            -> 新开票
  - 已知「即将开票」的演出状态跳变为「售卖中」      -> 正式开票
  - 已知演出新增了场次日期                          -> 加场

用法:
  python monitor.py            # 常规轮询
  python monitor.py --weekly   # 发送周报（周一由 GitHub Actions 定时触发）
"""
import json
import html
import os
import re
import sys
import time
import datetime as dt
from pathlib import Path

import requests

try:
    import yaml
except ImportError:  # 没装 PyYAML 时用内置默认配置
    yaml = None

# ----------------------------------------------------------------------------
# 常量与配置
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
CONFIG_FILE = BASE_DIR / "config.yml"

CALENDAR_URL = "https://www.chncpa.org/was5/web/search"
CAL_CHANNEL = "266971"  # 演出日历频道：全院（含北京艺术中心、台湖）
DETAIL_URL = "https://openapi.chncpa.org/product/detail"

CNTO_LIST_URL = "https://www.cnto.net.cn/ccno/ycyg/yanchuzixun_list.shtml"
CNTO_BASE = "https://www.cnto.net.cn"
CNTO_VENUE = "中央民族乐团·民族音乐厅"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 30
TZ = dt.timezone(dt.timedelta(hours=8))  # Asia/Shanghai

STATUS_ONSALE = "售卖中"
STATUS_UPCOMING = "即将开票"
STATUS_ENDED = "已结束"

WPOS_ID = "北京艺术中心"
WPOS_ID_TAIHU = "台湖"

ALERT_COOLDOWN_HOURS = 12      # 故障告警冷却，避免每次轮询都轰炸
WEEK_LOG_DAYS = 14             # 周报事件日志保留天数
UPCOMING_RECHECK_EVERY_MIN = 0  # >0 时限制对「即将开票」商品的复查频率（分钟）

DEFAULT_CONFIG = {
    "months_ahead": 2,        # 监控当月起共 3 个月（current + 2）
    "push_upcoming": False,   # 新演出官宣为「即将开票」时是否也推送
    "venues": {"ncpa": True, "bpac": True, "taihu": False, "cnto": True},
}

# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def http_get(url, retries=2):
    last_err = None
    for _ in range(retries + 1):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2)
    raise last_err


def fetch_calendar(month: str):
    """month 形如 2026-11，返回场次列表"""
    url = f"{CALENDAR_URL}?channelid={CAL_CHANNEL}&ycid=73&month={month}"
    r = http_get(url)
    m = re.search(r"getPerfInfos\((.*)\)", r.text, re.S)
    if not m:
        raise ValueError(f"日历返回格式异常: {month}")
    return json.loads(m.group(1))


def fetch_detail(pid: str):
    url = f"{DETAIL_URL}?productId={pid}&channel=pc"
    r = http_get(url)
    data = r.json().get("data") or {}
    if not data.get("productId"):
        raise ValueError(f"详情返回为空: {pid}")
    return data


def fetch_cnto_opens():
    """抓中央民族乐团「演出预告」列表页，返回【开票】公告文章列表"""
    r = http_get(CNTO_LIST_URL)
    if r.encoding is None or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = "utf-8"  # 服务器未声明字符集，页面实为 UTF-8
    items, seen = [], set()
    for m in re.finditer(
            r'<a[^>]+href="(/ccno/ycyg/[^"]+\.shtml)"[^>]*>([^<]+)</a>', r.text):
        path, title = m.group(1).strip(), html.unescape(m.group(2)).strip()
        if path.endswith("yanchuzixun_list.shtml") or path in seen:
            continue
        if not (title.startswith("【开票") or title.startswith("【购票")):
            continue
        seen.add(path)
        items.append({"pid": f"cnto:{path}", "title": title,
                      "url": CNTO_BASE + path})
    return items


# ----------------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------------
def now_bj():
    return dt.datetime.now(TZ)


def venue_bucket(vname: str) -> str:
    if vname and vname.startswith(WPOS_ID):
        return "bpac"
    if vname and vname.startswith(WPOS_ID_TAIHU):
        return "taihu"
    return "ncpa"


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists() and yaml:
        user = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # 损坏则重建
            pass
    return {
        "version": 1,
        "bootstrap": True,                 # 首次运行静默建档，不推送
        "edge_month_init": {},             # 已完成首启建档的最外层月份
        "last_alert_ts": 0,
        "upcoming_checked": {},            # pid -> 上次复查时间戳
        "week_log": [],                    # 近期事件（供周报）
        "products": {},                    # pid -> 记录
    }


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def month_range(months_ahead: int):
    """返回当月起 months_ahead+1 个月的 'YYYY-MM' 列表"""
    today = now_bj().date()
    out = []
    y, mth = today.year, today.month
    for _ in range(months_ahead + 1):
        out.append(f"{y:04d}-{mth:02d}")
        mth += 1
        if mth > 12:
            mth, y = 1, y + 1
    return out


def shift_month(base: str, n: int) -> str:
    y, m = map(int, base.split("-"))
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


# ----------------------------------------------------------------------------
# 推送（支持 Server酱 / PushPlus / WxPusher，三选一，配置了谁就用谁）
# ----------------------------------------------------------------------------
def push(title: str, content_md: str):
    key = os.getenv("SCT_SENDKEY", "").strip()
    if key:
        r = requests.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": title[:32], "desp": content_md},
            headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != 0:
            raise RuntimeError(f"Server酱推送失败: {j}")
        return True

    token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    if token:
        r = requests.post(
            "https://www.pushplus.plus/send",
            json={"token": token, "title": title[:100],
                  "content": content_md, "template": "markdown"},
            headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != 200:
            raise RuntimeError(f"PushPlus推送失败: {j}")
        return True

    app_token = os.getenv("WPUSHER_APP_TOKEN", "").strip()
    uid = os.getenv("WPUSHER_UID", "").strip()
    if app_token and uid:
        r = requests.post(
            "https://wxpusher.zjiecode.com/api/send/message",
            json={"appToken": app_token, "content": content_md,
                  "summary": title[:99], "contentType": 3, "uids": [uid]},
            headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            raise RuntimeError(f"WxPusher推送失败: {j}")
        return True

    print("[WARN] 未配置任何推送渠道（SCT_SENDKEY / PUSHPLUS_TOKEN / WPUSHER_APP_TOKEN+WPUSHER_UID），仅打印：")
    print(f"--- {title} ---\n{content_md}")
    return False


def maybe_alert(state, reason: str):
    """故障告警，带冷却时间"""
    now = time.time()
    if now - state.get("last_alert_ts", 0) < ALERT_COOLDOWN_HOURS * 3600:
        print(f"[ALERT-SUPPRESSED] {reason}")
        return False
    state["last_alert_ts"] = now
    ts = now_bj().strftime("%m-%d %H:%M")
    push("开票监控出问题了，请检查",
         f"**{ts}** 轮询异常：{reason}\n\n"
         f"下次轮询会自动重试；若持续异常请到仓库 Actions 页查看日志。")
    return True


# ----------------------------------------------------------------------------
# 事件与消息
# ----------------------------------------------------------------------------
def fmt_event(e):
    if e.get("url"):  # 中央民族乐团开票公告（详情见文章）
        return "\n".join([f"**{e['name']}**",
                          f"- 场馆：{e['vname']}",
                          f"- [查看开票公告]({e['url']})"])
    lines = [f"**{e['name']}**"]
    lines.append(f"- 场馆：{e['vname']}")
    lines.append(f"- 时间：{e['date_range']}")
    if e.get("playtime"):
        lines.append(f"- 场次：{e['playtime']}")
    if e.get("price"):
        lines.append(f"- 票价：{e['price']}")
    lines.append(f"- [立即购票](https://wticket.chncpa.org/product.html?id={e['pid']})")
    return "\n".join(lines)


def build_message(events):
    tag = {"new_on_sale": "新开票", "on_sale": "正式开票",
           "session_added": "加场", "upcoming": "官宣待开票"}
    parts = ["## 🎫 开票提醒\n"]
    by_type = {}
    for e in events:
        by_type.setdefault(e["type"], []).append(e)
    order = ["new_on_sale", "on_sale", "session_added", "upcoming"]
    for t in order:
        for i, e in enumerate(by_type.get(t, []), 1):
            prefix = tag[t]
            parts.append(f"### {i}. [{prefix}]\n{fmt_event(e)}\n")
    parts.append("\n---\n*自动监控，非官方。未做自动抢票，请尽快购票。*")
    return "\n".join(parts)


def log_week_event(state, e):
    state["week_log"].append({
        "ts": now_bj().strftime("%Y-%m-%d %H:%M"),
        "type": e["type"], "pid": e["pid"], "name": e["name"],
        "vname": e["vname"], "dates": e["date_range"],
        "price": e.get("price", ""),
    })


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def collect_products(cfg):
    """抓取月份窗口内的日历，聚合到产品级。返回 (products, failed_months)"""
    products, failed = {}, []
    for month in month_range(cfg["months_ahead"]):
        try:
            rows = fetch_calendar(month)
        except Exception as e:  # noqa: BLE001
            print(f"[ERR] 日历抓取失败 {month}: {e}")
            failed.append(month)
            continue
        for row in rows:
            pid = str(row.get("PRODUCTID") or "").strip()
            if not pid:
                continue  # 非票务演出，跳过
            vname = row.get("VNAME") or "未知场馆"
            bucket = venue_bucket(vname)
            if not cfg["venues"].get(bucket, True):
                continue
            p = products.setdefault(pid, {
                "pid": pid,
                "name": (row.get("CALENDARSHOWNAME") or row.get("PRODUCTNAME") or "").strip(),
                "vname": vname, "bucket": bucket,
                "dates": set(), "begin": row.get("BEGINDATE", ""),
                "end": row.get("ENDDATE", ""),
                "playtime": row.get("PLAYTIME", ""),
                "price": price_str(row),
            })
            if row.get("SHOWDATE"):
                p["dates"].add(row["SHOWDATE"])
    return products, failed


def price_str(row):
    lo, hi = row.get("ZDPJ"), row.get("ZGPJ")
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return ""
    if lo <= 0 and hi <= 0:
        return ""
    if lo == hi:
        return f"{int(lo) if lo == int(lo) else lo}元"
    return f"{int(lo) if lo == int(lo) else lo}-{int(hi) if hi == int(hi) else hi}元"


def date_range_str(dates, begin, end):
    if dates:
        ds = sorted(dates)
        return f"{ds[0]} ~ {ds[-1]}" + (f"（共{len(ds)}天）" if len(ds) > 1 else "")
    return f"{begin} ~ {end}" if begin or end else "详见购票页"


def monitor(cfg, state):
    today = now_bj()
    products, failed_months = collect_products(cfg)

    # 网站异常 -> 告警（带冷却）
    if failed_months:
        maybe_alert(state, f"演出日历 {','.join(failed_months)} 抓取失败")

    edge_month = month_range(cfg["months_ahead"])[-1]
    edge_first_poll = state["edge_month_init"].get(edge_month) is None
    bootstrap = bool(state.get("bootstrap"))
    events = []
    detail_fail = 0
    detail_total = 0

    for pid, p in sorted(products.items()):
        prev = state["products"].get(pid)
        dates = sorted(p["dates"])
        drange = date_range_str(p["dates"], p["begin"], p["end"])

        # ---------- 新演出 ----------
        if prev is None:
            detail_total += 1
            try:
                d = fetch_detail(pid)
                status = d.get("productSaleStatus") or ""
            except Exception as e:  # noqa: BLE001
                print(f"[ERR] 详情抓取失败 {pid}: {e}")
                detail_fail += 1
                continue

            rec = {"status": status, "name": p["name"], "vname": p["vname"],
                   "bucket": p["bucket"], "dates": dates, "first_seen": today.strftime("%Y-%m-%d %H:%M")}
            state["products"][pid] = rec

            if bootstrap:
                continue  # 首启静默建档

            # 首次进入监控窗口的最外层月份，静默收编（多为早已开票的老演出）
            if edge_first_poll and all(d.startswith(edge_month) for d in dates):
                continue

            if status == STATUS_ONSALE:
                e = mk_event(pid, p, drange, "new_on_sale")
                events.append(e); log_week_event(state, e)
            elif status == STATUS_UPCOMING and cfg["push_upcoming"]:
                e = mk_event(pid, p, drange, "upcoming")
                events.append(e); log_week_event(state, e)
            continue

        # ---------- 已知演出 ----------
        added = sorted(set(dates) - set(prev.get("dates") or []))
        if added and prev.get("status") == STATUS_ONSALE:
            prev["dates"] = dates
            e = mk_event(pid, p, drange, "session_added", added=added)
            events.append(e); log_week_event(state, e)
        elif added:
            prev["dates"] = dates

        if prev.get("status") == STATUS_ONSALE or prev.get("status") == STATUS_ENDED:
            continue  # 无需复查

        # 即将开票 -> 周期性复查状态
        if UPCOMING_RECHECK_EVERY_MIN > 0:
            last = state["upcoming_checked"].get(pid, 0)
            if time.time() - last < UPCOMING_RECHECK_EVERY_MIN * 60:
                continue
        state["upcoming_checked"][pid] = time.time()

        detail_total += 1
        try:
            d = fetch_detail(pid)
            status = d.get("productSaleStatus") or ""
        except Exception as e:  # noqa: BLE001
            print(f"[ERR] 详情抓取失败 {pid}: {e}")
            detail_fail += 1
            continue

        if status != prev.get("status"):
            prev["status"] = status
            if status == STATUS_ONSALE:
                e = mk_event(pid, p, drange, "on_sale")
                events.append(e); log_week_event(state, e)
            elif status == STATUS_ENDED:
                state["upcoming_checked"].pop(pid, None)

    # 商品详情接口大面积异常也告警
    if detail_total and detail_fail == detail_total:
        maybe_alert(state, "票务详情接口全部请求失败")

    # ---- 中央民族乐团·民族音乐厅：开票公告文章 ----
    if cfg["venues"].get("cnto", True):
        try:
            articles = fetch_cnto_opens()
            state["cnto_fail"] = 0
        except Exception as e:  # noqa: BLE001
            print(f"[ERR] 民族音乐厅列表抓取失败: {e}")
            streak = state.get("cnto_fail", 0) + 1
            state["cnto_fail"] = streak
            if streak >= 2:  # 连续2次失败才告警，瞬时抖动静默重试
                maybe_alert(state, f"中央民族乐团演出预告连续{streak}次抓取失败: {e}")
            articles = None
        if articles is not None:
            cnto_first = state.get("cnto_init") is None  # 首次接入静默收录
            for a in articles:
                if a["pid"] in state["products"]:
                    continue
                state["products"][a["pid"]] = {
                    "status": "已发布", "name": a["title"], "vname": CNTO_VENUE,
                    "bucket": "cnto", "dates": [], "url": a["url"],
                    "first_seen": today.strftime("%Y-%m-%d %H:%M"),
                }
                if bootstrap or cnto_first:
                    continue
                e = {"type": "new_on_sale", "pid": a["pid"], "name": a["title"],
                     "vname": CNTO_VENUE, "date_range": "详见公告",
                     "url": a["url"]}
                events.append(e); log_week_event(state, e)
            state["cnto_init"] = today.strftime("%Y-%m-%d")
            if cnto_first:
                print(f"[OK] 民族音乐厅首次收录 {len(articles)} 条开票公告（静默）")

    # ---- 首启收尾 ----
    if bootstrap:
        state["bootstrap"] = False
        state["edge_month_init"][edge_month] = today.strftime("%Y-%m-%d")
    elif edge_first_poll:
        state["edge_month_init"][edge_month] = today.strftime("%Y-%m-%d")

    prune(state)

    # ---- 推送 ----
    if events:
        n = len(events)
        title = f"开票提醒：{n} 场演出开票" if n > 1 else f"开票提醒：{events[0]['name']}"
        push(title, build_message(events))
        print(f"[OK] 推送 {n} 个事件")
    else:
        print("[OK] 无新开票")

    return len(events), failed_months


def mk_event(pid, p, drange, etype, added=None):
    e = {"type": etype, "pid": pid, "name": p["name"], "vname": p["vname"],
         "date_range": drange, "playtime": p.get("playtime", ""),
         "price": p.get("price", "")}
    if added:
        e["added"] = added
    return e


def prune(state):
    """清理：已结束超过60天的演出移出状态；周报日志保留最近14天"""
    cutoff = (now_bj() - dt.timedelta(days=60)).strftime("%Y-%m-%d")
    cutoff_cnto = (now_bj() - dt.timedelta(days=120)).strftime("%Y-%m-%d")
    for pid in list(state["products"]):
        rec = state["products"][pid]
        if rec.get("bucket") == "cnto":
            if (rec.get("first_seen") or "") < cutoff_cnto:
                del state["products"][pid]  # 旧公告清理
            continue
        dates = rec.get("dates") or []
        last_date = max(dates) if dates else (rec.get("first_seen") or "")
        if rec.get("status") == STATUS_ENDED and last_date < cutoff:
            del state["products"][pid]
    if state["upcoming_checked"]:
        keep = set(state["products"])
        for pid in list(state["upcoming_checked"]):
            if pid not in keep:
                del state["upcoming_checked"][pid]
    lim = (now_bj() - dt.timedelta(days=WEEK_LOG_DAYS)).strftime("%Y-%m-%d")
    state["week_log"] = [x for x in state["week_log"] if x["ts"] >= lim]


# ----------------------------------------------------------------------------
# 周报
# ----------------------------------------------------------------------------
def weekly(cfg, state):
    today = now_bj()
    start = (today - dt.timedelta(days=7)).strftime("%m-%d")
    end = today.strftime("%m-%d")
    lim = (today - dt.timedelta(days=7)).strftime("%Y-%m-%d")
    logs = [x for x in state.get("week_log", []) if x["ts"] >= lim]
    tag = {"new_on_sale": "新开票", "on_sale": "正式开票",
           "session_added": "加场", "upcoming": "官宣待开票"}

    lines = [f"## 📊 开票监控周报（{start} ~ {end}）\n"]
    if logs:
        lines.append(f"本周共 {len(logs)} 条开票动态：\n")
        for x in logs:
            lines.append(f"- `{x['ts'][5:16]}` [{tag.get(x['type'], x['type'])}] "
                         f"**{x['name']}**（{x['vname']}，{x['dates']}）")
    else:
        lines.append("本周无新开票/加场。")

    # 监控健康度：优先用 Actions 注入的运行统计
    total, ok = os.getenv("STATS_TOTAL", ""), os.getenv("STATS_OK", "")
    last_ok = os.getenv("LAST_OK", "")
    n_on = sum(1 for p in state["products"].values() if p.get("status") == STATUS_ONSALE)
    n_up = sum(1 for p in state["products"].values() if p.get("status") == STATUS_UPCOMING)
    lines.append("\n### 监控状态")
    if total:
        ok_n = int(ok) if ok.isdigit() else 0
        health = "✅ 正常" if total and ok_n >= int(total) * 0.9 else "⚠️ 有失败"
        lines.append(f"- 本周轮询 {total} 次，成功 {ok or 0} 次 —— {health}")
    if last_ok:
        lines.append(f"- 最近一次成功检查：{last_ok[:16].replace('T', ' ')}")
    lines.append(f"- 当前跟踪演出：在售 {n_on} 场，待开票 {n_up} 场")

    push("开票监控周报", "\n".join(lines))
    print("[OK] 周报已发送")


# ----------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    cfg = load_config()
    state = load_state()
    if "--weekly" in args:
        weekly(cfg, state)
    else:
        monitor(cfg, state)
    save_state(state)


if __name__ == "__main__":
    main()
