FROM python:3.13-slim

WORKDIR /app
RUN pip install --no-cache-dir pyyaml==6.0.3

COPY *.py /app/
COPY static /app/static

# stories/ and state/ are bind-mounted so stories hot-reload on edit and the
# database survives image rebuilds.
# config.py defaults the model URLs to 127.0.0.1, which is right for a native
# install. In a container that is the container itself, so they are overridden
# here with the compose service name.
ENV LOOM_STORIES=/stories \
    LOOM_STATE=/state \
    LOOM_HOST=0.0.0.0 \
    LOOM_PORT=8100 \
    LOOM_OLLAMA_URL=http://ollama:11434 \
    LOOM_EMBED_URL=http://ollama:11434 \
    PYTHONUNBUFFERED=1

EXPOSE 8100
CMD ["python3", "run.py"]
