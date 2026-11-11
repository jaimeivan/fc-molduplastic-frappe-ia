/* Asistente IA Molduplastic: chat + historial + reportes + Frappe Charts.
   JS plano (sin frameworks): el Desk v15 no expone Vue global. */

frappe.pages["asistente-ia"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Asistente IA",
		single_column: true,
	});

	const API = "fc_molduplastic_frappe_ia.molduplastic_ia.api";
	const CLAVE_PERIODO = "ia-periodo-consulta";

	// El período elegido sobrevive a chats nuevos y recargas. Guarda la clave y,
	// para "personalizado", las dos fechas (YYYY-MM-DD). También acepta el
	// formato viejo (solo la clave, guardada como texto).
	function leerPeriodo() {
		const crudo = localStorage.getItem(CLAVE_PERIODO);
		if (!crudo) return { clave: "este_mes", desde: "", hasta: "" };
		try {
			const d = JSON.parse(crudo);
			if (d && d.clave) return { clave: d.clave, desde: d.desde || "", hasta: d.hasta || "" };
		} catch (e) { /* valor viejo */ }
		return { clave: crudo, desde: "", hasta: "" };
	}

	const periodoGuardado = leerPeriodo();
	const state = {
		conv: null,
		vista: "chat",
		cargando: false,
		ultimo: null,
		universo: null,
		pendiente: null,
		periodo: null,
		periodo_consulta: periodoGuardado.clave,
		periodo_desde: periodoGuardado.desde,
		periodo_hasta: periodoGuardado.hasta,
		periodos: [],
	};

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
			.ia-pre.ia-md { white-space: normal; }
			.ia-md p { margin: 0 0 6px; }
			.ia-md table { border-collapse: collapse; margin: 6px 0; font-size: 12px; }
			.ia-md th, .ia-md td { border: 1px solid var(--border-color); padding: 4px 10px; text-align: left; }
			.ia-md th { background: var(--bg-light-gray); }
			.ia-loader span { display: inline-block; width: 7px; height: 7px; margin-right: 4px; border-radius: 50%; background: var(--text-muted); animation: ia-bounce 1.2s infinite; }
			.ia-loader span:nth-child(2) { animation-delay: 0.15s; }
			.ia-loader span:nth-child(3) { animation-delay: 0.3s; }
			@keyframes ia-bounce { 0%, 60%, 100% { transform: translateY(0); opacity: 0.4; } 30% { transform: translateY(-5px); opacity: 1; } }
			#ia-sugerencias { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
			.ia-chip { cursor: pointer; font-size: 12px; padding: 4px 10px; border: 1px solid var(--border-color); border-radius: 14px; background: var(--card-bg); color: var(--text-color); }
			.ia-chip:hover { background: var(--bg-light-gray); }
			#ia-periodo-bar { font-size: 12px; color: var(--text-muted); margin-bottom: 6px; }
			#ia-periodo-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
			#ia-periodo-row label { margin: 0; font-size: 12px; color: var(--text-muted); }
			#ia-periodo { width: auto; min-width: 230px; }
			#ia-periodo-fechas { display: none; align-items: center; gap: 4px; }
			#ia-periodo-fechas input { width: auto; }
			#ia-periodo-rango { font-size: 12px; color: var(--text-muted); }
			.ia-chart { margin-top: 8px; }
			.ia-tokens-badge { margin: 0 8px 0 0; cursor: pointer; white-space: nowrap; }
			.ia-total-row { font-weight: 600; border-top: 2px solid var(--border-color); }
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
				<div id="ia-periodo-bar" style="display:none"
					title="Desde la fecha de creación más antigua de las piezas hasta su última actualización.">
					<span id="ia-periodo-txt"></span>
				</div>
				<div id="ia-msgs"></div>
				<div id="ia-sugerencias"></div>
				<div id="ia-periodo-row" style="display:none">
					<label for="ia-periodo">Período</label>
					<select id="ia-periodo" class="form-control input-xs"></select>
					<span id="ia-periodo-fechas">
						<input type="date" id="ia-periodo-desde" class="form-control input-xs">
						<span>a</span>
						<input type="date" id="ia-periodo-hasta" class="form-control input-xs">
					</span>
					<span id="ia-periodo-rango"></span>
				</div>
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
	const $tokensBadge = $("<button>").attr({
		type: "button",
		title: "Tokens utilizados en el mes en curso. Clic para ver el detalle.",
		"aria-label": "Ver tokens utilizados",
	}).addClass("btn btn-default btn-sm ia-tokens-badge").text("Tokens utilizados: …");
	page.page_actions.prepend($tokensBadge);

	function formatoNumero(n) {
		return new Intl.NumberFormat("es-MX").format(Number(n) || 0);
	}

	function cargarTokens() {
		frappe.call(API + ".tokens_uso", { periodo: "este_mes" }).then((r) => {
			const d = r.message || {};
			if (!d.ok) return $tokensBadge.hide();
			$tokensBadge.text("Tokens utilizados: " + formatoNumero(d.total)).attr(
				"title",
				`Tokens utilizados en el mes en curso (${d.rango_texto || ""}). Clic para ver el detalle.`
			).show();
		}).catch(() => $tokensBadge.hide());
	}

	$tokensBadge.on("click", () => frappe.set_route("tokens-ia"));

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

	// El Desk no renderiza Markdown: las tablas del resumen llegaban como texto con
	// pipes y, debajo, se pintaba otra tabla HTML con los alias crudos del
	// resultado. Aquí se convierte el Markdown a HTML (solo respuestas del
	// asistente) y se sanea: lo único que se permite es formato, nunca atributos.
	const ETIQUETAS_OK = new Set([
		"P", "BR", "STRONG", "EM", "B", "I", "UL", "OL", "LI", "HR",
		"TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "CODE", "PRE",
		"H1", "H2", "H3", "H4",
	]);

	function limpiarHtml(html) {
		const cuerpo = new DOMParser().parseFromString("<div>" + html + "</div>", "text/html").body;
		const recorrer = (nodo) => {
			for (const hijo of Array.from(nodo.childNodes)) {
				if (hijo.nodeType === 1) {
					if (!ETIQUETAS_OK.has(hijo.tagName)) {
						recorrer(hijo);
						hijo.replaceWith(...Array.from(hijo.childNodes));
						continue;
					}
					for (const at of Array.from(hijo.attributes)) hijo.removeAttribute(at.name);
					recorrer(hijo);
				} else if (hijo.nodeType !== 3) {
					hijo.remove();
				}
			}
		};
		recorrer(cuerpo);
		return cuerpo.innerHTML;
	}

	function mdAHtml(texto) {
		if (typeof frappe.markdown !== "function") return null;
		frappe.markdown(""); // crea frappe.md2html si aún no existe
		// Converter propio: activar tablas en el compartido cambiaría el resto del Desk.
		if (!frappe.iaMd2html && frappe.md2html && frappe.md2html.constructor) {
			frappe.iaMd2html = new frappe.md2html.constructor();
			frappe.iaMd2html.setOption("tables", true);
			frappe.iaMd2html.setOption("breaks", true);
		}
		if (frappe.iaMd2html) return frappe.iaMd2html.makeHtml(texto || "");
		return frappe.markdown(texto || "");
	}

	// ¿La respuesta ya trae una tabla en Markdown? Entonces no hay que añadir
	// además la tabla HTML del resultado: sería la misma cifra dos veces.
	function traeTabla(texto) {
		return /\|\s*:?-{3,}/.test(texto || "");
	}

	// Encabezados de tabla sin guiones bajos. El resultado crudo trae los
	// nombres del esquema (id_taller, valor_pendientes); se muestran legibles
	// sin tocar las columnas que usa el gráfico.
	const NOMBRES_COLUMNA = {
		id_plaza: "Plaza",
		plaza: "Plaza",
		id_taller: "Taller",
		taller_asignado: "Taller",
		parte_pieza: "Tipo de pieza",
		region_pais: "Región",
		region: "Región",
		tipo_carga: "Tipo CDR",
		tipo: "Submarca",
		modelo: "Modelo",
		estado: "Estado",
		fechaingreso: "Fecha de ingreso",
		fecha_hora_recoleccion: "Fecha de recolección",
		fecha_inicio: "Fecha de inicio",
		fecha_completado: "Fecha de completado",
		preciovale: "Valor",
	};

	function tituloColumna(c) {
		const clave = String(c || "");
		const nombre = NOMBRES_COLUMNA[clave.toLowerCase()];
		if (nombre) return nombre;
		const texto = clave.replace(/_/g, " ").trim();
		return texto ? texto.charAt(0).toUpperCase() + texto.slice(1) : clave;
	}

	// Una sola celda (una cifra) no necesita tabla: la frase ya la dice.
	function esCifraUnica(tabla) {
		if (!tabla) return false;
		const filas = tabla.filas || [];
		const columnas = tabla.columnas || [];
		if (filas.length !== 1 || columnas.length !== 1) return false;
		const valor = filas[0][0];
		// Un nombre (listado de una sola fila) sí se muestra en tabla; una cifra
		// ("15 piezas") no necesita el cuadro de una casilla.
		if (typeof valor === "number") return true;
		return /^\s*\$?\s*-?[\d.,]+\s*%?\s*$/.test(String(valor == null ? "" : valor));
	}

	// Filas de una tabla Markdown en el texto del modelo (sin encabezado ni
	// separador). Sirve para detectar si el modelo recortó el resultado.
	function filasDeTablaTexto(texto) {
		const lineas = String(texto || "")
			.split("\n")
			.filter((l) => /^\s*\|/.test(l));
		return Math.max(0, lineas.length - 2);
	}

	// --- Gráficas ---------------------------------------------------------
	// El tipo y los ejes los elige el usuario; la app los propone leyendo la
	// tabla (primera columna no numérica como X, medidas como Y). La config se
	// guarda en `m.grafico` y viaja con el reporte, así el reporte reabre con
	// su gráfica y no depende del criterio del modelo.
	const TIPOS_GRAFICA = [
		{ valor: "bar", etiqueta: "Barras" },
		{ valor: "bar_stacked", etiqueta: "Barras apiladas" },
		{ valor: "line", etiqueta: "Líneas" },
		{ valor: "pie", etiqueta: "Pastel" },
		{ valor: "donut", etiqueta: "Anillo" },
	];
	const TIPO_GRAFICA_LEGADO = { bar: "bar", line: "line", pie: "pie", donut: "donut" };

	// Un valor numérico (admite "$1,234.50" y "50.0%"). Texto, fechas o null
	// devuelven null: así se distingue la medida de la dimensión.
	function numeroDe(valor) {
		if (typeof valor === "number") return isFinite(valor) ? valor : null;
		if (valor === null || valor === undefined) return null;
		const limpio = String(valor).trim().replace(/\s/g, "").replace(/^\$/, "")
			.replace(/,/g, "").replace(/%$/, "");
		if (!/^-?\d+(\.\d+)?$/.test(limpio)) return null;
		return Number(limpio);
	}

	function columnaEsNumero(tabla, indice) {
		let con_dato = 0;
		for (const fila of (tabla && tabla.filas) || []) {
			const valor = fila[indice];
			if (valor === null || valor === undefined || valor === "") continue;
			if (numeroDe(valor) === null) return false;
			con_dato++;
		}
		return con_dato > 0;
	}


	// Construye la fila agregada sin modificar los datos originales (que también
	// alimentan gráficas y reportes). Las columnas con cualquier texto quedan
	// fuera de la suma; los porcentajes se consideran numéricos igual que para
	// las gráficas.


	// Propuesta inicial: la primera columna de texto es el eje X y la primera
	// medida el eje Y. Si el spec trae un render_chart, manda eso (el modelo ya
	// había elegido qué graficar). Sin ninguna columna de texto (una fila de
	// medidas sueltas, ej. pendientes y valor), cada columna es una categoría.
	function sugerirGrafica(tabla, spec) {
		const columnas = (tabla && tabla.columnas) || [];
		if (!columnas.length || !(tabla.filas || []).length) return null;
		const numericas = columnas.filter((c, i) => columnaEsNumero(tabla, i));
		if (!numericas.length) return null;
		const categoricas = columnas.filter((c) => !numericas.includes(c));
		let x = categoricas.length ? categoricas[0] : "__columnas__";
		let y = x === "__columnas__" ? numericas.slice() : [numericas[0]];
		let tipo = "bar";
		const rc = spec && spec.render_chart;
		if (rc && columnas.includes(rc.x) && columnas.includes(rc.y)) {
			x = rc.x;
			y = [rc.y];
			tipo = TIPO_GRAFICA_LEGADO[String(rc.type || "").toLowerCase()] || "bar";
		}
		return { tipo, x, y };
	}

	function tituloGrafica(cfg) {
		if (cfg && cfg.titulo) return cfg.titulo;
		if (!cfg) return "Gráfica";
		if (cfg.x === "__columnas__") return "Medidas";
		const medida = (cfg.y || []).map(tituloColumna).join(" y ");
		return medida ? `${medida} por ${tituloColumna(cfg.x)}` : "Gráfica";
	}

	// Acepta la forma nueva ({tipo, x, y, titulo}) y la vieja ({type,
	// labels, values}) que guardaron los reportes anteriores.
	function normalizarGrafico(cfg) {
		if (!cfg) return null;
		let tipo = String(cfg.tipo || cfg.type || "").toLowerCase();
		if (!TIPOS_GRAFICA.some((t) => t.valor === tipo)) {
			tipo = TIPO_GRAFICA_LEGADO[tipo] || "bar";
		}
		let y = cfg.y;
		if (typeof y === "string") y = [y];
		y = (y || []).filter(Boolean);
		const datasets = Array.isArray(cfg.datasets)
			? cfg.datasets
			: (Array.isArray(cfg.values) ? [{ name: "", values: cfg.values }] : null);
		return {
			tipo,
			x: cfg.x || "",
			y,
			titulo: cfg.titulo || cfg.title || "",
			labels: Array.isArray(cfg.labels) ? cfg.labels : null,
			datasets,
		};
	}

	function datosDeGrafico(tabla, cfg) {
		if (cfg.labels && cfg.datasets) return { labels: cfg.labels, datasets: cfg.datasets };
		const columnas = (tabla && tabla.columnas) || [];
		const filas = (tabla && tabla.filas) || [];
		if (!filas.length) return { labels: [], datasets: [] };
		if (cfg.x === "__columnas__") {
			const medidas = (cfg.y && cfg.y.length
				? cfg.y
				: columnas.filter((c, i) => columnaEsNumero(tabla, i))
			).filter((c) => columnas.includes(c));
			return {
				labels: medidas.map(tituloColumna),
				datasets: [{
					name: "",
					values: medidas.map((c) => numeroDe(filas[0][columnas.indexOf(c)]) || 0),
				}],
			};
		}
		const xi = columnas.indexOf(cfg.x);
		const y = (cfg.y || []).filter((c) => columnas.includes(c));
		if (xi < 0 || !y.length) return { labels: [], datasets: [] };
		return {
			labels: filas.map((f) => esc(f[xi])),
			datasets: y.map((c) => {
				const yi = columnas.indexOf(c);
				return { name: tituloColumna(c), values: filas.map((f) => numeroDe(f[yi]) || 0) };
			}),
		};
	}

	function pintarGrafico(i) {
		const m = mensajes[i];
		const cfg = normalizarGrafico(m && m.grafico);
		if (!cfg) return false;
		const datos = datosDeGrafico(m.tabla, cfg);
		if (!datos.labels.length || !datos.datasets.length) return false;
		let $cont = page.body.find("#chart-" + i);
		if (!$cont.length) $cont = $("<div>").attr("id", "chart-" + i).appendTo($msgs);
		$cont.addClass("ia-chart").empty();
		let datasets = datos.datasets;
		const tipo = cfg.tipo === "bar_stacked" ? "bar" : cfg.tipo;
		if ((tipo === "pie" || tipo === "donut") && datasets.length > 1) {
			datasets = datasets.slice(0, 1);
		}
		// El lienzo va en su propio div: así el botón de guardar queda debajo
		// del gráfico y no lo borra el constructor de frappe.Chart.
		const $lienzo = $("<div>").appendTo($cont);
		new frappe.Chart($lienzo.get(0), {
			data: { labels: datos.labels, datasets },
			type: tipo,
			title: tituloGrafica(cfg),
			height: 300,
			barOptions: { stacked: cfg.tipo === "bar_stacked" ? 1 : 0 },
		});
		// Si la gráfica no se guardó al generarla (o si se cambió), el usuario
		// puede guardarla aquí: mismo popup de título que las tablas.
		if (!m.grafico_guardado) {
			$("<div>").addClass("ia-actions ia-chart-save").appendTo($cont)
				.append(
					$("<button>").addClass("btn btn-default btn-xs").text("Guardar gráfica")
						.on("click", () => guardarReporte(i))
				);
		}
		return true;
	}

	function actualizarBotonGrafica(i) {
		const m = mensajes[i];
		if (!m || !m.grafico || !m.grafico_guardado) return;
		page.body.find("#chart-" + i).find(".ia-chart-save").remove();
	}

	function firmaGrafico(cfg) {
		if (!cfg) return "";
		return [cfg.tipo, cfg.x, (cfg.y || []).join(","), cfg.titulo].join("|");
	}

	function preguntarGrafica(i) {
		const m = mensajes[i];
		if (!m || !m.tabla) return;
		const sugerida = sugerirGrafica(m.tabla, m.spec);
		if (!sugerida) {
			frappe.msgprint("Esta respuesta no tiene columnas numéricas para graficar.");
			return;
		}
		const columnas = m.tabla.columnas;
		const numericas = columnas.filter((c, idx) => columnaEsNumero(m.tabla, idx));
		const opcionesX = columnas.map((c) => ({ label: tituloColumna(c), value: c }));
		if (numericas.length === columnas.length) {
			opcionesX.push({ label: "Cada columna (medidas)", value: "__columnas__" });
		}
		const opcionesY = numericas.map((c) => ({
			label: tituloColumna(c),
			value: c,
			checked: sugerida.y.includes(c),
		}));
		const d = new frappe.ui.Dialog({
			title: "Configurar gráfica",
			fields: [
				{
					fieldname: "tipo",
					fieldtype: "Select",
					label: "Tipo de gráfica",
					options: TIPOS_GRAFICA.map((t) => ({ label: t.etiqueta, value: t.valor })),
					default: sugerida.tipo,
					reqd: 1,
				},
				{
					fieldname: "x",
					fieldtype: "Select",
					label: "Eje X (categorías)",
					options: opcionesX,
					default: sugerida.x,
					reqd: 1,
				},
				{
					fieldname: "y",
					fieldtype: "MultiCheck",
					label: "Eje Y (valores)",
					options: opcionesY,
					columns: 2,
					sort_options: false,
					reqd: 1,
				},
				{
					fieldname: "titulo",
					fieldtype: "Data",
					label: "Título",
					default: tituloGrafica(sugerida),
				},
				{
					fieldname: "guardar",
					fieldtype: "Check",
					label: "Guardar también en Reportes",
					default: 0,
				},
				{
					fieldname: "ayuda",
					fieldtype: "HTML",
					options:
						'<p class="text-muted small">Barras apiladas suma las columnas del eje Y. Pastel y Anillo usan solo la primera.</p>',
				},
			],
			primary_action_label: "Graficar",
			primary_action(vals) {
				const y = Array.isArray(vals.y) ? vals.y : (vals.y ? [vals.y] : []);
				if (!y.length) {
					frappe.msgprint("Elige al menos una columna del eje Y.");
					return;
				}
				const cfg = normalizarGrafico({
					tipo: vals.tipo,
					x: vals.x,
					y,
					titulo: String(vals.titulo || "").trim() || tituloGrafica({ x: vals.x, y }),
				});
				// Una gráfica distinta a la que ya estaba es una nueva: si la
				// anterior se había guardado, esta vuelve a necesitar guardarse.
				if (firmaGrafico(cfg) !== firmaGrafico(m.grafico)) m.grafico_guardado = false;
				m.grafico = cfg;
				if (!pintarGrafico(i)) {
					m.grafico = null;
					frappe.msgprint("No se pudo dibujar la gráfica con esas columnas.");
					return;
				}
				d.hide();
				if (Number(vals.guardar)) guardarReporte(i, m.grafico.titulo);
			},
		});
		d.show();
	}

	function addMsg(rol, contenido) {
		const div = $("<div>").addClass("ia-msg " + (rol === "usuario" ? "ia-user" : "ia-bot"));
		const cuerpo = $("<div>").addClass("ia-pre");
		const html = rol === "usuario" ? null : mdAHtml(contenido);
		if (html) {
			cuerpo.addClass("ia-md").html(limpiarHtml(html));
		}
		else cuerpo.text(contenido);
		cuerpo.appendTo(div);
		div.appendTo($msgs);
		$msgs.scrollTop($msgs[0].scrollHeight);
		return div;
	}

	function addTabla(div, tabla, spec, coberturaTxt, idx, omitirTabla) {
		if (!tabla || !tabla.columnas) return;
		if (!omitirTabla) {
			const t = $("<table>").addClass("ia-tbl");
			const thead = $("<thead>").appendTo(t);
			const trh = $("<tr>").appendTo(thead);
			tabla.columnas.forEach((c) => $("<th>").text(tituloColumna(c)).appendTo(trh));
			const tb = $("<tbody>").appendTo(t);
			// Sin fila de total: la app no la manda y el chat no la calcula
			// (decisión del dueño 2026-11-04).
			const todas = tabla.filas || [];
			todas.slice(0, 50).forEach((f) => {
				const tr = $("<tr>").appendTo(tb);
				f.forEach((v) => $("<td>").text(esc(v)).appendTo(tr));
			});
			t.appendTo(div);
			if (todas.length > 50) {
				$("<div>").addClass("ia-cob").text(`Mostrando 50 de ${todas.length} filas.`).appendTo(div);
			}
		}
		const acts = $("<div>").addClass("ia-actions").appendTo(div);
		// Toda tabla con datos se puede graficar: el tipo y los ejes se preguntan
		// en el diálogo (antes solo aparecía si el modelo pedía render_chart).
		if (!esCifraUnica(tabla)) {
			$("<button>").addClass("btn btn-default btn-xs").text("Graficar")
				.on("click", () => preguntarGrafica(idx)).appendTo(acts);
		}
		$("<button>").addClass("btn btn-default btn-xs").text("Guardar reporte")
			.on("click", () => guardarReporte(idx)).appendTo(acts);
		$("<div>").attr("id", "chart-" + idx).appendTo(div);
	}

	const mensajes = []; // {rol, contenido, tabla, spec, coberturaTxt, grafico}

	function renderAsistente(m) {
		const div = addMsg("asistente", m.contenido);
		const yaExplicada = /cobertura/i.test(m.contenido || "");
		if (m.coberturaTxt && !yaExplicada) {
			$("<div>").addClass("ia-cob").text(m.coberturaTxt).appendTo(div);
		}
		// Con más de una fila, la tabla del servidor se pinta siempre: si el
		// modelo escribió una tabla más corta, se completa con la del resultado.
		const filasServidor = ((m.tabla && m.tabla.filas) || []).length;
		const tablaTextoCompleta =
			traeTabla(m.contenido) && filasServidor <= Math.max(1, filasDeTablaTexto(m.contenido));
		addTabla(
			div,
			m.tabla,
			m.spec,
			m.coberturaTxt,
			mensajes.length - 1,
			// Un reporte guardado como gráfica se abre solo con su gráfica; uno
			// guardado como tabla, solo con su tabla.
			m.soloGrafico || esCifraUnica(m.tabla) || tablaTextoCompleta
		);
	}

	function setUniverso(u, nombre) {
		state.universo = u || null;
		if (u) {
			page.body.find("#ia-universo-bar").show();
			page.body.find("#ia-universo-txt").text("Universo: " + (nombre || u));
		} else {
			page.body.find("#ia-universo-bar").hide();
		}
		// La barra del período la decide el servidor: con un universo elegido o
		// con un usuario de aseguradora muestra el rango; un admin sin universo
		// devuelve mostrar=false y se queda oculta.
		cargarPeriodo();
	}

	// Rango de información del alcance (creación más antigua y última
	// actualización de las piezas). Va en la parte superior del chat.
	function pintarPeriodo(p) {
		state.periodo = p && p.mostrar ? p : null;
		const $bar = page.body.find("#ia-periodo-bar");
		if (!state.periodo || state.vista !== "chat") {
			$bar.hide();
			return;
		}
		const quien = state.periodo.universo ? "Información de " + state.periodo.universo : "Información disponible";
		page.body.find("#ia-periodo-txt")
			.text(`${quien}: del ${state.periodo.desde_texto} al ${state.periodo.hasta_texto}`);
		$bar.show();
	}

	function cargarPeriodo() {
		frappe.call(API + ".periodo_datos", { universo: state.universo }).then(
			(r) => pintarPeriodo(r && r.message),
			() => pintarPeriodo(null)
		);
	}

	// Selector del período de consulta: las opciones y sus fechas las manda el
	// servidor (una sola copia de la regla). La elegida se guarda en el
	// navegador y se manda en cada pregunta, también en un chat nuevo.
	function fechaCorta(iso) {
		if (!iso) return "";
		const d = new Date(String(iso).slice(0, 10) + "T00:00:00");
		if (isNaN(d.getTime())) return String(iso);
		return d.toLocaleDateString("es-MX", { day: "numeric", month: "long", year: "numeric" });
	}

	function pintarRangoPeriodo() {
		const $rango = page.body.find("#ia-periodo-rango");
		if (state.periodo_consulta === "personalizado") {
			const desde = fechaCorta(state.periodo_desde);
			const hasta = fechaCorta(state.periodo_hasta);
			$rango.text(desde && hasta ? `Del ${desde} al ${hasta}` : "Elige las fechas");
			return;
		}
		const p = (state.periodos || []).find((x) => x.clave === state.periodo_consulta);
		$rango.text(p ? p.rango_texto : "");
	}

	function guardarPeriodo() {
		try {
			localStorage.setItem(CLAVE_PERIODO, JSON.stringify({
				clave: state.periodo_consulta,
				desde: state.periodo_desde || "",
				hasta: state.periodo_hasta || "",
			}));
		} catch (e) { /* modo privado */ }
	}

	function actualizarControlesPeriodo() {
		const esPersonalizado = state.periodo_consulta === "personalizado";
		page.body.find("#ia-periodo").val(state.periodo_consulta);
		page.body.find("#ia-periodo-fechas").css("display", esPersonalizado ? "inline-flex" : "none");
		page.body.find("#ia-periodo-desde").val(state.periodo_desde || "");
		page.body.find("#ia-periodo-hasta").val(state.periodo_hasta || "");
		pintarRangoPeriodo();
		guardarPeriodo();
	}

	function cargarPeriodos() {
		frappe.call(API + ".periodos").then((r) => {
			const d = r.message || {};
			state.periodos = d.periodos || [];
			const claves = state.periodos.map((p) => p.clave);
			if (!claves.includes(state.periodo_consulta)) {
				state.periodo_consulta = d.defecto || "este_mes";
			}
			// "Personalizado" sin fechas guardadas no se puede consultar: se cae
			// al default.
			if (state.periodo_consulta === "personalizado" && !(state.periodo_desde && state.periodo_hasta)) {
				state.periodo_consulta = d.defecto || "este_mes";
			}
			const $sel = page.body.find("#ia-periodo").empty();
			state.periodos.forEach((p) => $("<option>").val(p.clave).text(p.etiqueta).appendTo($sel));
			actualizarControlesPeriodo();
			page.body.find("#ia-periodo-row").toggle(state.vista === "chat");
		});
	}

	// Si la pregunta trae una fecha explícita, el chat acomoda el selector a lo
	// que se consultó: el preset si el rango coincide exacto, si no,
	// "Personalizado" con esas fechas. Así el período visible nunca miente.
	function sincronizarPeriodo(m) {
		if (!m || !m.periodo_del_texto || !m.rango) return;
		const desde = String(m.rango.desde || "").slice(0, 10);
		const hasta = String(m.rango.hasta || "").slice(0, 10);
		if (!desde || !hasta) return;
		const preset = (state.periodos || []).find(
			(p) =>
				String(p.desde || "").slice(0, 10) === desde &&
				String(p.hasta || "").slice(0, 10) === hasta
		);
		if (preset) {
			state.periodo_consulta = preset.clave;
			state.periodo_desde = "";
			state.periodo_hasta = "";
		} else {
			state.periodo_consulta = "personalizado";
			state.periodo_desde = desde;
			state.periodo_hasta = hasta;
		}
		actualizarControlesPeriodo();
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
		page.body.find("#ia-periodo-row").toggle(v === "chat" && state.periodos.length > 0);
		pintarPeriodo(state.periodo);
		actualizarSugerencias();
		if (v === "chat") cargarHistorial();
		else cargarReportes();
	}

	// Las sugerencias solo se ven en un chat nuevo (sin conversación y sin mensajes).
	function esChatNuevo() {
		return state.vista === "chat" && !state.conv && !state.pendiente && mensajes.length === 0;
	}

	function actualizarSugerencias() {
		page.body.find("#ia-sugerencias").toggle(esChatNuevo());
	}

	// Preguntas sugeridas: vienen de Molduplastic IA Settings > Sugerencias
	// (activas, en orden). Un clic lanza la pregunta, no la copia al input.
	function cargarSugerencias() {
		frappe.call(API + ".sugerencias").then((r) => {
			const $cont = page.body.find("#ia-sugerencias").empty();
			(r.message || []).forEach((texto) => {
				$("<button>").attr("type", "button").addClass("ia-chip").text(texto)
					.on("click", () => {
						if (state.cargando) return;
						$input.val("");
						enviarPregunta(texto);
					})
					.appendTo($cont);
			});
			actualizarSugerencias();
		});
	}

	function cargarHistorial() {
		frappe.call(API + ".historial").then((r) => {
			// Una respuesta lenta no debe pintar conversaciones encima de la
			// lista de reportes (ni al revés): manda la vista actual.
			if (state.vista !== "chat") return;
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
					setUniverso(null);
					$msgs.empty();
					mensajes.length = 0;
					actualizarSugerencias();
				}
				cargarHistorial();
			});
		});
	}

	function abrir(name) {
		frappe.call(API + ".obtener_conversacion", { nombre: name }).then((r) => {
			const d = r.message;
			state.conv = d.name;
			setUniverso(d.universo || null, d.universo_nombre || (d.universo === "TODAS" ? "Todas las aseguradoras" : null));
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
		// Personalizado necesita las dos fechas: sin ellas el servidor caería al
		// default y la pregunta se contestaría sobre otro período.
		if (state.periodos.length && state.periodo_consulta === "personalizado") {
			if (!state.periodo_desde || !state.periodo_hasta) {
				frappe.msgprint("Elige las dos fechas del período personalizado.");
				return;
			}
			if (state.periodo_desde > state.periodo_hasta) {
				frappe.msgprint("La fecha inicial no puede ser posterior a la final.");
				return;
			}
		}
		mensajes.push({ rol: "usuario" });
		actualizarSugerencias();
		addMsg("usuario", q);
		const loader = $("<div>").addClass("ia-msg ia-bot ia-loader")
			.html("<span></span><span></span><span></span>").appendTo($msgs);
		$msgs.scrollTop($msgs[0].scrollHeight);
		state.cargando = true;
		page.body.find("#ia-enviar").text("...").prop("disabled", true);
		frappe.call(API + ".ask", {
			pregunta: q,
			conversacion: state.conv,
			universo: state.universo,
			periodo: state.periodo_consulta,
			desde: state.periodo_desde || null,
			hasta: state.periodo_hasta || null,
		}).then((r) => {
			loader.remove();
			state.cargando = false;
			page.body.find("#ia-enviar").text("Enviar").prop("disabled", false);
			const m = r.message;
			cargarTokens();
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
			sincronizarPeriodo(m);
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
		}).fail((xhr) => {
			loader.remove();
			state.cargando = false;
			page.body.find("#ia-enviar").text("Enviar").prop("disabled", false);
			cargarTokens();
			const codigo = xhr && xhr.status;
			let texto;
			if (codigo === 502 || codigo === 503 || codigo === 504) {
				texto =
					"El servidor se está reiniciando o la consulta tardó demasiado. " +
					"Intenta de nuevo en unos segundos.";
			} else if (codigo === 401 || codigo === 403) {
				texto = "Tu sesión expiró. Recarga la página e intenta de nuevo.";
			} else if (!codigo) {
				texto =
					"No se pudo conectar con el servidor (revisa tu red) o la consulta " +
					"tardó demasiado. Intenta de nuevo.";
			} else {
				texto = "El servidor respondió con un error (HTTP " + codigo + "). Intenta de nuevo.";
			}
			const msg = { rol: "asistente", contenido: texto };
			mensajes.push(msg);
			renderAsistente(msg);
			// Reintento con la misma pregunta, sin volver a escribirla.
			const div = $msgs.children().last();
			const acciones = $("<div>").addClass("ia-actions").appendTo(div);
			$("<button>")
				.addClass("btn btn-default btn-xs")
				.text("Reintentar")
				.on("click", () => {
					div.remove();
					mensajes.pop();
					enviarPregunta(q);
				})
				.appendTo(acciones);
		});
	}

	function guardarReporte(i, titulo) {
		const m = mensajes[i];
		if (!m || !m.tabla) return;
		if (titulo) {
			enviarReporte(i, titulo);
			return;
		}
		frappe.prompt(
			{
				fieldname: "titulo",
				fieldtype: "Data",
				label: "Título del reporte",
				reqd: 1,
				default: m.grafico ? tituloGrafica(normalizarGrafico(m.grafico)) : "",
			},
			(vals) => enviarReporte(i, vals.titulo),
			"Guardar reporte",
			"Guardar"
		);
	}

	function enviarReporte(i, titulo) {
		const m = mensajes[i];
		if (!m || !m.tabla) return;
		if (!m.spec) {
			frappe.msgprint("No puedo guardar este reporte: falta la consulta original.");
			return;
		}
		const grafico = normalizarGrafico(m.grafico);
		let carga = null;
		if (grafico) {
			carga = { tipo: grafico.tipo, x: grafico.x, y: grafico.y, titulo: grafico.titulo };
			// Serie de un reporte viejo (sin ejes): se manda tal cual para no
			// perder la gráfica al volver a guardarlo.
			if (!grafico.y.length && grafico.datasets && grafico.datasets.length) {
				carga.labels = grafico.labels || [];
				carga.values = grafico.datasets[0].values || [];
			}
		}
		frappe.call(API + ".guardar_reporte", {
			titulo: titulo,
			spec: m.spec,
			snapshot: { columnas: m.tabla.columnas, filas: m.tabla.filas },
			grafico: carga,
			cobertura: m.cobertura || null,
			universo: (m.cobertura && m.cobertura.universo) || 0,
			alcance: state.universo || "",
			conversacion: state.conv,
			tipo: grafico ? "Gráfico" : "Tabla",
		}).then((r) => {
			// Con la gráfica ya guardada, el botón de debajo sobra: se quita.
			if (grafico) {
				m.grafico_guardado = true;
				m.reporte = (r && r.message && r.message.reporte) || m.reporte;
				actualizarBotonGrafica(i);
			}
			frappe.msgprint("Reporte guardado.");
		});
	}

	function cargarReportes() {
		frappe.call(API + ".listar_reportes").then((r) => {
			if (state.vista !== "reportes") return;
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
			// El reporte se muestra en el área grande SIN salir de la pestaña
			// Reportes: antes se cambiaba a Chat y se recargaba la lista de
			// conversaciones, así que el clic siguiente (o un doble clic) caía
			// sobre una conversación y el reporte desaparecía.
			setVista("reportes");
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
			if (d.spec) msg.spec = d.spec;
			if (d.grafico) {
				msg.grafico = d.grafico;
				// Viene de un reporte guardado: no hace falta volver a guardarlo.
				msg.grafico_guardado = true;
				// Un reporte de gráfica se muestra solo con la gráfica: la tabla
				// es la fuente de los datos, no parte del reporte. Si la config
				// guardada no se puede dibujar (ejes que ya no existen), se cae a
				// la tabla para no dejar la respuesta en blanco.
				const cfg = normalizarGrafico(d.grafico);
				const datos = cfg ? datosDeGrafico(msg.tabla, cfg) : null;
				msg.soloGrafico = !!(datos && datos.labels.length && datos.datasets.length);
			}
			mensajes.push(msg);
			renderAsistente(msg);
			if (msg.soloGrafico) pintarGrafico(mensajes.length - 1);
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
	page.body.find("#ia-periodo").on("change", function () {
		const anterior = state.periodo_consulta;
		state.periodo_consulta = this.value;
		// Al pasar a "Personalizado" se arranca con las fechas de lo que estaba
		// elegido (o el mes en curso), para no dejar los campos vacíos.
		if (this.value === "personalizado" && !(state.periodo_desde && state.periodo_hasta)) {
			const base =
				(state.periodos || []).find((x) => x.clave === anterior && x.desde && x.hasta) ||
				(state.periodos || []).find((x) => x.clave === "este_mes");
			if (base && base.desde && base.hasta) {
				state.periodo_desde = String(base.desde).slice(0, 10);
				state.periodo_hasta = String(base.hasta).slice(0, 10);
			}
		}
		actualizarControlesPeriodo();
	});
	page.body.find("#ia-periodo-desde, #ia-periodo-hasta").on("change", function () {
		state.periodo_desde = page.body.find("#ia-periodo-desde").val() || "";
		state.periodo_hasta = page.body.find("#ia-periodo-hasta").val() || "";
		pintarRangoPeriodo();
		guardarPeriodo();
	});

	setVista("chat");
	cargarSugerencias();
	cargarPeriodo();
	cargarPeriodos();
	cargarTokens();
};
