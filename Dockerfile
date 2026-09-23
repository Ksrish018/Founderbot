FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Kolkata
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY prompts ./prompts
COPY config ./config
COPY data/notes ./data/notes
COPY data/published ./data/published

# data/app.db lives on a volume so notes and drafts survive redeploys.
# config/facts.yaml is also written by /facts; mount it too if you want resolutions to persist.
VOLUME ["/app/data"]

CMD ["python", "-m", "app"]
