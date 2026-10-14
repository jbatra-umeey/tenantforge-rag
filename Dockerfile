FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY tenantforge ./tenantforge
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home app && mkdir -p /app/data && chown -R app:app /app/data
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "tenantforge", "serve", "--host", "0.0.0.0"]
