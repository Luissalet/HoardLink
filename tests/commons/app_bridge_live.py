"""Run the App Dock host against a live Links Hoard.

python tests/commons/app_bridge_live.py --url http://127.0.0.1:5181 --out <evidence-dir>
Use --candidate before deployment to append the working bridge to the real launcher response.
The report records whether that browser-only overlay was used.
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5181")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--candidate", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    app_origin = "{uri.scheme}://{uri.netloc}".format(uri=urlsplit(args.url))
    html = """<!doctype html><html><title>App Dock verification</title><style>
      body{margin:0;background:#201e17;color:#ece9e1;font:15px/1.5 system-ui}header{padding:12px 20px}
      iframe{display:block;width:100%;height:calc(100vh - 48px);border:0}
      </style><header>App Dock · Links Hoard</header><iframe></iframe><script>
      window.messages=[];window.pending=new Map();window.counter=0;
      const frame=document.querySelector('iframe');
      window.send=(type,fields={})=>frame.contentWindow.postMessage({hoardDock:1,type,...fields},APP_ORIGIN);
      addEventListener('message',e=>{if(e.source!==frame.contentWindow||e.origin!==APP_ORIGIN)return;
        messages.push(e.data);if(e.data.type==='dock/result'){pending.get(e.data.id)?.(e.data);pending.delete(e.data.id)}});
      frame.addEventListener('load',()=>send('dock/hello',{host:'faustus',session:'bridge-live',theme:'dark',locale:'es'}));
      frame.src=APP_URL;
      window.call=(tool,args={},actor={id:'one',name:'Agent one',color:'#b7472a'})=>new Promise(resolve=>{
        const id=String(++counter);pending.set(id,resolve);send('dock/call',{id,tool,args,actor})});
      </script></html>""".replace("APP_ORIGIN", json.dumps(app_origin)).replace("APP_URL", json.dumps(args.url))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report = {"url": args.url, "candidate_overlay": args.candidate, "calls": [], "screenshots": []}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            if args.candidate:
                def launcher(route):
                    response = route.fetch()
                    script = response.text() + "\n" + (ROOT / "js/hoard-commons/app-bridge.js").read_text(encoding="utf-8")
                    route.fulfill(response=response, body=script)
                page.route("**/api/copilot/launcher.js", launcher)
            page.goto(f"http://127.0.0.1:{server.server_port}")
            page.wait_for_function("messages.some(m=>m.type==='dock/ready')", timeout=15000)
            report["ready"] = page.evaluate("messages.find(m=>m.type==='dock/ready')")
            frame = page.frames[1]
            frame.locator('input[aria-label="Buscar enlaces"]').wait_for()
            report["floating_button_hidden"] = not frame.locator(".hoard-copilot-toggle").is_visible()

            def call(tool, values):
                result = page.evaluate("([tool,args])=>call(tool,args)", [tool, values])
                report["calls"].append({"tool": tool, "args": values, "response": result})
                assert result["ok"], result
                return result["result"]

            call("app_read", {"scope": "screen"})
            call("app_click", {"text": "Todo"})
            initial = frame.locator('input[aria-label="Buscar enlaces"]').input_value()
            call("app_fill", {"label": "Buscar enlaces", "value": "App Dock verification"})
            assert frame.locator('input[aria-label="Buscar enlaces"]').input_value() == "App Dock verification"
            results = page.evaluate("Promise.all([call('app_highlight',{text:'Buscar enlaces',note:'Search field'},{id:'one',name:'Agent one',color:'#b7472a'}),call('app_highlight',{text:'Favoritos',note:'Saved favourites'},{id:'two',name:'Agent two',color:'#256a91'})])")
            assert all(result["ok"] for result in results), results
            report["concurrent_highlights"] = results
            report["actors"] = frame.locator("[data-hoard-dock-layer] .cursor").evaluate_all("els=>els.map(e=>({id:e.dataset.actor,name:e.querySelector('.name').textContent,note:e.querySelector('.note').textContent,transform:e.style.transform}))")
            assert len(report["actors"]) == 2
            for width, height, name in [(1440, 960, "desktop"), (430, 900, "mobile")]:
                page.set_viewport_size({"width": width, "height": height})
                for theme in ["dark", "light"]:
                    page.evaluate("theme=>send('dock/theme',{theme})", theme)
                    page.wait_for_timeout(200)
                    path = args.out / f"{name}-{theme}.png"
                    page.screenshot(path=str(path))
                    report["screenshots"].append(str(path))
            page.set_viewport_size({"width": 1440, "height": 960})
            call("app_fill", {"label": "Buscar enlaces", "value": initial})
            report["restored_search"] = frame.locator('input[aria-label="Buscar enlaces"]').input_value() == initial
            report["page_errors"] = errors
            assert report["floating_button_hidden"] and report["restored_search"]
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "candidate_overlay": args.candidate, "actors": len(report["actors"]), "report": str(args.out / "report.json")}))


if __name__ == "__main__":
    main()
