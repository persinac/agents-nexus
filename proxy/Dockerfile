FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py routing.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "4000", "--log-level", "warning"]
