"""Pruebas de la app. Con MOLDUPLASTIC_EVIDENCIA=1 se registra cada pregunta de las pruebas."""

import os

if os.environ.get("MOLDUPLASTIC_EVIDENCIA"):
	from fc_molduplastic_frappe_ia.molduplastic_ia.tests import evidencia

	evidencia.instalar_captura()
