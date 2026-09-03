FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir openai requests

ENTRYPOINT ["python", "/app/linkedin_auto.py"]
