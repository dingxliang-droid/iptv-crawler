#!/bin/sh
# 容器启动后立即跑一次(方便首次验证), 之后交给 cron 每3天自动执行
echo "[entrypoint] 首次运行开始(立即执行, 约15~25分钟)..."
/usr/local/bin/python /app/main.py || echo "[entrypoint] 首次运行出错, 请查看 /data/logs/crawl.log"
echo "[entrypoint] 首次运行结束, 启动定时任务: 每5天凌晨04:00自动执行"
exec cron -f
