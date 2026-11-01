# -*- coding: utf-8 -*-
"""
电视直播清单构建器（跨平台，跑在 GitHub Actions 或本机均可）

流程：多上游抓取 -> 频道名归一化 -> 同频道合并多地址 -> 并发探活 -> 官方 CDN 优先
产物：
  public/channels.m3u   通用 m3u（老播放器 / 兜底）
  public/channels.json  my-tv-0 原生格式，同一频道多个 uris，播放失败自动换线
  public/meta.json      本次构建的统计与时间戳

用法：
  python build_playlist.py --out-dir public
  python build_playlist.py --out-dir public --sources "url1,url2" --workers 24 --timeout 8
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Linux; Android 9) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36"

# 顺序即优先级：前两个是中文台名的主源，iptv-org 台名是英文，靠下面的别名表对齐
DEFAULT_SOURCES = [
    "https://guovin.github.io/iptv-api/result.m3u",
    "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u",
    "https://raw.githubusercontent.com/YueChan/Live/main/IPTV.m3u",
    "https://iptv-org.github.io/iptv/countries/cn.m3u",
]

# 官方广播机构 CDN：稳定性远高于个人转播 IP，排序时整体前置
OFFICIAL = (
    "cztv.com", "gztv.com", "nntv.cn", "hugd.com", "cnr.cn", "hebyun.com.cn",
    "nbs.cn", "zohi.tv", "wsrtv.com.cn", "xysrmt.cn", "juyun.tv", "myun.tv",
    "ybitv.com", "sztv.com", "hunantv.com", "smg.cn", "kankanews.com",
    "hntv.tv", "sctv.com", "qhtv.com.cn", "gdtv.cn", "btv.org", "jlntv.cn",
    "cnki.net", "cctv.com", "cntv.cn", "migu.cn", "wasu.tv",
)
# 纯音频电台：电视上表现为黑屏有声
AUDIO_ONLY = ("ls.qingting.fm", "lhttp.qingting.fm")
# 垃圾条目
BAD_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}")
BAD_FLAG = re.compile(r"\[(Geo-blocked|Not 24/7|Offline)\]", re.I)

INDEX_TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tv-playlist - 中文电视直播源</title>
<style>
  body{margin:0;padding:48px 24px;font:16px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#fafafa;color:#1a1a1a}
  .wrap{max-width:720px;margin:0 auto}
  h1{font-size:26px;margin:0 0 6px}
  p.sub{color:#666;margin:0 0 26px}
  .stats{display:flex;gap:22px;flex-wrap:wrap;margin:0 0 26px;font-size:14px;color:#444}
  .stats b{font-size:20px;color:#0b6b3a;display:block}
  .card{background:#fff;border:1px solid #e6e6e6;border-radius:12px;padding:16px 18px;margin:0 0 12px}
  .card h2{font-size:14px;margin:0 0 6px;color:#555;font-weight:600}
  code{display:block;word-break:break-all;background:#f2f2f2;border-radius:8px;padding:9px 12px;font-size:13.5px;color:#0b6b3a}
  .meta{font-size:13px;color:#888;margin-top:24px}
  a{color:#0b6b3a}
</style>
</head>
<body>
<div class="wrap">
  <h1>tv-playlist</h1>
  <p class="sub">自动聚合、探活、去重的中文电视直播源</p>
  <div class="stats">
    <div><b>__CHANNELS__</b>个频道</div>
    <div><b>__MULTI__</b>个多线路</div>
    <div><b>__OFFICIAL__</b>个官方 CDN</div>
    <div><b>__ALIVE__/__TESTED__</b>地址存活</div>
  </div>
  <div class="card"><h2>my-tv-0 订阅（推荐，多线路自动回退）</h2>
    <code>https://leo0769.github.io/tv-playlist/channels.json</code></div>
  <div class="card"><h2>通用 m3u</h2>
    <code>https://leo0769.github.io/tv-playlist/channels.m3u</code></div>
  <div class="card"><h2>构建状态</h2>
    <code>https://leo0769.github.io/tv-playlist/meta.json</code></div>
  <div class="card"><h2>EPG（合法空 XMLTV，占位）</h2>
    <code>https://leo0769.github.io/tv-playlist/epg.xml</code></div>
  <p class="meta">
    最后构建：__UPDATED__<br>
    数据来源：iptv-api / vbskycn-iptv / YueChan-Live / iptv-org<br>
    本项目只做公开源的聚合与测活，不存储、不转播任何流媒体内容。
  </p>
</div>
</body>
</html>
"""

# iptv-org 的 tvg-id（去掉 @SD/@HD 后缀）-> 中文台名, 分组
# 只收台名无歧义的主流频道，避免多路回退时串台
ORG_ALIAS = {
    "CCTV1.cn": ("CCTV-1", "央视"), "CCTV2.cn": ("CCTV-2", "央视"),
    "CCTV3.cn": ("CCTV-3", "央视"), "CCTV4.cn": ("CCTV-4", "央视"),
    "CCTV5.cn": ("CCTV-5", "央视"), "CCTV5Plus.cn": ("CCTV-5+", "央视"),
    "CCTV6.cn": ("CCTV-6", "央视"), "CCTV7.cn": ("CCTV-7", "央视"),
    "CCTV8.cn": ("CCTV-8", "央视"), "CCTV9.cn": ("CCTV-9", "央视"),
    "CCTV10.cn": ("CCTV-10", "央视"), "CCTV11.cn": ("CCTV-11", "央视"),
    "CCTV12.cn": ("CCTV-12", "央视"), "CCTV13.cn": ("CCTV-13", "央视"),
    "CCTV14.cn": ("CCTV-14", "央视"), "CCTV15.cn": ("CCTV-15", "央视"),
    "CCTV16.cn": ("CCTV-16", "央视"), "CCTV17.cn": ("CCTV-17", "央视"),
    "CCTV4K.cn": ("CCTV-4K", "央视"), "CCTV8K.cn": ("CCTV-8K", "央视"),
    "CETV1.cn": ("CETV-1", "央视"), "CETV2.cn": ("CETV-2", "央视"),
    "AnhuiTV.cn": ("安徽卫视", "卫视"), "BeijingSatelliteTV.cn": ("北京卫视", "卫视"),
    "DragonTVInternational.cn": ("东方卫视", "卫视"),
    "FujianComprehensiveChannel.cn": ("东南卫视", "卫视"),
    "GansuTV.cn": ("甘肃卫视", "卫视"), "GuangdongSatelliteTV.cn": ("广东卫视", "卫视"),
    "GuangxiTV.cn": ("广西卫视", "卫视"), "GuizhouTV.cn": ("贵州卫视", "卫视"),
    "HebeiTV.cn": ("河北卫视", "卫视"), "HenanTV.cn": ("河南卫视", "卫视"),
    "HeilongjiangTV.cn": ("黑龙江卫视", "卫视"), "HubeiTV.cn": ("湖北卫视", "卫视"),
    "HunanTV.cn": ("湖南卫视", "卫视"), "JiangsuSatelliteTV.cn": ("江苏卫视", "卫视"),
    "JiangxiTV.cn": ("江西卫视", "卫视"), "JilinTV.cn": ("吉林卫视", "卫视"),
    "LiaoningTV.cn": ("辽宁卫视", "卫视"), "NeiMonggolTV.cn": ("内蒙古卫视", "卫视"),
    "NingxiaSatelliteChannel.cn": ("宁夏卫视", "卫视"),
    "QinghaiTV.cn": ("青海卫视", "卫视"), "ShandongSatelliteTV.cn": ("山东卫视", "卫视"),
    "ShanxiTV.cn": ("山西卫视", "卫视"), "ShaanxiTV.cn": ("陕西卫视", "卫视"),
    "SichuanSatelliteTV.cn": ("四川卫视", "卫视"),
    "ShenzhenSatelliteTV.cn": ("深圳卫视", "卫视"),
    "TianjinTV.cn": ("天津卫视", "卫视"), "XinjiangTV2.cn": ("新疆卫视", "卫视"),
    "YunnanSatelliteTV.cn": ("云南卫视", "卫视"),
    "ZhejiangSatelliteTV.cn": ("浙江卫视", "卫视"),
    "ChongqingTV.cn": ("重庆卫视", "卫视"), "XizangTVChinese.cn": ("西藏卫视", "卫视"),
    "YanbianSatelliteTV.cn": ("延边卫视", "卫视"), "AnshunTV.cn": ("安顺新闻", "贵州"),
    "QTV1.cn": ("青岛新闻综合", "山东"), "QTV2.cn": ("青岛经济", "山东"),
    "QTV3.cn": ("青岛生活", "山东"), "QTV4.cn": ("青岛影视", "山东"),
    "QTV5.cn": ("青岛体育", "山东"), "QTV6.cn": ("青岛少儿", "山东"),
}

# 同台不同写法：归一化之后再替换，减少重复条目
ALIAS = {
    "浙江钱江都市": "浙江钱江",
    "上海第一财经": "第一财经",
    "涟水电视台综合": "涟水综合",
    "浙江教科": "浙江教育",
    "CCTV5+体育赛事": "CCTV5+",
}

QUALITY_SUFFIX = ("高清", "超清", "标清", "蓝光", "4K", "HD", "hd", "HDTV", "频道")

# 分组名统一：各上游写法不一（📺央视频道 / 央视频道 / 央视），去掉表情后归并
GROUP_MAP = {
    "央视": "央视频道", "CCTV": "央视频道", "央视付费": "央视付费频道",
    "卫视": "卫视频道", "省级卫视": "卫视频道", "数字频道": "数字付费",
    "地方": "地方频道", "体育频道": "体育", "音乐频道": "音乐",
    "山东": "山东频道", "河南": "河南频道", "贵州": "贵州频道",
}
# 不是电视频道的内容：景区慢直播、晚会轮播、境外宗教台
BLOCK_GROUPS = {"直播中国", "春晚频道", "Religious", "Undefined", "其他", "未分类", "未分组", "观众"}


def norm_group(g):
    s = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", g or "")
    if not s:
        return "未分组"
    return GROUP_MAP.get(s, s)


def fetch(url, timeout=40):
    """先直连，失败再走 gh 镜像（只对 raw.githubusercontent 生效）"""
    tries = [url]
    if "raw.githubusercontent.com" in url:
        tries.append("https://ghfast.top/" + url)
        tries.append("https://ghproxy.net/" + url)
    last = None
    for u in tries:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:  # noqa
            last = e
    raise last


def parse_m3u(text):
    """返回 [{name,group,logo,url,tvgid}]"""
    out, info = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            info = line
        elif line.startswith("#"):
            continue
        elif info:
            title = info.rsplit(",", 1)[-1].strip()
            g = re.search(r'group-title="([^"]*)"', info)
            l = re.search(r'tvg-logo="([^"]*)"', info)
            n = re.search(r'tvg-name="([^"]*)"', info)
            i = re.search(r'tvg-id="([^"]*)"', info)
            out.append({
                "name": title or (n.group(1) if n else ""),
                "tvgname": (n.group(1) if n else ""),
                "tvgid": (i.group(1) if i else ""),
                "group": (g.group(1) if g else "").strip(),
                "logo": (l.group(1) if l else "").strip(),
                "url": line,
            })
            info = None
    return out


def clean_name(name):
    s = name.strip()
    s = re.sub(r"\s*[\(\（][^\)）]*[\)）]\s*$", "", s)   # 尾部 (720p)
    s = re.sub(r"\s*\[[^\]]*\]\s*$", "", s)            # 尾部 [Not 24/7]
    return s.strip()


def norm_key(name):
    """频道名归一化：CCTV-1 / CCTV1 / CCTV-1高清 / CCTV-1 综合 -> CCTV1"""
    s = name.strip()
    s = s.replace("电视台", "").replace("广播电视台", "")
    s = re.sub(r"\s*[\(\（][^\)）]*[\)）]\s*$", "", s)
    for suf in QUALITY_SUFFIX:
        if s.endswith(suf) and len(s) > len(suf) + 1:
            s = s[: -len(suf)]
    s = s.replace("－", "-").replace("—", "-").replace(" ", "").replace("　", "")
    s = re.sub(r"(?i)CCTV[\s\-_]*(\d+)\s*\+", r"CCTV\1+", s)
    s = re.sub(r"(?i)CCTV[\s\-_]*(\d+)", r"CCTV\1", s)
    s = re.sub(r"(?i)^CETV[\s\-_]*(\d+)", r"CETV\1", s)
    s = re.sub(r"(CCTV\d+|CETV\d+)[\-_ ]?综合$", r"\1", s)
    s = s.replace("+体育赛事", "+").replace("+体育", "+")
    s = ALIAS.get(s, s)
    return s.upper()


def alive(url, timeout):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Connection": "close"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return False
            head = r.read(4000)
        return b"#EXTM3U" in head or b"#EXTINF" in head or b"#EXT-X-" in head
    except Exception:
        return False


IP_HOST = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def score(url, src_rank):
    """线路打分：官方 CDN > HTTPS > 上游顺序；纯 IP 中转扣分（这类地址寿命最短）"""
    s = 0
    if any(h in url for h in OFFICIAL):
        s += 6
    if url.startswith("https://"):
        s += 2
    host = (urllib.parse.urlparse(url).hostname or "")
    if IP_HOST.match(host):
        s -= 3
    s += max(0, 4 - src_rank)   # 越靠前的上游，其标注越可信
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="public")
    ap.add_argument("--sources", default=os.environ.get("SOURCES", ""))
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--timeout", type=int, default=8)
    ap.add_argument("--max-uris", type=int, default=3, help="同一频道最多保留几个候选地址")
    args = ap.parse_args()

    sources = [s.strip() for s in (args.sources or ",".join(DEFAULT_SOURCES)).split(",") if s.strip()]

    entries, stats = [], {}
    for si, s in enumerate(sources):
        try:
            text = fetch(s)
            got = parse_m3u(text)
            for e in got:
                e["src"] = si
            stats[s] = len(got)
            print("[ok]   %-72s %d 条" % (s, len(got)))
            entries.extend(got)
        except Exception as e:
            stats[s] = -1
            print("[fail] %-72s %s" % (s, e))
    print("合计条目:", len(entries))
    if not entries:
        print("没有拿到任何条目，中止")
        return 2

    chans, order = {}, []
    skipped = 0
    for e in entries:
        raw_name = e["name"] or e["tvgname"]
        if not raw_name:
            skipped += 1
            continue
        if BAD_FLAG.search(raw_name) or BAD_NAME.match(raw_name):
            skipped += 1
            continue
        if any(h in e["url"] for h in AUDIO_ONLY):
            skipped += 1
            continue

        # iptv-org 条目：tvg-id 能在别名表里对上才收，改写成中文台名
        org = ORG_ALIAS.get(e["tvgid"].split("@")[0])
        if e["tvgid"].endswith(".cn") or "@SD" in e["tvgid"] or "@HD" in e["tvgid"]:
            if not org:
                skipped += 1
                continue
            name, group = org
        else:
            name, group = clean_name(raw_name), e["group"]

        group = norm_group(group)
        if group in BLOCK_GROUPS:
            skipped += 1
            continue

        key = norm_key(name)
        if not key:
            skipped += 1
            continue
        if key not in chans:
            chans[key] = {"name": name, "group": group, "logo": e["logo"],
                          "uris": [], "srcmap": {}}
            order.append(key)
        c = chans[key]
        if e["url"] not in c["uris"]:
            c["uris"].append(e["url"])
            c["srcmap"][e["url"]] = e.get("src", 9)
        if not c["logo"] and e["logo"]:
            c["logo"] = e["logo"]
        if (not c["group"] or c["group"] in ("其他", "未分类", "Undefined")) and group:
            c["group"] = group

    all_urls = [u for k in order for u in chans[k]["uris"]]
    print("唯一频道:", len(order), "| 待测地址:", len(all_urls), "| 跳过:", skipped)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        flags = list(ex.map(lambda u: alive(u, args.timeout), all_urls))
    ok = dict(zip(all_urls, flags))
    print("存活:", sum(flags), "/", len(all_urls), "| 用时: %ds" % int(time.time() - t0))

    final, dropped = [], 0
    for k in order:
        c = chans[k]
        live = [u for u in c["uris"] if ok.get(u)]
        if not live:
            dropped += 1
            continue
        live.sort(key=lambda u: -score(u, c["srcmap"].get(u, 9)))
        c["uris"] = live[: args.max_uris]
        c["official"] = any(h in c["uris"][0] for h in OFFICIAL)
        final.append(c)

    def grank(c):
        g = re.sub(r"[^\u4e00-\u9fa5A-Za-z]", "", c["group"])
        if "央视" in g or "CCTV" in g.upper():
            return 0
        if "卫视" in g:
            return 1
        return 2

    final.sort(key=lambda c: (grank(c), 0 if c["official"] else 1, c["group"], c["name"]))

    os.makedirs(args.out_dir, exist_ok=True)

    m3u = ["#EXTM3U"]
    for i, c in enumerate(final, 1):
        m3u.append(
            '#EXTINF:-1 tvg-id="%s" tvg-chno="%d" tvg-name="%s" tvg-logo="%s" group-title="%s",%s'
            % (c["name"], i, c["name"], c["logo"], c["group"], c["name"])
        )
        m3u.append(c["uris"][0])
    with open(os.path.join(args.out_dir, "channels.m3u"), "w", encoding="utf-8") as f:
        f.write("\n".join(m3u) + "\n")

    js = [
        {
            "id": i,
            "group": c["group"] or "未分组",
            "name": c["name"],
            "title": c["name"],
            "logo": c["logo"],
            "number": i,
            "uris": c["uris"],
        }
        for i, c in enumerate(final, 1)
    ]
    with open(os.path.join(args.out_dir, "channels.json"), "w", encoding="utf-8") as f:
        json.dump(js, f, ensure_ascii=False, indent=1)

    meta = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "channels": len(final),
        "multi_uri_channels": sum(1 for c in final if len(c["uris"]) > 1),
        "official_cdn_channels": sum(1 for c in final if c["official"]),
        "urls_tested": len(all_urls),
        "urls_alive": int(sum(flags)),
        "dropped_all_dead": dropped,
        "sources": stats,
    }
    with open(os.path.join(args.out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    # 首页（静态占位页，带本次构建统计）
    html = INDEX_TPL
    for k, v in (
        ("__UPDATED__", meta["updated"].replace("T", " ").replace("Z", " UTC")),
        ("__CHANNELS__", str(meta["channels"])),
        ("__MULTI__", str(meta["multi_uri_channels"])),
        ("__OFFICIAL__", str(meta["official_cdn_channels"])),
        ("__ALIVE__", str(meta["urls_alive"])),
        ("__TESTED__", str(meta["urls_tested"])),
    ):
        html = html.replace(k, v)
    with open(os.path.join(args.out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    print("输出: %d 个频道（官方 CDN %d，多地址 %d，全死丢弃 %d）"
          % (len(final), meta["official_cdn_channels"],
             meta["multi_uri_channels"], dropped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
