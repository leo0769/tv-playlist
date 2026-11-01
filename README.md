# tv-playlist

自动聚合、探活、去重的中文电视直播源，供 **my-tv-0** 等播放器订阅。

由 GitHub Actions 每天三次（北京时间约 14:17 / 22:17 / 02:17）自动抓取、测活、生成并发布。

## 订阅地址（永久不变）

| 用途 | 地址 |
|---|---|
| **my-tv-0（推荐）** | `https://leo0769.github.io/tv-playlist/channels.json` |
| 通用 m3u 播放器 | `https://leo0769.github.io/tv-playlist/channels.m3u` |
| 构建状态 | `https://leo0769.github.io/tv-playlist/meta.json` |

`channels.json` 是 my-tv-0 原生格式，**同一频道可挂 2~3 条线路**，播放失败时播放器会自动换线。

## 生成方式

```
4 个上游 m3u  ──►  频道名归一化  ──►  同名频道合并多地址
     │
     └─ iptv-api / vbskycn / YueChan / iptv-org
                              │
                    并发探活（HTTP 200 + HLS 清单非空）
                              │
                官方 CDN 优先 ↑ 排序，每台留前 3 条
                              │
                    channels.m3u + channels.json + meta.json
                              │
                     GitHub Pages 发布（链接不变）
```

- **频道名归一化**：`CCTV-1` / `CCTV1` / `CCTV-1高清` / `CCTV-1综合` 归为同一台；`浙江钱江都市` 与 `浙江钱江` 合并。
- **iptv-org 台名是英文**，靠 `scripts/build_playlist.py` 里的 `ORG_ALIAS` 表映射回中文台名，映射不上的直接丢弃。
- 已剔除：景区慢直播（直播中国）、晚会轮播（春晚频道）、纯音频电台、`[Geo-blocked]` 条目。

## 手动刷新

仓库 Actions 页 → **Update playlist** → **Run workflow**。
注意 GitHub Pages 有约 10 分钟 CDN 缓存，发布后不会立刻生效。

## 声明

本项目只做公开直播源的聚合与测活，**不存储、不转播任何流媒体内容**，源均来自第三方公开仓库。
链接时效性由上游决定，可能随时失效；仅供个人学习研究使用，请勿用于商业用途。
