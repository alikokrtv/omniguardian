"use strict";
const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat();
const money = new Intl.NumberFormat("en-US", {style: "currency", currency: "USD", maximumFractionDigits: 2});
let credential = "", generation = 0, timer, controller, busy = false;
let tab = "overview", inventoryCursor = null, inventoryNext = null, auditCursor = null, auditNext = null;
let search = "", auditFilters = {};
let currentLang = localStorage.getItem("omni_lang") || "en";

const I18N = {
  en: {
    toggleBtn: "🇹🇷 TR",
    headings: {
      overview: ["Overview", "Operations overview", "A live view of your stock. A clear record of every decision."],
      audit: ["Audit trail", "Every movement. On record.", "Trace stock decisions back to an order, a moment, and an actor."],
      billing: ["Plan & usage", "Built for your next stage.", "Track your monthly allocations and understand your plan capacity."]
    },
    actionLabels: {
      RESERVE: "Inventory reserved", CONFIRM: "Order confirmed", CANCEL: "Reservation cancelled",
      FULFILL: "Order fulfilled", MANUAL_ADJUST: "Inventory adjusted", B2B_CONFIRM: "Wholesale confirmed",
      B2B_CANCEL: "Wholesale cancelled", B2B_FULFILL: "Wholesale shipped", RETURN_RECEIVE: "Return received",
      QUARANTINE_RELEASE: "Quarantine released"
    },
    statusLabels: { IN_STOCK: "In stock", LOW_STOCK: "Low stock", OOS: "Out of stock" },
    kpi: ["Total over-allocation prevented", "Estimated penalties saved", "Monthly allocations"],
    cards: ["Distinct orders protected · all time", "USD estimate · not verified savings", "Current UTC billing cycle"],
    headers: ["Product / SKU", "Physical SOH", "B2B", "Reserved", "Live AFS", "Status"],
    loginTitle: "A clearer view of your operations.",
    loginDesc: "Connect your workspace to see inventory, protected orders, and a complete history of every stock movement."
  },
  tr: {
    toggleBtn: "🇺🇸 EN",
    headings: {
      overview: ["Genel Bakış", "Operasyonel Genel Bakış", "Canlı stok görünümü. Alınan her kararın şeffaf kaydı."],
      audit: ["Denetim İzi", "Tüm Hareketler. Kayıt Altında.", "Stok kararlarını siparişe, zamana ve kullanıcıya kadar izleyin."],
      billing: ["Plan & Kullanım", "Büyüyen Operasyonlar İçin.", "Aylık sipariş tahsisatınızı ve plan kotanızı takip edin."]
    },
    actionLabels: {
      RESERVE: "Stok Rezerve Edildi", CONFIRM: "Sipariş Onaylandı", CANCEL: "Rezervasyon İptal",
      FULFILL: "Sipariş Kargolandı", MANUAL_ADJUST: "Stok Güncellendi", B2B_CONFIRM: "Toptan PO Onaylandı",
      B2B_CANCEL: "Toptan PO İptal", B2B_FULFILL: "Toptan PO Sevk Edildi", RETURN_RECEIVE: "İade Teslim Alındı",
      QUARANTINE_RELEASE: "Karantinadan Çıkarıldı"
    },
    statusLabels: { IN_STOCK: "Stokta Var", LOW_STOCK: "Kritik Stok", OOS: "Tükendi" },
    kpi: ["Engellenen Çift Satış (Oversell)", "Kurtarılan Ceza Tutarı", "Aylık Sipariş Tahsisi"],
    cards: ["Korunan tekil sipariş sayısı · Tüm zamanlar", "Tahmini ceza tasarrufu (USD)", "Mevcut UTC fatura dönemi"],
    headers: ["Ürün / SKU", "Fiziki Stok", "Toptan B2B", "Rezerve", "Serbest AFS", "Durum"],
    loginTitle: "Operasyonlarınız İçin Şeffaf ve Güvenli Bakış.",
    loginDesc: "Depo stoklarınızı, korunan siparişleri ve tüm envanter hareketlerini canlı görmek için çalışma alanınıza bağlanın."
  }
};

function applyLanguage() {
  const dict = I18N[currentLang];
  if ($("lang-toggle")) $("lang-toggle").textContent = dict.toggleBtn;
  if ($("breadcrumb")) $("breadcrumb").textContent = dict.headings[tab][0];
  if ($("page-title")) $("page-title").textContent = dict.headings[tab][1];
  if ($("page-description")) $("page-description").textContent = dict.headings[tab][2];
  
  // Sidebar tabs
  const navBtns = document.querySelectorAll(".nav-item");
  if (navBtns.length >= 3) {
    navBtns[0].innerHTML = `<span>◫</span> ${dict.headings.overview[0]}`;
    navBtns[1].innerHTML = `<span>≡</span> ${dict.headings.audit[0]}`;
    navBtns[2].innerHTML = `<span>◷</span> ${dict.headings.billing[0]}`;
  }
  
  // KPI labels
  const kpiCards = document.querySelectorAll(".kpi-grid article.kpi");
  if (kpiCards.length >= 3) {
    kpiCards[0].querySelector(".card-label").childNodes[0].textContent = dict.kpi[0] + " ";
    kpiCards[0].querySelector("p").textContent = dict.cards[0];
    kpiCards[1].querySelector(".card-label").childNodes[0].textContent = dict.kpi[1] + " ";
    kpiCards[1].querySelector("p").textContent = dict.cards[1];
    kpiCards[2].querySelector(".card-label").childNodes[0].textContent = dict.kpi[2] + " ";
  }
  
  // Table headers
  const ths = document.querySelectorAll(".inventory-panel table thead th");
  if (ths.length >= 6) {
    dict.headers.forEach((h, i) => { if (ths[i]) ths[i].textContent = h; });
  }
}

function toggleLanguage() {
  currentLang = currentLang === "en" ? "tr" : "en";
  localStorage.setItem("omni_lang", currentLang);
  applyLanguage();
  if (credential) refresh();
}

const headings = I18N.en.headings;
const actionLabels = I18N.en.actionLabels;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function emptyRow(body, count, message) {
  const row = element("tr"), cell = element("td", message, "empty");
  cell.colSpan = count; row.append(cell); body.replaceChildren(row);
}
function connection(label, state) {
  $("connection-label").textContent = label;
  $("connection-dot").className = `dot ${state}`;
}
async function api(path) {
  const response = await fetch(path, {headers: {"X-API-Key": credential}, cache: "no-store", signal: controller.signal});
  if (!response.ok) {
    const error = new Error(response.status === 401 ? "Your key is invalid or has been revoked. Connect with a valid workspace key." : "Unable to refresh workspace data. Displayed values may be stale; retry shortly.");
    error.status = response.status; throw error;
  }
  return response.json();
}
function renderInventory(data) {
  const body = $("inventory-rows"); body.replaceChildren();
  inventoryNext = data.next_cursor;
  $("inventory-next").disabled = !inventoryNext; $("inventory-first").disabled = !inventoryCursor;
  $("inventory-count").textContent = `${data.items.length} products on this page`;
  if (!data.items.length) return emptyRow(body, 6, "No matching inventory. Products appear once active warehouse stock is provisioned.");
  for (const item of data.items) {
    const row = element("tr"), product = element("td", undefined, "product");
    product.append(element("strong", item.title), element("span", item.sku)); row.append(product);
    for (const field of ["stock_on_hand", "committed_b2b", "in_flight_reserved", "available_for_sale"]) row.append(element("td", number.format(item[field]), field === "available_for_sale" ? "afs" : ""));
    const status = element("td"); status.append(element("span", {IN_STOCK: "In stock", LOW_STOCK: "Low stock", OOS: "Out of stock"}[item.status], `pill ${item.status.toLowerCase()}`));
    row.append(status); body.append(row);
  }
}
function renderActivity(data) {
  const list = $("activity"); list.replaceChildren();
  if (!data.items.length) { list.append(element("li", "No activity yet. Committed inventory events will appear here.", "empty")); return; }
  for (const item of data.items.slice(0, 6)) {
    const row = element("li"), text = element("div", undefined, "event-text");
    text.append(element("strong", actionLabels[item.action] || item.action));
    text.append(element("p", `${item.sku} · ${item.warehouse_code} · AFS ${item.previous_afs} → ${item.new_afs}`));
    const stamp = element("time", new Date(item.timestamp).toLocaleString()); stamp.dateTime = item.timestamp;
    text.append(stamp); row.append(element("span", item.quantity_delta > 0 ? "+" : item.quantity_delta < 0 ? "−" : "✓", "event-icon"), text); list.append(row);
  }
}
function renderAudit(data) {
  const body = $("audit-rows"); body.replaceChildren(); auditNext = data.next_cursor;
  $("audit-next").disabled = !auditNext; $("audit-first").disabled = !auditCursor;
  if (!data.items.length) return emptyRow(body, 6, "No audit records match these filters.");
  for (const item of data.items) {
    const row = element("tr");
    for (const text of [new Date(item.timestamp).toISOString().replace("T", " ").replace("Z", ""), item.action.replaceAll("_", " "), `${item.sku} / ${item.warehouse_code}`, `${item.previous_afs} → ${item.new_afs} (${item.quantity_delta > 0 ? "+" : ""}${item.quantity_delta})`, item.reference_order_id || "—"]) row.append(element("td", text));
    row.append(element("td", item.actor, "actor")); body.append(row);
  }
}
function renderUsage(data) {
  $("usage").textContent = number.format(data.allocation_count);
  $("usage-caption").textContent = `${data.tier_plan} · ${data.quota === null ? "Unlimited orders" : number.format(data.remaining_quota) + " orders remaining"}`;
  $("plan-name").textContent = data.tier_plan[0] + data.tier_plan.slice(1).toLowerCase();
  $("cycle").textContent = `${data.cycle_start} → ${data.cycle_end} · UTC`;
  $("plan-used").textContent = `${number.format(data.allocation_count)} orders`;
  $("plan-limit").textContent = data.quota === null ? "Unlimited quota" : `of ${number.format(data.quota)}`;
  $("quota-progress").value = data.quota === null ? 0 : Math.min(100, data.allocation_count / data.quota * 100);
  $("remaining").textContent = data.quota_exceeded ? "Monthly limit reached. New reservations are paused until renewal or a plan change." : data.quota === null ? "Unlimited allocations. Usage is still metered for visibility." : `${number.format(data.remaining_quota)} allocations available this cycle.`;
}
async function refresh() {
  clearTimeout(timer);
  if (!credential || busy || document.hidden) return;
  busy = true; const current = generation; controller = new AbortController();
  const activeController = controller;
  const timeout = setTimeout(() => activeController.abort(), 12000);
  $("refresh").disabled = true;
  try {
    const inventoryParams = new URLSearchParams({search, limit: "50"});
    if (inventoryCursor) inventoryParams.set("after_sku", inventoryCursor);
    const auditParams = new URLSearchParams({...auditFilters, limit: "50"});
    if (auditCursor) auditParams.set("before_id", auditCursor);
    const [usage, telemetry, activity, inventory, audit] = await Promise.all([
      api("/api/v1/billing/usage"), api("/api/v1/dashboard/telemetry"), api("/api/v1/audit-trail?limit=6"),
      tab === "overview" ? api(`/api/v1/dashboard/inventory?${inventoryParams}`) : null,
      tab === "audit" ? api(`/api/v1/audit-trail?${auditParams}`) : null,
    ]);
    if (current !== generation) return;
    renderUsage(usage); renderActivity(activity);
    $("prevented").textContent = number.format(telemetry.total_over_allocation_prevented);
    $("saved").textContent = money.format(Number(telemetry.estimated_penalties_saved_usd));
    if (inventory) renderInventory(inventory); if (audit) renderAudit(audit);
    $("login").hidden = true; $("workspace").hidden = false; $("disconnect").hidden = false;
    $("error").hidden = true; $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    connection("Connected · live", "live");
  } catch (error) {
    if (current !== generation) return;
    if (error.status === 401) disconnect();
    $("error").textContent = error.message === "Failed to fetch" || error.name === "AbortError" ? "Connection interrupted. Data may be stale. Automatic refresh will retry." : error.message;
    $("error").hidden = false; connection("Refresh unavailable", "stale");
  } finally {
    clearTimeout(timeout);
    if (current === generation) { busy = false; $("refresh").disabled = false; if (credential) timer = setTimeout(refresh, 5000); }
  }
}
function restartRefresh() {
  generation++; if (controller) controller.abort(); busy = false; refresh();
}
function disconnect() {
  credential = ""; generation++; clearTimeout(timer); if (controller) controller.abort(); busy = false;
  $("workspace").hidden = true; $("login").hidden = false; $("disconnect").hidden = true;
  $("api-key").value = ""; $("error").hidden = true; $("refresh").disabled = false;
  for (const id of ["inventory-rows", "audit-rows", "activity"]) $(id).replaceChildren();
  for (const id of ["prevented", "saved", "usage", "plan-name", "plan-used", "plan-limit", "remaining", "cycle", "usage-caption", "updated"]) $(id).textContent = "—";
  inventoryCursor = auditCursor = inventoryNext = auditNext = null; connection("Not connected", "");
}
document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => {
  tab = button.dataset.tab;
  document.querySelectorAll(".nav-item").forEach((item) => { const active = item.dataset.tab === tab; item.classList.toggle("selected", active); if (active) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current"); });
  for (const name of Object.keys(headings)) $(`${name}-view`).hidden = name !== tab;
  [$("breadcrumb").textContent, $("page-title").textContent, $("page-description").textContent] = headings[tab];
  restartRefresh();
}));
$("connect-form").addEventListener("submit", (event) => { event.preventDefault(); credential = $("api-key").value.trim(); $("api-key").value = ""; restartRefresh(); });
$("disconnect").addEventListener("click", disconnect);
$("refresh").addEventListener("click", refresh);
let searchTimer;
$("inventory-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { search = $("inventory-search").value; inventoryCursor = null; restartRefresh(); }, 300); });
$("inventory-next").addEventListener("click", () => { inventoryCursor = inventoryNext; restartRefresh(); });
$("inventory-first").addEventListener("click", () => { inventoryCursor = null; restartRefresh(); });
$("audit-next").addEventListener("click", () => { auditCursor = auditNext; restartRefresh(); });
$("audit-first").addEventListener("click", () => { auditCursor = null; restartRefresh(); });
$("audit-filter").addEventListener("submit", (event) => {
  event.preventDefault(); auditFilters = {}; auditCursor = null;
  if ($("audit-sku").value) auditFilters.sku = $("audit-sku").value;
  if ($("audit-start").value) auditFilters.start_date = $("audit-start").value + "T00:00:00Z";
  if ($("audit-end").value) auditFilters.end_date = $("audit-end").value + "T00:00:00Z";
  restartRefresh();
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); else clearTimeout(timer); });
if ($("lang-toggle")) $("lang-toggle").addEventListener("click", toggleLanguage);
applyLanguage();
