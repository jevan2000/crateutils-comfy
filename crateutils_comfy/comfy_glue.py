"""ComfyUI side of the trigger manager: queueing into this server, run tracking, notifications,
the path policy and web routes."""

from __future__ import annotations

import hmac
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import notify, paths, triggers

log = logging.getLogger("CrateUtils-Comfy")
_MANAGER: triggers.Manager | None = None
MAX_HOOK_BODY = 1024 * 1024


def user_dir() -> Path:
    import folder_paths

    return Path(folder_paths.get_user_directory()) / "crateutils-comfy"


def config() -> dict:
    f = user_dir() / "config.json"
    if not f.is_file():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"CrateUtils-Comfy config {f} is not valid JSON: {e}") from e


def named_destinations() -> dict[str, str]:
    return {str(k): str(v) for k, v in (config().get("notify") or {}).items()}


def allowed_roots() -> list[Path]:
    import folder_paths

    roots = [Path(folder_paths.get_input_directory()), Path(folder_paths.get_output_directory()),
             Path(folder_paths.get_temp_directory())]
    return roots + paths.config_roots(user_dir() / "config.json")


def check_read(p: str, what: str = "path") -> Path:
    import folder_paths

    return paths.check(p, allowed_roots(), Path(folder_paths.get_input_directory()), what)


def check_write_folder(p: str, what: str = "output folder") -> Path:
    import folder_paths

    return paths.check(p, allowed_roots(), Path(folder_paths.get_output_directory()), what)


def _base_url() -> tuple[str, ssl.SSLContext | None]:
    from server import PromptServer

    srv = PromptServer.instance
    addr = str(getattr(srv, "address", "") or "127.0.0.1").split(",")[0].strip()
    if addr in ("", "0.0.0.0"):
        addr = "127.0.0.1"
    elif addr in ("::", "[::]"):
        addr = "::1"
    if ":" in addr and not addr.startswith("["):
        addr = f"[{addr}]"
    tls = False
    try:
        from comfy.cli_args import args

        tls = bool(args.tls_keyfile and args.tls_certfile)
    except Exception:
        pass
    ctx = None
    if tls:  # our own server; its certificate is often self-signed
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return f"{'https' if tls else 'http'}://{addr}:{srv.port}", ctx


def _get(path: str, timeout: float = 10):
    base, ctx = _base_url()
    with urllib.request.urlopen(base + path, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode())


def queue_prompt(prompt: dict, extra_pnginfo: dict | None) -> str:
    """Queue through this server's own /prompt, so it is validated like any other run. We pick
    the prompt id, so the run can be tracked even if the reply is lost."""
    base, ctx = _base_url()
    pid = str(uuid.uuid4())
    body = {"prompt": prompt, "prompt_id": pid, "client_id": "crateutils-comfy-" + uuid.uuid4().hex[:8], "extra_data": {}}
    if extra_pnginfo:
        body["extra_data"]["extra_pnginfo"] = extra_pnginfo
    req = urllib.request.Request(base + "/prompt", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return json.loads(r.read().decode()).get("prompt_id", "") or pid
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(detail)
            msg = j.get("error", {}).get("message", "") if isinstance(j.get("error"), dict) else str(j.get("error"))
            errs = [f"{nid}: {er.get('errors', [{}])[0].get('message', '')}"
                    for nid, er in (j.get("node_errors") or {}).items()]
            detail = "; ".join([msg] + errs)
        except ValueError:
            pass
        raise RuntimeError(f"ComfyUI refused the workflow: {detail[:500]}") from None


def is_busy(prompt_id: str) -> bool:
    try:
        q = _get("/queue")
    except Exception:
        return False
    for item in q.get("queue_running", []) + q.get("queue_pending", []):
        if len(item) > 1 and item[1] == prompt_id:
            return True
    return False


def history(prompt_id: str) -> dict | None:
    try:
        h = _get("/history/" + urllib.parse.quote(prompt_id))
    except Exception:
        return None
    return h.get(prompt_id) if isinstance(h, dict) else None


def notify_failure(settings: dict, prompt_id: str, error: str) -> None:
    dests = notify.resolve(settings.get("destination", ""), named_destinations())
    msg = (settings.get("failure_message") or "The workflow failed").strip()
    run = settings.get("run")
    if run:
        msg += f"\nTrigger: {run}"
    msg += f"\n{error}"
    for line in notify.send(dests, settings.get("title") or "ComfyUI: run failed", msg, priority="high"):
        log.info("CrateUtils-Comfy failure notification: %s", line)


def manager() -> triggers.Manager:
    global _MANAGER
    if _MANAGER is None:
        d = user_dir()
        _MANAGER = triggers.Manager(d / "triggers.json", queue_prompt, is_busy, check_read,
                                    history_fn=history, notify_fn=notify_failure, log_path=d / "runs.csv")
        n = _MANAGER.load()
        if n:
            log.info("CrateUtils-Comfy: restored %d armed trigger(s)", n)
        _MANAGER.start()
    return _MANAGER


def on_prompt(json_data: dict) -> dict:
    """Track runs whose workflow has a Notify node set to report failures (it can't run itself then)."""
    try:
        prompt = json_data.get("prompt") or {}
        notifies = triggers.failure_notifies(prompt)
        if notifies:
            if not json_data.get("prompt_id"):
                json_data["prompt_id"] = str(uuid.uuid4())
            manager().track_external(json_data["prompt_id"], notifies)
    except Exception as e:  # never block a prompt over this
        log.warning("CrateUtils-Comfy: on_prompt: %s", e)
    return json_data


def add_routes() -> None:
    from aiohttp import web
    from server import PromptServer

    srv = PromptServer.instance
    r = srv.routes
    try:
        srv.add_on_prompt_handler(on_prompt)
    except Exception as e:
        log.warning("CrateUtils-Comfy: failure notifications unavailable: %s", e)

    def err(e, status=400):
        return web.json_response({"error": str(e)}, status=status)

    @r.get("/crateutils-comfy/triggers")
    async def list_triggers(request):
        return web.json_response({"triggers": manager().list()})

    @r.post("/crateutils-comfy/triggers")
    async def arm(request):
        try:
            d = await request.json()
            prompt = d["prompt"]
            extra = d.get("extra_pnginfo")
            if extra is None and d.get("workflow") is not None:
                extra = {"workflow": d["workflow"]}
            await _validate(prompt)
            st = manager().arm(str(d["key"]), str(d["node_id"]), prompt, extra, str(d.get("local_id", "")),
                               str(d.get("graph_id", "")), str(d.get("title", "")))
            return web.json_response(st)
        except Exception as e:
            return err(e)

    @r.post("/crateutils-comfy/triggers/disarm")
    async def disarm(request):
        d = await request.json()
        if d.get("all"):
            return web.json_response({"disarmed": manager().disarm_all()})
        return web.json_response({"disarmed": int(manager().disarm(str(d.get("key", ""))))})

    @r.post("/crateutils-comfy/triggers/pause")
    async def pause(request):
        d = await request.json()
        ok = manager().pause(str(d.get("key", "")), bool(d.get("paused", True)))
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    @r.post("/crateutils-comfy/triggers/fire")
    async def fire(request):
        d = await request.json()
        ok = manager().request_fire(str(d.get("key", "")))
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    @r.get("/crateutils-comfy/runs.csv")
    async def runs_csv(request):
        f = user_dir() / "runs.csv"
        if not f.is_file():
            return web.Response(text=",".join(triggers.LOG_COLUMNS) + "\n", content_type="text/csv")
        return web.FileResponse(f, headers={"Content-Disposition": 'attachment; filename="crateutils-comfy-runs.csv"'})

    async def hook(request):
        t = manager().find_hook(request.match_info["hook_id"])
        if t is None:
            return err("unknown webhook (is it armed?)", 404)
        auth = request.headers.get("Authorization", "")
        token = (request.headers.get("X-CrateUtils-Comfy-Token") or request.query.get("token")
                 or (auth[7:] if auth.lower().startswith("bearer ") else ""))
        if not token or not hmac.compare_digest(token, t.state.get("token", "")):
            return err("wrong or missing token", 403)
        if t.state.get("paused"):
            return err("this webhook is paused", 409)
        if t.state.get("done"):
            return err("this webhook has used all its runs", 410)
        if (request.content_length or 0) > MAX_HOOK_BODY:
            return err("body too large (1 MB max)", 413)
        payload: dict = {k: v for k, v in request.query.items() if k != "token"}
        raw = await request.read() if request.can_read_body else b""
        if len(raw) > MAX_HOOK_BODY:
            return err("body too large (1 MB max)", 413)
        if raw:
            ctype = request.content_type or ""
            text = raw.decode("utf-8", "replace")
            if "json" in ctype or text.lstrip().startswith(("{", "[")):
                try:
                    body = json.loads(text)
                    payload.update(body if isinstance(body, dict) else {"items": body})
                except ValueError:
                    payload["text"] = text
            elif "form" in ctype:
                payload.update({k: v for k, v in urllib.parse.parse_qsl(text)})
            else:
                payload["text"] = text
        try:
            manager().request_fire(t.key, {"event": "webhook", "paths": [], "payload": payload})
        except OverflowError as e:
            return err(e, 429)
        return web.json_response({"queued": True, "trigger": t.title or t.key}, status=202)

    r.get("/crateutils-comfy/hook/{hook_id}")(hook)
    r.post("/crateutils-comfy/hook/{hook_id}")(hook)


async def _validate(prompt: dict) -> None:
    """Refuse to arm a workflow ComfyUI would refuse to run, so mistakes show at once."""
    import execution

    try:
        res = await execution.validate_prompt(str(uuid.uuid4()), prompt, None)
    except TypeError:  # older signature
        res = await execution.validate_prompt(prompt)
    if not res[0]:
        e = res[1] if isinstance(res[1], dict) else {}
        msgs = [e.get("message", "invalid workflow"), str(e.get("details", "") or "")]
        for nid, ne in (res[3] or {}).items():
            for x in ne.get("errors", [])[:2]:
                msgs.append(f"node {nid} ({ne.get('class_type', '')}): {x.get('message', '')} {x.get('details', '')}")
        raise ValueError("; ".join(m for m in msgs if m.strip()))
