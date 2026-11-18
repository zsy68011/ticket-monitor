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
     「演出预告」列表页全部新文章：【开票】前缀→新开票；其他→乐团动态（含其他场馆演出官宣）

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
import subprocess
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

BJYT_API = "https://www.bjconcerthall.cn/yjzd-webapp/api/project/list"
BJYT_VENUE = "北京音乐厅"
BJYT_BUY_URL = "https://www.bjconcerthall.cn/bjyyt/ycgp/ycgp.shtml"

ZSYT_URL = "http://www.zsyyt.org/text_list.asp"
ZSYT_BASE = "http://www.zsyyt.org/"
ZSYT_VENUE = "中山公园音乐堂"

QPT_URL = "https://www.qianpiaotong.cn/mobile/ProductList.shtml"
QPT_LIST_URL = "https://www.qianpiaotong.cn/mobile/ProductList.shtml"
QPT_BASE = "https://www.qianpiaotong.cn"
GTYS_VENUE = "国图艺术中心"

CLCN_API = "https://bjreading.clcn.net.cn/cmss/apiweb/activity"
CLCN_DETAIL = "https://www.clcn.net.cn/event/activity/detail?event_id="
CLCN_VENUE = "首都图书馆"

XSD_URL = "https://xsd.bjchy.gov.cn/"
XSD_VENUE = "北京朝阳·宣传文化中心"

# 演出类活动关键词（用于首图/朝阳文章源的过滤）
PERF_KW = ("音乐会", "演出", "戏曲", "演奏", "音乐", "剧场", "歌剧", "话剧",
           "相声", "曲艺", "杂技", "舞剧", "京剧", "昆曲", "演唱会", "抢票")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 30
TZ = dt.timezone(dt.timedelta(hours=8))  # Asia/Shanghai

STATUS_ONSALE = "售卖中"
STATUS_UPCOMING = "即将开票"
STATUS_ENDED = "已结束"

WPOS_ID = "北京艺术中心"
WPOS_ID_TAIHU = "台湖"

ALERT_FAIL_STREAK = 5          # 连续5次失败才告警（失败期间数据不丢，恢复后自动补齐）
ALERT_COOLDOWN_HOURS = 20      # 告警冷却：同一故障每天最多提醒1次
WEEK_LOG_DAYS = 14             # 周报事件日志保留天数
UPCOMING_RECHECK_EVERY_MIN = 0  # >0 时限制对「即将开票」商品的复查频率（分钟）

URGENT_TYPES = ("new_on_sale", "on_sale", "session_added")  # 开票类即时推送
DAILY_PUSH_LIMIT = 5        # Server酱免费版每日5条，超出会发送失败
ANN_FLUSH_MINUTE = 15       # 公告类每小时首个轮询槽（:07）合并冲刷

DEFAULT_CONFIG = {
    "months_ahead": 2,        # 监控当月起共 3 个月（current + 2）
    "push_upcoming": True,    # 新演出官宣为「即将开票」时也推送（用户要提前选演出）
    "venues": {"ncpa": True, "bpac": True, "taihu": False, "cnto": True,
               "bjyt": True, "zsyt": True, "gtys": True, "clcn": True,
               "xsd": True},
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
    """抓中央民族乐团「演出预告」列表页，返回全部文章
    kind: open=【开票】公告 / dyn=演出预告动态（含其他场馆演出官宣）"""
    r = http_get(CNTO_LIST_URL)
    if r.encoding is None or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = "utf-8"  # 服务器未声明字符集，页面实为 UTF-8
    items, seen = [], set()
    for m in re.finditer(
            r'<a[^>]+href="(/ccno/ycyg/[^"]+\.shtml)"[^>]*>([^<]+)</a>', r.text):
        path, title = m.group(1).strip(), html.unescape(m.group(2)).strip()
        if path.endswith("yanchuzixun_list.shtml") or path in seen:
            continue
        seen.add(path)
        kind = "open" if (title.startswith("【开票")
                          or title.startswith("【购票")) else "dyn"
        items.append({"pid": f"cnto:{path}", "title": title,
                      "url": CNTO_BASE + path, "kind": kind})
    return items


def fetch_bjyt_products():
    """北京音乐厅在售/待售项目列表（官方API）"""
    url = (BJYT_API + "?page=1&pageSize=100&firstClassId=&secondClassId"
           "&yyyyMM=&date=&projectName=")
    r = http_get(url)
    data = r.json().get("data") or {}
    out = []
    for it in data.get("dataList") or []:
        rounds = json.loads(it.get("projectRound") or "[]")
        dates = [x.get("eventName", "").split(" ")[0] for x in rounds]
        vname = (rounds[0].get("venueName") if rounds else "") or BJYT_VENUE
        if "北京音乐厅" in vname:
            vname = BJYT_VENUE  # API返回"北京音乐厅1.0"，统一名称
        prices = [p.get("priceName") for p in (rounds[0].get("priceList") or [])
                  if p.get("priceName")]
        out.append({
            "pid": f"bjyt:{it.get('projectId')}",
            "name": (it.get("projectName") or "").strip(),
            "status": STATUS_ONSALE if it.get("projectSaleState") == 2 else STATUS_UPCOMING,
            "vname": vname,
            "dates": [d for d in dates if d.startswith("2")],
            "price": "/".join(prices),
            "buy_url": BJYT_BUY_URL,
        })
    return out


def fetch_zsyt_products():
    """中山公园音乐堂演出列表（官方授权订票站 zsyyt.org，GBK编码）"""
    r = http_get(ZSYT_URL)
    r.encoding = "gbk"
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S)
    out = []
    for row in rows:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 4:
            continue
        name_html = tds[0]
        name = re.sub(r"<[^>]+>|&nbsp;|\s+", "", name_html)
        m = re.search(r"pic_detail\.asp\?id=(\d+)", name_html)
        if not (name and m):
            continue
        venue = re.sub(r"<[^>]+>|\s+", "", tds[2])
        if "中山" not in venue:
            continue
        raw_dates = re.sub(r"<[^>]+>|\s+", "", tds[1])
        dates = sorted({x.replace(".", "-") for x in
                        re.findall(r"\d{4}\.\d{2}\.\d{2}", raw_dates)})
        prices = html.unescape(re.sub(r"<[^>]+>", "/", tds[3]))
        prices = re.sub(r"\s+", "/", prices)
        prices = re.sub(r"/{2,}", "/", prices).strip("/")
        onsale = "订票" in re.sub(r"<[^>]+>|\s+", "", tds[-1])
        out.append({
            "pid": f"zsyt:{m.group(1)}",
            "name": name,
            "status": STATUS_ONSALE if onsale else STATUS_UPCOMING,
            "vname": ZSYT_VENUE,
            "dates": dates,
            "price": prices,
            "buy_url": ZSYT_BASE + f"pic_detail.asp?id={m.group(1)}",
        })
    return out


def fetch_gtys_products():
    """国图艺术中心演出列表（官网gtyszx.com屏蔽海外IP，改用购票通代理渠道）。
    该站对python-requests的请求会挂起，改用curl子进程。"""
    jar = "/tmp/.qpt_cookies"
    def _curl(url, post=False, page=1):
        cmd = ["curl", "-s", "--compressed", "--max-time", str(TIMEOUT),
               "-A", UA, "-c", jar, "-b", jar, url]
        if post:
            cmd = ["curl", "-s", "--compressed", "--max-time", str(TIMEOUT),
                   "-A", UA, "-c", jar, "-b", jar,
                   "-H", "X-Requested-With: XMLHttpRequest",
                   "-H", f"Referer: {QPT_LIST_URL}",
                   "--data-urlencode", "kw=国图",
                   "-d", f"p_typeID=&datetype=&sorttype=&start=&end=&pageindex={page}",
                   url]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=TIMEOUT + 10, check=True)
        return p.stdout

    _curl(QPT_LIST_URL)  # 预热会话
    out, seen = [], set()
    for page in range(1, 9):
        try:
            text = _curl(QPT_URL, post=True, page=page)
        except Exception:  # noqa: BLE001  翻页瞬断：保留已抓到的条目
            if out:
                break
            raise
        lis = re.findall(r"<li>(.*?)</li>", text, re.S)
        if not lis:
            break
        for li in lis:
            m = re.search(r'href="(/mobile/Product/(\d+)\.shtml)"', li)
            if not m:
                continue
            gid = m.group(2)
            if gid in seen:
                continue
            seen.add(gid)
            title = re.search(r'<div class="title">([^<]+)</div>', li)
            desc = re.findall(r"<p>\s*([^<]+?)\s*</p>", li)
            tag = re.search(r'<div class="tag[^"]*">([^<]+)</div>', li)
            price = re.search(r'<div class="price"[^>]*>(.*?)</div>', li, re.S)
            if not (title and desc):
                continue
            venue = re.sub(r"<[^>]+>|\s+", "", desc[1]) if len(desc) > 1 else ""
            if "国图" not in venue:
                continue
            t = tag.group(1).strip() if tag else ""
            if any(w in t for w in ("售罄", "结束", "停售")):
                continue
            status = STATUS_ONSALE if any(
                w in t for w in ("热销", "在售", "售票", "预售", "销售")) else STATUS_UPCOMING
            dates = sorted({x.replace(".", "-") for x in
                            re.findall(r"\d{4}\.\d{2}\.\d{2}", desc[0])})
            prices = re.sub(r"<[^>]+>|\s+", "", price.group(1)) if price else ""
            out.append({
                "pid": f"gtys:{gid}",
                "name": title.group(1).strip(),
                "status": status,
                "vname": venue,
                "dates": dates,
                "price": prices,
                "buy_url": QPT_BASE + m.group(1),
            })
    if not out:
        raise RuntimeError("购票通列表为空或结构变更")
    return out


def fetch_clcn_articles():
    """首都图书馆活动列表（含剧场公益演出，POST表单接口）。
    该站会对GitHub Actions的IP段间歇性限流，失败自动重试一次"""
    last_err = None
    r = items = None
    for attempt in range(2):
        try:
            r = requests.post(
                CLCN_API,
                data={"name": "", "startTime": "", "endTime": "",
                      "channelId": "", "channelChildId": "",
                      "activity_status": "", "limit": "50", "page": "1"},
                headers={"User-Agent": UA,
                         "Referer": "https://www.clcn.net.cn/event",
                         "X-Requested-With": "XMLHttpRequest"},
                timeout=TIMEOUT)
            items = r.json().get("data") or []
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(3)
    if items is None:
        raise RuntimeError(f"clcn API重试后仍失败: {last_err}")
    out = []
    for it in items:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        is_perf = any(w in name for w in PERF_KW)
        note = ""
        if it.get("start_time"):
            note = f"活动时间：{it['start_time']}"
        if it.get("appointment_start"):
            note += f"｜预约开始：{it['appointment_start']}"
        out.append({
            "pid": f"clcn:{it['id']}",
            "title": name,
            "url": CLCN_DETAIL + str(it["id"]),
            "vname": CLCN_VENUE,
            "kind": "clcn_dyn" if is_perf else None,
            "note": note,
        })
    if not out:
        raise RuntimeError("首图活动列表为空或结构变更")
    return out


def fetch_xsd_articles():
    """北京朝阳·宣传文化中心（xsd.bjchy.gov.cn）首页公告/预约/新闻轮换"""
    r = http_get(XSD_URL)
    out, seen = [], set()
    pat = (r'<a[^>]+href="(?:https://xsd\.bjchy\.gov\.cn)?(/(?:news|subscribe)'
           r'/([0-9A-Fa-f]{32}))"[^>]*>(?:\s*(?:<img[^>]*>)?)*\s*<span[^>]*>'
           r'([^<]{4,80})</span>')
    for m in re.finditer(pat, r.text, re.S):
        path, hsh, title = m.group(1), m.group(2), m.group(3).strip()
        key = hsh
        if key in seen or not title:
            continue
        seen.add(key)
        out.append({
            "pid": f"xsd:{hsh}",
            "title": title,
            "url": "https://xsd.bjchy.gov.cn" + path,
            "vname": XSD_VENUE,
            "kind": "xsd_dyn" if any(w in title for w in PERF_KW) else None,
            "note": "",
        })
    if not out:
        raise RuntimeError("朝阳公告列表为空或结构变更")
    return out


def scan_articles(state, cfg, events, fetcher, bucket, today):
    """通用文章/公告源扫描（首图/朝阳等），kind非空的新文章才推送"""
    init_key = f"{bucket}_init"
    try:
        items = fetcher()
        state[f"{bucket}_fail"] = 0
    except Exception as e:  # noqa: BLE001
        streak = state.get(f"{bucket}_fail", 0) + 1
        state[f"{bucket}_fail"] = streak
        print(f"[ERR] {bucket} 抓取失败: {e}")
        if streak >= ALERT_FAIL_STREAK:
            maybe_alert(state, f"{bucket} 连续{streak}次抓取失败: {e}")
        return
    first = state.get(init_key) is None
    n_new = 0
    for a in items:
        pid = a["pid"]
        if pid in state["products"]:
            continue
        n_new += 1
        state["products"][pid] = {
            "status": "已发布", "name": a["title"], "vname": a["vname"],
            "bucket": bucket, "dates": [], "url": a["url"], "kind": a["kind"],
            "first_seen": today.strftime("%Y-%m-%d %H:%M"),
        }
        if first or not a["kind"]:
            continue
        e = {"type": a["kind"], "pid": pid, "name": a["title"],
             "vname": a["vname"], "date_range": "详见公告",
             "url": a["url"], "link_label": "查看详情"}
        if a.get("note"):
            e["note"] = a["note"]
        events.append(e); log_week_event(state, e)
    state[init_key] = today.strftime("%Y-%m-%d")
    if first:
        print(f"[OK] {bucket} 首次收录 {n_new} 条（静默）")


def scan_ext_products(state, cfg, events, fetcher, bucket, today):
    """通用外部源扫描：北京音乐厅/中山音乐堂等产品列表"""
    init_key = f"{bucket}_init"
    try:
        items = fetcher()
        state[f"{bucket}_fail"] = 0
    except Exception as e:  # noqa: BLE001
        streak = state.get(f"{bucket}_fail", 0) + 1
        state[f"{bucket}_fail"] = streak
        print(f"[ERR] {bucket} 抓取失败: {e}")
        if streak >= ALERT_FAIL_STREAK:  # 连续5次失败才告警
            maybe_alert(state, f"{bucket} 连续{streak}次抓取失败: {e}")
        return

    first = state.get(init_key) is None  # 首次接入静默收录
    n_new = 0
    for p in items:
        pid = p["pid"]
        prev = state["products"].get(pid)
        if prev is None:
            n_new += 1
            state["products"][pid] = {
                "status": p["status"], "name": p["name"], "vname": p["vname"],
                "bucket": bucket, "dates": p["dates"], "price": p["price"],
                "buy_url": p["buy_url"],
                "first_seen": today.strftime("%Y-%m-%d %H:%M"),
            }
            if first:
                continue
            if p["status"] == STATUS_ONSALE:
                e = {"type": "new_on_sale", "pid": pid, "name": p["name"],
                     "vname": p["vname"],
                     "date_range": date_range_str(p["dates"], "", ""),
                     "price": p["price"], "buy_url": p["buy_url"]}
                events.append(e); log_week_event(state, e)
            elif p["status"] == STATUS_UPCOMING and cfg["push_upcoming"]:
                e = {"type": "upcoming", "pid": pid, "name": p["name"],
                     "vname": p["vname"],
                     "date_range": date_range_str(p["dates"], "", ""),
                     "price": p["price"], "buy_url": p["buy_url"]}
                events.append(e); log_week_event(state, e)
            continue
        # 已知演出：状态变化 / 加场
        added = sorted(set(p["dates"]) - set(prev.get("dates") or []))
        if added and prev.get("status") == STATUS_ONSALE:
            prev["dates"] = p["dates"]
            e = {"type": "session_added", "pid": pid, "name": p["name"],
                 "vname": p["vname"],
                 "date_range": date_range_str(p["dates"], "", ""),
                 "price": p["price"], "buy_url": p["buy_url"],
                 "added": added}
            events.append(e); log_week_event(state, e)
        elif added:
            prev["dates"] = p["dates"]
        if p["status"] != prev.get("status"):
            old = prev.get("status")
            prev["status"] = p["status"]
            if p["status"] == STATUS_ONSALE and old not in (None, STATUS_ENDED):
                e = {"type": "on_sale", "pid": pid, "name": p["name"],
                     "vname": p["vname"],
                     "date_range": date_range_str(p["dates"], "", ""),
                     "price": p["price"], "buy_url": p["buy_url"]}
                events.append(e); log_week_event(state, e)
    state[init_key] = today.strftime("%Y-%m-%d")
    if first:
        print(f"[OK] {bucket} 首次收录 {n_new} 条（静默）")


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
    """故障告警，带冷却时间；告警不计预算限制（监控健康优先）"""
    now = time.time()
    if now - state.get("last_alert_ts", 0) < ALERT_COOLDOWN_HOURS * 3600:
        print(f"[ALERT-SUPPRESSED] {reason}")
        return False
    state["last_alert_ts"] = now
    ts = now_bj().strftime("%m-%d %H:%M")
    try_send(state, now_bj(), "开票监控出问题了，请检查",
             f"**{ts}** 轮询异常：{reason}\n\n"
             f"下次轮询会自动重试；若持续异常请到仓库 Actions 页查看日志。",
             force=True)
    return True


def _push_budget(state, today):
    """当日推送预算（Server酱免费版每日5条），跨日自动清零"""
    b = state.get("push_daily") or {}
    d = today.strftime("%Y-%m-%d")
    if b.get("date") != d:
        b = {"date": d, "count": 0}
    state["push_daily"] = b
    return b


def try_send(state, today, title, content, reserve=0, force=False):
    """按每日预算推送。reserve=为更紧急消息预留的名额；
    force=告警等关键消息不受预算限制。返回是否成功发送"""
    b = _push_budget(state, today)
    if not force and b["count"] >= DAILY_PUSH_LIMIT - reserve:
        print(f"[SKIP] 今日额度受限（{b['count']}/{DAILY_PUSH_LIMIT}）: {title[:20]}")
        return False
    try:
        push(title, content)
    except Exception as e:  # noqa: BLE001
        print(f"[ERR] 推送失败: {e}")
        return False
    b["count"] += 1
    print(f"[OK] 推送成功（今日 {b['count']}/{DAILY_PUSH_LIMIT}）")
    return True


# ----------------------------------------------------------------------------
# 事件与消息
# ----------------------------------------------------------------------------
def fmt_event(e):
    if e.get("url"):  # 公告/文章类事件（详情见链接）
        lines = [f"**{e['name']}**", f"- 场馆：{e['vname']}"]
        if e.get("note"):
            lines.append(f"- {e['note']}")
        lines.append(f"- [{e.get('link_label', '查看详情')}]({e['url']})")
        return "\n".join(lines)
    lines = [f"**{e['name']}**"]
    lines.append(f"- 场馆：{e['vname']}")
    lines.append(f"- 时间：{e['date_range']}")
    if e.get("playtime"):
        lines.append(f"- 场次：{e['playtime']}")
    if e.get("price"):
        lines.append(f"- 票价：{e['price']}")
    if e.get("buy_url"):
        lines.append(f"- [立即购票]({e['buy_url']})")
    else:
        lines.append(f"- [立即购票](https://wticket.chncpa.org/product.html?id={e['pid']})")
    return "\n".join(lines)


def build_message(events):
    tag = {"new_on_sale": "新开票", "on_sale": "正式开票",
           "session_added": "加场", "upcoming": "官宣待开票",
           "cnto_dyn": "乐团动态", "clcn_dyn": "首图活动", "xsd_dyn": "朝阳文化"}
    parts = ["## 🎫 开票提醒\n"]
    by_type = {}
    for e in events:
        by_type.setdefault(e["type"], []).append(e)
    order = ["new_on_sale", "on_sale", "session_added", "upcoming",
             "cnto_dyn", "clcn_dyn", "xsd_dyn"]
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

    # 网站异常 -> 连续N次失败才告警（期间数据不丢，恢复轮自动补齐）
    if failed_months:
        cal_streak = state.get("cal_fail", 0) + 1
        state["cal_fail"] = cal_streak
        if cal_streak >= ALERT_FAIL_STREAK:
            maybe_alert(state, f"演出日历已连续{cal_streak}轮抓取失败: "
                          f"{','.join(failed_months)}")
    else:
        state["cal_fail"] = 0

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

    # 商品详情接口大面积异常也纳入连续失败计数
    if detail_total and detail_fail == detail_total:
        d_streak = state.get("detail_fail", 0) + 1
        state["detail_fail"] = d_streak
        if d_streak >= ALERT_FAIL_STREAK:
            maybe_alert(state, f"票务详情接口已连续{d_streak}轮全部请求失败")
    else:
        state["detail_fail"] = 0

    # ---- 中央民族乐团·民族音乐厅：开票公告文章 ----
    if cfg["venues"].get("cnto", True):
        try:
            articles = fetch_cnto_opens()
            state["cnto_fail"] = 0
        except Exception as e:  # noqa: BLE001
            print(f"[ERR] 民族音乐厅列表抓取失败: {e}")
            streak = state.get("cnto_fail", 0) + 1
            state["cnto_fail"] = streak
            if streak >= ALERT_FAIL_STREAK:  # 连续5次失败才告警
                maybe_alert(state, f"中央民族乐团演出预告连续{streak}次抓取失败: {e}")
            articles = None
        if articles is not None:
            cnto_first = state.get("cnto_v2") is None  # 扩全量文章后首扫静默收录
            for a in articles:
                if a["pid"] in state["products"]:
                    continue
                state["products"][a["pid"]] = {
                    "status": "已发布", "name": a["title"], "vname": CNTO_VENUE,
                    "bucket": "cnto", "dates": [], "url": a["url"],
                    "kind": a["kind"],
                    "first_seen": today.strftime("%Y-%m-%d %H:%M"),
                }
                if bootstrap or cnto_first:
                    continue
                if a["kind"] == "open":
                    e = {"type": "new_on_sale", "pid": a["pid"], "name": a["title"],
                         "vname": CNTO_VENUE, "date_range": "详见公告",
                         "url": a["url"], "link_label": "查看开票公告"}
                else:  # 演出预告/动态（含其他场馆演出官宣）
                    e = {"type": "cnto_dyn", "pid": a["pid"], "name": a["title"],
                         "vname": CNTO_VENUE, "date_range": "详见公告",
                         "url": a["url"], "link_label": "查看演出详情"}
                events.append(e); log_week_event(state, e)
            state["cnto_init"] = today.strftime("%Y-%m-%d")
            state["cnto_v2"] = today.strftime("%Y-%m-%d")
            if cnto_first:
                print(f"[OK] 民族音乐厅全量文章首次收录 {len(articles)} 条（静默）")

    # ---- 北京音乐厅 / 中山公园音乐堂 / 国图艺术中心 ----
    if cfg["venues"].get("bjyt", True):
        scan_ext_products(state, cfg, events, fetch_bjyt_products, "bjyt", today)
    if cfg["venues"].get("zsyt", True):
        scan_ext_products(state, cfg, events, fetch_zsyt_products, "zsyt", today)
    if cfg["venues"].get("gtys", True):
        scan_ext_products(state, cfg, events, fetch_gtys_products, "gtys", today)
    if cfg["venues"].get("clcn", True):
        scan_articles(state, cfg, events, fetch_clcn_articles, "clcn", today)
    if cfg["venues"].get("xsd", True):
        scan_articles(state, cfg, events, fetch_xsd_articles, "xsd", today)

    # ---- 首启收尾 ----
    if bootstrap:
        state["bootstrap"] = False
        state["edge_month_init"][edge_month] = today.strftime("%Y-%m-%d")
    elif edge_first_poll:
        state["edge_month_init"][edge_month] = today.strftime("%Y-%m-%d")

    prune(state)

    # ---- 推送（预算管理：开票类即时，公告类整点合并，超额排队次日冲正）----
    urgent = [e for e in events if e["type"] in URGENT_TYPES]
    ann = [e for e in events if e["type"] not in URGENT_TYPES]
    ann_q = state.setdefault("ann_queue", [])
    pending = state.setdefault("pending_events", [])
    ann_q.extend(ann)

    prev_date = (state.get("push_daily") or {}).get("date")
    today_str = today.strftime("%Y-%m-%d")
    day_rolled = prev_date is not None and prev_date != today_str

    outbox = []
    if pending and day_rolled and today.hour >= 8:
        outbox.extend(pending)          # 昨日超额积压，上午补发
        pending.clear()
    if ann_q and (today.minute < ANN_FLUSH_MINUTE or len(ann_q) >= 6):
        outbox.extend(ann_q)            # 每小时首个槽位合并公告，省额度
        ann_q.clear()
    outbox.extend(urgent)               # 开票类永远即时

    if outbox:
        n = len(outbox)
        title = (f"开票提醒：{n} 场演出动态" if n > 1
                 else f"开票提醒：{outbox[0]['name']}")
        if try_send(state, today, title, build_message(outbox),
                    reserve=0 if urgent else 1):
            print(f"[OK] 推送 {n} 个事件")
        else:
            pending.extend(outbox)
            print(f"[SKIP] {n} 条事件已排队（额度不足或通道失败）")
    elif ann_q or pending:
        print(f"[OK] 公告合并中（待冲刷 {len(ann_q)} 条，积压 {len(pending)} 条）")
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
        if rec.get("bucket") in ("bjyt", "zsyt", "gtys"):
            # 外部源：项目下架/过期即清理（dates已为ISO格式）
            if dates and last_date < cutoff:
                del state["products"][pid]
            continue
        if rec.get("bucket") in ("clcn", "xsd"):
            # 公告类：按收录时间过期清理
            if (rec.get("first_seen") or "") < cutoff:
                del state["products"][pid]
            continue
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
           "session_added": "加场", "upcoming": "官宣待开票",
           "cnto_dyn": "乐团动态"}

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

    try_send(state, now_bj(), "开票监控周报", "\n".join(lines))
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
