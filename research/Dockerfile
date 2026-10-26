FROM python:3.11-slim
WORKDIR /app
COPY research/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY research /app/research
COPY scripts/report.py /app/scripts/report.py
COPY user_data/strategies/signal_gate.py /app/user_data/strategies/signal_gate.py
RUN mkdir -p /app/config /app/runtime/baseline /app/runtime/hybrid /app/runtime/fast /app/marks /app/data /app/signals \
    && touch /app/user_data/__init__.py /app/user_data/strategies/__init__.py
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "-m", "research.worker", "loop", "--out", "/app/signals", "--marks-out", "/app/marks", "--log", "/app/data/decisions.jsonl", "--ta-root", "/app/data/ta_bias"]
