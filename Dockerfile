FROM node:22-alpine AS frontend
WORKDIR /src
COPY package*.json ./
RUN npm ci
COPY app/frontend.js app/vip_frontend.js app/
RUN npm run build

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && groupadd -g 10001 app && useradd -u 10001 -g app app
COPY . .
COPY --from=frontend /src/app/static/app.js app/static/app.js
COPY --from=frontend /src/app/static/vip.js app/static/vip.js
RUN chmod +x scripts/entrypoint.sh
USER app
EXPOSE 8000
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["api"]
