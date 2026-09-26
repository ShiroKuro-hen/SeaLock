"use strict";

const CURRENCY = window.SEALOCK.currency;
let CURRENT_STOCK = null; // { ticker, assumptions, marketStatus } de la ultima ficha vista
let PORTFOLIO_ANY_MARKET_OPEN = true; // gate del auto-refresco: sin nada abierto, no vale la pena sondear
let REC_RETURN_INDEX = null; // indice en REC_COMBINED si la ficha se abrio desde Recomendaciones

/* ------------------------------------------------------------------ util */

async function api(group, tool, args = {}) {
  const r = await fetch(`/api/${group}/${tool}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-SeaLock-Token": window.SEALOCK.token },
    body: JSON.stringify(args),
  });
  const j = await r.json().catch(() => ({ error: "Respuesta ilegible del servidor" }));
  if (!r.ok || j.ok === false) throw new Error(j.error || `Error ${r.status}`);
  return j.data;
}

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function money(v, currency = CURRENCY) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return new Intl.NumberFormat("es-ES", {
    style: "currency", currency: currency || CURRENCY, maximumFractionDigits: 2,
  }).format(v);
}

function number(v, dec = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return new Intl.NumberFormat("es-ES", { maximumFractionDigits: dec }).format(v);
}

function pct(v, dec = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v > 0 ? "+" : ""}${number(v, dec)} %`;
}

// CSS classes ("verde"/"rojo") stay Spanish: estilo.css defines .verde/.rojo/.ambar
// and isn't part of this rename.
const sign = (v) => (v === null || v === undefined ? "" : v > 0 ? "verde" : v < 0 ? "rojo" : "");

function compact(v) {
  if (v === null || v === undefined) return "—";
  const a = Math.abs(v);
  if (a >= 1e12) return `${number(v / 1e12, 2)} B`;
  if (a >= 1e9) return `${number(v / 1e9, 2)} MM`;
  if (a >= 1e6) return `${number(v / 1e6, 1)} M`;
  if (a >= 1e3) return `${number(v / 1e3, 1)} k`;
  return number(v, 2);
}

/**
 * Edad de un precio, a partir de age_seconds / price_as_of que devuelve el
 * servidor (market.quote). Sin esto, un precio de hace 2 dias (mercado
 * cerrado el finde) se veria igual que uno de hace 10 segundos.
 */
function freshness(ageSeconds, priceAsOf) {
  if (ageSeconds === null || ageSeconds === undefined) {
    if (priceAsOf) return { texto: `dato del ${priceAsOf}`, clase: "tenue" };
    return { texto: "antigüedad desconocida", clase: "tenue" };
  }
  const s = ageSeconds;
  if (s < 0) return { texto: "reloj local desincronizado", clase: "tenue" };

  if (s < 120) return { texto: `hace ${Math.round(s)} s`, clase: "verde" };
  if (s < 20 * 60) return { texto: `hace ${Math.round(s / 60)} min`, clase: "ambar" };
  if (s < 3600) return { texto: `hace ${Math.round(s / 60)} min`, clase: "rojo" };
  if (s < 24 * 3600) return { texto: `hace ${Math.round(s / 3600)} h`, clase: "rojo" };

  // Mas de un dia: la cifra relativa deja de ser util (findes, festivos). Se muestra
  // el momento absoluto del precio.
  if (priceAsOf) {
    const fmt = new Intl.DateTimeFormat("es-ES", {
      weekday: "short", hour: "2-digit", minute: "2-digit",
    }).format(new Date(priceAsOf));
    return { texto: `último precio: ${fmt}`, clase: "rojo" };
  }
  return { texto: `hace ${Math.round(s / 3600)} h`, clase: "rojo" };
}

/**
 * Etiqueta del estado de mercado que devuelve market.quote (OPEN, PRE_MARKET,
 * POST_MARKET, CLOSED). Null si Yahoo no lo dio: se muestra vacio en vez de
 * inventar un estado.
 */
function marketStatusBadge(status) {
  switch (status) {
    case "OPEN": return { texto: "● ABIERTO", clase: "verde" };
    case "PRE_MARKET": return { texto: "◐ PREAPERTURA", clase: "ambar" };
    case "POST_MARKET": return { texto: "◐ POSTCIERRE", clase: "ambar" };
    case "CLOSED": return { texto: "○ CERRADO", clase: "tenue" };
    default: return null;
  }
}

const isMarketOpen = (status) => status === "OPEN" || status === "PRE_MARKET" || status === "POST_MARKET";

/**
 * Texto de "Actualizado" ajustado al estado de mercado. Con el mercado cerrado, la
 * frescura relativa ("hace 5 min") engañaria como si fuera un tick en vivo cuando en
 * realidad es el cierre definitivo: se prefija "cierre:". Si freshness() ya cayo en
 * su rama de fecha absoluta (>24h), el texto ya dice "último precio: ..." y no se
 * duplica el prefijo.
 */
function updateLabel(fr, closed) {
  if (!closed) return { texto: fr.texto, clase: fr.clase };
  const texto = fr.texto.startsWith("último precio") ? fr.texto : `cierre: ${fr.texto.replace(/^hace /, "")}`;
  return { texto, clase: "tenue" };
}

function notify(texto, clase = "") {
  const el = document.getElementById("aviso");
  el.textContent = texto;
  el.className = `aviso visible ${clase}`;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.className = "aviso"), 3600);
}

/* ------------------------------------------------------- modal de confirmacion */

/** Reemplaza confirm() del navegador: HU-21, "borrados con confirmacion, mostrando
 *  registro afectado antes". El <dialog> nativo da foco atrapado y ESC gratis; al
 *  cerrar, el foco vuelve a quien abrio el dialogo. Devuelve una promesa true/false. */
function confirmDialog(message, confirmLabel = "Eliminar") {
  return new Promise((resolve) => {
    const previouslyFocused = document.activeElement;
    const dialog = document.createElement("dialog");
    dialog.className = "dialogo-confirmar";

    const p = document.createElement("p");
    p.textContent = message;

    const acciones = document.createElement("div");
    acciones.className = "acciones-caja";
    const btnCancelar = document.createElement("button");
    btnCancelar.type = "button";
    btnCancelar.textContent = "Cancelar";
    const btnConfirmar = document.createElement("button");
    btnConfirmar.type = "button";
    btnConfirmar.className = "peligro";
    btnConfirmar.textContent = confirmLabel;
    acciones.append(btnCancelar, btnConfirmar);
    dialog.append(p, acciones);
    document.body.appendChild(dialog);

    let resolved = false;
    const finish = (result) => {
      if (resolved) return;
      resolved = true;
      resolve(result);
      dialog.close();
    };
    btnCancelar.addEventListener("click", () => finish(false));
    btnConfirmar.addEventListener("click", () => finish(true));
    dialog.addEventListener("cancel", () => finish(false)); // ESC nativo del <dialog>
    dialog.addEventListener("close", () => {
      dialog.remove();
      if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
    });

    dialog.showModal();
    btnCancelar.focus();
  });
}

/* ----------------------------------------------------------- glosario popover */

let _popoverEl = null;
let _popoverTimer = null;

function openGlossary(metricaEl) {
  closeGlossary();
  const etiqueta = metricaEl.dataset.glosario;
  const entrada = buscarGlosario(etiqueta);
  if (!entrada) return;

  const pop = document.createElement("div");
  pop.className = "glosario-popover";
  pop.setAttribute("role", "tooltip");
  pop.innerHTML =
    `<div class="gp-titulo">${esc(entrada.titulo)}</div>` +
    `<div class="gp-que">${esc(entrada.que_es)}</div>` +
    (entrada.rango ? `<div class="gp-rango"><span class="gp-rango-label">Rango tipico:</span> ${esc(entrada.rango)}</div>` : "") +
    (entrada.ideal ? `<div class="gp-ideal"><span class="gp-ideal-label">Lo ideal:</span> ${esc(entrada.ideal)}</div>` : "") +
    `<div class="gp-como">${esc(entrada.como_leerlo)}</div>` +
    `<div class="gp-trampa">⚠ ${esc(entrada.trampa)}</div>`;

  document.body.appendChild(pop);
  _popoverEl = pop;
  metricaEl.classList.add("metrica-activa");

  // Posicionar: debajo de la metrica, centrado
  const mr = metricaEl.getBoundingClientRect();
  let top = mr.bottom + 8 + window.scrollY;
  let left = mr.left + window.scrollX;
  pop.style.top = top + "px";
  pop.style.left = left + "px";

  // Ajustar si se sale por la derecha
  requestAnimationFrame(() => {
    const pr = pop.getBoundingClientRect();
    if (pr.right > window.innerWidth - 12) {
      pop.style.left = Math.max(12, window.innerWidth - pr.width - 12 + window.scrollX) + "px";
    }
    if (pr.left < 12) pop.style.left = (12 + window.scrollX) + "px";
    // Si se sale por abajo, arriba
    if (pr.bottom > window.innerHeight + window.scrollY - 12) {
      pop.style.top = (mr.top + window.scrollY - pr.height - 8) + "px";
      pop.classList.add("abajo");
    }
  });
}

function closeGlossary() {
  if (_popoverEl) { _popoverEl.remove(); _popoverEl = null; }
  document.querySelectorAll(".metrica-activa").forEach((el) => el.classList.remove("metrica-activa"));
  clearTimeout(_popoverTimer);
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeGlossary(); });
document.addEventListener("click", (e) => {
  if (_popoverEl && !_popoverEl.contains(e.target) && !e.target.closest(".metrica[data-glosario]")) {
    closeGlossary();
  }
});

/** tabindex + role + aria-label para que un elemento con popover sea alcanzable por teclado. */
function makeFocusable(el) {
  if (el.hasAttribute("tabindex")) return;
  el.tabIndex = 0;
  el.setAttribute("role", "button");
  if (!el.hasAttribute("aria-label")) el.setAttribute("aria-label", `Explicación de ${el.dataset.glosario}`);
}

function attachGlossary(metricaEl) {
  metricaEl.style.cursor = "help";
  makeFocusable(metricaEl);
  metricaEl.addEventListener("click", (e) => {
    e.stopPropagation();
    if (_popoverEl && metricaEl.classList.contains("metrica-activa")) { closeGlossary(); }
    else { openGlossary(metricaEl); }
  });
}

document.addEventListener("click", (e) => {
  const objetivo = e.target.closest("[data-glosario]");
  if (!objetivo) return;
  if (objetivo.classList.contains("metrica")) return; // ya enganchado
  e.stopPropagation();
  if (_popoverEl && objetivo.classList.contains("metrica-activa")) { closeGlossary(); }
  else { openGlossary(objetivo); }
});

// Enter/Espacio abren o cierran el popover igual que el clic. Cubre tanto las .metrica
// (con su propio listener de clic) como las cabeceras de tabla (enganchadas solo por
// delegacion): un solo listener generico sirve para ambas, porque openGlossary/
// closeGlossary no dependen de la clase del elemento.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const objetivo = e.target.closest("[data-glosario]");
  if (!objetivo) return;
  e.preventDefault(); // el espacio no debe desplazar la pagina
  if (_popoverEl && objetivo.classList.contains("metrica-activa")) { closeGlossary(); }
  else { openGlossary(objetivo); }
});

/* -------------------------------------------------------------- formateo */

const today = () => new Date().toISOString().slice(0, 10);

function formData(form) {
  const d = {};
  for (const [k, v] of new FormData(form).entries()) {
    const el = form.elements[k];
    if (el && el.type === "checkbox") { d[k] = el.checked; continue; }
    if (v === "" || v === null) continue;
    d[k] = el && el.type === "number" ? Number(v) : v;
  }
  return d;
}

function loading(id) {
  document.getElementById(id).innerHTML = '<div class="cargando">Cargando…</div>';
}

function renderError(id, e, retry) {
  const boton = retry ? ` <button type="button" class="mini" data-reintentar>Reintentar</button>` : "";
  const cont = document.getElementById(id);
  cont.innerHTML = `<div class="error">${esc(e.message || e)}${boton}</div>`;
  if (retry) cont.querySelector("[data-reintentar]").addEventListener("click", retry);
}

/* ------------------------------------------------------------- perfil inversor */

/* ------------------------------------------------------- pendientes del broker */

/** Lo que aun no has copied al broker, por broker. SeaLock no manda nada: esto es una
 *  lista de trabajo tuya. Se carga al abrir la app y al entrar en Cartera. */
async function loadPendientes() {
  const cont = document.getElementById("pendientes-broker");
  if (!cont) return;
  try {
    const d = await api("finance", "trades_pending");
    const grupos = Object.entries(d.by_broker || {});
    if (!grupos.length) {
      cont.innerHTML = "";
      return;
    }
    cont.innerHTML = `<b>Pendientes de copiar al broker: ${d.pending}</b> ` + grupos.map(
      ([k, v]) => `<span class="rec-badge">${esc(k)}: ${v.count}</span>`).join(" ") +
      `<div class="tenue" style="font-size:12px;margin-top:4px">SeaLock no envia nada a
        ninguna plataforma: esto es solo tu lista de trabajo.</div>
       <button id="btn-marcar-subido" style="margin-top:8px">Marcar todo como subido</button>`;
    document.getElementById("btn-marcar-subido").addEventListener("click", async () => {
      const r = await api("finance", "trades_mark_uploaded", {});
      notify(`${r.marked} operacion(es) marcadas como subidas`, "bueno");
      loadPendientes();
    });
  } catch (e) {
    cont.innerHTML = `<div class="tenue">No se pudieron leer los pendientes: ${esc(e.message)}</div>`;
  }
}

async function loadProfile() {
  try {
    const p = await api("finance", "profile_get");
    const form = document.getElementById("form-perfil");
    form.elements["risk_tolerance"].value = p.risk_tolerance;
    form.elements["horizon"].value = p.horizon;
    form.elements["preference"].value = p.preference;
  } catch (e) {
    // Si falla, el formulario se queda con los valores por defecto del HTML.
  }
}

/* ---------------------------------------------------------------- CARTERA */

async function loadPortfolio(silencioso = false) {
  if (!silencioso) loading("tabla-cartera");
  try {
    const d = await api("app", "portfolio_valued");
    PORTFOLIO_ANY_MARKET_OPEN = d.any_market_open !== false;
    const t = d.totals;

    document.getElementById("tarjetas-cartera").innerHTML = d.positions.length ? `
      <div class="tarjeta"><div class="etiqueta">Valor de mercado</div>
        <div class="cifra">${money(t.market_value)}</div>
        <div class="pie">coste ${money(t.cost)}</div></div>
      <div class="tarjeta"><div class="etiqueta" data-glosario="Plusvalia latente vs. realizada">Plusvalia latente</div>
        <div class="cifra ${sign(t.unrealized_gain)}">${money(t.unrealized_gain)}</div>
        <div class="pie">${pct(t.unrealized_gain_pct)}</div></div>
      <div class="tarjeta"><div class="etiqueta" data-glosario="Plusvalia latente vs. realizada">Realizado</div>
        <div class="cifra ${sign(t.accumulated_realized)}">${money(t.accumulated_realized)}</div>
        <div class="pie">ventas cerradas</div></div>` : "";
    document.querySelectorAll("#tarjetas-cartera [data-glosario]").forEach(makeFocusable);

    let avisos = "";
    if (d.no_price && d.no_price.length) {
      avisos += `<div class="nota">Sin precio de mercado para ${esc(d.no_price.join(", "))}.
        Esas posiciones no suman al total: el patrimonio mostrado es incompleto, no cero.</div>`;
    }
    if (d.currency_mismatch && d.currency_mismatch.length) {
      avisos += `<div class="nota"><b>Revisa la moneda de estas operaciones:</b>
        ${d.currency_mismatch.map((x) =>
          `${esc(x.ticker)} anotado en ${esc(x.cost_currency)} pero cotiza en
           ${esc(x.quote_currency)}`).join("; ")}.
        El P&L se calcula convirtiendo ambas a ${esc(CURRENCY)}, pero si te equivocaste al
        registrar la compra el coste estara mal.</div>`;
    }
    if (avisos) document.getElementById("tarjetas-cartera").insertAdjacentHTML("afterend", avisos);

    document.getElementById("tabla-cartera").innerHTML = d.positions.length
      ? `<div class="tabla-scroll"><table>
          <thead><tr><th>Ticker</th><th class="num">Cant.</th><th class="num" data-glosario="Coste medio">Coste medio</th>
            <th class="num">Precio</th><th class="num" data-glosario="Variacion del dia">Dia</th><th>Mercado</th><th>Actualizado</th>
            <th class="num">Valor</th><th class="num" data-glosario="Plusvalia latente vs. realizada">P&L</th><th class="num">%</th>
            <th class="num" data-glosario="Peso en cartera">Peso</th><th class="num" data-glosario="Nota">Nota</th>
            <th>Broker</th></tr></thead>
          <tbody>${d.positions.map((p) => {
            const fr = freshness(p.age_seconds, p.price_as_of);
            const badge = marketStatusBadge(p.market_status);
            const closed = p.market_status === "CLOSED";
            const act = updateLabel(fr, closed);
            return `
            <tr>
              <td><span class="ticker">${esc(p.ticker)}</span>
                ${p.name ? `<div class="tenue" style="font-size:12px">${esc(p.name)}</div>` : ""}</td>
              <td class="num">${number(p.quantity, 4)}</td>
              <td class="num tenue">${money(p.average_cost, p.cost_currency)}</td>
              <td class="num">${p.unvalued ? "—" : money(p.price, p.currency)}</td>
              <td class="num ${sign(p.day_change_pct)}">${pct(p.day_change_pct)}</td>
              <td class="${badge ? badge.clase : "tenue"}" style="font-size:12px">${badge ? esc(badge.texto) : "—"}</td>
              <td class="${act.clase}" title="${esc(p.price_as_of || "")}">${p.unvalued ? "—" : esc(act.texto)}</td>
              <td class="num">${p.unvalued ? "—" : money(p.market_value, p.currency)}</td>
              <td class="num ${sign(p.gain)}">${p.gain === null ? "—" : money(p.gain)}</td>
              <td class="num ${sign(p.gain_pct)}">${pct(p.gain_pct)}</td>
              <td class="num tenue">${p.weight_pct !== null ? `${number(p.weight_pct, 1)} %` : "—"}</td>
              <td class="num" data-nota="${esc(p.ticker)}">…</td>
              <td class="tenue" style="font-size:12px">${esc(p.brokers || "—")}</td>
            </tr>`;}).join("")}
          </tbody></table></div>`
      : '<div class="vacio">Sin posiciones abiertas. <a href="#" data-help="cartera">Como registrar una compra</a></div>';
    document.querySelectorAll("#tabla-cartera [data-glosario]").forEach(makeFocusable);
    if (d.positions.length) loadPortfolioScores();
  } catch (e) {
    if (silencioso) notify(`No se pudo actualizar la cartera: ${e.message}`, "malo");
    else renderError("tabla-cartera", e, () => loadPortfolio());
  }
  if (!silencioso) loadWatchlist();
}

/** Rellena la columna Nota tras pintar la tabla: el servidor cachea 1h por ticker,
 * asi que llamar aqui no dispara una tanda nueva de peticiones a Yahoo cada vez. */
async function loadPortfolioScores() {
  try {
    const d = await api("app", "portfolio_scores");
    for (const [ticker, s] of Object.entries(d.scores || {})) {
      const cell = document.querySelector(`[data-nota="${ticker}"]`);
      if (!cell) continue;
      if (!s || s.error) { cell.innerHTML = '<span class="tenue">—</span>'; continue; }
      const aviso = s.final_score < 4 ? ' <span class="ambar" title="Nota baja: revisar">⚠</span>' : "";
      cell.innerHTML = `<span class="${s.sentiment}">${notaMostrada(s)}/10</span>${marcaRevision(s)}${aviso}`;
      cell.title = s.label;
    }
  } catch (e) {
    // Silencioso: la tabla ya se mostro sin notas, no es un fallo bloqueante.
  }
}

/** Exporta cartera, seguimiento y revisiones. Todo se resuelve en el navegador: el
 *  servidor devuelve filas y aqui se arma el fichero. Sin red, sin escribir en disco. */
async function exportar(formato) {
  try {
    const d = await api("app", "export_all");
    const nombre = `sealock-${today()}`;
    if (formato === "json") {
      descargar(`${nombre}.json`, JSON.stringify(d, null, 2), "application/json");
      return;
    }
    const tablas = ["trades", "watchlist", "reviews"];
    const celda = (v) => {
      const s = v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const columnas = [...new Set(tablas.flatMap((t) => (d[t] || []).flatMap(Object.keys)))];
    const lineas = [["tabla", ...columnas].join(",")];
    for (const t of tablas)
      for (const fila of d[t] || []) lineas.push([t, ...columnas.map((c) => celda(fila[c]))].join(","));
    descargar(`${nombre}.csv`, lineas.join("\n"), "text/csv");
  } catch (e) {
    notify(`No se pudo exportar: ${e.message}`, "malo");
  }
}

function descargar(nombre, contenido, tipo) {
  const url = URL.createObjectURL(new Blob([contenido], { type: tipo }));
  const a = document.createElement("a");
  a.href = url;
  a.download = nombre;
  a.click();
  URL.revokeObjectURL(url);
}

async function loadWatchlist() {
  try {
    const d = await api("finance", "watchlist_list");
    document.getElementById("tabla-watchlist").innerHTML = d.watchlist.length
      ? `<div class="tabla-scroll"><table>
          <thead><tr><th>Ticker</th><th>Tesis</th><th class="num">Objetivo</th><th></th></tr></thead>
          <tbody>${d.watchlist.map((w) => `
            <tr>
              <td><span class="ticker">${esc(w.ticker)}</span></td>
              <td class="tenue">${esc(w.thesis || "")}</td>
              <td class="num">${w.target_price ? money(w.target_price) : "—"}</td>
              <td class="num">
                <button class="mini" data-ver="${esc(w.ticker)}">Analizar</button>
                <button class="mini peligro" data-quitar="${esc(w.ticker)}">Quitar</button></td>
            </tr>`).join("")}
          </tbody></table></div>`
      : '<div class="vacio">Sin valores en seguimiento.</div>';

    document.querySelectorAll("[data-quitar]").forEach((b) => {
      const w = d.watchlist.find((x) => x.ticker === b.dataset.quitar);
      b.addEventListener("click", async () => {
        const ok = await confirmDialog(
          `¿Quitar ${w.ticker}${w.name ? ` (${w.name})` : ""} de seguimiento? Se perdera la tesis y el objetivo guardados.`);
        if (!ok) return;
        await api("finance", "watchlist_remove", { ticker: w.ticker });
        loadWatchlist();
      });
    });
    document.querySelectorAll("[data-ver]").forEach((b) =>
      b.addEventListener("click", () => { changeView("acciones"); viewStock(b.dataset.ver); }));
  } catch (e) {
    renderError("tabla-watchlist", e, () => loadWatchlist());
  }
}

/* --------------------------------------------------------------- ACCIONES */

async function search(query) {
  loading("resultados-busqueda");
  try {
    const d = await api("market", "ticker_search", { query });
    if (!d.results.length) {
      document.getElementById("resultados-busqueda").innerHTML =
        '<div class="vacio">Sin resultados. Prueba con el ticker directamente.</div>';
      return;
    }
    document.getElementById("resultados-busqueda").innerHTML = `
      <div class="tabla-scroll"><table>
        <thead><tr><th>Ticker</th><th>Nombre</th><th>Mercado</th><th></th></tr></thead>
        <tbody>${d.results.slice(0, 8).map((r) => `
          <tr>
            <td><span class="ticker">${esc(r.ticker)}</span>
              ${r.found_via ? '<div class="tenue" style="font-size:11px">no listado por Yahoo</div>' : ""}</td>
            <td>${esc(r.name || "")}</td>
            <td class="tenue">${esc(r.market || "")}
              ${r.warning ? '<div class="ambar" style="font-size:11px">OTC · baja liquidez</div>' : ""}</td>
            <td class="num"><button class="mini" data-ficha="${esc(r.ticker)}">Analizar</button></td>
          </tr>`).join("")}
        </tbody></table></div>
      ${d.available_markets ? `<div class="nota">Cotiza en varios mercados; el precio y la
        moneda cambian segun cual elijas. El principal suele ser el de su pais de origen.</div>` : ""}`;

    document.querySelectorAll("[data-ficha]").forEach((b) =>
      b.addEventListener("click", () => viewStock(b.dataset.ficha)));
  } catch (e) {
    renderError("resultados-busqueda", e, () => search(query));
  }
}

/* Noticias de la ficha (HU-05): endpoint aparte para que la ficha no las espere. Se
 * guardan por ticker para que el auto-refresco de 60 s no parpadee; el servidor ya
 * cachea 30 min. */
let NEWS = { ticker: null, data: null, tab: "company" };

async function loadNews(ticker) {
  if (NEWS.ticker !== ticker) NEWS = { ticker, data: null, tab: "company" };
  if (NEWS.data) { renderNews(); return; }
  renderNews();
  try {
    const d = await api("app", "stock_news", { ticker });
    if (NEWS.ticker !== ticker) return;
    NEWS.data = d;
  } catch (e) {
    if (NEWS.ticker === ticker) NEWS.data = { error: e.message };
  }
  renderNews();
}

function relativeTime(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!(s >= 0)) return "";
  if (s < 3600) return `hace ${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400) return `hace ${Math.round(s / 3600)} h`;
  return `hace ${Math.round(s / 86400)} d`;
}

function renderNews() {
  const lista = document.getElementById("noticias-lista");
  if (!lista) return;
  document.querySelectorAll("[data-news-tab]").forEach((b) => {
    b.classList.toggle("activa", b.dataset.newsTab === NEWS.tab);
    b.setAttribute("aria-selected", b.dataset.newsTab === NEWS.tab);
  });
  const d = NEWS.data;
  if (!d) { lista.innerHTML = '<div class="cargando">Cargando noticias…</div>'; return; }
  const items = d[NEWS.tab] || [];
  if (d.error || (!(d.company || []).length && !(d.sector || []).length)) {
    lista.innerHTML = `<div class="vacio">No se pudieron cargar noticias.
      <button type="button" class="mini" id="btn-reintentar-noticias">Reintentar</button></div>`;
    document.getElementById("btn-reintentar-noticias").addEventListener("click", () => {
      NEWS.data = null;
      loadNews(NEWS.ticker);
    });
    return;
  }
  if (NEWS.tab === "sector" && !d.sector_query) {
    lista.innerHTML = '<div class="vacio">Sin sector conocido para este valor (ETF o dato ausente), asi que no hay noticias de sector.</div>';
    return;
  }
  if (!items.length) { lista.innerHTML = '<div class="vacio">Sin noticias recientes.</div>'; return; }
  // Las noticias son dato, no instruccion: todo va por textContent, nunca innerHTML.
  lista.replaceChildren(...items.map((n) => {
    const caja = document.createElement("div");
    caja.className = "noticia";
    const a = document.createElement("a");
    a.textContent = n.headline;
    if (/^https?:\/\//i.test(n.link || "")) a.href = n.link;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = [n.source, n.published ? relativeTime(n.published) : n.date].filter(Boolean).join(" · ");
    caja.append(a, meta);
    return caja;
  }));
}

async function viewStock(ticker, assumptions = {}, silencioso = false, fromRec = null, refresh = false) {
  const cont = document.getElementById("ficha");
  CURRENT_STOCK = { ticker, assumptions };
  if (!silencioso) REC_RETURN_INDEX = fromRec;

  if (!silencioso) {
    cont.innerHTML = `<div class="caja"><div class="cargando">Analizando ${esc(ticker)}…</div></div>`;
    cont.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  const boton = document.getElementById("btn-actualizar-nota");
  if (boton) { boton.disabled = true; boton.textContent = "Actualizando…"; }

  try {
    const d = await api("app", "stock_detail", { ticker, ...assumptions, refresh });
    const c = d.quote || {}, f = d.fundamentals || {}, h = d.historical || {};
    const v = f.valuation || {}, r = f.profitability || {}, s = f.financial_strength || {};
    CURRENT_STOCK.marketStatus = c.status;

    const metrica = (k, val) => `<div class="metrica" data-glosario="${esc(k)}"><div class="k">${k}</div><div class="v">${val}</div></div>`;

    cont.innerHTML = `
      <div class="caja">
        <div class="ficha-cabecera">
          <div>
            <h2 style="font-size:19px;margin:0">${esc(f.name || c.name || ticker)}</h2>
            <div class="tenue">${esc(ticker)}${f.sector ? ` · ${esc(f.sector)}` : ""}${c.market ? ` · ${esc(c.market)}` : ""}</div>
          </div>
          <div style="text-align:right">
            <div class="ficha-precio">${money(c.price, c.currency)}</div>
            <div class="${sign(c.change_pct)}">${pct(c.change_pct, 2)} hoy</div>
            ${(() => {
              const fr = freshness(c.age_seconds, c.price_as_of);
              const badge = marketStatusBadge(c.status);
              const act = updateLabel(fr, c.status === "CLOSED");
              return `${badge ? `<div class="${badge.clase}" style="font-size:12px;font-weight:600">${esc(badge.texto)}</div>` : ""}
                <div class="${act.clase}" style="font-size:12px" title="${esc(c.price_as_of || "")}">${esc(act.texto)}</div>`;
            })()}
          </div>
        </div>
        ${c.status === "CLOSED" ? `<div class="nota" style="margin-top:12px">Mercado cerrado: este es el
          <b>ultimo precio de cierre</b>, no una cotizacion en vivo.
          ${c.data_delay_min ? `Datos con ${c.data_delay_min} min de retraso en horario de mercado.` : ""}</div>` : ""}
        ${renderScore(d.score, d.score_age_seconds)}
        ${d.position ? `<div class="metricas" style="margin-top:14px">
          ${metrica("Cantidad", number(d.position.quantity, 4))}
          ${metrica("Coste medio", money(d.position.average_cost, d.position.currency))}
          ${metrica("Invertido", money(d.position.invested, d.position.currency))}
          ${metrica("Valor actual", d.position.current_value !== null ? money(d.position.current_value, d.position.currency) : "—")}
          ${metrica("P&L", d.position.gain !== null ? `${money(d.position.gain, d.position.currency)} (${pct(d.position.gain_pct)})` : "—")}
        </div>` : ""}
        <div class="metricas" style="margin-top:14px">
          ${metrica("PER", number(v.per_trailing, 1))}
          ${metrica("PER fwd", number(v.per_forward, 1))}
          ${metrica("EV/EBITDA", number(v.ev_ebitda, 1))}
          ${metrica("Margen neto", r.net_margin_pct !== undefined ? `${number(r.net_margin_pct, 1)} %` : "—")}
          ${metrica("ROE", r.roe_pct !== undefined ? `${number(r.roe_pct, 1)} %` : "—")}
          ${metrica("Capitaliz.", compact(f.market_cap))}
          ${metrica("Deuda neta", compact(s.net_debt))}
          ${metrica("Dividendo", f.dividend?.yield_pct ? `${number(f.dividend.yield_pct, 2)} %` : "—")}
        </div>
        ${f.error ? `<div class="error">Fundamentales no disponibles: ${esc(f.error)}</div>` : ""}
        ${f.derived_fields ? `<div class="nota"><b>Campos derivados:</b>
          ${esc(f.derived_fields.join("; "))}. Yahoo no los publica para este valor;
          se han calculado por identidad contable.</div>` : ""}
      </div>

      <div class="rejilla-2">
        <div class="caja">
          <h2>Comportamiento (1 año)</h2>
          <div class="metricas">
            ${metrica("Rentabilidad", h.period_return_pct !== undefined ? pct(h.period_return_pct) : "—")}
            ${metrica("Volatilidad", h.annualized_volatility_pct ? `${number(h.annualized_volatility_pct, 1)} %` : "—")}
            ${metrica("Max. caida", h.max_drawdown_pct !== undefined ? `${number(h.max_drawdown_pct, 1)} %` : "—")}
            ${metrica("RSI 14", number(h.rsi_14, 0))}
            ${metrica("Media 50", number(h.sma_50, 2))}
            ${metrica("Media 200", number(h.sma_200, 2))}
          </div>
        </div>
        <div class="caja">
          <h2>Valor intrinseco</h2>
          <form id="form-dcf" class="form-rejilla" style="margin-bottom:14px">
            <label>Crecim. % <input name="growth_pct" type="number" step="0.5"
              value="${assumptions.growth_pct ?? 8}"></label>
            <label>Descuento % <input name="discount_rate_pct" type="number" step="0.5"
              value="${assumptions.discount_rate_pct ?? 9}"></label>
            <label>Terminal % <input name="terminal_growth_pct" type="number" step="0.1"
              value="${assumptions.terminal_growth_pct ?? 2.5}"></label>
            <button type="submit">Recalcular</button>
          </form>
          ${renderValuation(d, c)}
        </div>
      </div>

      ${renderQuarterly(d.quarterly)}

      ${renderEstimates(d.estimates)}

      <div class="caja">
        <div class="cabecera-caja">
          <h2>Preguntar a Claude</h2>
          <a class="btn-ayuda" href="#" data-section="preguntar" title="Mas informacion">?</a>
        </div>
        <div class="tenue" style="font-size:12px;margin-bottom:10px">
          Le pasa los numeros de esta ficha (precio, nota, pilares, trimestres,
          <b>proyecciones</b> y noticias) y puede buscar en internet. <b>Es la unica parte de
          SeaLock que sale de tu equipo</b>: solo al pulsar el boton.
        </div>
        <form id="form-ask" class="form-linea" style="flex-wrap:wrap">
          <input name="question" placeholder="¿Esta cara ya? ¿Que mire en el proximo trimestre?"
            style="flex:1;min-width:240px" maxlength="2000" required>
          <label class="check"><input name="include_position" type="checkbox">Incluir mi posicion</label>
          <label class="check"><input name="web_search" type="checkbox" checked>Buscar en internet</label>
          <button type="submit" class="principal">Preguntar</button>
        </form>
        <div class="acciones-caja" style="margin-top:12px">
          <button id="btn-copiar-prompt" title="Copiar al portapapeles los datos, las URLs, tu perfil y tu cartera, para pegarlos en cualquier IA">
            Copiar prompt para otra IA</button>
        </div>
        <div id="respuesta-ask"></div>
      </div>

      <div class="caja">
        <h2>Noticias</h2>
        <div class="subpestanas" role="tablist">
          <button type="button" role="tab" data-news-tab="company">Empresa</button>
          <button type="button" role="tab" data-news-tab="sector">Sector</button>
        </div>
        <div id="noticias-lista"><div class="cargando">Cargando noticias…</div></div>
      </div>

      <div class="caja">
        <div class="acciones-caja">
          <button id="btn-seguir">Anadir a seguimiento</button>
          <button id="btn-actualizar-nota" title="Volver a pedir los datos y recalcular la nota">Actualizar nota</button>
          ${REC_RETURN_INDEX !== null ? '<button id="btn-volver-rec">← Volver a recomendaciones</button>' : ""}
        </div>
        <p class="ayuda" style="margin:12px 0 0">
          Datos de fuentes publicas, precios diferidos ~15 min. Analisis, no recomendacion
          de inversion: la decision es tuya.</p>
      </div>`;

    cont.querySelectorAll(".metrica[data-glosario]").forEach(attachGlossary);
    cont.querySelectorAll("[data-news-tab]").forEach((b) =>
      b.addEventListener("click", () => { NEWS.tab = b.dataset.newsTab; renderNews(); }));
    loadNews(ticker);

    const formAsk = document.getElementById("form-ask");
    document.getElementById("btn-copiar-prompt").addEventListener("click", async (ev) => {
      const boton = ev.currentTarget;
      boton.disabled = true;
      boton.textContent = "Preparando…";
      try {
        const r = await api("app", "build_prompt", { ticker });
        // API nativa del navegador; sin librerias.
        await navigator.clipboard.writeText(r.text);
        notify("Prompt copiado. Pégalo donde quieras: incluye tu perfil y tu cartera.", "bueno");
      } catch (e) {
        notify(`No se pudo copiar: ${e.message}`, "malo");
      } finally {
        boton.disabled = false;
        boton.textContent = "Copiar prompt para otra IA";
      }
    });

    formAsk.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const d = formData(ev.target);
      const salida = document.getElementById("respuesta-ask");
      const boton = formAsk.querySelector("button[type=submit]");
      boton.disabled = true;
      boton.textContent = "Pensando…";
      salida.innerHTML = '<div class="cargando">Consultando…</div>';
      try {
        const r = await api("app", "ask", {
          ticker,
          question: d.question,
          include_position: !!d.include_position,
          web_search: d.web_search !== false,
        });
        // La respuesta viene de fuera: se pinta como texto, nunca como HTML.
        salida.innerHTML = "";
        const meta = document.createElement("div");
        meta.className = "tenue";
        meta.style.fontSize = "12px";
        meta.textContent = `${r.model}${r.searched ? " · consultó internet" : ""}`
          + (r.sent_position ? " · incluye tu posición" : " · sin tu posición");
        const cuerpo = document.createElement("div");
        cuerpo.className = "respuesta-ask";
        cuerpo.textContent = r.answer;
        salida.append(meta, cuerpo);
      } catch (e) {
        salida.innerHTML = `<div class="error">${esc(e.message)}</div>`;
      } finally {
        boton.disabled = false;
        boton.textContent = "Preguntar";
      }
    });

    document.getElementById("form-dcf").addEventListener("submit", (ev) => {
      ev.preventDefault();
      viewStock(ticker, formData(ev.target), false, REC_RETURN_INDEX);
    });
    document.getElementById("btn-actualizar-nota").addEventListener("click", () => {
      CURRENT_STOCK.assumptions = formData(document.getElementById("form-dcf"));
      viewStock(ticker, CURRENT_STOCK.assumptions, false, REC_RETURN_INDEX, true);
    });
    document.getElementById("btn-seguir").addEventListener("click", async () => {
      await api("finance", "watchlist_add", { ticker, name: f.name || null });
      notify(`${ticker} anadido a seguimiento`, "bueno");
    });
    document.getElementById("btn-volver-rec")?.addEventListener("click", () => {
      const idx = REC_RETURN_INDEX;
      changeView("recomendaciones");
      openRecViewer(idx);
    });
  } catch (e) {
    if (silencioso) notify(`No se pudo actualizar ${ticker}: ${e.message}`, "malo");
    else renderError("ficha", e, () => viewStock(ticker, assumptions, false, fromRec));
  }
}

/** Caja "Resultados trimestrales": de donde sale el pilar de resultados recientes.
 *  Inline SVG, sin librerias, y "—" en toda celda sin dato. */
function renderQuarterly(q) {
  const quarters = (q && q.quarters) || [];
  const cur = quarters.find((x) => x.currency) || quarters[0] || {};
  const moneda = cur.currency || "";

  let html = `<div class="caja">
    <div class="cabecera-caja">
      <h2>Resultados trimestrales</h2>
      <a class="btn-ayuda" href="#" data-section="trimestres" title="Mas informacion">?</a>
    </div>`;
  if (!quarters.length) {
    html += `<div class="vacio">${esc(
      (q && q.note_es) || "Sin datos trimestrales publicos para este valor.")}</div></div>`;
    return html;
  }
  if (q.note_es && q.available < 3) html += `<div class="nota">${esc(q.note_es)}</div>`;

  const ingresos = quarters.map((x) => x.revenue);
  html += `<div class="trimestres-grafico">${barrasSVG(ingresos)}${
    moneda ? `<span class="tenue">Ventas en ${esc(moneda)}</span>` : ""}</div>`;

  html += `<table class="tabla">
    <thead><tr>
      <th>Cierre</th>
      <th class="num">Ventas${moneda ? ` (${esc(moneda)})` : ""}</th>
      <th class="num">Interanual</th>
      <th class="num">Beneficio</th>
      <th class="num">BPA</th>
      <th class="num">Margen</th>
    </tr></thead>
    <tbody>${quarters.map((x) => `<tr>
      <td>${esc(x.period_end || "—")}</td>
      <td class="num">${fila(x.revenue, (v) => compact(v))}</td>
      <td class="num ${sign(x.revenue_yoy_pct)}">${fila(x.revenue_yoy_pct, (v) => pct(v))}</td>
      <td class="num">${fila(x.net_income, (v) => compact(v))}</td>
      <td class="num">${fila(x.diluted_eps, (v) => number(v, 2))}</td>
      <td class="num">${fila(x.net_margin_pct, (v) => `${number(v, 1)} %`)}</td>
    </tr>`).join("")}</tbody></table>`;
  if (q.missing_fields && q.missing_fields.length) {
    html += `<div class="tenue" style="margin-top:8px">Sin dato en los tres trimestres:
      ${esc(q.missing_fields.join(", "))}.</div>`;
  }
  if (q.gaps && q.gaps.length) {
    html += `<div class="tenue" style="margin-top:4px">Huecos: ${esc(
      q.gaps.map((g) => g.join(" → ")).join("; "))}.</div>`;
  }
  return html + `</div>`;
}

/** Barras verticales SVG con el valor dentro. Una barra por trimestre, en el mismo orden
 *  que la tabla: si no hay dato, hueco; sin ventas positivas, no hay grafico. */
function barrasSVG(valores) {
  const utiles = valores.filter((v) => typeof v === "number" && v > 0);
  if (!utiles.length) return "";
  const max = Math.max(...utiles);
  const ancho = 34, gap = 12, alto = 74, base = alto;
  const svg = valores.map((v, i) => {
    const x = i * (ancho + gap) + 2;
    if (typeof v !== "number" || v <= 0) return "";
    const h = Math.max(2, Math.round((v / max) * 64));
    return `<rect x="${x}" y="${base - h}" width="${ancho}" height="${h}" rx="3"
      fill="var(--acento)"><title>${esc(compact(v))}</title></rect>`;
  }).join("");
  const texto = valores.map((v) => (typeof v === "number" ? compact(v) : "sin dato")).join(", ");
  return `<svg width="${valores.length * (ancho + gap)}" height="${alto + 4}"
    viewBox="0 0 ${valores.length * (ancho + gap)} ${alto + 4}" role="img"
    aria-label="Ventas por trimestre: ${esc(texto)}">${svg}</svg>`;
}

/** Celda de tabla: "—" cuando no hay dato, con el formato que le pase el que llama. */
function fila(v, fmt = (x) => x) {
  return v === null || v === undefined ? "—" : fmt(v);
}

/** Caja "Proyecciones": el consenso de analistas hacia delante. Es una prevision, no un
 *  hecho, y la nota solo usa el balance de revisiones, con poco peso. */
function renderEstimates(e) {
  const periods = (e && e.periods) || [];
  if (!periods.length) {
    return `<div class="caja"><div class="cabecera-caja"><h2>Proyecciones</h2>
      <a class="btn-ayuda" href="#" data-section="proyecciones" title="Mas informacion">?</a></div>
      <div class="vacio">Sin proyecciones publicas para este valor.</div></div>`;
  }
  const t = e.target || {};
  const revisionPositiva = (e.revisions_net_30d || 0) > 0;
  return `<div class="caja">
    <div class="cabecera-caja">
      <h2>Proyecciones</h2>
      <a class="btn-ayuda" href="#" data-section="proyecciones" title="Mas informacion">?</a>
    </div>
    <div class="tenue" style="font-size:12px;margin-bottom:10px">
      Consenso de analistas${t.analysts ? ` (${t.analysts})` : ""}, no un hecho.
      La nota solo usa el balance de revisiones, y con poco peso.</div>
    <div class="tabla-scroll"><table>
      <thead><tr><th>Periodo</th><th>Cierre</th><th class="num">BPA estimado</th>
        <th class="num">Analistas</th><th class="num">Ventas estimadas</th>
        <th class="num">Crecimiento esperado</th></tr></thead>
      <tbody>${periods.map((p) => `<tr>
        <td>${esc(p.period_es || p.period)}</td>
        <td class="tenue">${esc(fila(p.end_date))}</td>
        <td class="num">${fila(p.eps, (v) => number(v, 2))}</td>
        <td class="num">${fila(p.analysts, (v) => number(v, 0))}</td>
        <td class="num">${fila(p.revenue, (v) => compact(v))}</td>
        <td class="num ${sign(p.revenue_growth_pct)}">${fila(p.revenue_growth_pct, (v) => pct(v))}</td>
      </tr>`).join("")}</tbody></table></div>
    <div class="metricas" style="margin-top:12px">
      <div class="metrica"><div class="k">Revisiones 30 d</div>
        <div class="v ${revisionPositiva ? "verde" : (e.revisions_net_30d || 0) < 0 ? "rojo" : ""}">
          ${number(e.revisions_up_30d, 0)} arriba / ${number(e.revisions_down_30d, 0)} abajo</div></div>
      <div class="metrica" data-glosario="Objetivo de analistas"><div class="k">Objetivo medio</div>
        <div class="v">${fila(t.mean, (v) => number(v, 2))}</div></div>
      <div class="metrica"><div class="k">Rango</div>
        <div class="v">${fila(t.low, (v) => number(v, 2))} – ${fila(t.high, (v) => number(v, 2))}</div></div>
    </div>
    ${t.recommendation ? `<div class="tenue" style="font-size:12px;margin-top:8px">Consenso: ${esc(t.recommendation)}.</div>` : ""}
  </div>`;
}

/** La nota que se ve es la final (mecanica + ajuste de Claude). Sin decimales si no hay
 *  revision, para no ensuciar; con decimales cuando el ajuste los cambia. */
function edadLegible(segundos) {
  if (segundos < 90) return `${segundos} s`;
  if (segundos < 5400) return `${Math.round(segundos / 60)} min`;
  if (segundos < 172800) return `${Math.round(segundos / 3600)} h`;
  return `${Math.round(segundos / 86400)} dias`;
}

function notaMostrada(s) {
  const final = s.final_score ?? s.score;
  const decimales = s.review && s.review_applied && Math.abs(final - s.score) > 0.05 ? 1 : 0;
  return number(final, decimales);
}

/** Marca pequena de "hay revision de Claude" en listas. */
function marcaRevision(s) {
  if (!s || !s.review || !s.review_applied) return "";
  const signo = s.review.adjustment >= 0 ? "+" : "";
  return `<span class="revision-marca" title="Revision de Claude ${signo}${number(s.review.adjustment, 1)}">Claude</span>`;
}

function renderReview(s) {
  const r = s && s.review;
  if (!r) {
    return `<div class="metrica tenue" data-glosario="Ajuste de Claude" style="background:none;padding:6px 0;font-size:12px">
      Sin revision de Claude. Pidele <i>"analiza ${esc((s && s.ticker) || "una accion")}"</i> en Claude Code para anadirla.</div>`;
  }
  const signo = r.adjustment >= 0 ? "+" : "";
  const estado = r.status === "active" ? "vigente" : r.status === "expired" ? "caducada" : "obsoleta";
  const explica = r.status === "expired"
    ? "Se caduco: paso su fecha de caducidad."
    : r.status === "stale"
      ? `Quedo obsoleta: hay un trimestre mas nuevo que el visto (${esc(r.last_quarter_seen || "?")}).`
      : "";
  let html = `<div class="revision ${r.status === "active" ? "" : "revision-tachada"}">
    <div class="revision-cabecera">
      <span class="metrica" data-glosario="Nota mecanica">Nota mecanica ${number(s.score, 1)}</span>
      <span class="metrica" data-glosario="Ajuste de Claude">Revision Claude ${signo}${number(r.adjustment, 1)}</span>
      <span class="metrica" data-glosario="Nota final">Nota final ${number(s.final_score ?? s.score, 1)}</span>
      <span class="tenue">(${estado})</span>
    </div>
    <div>${esc(r.rationale)}</div>`;
  if (explica) html += `<div class="nota">${esc(explica)}</div>`;
  if (r.sources && r.sources.length) {
    html += `<div class="revision-fuentes">Fuentes: `;
    html += r.sources
      .map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(u)}</a>`)
      .join(" &middot; ");
    html += `</div>`;
  }
  html += `<div class="tenue">${esc(r.created_at || "")}${r.expires_at ? ` &middot; caduca ${esc(r.expires_at)}` : ""}</div>`;
  return html + `</div>`;
}

function renderScore(s, edadSegundos) {
  if (!s) return "";
  const color = s.sentiment || "ambar";
  let html = `<div class="veredicto veredicto-${color}">`;
  html += `<div class="veredicto-header">
      <span class="metrica" data-glosario="Nota" style="font-size:22px;font-weight:700;margin-right:10px">${notaMostrada(s)}/10</span>
      <span>${esc(s.label)}</span>
    </div>`;
  html += renderReview(s);
  if (edadSegundos !== undefined && edadSegundos !== null) {
    html += `<div class="tenue" style="font-size:12px;margin-top:6px">Nota calculada hace ${
      esc(edadLegible(edadSegundos))}. Los datos se refrescan al pedirlo, no cada 60 s.</div>`;
  }
  if (s.coverage_warning) html += `<div class="nota">${esc(s.coverage_warning)}</div>`;

  (s.pillars || []).filter((p) => p.subscore !== null).forEach((p) => {
    const anchoPct = Math.round((p.subscore + 1) * 50); // -1..1 -> 0..100
    html += `<div class="progreso">
      <div class="fila"><span class="metrica" data-glosario="${esc(p.name_es)}">${esc(p.name_es)} (${number(p.weight, 0)} %)</span>
        <b>${number(p.subscore, 2)}</b></div>
      <div class="barra"><div class="relleno" style="width:${anchoPct}%"></div></div>
    </div>`;
  });

  if (s.pros && s.pros.length) {
    html += `<div class="veredicto-seccion"><div class="veredicto-subtitulo verde">A favor</div><ul>`;
    s.pros.forEach((t) => { html += `<li>${esc(t)}</li>`; });
    html += `</ul></div>`;
  }
  if (s.cons && s.cons.length) {
    html += `<div class="veredicto-seccion"><div class="veredicto-subtitulo rojo">En contra</div><ul>`;
    s.cons.forEach((t) => { html += `<li>${esc(t)}</li>`; });
    html += `</ul></div>`;
  }
  if (s.red_flags && s.red_flags.length) {
    html += `<div class="veredicto-seccion"><div class="veredicto-subtitulo rojo">Senales de alerta</div><ul>`;
    s.red_flags.forEach((t) => { html += `<li>${esc(t)}</li>`; });
    html += `</ul></div>`;
  }
  html += `<div class="metrica tenue" data-glosario="Cobertura" style="background:none;padding:6px 0;font-size:12px">Cobertura de datos: ${number(s.coverage_pct, 0)} %</div>`;
  html += `<div class="veredicto-aviso">${esc(s.notice)}</div>`;
  html += `</div>`;
  return html;
}

function renderValuation(d, cot) {
  const dcf = d.dcf || {}, gra = d.graham || {};
  let html = "";

  if (dcf.intrinsic_value_per_share) {
    html += `<div class="metricas" style="margin-bottom:12px">
      <div class="metrica" data-glosario="Valor DCF"><div class="k">Valor DCF</div>
        <div class="v">${money(dcf.intrinsic_value_per_share, cot.currency)}</div></div>
      <div class="metrica" data-glosario="Margen seguridad"><div class="k">Margen seguridad</div>
        <div class="v ${sign(dcf.margin_of_safety_pct)}">${pct(dcf.margin_of_safety_pct)}</div></div>
    </div>`;
    if (dcf.sensitivity) {
      const cols = Object.keys(dcf.sensitivity[0]).filter((k) => k !== "growth_pct");
      html += `<div class="tabla-scroll"><table>
        <thead><tr><th>Crecim.</th>${cols.map((c) => `<th class="num">${esc(c)}</th>`).join("")}</tr></thead>
        <tbody>${dcf.sensitivity.map((f) => `<tr>
          <td class="tenue">${number(f.growth_pct, 1)} %</td>
          ${cols.map((c) => `<td class="num">${number(f[c], 2)}</td>`).join("")}
        </tr>`).join("")}</tbody></table></div>
        <p class="ayuda" style="margin-top:10px">Mira el rango, no la cifra única: el DCF es
        muy sensible a los supuestos.</p>`;
    }
  } else if (dcf.error) {
    html += `<div class="nota"><b>DCF no disponible.</b> ${esc(dcf.error.replace(/^ValueError:\s*/, ""))}</div>`;
  }

  if (gra.valuations) {
    // Fixed Spanish labels, not derived from the (English) dict keys: they must
    // keep matching the GLOSARIO aliases in glosario.js ("Numero Graham", etc).
    const GRAHAM_LABELS = {
      graham_number: "Numero Graham",
      graham_revised_formula: "Formula Graham Revisada",
      gordon_dividend_discount: "Descuento Dividendos Gordon",
    };
    html += `<div class="metricas" style="margin-top:12px">
      ${Object.entries(gra.valuations).map(([k, v]) => {
        const label = GRAHAM_LABELS[k] || k.replace(/_/g, " ");
        return `<div class="metrica" data-glosario="${esc(label)}"><div class="k">${esc(label)}</div>
         <div class="v">${money(v, cot.currency)}</div></div>`;
      }).join("")}
    </div>`;
  }
  return html || '<div class="vacio">Sin datos suficientes para valorar.</div>';
}

/* ------------------------------------------------------------- comparador */

/** HU-30: tabla lado a lado de 2-4 tickers. Reutiliza stock_compare (mismos datos
 *  y motor de nota que la ficha); las etiquetas de fila reusan claves ya existentes
 *  en glosario.js (PER, EV/EBITDA, Margen neto, ROE, Dividendo, Nota, pilares). */
async function compararAcciones(tickersText) {
  loading("tabla-comparar");
  const tickers = String(tickersText || "").split(",").map((t) => t.trim()).filter(Boolean);
  try {
    const d = await api("app", "stock_compare", { tickers });
    renderComparar(d.rows);
  } catch (e) {
    renderError("tabla-comparar", e, () => compararAcciones(tickersText));
  }
}

function renderComparar(rows) {
  const cont = document.getElementById("tabla-comparar");
  if (!rows || !rows.length) { cont.innerHTML = '<div class="vacio">Sin datos.</div>'; return; }

  const filas = [
    { label: "Nota", glos: "Nota", fmt: (r) => `<span class="${r.sentiment || ""}">${number(r.final_score, 1)}/10</span> <span class="tenue">(mecanica ${number(r.score, 1)})</span>` },
    { label: "Precio", fmt: (r) => money(r.price) },
    { label: "PER", glos: "PER", fmt: (r) => number(r.per_trailing, 1) },
    { label: "EV/EBITDA", glos: "EV/EBITDA", fmt: (r) => number(r.ev_ebitda, 1) },
    { label: "ROE", glos: "ROE", fmt: (r) => (r.roe_pct != null ? `${number(r.roe_pct, 1)} %` : "—") },
    { label: "Margen neto", glos: "Margen neto", fmt: (r) => (r.net_margin_pct != null ? `${number(r.net_margin_pct, 1)} %` : "—") },
    { label: "Crecim. ventas", fmt: (r) => (r.revenue_growth_pct != null ? pct(r.revenue_growth_pct) : "—") },
    { label: "Dividendo", glos: "Dividendo", fmt: (r) => (r.dividend_yield_pct != null ? `${number(r.dividend_yield_pct, 2)} %` : "—") },
    { label: "Ventas trim. interanual", fmt: (r) => (r.quarter_revenue_yoy_pct != null ? pct(r.quarter_revenue_yoy_pct) : "—") },
    { label: "Margen trim.", fmt: (r) => (r.quarter_net_margin_pct != null ? `${number(r.quarter_net_margin_pct, 1)} %` : "—") },
  ];
  const pillarNames = [...new Set(rows.flatMap((r) => (r.pillars || []).map((p) => p.name_es)))];

  let html = `<div class="tabla-scroll"><table><thead><tr><th></th>${
    rows.map((r) => `<th>${esc(r.ticker)}${r.name ? `<div class="tenue" style="font-size:11px;font-weight:400">${esc(r.name)}</div>` : ""}</th>`).join("")
  }</tr></thead><tbody>`;
  filas.forEach((m) => {
    html += `<tr><th${m.glos ? ` data-glosario="${esc(m.glos)}"` : ""}>${esc(m.label)}</th>${
      rows.map((r) => `<td class="num">${r.error ? "—" : m.fmt(r)}</td>`).join("")
    }</tr>`;
  });
  pillarNames.forEach((name) => {
    html += `<tr><th data-glosario="${esc(name)}">${esc(name)}</th>${
      rows.map((r) => {
        if (r.error) return `<td class="num">—</td>`;
        const p = (r.pillars || []).find((x) => x.name_es === name);
        return `<td class="num">${p && p.subscore !== null ? number(p.subscore, 2) : "—"}</td>`;
      }).join("")
    }</tr>`;
  });
  html += `</tbody></table></div>`;
  const errores = rows.filter((r) => r.error);
  if (errores.length) {
    html += `<div class="nota">${esc(errores.map((r) => `${r.ticker}: ${r.error}`).join("; "))}</div>`;
  }
  cont.innerHTML = html;
  cont.querySelectorAll("[data-glosario]").forEach(makeFocusable);
}

/* ---------------------------------------------------- RECOMENDACIONES */

const TRAD_RIESGO = { conservative: "Conservador", moderate: "Moderado", aggressive: "Agresivo" };
const TRAD_HORIZONTE = { short: "Corto", medium: "Medio", long: "Largo" };
const TRAD_PREFERENCIA = { dividend: "Dividendo", growth: "Crecimiento", balanced: "Equilibrado" };

let REC_POSITIONS = [];
let REC_SCORES = {};
let REC_IDEAS = [];
let REC_COMBINED = [];
let REC_LOADING = false;

function rebuildRecCombined() {
  REC_COMBINED = [
    ...REC_POSITIONS.map((p) => ({ type: "position", ...p })),
    ...REC_IDEAS.map((i) => ({ type: "idea", ...i })),
  ];
}

/** Mismo filtro que scoring.select_ideas (Python, con sus propios tests), reproducido
 * aqui en JS para poder repintar la lista tras cada tanda sin esperar al universo entero. */
function pickIdeas(scores, meta, heldSectors) {
  const ideas = [];
  for (const [ticker, s] of Object.entries(scores)) {
    if (!s || s.error) continue;
    const final = s.final_score ?? s.score; // la nota final manda, como en select_ideas()
    if (final === undefined || final < 7) continue;
    if (s.coverage_pct === undefined || s.coverage_pct < 50) continue;
    const m = meta[ticker] || {};
    ideas.push({
      ticker, name: m.name || s.name, indices: m.indices || [],
      score: final, mechanical_score: s.score, label: s.label, sentiment: s.sentiment, sector: s.sector,
      coverage_pct: s.coverage_pct, pros: s.pros || [], score_full: s,
      review: s.review || null, review_applied: !!s.review_applied,
      diversifies: heldSectors.size > 0 && !!s.sector && !heldSectors.has(s.sector),
    });
  }
  ideas.sort((a, b) => b.score - a.score || b.coverage_pct - a.coverage_pct);
  return ideas;
}

function renderRecPositions() {
  const cont = document.getElementById("rec-posiciones");
  if (!REC_POSITIONS.length) {
    cont.innerHTML = '<div class="vacio">Sin posiciones abiertas. <a href="#" id="rec-ir-cartera">Ir a Cartera</a></div>';
    document.getElementById("rec-ir-cartera")?.addEventListener("click", (e) => { e.preventDefault(); changeView("cartera"); });
    return;
  }
  cont.innerHTML = `<div class="tabla-scroll"><table>
    <thead><tr><th>Ticker</th><th class="num" data-glosario="Nota">Nota</th>
      <th class="num" data-glosario="Peso en cartera">Peso</th>
      <th class="num" data-glosario="Plusvalia latente vs. realizada">P&amp;L</th><th>Accion</th></tr></thead>
    <tbody>${REC_POSITIONS.map((p, i) => {
      const s = p.score || {};
      return `<tr class="rec-fila" data-rec-idx="${i}" tabindex="0" role="button" aria-label="Ver detalle de ${esc(p.ticker)}">
        <td><span class="ticker">${esc(p.ticker)}</span></td>
        <td class="num">${s.error ? "—" : `<span class="${s.sentiment}">${notaMostrada(s)}/10</span>${marcaRevision(s)}`}</td>
        <td class="num">${p.weight_pct !== null && p.weight_pct !== undefined ? `${number(p.weight_pct, 1)} %` : "—"}</td>
        <td class="num ${sign(p.gain_pct)}">${pct(p.gain_pct)}</td>
        <td><span class="rec-accion rec-accion-${p.advice.action_key}">${esc(p.advice.action_es)}</span></td>
      </tr>`;
    }).join("")}</tbody></table></div>`;
  document.querySelectorAll("#rec-posiciones [data-glosario]").forEach(makeFocusable);
  wireRecItems(cont);
}

function renderRecIdeas() {
  const cont = document.getElementById("rec-ideas");
  if (!REC_IDEAS.length) {
    cont.innerHTML = REC_LOADING
      ? '<div class="cargando">Analizando candidatos…</div>'
      : '<div class="vacio">Ninguna idea nueva alcanza nota 7 o mas por ahora.</div>';
    return;
  }
  const offset = REC_POSITIONS.length;
  cont.innerHTML = `<div class="rec-tarjetas">${REC_IDEAS.map((idea, i) => `
    <div class="rec-idea-tarjeta" data-rec-idx="${offset + i}" tabindex="0" role="button"
      aria-label="Ver detalle de ${esc(idea.ticker)}">
      <div class="rec-idea-cab"><span class="ticker">${esc(idea.ticker)}</span>
        <span class="${idea.sentiment}" style="font-weight:700">${number(idea.score, idea.review_applied ? 1 : 0)}/10</span>${marcaRevision(idea.score_full)}</div>
      <div class="tenue" style="font-size:13px">${esc(idea.name || "")}</div>
      <div style="margin-top:6px">${(idea.indices || []).map((ix) => `<span class="rec-badge">${esc(ix)}</span>`).join("")}
        ${idea.diversifies ? '<span class="rec-badge rec-diversifica">Diversifica</span>' : ""}</div>
      <div class="tenue" style="font-size:13px;margin-top:4px">${esc(idea.label || "")}</div>
      ${idea.pros && idea.pros.length ? `<ul class="rec-pros">${idea.pros.slice(0, 2).map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
    </div>`).join("")}</div>`;
  wireRecItems(cont);
}

function wireRecItems(cont) {
  cont.querySelectorAll("[data-rec-idx]").forEach((el) => {
    const open = () => openRecViewer(Number(el.dataset.recIdx));
    el.addEventListener("click", open);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  });
}

function openRecViewer(index) {
  location.hash = `#rec/${index}`;
}

function closeRecViewer() {
  if (location.hash.startsWith("#rec/")) location.hash = "";
  else {
    document.getElementById("rec-visor").style.display = "none";
    document.getElementById("rec-listas").style.display = "";
  }
}

function renderRecViewer(index) {
  const item = REC_COMBINED[index];
  const visor = document.getElementById("rec-visor");
  if (!item) { closeRecViewer(); return; }

  document.getElementById("rec-listas").style.display = "none";
  visor.style.display = "";
  const total = REC_COMBINED.length;
  const s = item.type === "position" ? (item.score || {}) : (item.score_full || {});

  visor.innerHTML = `
    <div class="caja">
      <div class="rec-visor-nav">
        <button id="rec-prev" ${index === 0 ? "disabled" : ""}>← Anterior</button>
        <span class="tenue">${index + 1} / ${total}</span>
        <button id="rec-next" ${index === total - 1 ? "disabled" : ""}>Siguiente →</button>
      </div>
      <h2 tabindex="-1" id="rec-visor-titulo">${esc(item.name || item.ticker)} <span class="tenue">${esc(item.ticker)}</span></h2>
      ${(item.indices || []).length ? `<div>${item.indices.map((ix) => `<span class="rec-badge">${esc(ix)}</span>`).join("")}
        ${item.diversifies ? '<span class="rec-badge rec-diversifica">Diversifica</span>' : ""}</div>` : ""}
      ${item.type === "position" ? `
        <div class="metricas" style="margin:14px 0">
          <div class="metrica"><div class="k">Peso</div><div class="v">${item.weight_pct !== null && item.weight_pct !== undefined ? `${number(item.weight_pct, 1)} %` : "—"}</div></div>
          <div class="metrica"><div class="k">P&amp;L</div><div class="v ${sign(item.gain_pct)}">${pct(item.gain_pct)}</div></div>
        </div>
        <div class="rec-accion rec-accion-${item.advice.action_key}" style="display:inline-block;margin-bottom:8px">${esc(item.advice.action_es)}</div>
        <ul>${(item.advice.reasons_es || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
      ${renderScore(s)}
      <div class="acciones-caja">
        <button id="rec-volver-lista">Volver a la lista</button>
        <button id="rec-ver-ficha">Ver ficha completa</button>
      </div>
    </div>`;

  document.getElementById("rec-prev").addEventListener("click", () => openRecViewer(index - 1));
  document.getElementById("rec-next").addEventListener("click", () => openRecViewer(index + 1));
  document.getElementById("rec-volver-lista").addEventListener("click", closeRecViewer);
  visor.querySelectorAll(".metrica[data-glosario]").forEach(attachGlossary);
  document.getElementById("rec-ver-ficha").addEventListener("click", () => {
    changeView("acciones");
    viewStock(item.ticker, {}, false, index);
  });
  document.getElementById("rec-visor-titulo").focus();
}

function maybeRestoreRecViewerFromHash() {
  const m = location.hash.match(/^#rec\/(\d+)$/);
  if (m && Number(m[1]) < REC_COMBINED.length) renderRecViewer(Number(m[1]));
}

window.addEventListener("hashchange", () => {
  if (document.querySelector(".pestana.activa")?.dataset.view !== "recomendaciones") return;
  const m = location.hash.match(/^#rec\/(\d+)$/);
  if (m) renderRecViewer(Number(m[1]));
  else {
    document.getElementById("rec-visor").style.display = "none";
    document.getElementById("rec-listas").style.display = "";
  }
});

document.addEventListener("keydown", (e) => {
  const visor = document.getElementById("rec-visor");
  if (!visor || visor.style.display === "none") return;
  if (e.key === "Escape") { closeRecViewer(); return; }
  const m = location.hash.match(/^#rec\/(\d+)$/);
  if (!m) return;
  const idx = Number(m[1]);
  if (e.key === "ArrowLeft" && idx > 0) openRecViewer(idx - 1);
  if (e.key === "ArrowRight" && idx < REC_COMBINED.length - 1) openRecViewer(idx + 1);
});

async function loadRecommendations() {
  if (REC_LOADING) return;
  REC_LOADING = true;
  document.getElementById("rec-listas").style.display = "";
  document.getElementById("rec-visor").style.display = "none";
  loading("rec-posiciones");
  loading("rec-ideas");
  const progreso = document.getElementById("rec-progreso");
  progreso.style.display = "none";

  let profile = null;
  try {
    profile = await api("finance", "profile_get");
    document.getElementById("rec-perfil").innerHTML =
      `Perfil activo: <b>${esc(TRAD_RIESGO[profile.risk_tolerance] || profile.risk_tolerance)}</b>, ` +
      `horizonte <b>${esc(TRAD_HORIZONTE[profile.horizon] || profile.horizon)}</b>, ` +
      `preferencia <b>${esc(TRAD_PREFERENCIA[profile.preference] || profile.preference)}</b>. ` +
      `<a href="#" id="rec-cambiar-perfil">Cambiar en Cartera</a>`;
    document.getElementById("rec-cambiar-perfil")?.addEventListener("click", (e) => { e.preventDefault(); changeView("cartera"); });
  } catch (e) {
    document.getElementById("rec-perfil").textContent = "No se pudo cargar el perfil.";
  }

  try {
    const pos = await api("app", "recommendation_positions");
    REC_POSITIONS = pos.positions;
  } catch (e) {
    REC_POSITIONS = [];
    renderError("rec-posiciones", e, () => loadRecommendations());
  }
  REC_SCORES = {};
  REC_IDEAS = [];
  rebuildRecCombined();
  renderRecPositions();
  renderRecIdeas();
  maybeRestoreRecViewerFromHash();

  const heldSectors = new Set(REC_POSITIONS.map((p) => p.score && p.score.sector).filter(Boolean));

  let universe;
  try {
    universe = await api("app", "recommendation_universe");
  } catch (e) {
    REC_LOADING = false;
    renderError("rec-ideas", e, () => loadRecommendations());
    return;
  }
  document.getElementById("rec-fuente").innerHTML =
    `Universo: blue chips liquidos del Dow Jones, EURO STOXX 50 e IBEX 35 (datos ${esc(universe.as_of || "")}),
     mas tu watchlist y tu cartera. No son chivatazos: la misma puntuacion mecanica que la ficha de Acciones.`;

  const heldTickers = new Set(universe.held);
  const meta = {};
  universe.candidates.forEach((c) => { meta[c.ticker] = c; });
  const toScore = universe.candidates.filter((c) => !heldTickers.has(c.ticker)).map((c) => c.ticker);

  progreso.style.display = "";
  let done = 0;
  for (let i = 0; i < toScore.length; i += 8) {
    const batch = toScore.slice(i, i + 8);
    progreso.textContent = `Analizando ${done}/${toScore.length}...`;
    try {
      const r = await api("app", "stock_scores", { tickers: batch });
      Object.assign(REC_SCORES, r.scores);
    } catch (e) {
      // Una tanda fallida no bloquea las siguientes.
    }
    done += batch.length;
    progreso.textContent = `Analizando ${Math.min(done, toScore.length)}/${toScore.length}...`;
    REC_IDEAS = pickIdeas(REC_SCORES, meta, heldSectors);
    rebuildRecCombined();
    renderRecIdeas();
    maybeRestoreRecViewerFromHash();
  }
  progreso.style.display = "none";
  REC_LOADING = false;
  renderRecIdeas();
}

/* ------------------------------------------------------------ navegacion */

function changeView(name) {
  document.querySelectorAll(".pestana").forEach((b) =>
    b.classList.toggle("activa", b.dataset.view === name));
  document.querySelectorAll(".vista").forEach((v) =>
    v.classList.toggle("activa", v.id === `vista-${name}`));

  if (name === "cartera") { loadPortfolio(); loadProfile(); loadPendientes(); }
  if (name === "ayuda") loadHelp();
  if (name === "recomendaciones") loadRecommendations();
}

/**
 * Auto-refresco de cartera y ficha cada 60s. Un solo temporizador global que mira la
 * pestana activa en cada tick, en vez de uno por vista: mas simple y sin fugas al
 * cambiar de pestana. Se salta el tick si la pagina esta oculta (Page Visibility API),
 * para no gastar peticiones y limite de Yahoo en una pestana en segundo plano.
 *
 * Tambien se salta si el mercado relevante esta CLOSED: el precio no va a cambiar
 * hasta la siguiente sesion, asi que reconsultar cada 60s solo gasta peticiones sin
 * aportar nada nuevo en pantalla.
 */
function startAutoRefresh() {
  setInterval(() => {
    if (document.hidden) return;
    const view = document.querySelector(".pestana.activa")?.dataset.view;
    if (view === "cartera" && PORTFOLIO_ANY_MARKET_OPEN) {
      loadPortfolio(true);
    }
    if (view === "acciones" && CURRENT_STOCK && CURRENT_STOCK.marketStatus !== "CLOSED") {
      viewStock(CURRENT_STOCK.ticker, CURRENT_STOCK.assumptions, true);
    }
  }, 60000);
}

/* ----------------------------------------------------------------- ayuda */

const HELP_CARD = {
  cartera: {
    titulo: "Cartera",
    contenido: `<p>Gestionar posiciones de inversion. Compras/ventas y valor actualizado.</p>
      <ul>
        <li><b>Mi perfil</b> — pondera la nota de cada accion segun tu riesgo, horizonte y preferencia.</li>
        <li><b>Tarjetas</b> — valor de mercado, plusvalia latente, realizado.</li>
        <li><b>Tabla</b> — coste medio, precio, variacion, peso, P&L y Nota.</li>
        <li><b>Actualizar precios</b> — boton manual o auto cada 60s si mercado abierto.</li>
        <li><b>Watchlist</b> — seguir tickers sin posicion.</li>
        <li><b>Monedas</b> — compra en EUR, cotiza en USD = conversion automatica.</li>
      </ul>`,
  },
  acciones: {
    titulo: "Acciones",
    contenido: `<p>Ficha completa: cotizacion, fundamentales, historico, valoracion y nota.</p>
      <ul>
        <li><b>Busqueda</b> — nombre o ticker. Europeos sondeados aparte.</li>
        <li><b>Nota 1-10</b> — sintetiza siete pilares segun tu perfil inversor.</li>
        <li><b>Revision de Claude</b> — si se la pides, suma o resta hasta 2 puntos, con motivo y fuentes y siempre a la vista.</li>
        <li><b>Fundamentales</b> — PER, EV/EBITDA, margen, ROE, dividendo.</li>
        <li><b>Historico 1 ano</b> — rentabilidad, volatilidad, RSI, medias moviles.</li>
        <li><b>Valoracion</b> — DCF con sensibilidad + Graham.</li>
        <li><b>Resultados trimestrales</b> — los 3 ultimos trimestres, de donde sale el pilar de resultados.</li>
      </ul>`,
  },
  trimestres: {
    titulo: "Resultados trimestrales",
    contenido: `<p>Los tres ultimos trimestres fiscales publicados, no el ultimo ano entero.
      Es la base del pilar <b>Resultados recientes</b> (15 de los 100 puntos de la nota).</p>
      <ul>
        <li><b>Interanual</b> — cada trimestre frente al <b>mismo</b> trimestre del ano
          anterior, para que no salga el efecto de la estacionalidad.</li>
        <li><b>—</b> — ese dato no lo publica la empresa. No se rellena con estimaciones.</li>
        <li><b>Margen</b> — beneficio neto sobre ventas, en puntos porcentuales.</li>
        <li><b>Aviso en ambar</b> — faltan trimestres o hay un hueco: la nota se apoya en
          menos datos y lo dice.</li>
      </ul>
      <p>Fuera de EE. UU. los datos llegan incompletos con frecuencia (Inditex y Banco
        Santander no publican flujo de caja trimestral). La cobertura de cada mercado se
        comprueba antes de fiarse de la tendencia.</p>`,
  },
  proyecciones: {
    titulo: "Proyecciones",
    contenido: `<p>Lo que espera el <b>consenso de analistas</b>: BPA y ventas del trimestre
      y del año que vienen, cuantos analistas lo cubren y cuanto crecimiento espera el mercado.
      Es una <b>prevision, no un dato de la empresa</b>.</p>
      <ul>
        <li><b>Revisiones 30 d</b> - cuantos analistas han subido o bajado su estimacion en
          el ultimo mes. Es lo unico que entra en la nota, y con poco peso.</li>
        <li><b>Por que no mas?</b> El consenso de Wall Street tiende al optimismo, asi que
          su <i>nivel</i> se enseña pero no puntua: mediria el optimismo del analysta, no
          el negocio.</li>
        <li><b>Objetivo de analistas</b> - precio medio y rango. Tambien con medio peso,
          y con la misma prudencia.</li>
        <li><b>Poca cobertura</b> - 5 analistas no es consenso, es opinion. Con menos de 3
          la cifra se lee como orientativa.</li>
      </ul>
      <p>Las proyecciones salen de Yahoo. Para las europeas llegan con retraso y a veces no
        hay cobertura: la caja avisa en vez de inventar.</p>`,
  },
  preguntar: {
    titulo: "Preguntar a Claude",
    contenido: `<p>Escribe una pregunta sobre el valor y la recibes con los datos de la ficha
      como contexto: precio, nota, pilares, trimestres, fundamentales y noticias. Puede
      buscar en internet para contrastar.</p>
      <ul>
        <li><b>Sale de tu equipo.</b> Es la unica parte de SeaLock que envia datos: la
          pregunta y el contexto van a la API de Anthropic, y solo al pulsar el boton.</li>
        <li><b>Tu posicion va aparte.</b> Solo se incluye si marcas la casilla; sin ella,
          Claude no ve tu coste medio, tu peso ni tu P&amp;L.</li>
        <li><b>La clave no sale del servidor.</b> Se lee de <code>ANTHROPIC_API_KEY</code>
          en el entorno; no se guarda en la base de datos ni llega al navegador.</li>
        <li><b>No es asesoramiento.</b> Es la lectura de un modelo sobre las mismas fuentes
          publicas. La decision es tuya.</li>
      </ul>
      <p>Si no hay clave, la caja te lo dice y no envia nada. Puedes cambiar de modelo con
        <code>SEALOCK_CLAUDE_MODEL</code>.</p>
      <p><b>Copiar prompt para otra IA</b> es la via libre: arma lo mismo (datos, URLs,
        tu perfil y tu cartera) en un texto que copias al portapapeles y pegas donde quieras.
        Ahi <b>si viaja tu cartera entera</b>, porque la pegas tu a mano y eliges el sitio.
        El prompt pide analisis y riesgos, nunca comprar o vender.</p>`,
  },
  recomendaciones: {
    titulo: "Recomendaciones",
    contenido: `<p>Sugerencias sobre tu cartera y un universo de blue chips (Dow Jones, EURO STOXX 50, IBEX 35) segun tu perfil.</p>
      <ul>
        <li><b>Tus posiciones</b> — accion sugerida por posicion: mantener, ampliar, vigilar o vender.</li>
        <li><b>Ideas nuevas</b> — candidatos con nota 7+ que no tienes, marcando si diversifican sector.</li>
        <li><b>Una a una</b> — clic abre el detalle, con Anterior/Siguiente y el boton Atras del navegador.</li>
      </ul>`,
  },
  cartera_vacio: {
    titulo: "Como registrar una compra",
    contenido: `<p>En la pestana <b>Cartera</b>, formulario "Registrar operacion":</p>
      <ul>
        <li><b>Ticker</b> — formato Yahoo: <code>AAPL</code>, <code>ITX.MC</code>, <code>VWCE.DE</code></li>
        <li><b>Operacion</b> — Compra o Venta.</li>
        <li><b>Cantidad</b> y <b>Precio</b> por accion.</li>
        <li><b>Comision</b> — por defecto 0.</li>
      </ul>
      <p>El coste medio y la plusvalia se calculan automaticamente.</p>`,
  },
};

let _ayudaCardEl = null;

function closeHelpCard() {
  if (_ayudaCardEl) { _ayudaCardEl.remove(); _ayudaCardEl = null; }
}

function openHelpCard(btn, seccion) {
  closeHelpCard();
  const data = HELP_CARD[seccion];
  if (!data) return;

  const card = document.createElement("div");
  card.className = "ayuda-card";
  card.innerHTML = `<div class="ayuda-card-flecha"></div>
    <div class="ayuda-card-header"><h3>${esc(data.titulo)}</h3>
      <button class="ayuda-card-close" aria-label="Cerrar">&times;</button></div>
    <div class="ayuda-card-body">${data.contenido}</div>`;

  document.body.appendChild(card);
  _ayudaCardEl = card;

  // Posicionar debajo del boton
  const br = btn.getBoundingClientRect();
  let top = br.bottom + 10;
  let left = br.left;
  card.style.top = top + "px";
  card.style.left = left + "px";

  requestAnimationFrame(() => {
    const cr = card.getBoundingClientRect();
    // Si se sale por la derecha
    if (cr.right > window.innerWidth - 12) {
      card.style.left = Math.max(12, window.innerWidth - cr.width - 12) + "px";
    }
    if (cr.left < 12) card.style.left = "12px";
    // Si se sale por abajo
    if (cr.bottom > window.innerHeight - 12) {
      card.style.top = (br.top - cr.height - 10) + "px";
      card.classList.add("abajo");
    }
  });

  card.querySelector(".ayuda-card-close").addEventListener("click", closeHelpCard);
}

function loadHelp() {
  const lista = document.getElementById("glosario-lista");
  if (lista && !lista.dataset.cargado) {
    lista.dataset.cargado = "1";
    const ids = Object.keys(GLOSARIO);
    lista.innerHTML = ids.map((id) => {
      const e = GLOSARIO[id];
      return `<details><summary>${esc(e.titulo)}</summary>
        <p><b>Que es:</b> ${esc(e.que_es)}</p>
        ${e.rango ? `<p><b>Rango tipico:</b> ${esc(e.rango)}</p>` : ""}
        ${e.ideal ? `<p><b>Lo ideal:</b> ${esc(e.ideal)}</p>` : ""}
        <p><b>Como leerlo:</b> ${esc(e.como_leerlo)}</p>
        <p><b>Trampa:</b> ${esc(e.trampa)}</p></details>`;
    }).join("");
  }
  // Buscador de ayuda: filtra secciones por texto
  const buscador = document.getElementById("buscador-ayuda");
  if (buscador && !buscador._enganchado) {
    buscador._enganchado = true;
    buscador.addEventListener("input", () => {
      const q = buscador.value.toLowerCase();
      document.querySelectorAll("#vista-ayuda .caja[data-section]").forEach((caja) => {
        caja.style.display = q && !caja.textContent.toLowerCase().includes(q) ? "none" : "";
      });
      // Glosario: abrir/buscar dentro de details
      if (q) {
        document.querySelectorAll("#glosario-lista details").forEach((d) => {
          const match = d.textContent.toLowerCase().includes(q);
          d.style.display = match ? "" : "none";
          if (match) d.open = true;
        });
      } else {
        document.querySelectorAll("#glosario-lista details").forEach((d) => { d.style.display = ""; d.open = false; });
      }
    });
  }
}

/* --------------------------------------------------------------- arranque */

function wireForm(id, group, tool, onDone, transform) {
  const form = document.getElementById(id);
  if (!form) return;
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      let data = formData(form);
      if (transform) data = transform(data);
      await api(group, tool, data);
      form.reset();
      notify("Guardado", "bueno");
      onDone();
    } catch (e) {
      notify(e.message, "malo");
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".pestana").forEach((b) =>
    b.addEventListener("click", () => changeView(b.dataset.view)));

  wireForm("form-operacion", "finance", "trade_create", loadPortfolio);
  wireForm("form-watchlist", "finance", "watchlist_add", loadWatchlist);

  document.getElementById("form-perfil").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await api("finance", "profile_set", formData(ev.target));
      notify("Perfil guardado", "bueno");
      if (CURRENT_STOCK) viewStock(CURRENT_STOCK.ticker, CURRENT_STOCK.assumptions);
    } catch (e) {
      notify(e.message, "malo");
    }
  });

  document.getElementById("form-buscar").addEventListener("submit", (ev) => {
    ev.preventDefault();
    search(new FormData(ev.target).get("query"));
  });

  document.getElementById("form-comparar").addEventListener("submit", (ev) => {
    ev.preventDefault();
    compararAcciones(new FormData(ev.target).get("tickers"));
  });

  document.getElementById("btn-refrescar-cartera").addEventListener("click", () => loadPortfolio());
  document.getElementById("btn-actualizar-recomendaciones").addEventListener("click", () => loadRecommendations());
  document.getElementById("btn-exportar-json").addEventListener("click", () => exportar("json"));
  document.getElementById("btn-exportar-csv").addEventListener("click", () => exportar("csv"));

  // Botones ? y links data-help → card emergente
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".btn-ayuda");
    const link = e.target.closest("[data-help]");
    const target = btn || link;
    if (!target) return;
    e.preventDefault();
    const seccion = target.dataset.section || target.dataset.help;
    if (!seccion) return;
    if (_ayudaCardEl && _ayudaCardEl._origen === target) { closeHelpCard(); return; }
    openHelpCard(target, seccion);
    _ayudaCardEl._origen = target;
  });

  // Cerrar card ayuda con ESC
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeHelpCard(); });
  // Cerrar card ayuda con click fuera
  document.addEventListener("click", (e) => {
    if (_ayudaCardEl && !_ayudaCardEl.contains(e.target) && !e.target.closest(".btn-ayuda") && !e.target.closest("[data-help]")) {
      closeHelpCard();
    }
  });

  document.getElementById("estado").textContent = `datos locales · ${CURRENCY}`;
  changeView("acciones");
  startAutoRefresh();
});
