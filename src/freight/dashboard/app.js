// Freight Rep Desk: Cognito hosted UI login (authorization code + PKCE), then the /rep API.
(function () {
  "use strict";
  const cfg = window.FREIGHT_CONFIG || {};
  const redirectUri = location.origin + location.pathname;
  const QUEUES = [
    ["review", "Review"],
    ["human", "Needs human"],
    ["quoted", "Quoted"],
    ["asking", "Asking customer"],
    ["completed", "Completed"],
  ];
  const FIELD_LABELS = {
    quantity: "Quantity", packaging: "Packaging", total_weight_kg: "Total weight (kg)",
    dimensions: "Dimensions", pickup: "Pickup", delivery: "Delivery",
    requested_delivery_date: "Deliver by", mode_preference: "Mode", priority: "Priority",
    temperature_control: "Temperature", incoterms: "Incoterms", responsibilities: "Responsibilities",
  };
  let tab = "review";
  let current = null;
  let currentState = null;

  // --- auth ---------------------------------------------------------------------------------
  function storage(kind) {
    try { return window[kind]; } catch (e) { return null; }
  }
  const session = storage("sessionStorage");
  function getToken() {
    const raw = session && session.getItem("freight.token");
    if (!raw) return null;
    const t = JSON.parse(raw);
    return t.expires > Date.now() ? t.idToken : null;
  }
  function b64url(bytes) {
    return btoa(String.fromCharCode.apply(null, new Uint8Array(bytes)))
      .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  async function signIn() {
    const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
    const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
    session.setItem("freight.verifier", verifier);
    const q = new URLSearchParams({
      response_type: "code", client_id: cfg.clientId, redirect_uri: redirectUri,
      scope: "openid email", code_challenge_method: "S256", code_challenge: challenge,
    });
    location.assign(`${cfg.cognitoDomain}/oauth2/authorize?${q}`);
  }
  async function finishSignIn(code) {
    const body = new URLSearchParams({
      grant_type: "authorization_code", client_id: cfg.clientId, code,
      redirect_uri: redirectUri, code_verifier: session.getItem("freight.verifier") || "",
    });
    const r = await fetch(`${cfg.cognitoDomain}/oauth2/token`, {
      method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body,
    });
    if (!r.ok) throw new Error("sign-in failed");
    const t = await r.json();
    session.setItem("freight.token", JSON.stringify({
      idToken: t.id_token, expires: Date.now() + (t.expires_in - 60) * 1000,
    }));
    history.replaceState(null, "", redirectUri);
  }
  function signOut() {
    session.removeItem("freight.token");
    const q = new URLSearchParams({ client_id: cfg.clientId, logout_uri: redirectUri });
    location.assign(`${cfg.cognitoDomain}/logout?${q}`);
  }

  // --- api ----------------------------------------------------------------------------------
  async function api(path, opts) {
    const r = await fetch(cfg.apiBase + path, Object.assign({}, opts, {
      headers: Object.assign({ authorization: `Bearer ${getToken()}`, "content-type": "application/json" },
        (opts || {}).headers),
    }));
    if (r.status === 401) { session.removeItem("freight.token"); location.reload(); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.message || `HTTP ${r.status}`);
    return data;
  }
  const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

  // --- rendering helpers --------------------------------------------------------------------
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function fmtValue(v) {
    if (v == null) return "";
    if (typeof v !== "object") return String(v);
    if ("amount" in v && "unit" in v) return `${v.amount} ${v.unit}`;
    if ("city" in v) return [v.name, v.street, v.postal_code, v.city, v.country].filter(Boolean).join(", ");
    if ("required" in v) return v.required ? `required ${v.min_c ?? ""}..${v.max_c ?? ""} °C` : "not required";
    if ("length_cm" in v) return `${v.length_cm}×${v.width_cm}×${v.height_cm} cm × ${v.pieces}`;
    return Object.entries(v).filter(([, x]) => x != null).map(([k, x]) => `${k}: ${x}`).join(", ");
  }
  // Wrap each source quote that came from this email in <mark>, matching case-insensitively.
  function highlight(text, quotes) {
    let html = esc(text);
    const uniq = [...new Map(quotes.map((q) => [q.quote.toLowerCase(), q])).values()]
      .sort((a, b) => b.quote.length - a.quote.length);
    for (const q of uniq) {
      const needle = esc(q.quote).replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+");
      html = html.replace(new RegExp(`(${needle})(?![^<]*>)`, "i"),
        `<mark title="${esc(q.field)}">$1</mark>`);
    }
    return html;
  }

  // --- views --------------------------------------------------------------------------------
  function renderTabs(counts) {
    document.getElementById("tabs").innerHTML = QUEUES.map(([k, label]) =>
      `<button data-tab="${k}" class="${k === tab ? "active" : ""}">${label} (${(counts[k] || []).length})</button>`).join("");
  }
  async function loadQueue() {
    const queues = await api("/rep/queue");
    renderTabs(queues);
    const items = queues[tab] || [];
    document.getElementById("list").innerHTML = items.length ? items.map((o) => `
      <button class="order ${o.orderId === current ? "active" : ""}" data-order="${esc(o.orderId)}">
        <div class="id">${esc(o.orderId)}</div>
        <div class="sub">${esc(o.customer)} · ${esc(o.subject)}</div>
        <div class="sub">${esc(o.updatedAt.slice(0, 16).replace("T", " "))}</div>
      </button>`).join("") : `<div class="empty">Nothing here.</div>`;
  }

  function actionsFor(meta) {
    const s = meta.state;
    if (s === "PENDING_REP_REVIEW") return `
      <button class="primary" data-act="approve">Approve &amp; send quote</button>
      <button data-act="replan">Re-plan after edits</button>
      <button class="danger" data-act="reject">Reject</button>`;
    if (s === "NEEDS_HUMAN") return `
      <button class="primary" data-act="retry_extraction">Re-run extraction</button>
      <button data-act="replan">Re-plan</button>
      <button class="danger" data-act="cancel">Cancel order</button>`;
    if (s === "QUOTE_SENT") return `<button data-act="reextract">Customer changed details: re-extract</button>`;
    return `<span class="muted">No actions in state ${esc(s)}.</span>`;
  }

  async function loadOrder(id) {
    current = id;
    const d = await api(`/rep/orders/${encodeURIComponent(id)}`);
    const m = d.meta;
    currentState = m.state;
    const req = (d.extraction && d.extraction.request) || {};
    const quotesByEmail = {};
    for (const [field, f] of Object.entries(req)) {
      for (const s of f.sources || []) (quotesByEmail[s.email_id] = quotesByEmail[s.email_id] || []).push({ field, quote: s.quote });
    }
    const editable = ["PENDING_REP_REVIEW", "NEEDS_HUMAN"].includes(m.state);
    const fields = Object.entries(req).map(([name, f]) => `
      <tr>
        <td>${esc(FIELD_LABELS[name] || name)}</td>
        <td><span class="chip ${esc(f.status)}">${esc(f.status)}</span></td>
        <td>${esc(f.status === "conflicting" ? f.candidates.map(fmtValue).join(" / ") : fmtValue(f.value))}</td>
        <td>${f.status === "missing" ? "" : esc(Math.round(f.confidence * 100) + "%")}</td>
        <td class="muted">${(f.sources || []).map((s) => `“${esc(s.quote)}”`).join("<br>")}</td>
        <td>${editable ? `<button data-edit="${esc(name)}">Edit</button>` : ""}</td>
      </tr>`).join("");
    const quotes = d.quotes.map((q) => `
      <tr><td>${esc(q.labels.join(", "))}</td><td>${esc(q.mode)}</td><td>${esc(q.service)}<br><span class="muted">${esc(q.carrier)}</span></td>
      <td>${esc(q.departure)} → ${esc(q.arrival)}</td><td>${esc(q.price)} ${esc(q.currency)}</td>
      <td class="muted">${esc(q.validUntil.slice(0, 16).replace("T", " "))}</td></tr>`).join("");
    const emails = d.emails.map((e) => `
      <div class="card"><div class="row"><span class="chip">${esc(e.direction)}</span>
      <strong>${esc(e.subject)}</strong><span class="muted">${esc(e.sentAt.slice(0, 16).replace("T", " "))}</span></div>
      <pre class="email">${highlight(e.text, quotesByEmail[e.emailId] || [])}</pre>
      ${(e.attachments || []).map((a) => `<div class="muted">Attachment ${esc(a.filename)}</div>
        <pre class="email">${highlight(a.text || "(no text)", quotesByEmail[e.emailId] || [])}</pre>`).join("")}</div>`).join("");
    const events = d.events.slice().reverse().map((e) => `
      <li><span class="muted">${esc(e.at.slice(0, 19).replace("T", " "))}</span> <strong>${esc(e.kind)}</strong>
      ${e.detail && e.detail.from ? `${esc(e.detail.from)} → ${esc(e.detail.to)}` : ""}
      <span class="muted">${esc((e.detail && e.detail.reason) || "")} · ${esc(e.actor)}</span></li>`).join("");

    document.getElementById("detail").innerHTML = `
      <div class="card">
        <div class="row"><h2 style="margin:0">${esc(m.order_id)}</h2><span class="chip">${esc(m.state)}</span>
        <span class="muted">${esc(m.customer_name || m.customer_email)} · ${esc(m.language)}</span></div>
        ${m.review_reasons && m.review_reasons.length && m.state === "PENDING_REP_REVIEW"
          ? `<div class="reasons">Why you are seeing this: ${m.review_reasons.map(esc).join("; ")}</div>` : ""}
        ${m.injection_suspected ? `<div class="reasons">Possible prompt injection in the email. The model's output cannot act on it, but read the email.</div>` : ""}
        <div class="row" style="margin-top:12px">${actionsFor(m)}</div>
      </div>
      <div class="card"><h2>Extracted request (v${esc(d.extractionCount)})</h2><div class="scroll"><table>
        <tr><th>Field</th><th>Status</th><th>Value</th><th>Conf.</th><th>Source</th><th></th></tr>${fields}</table></div></div>
      ${d.plan ? `<div class="card"><h2>Plan</h2><div>${esc(d.plan)}</div></div>` : ""}
      ${quotes ? `<div class="card"><h2>Options (round ${esc(m.quote_round)})</h2><div class="scroll"><table>
        <tr><th>Label</th><th>Mode</th><th>Service</th><th>Schedule</th><th>Total</th><th>Valid until</th></tr>${quotes}</table></div></div>` : ""}
      <h2>Emails</h2>${emails}
      <div class="card"><h2>Audit trail</h2><ul class="events">${events}</ul></div>`;
    loadQueue();
  }

  async function act(action) {
    const id = current;
    const routes = {
      approve: ["decision", { decision: "approve" }], reject: ["decision", { decision: "reject" }],
      replan: ["replan", {}], reextract: ["reextract", {}],
      retry_extraction: ["resolve", { action: "retry_extraction" }],
      cancel: ["resolve", { action: "cancel" }],
    };
    let [path, body] = routes[action];
    if (action === "replan" && currentState === "NEEDS_HUMAN") {
      [path, body] = ["resolve", { action: "replan" }];
    }
    if ((action === "reject" || action === "cancel") && !confirm("Are you sure?")) return;
    await post(`/rep/orders/${encodeURIComponent(id)}/${path}`, body);
    setTimeout(() => loadOrder(id), 1500);
  }

  async function edit(field) {
    const raw = prompt(`New value for ${field} as JSON, e.g. {"amount": 18, "unit": "pallets"} or "2026-12-15"`);
    if (raw == null) return;
    let value;
    try { value = JSON.parse(raw); } catch (e) { value = raw; }
    try {
      await post(`/rep/orders/${encodeURIComponent(current)}/edit`, { field, value });
      loadOrder(current);
    } catch (e) { alert(e.message); }
  }

  function simulateForm() {
    document.getElementById("detail").innerHTML = `
      <div class="card"><h2>Simulate an inbound email</h2>
      <p class="muted">Writes an .eml to the raw bucket's inbound/ prefix, exactly where SES would.</p>
      <div class="row"><label>From <input id="sim-from" value="customer@example.com"></label>
      <label>Subject <input id="sim-subject" value="Anfrage Corona"></label></div>
      <p><textarea id="sim-text">Wir möchten 18 Paletten Corona Extra in Flaschen nach Berlin (Torstraße 1, 10115 Berlin), bis 15.12.2026.</textarea></p>
      <button class="primary" id="sim-send">Send</button></div>`;
    document.getElementById("sim-send").onclick = async () => {
      const r = await post("/rep/simulate", {
        from: document.getElementById("sim-from").value,
        subject: document.getElementById("sim-subject").value,
        text: document.getElementById("sim-text").value,
      });
      alert(`Uploaded ${r.s3Key}. The order appears in a few seconds.`);
    };
  }

  // --- wiring -------------------------------------------------------------------------------
  document.addEventListener("click", (ev) => {
    const t = ev.target.closest("button");
    if (!t) return;
    if (t.dataset.tab) { tab = t.dataset.tab; loadQueue(); }
    else if (t.dataset.order) loadOrder(t.dataset.order);
    else if (t.dataset.act) act(t.dataset.act).catch((e) => alert(e.message));
    else if (t.dataset.edit) edit(t.dataset.edit);
  });

  async function boot() {
    const code = new URLSearchParams(location.search).get("code");
    if (code) await finishSignIn(code);
    if (!getToken()) {
      document.getElementById("login").hidden = false;
      document.getElementById("signin").onclick = signIn;
      return;
    }
    document.getElementById("app").hidden = false;
    document.getElementById("refresh").onclick = () => (current ? loadOrder(current) : loadQueue());
    document.getElementById("signout").onclick = signOut;
    document.getElementById("simulate-open").onclick = simulateForm;
    loadQueue();
  }
  boot().catch((e) => { document.body.textContent = `Error: ${e.message}`; });
})();
