# Arya FiveM Tool

Local panel for FiveM. Write JS, hit run, watch traffic, block URLs — all from one window.

## Run it

```bash
pip install -r requirements.txt
python main.py
```

1. Open FiveM and join a server
2. Run `python main.py`
3. Wait until the panel says **Ready**
4. Done

Needs Python 3.10+ and Windows.

---

## How the JS injection works

FiveM runs its UI (NUI) inside Chromium. That browser exposes **Chrome DevTools** on your PC at `localhost:13172` — same thing you'd use to inspect a website.

This tool hooks into that:

```
You type JS in the panel
        ↓
main.py finds the FiveM "CitizenFX root UI" tab
        ↓
Opens a WebSocket to that tab's debugger
        ↓
Sends your code via CDP (Chrome DevTools Protocol)
        ↓
Runtime.evaluate runs it inside the game UI
```

**In plain terms:** the tool talks to FiveM's built-in browser like DevTools would, and runs your JavaScript in the game's UI context.

### The iframe part

FiveM's root UI loads the actual game UI inside an `<iframe>`. So before your code runs, the tool wraps it like this:

1. Wait for the iframe to exist
2. Call `eval()` on `iframe.contentWindow`
3. Your script runs inside the NUI — same place menus, HUD, and fetch calls live

That's why you can do stuff like `fetch("https://...")` and it hits the server's NUI callbacks.

### Traffic monitor + blocker

Same DevTools connection, different trick:

- **Monitor** — listens to `Network.*` events over the WebSocket and shows requests in the panel
- **Blocker** — tells DevTools to block URLs matching your patterns before they load

No external hooks, no DLL injection. Just the debug port FiveM already opens locally.

---

## What's in the panel

- **Traffic** — see NUI/network requests live
- **Executor** — write and run JS, save scripts
- **Blocker** — block URLs by pattern
- **vRP tools** — pull players, ban, jail, message, bank transfer (server-specific)

---

## If something breaks

- **"Waiting for FiveM"** → open FiveM first, then join a server
- **Panel won't open** → install [WebView2](https://developer.microsoft.com/microsoft-edge/webview2/) or it'll fall back to your browser
- **Injection fails** → make sure you're actually in a server (root UI tab needs to be loaded)
- **Port in use** → close any other copy of the tool
