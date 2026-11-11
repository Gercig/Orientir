FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY spravochnik.json ./spravochnik.json
COPY app ./app
WORKDIR /srv/app
# База данных (orientir.db) лежит в /data. Эту папку нужно подключать как том,
# иначе данные пропадут при пересоздании контейнера.
ENV ORIENTIR_DATA=/data
ENV PORT=8080
VOLUME /data
EXPOSE 8080
CMD ["sh", "-c", "gunicorn -w 2 -b 0.0.0.0:${PORT} --access-logfile - main:app"]
