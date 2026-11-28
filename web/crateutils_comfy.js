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

// ---------------------------------------------------------------- SVG page node
// The browser draws the page (in the node body and, when the node runs, for the output),
// so the picture on the node and the picture in the graph are the same pixels. The
// server only stores what was drawn: see crateutils_comfy/svgcache.py, which owns the
// key format used here, and comfy_glue.py for the three routes.
const SVG_NODE = "CUCSvgPage";
const INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape";
const SVG_POLL_MS = 1500;
const RENDER_DEBOUNCE_MS = 300;
const SERIF_ALIASES = new Set(["serif", "times", "times new roman", "liberation serif", "dejavu serif"]);
let svgTick = 0;

function svgPathOf(node) {
  const raw = String(node.widgets?.find((w) => w.name === "svg_path")?.value || "");
  return raw.split(/\r?\n/).map((s) => s.trim().replace(/^"|"$/g, "")).find(Boolean) || "";
}

function svgValue(node, name, fallback) {
  const w = node.widgets?.find((x) => x.name === name);
  return w && w.value !== undefined ? w.value : fallback;
}

function slugLabel(label) {
  const s = String(label || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return s.slice(0, 24) || "x";
}

function bgKey(bg) {
  return { transparent: "transparent", white: "white", "page colour": "page" }[bg] || "transparent";
}

function svgKey(hash, page, label, width, height, background) {
  const p = Math.max(1, Math.round(Number(page) || 1));
  return `v1_${String(hash).slice(0, 16)}_p${p}_l${slugLabel(label)}_${Math.round(Number(width) || 0)}`
    + `x${Math.round(Number(height) || 0)}_${bgKey(background)}`;
}

async function svgSource(path, statOnly) {
  const q = `?path=${encodeURIComponent(path)}${statOnly ? "&stat=1" : ""}`;
  const r = await api.fetchApi(`/crateutils-comfy/svg/source${q}`);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

async function svgUpload(key, png) {
  const r = await api.fetchApi("/crateutils-comfy/svg/render", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key, png }),
  });
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    throw new Error(j.error || `the render could not be saved (HTTP ${r.status})`);
  }
}

function svgParse(text) {
  const doc = new DOMParser().parseFromString(text, "image/svg+xml");
  if (doc.querySelector("parsererror")) throw new Error("this file is not valid SVG");
  // Inkscape pages are <inkscape:page> elements, NOT <g> wrappers. Layers carry
  // inkscape:label too, so filter on the element name or a layer becomes a "page".
  const pages = [...doc.getElementsByTagNameNS("*", "page")]
    .filter((el) => el.namespaceURI === INKSCAPE_NS && el.hasAttribute("width") && el.hasAttribute("height"))
    .map((el) => ({
      label: el.getAttributeNS(INKSCAPE_NS, "label") || "",
      x: Number(el.getAttribute("x")) || 0, y: Number(el.getAttribute("y")) || 0,
      w: Number(el.getAttribute("width")), h: Number(el.getAttribute("height")),
    }))
    .filter((p) => p.w > 0 && p.h > 0);
  if (!pages.length) {  // a plain SVG with no pages: its own viewBox (or size) is the one page
    const root = doc.documentElement;
    const vb = String(root.getAttribute("viewBox") || "").trim().split(/[\s,]+/).map(Number);
    if (vb.length === 4 && vb[2] > 0 && vb[3] > 0) {
      pages.push({ label: "", x: vb[0], y: vb[1], w: vb[2], h: vb[3] });
    } else {
      const w = parseFloat(root.getAttribute("width")) || 1000;
      const hh = parseFloat(root.getAttribute("height")) || 1000;
      pages.push({ label: "", x: 0, y: 0, w, h: hh });
    }
  }
  const named = doc.getElementsByTagNameNS("*", "namedview")[0];
  return { doc, pages, pageColor: named?.getAttribute("pagecolor") || "" };
}

function svgPage(spec, pages) {
  const label = String(spec.page_label || "").trim();
  if (label) {
    const hit = pages.find((p) => p.label.toLowerCase() === label.toLowerCase());
    if (!hit) throw new Error(`no page labelled "${label}"`);
    return hit;
  }
  const i = Math.min(Math.max(1, Math.round(Number(spec.page) || 1)), pages.length) - 1;
  return pages[i];
}

function withViewBox(text, vb) {
  if (/<svg\b[^>]*\sviewBox="/s.test(text)) {
    return text.replace(/<svg\b([^>]*?)\sviewBox="[^"]*"/s, `<svg$1 viewBox="${vb}"`);
  }
  return text.replace(/<svg\b([^>]*?)(\/?)>/s, `<svg$1 viewBox="${vb}"$2>`);
}

function svgImage(text) {
  const url = URL.createObjectURL(new Blob([text], { type: "image/svg+xml;charset=utf-8" }));
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("the browser could not draw this SVG")); };
    img.src = url;
  });
}

// Width measurement, because document.fonts.check() answers true for fonts that are not
// installed (measured: true for all 25 families tried, 8 of them absent). A family that
// is not installed falls back to the browser's default serif, so "same width as serif"
// is the signal -- except for the fonts that ARE that default, which are allowed below.
let measureCtx = null;
function svgFonts(doc, text) {
  const fams = new Set();
  const add = (v) => String(v || "").split(",").forEach((f) => {
    const n = f.trim().replace(/^["']|["']$/g, "");
    if (n && !["serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui", "inherit"].includes(n.toLowerCase())) {
      fams.add(n);
    }
  });
  for (const el of doc.querySelectorAll("text, tspan, textPath")) {
    add(el.getAttribute("font-family"));
    add(el.style?.fontFamily);
    for (const m of String(el.getAttribute("style") || "").matchAll(/font-family\s*:\s*([^;}]+)/gi)) add(m[1]);
  }
  for (const st of doc.querySelectorAll("style")) {
    for (const m of String(st.textContent || "").matchAll(/font-family\s*:\s*([^;}]+)/gi)) add(m[1]);
  }
  const out = [];
  if (fams.size) {
    measureCtx = measureCtx || document.createElement("canvas").getContext("2d");
    const width = (f) => { measureCtx.font = `72px ${f}`; return measureCtx.measureText("Hamburgefonstiv").width; };
    const serif = width("serif");
    const missing = [...fams].filter((f) => Math.abs(width(`"${f}"`) - serif) < 0.5
      && !SERIF_ALIASES.has(f.toLowerCase()));
    if (missing.length) out.push(`font substituted: ${missing.slice(0, 3).join(", ")}${missing.length > 3 ? "…" : ""}`);
  }
  if (/device-cmyk\(/.test(text)) out.push("CMYK fills may not match Inkscape");
  return out;
}

async function svgDraw(spec, source) {
  const src = source || await svgSource(spec.path);
  const { doc, pages, pageColor } = svgParse(src.text);
  const page = svgPage(spec, pages);
  const wantW = Number(spec.width) || 0;
  const wantH = Number(spec.height) || 0;
  let w = wantW > 0 ? wantW : (wantH > 0 ? Math.round(page.w * wantH / page.h) : Math.round(page.w));
  let h = wantH > 0 ? wantH : (wantW > 0 ? Math.round(page.h * wantW / page.w) : Math.round(page.h));
  w = Math.min(16384, Math.max(1, Math.round(w)));
  h = Math.min(16384, Math.max(1, Math.round(h)));

  const img = await svgImage(withViewBox(src.text, `${page.x} ${page.y} ${page.w} ${page.h}`));
  const canvas = document.createElement("canvas");
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext("2d");
  const bg = bgKey(spec.background);
  if (bg !== "transparent") {   // "page" uses the colour Inkscape shows behind the page
    ctx.fillStyle = bg === "page" ? (pageColor || "#ffffff") : "#ffffff";
    ctx.fillRect(0, 0, w, h);
  } else {
    ctx.clearRect(0, 0, w, h);
  }
  ctx.drawImage(img, 0, 0, w, h);
  return { png: canvas.toDataURL("image/png").split(",")[1], w, h, label: page.label,
           warnings: svgFonts(doc, src.text), hash: src.hash, stat: { size: src.size, mtime_ns: src.mtime_ns } };
}

function svgBar(node, title, warnings, error) {
  const c = node._cuc;
  if (!c) return;
  c.title.textContent = title.length > 160 ? `${title.slice(0, 160)}…` : title;
  c.title.title = title;  // the full message, however long it is
  c.title.style.color = error ? "#e2807a" : "";
  c.warn.textContent = (warnings || []).join(" · ");
  c.warn.title = (warnings || []).join("\n");
}

function svgSchedule(node) {
  const c = node._cuc;
  if (!c) return;
  clearTimeout(c.timer);
  c.timer = setTimeout(() => renderSvgNode(node), RENDER_DEBOUNCE_MS);
}

async function renderSvgNode(node) {
  const c = node._cuc;
  if (!c) return;
  if (c.busy) { c.again = true; return; }
  const path = svgPathOf(node);
  if (!path) {
    svgBar(node, "no file: type the path of an Inkscape .svg", []);
    c.img.removeAttribute("src");
    c.lastKey = "";
    return;
  }
  c.busy = true;
  try {
    const spec = { path, page: svgValue(node, "page", 1), page_label: svgValue(node, "page_label", ""),
      width: svgValue(node, "width", 0), height: svgValue(node, "height", 0),
      background: svgValue(node, "background", "transparent") };
    const r = await svgDraw(spec, await svgSource(path));
    c.img.src = `data:image/png;base64,${r.png}`;
    c.aspect = r.h / r.w;
    c.stat = r.stat;
    svgBar(node, `${r.label || `page ${spec.page}`} · ${r.w}×${r.h}`, r.warnings);
    const key = svgKey(r.hash, spec.page, spec.page_label, spec.width, spec.height, spec.background);
    if (key !== c.lastKey) {
      await svgUpload(key, r.png);   // warm the server cache, so a run finds this exact page
      c.lastKey = key;
    }
    node.setDirtyCanvas?.(true, true);
  } catch (e) {
    svgBar(node, String(e.message || e).slice(0, 200), [], true);
  } finally {
    c.busy = false;
    if (c.again) { c.again = false; svgSchedule(node); }
  }
}

async function pickSvg(node) {
  const c = node._cuc;
  if (!c || c.picking) return;
  if (node.inputs?.find((i) => i.name === "svg_path")?.link != null) {
    return toast("warn", "CrateUtils-Comfy", "svg_path is connected: disconnect it to pick a file");
  }
  c.picking = true;
  const setLabel = (t) => {
    c.browse.name = t;
    c.browse.label = t;
    node.setDirtyCanvas?.(true, true);
  };
  setLabel("Waiting for the file dialog…");
  try {
    const r = await api.fetchApi("/crateutils-comfy/svg/pick", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start_at: svgPathOf(node) }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    if (j.cancelled || !j.path) return;
    if (j.allowed === false) {
      if (!confirm(`${j.folder} is not in CrateUtils-Comfy's allowed folders. Allow it?`)) {
        return toast("info", "CrateUtils-Comfy",
          "Not allowed: add the folder to allowed_folders when you want to use that file");
      }
      const a = await api.fetchApi("/crateutils-comfy/svg/allow", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder: j.folder }),
      });
      const aj = await a.json().catch(() => ({}));
      if (!a.ok) throw new Error(aj.error || `HTTP ${a.status}`);
      toast("success", "CrateUtils-Comfy", `Allowed ${j.folder}`);
    }
    const w = node.widgets?.find((x) => x.name === "svg_path");
    w.value = j.path;
    w.callback?.(j.path);
    node.setDirtyCanvas?.(true, true);
  } catch (e) {
    toast("error", "CrateUtils-Comfy: could not pick a file", String(e.message || e));
  } finally {
    c.picking = false;
    setLabel("Browse for .svg…");
  }
}

function attachSvg(node) {
  const el = document.createElement("div");
  el.style.cssText = "display:flex;flex-direction:column;width:100%;height:100%;overflow:hidden;"
    + "pointer-events:none;gap:2px";
  const title = document.createElement("div");
  title.style.cssText = "font-size:11px;opacity:0.8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis";
  title.textContent = "no file";
  const warn = document.createElement("div");
  warn.style.cssText = "font-size:10px;color:#e8a33d;white-space:nowrap;overflow:hidden;text-overflow:ellipsis";
  const img = document.createElement("img");
  img.style.cssText = "flex:1 1 auto;width:100%;min-height:0;object-fit:contain;display:block";
  img.alt = "";
  el.append(title, warn, img);

  // A normal button widget, so it sits under the inputs and stays out of widgets_values.
  // It opens the Windows Open dialog on the machine running ComfyUI and returns a real
  // path: a browser's own file picker only gives bytes, and then saving in Inkscape
  // would not repaint this node.
  const browse = node.addWidget("button", "Browse for .svg…", null, () => pickSvg(node));
  // Keep the button's slot in widgets_values on every frontend version. 1.51 writes a null
  // placeholder for a widget with serialize = false and 1.55 omits it entirely, so a workflow
  // saved on one instance would shift page / size when opened on the other. options.serialize
  // still keeps it out of the API prompt, so ComfyUI never sees it.
  browse.serialize = true;
  browse.options = browse.options || {};
  browse.options.serialize = false;
  // addWidget appends; move it up so it reads as part of the path box, not as a seventh input.
  const from = node.widgets.indexOf(browse);
  const pathAt = node.widgets.findIndex((w) => w.name === "svg_path");
  if (from > 0 && pathAt >= 0 && from !== pathAt + 1) {
    node.widgets.splice(from, 1);
    node.widgets.splice(pathAt + 1, 0, browse);
  }

  const widget = node.addDOMWidget("svg_preview", "svg_preview", el, {
    serialize: false, afterResize: () => node.setDirtyCanvas?.(true, true),
  });
  widget.serialize = false;          // not in the workflow JSON (svg_path already is)
  widget.options.serialize = false;  // not in the API prompt
  node._cuc = { el, img, title, warn, browse, picking: false, aspect: 0.75, lastKey: "", stat: null,
    busy: false, again: false, timer: 0 };
  widget.computeLayoutSize = () => {
    const width = (node.size?.[0] || 320) - 20;
    return { minHeight: Math.max(120, Math.round(width * (node._cuc?.aspect || 0.75)) + 26), minWidth: 160 };
  };

  for (const name of ["svg_path", "page", "page_label", "width", "height", "background"]) {
    const w = node.widgets?.find((x) => x.name === name);
    if (!w) continue;
    const cb = w.callback;
    w.callback = function (...args) {
      const r = cb?.apply(this, args);
      svgSchedule(node);
      return r;
    };
  }
  // Room for the picture without shrinking the inputs the user needs to reach.
  setTimeout(() => {
    if (node.size[0] < 340 || node.size[1] < 360) node.setSize([Math.max(node.size[0], 340), Math.max(node.size[1], 360)]);
    svgSchedule(node);
  }, 0);
}

async function svgPollPending() {
  const r = await api.fetchApi("/crateutils-comfy/svg/pending");
  if (!r.ok) return;
  const { requests } = await r.json();
  for (const req of requests || []) {
    try {
      const drawn = await svgDraw(req);
      await svgUpload(req.key, drawn.png);
    } catch (e) {
      console.warn("CrateUtils-Comfy: could not draw", req.path, e);
    }
  }
}

function svgNodes() {
  return (app.graph?._nodes || []).filter((n) => n && n.comfyClass === SVG_NODE && n._cuc);
}

async function svgPollSources() {
  for (const node of svgNodes()) {
    const path = svgPathOf(node);
    if (!path) continue;
    try {
      const st = await svgSource(path, true);
      const prev = node._cuc.stat;
      if (!prev || prev.size !== st.size || prev.mtime_ns !== st.mtime_ns) {
        node._cuc.stat = { size: st.size, mtime_ns: st.mtime_ns };
        if (prev) svgSchedule(node);  // changed on disk: repaint (a first look happens in renderSvgNode)
      }
    } catch (_) { /* the file moved while being saved; the next poll will see it */ }
  }
}

app.registerExtension({
  name: "CrateUtilsComfy.SvgPage",
  async setup() {
    setInterval(async () => {
      if (document.hidden) return;
      try { await svgPollPending(); } catch (_) { /* server restarting */ }
      if (svgTick++ % 2) return;
      try { await svgPollSources(); } catch (_) { /* server restarting */ }
    }, SVG_POLL_MS);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) svgPollPending().catch(() => {});
    });
  },
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== SVG_NODE) return;
    const created = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = created?.apply(this, arguments);
      attachSvg(this);
      return r;
    };
    // Widget values are restored before onConfigure runs, so the picture is drawn from
    // the saved path here; doing it in onNodeCreated would leave a reloaded node blank.
    const configured = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const r = configured?.apply(this, arguments);
      setTimeout(() => svgSchedule(this), 0);
      return r;
    };
    const removed = nodeType.prototype.onRemoved;
    nodeType.prototype.onRemoved = function () {
      if (this._cuc) clearTimeout(this._cuc.timer);
      this._cuc = null;
      return removed?.apply(this, arguments);
    };
  },
});
