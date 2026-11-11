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

Una respuesta analítica puede tardar hasta 120 s. Cada capa que atiende la petición tiene que aguantar ese tiempo, o el chat muestra "Error de conexión" aunque el análisis haya terminado. Al mover la app a otro servidor, revisa estos valores en orden:

| Capa | Dónde se configura | Valor requerido | Hoy |
|---|---|---|---|
| Balanceador o proxy externo (si existe, p. ej. HAProxy) | Su configuración (`timeout server`, `timeout client`) | ≥ 120 s | No está en este servidor; revisar en el balanceador |
| nginx | `config/nginx.conf` del bench, bloque `location @webserver`: `proxy_read_timeout`, `proxy_send_timeout` | 120 | 120 / 120 |
| gunicorn | `config/supervisor.conf` (`-t`) y `http_timeout` en `sites/common_site_config.json` | ≥ 120 | `-t 6000` |
| Presupuesto del análisis | Molduplastic IA Settings > Presupuesto de tiempo del análisis | ≤ 120 | 120 |
| Reserva para la respuesta de respaldo | `RESERVA_RESPALDO_SEG` en `molduplastic_ia/api.py` | 35 s | 35 s |
| Llamada al modelo | Molduplastic IA Settings > Timeout LLM (tope duro 55 s en código) | ≤ 55 s | 55 s |

Notas:
- El análisis termina antes de `presupuesto − reserva` (85 s con el valor por defecto), así que el tiempo útil para el análisis es menor que 120 s. La reserva deja tiempo para la respuesta breve de respaldo.
- El análisis solo corre en modo Analítico y solo para periodos de un mes o menos. Los rangos más largos (trimestre, cuatrimestre, año) responden en breve con un aviso.
- Después de cambiar nginx: `sudo nginx -t && sudo nginx -s reload`. Después de cambiar supervisor: `sudo supervisorctl reread && sudo supervisorctl update`.

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
