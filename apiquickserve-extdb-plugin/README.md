# ApiQuickServe — External DB Plugin

Plugin stateless para conectividad con bases de datos externas desde ApiQuickServe.
Permite ejecutar DML y DDL contra SQL Server (y otros motores en versiones futuras)
directamente desde ApiQuickServe, sin almacenar configuraciones ni credenciales.

---

## Principios de diseño

- **Stateless total**: no guarda conexiones, credenciales ni estado entre requests.
- **Credenciales en tránsito**: viajan en el body del request, nunca en URL ni headers.
- **Driver pattern**: agregar un nuevo motor de BD es agregar un módulo, sin tocar el core.
- **JSON normalizado**: la respuesta siempre tiene la misma estructura, sin importar el motor.
- **HTTP 200 siempre**: los errores de BD se reportan en el body (`status: "error"`), no como HTTP 4xx/5xx. Esto simplifica el manejo en PL/SQL.

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
    │   └── __init__.py
    └── core/
        └── logging_config.py    # Configuración de logging
```

---

## Endpoints

### `GET /health`

Verifica que el plugin esté operativo.

**Response:**
```json
{
  "status":  "ok",
  "version": "1.0.0",
  "drivers": ["sqlserver"]
}
```

---

### `POST /execute`

Ejecuta una operación en la base de datos externa.

**Request body:**

| Campo              | Tipo     | Requerido | Descripción                                      |
|--------------------|----------|-----------|--------------------------------------------------|
| `driver`           | string   | Sí        | Motor: `"sqlserver"` (v1.0)                      |
| `connection.host`  | string   | Sí        | IP o hostname del servidor de BD                 |
| `connection.port`  | int      | Sí        | Puerto TCP (ej: 1433)                            |
| `connection.database` | string | Sí      | Nombre de la base de datos                       |
| `connection.username` | string | Sí      | Usuario de conexión                              |
| `connection.password` | string | Sí      | Contraseña                                       |
| `mode`             | string   | Sí        | `"sql"` \| `"block"` \| `"callable"`             |
| `statement`        | string   | Sí        | Query, bloque T-SQL, o nombre de SP              |
| `params`           | array    | No        | Parámetros posicionales (default: `[]`)          |

**Modos de ejecución:**

| Modo       | Uso                                          | Parámetros         |
|------------|----------------------------------------------|--------------------|
| `sql`      | SELECT, INSERT, UPDATE, DELETE               | Posicionales `?`   |
| `block`    | T-SQL batch, DDL, bloques BEGIN/END          | No acepta          |
| `callable` | EXEC stored_procedure nombre + argumentos    | Posicionales `?`   |

**Response exitoso:**
```json
{
  "status":       "ok",
  "rows_affected": 1,
  "columns":      ["id", "nombre", "ruc"],
  "data": [
    { "id": 1, "nombre": "Juan Pérez", "ruc": "8-123-456" }
  ],
  "execution_ms": 12,
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

| Código                | Causa                                              |
|-----------------------|----------------------------------------------------|
| `CONNECTION_FAILED`   | No se pudo abrir conexión al servidor              |
| `QUERY_FAILED`        | Error al ejecutar el statement (syntax, permisos)  |
| `UNSUPPORTED_MODE`    | El modo no es válido para este driver              |
| `DRIVER_NOT_AVAILABLE`| El driver solicitado no está en esta imagen        |
| `TIMEOUT`             | La conexión excedió el tiempo límite               |
| `UNKNOWN_ERROR`       | Error no controlado — revisar logs del contenedor  |

---

## Ejemplos de uso

### SELECT con parámetros
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

### INSERT
```json
{
  "driver": "sqlserver",
  "connection": { ... },
  "mode":      "sql",
  "statement": "INSERT INTO log_accesos (usuario, ip, fecha) VALUES (?, ?, GETDATE())",
  "params":    ["daniel.ramirez", "192.168.1.10"]
}
```

### DDL (bloque)
```json
{
  "driver": "sqlserver",
  "connection": { ... },
  "mode":      "block",
  "statement": "IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name='temp_sync') CREATE TABLE temp_sync (id INT IDENTITY, payload NVARCHAR(MAX), created_at DATETIME DEFAULT GETDATE())",
  "params":    []
}
```

### Stored procedure
```json
{
  "driver": "sqlserver",
  "connection": { ... },
  "mode":      "callable",
  "statement": "dbo.sp_calcular_riesgo_cliente",
  "params":    ["8-123-456", "2024-01-01"]
}
```

---

## Despliegue

### Build de la imagen
```bash
docker build -t apiquickserve/extdb:1.0.0 .
```

### Publicar en registry
```bash
docker push apiquickserve/extdb:1.0.0
```

### Integrar en docker-compose existente
Agregar el bloque `extdb-plugin` del `docker-compose.yml` incluido
al compose principal de ApiQuickServe. Asegurarse de que ambos servicios
estén en la misma red Docker (`aqsnet` o el nombre que uses).

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

## Agregar un nuevo driver (v1.1+)

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
5. Rebuild y bump de versión: `apiquickserve/extdb:1.1.0`

---

## Versionado

| Versión | Drivers incluidos           | Notas                        |
|---------|-----------------------------|------------------------------|
| 1.0.0   | SQL Server                  | Versión inicial              |
| 1.1.0   | SQL Server, PostgreSQL      | Planificado                  |
| 1.2.0   | + MySQL / MariaDB           | Planificado                  |
| 2.0.0   | Todos + connection pooling  | Planificado                  |

---

## Seguridad

- Las credenciales **nunca se loguean**. El logger registra driver, modo, database, host, port — no username ni password.
- El plugin **no expone puertos al exterior** en configuración de producción. El puerto 9000 es opcional y solo para pruebas.
- `TrustServerCertificate=yes` está habilitado para compatibilidad con entornos sin CA corporativo. En producción con certificado válido, cambiar a `no`.
- El control de autorización (quién puede llamar al plugin y con qué permisos) es responsabilidad de ApiQuickServe, no del plugin.
