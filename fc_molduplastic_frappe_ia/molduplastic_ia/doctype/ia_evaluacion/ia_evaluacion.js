frappe.ui.form.on("IA Evaluacion", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Preguntar al asistente"), () => {
			frappe.call({
				method: "fc_molduplastic_frappe_ia.molduplastic_ia.evaluacion.preguntar_evaluacion",
				args: { name: frm.doc.name },
				freeze: true,
				freeze_message: __("Consultando al asistente..."),
			}).then(() => frm.reload_doc());
		});
	},
});
