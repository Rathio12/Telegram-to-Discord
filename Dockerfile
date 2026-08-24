FROM python:3.11-slim
WORKDIR /app
RUN mkdir -p /app/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Ensure the database folder is writable
RUN chmod -R 777 /app/data
CMD ["python", "main.py"]