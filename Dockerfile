FROM python:3.13-slim

WORKDIR /app
RUN pip install --no-cache-dir pyyaml==6.0.3

COPY *.py /app/
COPY static /app/static

# stories/ and state/ are bind-mounted so stories hot-reload on edit and the
# database survives image rebuilds.
ENV LOOM_STORIES=/stories \
    LOOM_STATE=/state \
    LOOM_HOST=0.0.0.0 \
    LOOM_PORT=8100 \
    PYTHONUNBUFFERED=1

EXPOSE 8100
CMD ["python3", "run.py"]
