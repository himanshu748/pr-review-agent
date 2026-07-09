FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# HF Spaces uses 7860; override PORT for other hosts.
ENV PORT=7860
EXPOSE 7860

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
