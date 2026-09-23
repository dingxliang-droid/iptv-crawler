# -*- coding: utf-8 -*-
"""
IPTV 组播源爬取器 (Docker 无头版) v1.0
由 iptv源管理_v3.4.6 GUI 版改造:
  - 去掉 tkinter, 配置由 /data/config.json 驱动
  - 每次运行: 先复测旧IP -> 可播不足目标再爬 FOFA -> 每省凑够 target 个可播即收工
  - 导出每省 m3u 到 /data/output, 运行摘要写入 /data/output/status.json
  - 日志 /data/logs/crawl.log (轮转 3 份 x 5MB), 同时输出到容器标准输出
"""
import os, re, json, time, base64, datetime, shutil, logging, threading, random, hashlib
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from dotenv import load_dotenv

DATA_DIR     = "/data"
CONFIG_FILE  = os.path.join(DATA_DIR, "config.json")
ENV_FILE     = os.path.join(DATA_DIR, ".env")
STATE_FILE   = os.path.join(DATA_DIR, "iptv_state.json")
OUTPUT_DIR   = os.path.join(DATA_DIR, "output")
LOG_DIR      = os.path.join(DATA_DIR, "logs")
TEMPLATE_DIR = os.path.join(DATA_DIR, "templates")

# ---------------- 日志 ----------------
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
logger = logging.getLogger("iptv-crawler")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%m-%d %H:%M:%S")
_fh = RotatingFileHandler(os.path.join(LOG_DIR, "crawl.log"),
                          maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
logger.addHandler(_fh)
logger.addHandler(_sh)
log = logger.info

# ---------------- 配置 ----------------
DEFAULT_CONFIG = {
    "provinces": ["上海", "湖南", "安徽"],   # 要处理的省份
    "target_alive_per_province": 4,          # 每省凑够多少个可播IP即收工(2026-09-23由5改为4)
    "skip_if_alive": 2,                      # 复测后仍有这么多个可播 -> 直接收工不爬FOFA
    "rounds": 4,                             # 不足目标时, FOFA 最多爬几轮
    "telecom_only": True,                    # 仅电信ASN (FOFA查询条件)
}

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            user = json.load(f)
        cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
    except FileNotFoundError:
        logger.warning("未找到 %s, 使用默认配置", CONFIG_FILE)
    except Exception as e:
        logger.warning("配置读取失败(%s), 使用默认配置", e)
    return cfg

if os.path.exists(ENV_FILE):
    load_dotenv(ENV_FILE)
COOKIE = os.getenv("FOFA_COOKIE", "")
UA = os.getenv("FOFA_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")

# ---------------- 省份 / ASN / 端口 ----------------
PROVINCES = {
    "上海": "Shanghai", "湖南": "Hunan", "安徽": "Anhui", "四川": "Sichuan",
    "河南": "Henan", "河北": "Hebei", "北京": "Beijing", "重庆": "Chongqing",
    "天津": "Tianjin", "湖北": "Hubei", "江苏": "Jiangsu", "浙江": "Zhejiang",
    "山东": "Shandong", "广东": "Guangdong", "福建": "Fujian", "江西": "Jiangxi",
    "辽宁": "Liaoning", "吉林": "Jilin", "黑龙江": "Heilongjiang", "山西": "Shanxi",
    "陕西": "Shaanxi", "甘肃": "Gansu", "青海": "Qinghai", "云南": "Yunnan",
    "贵州": "Guizhou", "广西": "Guangxi", "海南": "Hainan", "内蒙古": "Inner Mongolia",
    "宁夏": "Ningxia", "新疆": "Xinjiang", "西藏": "Tibet",
}

BROWSER_HEADERS = {
    "Cookie": COOKIE, "User-Agent": UA,
    "Referer": "https://fofa.info/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

QUOTA_MARKERS = ["查询次数", "数据额度", "已达上限", "额度不足", "开通会员", "升级会员",
                 "剩余次数", "本月已用", "frequency", "rate limit"]

# 中国电信核心ASN (骨干+集团)
CORE_ASNS = ["4134", "4809", "23724", "4811", "4812", "4813", "4816", "4835"]

MIN_QUERY_GAP = 50   # FOFA限流: 相邻查询最小间隔(秒)

UDPXY_PORTS = ["4022", "8888", "4000", "8000", "8881", "9000", "7000", "5555",
               "8899", "8883", "8686", "8088", "10000", "8800", "8118", "6868"]

# ---------------- FOFA ----------------
def _fofa_page(sess, query, page, log_fn=log):
    q64 = base64.b64encode(query.encode()).decode()
    r = sess.get(f"https://fofa.info/result?qbase64={q64}&page={page}",
                 headers=BROWSER_HEADERS, timeout=30)
    if not re.search(r"\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}", r.text):
        hits = [k for k in QUOTA_MARKERS if k in r.text]
        if hits:
            log_fn("⚠ FOFA可能已限流/额度耗尽(检测到: %s), 请到fofa.info确认额度", hits)
    found = [ip for ip in re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})\b", r.text)
             if not ip.startswith(("0.", "127.", "255."))]
    return found

def _query_retry(sess, q, log_fn=log, wait=45):
    found = []
    for attempt in range(2):
        try:
            found = _fofa_page(sess, q, 1, log_fn)
        except Exception as e:
            log_fn("  查询请求失败: %s", e)
            found = []
        if found:
            return found
        if attempt == 0:
            log_fn("  结果为空, 等待%d秒后重试(FOFA限流)...", wait)
            time.sleep(wait)
    return found

def _diagnose(sess, log_fn=log):
    try:
        probe = _fofa_page(sess, "udpxy", 1, log_fn)
        if not probe:
            log_fn("⚠ 连裸查询'udpxy'都无结果: cookie失效或被风控, 请更新 /data/.env")
            return None
        return probe
    except Exception as e:
        log_fn("诊断出错: %s", e)
        return None

# ---------------- 测流 ----------------
def check_alive(ip, timeout=3.5):
    try:
        r = requests.get(f"http://{ip}/status", timeout=timeout)
        return r.status_code == 200 and ("udpxy" in r.text.lower() or "status" in r.text.lower())
    except Exception:
        return False

def probe_stream(ip, suffix, need=150000, t_limit=6):
    try:
        r = requests.get(f"http://{ip}{suffix}", timeout=6, stream=True)
        t0, n = time.time(), 0
        for chunk in r.iter_content(32768):
            n += len(chunk)
            if n >= need or time.time() - t0 > t_limit:
                break
        r.close()
        total_t = time.time() - t0
        if n >= 60000:
            return True, n / max(total_t, 0.1) / 1024, n / 1024
        return False, 0.0, n / 1024
    except Exception:
        return False, 0.0, 0.0

def deep_check(ip, groups, log_fn=log, fast=False, t_limit=None):
    """CCTV1实测拉流, 返回 (可播?, 速度KB/s, 生效路径或None)"""
    if t_limit is not None:
        pkw = {"need": 150000, "t_limit": t_limit}
    elif fast:
        pkw = {"need": 60000, "t_limit": 3}
    else:
        pkw = {}
    cctv1 = None
    for _, chs in groups:
        for name, suffix in chs:
            n = name.replace("-", "").replace(" ", "").lower()
            # cctv1后不能紧跟数字, 防止cctv10/11/12误命中; 带"综合"的跳过
            if re.search(r"cctv1(?!\d)", n) and "综合" not in n:
                cctv1 = (name, suffix); break
        if cctv1:
            break
    if not cctv1:
        for _, chs in groups:
            if chs:
                cctv1 = chs[0]; break
    if not cctv1:
        return False, 0.0, None
    suffix = cctv1[1]
    ok, spd, _ = probe_stream(ip, suffix, **pkw)
    tried = [suffix.split("/")[1]]
    if not ok:
        alt = suffix.replace("/udp/", "/rtp/") if "/udp/" in suffix else suffix.replace("/rtp/", "/udp/")
        if alt != suffix:
            tried.append(alt.split("/")[1])
            ok, spd, _ = probe_stream(ip, alt, **pkw)
            if ok:
                suffix = alt
    way = "+".join(tried)
    log_fn("    CCTV1测试[%s]: %s", way, ("✓ %.0fKB/s" % spd) if ok else "✗ 两条路径都不通")
    return ok, spd, (suffix if ok else None)

def deep_check_multi(ip, groups, log_fn=log, n=5):
    """多样本检测: 2央视+2卫视+1其他, 任一可播即通过"""
    cctvs, weishis, others = [], [], []
    for _, chs in groups:
        for name, suffix in chs:
            nl = name.lower()
            if "cctv" in nl or "cgtn" in nl:
                cctvs.append((name, suffix))
            elif "卫视" in name:
                weishis.append((name, suffix))
            else:
                others.append((name, suffix))
    seed = int(hashlib.md5(ip.encode()).hexdigest()[:8], 16)
    rnd = random.Random(seed)
    picks = []
    for lst, k in ((cctvs, 2), (weishis, 2), (others, 1)):
        lst = list(lst)
        rnd.shuffle(lst)
        picks += lst[:k]
    for name, suffix in picks[:n]:
        ok, spd, path = probe_stream(ip, suffix, t_limit=5)
        way = suffix.split("/")[1]
        if not ok:
            alt = suffix.replace("/udp/", "/rtp/") if "/udp/" in suffix else suffix.replace("/rtp/", "/udp/")
            if alt != suffix:
                ok, spd, path = probe_stream(ip, alt, t_limit=5)
                if ok:
                    way = alt.split("/")[1]
        log_fn("    %s[%s]: %s", name, way, "✓" if ok else "✗")
        if ok:
            return True, spd, path
    return False, 0.0, None

# ---------------- 模板 ----------------
_TPL_CACHE = {}

def parse_template(path):
    """解析频道模板, 返回 [(组名, [(频道名, 后缀), ...]), ...]"""
    raw = open(path, "rb").read()
    text, used = None, ""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            text = raw.decode(enc); used = enc; break
        except Exception:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="ignore"); used = "utf-8(容错)"
    groups, cur, seen = [], "未分组", set()
    total = parsed = dup = skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        total += 1
        if "#genre#" in line:
            cur = line.replace(",#genre#", "").replace("#genre#", "").strip() or "未分组"
            continue
        m = re.search(r"((?:/udp/|/rtp/)\d{1,3}(?:\.\d{1,3}){3}:\d+)", line)
        if not m:
            skipped += 1
            continue
        suffix = m.group(1)
        name = line.split("http")[0].strip().rstrip(",,").strip()
        if not name:
            name = "频道" + str(total)
        key = (name, suffix)
        if key in seen:
            dup += 1
            continue
        seen.add(key)
        g = next((g for g in groups if g[0] == cur), None)
        if not g:
            groups.append((cur, [])); g = groups[-1]
        g[1].append((name, suffix))
        parsed += 1
    log("模板解析[%s]: 共%d行 -> %d频道 (跳过重复%d/无法识别%d)",
        used, total, parsed, dup, skipped)
    return groups

def find_template(pinyin):
    if not os.path.isdir(TEMPLATE_DIR):
        return None
    cands = []
    for root, _, files in os.walk(TEMPLATE_DIR):
        for fn in files:
            if pinyin.lower() in fn.lower() and fn.endswith(".txt"):
                pri = 0 if "telecom" in root.lower() else (1 if "unicom" in root.lower() else 2)
                full = os.path.join(root, fn)
                try:
                    size = os.path.getsize(full)
                except Exception:
                    size = 0
                cands.append((pri, -size, full))
    return sorted(cands)[0][2] if cands else None

def load_template(prov):
    if prov in _TPL_CACHE:
        return _TPL_CACHE[prov]
    p = find_template(PROVINCES[prov])
    if not p:
        logger.error("%s: 模板目录 %s 中找不到文件名含 '%s' 的txt, 该省跳过",
                     prov, TEMPLATE_DIR, PROVINCES[prov])
        _TPL_CACHE[prov] = []
        return []
    groups = parse_template(p)
    _TPL_CACHE[prov] = groups
    log("%s模板: %s (%d频道)", prov, p, sum(len(c) for _, c in groups))
    return groups

# ---------------- 地址改写 / 渲染 ----------------
def _rewrite_url(ip, suffix, okp=None):
    if isinstance(okp, str) and "/" in okp:
        proto = "/" + okp.strip("/").split("/")[0]
        parts = suffix.strip("/").split("/", 1)
        if len(parts) == 2:
            return f"http://{ip}{proto}/{parts[1]}"
    return f"http://{ip}{suffix}"

def _render(fmt, got_groups):
    lines = []
    for gname, got in got_groups:
        if not got:
            continue
        g = gname.replace('"', "'")
        for n, u in got:
            nm = n.replace(",", " ").replace('"', "'")
            lines.append(f'#EXTINF:-1 tvg-id="{nm}" tvg-name="{nm}" group-title="{g}",{nm}')
            lines.append(u)
    return lines

def _count(fmt, lines):
    if fmt == "m3u":
        return sum(1 for l in lines if l.startswith("#EXTINF"))
    return sum(1 for l in lines if l and "#genre#" not in l)

# ---------------- 状态存档 ----------------
_STATE_LOCK = threading.Lock()

def load_state():
    for path in (STATE_FILE, STATE_FILE + ".bak"):
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
    return {}

def save_state(s):
    with _STATE_LOCK:
        try:
            if os.path.exists(STATE_FILE):
                shutil.copy2(STATE_FILE, STATE_FILE + ".bak")
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=1)
            os.replace(tmp, STATE_FILE)
        except Exception:
            pass

# ---------------- IP 判定 ----------------
def classify(ip, groups):
    """新IP判定: status页通->CCTV1拉流->12秒慢启动救回; status不通->快速拉流兜底"""
    if check_alive(ip):
        ok, spd, path = deep_check(ip, groups)
        if ok:
            return "可播", round(spd), path
        log("    %s 6秒未出流, 12秒深度复测...", ip)
        ok2, spd2, path2 = deep_check(ip, groups, t_limit=12)
        if ok2:
            log("    %s 慢启动流已救回", ip)
            return "可播", round(spd2), path2
        return "在线", "-", None
    ok, spd, path = deep_check(ip, groups, fast=True)
    if ok:
        log("  [!] %s status不通但能拉流, 已标可播", ip)
        return "可播", round(spd), path
    return "失效", "-", None

def recheck_one(it, groups, fails):
    """旧IP复测(就地更新it), 返回日志摘要; 可播降级必须多样本复测确认"""
    ip = it["ip"]
    if check_alive(ip):
        ok, spd, okp = deep_check(ip, groups)
        if ok:
            it["status"], it["speed"], it["okpath"] = "可播", round(spd), okp
            fails.pop(ip, None)
            return "★%.0fKB/s" % spd
        log("    单频道未通, 5频道多样本复测...")
        okm, spdm, okpm = deep_check_multi(ip, groups)
        if okm:
            it["status"], it["speed"], it["okpath"] = "可播", round(spdm), okpm
            fails.pop(ip, None)
            return "★%.0fKB/s(多样本)" % spdm
        it["status"] = "失效"
        fails[ip] = fails.get(ip, 0) + 1
        return "✗"
    ok, spd, okp = deep_check(ip, groups, fast=True)
    if ok:
        it["status"], it["speed"], it["okpath"] = "可播", round(spd), okp
        fails.pop(ip, None)
        return "★%.0fKB/s" % spd
    it["status"] = "失效"
    fails[ip] = fails.get(ip, 0) + 1
    return "✗"

# ---------------- 省份处理 ----------------
def run_province(prov, cfg, st):
    log("========== 处理省份: %s ==========", prov)
    groups = load_template(prov)
    if not groups:
        log("  %s: 无有效模板, 整省跳过(请在 %s 放文件名含 '%s' 的频道模板txt)",
            prov, TEMPLATE_DIR, PROVINCES[prov])
        return 0
    prov_state = st.setdefault(prov, {})
    old_list = prov_state.get("ips", [])
    old = {i["ip"]: i for i in old_list}
    fails = st.setdefault("_gy_fails", {})
    gy = st.setdefault("_graveyard", {})

    # ---- 1) 复测旧IP ----
    pool = {}
    if old_list:
        log("  复测旧IP %d 个...", len(old_list))
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(recheck_one, it, groups, fails): it for it in old_list}
            done = 0
            for fut in as_completed(futs):
                it = futs[fut]
                try:
                    summary = fut.result()
                except Exception as e:
                    it["status"] = "失效"
                    fails[it["ip"]] = fails.get(it["ip"], 0) + 1
                    summary = "✗异常:%s" % e
                done += 1
                log("  复测 [%d/%d] %s -> %s %s", done, len(old_list), it["ip"], it["status"], summary)
                if it["status"] != "失效":
                    pool[it["ip"]] = it
    alive = sum(1 for v in pool.values() if v["status"] == "可播")
    target = cfg["target_alive_per_province"]
    skip_min = max(1, min(cfg.get("skip_if_alive", 2), target))   # 复测收工门槛(默认2个)
    log("  复测完成: 存活 %d 个, 可播 %d 个 (目标 %d, 复测收工门槛 %d)",
        len(pool), alive, target, skip_min)

    # ---- 2) 坟墓超30天复活 ----
    today = datetime.date.today()
    revived = []
    for ip0, e in list(gy.items()):
        ent = e if isinstance(e, dict) else {"date": e, "fails": 0}
        gy[ip0] = ent
        try:
            d0 = datetime.date.fromisoformat(str(ent.get("date", "1970-01-01")))
            stale = (today - d0).days > 30
        except Exception:
            stale = True
        if stale:
            revived.append(ip0)
    for ip0 in revived:
        del gy[ip0]
    if revived:
        log("  坟墓复活 %d 个(超30天)", len(revived))

    # ---- 3) 不足目标 -> 爬 FOFA ----
    if alive >= skip_min:
        log("  复测仍有 %d 个可播 (≥%d), 旧IP够用, 跳过 FOFA 爬取", alive, skip_min)
    else:
        zh = PROVINCES[prov]
        sess = requests.Session()
        last_query = 0.0
        diag_fail = 0
        for rd in range(1, cfg["rounds"] + 1):
            alive = sum(1 for v in pool.values() if v and v["status"] == "可播")
            if alive >= target:
                log("  已凑够 %d 个可播, 提前结束", alive)
                break
            port = None if rd == 1 else UDPXY_PORTS[(rd - 2) % len(UDPXY_PORTS)]
            if cfg["telecom_only"]:
                q = 'udpxy && region="%s" && (%s)' % (zh, " || ".join('asn="%s"' % a for a in CORE_ASNS))
            else:
                q = 'udpxy && region="%s"' % zh
            if port:
                q += ' && port="%s"' % port
            gap = MIN_QUERY_GAP - (time.time() - last_query)
            if gap > 0:
                log("  等待 %.0f 秒(FOFA限流间隔)...", gap)
                time.sleep(gap)
            log("--- 第%d/%d轮 (%s) ---", rd, cfg["rounds"], q)
            last_query = time.time()
            found = _query_retry(sess, q, log)
            if not found:
                if _diagnose(sess, log) is None:
                    diag_fail += 1
                    if diag_fail >= 2:
                        log("连续2轮诊断失败, cookie大概率失效, 中止本省份")
                        break
                    log("  诊断未通过(可能限流窗口), 继续下一轮")
                    continue
                diag_fail = 0
                log("  核心条件无结果, 继续下一轮")
                continue
            fresh = []
            for ip in dict.fromkeys(found):
                if ip in gy:
                    continue
                if ip in pool:
                    continue
                pool[ip] = None
                fresh.append(ip)
            log("  本轮新测 %d 个 (坟墓跳过, 池内共 %d)", len(fresh), len(pool))
            if not fresh:
                continue
            with ThreadPoolExecutor(max_workers=10) as ex:
                futs = {ex.submit(classify, ip, groups): ip for ip in fresh}
                dn = 0
                for fut in as_completed(futs):
                    ip = futs[fut]
                    try:
                        status, spd, okpath = fut.result()
                    except Exception as e:
                        status, spd, okpath = "失效", "-", None
                        log("  检测异常 %s: %s", ip, e)
                    if status == "失效":
                        fails[ip] = fails.get(ip, 0) + 1
                    else:
                        fails.pop(ip, None)
                    now = datetime.datetime.now().strftime("%m-%d %H:%M")
                    pool[ip] = {"ip": ip, "status": status, "speed": spd, "checked": now,
                                "note": "旧" if ip in old else "新", "okpath": okpath}
                    dn += 1
                    log("  检测 [%d/%d] %s %s %s", dn, len(fresh), ip, status,
                        ("%sKB/s" % spd) if status == "可播" else "")
            alive = sum(1 for v in pool.values() if v and v["status"] == "可播")
            log("  轮次结束: 可播 %d / 存活 %d", alive, len([v for v in pool.values() if v]))

    # ---- 4) 反复死亡2次的IP: 入坟30天, 不再搜不再测 ----
    for ip0, f in list(fails.items()):
        if f >= 2 and (ip0 not in pool or not pool[ip0] or pool[ip0]["status"] == "失效"):
            gy[ip0] = {"date": today.isoformat(), "fails": f}
            del fails[ip0]
            pool.pop(ip0, None)
            log("  ⛏ %s 反复死亡, 入坟30天", ip0)

    prov_state["ips"] = [v for v in pool.values() if v]
    st[prov] = prov_state
    save_state(st)
    final_alive = sum(1 for v in prov_state["ips"] if v["status"] == "可播")
    log("  %s 本次结果: 可播 %d 个 / 存活 %d 个", prov, final_alive, len(prov_state["ips"]))
    return final_alive

# ---------------- 导出 ----------------
def export_province(prov, st, target=5):
    """每个频道 × 全部可播IP 全组合导出: 同一组播地址依次挂上每个可播IP,
    按速度从快到慢排列, 播放器可自动/手动换下一个IP重试"""
    groups = _TPL_CACHE.get(prov) or load_template(prov)
    ips = st.get(prov, {}).get("ips", [])
    good = [i for i in ips if i["status"] == "可播"]
    if not good:
        log("  %s: 没有可播IP, 不导出", prov)
        return 0
    if not groups:
        log("  %s: 模板未加载, 不导出", prov)
        return 0
    good.sort(key=lambda x: -(x.get("speed") if isinstance(x.get("speed"), (int, float)) else 0))
    # target只是爬取下限(凑够即停), 导出时不截断: 池里全部可播IP都挂上
    n_ch = sum(len(c) for _, c in groups)
    got_groups = []
    for gname, chs in groups:
        got = []
        for name, suffix in chs:
            for info in good:   # 同一频道依次挂全部可播IP, 地址不变
                okp = info.get("okpath") if isinstance(info.get("okpath"), str) else None
                got.append((name, _rewrite_url(info["ip"], suffix, okp)))
        got_groups.append((gname, got))
    lines = _render("m3u", got_groups)
    out = os.path.join(OUTPUT_DIR, f"{prov}.m3u")
    with open(out, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n" + "\n".join(lines) + "\n")
    n = _count("m3u", lines)
    log("  导出 %s: %d 频道 × 全部%d个可播IP = %d 条地址", out, n_ch, len(good), n)
    return n

# ---------------- 主流程 ----------------
def main():
    t0 = time.time()
    log("=" * 56)
    log("IPTV 组播源爬取器(无头版) 开始运行")
    cfg = load_config()
    log("配置: %s", cfg)
    summary = {}
    if not COOKIE:
        logger.error("未找到 FOFA_COOKIE! 请确认 /data/.env 存在且包含 FOFA_COOKIE=...")
        summary["error"] = "缺少 FOFA_COOKIE, 请检查 /data/.env"
    else:
        st = load_state()
        for prov in cfg["provinces"]:
            if prov not in PROVINCES:
                logger.error("未知省份: %s, 跳过", prov)
                continue
            try:
                run_province(prov, cfg, st)
            except Exception:
                logger.exception("%s 处理过程异常:", prov)
                save_state(st)   # 异常时也保住已复测的结果
            st = load_state()
            ips = st.get(prov, {}).get("ips", [])
            n_play = sum(1 for i in ips if i["status"] == "可播")
            n_online = sum(1 for i in ips if i["status"] == "在线")
            n_ch = export_province(prov, st, cfg["target_alive_per_province"])
            summary[prov] = {
                "playable": n_play,
                "online": n_online,
                "m3u": f"{prov}.m3u" if n_play else None,
                "channels": n_ch,
            }
        save_state(st)
    summary["last_run"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary["elapsed_min"] = round((time.time() - t0) / 60, 1)
    with open(os.path.join(OUTPUT_DIR, "status.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    log("全部完成, 用时 %.1f 分钟, 摘要已写入 status.json", summary["elapsed_min"])
    log("=" * 56)

if __name__ == "__main__":
    main()
