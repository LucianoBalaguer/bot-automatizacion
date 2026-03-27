FROM python:3.11-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir flask requests twilio firebase-admin gunicorn openai gspread google-auth

EXPOSE 80

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:80", "--workers", "1"]
