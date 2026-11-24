// CrateUtils-Comfy: Arm / Disarm / Fire-now buttons and a live status line on the trigger nodes,
// plus the Trigger Manager side panel (every armed trigger, pause / fire / disarm, run history).
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const TRIGGERS = new Set(["CUCScheduler", "CUCFolderWatcher", "CUCFileWatcher", "CUCWebhook", "CUCFolderBatch"]);
const POLL_MS = 3000;
let latest = [];
const panels = new Set();

function graphId() {
  return String(app.graph?.id || app.graph?.extra?.crateutils_comfy_id || "workflow");
}

function keyFor(node) {
  return `${graphId()}:${node.id}`;
}

function toast(severity, summary, detail) {
  const t = app.extensionManager?.toast;
  if (t?.add) t.add({ severity, summary, detail, life: severity === "error" ? 8000 : 4000 });
  else if (severity === "error") alert(`${summary}\n${detail}`);
}

function findApiId(output, node) {
  const own = String(node.id);
  if (output[own]?.class_type === node.comfyClass) return own;
  // inside a subgraph the id is "<path>:<id>"
  const hit = Object.keys(output).find((k) => k.endsWith(`:${own}`) && output[k].class_type === node.comfyClass);
  return hit || null;
}

async function post(path, body) {
  const r = await api.fetchApi(path, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

function statusFor(node) {
  const k = keyFor(node);
  return latest.find((t) => t.key === k);
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
               : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function hookUrl(t) {
  return `${location.origin}${api.api_base || ""}${t.hook_path}?token=${encodeURIComponent(t.token || "")}`;
}

function statusText(t) {
  const runs = t.limit ? `${t.fired}/${t.limit} runs` : `${t.fired} runs`;
  if (t.last_error) return `⚠ ${t.last_error}`;
  if (t.done) return `■ Finished, ${runs}`;
  if (t.paused) return `❚❚ Paused, ${runs}`;
  if (t.kind === "schedule") return `● Armed, next ${fmtTime(t.next_at)}, ${runs}`;
  if (t.kind === "batch") return `● Running, ${t.fired}/${t.limit} files queued${t.pending ? `, ${t.pending} to go` : ""}`;
  if (t.kind === "webhook") return `● Listening, ${runs}`;
  if (t.watch_ok === false) return "⚠ Folder unreachable, waiting";
  return `● Watching, ${runs}${t.held ? `, ${t.held} waiting` : ""}`;
}

function label(node) {
  const t = statusFor(node);
  if (!t) return node.comfyClass === "CUCFolderBatch" ? "▶ Start batch" : "▶ Arm (not armed)";
  return `${statusText(t)} (click to ${t.done && node.comfyClass === "CUCFolderBatch" ? "clear" : "disarm"})`.slice(0, 100);
}

function refresh() {
  for (const node of app.graph?._nodes || []) {
    if (!TRIGGERS.has(node.comfyClass) || !node.cuArm) continue;
    const l = label(node);
    if (node.cuArm.label !== l) {
      node.cuArm.label = l;
      node.cuArm.name = l;
      node.setDirtyCanvas?.(true, true);
    }
  }
  for (const p of panels) {
    if (p.isConnected) renderPanel(p);
    else panels.delete(p);
  }
}

async function poll() {
  try {
    const r = await api.fetchApi("/crateutils-comfy/triggers");
    if (r.ok) latest = (await r.json()).triggers || [];
    refresh();
  } catch (_) { /* server restarting */ }
}

async function toggle(node) {
  const key = keyFor(node);
  if (statusFor(node)) {
    await post("/crateutils-comfy/triggers/disarm", { key });
    toast("info", "CrateUtils-Comfy", "Disarmed");
    return poll();
  }
  try {
    const p = await app.graphToPrompt();
    const apiId = findApiId(p.output, node);
    if (!apiId) throw new Error("this node is muted or bypassed, so there is nothing to arm");
    const st = await post("/crateutils-comfy/triggers", { key, node_id: apiId, local_id: String(node.id),
      graph_id: graphId(), title: node.title || "", prompt: p.output, extra_pnginfo: { workflow: p.workflow } });
    let detail = st.summary || "";
    if (st.kind === "webhook") detail += " (URL and token: CrateUtils-Comfy panel in the side bar)";
    toast("success", "CrateUtils-Comfy: armed", detail);
  } catch (e) {
    toast("error", "CrateUtils-Comfy: could not arm", String(e.message || e));
  }
  return poll();
}

async function fireNow(key) {
  try {
    await post("/crateutils-comfy/triggers/fire", { key });
    toast("info", "CrateUtils-Comfy", "Queued a test run");
  } catch (e) {
    toast("error", "CrateUtils-Comfy", e.message);
  }
  setTimeout(poll, 800);
}

// ---------------------------------------------------------------- side panel
function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "style") Object.assign(e.style, v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
}

const S = {
  card: { border: "1px solid var(--border-color, #444)", borderRadius: "6px", padding: "8px 10px", margin: "8px 0",
          background: "var(--comfy-menu-bg, transparent)" },
  btn: { padding: "2px 8px", marginRight: "4px", marginTop: "4px", cursor: "pointer", fontSize: "12px",
         background: "var(--comfy-input-bg, #333)", color: "var(--input-text, inherit)",
         border: "1px solid var(--border-color, #555)", borderRadius: "4px" },
  dim: { opacity: "0.7", fontSize: "12px" },
  mono: { fontFamily: "monospace", fontSize: "11px", wordBreak: "break-all" },
};

function button(text, fn, title = "") {
  return h("button", { style: S.btn, title, onclick: async (ev) => { ev.stopPropagation(); await fn(); poll(); } },
    text);
}

function runRow(r) {
  const file = (r.paths || [])[0] ? (r.paths[0].split(/[\\/]/).pop()) : "";
  const out = (r.outputs || [])[0] || "";
  const dur = r.duration != null ? `${r.duration}s` : "";
  const status = r.status === "success" ? "✓" : r.status === "queued" ? "…" : `✗ ${r.status}`;
  return h("tr", { title: r.error || "" },
    h("td", {}, fmtTime(r.time)), h("td", {}, r.index || "test"), h("td", {}, r.event || ""),
    h("td", { style: S.mono }, file), h("td", {}, status), h("td", {}, dur), h("td", { style: S.mono }, out));
}

function card(t) {
  const here = t.graph_id === graphId();
  const kids = [
    h("div", { style: { display: "flex", justifyContent: "space-between", gap: "6px" } },
      h("b", {}, t.title || t.kind), h("span", { style: S.dim }, t.kind + (here ? "" : " · other workflow"))),
    h("div", { style: { margin: "3px 0" } }, statusText(t)),
    h("div", { style: S.dim }, t.summary),
  ];
  if (t.last_fired) kids.push(h("div", { style: S.dim }, `last run ${fmtTime(t.last_fired)}`));
  if (t.kind === "webhook") {
    const url = hookUrl(t);
    kids.push(h("div", { style: { ...S.mono, margin: "4px 0" } }, url),
      button("Copy URL", () => navigator.clipboard.writeText(url).then(() => toast("info", "CrateUtils-Comfy", "URL copied"))),
      button("Copy curl", () => navigator.clipboard.writeText(
        `curl -X POST "${url}" -H "Content-Type: application/json" -d "{\\"text\\": \\"hello\\"}"`)
        .then(() => toast("info", "CrateUtils-Comfy", "curl command copied"))));
  }
  const actions = h("div", {});
  if (!t.done) actions.append(button(t.paused ? "Resume" : "Pause",
    () => post("/crateutils-comfy/triggers/pause", { key: t.key, paused: !t.paused })));
  actions.append(button("Fire now", () => fireNow(t.key), "Queue one test run (not counted)"));
  actions.append(button("Disarm", () => post("/crateutils-comfy/triggers/disarm", { key: t.key })));
  if (here) actions.append(button("Show node", () => {
    const n = app.graph.getNodeById?.(Number(t.local_id)) || app.graph._nodes.find((x) => String(x.id) === t.local_id);
    if (n) { app.canvas.centerOnNode?.(n); app.canvas.selectNode?.(n); }
  }));
  kids.push(actions);
  const runs = (t.runs || []).slice().reverse();
  if (runs.length) {
    kids.push(h("details", {}, h("summary", { style: { cursor: "pointer", ...S.dim } }, `recent runs (${runs.length})`),
      h("table", { style: { width: "100%", fontSize: "11px", borderCollapse: "collapse" } },
        h("tr", { style: S.dim }, ["time", "#", "event", "file", "status", "took", "output"].map((x) => h("th", {
          style: { textAlign: "left" } }, x))),
        runs.map(runRow))));
  }
  return h("div", { style: S.card, "data-key": t.key }, kids);
}

function renderPanel(root) {
  // Rebuilding the whole panel every poll steals scroll, selection and focus; only do it
  // when the data actually changed, and keep the scroll position when it does.
  const sig = JSON.stringify(latest);
  if (root.dataset.cusig === sig) return;
  root.dataset.cusig = sig;
  const top = root.scrollTop;
  const open = new Set([...root.querySelectorAll("details[open]")].map((d) => d.closest("[data-key]")?.dataset.key));
  root.replaceChildren(
    h("div", { style: { padding: "8px" } },
      h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between" } },
        h("b", {}, "CrateUtils-Comfy triggers"),
        h("span", {}, button("↻", poll, "Refresh"),
          button("Run log (CSV)", () => { window.open(api.apiURL("/crateutils-comfy/runs.csv"), "_blank"); }),
          latest.length ? button("Disarm all", async () => {
            if (confirm(`Disarm all ${latest.length} triggers?`)) await post("/crateutils-comfy/triggers/disarm", { all: true });
          }) : null)),
      latest.length ? latest.map(card)
        : h("p", { style: S.dim }, "Nothing armed. Add a Task Scheduler, Folder Watcher, File Watcher, Webhook "
            + "Trigger or Folder Batch node and press its Arm / Start button.")));
  for (const k of open) root.querySelector(`[data-key="${CSS.escape(k)}"] details`)?.setAttribute("open", "");
  root.scrollTop = top;
}

app.registerExtension({
  name: "CrateUtilsComfy.Triggers",
  async setup() {
    // A graph without an id (older frontends) gets one, saved with the workflow, so keys stay stable.
    if (app.graph && !app.graph.id) {
      app.graph.extra = app.graph.extra || {};
      app.graph.extra.crateutils_comfy_id = app.graph.extra.crateutils_comfy_id || crypto.randomUUID();
    }
    try {
      app.extensionManager?.registerSidebarTab?.({
        id: "crateutils-comfy-triggers", icon: "pi pi-clock", title: "Triggers", tooltip: "CrateUtils-Comfy Trigger Manager",
        type: "custom",
        render: (el) => { panels.add(el); renderPanel(el); poll(); },
      });
    } catch (e) { console.warn("CrateUtils-Comfy: no side panel", e); }
    setInterval(poll, POLL_MS);
    poll();
  },
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!TRIGGERS.has(nodeData.name)) return;
    const created = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = created?.apply(this, arguments);
      this.cuArm = this.addWidget("button", label(this), null, () => toggle(this));
      this.cuFire = this.addWidget("button", "Fire now (test run)", null, () => {
        const st = statusFor(this);
        if (!st) return toast("warn", "CrateUtils-Comfy", "Arm the trigger first");
        fireNow(st.key);
      });
      for (const w of [this.cuArm, this.cuFire]) w.serialize = false;
      setTimeout(refresh, 0);
      return r;
    };
    // Deleting the node does not disarm it: switching or reloading workflows also "removes" nodes.
  },
});
