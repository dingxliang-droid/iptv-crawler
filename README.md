# 📺 iptv-crawler

![License](https://img.shields.io/badge/License-MIT-green.svg)
![Docker](https://img.shields.io/badge/Docker-一键部署-2496ED?logo=docker&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-NAS·群晖·Linux-999999)

> 自动发现、测速并导出 **udpxy 组播源** 的无头(Headless)工具。
> 定时从 FOFA 发现电信网络中的 udpxy 服务，用**真实拉流**验证可用性，自动生成可直接订阅的 m3u 播放列表。Docker 一键部署，开箱即用。

---

## ✨ 功能特性

| 模块 | 说明 |
| :--- | :--- |
| 🔍 **FOFA 智能查询** | 核心8 ASN 全端口首轮 + 按实测命中率排序的优质端口切片轮；50秒限流间隔自适应等待；空结果自动重试；裸查询 `udpxy` 诊断 cookie 状态 |
| ✅ **真实拉流验活** | udpxy status 页 → CCTV1 实测拉流 → udp/rtp 双协议互切 → 6秒不出流再12秒慢启动救回 → 2央视+2卫视+1其他多样本复测防误判 |
| 🏷️ **三级状态** | `可播`（能拉流）/ `在线`（服务活着但拉不出）/ `失效` |
| ⚠️ **坟墓机制** | 反复死亡2次自动入坟30天 —— 不再搜索、不再检测，到期自动复活 |
| ⏰ **智能调度** | 先复测旧IP，≥ `skip_if_alive` 个可播则**跳过FOFA不烧额度**；爬取凑够 `target` 个提前收工；cron 每5天自动执行 |
| 📺 **导出 m3u** | 按测速取最快的前 N 个可播IP，**按频道逐个轮换**分配（不依赖模板分组结构），负载均匀，单IP故障只影响部分频道 |
| 📊 **可观测** | 每次运行输出 `status.json` 摘要 + 滚动日志；浏览器打开 `:8899` 即可验证下载 |

## 🏗️ 架构

```
┌────────────────┐   定时生成m3u    ┌─────────────┐
│  iptv-crawler  │ ───────────────► │  data/output │
│ (本仓库: 爬取+  │                  └──────┬───────┘
│  测速+导出m3u)  │                         │ 共享卷
└────────────────┘                  ┌──────▼───────┐
                                    │  m3u-server  │
                                    │ (nginx 静态) │──► http://nas-ip:8899/xx.m3u
                                    └──────────────┘    供播放器 / IPTV管理器订阅
```

## 🚀 快速开始

### 1️⃣ 准备数据目录

```bash
mkdir -p data/templates data/output data/logs
cp config.json data/
cp .env.example data/.env
```

### 2️⃣ 填入 FOFA Cookie（关键一步）

`.env` 只需两行，`FOFA_COOKIE` 获取方法：

1. 浏览器打开 [fofa.info](https://fofa.info) 并**登录**你的账号（免费版即可）；
2. 按 `F12` 打开开发者工具，切到 **「网络 / Network」** 标签；
3. 刷新页面（或随便执行一次搜索）；
4. 在请求列表里点任意一条（如 `result`），右侧找到 **「请求标头 / Request Headers」** 里的 `Cookie:`；
5. 复制 **冒号后面** 的整串内容（很长，包含 `fofa_token=...` 等）；
6. 粘贴到 `data/.env`：

```bash
FOFA_COOKIE=fofa_token=xxxxxx;这里=粘贴整串;...
FOFA_USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...
```

> ⚠️ `.env` 含你的账号凭证，**永远不要上传到任何公开仓库**（本仓库 `.gitignore` 已默认排除）。

### 👤 FOFA 账号：免费版 vs 付费会员

| | 免费版 | 付费会员 |
| :--- | :--- | :--- |
| 开箱即用 | ✅ 默认参数即为免费版适配 | ✅ 无需改代码即可用 |
| 查询额度 | 每月有限，3省×4轮够用 | 额度高很多 |
| 限流间隔 | 严格（工具已按 50 秒等待适配） | 宽松，**可加速** |
| 建议调优 | 保持默认 | `main.py` 顶部 **「FOFA 账号适配区」** 注释块：`MIN_QUERY_GAP` 50→5~10；`config.json` 的 `rounds` 4→8 扩大搜索面 |

> 💡 额度状态看运行日志：出现 `⚠ FOFA可能已限流/额度耗尽` 即当月额度用完，等下月重置。

### 3️⃣ 准备频道模板

往 `data/templates/` 放 txt 文件，**文件名包含省份拼音**即可（如 `template_Shanghai.txt`、`template_Hunan.txt`），程序自动模糊匹配。仓库 [`templates/`](templates/) 目录下有示例可直接使用。

模板格式（模板中的 IP 会被自动替换为实测可播IP，并按各IP生效的 udp/rtp 协议改写）：

```
央视,#genre#
CCTV1,http://placeholder/udp/239.1.1.1:8000
CCTV2,http://placeholder/udp/239.1.1.2:8000

卫视,#genre#
湖南卫视,http://placeholder/udp/239.2.1.1:8000
```

### 4️⃣ 启动

```bash
docker compose up -d --build
```

首次启动**立即执行一次**（约 3~25 分钟），之后 cron 每 5 天凌晨 4 点自动执行。
**重启容器 = 立即补跑一次**，随时可手动触发。

### 5️⃣ 订阅

浏览器打开 `http://nas-ip:8899/` 可看到生成的 m3u 文件列表，把地址添加到你的播放器 / IPTV 管理器：

```
http://nas-ip:8899/上海.m3u
http://nas-ip:8899/湖南.m3u
```

## ⚙️ 配置说明（`data/config.json`）

| 字段 | 默认 | 含义 |
| :--- | :--- | :--- |
| `provinces` | `["上海","湖南","安徽"]` | 要处理的省份（需对应拼音的模板文件） |
| `target_alive_per_province` | `5` | 每省凑够多少个可播IP即提前收工 |
| `skip_if_alive` | `2` | 复测后仍有这么多个可播 → 跳过FOFA爬取（省额度） |
| `rounds` | `4` | 不足目标时，FOFA 最多爬几轮 |
| `telecom_only` | `true` | 仅查电信 ASN |

> 凑不满目标不担心：导出**有几个算几个**，按速度取前 `target` 个轮换，≥1 个可播必出 m3u。

## 🔍 验证与排障

```bash
# 实时日志
tail -f data/logs/crawl.log

# 结果摘要（每省可播数 / 频道数 / 上次运行时间）
cat data/output/status.json

# 确认容器内定时任务
docker exec iptv-crawler cat /etc/cron.d/iptv-cron

# 看某省 m3u 的 IP 分布（应显示多个IP各挂一段频道）
grep -o 'http://[0-9.]*:[0-9]*' data/output/上海.m3u | sort | uniq -c
```

| 日志现象 | 原因与处理 |
| :--- | :--- |
| `缺少 FOFA_COOKIE` | 检查 `data/.env` 是否存在、cookie 是否为真实值 |
| `⚠ FOFA可能已限流/额度耗尽` | 免费额度用完，等下月重置或升级会员 |
| `连裸查询'udpxy'都无结果` | cookie 失效，重新从浏览器复制 |

## 🤖 致谢与说明

- 本项目开发过程中使用了 **Kimi（Moonshot AI）** 辅助完成代码改造、调试与文档编写；
- README 文档由 AI 生成后经人工校对；
- 灵感来源于众多开源 IPTV 生态项目。

## ⚠️ 免责声明

本项目仅供**个人学习与技术研究**使用：

- 使用者需自行准备 FOFA 账号，并遵守 [FOFA](https://fofa.info) 的服务条款（免费额度内使用）；
- 程序对发现的 udpxy 服务仅做**轻量连通性验证**（拉流数秒即断开），请勿改造为高强度扫描工具；
- 请勿将本项目用于任何商业用途；生成的播放列表仅供个人使用，禁止公开再分发；
- 因使用本项目产生的任何后果，由使用者自行承担。

## 📄 License

[MIT](LICENSE)
