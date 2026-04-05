FROM python:3.11

# build v2

WORKDIR /app

RUN apt-get update && apt-get install -y libstdc++6 && rm -rf /var/lib/apt/lists/*

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 80

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:80", "--workers", "1", "--log-level", "debug", "--capture-output"]
