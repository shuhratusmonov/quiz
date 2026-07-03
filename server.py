#!/usr/bin/env python3
"""
Portals Offers — бэкенд-прокси + движок авто-офферов.

Зачем нужен:
  * Браузер не даёт мини-аппу напрямую дёргать https://portals-market.com/api/
    (CORS). Прокси стоит на том же origin, что и страница, поэтому CORS нет.
  * Токен авторизации Portals (tma ...) хранится на сервере, а не в браузере.
  * Фоновый движок следит за флором и ставит офферы по правилам, даже когда
    приложение закрыто.

Как запустить:
    export PORTALS_AUTH="ВАШ_ТОКЕН"     # без префикса 'tma ' — добавится сам
    python3 server.py                   # http://localhost:8080

    # чтобы движок РЕАЛЬНО ставил офферы (иначе только наблюдение/лог):
    export AUTO_LIVE=1

Где взять PORTALS_AUTH:
    web.telegram.org → откройте @portals → DevTools → вкладка Network →
    любой запрос на portals-market.com → заголовок Authorization →
    скопируйте всё ПОСЛЕ 'tma '. Токен живёт ~1-7 дней, потом обновить.

Без токена страница работает в демо-режиме на мок-данных; движок простаивает.

ВАЖНО: portals-market.com/api — неофициальный внутренний API. Он не
поддерживается Portals, может измениться в любой момент, а автоматические
офферы могут нарушать правила площадки. Поэтому движок по умолчанию работает
в режиме НАБЛЮДЕНИЯ (пишет «сработало бы», ничего не отправляя). Реальные
офферы включаются только флагом AUTO_LIVE=1. Используйте на свой риск.
"""

import http.server
import socketserver
import urllib.request
import urllib.error
import os
import json
import ssl
import time
import threading
from collections import deque
from datetime import datetime

PORT = int(os.environ.get("PORT", "8080"))
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

# Токен берётся из переменной окружения PORTALS_AUTH, а если её нет —
# из файла token.txt рядом с сервером. Файл — самый надёжный способ на
# Windows: cmd искажает токен из-за символов % (URL-кодирование).
PORTALS_AUTH = os.environ.get("PORTALS_AUTH", "").strip()
if not PORTALS_AUTH:
    try:
        # utf-8-sig убирает BOM-метку, которую иногда добавляет Блокнот
        with open(os.path.join(STATIC_DIR, "token.txt"), encoding="utf-8-sig") as _f:
            PORTALS_AUTH = _f.read().strip()
    except FileNotFoundError:
        pass
# на случай, если скопировали строку целиком вместе с префиксом 'tma '
if PORTALS_AUTH.lower().startswith("tma "):
    PORTALS_AUTH = PORTALS_AUTH[4:].strip()

AUTO_LIVE = os.environ.get("AUTO_LIVE", "") not in ("", "0", "false", "False")
ENGINE_INTERVAL = int(os.environ.get("ENGINE_INTERVAL", "60"))   # сек между проверками
RULE_COOLDOWN = int(os.environ.get("RULE_COOLDOWN", "600"))      # сек между срабатываниями одного правила

UPSTREAM = "https://portals-market.com/api/"
RULES_FILE = os.path.join(STATIC_DIR, "rules.json")
PROXY_PREFIX = "/portals-api/"

_ssl_ctx = ssl.create_default_context()
_lock = threading.Lock()
_log = deque(maxlen=50)


# ----------------------------------------------------------------------------
# Общие помощники
# ----------------------------------------------------------------------------
def _auth_header():
    return f"tma {PORTALS_AUTH}" if PORTALS_AUTH else ""


def pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def call_portals(method, path, body=None):
    """Внутренний вызов Portals API (для движка). Возвращает (status, parsed)."""
    url = UPSTREAM + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", _auth_header())
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Origin", "https://portals-market.com")
    req.add_header("Referer", "https://portals-market.com/")
    req.add_header("User-Agent", "Mozilla/5.0 (compatible; PortalsOffers/1.0)")
    with urllib.request.urlopen(req, context=_ssl_ctx, timeout=20) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None)


def log_event(event, message, collection_id=None, amount=None):
    with _lock:
        _log.appendleft({
            "event": event,
            "message": message,
            "collection_id": collection_id,
            "amount": amount,
            "time": datetime.now().strftime("%H:%M:%S"),
        })


# ----------------------------------------------------------------------------
# Хранилище правил
# ----------------------------------------------------------------------------
def load_rules():
    try:
        with open(RULES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception:
        return []


def save_rules(rules):
    with _lock:
        with open(RULES_FILE, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)


def new_rule(data):
    return {
        "id": "r" + str(int(time.time() * 1000)),
        "col": data.get("col", ""),
        "maxPrice": float(data.get("maxPrice", 0) or 0),
        "expiration_days": int(data.get("expiration_days", 0) or 0),
        "max_nfts": int(data.get("max_nfts", 1) or 1),
        "enabled": bool(data.get("enabled", True)),
        "last_fired": 0,
    }


# ----------------------------------------------------------------------------
# Движок авто-офферов
# ----------------------------------------------------------------------------
def _floor_map():
    """{collection_id: floor} по данным /collections (та же схема, что на фронте)."""
    status, data = call_portals("GET", "collections?limit=200")
    arr = data if isinstance(data, list) else (
        pick(data, "collections", "data") or [])
    out = {}
    for c in arr:
        name = pick(c, "name", "short_name", "collection_name", "title") or "—"
        cid = str(pick(c, "id", "collection_id", "short_name", "name") or name)
        floor = float(pick(c, "floor_price", "floor", "floorPrice", "min_price") or 0)
        out[cid] = floor
    return out


def run_engine_once():
    rules = load_rules()
    active = [r for r in rules if r.get("enabled")]
    if not active:
        return

    floors = _floor_map()
    now = time.time()
    changed = False

    for r in rules:
        if not r.get("enabled"):
            continue
        floor = floors.get(r["col"])
        if not floor or floor <= 0:
            continue
        if floor > r["maxPrice"]:
            continue
        if now - r.get("last_fired", 0) < RULE_COOLDOWN:
            continue  # уже недавно срабатывало — не спамим

        if AUTO_LIVE:
            try:
                st, resp = call_portals("POST", "collection-offers/", {
                    "amount": str(r["maxPrice"]),
                    "collection_id": r["col"],
                    "expiration_days": r["expiration_days"],
                    "max_nfts": r["max_nfts"],
                })
                log_event("placed", f"Оффер поставлен (флор {floor})",
                          r["col"], r["maxPrice"])
            except Exception as e:
                log_event("error", f"Ошибка постановки: {e}", r["col"], r["maxPrice"])
        else:
            log_event("would_fire",
                      f"Сработало бы (флор {floor} ≤ {r['maxPrice']})",
                      r["col"], r["maxPrice"])

        r["last_fired"] = now
        changed = True

    if changed:
        save_rules(rules)


def engine_loop():
    while True:
        try:
            if PORTALS_AUTH:
                run_engine_once()
        except Exception as e:
            log_event("error", f"Сбой движка: {e}")
        time.sleep(ENGINE_INTERVAL)


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def log_message(self, fmt, *args):
        print("·", self.command, self.path.split("?")[0])

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return {}

    # --- сквозной прокси к Portals (для запросов данных с фронта) ---
    def _proxy(self, method):
        rest = self.path[len(PROXY_PREFIX):]
        if not PORTALS_AUTH:
            self._json(401, {"error": "no_token",
                             "message": "PORTALS_AUTH не задан. Демо-режим."})
            return
        body = None
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            body = self.rfile.read(length)
        req = urllib.request.Request(UPSTREAM + rest, data=body, method=method)
        req.add_header("Authorization", _auth_header())
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Origin", "https://portals-market.com")
        req.add_header("Referer", "https://portals-market.com/")
        req.add_header("User-Agent", "Mozilla/5.0 (compatible; PortalsOffers/1.0)")
        try:
            with urllib.request.urlopen(req, context=_ssl_ctx, timeout=20) as r:
                data = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self._json(e.code, {"error": "upstream_http_error", "status": e.code,
                                "body": e.read().decode("utf-8", "replace")[:500]})
        except Exception as e:
            self._json(502, {"error": "upstream_unreachable", "message": str(e)})

    # --- rule id из пути /api/rules/<id> ---
    def _rule_id(self):
        return self.path.rsplit("/", 1)[-1]

    def do_GET(self):
        p = self.path
        if p == "/api/status":
            self._json(200, {"connected": bool(PORTALS_AUTH)})
            return
        if p == "/api/rules/engine":
            self._json(200, {"live": bool(PORTALS_AUTH) and AUTO_LIVE,
                             "watching": bool(PORTALS_AUTH),
                             "interval": ENGINE_INTERVAL})
            return
        if p == "/api/rules/log":
            self._json(200, list(_log))
            return
        if p == "/api/rules":
            self._json(200, load_rules())
            return
        if p.startswith(PROXY_PREFIX):
            self._proxy("GET")
            return
        if p in ("/", ""):
            self.path = "/portals.html"
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/rules":
            rules = load_rules()
            r = new_rule(self._read_body())
            rules.append(r)
            save_rules(rules)
            self._json(200, r)
            return
        if self.path.startswith(PROXY_PREFIX):
            self._proxy("POST")
            return
        self._json(404, {"error": "not_found"})

    def do_PATCH(self):
        if self.path.startswith("/api/rules/"):
            rid = self._rule_id()
            body = self._read_body()
            rules = load_rules()
            for r in rules:
                if str(r["id"]) == str(rid):
                    for k in ("col", "maxPrice", "expiration_days", "max_nfts", "enabled"):
                        if k in body:
                            r[k] = body[k]
                    save_rules(rules)
                    self._json(200, r)
                    return
            self._json(404, {"error": "rule_not_found"})
            return
        if self.path.startswith(PROXY_PREFIX):
            self._proxy("PATCH")
            return
        self._json(404, {"error": "not_found"})

    def do_DELETE(self):
        if self.path.startswith("/api/rules/"):
            rid = self._rule_id()
            rules = [r for r in load_rules() if str(r["id"]) != str(rid)]
            save_rules(rules)
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not_found"})


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    if PORTALS_AUTH:
        mode = f"РЕАЛЬНЫЙ API · движок: {'LIVE (ставит офферы!)' if AUTO_LIVE else 'наблюдение'}"
    else:
        mode = "ДЕМО (мок-данные) — PORTALS_AUTH не задан"
    print(f"Portals Offers → http://localhost:{PORT}   [{mode}]")
    threading.Thread(target=engine_loop, daemon=True).start()
    ThreadingServer(("0.0.0.0", PORT), Handler).serve_forever()
