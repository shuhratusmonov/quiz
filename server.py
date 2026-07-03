#!/usr/bin/env python3
"""
Portals Offers — бэкенд-прокси.

Зачем нужен:
  * Браузер не даёт мини-аппу напрямую дёргать https://portals-market.com/api/
    (CORS). Прокси стоит на том же origin, что и страница, поэтому CORS нет.
  * Токен авторизации Portals (tma ...) хранится на сервере, а не в браузере.

Как запустить:
    export PORTALS_AUTH="ВАШ_ТОКЕН"     # без префикса 'tma ' — он добавится сам
    python3 server.py                   # откроется на http://localhost:8080

Где взять PORTALS_AUTH:
    web.telegram.org → откройте @portals → DevTools → вкладка Network →
    любой запрос на portals-market.com → заголовок Authorization →
    скопируйте всё ПОСЛЕ 'tma '. Токен живёт ~1-7 дней, потом обновить.

Без токена страница всё равно работает — в демо-режиме на мок-данных.

ВАЖНО: portals-market.com/api — неофициальный внутренний API. Он не
поддерживается Portals, может измениться в любой момент, а автоматические
офферы могут нарушать правила площадки. Используйте на свой риск.
"""

import http.server
import socketserver
import urllib.request
import urllib.error
import os
import json
import ssl

PORT = int(os.environ.get("PORT", "8080"))
PORTALS_AUTH = os.environ.get("PORTALS_AUTH", "").strip()
UPSTREAM = "https://portals-market.com/api/"
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

# Пути прокси → (метод по умолчанию не важен, метод берётся из запроса)
# Всё, что приходит на /portals-api/<path>, уходит на UPSTREAM<path>.
PROXY_PREFIX = "/portals-api/"

_ssl_ctx = ssl.create_default_context()


def _auth_header():
    return f"tma {PORTALS_AUTH}" if PORTALS_AUTH else ""


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    # --- отдаём короткий лог без шума ---
    def log_message(self, fmt, *args):
        print("·", self.command, self.path.split("?")[0])

    # --- статус подключения для фронта ---
    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method):
        # /portals-api/collections?limit=100 → https://portals-market.com/api/collections?limit=100
        rest = self.path[len(PROXY_PREFIX):]
        url = UPSTREAM + rest

        if not PORTALS_AUTH:
            self._send_json(401, {
                "error": "no_token",
                "message": "PORTALS_AUTH не задан. Запущено в демо-режиме."
            })
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None

        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", _auth_header())
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        # Portals ждёт «браузерные» заголовки
        req.add_header("Origin", "https://portals-market.com")
        req.add_header("Referer", "https://portals-market.com/")
        req.add_header(
            "User-Agent",
            "Mozilla/5.0 (compatible; PortalsOffers/1.0)"
        )

        try:
            with urllib.request.urlopen(req, context=_ssl_ctx, timeout=20) as r:
                data = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self._send_json(e.code, {
                "error": "upstream_http_error",
                "status": e.code,
                "body": data.decode("utf-8", "replace")[:500],
            })
        except Exception as e:
            self._send_json(502, {"error": "upstream_unreachable", "message": str(e)})

    def do_GET(self):
        if self.path == "/api/status":
            self._send_json(200, {"connected": bool(PORTALS_AUTH)})
            return
        if self.path.startswith(PROXY_PREFIX):
            self._proxy("GET")
            return
        # статика: / → portals.html
        if self.path == "/" or self.path == "":
            self.path = "/portals.html"
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith(PROXY_PREFIX):
            self._proxy("POST")
            return
        self._send_json(404, {"error": "not_found"})

    def do_PATCH(self):
        if self.path.startswith(PROXY_PREFIX):
            self._proxy("PATCH")
            return
        self._send_json(404, {"error": "not_found"})


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    mode = "РЕАЛЬНЫЙ API" if PORTALS_AUTH else "ДЕМО (мок-данные) — PORTALS_AUTH не задан"
    print(f"Portals Offers → http://localhost:{PORT}   [{mode}]")
    ThreadingServer(("0.0.0.0", PORT), Handler).serve_forever()
