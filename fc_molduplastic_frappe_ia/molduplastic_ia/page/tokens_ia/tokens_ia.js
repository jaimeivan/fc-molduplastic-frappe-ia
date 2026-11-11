/* Tokens consumidos: detalle por día con los períodos compartidos del chat. */
frappe.pages["tokens-ia"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Tokens utilizados",
		single_column: true,
	});
	const API = "fc_molduplastic_frappe_ia.molduplastic_ia.api";
	const KEY = "ia-tokens-periodo";
	const KEY_ASEGURADORA = "ia-tokens-aseguradora";
	const nf = new Intl.NumberFormat("es-MX");
	let opcionesPeriodo = [];
	let esAdmin = false;
	let secuencia = 0;
	let seleccion = { clave: "este_mes", desde: "", hasta: "" };
	let aseguradora = "TODAS";

	const $body = page.body;
	$body.html(`
		<style>
			.ia-token-controls { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin: 8px 0 16px; }
			.ia-token-controls label { margin:0; color:var(--text-muted); }
			.ia-token-controls select, .ia-token-controls input { width:auto; min-width:180px; }
			#ia-token-dates { display:none; align-items:center; gap:6px; }
			#ia-token-range { color:var(--text-muted); font-size:12px; }
			.ia-token-summary { margin:12px 0; padding:16px; border:1px solid var(--border-color); border-radius:8px; background:var(--card-bg); }
			.ia-token-total { font-size:30px; font-weight:600; line-height:1.25; }
			.ia-token-subtitle { color:var(--text-muted); margin-top:4px; }
			#ia-token-chart { min-height:340px; margin:12px 0 20px; }
			.ia-token-table { width:100%; border-collapse:collapse; }
			.ia-token-table th, .ia-token-table td { border:1px solid var(--border-color); padding:6px 10px; text-align:right; }
			.ia-token-table th:first-child, .ia-token-table td:first-child { text-align:left; }
			.ia-token-table th { background:var(--bg-light-gray); }
			#ia-token-empty { color:var(--text-muted); padding:14px 0; }
		</style>
		<div class="ia-token-controls">
			<span id="ia-token-insurer-control" style="display:none">
				<label for="ia-token-insurer">Aseguradora</label>
				<select id="ia-token-insurer" class="form-control input-xs"></select>
			</span>
			<label for="ia-token-period">Período</label>
			<select id="ia-token-period" class="form-control input-xs"></select>
			<span id="ia-token-dates">
				<input type="date" id="ia-token-from" class="form-control input-xs">
				<span>a</span>
				<input type="date" id="ia-token-to" class="form-control input-xs">
			</span>
			<span id="ia-token-range"></span>
		</div>
		<div class="ia-token-summary">
			<div id="ia-token-total" class="ia-token-total">Cargando…</div>
			<div id="ia-token-subtitle" class="ia-token-subtitle"></div>
		</div>
		<div id="ia-token-chart"></div>
		<div id="ia-token-empty" style="display:none">No hay consumo de tokens en el período.</div>
		<div id="ia-token-table-wrap"></div>
	`);

	function leerSeleccion() {
		try {
			const valor = JSON.parse(localStorage.getItem(KEY) || "null");
			if (valor && valor.clave) seleccion = {
				clave: valor.clave,
				desde: valor.desde || "",
				hasta: valor.hasta || "",
			};
		} catch (e) { /* iniciar en el mes actual */ }
		try { aseguradora = localStorage.getItem(KEY_ASEGURADORA) || "TODAS"; } catch (e) { /* storage no disponible */ }
	}

	function guardarSeleccion() {
		try {
			localStorage.setItem(KEY, JSON.stringify(seleccion));
			localStorage.setItem(KEY_ASEGURADORA, aseguradora);
		} catch (e) { /* modo privado */ }
	}

	function fechaCorta(iso) {
		if (!iso) return "";
		const fecha = new Date(String(iso).slice(0, 10) + "T00:00:00");
		return isNaN(fecha.getTime()) ? String(iso) : fecha.toLocaleDateString("es-MX", {
			day: "numeric", month: "long", year: "numeric",
		});
	}

	function pintarRango() {
		if (seleccion.clave === "personalizado") {
			const desde = fechaCorta(seleccion.desde);
			const hasta = fechaCorta(seleccion.hasta);
			$body.find("#ia-token-range").text(desde && hasta ? `Del ${desde} al ${hasta}` : "Elige las fechas");
		} else {
			const opcion = opcionesPeriodo.find((x) => x.clave === seleccion.clave);
			$body.find("#ia-token-range").text(opcion ? opcion.rango_texto : "");
		}
	}

	function sincronizarControles() {
		const personalizado = seleccion.clave === "personalizado";
		$body.find("#ia-token-period").val(seleccion.clave);
		$body.find("#ia-token-dates").css("display", personalizado ? "inline-flex" : "none");
		$body.find("#ia-token-from").val(seleccion.desde);
		$body.find("#ia-token-to").val(seleccion.hasta);
		pintarRango();
		guardarSeleccion();
	}

	function pintarTabla(dias) {
		const $wrap = $body.find("#ia-token-table-wrap").empty();
		const activos = dias.filter((d) => d.total > 0);
		if (!activos.length) return;
		const $tabla = $("<table>").addClass("ia-token-table");
		const $thead = $("<thead>").appendTo($tabla);
		const $head = $("<tr>").appendTo($thead);
		["Día", "Entrada", "Salida", "Total"].forEach((texto) => $("<th>").text(texto).appendTo($head));
		const $tbody = $("<tbody>").appendTo($tabla);
		activos.forEach((d) => {
			const $fila = $("<tr>").appendTo($tbody);
			[$("<td>").text(fechaCorta(d.fecha)), d.entrada, d.salida, d.total].forEach((valor) => {
				if (valor && valor.jquery) valor.appendTo($fila);
				else $("<td>").text(nf.format(valor)).appendTo($fila);
			});
		});
		$wrap.append($tabla);
	}

	function cargarDatos() {
		if (seleccion.clave === "personalizado" && (!seleccion.desde || !seleccion.hasta || seleccion.desde > seleccion.hasta)) {
			$body.find("#ia-token-total").text("Selecciona un rango válido");
			$body.find("#ia-token-chart, #ia-token-table-wrap").empty();
			return;
		}
		const id = ++secuencia;
		const args = {
			periodo: seleccion.clave,
			desde: seleccion.desde || null,
			hasta: seleccion.hasta || null,
			aseguradora: esAdmin ? aseguradora : null,
		};
		$body.find("#ia-token-total").text("Cargando…");
		frappe.call(API + ".tokens_uso", args).then((r) => {
			if (id !== secuencia) return;
			const d = r.message || {};
			if (!d.ok) throw new Error("No se pudieron cargar los tokens.");
			$body.find("#ia-token-total").text(nf.format(d.total) + " tokens");
			$body.find("#ia-token-subtitle").text(
				`Entrada: ${nf.format(d.entrada)} · Salida: ${nf.format(d.salida)} · ${d.universo}`
			);
			$body.find("#ia-token-range").text(d.rango_texto || "");
			const tieneConsumo = d.total > 0;
			$body.find("#ia-token-empty").toggle(!tieneConsumo);
			const $chart = $body.find("#ia-token-chart").empty();
			if (tieneConsumo && frappe.Chart) {
				new frappe.Chart($chart.get(0), {
					data: {
						labels: d.dias.map((dia) => dia.etiqueta),
						datasets: [{ name: "Tokens", values: d.dias.map((dia) => dia.total) }],
					},
					type: "bar",
					title: "Tokens utilizados por día",
					height: 340,
				});
			}
			pintarTabla(d.dias || []);
		}).catch((error) => {
			if (id !== secuencia) return;
			$body.find("#ia-token-total").text(error.message || "No se pudieron cargar los tokens.");
			$body.find("#ia-token-chart, #ia-token-table-wrap").empty();
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
			const $select = $body.find("#ia-token-period").empty();
			opcionesPeriodo.forEach((p) => $("<option>").val(p.clave).text(p.etiqueta).appendTo($select));
			sincronizarControles();
		});
	}

	function configurarAseguradoras(d) {
		esAdmin = !!d.es_admin;
		if (!esAdmin) return;
		$body.find("#ia-token-insurer-control").show();
		const $select = $body.find("#ia-token-insurer").empty();
		$("<option>").val("TODAS").text("Todas las aseguradoras").appendTo($select);
		(d.aseguradoras || []).forEach((a) => $("<option>").val(a.name).text(a.nombre || a.name).appendTo($select));
		if (!["TODAS", ...(d.aseguradoras || []).map((a) => String(a.name))].includes(String(aseguradora))) {
			aseguradora = "TODAS";
		}
		$select.val(aseguradora);
	}

	$body.find("#ia-token-period").on("change", function () {
		const anterior = seleccion.clave;
		seleccion.clave = this.value;
		if (this.value === "personalizado" && !(seleccion.desde && seleccion.hasta)) {
			const base = opcionesPeriodo.find((p) => p.clave === anterior && p.desde && p.hasta)
				|| opcionesPeriodo.find((p) => p.clave === "este_mes");
			if (base) {
				seleccion.desde = String(base.desde).slice(0, 10);
				seleccion.hasta = String(base.hasta).slice(0, 10);
			}
		}
		sincronizarControles();
		cargarDatos();
	});
	$body.find("#ia-token-from, #ia-token-to").on("change", function () {
		seleccion.desde = $body.find("#ia-token-from").val() || "";
		seleccion.hasta = $body.find("#ia-token-to").val() || "";
		pintarRango();
		guardarSeleccion();
		cargarDatos();
	});
	$body.find("#ia-token-insurer").on("change", function () {
		aseguradora = this.value || "TODAS";
		guardarSeleccion();
		cargarDatos();
	});

	leerSeleccion();
	frappe.call(API + ".tokens_uso", { periodo: "este_mes" }).then((r) => {
		configurarAseguradoras(r.message || {});
		return cargarPeriodos();
	}).then(cargarDatos).catch(() => {
		$body.find("#ia-token-total").text("No se pudieron cargar los tokens.");
	});
};
