/* Uso de las preguntas sugeridas del análisis: ofertas, clics y éxito por modo. */
frappe.pages["sugerencias-ia"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Uso de sugerencias",
		single_column: true,
	});
	const API = "fc_molduplastic_frappe_ia.molduplastic_ia.api";
	const KEY = "ia-sugerencias-periodo";
	const nf = new Intl.NumberFormat("es-MX");
	let opcionesPeriodo = [];
	let secuencia = 0;
	let seleccion = { clave: "este_mes", desde: "", hasta: "" };

	const $body = page.body;
	$body.html(`
		<style>
			.ia-sug-controls { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin: 8px 0 16px; }
			.ia-sug-controls label { margin:0; color:var(--text-muted); }
			.ia-sug-controls select, .ia-sug-controls input { width:auto; min-width:180px; }
			#ia-sug-dates { display:none; align-items:center; gap:6px; }
			#ia-sug-range { color:var(--text-muted); font-size:12px; }
			.ia-sug-summary { display:flex; gap:24px; flex-wrap:wrap; margin:12px 0; padding:16px; border:1px solid var(--border-color); border-radius:8px; background:var(--card-bg); }
			.ia-sug-metric { min-width:140px; }
			.ia-sug-metric .valor { font-size:26px; font-weight:600; line-height:1.2; }
			.ia-sug-metric .etiqueta { color:var(--text-muted); font-size:12px; }
			#ia-sug-chart { min-height:320px; margin:12px 0 20px; }
			.ia-sug-table { width:100%; border-collapse:collapse; }
			.ia-sug-table th, .ia-sug-table td { border:1px solid var(--border-color); padding:6px 10px; text-align:right; }
			.ia-sug-table th:first-child, .ia-sug-table td:first-child { text-align:left; }
			.ia-sug-table th { background:var(--bg-light-gray); }
			#ia-sug-empty { color:var(--text-muted); padding:14px 0; }
			#ia-sug-error { color:var(--red-600); padding:10px 0; }
		</style>
		<div class="ia-sug-controls">
			<label for="ia-sug-period">Período</label>
			<select id="ia-sug-period" class="form-control input-xs"></select>
			<span id="ia-sug-dates">
				<input type="date" id="ia-sug-from" class="form-control input-xs">
				<span>a</span>
				<input type="date" id="ia-sug-to" class="form-control input-xs">
			</span>
			<span id="ia-sug-range"></span>
		</div>
		<div class="ia-sug-summary">
			<div class="ia-sug-metric"><div id="ia-sug-ofertas" class="valor">—</div><div class="etiqueta">Sugerencias ofrecidas</div></div>
			<div class="ia-sug-metric"><div id="ia-sug-clics" class="valor">—</div><div class="etiqueta">Clics</div></div>
			<div class="ia-sug-metric"><div id="ia-sug-ctr" class="valor">—</div><div class="etiqueta">CTR</div></div>
			<div class="ia-sug-metric"><div id="ia-sug-exito" class="valor">—</div><div class="etiqueta">Turnos resueltos</div></div>
		</div>
		<div id="ia-sug-error" style="display:none"></div>
		<div id="ia-sug-chart"></div>
		<div id="ia-sug-empty" style="display:none">No hay sugerencias registradas en el período.</div>
		<div id="ia-sug-table-wrap"></div>
	`);

	function leerSeleccion() {
		try {
			const valor = JSON.parse(localStorage.getItem(KEY) || "null");
			if (valor && valor.clave) {
				seleccion = { clave: valor.clave, desde: valor.desde || "", hasta: valor.hasta || "" };
			}
		} catch (e) {
			/* iniciar en el mes actual */
		}
	}

	function guardarSeleccion() {
		try {
			localStorage.setItem(KEY, JSON.stringify(seleccion));
		} catch (e) {
			/* modo privado */
		}
	}

	function fechaCorta(iso) {
		if (!iso) return "";
		const fecha = new Date(String(iso).slice(0, 10) + "T00:00:00");
		return isNaN(fecha.getTime())
			? String(iso)
			: fecha.toLocaleDateString("es-MX", { day: "numeric", month: "long", year: "numeric" });
	}

	function pintarRango() {
		if (seleccion.clave === "personalizado") {
			const desde = fechaCorta(seleccion.desde);
			const hasta = fechaCorta(seleccion.hasta);
			$body.find("#ia-sug-range").text(desde && hasta ? `Del ${desde} al ${hasta}` : "Elige las fechas");
			return;
		}
		const opcion = opcionesPeriodo.find((x) => x.clave === seleccion.clave);
		$body.find("#ia-sug-range").text(opcion ? opcion.rango_texto : "");
	}

	function sincronizarControles() {
		const personalizado = seleccion.clave === "personalizado";
		$body.find("#ia-sug-period").val(seleccion.clave);
		$body.find("#ia-sug-dates").css("display", personalizado ? "inline-flex" : "none");
		$body.find("#ia-sug-from").val(seleccion.desde);
		$body.find("#ia-sug-to").val(seleccion.hasta);
		pintarRango();
		guardarSeleccion();
	}

	function numero(valor) {
		return valor === null || valor === undefined ? "—" : nf.format(valor);
	}

	function porcentaje(valor) {
		return valor === null || valor === undefined ? "—" : `${Number(valor).toFixed(1)}%`;
	}

	function pintarTabla(modos) {
		const $wrap = $body.find("#ia-sug-table-wrap").empty();
		const filas = Object.keys(modos || {}).map((modo) => ({ modo, ...modos[modo] }));
		if (!filas.length) return;
		const $tabla = $("<table>").addClass("ia-sug-table");
		const $head = $("<tr>").appendTo($("<thead>").appendTo($tabla));
		[
			"Modo",
			"Ofertas",
			"Clics",
			"CTR",
			"Turnos",
			"Resueltos",
			"Éxito",
			"Fallback",
			"Sin respaldo",
			"Tokens",
			"Latencia media",
		].forEach((texto) => $("<th>").text(texto).appendTo($head));
		const $tbody = $("<tbody>").appendTo($tabla);
		filas.forEach((f) => {
			const $tr = $("<tr>").appendTo($tbody);
			[
				f.modo,
				numero(f.ofertas),
				numero(f.clics),
				porcentaje(f.ctr),
				numero(f.turnos),
				numero(f.resueltos),
				porcentaje(f.tasa_exito),
				numero(f.con_fallback),
				numero(f.con_cifras_sin_respaldo),
				numero(f.tokens),
				f.latencia_promedio_ms ? `${numero(f.latencia_promedio_ms)} ms` : "—",
			].forEach((valor) => $("<td>").text(valor).appendTo($tr));
		});
		$wrap.append($tabla);
	}

	function cargarDatos() {
		if (
			seleccion.clave === "personalizado" &&
			(!seleccion.desde || !seleccion.hasta || seleccion.desde > seleccion.hasta)
		) {
			$body.find("#ia-sug-error").show().text("Selecciona un rango válido.");
			return;
		}
		const id = ++secuencia;
		$body.find("#ia-sug-error").hide().text("");
		frappe.call(API + ".sugerencias_uso", {
			periodo: seleccion.clave,
			desde: seleccion.desde || null,
			hasta: seleccion.hasta || null,
		}).then((r) => {
			if (id !== secuencia) return;
			const d = r.message || {};
			const modos = d.modos || {};
			const totales = d.totales || {};
			$body.find("#ia-sug-ofertas").text(numero(totales.ofertas));
			$body.find("#ia-sug-clics").text(numero(totales.clics));
			$body.find("#ia-sug-ctr").text(
				porcentaje(totales.ofertas ? (totales.clics / totales.ofertas) * 100 : 0)
			);
			$body.find("#ia-sug-exito").text(
				`${numero(totales.resueltos)} / ${numero(totales.turnos)}`
			);
			$body.find("#ia-sug-range").text(
				d.rango
					? `Del ${fechaCorta(d.rango.desde)} al ${fechaCorta(d.rango.hasta)}`
					: $body.find("#ia-sug-range").text()
			);
			const modosLista = Object.keys(modos);
			const hayDatos = totales.ofertas > 0 || totales.clics > 0;
			$body.find("#ia-sug-empty").toggle(!hayDatos);
			const $chart = $body.find("#ia-sug-chart").empty();
			if (hayDatos && frappe.Chart) {
				new frappe.Chart($chart.get(0), {
					data: {
						labels: modosLista,
						datasets: [
							{ name: "Ofertas", values: modosLista.map((m) => modos[m].ofertas || 0) },
							{ name: "Clics", values: modosLista.map((m) => modos[m].clics || 0) },
						],
					},
					type: "bar",
					title: "Sugerencias ofrecidas y clics por modo",
					height: 320,
				});
			}
			pintarTabla(modos);
		}).catch((error) => {
			if (id !== secuencia) return;
			const mensaje =
				(error && error.message) ||
				"No se pudo cargar el uso de sugerencias. Solo un administrador puede verlo.";
			$body.find("#ia-sug-error").show().text(mensaje);
			$body.find("#ia-sug-chart, #ia-sug-table-wrap").empty();
		});
	}

	function cargarPeriodos() {
		return frappe.call(API + ".periodos").then((r) => {
			const d = r.message || {};
			opcionesPeriodo = d.periodos || [];
			if (!opcionesPeriodo.some((p) => p.clave === seleccion.clave)) {
				seleccion = { clave: d.defecto || "este_mes", desde: "", hasta: "" };
			}
			if (seleccion.clave === "personalizado" && !(seleccion.desde && seleccion.hasta)) {
				seleccion = { clave: d.defecto || "este_mes", desde: "", hasta: "" };
			}
			const $select = $body.find("#ia-sug-period").empty();
			opcionesPeriodo.forEach((p) => $("<option>").val(p.clave).text(p.etiqueta).appendTo($select));
			sincronizarControles();
		});
	}

	$body.find("#ia-sug-period").on("change", function () {
		const anterior = seleccion.clave;
		seleccion.clave = this.value;
		if (this.value === "personalizado" && !(seleccion.desde && seleccion.hasta)) {
			const base =
				opcionesPeriodo.find((p) => p.clave === anterior && p.desde && p.hasta) ||
				opcionesPeriodo.find((p) => p.clave === "este_mes");
			if (base) {
				seleccion.desde = String(base.desde).slice(0, 10);
				seleccion.hasta = String(base.hasta).slice(0, 10);
			}
		}
		sincronizarControles();
		cargarDatos();
	});
	$body.find("#ia-sug-from, #ia-sug-to").on("change", function () {
		seleccion.desde = $body.find("#ia-sug-from").val() || "";
		seleccion.hasta = $body.find("#ia-sug-to").val() || "";
		pintarRango();
		guardarSeleccion();
		cargarDatos();
	});

	leerSeleccion();
	cargarPeriodos().then(cargarDatos).catch(() => {
		$body.find("#ia-sug-error").show().text("No se pudo cargar el uso de sugerencias.");
	});
};
