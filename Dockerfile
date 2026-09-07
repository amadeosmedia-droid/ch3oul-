FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 libffi-dev nodejs npm git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN git clone --depth 1 --branch 1.3.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git bgutil-ytdlp-pot-provider \
    && cd bgutil-ytdlp-pot-provider/server \
    && npm ci \
    && npx tsc

CMD ["sh", "-c", "node bgutil-ytdlp-pot-provider/server/build/main.js --host 127.0.0.1 --port 4416 & exec python main.py"]
