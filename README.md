# ApiQuickServe — External DB Plugin

Plugin stateless para conectividad con bases de datos externas desde ApiQuickServe.
Permite ejecutar DML y DDL contra SQL Server y MySQL/MariaDB directamente desde
ApiQuickServe, sin almacenar configuraciones ni credenciales.

---

## Principios de diseño

- **Stateless total**: no guarda conexiones, credenciales ni estado entre requests.
- **Credenciales en tránsito**: viajan en el body del request, nunca en URL ni headers.
- **Driver pattern**: agregar un nuevo motor de BD es agregar un módulo, sin tocar el core.
- **JSON normalizado**: la respuesta siempre tiene la misma estructura, sin importar el motor.
- **HTTP 200 siempre**: los errores de BD se reportan en el body (`status: "error"`), no como HTTP 4xx/5xx. Esto simplifica el manejo en PL/SQL.
- **Autenticación obligatoria**: el endpoint `/execute` requiere un `X-API-Key` configurado al momento de desplegar el contenedor.

---

## Estructura del proyecto

```
apiquickserve-extdb-plugin/
├── Dockerfile
├── requirements.txt
├── docker-compose.yml
└── app/
    ├── main.py                  # Aplicación FastAPI — endpoints
    ├── models/
    │   └── schemas.py           # Modelos Pydantic (request / response)
    ├── drivers/
    │   ├── base.py              # Clase base abstracta (contrato)
    │   ├── registry.py          # Registry de drivers disponibles
    │   ├── sqlserver.py         # Driver SQL Server (v1.0)
    │   ├── mysql.py             # Driver MySQL / MariaDB (v1.1)
    │   └── __init__.py
    └── core/
        ├── logging_config.py    # Configuración de logging
        └── security.py          # Validación de X-API-Key
```

---

## Autenticación

A partir de la **v1.1.0** el plugin requiere autenticación mediante el header `X-API-Key`
en todas las llamadas al endpoint `/execute`.

### Cómo funciona

- La API Key se define al momento de levantar el contenedor mediante la variable de
  entorno `PLUGIN_API_KEY`.
- Si la variable **no está definida**, el contenedor **no arranca** y emite un error claro.
- Al arrancar correctamente, la key queda visible en el log del contenedor (`docker logs`).
- La validación usa comparación en tiempo constante (`secrets.compare_digest`) para
  evitar timing attacks.
- El endpoint `/health` permanece **público** — no requiere autenticación.

### Ver la key en el log

```bash
docker logs extdb-plugin
```

Salida esperada al arrancar:
```
╔══════════════════════════════════════════════════════════════╗
║            ApiQuickServe — External DB Plugin                ║
╠══════════════════════════════════════════════════════════════╣
║  X-API-Key cargada correctamente.                            ║
║                                                              ║
║  PLUGIN_API_KEY → tu_clave_super_secreta                     ║
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

## Endpoints

### `GET /health`

Verifica que el plugin esté operativo. **No requiere autenticación.**

**Response:**
```json
{
  "status":  "ok",
  "version": "1.1.0",
  "drivers": ["sqlserver", "mysql"]
}
```

---

### `POST /execute`

Ejecuta una operación en la base de datos externa.

> **Requiere el header `X-API-Key`** con la clave configurada en `PLUGIN_API_KEY`.

**Headers requeridos:**

| Header      | Valor                        |
|-------------|------------------------------|
| `X-API-Key` | La clave definida en el `.env` o `-e PLUGIN_API_KEY` |

**Request body:**

| Campo                 | Tipo   | Requerido | Descripción                                              |
|-----------------------|--------|-----------|----------------------------------------------------------|
| `driver`              | string | Sí        | Motor: `"sqlserver"` \| `"mysql"`                        |
| `connection.host`     | string | Sí        | IP o hostname del servidor de BD                         |
| `connection.port`     | int    | Sí        | Puerto TCP (ej: 1433 / 3306)                             |
| `connection.database` | string | Sí        | Nombre de la base de datos                               |
| `connection.username` | string | Sí        | Usuario de conexión                                      |
| `connection.password` | string | Sí        | Contraseña                                               |
| `mode`                | string | Sí        | `"sql"` \| `"block"` \| `"callable"`                     |
| `statement`           | string | Sí        | Query, bloque T-SQL/SQL, o nombre de stored procedure    |
| `params`              | array  | No        | Parámetros posicionales, uno por `?` (default: `[]`)     |

**Modos de ejecución:**

| Modo       | Uso                                          | Parámetros         |
|------------|----------------------------------------------|--------------------|
| `sql`      | SELECT, INSERT, UPDATE, DELETE               | Posicionales `?`   |
| `block`    | T-SQL batch, DDL, bloques BEGIN/END          | No acepta          |
| `callable` | Nombre de stored procedure + argumentos      | Posicionales `?`   |

**Response exitoso:**
```json
{
  "status":        "ok",
  "rows_affected":  1,
  "columns":       ["id", "nombre", "ruc"],
  "data": [
    { "id": 1, "nombre": "Juan Pérez", "ruc": "8-123-456" }
  ],
  "execution_ms":  12,
  "error_code":    null,
  "error_message": null
}
```

**Response con error:**
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

**Códigos de error (`error_code`):**

| Código                 | Causa                                              |
|------------------------|----------------------------------------------------|
| `CONNECTION_FAILED`    | No se pudo abrir conexión al servidor              |
| `QUERY_FAILED`         | Error al ejecutar el statement (syntax, permisos)  |
| `UNSUPPORTED_MODE`     | El modo no es válido para este driver              |
| `DRIVER_NOT_AVAILABLE` | El driver solicitado no está en esta imagen        |
| `TIMEOUT`              | La conexión excedió el tiempo límite               |
| `UNKNOWN_ERROR`        | Error no controlado — revisar logs del contenedor  |

---

## Ejemplos de uso

### SELECT con parámetros — SQL Server
```json
{
  "driver": "sqlserver",
  "connection": {
    "host": "10.0.1.45", "port": 1433,
    "database": "CoreBancario",
    "username": "apireader", "password": "s3cret"
  },
  "mode":      "sql",
  "statement": "SELECT id, nombre, saldo FROM cuentas WHERE cliente_id = ? AND activo = ?",
  "params":    [1042, 1]
}
```
```http
X-API-Key: tu_clave_super_secreta
```

---

### SELECT con parámetros — MySQL
```json
{
  "driver": "mysql",
  "connection": {
    "host": "10.0.1.50", "port": 3306,
    "database": "mi_db",
    "username": "apireader", "password": "s3cret"
  },
  "mode":      "sql",
  "statement": "SELECT id, nombre FROM clientes WHERE ruc = ? AND activo = ? AND tipo = ?",
  "params":    ["8-123-456", 1, "natural"]
}
```

---

### INSERT
```json
{
  "driver": "mysql",
  "connection": { "..." : "..." },
  "mode":      "sql",
  "statement": "INSERT INTO clientes (ruc, nombre, tipo, activo) VALUES (?, ?, ?, ?)",
  "params":    ["8-123-456", "Juan Pérez", "natural", 1]
}
```

---

### DDL (bloque)
```json
{
  "driver": "sqlserver",
  "connection": { "..." : "..." },
  "mode":      "block",
  "statement": "IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name='temp_sync') CREATE TABLE temp_sync (id INT IDENTITY, payload NVARCHAR(MAX), created_at DATETIME DEFAULT GETDATE())",
  "params":    []
}
```

---

### Stored procedure
```json
{
  "driver": "sqlserver",
  "connection": { "..." : "..." },
  "mode":      "callable",
  "statement": "dbo.sp_calcular_riesgo_cliente",
  "params":    ["8-123-456", "2024-01-01"]
}
```

---

## Despliegue

### Build de la imagen
```bash
docker build -t apiquickserve/extdb:1.1.0 .
```

### Levantar el contenedor
```bash
docker run -d \
  --name extdb-plugin \
  --restart unless-stopped \
  -p 9000:8000 \
  --network oracle-ha-net \
  -e PLUGIN_API_KEY=tu_clave_super_secreta \
  apiquickserve/extdb:1.1.0
```

> ⚠️ Sin `-e PLUGIN_API_KEY=...` el contenedor no arranca.

### Comando completo (borrar + rebuild + levantar)
```bash
docker stop extdb-plugin && docker rm extdb-plugin && docker rmi apiquickserve/extdb:1.1.0 && docker build -t apiquickserve/extdb:1.1.0 . && docker run -d --name extdb-plugin --restart unless-stopped -p 9000:8000 --network oracle-ha-net -e PLUGIN_API_KEY=tu_clave_super_secreta apiquickserve/extdb:1.1.0 && docker logs extdb-plugin
```

### Con docker-compose
Editar `docker-compose.yml` con la key antes de ejecutar:
```yaml
environment:
  PLUGIN_API_KEY: "tu_clave_super_secreta"
```
```bash
docker compose down --rmi local && docker compose up -d --build && docker logs extdb-plugin
```

### Integrar en docker-compose existente de ApiQuickServe
Agregar el bloque `extdb-plugin` del `docker-compose.yml` incluido al compose principal
de ApiQuickServe. Asegurarse de que ambos servicios estén en la misma red Docker.

### Llamar desde ApiQuickServe (PL/SQL / ORDS)
```
http://extdb-plugin:8000/execute
```
El nombre `extdb-plugin` es el nombre del servicio en docker-compose.
Docker DNS lo resuelve automáticamente dentro de la red.

### Verificar estado
```bash
curl http://localhost:9000/health
```

### Ver documentación interactiva
```
http://localhost:9000/docs      (Swagger UI)
http://localhost:9000/redoc     (ReDoc)
```

---

## Agregar un nuevo driver (v1.2+)

1. Crear `app/drivers/postgresql.py` con `class PostgreSqlDriver(BaseDriver)`
2. Instalar el driver Python: agregar `psycopg2-binary` a `requirements.txt`
3. Registrarlo en `app/drivers/registry.py`:
   ```python
   from app.drivers.postgresql import PostgreSqlDriver
   DRIVER_REGISTRY[DriverType.postgresql] = PostgreSqlDriver
   ```
4. Activar el enum en `app/models/schemas.py`:
   ```python
   postgresql = "postgresql"
   ```
5. Rebuild y bump de versión: `apiquickserve/extdb:1.2.0`

---

## Versionado

| Versión | Drivers incluidos              | Notas                                     |
|---------|--------------------------------|-------------------------------------------|
| 1.0.0   | SQL Server                     | Versión inicial                           |
| 1.1.0   | SQL Server, MySQL / MariaDB    | Driver MySQL + autenticación X-API-Key    |
| 1.2.0   | + PostgreSQL                   | Planificado                               |
| 2.0.0   | Todos + connection pooling     | Planificado                               |

---

## Seguridad

- Las credenciales de BD **nunca se loguean**. El logger registra driver, modo, database,
  host y port — nunca username ni password.
- El endpoint `/execute` requiere **`X-API-Key`** en cada request. Sin ella devuelve
  `HTTP 401`. La validación usa `secrets.compare_digest` para evitar timing attacks.
- El endpoint `/health` es público para permitir health checks sin credenciales.
- La `PLUGIN_API_KEY` se define **al momento del despliegue** vía variable de entorno.
  Si no está definida, el contenedor no arranca.
- El plugin **no expone puertos al exterior** en configuración de producción.
  El puerto `9000` es opcional y solo para pruebas o monitoreo externo.
- `TrustServerCertificate=yes` está habilitado en SQL Server para compatibilidad con
  entornos sin CA corporativo. En producción con certificado válido, cambiar a `no`.
