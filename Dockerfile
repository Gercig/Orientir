FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY spravochnik.json ./spravochnik.json
COPY app ./app
WORKDIR /srv/app
ENV PORT=8080
EXPOSE 8080
# Логи доступа без тела запросов (по умолчанию gunicorn их не пишет)
CMD ["sh", "-c", "gunicorn -w 2 -b 0.0.0.0:${PORT} --access-logfile - main:app"]
