#!/usr/bin/env python3
"""
Парсер вторичного рынка Telegram — Fragment.com
Поддерживает: юзернеймы, анонимные номера, подарки

Установка:
  pip install playwright httpx rich
  (браузер уже установлен, дополнительно скачивать не нужно)

Использование:
  python3 fragment_parser.py                         # юзернеймы, 20 шт.
  python3 fragment_parser.py --type numbers          # номера
  python3 fragment_parser.py --type all --max 10     # всё по 10 шт.
  python3 fragment_parser.py --search crypto         # поиск юзернеймов
  python3 fragment_parser.py --min-price 50          # от 50 TON
  python3 fragment_parser.py --status auction        # только аукционы
  python3 fragment_parser.py --no-browser            # лёгкий режим (httpx)
"""

import asyncio
import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Optional, List

# ─── Цвета ───────────────────────────────────────────────────────────────────
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

CHROMIUM_PATH = "/opt/pw-browsers/chromium"  # путь в облачной среде Claude
BASE = "https://fragment.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    "Referer": "https://fragment.com/",
}


# ─── Модель данных ───────────────────────────────────────────────────────────

@dataclass
class Listing:
    kind: str              # username | number | gift
    title: str
    price_ton: Optional[float]
    status: str            # for_sale | auction | sold
    url: str
    bids: int = 0
    ends_at: Optional[str] = None


KIND_LABEL = {"username": "USERNAME", "number": "НОМЕР", "gift": "ПОДАРОК"}
STATUS_LABEL = {"for_sale": "ПРОДАЖА", "auction": "АУКЦИОН", "sold": "ПРОДАНО"}
STATUS_COLOR = {"for_sale": GREEN, "auction": YELLOW, "sold": RED}


def _price_str(val: Optional[float]) -> str:
    return f"{val:.2f} TON" if val is not None else "N/A"


def _parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


# ─── Парсер через Playwright (браузер) ───────────────────────────────────────

class BrowserParser:
    """Парсит Fragment.com через headless Chromium (самый надёжный режим)."""

    def __init__(self):
        self._pw = None
        self._browser = None

    async def start(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        # Пробуем предустановленный браузер, иначе — системный chromium
        import os
        exe = CHROMIUM_PATH if os.path.exists(CHROMIUM_PATH) else None
        kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-setuid-sandbox"]}
        if exe:
            kwargs["executable_path"] = exe
        self._browser = await self._pw.chromium.launch(**kwargs)

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def _page(self):
        ctx = await self._browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="ru-RU",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"},
        )
        page = await ctx.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        return page

    async def _extract_table(self, page, kind: str, max_items: int) -> List[Listing]:
        listings: List[Listing] = []
        try:
            await page.wait_for_selector(
                "table tbody tr, .table-cell-name", timeout=15000
            )
        except Exception:
            pass

        rows = await page.query_selector_all("table tbody tr")
        for row in rows[:max_items]:
            try:
                link = await row.query_selector("a")
                if not link:
                    continue
                title = (await link.inner_text()).strip()
                href = (await link.get_attribute("href")) or ""
                url = (BASE + href) if href.startswith("/") else href

                price_el = await row.query_selector(".tm-value")
                price_ton = _parse_price(
                    (await price_el.inner_text()).strip() if price_el else ""
                )

                status = "for_sale"
                bids = 0
                btn = await row.query_selector("[class*='status'], [class*='bid'], button")
                if btn:
                    t = (await btn.inner_text()).lower()
                    if any(w in t for w in ("bid", "ставк", "auc")):
                        status = "auction"
                    elif "sold" in t or "продан" in t:
                        status = "sold"

                bids_el = await row.query_selector("[class*='bids']")
                if bids_el:
                    raw = re.sub(r"\D", "", await bids_el.inner_text())
                    bids = int(raw) if raw else 0

                if title:
                    listings.append(
                        Listing(kind=kind, title=title, price_ton=price_ton,
                                status=status, url=url, bids=bids)
                    )
            except Exception:
                continue
        return listings

    async def parse_usernames(self, max_items: int, search: str = "") -> List[Listing]:
        page = await self._page()
        try:
            url = f"{BASE}/usernames" + (f"?query={search}" if search else "")
            await page.goto(url, wait_until="networkidle", timeout=30000)
            return await self._extract_table(page, "username", max_items)
        finally:
            await page.close()

    async def parse_numbers(self, max_items: int) -> List[Listing]:
        page = await self._page()
        try:
            await page.goto(f"{BASE}/numbers", wait_until="networkidle", timeout=30000)
            return await self._extract_table(page, "number", max_items)
        finally:
            await page.close()

    async def parse_gifts(self, max_items: int) -> List[Listing]:
        page = await self._page()
        try:
            await page.goto(f"{BASE}/gifts", wait_until="networkidle", timeout=30000)
            items = await self._extract_table(page, "gift", max_items)
            if not items:
                cards = await page.query_selector_all("[class*='gift'], [class*='item']")
                for card in cards[:max_items]:
                    try:
                        link = await card.query_selector("a")
                        if not link:
                            continue
                        title = (await link.inner_text()).strip()
                        href = (await link.get_attribute("href")) or ""
                        url = (BASE + href) if href.startswith("/") else href
                        pe = await card.query_selector(".tm-value, [class*='price']")
                        price = _parse_price((await pe.inner_text()) if pe else "")
                        if title:
                            items.append(Listing(kind="gift", title=title,
                                                 price_ton=price, status="for_sale", url=url))
                    except Exception:
                        continue
            return items
        finally:
            await page.close()


# ─── Лёгкий парсер через httpx (без браузера) ────────────────────────────────

class HttpParser:
    """
    Пробует получить данные через Fragment JSON API.
    Fragment не имеет публичного API, поэтому этот метод может работать
    только если сайт отдаёт данные в HTML/JSON без рендеринга JS.
    """

    def __init__(self):
        import httpx
        self.client = httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=20,
        )

    async def close(self):
        await self.client.aclose()

    async def _fetch(self, url: str) -> str:
        resp = await self.client.get(url)
        resp.raise_for_status()
        return resp.text

    def _extract_from_html(self, html: str, kind: str, max_items: int) -> List[Listing]:
        """Извлекает объявления из HTML-источника Fragment.com."""
        listings: List[Listing] = []

        # Fragment хранит данные в тэге <script> как JSON
        json_match = re.search(
            r'<script[^>]*>\s*var\s+frag\s*=\s*(\{.*?\})\s*;?\s*</script>',
            html, re.DOTALL
        )
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                items = data.get("items") or data.get("found") or []
                for item in items[:max_items]:
                    title = item.get("username") or item.get("title") or item.get("number", "")
                    price = _parse_price(str(item.get("price", item.get("bid", ""))))
                    status_raw = str(item.get("status", "")).lower()
                    status = ("auction" if "auc" in status_raw else
                              "sold" if "sold" in status_raw else "for_sale")
                    href = item.get("url", f"/{kind}/{title}")
                    url = (BASE + href) if href.startswith("/") else href
                    if title:
                        listings.append(Listing(kind=kind, title=title,
                                                price_ton=price, status=status, url=url))
                if listings:
                    return listings
            except (json.JSONDecodeError, KeyError):
                pass

        # Fallback: парсим HTML-таблицу без JS
        from html.parser import HTMLParser

        class TableParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.rows: List[dict] = []
                self._in_row = False
                self._cur: dict = {}
                self._cur_tag = ""
                self._text = ""

            def handle_starttag(self, tag, attrs):
                attrs_d = dict(attrs)
                if tag == "tr":
                    self._in_row = True
                    self._cur = {"href": "", "name": "", "price": ""}
                elif tag == "a" and self._in_row:
                    self._cur["href"] = attrs_d.get("href", "")
                self._cur_tag = tag
                self._text = ""

            def handle_data(self, data):
                if not self._in_row:
                    return
                t = data.strip()
                if not t:
                    return
                if self._cur_tag == "a" and not self._cur["name"]:
                    self._cur["name"] = t
                elif re.match(r"^\d[\d.,]*$", t):
                    self._cur["price"] = t

            def handle_endtag(self, tag):
                if tag == "tr" and self._in_row:
                    if self._cur.get("name"):
                        self.rows.append(dict(self._cur))
                    self._in_row = False

        tp = TableParser()
        tp.feed(html)
        for row in tp.rows[:max_items]:
            href = row["href"]
            url = (BASE + href) if href.startswith("/") else (href or BASE)
            listings.append(Listing(
                kind=kind,
                title=row["name"],
                price_ton=_parse_price(row["price"]),
                status="for_sale",
                url=url,
            ))
        return listings

    async def parse_usernames(self, max_items: int, search: str = "") -> List[Listing]:
        url = f"{BASE}/usernames" + (f"?query={search}" if search else "")
        html = await self._fetch(url)
        return self._extract_from_html(html, "username", max_items)

    async def parse_numbers(self, max_items: int) -> List[Listing]:
        html = await self._fetch(f"{BASE}/numbers")
        return self._extract_from_html(html, "number", max_items)

    async def parse_gifts(self, max_items: int) -> List[Listing]:
        html = await self._fetch(f"{BASE}/gifts")
        return self._extract_from_html(html, "gift", max_items)


# ─── Вывод ───────────────────────────────────────────────────────────────────

def _try_rich_table(listings: List[Listing], section: str):
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.text import Text
        from rich import box
        con = Console()
        t = Table(title=section, box=box.SIMPLE_HEAVY, show_lines=True,
                  header_style="bold cyan")
        t.add_column("#", style="dim", width=4)
        t.add_column("Тип", width=10)
        t.add_column("Название", style="bold")
        t.add_column("TON", justify="right")
        t.add_column("Статус", justify="center")
        t.add_column("URL")
        styles = {"for_sale": "green", "auction": "yellow", "sold": "red"}
        for i, l in enumerate(listings, 1):
            t.add_row(
                str(i),
                KIND_LABEL.get(l.kind, l.kind),
                l.title,
                f"{l.price_ton:.2f}" if l.price_ton is not None else "—",
                Text(STATUS_LABEL.get(l.status, l.status), style=styles.get(l.status, "")),
                l.url,
            )
        con.print(t)
        return True
    except ImportError:
        return False


def print_results(listings: List[Listing], section: str = ""):
    print(f"\n{BOLD}{'─'*56}{RESET}")
    print(f"{BOLD}  {section}{RESET}  ({len(listings)} шт.)")
    print(f"{BOLD}{'─'*56}{RESET}")

    if not listings:
        print("  Нет результатов.")
        return

    if _try_rich_table(listings, section):
        return

    for i, l in enumerate(listings, 1):
        color = STATUS_COLOR.get(l.status, RESET)
        extra = f"  Ставок: {l.bids}" if l.bids else ""
        print(
            f"  {CYAN}{i:>3}.{RESET} {BOLD}{l.title}{RESET}\n"
            f"       Цена: {_price_str(l.price_ton)}  "
            f"{color}{STATUS_LABEL.get(l.status, l.status)}{RESET}{extra}\n"
            f"       {l.url}"
        )
        print()


# ─── Фильтр ──────────────────────────────────────────────────────────────────

def apply_filters(
    listings: List[Listing],
    min_price: float,
    max_price: float,
    status_filter: Optional[str],
) -> List[Listing]:
    out = listings
    if min_price > 0 or max_price < float("inf"):
        out = [l for l in out if l.price_ton is not None
               and min_price <= l.price_ton <= max_price]
    if status_filter:
        out = [l for l in out if l.status == status_filter]
    return out


# ─── Точка входа ─────────────────────────────────────────────────────────────

async def main():
    ap = argparse.ArgumentParser(
        description="Парсер вторичного рынка Telegram (Fragment.com)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--type", choices=["usernames", "numbers", "gifts", "all"],
                    default="usernames", help="Раздел (default: usernames)")
    ap.add_argument("--max", type=int, default=20, help="Кол-во записей (default: 20)")
    ap.add_argument("--search", default="", help="Поиск (только для usernames)")
    ap.add_argument("--min-price", type=float, default=0, help="Мин. цена в TON")
    ap.add_argument("--max-price", type=float, default=float("inf"), help="Макс. цена в TON")
    ap.add_argument("--status", choices=["for_sale", "auction", "sold"],
                    default=None, help="Фильтр по статусу")
    ap.add_argument("--no-browser", action="store_true",
                    help="Не использовать браузер (httpx, быстрее но менее надёжно)")
    args = ap.parse_args()

    print(f"\n{BOLD}Fragment.com Marketplace Parser{RESET}")
    parts = [f"раздел={args.type}", f"макс={args.max}"]
    if args.search:
        parts.append(f"поиск={args.search!r}")
    if args.min_price > 0:
        parts.append(f"мин {args.min_price} TON")
    if args.max_price < float("inf"):
        parts.append(f"макс {args.max_price} TON")
    if args.status:
        parts.append(f"статус={args.status}")
    mode = "httpx (без браузера)" if args.no_browser else "Playwright (браузер)"
    parts.append(f"режим={mode}")
    print("  " + " | ".join(parts) + "\n")

    sections = (["usernames", "numbers", "gifts"] if args.type == "all"
                else [args.type])
    section_labels = {"usernames": "Юзернеймы", "numbers": "Анонимные номера",
                      "gifts": "Подарки"}

    if args.no_browser:
        import httpx as _httpx  # noqa: F401 — проверка наличия
        parser = HttpParser()
        async_stop = parser.close
    else:
        parser = BrowserParser()
        await parser.start()
        async_stop = parser.stop

    try:
        total = 0
        for section in sections:
            if section == "usernames":
                raw = await parser.parse_usernames(args.max, search=args.search)
            elif section == "numbers":
                raw = await parser.parse_numbers(args.max)
            else:
                raw = await parser.parse_gifts(args.max)

            filtered = apply_filters(raw, args.min_price, args.max_price, args.status)
            print_results(filtered, section_labels[section])
            total += len(filtered)

        if len(sections) > 1:
            print(f"\n  {BOLD}Всего: {total} объявлений{RESET}\n")

    except KeyboardInterrupt:
        print("\nПрервано.")
    except Exception as exc:
        print(f"\n{RED}Ошибка: {exc}{RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        await async_stop()


if __name__ == "__main__":
    asyncio.run(main())
