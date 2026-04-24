# ApiQuickServe — External DB Plugin

Plugin para conectividad con bases de datos externas desde ApiQuickServe.
Permite ejecutar DML y DDL contra SQL Server, MySQL/MariaDB y PostgreSQL
directamente desde ApiQuickServe (Oracle ORDS/APEX), sin exponer credenciales
en cada llamada.

---

## Tabla de contenido

1. [¿Qué hace este plugin?](#1-qué-hace-este-plugin)
2. [Requisitos previos](#2-requisitos-previos)
3. [Estructura del proyecto](#3-estructura-del-proyecto)
4. [Drivers disponibles](#4-drivers-disponibles)
5. [Autenticación](#5-autenticación)
6. [Gestión de conexiones — /setup](#6-gestión-de-conexiones--setup)
7. [Encriptación de contraseñas — AES-256-GCM](#7-encriptación-de-contraseñas--aes-256-gcm)
8. [Connection Pool](#8-connection-pool)
9. [IP Allowlist](#9-ip-allowlist)
10. [Variables de entorno completas](#10-variables-de-entorno-completas)
11. [Endpoints](#11-endpoints)
12. [Modos de ejecución](#12-modos-de-ejecución)
13. [Estructura de la respuesta](#13-estructura-de-la-respuesta)
14. [Códigos de error](#14-códigos-de-error)
15. [Ejemplos por driver](#15-ejemplos-por-driver)
16. [Despliegue paso a paso](#16-despliegue-paso-a-paso)
17. [Agregar un nuevo driver](#17-agregar-un-nuevo-driver)
18. [Versionado](#18-versionado)
19. [Seguridad](#19-seguridad)

---

## 1. ¿Qué hace este plugin?

Este plugin es un microservicio HTTP que actúa como puente entre ApiQuickServe
(Oracle ORDS/APEX) y bases de datos externas como SQL Server, MySQL o PostgreSQL.

**¿Por qué existe?**
Oracle ORDS puede hacer llamadas HTTP a servicios externos, pero no puede conectarse
directamente a SQL Server o MySQL. Este plugin resuelve ese problema.

**¿Cómo funciona en una llamada?**

```
ApiQuickServe (PL/SQL)
        │
        │  POST http://extdb-plugin:8000/execute
        │  Body: { connection_alias, mode, statement, params }
        │  Header: X-API-Key
        ▼
  extdb-plugin (este plugin)
        │
        │  Busca credenciales por alias → desencripta contraseña en RAM
        │  Conecta a SQL Server / MySQL / PostgreSQL (via pool)
        │  Ejecuta el query → devuelve la conexión al pool
        ▼
  Respuesta JSON normalizada
        │
        ▼
ApiQuickServe (PL/SQL continúa con los datos)
```

**Principios de diseño:**

- **Credenciales registradas una sola vez**: se configuran vía `POST /setup` y se persisten encriptadas. Los requests a `/execute` solo llevan un alias.
- **Contraseñas encriptadas en disco**: AES-256-GCM con clave derivada por PBKDF2. Nunca texto plano en el archivo de persistencia (si `ENCRYPTION_KEY` está activa).
- **Driver pattern**: agregar un nuevo motor es agregar un módulo.
- **JSON normalizado**: la respuesta siempre tiene la misma estructura.
- **HTTP 200 siempre**: los errores van en el body (`status: "error"`). Simplifica el manejo en PL/SQL.
- **Autenticación obligatoria**: todos los endpoints requieren `X-API-Key`.
- **Connection pooling**: las conexiones se reutilizan entre requests.
- **IP Allowlist opcional**: filtra qué IPs pueden acceder al plugin.

---

## 2. Requisitos previos

- Docker Engine 20+ y Docker Compose v2+
- Acceso de red desde el contenedor a los servidores de BD
- SQL Server: puerto 1433 (o el configurado)
- MySQL/MariaDB: puerto 3306 (o el configurado)
- PostgreSQL: puerto 5432 (o el configurado)

---

## 3. Estructura del proyecto

```
apiquickserve-extdb-plugin/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── build.sh
└── app/
    ├── main.py                       # FastAPI — todos los endpoints
    ├── models/
    │   └── schemas.py                # Modelos Pydantic (request / response)
    ├── drivers/
    │   ├── base.py                   # Clase base abstracta
    │   ├── registry.py               # Mapea nombre → clase del driver
    │   ├── sqlserver.py              # Driver SQL Server
    │   ├── mysql.py                  # Driver MySQL / MariaDB
    │   └── postgres.py               # Driver PostgreSQL
    └── core/
        ├── logging_config.py         # Configuración de logging
        ├── security.py               # Validación de X-API-Key
        ├── crypto.py                 # Encriptación AES-256-GCM
        ├── connection_store.py       # Persistencia de conexiones en /data
        ├── pool_config.py            # Configuración del connection pool
        ├── pool_manager.py           # Implementación del pool
        └── ip_filter.py              # Filtro de IPs (allowlist)
```

---

## 4. Drivers disponibles

| Driver       | Motor                | Puerto default | Librería Python         |
|--------------|----------------------|----------------|-------------------------|
| `sqlserver`  | Microsoft SQL Server | 1433           | pyodbc + ODBC Driver 18 |
| `mysql`      | MySQL / MariaDB      | 3306           | mysql-connector-python  |
| `postgresql` | PostgreSQL           | 5432           | psycopg2-binary         |

---

## 5. Autenticación

**Todos los endpoints** requieren el header `X-API-Key`, incluyendo `/health` y `/setup`.

```http
X-API-Key: tu_clave_secreta
```

### Configurar la key

```yaml
# docker-compose.yml
environment:
  PLUGIN_API_KEY: "mi_clave_super_secreta"
```

> Si no defines `PLUGIN_API_KEY`, el contenedor **no arranca**.

### Ver la key al arrancar

```bash
docker logs extdb-plugin
```

```
║  PLUGIN_API_KEY  → ***xxxx        ← solo los últimos 4 caracteres
║  Encriptación    → AES-256-GCM activa
║  IP allowlist    → desactivada
```

### Sin autenticación

```
HTTP 401 — { "detail": "X-API-Key inválida o ausente." }
```

---

## 6. Gestión de conexiones — /setup

A partir de **v1.3.0** las credenciales de BD **no viajan en cada request**.
Se registran una sola vez y se identifican por un **alias**.

### Flujo completo

```
1. Registrar la conexión (una sola vez)
   POST /setup  →  alias "core_bancario" guardado y encriptado en /data

2. Ejecutar queries (sin credenciales)
   POST /execute  →  { "connection_alias": "core_bancario", ... }
```

### Registrar una conexión

```bash
curl -X POST http://localhost:9000/setup \
  -H "X-API-Key: tu_clave" \
  -H "Content-Type: application/json" \
  -d '{
    "alias":    "core_sqlserver",
    "driver":   "sqlserver",
    "host":     "10.0.1.45",
    "port":     1433,
    "database": "CoreBancario",
    "username": "apireader",
    "password": "s3cret"
  }'
```

Respuesta:
```json
{ "status": "ok", "alias": "core_sqlserver", "message": "Conexión registrada" }
```

> Si el alias ya existe, lo sobreescribe.

### Listar conexiones registradas

```bash
curl http://localhost:9000/setup \
  -H "X-API-Key: tu_clave"
```

```json
{
  "connections": [
    { "alias": "core_sqlserver", "driver": "sqlserver", "host": "10.0.1.45", "port": 1433, "database": "CoreBancario" }
  ]
}
```

> Las contraseñas **nunca aparecen** en esta respuesta.

### Eliminar una conexión

```bash
curl -X DELETE http://localhost:9000/setup/core_sqlserver \
  -H "X-API-Key: tu_clave"
```

```json
{ "status": "ok", "alias": "core_sqlserver", "message": "Conexión eliminada" }
```

### Persistencia

Las conexiones se guardan en `/data/connections.json` dentro del contenedor,
mapeado al volumen Docker `extdb-data`. **Sobreviven a reinicios y rebuilds.**

```bash
# Ver el archivo directamente
docker exec extdb-plugin cat /data/connections.json
```

---

## 7. Encriptación de contraseñas — AES-256-GCM

### Cómo funciona

Cuando `ENCRYPTION_KEY` está configurada, el plugin encripta cada contraseña
antes de guardarla en disco:

```
Contraseña real:  Admin1234!
En disco:         ENC:dBXBhy9mCkLeEntkmXgL9leMOBY3PVAtAKBKOH4eKPwqOvyTtd8=
```

**Algoritmo:**
- Derivación de clave: **PBKDF2-HMAC-SHA256** con 100,000 iteraciones → clave de 256 bits
- Encriptación: **AES-256-GCM** (confidencialidad + integridad)
- Nonce aleatorio de 96 bits generado por `os.urandom()` en cada encriptación
- Resultado: `ENC:` + base64(nonce + ciphertext + tag GCM)

La contraseña en texto plano solo existe en RAM durante los milisegundos
que dura la ejecución del query.

### Configurar la clave

```yaml
# docker-compose.yml
environment:
  ENCRYPTION_KEY: "mi_frase_secreta_larga_y_segura"
```

Genera una clave fuerte:
```bash
openssl rand -hex 32
```

### Opción más segura — clave desde el entorno del host

Para que la clave no quede escrita en ningún archivo:

```yaml
# docker-compose.yml
environment:
  ENCRYPTION_KEY: "${ENCRYPTION_KEY}"   # viene del host
```

```bash
# En el servidor, antes de docker compose up
export ENCRYPTION_KEY="$(openssl rand -hex 32)"
docker compose up -d
```

### Migración automática

Si arrancas el plugin con `ENCRYPTION_KEY` activa y ya tienes conexiones
guardadas en texto plano (de una versión anterior), el plugin las
**encripta automáticamente al arrancar** sin que hagas nada.

### Sin ENCRYPTION_KEY

Si no la defines, las contraseñas se guardan en texto plano con un `WARNING`
en los logs. El plugin funciona igual, solo sin encriptación en disco.

### Si cambias la ENCRYPTION_KEY

Las contraseñas guardadas con la clave anterior no se podrán desencriptar.
El endpoint `/execute` devolverá `DECRYPTION_ERROR`. Solución: volver a
registrar las conexiones con `POST /setup`.

---

## 8. Connection Pool

### ¿Para qué sirve?

Sin pool, cada request abre y cierra una conexión de BD (~50-200ms de overhead).
Con pool, las conexiones se reutilizan — el overhead de conexión es 0ms.

### Cómo funciona

- Un pool por cada combinación única `(driver + host + puerto + BD + usuario)`
- Se crea la primera vez que llega un request para esas credenciales
- Se pre-calientan `POOL_MIN_SIZE` conexiones al crear el pool
- Crece hasta `POOL_MAX_SIZE` bajo demanda
- Las conexiones con más de `POOL_RECYCLE` segundos se reemplazan automáticamente
- Las conexiones rotas se descartan, no se devuelven al pool

### Configuración

```yaml
environment:
  POOL_ENABLED:  "true"    # true | false
  POOL_MIN_SIZE: "2"       # conexiones al inicializar
  POOL_MAX_SIZE: "10"      # máximo de conexiones simultáneas
  POOL_TIMEOUT:  "30"      # segundos esperando conexión libre
  POOL_RECYCLE:  "1800"    # segundos de vida máxima (30 min)
```

| Escenario                    | Ajuste recomendado                         |
|------------------------------|--------------------------------------------|
| Baja carga (< 10 req/min)    | `MIN=1`, `MAX=5`                           |
| Carga media                  | `MIN=2`, `MAX=10` (defaults)               |
| Alta carga (> 100 req/min)   | `MIN=5`, `MAX=20`                          |
| BD con límite de conexiones  | `MAX=3` o `POOL_ENABLED=false`             |

---

## 9. IP Allowlist

Si defines `ALLOWED_IPS`, **solo esas IPs pueden acceder al plugin**.
Cualquier otra IP recibe `HTTP 403` con cuerpo vacío — el plugin no revela que existe.

```yaml
environment:
  # Una IP
  ALLOWED_IPS: "192.168.1.10"

  # Varias IPs
  ALLOWED_IPS: "192.168.1.10,10.0.0.5"

  # Rango CIDR
  ALLOWED_IPS: "10.0.0.0/8"

  # Mix
  ALLOWED_IPS: "192.168.1.10,10.0.0.0/8,172.16.0.0/12"
```

Si no defines `ALLOWED_IPS` (o la dejas vacía), acepta cualquier IP.

**Detrás de un proxy (nginx, traefik):**
El plugin lee la IP real del cliente desde `X-Real-IP` (preferido) o `X-Forwarded-For`.

---

## 10. Variables de entorno completas

| Variable         | Obligatoria | Default | Descripción                                                  |
|------------------|-------------|---------|--------------------------------------------------------------|
| `PLUGIN_API_KEY` | ✅ Sí       | —       | Clave para el header `X-API-Key`                             |
| `ENCRYPTION_KEY` | No          | vacío   | Passphrase para AES-256-GCM. Sin ella, texto plano en disco  |
| `POOL_ENABLED`   | No          | `true`  | Activa/desactiva el connection pool                          |
| `POOL_MIN_SIZE`  | No          | `2`     | Conexiones mínimas por pool (pre-calentamiento)              |
| `POOL_MAX_SIZE`  | No          | `10`    | Máximo de conexiones simultáneas por pool                    |
| `POOL_TIMEOUT`   | No          | `30`    | Segundos esperando conexión del pool antes de dar TIMEOUT    |
| `POOL_RECYCLE`   | No          | `1800`  | Segundos de vida máxima de una conexión                      |
| `ALLOWED_IPS`    | No          | vacío   | IPs/CIDR autorizadas. Vacío = sin restricción                |

---

## 11. Endpoints

Todos los endpoints requieren `X-API-Key`.

### `GET /health`
Verifica que el plugin esté operativo.

```bash
curl http://localhost:9000/health -H "X-API-Key: tu_clave"
```
```json
{ "status": "ok", "version": "1.4.0", "drivers": [] }
```

---

### `POST /setup`
Registra o actualiza una conexión de BD.

**Body:**
```json
{
  "alias":    "nombre_unico",
  "driver":   "sqlserver | mysql | postgresql",
  "host":     "ip_o_hostname",
  "port":     1433,
  "database": "nombre_bd",
  "username": "usuario",
  "password": "contraseña"
}
```

---

### `GET /setup`
Lista todos los alias registrados (sin contraseñas).

---

### `DELETE /setup/{alias}`
Elimina una conexión registrada.

---

### `POST /execute`
Ejecuta una operación en la BD referenciada por alias.

**Body:**
```json
{
  "connection_alias": "nombre_alias_registrado",
  "mode":             "sql | block | callable",
  "statement":        "SELECT ...",
  "params":           []
}
```

> HTTP 200 siempre. Los errores van en el body con `status: "error"`.

---

## 12. Modos de ejecución

### `sql` — Queries y DML con parámetros

Para SELECT, INSERT, UPDATE, DELETE con valores variables.
Usa `?` como marcador de posición (todos los drivers).

```json
{
  "mode":      "sql",
  "statement": "SELECT * FROM clientes WHERE id = ? AND activo = ?",
  "params":    [42, 1]
}
```

> Siempre usa parámetros para valores de usuario. Nunca concatenes strings (SQL injection).

### `block` — DDL y bloques sin parámetros

Para CREATE TABLE, ALTER, DROP, scripts completos.
No acepta parámetros.

```json
{
  "mode":      "block",
  "statement": "CREATE TABLE IF NOT EXISTS log_sync (id SERIAL PRIMARY KEY, msg TEXT)",
  "params":    []
}
```

### `callable` — Stored Procedures

El `statement` es el **nombre** del procedure. Los `params` son sus argumentos.

```json
{
  "mode":      "callable",
  "statement": "dbo.sp_calcular_saldo",
  "params":    ["8-123-456", "2024-01-01"]
}
```

> El nombre del callable solo puede contener letras, números, puntos y guiones bajos.
> Esto previene SQL injection en el nombre del procedure.

---

## 13. Estructura de la respuesta

Siempre HTTP 200 con este JSON:

### SELECT exitoso
```json
{
  "status":        "ok",
  "rows_affected":  2,
  "columns":       ["id", "nombre", "email"],
  "data": [
    { "id": 1, "nombre": "Ana García", "email": "ana@ejemplo.com" }
  ],
  "execution_ms":  8,
  "error_code":    null,
  "error_message": null
}
```

### DML exitoso (INSERT / UPDATE / DELETE)
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

### Error
```json
{
  "status":        "error",
  "rows_affected":  null,
  "columns":        [],
  "data":           [],
  "execution_ms":   5,
  "error_code":    "ALIAS_NOT_FOUND",
  "error_message": "El alias 'mi_bd' no está registrado..."
}
```

---

## 14. Códigos de error

| Código                 | Cuándo ocurre                                                         |
|------------------------|-----------------------------------------------------------------------|
| `ALIAS_NOT_FOUND`      | El `connection_alias` no está registrado vía `/setup`                 |
| `DECRYPTION_ERROR`     | La contraseña está encriptada pero `ENCRYPTION_KEY` es incorrecta     |
| `CONNECTION_FAILED`    | No se pudo conectar al servidor de BD                                 |
| `QUERY_FAILED`         | El query tiene error de sintaxis, falta permiso, o viola una regla    |
| `UNSUPPORTED_MODE`     | El valor de `mode` no es válido                                       |
| `DRIVER_NOT_AVAILABLE` | El driver solicitado no está disponible                               |
| `TIMEOUT`              | El pool no pudo entregar conexión en `POOL_TIMEOUT` segundos          |
| `UNKNOWN_ERROR`        | Error inesperado — revisar `docker logs extdb-plugin`                 |

---

## 15. Ejemplos por driver

### SQL Server

```json
{
  "connection_alias": "core_sqlserver",
  "mode":             "sql",
  "statement":        "SELECT id, nombre, saldo FROM cuentas WHERE cliente_id = ? AND activo = ?",
  "params":           [1042, 1]
}
```

```json
{
  "connection_alias": "core_sqlserver",
  "mode":             "callable",
  "statement":        "dbo.sp_calcular_riesgo_cliente",
  "params":           ["8-123-456", "2024-01-01"]
}
```

```json
{
  "connection_alias": "core_sqlserver",
  "mode":             "block",
  "statement":        "IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name='sync_log') CREATE TABLE sync_log (id INT IDENTITY PRIMARY KEY, msg NVARCHAR(500), creado_en DATETIME DEFAULT GETDATE())",
  "params":           []
}
```

---

### MySQL / MariaDB

```json
{
  "connection_alias": "core_mysql",
  "mode":             "sql",
  "statement":        "SELECT id, nombre FROM usuarios WHERE activo = ? AND tipo = ?",
  "params":           [1, "premium"]
}
```

```json
{
  "connection_alias": "core_mysql",
  "mode":             "callable",
  "statement":        "sp_resumen_ventas_mes",
  "params":           ["2024-03"]
}
```

---

### PostgreSQL

```json
{
  "connection_alias": "core_postgres",
  "mode":             "sql",
  "statement":        "SELECT id, sku, stock FROM productos WHERE categoria_id = ? AND stock > ?",
  "params":           [5, 0]
}
```

```json
{
  "connection_alias": "core_postgres",
  "mode":             "sql",
  "statement":        "INSERT INTO pedidos (cliente_id, total, estado) VALUES (?, ?, ?) RETURNING id",
  "params":           [42, 299.99, "pendiente"]
}
```

> **Funciones PostgreSQL:** usa modo `sql` con `SELECT nombre_funcion(?)` en lugar de `callable`.

---

## 16. Despliegue paso a paso

### Paso 1: Build de la imagen

```bash
cd apiquickserve-extdb-plugin/
docker build -t apiquickserve/extdb:1.4.0 -t apiquickserve/extdb:latest .
```

### Paso 2: Configurar docker-compose.yml

Edita al menos estas variables:

```yaml
environment:
  PLUGIN_API_KEY:  "genera_con_openssl_rand_hex_32"
  ENCRYPTION_KEY:  "genera_con_openssl_rand_hex_32"
```

Genera claves seguras:
```bash
openssl rand -hex 32
```

### Paso 3: Levantar

```bash
docker compose up -d
```

### Paso 4: Verificar

```bash
docker logs extdb-plugin
curl http://localhost:9000/health -H "X-API-Key: tu_clave"
```

### Paso 5: Registrar conexiones

```bash
curl -X POST http://localhost:9000/setup \
  -H "X-API-Key: tu_clave" \
  -H "Content-Type: application/json" \
  -d '{"alias":"mi_bd","driver":"sqlserver","host":"10.0.1.45","port":1433,"database":"MiBD","username":"user","password":"pass"}'
```

### Paso 6: Ejecutar queries

```bash
curl -X POST http://localhost:9000/execute \
  -H "X-API-Key: tu_clave" \
  -H "Content-Type: application/json" \
  -d '{"connection_alias":"mi_bd","mode":"sql","statement":"SELECT 1 AS test","params":[]}'
```

### Rebuild completo

```bash
docker compose down && docker build -t apiquickserve/extdb:1.4.0 -t apiquickserve/extdb:latest . && docker compose up -d && docker logs extdb-plugin
```

### Llamar desde ApiQuickServe (PL/SQL / ORDS)

```
http://extdb-plugin:8000/execute
```

---

## 17. Agregar un nuevo driver

1. Crear `app/drivers/oracle.py` heredando de `BaseDriver`
2. Agregar librería en `requirements.txt`
3. Registrar en `app/drivers/registry.py`
4. Agregar enum en `app/models/schemas.py`
5. Rebuild con bump de versión

---

## 18. Versionado

| Versión | Cambios principales                                                              |
|---------|----------------------------------------------------------------------------------|
| 1.0.0   | Versión inicial — SQL Server, conexión por request                               |
| 1.1.0   | Driver MySQL/MariaDB + autenticación X-API-Key                                   |
| 1.2.0   | Driver PostgreSQL + connection pooling + IP allowlist                            |
| 1.3.0   | Gestión de conexiones por alias (`/setup`) · validación SQL injection · seguridad hardening (docs desactivados, /health protegido, API Key enmascarada) |
| 1.4.0   | Encriptación AES-256-GCM de contraseñas en disco + migración automática         |

---

## 19. Seguridad

| Aspecto | Implementación |
|---|---|
| Autenticación | `X-API-Key` requerida en todos los endpoints. Comparación en tiempo constante (`secrets.compare_digest`). |
| Contraseñas en disco | AES-256-GCM con PBKDF2-HMAC-SHA256 (100k iteraciones). Nonce aleatorio por encriptación. |
| Contraseñas en tránsito | No viajan en los requests a `/execute` desde v1.3.0. Solo se registran una vez en `/setup` por HTTPS. |
| Contraseñas en logs | Nunca se loguean. El logger registra alias, driver, host, database — nunca username ni password. |
| API Key en logs | Solo los últimos 4 caracteres (`***xxxx`). |
| Documentación API | `/docs`, `/redoc` y `/openapi.json` desactivados. No expone el stack tecnológico. |
| IP Allowlist | Opcional. IPs no autorizadas reciben `HTTP 403` sin cuerpo. |
| Callable SQL injection | El nombre del stored procedure se valida con regex antes de ejecutar. |
| `ENCRYPTION_KEY` | Única pieza sensible restante. Recomendado: pasarla desde variable de entorno del host, no escribirla en archivos. |
