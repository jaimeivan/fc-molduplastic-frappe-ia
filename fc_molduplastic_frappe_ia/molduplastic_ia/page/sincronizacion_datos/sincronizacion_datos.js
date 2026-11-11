frappe.pages["sincronizacion-datos"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Sincronización de datos",
		single_column: true,
	});
	const API = "fc_molduplastic_frappe_ia.molduplastic_ia.sincronizacion";
	const $body = page.body;
	let polling = false;

	page.set_primary_action("Sincronizar ahora", iniciar);
	$body.html(`
		<style>
			.ia-sync-note { margin: 8px 0 16px; color: var(--text-muted); }
			.ia-sync-table { width:100%; border-collapse:collapse; margin:12px 0 24px; }
			.ia-sync-table th, .ia-sync-table td {
				border:1px solid var(--border-color); padding:7px 10px; vertical-align:top;
			}
			.ia-sync-table th { background:var(--bg-light-gray); text-align:left; }
			.ia-sync-detail-row td { background:var(--control-bg); }
			.ia-sync-detail-table { width:100%; border-collapse:collapse; background:var(--card-bg); }
			.ia-sync-detail-table th, .ia-sync-detail-table td {
				border:1px solid var(--border-color); padding:5px 8px; text-align:left;
			}
			.ia-sync-detail-table th { background:var(--bg-light-gray); }
			.ia-sync-details { margin:6px 0 0; color:var(--text-muted); font-size:12px; }
			.ia-sync-error { color:var(--red-500); white-space:pre-wrap; }
			.ia-sync-empty { padding:18px 0; color:var(--text-muted); }
			@media (max-width: 900px) {
				.ia-sync-table, .ia-sync-detail-table { display:block; overflow-x:auto; white-space:nowrap; }
			}
		</style>
		<p class="ia-sync-note">Copia datos desde producción por lotes. No elimina registros del destino.
			La primera ejecución hace una carga inicial; las siguientes usan cambios recientes.</p>
		<div id="ia-sync-status" class="text-muted">Cargando historial…</div>
		<div id="ia-sync-log"></div>
	`);

	function numero(value) {
		return new Intl.NumberFormat("es-MX").format(Number(value || 0));
	}

	function fecha(value) {
		if (!value) return "—";
		const parsed = new Date(String(value).replace(" ", "T"));
		return isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString("es-MX");
	}

	function celda($row, value, tag) {
		$(`<${tag || "td"}>`).text(value == null ? "—" : value).appendTo($row);
	}

	function duracion(value) {
		let seconds = Math.max(0, Math.floor(Number(value || 0)));
		const hours = Math.floor(seconds / 3600);
		seconds %= 3600;
		const minutes = Math.floor(seconds / 60);
		seconds %= 60;
		const parts = [];
		if (hours) parts.push(`${hours} h`);
		if (minutes) parts.push(`${minutes} min`);
		if (seconds || !parts.length) parts.push(`${seconds} s`);
		return parts.join(" ");
	}

	function pintarDetalle($container, detalle) {
		if (!detalle || !detalle.length) {
			$container.text("Sin detalle disponible.");
			return;
		}
		const $table = $('<table class="ia-sync-detail-table">').appendTo($container);
		const $head = $("<tr>").appendTo($("<thead>").appendTo($table));
		["Tabla", "Estado", "Procesados", "Nuevos", "Actualizados", "Lotes", "Método", "Vistas"]
			.forEach((label) => celda($head, label, "th"));
		const $body = $("<tbody>").appendTo($table);
		detalle.forEach((table) => {
			const $row = $("<tr>").appendTo($body);
			celda($row, table.tabla);
			celda($row, table.estado || "Pendiente");
			celda($row, numero(table.procesados));
			celda($row, numero(table.insertados));
			celda($row, numero(table.actualizados));
			celda($row, numero(table.lotes));
			celda($row, table.modo || "—");
			celda($row, (table.vistas || []).join(", ") || "—");
			if (table.nota) {
				const $noteRow = $("<tr>").appendTo($body);
				$noteRow.append($(`<td colspan="8">`).addClass("ia-sync-details").text(table.nota));
			}
		});
	}

	function render(detalles) {
		const $wrap = $body.find("#ia-sync-log").empty();
		if (!detalles.length) {
			$wrap.append($('<div class="ia-sync-empty">').text("Todavía no hay sincronizaciones."));
			return;
		}
		const $table = $('<table class="ia-sync-table">').appendTo($wrap);
		const $thead = $("<thead>").appendTo($table);
		const $head = $("<tr>").appendTo($thead);
		["Solicitada", "Estado", "Inicio", "Fin", "Duración", "Registros"].forEach((x) => celda($head, x, "th"));
		const $tbody = $("<tbody>").appendTo($table);
		detalles.forEach((run) => {
			const $row = $("<tr>").appendTo($tbody);
			celda($row, fecha(run.fecha_solicitud));
			celda($row, run.estado);
			celda($row, fecha(run.inicio));
			celda($row, fecha(run.fin));
			celda($row, run.duracion_segundos == null ? "—" : duracion(run.duracion_segundos));
			celda($row, numero(run.registros_sincronizados));
			const $detailRow = $('<tr class="ia-sync-detail-row">').appendTo($tbody);
			const $detail = $("<td>").attr("colspan", 6).appendTo($detailRow);
			pintarDetalle($detail, run.detalle || []);
			if (run.error) $detail.append($('<div class="ia-sync-error">').text(run.error));
		});
	}

	function cargar() {
		return frappe.call(`${API}.listar_sincronizaciones`, { limite: 30 }).then((r) => {
			const runs = (r.message || {}).sincronizaciones || [];
			render(runs);
			const active = runs.some((x) => ["En cola", "En curso"].includes(x.estado));
			$body.find("#ia-sync-status").text(active
				? "Sincronización en cola o en curso; el historial se actualiza automáticamente."
				: "Historial actualizado."
			);
			if (active && !polling) {
				polling = true;
				setTimeout(() => { polling = false; cargar(); }, 5000);
			}
			return runs;
		}).catch((error) => {
			$body.find("#ia-sync-status").text(error.message || "No se pudo cargar el historial.");
		});
	}

	function iniciar() {
		let confirmacion;
		confirmacion = frappe.confirm(
			"¿Iniciar la sincronización manual desde producción? " +
				"La primera ejecución puede tardar debido al volumen de Piezas.",
			() => {
				confirmacion.hide();
				page.set_primary_action("Enviando…", null).prop("disabled", true);
				frappe.call(`${API}.iniciar_sincronizacion`).then((r) => {
					const result = r.message || {};
					if (!result.ok) frappe.msgprint(result.mensaje || "Ya existe una sincronización activa.");
					else frappe.show_alert({ message: result.mensaje, indicator: "green" });
					cargar();
				}).catch((error) => {
					frappe.msgprint(error.message || "No se pudo iniciar la sincronización.");
				}).finally(() => page.set_primary_action("Sincronizar ahora", iniciar));
			}
		);
	}

	cargar();
};
