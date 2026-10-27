FROM python:3.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --system --uid 10001 app
COPY app ./app
USER 10001
EXPOSE 8000
CMD ["python", "-m", "app"]
