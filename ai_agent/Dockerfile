FROM python:3.12-slim

# шрифт с кириллицей для офлайн-иллюстраций
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Опционально: Codex CLI для режима LLM_PROVIDER=codex (аккаунт ChatGPT).
# docker compose build --build-arg INSTALL_CODEX=true ai-agent
ARG INSTALL_CODEX=false
RUN if [ "$INSTALL_CODEX" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends nodejs npm && \
      npm i -g @openai/codex && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY marketer ./marketer
COPY admin ./admin

ENV AI_MEDIA_DIR=/data/media
EXPOSE 8000
CMD ["uvicorn", "marketer.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
