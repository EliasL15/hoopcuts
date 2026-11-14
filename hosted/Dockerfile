FROM node:24-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm test && npm run build

FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY hosted/requirements.txt /app/hosted/requirements.txt
RUN pip install --no-cache-dir -r /app/hosted/requirements.txt
COPY basketball_highlights /app/basketball_highlights
COPY hosted /app/hosted
COPY --from=frontend /app/frontend/dist /app/frontend/dist
ENV PYTHONPATH=/app
CMD ["uvicorn", "hosted.app.main:app", "--host", "0.0.0.0", "--port", "8080"]
