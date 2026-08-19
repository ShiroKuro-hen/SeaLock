"use strict";

const CURRENCY = window.SEALOCK.currency;
let ACCOUNTS = [];
let TRANSACTIONS = [];
let TRANSACTIONS_OFFSET = 0;
const TRANSACTIONS_PAGE_SIZE = 50;
let CURRENT_STOCK = null; // { ticker, assumptions, marketStatus } de la ultima ficha vista
let PORTFOLIO_ANY_MARKET_OPEN = true; // gate del auto-refresco: sin nada abierto, no vale la pena sondear

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
// (con su propio listener de clic) como las cabeceras de tabla y tarjetas del panel
// (enganchadas solo por delegacion): un solo listener generico sirve para ambas, porque
// openGlossary/closeGlossary no dependen de la clase del elemento.
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
const currentMonth = () => new Date().toISOString().slice(0, 7);

function formData(form) {
  const d = {};
  for (const [k, v] of new FormData(form).entries()) {
    if (v === "" || v === null) continue;
    const el = form.elements[k];
    d[k] = el && el.type === "number" ? Number(v) : v;
  }
  return d;
}

function loading(id) {
  document.getElementById(id).innerHTML = '<div class="cargando">Cargando…</div>';
}

function renderError(id, e) {
  document.getElementById(id).innerHTML = `<div class="error">${esc(e.message || e)}</div>`;
}

/* --------------------------------------------------------------- graficos */

function evolutionChart(series) {
  if (!series.length) return '<div class="vacio">Aun no hay movimientos registrados. <a href="#" data-help="movimientos">Como apuntar gastos</a></div>';

  const W = 640, H = 220, ml = 52, mb = 26, mt = 12;
  const max = Math.max(...series.flatMap((d) => [d.income, d.expenses]), 1);
  const anchoPaso = (W - ml - 10) / series.length;
  const ancho = Math.min(anchoPaso * 0.36, 22);
  const y = (v) => H - mb - (v / max) * (H - mb - mt);

  let barras = "", etiquetas = "", puntos = [];
  series.forEach((d, i) => {
    const x = ml + i * anchoPaso + anchoPaso / 2;
    barras += `<rect x="${x - ancho - 1}" y="${y(d.income)}" width="${ancho}"
      height="${Math.max(0, H - mb - y(d.income))}" fill="var(--verde)" opacity="0.85" rx="2"/>`;
    barras += `<rect x="${x + 1}" y="${y(d.expenses)}" width="${ancho}"
      height="${Math.max(0, H - mb - y(d.expenses))}" fill="var(--rojo)" opacity="0.85" rx="2"/>`;
    puntos.push(`${x},${y(Math.max(d.savings, 0))}`);
    if (series.length <= 14 || i % 2 === 0) {
      etiquetas += `<text x="${x}" y="${H - 8}" text-anchor="middle"
        font-size="10" fill="var(--texto-tenue)">${esc(d.month.slice(5))}</text>`;
    }
  });

  let ejes = "";
  for (let i = 0; i <= 3; i++) {
    const v = (max / 3) * i, yy = y(v);
    ejes += `<line x1="${ml}" y1="${yy}" x2="${W - 6}" y2="${yy}" stroke="var(--borde)" stroke-width="1"/>`;
    ejes += `<text x="${ml - 7}" y="${yy + 3}" text-anchor="end" font-size="10"
      fill="var(--texto-tenue)">${compact(v)}</text>`;
  }

  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Evolucion mensual">
    ${ejes}${barras}
    <polyline points="${puntos.join(" ")}" fill="none" stroke="var(--acento)"
      stroke-width="2" stroke-linejoin="round"/>
    ${etiquetas}
  </svg>
  <div class="resumen-filtro" style="padding-top:8px">
    <span><b class="verde">■</b> Ingresos</span>
    <span><b class="rojo">■</b> Gastos</span>
    <span><b style="color:var(--acento)">—</b> Ahorro</span>
  </div>`;
}

function categoryChart(byCategory) {
  const expenses = byCategory
    .filter((c) => c.type === "expense")
    .sort((a, b) => b.total - a.total)
    .slice(0, 8);
  if (!expenses.length) return '<div class="vacio">Sin gastos este mes.</div>';

  const max = Math.max(...expenses.map((g) => g.total));
  return expenses.map((g) => `
    <div class="progreso">
      <div class="fila"><span>${esc(g.category)}</span><b>${money(g.total)}</b></div>
      <div class="barra"><div class="relleno" style="width:${(100 * g.total) / max}%"></div></div>
    </div>`).join("");
}

/* ------------------------------------------------------------------ PANEL */

async function loadDashboard() {
  loading("tarjetas");
  try {
    const p = await api("app", "dashboard");
    const m = p.month;

    document.getElementById("tarjetas").innerHTML = `
      <div class="tarjeta">
        <div class="etiqueta">Patrimonio</div>
        <div class="cifra">${money(p.net_worth)}</div>
        <div class="pie">${money(p.cash)} efectivo · ${money(p.portfolio)} cartera</div>
      </div>
      <div class="tarjeta">
        <div class="etiqueta" data-glosario="Tasa de ahorro">Ahorro del mes</div>
        <div class="cifra ${sign(m.savings)}">${money(m.savings)}</div>
        <div class="pie">${m.savings_rate_pct !== null ? `tasa ${number(m.savings_rate_pct, 1)} %` : "sin ingresos"}</div>
      </div>
      <div class="tarjeta">
        <div class="etiqueta">Ingresos / Gastos</div>
        <div class="cifra">${money(m.expenses)}</div>
        <div class="pie">de ${money(m.income)} ingresados</div>
      </div>
      <div class="tarjeta">
        <div class="etiqueta" data-glosario="Plusvalia latente vs. realizada">Plusvalia latente</div>
        <div class="cifra ${sign(p.unrealized_gain)}">${money(p.unrealized_gain ?? 0)}</div>
        <div class="pie">${p.no_price.length ? `${p.no_price.length} sin precio` : "cartera a mercado"}</div>
      </div>`;
    document.querySelectorAll("#tarjetas [data-glosario]").forEach(makeFocusable);

    document.getElementById("grafico-evolucion").innerHTML = evolutionChart(p.evolution);
    document.getElementById("grafico-categorias").innerHTML = categoryChart(m.by_category);

    ACCOUNTS = p.accounts;
    document.getElementById("lista-cuentas").innerHTML = p.accounts.length
      ? `<div class="tabla-scroll"><table><tbody>${p.accounts.map((c) => `
          <tr data-id="${c.id}">
            <td class="c-nombre">${esc(c.name)}</td>
            <td class="c-tipo tenue">${esc(c.type)}</td>
            <td class="c-moneda tenue">${esc(c.currency || CURRENCY)}</td>
            <td class="c-saldo num"><b>${money(c.balance, c.currency)}</b></td>
            <td class="num acc-cuenta">
              <button class="mini" data-editar-cuenta="${c.id}">Editar</button>
              <button class="mini peligro" data-borrar-cuenta="${c.id}">Eliminar</button>
            </td>
          </tr>`).join("")}
        </tbody></table></div>`
      : '<div class="vacio">Crea tu primera cuenta abajo.</div>';

    document.querySelectorAll("[data-editar-cuenta]").forEach((b) =>
      b.addEventListener("click", () => editAccount(Number(b.dataset.editarCuenta))));
    document.querySelectorAll("[data-borrar-cuenta]").forEach((b) => {
      const cId = Number(b.dataset.borrarCuenta);
      const cName = (ACCOUNTS.find((c) => c.id === cId) || {}).name || "";
      b.addEventListener("click", () => deleteAccount(cId, esc(cName)));
    });

    document.getElementById("lista-presupuestos").innerHTML = p.budgets.length
      ? p.budgets.map((b) => {
          const cl = b.status === "EXCEEDED" ? "excedido"
            : b.status === "WILL_EXCEED" || b.consumed_pct > 80 ? "cerca" : "";
          const color = b.status === "EXCEEDED" ? "rojo" : b.status === "WILL_EXCEED" ? "ambar" : "";
          return `<div class="progreso">
            <div class="fila"><span>${esc(b.category)}</span>
              <span class="${color}">${money(b.spent)} / ${money(b.limit_amount)}</span></div>
            <div class="barra"><div class="relleno ${cl}"
              style="width:${Math.min(100, b.consumed_pct)}%"></div></div>
            ${b.committed_future > 0 ? `<div class="tenue" style="font-size:12px;margin-top:3px">
              + ${money(b.committed_future)} anotado con fecha posterior a hoy
              → ${money(b.projected_month_end)} a fin de mes</div>` : ""}
          </div>`;
        }).join("")
      : '<div class="vacio">Sin presupuestos definidos.</div>';

    fillAccountSelectors();
  } catch (e) {
    renderError("tarjetas", e);
  }
}

function fillAccountSelectors() {
  const opciones = ACCOUNTS.map((c) => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join("");
  const mov = document.querySelector('#form-movimiento select[name="account"]');
  if (mov) mov.innerHTML = `<option value="">(ninguna)</option>${opciones}`;
  document.querySelectorAll('#form-traspaso select').forEach((s) => (s.innerHTML = opciones));
}

/* ------------------------------------------------------------ MOVIMIENTOS */

async function loadTransactions(resetOffset = true) {
  if (resetOffset) TRANSACTIONS_OFFSET = 0;
  loading("tabla-movimientos");
  const month = document.getElementById("filtro-mes").value;
  const args = { limit: TRANSACTIONS_PAGE_SIZE, offset: TRANSACTIONS_OFFSET };
  if (month) { args.date_from = `${month}-01`; args.date_to = `${month}-31`; }
  const type = document.getElementById("filtro-tipo").value;
  if (type) args.type = type;
  const text = document.getElementById("filtro-texto").value.trim();
  if (text) args.search = text;

  try {
    const d = await api("finance", "transactions_list", args);
    TRANSACTIONS = d.transactions;
    document.getElementById("resumen-filtro").innerHTML = `
      <span>Ingresos <b class="verde">${money(d.income)}</b></span>
      <span>Gastos <b class="rojo">${money(d.expenses)}</b></span>
      <span>Neto <b class="${sign(d.net)}">${money(d.net)}</b></span>
      <span>${d.total} apuntes</span>`;

    document.getElementById("tabla-movimientos").innerHTML = d.transactions.length
      ? `<div class="tabla-scroll"><table>
          <thead><tr><th>Fecha</th><th>Categoria</th><th>Descripcion</th>
            <th>Cuenta</th><th class="num">Importe</th><th></th></tr></thead>
          <tbody>${d.transactions.map((m) => `
            <tr data-tx-id="${m.id}">
              <td class="tenue">${esc(m.date)}</td>
              <td>${esc(m.category)}${m.subcategory ? ` <span class="tenue">/ ${esc(m.subcategory)}</span>` : ""}</td>
              <td class="tenue">${esc(m.description || "")}</td>
              <td class="tenue">${esc(m.account || "")}</td>
              <td class="num ${m.type === "income" ? "verde" : ""}">
                ${m.type === "income" ? "+" : "−"}${money(m.amount, m.currency).replace(/^[+-]/, "")}</td>
              <td class="num">
                <button class="mini" data-editar="${m.id}">Editar</button>
                <button class="mini peligro" data-borrar="${m.id}">Borrar</button></td>
            </tr>`).join("")}
          </tbody></table></div>
          <div class="resumen-filtro" style="padding-top:8px">
            <button class="mini" id="btn-tx-anterior" ${TRANSACTIONS_OFFSET === 0 ? "disabled" : ""}>&larr; Anterior</button>
            <span class="tenue">${TRANSACTIONS_OFFSET + 1}–${TRANSACTIONS_OFFSET + d.shown} de ${d.total}</span>
            <button class="mini" id="btn-tx-siguiente" ${d.has_more ? "" : "disabled"}>Siguiente &rarr;</button>
          </div>`
      : '<div class="vacio">No hay movimientos con estos filtros.</div>';

    document.querySelectorAll("[data-borrar]").forEach((b) =>
      b.addEventListener("click", () => deleteTransaction(Number(b.dataset.borrar))));
    document.querySelectorAll("[data-editar]").forEach((b) =>
      b.addEventListener("click", () => editTransaction(Number(b.dataset.editar))));
    const prevBtn = document.getElementById("btn-tx-anterior");
    if (prevBtn) prevBtn.addEventListener("click", () => {
      TRANSACTIONS_OFFSET = Math.max(0, TRANSACTIONS_OFFSET - TRANSACTIONS_PAGE_SIZE);
      loadTransactions(false);
    });
    const nextBtn = document.getElementById("btn-tx-siguiente");
    if (nextBtn) nextBtn.addEventListener("click", () => {
      TRANSACTIONS_OFFSET += TRANSACTIONS_PAGE_SIZE;
      loadTransactions(false);
    });

    const cats = await api("finance", "sql_query", {
      sql: "SELECT DISTINCT category FROM transactions ORDER BY category",
    });
    document.getElementById("categorias").innerHTML =
      cats.rows.map((c) => `<option value="${esc(c.category)}">`).join("");
  } catch (e) {
    renderError("tabla-movimientos", e);
  }
}

async function deleteTransaction(id) {
  if (!confirm(`¿Borrar el movimiento ${id}? No se puede deshacer.`)) return;
  try {
    await api("finance", "transaction_delete", { id });
    notify("Movimiento borrado", "bueno");
    loadTransactions(false);
  } catch (e) {
    notify(e.message, "malo");
  }
}

function editTransaction(id) {
  const m = TRANSACTIONS.find((t) => t.id === id);
  if (!m) return;
  const tr = document.querySelector(`tr[data-tx-id="${id}"]`);
  if (!tr) return;

  tr.classList.add("editando");
  tr.innerHTML = `
    <td><input class="ed-fecha" type="date" value="${esc(m.date)}"></td>
    <td><input class="ed-categoria" value="${esc(m.category)}" required></td>
    <td><input class="ed-descripcion" value="${esc(m.description || "")}"></td>
    <td class="tenue">${esc(m.account || "")}</td>
    <td class="num"><input class="ed-importe" type="number" step="0.01" min="0.01"
      value="${m.amount}" style="max-width:90px"></td>
    <td class="num acc-cuenta">
      <button class="mini principal" data-guardar-mov="${id}">Guardar</button>
      <button class="mini" data-cancelar-mov="${id}">Cancelar</button>
    </td>`;

  tr.querySelector(".ed-categoria").addEventListener("keydown", (e) => { if (e.key === "Enter") saveTransaction(id); });
  tr.querySelector("[data-guardar-mov]").addEventListener("click", () => saveTransaction(id));
  tr.querySelector("[data-cancelar-mov]").addEventListener("click", () => loadTransactions(false));
}

async function saveTransaction(id) {
  const tr = document.querySelector(`tr[data-tx-id="${id}"]`);
  if (!tr) return;
  const date = tr.querySelector(".ed-fecha").value;
  const category = tr.querySelector(".ed-categoria").value.trim();
  const description = tr.querySelector(".ed-descripcion").value.trim();
  const amount = Number(tr.querySelector(".ed-importe").value);
  if (!category) { notify("La categoria no puede estar vacia", "malo"); return; }
  try {
    await api("finance", "transaction_update", { id, date, category, description, amount });
    notify("Movimiento actualizado", "bueno");
    loadTransactions(false);
  } catch (e) {
    notify(e.message, "malo");
  }
}

function editAccount(id) {
  const c = ACCOUNTS.find((c) => c.id === id);
  if (!c) return;
  const tr = document.querySelector(`tr[data-id="${id}"]`);
  if (!tr) return;

  tr.classList.add("editando");
  tr.innerHTML = `
    <td><input class="ed-nombre" value="${esc(c.name)}" required></td>
    <td><select class="ed-tipo">
      ${["checking","savings","cash","investment","other"].map((t) =>
        `<option value="${t}"${t === c.type ? " selected" : ""}>${esc(t)}</option>`).join("")}
    </select></td>
    <td><input class="ed-moneda" maxlength="3" value="${esc(c.currency || CURRENCY)}" style="max-width:60px"></td>
    <td class="num">${money(c.balance, c.currency)}</td>
    <td class="num acc-cuenta">
      <button class="mini principal" data-guardar-cuenta="${id}">Guardar</button>
      <button class="mini" data-cancelar="${id}">Cancelar</button>
    </td>`;

  tr.querySelector(".ed-nombre").focus();
  tr.querySelector("[data-guardar-cuenta]").addEventListener("click", () => saveAccount(id));
  tr.querySelector("[data-cancelar]").addEventListener("click", () => loadDashboard());
  tr.querySelector(".ed-nombre").addEventListener("keydown", (e) => { if (e.key === "Enter") saveAccount(id); });
}

async function saveAccount(id) {
  const tr = document.querySelector(`tr[data-id="${id}"]`);
  if (!tr) return;
  const name = tr.querySelector(".ed-nombre").value.trim();
  const type = tr.querySelector(".ed-tipo").value;
  const currency = tr.querySelector(".ed-moneda").value.trim().toUpperCase();
  if (!name) { notify("El nombre no puede estar vacio", "malo"); return; }
  try {
    await api("finance", "account_update", { id, name, type, currency });
    notify("Cuenta actualizada", "bueno");
    loadDashboard();
  } catch (e) {
    notify(e.message, "malo");
  }
}

async function deleteAccount(id, name) {
  if (!confirm(`¿Eliminar la cuenta "${name}"?\n\nSe borrarán TODOS los movimientos, operaciones y recurrentes asociados. Esta accion no se puede deshacer.`)) return;
  try {
    await api("finance", "account_delete", { id });
    notify("Cuenta eliminada", "bueno");
    loadDashboard();
  } catch (e) {
    notify(e.message, "malo");
  }
}

async function loadRecurring() {
  try {
    const d = await api("finance", "recurring_list");
    document.getElementById("lista-recurrentes").innerHTML = d.recurring.length
      ? `<div class="tabla-scroll"><table>
          <thead><tr><th>Concepto</th><th>Categoria</th><th>Cuando</th>
            <th class="num">Importe</th><th class="num">Generados</th><th></th></tr></thead>
          <tbody>${d.recurring.map((r) => `
            <tr>
              <td>${esc(r.concept)}</td>
              <td class="tenue">${esc(r.category)}</td>
              <td class="tenue">${esc(r.frequency)}, dia ${r.day}</td>
              <td class="num ${r.type === "income" ? "verde" : ""}">${money(r.amount, r.currency)}</td>
              <td class="num tenue">${r.generated_count}</td>
              <td class="num"><button class="mini peligro" data-quitar-rec="${r.id}">Desactivar</button></td>
            </tr>`).join("")}
          </tbody></table></div>
         <div class="resumen-filtro">
           <span>Fijo mensual: <b class="rojo">${money(d.fixed_monthly_commitment)}</b></span>
           <span>Ingreso fijo: <b class="verde">${money(d.fixed_monthly_income)}</b></span>
           <span>Margen: <b class="${sign(d.fixed_monthly_margin)}">${money(d.fixed_monthly_margin)}</b></span>
         </div>`
      : '<div class="vacio">Sin recurrentes definidos.</div>';

    document.querySelectorAll("[data-quitar-rec]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm("¿Desactivar este recurrente? El historial se conserva.")) return;
        await api("finance", "recurring_deactivate", { id: Number(b.dataset.quitarRec) });
        notify("Recurrente desactivado", "bueno");
        loadRecurring();
      }));
  } catch (e) {
    renderError("lista-recurrentes", e);
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
            <th class="num" data-glosario="Peso en cartera">Peso</th></tr></thead>
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
            </tr>`;}).join("")}
          </tbody></table></div>`
      : '<div class="vacio">Sin posiciones abiertas. <a href="#" data-help="cartera">Como registrar una compra</a></div>';
    document.querySelectorAll("#tabla-cartera [data-glosario]").forEach(makeFocusable);
  } catch (e) {
    if (silencioso) notify(`No se pudo actualizar la cartera: ${e.message}`, "malo");
    else renderError("tabla-cartera", e);
  }
  if (!silencioso) loadWatchlist();
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

    document.querySelectorAll("[data-quitar]").forEach((b) =>
      b.addEventListener("click", async () => {
        await api("finance", "watchlist_remove", { ticker: b.dataset.quitar });
        loadWatchlist();
      }));
    document.querySelectorAll("[data-ver]").forEach((b) =>
      b.addEventListener("click", () => { changeView("acciones"); viewStock(b.dataset.ver); }));
  } catch (e) {
    renderError("tabla-watchlist", e);
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
    renderError("resultados-busqueda", e);
  }
}

async function viewStock(ticker, assumptions = {}, silencioso = false) {
  const cont = document.getElementById("ficha");
  CURRENT_STOCK = { ticker, assumptions };

  if (!silencioso) {
    cont.innerHTML = `<div class="caja"><div class="cargando">Analizando ${esc(ticker)}…</div></div>`;
    cont.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  try {
    const d = await api("app", "stock_detail", { ticker, ...assumptions });
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
        ${renderVerdict(d.verdict)}
        <div class="metricas">
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

      <div class="caja">
        <h2>Noticias</h2>
        ${Array.isArray(d.news) && d.news.length
          ? d.news.map((n) => `<div class="noticia">
              <a href="${esc(n.link)}" target="_blank" rel="noopener noreferrer">${esc(n.headline)}</a>
              <div class="meta">${esc(n.source || "")} ${n.date ? `· ${esc(n.date)}` : ""}</div>
            </div>`).join("")
          : '<div class="vacio">Sin noticias recientes.</div>'}
      </div>

      <div class="caja">
        <div class="acciones-caja">
          <button id="btn-seguir">Anadir a seguimiento</button>
        </div>
        <p class="ayuda" style="margin:12px 0 0">
          Datos de fuentes publicas, precios diferidos ~15 min. Analisis, no recomendacion
          de inversion: la decision es tuya.</p>
      </div>`;

    cont.querySelectorAll(".metrica[data-glosario]").forEach(attachGlossary);

    document.getElementById("form-dcf").addEventListener("submit", (ev) => {
      ev.preventDefault();
      viewStock(ticker, formData(ev.target));
    });
    document.getElementById("btn-seguir").addEventListener("click", async () => {
      await api("finance", "watchlist_add", { ticker, name: f.name || null });
      notify(`${ticker} anadido a seguimiento`, "bueno");
    });
  } catch (e) {
    if (silencioso) notify(`No se pudo actualizar ${ticker}: ${e.message}`, "malo");
    else renderError("ficha", e);
  }
}

function renderVerdict(v) {
  if (!v) return "";
  const color = v.sentiment || "ambar";
  const icon = color === "verde" ? "▲" : color === "rojo" ? "▼" : "◆";
  let html = `<div class="veredicto veredicto-${color}">`;
  html += `<div class="veredicto-header"><span class="veredicto-icono ${color}">${icon}</span> ${esc(v.summary)}</div>`;
  if (v.pros && v.pros.length) {
    html += `<div class="veredicto-seccion"><div class="veredicto-subtitulo verde">A favor</div><ul>`;
    v.pros.forEach((t) => { html += `<li>${esc(t)}</li>`; });
    html += `</ul></div>`;
  }
  if (v.cons && v.cons.length) {
    html += `<div class="veredicto-seccion"><div class="veredicto-subtitulo rojo">En contra</div><ul>`;
    v.cons.forEach((t) => { html += `<li>${esc(t)}</li>`; });
    html += `</ul></div>`;
  }
  if (v.watch_points && v.watch_points.length) {
    html += `<div class="veredicto-seccion"><div class="veredicto-subtitulo ambar">A tener en cuenta</div><ul>`;
    v.watch_points.forEach((t) => { html += `<li>${esc(t)}</li>`; });
    html += `</ul></div>`;
  }
  html += `<div class="veredicto-aviso">${esc(v.notice)}</div>`;
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

/* ------------------------------------------------------------ navegacion */

function changeView(name) {
  document.querySelectorAll(".pestana").forEach((b) =>
    b.classList.toggle("activa", b.dataset.view === name));
  document.querySelectorAll(".vista").forEach((v) =>
    v.classList.toggle("activa", v.id === `vista-${name}`));

  if (name === "panel") loadDashboard();
  if (name === "movimientos") { loadTransactions(); loadRecurring(); }
  if (name === "cartera") loadPortfolio();
  if (name === "ayuda") loadHelp();
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
  panel: {
    titulo: "Panel",
    contenido: `<p>Vista general: patrimonio, ahorro, ingresos/gastos y plusvalia de la cartera.</p>
      <ul>
        <li><b>Patrimonio</b> = efectivo en cuentas + valor de mercado de la cartera.</li>
        <li><b>Ahorro</b> = ingresos - gastos del mes. La tasa es el porcentaje.</li>
        <li><b>Cuentas</b> — crear, renombrar o eliminar. Saldo se recalcula solo.</li>
        <li><b>Presupuestos</b> — limite por categoria. Se compara con gasto real.</li>
      </ul>`,
  },
  movimientos: {
    titulo: "Movimientos",
    contenido: `<p>Registrar gastos e ingresos. Importes siempre positivos; el tipo va en campo aparte.</p>
      <ul>
        <li><b>Fecha</b> por defecto es hoy. Poner la real si es apunte atrasado.</li>
        <li><b>Categorias</b> — reutilizar existentes (minusculas, sin acentos).</li>
        <li><b>Filtros</b> — mes, tipo y texto libre.</li>
        <li><b>Recurrentes</b> — nominas, alquiler. Se generan con "Generar pendientes".</li>
        <li><b>Traspasos</b> — mover saldo sin contar como ingreso/gasto.</li>
      </ul>`,
  },
  cartera: {
    titulo: "Cartera",
    contenido: `<p>Gestionar posiciones de inversion. Compras/ventas y valor actualizado.</p>
      <ul>
        <li><b>Tarjetas</b> — valor de mercado, plusvalia latente, realizado.</li>
        <li><b>Tabla</b> — coste medio, precio, variacion, peso, P&L.</li>
        <li><b>Actualizar precios</b> — boton manual o auto cada 60s si mercado abierto.</li>
        <li><b>Watchlist</b> — seguir tickers sin posicion.</li>
        <li><b>Monedas</b> — compra en EUR, cotiza en USD = conversion automatica.</li>
      </ul>`,
  },
  acciones: {
    titulo: "Acciones",
    contenido: `<p>Ficha completa: cotizacion, fundamentales, historico y valoracion.</p>
      <ul>
        <li><b>Busqueda</b> — nombre o ticker. Europeos sondeados aparte.</li>
        <li><b>Fundamentales</b> — PER, EV/EBITDA, margen, ROE, dividendo.</li>
        <li><b>Historico 1 ano</b> — rentabilidad, volatilidad, RSI, medias moviles.</li>
        <li><b>Valoracion</b> — DCF con sensibilidad + Graham.</li>
      </ul>`,
  },
  movimientos_vacio: {
    titulo: "Como apuntar gastos",
    contenido: `<p>Ve a la pestana <b>Movimientos</b> y rellena el formulario:</p>
      <ul>
        <li>Selecciona <b>Gasto</b> o <b>Ingreso</b>.</li>
        <li>Pon el <b>importe</b> (positivo).</li>
        <li>Escribe una <b>categoria</b> (reutiliza las que ya existen).</li>
        <li>La <b>fecha</b> por defecto es hoy.</li>
      </ul>
      <p>Tambien puedes importar CSV del banco desde Claude Code: "importa este CSV del banco".</p>`,
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

  document.querySelector('#form-movimiento input[name="date"]').value = today();
  document.getElementById("filtro-mes").value = currentMonth();

  wireForm("form-movimiento", "finance", "transaction_create",
    () => { loadTransactions(); document.querySelector('#form-movimiento input[name="date"]').value = today(); });
  wireForm("form-cuenta", "finance", "account_create", loadDashboard);
  wireForm("form-presupuesto", "finance", "budget_set", loadDashboard);
  wireForm("form-traspaso", "finance", "transfer_create",
    () => { loadTransactions(); loadDashboard(); });
  wireForm("form-recurrente", "finance", "recurring_create", loadRecurring);
  wireForm("form-operacion", "finance", "trade_create", loadPortfolio);
  wireForm("form-watchlist", "finance", "watchlist_add", loadWatchlist);

  document.getElementById("form-buscar").addEventListener("submit", (ev) => {
    ev.preventDefault();
    search(new FormData(ev.target).get("query"));
  });

  ["filtro-mes", "filtro-tipo"].forEach((id) =>
    document.getElementById(id).addEventListener("change", () => loadTransactions()));
  let t;
  document.getElementById("filtro-texto").addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(() => loadTransactions(), 320);
  });

  document.getElementById("btn-refrescar-cartera").addEventListener("click", () => loadPortfolio());

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

  document.getElementById("btn-aplicar-recurrentes").addEventListener("click", async () => {
    try {
      const sim = await api("finance", "recurring_apply", { simulate: true });
      if (!sim.pending) return notify("No hay movimientos pendientes de generar");
      const detalle = sim.detail.slice(0, 8).map((p) => `  ${p.date}  ${p.concept}  ${p.amount}`).join("\n");
      if (!confirm(`Se generaran ${sim.pending} movimientos:\n\n${detalle}${
        sim.pending > 8 ? "\n  …" : ""}\n\n¿Continuar?`)) return;
      const r = await api("finance", "recurring_apply", { simulate: false });
      notify(`${r.pending} movimientos generados`, "bueno");
      loadRecurring();
      loadTransactions();
    } catch (e) {
      notify(e.message, "malo");
    }
  });

  document.getElementById("estado").textContent = `datos locales · ${CURRENCY}`;
  loadDashboard();
  startAutoRefresh();
});
