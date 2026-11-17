### Molduplastic IA

Aplicación de frappe para Molduplastic para integrar funcionalidades relacionadas con IA

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app fc_molduplastic_frappe_ia
```

### Tiempos de espera del asistente IA

Una respuesta analítica puede tardar hasta 240 s. Cada capa que atiende la petición tiene que aguantar ese tiempo, o el chat muestra "Error de conexión" aunque el análisis haya terminado. Al mover la app a otro servidor, revisa estos valores en orden:

| Capa | Dónde se configura | Valor requerido | Hoy |
|---|---|---|---|
| Balanceador o proxy externo (si existe, p. ej. HAProxy) | Su configuración (`timeout server`, `timeout client`) | ≥ 240 s | No está en este servidor; revisar en el balanceador |
| nginx | `config/nginx.conf` del bench, bloque `location @webserver`: `proxy_read_timeout`, `proxy_send_timeout` | 240 | 240 / 240 |
| gunicorn | `config/supervisor.conf` (`-t`) y `http_timeout` en `sites/common_site_config.json` | ≥ 240 | `-t 6000` |
| Presupuesto del análisis | Molduplastic IA Settings > Presupuesto de tiempo del análisis | ≤ 240 | 240 |
| Reserva para la respuesta de respaldo | `RESERVA_RESPALDO_SEG` en `molduplastic_ia/api.py` | 35 s | 35 s |
| Llamada al modelo | Molduplastic IA Settings > Timeout LLM (tope duro 55 s en código) | ≤ 55 s | 55 s |

Notas:
- El análisis termina antes de `presupuesto − reserva` (205 s con el valor por defecto), así que el tiempo útil para el análisis es menor que 240 s. La reserva deja tiempo para la respuesta breve de respaldo.
- Si el balanceador externo corta antes (pasó el 2026-11-11: cerró la petición a los ~50 s con un 499 en nginx), el presupuesto del servidor no sirve de nada: hay que subir también ese límite, y no está en este servidor.
- El análisis solo corre en modo Analítico y solo para periodos de un mes o menos. Los rangos más largos (trimestre, cuatrimestre, año) responden en breve con un aviso.
- Después de cambiar nginx: `sudo nginx -t && sudo nginx -s reload`. Después de cambiar supervisor: `sudo supervisorctl reread && sudo supervisorctl update`.

### Base de datos del asistente

El asistente mide lo mismo que los tableros de BI (`VW_PIEZAS` / `vwPiezas`): el período de Pieza es **`fecha_hora_carga`** (fecha del lote de carga) y el universo es **`recolectable = 1 AND rutas_validas = '1'`**. La reconciliación es exacta: `SELECT COUNT(*) FROM vwPiezas WHERE Aseguradora = 'GNP' AND FechaIngreso BETWEEN …` debe coincidir con "piezas ingresadas" del asistente para ese mes.

Los índices de `tabPieza` se crean **fuera de `migrate`** y hay que recrearlos si se reinstala la BD; están documentados en `AGENTS.md` (sección "Rendimiento: índices de tabPieza").

Configuración pendiente en el host de BD (`mariadb-dev`, 2 GB de RAM): el buffer pool sigue en 128 MB. La recomendación es:

```ini
innodb_buffer_pool_size       = 1G
innodb_buffer_pool_instances  = 1
max_connections               = 50
tmp_table_size                = 64M
max_heap_table_size           = 64M
userstat                      = ON
```

Requiere reinicio de MariaDB (el usuario de BD de la app no tiene `SUPER` global, así que no se aplica desde la app). Con los índices cubrientes actuales la lectura ya es secuencial; el buffer pool es la mejora que queda.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/fc_molduplastic_frappe_ia
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
