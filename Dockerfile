FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY examples /app/examples
COPY web /app/web
COPY zeabur-start.sh /app/zeabur-start.sh
RUN chmod +x /app/zeabur-start.sh

ENV PYTHONUNBUFFERED=1
ENV RELAY_DB=/data/relay.db
ENV RELAY_UPLOAD_DIR=/data/uploads
ENV RELAY_BRAIN_FILE=/data/brain_target
ENV LOOP_CONFIG=/data/api_loop.config.json
ENV RELAY_WEB_DIR=/app/web

EXPOSE 8080

CMD ["/app/zeabur-start.sh"]
