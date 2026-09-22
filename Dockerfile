FROM python:3.11-slim

ENV TZ=Asia/Shanghai
RUN apt-get update \
 && apt-get install -y --no-install-recommends cron tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py /app/main.py
COPY entrypoint.sh /entrypoint.sh
COPY iptv-cron /etc/cron.d/iptv-cron
RUN chmod +x /entrypoint.sh && chmod 0644 /etc/cron.d/iptv-cron

ENTRYPOINT ["/entrypoint.sh"]
