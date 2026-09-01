from flask import Flask, request, jsonify, Response, make_response, send_from_directory
from flask_cors import CORS
import threading
import time
import json
import os
import re
import subprocess
import sys
import hashlib
import secrets
import logging
import warnings
from pathlib import Path
import requests
import websocket
import queue

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
PORT = 5000
TOOL_VERSION = "1.0.0"
FIVEM_DEVTOOLS_HOST = "localhost"
FIVEM_DEVTOOLS_PORT = 13172
FIVEM_DEVTOOLS_URL = f"http://{FIVEM_DEVTOOLS_HOST}:{FIVEM_DEVTOOLS_PORT}/json"
USE_WEBVIEW = True

CORS(
    app,
    resources={
        r"/*": {
            "origins": [
                f"http://127.0.0.1:{PORT}",
                f"http://localhost:{PORT}",
            ],
            "supports_credentials": True,
        }
    },
)

_SUBPROCESS_FLAGS = 0
if sys.platform == "win32":
    _SUBPROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

notify_queue = queue.Queue()
http_session = requests.Session()
http_session.headers.update({"Connection": "keep-alive"})

panel_state = {
    "fivem_connected": False,
    "bypass_active": False,
    "bypass_message": "Waiting for FiveM…",
    "last_probe": 0.0,
}
panel_state_lock = threading.Lock()

_notify_lock = threading.Lock()
_notify_last: dict[str, float] = {}

_flask_boot_error: BaseException | None = None

def run_hidden(cmd, **kwargs):
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs.setdefault("startupinfo", startupinfo)
    kwargs.setdefault("creationflags", _SUBPROCESS_FLAGS)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("errors", "ignore")
    return subprocess.run(cmd, **kwargs)


def install_hidden_subprocess() -> None:
    """Hide CMD windows from subprocess.run/Popen (e.g. event_blocker tasklist/netstat)."""
    if sys.platform != "win32":
        return
    if getattr(subprocess, "_arya_hidden_patched", False):
        return

    _real_run = subprocess.run
    _real_popen = subprocess.Popen

    def _hidden_run(*args, **kwargs):
        if "startupinfo" not in kwargs:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo
        kwargs.setdefault("creationflags", _SUBPROCESS_FLAGS)
        return _real_run(*args, **kwargs)

    def _hidden_popen(*args, **kwargs):
        if "startupinfo" not in kwargs:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
            kwargs["startupinfo"] = startupinfo
        kwargs.setdefault("creationflags", _SUBPROCESS_FLAGS)
        return _real_popen(*args, **kwargs)

    subprocess.run = _hidden_run  # type: ignore[method-assign]
    subprocess.Popen = _hidden_popen  # type: ignore[method-assign]
    subprocess._arya_hidden_patched = True  # type: ignore[attr-defined]


install_hidden_subprocess()


def free_local_port(port: int) -> None:
    """Stop stale tool instances still listening on our port."""
    if sys.platform != "win32":
        return
    my_pid = os.getpid()
    suffix = f":{port}"
    targets: set[int] = set()
    try:
        r = run_hidden(["netstat", "-ano"], timeout=8)
    except OSError:
        return
    for line in r.stdout.splitlines():
        upper = line.upper()
        if "LISTENING" not in upper or suffix not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid in (my_pid, 0):
            continue
        targets.add(pid)
    for pid in targets:
        try:
            run_hidden(["taskkill", "/F", "/PID", str(pid)], timeout=5)
        except OSError:
            pass
    if targets:
        time.sleep(0.5)


def suppress_console_windows() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
            ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass


def ensure_gui_process() -> None:
    """Stay in this Python process; only hide the console on Windows."""
    if sys.platform != "win32":
        return
    os.environ["ARYA_GUI_MODE"] = "1"
    suppress_console_windows()


def notify(msg: str, *, key: str | None = None, cooldown: float = 0.0) -> None:
    """Queue a panel notification; optional per-key cooldown reduces spam."""
    if key and cooldown > 0:
        now = time.monotonic()
        with _notify_lock:
            last = _notify_last.get(key, 0.0)
            if (now - last) < cooldown:
                return
            _notify_last[key] = now
    notify_queue.put(msg)

class TabCache:
    def __init__(self, ttl=0.08):
        self._ttl = ttl
        self._tabs = []
        self._ts = 0.0
        self._lock = threading.Lock()

    def get_tabs(self, force=False):
        with self._lock:
            now = time.monotonic()
            if not force and self._tabs and (now - self._ts) < self._ttl:
                return list(self._tabs)
            try:
                r = http_session.get(FIVEM_DEVTOOLS_URL, timeout=0.45)
                r.raise_for_status()
                self._tabs = r.json()
                self._ts = now
            except Exception:
                if not self._tabs:
                    return []
            return list(self._tabs)

    def invalidate(self):
        with self._lock:
            self._ts = 0.0


tab_cache = TabCache()

WS_CONNECT_TIMEOUT = 6
ROOT_UI_URL = "nui://game/ui/root.html"


def is_root_tab(tab: dict | None) -> bool:
    if not tab:
        return False
    url = str(tab.get("url") or "")
    return tab.get("title") == "CitizenFX root UI" or url == ROOT_UI_URL


def find_root_tab(tabs):
    for tab in tabs or []:
        if is_root_tab(tab):
            return tab
    return None


def is_fivem_devtools_up() -> bool:
    try:
        r = http_session.get(FIVEM_DEVTOOLS_URL, timeout=0.35)
        r.raise_for_status()
        return True
    except Exception:
        return False


def is_devtools_ready() -> bool:
    return find_root_tab(tab_cache.get_tabs()) is not None


def wrap_iframe_injection(js_code: str) -> str:
    code_literal = json.dumps(js_code)
    return f"""(function waitForIframe() {{
  const ifr = top.document.querySelector("iframe");
  if (!ifr) {{
    return setTimeout(waitForIframe, 250);
  }}
  ifr.contentWindow.eval({code_literal});
}})();"""

class InjectionPool:
    def __init__(self):
        self._ws = None
        self._ws_url = None
        self._direct: dict[str, object] = {}
        self._lock = threading.Lock()

    def _close(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None
        self._ws_url = None

    def _close_direct(self, ws_url: str | None = None):
        if ws_url:
            ws = self._direct.pop(ws_url, None)
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass
            return
        for ws in self._direct.values():
            try:
                ws.close()
            except Exception:
                pass
        self._direct.clear()

    def _alive(self):
        return bool(self._ws and getattr(self._ws, "connected", False))

    def _connect(self, ws_url):
        self._close()
        ws = websocket.create_connection(ws_url, timeout=WS_CONNECT_TIMEOUT)
        self._ws = ws
        self._ws_url = ws_url
        return ws

    def _resolve_target(self):
        tabs = tab_cache.get_tabs(force=True)
        if not tabs:
            return None, "Open FiveM first."
        target = find_root_tab(tabs)
        if not target:
            return None, "Game root UI not ready yet."

        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            return None, "FiveM not ready yet."
        return ws_url, None

    def _get_ws(self):
        ws_url, err = self._resolve_target()
        if err:
            return None, err

        if self._alive() and self._ws_url == ws_url:
            return self._ws, None

        try:
            return self._connect(ws_url), None
        except Exception as first_err:
            tab_cache.invalidate()
            ws_url, err = self._resolve_target()
            if err:
                return None, err
            try:
                return self._connect(ws_url), None
            except Exception:
                self._close()
                return None, f"Connection failed ({first_err})."

    def _get_direct_ws(self, ws_url: str):
        ws = self._direct.get(ws_url)
        if ws and getattr(ws, "connected", False):
            return ws, None
        self._close_direct(ws_url)
        try:
            ws = websocket.create_connection(ws_url, timeout=WS_CONNECT_TIMEOUT)
            self._direct[ws_url] = ws
            return ws, None
        except Exception as e:
            return None, str(e)

    def _evaluate_on(self, ws, js_code, *, fast: bool = False):
        msg_id = int(time.time() * 1000) % 1_000_000
        params: dict = {
            "expression": js_code,
            "userGesture": True,
        }
        if fast:
            params["returnByValue"] = False
        else:
            params["returnByValue"] = True
            params["awaitPromise"] = True
        payload = {
            "id": msg_id,
            "method": "Runtime.evaluate",
            "params": params,
        }
        ws.send(json.dumps(payload, separators=(",", ":")))
        ws.settimeout(2.5 if fast else 5)
        while True:
            data = json.loads(ws.recv())
            if data.get("id") == msg_id:
                if "error" in data:
                    return False, data["error"].get("message", "Evaluation failed")
                result = data.get("result", {})
                if result.get("exceptionDetails"):
                    details = result["exceptionDetails"]
                    text = details.get("text") or "Script error"
                    exc = details.get("exception") or {}
                    desc = exc.get("description") or exc.get("value")
                    return False, desc or text
                return True, result

    def _evaluate(self, ws, js_code):
        return self._evaluate_on(ws, js_code, fast=False)

    def inject(self, js_code, *, ws_url=None, kind="root", iframe_index=0, fast=False):
        if kind == "root" or not ws_url:
            wrapped_code = wrap_iframe_injection(js_code)
            with self._lock:
                ws, err = self._get_ws()
                if not ws:
                    return False, err
                try:
                    return self._evaluate_on(ws, wrapped_code, fast=fast)
                except Exception:
                    self._close()
                    ws, err = self._get_ws()
                    if not ws:
                        return False, err or "Connection lost."
                    try:
                        return self._evaluate_on(ws, wrapped_code, fast=fast)
                    except Exception as e:
                        self._close()
                        return False, f"Failed to run script: {e}"

        if kind == "iframe":
            root_ws, err = self._resolve_target()
            if err:
                return False, err
            wrapped_code = wrap_iframe_index_injection(iframe_index, js_code)
            with self._lock:
                ws, err = self._get_direct_ws(root_ws)
                if not ws:
                    return False, err
                try:
                    return self._evaluate_on(ws, wrapped_code, fast=fast)
                except Exception:
                    self._close_direct(root_ws)
                    ws, err = self._get_direct_ws(root_ws)
                    if not ws:
                        return False, err or "Connection lost."
                    try:
                        return self._evaluate_on(ws, wrapped_code, fast=fast)
                    except Exception as e:
                        self._close_direct(root_ws)
                        return False, f"Failed to run script: {e}"

        with self._lock:
            ws, err = self._get_direct_ws(ws_url)
            if not ws:
                return False, err
            try:
                return self._evaluate_on(ws, js_code, fast=fast)
            except Exception:
                self._close_direct(ws_url)
                ws, err = self._get_direct_ws(ws_url)
                if not ws:
                    return False, err or "Connection lost."
                try:
                    return self._evaluate_on(ws, js_code, fast=fast)
                except Exception as e:
                    self._close_direct(ws_url)
                    return False, f"Failed to run script: {e}"


injection_pool = InjectionPool()

NUI_DUMP_INNER_SCRIPT = """(function(){
  function fetchScriptSync(url){
    try{
      var x=new XMLHttpRequest();
      x.open('GET',url,false);
      x.send(null);
      if(x.status>=200&&x.status<300) return x.responseText||'';
      return '// HTTP '+x.status;
    }catch(e){
      return '// fetch failed: '+(e&&e.message?e.message:e);
    }
  }
  var scripts=Array.from(document.querySelectorAll('script'));
  var parts=[];
  for(var i=0;i<scripts.length;i++){
    var s=scripts[i];
    if(s.src){
      parts.push('// ['+i+'] '+s.src+'\\n'+fetchScriptSync(s.src));
    }else if(s.textContent&&s.textContent.trim()){
      parts.push('// ['+i+'] inline\\n'+s.textContent.trim());
    }
  }
  return {html:document.documentElement.outerHTML,js:parts.join('\\n\\n')||'// no scripts found'};
})()"""

DUMP_TAB_SCRIPT = """(async function(){
  async function readScript(s,i){
    if(s.src){
      try{
        var r=await fetch(s.src);
        var t=await r.text();
        return '// ['+i+'] '+s.src+'\\n'+t;
      }catch(e){
        try{
          var x=new XMLHttpRequest();
          x.open('GET',s.src,false);
          x.send(null);
          if(x.status>=200&&x.status<300) return '// ['+i+'] '+s.src+'\\n'+(x.responseText||'');
          return '// ['+i+'] '+s.src+'\\n// HTTP '+x.status;
        }catch(err){
          return '// ['+i+'] '+s.src+'\\n// fetch failed: '+(err&&err.message?err.message:err);
        }
      }
    }
    if(s.textContent&&s.textContent.trim()) return '// ['+i+'] inline\\n'+s.textContent.trim();
    return '';
  }
  var scripts=Array.from(document.querySelectorAll('script'));
  var parts=[];
  for(var i=0;i<scripts.length;i++){
    var block=await readScript(scripts[i],i);
    if(block) parts.push(block);
  }
  return {html:document.documentElement.outerHTML,js:parts.join('\\n\\n')||'// no scripts found'};
})()"""

IFRAME_NUI_LIST_SCRIPT = """(function(){
  return Array.from(document.querySelectorAll('iframe')).filter(function(f){
    var s=(f.src||f.getAttribute('src')||'').toLowerCase();
    return s.indexOf('nui')>=0;
  }).map(function(f,i){
    return {index:i,src:f.src||f.getAttribute('src')||'',id:f.id||'',title:f.title||'NUI iframe '+i};
  });
})()"""

IFRAME_DUMP_SCRIPT_TEMPLATE = """(function(){
  var frames=Array.from(document.querySelectorAll('iframe')).filter(function(f){
    var s=(f.src||f.getAttribute('src')||'').toLowerCase();
    return s.indexOf('nui')>=0;
  });
  var win=frames[__IFRAME_INDEX__]&&frames[__IFRAME_INDEX__].contentWindow;
  if(!win) return {error:'Iframe not ready'};
  try{
    return win.eval(__INNER_DUMP__);
  }catch(e){
    return {error:String(e&&(e.message||e))};
  }
})()"""


def iframe_dump_script(index: int) -> str:
    inner = json.dumps(NUI_DUMP_INNER_SCRIPT)
    return (
        IFRAME_DUMP_SCRIPT_TEMPLATE.replace("__IFRAME_INDEX__", str(int(index))).replace(
            "__INNER_DUMP__", inner
        )
    )


def wrap_iframe_index_injection(index: int, js_code: str) -> str:
    code_literal = json.dumps(js_code)
    return f"""(function(){{
  var frames=Array.from(top.document.querySelectorAll('iframe')).filter(function(f){{
    var s=(f.src||f.getAttribute('src')||'').toLowerCase();
    return s.indexOf('nui')>=0;
  }});
  var ifr=frames[{index}];
  if(!ifr||!ifr.contentWindow) throw new Error('NUI iframe not ready');
  ifr.contentWindow.eval({code_literal});
}})();"""


def evaluate_on_tab(ws_url: str, js_code: str, timeout: float = 8):
    ws = None
    try:
        ws = websocket.create_connection(ws_url, timeout=WS_CONNECT_TIMEOUT)
        msg_id = int(time.time() * 1000) % 1_000_000
        payload = {
            "id": msg_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": js_code,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
        }
        ws.send(json.dumps(payload, separators=(",", ":")))
        ws.settimeout(timeout)
        while True:
            data = json.loads(ws.recv())
            if data.get("id") != msg_id:
                continue
            if "error" in data:
                return False, data["error"].get("message", "Evaluation failed")
            result = data.get("result", {})
            if result.get("exceptionDetails"):
                details = result["exceptionDetails"]
                text = details.get("text") or "Script error"
                exc = details.get("exception") or {}
                desc = exc.get("description") or exc.get("value")
                return False, desc or text
            val = result.get("result", {}).get("value")
            return True, val if val is not None else result
    except Exception as e:
        return False, str(e)
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass


def tab_entry_id(tab: dict) -> str:
    ws = tab.get("webSocketDebuggerUrl") or ""
    url = str(tab.get("url") or "")
    return hashlib.sha1(f"{ws}|{url}".encode()).hexdigest()[:12]


def collect_nui_tabs():
    tabs = tab_cache.get_tabs(force=True) or []
    entries = []
    seen = set()

    for tab in tabs:
        url = str(tab.get("url") or "")
        if "nui" not in url.lower():
            continue
        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url or ws_url in seen:
            continue
        seen.add(ws_url)
        entries.append({
            "id": tab_entry_id(tab),
            "title": tab.get("title") or "NUI Tab",
            "url": url,
            "ws_url": ws_url,
            "kind": "devtools",
        })

    root = find_root_tab(tabs)
    root_ws = root.get("webSocketDebuggerUrl") if root else None
    if root_ws:
        ok, payload = evaluate_on_tab(root_ws, IFRAME_NUI_LIST_SCRIPT)
        if ok and isinstance(payload, list):
            for row in payload:
                src = str(row.get("src") or "")
                if "nui" not in src.lower():
                    continue
                idx = int(row.get("index", 0))
                entry_id = f"iframe-{idx}-{hashlib.sha1(src.encode()).hexdigest()[:8]}"
                entries.append({
                    "id": entry_id,
                    "title": row.get("title") or row.get("id") or f"NUI iframe {idx}",
                    "url": src,
                    "ws_url": root_ws,
                    "kind": "iframe",
                    "iframe_index": idx,
                })

    return entries


DUMP_EVAL_TIMEOUT = 45


def dump_nui_target(*, ws_url: str, kind: str = "devtools", iframe_index: int = 0):
    if not ws_url:
        return False, "Missing target"
    if kind == "iframe":
        script = iframe_dump_script(iframe_index)
        ok, payload = evaluate_on_tab(ws_url, script, timeout=DUMP_EVAL_TIMEOUT)
    else:
        ok, payload = evaluate_on_tab(ws_url, DUMP_TAB_SCRIPT, timeout=DUMP_EVAL_TIMEOUT)
    if not ok:
        return False, payload
    if isinstance(payload, dict) and payload.get("error"):
        return False, payload["error"]
    if not isinstance(payload, dict):
        return False, "Unexpected dump response"
    html = payload.get("html") or ""
    js = payload.get("js") or ""
    if not isinstance(html, str):
        html = str(html)
    if not isinstance(js, str):
        js = str(js)
    max_chars = 1_500_000
    if len(html) > max_chars:
        html = html[:max_chars] + "\n\n/* truncated */"
    if len(js) > max_chars:
        js = js[:max_chars] + "\n\n// truncated"
    return True, {"html": html, "js": js}


def set_panel_state(**kwargs):
    with panel_state_lock:
        panel_state.update(kwargs)


_bootstrap_lock = threading.Lock()
_bootstrap = {
    "ready": True,
    "phase": "fivem",
    "message": "Starting…",
    "detail": "Preparing tool…",
    "progress": 0,
    "steps": [],
    "started_at": time.monotonic(),
}

BOOT_STEPS = (
    ("fivem", "FiveM running"),
    ("fivem_connected", "FiveM connected"),
    ("server", "Join server"),
    ("server_connected", "Server connected"),
    ("ready", "Ready"),
)
PHASE_ORDER = [step[0] for step in BOOT_STEPS]
PHASE_PROGRESS = {
    "fivem": 8,
    "fivem_connected": 28,
    "server": 52,
    "server_connected": 78,
    "ready": 100,
}


def refresh_bootstrap_state() -> None:
    phase, message, ready = compute_connection_state()
    detail = bootstrap_detail(phase)
    set_bootstrap(phase, message, ready=ready, detail=detail)


def set_bootstrap(phase: str, message: str, *, ready: bool | None = None, detail: str = "") -> None:
    phase_index = PHASE_ORDER.index(phase) if phase in PHASE_ORDER else 0
    steps = []
    for index, (step_id, label) in enumerate(BOOT_STEPS):
        if ready and step_id == "ready":
            status = "done"
        elif index < phase_index:
            status = "done"
        elif step_id == phase and not ready:
            status = "active"
        elif step_id == phase and ready:
            status = "done"
        else:
            status = "pending"
        steps.append({"id": step_id, "label": label, "status": status})

    with _bootstrap_lock:
        same_core = (
            _bootstrap.get("phase") == phase
            and _bootstrap.get("message") == message
            and _bootstrap.get("detail") == detail
            and _bootstrap.get("steps") == steps
            and (ready is None or _bootstrap.get("ready") == ready)
        )
        if same_core:
            return

        _bootstrap["phase"] = phase
        _bootstrap["message"] = message
        _bootstrap["detail"] = detail
        _bootstrap["progress"] = PHASE_PROGRESS.get(phase, 0)
        _bootstrap["steps"] = steps
        if ready is not None:
            _bootstrap["ready"] = ready


def bootstrap_detail(phase: str) -> str:
    static = {
        "fivem": "Open FiveM to continue",
        "fivem_connected": "Local devtools online",
        "server": "Join a server in FiveM",
        "server_connected": "Game UI connected",
        "ready": "All checks passed",
    }
    return static.get(phase, "")


_fivem_connected_at = 0.0
_server_connected_at = 0.0


def compute_connection_state() -> tuple[str, str, bool]:
    global _fivem_connected_at, _server_connected_at

    if not is_fivem_devtools_up():
        _fivem_connected_at = 0.0
        _server_connected_at = 0.0
        return "fivem", "Waiting for FiveM…", True

    if _fivem_connected_at <= 0.0:
        _fivem_connected_at = time.monotonic()

    if not is_devtools_ready():
        _server_connected_at = 0.0
        return "server", "Waiting for server…", True

    if _server_connected_at <= 0.0:
        _server_connected_at = time.monotonic()

    return "ready", "Ready", True


def bootstrap_worker() -> None:
    refresh_bootstrap_state()
    while True:
        try:
            phase, message, ready = compute_connection_state()
            detail = bootstrap_detail(phase)
            set_bootstrap(phase, message, ready=ready, detail=detail)

            fivem_up = is_fivem_devtools_up()
            with panel_state_lock:
                bypass_active = panel_state.get("bypass_active", False)

            if not bypass_active:
                set_panel_state(
                    fivem_connected=fivem_up,
                    bypass_message=message if not ready else "Ready",
                )
        except Exception:
            pass
        time.sleep(0.25)


def session_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AryaJSTool"
    base.mkdir(parents=True, exist_ok=True)
    return base


HIDDEN_BLOCK_PATTERNS = ("*script-obfuscated.js",)


def _cdp_url_pattern(pattern: str) -> str:
    pat = str(pattern or "").strip()
    if not pat:
        return pat
    if "*" not in pat:
        return f"*{pat}*"
    return pat


def _url_host_path(url: str) -> str:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(str(url or "").strip())
        host = parsed.hostname or ""
        path = parsed.path or ""
        return f"{host}{path}"
    except Exception:
        text = str(url or "").strip()
        for prefix in ("https://", "http://"):
            if text.lower().startswith(prefix):
                text = text[len(prefix):]
        return text.split("?", 1)[0]


def _url_matches_pattern(url: str, pattern: str) -> bool:
    if not url or not pattern:
        return False
    from fnmatch import fnmatch

    url_full = str(url).strip()
    target = url_full.split("?", 1)[0]
    pat = str(pattern).strip()
    if not pat:
        return False

    host_path = _url_host_path(url_full)
    pat_low = pat.lower()
    url_low = url_full.lower()
    host_low = host_path.lower()

    if pat_low in url_low or pat_low in host_low:
        return True

    for candidate in {pat, _cdp_url_pattern(pat)}:
        if fnmatch(url_full, candidate) or fnmatch(target, candidate):
            return True
        if fnmatch(url_low, candidate.lower()) or fnmatch(target.lower(), candidate.lower()):
            return True

    stripped = pat_low.lstrip("*").rstrip("*")
    if stripped and (stripped in url_low or stripped in host_low):
        return True
    return False


def _is_blocked_network_failure(params: dict) -> bool:
    err = str(params.get("errorText") or "").upper()
    reason = str(params.get("blockedReason") or "").lower()
    canceled = params.get("canceled")
    if "ERR_BLOCKED_BY_CLIENT" in err or "BLOCKED_BY_CLIENT" in err:
        return True
    if "BLOCKED" in err and ("CLIENT" in err or "INSPECTOR" in err or "CANCELED" in err):
        return True
    if reason in (
        "inspector",
        "content_blocking",
        "blockedbyclient",
        "blocked_by_client",
        "csp",
        "other",
    ):
        return True
    if canceled and ("BLOCKED" in err or reason == "inspector"):
        return True
    return False


class BlockerManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules: dict[str, int] = {}
        self._request_urls: dict[str, str] = {}
        self._recent_blocked: dict[str, float] = {}
        self._ws_send = None
        self._load()

    def _store_path(self) -> Path:
        return session_dir() / "blocked-urls.json"

    def _load(self) -> None:
        path = self._store_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                rules = data.get("rules") if isinstance(data, dict) else None
                if isinstance(rules, dict):
                    self._rules = {
                        str(k): int(v or 0)
                        for k, v in rules.items()
                        if str(k).strip() and str(k) not in HIDDEN_BLOCK_PATTERNS
                    }
        except Exception:
            self._rules = {}

    def _save(self) -> None:
        try:
            self._store_path().write_text(
                json.dumps({"rules": self._rules}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def list_rules(self) -> list[dict]:
        with self._lock:
            return [
                {"pattern": pattern, "count": int(count)}
                for pattern, count in sorted(self._rules.items(), key=lambda x: (-x[1], x[0]))
            ]

    def total_blocked(self) -> int:
        with self._lock:
            return int(sum(self._rules.values()))

    def all_cdp_patterns(self) -> list[str]:
        with self._lock:
            return [_cdp_url_pattern(p) for p in HIDDEN_BLOCK_PATTERNS] + [
                _cdp_url_pattern(p) for p in self._rules.keys()
            ]

    def add_rule(self, pattern: str) -> tuple[bool, str]:
        clean = str(pattern or "").strip()
        if not clean:
            return False, "Pattern required."
        if clean in HIDDEN_BLOCK_PATTERNS:
            return False, "Pattern already active."
        with self._lock:
            if clean not in self._rules:
                self._rules[clean] = 0
            self._save()
        self.apply_all()
        return True, clean

    def remove_rule(self, pattern: str) -> bool:
        clean = str(pattern or "").strip()
        with self._lock:
            if clean not in self._rules:
                return False
            del self._rules[clean]
            self._save()
        self.apply_all()
        return True

    def register_sender(self, send_fn) -> None:
        with self._lock:
            self._ws_send = send_fn
        self.apply_all()

    def clear_sender(self) -> None:
        with self._lock:
            self._ws_send = None

    def apply_all(self) -> None:
        with self._lock:
            sender = self._ws_send
            patterns = [_cdp_url_pattern(p) for p in HIDDEN_BLOCK_PATTERNS] + [
                _cdp_url_pattern(p) for p in self._rules.keys()
            ]
        if not sender:
            return
        try:
            sender("Network.setBlockedURLs", {"urls": patterns})
        except Exception:
            pass

    def note_blocked(self, url: str, request_id: str | None = None) -> None:
        if not url:
            return
        now = time.monotonic()
        dedupe_key = str(request_id or url)
        updated = False
        with self._lock:
            last = self._recent_blocked.get(dedupe_key)
            if last and now - last < 1.0:
                return
            self._recent_blocked[dedupe_key] = now
            if len(self._recent_blocked) > 5000:
                cutoff = now - 2.0
                self._recent_blocked = {
                    key: ts for key, ts in self._recent_blocked.items() if ts >= cutoff
                }

            for pattern in self._rules:
                if _url_matches_pattern(url, pattern):
                    self._rules[pattern] += 1
                    self._save()
                    updated = True
                    break
        if updated:
            notify(
                json.dumps({"type": "blocker_update"}),
                key="blocker_update",
                cooldown=0.2,
            )

    def handle_cdp_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if not isinstance(msg, dict):
            return

        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Network.requestWillBeSent":
            rid = params.get("requestId")
            request = params.get("request") or {}
            url = request.get("url")
            if rid and url:
                with self._lock:
                    self._request_urls[str(rid)] = str(url)
                    if len(self._request_urls) > 4000:
                        self._request_urls.pop(next(iter(self._request_urls)))
            return

        if method != "Network.loadingFailed":
            return

        err = str(params.get("errorText") or "")
        if not _is_blocked_network_failure(params):
            return

        rid = params.get("requestId")
        with self._lock:
            url = self._request_urls.pop(str(rid), None) if rid else None
        if url:
            self.note_blocked(url, str(rid) if rid else None)


blocker_manager = BlockerManager()


class ExecutorScriptsStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scripts: dict[str, dict] = {}
        self._load()

    def _path(self) -> Path:
        return session_dir() / "executor-scripts.json"

    def _load(self) -> None:
        try:
            raw = json.loads(self._path().read_text(encoding="utf-8"))
            scripts = raw.get("scripts") if isinstance(raw, dict) else None
            if not isinstance(scripts, list):
                return
            for item in scripts:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                sid = str(item["id"])
                self._scripts[sid] = {
                    "id": sid,
                    "name": str(item.get("name") or "Untitled")[:80],
                    "code": str(item.get("code") or ""),
                    "updated_at": float(item.get("updated_at") or 0),
                }
        except Exception:
            self._scripts = {}

    def _save(self) -> None:
        try:
            scripts = sorted(
                self._scripts.values(),
                key=lambda row: row.get("updated_at", 0),
                reverse=True,
            )
            self._path().write_text(
                json.dumps({"scripts": scripts}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def list_scripts(self) -> list[dict]:
        with self._lock:
            rows = []
            for row in self._scripts.values():
                code = row.get("code") or ""
                rows.append({
                    "id": row["id"],
                    "name": row.get("name") or "Untitled",
                    "updatedAt": row.get("updated_at", 0),
                    "chars": len(code),
                })
            rows.sort(key=lambda item: item.get("updatedAt") or 0, reverse=True)
            return rows

    def get_script(self, script_id: str) -> dict | None:
        with self._lock:
            row = self._scripts.get(str(script_id))
            return dict(row) if row else None

    def save_script(self, name: str, code: str, script_id: str | None = None) -> dict:
        clean_name = str(name or "").strip() or "Untitled"
        now = time.time()
        with self._lock:
            sid = str(script_id or "").strip() or secrets.token_urlsafe(9)
            self._scripts[sid] = {
                "id": sid,
                "name": clean_name[:80],
                "code": str(code),
                "updated_at": now,
            }
            self._save()
            return dict(self._scripts[sid])

    def delete_script(self, script_id: str) -> bool:
        with self._lock:
            if str(script_id) not in self._scripts:
                return False
            del self._scripts[str(script_id)]
            self._save()
            return True


executor_scripts = ExecutorScriptsStore()


PANEL_HTML = r"""<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Arya FiveM Tool</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/fold/foldgutter.min.css">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/dialog/dialog.min.css">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
  <style>
    :root {
      --bg: #08090a;
      --bg-elevated: #111214;
      --sidebar: #0b0c0e;
      --surface: #16181c;
      --surface-hover: #1c1e24;
      --surface-active: #22252b;
      --border: #2a2d35;
      --border-subtle: #22252b;
      --text: #e6e8ec;
      --text-secondary: #9aa0a8;
      --text-dim: #6b7078;
      --text-muted: #9aa0a8;
      --accent: #3dd68c;
      --accent-soft: rgba(61, 214, 140, 0.12);
      --accent-hover: #4de89a;
      --amber: #d4a017;
      --amber-bg: rgba(212, 160, 23, 0.1);
      --success: #3dd68c;
      --success-bg: rgba(61, 214, 140, 0.1);
      --error: #e05c5c;
      --error-bg: rgba(224, 92, 92, 0.1);
      --warn: #d4a017;
      --get: #3dd68c;
      --post: #7eb8da;
      --put: #d4a017;
      --delete: #e05c5c;
      --font-ui: 'JetBrains Mono', 'IBM Plex Mono', ui-monospace, monospace;
      --font-mono: 'JetBrains Mono', 'IBM Plex Mono', ui-monospace, monospace;
      --bg-panel: #0d0e10;
      --radius: 8px;
      --radius-sm: 6px;
      --sidebar-w: 228px;
      --transition: 0.16s ease-out;
    }

    .mono {
      font-family: var(--font-mono);
      font-size: 0.92em;
      letter-spacing: -0.01em;
    }

    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    * {
      scrollbar-width: thin;
      scrollbar-color: #353941 transparent;
    }

    *::-webkit-scrollbar {
      width: 6px;
      height: 6px;
    }

    *::-webkit-scrollbar-track {
      background: transparent;
    }

    *::-webkit-scrollbar-thumb {
      background: #353941;
      border-radius: 999px;
      border: 1px solid transparent;
      background-clip: padding-box;
    }

    *::-webkit-scrollbar-thumb:hover {
      background: #454a54;
      background-clip: padding-box;
    }

    *::-webkit-scrollbar-corner {
      background: transparent;
    }

    html, body {
      height: 100%;
      overflow: hidden;
    }

    body {
      font-family: var(--font-mono);
      background: var(--bg);
      color: var(--text);
      -webkit-font-smoothing: antialiased;
    }

    .app {
      display: grid;
      grid-template-columns: var(--sidebar-w) 1fr;
      height: 100vh;
    }

    /* ── Sidebar ── */
    .sidebar {
      background: var(--sidebar);
      border-right: 1px solid var(--border-subtle);
      display: flex;
      flex-direction: column;
      padding: 20px 12px;
      height: 100%;
      min-height: 0;
      overflow: hidden;
    }

    .brand {
      text-align: center;
      padding: 6px 8px 16px;
      margin-bottom: 4px;
      border-bottom: 1px solid var(--border-subtle);
    }

    .user-chip[hidden] { display: none !important; }

    .user-avatar {
      width: 24px;
      height: 24px;
      border-radius: 50%;
      object-fit: cover;
      flex-shrink: 0;
      background: var(--bg);
    }

    .user-name {
      font-size: 11px;
      font-weight: 600;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
      max-width: 42%;
    }

    .user-divider {
      color: var(--text-dim);
      font-size: 10px;
      flex-shrink: 0;
      opacity: 0.55;
    }

    .user-days {
      font-size: 10px;
      font-weight: 500;
      color: var(--text-secondary);
      white-space: nowrap;
      flex-shrink: 0;
    }

    .brand h1 {
      font-size: 13px;
      font-weight: 600;
      letter-spacing: -0.02em;
      line-height: 1.35;
    }

    .nav {
      display: flex;
      flex-direction: column;
      gap: 4px;
      flex: 1;
      min-height: 0;
      overflow-y: auto;
      overflow-x: hidden;
      padding-right: 2px;
    }

    .nav-btn {
      display: flex;
      align-items: center;
      gap: 10px;
      width: 100%;
      padding: 9px 12px;
      border: 1px solid transparent;
      border-radius: var(--radius-sm);
      background: transparent;
      color: var(--text-secondary);
      font-family: inherit;
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      transition: background var(--transition), color var(--transition), border-color var(--transition);
      text-align: left;
    }

    .nav-btn svg {
      width: 16px;
      height: 16px;
      flex-shrink: 0;
      opacity: 0.7;
    }

    .nav-btn-icon-pair {
      display: inline-flex;
      align-items: center;
      gap: 3px;
      flex-shrink: 0;
    }

    .nav-btn-icon-pair svg {
      width: 14px;
      height: 14px;
    }

    .nav-btn:hover {
      background: var(--surface);
      color: var(--text);
      border-color: var(--border-subtle);
    }

    .nav-btn.active {
      background: var(--surface-active);
      color: var(--text);
      border-color: var(--border);
    }

    .nav-btn.active svg { opacity: 1; }

    .nav-group {
      display: flex;
      flex-direction: column;
      gap: 2px;
      margin-top: 8px;
    }

    .nav-group-label {
      padding: 8px 12px 4px;
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--text-dim);
    }

    .sidebar-footer {
      padding: 10px 8px 0;
      border-top: 1px solid var(--border-subtle);
      margin-top: auto;
      display: flex;
      flex-direction: column;
      gap: 8px;
      flex-shrink: 0;
    }

    .sidebar-footer {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .status-item {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 11px;
      color: var(--text-dim);
    }

    .conn-status {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 11px;
      color: var(--text-dim);
    }

    .conn-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--text-dim);
      transition: background var(--transition), box-shadow var(--transition);
    }

    .conn-dot.live {
      background: var(--success);
      box-shadow: 0 0 6px rgba(61, 214, 140, 0.35);
    }

    .conn-dot.paused { background: var(--warn); }

    .conn-dot.bypass-active {
      background: var(--success);
      box-shadow: 0 0 6px rgba(61, 214, 140, 0.35);
    }

    .conn-dot.bypass-inactive {
      background: var(--text-dim);
      opacity: 0.45;
    }

    .status-item.monitoring-active .status-label {
      color: var(--text-secondary);
      font-weight: 500;
    }

    .status-item.monitoring-active .conn-dot {
      background: var(--success);
      box-shadow: 0 0 6px rgba(61, 214, 140, 0.35);
    }

    /* ── Main ── */
    .main {
      display: flex;
      flex-direction: column;
      overflow: hidden;
      background: var(--bg);
      min-height: 0;
      height: 100%;
    }

    .view {
      display: none;
      flex-direction: column;
      flex: 1;
      overflow: hidden;
      min-height: 0;
      animation: viewIn 0.2s ease-out;
    }

    .view.active { display: flex; }

    @keyframes viewIn {
      from { opacity: 0; transform: translateY(4px); }
      to   { opacity: 1; transform: translateY(0); }
    }

    /* ── Toolbar ── */
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 24px;
      border-bottom: 1px solid var(--border-subtle);
      background: var(--bg-elevated);
      flex-shrink: 0;
    }

    .toolbar-left, .toolbar-right {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .toolbar-title {
      font-size: 14px;
      font-weight: 600;
      letter-spacing: -0.01em;
    }

    .toolbar-sub {
      font-size: 11px;
      font-family: var(--font-mono);
      color: var(--text-dim);
      margin-left: 12px;
      padding-left: 12px;
      border-left: 1px solid var(--border-subtle);
    }

    .stat-chip {
      font-size: 10px;
      font-weight: 500;
      padding: 4px 10px;
      border-radius: 999px;
      background: var(--surface);
      color: var(--text-secondary);
      border: 1px solid var(--border-subtle);
      font-family: var(--font-mono);
      letter-spacing: 0.02em;
    }

    .stat-chip strong {
      color: var(--text);
      font-weight: 600;
    }

    /* ── Buttons ── */
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      font-family: inherit;
      font-size: 12px;
      font-weight: 500;
      padding: 8px 14px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text);
      cursor: pointer;
      transition: background var(--transition), border-color var(--transition), transform 0.1s ease, opacity var(--transition);
      white-space: nowrap;
    }

    .btn:hover:not(:disabled) {
      background: var(--surface-hover);
      border-color: var(--border);
    }

    .btn:active:not(:disabled) { opacity: 0.88; }

    .btn:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }

    .btn svg { width: 14px; height: 14px; }

    .btn-accent {
      background: var(--accent-soft);
      border-color: rgba(61, 214, 140, 0.35);
      color: var(--accent);
    }

    .btn-accent:hover:not(:disabled) {
      background: rgba(61, 214, 140, 0.18);
      border-color: rgba(61, 214, 140, 0.5);
      color: var(--accent-hover);
    }

    .btn-warn {
      background: var(--amber-bg);
      border-color: rgba(212, 160, 23, 0.35);
      color: var(--amber);
    }

    .btn-warn:hover:not(:disabled) {
      background: rgba(212, 160, 23, 0.18);
      border-color: rgba(212, 160, 23, 0.5);
      color: #e8b830;
    }

    .btn-ghost {
      background: transparent;
      border-color: transparent;
      color: var(--text-secondary);
    }

    .btn-ghost:hover:not(:disabled) {
      background: var(--surface);
      color: var(--text);
      border-color: var(--border-subtle);
    }

    .btn-icon {
      padding: 8px;
      min-width: 34px;
    }

    .btn-sm {
      padding: 5px 10px;
      font-size: 11px;
    }

    /* ── Request list ── */
    .request-list {
      flex: 1;
      overflow-y: auto;
      padding: 16px 24px 24px;
      scroll-behavior: auto;
      position: relative;
    }

    .monitor-spacer {
      width: 100%;
      pointer-events: none;
    }

    .monitor-stage {
      position: absolute;
      top: 16px;
      left: 24px;
      right: 24px;
      will-change: transform;
    }

    .monitor-stage .req-card {
      margin-bottom: 10px;
    }

    .empty-state {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 320px;
      text-align: center;
      color: var(--text-dim);
      position: absolute;
      inset: 16px 24px 24px;
      z-index: 2;
      pointer-events: none;
    }

    .empty-state[hidden] { display: none !important; }

    .empty-icon {
      width: 48px;
      height: 48px;
      border-radius: var(--radius-sm);
      background: var(--surface);
      border: 1px solid var(--border-subtle);
      display: grid;
      place-items: center;
      margin-bottom: 16px;
    }

    .empty-icon svg {
      width: 22px;
      height: 22px;
      stroke: var(--text-dim);
    }

    .empty-state h3 {
      font-size: 14px;
      font-weight: 600;
      color: var(--text-secondary);
      margin-bottom: 6px;
    }

    .empty-state p {
      font-size: 12px;
      max-width: 260px;
      line-height: 1.5;
    }

    /* ── Request card ── */
    .req-card {
      background: var(--bg-elevated);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius);
      margin-bottom: 10px;
      overflow: hidden;
      transition: border-color var(--transition);
    }

    .req-card:hover { border-color: var(--border); }

    @keyframes cardIn {
      from { opacity: 0; transform: translateY(4px); }
      to   { opacity: 1; transform: translateY(0); }
    }

    .req-header {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 12px 14px;
      cursor: pointer;
      user-select: none;
    }

    .req-url {
      flex: 1;
      min-width: 0;
      font-size: 12px;
      color: var(--text-secondary);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-family: var(--font-mono);
    }

    .req-url.money-flag {
      color: var(--error);
    }

    .req-meta {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-shrink: 0;
    }

    .req-time {
      font-size: 10px;
      color: var(--text-dim);
      font-family: var(--font-mono);
      white-space: nowrap;
    }

    .req-long-tag {
      font-size: 9px;
      color: var(--text-dim);
      font-family: var(--font-mono);
      padding: 1px 5px;
      border: 1px solid var(--border-subtle);
      border-radius: 3px;
      line-height: 1.2;
    }

    .req-status {
      font-size: 10px;
      font-weight: 600;
      padding: 2px 7px;
      border-radius: 4px;
      font-family: var(--font-mono);
      display: none;
    }

    .req-status.ok  { color: var(--success); background: var(--success-bg); }
    .req-status.err { color: var(--error);   background: var(--error-bg); }

    .req-actions {
      display: flex;
      gap: 4px;
      opacity: 0.55;
      transition: opacity var(--transition);
    }

    .req-card:hover .req-actions,
    .req-card.open .req-actions { opacity: 1; }

    .req-toggle {
      flex-shrink: 0;
      padding: 4px;
      min-width: 28px;
      color: var(--text-dim);
    }

    .req-toggle:hover { color: var(--text-secondary); }

    .req-chevron {
      width: 16px;
      height: 16px;
      color: var(--text-dim);
      transition: transform var(--transition);
      flex-shrink: 0;
    }

    .req-card.open .req-toggle .req-chevron { transform: rotate(180deg); }

    .req-body {
      display: none;
      border-top: 1px solid var(--border-subtle);
    }

    .req-card.open .req-body { display: block; }

    .req-code-wrap {
      padding: 14px;
      background: var(--sidebar);
    }

    .req-code-edit {
      display: block;
      width: 100%;
      min-height: 72px;
      padding: 0;
      margin: 0;
      border: none;
      border-radius: 0;
      background: transparent;
      color: var(--text);
      font-family: var(--font-mono);
      font-size: 11px;
      line-height: 1.65;
      resize: vertical;
      outline: none;
      white-space: pre-wrap;
      word-break: break-word;
      overflow-x: auto;
      overflow-y: auto;
      tab-size: 2;
      box-sizing: border-box;
      user-select: text;
      -webkit-user-select: text;
    }

    .req-code-edit:focus {
      outline: none;
    }

    .req-code-edit::selection {
      background: rgba(255, 255, 255, 0.12);
    }

    .req-footer {
      display: flex;
      justify-content: flex-end;
      gap: 6px;
      padding: 10px 14px;
      background: var(--surface);
      border-top: 1px solid var(--border-subtle);
    }

    /* ── Executor ── */
    .executor-layout {
      flex: 1;
      display: grid;
      grid-template-columns: 248px 1fr;
      overflow: hidden;
      min-height: 0;
    }

    .executor-main {
      display: grid;
      grid-template-rows: 1fr auto;
      overflow: hidden;
      min-height: 0;
    }

    .executor-scripts-rail {
      display: flex;
      flex-direction: column;
      border-right: 1px solid var(--border-subtle);
      background: var(--bg-panel);
      min-height: 0;
    }

    .executor-scripts-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 14px 14px 10px;
      border-bottom: 1px solid var(--border-subtle);
      flex-shrink: 0;
    }

    .executor-scripts-title {
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--text-dim);
    }

    .executor-save-box {
      display: flex;
      gap: 6px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--border-subtle);
      flex-shrink: 0;
    }

    .executor-save-box input {
      flex: 1;
      min-width: 0;
      padding: 7px 10px;
      font-size: 12px;
      font-family: var(--font-mono);
      color: var(--text);
      background: var(--bg-elevated);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      outline: none;
    }

    .executor-save-box input:focus {
      border-color: var(--border);
    }

    .executor-scripts-list {
      flex: 1;
      overflow-y: auto;
      padding: 8px;
      min-height: 0;
    }

    .executor-scripts-empty {
      padding: 24px 12px;
      text-align: center;
      font-size: 12px;
      color: var(--text-dim);
      line-height: 1.5;
    }

    .executor-script-item {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 10px 10px;
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      background: var(--bg-elevated);
      margin-bottom: 6px;
      cursor: pointer;
      transition: border-color var(--transition), background var(--transition);
    }

    .executor-script-item:hover {
      border-color: var(--border);
      background: var(--surface);
    }

    .executor-script-item.active {
      border-color: var(--accent);
      background: var(--accent-soft);
    }

    .executor-script-copy {
      flex: 1;
      min-width: 0;
    }

    .executor-script-copy strong {
      display: block;
      font-size: 12px;
      font-weight: 500;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .executor-script-copy span {
      display: block;
      font-size: 10px;
      color: var(--text-dim);
      font-family: var(--font-mono);
      margin-top: 2px;
    }

    .executor-script-del {
      flex-shrink: 0;
      opacity: 0.5;
    }

    .executor-script-item:hover .executor-script-del {
      opacity: 1;
    }

    .editor-wrap {
      display: flex;
      flex-direction: column;
      overflow: hidden;
      border-bottom: 1px solid var(--border-subtle);
    }

    .editor-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 24px;
      background: var(--bg-elevated);
      border-bottom: 1px solid var(--border-subtle);
      flex-shrink: 0;
    }

    .editor-toolbar-left {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }

    .editor-toolbar-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--text);
    }

    .editor-toolbar-hint {
      font-size: 11px;
      color: var(--text-dim);
      font-family: var(--font-mono);
    }

    .editor-toolbar-actions {
      display: flex;
      gap: 8px;
      align-items: center;
    }

    .editor-lang-badge {
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--text-secondary);
      background: var(--surface);
      border: 1px solid var(--border-subtle);
      padding: 3px 8px;
      border-radius: 999px;
      font-family: var(--font-mono);
    }

    .code-editor-host {
      flex: 1;
      min-height: 0;
      display: flex;
      overflow: hidden;
      background: #0b0c0e;
      position: relative;
    }

    .editor-placeholder {
      position: absolute;
      inset: 0;
      margin: 0;
      padding: 4px 16px 4px 58px;
      font-family: var(--font-mono);
      font-size: 13px;
      line-height: 1.65;
      color: #454850;
      white-space: pre;
      pointer-events: none;
      overflow: hidden;
      z-index: 2;
      user-select: none;
    }

    .editor-placeholder.is-hidden {
      display: none;
    }

    .code-editor-host .CodeMirror {
      flex: 1;
      height: auto !important;
      font-family: var(--font-mono);
      font-size: 13px;
      line-height: 1.65;
      background: #0b0c0e;
      position: relative;
      z-index: 1;
    }

    .code-editor-host textarea.plain-editor {
      flex: 1;
      width: 100%;
      min-height: 220px;
      padding: 12px 16px;
      border: none;
      outline: none;
      resize: none;
      background: #0b0c0e;
      color: #c8ccd4;
      font-family: var(--font-mono);
      font-size: 13px;
      line-height: 1.65;
      position: relative;
      z-index: 1;
    }

    .cm-s-fivem-editor.CodeMirror {
      background: #0b0c0e;
      color: #c8ccd4;
    }

    .cm-s-fivem-editor .CodeMirror-gutters {
      background: #111214;
      border-right: 1px solid #22252b;
      color: #50545c;
    }

    .cm-s-fivem-editor .CodeMirror-linenumber {
      color: #50545c;
      padding: 0 10px 0 6px;
    }

    .cm-s-fivem-editor .CodeMirror-cursor {
      border-left: 2px solid #e6e8ec;
    }

    .cm-s-fivem-editor .CodeMirror-selected {
      background: rgba(255, 255, 255, 0.12) !important;
    }

    .cm-s-fivem-editor .CodeMirror-activeline-background {
      background: rgba(255, 255, 255, 0.03);
    }

    .cm-s-fivem-editor .CodeMirror-matchingbracket {
      color: #fff !important;
      background: rgba(255, 255, 255, 0.1);
      outline: 1px solid rgba(255, 255, 255, 0.2);
    }

    .cm-s-fivem-editor .cm-keyword { color: #e6e8ec; }
    .cm-s-fivem-editor .cm-atom { color: #c8ccd4; }
    .cm-s-fivem-editor .cm-number { color: #c8ccd4; }
    .cm-s-fivem-editor .cm-def { color: #c8ccd4; }
    .cm-s-fivem-editor .cm-variable { color: #c8ccd4; }
    .cm-s-fivem-editor .cm-variable-2 { color: #c8ccd4; }
    .cm-s-fivem-editor .cm-property { color: #c8ccd4; }
    .cm-s-fivem-editor .cm-operator { color: #9aa0a8; }
    .cm-s-fivem-editor .cm-string { color: #c8ccd4; }
    .cm-s-fivem-editor .cm-string-2 { color: #c8ccd4; }
    .cm-s-fivem-editor .cm-comment { color: #50545c; font-style: italic; }
    .cm-s-fivem-editor .cm-meta { color: #9aa0a8; }
    .cm-s-fivem-editor .cm-builtin { color: #c8ccd4; }
    .cm-s-fivem-editor .cm-tag { color: #c8ccd4; }
    .cm-s-fivem-editor .cm-attribute { color: #c8ccd4; }
    .cm-s-fivem-editor .cm-qualifier { color: #c8ccd4; }
    .cm-s-fivem-editor .cm-error { color: #e05c5c; }

    .cm-s-fivem-editor .CodeMirror-vscrollbar,
    .cm-s-fivem-editor .CodeMirror-hscrollbar {
      outline: none;
    }

    .output-panel {
      background: var(--bg-elevated);
      display: flex;
      flex-direction: column;
      max-height: 200px;
      min-height: 120px;
    }

    .output-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 24px;
      border-bottom: 1px solid var(--border-subtle);
      flex-shrink: 0;
    }

    .output-header span {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-dim);
    }

    #execResult {
      flex: 1;
      padding: 14px 24px;
      font-family: var(--font-mono);
      font-size: 12px;
      line-height: 1.55;
      color: var(--text-secondary);
      overflow-y: auto;
      white-space: pre-wrap;
    }


    /* ── Pull All ── */
    .pull-layout {
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      min-height: 0;
    }

    .pull-body {
      flex: 1;
      display: flex;
      gap: 16px;
      padding: 16px 20px;
      overflow: hidden;
      min-height: 0;
    }

    @media (max-width: 900px) {
      .pull-body {
        flex-direction: column;
        overflow-y: auto;
      }
    }

    .pull-panel {
      flex: 1;
      display: flex;
      flex-direction: column;
      background: var(--bg-elevated);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius);
      overflow: hidden;
      min-height: 0;
      min-width: 0;
    }

    .pull-panel-head {
      padding: 10px 14px;
      border-bottom: 1px solid var(--border-subtle);
      font-family: var(--font-mono);
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-dim);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-shrink: 0;
    }

    .pull-panel-head span {
      flex: 1;
      min-width: 0;
    }

    .pull-panel-head .case-preserve {
      text-transform: none;
      letter-spacing: normal;
    }

    .pull-textarea {
      flex: 1;
      min-height: 0;
      width: 100%;
      padding: 16px;
      background: var(--sidebar);
      border: none;
      color: var(--text);
      font-family: var(--font-mono);
      font-size: 13px;
      line-height: 1.7;
      resize: none;
      outline: none;
      box-sizing: border-box;
      user-select: text;
      -webkit-user-select: text;
    }

    .pull-textarea::placeholder { color: var(--text-dim); }

    .pull-ids-panel {
      display: flex;
      flex-direction: column;
      min-height: 0;
      height: 100%;
    }

    .pull-ids-panel .pull-textarea-ids {
      flex: 1 1 auto;
      min-height: 0;
    }

    .pull-ban-text-wrap {
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
      border-top: 1px solid var(--border-subtle);
      min-height: 0;
    }

    .pull-ban-text-wrap .pull-panel-head {
      border-bottom: none;
      padding-bottom: 8px;
    }

    .pull-textarea-ban {
      min-height: 72px;
      max-height: 120px;
      resize: vertical;
      flex: 0 0 auto;
      border-top: none;
    }

    .pull-ban-fields {
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
    }

    .pull-ban-fields .pull-ban-text-wrap + .pull-ban-text-wrap {
      border-top: 1px solid var(--border-subtle);
    }

    .pull-textarea-jail-time {
      min-height: 44px;
      max-height: 72px;
    }

    .pull-log {
      flex: 1;
      min-height: 0;
      padding: 14px 16px;
      overflow-y: auto;
      font-family: var(--font-mono);
      font-size: 12px;
      line-height: 1.6;
      color: var(--text-secondary);
      background: var(--sidebar);
    }

    .pull-log .line { margin-bottom: 6px; }
    .pull-log .line.ok  { color: var(--success); }
    .pull-log .line.err { color: var(--error); }
    .pull-log .line.dim { color: var(--text-dim); }

    .pull-status-bar {
      flex-shrink: 0;
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 20px;
      border-top: 1px solid var(--border-subtle);
      background: var(--bg-panel);
      font-family: var(--font-mono);
      font-size: 10px;
      line-height: 1.2;
      color: var(--text-dim);
      min-height: 30px;
    }

    .pull-status-label {
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--text-dim);
    }

    .pull-status-value {
      font-size: 10px;
      font-weight: 500;
      color: var(--text-secondary);
    }

    .pull-status-bar strong { color: var(--text-secondary); font-size: 10px; font-weight: 500; }

    .pull-progress {
      font-family: var(--font-mono);
      font-size: 10px;
      color: var(--text-dim);
      margin-left: auto;
    }

    /* ── Toasts ── */
    .toast-stack {
      position: fixed;
      bottom: 20px;
      right: 20px;
      display: flex;
      flex-direction: column-reverse;
      gap: 8px;
      z-index: 1000;
      pointer-events: none;
    }

    .toast {
      background: var(--surface-active);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 10px 14px;
      border-radius: var(--radius-sm);
      font-size: 11px;
      font-weight: 500;
      box-shadow: 0 8px 24px rgba(0,0,0,0.4);
      animation: toastIn 0.22s ease-out;
      pointer-events: auto;
      max-width: 340px;
      font-family: var(--font-mono);
    }

    .toast.ok    { border-color: rgba(61, 214, 140, 0.35); color: var(--success); }
    .toast.error { border-color: rgba(224, 92, 92, 0.35); color: var(--error); }

    @keyframes toastIn {
      from { opacity: 0; transform: translateY(8px); }
      to   { opacity: 1; transform: translateY(0); }
    }

    @keyframes toastOut {
      to { opacity: 0; transform: translateY(8px); }
    }

    /* ── Pull modal ── */
    .pull-modal {
      position: fixed;
      inset: 0;
      background: rgba(8, 9, 10, 0.72);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 2000;
      backdrop-filter: blur(3px);
      animation: fadeIn 0.18s ease-out;
    }

    .pull-modal.show { display: flex; }

    .pull-modal-box {
      background: var(--bg-elevated);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 26px 28px 18px;
      max-width: 420px;
      width: 90%;
      text-align: center;
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5);
    }

    .pull-modal-box p {
      font-size: 16px;
      font-weight: 600;
      line-height: 1.55;
      color: var(--text);
      margin-bottom: 22px;
      direction: rtl;
      font-family: var(--font-mono);
    }

    .pull-modal-box .btn {
      width: 100%;
    }

    /* ── Boot loader ── */
    .boot-loader {
      display: none !important;
      position: fixed;
      inset: 0;
      z-index: 5000;
      display: flex;
      align-items: stretch;
      justify-content: center;
      padding: clamp(20px, 4vw, 48px);
      background: var(--bg);
      transition: opacity 0.3s ease-out, visibility 0.3s ease-out;
    }

    .boot-loader.hide {
      opacity: 0;
      visibility: hidden;
      pointer-events: none;
    }

    .boot-grid {
      position: absolute;
      inset: 0;
      pointer-events: none;
      background:
        linear-gradient(rgba(255,255,255,0.018) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.018) 1px, transparent 1px);
      background-size: 28px 28px;
      mask-image: radial-gradient(ellipse 85% 75% at 50% 42%, black 15%, transparent 100%);
    }

    .boot-grid::after {
      content: "";
      position: absolute;
      inset: 0;
      background: repeating-linear-gradient(
        0deg,
        transparent,
        transparent 2px,
        rgba(0,0,0,0.03) 2px,
        rgba(0,0,0,0.03) 4px
      );
      opacity: 0.35;
    }

    .boot-shell {
      position: relative;
      width: min(920px, 100%);
      margin: auto;
      display: flex;
      flex-direction: column;
      gap: 18px;
      animation: bootIn 0.32s ease-out;
    }

    @keyframes bootIn {
      from { opacity: 0; transform: translateY(6px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .boot-header {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 16px;
      padding: 0 2px;
    }

    .boot-brand-row {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .boot-brand-mark {
      width: 44px;
      height: 44px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--border);
      background: var(--surface);
      display: grid;
      place-items: center;
    }

    .boot-brand-mark img {
      width: 24px;
      height: 24px;
      object-fit: contain;
    }

    .boot-kicker {
      font-size: 10px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--text-dim);
      margin-bottom: 2px;
    }

    .boot-title {
      font-size: 20px;
      font-weight: 600;
      letter-spacing: -0.03em;
      line-height: 1.1;
    }

    .boot-meta {
      font-size: 10px;
      color: var(--text-dim);
      white-space: nowrap;
    }

    .boot-panels {
      display: grid;
      grid-template-columns: 1.05fr 0.95fr;
      gap: 14px;
      min-height: min(420px, calc(100vh - 160px));
    }

    @media (max-width: 860px) {
      .boot-panels { grid-template-columns: 1fr; min-height: auto; }
    }

    .boot-panel {
      background: var(--sidebar);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius);
      padding: 14px 14px 16px;
      display: flex;
      flex-direction: column;
      min-height: 0;
    }

    .boot-panel-head {
      font-size: 10px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--text-dim);
      margin-bottom: 10px;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--border-subtle);
    }

    .boot-log {
      flex: 1;
      min-height: 180px;
      max-height: 320px;
      overflow: auto;
      font-family: var(--font-mono);
      font-size: 11px;
      line-height: 1.55;
      color: var(--text-secondary);
      padding-right: 4px;
    }

    .boot-log-line {
      opacity: 0;
      animation: logIn 0.25s ease forwards;
    }

    @keyframes logIn {
      from { opacity: 0; transform: translateY(3px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .boot-log-line.dim { color: var(--text-dim); }
    .boot-log-line.ok { color: var(--success); }
    .boot-log-line.warn { color: var(--amber); }

    .boot-log-active {
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px solid var(--border-subtle);
      font-size: 11px;
      color: var(--text);
      min-height: 18px;
    }

    .boot-cursor {
      display: inline-block;
      color: var(--accent);
      animation: cursorBlink 1s step-end infinite;
    }

    @keyframes cursorBlink {
      50% { opacity: 0; }
    }

    .boot-status-rows {
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin: 0;
      padding: 0;
      flex: 1;
    }

    .boot-status-row {
      display: grid;
      grid-template-columns: 10px 1fr auto;
      align-items: center;
      gap: 10px;
      font-size: 12px;
      color: var(--text-secondary);
      transition: color var(--transition);
    }

    .boot-status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      border: 1px solid var(--border);
      background: transparent;
      transition: background var(--transition), border-color var(--transition), box-shadow var(--transition);
    }

    .boot-status-row.pending .boot-status-dot { border-color: var(--text-dim); }
    .boot-status-row.active { color: var(--text); }
    .boot-status-row.active .boot-status-dot {
      background: var(--amber);
      border-color: var(--amber);
      box-shadow: 0 0 0 2px var(--amber-bg);
    }
    .boot-status-row.done { color: var(--success); }
    .boot-status-row.done .boot-status-dot {
      background: var(--success);
      border-color: var(--success);
    }
    .boot-status-row.failed { color: var(--error); }
    .boot-status-row.failed .boot-status-dot {
      background: var(--error);
      border-color: var(--error);
    }

    .boot-status-label { font-weight: 500; }
    .boot-status-value {
      font-family: var(--font-mono);
      font-size: 10px;
      letter-spacing: 0.04em;
      color: var(--text-dim);
    }

    .boot-status-row.active .boot-status-value { color: var(--amber); }
    .boot-status-row.done .boot-status-value { color: var(--success); }
    .boot-status-row.failed .boot-status-value { color: var(--error); }

    .boot-progress {
      height: 3px;
      background: var(--border-subtle);
      border-radius: 99px;
      margin-top: 14px;
      overflow: hidden;
    }

    .boot-progress-fill {
      height: 100%;
      width: 0%;
      background: var(--accent);
      border-radius: 99px;
      transition: width 0.28s ease-out;
    }

    .boot-headline {
      margin-top: 12px;
      font-size: 14px;
      font-weight: 600;
      letter-spacing: -0.02em;
    }

    .boot-detail {
      margin-top: 4px;
      font-size: 11px;
      line-height: 1.45;
      color: var(--text-dim);
      min-height: 16px;
    }

    .boot-error {
      border: 1px solid rgba(224, 92, 92, 0.25);
      background: var(--error-bg);
      border-radius: var(--radius);
      padding: 16px 18px;
      animation: bootIn 0.28s ease-out;
    }

    .boot-error[hidden] { display: none !important; }

    .boot-error-title {
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--error);
      margin-bottom: 8px;
    }

    .boot-error-detail {
      font-size: 11px;
      line-height: 1.5;
      color: var(--text-secondary);
      margin-bottom: 14px;
    }

    .boot-retry-btn {
      appearance: none;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text);
      font-family: inherit;
      font-size: 12px;
      font-weight: 500;
      padding: 8px 14px;
      border-radius: var(--radius-sm);
      cursor: pointer;
      transition: background var(--transition), border-color var(--transition);
    }

    .boot-retry-btn:hover {
      background: var(--surface-hover);
      border-color: var(--accent);
    }

    .boot-retry-btn:focus-visible {
      outline: 2px solid rgba(61, 214, 140, 0.45);
      outline-offset: 2px;
    }

    .app.boot-hidden {
      opacity: 0;
      visibility: hidden;
      pointer-events: none;
    }

    .app.boot-visible {
      opacity: 1;
      visibility: visible;
      transition: opacity 0.3s ease-out;
    }

    .blocker-layout {
      display: flex;
      flex-direction: column;
      gap: 14px;
      height: calc(100vh - 88px);
      padding: 16px 18px 18px;
    }

    .blocker-add {
      display: flex;
      gap: 10px;
      align-items: center;
    }

    .blocker-add input {
      flex: 1;
      min-width: 0;
      padding: 11px 12px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--border-subtle);
      background: var(--surface);
      color: var(--text);
      font-family: inherit;
      font-size: 13px;
      user-select: text;
      -webkit-user-select: text;
    }

    .blocker-add input:focus {
      outline: none;
      border-color: var(--accent);
    }

    .blocker-stats {
      font-size: 12px;
      color: var(--text-dim);
    }

    .blocker-list {
      flex: 1;
      min-height: 0;
      overflow: auto;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .blocker-item {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 12px 14px;
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      background: var(--surface);
    }

    .blocker-item-copy {
      flex: 1;
      min-width: 0;
    }

    .blocker-item-copy strong {
      display: block;
      font-size: 13px;
      font-weight: 500;
      word-break: break-all;
    }

    .blocker-item-copy span {
      display: block;
      margin-top: 4px;
      font-size: 11px;
      color: var(--text-dim);
    }

    .blocker-count {
      font-size: 12px;
      font-weight: 600;
      color: var(--warn);
      white-space: nowrap;
    }

    .blocker-empty {
      padding: 28px 12px;
      text-align: center;
      color: var(--text-dim);
      font-size: 13px;
    }

    .debug-layout {
      flex: 1;
      display: grid;
      grid-template-columns: 280px 1fr;
      overflow: hidden;
      min-height: 0;
    }

    .debug-tabs-rail {
      display: flex;
      flex-direction: column;
      border-right: 1px solid var(--border-subtle);
      background: var(--bg-panel);
      min-height: 0;
    }

    .debug-tabs-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 14px 14px 10px;
      border-bottom: 1px solid var(--border-subtle);
      flex-shrink: 0;
    }

    .debug-tabs-title {
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--text-dim);
    }

    .debug-tabs-sub {
      padding: 0 14px 10px;
      font-size: 11px;
      color: var(--text-dim);
      line-height: 1.45;
      border-bottom: 1px solid var(--border-subtle);
    }

    .debug-tabs-list {
      flex: 1;
      overflow-y: auto;
      padding: 8px;
      min-height: 0;
    }

    .debug-tabs-empty {
      padding: 24px 12px;
      text-align: center;
      font-size: 12px;
      color: var(--text-dim);
      line-height: 1.5;
    }

    .debug-tab-item {
      display: flex;
      flex-direction: column;
      gap: 4px;
      padding: 10px 10px;
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      background: var(--bg-elevated);
      margin-bottom: 6px;
      cursor: pointer;
      transition: border-color var(--transition), background var(--transition);
    }

    .debug-tab-item:hover {
      border-color: var(--border);
      background: var(--surface);
    }

    .debug-tab-item.active {
      border-color: var(--accent);
      background: var(--accent-soft);
    }

    .debug-tab-item strong {
      font-size: 12px;
      font-weight: 500;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .debug-tab-item span {
      font-size: 10px;
      color: var(--text-dim);
      font-family: var(--font-mono);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .debug-tab-kind {
      display: inline-block;
      font-size: 9px;
      font-weight: 600;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      color: var(--accent);
      background: var(--accent-soft);
      padding: 2px 6px;
      border-radius: 999px;
      width: fit-content;
    }

    .debug-workspace {
      display: grid;
      grid-template-rows: auto auto 1fr auto auto;
      overflow: hidden;
      min-height: 0;
    }

    .debug-target-bar {
      padding: 12px 20px;
      border-bottom: 1px solid var(--border-subtle);
      background: var(--bg-elevated);
      font-size: 12px;
      color: var(--text-secondary);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .debug-target-bar strong {
      color: var(--text);
      font-weight: 600;
    }

    .debug-pane-tabs {
      display: flex;
      gap: 4px;
      padding: 8px 16px;
      border-bottom: 1px solid var(--border-subtle);
      background: var(--bg-panel);
    }

    .debug-pane-tab {
      padding: 6px 12px;
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--text-dim);
      background: transparent;
      border: 1px solid transparent;
      border-radius: var(--radius-sm);
      cursor: pointer;
      transition: color var(--transition), background var(--transition), border-color var(--transition);
    }

    .debug-pane-tab:hover {
      color: var(--text);
      background: var(--surface);
    }

    .debug-pane-tab.active {
      color: var(--text);
      background: var(--surface);
      border-color: var(--border-subtle);
    }

    .debug-panes {
      position: relative;
      overflow: hidden;
      min-height: 0;
    }

    .debug-pane {
      position: absolute;
      inset: 0;
      display: none;
      flex-direction: column;
      overflow: hidden;
    }

    .debug-pane.active {
      display: flex;
    }

    .debug-editor-host {
      flex: 1;
      min-height: 0;
      display: flex;
      overflow: hidden;
      background: #0b0c0e;
    }

    .debug-editor-host .CodeMirror {
      flex: 1;
      height: auto !important;
      font-family: var(--font-mono);
      font-size: 13px;
      line-height: 1.65;
      background: #0b0c0e;
    }

    .debug-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 10px 16px;
      border-top: 1px solid var(--border-subtle);
      background: var(--bg-elevated);
    }

    .debug-actions-left,
    .debug-actions-right {
      display: flex;
      gap: 8px;
      align-items: center;
    }

    .debug-result {
      max-height: 120px;
      min-height: 72px;
      padding: 10px 20px;
      font-family: var(--font-mono);
      font-size: 12px;
      line-height: 1.5;
      color: var(--text-secondary);
      background: var(--bg-panel);
      border-top: 1px solid var(--border-subtle);
      overflow-y: auto;
      white-space: pre-wrap;
    }

    .editor-toolbar-tools {
      display: flex;
      gap: 4px;
      align-items: center;
      margin-right: 8px;
    }

    .editor-tool-btn {
      padding: 4px 8px;
      font-size: 10px;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--text-dim);
      background: var(--surface);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      cursor: pointer;
      transition: color var(--transition), border-color var(--transition);
    }

    .editor-tool-btn:hover {
      color: var(--text);
      border-color: var(--border);
    }

    .CodeMirror-foldmarker {
      color: var(--text-dim);
      cursor: pointer;
    }

    .cm-s-fivem-editor .CodeMirror-foldgutter-open,
    .cm-s-fivem-editor .CodeMirror-foldgutter-folded {
      color: #50545c;
    }

  </style>
</head>
<body>
  <div class="boot-loader" id="bootLoader">
    <div class="boot-grid" aria-hidden="true"></div>
    <div class="boot-shell">
      <header class="boot-header">
        <div class="boot-brand-row">
          <div class="boot-brand-mark">
            <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E" alt="" width="24" height="24">
          </div>
          <div>
            <div class="boot-kicker mono">Arya // Initialization</div>
            <div class="boot-title">FiveM Tool</div>
          </div>
        </div>
        <div class="boot-meta mono">v__TOOL_VERSION__ · build __UI_BUILD__</div>
      </header>

      <div class="boot-panels" id="bootPanels">
        <section class="boot-panel">
          <div class="boot-panel-head mono">Init Log</div>
          <div class="boot-log" id="bootTerminalLog"></div>
          <div class="boot-log-active mono" id="bootActiveLine">&gt; Initializing…<span class="boot-cursor" id="bootCursor">▌</span></div>
        </section>

        <section class="boot-panel">
          <div class="boot-panel-head mono">System Status</div>
          <ul class="boot-status-rows" id="bootStatusRows"></ul>
          <div class="boot-progress" aria-hidden="true">
            <div class="boot-progress-fill" id="bootProgress"></div>
          </div>
          <div class="boot-headline" id="bootStep">Starting</div>
          <div class="boot-detail mono" id="bootDetail">Preparing workspace…</div>
        </section>
      </div>

      <div class="boot-error" id="bootError" hidden>
        <div class="boot-error-title mono">Initialization Failed</div>
        <div class="boot-error-detail mono" id="bootErrorDetail">Unable to continue.</div>
        <button type="button" class="boot-retry-btn" id="bootRetryBtn">Retry</button>
      </div>
    </div>
  </div>

  <div class="app boot-visible" id="appRoot">
    <aside class="sidebar">
      <div class="brand">
        <h1>Arya FiveM Tool</h1>
      </div>

      <nav class="nav">
        <button class="nav-btn active" data-view="monitor">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
          </svg>
          Monitor
        </button>
        <button class="nav-btn" data-view="executor">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="4 17 10 11 4 5"/>
            <line x1="12" y1="19" x2="20" y2="19"/>
          </svg>
          Executor
        </button>
        <button class="nav-btn" data-view="debug">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="m8 2 1.88 1.88"/>
            <path d="M14.12 3.88 16 2"/>
            <path d="M9 7.13v-1a3.003 3.003 0 1 1 6 0v1"/>
            <path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6"/>
            <path d="M12 20v-9"/>
            <path d="M6.53 9C4.6 8.8 3 7.1 3 5"/>
            <path d="M6 13H2"/>
            <path d="M3 21c0-2.1 1.7-3.9 3.8-4"/>
            <path d="M20.97 5c0 2.1-1.6 3.8-3.5 4"/>
            <path d="M22 13h-4"/>
            <path d="M17.2 17c2.1.1 3.8 1.9 3.8 4"/>
          </svg>
          Debug
        </button>
        <button class="nav-btn" data-view="blocker">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>
          </svg>
          Blocker
        </button>
        <div class="nav-group">
          <span class="nav-group-label">vRP</span>
          <button class="nav-btn" data-view="pullall">
            <svg viewBox="0 0 32 32" fill="currentColor" aria-hidden="true">
              <path d="M30.052 8.772l-0.337-0.659c-0.372-0.728-1.263-1.018-1.992-0.646l-5.919 3.022c-0.316 0.161-0.549 0.42-0.681 0.721-0.078 0.102-0.143 0.215-0.194 0.34l-1.451 3.571-0.514-0.733-3.122-5.355v-6.029c0-0.818-0.663-1.481-1.48-1.481h-0.74c-0.817 0-1.48 0.663-1.48 1.481v6.649c0 0.338 0.113 0.649 0.303 0.898 0.009 0.017 0.018 0.034 0.028 0.050l2.671 4.581-4.581 3.215 0.381-3.049c0.065-0.516-0.145-1.002-0.513-1.315l-4.141-5.117c-0.514-0.636-1.446-0.734-2.082-0.219l-0.575 0.466c-0.635 0.514-0.733 1.447-0.219 2.083l3.736 4.618-0.703 5.623c-0.036 0.285 0.013 0.562 0.125 0.805 0.040 0.192 0.118 0.379 0.238 0.549l2.55 3.637c0.021 0.029 0.043 0.058 0.065 0.085 0.126 0.367 0.394 0.684 0.773 0.862l6.021 2.815c0.015 0.007 0.031 0.014 0.046 0.020 0.493 0.336 1.163 0.352 1.681-0.010l5.447-3.809c0.67-0.469 0.834-1.392 0.365-2.062l-0.424-0.607c-0.468-0.67-1.391-0.834-2.061-0.366l-4.38 3.063-2.967-1.387 7.189-5.044c0.174-0.122 0.313-0.274 0.416-0.445 0.135-0.135 0.246-0.299 0.322-0.487l2.3-5.658 5.253-2.682c0.728-0.372 1.017-1.264 0.645-1.992zM8.499 27.249c0 2.071-1.679 3.75-3.75 3.75s-3.75-1.679-3.75-3.75c0-2.071 1.679-3.75 3.75-3.75s3.75 1.679 3.75 3.75z"/>
            </svg>
            Pull Players
          </button>
          <button class="nav-btn" data-view="banplayer">
            <svg viewBox="0 0 32 32" fill="currentColor" aria-hidden="true">
              <g transform="translate(-310.001 -321.695)">
                <path d="M326,321.7a16,16,0,1,0,16,16A16,16,0,0,0,326,321.7Zm0,28a12,12,0,1,1,12-12A12,12,0,0,1,326,349.7Z"/>
                <rect width="28.969" height="4" transform="translate(314.348 346.523) rotate(-45.001)"/>
              </g>
            </svg>
            Ban / Kick Players
          </button>
          <button class="nav-btn" data-view="jailplayer">
            <svg viewBox="0 0 512 512" fill="currentColor" aria-hidden="true">
              <path d="M406.344,336.641c-1.766,0-3.953,0-6.344,0V0h-32v336.844c-18.141,1.063-25,13.641-25,25.125c0,1.547,0,6.031,0,11.906c-4.875-29.281-17.125-55.906-32.422-75.766c19.953-16.016,32.766-40.547,32.766-68.109c0-39.906-26.797-73.5-63.344-83.922v25.328c23.063,9.484,39.344,32.156,39.344,58.594c0,26.422-16.281,49.094-39.344,58.563v48.75h26.531c4.594,9.703,8.266,20.016,10.781,30.594H280v68.156h41.344v29.172H280V512h65.344c0,0,0-27.375,0-79.094c3.453,7.031,10.719,10.406,21,10.406c0.469,0,1.094,0,1.656,0V512h32v-68.688c0.109,0,0.219,0,0.344,0c14,0,19.188-7.281,23.344-12.672c6.656-8.672,15.313-41.328,11.313-68C433.609,353.313,423.688,336.641,406.344,336.641z M410.969,420.906l-0.344,0.438c-3.406,4.469-4.563,5.969-10.281,5.969h-34c-7.344,0-7.344-1.031-7.344-5.344v-60c0-8.266,6.063-9.328,11.344-9.328h36c7.688,0,12.406,9.391,12.844,12.375C422.75,388.813,414.406,416.172,410.969,420.906z"/>
              <rect x="480" width="32" height="512"/>
              <rect width="32" height="512"/>
              <path d="M168.672,230c0,27.563,12.797,52.094,32.75,68.094c-15.297,19.875-27.547,46.5-32.422,75.766c0-5.859,0-10.344,0-11.891c0-11.484-6.859-24.063-25-25.125V0h-32v336.641c-2.391,0-4.578,0-6.344,0c-17.328,0-27.266,16.672-28.656,26c-4,26.672,4.656,59.328,11.328,68c4.141,5.391,9.328,12.672,23.328,12.672c0.125,0,0.234,0,0.344,0V512h32v-68.688c0.563,0,1.188,0,1.656,0c10.297,0,17.563-3.375,21.016-10.406c0,51.719,0,79.094,0,79.094H232v-46.766h-41.328v-29.172H232v-68.156h-37.297c2.5-10.578,6.172-20.906,10.766-30.594H232v-48.75c-23.047-9.469-39.328-32.141-39.328-58.563c0-26.438,16.281-49.109,39.328-58.594v-25.344C195.453,156.5,168.672,190.094,168.672,230z M153,421.969c0,4.313,0,5.344-7.344,5.344h-34c-5.719,0-6.875-1.5-10.281-5.969l-0.344-0.438c-3.422-4.734-11.781-32.094-8.219-55.891c0.453-2.984,5.172-12.375,12.844-12.375h36c5.281,0,11.344,1.063,11.344,9.328V421.969z"/>
              <rect x="240" width="32" height="512"/>
            </svg>
            Jail All Players
          </button>
          <button class="nav-btn" data-view="msgplayer">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            </svg>
            Message Players
          </button>
          <button class="nav-btn" data-view="banktransfer">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="1" x2="12" y2="23"/>
              <path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>
            </svg>
            Give Money to Players
          </button>
        </div>
      </nav>

      <div class="sidebar-footer">
        <div class="status-item" id="monitorStatus">
          <span class="conn-dot bypass-inactive" id="monitorDot"></span>
          <span class="status-label" id="monitorLabel">Waiting for FiveM</span>
        </div>
      </div>
    </aside>

    <main class="main">
      <section id="monitor" class="view active">
        <div class="toolbar">
          <div class="toolbar-left">
            <span class="toolbar-title">Traffic</span>
            <span class="toolbar-sub" id="statusText">Ready</span>
          </div>
          <div class="toolbar-right">
            <span class="stat-chip"><strong id="reqCount">0</strong> captured</span>
            <button id="pauseBtn" class="btn btn-ghost" disabled>
              <svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>
              Pause
            </button>
            <button id="exportBtn" class="btn btn-ghost">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/><polyline points="9 15 12 18 15 15"/></svg>
              Export
            </button>
            <button id="clearBtn" class="btn btn-ghost">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
              Clear
            </button>
            <button id="startBtn" class="btn btn-accent">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polygon points="10 8 16 12 10 16 10 8" fill="currentColor" stroke="none"/></svg>
              Start
            </button>
          </div>
        </div>

        <div class="request-list" id="output">
          <div class="empty-state" id="emptyState">
            <div class="empty-icon">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <line x1="18" y1="20" x2="18" y2="10"/>
                <line x1="12" y1="20" x2="12" y2="4"/>
                <line x1="6" y1="20" x2="6" y2="14"/>
              </svg>
            </div>
            <h3>No traffic yet</h3>
            <p>Press Start to monitor traffic from FiveM.</p>
          </div>
        </div>
      </section>

      <section id="executor" class="view">
        <div class="executor-layout">
          <aside class="executor-scripts-rail">
            <div class="executor-scripts-head">
              <span class="executor-scripts-title">Saved Scripts</span>
              <button type="button" id="execNewScriptBtn" class="btn btn-ghost btn-sm btn-icon" title="New script">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
              </button>
            </div>
            <div class="executor-save-box">
              <input id="execScriptName" type="text" placeholder="Script name" autocomplete="off" spellcheck="false">
              <button type="button" id="execSaveScriptBtn" class="btn btn-accent btn-sm">Save</button>
            </div>
            <div class="executor-scripts-list" id="execScriptsList">
              <div class="executor-scripts-empty">No saved scripts yet</div>
            </div>
          </aside>
          <div class="executor-main">
            <div class="editor-wrap">
              <div class="editor-toolbar">
                <div class="editor-toolbar-left">
                  <span class="editor-toolbar-title">Script Editor</span>
                  <span class="editor-toolbar-hint" id="execScriptHint">Ctrl + Enter to run</span>
                </div>
                <div class="editor-toolbar-actions">
                  <span class="editor-lang-badge">JavaScript</span>
                  <button type="button" id="execTestBtn" class="btn btn-warn">Test</button>
                  <button id="execBtn" class="btn btn-accent">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                    Run
                  </button>
                </div>
              </div>
              <div class="code-editor-host" id="execEditorHost">
                <textarea id="execCode"></textarea>
              </div>
            </div>
            <div class="output-panel">
              <div class="output-header">
                <span>Output</span>
                <button id="clearOutputBtn" class="btn btn-ghost btn-sm">Clear</button>
              </div>
              <pre id="execResult">Ready.</pre>
            </div>
          </div>
        </div>
      </section>

      <section id="debug" class="view">
        <div class="debug-layout">
          <aside class="debug-tabs-rail">
            <div class="debug-tabs-head">
              <span class="debug-tabs-title">NUI Targets</span>
              <button type="button" id="debugRefreshBtn" class="btn btn-ghost btn-sm btn-icon" title="Refresh list">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
              </button>
            </div>
            <div class="debug-tabs-sub">Active DevTools tabs and NUI iframes</div>
            <div class="debug-tabs-list" id="debugNuiList">
              <div class="debug-tabs-empty">No NUI targets found</div>
            </div>
          </aside>
          <div class="debug-workspace">
            <div class="debug-target-bar" id="debugTargetBar">Select an NUI target to inspect</div>
            <div class="debug-pane-tabs">
              <button type="button" class="debug-pane-tab active" data-debug-pane="html">HTML</button>
              <button type="button" class="debug-pane-tab" data-debug-pane="js">JavaScript</button>
              <button type="button" class="debug-pane-tab" data-debug-pane="inject">Inject</button>
            </div>
            <div class="debug-panes">
              <div class="debug-pane active" data-debug-pane="html">
                <div class="debug-editor-host" id="debugHtmlHost"><textarea id="debugHtmlCode"></textarea></div>
              </div>
              <div class="debug-pane" data-debug-pane="js">
                <div class="debug-editor-host" id="debugJsHost"><textarea id="debugJsCode"></textarea></div>
              </div>
              <div class="debug-pane" data-debug-pane="inject">
                <div class="debug-editor-host" id="debugInjectHost"><textarea id="debugInjectCode"></textarea></div>
              </div>
            </div>
            <div class="debug-actions">
              <div class="debug-actions-left">
                <button type="button" id="debugDumpBtn" class="btn btn-ghost" disabled>Refresh Dump</button>
              </div>
              <div class="debug-actions-right">
                <button type="button" id="debugTestBtn" class="btn btn-warn" disabled>Test</button>
                <button type="button" id="debugInjectBtn" class="btn btn-accent" disabled>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                  Inject
                </button>
              </div>
            </div>
            <pre class="debug-result" id="debugResult">Ready.</pre>
          </div>
        </div>
      </section>

      <section id="blocker" class="view">
        <div class="toolbar">
          <div class="toolbar-left">
            <span class="toolbar-title">URL Blocker</span>
            <span class="toolbar-sub" id="blockerStatusText">Block network requests by URL</span>
          </div>
        </div>
        <div class="blocker-layout">
          <div class="blocker-add">
            <input id="blockerInput" type="text" placeholder="example.com/path" autocomplete="off">
            <button id="blockerAddBtn" class="btn btn-accent">Add</button>
          </div>
          <div class="blocker-stats" id="blockerStats">0 blocked requests</div>
          <div class="blocker-list" id="blockerList">
            <div class="blocker-empty">No blocked URLs yet</div>
          </div>
        </div>
      </section>

      <section id="pullall" class="view">
        <div class="toolbar">
          <div class="toolbar-left">
            <span class="toolbar-title">Pull Players</span>
            <span class="toolbar-sub" id="pullStatusText">Enter IDs, then start</span>
          </div>
          <div class="toolbar-right">
            <button id="pullStopBtn" class="btn btn-ghost" disabled>Stop</button>
            <button id="pullStartBtn" class="btn btn-accent">
              <svg viewBox="0 0 32 32" fill="currentColor"><path d="M30.052 8.772l-0.337-0.659c-0.372-0.728-1.263-1.018-1.992-0.646l-5.919 3.022c-0.316 0.161-0.549 0.42-0.681 0.721-0.078 0.102-0.143 0.215-0.194 0.34l-1.451 3.571-0.514-0.733-3.122-5.355v-6.029c0-0.818-0.663-1.481-1.48-1.481h-0.74c-0.817 0-1.48 0.663-1.48 1.481v6.649c0 0.338 0.113 0.649 0.303 0.898 0.009 0.017 0.018 0.034 0.028 0.050l2.671 4.581-4.581 3.215 0.381-3.049c0.065-0.516-0.145-1.002-0.513-1.315l-4.141-5.117c-0.514-0.636-1.446-0.734-2.082-0.219l-0.575 0.466c-0.635 0.514-0.733 1.447-0.219 2.083l3.736 4.618-0.703 5.623c-0.036 0.285 0.013 0.562 0.125 0.805 0.040 0.192 0.118 0.379 0.238 0.549l2.55 3.637c0.021 0.029 0.043 0.058 0.065 0.085 0.126 0.367 0.394 0.684 0.773 0.862l6.021 2.815c0.015 0.007 0.031 0.014 0.046 0.020 0.493 0.336 1.163 0.352 1.681-0.010l5.447-3.809c0.67-0.469 0.834-1.392 0.365-2.062l-0.424-0.607c-0.468-0.67-1.391-0.834-2.061-0.366l-4.38 3.063-2.967-1.387 7.189-5.044c0.174-0.122 0.313-0.274 0.416-0.445 0.135-0.135 0.246-0.299 0.322-0.487l2.3-5.658 5.253-2.682c0.728-0.372 1.017-1.264 0.645-1.992zM8.499 27.249c0 2.071-1.679 3.75-3.75 3.75s-3.75-1.679-3.75-3.75c0-2.071 1.679-3.75 3.75-3.75s3.75 1.679 3.75 3.75z"/></svg>
              Start
            </button>
          </div>
        </div>

        <div class="pull-layout">
          <div class="pull-body">
            <div class="pull-panel pull-ids-panel">
              <div class="pull-panel-head">
                <span class="case-preserve">IDs</span>
              </div>
              <textarea id="pullIds" class="pull-textarea pull-textarea-ids" placeholder="123&#10;456&#10;789"></textarea>
            </div>
            <div class="pull-panel">
              <div class="pull-panel-head">Log</div>
              <div class="pull-log" id="pullLog">
                <div class="line dim">Ready when you are.</div>
              </div>
            </div>
          </div>
          <div class="pull-status-bar">
            <span class="pull-status-label">Status</span>
            <span class="pull-status-value" id="pullPhase">Idle</span>
            <span class="pull-progress" id="pullProgress"></span>
          </div>
        </div>
      </section>

      <section id="banplayer" class="view">
        <div class="toolbar">
          <div class="toolbar-left">
            <span class="toolbar-title">Ban / Kick Players</span>
            <span class="toolbar-sub" id="banStatusText">Enter IDs, then start</span>
          </div>
          <div class="toolbar-right">
            <button id="banStopBtn" class="btn btn-ghost" disabled>Stop</button>
            <button id="banStartBtn" class="btn btn-accent">
              <svg viewBox="0 0 32 32" fill="currentColor"><g transform="translate(-310.001 -321.695)"><path d="M326,321.7a16,16,0,1,0,16,16A16,16,0,0,0,326,321.7Zm0,28a12,12,0,1,1,12-12A12,12,0,0,1,326,349.7Z"/><rect width="28.969" height="4" transform="translate(314.348 346.523) rotate(-45.001)"/></g></svg>
              Start
            </button>
          </div>
        </div>

        <div class="pull-layout">
          <div class="pull-body">
            <div class="pull-panel pull-ids-panel">
              <div class="pull-panel-head">
                <span class="case-preserve">IDs</span>
              </div>
              <textarea id="banIds" class="pull-textarea pull-textarea-ids" placeholder="123&#10;456&#10;789"></textarea>
              <div class="pull-ban-text-wrap">
                <div class="pull-panel-head"><span>Reason</span></div>
                <textarea id="banText" class="pull-textarea pull-textarea-ban">vjmi banned you.</textarea>
              </div>
            </div>
            <div class="pull-panel">
              <div class="pull-panel-head">Log</div>
              <div class="pull-log" id="banLog">
                <div class="line dim">Ready when you are.</div>
              </div>
            </div>
          </div>
          <div class="pull-status-bar">
            <span class="pull-status-label">Status</span>
            <span class="pull-status-value" id="banPhase">Idle</span>
            <span class="pull-progress" id="banProgress"></span>
          </div>
        </div>
      </section>

      <section id="jailplayer" class="view">
        <div class="toolbar">
          <div class="toolbar-left">
            <span class="toolbar-title">Jail All Players</span>
            <span class="toolbar-sub" id="jailStatusText">Enter IDs, then start</span>
          </div>
          <div class="toolbar-right">
            <button id="jailStopBtn" class="btn btn-ghost" disabled>Stop</button>
            <button id="jailStartBtn" class="btn btn-accent">
              <svg viewBox="0 0 512 512" fill="currentColor"><path d="M406.344,336.641c-1.766,0-3.953,0-6.344,0V0h-32v336.844c-18.141,1.063-25,13.641-25,25.125c0,1.547,0,6.031,0,11.906c-4.875-29.281-17.125-55.906-32.422-75.766c19.953-16.016,32.766-40.547,32.766-68.109c0-39.906-26.797-73.5-63.344-83.922v25.328c23.063,9.484,39.344,32.156,39.344,58.594c0,26.422-16.281,49.094-39.344,58.563v48.75h26.531c4.594,9.703,8.266,20.016,10.781,30.594H280v68.156h41.344v29.172H280V512h65.344c0,0,0-27.375,0-79.094c3.453,7.031,10.719,10.406,21,10.406c0.469,0,1.094,0,1.656,0V512h32v-68.688c0.109,0,0.219,0,0.344,0c14,0,19.188-7.281,23.344-12.672c6.656-8.672,15.313-41.328,11.313-68C433.609,353.313,423.688,336.641,406.344,336.641z M410.969,420.906l-0.344,0.438c-3.406,4.469-4.563,5.969-10.281,5.969h-34c-7.344,0-7.344-1.031-7.344-5.344v-60c0-8.266,6.063-9.328,11.344-9.328h36c7.688,0,12.406,9.391,12.844,12.375C422.75,388.813,414.406,416.172,410.969,420.906z"/><rect x="480" width="32" height="512"/><rect width="32" height="512"/><path d="M168.672,230c0,27.563,12.797,52.094,32.75,68.094c-15.297,19.875-27.547,46.5-32.422,75.766c0-5.859,0-10.344,0-11.891c0-11.484-6.859-24.063-25-25.125V0h-32v336.641c-2.391,0-4.578,0-6.344,0c-17.328,0-27.266,16.672-28.656,26c-4,26.672,4.656,59.328,11.328,68c4.141,5.391,9.328,12.672,23.328,12.672c0.125,0,0.234,0,0.344,0V512h32v-68.688c0.563,0,1.188,0,1.656,0c10.297,0,17.563-3.375,21.016-10.406c0,51.719,0,79.094,0,79.094H232v-46.766h-41.328v-29.172H232v-68.156h-37.297c2.5-10.578,6.172-20.906,10.766-30.594H232v-48.75c-23.047-9.469-39.328-32.141-39.328-58.563c0-26.438,16.281-49.109,39.328-58.594v-25.344C195.453,156.5,168.672,190.094,168.672,230z M153,421.969c0,4.313,0,5.344-7.344,5.344h-34c-5.719,0-6.875-1.5-10.281-5.969l-0.344-0.438c-3.422-4.734-11.781-32.094-8.219-55.891c0.453-2.984,5.172-12.375,12.844-12.375h36c5.281,0,11.344,1.063,11.344,9.328V421.969z"/><rect x="240" width="32" height="512"/></svg>
              Start
            </button>
          </div>
        </div>

        <div class="pull-layout">
          <div class="pull-body">
            <div class="pull-panel pull-ids-panel">
              <div class="pull-panel-head">
                <span class="case-preserve">IDs</span>
              </div>
              <textarea id="jailIds" class="pull-textarea pull-textarea-ids" placeholder="123&#10;456&#10;789"></textarea>
              <div class="pull-ban-fields">
                <div class="pull-ban-text-wrap">
                  <div class="pull-panel-head"><span>Time</span></div>
                  <textarea id="jailTime" class="pull-textarea pull-textarea-ban pull-textarea-jail-time" placeholder="60"></textarea>
                </div>
                <div class="pull-ban-text-wrap">
                  <div class="pull-panel-head"><span>Reason</span></div>
                  <textarea id="jailMsg" class="pull-textarea pull-textarea-ban">Jailed by Arya</textarea>
                </div>
              </div>
            </div>
            <div class="pull-panel">
              <div class="pull-panel-head">Log</div>
              <div class="pull-log" id="jailLog">
                <div class="line dim">Ready when you are.</div>
              </div>
            </div>
          </div>
          <div class="pull-status-bar">
            <span class="pull-status-label">Status</span>
            <span class="pull-status-value" id="jailPhase">Idle</span>
            <span class="pull-progress" id="jailProgress"></span>
          </div>
        </div>
      </section>

      <section id="msgplayer" class="view">
        <div class="toolbar">
          <div class="toolbar-left">
            <span class="toolbar-title">Message Players</span>
            <span class="toolbar-sub" id="msgStatusText">Enter IDs, then start</span>
          </div>
          <div class="toolbar-right">
            <button id="msgStopBtn" class="btn btn-ghost" disabled>Stop</button>
            <button id="msgStartBtn" class="btn btn-accent">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
              Start
            </button>
          </div>
        </div>

        <div class="pull-layout">
          <div class="pull-body">
            <div class="pull-panel pull-ids-panel">
              <div class="pull-panel-head">
                <span class="case-preserve">IDs</span>
              </div>
              <textarea id="msgIds" class="pull-textarea pull-textarea-ids" placeholder="123&#10;456&#10;789"></textarea>
              <div class="pull-ban-text-wrap">
                <div class="pull-panel-head"><span>Message Text</span></div>
                <textarea id="msgText" class="pull-textarea pull-textarea-ban">Hello from Arya FiveM Tool</textarea>
              </div>

            </div>
            <div class="pull-panel">
              <div class="pull-panel-head">Log</div>
              <div class="pull-log" id="msgLog">
                <div class="line dim">Ready when you are.</div>
              </div>
            </div>
          </div>
          <div class="pull-status-bar">
            <span class="pull-status-label">Status</span>
            <span class="pull-status-value" id="msgPhase">Idle</span>
            <span class="pull-progress" id="msgProgress"></span>
          </div>
        </div>
      </section>

      <section id="banktransfer" class="view">
        <div class="toolbar">
          <div class="toolbar-left">
            <span class="toolbar-title">Give Money to Players</span>
            <span class="toolbar-sub" id="bankStatusText">Enter player ID and amount, then start</span>
          </div>
          <div class="toolbar-right">
            <button id="bankStopBtn" class="btn btn-ghost" disabled>Stop</button>
            <button id="bankStartBtn" class="btn btn-accent">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
              Start
            </button>
          </div>
        </div>

        <div class="pull-layout">
          <div class="pull-body">
            <div class="pull-panel pull-ids-panel">
              <div class="pull-panel-head">
                <span class="case-preserve">VRP ID</span>
              </div>
              <textarea id="bankIds" class="pull-textarea pull-textarea-ids" placeholder="123&#10;456&#10;789"></textarea>
              <div class="pull-ban-text-wrap">
                <div class="pull-panel-head"><span>Amount</span></div>
                <textarea id="bankAmount" class="pull-textarea pull-textarea-ban" placeholder="1000"></textarea>
              </div>

            </div>
            <div class="pull-panel">
              <div class="pull-panel-head">Log</div>
              <div class="pull-log" id="bankLog">
                <div class="line dim">Ready when you are.</div>
              </div>
            </div>
          </div>
          <div class="pull-status-bar">
            <span class="pull-status-label">Status</span>
            <span class="pull-status-value" id="bankPhase">Idle</span>
            <span class="pull-progress" id="bankProgress"></span>
          </div>
        </div>
      </section>
    </main>
  </div>

  <div class="toast-stack" id="toastStack"></div>

  <div class="pull-modal" id="pullModal">
    <div class="pull-modal-box">
      <p id="pullModalText">! اضغط زر السحب</p>
      <button id="pullModalClose" class="btn btn-ghost">Close</button>
    </div>
  </div>

  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/javascript/javascript.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/xml/xml.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/htmlmixed/htmlmixed.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/edit/matchbrackets.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/edit/closebrackets.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/selection/active-line.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/fold/foldcode.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/fold/foldgutter.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/fold/brace-fold.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/search.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/searchcursor.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/jump-to-line.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/dialog/dialog.min.js"></script>

  <script>
    const API = window.location.origin;

    async function apiFetch(path, opts = {}) {
      const options = Object.assign({ credentials: 'same-origin' }, opts);
      if (options.body != null && typeof options.body !== 'string') {
        options.body = JSON.stringify(options.body);
        options.headers = Object.assign({}, options.headers || {}, { 'Content-Type': 'application/json' });
      }
      return fetch(`${API}${path}`, options);
    }

    async function parseApiJson(res) {
      const text = await res.text();
      if (!text) return {};
      try {
        return JSON.parse(text);
      } catch (_) {
        if (text.trim().toLowerCase().startsWith('<!doctype') || text.trim().startsWith('<')) {
          throw new Error(`API error (HTTP ${res.status}): server returned HTML. Restart the tool and try again.`);
        }
        throw new Error(`API error (HTTP ${res.status}): invalid JSON response`);
      }
    }

    const VRP_WARMUP_IDS = ['0', '00', '000'];
    const VRP_WARMUP_COUNT = VRP_WARMUP_IDS.length;

    function vrpWarmupIdsScriptLiteral() {
      return JSON.stringify(VRP_WARMUP_IDS);
    }

    const EDITOR_PLACEHOLDER = `fetch("https://melix_chat/chatResult", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    "message": "Hello from Arya Tool"
  })
});`;

    function nuiNameFromUrl(url) {
      const raw = String(url || '').trim();
      if (!raw) return '';
      const match = raw.match(/^nui:\/\/([^/?#]+)/i);
      if (match) return match[1];
      const parts = raw.replace(/^nui:\/\//i, '').split('/').filter(Boolean);
      return parts[0] || '';
    }

    function buildDebugInjectAlert(target) {
      let name = 'NUI';
      if (target) {
        const fromUrl = nuiNameFromUrl(target.url);
        if (fromUrl) name = fromUrl;
        else {
          const title = String(target.title || '').trim();
          if (title) name = title;
        }
      }
      const safe = name.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\r?\n/g, ' ');
      return `alert();`;
    }

    const TEST_INJECTION_CODE = "alert('vjmi test');";

    const IDE_EDITOR_DEFAULTS = {
      theme: 'fivem-editor',
      lineNumbers: true,
      lineWrapping: true,
      tabSize: 2,
      indentUnit: 2,
      indentWithTabs: false,
      matchBrackets: true,
      autoCloseBrackets: true,
      styleActiveLine: true,
      scrollbarStyle: 'native',
      foldGutter: false,
      gutters: ['CodeMirror-linenumbers'],
    };

    function createIdeEditor(textarea, options = {}) {
      if (!textarea || typeof CodeMirror === 'undefined') return null;
      const opts = Object.assign({}, IDE_EDITOR_DEFAULTS, options);
      try {
        return CodeMirror.fromTextArea(textarea, opts);
      } catch (err) {
        console.warn('CodeMirror init failed', err);
        return null;
      }
    }

    function bindIdeTabKey(editor, onRun) {
      if (!editor || !onRun) return;
      editor.setOption('extraKeys', Object.assign({}, editor.getOption('extraKeys') || {}, {
        'Ctrl-Enter': onRun,
        'Cmd-Enter': onRun,
        Tab: (cm) => {
          if (cm.somethingSelected()) cm.indentSelection('add');
          else cm.replaceSelection('  ', 'end');
        },
      }));
    }

    const ICONS = {
      chevron: `<svg class="req-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>`,
    };

    function toHttps(url) {
      return url.replace(/^http:\/\//i, 'https://');
    }

    function isHiddenTraffic(url) {
      if (!url) return false;
      const low = String(url).toLowerCase();
      const local = low.includes('127.0.0.1') || low.includes('localhost');
      if (!local) return false;
      const onGamePort = low.includes(':30120') || low.includes(':40120');
      if (!onGamePort) return false;
      return /\.json(\?|$|")/i.test(low);
    }

    function formatTime(ts) {
      const d = new Date(ts);
      const p = (n, l = 2) => String(n).padStart(l, '0');
      return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
    }

    function setTextIfChanged(el, value) {
      if (!el) return;
      const next = value == null ? '' : String(value);
      if (el.textContent !== next) el.textContent = next;
    }

    function fitReqEditor(editor) {
      if (!editor) return;
      editor.style.height = 'auto';
      const lines = String(editor.value || '').split('\n').length;
      editor.rows = Math.max(3, lines);
      editor.style.height = '0';
      editor.style.height = `${Math.max(editor.scrollHeight, 72)}px`;
    }

    const MAX_MONITOR_CARDS = 400;
    const MONITOR_BATCH_SIZE = 24;
    const MONITOR_ROW_STRIDE = 58;
    const MONITOR_WINDOW_PAD = 12;
    const MONITOR_AUTO_COLLAPSE_CHARS = 320;

    function shouldAutoCollapseFetch(code) {
      return String(code || '').length > MONITOR_AUTO_COLLAPSE_CHARS;
    }

    function fetchHasMoneyKeywords(text) {
      const low = String(text || '').toLowerCase();
      return low.includes('amount') || low.includes('price') || low.includes('money');
    }

    class App {
      constructor() {
        this.ws = null;
        this.blockedUrls = new Set();
        this.paused = false;
        this.reqCount = 0;
        this.live = false;
        this.cardsByRequestId = new Map();
        this.pendingPostFetches = new Map();
        this.cdpMsgId = 10000;
        this.requests = [];
        this._fetchQueue = [];
        this._fetchFlushScheduled = false;
        this.requestUrlsById = new Map();
        this._blockerRefreshTimer = null;
        this.renderedIndexes = new Map();
        this.collapsedRequestKeys = new Set();
        this._monitorRenderScheduled = false;
        this.activeScriptId = null;
        this.debugTarget = null;
        this.debugEditors = { html: null, js: null, inject: null };
        this.pullAll = {
          running: false,
          stop: false,
          templateWaiter: null,
          templateTimer: null,
          templateReject: null,
          loggingPulls: false,
          pulledCount: 0,
          expectedCount: 0,
          userCount: 0,
          testCount: 0,
          userIdQueue: [],
        };
        this.banPlayer = {
          running: false,
          stop: false,
          templateWaiter: null,
          templateTimer: null,
          templateReject: null,
          loggingBans: false,
          bannedCount: 0,
          expectedCount: 0,
          userCount: 0,
          testCount: 0,
          userIdQueue: [],
          banText: '',
        };
        this.jailPlayer = {
          running: false,
          stop: false,
          templateWaiter: null,
          templateTimer: null,
          templateReject: null,
          loggingJails: false,
          jailedCount: 0,
          expectedCount: 0,
          userCount: 0,
          testCount: 0,
          userIdQueue: [],
          jailTime: '',
          jailMsg: '',
        };
        this.msgPlayer = {
          running: false,
          stop: false,
          templateWaiter: null,
          templateTimer: null,
          templateReject: null,
          loggingMsgs: false,
          sentCount: 0,
          expectedCount: 0,
          userCount: 0,
          testCount: 0,
          idQueue: [],
          msgText: '',
        };
        this.bankTransfer = {
          running: false,
          stop: false,
          templateWaiter: null,
          templateTimer: null,
          templateReject: null,
          loggingTransfers: false,
          sentCount: 0,
          expectedCount: 0,
          userCount: 0,
          testCount: 0,
          idQueue: [],
          amountText: '',
        };
        this.els = {
          output: document.getElementById('output'),
          emptyState: document.getElementById('emptyState'),
          toastStack: document.getElementById('toastStack'),
          pauseBtn: document.getElementById('pauseBtn'),
          execResult: document.getElementById('execResult'),
          statusText: document.getElementById('statusText'),
          reqCount: document.getElementById('reqCount'),
          monitorDot: document.getElementById('monitorDot'),
          monitorLabel: document.getElementById('monitorLabel'),
          monitorStatus: document.getElementById('monitorStatus'),
        };

        this.bindEvents();
        this.initMonitorList();
        this.initCodeEditor();
        this.initDebugTab();
        this.initExecutorScripts();
        this.initBlockerUi();
        this.initVrpIdPlaceholders();
      }

      initVrpIdPlaceholders() {
        const sample = '123\n456\n789';
        ['pullIds', 'banIds', 'jailIds', 'msgIds', 'bankIds'].forEach((id) => {
          const el = document.getElementById(id);
          if (el) el.placeholder = sample;
        });
      }

      initCodeEditor() {
        const textarea = document.getElementById('execCode');
        const host = document.getElementById('execEditorHost');
        if (!textarea || !host) return;

        this.codeEditor = createIdeEditor(textarea, {
          mode: 'javascript',
          extraKeys: {
            'Ctrl-Enter': () => this.runExecutor(),
            'Cmd-Enter': () => this.runExecutor(),
          },
        });
        if (this.codeEditor) {
          bindIdeTabKey(this.codeEditor, () => this.runExecutor());
          host.classList.add('cm-ready');
        } else {
          console.warn('CodeMirror init failed, using plain editor');
          textarea.classList.add('plain-editor');
          textarea.style.display = 'block';
          textarea.addEventListener('keydown', (e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
              e.preventDefault();
              this.runExecutor();
            }
          });
        }

        if (!document.getElementById('execPlaceholder')) {
          const placeholder = document.createElement('pre');
          placeholder.id = 'execPlaceholder';
          placeholder.className = 'editor-placeholder';
          placeholder.setAttribute('aria-hidden', 'true');
          placeholder.textContent = EDITOR_PLACEHOLDER;
          host.appendChild(placeholder);
        }

        const resize = () => {
          if (this.codeEditor) {
            this.codeEditor.setSize('100%', host.clientHeight);
          } else if (textarea.classList.contains('plain-editor')) {
            textarea.style.height = `${Math.max(host.clientHeight, 220)}px`;
          }
        };
        resize();
        window.addEventListener('resize', resize);
        if (this.codeEditor) {
          this.codeEditor.on('change', () => {
            this.syncEditorPlaceholder();
            this.codeEditor.refresh();
          });
        } else {
          textarea.addEventListener('input', () => this.syncEditorPlaceholder());
        }
        this.syncEditorPlaceholder();
        setTimeout(resize, 0);

        document.getElementById('execTestBtn')?.addEventListener('click', () => this.runTestInjection());
      }

      initDebugTab() {
        const htmlTa = document.getElementById('debugHtmlCode');
        const jsTa = document.getElementById('debugJsCode');
        const injectTa = document.getElementById('debugInjectCode');
        const htmlHost = document.getElementById('debugHtmlHost');
        const jsHost = document.getElementById('debugJsHost');
        const injectHost = document.getElementById('debugInjectHost');

        this.debugEditors.html = createIdeEditor(htmlTa, { mode: 'htmlmixed', readOnly: true });
        this.debugEditors.js = createIdeEditor(jsTa, { mode: 'javascript', readOnly: true });
        if (injectTa) injectTa.value = buildDebugInjectAlert(null);
        this.debugEditors.inject = createIdeEditor(injectTa, { mode: 'javascript' });
        bindIdeTabKey(this.debugEditors.inject, () => this.runDebugInject());

        const resizeDebugEditors = () => {
          ['html', 'js', 'inject'].forEach((key) => {
            const editor = this.debugEditors[key];
            const host = key === 'html' ? htmlHost : key === 'js' ? jsHost : injectHost;
            if (editor && host) editor.setSize('100%', host.clientHeight);
          });
        };
        resizeDebugEditors();
        window.addEventListener('resize', resizeDebugEditors);

        document.getElementById('debugRefreshBtn')?.addEventListener('click', () => this.loadDebugNuiTabs(true, true));
        document.getElementById('debugDumpBtn')?.addEventListener('click', () => this.dumpDebugTarget());
        document.getElementById('debugTestBtn')?.addEventListener('click', () => this.runDebugTestInject());
        document.getElementById('debugInjectBtn')?.addEventListener('click', () => this.runDebugInject());

        document.querySelectorAll('.debug-pane-tab').forEach((btn) => {
          btn.addEventListener('click', () => {
            const pane = btn.dataset.debugPane;
            document.querySelectorAll('.debug-pane-tab').forEach((b) => b.classList.toggle('active', b === btn));
            document.querySelectorAll('.debug-pane').forEach((p) => {
              p.classList.toggle('active', p.dataset.debugPane === pane);
            });
            const editor = this.debugEditors[pane];
            if (editor) setTimeout(() => editor.refresh(), 30);
          });
        });

        const list = document.getElementById('debugNuiList');
        list?.addEventListener('click', (e) => {
          const item = e.target.closest('.debug-tab-item');
          if (!item) return;
          const tabId = item.dataset.tabId;
          const tab = (this._debugTabs || []).find((row) => row.id === tabId);
          if (tab) this.selectDebugTarget(tab);
        });
      }

      setDebugResult(text, ok = null) {
        const el = document.getElementById('debugResult');
        if (!el) return;
        el.textContent = text;
        el.style.color = ok === true ? 'var(--success)' : ok === false ? 'var(--error)' : 'var(--text-secondary)';
      }

      updateDebugTargetBar() {
        const bar = document.getElementById('debugTargetBar');
        const dumpBtn = document.getElementById('debugDumpBtn');
        const testBtn = document.getElementById('debugTestBtn');
        const injectBtn = document.getElementById('debugInjectBtn');
        if (!this.debugTarget) {
          if (bar) bar.textContent = 'Select an NUI target to inspect';
          if (dumpBtn) dumpBtn.disabled = true;
          if (testBtn) testBtn.disabled = true;
          if (injectBtn) injectBtn.disabled = true;
          return;
        }
        const t = this.debugTarget;
        if (bar) {
          bar.innerHTML = `<strong>${this.escapeHtml(t.title || 'NUI')}</strong> · ${this.escapeHtml(t.url || '')} · <span class="mono">${t.kind || 'devtools'}</span>`;
        }
        if (dumpBtn) dumpBtn.disabled = false;
        if (testBtn) testBtn.disabled = false;
        if (injectBtn) injectBtn.disabled = false;
      }

      renderDebugNuiTabs(tabs) {
        const list = document.getElementById('debugNuiList');
        if (!list) return;
        this._debugTabs = tabs || [];
        if (!tabs.length) {
          list.innerHTML = '<div class="debug-tabs-empty">No NUI targets found. Join a server and open NUI resources.</div>';
          return;
        }
        list.innerHTML = tabs.map((row) => `
          <div class="debug-tab-item${this.debugTarget?.id === row.id ? ' active' : ''}" data-tab-id="${this.escapeHtml(row.id)}">
            <span class="debug-tab-kind">${this.escapeHtml(row.kind || 'devtools')}</span>
            <strong>${this.escapeHtml(row.title || 'NUI')}</strong>
            <span>${this.escapeHtml(row.url || '')}</span>
          </div>
        `).join('');
      }

      async loadDebugNuiTabs(forceToast = false, redump = false) {
        try {
          const res = await apiFetch('/api/debug/nui-tabs?force=1');
          const data = await parseApiJson(res);
          if (!data.success) throw new Error(data.error || 'Failed to load NUI tabs');
          this.renderDebugNuiTabs(data.tabs || []);
          if (forceToast) this.toast(`${(data.tabs || []).length} NUI target(s)`, 'ok');
          if (this.debugTarget) {
            const still = (data.tabs || []).find((row) => row.id === this.debugTarget.id);
            if (still) {
              this.debugTarget = still;
              if (redump) await this.dumpDebugTarget();
            } else {
              this.debugTarget = null;
              this.updateDebugTargetBar();
            }
          }
        } catch (err) {
          this.setDebugResult(err.message, false);
          if (forceToast) this.toast(err.message, 'error');
        }
      }

      async refreshDebugTab() {
        await this.loadDebugNuiTabs(false, false);
        setTimeout(() => {
          Object.values(this.debugEditors).forEach((ed) => ed && ed.refresh());
        }, 50);
      }

      selectDebugTarget(tab) {
        this.debugTarget = tab;
        document.querySelectorAll('.debug-tab-item').forEach((el) => {
          el.classList.toggle('active', el.dataset.tabId === tab.id);
        });
        if (this.debugEditors.inject) {
          this.debugEditors.inject.setValue(buildDebugInjectAlert(tab));
          this.debugEditors.inject.refresh();
        }
        this.updateDebugTargetBar();
        this.dumpDebugTarget();
      }

      debugInjectTarget() {
        if (!this.debugTarget) return null;
        return {
          ws_url: this.debugTarget.ws_url,
          kind: this.debugTarget.kind || 'devtools',
          iframe_index: Number(this.debugTarget.iframe_index) || 0,
        };
      }

      async runDebugTestInject() {
        if (!this.debugTarget) {
          this.setDebugResult('Select an NUI target first.', false);
          return;
        }
        const code = buildDebugInjectAlert(this.debugTarget);
        if (this.debugEditors.inject) {
          this.debugEditors.inject.setValue(code);
          this.debugEditors.inject.refresh();
        }
        const btn = document.getElementById('debugTestBtn');
        if (btn) btn.disabled = true;
        this.setDebugResult('Running test inject…');
        try {
          await this.injectCode(code, this.debugInjectTarget(), { fast: true });
          this.setDebugResult('Test alert injected.', true);
          this.toast('Test injection OK', 'ok');
        } catch (err) {
          this.setDebugResult(err.message, false);
          this.toast(err.message, 'error', 4000);
        } finally {
          if (btn) btn.disabled = !this.debugTarget;
        }
      }

      async dumpDebugTarget() {
        if (!this.debugTarget) return;
        const btn = document.getElementById('debugDumpBtn');
        if (btn) btn.disabled = true;
        this.setDebugResult('Dumping HTML and JavaScript…');
        try {
          const res = await apiFetch('/api/debug/dump', {
            method: 'POST',
            body: {
              ws_url: this.debugTarget.ws_url,
              kind: this.debugTarget.kind || 'devtools',
              iframe_index: this.debugTarget.iframe_index || 0,
            },
          });
          const data = await parseApiJson(res);
          if (!res.ok && !data.error) throw new Error(`Dump failed (HTTP ${res.status})`);
          if (!data.success) throw new Error(data.error || 'Dump failed');
          if (this.debugEditors.html) {
            this.debugEditors.html.setValue(data.html || '');
            this.debugEditors.html.refresh();
          }
          if (this.debugEditors.js) {
            this.debugEditors.js.setValue(data.js || '');
            this.debugEditors.js.refresh();
          }
          const htmlLen = (data.html || '').length;
          const jsLen = (data.js || '').length;
          this.setDebugResult(`Dump complete · ${htmlLen.toLocaleString()} HTML chars · ${jsLen.toLocaleString()} JS chars`, true);
        } catch (err) {
          this.setDebugResult(err.message, false);
          this.toast(err.message, 'error');
        } finally {
          if (btn) btn.disabled = !this.debugTarget;
        }
      }

      async runDebugInject() {
        if (!this.debugTarget) {
          this.setDebugResult('Select an NUI target first.', false);
          return;
        }
        const editor = this.debugEditors.inject;
        let code = editor ? editor.getValue().trim() : '';
        if (!code) code = buildDebugInjectAlert(this.debugTarget);
        const btn = document.getElementById('debugInjectBtn');
        if (btn) btn.disabled = true;
        this.setDebugResult('Injecting…');
        try {
          await this.injectCode(code, this.debugInjectTarget(), { fast: true });
          this.setDebugResult('Injection complete.', true);
          this.toast('Injected into NUI', 'ok');
        } catch (err) {
          this.setDebugResult(err.message, false);
          this.toast(err.message, 'error', 4000);
        } finally {
          if (btn) btn.disabled = !this.debugTarget;
        }
      }

      async runTestInjection() {
        const btn = document.getElementById('execTestBtn');
        if (btn) btn.disabled = true;
        this.setExecResult('Running test injection…');
        try {
          await this.injectCode(TEST_INJECTION_CODE);
          this.setExecResult('Test alert injected.', true);
          this.toast('Test injection OK', 'ok');
        } catch (err) {
          this.setExecResult(err.message, false);
          this.toast(err.message, 'error', 4000);
        } finally {
          if (btn) btn.disabled = false;
        }
      }

      syncEditorPlaceholder() {
        const placeholder = document.getElementById('execPlaceholder');
        if (!placeholder) return;
        const hasCode = this.codeEditor
          ? !!this.codeEditor.getValue().trim()
          : !!((document.getElementById('execCode') || {}).value || '').trim();
        placeholder.classList.toggle('is-hidden', hasCode);
      }

      getEditorCode() {
        if (this.codeEditor) return this.codeEditor.getValue().trim();
        const el = document.getElementById('execCode');
        return el ? el.value.trim() : '';
      }

      setEditorCode(code) {
        const next = String(code || '');
        if (this.codeEditor) {
          this.codeEditor.setValue(next);
          this.syncEditorPlaceholder();
          this.codeEditor.refresh();
        } else {
          const el = document.getElementById('execCode');
          if (el) el.value = next;
        }
      }

      formatScriptStamp(ts) {
        const n = Number(ts);
        if (!n) return '—';
        const d = new Date(n * 1000);
        if (Number.isNaN(d.getTime())) return '—';
        return d.toLocaleString(undefined, {
          month: 'short',
          day: 'numeric',
          hour: '2-digit',
          minute: '2-digit',
        });
      }

      initExecutorScripts() {
        const saveBtn = document.getElementById('execSaveScriptBtn');
        const newBtn = document.getElementById('execNewScriptBtn');
        const nameInput = document.getElementById('execScriptName');
        const list = document.getElementById('execScriptsList');

        if (saveBtn) saveBtn.addEventListener('click', () => this.saveExecutorScript());
        if (newBtn) newBtn.addEventListener('click', () => this.newExecutorScript());
        if (nameInput) {
          nameInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') this.saveExecutorScript();
          });
        }
        if (list) {
          list.addEventListener('click', (e) => {
            const delBtn = e.target.closest('[data-act="delete-script"]');
            if (delBtn) {
              e.stopPropagation();
              const item = delBtn.closest('.executor-script-item');
              const id = item?.dataset?.scriptId;
              if (id) this.deleteExecutorScript(id);
              return;
            }
            const item = e.target.closest('.executor-script-item');
            const id = item?.dataset?.scriptId;
            if (id) this.loadExecutorScript(id);
          });
        }

        this.loadExecutorScripts();
      }

      updateExecutorScriptHint() {
        const hint = document.getElementById('execScriptHint');
        if (!hint) return;
        const nameInput = document.getElementById('execScriptName');
        const name = (nameInput?.value || '').trim();
        if (this.activeScriptId && name) {
          hint.textContent = `Editing · ${name}`;
        } else {
          hint.textContent = 'Ctrl + Enter to run';
        }
      }

      newExecutorScript() {
        this.activeScriptId = null;
        const nameInput = document.getElementById('execScriptName');
        if (nameInput) nameInput.value = '';
        this.setEditorCode('');
        this.setExecResult('Ready.');
        this.updateExecutorScriptHint();
        document.querySelectorAll('.executor-script-item.active').forEach((el) => {
          el.classList.remove('active');
        });
      }

      renderExecutorScripts(scripts) {
        const list = document.getElementById('execScriptsList');
        if (!list) return;
        if (!scripts.length) {
          list.innerHTML = '<div class="executor-scripts-empty">No saved scripts yet</div>';
          return;
        }
        list.innerHTML = scripts.map((row) => `
          <div class="executor-script-item${row.id === this.activeScriptId ? ' active' : ''}" data-script-id="${row.id}">
            <div class="executor-script-copy">
              <strong>${this.escapeHtml(row.name || 'Untitled')}</strong>
              <span>${this.formatScriptStamp(row.updatedAt)} · ${row.chars || 0} chars</span>
            </div>
            <button type="button" class="btn btn-ghost btn-sm btn-icon executor-script-del" data-act="delete-script" title="Delete">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>
            </button>
          </div>
        `).join('');
      }

      async loadExecutorScripts() {
        try {
          const res = await apiFetch('/api/executor/scripts');
          const data = await res.json();
          if (!data.success) return;
          this.renderExecutorScripts(data.scripts || []);
        } catch (_) {}
      }

      async saveExecutorScript() {
        const code = this.getEditorCode();
        if (!code) {
          this.toast('Script is empty', 'error');
          return;
        }
        const nameInput = document.getElementById('execScriptName');
        const name = (nameInput?.value || '').trim() || 'Untitled';
        try {
          const res = await apiFetch('/api/executor/scripts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              id: this.activeScriptId,
              name,
              code,
            }),
          });
          const data = await res.json();
          if (!data.success) throw new Error(data.error || 'Save failed');
          const script = data.script || {};
          this.activeScriptId = script.id || this.activeScriptId;
          if (nameInput) nameInput.value = script.name || name;
          this.renderExecutorScripts(data.scripts || []);
          this.updateExecutorScriptHint();
          this.toast('Script saved', 'ok');
        } catch (err) {
          this.toast(err.message || 'Save failed', 'error');
        }
      }

      async loadExecutorScript(scriptId) {
        try {
          const res = await apiFetch(`/api/executor/scripts/${encodeURIComponent(scriptId)}`);
          const data = await res.json();
          if (!data.success || !data.script) throw new Error(data.error || 'Load failed');
          const script = data.script;
          this.activeScriptId = script.id;
          const nameInput = document.getElementById('execScriptName');
          if (nameInput) nameInput.value = script.name || '';
          this.setEditorCode(script.code || '');
          this.setExecResult('Loaded.');
          this.updateExecutorScriptHint();
          document.querySelectorAll('.executor-script-item').forEach((el) => {
            el.classList.toggle('active', el.dataset.scriptId === script.id);
          });
          if (this.codeEditor) setTimeout(() => this.codeEditor.refresh(), 0);
        } catch (err) {
          this.toast(err.message || 'Load failed', 'error');
        }
      }

      async deleteExecutorScript(scriptId) {
        try {
          const res = await apiFetch('/api/executor/scripts/remove', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: scriptId }),
          });
          const data = await res.json();
          if (!data.success) throw new Error(data.error || 'Delete failed');
          if (this.activeScriptId === scriptId) this.activeScriptId = null;
          this.renderExecutorScripts(data.scripts || []);
          this.updateExecutorScriptHint();
          this.toast('Script deleted', 'ok');
        } catch (err) {
          this.toast(err.message || 'Delete failed', 'error');
        }
      }

      initFromBootstrap(data) {
        if (!data) return;
        const statusMessage = data.bypass_message || data.message || 'Waiting for FiveM';
        const monitoring = !!data.bypass_active;
        this.setMonitoring(monitoring, monitoring ? 'Monitoring' : statusMessage);
      }

      setMonitoring(active, message) {
        const { monitorDot, monitorLabel, monitorStatus } = this.els;
        if (!monitorDot || !monitorLabel || !monitorStatus) return;
        monitorDot.className = 'conn-dot ' + (active ? 'bypass-active' : 'bypass-inactive');
        const label = active ? 'Monitoring' : (message || 'Waiting for FiveM');
        setTextIfChanged(monitorLabel, label);
        monitorStatus.classList.toggle('monitoring-active', active);
      }

      async pollStatus() {
        try {
          const res = await apiFetch('/api/status');
          if (!res.ok) return;
          const data = await res.json();
          const monitoring = !!data.bypass_active;
          const label = monitoring
            ? 'Monitoring'
            : (data.bypass_message || data.message || 'Waiting for FiveM');
          this.setMonitoring(monitoring, label);
        } catch {
        }
      }

      startStatusPolling() {
        if (this._statusPollTimer) return;
        this.pollStatus();
        this._statusPollTimer = setInterval(() => this.pollStatus(), 1200);
      }

      toast(msg, type = '', ms = 2800) {
        if (!this.els.toastStack) return;
        const el = document.createElement('div');
        el.className = 'toast' + (type ? ' ' + type : '');
        el.textContent = msg;
        this.els.toastStack.appendChild(el);
        setTimeout(() => {
          el.style.animation = 'toastOut 0.2s ease forwards';
          setTimeout(() => el.remove(), 200);
        }, ms);
      }

      setConn(state) {
        const connDot = this.els.connDot;
        const connLabel = this.els.connLabel;
        if (!connDot || !connLabel) return;
        connDot.className = 'conn-dot' + (state === 'live' ? ' live' : state === 'paused' ? ' paused' : '');
        connLabel.textContent = { idle: 'Idle', live: 'Monitoring', paused: 'Paused', error: 'Disconnected' }[state] || state;
      }

      setExecResult(text, ok) {
        const el = this.els.execResult;
        if (!el) return;
        el.textContent = text;
        el.className = ok === true ? 'ok' : ok === false ? 'err' : '';
      }

      updateReqCount() {
        this.els.reqCount.textContent = this.reqCount;
      }

      hideEmpty() {
        if (this.els.emptyState) this.els.emptyState.hidden = true;
      }

      showEmpty() {
        if (this.requests.length) {
          this.hideEmpty();
          return;
        }
        if (!this.els.emptyState) {
          const div = document.createElement('div');
          div.className = 'empty-state';
          div.id = 'emptyState';
          div.innerHTML = `
            <div class="empty-icon">
              <svg viewBox="0 0 24 24" fill="none" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" stroke="currentColor">
                <line x1="18" y1="20" x2="18" y2="10"/>
                <line x1="12" y1="20" x2="12" y2="4"/>
                <line x1="6" y1="20" x2="6" y2="14"/>
              </svg>
            </div>
            <h3>No traffic yet</h3>
            <p>Press Start to monitor traffic from FiveM.</p>`;
          this.els.output.appendChild(div);
          this.els.emptyState = div;
        }
        this.els.emptyState.hidden = false;
      }

      initMonitorList() {
        const output = this.els.output;
        this.monitorRowStride = MONITOR_ROW_STRIDE;
        this.monitorWindowPad = MONITOR_WINDOW_PAD;

        this.monitorSpacer = document.createElement('div');
        this.monitorSpacer.className = 'monitor-spacer';

        this.monitorStage = document.createElement('div');
        this.monitorStage.className = 'monitor-stage';

        const empty = this.els.emptyState;
        output.innerHTML = '';
        output.appendChild(this.monitorSpacer);
        output.appendChild(this.monitorStage);
        if (empty) output.appendChild(empty);

        output.addEventListener('scroll', () => this.scheduleMonitorRender(), { passive: true });
      }

      scheduleMonitorRender() {
        if (this._monitorRenderScheduled) return;
        this._monitorRenderScheduled = true;
        requestAnimationFrame(() => {
          this._monitorRenderScheduled = false;
          this.renderMonitorWindow();
        });
      }

      reqOpenKey(entry, index) {
        return entry?.requestId ? String(entry.requestId) : `i:${index}`;
      }

      entryStride(entry, index) {
        const key = this.reqOpenKey(entry, index);
        if (this.collapsedRequestKeys.has(key)) return MONITOR_ROW_STRIDE;
        const lines = String(entry.fetch || '').split('\n').length;
        return Math.max(160, Math.min(480, 96 + lines * 19));
      }

      buildMonitorOffsets() {
        const offsets = new Array(this.requests.length + 1);
        offsets[0] = 0;
        for (let i = 0; i < this.requests.length; i++) {
          offsets[i + 1] = offsets[i] + this.entryStride(this.requests[i], i);
        }
        return offsets;
      }

      findMonitorIndex(offsets, y) {
        let lo = 0;
        let hi = this.requests.length;
        while (lo < hi) {
          const mid = (lo + hi) >> 1;
          if (offsets[mid] <= y) lo = mid + 1;
          else hi = mid;
        }
        return Math.max(0, lo - 1);
      }

      clearMonitorDom() {
        this.renderedIndexes.clear();
        this.cardsByRequestId.clear();
        if (this.monitorStage) this.monitorStage.innerHTML = '';
        if (this.monitorSpacer) this.monitorSpacer.style.height = '0';
      }

      renderMonitorWindow() {
        if (!this.monitorStage || !this.monitorSpacer) return;

        if (!this.requests.length) {
          this.clearMonitorDom();
          this.showEmpty();
          return;
        }

        this.hideEmpty();

        const list = this.els.output;
        const offsets = this.buildMonitorOffsets();
        const totalHeight = offsets[this.requests.length] || 0;
        const scrollTop = list.scrollTop;
        const viewH = list.clientHeight || 480;
        const start = Math.max(0, this.findMonitorIndex(offsets, scrollTop) - this.monitorWindowPad);
        const end = Math.min(
          this.requests.length,
          this.findMonitorIndex(offsets, scrollTop + viewH) + 1 + this.monitorWindowPad
        );

        this.monitorSpacer.style.height = `${totalHeight}px`;
        this.monitorStage.style.transform = `translateY(${offsets[start]}px)`;

        for (const [idx, card] of this.renderedIndexes) {
          if (idx >= start && idx < end) continue;
          const key = card.dataset.openKey;
          if (key && !this.collapsedRequestKeys.has(key)) continue;
          card.remove();
          this.renderedIndexes.delete(idx);
          if (card.dataset.requestId) this.cardsByRequestId.delete(card.dataset.requestId);
        }

        const fragment = document.createDocumentFragment();
        for (let i = start; i < end; i++) {
          if (this.renderedIndexes.has(i)) continue;
          const entry = this.requests[i];
          const card = this.createReqRow(entry, i);
          const openKey = this.reqOpenKey(entry, i);
          if (!this.collapsedRequestKeys.has(openKey)) {
            card.classList.add('open');
            this.mountReqBody(card, entry);
            requestAnimationFrame(() => fitReqEditor(card.querySelector('.req-code-edit')));
          }
          fragment.appendChild(card);
          this.renderedIndexes.set(i, card);
          if (entry.requestId) this.cardsByRequestId.set(entry.requestId, card);
        }
        if (fragment.childNodes.length) this.monitorStage.appendChild(fragment);
      }

      toggleReqCard(card) {
        if (!card) return;
        const opening = !card.classList.contains('open');
        const idx = Number(card.dataset.listIndex);
        const entry = this.requests[idx];
        const openKey = card.dataset.openKey || this.reqOpenKey(entry, idx);

        if (opening) {
          card.classList.add('open');
          this.collapsedRequestKeys.delete(openKey);
          if (!card.querySelector('.req-body') && entry) this.mountReqBody(card, entry);
          requestAnimationFrame(() => {
            fitReqEditor(card.querySelector('.req-code-edit'));
            this.scheduleMonitorRender();
          });
        } else {
          card.classList.remove('open');
          this.collapsedRequestKeys.add(openKey);
          this.scheduleMonitorRender();
        }
      }

      mountReqBody(card, entry) {
        if (!card || card.querySelector('.req-body') || !entry) return;

        const fullFetch = String(entry.fetch || '');
        const urlMatch = fullFetch.match(/fetch\("([^"]+)"/);

        const body = document.createElement('div');
        body.className = 'req-body';

        const codeWrap = document.createElement('div');
        codeWrap.className = 'req-code-wrap';

        const editor = document.createElement('textarea');
        editor.className = 'req-code-edit';
        editor.spellcheck = false;
        editor.value = fullFetch;
        editor.setAttribute('aria-label', 'Fetch request');
        editor.addEventListener('click', (e) => e.stopPropagation());
        editor.addEventListener('mousedown', (e) => e.stopPropagation());

        const urlEl = card.querySelector('.req-url');
        editor.addEventListener('input', () => {
          entry.fetch = editor.value;
          fitReqEditor(editor);
          if (urlEl) urlEl.classList.toggle('money-flag', fetchHasMoneyKeywords(editor.value));
        });

        const footer = document.createElement('div');
        footer.className = 'req-footer';
        footer.innerHTML = `<button class="btn btn-accent btn-sm" data-act="inject">Run</button>`;

        codeWrap.appendChild(editor);
        body.appendChild(codeWrap);
        body.appendChild(footer);
        card.appendChild(body);

        footer.querySelector('[data-act="inject"]').addEventListener('click', async (e) => {
          e.stopPropagation();
          const btn = e.currentTarget;
          const code = editor.value.trim();
          if (!code) {
            this.toast('Fetch code is empty', 'error');
            return;
          }
          btn.disabled = true;
          btn.textContent = 'Running…';
          try {
            await this.injectCode(code);
            entry.fetch = code;
            this.toast('Done', 'ok');
          } catch (err) {
            this.toast(err.message, 'error', 4000);
          } finally {
            btn.disabled = false;
            btn.textContent = 'Run';
          }
        });

        requestAnimationFrame(() => fitReqEditor(editor));
      }

      clearOutput() {
        this._fetchQueue = [];
        this._fetchFlushScheduled = false;
        this.reqCount = 0;
        this.pendingPostFetches.clear();
        this.requestUrlsById.clear();
        this.requests = [];
        this.collapsedRequestKeys.clear();
        this.clearMonitorDom();
        this.els.emptyState = null;
        this.initMonitorList();
        this.updateReqCount();
        this.showEmpty();
      }

      async exportRequests() {
        if (!this.requests.length) {
          this.toast('Nothing to export', 'error');
          return;
        }

        const entries = this.requests.map((r) => {
          const card = r.requestId ? this.cardsByRequestId.get(r.requestId) : null;
          const fetchCode = r.fetch || (card ? this.getReqCardCode(card) : '') || '';
          return {
            time: r.time || null,
            method: r.method || null,
            url: r.url || null,
            status: r.status ?? null,
            statusText: r.statusText || null,
            responseTime: r.responseTime || null,
            fetch: fetchCode || '',
          };
        });

        const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
        const filename = `arya-traffic-${stamp}.json`;
        try {
          const res = await apiFetch('/api/export', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              filename,
              data: {
                exportedAt: new Date().toISOString(),
                count: entries.length,
                entries,
              },
            }),
          });
          const data = await res.json();
          if (!data.success) throw new Error(data.error || 'Export failed');
          this.toast('Saved to desktop', 'ok', 3200);
        } catch (err) {
          this.toast(err.message || 'Export failed', 'error');
        }
      }

      togglePause() {
        this.paused = !this.paused;
        const btn = this.els.pauseBtn;
        if (!btn) return;
        btn.innerHTML = this.paused
          ? `<svg viewBox="0 0 24 24" fill="currentColor"><polygon points="8,5 19,12 8,19"/></svg> Resume`
          : `<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg> Pause`;
        this.setConn(this.paused ? 'paused' : 'live');
        if (this.els.statusText) this.els.statusText.textContent = this.paused ? 'Paused' : 'Live';
      }

      sleep(ms) {
        return new Promise(r => setTimeout(r, ms));
      }

      parseVrpIds(text) {
        return text
          .split('\n')
          .map(l => l.trim())
          .filter(Boolean)
          .map(n => Number(n))
          .filter(n => Number.isFinite(n) && n > 0);
      }

      isVrpMenu(url) {
        return /vrp\/menu/i.test(url || '');
      }

      parsePostJson(postData) {
        if (!postData) return null;
        if (typeof postData === 'object') return postData;
        try { return JSON.parse(postData); } catch { return null; }
      }

      showPullModal(text) {
        document.getElementById('pullModalText').textContent =
          text || 'اضغط زر السحب';
        document.getElementById('pullModal').classList.add('show');
      }

      hidePullModal() {
        document.getElementById('pullModal').classList.remove('show');
      }

      startPullLogging(expectedCount, userIdQueue, testCount = 0) {
        this.pullAll.loggingPulls = true;
        this.pullAll.pulledCount = 0;
        this.pullAll.expectedCount = expectedCount;
        this.pullAll.userCount = userIdQueue.length;
        this.pullAll.testCount = testCount;
        this.pullAll.userIdQueue = userIdQueue;
      }

      stopPullLogging() {
        this.pullAll.loggingPulls = false;
        this.pullAll.pulledCount = 0;
        this.pullAll.expectedCount = 0;
        this.pullAll.userCount = 0;
        this.pullAll.testCount = 0;
        this.pullAll.userIdQueue = [];
      }

      handleVrpPrompt(request) {
        this.handlePullPrompt(request);
        this.handleBanPrompt(request);
        this.handleJailPrompt(request);
        this.handleMsgPrompt(request);
        this.handleBankPrompt(request);
      }

      matchNextVrpUserId(queue, loggedCount, result, skipValues = []) {
        const value = String(result ?? '').trim();
        if (!Array.isArray(queue) || loggedCount >= queue.length) return null;

        const nextId = queue[loggedCount];
        const nextStr = String(nextId);
        const skips = skipValues
          .filter((s) => s != null && String(s).trim() !== '')
          .map((s) => String(s).trim());

        if (value && skips.includes(value) && value !== nextStr) return null;
        if (value === '0' || value === '00' || value === '000') return null;

        const userIndex = loggedCount + 1;
        const userCount = queue.length;
        return { id: nextId, userIndex, userCount, done: userIndex >= userCount };
      }

      handlePullPrompt(request) {
        if (!this.pullAll.loggingPulls || this.banPlayer.loggingBans || this.jailPlayer.loggingJails || this.msgPlayer.loggingMsgs || this.bankTransfer.loggingTransfers) return;
        if (!/vrp\/prompt/i.test(request.url || '')) return;

        const body = this.parsePostJson(request.postData);
        if (body?.act !== 'close') return;

        const match = this.matchNextVrpUserId(this.pullAll.userIdQueue, this.pullAll.pulledCount, body.result);
        if (!match) return;

        this.pullAll.pulledCount = match.userIndex;
        this.pullLog(`Pulled — ${match.id} (${match.userIndex}/${match.userCount})`, 'ok');
        this.setPullProgress(match.userIndex, match.userCount);
        this.setPullPhase('Running', `Pulling ${match.userIndex} / ${match.userCount}`);

        if (match.done) {
          const n = match.userCount;
          this.stopPullLogging();
          this.setPullPhase('Done', `${n} IDs pulled`);
          this.pullLog(`Done — ${n} IDs`, 'ok');
          document.getElementById('pullStartBtn').disabled = false;
        }
      }

      handleBanPrompt(request) {
        if (!this.banPlayer.loggingBans || this.pullAll.loggingPulls || this.jailPlayer.loggingJails || this.msgPlayer.loggingMsgs || this.bankTransfer.loggingTransfers) return;
        if (!/vrp\/prompt/i.test(request.url || '')) return;

        const body = this.parsePostJson(request.postData);
        if (body?.act !== 'close') return;

        const match = this.matchNextVrpUserId(
          this.banPlayer.userIdQueue,
          this.banPlayer.bannedCount,
          body.result,
          [this.banPlayer.banText]
        );
        if (!match) return;

        this.banPlayer.bannedCount = match.userIndex;
        this.banLog(`Banned — ${match.id} (${match.userIndex}/${match.userCount})`, 'ok');
        this.setBanProgress(match.userIndex, match.userCount);
        this.setBanPhase('Running', `Ban / kick players ${match.userIndex} / ${match.userCount}`);

        if (match.done) {
          const n = match.userCount;
          this.stopBanLogging();
          this.setBanPhase('Done', `${n} IDs done`);
          this.banLog(`Done — ${n} IDs`, 'ok');
          document.getElementById('banStartBtn').disabled = false;
        }
      }

      handleJailPrompt(request) {
        if (!this.jailPlayer.loggingJails || this.pullAll.loggingPulls || this.banPlayer.loggingBans || this.msgPlayer.loggingMsgs || this.bankTransfer.loggingTransfers) return;
        if (!/vrp\/prompt/i.test(request.url || '')) return;

        const body = this.parsePostJson(request.postData);
        if (body?.act !== 'close') return;

        const match = this.matchNextVrpUserId(
          this.jailPlayer.userIdQueue,
          this.jailPlayer.jailedCount,
          body.result,
          [this.jailPlayer.jailTime, this.jailPlayer.jailMsg]
        );
        if (!match) return;

        this.jailPlayer.jailedCount = match.userIndex;
        this.jailLog(`Jailed — ${match.id} (${match.userIndex}/${match.userCount}) — ${this.jailPlayer.jailTime}`, 'ok');
        this.setJailProgress(match.userIndex, match.userCount);
        this.setJailPhase('Running', `Jail all players ${match.userIndex} / ${match.userCount}`);

        if (match.done) {
          const n = match.userCount;
          this.stopJailLogging();
          this.setJailPhase('Done', `${n} IDs jailed`);
          this.jailLog(`Done — ${n} IDs`, 'ok');
          document.getElementById('jailStartBtn').disabled = false;
        }
      }

      handleMsgPrompt(request) {
        if (!this.msgPlayer.loggingMsgs || this.pullAll.loggingPulls || this.banPlayer.loggingBans || this.jailPlayer.loggingJails || this.bankTransfer.loggingTransfers) return;
        if (!/vrp\/prompt/i.test(request.url || '')) return;

        const body = this.parsePostJson(request.postData);
        if (body?.act !== 'close') return;

        const match = this.matchNextVrpUserId(
          this.msgPlayer.idQueue,
          this.msgPlayer.sentCount,
          body.result,
          [this.msgPlayer.msgText]
        );
        if (!match) return;

        this.msgPlayer.sentCount = match.userIndex;
        this.msgLog(`Messaged — ${match.id} (${match.userIndex}/${match.userCount})`, 'ok');
        this.setMsgProgress(match.userIndex, match.userCount);
        this.setMsgPhase('Running', `Messaging players ${match.userIndex} / ${match.userCount}`);

        if (match.done) {
          const n = match.userCount;
          this.stopMsgLogging();
          this.setMsgPhase('Done', `${n} IDs messaged`);
          this.msgLog(`Done — ${n} IDs`, 'ok');
          document.getElementById('msgStartBtn').disabled = false;
        }
      }

      handleBankPrompt(request) {
        if (!this.bankTransfer.loggingTransfers || this.pullAll.loggingPulls || this.banPlayer.loggingBans || this.jailPlayer.loggingJails || this.msgPlayer.loggingMsgs) return;
        if (!/vrp\/prompt/i.test(request.url || '')) return;

        const body = this.parsePostJson(request.postData);
        if (body?.act !== 'close') return;

        const match = this.matchNextVrpUserId(
          this.bankTransfer.idQueue,
          this.bankTransfer.sentCount,
          body.result,
          [this.bankTransfer.amountText]
        );
        if (!match) return;

        this.bankTransfer.sentCount = match.userIndex;
        this.bankLog(`Transferred — ${match.id} (${match.userIndex}/${match.userCount}) — ${this.bankTransfer.amountText}`, 'ok');
        this.setBankProgress(match.userIndex, match.userCount);
        this.setBankPhase('Running', `Giving money ${match.userIndex} / ${match.userCount}`);

        if (match.done) {
          const n = match.userCount;
          this.stopBankLogging();
          this.setBankPhase('Done', `${n} IDs paid`);
          this.bankLog(`Done — ${n} IDs`, 'ok');
          document.getElementById('bankStartBtn').disabled = false;
        }
      }

      pullLog(msg, type = '') {
        const log = document.getElementById('pullLog');
        const line = document.createElement('div');
        line.className = 'line' + (type ? ' ' + type : '');
        line.textContent = `[${formatTime(Date.now())}] ${msg}`;
        log.appendChild(line);
        log.scrollTop = log.scrollHeight;
      }

      setPullPhase(phase, sub = '') {
        document.getElementById('pullPhase').textContent = phase;
        document.getElementById('pullStatusText').textContent = sub || phase;
      }

      setPullProgress(current, total) {
        document.getElementById('pullProgress').textContent =
          total ? `${current} / ${total}` : '';
      }

      buildPullAllScript(userIds, template) {
        const userIdsLine = userIds.join(',');
        const warmupIds = vrpWarmupIdsScriptLiteral();
        const choice = JSON.stringify(template.choice);
        const menuId = typeof template.id === 'number'
          ? template.id
          : JSON.stringify(template.id);
        const mod = template.mod ?? 0;

        return `(async () => {
  const TEST_IDS = ${warmupIds};
  const IDS = [${userIdsLine}];
  const FETCH_WAIT = 15;
  const ID_WAIT = 30;

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  const pullId = async (id) => {
    fetch("https://vrp/menu", {
      method: "POST",
      body: JSON.stringify({
        act: "valid",
        id: ${menuId},
        choice: ${choice},
        mod: ${mod}
      })
    });
    await sleep(FETCH_WAIT);
    fetch("https://vrp/prompt", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        act: "close",
        result: String(id)
      })
    });
    await sleep(ID_WAIT);
  };

  for (const id of TEST_IDS) {
    await pullId(id);
  }

  for (const id of IDS) {
    await pullId(id);
  }
})();`;
      }

      buildBanPlayerScript(userIds, template, banTextValue) {
        const userIdsLine = userIds.join(',');
        const warmupIds = vrpWarmupIdsScriptLiteral();
        const banText = JSON.stringify(banTextValue);
        const choice = JSON.stringify(template.choice);
        const menuId = typeof template.id === 'number'
          ? template.id
          : JSON.stringify(template.id);
        const mod = template.mod ?? 0;

        return `(async () => {
  const TEST_IDS = ${warmupIds};
  const IDS = [${userIdsLine}];
  const BAN_TEXT = ${banText};
  const FETCH_WAIT = 5;
  const ID_WAIT = 20;

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  fetch("https://vrp/menu", {
    method: "POST",
    body: JSON.stringify({
      act: "valid",
      id: ${menuId},
      choice: ${choice},
      mod: ${mod}
    })
  });
  await sleep(FETCH_WAIT);

  const banId = async (id) => {
    fetch("https://vrp/prompt", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        act: "close",
        result: String(id)
      })
    });
    await sleep(FETCH_WAIT);
    fetch("https://vrp/prompt", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        act: "close",
        result: BAN_TEXT
      })
    });
    await sleep(ID_WAIT);
  };

  for (const id of TEST_IDS) {
    await banId(id);
  }

  for (const id of IDS) {
    await banId(id);
  }
})();`;
      }

      buildJailPlayerScript(userIds, template, jailTimeValue, jailMsgValue) {
        const userIdsLine = userIds.join(',');
        const warmupIds = vrpWarmupIdsScriptLiteral();
        const jailTime = JSON.stringify(jailTimeValue);
        const jailMsg = JSON.stringify(jailMsgValue);
        const choice = JSON.stringify(template.choice);
        const menuId = typeof template.id === 'number'
          ? template.id
          : JSON.stringify(template.id);
        const mod = template.mod ?? 0;

        return `(async () => {
  const TEST_IDS = ${warmupIds};
  const IDS = [${userIdsLine}];
  const JAIL_TIME = ${jailTime};
  const JAIL_MSG = ${jailMsg};
  const FETCH_WAIT = 5;
  const ID_WAIT = 20;

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  const jailId = async (id) => {
    fetch("https://vrp/menu", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        act: "valid",
        id: ${menuId},
        choice: ${choice},
        mod: ${mod}
      })
    });
    await sleep(FETCH_WAIT);
    fetch("https://vrp/prompt", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        act: "close",
        result: String(id)
      })
    });
    await sleep(FETCH_WAIT);
    fetch("https://vrp/prompt", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        act: "close",
        result: JAIL_TIME
      })
    });
    await sleep(FETCH_WAIT);
    fetch("https://vrp/prompt", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        act: "close",
        result: JAIL_MSG
      })
    });
    await sleep(ID_WAIT);
  };

  for (const id of TEST_IDS) {
    await jailId(id);
  }

  for (const id of IDS) {
    await jailId(id);
  }
})();`;
      }

      clearPullWait() {
        if (this.pullAll.templateTimer) {
          clearTimeout(this.pullAll.templateTimer);
          this.pullAll.templateTimer = null;
        }
        this.pullAll.templateWaiter = null;
        this.pullAll.templateReject = null;
      }

      cancelPullWait(message = 'Stopped') {
        if (this.pullAll.templateReject) {
          const reject = this.pullAll.templateReject;
          this.clearPullWait();
          reject(new Error(message));
          return;
        }
        this.clearPullWait();
      }

      resetPullUi() {
        this.pullAll.running = false;
        this.pullAll.stop = false;
        this.clearPullWait();
        this.stopPullLogging();
        this.hidePullModal();
        document.getElementById('pullStartBtn').disabled = false;
        document.getElementById('pullStopBtn').disabled = true;
      }

      waitForMenuTemplate(timeout = 120000) {
        this.clearPullWait();

        return new Promise((resolve, reject) => {
          this.pullAll.templateReject = reject;
          this.pullAll.templateTimer = setTimeout(() => {
            this.clearPullWait();
            reject(new Error('Timed out — press Pull in game'));
          }, timeout);

          this.pullAll.templateWaiter = (body) => {
            if (body?.act !== 'valid') return;
            this.clearPullWait();
            resolve({
              choice: body.choice,
              mod: body.mod ?? 0,
              id: body.id,
            });
          };
        });
      }

      handleVrpMenu(request) {
        if (!this.isVrpMenu(request.url) || !request.postData) return;
        const body = this.parsePostJson(request.postData);
        if (!body) return;

        if (this.pullAll.templateWaiter) {
          this.pullAll.templateWaiter(body);
          this.hidePullModal();
          return;
        }

        if (this.banPlayer.templateWaiter) {
          this.banPlayer.templateWaiter(body);
          this.hidePullModal();
          return;
        }

        if (this.jailPlayer.templateWaiter) {
          this.jailPlayer.templateWaiter(body);
          this.hidePullModal();
          return;
        }

        if (this.msgPlayer.templateWaiter) {
          this.msgPlayer.templateWaiter(body);
          this.hidePullModal();
          return;
        }

        if (this.bankTransfer.templateWaiter) {
          this.bankTransfer.templateWaiter(body);
          this.hidePullModal();
        }
      }

      nextCdpId() {
        return ++this.cdpMsgId;
      }

      resolvePostDataFetch(msg) {
        const pending = this.pendingPostFetches.get(msg.id);
        if (!pending) return false;
        this.pendingPostFetches.delete(msg.id);
        const enriched = { ...pending.request };
        if (msg.result?.postData) enriched.postData = msg.result.postData;
        this.logFetch(this.buildFetchSnippet(enriched), {
          requestId: pending.requestId,
          time: pending.time,
        });
        return true;
      }

      captureNetworkRequest(request, type, requestId, wallTime) {
        if (!['Fetch', 'XHR'].includes(type)) return;
        if (isHiddenTraffic(request.url)) return;
        const ts = wallTime ? wallTime * 1000 : Date.now();
        const time = formatTime(ts);

        if (!request.postData && request.hasPostData && requestId && this.ws?.readyState === 1) {
          const cdpId = this.nextCdpId();
          this.pendingPostFetches.set(cdpId, { request, requestId, time });
          this.ws.send(JSON.stringify({
            id: cdpId,
            method: 'Network.getRequestPostData',
            params: { requestId },
          }));
          return;
        }

        this.logFetch(this.buildFetchSnippet(request), { requestId, time });
      }

      handleNetworkMessage(ev) {
        try {
          const msg = JSON.parse(ev.data);

          if (msg.id && this.resolvePostDataFetch(msg)) return;

          if (msg.method === 'Network.requestWillBeSent') {
            const { request, type, requestId, wallTime } = msg.params;
            if (requestId && request?.url) this.trackRequestUrl(requestId, request.url);
            this.handleVrpMenu(request);
            this.handleVrpPrompt(request);
            this.captureNetworkRequest(request, type, requestId, wallTime);
            return;
          }

          if (msg.method === 'Network.loadingFailed') {
            this.handleLoadingFailed(msg.params);
            return;
          }

          if (msg.method === 'Network.responseReceived') {
            const { requestId, response } = msg.params;
            this.updateResponse(requestId, {
              status: response.status,
              statusText: response.statusText,
              responseTime: Date.now(),
            });
          }
        } catch (err) {
          console.error('monitor message error', err);
        }
      }

      async ensureWs() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) return;

        const res = await apiFetch('/get_ws_url');
        const data = await res.json();
        if (!data.ws_url) throw new Error('Could not connect to FiveM');

        await new Promise((resolve, reject) => {
          this.ws = new WebSocket(data.ws_url);
          this.ws.onopen = () => {
            this.ws.send(JSON.stringify({ id: 1, method: 'Network.enable' }));
            this.ws.onmessage = (ev) => this.handleNetworkMessage(ev);
            resolve();
          };
          this.ws.onerror = () => reject(new Error('WebSocket error'));
          this.ws.onclose = () => { this.live = false; };
        });
      }

      async startPullAll() {
        if (this.pullAll.running) return;

        this.clearPullWait();

        const userIds = this.parseVrpIds(document.getElementById('pullIds').value);
        if (!userIds.length) {
          this.toast('Enter at least one ID', 'error');
          return;
        }

        const userCount = userIds.length;
        const totalCount = VRP_WARMUP_COUNT + userCount;

        this.pullAll.running = true;
        this.pullAll.stop = false;

        const startBtn = document.getElementById('pullStartBtn');
        const stopBtn = document.getElementById('pullStopBtn');
        startBtn.disabled = true;
        stopBtn.disabled = false;

        document.getElementById('pullLog').innerHTML = '';
        this.pullLog(`Starting pull for ${userCount} IDs…`);
        this.setPullProgress(0, userCount);

        try {
          await this.ensureWs();

          this.setPullPhase('Waiting', 'Press Pull in game…');
          this.showPullModal();
          this.pullLog('Waiting for Pull in game…', 'dim');
          this.toast('Press Pull in game', '', 4000);

          const template = await this.waitForMenuTemplate();
          this.hidePullModal();
          this.pullLog(`Template captured — menu id: ${template.id}, choice: "${template.choice}", mod: ${template.mod}`, 'ok');

          await this.sleep(200);
          if (this.pullAll.stop) throw new Error('Stopped');

          const script = this.buildPullAllScript(userIds, template);
          this.setPullPhase('Running', `Pulling 0 / ${userCount}`);
          this.startPullLogging(totalCount, userIds, VRP_WARMUP_COUNT);

          await this.injectCode(script);

          this.setPullPhase('Running', `Pulling 0 / ${userCount}`);
          this.toast('Pull started', 'ok');
          document.getElementById('pullStopBtn').disabled = false;
        } catch (err) {
          if (err.message !== 'Stopped') {
            this.setPullPhase('Error', err.message);
            this.pullLog(err.message, 'err');
            this.toast(err.message, 'error', 4000);
          } else {
            this.setPullPhase('Stopped', 'Ready');
            this.pullLog('Stopped', 'dim');
            this.toast('Pull stopped');
          }
        } finally {
          this.pullAll.running = false;
          this.pullAll.stop = false;
          this.clearPullWait();
          document.getElementById('pullStopBtn').disabled = !this.pullAll.loggingPulls;
          if (!this.pullAll.loggingPulls) {
            document.getElementById('pullStartBtn').disabled = false;
          }
        }
      }

      stopPullAll() {
        if (!this.pullAll.running && !this.pullAll.loggingPulls) return;
        this.pullAll.stop = true;
        this.stopPullLogging();
        this.setPullPhase('Stopped', 'Ready');
        this.pullLog('Stop requested…', 'dim');
        this.cancelPullWait('Stopped');
        this.hidePullModal();
        document.getElementById('pullStartBtn').disabled = false;
        document.getElementById('pullStopBtn').disabled = true;
      }

      startBanLogging(expectedCount, userIdQueue, banTextValue, testCount = 0) {
        this.banPlayer.loggingBans = true;
        this.banPlayer.bannedCount = 0;
        this.banPlayer.expectedCount = expectedCount;
        this.banPlayer.userCount = userIdQueue.length;
        this.banPlayer.testCount = testCount;
        this.banPlayer.userIdQueue = userIdQueue;
        this.banPlayer.banText = banTextValue;
      }

      stopBanLogging() {
        this.banPlayer.loggingBans = false;
        this.banPlayer.bannedCount = 0;
        this.banPlayer.expectedCount = 0;
        this.banPlayer.userCount = 0;
        this.banPlayer.testCount = 0;
        this.banPlayer.userIdQueue = [];
        this.banPlayer.banText = '';
      }

      banLog(msg, type = '') {
        const log = document.getElementById('banLog');
        const line = document.createElement('div');
        line.className = 'line' + (type ? ' ' + type : '');
        line.textContent = `[${formatTime(Date.now())}] ${msg}`;
        log.appendChild(line);
        log.scrollTop = log.scrollHeight;
      }

      setBanPhase(phase, sub = '') {
        document.getElementById('banPhase').textContent = phase;
        document.getElementById('banStatusText').textContent = sub || phase;
      }

      setBanProgress(current, total) {
        document.getElementById('banProgress').textContent =
          total ? `${current} / ${total}` : '';
      }

      clearBanWait() {
        if (this.banPlayer.templateTimer) {
          clearTimeout(this.banPlayer.templateTimer);
          this.banPlayer.templateTimer = null;
        }
        this.banPlayer.templateWaiter = null;
        this.banPlayer.templateReject = null;
      }

      cancelBanWait(message = 'Stopped') {
        if (this.banPlayer.templateReject) {
          const reject = this.banPlayer.templateReject;
          this.clearBanWait();
          reject(new Error(message));
          return;
        }
        this.clearBanWait();
      }

      waitForBanMenuTemplate(timeout = 120000) {
        this.clearBanWait();

        return new Promise((resolve, reject) => {
          this.banPlayer.templateReject = reject;
          this.banPlayer.templateTimer = setTimeout(() => {
            this.clearBanWait();
            reject(new Error('Timed out — press Ban / Kick Players in game'));
          }, timeout);

          this.banPlayer.templateWaiter = (body) => {
            if (body?.act !== 'valid') return;
            this.clearBanWait();
            resolve({
              choice: body.choice,
              mod: body.mod ?? 0,
              id: body.id,
            });
          };
        });
      }

      async startBanPlayer() {
        if (this.banPlayer.running) return;

        this.clearBanWait();

        const userIds = this.parseVrpIds(document.getElementById('banIds').value);
        if (!userIds.length) {
          this.toast('Enter at least one ID', 'error');
          return;
        }

        const banTextValue = document.getElementById('banText').value.trim();
        if (!banTextValue) {
          this.toast('Enter a reason', 'error');
          return;
        }

        const userCount = userIds.length;
        const totalCount = VRP_WARMUP_COUNT + userCount;

        this.banPlayer.running = true;
        this.banPlayer.stop = false;

        const startBtn = document.getElementById('banStartBtn');
        const stopBtn = document.getElementById('banStopBtn');
        startBtn.disabled = true;
        stopBtn.disabled = false;

        document.getElementById('banLog').innerHTML = '';
        this.banLog(`Starting ban / kick players for ${userCount} IDs…`);
        this.setBanProgress(0, userCount);

        try {
          await this.ensureWs();

          this.setBanPhase('Waiting', 'Press Ban / Kick Players in game…');
          this.showPullModal('اضغط زر الباند او الكيك !');
          this.banLog('Waiting for Ban / Kick Players in game…', 'dim');
          this.toast('Press Ban / Kick Players in game', '', 4000);

          const template = await this.waitForBanMenuTemplate();
          this.hidePullModal();
          this.banLog(`Template captured — menu id: ${template.id}, choice: "${template.choice}", mod: ${template.mod}`, 'ok');

          await this.sleep(200);
          if (this.banPlayer.stop) throw new Error('Stopped');

          const script = this.buildBanPlayerScript(userIds, template, banTextValue);
          this.setBanPhase('Running', `Ban / kick players 0 / ${userCount}`);
          this.startBanLogging(totalCount, userIds, banTextValue, VRP_WARMUP_COUNT);

          await this.injectCode(script);

          this.setBanPhase('Running', `Ban / kick players 0 / ${userCount}`);
          this.toast('Ban / Kick Players started', 'ok');
          document.getElementById('banStopBtn').disabled = false;
        } catch (err) {
          if (err.message !== 'Stopped') {
            this.setBanPhase('Error', err.message);
            this.banLog(err.message, 'err');
            this.toast(err.message, 'error', 4000);
          } else {
            this.setBanPhase('Stopped', 'Ready');
            this.banLog('Stopped', 'dim');
            this.toast('Ban / Kick Players stopped');
          }
        } finally {
          this.banPlayer.running = false;
          this.banPlayer.stop = false;
          this.clearBanWait();
          document.getElementById('banStopBtn').disabled = !this.banPlayer.loggingBans;
          if (!this.banPlayer.loggingBans) {
            document.getElementById('banStartBtn').disabled = false;
          }
        }
      }

      stopBanPlayer() {
        if (!this.banPlayer.running && !this.banPlayer.loggingBans) return;
        this.banPlayer.stop = true;
        this.stopBanLogging();
        this.setBanPhase('Stopped', 'Ready');
        this.banLog('Stop requested…', 'dim');
        this.cancelBanWait('Stopped');
        this.hidePullModal();
        document.getElementById('banStartBtn').disabled = false;
        document.getElementById('banStopBtn').disabled = true;
      }

      startJailLogging(expectedCount, userIdQueue, jailTimeValue, jailMsgValue, testCount = 0) {
        this.jailPlayer.loggingJails = true;
        this.jailPlayer.jailedCount = 0;
        this.jailPlayer.expectedCount = expectedCount;
        this.jailPlayer.userCount = userIdQueue.length;
        this.jailPlayer.testCount = testCount;
        this.jailPlayer.userIdQueue = userIdQueue;
        this.jailPlayer.jailTime = jailTimeValue;
        this.jailPlayer.jailMsg = jailMsgValue;
      }

      stopJailLogging() {
        this.jailPlayer.loggingJails = false;
        this.jailPlayer.jailedCount = 0;
        this.jailPlayer.expectedCount = 0;
        this.jailPlayer.userCount = 0;
        this.jailPlayer.testCount = 0;
        this.jailPlayer.userIdQueue = [];
        this.jailPlayer.jailTime = '';
        this.jailPlayer.jailMsg = '';
      }

      jailLog(msg, type = '') {
        const log = document.getElementById('jailLog');
        const line = document.createElement('div');
        line.className = 'line' + (type ? ' ' + type : '');
        line.textContent = `[${formatTime(Date.now())}] ${msg}`;
        log.appendChild(line);
        log.scrollTop = log.scrollHeight;
      }

      setJailPhase(phase, sub = '') {
        document.getElementById('jailPhase').textContent = phase;
        document.getElementById('jailStatusText').textContent = sub || phase;
      }

      setJailProgress(current, total) {
        document.getElementById('jailProgress').textContent =
          total ? `${current} / ${total}` : '';
      }

      clearJailWait() {
        if (this.jailPlayer.templateTimer) {
          clearTimeout(this.jailPlayer.templateTimer);
          this.jailPlayer.templateTimer = null;
        }
        this.jailPlayer.templateWaiter = null;
        this.jailPlayer.templateReject = null;
      }

      cancelJailWait(message = 'Stopped') {
        if (this.jailPlayer.templateReject) {
          const reject = this.jailPlayer.templateReject;
          this.clearJailWait();
          reject(new Error(message));
          return;
        }
        this.clearJailWait();
      }

      waitForJailMenuTemplate(timeout = 120000) {
        this.clearJailWait();

        return new Promise((resolve, reject) => {
          this.jailPlayer.templateReject = reject;
          this.jailPlayer.templateTimer = setTimeout(() => {
            this.clearJailWait();
            reject(new Error('Timed out — press Jail All Players in game'));
          }, timeout);

          this.jailPlayer.templateWaiter = (body) => {
            if (body?.act !== 'valid') return;
            this.clearJailWait();
            resolve({
              choice: body.choice,
              mod: body.mod ?? 0,
              id: body.id,
            });
          };
        });
      }

      async startJailPlayer() {
        if (this.jailPlayer.running) return;

        this.clearJailWait();

        const userIds = this.parseVrpIds(document.getElementById('jailIds').value);
        if (!userIds.length) {
          this.toast('Enter at least one ID', 'error');
          return;
        }

        const jailTimeValue = document.getElementById('jailTime').value.trim();
        if (!jailTimeValue) {
          this.toast('Enter a time', 'error');
          return;
        }

        const jailMsgValue = document.getElementById('jailMsg').value.trim();
        if (!jailMsgValue) {
          this.toast('Enter a reason', 'error');
          return;
        }

        const userCount = userIds.length;
        const totalCount = VRP_WARMUP_COUNT + userCount;

        this.jailPlayer.running = true;
        this.jailPlayer.stop = false;

        const startBtn = document.getElementById('jailStartBtn');
        const stopBtn = document.getElementById('jailStopBtn');
        startBtn.disabled = true;
        stopBtn.disabled = false;

        document.getElementById('jailLog').innerHTML = '';
        this.jailLog(`Starting jail all players for ${userCount} IDs…`);
        this.setJailProgress(0, userCount);

        try {
          await this.ensureWs();

          this.setJailPhase('Waiting', 'Press Jail All Players in game…');
          this.showPullModal('اضغط زر السجن !');
          this.jailLog('Waiting for Jail All Players in game…', 'dim');
          this.toast('Press Jail All Players in game', '', 4000);

          const template = await this.waitForJailMenuTemplate();
          this.hidePullModal();
          this.jailLog(`Template captured — menu id: ${template.id}, choice: "${template.choice}", mod: ${template.mod}`, 'ok');

          await this.sleep(200);
          if (this.jailPlayer.stop) throw new Error('Stopped');

          const script = this.buildJailPlayerScript(userIds, template, jailTimeValue, jailMsgValue);
          this.setJailPhase('Running', `Jail all players 0 / ${userCount}`);
          this.startJailLogging(totalCount, userIds, jailTimeValue, jailMsgValue, VRP_WARMUP_COUNT);

          await this.injectCode(script);

          this.setJailPhase('Running', `Jail all players 0 / ${userCount}`);
          this.toast('Jail All Players started', 'ok');
          document.getElementById('jailStopBtn').disabled = false;
        } catch (err) {
          if (err.message !== 'Stopped') {
            this.setJailPhase('Error', err.message);
            this.jailLog(err.message, 'err');
            this.toast(err.message, 'error', 4000);
          } else {
            this.setJailPhase('Stopped', 'Ready');
            this.jailLog('Stopped', 'dim');
            this.toast('Jail All Players stopped');
          }
        } finally {
          this.jailPlayer.running = false;
          this.jailPlayer.stop = false;
          this.clearJailWait();
          document.getElementById('jailStopBtn').disabled = !this.jailPlayer.loggingJails;
          if (!this.jailPlayer.loggingJails) {
            document.getElementById('jailStartBtn').disabled = false;
          }
        }
      }

      stopJailPlayer() {
        if (!this.jailPlayer.running && !this.jailPlayer.loggingJails) return;
        this.jailPlayer.stop = true;
        this.stopJailLogging();
        this.setJailPhase('Stopped', 'Ready');
        this.jailLog('Stop requested…', 'dim');
        this.cancelJailWait('Stopped');
        this.hidePullModal();
        document.getElementById('jailStartBtn').disabled = false;
        document.getElementById('jailStopBtn').disabled = true;
      }

      startMsgLogging(expectedCount, idQueue, msgTextValue, testCount = 0) {
        this.msgPlayer.loggingMsgs = true;
        this.msgPlayer.sentCount = 0;
        this.msgPlayer.expectedCount = expectedCount;
        this.msgPlayer.userCount = idQueue.length;
        this.msgPlayer.testCount = testCount;
        this.msgPlayer.idQueue = idQueue;
        this.msgPlayer.msgText = msgTextValue;
      }

      stopMsgLogging() {
        this.msgPlayer.loggingMsgs = false;
        this.msgPlayer.sentCount = 0;
        this.msgPlayer.expectedCount = 0;
        this.msgPlayer.userCount = 0;
        this.msgPlayer.testCount = 0;
        this.msgPlayer.idQueue = [];
        this.msgPlayer.msgText = '';
      }

      msgLog(msg, type = '') {
        const log = document.getElementById('msgLog');
        const line = document.createElement('div');
        line.className = 'line' + (type ? ' ' + type : '');
        line.textContent = `[${formatTime(Date.now())}] ${msg}`;
        log.appendChild(line);
        log.scrollTop = log.scrollHeight;
      }

      setMsgPhase(phase, sub = '') {
        document.getElementById('msgPhase').textContent = phase;
        document.getElementById('msgStatusText').textContent = sub || phase;
      }

      setMsgProgress(current, total) {
        document.getElementById('msgProgress').textContent =
          total ? `${current} / ${total}` : '';
      }

      clearMsgWait() {
        if (this.msgPlayer.templateTimer) {
          clearTimeout(this.msgPlayer.templateTimer);
          this.msgPlayer.templateTimer = null;
        }
        this.msgPlayer.templateWaiter = null;
        this.msgPlayer.templateReject = null;
      }

      cancelMsgWait(message = 'Stopped') {
        if (this.msgPlayer.templateReject) {
          const reject = this.msgPlayer.templateReject;
          this.clearMsgWait();
          reject(new Error(message));
          return;
        }
        this.clearMsgWait();
      }

      waitForMsgMenuTemplate(timeout = 120000) {
        this.clearMsgWait();

        return new Promise((resolve, reject) => {
          this.msgPlayer.templateReject = reject;
          this.msgPlayer.templateTimer = setTimeout(() => {
            this.clearMsgWait();
            reject(new Error('Timed out — press Message Players in game'));
          }, timeout);

          this.msgPlayer.templateWaiter = (body) => {
            if (body?.act !== 'valid') return;
            this.clearMsgWait();
            resolve({
              choice: body.choice,
              mod: body.mod ?? 0,
              id: body.id,
            });
          };
        });
      }

      buildMsgPlayerScript(userIds, template, msgTextValue) {
        const userIdsLine = userIds.join(',');
        const warmupIds = vrpWarmupIdsScriptLiteral();
        const msgText = JSON.stringify(msgTextValue);
        const choice = JSON.stringify(template.choice);
        const menuId = typeof template.id === 'number'
          ? template.id
          : JSON.stringify(template.id);
        const mod = template.mod ?? 0;

        return `(async () => {
  const TEST_IDS = ${warmupIds};
  const IDS = [${userIdsLine}];
  const MSG_TEXT = ${msgText};
  const FETCH_WAIT = 5;
  const ID_WAIT = 20;

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  const msgId = async (id) => {
    fetch("https://vrp/menu", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        act: "valid",
        id: ${menuId},
        choice: ${choice},
        mod: ${mod}
      })
    });
    await sleep(FETCH_WAIT);
    fetch("https://vrp/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        act: "close",
        result: String(id)
      })
    });
    await sleep(FETCH_WAIT);
    fetch("https://vrp/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        act: "close",
        result: MSG_TEXT
      })
    });
    await sleep(ID_WAIT);
  };

  for (const id of TEST_IDS) {
    await msgId(id);
  }

  for (const id of IDS) {
    await msgId(id);
  }
})();`;
      }

      async startMsgPlayer() {
        if (this.msgPlayer.running) return;

        this.clearMsgWait();

        const userIds = this.parseVrpIds(document.getElementById('msgIds').value);
        if (!userIds.length) {
          this.toast('Enter at least one ID', 'error');
          return;
        }

        const msgTextValue = document.getElementById('msgText').value.trim();
        if (!msgTextValue) {
          this.toast('Enter message text', 'error');
          return;
        }

        const userCount = userIds.length;
        const totalCount = VRP_WARMUP_COUNT + userCount;
        const idQueue = [...userIds];

        this.msgPlayer.running = true;
        this.msgPlayer.stop = false;

        const startBtn = document.getElementById('msgStartBtn');
        const stopBtn = document.getElementById('msgStopBtn');
        startBtn.disabled = true;
        stopBtn.disabled = false;

        document.getElementById('msgLog').innerHTML = '';
        this.msgLog(`Starting message players for ${userCount} IDs…`);
        this.setMsgProgress(0, userCount);

        try {
          await this.ensureWs();

          this.setMsgPhase('Waiting', 'Press Message Players in game…');
          this.showPullModal('اضغط زر الرسالة !');
          this.msgLog('Waiting for Message Players in game…', 'dim');
          this.toast('Press Message Players in game', '', 4000);

          const template = await this.waitForMsgMenuTemplate();
          this.hidePullModal();
          this.msgLog(`Template captured — menu id: ${template.id}, choice: "${template.choice}", mod: ${template.mod}`, 'ok');

          await this.sleep(200);
          if (this.msgPlayer.stop) throw new Error('Stopped');

          const script = this.buildMsgPlayerScript(userIds, template, msgTextValue);
          this.setMsgPhase('Running', `Messaging players 0 / ${userCount}`);
          this.startMsgLogging(totalCount, idQueue, msgTextValue, VRP_WARMUP_COUNT);

          await this.injectCode(script);

          this.setMsgPhase('Running', `Messaging players 0 / ${userCount}`);
          this.toast('Message Players started', 'ok');
          document.getElementById('msgStopBtn').disabled = false;
        } catch (err) {
          if (err.message !== 'Stopped') {
            this.setMsgPhase('Error', err.message);
            this.msgLog(err.message, 'err');
            this.toast(err.message, 'error', 4000);
          } else {
            this.setMsgPhase('Stopped', 'Ready');
            this.msgLog('Stopped', 'dim');
            this.toast('Message Players stopped');
          }
        } finally {
          this.msgPlayer.running = false;
          this.msgPlayer.stop = false;
          this.clearMsgWait();
          document.getElementById('msgStopBtn').disabled = !this.msgPlayer.loggingMsgs;
          if (!this.msgPlayer.loggingMsgs) {
            document.getElementById('msgStartBtn').disabled = false;
          }
        }
      }

      stopMsgPlayer() {
        if (!this.msgPlayer.running && !this.msgPlayer.loggingMsgs) return;
        this.msgPlayer.stop = true;
        this.stopMsgLogging();
        this.setMsgPhase('Stopped', 'Ready');
        this.msgLog('Stop requested…', 'dim');
        this.cancelMsgWait('Stopped');
        this.hidePullModal();
        document.getElementById('msgStartBtn').disabled = false;
        document.getElementById('msgStopBtn').disabled = true;
      }

      startBankLogging(expectedCount, idQueue, amountTextValue, testCount = 0) {
        this.bankTransfer.loggingTransfers = true;
        this.bankTransfer.sentCount = 0;
        this.bankTransfer.expectedCount = expectedCount;
        this.bankTransfer.userCount = idQueue.length;
        this.bankTransfer.testCount = testCount;
        this.bankTransfer.idQueue = idQueue;
        this.bankTransfer.amountText = amountTextValue;
      }

      stopBankLogging() {
        this.bankTransfer.loggingTransfers = false;
        this.bankTransfer.sentCount = 0;
        this.bankTransfer.expectedCount = 0;
        this.bankTransfer.userCount = 0;
        this.bankTransfer.testCount = 0;
        this.bankTransfer.idQueue = [];
        this.bankTransfer.amountText = '';
      }

      bankLog(msg, type = '') {
        const log = document.getElementById('bankLog');
        const line = document.createElement('div');
        line.className = 'line' + (type ? ' ' + type : '');
        line.textContent = `[${formatTime(Date.now())}] ${msg}`;
        log.appendChild(line);
        log.scrollTop = log.scrollHeight;
      }

      setBankPhase(phase, sub = '') {
        document.getElementById('bankPhase').textContent = phase;
        document.getElementById('bankStatusText').textContent = sub || phase;
      }

      setBankProgress(current, total) {
        document.getElementById('bankProgress').textContent =
          total ? `${current} / ${total}` : '';
      }

      clearBankWait() {
        if (this.bankTransfer.templateTimer) {
          clearTimeout(this.bankTransfer.templateTimer);
          this.bankTransfer.templateTimer = null;
        }
        this.bankTransfer.templateWaiter = null;
        this.bankTransfer.templateReject = null;
      }

      cancelBankWait(message = 'Stopped') {
        if (this.bankTransfer.templateReject) {
          const reject = this.bankTransfer.templateReject;
          this.clearBankWait();
          reject(new Error(message));
          return;
        }
        this.clearBankWait();
      }

      waitForBankMenuTemplate(timeout = 120000) {
        this.clearBankWait();

        return new Promise((resolve, reject) => {
          this.bankTransfer.templateReject = reject;
          this.bankTransfer.templateTimer = setTimeout(() => {
            this.clearBankWait();
            reject(new Error('Timed out — press Give Money to Players in game'));
          }, timeout);

          this.bankTransfer.templateWaiter = (body) => {
            if (body?.act !== 'valid') return;
            this.clearBankWait();
            resolve({
              choice: body.choice,
              mod: body.mod ?? 0,
              id: body.id,
            });
          };
        });
      }

      buildBankTransferScript(userIds, template, amountTextValue) {
        const userIdsLine = userIds.join(',');
        const warmupIds = vrpWarmupIdsScriptLiteral();
        const amountText = JSON.stringify(amountTextValue);
        const choice = JSON.stringify(template.choice);
        const menuId = typeof template.id === 'number'
          ? template.id
          : JSON.stringify(template.id);
        const mod = template.mod ?? 0;

        return `(async () => {
  const TEST_IDS = ${warmupIds};
  const IDS = [${userIdsLine}];
  const AMOUNT = ${amountText};
  const FETCH_WAIT = 5;
  const ID_WAIT = 20;

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  const transferId = async (id) => {
    fetch("https://vrp/menu", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        act: "valid",
        id: ${menuId},
        choice: ${choice},
        mod: ${mod}
      })
    });
    await sleep(FETCH_WAIT);
    fetch("https://vrp/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        act: "close",
        result: String(id)
      })
    });
    await sleep(FETCH_WAIT);
    fetch("https://vrp/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        act: "close",
        result: AMOUNT
      })
    });
    await sleep(ID_WAIT);
  };

  for (const id of TEST_IDS) {
    await transferId(id);
  }

  for (const id of IDS) {
    await transferId(id);
  }
})();`;
      }

      async startBankTransfer() {
        if (this.bankTransfer.running) return;

        this.clearBankWait();

        const userIds = this.parseVrpIds(document.getElementById('bankIds').value);
        if (!userIds.length) {
          this.toast('Enter at least one VRP ID', 'error');
          return;
        }

        const amountTextValue = document.getElementById('bankAmount').value.trim();
        if (!amountTextValue) {
          this.toast('Enter amount to give', 'error');
          return;
        }

        const userCount = userIds.length;
        const totalCount = VRP_WARMUP_COUNT + userCount;
        const idQueue = [...userIds];

        this.bankTransfer.running = true;
        this.bankTransfer.stop = false;

        const startBtn = document.getElementById('bankStartBtn');
        const stopBtn = document.getElementById('bankStopBtn');
        startBtn.disabled = true;
        stopBtn.disabled = false;

        document.getElementById('bankLog').innerHTML = '';
        this.bankLog(`Starting give money to players for ${userCount} IDs…`);
        this.setBankProgress(0, userCount);

        try {
          await this.ensureWs();

          this.setBankPhase('Waiting', 'Press Give Money to Players in game…');
          this.showPullModal('اضغط زر التحويل البنكي !');
          this.bankLog('Waiting for Give Money to Players in game…', 'dim');
          this.toast('Press Give Money to Players in game', '', 4000);

          const template = await this.waitForBankMenuTemplate();
          this.hidePullModal();
          this.bankLog(`Template captured — menu id: ${template.id}, choice: "${template.choice}", mod: ${template.mod}`, 'ok');

          await this.sleep(200);
          if (this.bankTransfer.stop) throw new Error('Stopped');

          const script = this.buildBankTransferScript(userIds, template, amountTextValue);
          this.setBankPhase('Running', `Giving money 0 / ${userCount}`);
          this.startBankLogging(totalCount, idQueue, amountTextValue, VRP_WARMUP_COUNT);

          await this.injectCode(script);

          this.setBankPhase('Running', `Giving money 0 / ${userCount}`);
          this.toast('Give Money to Players started', 'ok');
          document.getElementById('bankStopBtn').disabled = false;
        } catch (err) {
          if (err.message !== 'Stopped') {
            this.setBankPhase('Error', err.message);
            this.bankLog(err.message, 'err');
            this.toast(err.message, 'error', 4000);
          } else {
            this.setBankPhase('Stopped', 'Ready');
            this.bankLog('Stopped', 'dim');
            this.toast('Give Money to Players stopped');
          }
        } finally {
          this.bankTransfer.running = false;
          this.bankTransfer.stop = false;
          this.clearBankWait();
          document.getElementById('bankStopBtn').disabled = !this.bankTransfer.loggingTransfers;
          if (!this.bankTransfer.loggingTransfers) {
            document.getElementById('bankStartBtn').disabled = false;
          }
        }
      }

      stopBankTransfer() {
        if (!this.bankTransfer.running && !this.bankTransfer.loggingTransfers) return;
        this.bankTransfer.stop = true;
        this.stopBankLogging();
        this.setBankPhase('Stopped', 'Ready');
        this.bankLog('Stop requested…', 'dim');
        this.cancelBankWait('Stopped');
        this.hidePullModal();
        document.getElementById('bankStartBtn').disabled = false;
        document.getElementById('bankStopBtn').disabled = true;
      }


      urlToBlockPattern(url) {
        try {
          const u = new URL(url);
          return `${u.hostname}${u.pathname}`;
        } catch {
          return String(url || '').replace(/^https?:\/\//i, '').split('?')[0];
        }
      }


      trackRequestUrl(requestId, url) {
        if (!requestId || !url) return;
        this.requestUrlsById.set(String(requestId), String(url));
        if (this.requestUrlsById.size > 4000) {
          const first = this.requestUrlsById.keys().next().value;
          this.requestUrlsById.delete(first);
        }
      }

      isBlockedNetworkFailure(params) {
        const err = String(params?.errorText || '').toUpperCase();
        const reason = String(params?.blockedReason || '').toLowerCase();
        const canceled = params?.canceled;
        if (err.includes('ERR_BLOCKED_BY_CLIENT') || err.includes('BLOCKED_BY_CLIENT')) return true;
        if (err.includes('BLOCKED') && (err.includes('CLIENT') || err.includes('INSPECTOR') || err.includes('CANCELED'))) return true;
        if (reason === 'inspector' || reason === 'content_blocking' || reason === 'blockedbyclient' || reason === 'csp' || reason === 'other') return true;
        if (canceled && (err.includes('BLOCKED') || reason === 'inspector')) return true;
        return false;
      }

      reportBlockedUrl(url, requestId = null) {
        if (!url) return;
        apiFetch('/api/blocker/note', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url, requestId }),
        }).then(async (res) => {
          const data = await res.json();
          if (data?.success) {
            this.renderBlockerRules(data.rules || [], data.totalBlocked || 0);
          }
        }).catch(() => {});
      }

      handleLoadingFailed(params) {
        if (!this.isBlockedNetworkFailure(params)) return;
        const rid = params?.requestId;
        const report = () => {
          const url = rid ? this.requestUrlsById.get(String(rid)) : null;
          if (!url) return false;
          if (rid) this.requestUrlsById.delete(String(rid));
          this.reportBlockedUrl(url, rid);
          return true;
        };
        if (report()) return;
        if (rid) setTimeout(() => report(), 60);
      }

      async loadBlockerRules() {
        try {
          const res = await apiFetch('/api/blocker');
          const data = await res.json();
          if (!data.success) return;
          this.renderBlockerRules(data.rules || [], data.totalBlocked || 0);
        } catch (_) {}
      }

      renderBlockerRules(rules, totalBlocked) {
        const list = document.getElementById('blockerList');
        const stats = document.getElementById('blockerStats');
        const statusText = document.getElementById('blockerStatusText');
        if (!list) return;
        if (stats) {
          stats.textContent = `${totalBlocked} blocked request${totalBlocked === 1 ? '' : 's'}`;
        }
        if (statusText) {
          const ruleCount = rules.length;
          statusText.textContent = ruleCount
            ? `${totalBlocked} blocked · ${ruleCount} rule${ruleCount === 1 ? '' : 's'}`
            : 'Block network requests by URL';
        }
        if (!rules.length) {
          list.innerHTML = '<div class="blocker-empty">No blocked URLs yet</div>';
          return;
        }
        list.innerHTML = rules.map((row) => `
          <div class="blocker-item" data-pattern="${encodeURIComponent(row.pattern)}">
            <div class="blocker-item-copy">
              <strong>${this.escapeHtml(row.pattern)}</strong>
            </div>
            <span class="blocker-count">${row.count || 0} blocked</span>
            <button type="button" class="btn btn-ghost btn-sm" data-act="remove-block">Remove</button>
          </div>
        `).join('');
      }

      escapeHtml(value) {
        return String(value || '')
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;');
      }

      async addBlockerRule(pattern) {
        const clean = String(pattern || '').trim();
        if (!clean) {
          this.toast('Enter a URL pattern', 'error');
          return;
        }
        const res = await apiFetch('/api/blocker', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pattern: clean }),
        });
        const data = await res.json();
        if (!data.success) {
          this.toast(data.error || 'Could not add pattern', 'error');
          return;
        }
        this.renderBlockerRules(data.rules || [], data.totalBlocked || 0);
        const input = document.getElementById('blockerInput');
        if (input) input.value = '';
        this.toast('URL blocked', 'ok');
      }

      async removeBlockerRule(pattern) {
        const res = await apiFetch('/api/blocker/remove', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pattern }),
        });
        const data = await res.json();
        if (!data.success) {
          this.toast(data.error || 'Could not remove pattern', 'error');
          return;
        }
        this.renderBlockerRules(data.rules || [], data.totalBlocked || 0);
        this.toast('Pattern removed', 'ok');
      }

      async applyBlockerPatternsToMonitor() {
        if (!this.ws || this.ws.readyState !== 1) return;
        try {
          const res = await apiFetch('/api/blocker/patterns');
          const data = await res.json();
          if (!data.success || !Array.isArray(data.patterns) || !data.patterns.length) return;
          this.ws.send(JSON.stringify({
            id: Date.now(),
            method: 'Network.setBlockedURLs',
            params: { urls: data.patterns },
          }));
        } catch (_) {}
      }

      initBlockerUi() {
        const input = document.getElementById('blockerInput');
        const addBtn = document.getElementById('blockerAddBtn');
        const list = document.getElementById('blockerList');
        if (addBtn) {
          addBtn.addEventListener('click', () => this.addBlockerRule(input?.value || ''));
        }
        if (input) {
          input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') this.addBlockerRule(input.value);
          });
        }
        if (list) {
          list.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-act="remove-block"]');
            if (!btn) return;
            const item = btn.closest('.blocker-item');
            const pattern = decodeURIComponent(item?.dataset?.pattern || '');
            if (pattern) this.removeBlockerRule(pattern);
          });
        }
        this.loadBlockerRules();
        if (this._blockerPoll) clearInterval(this._blockerPoll);
        this._blockerPoll = setInterval(() => this.loadBlockerRules(), 1000);
      }

      async injectCode(code, target = null, options = {}) {
        const body = { code, fast: !!options.fast };
        if (target && target.ws_url) {
          body.ws_url = target.ws_url;
          body.kind = target.kind || 'devtools';
          body.iframe_index = target.iframe_index || 0;
        }
        const res = await apiFetch('/api/inject', {
          method: 'POST',
          body,
        });
        const data = await parseApiJson(res);
        if (!res.ok && !data.error) {
          throw new Error(`Inject failed (HTTP ${res.status})`);
        }
        if (!data.success) {
          throw new Error(data.error || 'Could not run script. Join a FiveM server first.');
        }
        return data;
      }

      getReqCardCode(card) {
        const editor = card?.querySelector('.req-code-edit');
        return editor ? editor.value.trim() : '';
      }

      syncReqEntryFetch(card, code) {
        const requestId = card?.dataset?.requestId;
        const entry = requestId
          ? this.requests.find(r => r.requestId === requestId)
          : null;
        if (entry) entry.fetch = code;
      }

      createReqRow(entry, index) {
        const url = entry.url || 'unknown';
        const time = entry.time || formatTime(Date.now());
        const moneyFlag = fetchHasMoneyKeywords(entry.fetch);
        const openKey = this.reqOpenKey(entry, index);
        const status = entry.status;

        const card = document.createElement('div');
        card.className = 'req-card';
        card.dataset.listIndex = String(index);
        card.dataset.openKey = openKey;
        if (entry.requestId) card.dataset.requestId = entry.requestId;

        const toggleTitle = entry.autoCollapsed ? 'Expand long fetch' : 'Show fetch';

        const header = document.createElement('div');
        header.className = 'req-header';
        header.innerHTML = `
          <span class="req-url${moneyFlag ? ' money-flag' : ''}" title="${url}">${url}</span>
          <div class="req-meta">
            <span class="req-status" data-status${status ? ' ' + (status >= 200 && status < 400 ? 'ok' : 'err') : ''}"${status ? '' : ''}>${status || ''}</span>
            ${entry.autoCollapsed ? '<span class="req-long-tag">long</span>' : ''}
            <span class="req-time">${entry.responseTime || time}</span>
          </div>
          <div class="req-actions">
            <button type="button" class="btn btn-ghost btn-sm btn-icon" data-act="copy" title="Copy">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            </button>
            <button type="button" class="btn btn-ghost btn-sm btn-icon" data-act="block" title="Block URL">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>
            </button>
            <button type="button" class="btn btn-ghost btn-sm btn-icon" data-act="hide" title="Hide URL">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
            </button>
          </div>
          <button type="button" class="btn btn-ghost btn-sm btn-icon req-toggle" data-act="toggle" title="${toggleTitle}">
            ${ICONS.chevron}
          </button>`;

        if (status) {
          const statusEl = header.querySelector('[data-status]');
          if (statusEl) statusEl.style.display = 'inline-block';
        }

        card.appendChild(header);

        header.addEventListener('click', (e) => {
          if (e.target.closest('.req-actions')) return;
          if (e.target.closest('[data-act="toggle"]') || e.target.closest('.req-url') || e.target.closest('.req-meta')) {
            this.toggleReqCard(card);
          }
        });

        header.querySelector('[data-act="toggle"]').addEventListener('click', (e) => {
          e.stopPropagation();
          this.toggleReqCard(card);
        });

        header.querySelector('[data-act="copy"]').addEventListener('click', (e) => {
          e.stopPropagation();
          navigator.clipboard.writeText(entry.fetch || '');
          this.toast('Copied to clipboard', 'ok');
        });

        header.querySelector('[data-act="block"]').addEventListener('click', async (e) => {
          e.stopPropagation();
          const liveUrl = entry.url || entry.fetch?.match(/fetch\("([^"]+)"/)?.[1];
          if (!liveUrl) return;
          const pattern = this.urlToBlockPattern(liveUrl);
          await this.addBlockerRule(pattern);
          await this.applyBlockerPatternsToMonitor();
        });

        header.querySelector('[data-act="hide"]').addEventListener('click', (e) => {
          e.stopPropagation();
          const liveUrl = entry.url || entry.fetch?.match(/fetch\("([^"]+)"/)?.[1];
          if (!liveUrl) return;
          this.blockedUrls.add(liveUrl);
          const openKey = card.dataset.openKey;
          if (openKey) this.collapsedRequestKeys.delete(openKey);
          if (entry.requestId) this.cardsByRequestId.delete(entry.requestId);
          const idx = this.requests.indexOf(entry);
          if (idx >= 0) this.requests.splice(idx, 1);
          this.renderedIndexes.delete(index);
          card.remove();
          this.reqCount = this.requests.length;
          this.updateReqCount();
          this.clearMonitorDom();
          this.scheduleMonitorRender();
          this.toast('URL hidden');
          if (!this.requests.length) this.showEmpty();
        });

        return card;
      }

      updateResponse(requestId, response) {
        const card = this.cardsByRequestId.get(requestId);
        const entry = this.requests.find(r => r.requestId === requestId);

        if (entry) {
          entry.status = response.status;
          entry.statusText = response.statusText || '';
          if (response.responseTime) {
            entry.responseTime = formatTime(response.responseTime);
          }
        }

        if (!card) return;

        const statusEl = card.querySelector('[data-status]');
        const status = response.status;
        statusEl.textContent = status;
        statusEl.className = 'req-status ' + (status >= 200 && status < 400 ? 'ok' : 'err');
        statusEl.style.display = 'inline-block';
        statusEl.title = response.statusText || '';

        if (response.responseTime) {
          const timeEl = card.querySelector('.req-time');
          timeEl.textContent = formatTime(response.responseTime);
          timeEl.title = 'Response received';
        }
      }

      pruneOldCards() {
        while (this.requests.length > MAX_MONITOR_CARDS) {
          const old = this.requests.shift();
          if (old?.requestId) {
            this.cardsByRequestId.delete(old.requestId);
            this.collapsedRequestKeys.delete(String(old.requestId));
          }
        }
        if (this.requests.length < this.reqCount) {
          this.clearMonitorDom();
        }
        this.reqCount = this.requests.length;
        this.updateReqCount();
      }

      flushFetchQueue() {
        this._fetchFlushScheduled = false;
        const batch = this._fetchQueue.splice(0, MONITOR_BATCH_SIZE);
        if (!batch.length) return;

        this.hideEmpty();
        this.pruneOldCards();
        this.scheduleMonitorRender();

        if (this._fetchQueue.length) {
          this._fetchFlushScheduled = true;
          requestAnimationFrame(() => this.flushFetchQueue());
        } else {
          this.els.output.scrollTop = this.els.output.scrollHeight;
        }
      }

      queueFetch(code, meta = {}) {
        if (this.paused) return;
        const m = code.match(/fetch\("([^"]+)"/);
        if (m?.[1] && this.blockedUrls.has(m[1])) return;

        const methodMatch = code.match(/method:\s*"(\w+)"/);
        const entry = {
          requestId: meta.requestId || null,
          method: methodMatch?.[1] || 'GET',
          url: m?.[1] || 'unknown',
          time: meta.time || formatTime(Date.now()),
          status: null,
          statusText: '',
          responseTime: null,
          fetch: code,
          autoCollapsed: shouldAutoCollapseFetch(code),
        };
        this.requests.push(entry);

        if (entry.autoCollapsed) {
          this.collapsedRequestKeys.add(this.reqOpenKey(entry, this.requests.length - 1));
        }

        this._fetchQueue.push({ code, meta });
        if (!this._fetchFlushScheduled) {
          this._fetchFlushScheduled = true;
          requestAnimationFrame(() => this.flushFetchQueue());
        }
      }

      logFetch(code, meta = {}) {
        this.queueFetch(code, meta);
      }

      buildFetchSnippet(request) {
        const url = toHttps(request.url);
        let code = `fetch("${url}", {\n  method: "${request.method}",`;

        if (request.postData) {
          try {
            const json = JSON.parse(request.postData);
            code += `\n  headers: { "Content-Type": "application/json" },`;
            code += `\n  body: JSON.stringify(${JSON.stringify(json, null, 2)})`;
          } catch {
            const escaped = JSON.stringify(request.postData);
            code += `\n  body: ${escaped}`;
          }
        }

        code += '\n});';
        return code;
      }

      async startInspecting() {
        if (this.ws) { this.ws.close(); this.ws = null; }

        this.clearOutput();
        this.els.statusText.textContent = 'Connecting…';
        this.setConn('idle');

        const startBtn = document.getElementById('startBtn');
        if (startBtn) startBtn.disabled = true;

        try {
          const res = await apiFetch('/get_ws_url');
          if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.error || `Request failed (${res.status})`);
          }
          const data = await res.json();
          if (!data.ws_url) {
            this.toast('Could not connect to FiveM', 'error');
            this.els.statusText.textContent = 'Connection failed';
            return;
          }

          this.ws = new WebSocket(data.ws_url);

          this.ws.onopen = () => {
            this.ws.send(JSON.stringify({ id: 1, method: 'Network.enable' }));
            this.applyBlockerPatternsToMonitor();
            this.live = true;
            this.paused = false;
            this.setConn('live');
            this.els.statusText.textContent = 'Live';
            if (this.els.pauseBtn) this.els.pauseBtn.disabled = false;
            if (startBtn) {
              startBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg> Restart`;
            }
            this.toast('Monitor started', 'ok');
          };

          this.ws.onmessage = (ev) => this.handleNetworkMessage(ev);

          this.ws.onerror = () => {
            this.toast('WebSocket error', 'error');
            this.setConn('error');
            if (this.els.statusText) this.els.statusText.textContent = 'Disconnected';
          };

          this.ws.onclose = () => {
            this.live = false;
            this.setConn('error');
            if (this.els.statusText) this.els.statusText.textContent = 'Disconnected';
            const pauseBtn = document.getElementById('pauseBtn');
            if (pauseBtn) pauseBtn.disabled = true;
          };
        } catch (err) {
          this.toast(err.message, 'error');
          if (this.els.statusText) this.els.statusText.textContent = 'Connection failed';
        } finally {
          if (startBtn) startBtn.disabled = false;
        }
      }

      async runExecutor() {
        const code = this.getEditorCode();
        if (!code) {
          this.setExecResult('Enter code to execute.', false);
          return;
        }

        const btn = document.getElementById('execBtn');
        if (!btn) return;
        btn.disabled = true;
        btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" style="animation:spin 1s linear infinite"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg> Running…`;
        this.setExecResult('Running…');

        try {
          await this.injectCode(code);
          this.setExecResult('Done.', true);
          this.toast('Done', 'ok');
        } catch (err) {
          this.setExecResult(err.message, false);
          this.toast(err.message, 'error', 4000);
        } finally {
          btn.disabled = false;
          btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><polygon points="5 3 19 12 5 21 5 3"/></svg> Run`;
        }
      }


      bindEvents() {
        document.getElementById('startBtn')?.addEventListener('click', () => this.startInspecting());
        document.getElementById('exportBtn')?.addEventListener('click', () => this.exportRequests());
        document.getElementById('clearBtn')?.addEventListener('click', () => this.clearOutput());
        document.getElementById('pauseBtn')?.addEventListener('click', () => this.togglePause());
        document.getElementById('execBtn')?.addEventListener('click', () => this.runExecutor());
        document.getElementById('pullStartBtn')?.addEventListener('click', () => this.startPullAll());
        document.getElementById('pullStopBtn')?.addEventListener('click', () => this.stopPullAll());
        document.getElementById('banStartBtn')?.addEventListener('click', () => this.startBanPlayer());
        document.getElementById('banStopBtn')?.addEventListener('click', () => this.stopBanPlayer());
        document.getElementById('jailStartBtn')?.addEventListener('click', () => this.startJailPlayer());
        document.getElementById('jailStopBtn')?.addEventListener('click', () => this.stopJailPlayer());
        document.getElementById('msgStartBtn')?.addEventListener('click', () => this.startMsgPlayer());
        document.getElementById('msgStopBtn')?.addEventListener('click', () => this.stopMsgPlayer());
        document.getElementById('bankStartBtn')?.addEventListener('click', () => this.startBankTransfer());
        document.getElementById('bankStopBtn')?.addEventListener('click', () => this.stopBankTransfer());
        document.getElementById('pullModalClose')?.addEventListener('click', () => this.hidePullModal());
        document.getElementById('clearOutputBtn')?.addEventListener('click', () => this.setExecResult('Ready.'));

        document.querySelectorAll('.nav-btn').forEach(btn => {
          btn.addEventListener('click', () => {
            const viewId = btn.dataset.view;
            const view = viewId ? document.getElementById(viewId) : null;
            if (!view) return;

            if (viewId === 'executor') {
              if (this.codeEditor) {
                setTimeout(() => this.codeEditor.refresh(), 50);
              } else {
                const ta = document.getElementById('execCode');
                if (ta) setTimeout(() => ta.focus(), 50);
              }
              this.loadExecutorScripts();
            }
            if (viewId === 'debug') {
              this.refreshDebugTab();
            }
            if (viewId === 'blocker') {
              this.loadBlockerRules();
            }
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
            btn.classList.add('active');
            view.classList.add('active');
          });
        });
      }
    }

    class BootLoader {
      constructor() {
        this.loader = document.getElementById('bootLoader');
        this.app = document.getElementById('appRoot');
        this.stepEl = document.getElementById('bootStep');
        this.detailEl = document.getElementById('bootDetail');
        this.progressEl = document.getElementById('bootProgress');
        this.statusRowsEl = document.getElementById('bootStatusRows');
        this.logEl = document.getElementById('bootTerminalLog');
        this.activeLineEl = document.getElementById('bootActiveLine');
        this.errorPanel = document.getElementById('bootError');
        this.errorDetailEl = document.getElementById('bootErrorDetail');
        this.panelsEl = document.getElementById('bootPanels');
        this.retryBtn = document.getElementById('bootRetryBtn');
        this.retryResolver = null;
        this.lastPhase = '';
        this.lastLogLine = '';
        this.lastStepsKey = '';
        this.lastMessage = '';
        this.lastDetail = '';
        this.lastActiveLine = '';
        this.lastProgress = -1;
        this.statusLabels = {
          fivem: 'FiveM Process',
          fivem_connected: 'FiveM Client',
          server: 'Server Session',
          server_connected: 'Server Link',
          ready: 'Workspace',
        };
        this.phaseLogs = {
          fivem: 'Detecting FiveM process…',
          fivem_connected: 'Local devtools endpoint online.',
          server: 'Waiting for in-game server session…',
          server_connected: 'CitizenFX root UI connected.',
          ready: 'Workspace prepared.',
        };
        this.defaultSteps = [
          { id: 'fivem', label: 'FiveM running', status: 'pending' },
          { id: 'fivem_connected', label: 'FiveM connected', status: 'pending' },
          { id: 'server', label: 'Join server', status: 'pending' },
          { id: 'server_connected', label: 'Server connected', status: 'pending' },
          { id: 'ready', label: 'Ready', status: 'pending' },
        ];
        this.retryBtn?.addEventListener('click', () => this.resolveRetry());
        this.appendLog('Initializing Arya FiveM Tool…', 'dim');
        this.appendLog('Checking local environment…', 'dim');
        this.setActiveLine('Waiting for local service…');
        this.renderStatusRows(this.defaultSteps);
        this.setMessage('Starting');
        this.detailEl.textContent = 'Preparing workspace…';
      }

      statusTextFor(step) {
        if (step.status === 'done') {
          return step.id === 'ready' ? 'READY' : 'CONNECTED';
        }
        if (step.status === 'active') return 'CONNECTING';
        return 'PENDING';
      }

      renderStatusRows(steps) {
        const list = Array.isArray(steps) && steps.length ? steps : this.defaultSteps;
        this.statusRowsEl.innerHTML = list.map((step) => `
          <li class="boot-status-row ${step.status || 'pending'}" data-phase="${step.id}">
            <span class="boot-status-dot"></span>
            <span class="boot-status-label">${this.statusLabels[step.id] || step.label}</span>
            <span class="boot-status-value">${this.statusTextFor(step)}</span>
          </li>
        `).join('');
      }

      appendLog(message, tone = '') {
        const line = (message || '').trim();
        if (!line || line === this.lastLogLine) return;
        this.lastLogLine = line;
        const el = document.createElement('div');
        el.className = 'boot-log-line' + (tone ? ` ${tone}` : '');
        el.textContent = `> ${line}`;
        this.logEl.appendChild(el);
        while (this.logEl.childElementCount > 36) {
          this.logEl.removeChild(this.logEl.firstElementChild);
        }
        this.logEl.scrollTop = this.logEl.scrollHeight;
      }

      setActiveLine(message) {
        const text = (message || 'Working…').trim();
        if (text === this.lastActiveLine) return;
        this.lastActiveLine = text;
        this.activeLineEl.textContent = `> ${text}`;
        const cursor = document.createElement('span');
        cursor.className = 'boot-cursor';
        cursor.id = 'bootCursor';
        cursor.textContent = '▌';
        this.activeLineEl.appendChild(cursor);
      }

      setMessage(message) {
        const next = message || 'Loading';
        if (next === this.lastMessage) return;
        this.lastMessage = next;
        this.stepEl.textContent = next;
      }

      showError(message) {
        this.errorDetailEl.textContent = message || 'Unable to continue.';
        this.errorPanel.hidden = false;
        this.panelsEl.hidden = true;
        this.appendLog(message, 'warn');
        this.setActiveLine('Initialization halted.');
      }

      hideError() {
        this.errorPanel.hidden = true;
        this.panelsEl.hidden = false;
      }

      resolveRetry() {
        if (this.retryResolver) {
          const resolve = this.retryResolver;
          this.retryResolver = null;
          resolve();
        }
      }

      waitForRetry() {
        return new Promise((resolve) => {
          this.retryResolver = resolve;
        });
      }

      notePhase(data) {
        const phase = data?.phase || '';
        if (!phase || phase === this.lastPhase) return;
        this.lastPhase = phase;
        const line = this.phaseLogs[phase] || data.message || phase;
        this.appendLog(line, phase === 'ready' ? 'ok' : '');
      }

      update(data) {
        if (!data) return;
        this.notePhase(data);
        this.setMessage(data.message || 'Loading');

        const detail = data.detail || '';
        if (detail !== this.lastDetail) {
          this.lastDetail = detail;
          setTextIfChanged(this.detailEl, detail);
        }

        this.setActiveLine(data.message || detail || 'Working…');

        const progress = Math.max(0, Math.min(100, Number(data.progress) || 0));
        if (progress !== this.lastProgress) {
          this.lastProgress = progress;
          this.progressEl.style.width = `${progress}%`;
        }

        if (Array.isArray(data.steps) && data.steps.length) {
          const stepsKey = data.steps.map((step) => `${step.id}:${step.status}`).join('|');
          if (stepsKey !== this.lastStepsKey) {
            this.lastStepsKey = stepsKey;
            this.renderStatusRows(data.steps);
          }
        }
      }

      async bootstrapFetch() {
        return fetch(`${API}/api/bootstrap`, { credentials: 'same-origin' });
      }

      async waitUntilReady() {
        const started = Date.now();
        let failSince = 0;
        let lastReadyPhase = 'fivem';

        while (true) {
          try {
            const res = await this.bootstrapFetch();
            if (!res.ok) throw new Error('bootstrap failed');
            const data = await res.json();
            failSince = 0;
            this.hideError();
            lastReadyPhase = data.phase || lastReadyPhase;

            if (data.ready || data.phase === 'ready') {
              this.update({ ...data, ready: true });
              this.appendLog('Initialization complete.', 'ok');
              this.setActiveLine('Launching workspace…');
              return { ...data, ready: true };
            }

            if (Date.now() - started > 4000) {
              return { ...data, ready: true, phase: 'ready', message: 'Ready' };
            }

            this.update(data);

            const elapsed = Date.now() - started;
            if (elapsed > 120000 && (data.phase === 'fivem' || data.phase === 'server')) {
              const hint = data.phase === 'fivem'
                ? 'Unable to detect FiveM. Launch FiveM and click Retry.'
                : 'Unable to connect to a server. Join a server in FiveM, then Retry.';
              this.showError(hint);
              await this.waitForRetry();
              this.hideError();
              this.appendLog('Retry requested.', 'dim');
              continue;
            }
          } catch {
            if (!failSince) failSince = Date.now();
            this.setMessage('Connecting');
            const waiting = 'Waiting for local service…';
            if (waiting !== this.lastDetail) {
              this.lastDetail = waiting;
              setTextIfChanged(this.detailEl, waiting);
            }
            this.setActiveLine(waiting);
            if (Date.now() - failSince > 15000) {
              this.showError('Unable to reach the local tool service. Retry when the app is ready.');
              await this.waitForRetry();
              failSince = 0;
              this.hideError();
              this.appendLog('Retry requested.', 'dim');
              continue;
            }
          }
          await new Promise((r) => setTimeout(r, 250));
        }
      }

      finish(data) {
        const doneSteps = (data?.steps || this.defaultSteps).map((step) => ({ ...step, status: 'done' }));
        this.update({
          ...data,
          message: 'Ready',
          detail: data?.detail || 'Launching workspace…',
          progress: 100,
          ready: true,
          steps: doneSteps,
        });
        setTimeout(() => {
          this.loader.classList.add('hide');
          this.app.classList.remove('boot-hidden');
          this.app.classList.add('boot-visible');
          if (app) {
            app.startStatusPolling();
            apiFetch('/api/bootstrap')
              .then((res) => (res.ok ? res.json() : null))
              .then((bootData) => { if (bootData && app) app.initFromBootstrap(bootData); })
              .catch(() => {});
          }
        }, 260);
        return data;
      }
    }

    let app;

    function launchApp() {
      const start = () => {
        try {
          app = new App();
          app.startStatusPolling();
          apiFetch('/api/bootstrap')
            .then((res) => (res.ok ? res.json() : null))
            .then((data) => { if (data && app) app.initFromBootstrap(data); })
            .catch(() => {});
        } catch (err) {
          console.error('App init failed', err);
        }
      };

      if (document.readyState === 'complete') {
        start();
      } else {
        window.addEventListener('load', start, { once: true });
      }
    }

    launchApp();

    const evtSource = new EventSource(`${API}/notify_stream`);
    evtSource.onmessage = (e) => {
      const msg = (e.data || '').trim();
      if (!msg) return;

      if (msg.startsWith('{')) {
        try {
          const evt = JSON.parse(msg);
          if (evt.type === 'blocker_update') {
            app?.loadBlockerRules();
            return;
          }
        } catch (_) {}
      }

      if (/connected to server/i.test(msg)) {
        app?.setMonitoring(false, 'Connected to server');
        app?.pollStatus();
        app.toast(msg, 'ok', 2800);
        return;
      }
      if (/connected to fivem/i.test(msg)) {
        app?.setMonitoring(false, 'Connected to FiveM');
        app?.toast(msg, 'ok', 2400);
        return;
      }
      if (/waiting for fivem|please open fivem/i.test(msg)) {
        app?.setMonitoring(false, 'Waiting for FiveM');
        app?.toast(msg, '', 2800);
        return;
      }
      if (/reconnecting/i.test(msg)) {
        app?.setMonitoring(false, 'Reconnecting…');
        app?.toast(msg, '', 2800);
        return;
      }
      if (/websocket error/i.test(msg)) {
        app?.setMonitoring(false, 'Disconnected');
        app?.toast(msg, 'error', 3200);
      }
    };
  </script>
</body>
</html>"""

def get_tool_version() -> str:
    return TOOL_VERSION


def arya_logo_url() -> str:
    return "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E"


def ui_build_id() -> str:
    return hashlib.sha256(PANEL_HTML.encode("utf-8")).hexdigest()[:10]


def get_panel_html() -> str:
    return (
        PANEL_HTML.replace("__ARYA_LOGO_URL__", arya_logo_url())
        .replace("__TOOL_VERSION__", get_tool_version())
        .replace("__UI_BUILD__", ui_build_id())
    )


def is_local_request() -> bool:
    addr = (request.remote_addr or "").strip().lower()
    return addr in ("127.0.0.1", "::1", "localhost")


def arya_tool_export_dir() -> Path:
    desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    exports = desktop / "AryaTool" / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    return exports

@app.route("/")
def index():
    if not is_local_request():
        return jsonify({"success": False, "error": "Local access only"}), 403
    html = get_panel_html()
    if "<body" in html and 'data-build="' not in html:
        html = html.replace("<body", f'<body data-build="{ui_build_id()}"', 1)
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Arya-Build"] = ui_build_id()
    return resp


@app.route("/assets/<path:filename>")
def assets(filename):
    assets_dir = BASE_DIR / "assets"
    if not (assets_dir / filename).is_file():
        return Response(status=404)
    return send_from_directory(assets_dir, filename)


@app.route("/api/blocker", methods=["GET"])
def api_blocker_list():
    return jsonify({
        "success": True,
        "rules": blocker_manager.list_rules(),
        "totalBlocked": blocker_manager.total_blocked(),
    })


@app.route("/api/blocker/patterns", methods=["GET"])
def api_blocker_patterns():
    return jsonify({
        "success": True,
        "patterns": blocker_manager.all_cdp_patterns(),
    })


@app.route("/api/blocker/note", methods=["POST"])
def api_blocker_note():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url") or "").strip()
    request_id = str(data.get("requestId") or "").strip() or None
    if url:
        blocker_manager.note_blocked(url, request_id=request_id)
    return jsonify({
        "success": True,
        "rules": blocker_manager.list_rules(),
        "totalBlocked": blocker_manager.total_blocked(),
    })


@app.route("/api/blocker", methods=["POST"])
def api_blocker_add():
    data = request.get_json(silent=True) or {}
    pattern = data.get("pattern") or data.get("url") or ""
    ok, result = blocker_manager.add_rule(pattern)
    if not ok:
        return jsonify({"success": False, "error": result}), 400
    return jsonify({
        "success": True,
        "pattern": result,
        "rules": blocker_manager.list_rules(),
        "totalBlocked": blocker_manager.total_blocked(),
    })


@app.route("/api/blocker", methods=["DELETE"])
def api_blocker_remove():
    data = request.get_json(silent=True) or {}
    pattern = data.get("pattern") or request.args.get("pattern") or ""
    if not blocker_manager.remove_rule(pattern):
        return jsonify({"success": False, "error": "Pattern not found."}), 404
    return jsonify({
        "success": True,
        "rules": blocker_manager.list_rules(),
        "totalBlocked": blocker_manager.total_blocked(),
    })


@app.route("/api/blocker/remove", methods=["POST"])
def api_blocker_remove_post():
    data = request.get_json(silent=True) or {}
    pattern = data.get("pattern") or ""
    if not blocker_manager.remove_rule(pattern):
        return jsonify({"success": False, "error": "Pattern not found."}), 404
    return jsonify({
        "success": True,
        "rules": blocker_manager.list_rules(),
        "totalBlocked": blocker_manager.total_blocked(),
    })


@app.route("/api/executor/scripts", methods=["GET"])
def api_executor_scripts_list():
    return jsonify({
        "success": True,
        "scripts": executor_scripts.list_scripts(),
    })


@app.route("/api/executor/scripts/<script_id>", methods=["GET"])
def api_executor_script_get(script_id: str):
    script = executor_scripts.get_script(script_id)
    if not script:
        return jsonify({"success": False, "error": "Script not found."}), 404
    return jsonify({"success": True, "script": script})


@app.route("/api/executor/scripts", methods=["POST"])
def api_executor_scripts_save():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "")
    if not code.strip():
        return jsonify({"success": False, "error": "Script is empty."}), 400
    name = str(data.get("name") or "").strip()
    script_id = str(data.get("id") or "").strip() or None
    script = executor_scripts.save_script(name, code, script_id=script_id)
    return jsonify({
        "success": True,
        "script": {
            "id": script["id"],
            "name": script.get("name"),
            "code": script.get("code"),
            "updatedAt": script.get("updated_at"),
            "chars": len(script.get("code") or ""),
        },
        "scripts": executor_scripts.list_scripts(),
    })


@app.route("/api/executor/scripts/remove", methods=["POST"])
def api_executor_scripts_remove():
    data = request.get_json(silent=True) or {}
    script_id = str(data.get("id") or "").strip()
    if not script_id or not executor_scripts.delete_script(script_id):
        return jsonify({"success": False, "error": "Script not found."}), 404
    return jsonify({
        "success": True,
        "scripts": executor_scripts.list_scripts(),
    })


@app.route("/api/export", methods=["POST"])
def api_export_traffic():
    data = request.get_json(silent=True) or {}
    payload = data.get("data")
    filename = str(data.get("filename") or "").strip()

    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "Nothing to export"}), 400

    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        return jsonify({"success": False, "error": "Nothing to export"}), 400

    safe_name = re.sub(r"[^\w\-.]", "_", filename)[:120]
    if not safe_name.lower().endswith(".json"):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_name = f"arya-traffic-{stamp}.json"

    out_dir = arya_tool_export_dir()
    out_path = out_dir / safe_name
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return jsonify({
        "success": True,
        "path": str(out_path),
        "folder": str(out_dir),
        "filename": safe_name,
    })


@app.route("/get_ws_url", methods=["GET"])
def get_ws_url():
    try:
        tab = find_root_tab(tab_cache.get_tabs(force=True))
        if not tab:
            return jsonify({"error": "No matching tab found"}), 404

        devtools_path = tab.get("devtoolsFrontendUrl", "")
        full_url = ""
        if devtools_path.startswith("/devtools/inspector.html?ws="):
            full_url = f"http://{FIVEM_DEVTOOLS_HOST}:{FIVEM_DEVTOOLS_PORT}" + devtools_path

        return jsonify({
            "ws_url": tab["webSocketDebuggerUrl"],
            "inspector_url": full_url,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/inject", methods=["POST"])
@app.route("/api/inject", methods=["POST"])
def inject_js():
    data = request.get_json(silent=True) or {}
    js_code = data.get("code", "")
    if not js_code:
        return jsonify({"success": False, "error": "No code provided"}), 400

    ws_url = data.get("ws_url")
    kind = data.get("kind") or ("root" if not ws_url else "devtools")
    iframe_index = int(data.get("iframe_index") or 0)
    fast = bool(data.get("fast"))

    success, result = injection_pool.inject(
        js_code,
        ws_url=ws_url,
        kind=kind,
        iframe_index=iframe_index,
        fast=fast,
    )
    if not success:
        status = 404 if "root" in str(result).lower() or "Not ready" in str(result) else 500
        return jsonify({"success": False, "error": result}), status

    return jsonify({"success": True, "result": result})


@app.route("/api/debug/nui-tabs", methods=["GET"])
def api_debug_nui_tabs():
    try:
        entries = collect_nui_tabs()
        return jsonify({"success": True, "tabs": entries})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/debug/dump", methods=["POST"])
def api_debug_dump():
    try:
        data = request.get_json(silent=True) or {}
        ws_url = data.get("ws_url")
        kind = data.get("kind") or "devtools"
        try:
            iframe_index = int(data.get("iframe_index") or 0)
        except (TypeError, ValueError):
            iframe_index = 0
        if not ws_url:
            return jsonify({"success": False, "error": "Missing target"}), 400
        success, result = dump_nui_target(ws_url=ws_url, kind=kind, iframe_index=iframe_index)
        if not success:
            return jsonify({"success": False, "error": result}), 500
        return jsonify({"success": True, **result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/bootstrap")
def api_bootstrap():
    with panel_state_lock:
        state = dict(panel_state)
    with _bootstrap_lock:
        boot = dict(_bootstrap)
    return jsonify({
        "ready": boot.get("ready", False),
        "phase": boot.get("phase", "fivem"),
        "message": boot.get("message", "Loading…"),
        "detail": boot.get("detail", ""),
        "progress": boot.get("progress", 0),
        "steps": boot.get("steps", []),
        "devtools_ready": is_fivem_devtools_up(),
        "server_ready": is_devtools_ready(),
        "fivem_connected": state.get("fivem_connected", False),
        "bypass_active": state.get("bypass_active", False),
        "bypass_message": state.get("bypass_message", ""),
    })


@app.route("/api/status")
def api_status():
    with panel_state_lock:
        state = dict(panel_state)

    tabs = tab_cache.get_tabs()

    with _bootstrap_lock:
        boot = dict(_bootstrap)

    return jsonify({
        "fivem_connected": state.get("fivem_connected", False),
        "bypass_active": state.get("bypass_active", False),
        "bypass_message": state.get("bypass_message", ""),
        "message": boot.get("message", state.get("bypass_message", "")),
        "detail": boot.get("detail", ""),
        "progress": boot.get("progress", 0),
        "steps": boot.get("steps", []),
        "phase": boot.get("phase", "fivem"),
        "ready": boot.get("ready", False),
        "devtools_ready": is_fivem_devtools_up(),
        "server_ready": is_devtools_ready(),
        "devtools_tabs": len(tabs),
    })


@app.route("/notify_stream")
def notify_stream():
    if not is_local_request():
        return jsonify({"success": False, "error": "Local access only"}), 403

    def event_stream():
        while True:
            msg = notify_queue.get()
            yield f"data: {msg}\n\n"

    return Response(event_stream(), mimetype="text/event-stream")

def get_root_ws():
    try:
        tab = find_root_tab(tab_cache.get_tabs(force=True))
        if tab:
            return tab.get("webSocketDebuggerUrl")
    except Exception:
        pass
    if not is_fivem_devtools_up():
        notify("Waiting for FiveM…", key="need_fivem", cooldown=45.0)
    return None


def block(ws_url):
    rid = 1

    def send(ws, method, params=None):
        nonlocal rid
        msg = {"id": rid, "method": method}
        if params:
            msg["params"] = params
        ws.send(json.dumps(msg))
        rid += 1

    def send_named(method, params=None):
        send(ws_ref["ws"], method, params)

    ws_ref = {"ws": None}

    def on_open(ws):
        ws_ref["ws"] = ws
        set_panel_state(fivem_connected=True, bypass_active=False, bypass_message="Connecting…")
        send(ws, "Network.enable")
        blocker_manager.register_sender(send_named)
        set_panel_state(bypass_active=True, bypass_message="Monitoring")

    def on_message(ws, message):
        blocker_manager.handle_cdp_message(message)

    def on_close(ws, *_):
        blocker_manager.clear_sender()
        set_panel_state(fivem_connected=False, bypass_active=False, bypass_message="Reconnecting…")
        notify("Connection closed. Reconnecting…", key="reconnecting", cooldown=20.0)
        threading.Thread(target=wait_ui, daemon=True).start()

    def on_error(ws, error):
        notify(f"WebSocket error: {error}", key="ws_error", cooldown=30.0)

    websocket.WebSocketApp(
        ws_url,
        on_open=on_open,
        on_message=on_message,
        on_close=on_close,
        on_error=on_error,
    ).run_forever()


def wait_ui():
    set_panel_state(fivem_connected=False, bypass_active=False, bypass_message="Waiting for FiveM…")
    notify("Waiting for FiveM…", key="waiting_fivem", cooldown=60.0)
    fivem_announced = False
    server_announced = False
    while True:
        if is_fivem_devtools_up():
            if not fivem_announced:
                fivem_announced = True
                set_panel_state(fivem_connected=True, bypass_message="Waiting for server…")
                notify("Connected to FiveM.", key="fivem_connected", cooldown=30.0)
        else:
            fivem_announced = False
            server_announced = False
            set_panel_state(fivem_connected=False, bypass_message="Waiting for FiveM…")

        ws = get_root_ws()
        if ws:
            if not server_announced:
                server_announced = True
                set_panel_state(fivem_connected=True, bypass_message="Connected to server")
                notify("Connected to server.", key="server_connected", cooldown=15.0)
            threading.Thread(target=block, args=(ws,), daemon=True).start()
            break
        time.sleep(0.25)


def format_startup_error(title: str, exc: BaseException) -> str:
    message = str(exc).strip()
    if not message:
        message = f"{type(exc).__name__} (no details)"
    log_path = Path(os.environ.get("TEMP", BASE_DIR)) / "arya_tool_startup.log"
    return f"{title}:\r\n\r\n{message}\r\n\r\nLog: {log_path}"


def run_flask():
    global _flask_boot_error
    _flask_boot_error = None
    import flask.cli
    from werkzeug.serving import WSGIRequestHandler, run_simple

    for name in ("werkzeug", "werkzeug.serving", "flask.app", "flask.cli"):
        log = logging.getLogger(name)
        log.disabled = True
        log.propagate = False

    flask.cli.show_server_banner = lambda *args, **kwargs: None
    warnings.filterwarnings("ignore", message=".*development server.*", category=UserWarning)

    class _QuietHandler(WSGIRequestHandler):
        def log_request(self, code="-", size="-"):
            pass

        def log_message(self, format, *args):
            pass

    try:
        run_simple(
            "127.0.0.1",
            PORT,
            app,
            threaded=True,
            use_reloader=False,
            request_handler=_QuietHandler,
        )
    except BaseException as exc:
        _flask_boot_error = exc
        _log_startup_error(f"flask failed: {type(exc).__name__}: {exc!r}")
        raise


def _console_watchdog(seconds: float = 4.0) -> None:
    if sys.platform != "win32" or not USE_WEBVIEW:
        return

    def _watch() -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            suppress_console_windows()
            time.sleep(0.25)

    threading.Thread(target=_watch, daemon=True).start()


def wait_for_flask(timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    probe = f"http://127.0.0.1:{PORT}/"
    while time.monotonic() < deadline:
        if _flask_boot_error is not None:
            return False
        try:
            r = http_session.get(probe, timeout=1.5)
            if r.status_code < 500:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.15)
    return False


def open_ui():
    url = f"http://127.0.0.1:{PORT}/?v={ui_build_id()}"
    try:
        if not wait_for_flask():
            detail = "Flask did not start in time."
            if _flask_boot_error is not None:
                detail = f"Flask failed: {type(_flask_boot_error).__name__}: {_flask_boot_error!r}"
            raise RuntimeError(detail)
        try:
            import webview
            webview.create_window(
                "Arya FiveM Tool",
                url,
                width=1280,
                height=820,
                min_size=(960, 640),
                text_select=True,
                easy_drag=False,
            )
            webview.start(gui="edgechromium", private_mode=False, debug=False)
            return
        except ImportError:
            pass
        except Exception as exc:
            _log_startup_error(f"webview failed: {type(exc).__name__}: {exc!r}")
        import webbrowser
        webbrowser.open(url)
        while True:
            time.sleep(3600)
    except Exception as exc:
        _log_startup_error(f"open_ui failed: {type(exc).__name__}: {exc!r}")
        _show_startup_error(format_startup_error("UI failed to open", exc))


def _show_startup_prompt(message: str, title: str = "Arya FiveM Tool") -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        result = ctypes.windll.user32.MessageBoxW(
            None,
            message,
            title,
            0x40,
        )
        return result == 1
    except Exception:
        return True


def _show_startup_error(message: str) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "Arya FiveM Tool",
            0x10,
        )
    except Exception:
        pass


def _log_startup_error(message: str) -> None:
    try:
        log_path = Path(os.environ.get("TEMP", BASE_DIR)) / "arya_tool_startup.log"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


def main() -> None:
    try:
        os.chdir(BASE_DIR)
        ensure_gui_process()
        _console_watchdog()
        install_hidden_subprocess()
        free_local_port(PORT)
        threading.Thread(target=run_flask, daemon=True).start()
        if not wait_for_flask(timeout=20.0):
            raise RuntimeError("Local server did not start on port %s" % PORT)
        refresh_bootstrap_state()
        threading.Thread(target=bootstrap_worker, daemon=True).start()
        threading.Thread(target=wait_ui, daemon=True).start()
        open_ui()
    except Exception as exc:
        _show_startup_error(format_startup_error("Startup failed", exc))


if __name__ == "__main__":
    main()
