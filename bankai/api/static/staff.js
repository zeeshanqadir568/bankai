const $ = (id) => document.getElementById(id);
let token = "", poller = null;
const H = () => ({ authorization: "Bearer " + token, "content-type": "application/json" });
const money = (c) => "$" + (c / 100).toLocaleString(undefined, { minimumFractionDigits: 2 });

function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
function status(msg) { $("status").textContent = msg; }
function show(msg) { $("out").textContent = msg; $("out").scrollIntoView({ block: "nearest" }); }
function needLogin() {
  if (token) return false;
  status("Sign in first (button at the top).");
  show("You are not signed in. Click 'Sign in as sam (approver)' at the top of the page first.");
  return true;
}

async function load() {
  const list = $("list");
  let r;
  try { r = await fetch("/staff/approvals", { headers: H() }); }
  catch (e) { list.textContent = "Could not reach the server - retrying..."; return; }
  list.textContent = "";
  if (!r.ok) { list.appendChild(el("p", "mut", "Could not load approvals (" + r.status + ").")); return; }
  const { approvals } = await r.json();
  if (!approvals.length) list.appendChild(el("p", "mut", "Nothing waiting for approval. (Small transfers to known payees complete immediately. A transfer waits here only if it is $1,000 or more, or to a new account - AND the customer answered 'yes' to the confirmation.)"));
  for (const a of approvals) {
    const card = el("div", "card");
    card.appendChild(el("div", "", money(a.amount_cents) + "  " + a.from_account.slice(-4).padStart(8, "*") + "  ->  " + a.to_account.slice(-4).padStart(8, "*")));
    card.appendChild(el("div", "mut", "requested by " + a.requested_by + " | reasons: " + a.reasons.join(", ") + " | memo: " + (a.memo || "-")));
    const row = el("div", "row");
    const ok = el("button", "", "Approve"), no = el("button", "danger", "Deny");
    ok.onclick = () => decide(a.approval_id, true); no.onclick = () => decide(a.approval_id, false);
    row.append(ok, no); card.appendChild(row); list.appendChild(card);
  }
}
async function decide(id, approve) {
  const r = await fetch("/staff/approvals/" + id, { method: "POST", headers: H(), body: JSON.stringify({ approve, note: "" }) });
  if (!r.ok) alert("Decision failed: " + r.status + " " + (await r.text()));
  load();
}
async function loadTraces() {
  const box = $("traces");
  let r;
  try { r = await fetch("/debug/traces", { headers: H() }); } catch (e) { return; }
  if (!r.ok) return;
  const { traces } = await r.json();
  box.textContent = "";
  if (!traces.length) { box.appendChild(el("p", "mut", "No conversations yet.")); return; }
  for (const t of traces) {
    const row = el("div", "row trace");
    row.appendChild(el("span", "mut", new Date(t.ts * 1000).toLocaleTimeString() + "  " + t.user + "  [" + t.outcome + "]"));
    row.appendChild(el("span", "grow", t.message));
    const b = el("button", "secondary", "Replay");
    b.onclick = () => replay(t.trace_id);
    row.appendChild(b);
    box.appendChild(row);
  }
}
async function replay(id) {
  if (needLogin()) return;
  status("Loading trace...");
  $("traceid").value = id;
  const r = await fetch("/debug/traces/" + encodeURIComponent(id) + "?format=text", { headers: H() });
  status(r.ok ? "Trace loaded (below)." : "Trace not found (" + r.status + ").");
  show(r.ok ? await r.text() : "Trace not found (" + r.status + "). Pick one from 'Recent conversations' above, or paste the full 32-character trace id.");
}
async function stream() {  // instant push; the 4s poll below is the safety net if the stream drops
  try {
    const r = await fetch("/staff/stream", { headers: H() });
    $("live").textContent = "(live)";
    const reader = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      while (buf.includes("\n\n")) { const i = buf.indexOf("\n\n"); const raw = buf.slice(0, i); buf = buf.slice(i + 2); if (raw.includes("approval_requested")) load(); }
    }
  } catch (e) { /* fall back to polling */ }
  $("live").textContent = "";
}
$("login").onclick = async () => {
  const r = await fetch("/auth/dev-token", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ username: "sam" }) });
  token = (await r.json()).token; $("who").textContent = "signed in as sam"; status("");
  $("list").textContent = "Loading...";
  load(); loadTraces(); stream();
  clearInterval(poller); poller = setInterval(() => { load(); loadTraces(); }, 4000);
};
$("loadtrace").onclick = () => {
  if (needLogin()) return;
  const id = $("traceid").value.trim();
  if (!id) { status("Nothing to replay."); show("Paste a trace id first, or click 'Replay' next to a conversation in 'Recent conversations' above."); return; }
  replay(id);
};
$("audit").onclick = async () => {
  if (needLogin()) return;
  status("Verifying audit chain...");
  const r = await fetch("/debug/audit", { headers: H() });
  if (!r.ok) { status("Failed (" + r.status + ")."); show("Could not verify (" + r.status + ")."); return; }
  const d = await r.json();
  status(d.chain_ok ? "Audit chain OK - " + d.rows + " records, none tampered." : "AUDIT CHAIN BROKEN: " + d.message);
  show((d.chain_ok ? "audit chain OK" : "audit chain BROKEN") + " - " + d.rows + " records checked (" + d.message + ")\n\nMost recent entries:\n" +
       d.recent.slice(0, 12).map((a) => "#" + a.id + "  " + a.decision.padEnd(5) + " " + a.actor.padEnd(10) + " " + a.tool + "  " + (a.reason || "")).join("\n"));
};
