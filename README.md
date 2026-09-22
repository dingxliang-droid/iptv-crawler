# iptv-crawler

自动发现、测速并导出 **udpxy 组播源** 的无头(Headless)工具，Docker 一键部署。
定时从 FOFA 发现电信网络中的 udpxy 服务，用真实拉流验证可用性，自动生成可直接订阅的 m3u 播放列表。

## 功能特性

- 🔍 **FOFA 智能查询**：核心8 ASN 全端口首轮 + 按实测命中率排序的优质端口切片轮，50秒限流间隔自适应等待，空结果自动重试，裸查询诊断 cookie 状态
- ✅ **真实拉流验活**：udpxy status 页 → CCTV1 实测拉流 → udp/rtp 双协议互切 → 6秒不出流再12秒慢启动救回 → 2央视+2卫视+1其他多样本复测防误判
- 🏷️ **三级状态**：可播（能拉流）/ 在线（服务活着但拉不出）/ 失效
- ⚠️ **坟墓机制**：反复死亡2次自动入坟30天，不再搜索不再检测，到期自动复活
- ⚀ **智能调度**：运行先复测旧IP，≥`skip_if_alive`个可播则跳过FOFA不烧额度；爬取凑够`target`个提前收工
- 📺 **导出 m3u**：按测速取最快的前 N 个可播IP，**按频道逐个轮换**分配（不依赖模板分组结构），负载均匀、单IP故障只影响部分频道
- 📊 **可观测**：每次运行输出 `status.json` 摘要 + 滚动日志，浏览器即可验证下载

## 架构

```
┌────────────────┐   定时生成m3u    ┌─────────────┐
│  iptv-crawler  │ ───────────────► │  data/output │
│ (本仓库: 爬取+  │                  └──────┬───────┘
│  测速+导出m3u)  │                         │ 共享卷
└────────────────┘                  ┌──────▼───────┐
                                    │  m3u-server  │
                                    │  (nginx静态) │──► http://nas:8899/xx.m3u
                                    └──────────────┘   供播放器/聚合器订阅
```

## 快速开始

### 1. 准备数据目录

```bash
mkdir -p data/templates data/output data/logs
cp config.json data/
cp .env.example data/.env    # 然后编辑 data/.env 填入你的 FOFA cookie
```

`.env` 格式：
```
FOFA_COOKIE=fofa_token=xxxx;...      # 浏览器登录 fofa.info 后 F12 复制 Cookie
FOFA_USER_AGENT=Mozilla/5.0 ...
```

频道模板：往 `data/templates/` 放 txt 文件，文件名含省份拼音（如 `template_Shanghai.txt`），格式：

```
组名,#genre#
频道名,http://任意占位/udp/239.x.x.x:xxxx
```

模板中的 IP 会被自动替换为实测可播 IP（并按各 IP 生效的 udp/rtp 协议改写）。

### 2. 启动

```bash
docker compose up -d --build
```

首次启动立即执行一次（约3~25分钟），之后按 cron 每5天凌晨4点自动执行。重启容器 = 立即补跑一次。

### 3. 订阅

浏览器打开 `http://群晖IP:8899/` 可看到生成的 m3u 文件。把对应地址添加到你的播放器/IPTV管理器：

```
http://nas-ip:8899/上海.m3u
http://nas-ip:8899/湖南.m3u
```

## 配置说明（data/config.json）

| 字段 | 默认 | 含义 |
|---|---|---|
| `provinces` | `["上海","湖南","安徽"]` | 要处理的省份 |
| `target_alive_per_province` | `5` | 每省凑够多少个可播IP即提前收工 |
| `skip_if_alive` | `2` | 复测后仍有这么多个可播 → 跳过FOFA爬取 |
| `rounds` | `4` | 不足目标时 FOFA 最多爬几轮 |
| `telecom_only` | `true` | 仅查电信 ASN |

凑不满目标不担心：导出有几个算几个，按速度取前 `target` 个轮换。

## 验证与排障

```bash
# 实时日志
tail -f data/logs/crawl.log
# 结果摘要（每省可播数/频道数/上次运行时间）
cat data/output/status.json
# 容器内定时任务确认
docker exec iptv-crawler cat /etc/cron.d/iptv-cron
```

常见报错对照：
- `缺少 FOFA_COOKIE` → 检查 `data/.env` 是否存在、cookie 是否填了真实值
- `⚠ FOFA可能已限流/额度耗尽` → FOFA 免费额度用完，等下月或升级
- `连裸查询'udpxy'都无结果` → cookie 失效，重新从浏览器复制

## 免责声明

本项目仅供**个人学习与技术研究**使用：

- 使用者需自行准备 FOFA 账号，并遵守 [FOFA](https://fofa.info) 的服务条款（免费额度内使用）；
- 程序对发现的 udpxy 服务仅做**轻量连通性验证**（拉流数秒即断开），请勿改造为高强度扫描工具；
- 请勿将本项目用于任何商业用途；生成的播放列表仅供个人使用，禁止公开再分发；
- 因使用本项目产生的任何后果，由使用者自行承担。

## License

MIT（见 LICENSE 文件）
