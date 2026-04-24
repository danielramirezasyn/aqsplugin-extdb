# ApiQuickServe — External DB Plugin

Plugin para conectividad con bases de datos externas desde ApiQuickServe.
Permite ejecutar DML y DDL contra SQL Server, MySQL/MariaDB y PostgreSQL
directamente desde ApiQuickServe, sin almacenar configuraciones ni credenciales.

---

## Tabla de contenido

1. [¿Qué hace este plugin?](#1-qué-hace-este-plugin)
2. [Requisitos previos](#2-requisitos-previos)
3. [Estructura del proyecto](#3-estructura-del-proyecto)
4. [Drivers disponibles](#4-drivers-disponibles)
5. [Autenticación](#5-autenticación)
6. [Connection Pool — qué es y cómo funciona](#6-connection-pool--qué-es-y-cómo-funciona)
7. [Variables de entorno completas](#7-variables-de-entorno-completas)
8. [Endpoints](#8-endpoints)
9. [Modos de ejecución](#9-modos-de-ejecución)
10. [Estructura de la respuesta](#10-estructura-de-la-respuesta)
11. [Códigos de error](#11-códigos-de-error)
12. [Ejemplos por driver](#12-ejemplos-por-driver)
13. [Despliegue paso a paso](#13-despliegue-paso-a-paso)
14. [Agregar un nuevo driver](#14-agregar-un-nuevo-driver)
15. [Versionado](#15-versionado)
16. [Seguridad](#16-seguridad)

---

## 1. ¿Qué hace este plugin?

Este plugin es un microservicio HTTP que actúa como puente entre ApiQuickServe
(que corre sobre Oracle ORDS) y bases de datos externas como SQL Server, MySQL o PostgreSQL.

**¿Por qué existe?**
Oracle ORDS puede hacer llamadas HTTP a servicios externos, pero no puede conectarse
directamente a SQL Server o MySQL. Este plugin resuelve ese problema: ORDS le manda
un request HTTP con las credenciales y el query, el plugin lo ejecuta y devuelve el
resultado como JSON.

**¿Cómo funciona en una llamada?**

```
ApiQuickServe (PL/SQL)
        │
        │  POST http://extdb-plugin:8000/execute
        │  Body: { driver, connection, mode, statement, params }
        │  Header: X-API-Key
        ▼
  extdb-plugin (este plugin)
        │
        │  Conecta a SQL Server / MySQL / PostgreSQL
        │  Ejecuta el query
        │  Cierra / devuelve la conexión al pool
        ▼
  Respuesta JSON normalizada
        │
        ▼
ApiQuickServe (PL/SQL continúa con los datos)
```

**Principios de diseño:**

- **Credenciales en tránsito**: las credenciales de BD van en el body de cada request, nunca almacenadas.
- **Driver pattern**: agregar un nuevo motor es agregar un módulo, sin tocar el core.
- **JSON normalizado**: la respuesta siempre tiene la misma estructura, sin importar el motor.
- **HTTP 200 siempre**: los errores de BD se reportan en el body (`status: "error"`), no como HTTP 4xx/5xx. Esto simplifica el manejo en PL/SQL.
- **Autenticación obligatoria**: el endpoint `/execute` requiere un `X-API-Key`.
- **Connection pooling**: las conexiones se reutilizan entre requests para reducir latencia y carga en la BD.

---

## 2. Requisitos previos

- Docker Engine 20+ y Docker Compose v2+
- Acceso de red desde el contenedor a los servidores de BD que quieras usar
- Para SQL Server: el servidor debe aceptar conexiones TCP (port 1433 o el configurado)
- Para MySQL/MariaDB: el servidor debe aceptar conexiones TCP (port 3306 o el configurado)
- Para PostgreSQL: el servidor debe aceptar conexiones TCP (port 5432 o el configurado)

---

## 3. Estructura del proyecto

```
apiquickserve-extdb-plugin/
├── Dockerfile                        # Definición de la imagen Docker
├── docker-compose.yml                # Configuración del servicio (editar antes de usar)
├── requirements.txt                  # Dependencias Python
├── build.sh                          # Script de build de la imagen
└── app/
    ├── main.py                       # Aplicación FastAPI — endpoints HTTP
    ├── models/
    │   └── schemas.py                # Modelos de datos (request / response)
    ├── drivers/
    │   ├── base.py                   # Clase base abstracta (contrato de drivers)
    │   ├── registry.py               # Registry: mapea nombre → clase del driver
    │   ├── sqlserver.py              # Driver SQL Server (pyodbc + ODBC Driver 18)
    │   ├── mysql.py                  # Driver MySQL / MariaDB (mysql-connector-python)
    │   └── postgres.py               # Driver PostgreSQL (psycopg2)
    └── core/
        ├── logging_config.py         # Configuración de logging
        ├── security.py               # Validación de X-API-Key
        ├── pool_config.py            # Configuración del connection pool (desde env vars)
        └── pool_manager.py           # Implementación del connection pool
```

---

## 4. Drivers disponibles

| Driver       | Motor                  | Puerto default | Librería Python          |
|--------------|------------------------|----------------|--------------------------|
| `sqlserver`  | Microsoft SQL Server   | 1433           | pyodbc + ODBC Driver 18  |
| `mysql`      | MySQL / MariaDB        | 3306           | mysql-connector-python   |
| `postgresql` | PostgreSQL             | 5432           | psycopg2-binary          |

---

## 5. Autenticación

El endpoint `/execute` requiere el header `X-API-Key` en todas las llamadas.

### Cómo configurar la key

Se define al levantar el contenedor mediante la variable de entorno `PLUGIN_API_KEY`:

```yaml
# En docker-compose.yml
environment:
  PLUGIN_API_KEY: "mi_clave_super_secreta_aqui"
```

```bash
# O directamente con docker run
docker run -e PLUGIN_API_KEY=mi_clave_super_secreta_aqui ...
```

> **Importante:** Si no defines `PLUGIN_API_KEY`, el contenedor **no arranca** y
> muestra un error claro en los logs.

### Cómo usar la key en un request

```http
POST http://extdb-plugin:8000/execute
X-API-Key: mi_clave_super_secreta_aqui
Content-Type: application/json

{ ... body del request ... }
```

### Ver la key en los logs

```bash
docker logs extdb-plugin
```

Al arrancar correctamente verás:
```
╔══════════════════════════════════════════════════════════════╗
║            ApiQuickServe — External DB Plugin                ║
╠══════════════════════════════════════════════════════════════╣
║  X-API-Key cargada correctamente.                            ║
║                                                              ║
║  PLUGIN_API_KEY → mi_clave_super_secreta_aqui                ║
║                                                              ║
║  Incluye este header en cada request a /execute              ║
╚══════════════════════════════════════════════════════════════╝
```

### Respuesta sin autenticación

```
HTTP 401 Unauthorized
{ "detail": "X-API-Key inválida o ausente." }
```

---

## 6. Connection Pool — qué es y cómo funciona

### ¿Qué es un connection pool?

Sin pooling, cada request al endpoint `/execute` hace esto:

```
Request llega → abrir conexión → ejecutar query → cerrar conexión → Response
```

Abrir y cerrar una conexión de BD tiene un costo: autenticación TCP, handshake TLS,
negociación de protocolo. En SQL Server esto puede tomar 50-200ms solo para conectar.

Con pooling:

```
Al arrancar: crear N conexiones y mantenerlas abiertas
Request llega → tomar conexión del pool → ejecutar query → devolver conexión → Response
```

La conexión ya existe, solo se ejecuta el query. Latencia de conexión: 0ms.

### ¿Cómo funciona el pool en este plugin?

- **Un pool por combinación única**: cada par `(driver, host, puerto, BD, usuario, contraseña)` 
  tiene su propio pool independiente. Puedes conectarte a SQL Server y a MySQL
  simultáneamente y ambos tendrán sus propios pools.

- **Lazy creation**: el pool se crea la primera vez que se recibe un request para
  esa combinación de conexión. No se crean pools vacíos al arrancar.

- **Pre-calentamiento**: al crear un pool, se abren `POOL_MIN_SIZE` conexiones
  inmediatamente (default: 2).

- **Crecimiento bajo demanda**: si todas las conexiones del pool están en uso y
  llegan más requests, el pool crea conexiones adicionales hasta `POOL_MAX_SIZE`.

- **Reciclado automático**: las conexiones con más de `POOL_RECYCLE` segundos de vida
  se reemplazan automáticamente al ser adquiridas. Esto evita usar conexiones que el
  servidor de BD ya cerró silenciosamente (típico después de un timeout del servidor).

- **Detección de conexiones rotas**: si un query falla por error de conexión
  (servidor caído, red cortada), la conexión se descarta del pool y no se devuelve.

### ¿Cuándo desactivar el pool?

Si tienes un volumen muy bajo de requests (ej: menos de 1 por minuto) o si el servidor
de BD tiene un límite muy estricto de conexiones, puedes desactivarlo:

```yaml
environment:
  POOL_ENABLED: "false"
```

Con pool desactivado, el comportamiento es idéntico a v1.0: una conexión por request.

---

## 7. Variables de entorno completas

Todas las variables se configuran en `docker-compose.yml` bajo `environment:`.

### Obligatorias

| Variable         | Descripción                                      | Ejemplo                  |
|------------------|--------------------------------------------------|--------------------------|
| `PLUGIN_API_KEY` | Clave de autenticación para el header X-API-Key  | `"mi_clave_secreta_123"` |

### Connection Pool (opcionales — tienen defaults)

| Variable         | Default | Descripción                                                              |
|------------------|---------|--------------------------------------------------------------------------|
| `POOL_ENABLED`   | `true`  | Activa (`true`) o desactiva (`false`) el pool globalmente               |
| `POOL_MIN_SIZE`  | `2`     | Conexiones que se crean al inicializar el pool (pre-calentamiento)      |
| `POOL_MAX_SIZE`  | `10`    | Máximo de conexiones simultáneas por pool                               |
| `POOL_TIMEOUT`   | `30`    | Segundos que un request espera por una conexión libre (luego da TIMEOUT)|
| `POOL_RECYCLE`   | `1800`  | Segundos de vida máxima de una conexión antes de ser reemplazada        |

### Ejemplo docker-compose.yml con todas las variables

```yaml
environment:
  # Obligatoria
  PLUGIN_API_KEY: "mi_clave_super_secreta"

  # Pool — ajustar según la carga esperada
  POOL_ENABLED:  "true"
  POOL_MIN_SIZE: "2"     # Subir a 5 si tienes carga constante
  POOL_MAX_SIZE: "10"    # Subir si tienes muchos requests simultáneos
  POOL_TIMEOUT:  "30"    # Bajar si quieres fallar rápido bajo saturación
  POOL_RECYCLE:  "1800"  # 30 min — está bien para la mayoría de los casos
```

### Guía rápida de ajuste del pool

| Escenario                               | Ajuste recomendado                                     |
|-----------------------------------------|--------------------------------------------------------|
| Baja carga (< 10 req/min)               | `POOL_MIN_SIZE=1`, `POOL_MAX_SIZE=5`                  |
| Carga media (10-100 req/min)            | `POOL_MIN_SIZE=2`, `POOL_MAX_SIZE=10` (defaults)      |
| Alta carga (> 100 req/min)              | `POOL_MIN_SIZE=5`, `POOL_MAX_SIZE=20`                 |
| Servidor BD con límite estricto         | `POOL_MAX_SIZE=3` o `POOL_ENABLED=false`              |
| Queries lentos (> 10s)                  | `POOL_TIMEOUT=60`                                     |
| Servidor BD reinicia frecuentemente     | `POOL_RECYCLE=300` (5 min)                            |

---

## 8. Endpoints

### `GET /health`

Verifica que el plugin esté operativo. **No requiere autenticación.**

```bash
curl http://localhost:9000/health
```

**Response:**
```json
{
  "status":  "ok",
  "version": "1.2.0",
  "drivers": ["sqlserver", "mysql", "postgresql"]
}
```

---

### `POST /execute`

Ejecuta una operación en la base de datos externa.

**Requiere el header `X-API-Key`.**

**Headers:**

| Header          | Valor                                |
|-----------------|--------------------------------------|
| `X-API-Key`     | La clave definida en `PLUGIN_API_KEY`|
| `Content-Type`  | `application/json`                   |

**Body del request:**

```json
{
  "driver":     "sqlserver",
  "connection": {
    "host":     "10.0.1.45",
    "port":     1433,
    "database": "MiBaseDeDatos",
    "username": "mi_usuario",
    "password": "mi_password"
  },
  "mode":      "sql",
  "statement": "SELECT id, nombre FROM clientes WHERE id = ?",
  "params":    [42]
}
```

**Campos del body:**

| Campo                 | Tipo    | Requerido | Descripción                                                  |
|-----------------------|---------|-----------|--------------------------------------------------------------|
| `driver`              | string  | Sí        | `"sqlserver"` \| `"mysql"` \| `"postgresql"`                 |
| `connection.host`     | string  | Sí        | IP o hostname del servidor de BD                             |
| `connection.port`     | integer | Sí        | Puerto TCP (1433 / 3306 / 5432 son los defaults)             |
| `connection.database` | string  | Sí        | Nombre de la base de datos                                   |
| `connection.username` | string  | Sí        | Usuario de conexión                                          |
| `connection.password` | string  | Sí        | Contraseña del usuario                                       |
| `mode`                | string  | Sí        | `"sql"` \| `"block"` \| `"callable"`                         |
| `statement`           | string  | Sí        | Query SQL, bloque DDL, o nombre del stored procedure         |
| `params`              | array   | No        | Parámetros posicionales (uno por `?`). Default: `[]`         |

---

## 9. Modos de ejecución

### Modo `sql` — Queries y DML con parámetros

Usar para: SELECT, INSERT, UPDATE, DELETE con valores variables.

Los parámetros se pasan en el array `params` y se insertan en el statement en el orden
de los marcadores `?` (todos los drivers usan `?` — el plugin hace la conversión interna).

```json
{
  "mode":      "sql",
  "statement": "SELECT * FROM productos WHERE categoria = ? AND precio < ?",
  "params":    ["electronica", 500]
}
```

> **Importante:** Usa siempre parámetros para valores de usuario. Nunca concatenes
> strings en el statement — eso abre la puerta a SQL injection.

### Modo `block` — DDL y bloques sin parámetros

Usar para: CREATE TABLE, ALTER TABLE, DROP, scripts completos, bloques BEGIN/END.

No acepta parámetros (`params` debe ser `[]`). El statement debe ser autocontenido.

```json
{
  "mode":      "block",
  "statement": "CREATE TABLE IF NOT EXISTS sync_log (id SERIAL PRIMARY KEY, mensaje TEXT, creado_en TIMESTAMP DEFAULT NOW())",
  "params":    []
}
```

### Modo `callable` — Stored Procedures

Usar para llamar stored procedures o funciones almacenadas.

El `statement` es el **nombre** del procedure. Los `params` son sus argumentos en orden.

```json
{
  "mode":      "callable",
  "statement": "sp_calcular_saldo",
  "params":    ["8-123-456", "2024-01-01"]
}
```

Internamente el plugin construye la llamada correcta según el driver:
- SQL Server: `EXEC sp_calcular_saldo ?, ?`
- MySQL: `CALL sp_calcular_saldo(%s, %s)`
- PostgreSQL: `CALL sp_calcular_saldo(%s, %s)`

---

## 10. Estructura de la respuesta

La respuesta siempre es HTTP 200 con este JSON, sin importar si hubo error o no.

### Response exitoso (SELECT)

```json
{
  "status":        "ok",
  "rows_affected":  2,
  "columns":       ["id", "nombre", "email"],
  "data": [
    { "id": 1, "nombre": "Ana García",   "email": "ana@ejemplo.com" },
    { "id": 2, "nombre": "Luis Pérez",   "email": "luis@ejemplo.com" }
  ],
  "execution_ms":  8,
  "error_code":    null,
  "error_message": null
}
```

### Response exitoso (INSERT / UPDATE / DELETE)

```json
{
  "status":        "ok",
  "rows_affected":  1,
  "columns":       [],
  "data":          [],
  "execution_ms":  12,
  "error_code":    null,
  "error_message": null
}
```

### Response con error

```json
{
  "status":        "error",
  "rows_affected":  null,
  "columns":        [],
  "data":           [],
  "execution_ms":   203,
  "error_code":    "CONNECTION_FAILED",
  "error_message": "No se pudo conectar al servidor. SQLSTATE: 08001"
}
```

### Descripción de cada campo

| Campo           | Tipo          | Descripción                                                |
|-----------------|---------------|------------------------------------------------------------|
| `status`        | string        | `"ok"` si todo salió bien, `"error"` si hubo algún fallo   |
| `rows_affected` | integer/null  | Filas afectadas por el DML. `null` en DDL o cuando no aplica |
| `columns`       | array         | Nombres de las columnas retornadas (solo en SELECT)        |
| `data`          | array         | Array de objetos, uno por fila. Vacío si no hay filas      |
| `execution_ms`  | integer       | Tiempo de ejecución en milisegundos                        |
| `error_code`    | string/null   | Código de error (ver tabla abajo). `null` si no hay error  |
| `error_message` | string/null   | Descripción del error. `null` si no hay error              |

---

## 11. Códigos de error

| Código                 | Cuándo ocurre                                                      |
|------------------------|--------------------------------------------------------------------|
| `CONNECTION_FAILED`    | No se pudo abrir conexión (servidor apagado, credenciales malas)   |
| `QUERY_FAILED`         | El query tiene error de sintaxis, falta permiso, o viola una regla |
| `UNSUPPORTED_MODE`     | El valor de `mode` no es `sql`, `block`, ni `callable`             |
| `DRIVER_NOT_AVAILABLE` | El `driver` solicitado no está en esta imagen                      |
| `TIMEOUT`              | El pool no pudo entregar una conexión en `POOL_TIMEOUT` segundos   |
| `UNKNOWN_ERROR`        | Error inesperado — revisar logs del contenedor                     |

---

## 12. Ejemplos por driver

### SQL Server

#### SELECT con parámetros
```json
{
  "driver": "sqlserver",
  "connection": {
    "host": "10.0.1.45",
    "port": 1433,
    "database": "CoreBancario",
    "username": "apireader",
    "password": "s3cret"
  },
  "mode":      "sql",
  "statement": "SELECT id, nombre, saldo FROM cuentas WHERE cliente_id = ? AND activo = ?",
  "params":    [1042, 1]
}
```

#### INSERT
```json
{
  "driver": "sqlserver",
  "connection": { "host": "10.0.1.45", "port": 1433, "database": "CoreBancario", "username": "apiwriter", "password": "s3cret" },
  "mode":      "sql",
  "statement": "INSERT INTO movimientos (cuenta_id, monto, tipo, fecha) VALUES (?, ?, ?, GETDATE())",
  "params":    [1042, 150.00, "credito"]
}
```

#### DDL (crear tabla si no existe)
```json
{
  "driver": "sqlserver",
  "connection": { "host": "10.0.1.45", "port": 1433, "database": "CoreBancario", "username": "apiwriter", "password": "s3cret" },
  "mode":      "block",
  "statement": "IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name='sync_log') CREATE TABLE sync_log (id INT IDENTITY PRIMARY KEY, mensaje NVARCHAR(500), creado_en DATETIME DEFAULT GETDATE())",
  "params":    []
}
```

#### Stored Procedure
```json
{
  "driver": "sqlserver",
  "connection": { "host": "10.0.1.45", "port": 1433, "database": "CoreBancario", "username": "apireader", "password": "s3cret" },
  "mode":      "callable",
  "statement": "dbo.sp_calcular_riesgo_cliente",
  "params":    ["8-123-456", "2024-01-01"]
}
```

---

### MySQL / MariaDB

#### SELECT con parámetros
```json
{
  "driver": "mysql",
  "connection": {
    "host": "10.0.1.50",
    "port": 3306,
    "database": "mi_app",
    "username": "apireader",
    "password": "s3cret"
  },
  "mode":      "sql",
  "statement": "SELECT id, nombre, email FROM usuarios WHERE activo = ? AND tipo = ?",
  "params":    [1, "premium"]
}
```

#### UPDATE
```json
{
  "driver": "mysql",
  "connection": { "host": "10.0.1.50", "port": 3306, "database": "mi_app", "username": "apiwriter", "password": "s3cret" },
  "mode":      "sql",
  "statement": "UPDATE usuarios SET ultimo_acceso = NOW() WHERE id = ?",
  "params":    [99]
}
```

#### Stored Procedure MySQL
```json
{
  "driver": "mysql",
  "connection": { "host": "10.0.1.50", "port": 3306, "database": "mi_app", "username": "apireader", "password": "s3cret" },
  "mode":      "callable",
  "statement": "sp_resumen_ventas_mes",
  "params":    ["2024-03"]
}
```

---

### PostgreSQL

#### SELECT con parámetros
```json
{
  "driver": "postgresql",
  "connection": {
    "host": "10.0.1.60",
    "port": 5432,
    "database": "inventario",
    "username": "apireader",
    "password": "s3cret"
  },
  "mode":      "sql",
  "statement": "SELECT id, sku, descripcion, stock FROM productos WHERE categoria_id = ? AND stock > ?",
  "params":    [5, 0]
}
```

#### INSERT con RETURNING (retorna el id generado)
```json
{
  "driver": "postgresql",
  "connection": { "host": "10.0.1.60", "port": 5432, "database": "inventario", "username": "apiwriter", "password": "s3cret" },
  "mode":      "sql",
  "statement": "INSERT INTO pedidos (cliente_id, total, estado) VALUES (?, ?, ?) RETURNING id",
  "params":    [42, 299.99, "pendiente"]
}
```

#### DDL en PostgreSQL
```json
{
  "driver": "postgresql",
  "connection": { "host": "10.0.1.60", "port": 5432, "database": "inventario", "username": "apiwriter", "password": "s3cret" },
  "mode":      "block",
  "statement": "CREATE TABLE IF NOT EXISTS sync_eventos (id BIGSERIAL PRIMARY KEY, payload JSONB, procesado BOOLEAN DEFAULT FALSE, creado_en TIMESTAMPTZ DEFAULT NOW())",
  "params":    []
}
```

#### Stored Procedure PostgreSQL (requiere PG 11+)
```json
{
  "driver": "postgresql",
  "connection": { "host": "10.0.1.60", "port": 5432, "database": "inventario", "username": "apireader", "password": "s3cret" },
  "mode":      "callable",
  "statement": "sp_calcular_descuento",
  "params":    [42, "VIP"]
}
```

> **Nota sobre funciones PostgreSQL:** si tienes una FUNCTION (no PROCEDURE), usa modo `sql`
> con `SELECT nombre_funcion(?)` en lugar de `callable`.

---

## 13. Despliegue paso a paso

### Paso 1: Build de la imagen

Desde el directorio `apiquickserve-extdb-plugin/`:

```bash
# Versión default (1.2.0)
./build.sh

# O con versión específica
./build.sh 1.2.0
```

O directamente con docker:
```bash
docker build -t apiquickserve/extdb:1.2.0 -t apiquickserve/extdb:latest .
```

### Paso 2: Configurar docker-compose.yml

Edita el archivo `docker-compose.yml` y cambia al menos `PLUGIN_API_KEY`:

```yaml
environment:
  PLUGIN_API_KEY: "pon_aqui_tu_clave_real_y_segura"
```

> Genera una clave segura con:
> ```bash
> openssl rand -hex 32
> ```

### Paso 3: Levantar el contenedor

```bash
docker compose up -d
```

### Paso 4: Verificar que arrancó correctamente

```bash
# Ver logs (deberías ver el banner con la API Key)
docker logs extdb-plugin

# Verificar el endpoint de salud
curl http://localhost:9000/health
```

Respuesta esperada:
```json
{"status": "ok", "version": "1.2.0", "drivers": ["sqlserver", "mysql", "postgresql"]}
```

### Paso 5: Hacer una prueba de conexión

```bash
curl -X POST http://localhost:9000/execute \
  -H "X-API-Key: tu_clave" \
  -H "Content-Type: application/json" \
  -d '{
    "driver": "postgresql",
    "connection": {
      "host": "tu_servidor_pg",
      "port": 5432,
      "database": "tu_bd",
      "username": "tu_usuario",
      "password": "tu_password"
    },
    "mode": "sql",
    "statement": "SELECT version()",
    "params": []
  }'
```

### Paso 6: Integrar con ApiQuickServe

En el compose principal de ApiQuickServe, agrega el servicio `extdb-plugin` asegurándote
de que ambos estén en la misma red Docker. Desde PL/SQL de ORDS, llama:

```
http://extdb-plugin:8000/execute
```

El nombre `extdb-plugin` es el nombre del servicio Docker. El DNS interno de Docker
lo resuelve automáticamente dentro de la misma red.

### Rebuild completo (desarrollo)

```bash
docker compose down --rmi local && docker compose up -d --build && docker logs -f extdb-plugin
```

---

## 14. Agregar un nuevo driver

Para añadir soporte a un nuevo motor de BD (ej: Oracle, MongoDB):

1. **Crear el archivo del driver:**
   ```python
   # app/drivers/oracle.py
   from app.drivers.base import BaseDriver
   from app.core.pool_config import pool_config
   from app.core.pool_manager import PoolManager, make_pool_key

   class OracleDriver(BaseDriver):
       def build_connection_string(self): ...
       def _connect(self): ...
       def _pool_key(self): ...
       def _get_conn(self): ...
       def _return_conn(self, conn, born, use_pool, broken): ...
       def execute(self, mode, statement, params): ...
   ```

2. **Agregar la librería a `requirements.txt`:**
   ```
   cx_Oracle==8.3.0
   ```

3. **Registrar el driver en `app/drivers/registry.py`:**
   ```python
   from app.drivers.oracle import OracleDriver
   DRIVER_REGISTRY[DriverType.oracle] = OracleDriver
   ```

4. **Agregar el enum en `app/models/schemas.py`:**
   ```python
   class DriverType(str, Enum):
       oracle = "oracle"  # agregar
   ```

5. **Instalar dependencias del sistema en `Dockerfile`** si aplica (ej: Oracle Instant Client).

6. **Rebuild** y bump de versión: `apiquickserve/extdb:1.3.0`.

---

## 15. Versionado

| Versión | Drivers incluidos                        | Notas                                              |
|---------|------------------------------------------|----------------------------------------------------|
| 1.0.0   | SQL Server                               | Versión inicial, conexión por request              |
| 1.1.0   | SQL Server, MySQL / MariaDB              | Driver MySQL + autenticación X-API-Key             |
| 1.2.0   | SQL Server, MySQL / MariaDB, PostgreSQL  | Driver PostgreSQL + connection pooling configurable |

---

## 16. Seguridad

- **Credenciales de BD nunca se loguean.** El logger registra driver, modo, database, host y port — nunca username ni password.
- El endpoint `/execute` requiere **`X-API-Key`** en cada request. Sin ella devuelve `HTTP 401`. La validación usa `secrets.compare_digest` para evitar timing attacks.
- El endpoint `/health` es público para permitir health checks sin credenciales.
- La `PLUGIN_API_KEY` se define **al momento del despliegue** vía variable de entorno. Si no está definida, el contenedor no arranca.
- El plugin **no expone puertos al exterior** en configuración de producción. El puerto `9000` es opcional y solo para pruebas.
- `TrustServerCertificate=yes` está habilitado en SQL Server para compatibilidad con entornos sin CA corporativo. En producción con certificado válido, cambiar a `no`.
- La clave del pool (`pool_key`) se genera como SHA-256 de los parámetros de conexión incluyendo la contraseña, por lo que nunca aparece texto plano en memoria ni en logs.
