FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock \
    && useradd --create-home --uid 10001 coinpup
COPY services/api/ services/api/
COPY alembic.ini ./
USER coinpup
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "coinpup_api.main:create_app", "--factory", "--app-dir", "services/api/src", "--host", "0.0.0.0", "--port", "8000"]
