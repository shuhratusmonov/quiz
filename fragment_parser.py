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

Отправка подарков в Telegram-канал:
  python3 fragment_parser.py --type gifts --token BOT_TOKEN --channel @mychannel
  python3 fragment_parser.py --type gifts --token BOT_TOKEN --channel -1001234567890 --max 5
"""

import asyncio
import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Tuple

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


@dataclass
class StarGift:
    """Подарок из внутреннего рынка Telegram Stars."""
    model: str
    rarity: Optional[float]       # процент редкости, напр. 3.0
    backdrop: str
    stars_price: Optional[int]    # цена в Stars ⭐
    ton_price: Optional[float]    # примерная цена в TON
    xgift: Optional[float]        # оценка xgift
    seller: str                   # @username продавца
    buy_url: str = ""             # ссылка для кнопки (опционально)
    pattern: str = ""             # узор/скин подарка
    model_name: str = ""          # атрибут "модель" (напр. Resistant)
    avg_stars: Optional[float] = None   # среднее за последние N продаж
    avg_count: int = 0                  # сколько продаж учтено в среднем
    floor_stars: Optional[int] = None   # флор (самый дешёвый другой на рынке)
    discount_pct: Optional[float] = None  # на сколько % ниже флора

    @staticmethod
    def parse(text: str) -> "StarGift":
        """Парсит текст в формате:
            Model:  Ocean Oasis
            рарность:  3.0%
            Backdrop:  Lemongrass
            цена:  540 ⭐  ≈ 4.71 TON
            Xgift  •  5.16
            продавец:  @Rrrruiojg
        """
        def find(pattern: str) -> str:
            m = re.search(pattern, text, re.IGNORECASE)
            return m.group(1).strip() if m else ""

        model    = find(r"Model\s*[:\•]\s*(.+)")
        backdrop = find(r"Backdrop\s*[:\•]\s*(.+)")
        seller   = find(r"продав\w*\s*[:\•]\s*(@\S+)")

        rarity_raw = find(r"рар\w*\s*[:\•]\s*([\d.]+)\s*%")
        rarity = float(rarity_raw) if rarity_raw else None

        stars_raw = find(r"цена\s*[:\•]\s*([\d\s]+)\s*[⭐✨]")
        stars_price = int(re.sub(r"\D", "", stars_raw)) if stars_raw else None

        ton_raw = find(r"≈\s*([\d.]+)\s*TON")
        ton_price = float(ton_raw) if ton_raw else None

        xgift_raw = find(r"[Xx]gift\s*[•·:]\s*([\d.]+)")
        xgift = float(xgift_raw) if xgift_raw else None

        # Ссылка на подарок — если передана в тексте
        buy_url = find(r"(https?://\S+)")

        return StarGift(
            model=model,
            rarity=rarity,
            backdrop=backdrop,
            stars_price=stars_price,
            ton_price=ton_price,
            xgift=xgift,
            seller=seller,
            buy_url=buy_url,
        )


# ─── История цен ─────────────────────────────────────────────────────────────

class PriceHistory:
    """SQLite-хранилище истории цен подарков.
    Файл gift_prices.db создаётся автоматически рядом со скриптом.
    """

    DEFAULT_DB = "gift_prices.db"

    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        self._init()

    def _init(self):
        with sqlite3.connect(self.db_path) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS gift_prices (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    model       TEXT    NOT NULL,
                    stars_price INTEGER,
                    ton_price   REAL,
                    seller      TEXT,
                    recorded_at TEXT    DEFAULT (datetime('now','localtime'))
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_model ON gift_prices(model)")
            # Учёт уже отправленных в канал объявлений (антиповтор)
            c.execute("""
                CREATE TABLE IF NOT EXISTS sent_gifts (
                    gift_key  TEXT PRIMARY KEY,
                    sent_at   TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            # Зафиксированные продажи (лот исчез с рынка)
            c.execute("""
                CREATE TABLE IF NOT EXISTS sales (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection  TEXT NOT NULL,
                    slug        TEXT,
                    stars_price INTEGER,
                    sold_at     TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_sales_coll ON sales(collection)")
            c.commit()

    def record(self, gift: "StarGift"):
        """Сохраняет текущую цену в историю."""
        if gift.stars_price is None and gift.ton_price is None:
            return
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT INTO gift_prices (model, stars_price, ton_price, seller) "
                "VALUES (?, ?, ?, ?)",
                (gift.model.strip(), gift.stars_price, gift.ton_price, gift.seller),
            )
            c.commit()

    def last_n(self, model: str, n: int = 10) -> List[Tuple[int, str]]:
        """Возвращает последние N записей (stars_price, recorded_at) для модели."""
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(
                "SELECT stars_price, recorded_at FROM gift_prices "
                "WHERE model = ? AND stars_price IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (model.strip(), n),
            ).fetchall()
        return rows  # [(stars, date), ...]

    def avg_stars(self, model: str, n: int = 10) -> Tuple[Optional[float], int]:
        """Среднее арифметическое Stars по последним N продажам.
        Возвращает (среднее, кол-во учтённых записей).
        """
        rows = self.last_n(model, n)
        prices = [r[0] for r in rows if r[0] is not None]
        if not prices:
            return None, 0
        return sum(prices) / len(prices), len(prices)

    def enrich(self, gift: "StarGift", n: int = 10) -> "StarGift":
        """Вычисляет среднее и записывает его в gift.avg_stars / avg_count.
        Вызывать ДО record(), чтобы текущая цена не вошла в среднее.
        """
        avg, count = self.avg_stars(gift.model, n)
        gift.avg_stars = avg
        gift.avg_count = count
        return gift

    def record_sale(self, collection: str, slug: str, stars_price: Optional[int]):
        """Фиксирует продажу (лот исчез с рынка)."""
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT INTO sales (collection, slug, stars_price) VALUES (?, ?, ?)",
                (collection.strip(), slug, stars_price),
            )
            c.commit()

    def recent_sales(self, collection: str, n: int = 30) -> List[Tuple[int, str]]:
        """Последние N продаж коллекции: [(stars_price, sold_at), ...] новые первыми."""
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(
                "SELECT stars_price, sold_at FROM sales "
                "WHERE collection = ? AND stars_price IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (collection.strip(), n),
            ).fetchall()
        return rows

    def was_sent(self, gift_key: str) -> bool:
        """Проверяет, отправлялось ли уже это объявление в канал."""
        if not gift_key:
            return False
        with sqlite3.connect(self.db_path) as c:
            row = c.execute(
                "SELECT 1 FROM sent_gifts WHERE gift_key = ?", (gift_key,)
            ).fetchone()
        return row is not None

    def mark_sent(self, gift_key: str):
        """Помечает объявление как отправленное."""
        if not gift_key:
            return
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT OR IGNORE INTO sent_gifts (gift_key) VALUES (?)", (gift_key,)
            )
            c.commit()

    def total(self, model: str) -> int:
        """Общее кол-во записей для модели."""
        with sqlite3.connect(self.db_path) as c:
            row = c.execute(
                "SELECT COUNT(*) FROM gift_prices WHERE model = ?", (model.strip(),)
            ).fetchone()
        return row[0] if row else 0


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


# ─── Telegram-уведомления ────────────────────────────────────────────────────

GIFT_EMOJI = {
    "for_sale": "🟢",
    "auction": "🔶",
    "sold": "🔴",
}

STATUS_RU = {"for_sale": "Продажа", "auction": "Аукцион", "sold": "Продано"}


class TelegramNotifier:
    """Отправляет объявления о подарках в Telegram-канал через Bot API."""

    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str, channel: str):
        self.token = token
        self.channel = channel  # @channelusername или -100xxxxxxx

    def _url(self, method: str) -> str:
        return self.API.format(token=self.token, method=method)

    # ── Форматтеры ────────────────────────────────────────────────────────────

    @staticmethod
    def _format_star_gift(g: "StarGift") -> str:
        """Форматирует StarGift в красивое Telegram-сообщение."""
        price_str = ""
        if g.stars_price is not None:
            price_str = f"{g.stars_price} ⭐"
            if g.ton_price is not None:
                price_str += f"  ≈  {g.ton_price:.2f} TON"
        elif g.ton_price is not None:
            price_str = f"{g.ton_price:.2f} TON"
        else:
            price_str = "—"

        lines = [
            f"🎁 <b>{g.model}</b>",
            "",
        ]
        if g.model_name:
            lines.append(f"🧬  Модель:      <b>{g.model_name}</b>")
        lines.append(f"🎨  Backdrop:    <b>{g.backdrop}</b>")
        if g.pattern:
            lines.append(f"🌀  Узор:        <b>{g.pattern}</b>")
        lines.append(f"💰  Цена:        <b>{price_str}</b>")
        if g.seller:
            lines.append(f"👤  Продавец:   <b>{g.seller}</b>")

        # Флор и скидка относительно рынка (только если есть реальная выгода)
        if g.floor_stars is not None and g.discount_pct is not None and g.discount_pct > 0:
            lines.append(f"🏷  Флор:        <b>{g.floor_stars} ⭐</b>")
            lines.append(f"🔥  Выгода:      <b>−{g.discount_pct:.0f}% от флора</b>")

        return "\n".join(lines)

    @staticmethod
    def _format_fragment_gift(lst: "Listing") -> str:
        """Форматирует Fragment.com Listing для канала."""
        emoji = GIFT_EMOJI.get(lst.status, "🎁")
        status = STATUS_RU.get(lst.status, lst.status)
        price = f"{lst.price_ton:.2f} TON" if lst.price_ton is not None else "N/A"
        lines = [
            f"🎁 <b>{lst.title}</b>",
            "",
            f"💎 Цена: <b>{price}</b>",
            f"{emoji} Статус: {status}",
        ]
        if lst.bids:
            lines.append(f"🏷 Ставок: {lst.bids}")
        if lst.ends_at:
            lines.append(f"⏰ До: {lst.ends_at}")
        lines += ["", f'🔗 <a href="{lst.url}">Смотреть на Fragment</a>']
        return "\n".join(lines)

    @staticmethod
    def _inline_button(text: str, url: str) -> dict:
        return {"inline_keyboard": [[{"text": text, "url": url}]]}

    # ── Отправка ──────────────────────────────────────────────────────────────

    async def _send_message(self, text: str, reply_markup: dict = None) -> bool:
        import httpx
        payload: dict = {
            "chat_id": self.channel,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(self._url("sendMessage"), json=payload)
            data = resp.json()
            if not data.get("ok"):
                print(f"  {RED}TG API: {data.get('description', resp.text)}{RESET}")
                return False
            return True
        except Exception as exc:
            print(f"  {RED}Ошибка: {exc}{RESET}")
            return False

    async def send_star_gift(self, gift: "StarGift") -> bool:
        """Отправляет StarGift (внутренний рынок TG) в канал."""
        markup = None
        if gift.buy_url:
            markup = self._inline_button("🎁 Открыть подарок", gift.buy_url)
        return await self._send_message(self._format_star_gift(gift), markup)

    async def send_fragment_gift(self, listing: "Listing") -> bool:
        """Отправляет Fragment.com подарок в канал."""
        markup = self._inline_button("🛒 Купить на Fragment", listing.url)
        return await self._send_message(self._format_fragment_gift(listing), markup)

    async def send_all_star_gifts(self, gifts: List["StarGift"], delay: float = 0.5) -> int:
        sent = 0
        for g in gifts:
            ok = await self.send_star_gift(g)
            if ok:
                sent += 1
                print(f"  {GREEN}✓{RESET} Отправлено: {g.model}")
            if delay and g is not gifts[-1]:
                await asyncio.sleep(delay)
        return sent

    async def send_all(self, listings: List["Listing"], delay: float = 0.5) -> int:
        """Отправляет список Fragment-объявлений."""
        sent = 0
        for lst in listings:
            ok = await self.send_fragment_gift(lst)
            if ok:
                sent += 1
                print(f"  {GREEN}✓{RESET} Отправлено: {lst.title}")
            if delay and lst is not listings[-1]:
                await asyncio.sleep(delay)
        return sent


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

    tg = ap.add_argument_group("Telegram-канал")
    tg.add_argument("--token", default="", metavar="BOT_TOKEN",
                    help="Токен бота (получить у @BotFather)")
    tg.add_argument("--channel", default="", metavar="CHANNEL",
                    help="ID или @username канала (бот должен быть администратором)")
    tg.add_argument("--delay", type=float, default=0.5, metavar="SEC",
                    help="Пауза между сообщениями в секундах (default: 0.5)")

    manual = ap.add_argument_group(
        "Ручная отправка одного подарка (--send-gift)",
        "Принимает текст в формате: Model / рарность / Backdrop / цена / Xgift / продавец"
    )
    manual.add_argument("--send-gift", default="", metavar="TEXT",
                        help="Текст подарка для разовой отправки в канал")
    manual.add_argument("--buy-url", default="", metavar="URL",
                        help="Ссылка на подарок (кнопка 'Купить')")
    manual.add_argument("--db-path", default=PriceHistory.DEFAULT_DB, metavar="FILE",
                        help=f"Файл истории цен SQLite (default: {PriceHistory.DEFAULT_DB})")

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
    if args.token and args.channel:
        parts.append(f"→ {args.channel}")
    print("  " + " | ".join(parts) + "\n")

    # Валидация Telegram-параметров
    notifier: Optional[TelegramNotifier] = None
    if args.token or args.channel or args.send_gift:
        if not args.token:
            print(f"{RED}Ошибка: укажите --token BOT_TOKEN{RESET}")
            sys.exit(1)
        if not args.channel:
            print(f"{RED}Ошибка: укажите --channel @username или ID{RESET}")
            sys.exit(1)
        notifier = TelegramNotifier(token=args.token, channel=args.channel)

    # ── Режим ручной отправки одного подарка ─────────────────────────────────
    if args.send_gift:
        if not notifier:
            print(f"{RED}Ошибка: для --send-gift нужен --token и --channel{RESET}")
            sys.exit(1)
        gift = StarGift.parse(args.send_gift)
        if args.buy_url:
            gift.buy_url = args.buy_url

        # История цен
        history = PriceHistory(args.db_path)
        history.enrich(gift, n=10)   # считаем среднее ДО записи текущей
        history.record(gift)          # сохраняем текущую цену

        print(f"\n{BOLD}Подарок для отправки:{RESET}")
        print(f"  Модель:    {gift.model or '—'}")
        print(f"  Рарность:  {gift.rarity}%" if gift.rarity else "  Рарность:  —")
        print(f"  Backdrop:  {gift.backdrop or '—'}")
        print(f"  Цена:      {gift.stars_price} ⭐  ≈  {gift.ton_price} TON"
              if gift.stars_price else f"  Цена:      {gift.ton_price} TON")
        print(f"  Xgift:     {gift.xgift}" if gift.xgift else "")
        print(f"  Продавец:  {gift.seller or '—'}")
        print(f"  Ссылка:    {gift.buy_url or '—'}")
        if gift.avg_stars is not None:
            print(f"  Ср. цена:  {gift.avg_stars:.0f} ⭐  по {gift.avg_count} прод.")
        else:
            print(f"  Ср. цена:  нет данных (первая запись)")
        print(f"\n{BOLD}Отправка в {args.channel}...{RESET}")
        ok = await notifier.send_star_gift(gift)
        if ok:
            print(f"{GREEN}✓ Успешно отправлено!{RESET}")
        sys.exit(0 if ok else 1)

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

            # Отправка подарков в канал
            if notifier and section == "gifts" and filtered:
                gifts_only = [l for l in filtered if l.kind == "gift"]
                if gifts_only:
                    print(f"\n{BOLD}Отправка в {args.channel}...{RESET}")
                    sent = await notifier.send_all(gifts_only, delay=args.delay)
                    print(f"  Отправлено {sent}/{len(gifts_only)} сообщений.\n")

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
