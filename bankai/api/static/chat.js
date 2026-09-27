// All model/tool output is untrusted: it is rendered as plain text (textContent) only.
const $ = (id) => document.getElementById(id);
let token = "", sessionId = null, poller = null, busy = false;

function bubble(cls, text) {
  const d = document.createElement("div");
  d.className = "msg " + cls;
  d.textContent = text;
  $("log").appendChild(d);
  d.scrollIntoView({ block: "end" });
  return d;
}
function chip(parent, cls, text) {
  let box = parent.querySelector(".chips");
  if (!box) { box = document.createElement("div"); box.className = "chips"; parent.appendChild(box); }
  const c = document.createElement("span");
  c.className = "chip " + cls;
  c.textContent = text;
  box.appendChild(c);
}
// Confirm / Cancel buttons: they just send "yes" / "no" as the customer's own next message. The server (not the model)
// interprets that message, so a button is exactly as authoritative as typing the word.
function confirmButtons(parent) {
  const row = document.createElement("div");
  row.className = "row confirm";
  const yes = document.createElement("button"), no = document.createElement("button");
  yes.textContent = "Yes, confirm transfer";
  no.textContent = "No, cancel"; no.className = "secondary";
  const go = (word) => { yes.disabled = no.disabled = true; send(word); };
  yes.onclick = () => go("yes"); no.onclick = () => go("no");
  row.append(yes, no);
  parent.appendChild(row);
  row.scrollIntoView({ block: "end" });
}

$("login").onclick = async () => {
  const r = await fetch("/auth/dev-token", { method: "POST", headers: { "content-type": "application/json" },
                                             body: JSON.stringify({ username: $("user").value }) });
  if (!r.ok) { $("who").textContent = "login failed"; return; }
  token = (await r.json()).token;
  sessionId = null;
  $("log").textContent = "";
  $("who").textContent = "signed in as " + $("user").value;
  $("msg").disabled = false; $("send").disabled = false; $("msg").focus();
  clearInterval(poller);
  poller = setInterval(pollNotifications, 3000);
};

async function pollNotifications() {
  if (!token) return;
  try {
    const r = await fetch("/notifications", { headers: { authorization: "Bearer " + token } });
    if (!r.ok) return;
    for (const n of (await r.json()).notifications) bubble("note", "Update: " + n.body);
  } catch (e) { /* server restarting: try again on the next tick */ }
}

async function send(text) {
  text = (text || "").trim();
  if (!text || !token || busy) return;
  busy = true; $("send").disabled = true;
  bubble("user", text);
  const bot = bubble("bot", "");
  const body = document.createElement("span");
  bot.appendChild(body);
  let needsConfirm = false;
  try {
    const r = await fetch("/chat", { method: "POST",
      headers: { "content-type": "application/json", authorization: "Bearer " + token },
      body: JSON.stringify({ message: text, session_id: sessionId }) });
    if (r.status === 404 || r.status === 410) { sessionId = null; body.textContent = "Session expired. Please ask again."; return; }
    if (!r.ok) { body.textContent = "Request failed (" + r.status + ")."; return; }
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, i); buf = buf.slice(i + 2);
        const ev = /^event: (.*)$/m.exec(raw), data = /^data: (.*)$/m.exec(raw);
        if (!ev || !data) continue;
        const d = JSON.parse(data[1]);
        if (ev[1] === "progress") {
          if (d.type === "route") chip(bot, "route", "route: " + (d.agents.join(", ") || "human"));
          else if (d.type === "tool") chip(bot, "", "tool: " + d.name);
          else if (d.type === "guardrail") {
            chip(bot, "guard", "guardrail: " + d.code);
            if (d.code === "confirmation_required") needsConfirm = true;
          } else if (d.type === "delegate") chip(bot, "route", d.from + " -> " + d.to);
        } else if (ev[1] === "session") sessionId = d.session_id;
        else if (ev[1] === "token") body.textContent += d.text;
        else if (ev[1] === "done") {
          if (d.outcome !== "ok") chip(bot, "guard", d.outcome);
          for (const f of d.flags) chip(bot, "guard", "flag: " + f);
          if (d.approvals.length) chip(bot, "guard", "awaiting staff approval - see /staff");
          chip(bot, "ok", "trace " + d.trace_id.slice(0, 8));
          if (needsConfirm) confirmButtons(bot);
        } else if (ev[1] === "error") body.textContent = d.message;
      }
    }
  } catch (e) {
    body.textContent = "Could not reach the server. Please try again.";
  } finally { busy = false; $("send").disabled = false; $("msg").focus(); }
}

$("form").onsubmit = (e) => { e.preventDefault(); const t = $("msg").value; $("msg").value = ""; send(t); };
