/* IA Calificacion: revisión de una respuesta calificada y replicación de la
   consulta guardada en el mensaje enlazado (sin volver a llamar al modelo). */

function celdaReplicada(valor) {
	const texto = valor === null || valor === undefined || valor === "" ? "Sin dato" : String(valor);
	return frappe.utils.escape_html(texto);
}

function tablaHtml(columnas, filas) {
	let html = '<table class="table table-bordered table-condensed"><thead><tr>';
	(columnas || []).forEach((c) => {
		html += "<th>" + celdaReplicada(c) + "</th>";
	});
	html += "</tr></thead><tbody>";
	(filas || []).forEach((fila) => {
		html += "<tr>";
		(fila || []).forEach((v) => {
			html += "<td>" + celdaReplicada(v) + "</td>";
		});
		html += "</tr>";
	});
	return html + "</tbody></table>";
}

function tablaReplicada(d) {
	const meta = [d.pregunta, d.universo, d.periodo].filter(Boolean).join(" · ");
	let html = meta
		? '<p class="text-muted small">' + frappe.utils.escape_html(meta) + "</p>"
		: "";

	html += "<h5>Respuesta original</h5>";
	if (d.original && (d.original.columnas || []).length) {
		html += tablaHtml(d.original.columnas, d.original.filas);
	} else {
		html +=
			'<div style="white-space:pre-wrap;max-height:220px;overflow:auto;' +
			'border:1px solid var(--border-color);border-radius:6px;padding:8px">' +
			frappe.utils.escape_html(d.respuesta || "Sin texto.") +
			"</div>";
	}

	html += '<h5 class="mt-3">Consulta replicada</h5>';
	if (!(d.columnas || []).length) {
		html += '<p class="text-muted">La consulta no devolvió datos.</p>';
	} else {
		html += tablaHtml(d.columnas, d.filas);
		const mostradas = (d.filas || []).length;
		if ((d.total_filas || 0) > mostradas) {
			html +=
				'<p class="text-muted small">Se muestran las primeras ' +
				mostradas +
				" de " +
				d.total_filas +
				" filas.</p>";
		}
	}
	html +=
		'<p class="text-muted small">La consulta se vuelve a ejecutar; el texto de la ' +
		"respuesta no se regenera.</p>";
	return html;
}

frappe.ui.form.on("IA Calificacion", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Replicar respuesta"), () => {
			frappe.call({
				method: "fc_molduplastic_frappe_ia.molduplastic_ia.api.replicar_calificacion",
				args: { nombre: frm.doc.name },
				freeze: true,
				freeze_message: __("Replicando la respuesta..."),
			}).then((r) => {
				const d = r.message || {};
				if (!d.ok) {
					frappe.msgprint({
						title: __("No se pudo replicar"),
						message: d.mensaje || __("Intenta de nuevo."),
						indicator: "orange",
					});
					return;
				}
				const dlg = new frappe.ui.Dialog({
					title: __("Respuesta replicada"),
					size: "large",
					fields: [{ fieldtype: "HTML", fieldname: "tabla" }],
					primary_action_label: __("Cerrar"),
					primary_action() {
						dlg.hide();
					},
				});
				dlg.fields_dict.tabla.$wrapper.html(tablaReplicada(d));
				dlg.show();
			});
		});
	},
});
