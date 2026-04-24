# ============================================================
# apiquickserve/extdb — External DB Plugin
# Base image: python:3.12-slim (Debian Bookworm)
# Incluye: FastAPI + pyodbc + ODBC Driver 18 for SQL Server
#                   + mysql-connector-python para MySQL/MariaDB
#                   + psycopg2-binary para PostgreSQL
# ============================================================

FROM python:3.12-slim

# --- Metadatos de la imagen ---
LABEL maintainer="ApiQuickServe <team@apiquickserve.io>"
LABEL version="1.4.0"
LABEL description="Plugin para conectividad con bases de datos externas desde ApiQuickServe"

# --- Variables de entorno base ---
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_PORT=8000

# --- Dependencias del sistema + ODBC Driver 18 for SQL Server ---
# Microsoft publica los paquetes para Debian en su propio repositorio APT.
# psycopg2-binary incluye libpq estáticamente — no se necesita instalar libpq por separado.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        gnupg2 \
        apt-transport-https \
        unixodbc \
        unixodbc-dev \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
        | tee /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
        msodbcsql18 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# --- Directorio de trabajo ---
WORKDIR /app

# --- Dependencias Python ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Código fuente ---
COPY app/ ./app/

# --- Puerto expuesto ---
EXPOSE 8000

# --- Healthcheck interno ---
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# --- Arranque ---
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
