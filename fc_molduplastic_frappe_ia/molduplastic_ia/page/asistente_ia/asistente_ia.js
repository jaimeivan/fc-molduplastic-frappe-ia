/* Asistente IA Molduplastic: chat + historial + reportes + Frappe Charts.
   JS plano (sin frameworks): el Desk v15 no expone Vue global. */

frappe.pages["asistente-ia"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Asistente IA",
		single_column: true,
	});

	const API = "fc_molduplastic_frappe_ia.molduplastic_ia.api";
	const state = { conv: null, vista: "chat", cargando: false, ultimo: null, universo: null, pendiente: null };

	$(`
		<style>
			#ia-app { display: flex; gap: 12px; height: calc(100vh - 220px); min-height: 480px; }
			#ia-side { width: 260px; border-right: 1px solid var(--border-color); padding-right: 12px; overflow-y: auto; }
			#ia-main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
			#ia-msgs { flex: 1; overflow-y: auto; padding: 8px 4px; }
			.ia-msg { margin-bottom: 12px; max-width: 95%; }
			.ia-user { margin-left: auto; background: var(--bg-blue); border-radius: 8px; padding: 8px 12px; width: fit-content; }
			.ia-bot { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 8px; padding: 8px 12px; }
			.ia-cob { font-size: 12px; color: var(--text-muted); margin-top: 6px; }
			.ia-tbl { width: 100%; font-size: 12px; margin-top: 6px; border-collapse: collapse; }
			.ia-tbl th, .ia-tbl td { border: 1px solid var(--border-color); padding: 4px 8px; text-align: left; }
			.ia-actions { margin-top: 6px; display: flex; gap: 8px; }
			#ia-input-row { display: flex; gap: 8px; margin-top: 8px; }
			.ia-conv { cursor: pointer; padding: 6px 8px; border-radius: 6px; }
			.ia-conv:hover, .ia-conv.active { background: var(--bg-light-gray); }
			.ia-pre { white-space: pre-wrap; }
			.ia-loader span { display: inline-block; width: 7px; height: 7px; margin-right: 4px; border-radius: 50%; background: var(--text-muted); animation: ia-bounce 1.2s infinite; }
			.ia-loader span:nth-child(2) { animation-delay: 0.15s; }
			.ia-loader span:nth-child(3) { animation-delay: 0.3s; }
			@keyframes ia-bounce { 0%, 60%, 100% { transform: translateY(0); opacity: 0.4; } 30% { transform: translateY(-5px); opacity: 1; } }
		</style>
		<div id="ia-app">
			<div id="ia-side">
				<button id="ia-nueva" class="btn btn-primary btn-sm btn-block">+ Nueva conversación</button>
				<div style="margin:8px 0; display:flex; gap:4px">
					<button id="ia-tab-chat" class="btn btn-primary btn-xs">Chat</button>
					<button id="ia-tab-rep" class="btn btn-default btn-xs">Reportes</button>
				</div>
				<div id="ia-lista"></div>
			</div>
			<div id="ia-main">
				<div id="ia-universo-bar" style="display:none; margin-bottom:6px">
					<span class="label label-info" id="ia-universo-txt"></span>
					<button id="ia-universo-cambiar" class="btn btn-default btn-xs">Cambiar</button>
				</div>
				<div id="ia-msgs"></div>
				<div id="ia-input-row">
					<input id="ia-pregunta" class="form-control" placeholder="Pregunta sobre piezas, recolecciones o talleres...">
					<button id="ia-enviar" class="btn btn-primary">Enviar</button>
				</div>
			</div>
		</div>
	`).appendTo(page.body);

	const $msgs = page.body.find("#ia-msgs");
	const $lista = page.body.find("#ia-lista");
	const $input = page.body.find("#ia-pregunta");

	const esc = (s) => String(s == null ? "" : s);

	function textoCobertura(cob) {
		if (typeof cob === "string") return cob;
		if (cob && cob.texto) return cob.texto;
		if (!cob || !cob.universo) return "Sin datos en el alcance de tu aseguradora.";
		let t = `Universo: ${cob.universo} registros de tu aseguradora.`;
		for (const [campo, d] of Object.entries(cob.campos || {})) {
			t += ` ${campo}: ${d.con_datos} con datos (${d.pct}%).`;
		}
		return t;
	}

	function addMsg(rol, contenido) {
		const div = $("<div>").addClass("ia-msg " + (rol === "usuario" ? "ia-user" : "ia-bot"));
		$("<div>").addClass("ia-pre").text(contenido).appendTo(div);
		div.appendTo($msgs);
		$msgs.scrollTop($msgs[0].scrollHeight);
		return div;
	}

	function addTabla(div, tabla, spec, coberturaTxt, idx) {
		if (!tabla || !tabla.columnas) return;
		const t = $("<table>").addClass("ia-tbl");
		const thead = $("<thead>").appendTo(t);
		const trh = $("<tr>").appendTo(thead);
		tabla.columnas.forEach((c) => $("<th>").text(c).appendTo(trh));
		const tb = $("<tbody>").appendTo(t);
		tabla.filas.slice(0, 50).forEach((f) => {
			const tr = $("<tr>").appendTo(tb);
			f.forEach((v) => $("<td>").text(esc(v)).appendTo(tr));
		});
		t.appendTo(div);
		if (tabla.filas.length > 50) {
			$("<div>").addClass("ia-cob").text(`Mostrando 50 de ${tabla.filas.length} filas.`).appendTo(div);
		}
		const acts = $("<div>").addClass("ia-actions").appendTo(div);
		if (spec && spec.render_chart) {
			$("<button>").addClass("btn btn-default btn-xs").text("Graficar")
				.on("click", () => graficar(idx)).appendTo(acts);
		}
		$("<button>").addClass("btn btn-default btn-xs").text("Guardar reporte")
			.on("click", () => guardarReporte(idx)).appendTo(acts);
		$("<div>").attr("id", "chart-" + idx).appendTo(div);
	}

	const mensajes = []; // {rol, contenido, tabla, spec, coberturaTxt, grafico}

	function renderAsistente(m) {
		const div = addMsg("asistente", m.contenido);
		if (m.coberturaTxt) $("<div>").addClass("ia-cob").text(m.coberturaTxt).appendTo(div);
		addTabla(div, m.tabla, m.spec, m.coberturaTxt, mensajes.length - 1);
	}

	function setUniverso(u, nombre) {
		state.universo = u || null;
		if (u) {
			page.body.find("#ia-universo-bar").show();
			page.body.find("#ia-universo-txt").text("Universo: " + (nombre || u));
		} else {
			page.body.find("#ia-universo-bar").hide();
		}
	}

	function pedirUniverso(aseguradoras, pendiente) {
		state.pendiente = pendiente || null;
		const div = addMsg("asistente", "¿Sobre qué aseguradora quieres consultar?");
		const acts = $("<div>").addClass("ia-actions").appendTo(div);
		$("<button>").addClass("btn btn-primary btn-xs").text("Todas")
			.on("click", () => elegirUniverso("TODAS")).appendTo(acts);
		(aseguradoras || []).forEach((a) => {
			$("<button>").addClass("btn btn-default btn-xs").text(a.nombre || a.name)
				.on("click", () => elegirUniverso(a.name, a.nombre)).appendTo(acts);
		});
		$msgs.scrollTop($msgs[0].scrollHeight);
	}

	function elegirUniverso(valor, nombre) {
		setUniverso(valor, valor === "TODAS" ? "Todas las aseguradoras" : nombre);
		addMsg("sistema", "Universo: " + (valor === "TODAS" ? "Todas las aseguradoras" : (nombre || valor)));
		if (state.pendiente) {
			const q = state.pendiente;
			state.pendiente = null;
			enviarPregunta(q);
		}
	}

	function setVista(v) {
		state.vista = v;
		page.body.find("#ia-tab-chat").toggleClass("btn-primary", v === "chat").toggleClass("btn-default", v !== "chat");
		page.body.find("#ia-tab-rep").toggleClass("btn-primary", v === "reportes").toggleClass("btn-default", v !== "reportes");
		page.body.find("#ia-input-row").toggle(v === "chat");
		if (v === "chat") cargarHistorial();
		else cargarReportes();
	}

	function cargarHistorial() {
		frappe.call(API + ".historial").then((r) => {
			$lista.empty();
			(r.message || []).forEach((c) => {
				const d = $("<div>").addClass("ia-conv" + (c.name === state.conv ? " active" : ""));
				d.css({ display: "flex", "align-items": "center", gap: "6px" });
				const cuerpo = $("<div>").css({ flex: 1, "min-width": 0 }).appendTo(d);
				$("<div>").html("<strong></strong>").find("strong").text(c.titulo).end().appendTo(cuerpo);
				$("<div>").addClass("text-muted small").text(c.ultima_actividad || "").appendTo(cuerpo);
				cuerpo.on("click", () => abrir(c.name));
				$("<button>").addClass("btn btn-default btn-xs").text("×").attr("title", "Eliminar conversación")
					.on("click", (e) => { e.stopPropagation(); eliminarConversacion(c.name, c.titulo); }).appendTo(d);
				d.appendTo($lista);
			});
		});
	}

	function eliminarConversacion(name, titulo) {
		frappe.confirm(`¿Eliminar la conversación "${titulo}"?`, () => {
			frappe.call(API + ".eliminar_conversacion", { nombre: name }).then(() => {
				if (state.conv === name) {
					state.conv = null;
					$msgs.empty();
					mensajes.length = 0;
				}
				cargarHistorial();
			});
		});
	}

	function abrir(name) {
		frappe.call(API + ".obtener_conversacion", { nombre: name }).then((r) => {
			const d = r.message;
			state.conv = d.name;
			setUniverso(d.universo || null, d.universo === "TODAS" ? "Todas las aseguradoras" : null);
			state.vista = "chat";
			$msgs.empty();
			mensajes.length = 0;
			(d.mensajes || []).forEach((m) => {
				if (m.rol === "usuario") {
					mensajes.push({ rol: "usuario" });
					addMsg("usuario", m.contenido);
				} else {
					const msg = { rol: "asistente", contenido: m.contenido };
					try {
						if (m.datos_json) {
							const _cob = m.cobertura_json ? JSON.parse(m.cobertura_json) : null;
							msg.tabla = JSON.parse(m.datos_json);
							msg.spec = m.spec_json ? JSON.parse(m.spec_json) : null;
							msg.cobertura = _cob;
							msg.coberturaTxt = _cob ? textoCobertura(_cob) : "";
						}
					} catch (e) { /* solo texto */ }
					mensajes.push(msg);
					renderAsistente(msg);
				}
			});
			setVista("chat");
		});
	}

	function preguntar() {
		const q = $input.val().trim();
		if (!q || state.cargando) return;
		$input.val("");
		enviarPregunta(q);
	}

	function enviarPregunta(q) {
		mensajes.push({ rol: "usuario" });
		addMsg("usuario", q);
		const loader = $("<div>").addClass("ia-msg ia-bot ia-loader")
			.html("<span></span><span></span><span></span>").appendTo($msgs);
		$msgs.scrollTop($msgs[0].scrollHeight);
		state.cargando = true;
		page.body.find("#ia-enviar").text("...").prop("disabled", true);
		frappe.call(API + ".ask", { pregunta: q, conversacion: state.conv, universo: state.universo }).then((r) => {
			loader.remove();
			state.cargando = false;
			page.body.find("#ia-enviar").text("Enviar").prop("disabled", false);
			const m = r.message;
			if (!m.ok) {
				const msg = { rol: "asistente", contenido: "Error: " + m.mensaje };
				mensajes.push(msg);
				renderAsistente(msg);
				return;
			}
			if (m.pregunta_universo) {
				state.conv = m.conversacion || state.conv;
				pedirUniverso(m.aseguradoras, m.pregunta_pendiente);
				return;
			}
			state.conv = m.conversacion;
			setUniverso(m.universo, m.universo_nombre);
			const msg = { rol: "asistente", contenido: m.mensaje || m.respuesta };
			if (m.columnas) {
				msg.tabla = { columnas: m.columnas, filas: m.filas };
				msg.spec = m.spec;
				msg.cobertura = m.cobertura || null;
				msg.coberturaTxt = textoCobertura(m.cobertura);
				state.ultimo = m;
			}
			mensajes.push(msg);
			renderAsistente(msg);
			cargarHistorial();
		}).fail(() => {
			loader.remove();
			state.cargando = false;
			page.body.find("#ia-enviar").text("Enviar").prop("disabled", false);
			const msg = { rol: "asistente", contenido: "Error de conexión con el servidor." };
			mensajes.push(msg);
			renderAsistente(msg);
		});
	}

	let chartSeq = 0;
	function graficar(i) {
		const m = mensajes[i];
		if (!m || !m.spec || !m.spec.render_chart || !m.tabla) return;
		const rc = m.spec.render_chart;
		const xi = m.tabla.columnas.indexOf(rc.x);
		const yi = m.tabla.columnas.indexOf(rc.y);
		if (xi < 0 || yi < 0) {
			frappe.msgprint("El gráfico pedido no coincide con las columnas.");
			return;
		}
		const labels = m.tabla.filas.slice(0, 30).map((f) => String(f[xi]));
		const values = m.tabla.filas.slice(0, 30).map((f) => Number(f[yi]) || 0);
		m.grafico = { labels, values, type: rc.type, title: rc.title || "" };
		const sel = "#chart-" + i;
		$(sel).empty();
		new frappe.Chart(sel, {
			data: { labels, datasets: [{ values }] },
			type: rc.type === "pie" ? "pie" : rc.type === "line" ? "line" : "bar",
			title: rc.title || "",
			height: 280,
		});
		chartSeq++;
	}

	function guardarReporte(i) {
		const m = mensajes[i];
		if (!m || !m.tabla) return;
		frappe.prompt("Título del reporte", (vals) => {
			frappe.call(API + ".guardar_reporte", {
				titulo: vals.value,
				spec: m.spec,
				snapshot: { columnas: m.tabla.columnas, filas: m.tabla.filas },
				grafico: m.grafico || null,
				cobertura: m.cobertura || null,
				universo: (m.cobertura && m.cobertura.universo) || 0,
				alcance: state.universo || "",
				conversacion: state.conv,
				tipo: m.spec && m.spec.render_chart ? "Gráfico" : "Tabla",
			}).then(() => frappe.msgprint("Reporte guardado."));
		});
	}

	function cargarReportes() {
		frappe.call(API + ".listar_reportes").then((r) => {
			$lista.empty();
			(r.message || []).forEach((x) => {
				const d = $("<div>").addClass("ia-conv");
				d.css({ display: "flex", "align-items": "center", gap: "6px" });
				const cuerpo = $("<div>").css({ flex: 1, "min-width": 0 }).appendTo(d);
				$("<div>").html("<strong></strong> ").find("strong").text(x.titulo).end()
					.append($("<span>").addClass("label label-info").text(x.tipo)).appendTo(cuerpo);
				$("<div>").addClass("text-muted small").text(x.fecha || "").appendTo(cuerpo);
				cuerpo.on("click", () => abrirReporte(x.name));
				$("<button>").addClass("btn btn-default btn-xs").text("×").attr("title", "Eliminar reporte")
					.on("click", (e) => { e.stopPropagation(); eliminarReporte(x.name, x.titulo); }).appendTo(d);
				d.appendTo($lista);
			});
		});
	}

	function eliminarReporte(name, titulo) {
		frappe.confirm(`¿Eliminar el reporte "${titulo}"?`, () => {
			frappe.call(API + ".eliminar_reporte", { nombre: name }).then(() => cargarReportes());
		});
	}

	function abrirReporte(name) {
		frappe.call(API + ".reabrir_reporte", { nombre: name }).then((r) => {
			const d = r.message;
			setVista("chat");
			$msgs.empty();
			mensajes.length = 0;
			const msg = {
				rol: "asistente",
				contenido: `Reporte: ${d.titulo} (${d.en_vivo ? "datos en vivo" : "snapshot"})`,
			};
			if (d.columnas) {
				msg.tabla = { columnas: d.columnas, filas: d.filas };
				msg.cobertura = (d.cobertura && typeof d.cobertura === "object") ? d.cobertura : null;
				msg.coberturaTxt = typeof d.cobertura === "string" ? d.cobertura : textoCobertura(d.cobertura);
			}
			mensajes.push(msg);
			renderAsistente(msg);
		});
	}

	page.body.find("#ia-nueva").on("click", () => {
		state.conv = null;
		setUniverso(null);
		state.pendiente = null;
		$msgs.empty();
		mensajes.length = 0;
		setVista("chat");
	});
	page.body.find("#ia-universo-cambiar").on("click", () => {
		setUniverso(null);
		frappe.call(API + ".universos").then((r) => pedirUniverso(r.message, null));
	});
	page.body.find("#ia-tab-chat").on("click", () => setVista("chat"));
	page.body.find("#ia-tab-rep").on("click", () => setVista("reportes"));
	page.body.find("#ia-enviar").on("click", preguntar);
	$input.on("keyup", (e) => { if (e.key === "Enter") preguntar(); });

	setVista("chat");
};
