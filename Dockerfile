FROM python:3.11-slim

WORKDIR /app

# 只需要 kubernetes SDK，无其他依赖
RUN pip install --no-cache-dir kubernetes==31.0.0

COPY server.py .

CMD ["python", "server.py"]
