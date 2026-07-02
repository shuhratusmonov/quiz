#!/usr/bin/env python3
"""
Парсер ОФИЦИАЛЬНОГО вторичного рынка подарков Telegram (resale Star Gifts).

Берёт данные напрямую из Telegram MTProto API (payments.getResaleStarGifts)
через Telethon — это тот самый встроенный рынок перепродажи уникальных
коллекционных подарков (модель, backdrop, редкость, цена в Stars/TON,
продавец). Опционально считает среднюю цену и постит объявления в канал
через бота.

────────────────────────────────────────────────────────────────────────────
ПОДГОТОВКА (один раз):

1. Получите api_id и api_hash:
     https://my.telegram.org  →  "API development tools"  →  создать приложение
2. Установите зависимости:
     pip install telethon httpx
3. Первый запуск попросит номер телефона и код из Telegram (создаст файл
   сессии gifts.session — больше код вводить не нужно).

────────────────────────────────────────────────────────────────────────────
ИСПОЛЬЗОВАНИЕ:

  # Показать все коллекции, где есть перепродажа (узнать названия):
  python3 gifts_resale_parser.py --api-id 12345 --api-hash abcdef --list

  # Парсить ВСЕ коллекции, по 10 самых дешёвых, вывод в консоль:
  python3 gifts_resale_parser.py --api-id 12345 --api-hash abcdef --all --max 10

  # Только одну коллекцию:
  python3 gifts_resale_parser.py --api-id 12345 --api-hash abcdef \
      --collection "Plush Pepe" --max 20

  # Парсить всё И постить в канал через бота:
  python3 gifts_resale_parser.py --api-id 12345 --api-hash abcdef --all \
      --token BOT_TOKEN --channel @abcuzbek

  # Только выгодные (ниже средней цены по истории):
  python3 gifts_resale_parser.py --api-id 12345 --api-hash abcdef --all \
      --below-average --token BOT_TOKEN --channel @abcuzbek

Ключи можно задать и через переменные окружения:
  TG_API_ID, TG_API_HASH, BOT_TOKEN
"""

import argparse
import asyncio
import logging
import os
import re
import sys
from typing import List, Optional, Tuple

# Приглушаем служебный лог Telethon (PersistentTimestampOutdatedError и пр.) —
# это внутренний шум обновлений, на работу парсера не влияет.
logging.getLogger("telethon").setLevel(logging.CRITICAL)

# Переиспользуем готовые модели из fragment_parser.py
try:
    from fragment_parser import (
        StarGift, PriceHistory, TelegramNotifier,
        GREEN, YELLOW, RED, CYAN, BOLD, RESET,
    )
except ImportError:
    print("Ошибка: рядом должен лежать файл fragment_parser.py")
    sys.exit(1)


def _require_telethon():
    try:
        from telethon import TelegramClient  # noqa: F401
        from telethon.tl.functions.payments import (  # noqa: F401
            GetStarGiftsRequest, GetResaleStarGiftsRequest,
        )
    except ImportError:
        print(f"{RED}Ошибка: не установлен Telethon. Выполните: pip install telethon{RESET}")
        sys.exit(1)


# ─── Конвертация Telegram-объектов в наш StarGift ────────────────────────────

def _stars_from_amount(amount_list) -> Optional[int]:
    """Извлекает цену в Stars из resell_amount (Vector<StarsAmount|StarsTonAmount>)."""
    if not amount_list:
        return None
    for a in amount_list:
        # StarsAmount: amount (целые звёзды) + nanos (дробная часть, 1e-9)
        if type(a).__name__ == "StarsAmount":
            whole = getattr(a, "amount", 0) or 0
            nanos = getattr(a, "nanos", 0) or 0
            return int(round(whole + nanos / 1e9))
    return None


def _ton_from_amount(amount_list) -> Optional[float]:
    """Извлекает цену в TON из resell_amount (StarsTonAmount, в нано-TON)."""
    if not amount_list:
        return None
    for a in amount_list:
        if type(a).__name__ == "StarsTonAmount":
            nano = getattr(a, "amount", 0) or 0
            return round(nano / 1e9, 2)
    return None


def _attr(attributes, type_suffix: str):
    """Находит атрибут по типу (Model / Backdrop / Pattern)."""
    for a in attributes or []:
        if type(a).__name__.endswith(type_suffix):
            return a
    return None


def _rarity_percent(attr) -> Optional[float]:
    """Извлекает редкость в процентах из атрибута.
    Поле rarity может быть числом (промилле) или объектом
    StarGiftAttributeRarity с полем .permille. 30‰ → 3.0%."""
    if attr is None:
        return None
    r = getattr(attr, "rarity", None)
    if r is None:
        return None
    # Объект StarGiftAttributeRarity → берём .permille
    permille = getattr(r, "permille", r)
    try:
        return round(float(permille) / 10.0, 1)
    except (TypeError, ValueError):
        return None


def _seller_name(unique, users_by_id: dict) -> str:
    """Определяет продавца: @username, имя или адрес."""
    owner_name = getattr(unique, "owner_name", None)
    owner_id = getattr(unique, "owner_id", None)

    # Пытаемся достать @username из объекта peer
    uid = None
    if owner_id is not None:
        uid = getattr(owner_id, "user_id", None) or owner_id
    user = users_by_id.get(uid) if uid is not None else None
    if user is not None:
        username = getattr(user, "username", None)
        if username:
            return f"@{username}"
        first = getattr(user, "first_name", "") or ""
        last = getattr(user, "last_name", "") or ""
        full = (first + " " + last).strip()
        if full:
            return full

    if owner_name:
        return owner_name
    addr = getattr(unique, "owner_address", None)
    return addr or "—"


def unique_to_stargift(unique, users_by_id: dict) -> StarGift:
    """Конвертирует Telegram StarGiftUnique → наш StarGift."""
    attrs = getattr(unique, "attributes", []) or []

    model_a = _attr(attrs, "Model")
    backdrop_a = _attr(attrs, "Backdrop")
    pattern_a = _attr(attrs, "Pattern")

    model_name = getattr(model_a, "name", "") if model_a else ""
    backdrop_name = getattr(backdrop_a, "name", "") if backdrop_a else ""
    pattern_name = getattr(pattern_a, "name", "") if pattern_a else ""

    # rarity в API хранится в промилле (‰): 30 → 3.0%
    # Берём редкость модели, а если её нет — backdrop'а
    rarity = _rarity_percent(model_a)
    if rarity is None:
        rarity = _rarity_percent(backdrop_a)

    title = getattr(unique, "title", "") or model_name
    num = getattr(unique, "num", None)
    full_title = f"{title} #{num}" if num else title

    slug = getattr(unique, "slug", "") or ""
    buy_url = f"https://t.me/nft/{slug}" if slug else ""

    resell = getattr(unique, "resell_amount", None)
    stars_price = _stars_from_amount(resell)
    ton_price = _ton_from_amount(resell)

    return StarGift(
        model=full_title,
        rarity=rarity,
        backdrop=backdrop_name,
        stars_price=stars_price,
        ton_price=ton_price,
        xgift=None,                       # оценка xgift есть только у сторонних агрегаторов
        seller=_seller_name(unique, users_by_id),
        buy_url=buy_url,
        pattern=pattern_name,
        model_name=model_name,
    )


# ─── Работа с Telegram API ───────────────────────────────────────────────────

class ResaleMarket:
    def __init__(self, api_id: int, api_hash: str, session: str = "gifts"):
        from telethon import TelegramClient
        # receive_updates=False отключает фоновое получение апдейтов
        # (GetChannelDifference), которое сыпало PersistentTimestampOutdatedError.
        # Нам нужны только запросы к рынку, апдейты не используются.
        self.client = TelegramClient(
            session, api_id, api_hash, receive_updates=False
        )

    async def start(self):
        await self.client.start()

    async def stop(self):
        await self.client.disconnect()

    async def _call(self, request):
        """Выполняет запрос с обработкой FloodWait — Telegram просит подождать
        при слишком частых обращениях; ждём столько, сколько он указал."""
        from telethon.errors import FloodWaitError
        while True:
            try:
                return await self.client(request)
            except FloodWaitError as e:
                wait = int(getattr(e, "seconds", 5)) + 1
                print(f"  {YELLOW}⏳ FloodWait: Telegram просит подождать "
                      f"{wait} сек (слишком частые запросы)...{RESET}")
                await asyncio.sleep(wait)

    async def list_collections(self) -> List[dict]:
        """Возвращает коллекции, доступные на вторичном рынке.
        [{id, title, resale, floor_stars}, ...]"""
        from telethon.tl.functions.payments import GetStarGiftsRequest
        res = await self._call(GetStarGiftsRequest(hash=0))
        out = []
        for g in getattr(res, "gifts", []):
            resale = getattr(g, "availability_resale", 0) or 0
            if resale <= 0:
                continue
            out.append({
                "id": getattr(g, "id", None),
                "title": getattr(g, "title", "") or f"Gift {getattr(g,'id','')}",
                "resale": resale,
                "floor_stars": getattr(g, "resell_min_stars", None),
            })
        out.sort(key=lambda c: c["resale"], reverse=True)
        return out

    async def resale_listings(self, gift_id: int, limit: int = 10,
                              sort_by_price: bool = True) -> List[StarGift]:
        """Объявления перепродажи для одной коллекции (по умолчанию — дешёвые первыми)."""
        from telethon.tl.functions.payments import GetResaleStarGiftsRequest
        res = await self._call(GetResaleStarGiftsRequest(
            gift_id=gift_id,
            offset="",
            limit=limit,
            sort_by_price=sort_by_price,
        ))
        users_by_id = {getattr(u, "id", None): u for u in getattr(res, "users", [])}
        gifts = []
        for unique in getattr(res, "gifts", []):
            try:
                gifts.append(unique_to_stargift(unique, users_by_id))
            except Exception as exc:
                print(f"  {YELLOW}Пропуск объявления: {exc}{RESET}")
        return gifts

    async def get_balance(self) -> Optional[int]:
        """Возвращает баланс Stars аккаунта (или None при ошибке)."""
        try:
            from telethon.tl.functions.payments import GetStarsStatusRequest
            from telethon.tl.types import InputPeerSelf
            status = await self._call(GetStarsStatusRequest(peer=InputPeerSelf()))
            bal = getattr(status, "balance", None)
            if bal is None:
                return None
            amount = getattr(bal, "amount", 0) or 0
            nanos = getattr(bal, "nanos", 0) or 0
            return int(amount + nanos / 1e9)
        except Exception as exc:
            print(f"  {YELLOW}Не удалось узнать баланс: {exc}{RESET}")
            return None

    async def buy_resale(self, slug: str, dry_run: bool = True) -> Tuple[bool, str]:
        """Покупает resale-подарок по slug на свой аккаунт (оплата Stars).
        dry_run=True — ничего не покупает, только имитирует.
        Возвращает (успех, сообщение)."""
        if not slug:
            return False, "нет slug подарка"
        if dry_run:
            return True, "ТЕСТ (dry-run): покупка НЕ выполнена"
        try:
            from telethon.tl.functions.payments import (
                GetPaymentFormRequest, SendStarsFormRequest,
            )
            from telethon.tl.types import (
                InputInvoiceStarGiftResale, InputPeerSelf,
            )
            invoice = InputInvoiceStarGiftResale(
                slug=slug, to_id=InputPeerSelf(), ton=False
            )
            form = await self._call(GetPaymentFormRequest(invoice=invoice))
            form_id = getattr(form, "form_id", None)
            if form_id is None:
                return False, "не получили форму оплаты"
            await self._call(SendStarsFormRequest(form_id=form_id, invoice=invoice))
            return True, "куплено"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


def _slug_from(gift: StarGift) -> str:
    """Достаёт slug из buy_url (https://t.me/nft/SLUG)."""
    url = gift.buy_url or ""
    return url.rstrip("/").split("/")[-1] if "/nft/" in url else ""


# ─── Вывод в консоль ─────────────────────────────────────────────────────────

def print_gift(g: StarGift, idx: int = 0):
    price = ""
    if g.stars_price is not None:
        price = f"{g.stars_price} ⭐"
        if g.ton_price is not None:
            price += f" ≈ {g.ton_price} TON"
    elif g.ton_price is not None:
        price = f"{g.ton_price} TON"
    else:
        price = "—"
    avg = ""
    if g.avg_stars is not None:
        avg = f"  {CYAN}(ср. {g.avg_stars:.0f} ⭐ по {g.avg_count}){RESET}"
    prefix = f"  {CYAN}{idx:>3}.{RESET} " if idx else "  "
    tags = [t for t in (g.model_name, g.backdrop, g.pattern) if t]
    tag_str = f"  [{' · '.join(tags)}]" if tags else ""
    print(f"{prefix}{BOLD}{g.model}{RESET}{tag_str}")
    print(f"       💰 {price}{avg}   👤 {g.seller}")
    if g.floor_stars is not None and g.discount_pct is not None and g.discount_pct > 0:
        print(f"       🏷 Флор {g.floor_stars} ⭐  "
              f"{GREEN}🔥 −{g.discount_pct:.0f}% от флора{RESET}")
    if g.buy_url:
        print(f"       🔗 {g.buy_url}")
    print()


# ─── Лимиты цен по атрибутам ──────────────────────────────────────────────────

def load_limits(path: str) -> list:
    """Читает limits.txt. Правило: набор условий через '+' и цена.
    Примеры:
      gift:Whip Cupcake+model:Resistant=700   (подарок И модель)
      model:Resistant=700                      (любой с моделью Resistant)
      backdrop:Black=300
    Возвращает список [([('collection','whip cupcake'),('model','resistant')], 700), ...]"""
    rules = []
    if not path or not os.path.exists(path):
        return rules
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                left, price = line.rsplit("=", 1)
                conds = []
                for part in left.split("+"):
                    if ":" not in part:
                        continue
                    kind, val = part.split(":", 1)
                    kind = kind.strip().lower()
                    if kind in ("gift", "collection", "коллекция", "подарок"):
                        kind = "collection"
                    elif kind in ("model", "модель"):
                        kind = "model"
                    elif kind in ("backdrop", "фон"):
                        kind = "backdrop"
                    elif kind in ("pattern", "узор", "скин"):
                        kind = "pattern"
                    else:
                        continue
                    conds.append((kind, val.strip().lower()))
                if not conds:
                    continue
                digits = re.sub(r"\D", "", price)
                if digits:
                    rules.append((conds, int(digits)))
    except Exception as exc:
        print(f"{YELLOW}Не удалось прочитать {path}: {exc}{RESET}")
    return rules


def _gift_attr(g: StarGift, kind: str) -> str:
    if kind == "collection":
        return (g.model or "").split(" #")[0].strip().lower()
    if kind == "model":
        return (g.model_name or "").strip().lower()
    if kind == "backdrop":
        return (g.backdrop or "").strip().lower()
    if kind == "pattern":
        return (g.pattern or "").strip().lower()
    return ""


def _applicable_limit(g: StarGift, rules: list) -> Optional[int]:
    """Минимальная цена среди правил, ВСЕ условия которых подходят подарку."""
    if not rules:
        return None
    matched = [price for conds, price in rules
               if all(_gift_attr(g, k) == v for k, v in conds)]
    return min(matched) if matched else None


def _buy_price_ok(g: StarGift, args) -> bool:
    """Проходит ли цена по лимитам покупки (прайс-лист или общий потолок)."""
    price = g.stars_price or 0
    rules = getattr(args, "_limits", None) or []
    if rules:
        lim = _applicable_limit(g, rules)
        # цена-потолок: индивидуальный лимит, иначе общий buy_max_price
        cap = lim if lim is not None else args.buy_max_price
        # если прайс-лист задан, но подарок никуда не подходит и общего потолка нет — не берём
        return cap > 0 and price <= cap
    # без прайс-листа: старое поведение с общим потолком
    return (args.buy_max_price <= 0) or (price <= args.buy_max_price)


# ─── Оценка лота (флор/скидка) ───────────────────────────────────────────────

def _evaluate(g: StarGift, priced: list, args) -> Tuple[bool, bool]:
    """Считает флор/скидку и решает, слать и/или покупать лот.
    Возвращает (можно_слать, можно_покупать).
    priced — лоты этой модели с ценами (для расчёта флора)."""
    if g.stars_price is None:
        return False, False

    others = [x.stars_price for x in priced if x is not g]
    floor = min(others) if others else None
    discount = ((floor - g.stars_price) / floor * 100.0) if floor else None
    g.floor_stars = floor
    g.discount_pct = discount

    # Отправка в канал: нужна скидка от флора >= порога
    send_ok = discount is not None and discount >= args.min_discount

    # Покупка: цена в пределах лимита (прайс-лист/потолок) И скидка (если задана)
    buy_ok = False
    if args.auto_buy:
        price_ok = _buy_price_ok(g, args)
        disc_ok = (args.buy_min_discount <= 0) or (
            discount is not None and discount >= args.buy_min_discount
        )
        buy_ok = price_ok and disc_ok
    return send_ok, buy_ok


# ─── Снайперский режим ───────────────────────────────────────────────────────

async def sniper_loop(market, notifier, history, args, targets, buy_state):
    """Частый параллельный опрос 1-3 коллекций. Покупка — РАНЬШЕ отправки."""
    names = ", ".join(c["title"] for c in targets)
    print(f"\n{RED}🎯 СНАЙПЕРСКИЙ РЕЖИМ{RESET}")
    print(f"{GREEN}   Коллекции: {names}{RESET}")
    print(f"{GREEN}   Опрос каждые {args.sniper_interval} сек, "
          f"топ-{args.sniper_depth} лотов, параллельно{RESET}")
    print(f"{GREEN}   Покупка при −{args.buy_min_discount:.0f}% от флора"
          f"{' (РЕАЛЬНО)' if args.auto_buy and not args._buy_dry else ' (тест)'}"
          f"{RESET}")
    if args.sniper_interval < 1:
        print(f"{YELLOW}   ⚠ Период < 1 сек почти наверняка даст FloodWait. "
              f"Скрипт переждёт, но реальная частота упадёт.{RESET}")
    print(f"{CYAN}   (Ctrl+C — стоп){RESET}\n")

    cycle = 0
    from datetime import datetime
    while True:
        cycle += 1
        # Параллельно опрашиваем все целевые коллекции
        results = await asyncio.gather(
            *[market.resale_listings(c["id"], limit=args.sniper_depth)
              for c in targets],
            return_exceptions=True,
        )
        for c, gifts in zip(targets, results):
            if isinstance(gifts, Exception) or not gifts:
                continue
            priced = [g for g in gifts if g.stars_price is not None]
            for g in gifts:
                send_ok, buy_ok = _evaluate(g, priced, args)
                if not (send_ok or buy_ok):
                    continue
                key = g.buy_url or f"{g.model}|{g.stars_price}|{g.seller}"
                if history.was_sent(key):
                    continue

                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                extra = ""
                if g.discount_pct is not None and g.discount_pct > 0:
                    extra = f" (−{g.discount_pct:.0f}% от флора {g.floor_stars})"
                print(f"{BOLD}[{ts}] {g.model} — {g.stars_price} ⭐{extra}{RESET}")

                # ПОКУПКА ПЕРВОЙ — ради скорости
                if buy_ok:
                    await _try_buy(market, args, buy_state, g)
                # Потом уведомление в канал
                if send_ok and notifier:
                    await notifier.send_star_gift(g)
                    print(f"       {GREEN}✓ в канал {args.channel}{RESET}")

                history.mark_sent(key)

        # Лёгкий «пульс» раз в ~30 циклов, чтобы видеть, что жив
        if cycle % 30 == 0:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"{CYAN}· {ts}: проверка #{cycle}, "
                  f"куплено за сессию {buy_state['bought']}{RESET}")

        await asyncio.sleep(args.sniper_interval)


# ─── Автопокупка одного подарка ──────────────────────────────────────────────

async def _try_buy(market, args, buy_state, g: StarGift):
    """Пытается купить подарок g с учётом всех предохранителей."""
    price = g.stars_price or 0
    slug = _slug_from(g)
    dry = args._buy_dry

    # Предохранитель 0: порог скидки от флора (если задан > 0)
    if args.buy_min_discount > 0:
        if g.discount_pct is None or g.discount_pct < args.buy_min_discount:
            return  # не дотягивает до порога скидки — пропускаем

    # Предохранитель 1: цена за подарок (прайс-лист по атрибутам / общий потолок)
    if not _buy_price_ok(g, args):
        lim = _applicable_limit(g, getattr(args, "_limits", None) or {})
        cap = lim if lim is not None else args.buy_max_price
        print(f"       {YELLOW}⊘ покупка пропущена: {price} ⭐ дороже лимита "
              f"{cap} ⭐{RESET}")
        return

    # Предохранитель 2: бюджет на запуск (только для реальной покупки)
    if not dry:
        remaining = args.buy_budget - buy_state["spent"]
        if price > remaining:
            print(f"       {YELLOW}⊘ покупка пропущена: бюджет почти исчерпан "
                  f"(осталось {remaining} ⭐, нужно {price} ⭐){RESET}")
            return

    ok, msg = await market.buy_resale(slug, dry_run=dry)
    if ok:
        if not dry:
            buy_state["spent"] += price
            buy_state["bought"] += 1
            print(f"       {GREEN}🛒 КУПЛЕНО за {price} ⭐  "
                  f"(потрачено {buy_state['spent']}/{args.buy_budget} ⭐){RESET}\n")
        else:
            buy_state["bought"] += 1
            print(f"       {CYAN}🛒 {msg} (купил бы за {price} ⭐){RESET}\n")
    else:
        print(f"       {RED}✗ покупка не удалась: {msg}{RESET}\n")


# ─── Одна проверка рынка ─────────────────────────────────────────────────────

async def scan_once(market, notifier, history, args, target, buy_state) -> Tuple[int, int]:
    """Один проход по коллекциям. Возвращает (найдено, отправлено)."""
    total_found = 0
    total_sent = 0

    for ci, c in enumerate(target):
        # Троттлинг: небольшая пауза между коллекциями, чтобы не слать
        # запросы залпом (снижает риск FloodWait)
        if ci > 0 and args.req_delay > 0:
            await asyncio.sleep(args.req_delay)

        gifts = await market.resale_listings(c["id"], limit=args.max)
        if not gifts:
            continue

        # Все текущие цены этой модели на рынке — для расчёта флора
        priced = [g for g in gifts if g.stars_price is not None]

        printed_header = False
        for i, g in enumerate(gifts, 1):
            history.enrich(g, n=10)     # средняя ДО записи текущей
            history.record(g)           # сохраняем в историю цен

            send_ok, buy_ok = _evaluate(g, priced, args)

            # Доп. фильтр "ниже средней" — ограничивает только отправку
            if send_ok and args.below_average:
                if (g.avg_stars is None or (g.stars_price or 0) >= g.avg_stars):
                    send_ok = False

            if not (send_ok or buy_ok):
                continue

            # Антиповтор активен только при реальном действии (отправка/покупка)
            tracking = bool(notifier) or args.auto_buy
            gift_key = g.buy_url or f"{g.model}|{g.stars_price}|{g.seller}"
            if tracking and history.was_sent(gift_key):
                continue

            if not printed_header:
                print(f"\n{BOLD}── {c['title']} ──{RESET}")
                printed_header = True

            print_gift(g, i)
            total_found += 1

            if send_ok and notifier:
                ok = await notifier.send_star_gift(g)
                if ok:
                    total_sent += 1
                    print(f"       {GREEN}✓ отправлено в {args.channel}{RESET}")
                await asyncio.sleep(args.delay)

            # ── Автопокупка ──
            if buy_ok:
                await _try_buy(market, args, buy_state, g)

            if tracking:
                history.mark_sent(gift_key)  # не обрабатывать повторно

    suffix = f", отправлено {total_sent}" if notifier else ""
    if args.auto_buy:
        word = "куплено" if not args._buy_dry else "купил бы"
        suffix += f", {word} {buy_state['bought']}"
        if not args._buy_dry:
            suffix += f" (потрачено {buy_state['spent']} ⭐)"
    if total_found == 0:
        print(f"  {YELLOW}Новых подходящих объявлений нет.{RESET}")
    else:
        print(f"\n{BOLD}Итого: найдено {total_found}{suffix}{RESET}")
    return total_found, total_sent


# ─── Отчёт "топ продаваемых" ─────────────────────────────────────────────────

async def top_report(market, args, collections):
    """Два снимка рынка с паузой: исчезнувшие лоты ≈ проданные.
    Показывает топ коллекций по активности продаж."""
    pool = collections[:args.top_collections]  # уже отсортированы по кол-ву лотов
    wait = args.top_wait

    print(f"\n{BOLD}📊 Замер продаж: {len(pool)} коллекций, "
          f"пауза {wait} сек между снимками{RESET}")
    print(f"{CYAN}   (исчезнувший лот ≈ продан; оценка приблизительная){RESET}\n")

    async def snapshot():
        snap = {}
        for i, c in enumerate(pool):
            if i > 0 and args.req_delay > 0:
                await asyncio.sleep(args.req_delay)
            gifts = await market.resale_listings(c["id"], limit=args.top_depth)
            slugs = {_slug_from(g) for g in gifts if _slug_from(g)}
            prices = [g.stars_price for g in gifts if g.stars_price is not None]
            snap[c["id"]] = (slugs, min(prices) if prices else None)
        return snap

    print(f"{BOLD}Снимок 1/2...{RESET}")
    snap1 = await snapshot()
    print(f"{CYAN}⏳ Ждём {wait} сек...{RESET}")
    await asyncio.sleep(wait)
    print(f"{BOLD}Снимок 2/2...{RESET}")
    snap2 = await snapshot()

    rows = []
    for c in pool:
        s1, floor1 = snap1.get(c["id"], (set(), None))
        s2, floor2 = snap2.get(c["id"], (set(), None))
        sold = len(s1 - s2)         # были — исчезли
        appeared = len(s2 - s1)     # новые выставленные
        rows.append((c, sold, appeared, floor1, floor2))

    rows.sort(key=lambda r: r[1], reverse=True)

    mins = wait / 60
    print(f"\n{BOLD}{'─'*68}{RESET}")
    print(f"{BOLD}  ТОП ПРОДАВАЕМЫХ (за {mins:.0f} мин)  "
          f"— продано / выставлено / флор{RESET}")
    print(f"{BOLD}{'─'*68}{RESET}")
    for i, (c, sold, appeared, f1, f2) in enumerate(rows, 1):
        if sold == 0 and appeared == 0 and i > 10:
            continue  # хвост без активности не показываем
        floor_str = f"{f2} ⭐" if f2 is not None else "—"
        trend = ""
        if f1 is not None and f2 is not None and f1 != f2:
            arrow = "↑" if f2 > f1 else "↓"
            trend = f"  (флор {arrow} {f1}→{f2})"
        hot = " 🔥" if sold >= 5 else ""
        print(f"  {CYAN}{i:>3}.{RESET} {BOLD}{c['title']}{RESET}{hot}")
        print(f"       продано ~{sold}  ·  новых {appeared}  ·  "
              f"в продаже {c['resale']}  ·  флор {floor_str}{trend}")
    total_sold = sum(r[1] for r in rows)
    print(f"\n{BOLD}Всего продано за период: ~{total_sold} "
          f"(~{total_sold / max(mins, 1):.1f} в минуту){RESET}")
    speed_hint = [r for r in rows[:3] if r[1] > 0]
    if speed_hint:
        names = ", ".join(r[0]["title"] for r in speed_hint)
        print(f"{GREEN}Самые горячие: {names}{RESET}")


# ─── Конфиг-файл ─────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    """Читает config.txt в формате KEY=VALUE (по строке).
    Пустые строки и строки с # игнорируются. Кодировка устойчива к BOM."""
    cfg = {}
    if not path or not os.path.exists(path):
        return cfg
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip().lower()] = v.strip()
    except Exception as exc:
        print(f"{YELLOW}Не удалось прочитать {path}: {exc}{RESET}")
    return cfg


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    raw = cfg.get(key, "")
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return default


def _cfg_bool(cfg: dict, key: str) -> bool:
    return str(cfg.get(key, "")).strip().lower() in ("1", "true", "yes", "да", "y")


# ─── Точка входа ─────────────────────────────────────────────────────────────

async def main():
    # config.txt используется как источник значений по умолчанию.
    # Путь можно переопределить через --config.
    cfg_path = "config.txt"
    if "--config" in sys.argv:
        try:
            cfg_path = sys.argv[sys.argv.index("--config") + 1]
        except IndexError:
            pass
    cfg = load_config(cfg_path)

    ap = argparse.ArgumentParser(
        description="Парсер официального вторичного рынка подарков Telegram",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--config", default="config.txt",
                    help="Файл настроек KEY=VALUE (default: config.txt)")
    ap.add_argument("--limits", default=cfg.get("limits", "limits.txt"),
                    help="Файл лимитов цен по модели/фону/узору (default: limits.txt)")
    ap.add_argument("--api-id", default=cfg.get("api_id") or os.getenv("TG_API_ID"),
                    help="Telegram API ID (config: api_id / env TG_API_ID)")
    ap.add_argument("--api-hash", default=cfg.get("api_hash") or os.getenv("TG_API_HASH"),
                    help="Telegram API Hash (config: api_hash / env TG_API_HASH)")
    ap.add_argument("--session", default="gifts", help="Имя файла сессии (default: gifts)")

    ap.add_argument("--list", action="store_true",
                    help="Показать все коллекции с перепродажей и выйти")
    ap.add_argument("--all", action="store_true", default=_cfg_bool(cfg, "all"),
                    help="Парсить все коллекции с перепродажей")
    ap.add_argument("--collection", default=cfg.get("collection", ""),
                    help="Название одной коллекции (напр. 'Plush Pepe')")
    ap.add_argument("--max", type=int, default=_cfg_int(cfg, "max", 10),
                    help="Сколько объявлений на коллекцию (default: 10)")
    ap.add_argument("--max-collections", type=int,
                    default=_cfg_int(cfg, "max_collections", 0),
                    help="Ограничить число коллекций при --all (0 = без лимита)")

    ap.add_argument("--below-average", action="store_true",
                    default=_cfg_bool(cfg, "below_average"),
                    help="Слать только объявления дешевле средней цены по истории")
    ap.add_argument("--min-discount", type=float,
                    default=float(_cfg_int(cfg, "min_discount", 20)),
                    help="Слать только если цена ниже флора на N%% (default: 20)")
    ap.add_argument("--max-price-stars", type=int,
                    default=_cfg_int(cfg, "max_price_stars", 0),
                    help="(устарело) Лимит по звёздам; 0 = выкл")
    ap.add_argument("--interval", type=int, default=_cfg_int(cfg, "interval", 0),
                    help="Повторять каждые N секунд (0 = один раз, напр. 60)")
    ap.add_argument("--req-delay", type=float,
                    default=float(cfg.get("req_delay", "0.5") or 0.5),
                    help="Пауза между запросами коллекций, сек (анти-FloodWait)")

    # ── Снайперский режим ──
    ap.add_argument("--sniper", action="store_true", default=_cfg_bool(cfg, "sniper"),
                    help="Снайпер: 1-3 коллекции, частый параллельный опрос топ-лотов")
    ap.add_argument("--sniper-collections",
                    default=cfg.get("sniper_collections", ""),
                    help="Коллекции для снайпера через запятую, напр. 'Plush Pepe,Heart'")
    ap.add_argument("--sniper-interval", type=float,
                    default=float(cfg.get("sniper_interval", "2") or 2),
                    help="Период опроса в снайпере, сек (default: 2)")
    ap.add_argument("--sniper-depth", type=int,
                    default=_cfg_int(cfg, "sniper_depth", 5),
                    help="Сколько топ-лотов смотреть в снайпере (default: 5)")
    ap.add_argument("--sniper-max-collections", type=int,
                    default=_cfg_int(cfg, "sniper_max_collections", 5),
                    help="Сколько коллекций брать при авто-выборе (default: 5)")
    # ── Отчёт "топ продаваемых" ──
    ap.add_argument("--top", action="store_true",
                    help="Показать топ продаваемых подарков (замер продаж) и выйти")
    ap.add_argument("--top-wait", type=int, default=120,
                    help="Сколько секунд ждать между замерами (default: 120)")
    ap.add_argument("--top-collections", type=int, default=20,
                    help="Сколько коллекций замерять (default: 20 самых активных)")
    ap.add_argument("--top-depth", type=int, default=30,
                    help="Сколько дешёвых лотов брать в снимок (default: 30)")

    ap.add_argument("--db-path", default=PriceHistory.DEFAULT_DB,
                    help=f"Файл истории цен (default: {PriceHistory.DEFAULT_DB})")

    ap.add_argument("--token", default=cfg.get("token") or os.getenv("BOT_TOKEN", ""),
                    help="Токен бота для постинга (config: token / env BOT_TOKEN)")
    ap.add_argument("--channel", default=cfg.get("channel", ""),
                    help="Канал назначения, напр. @abcuzbek")
    ap.add_argument("--no-post", action="store_true",
                    default=(_cfg_int(cfg, "post", 1) == 0),
                    help="Не отправлять в канал (config: post=0)")
    ap.add_argument("--delay", type=float, default=0.6, help="Пауза между постами, сек")

    # ── Автопокупка (тратит реальные Stars!) ──
    ap.add_argument("--auto-buy", action="store_true", default=_cfg_bool(cfg, "auto_buy"),
                    help="Покупать подарки, прошедшие фильтр (по умолчанию ВЫКЛ)")
    ap.add_argument("--buy-real", action="store_true", default=_cfg_bool(cfg, "buy_real"),
                    help="РЕАЛЬНАЯ покупка (иначе тестовый dry-run)")
    ap.add_argument("--buy-budget", type=int, default=_cfg_int(cfg, "buy_budget", 0),
                    help="Лимит трат Stars за запуск (0 = реальная покупка запрещена)")
    ap.add_argument("--buy-max-price", type=int,
                    default=_cfg_int(cfg, "buy_max_price", 0),
                    help="Не покупать дороже N звёзд за подарок (0 = без лимита)")
    ap.add_argument("--buy-min-discount", type=float,
                    default=float(_cfg_int(cfg, "buy_min_discount", 30)),
                    help="Покупать только если цена ниже флора на N%% (default: 30)")

    args = ap.parse_args()

    if cfg:
        print(f"{GREEN}⚙ Настройки загружены из {cfg_path}{RESET}")

    # Прайс-лист лимитов по модели/фону/узору
    args._limits = load_limits(args.limits)
    if args._limits:
        print(f"{GREEN}💲 Лимиты цен: {len(args._limits)} правил из {args.limits}{RESET}")

    if not args.api_id or not args.api_hash:
        print(f"{RED}Ошибка: нужны --api-id и --api-hash "
              f"(получить на https://my.telegram.org){RESET}")
        sys.exit(1)

    # Проверка, что api-id — число (частая ошибка: незаполненный .bat)
    try:
        args.api_id = int(args.api_id)
    except (ValueError, TypeError):
        print(f"{RED}Ошибка: api-id должен быть числом, а получено: "
              f"'{args.api_id}'.{RESET}")
        print(f"{YELLOW}Похоже, вы не вписали реальные значения. "
              f"Откройте run_monitor.bat и замените ВАШ_API_ID / ВАШ_API_HASH "
              f"на настоящие ключи с my.telegram.org.{RESET}")
        sys.exit(1)

    _require_telethon()

    notifier = None
    if args.no_post:
        print(f"{YELLOW}🚫 Отправка в канал ОТКЛЮЧЕНА (только покупка/консоль).{RESET}")
    elif args.token and args.channel:
        notifier = TelegramNotifier(token=args.token, channel=args.channel)
        print(f"{GREEN}📨 Постинг включён → {args.channel}{RESET}")
    elif args.token or args.channel:
        missing = "--channel" if not args.channel else "--token"
        print(f"{RED}Для постинга нужны И --token, И --channel "
              f"(не хватает {missing}){RESET}")
        sys.exit(1)
    elif not args.list:
        print(f"{YELLOW}ℹ Постинг ВЫКЛЮЧЕН — только вывод в консоль.{RESET}")
        print(f"{YELLOW}  Чтобы слать в канал, добавьте: "
              f"--token ВАШ_ТОКЕН --channel @канал{RESET}")

    # ── Настройка автопокупки с предохранителями ──
    buy_state = {"spent": 0, "bought": 0}
    # Описание условия покупки (прайс-лист / цена / скидка от флора)
    conds = []
    if args._limits:
        conds.append(f"по прайс-листу ({len(args._limits)} правил)")
    elif args.buy_max_price > 0:
        conds.append(f"дешевле {args.buy_max_price} ⭐")
    if args.buy_min_discount > 0:
        conds.append(f"ниже флора на {args.buy_min_discount:.0f}%+")
    cond_str = " и ".join(conds) if conds else "ЛЮБОЙ лот (нет условий!)"

    if args.auto_buy:
        # Реальная покупка возможна только при заданном бюджете > 0
        if args.buy_real and args.buy_budget > 0:
            print(f"{RED}🛒 АВТОПОКУПКА ВКЛЮЧЕНА (РЕАЛЬНЫЕ ТРАТЫ){RESET}")
            print(f"{RED}   Условие: {cond_str}, бюджет {args.buy_budget} ⭐{RESET}")
            args._buy_dry = False
        else:
            reason = "не задан buy_budget>0" if args.buy_real else "режим теста"
            print(f"{YELLOW}🛒 Автопокупка в ТЕСТОВОМ режиме (dry-run): {reason}.{RESET}")
            print(f"{YELLOW}   Условие: {cond_str}. Реальные траты НЕ выполняются.{RESET}")
            print(f"{YELLOW}   Для реальной покупки: buy_real=1 и buy_budget>0 "
                  f"в config.txt.{RESET}")
            args._buy_dry = True
    else:
        args._buy_dry = True

    history = PriceHistory(args.db_path)

    market = ResaleMarket(int(args.api_id), args.api_hash, session=args.session)
    print(f"{BOLD}Подключение к Telegram...{RESET}")
    await market.start()

    if args.auto_buy and not args._buy_dry:
        bal = await market.get_balance()
        if bal is not None:
            print(f"{BOLD}💰 Баланс: {bal} ⭐{RESET}")

    try:
        collections = await market.list_collections()
        if not collections:
            print(f"{YELLOW}Нет коллекций с активной перепродажей.{RESET}")
            return

        # Режим списка
        if args.list:
            print(f"\n{BOLD}Коллекции на вторичном рынке ({len(collections)}):{RESET}\n")
            for c in collections:
                floor = f"{c['floor_stars']} ⭐" if c["floor_stars"] else "—"
                print(f"  • {BOLD}{c['title']}{RESET}  "
                      f"(в продаже: {c['resale']}, от {floor})")
            print(f"\nДальше: --collection \"Название\"  или  --all")
            return

        # ── Отчёт "топ продаваемых" ──
        if args.top:
            await top_report(market, args, collections)
            return

        # ── Снайперский режим ──
        if args.sniper:
            wanted = [s.strip().lower() for s in args.sniper_collections.split(",")
                      if s.strip()]
            if wanted:
                # Ручной список коллекций
                targets = [c for c in collections if c["title"].lower() in wanted]
                if not targets:
                    print(f"{RED}Ни одна из коллекций не найдена в перепродаже: "
                          f"{args.sniper_collections}{RESET}")
                    print("Проверьте названия через --list")
                    return
            else:
                # Авто-выбор: самые дешёвые коллекции (флор ≤ макс. цены покупки)
                cap = args.buy_max_price if args.buy_max_price > 0 else None
                pool = [c for c in collections if c.get("floor_stars") is not None
                        and (cap is None or c["floor_stars"] <= cap)]
                if not pool:
                    where = f"с флором ≤ {cap} ⭐" if cap else ""
                    print(f"{RED}Нет коллекций {where} для снайпера.{RESET}")
                    print("Задайте sniper_collections вручную или поднимите "
                          "buy_max_price. Список — через --list")
                    return
                # сортируем по дешевизне флора, берём верхние N
                pool.sort(key=lambda c: c["floor_stars"])
                targets = pool[:args.sniper_max_collections]
                names = ", ".join(f"{c['title']}({c['floor_stars']}⭐)" for c in targets)
                print(f"{GREEN}🎯 Авто-выбор {len(targets)} самых дешёвых коллекций"
                      + (f" с флором ≤ {cap} ⭐" if cap else "") + f":{RESET}")
                print(f"{GREEN}   {names}{RESET}")
            await sniper_loop(market, notifier, history, args, targets, buy_state)
            return

        # Выбор коллекций для парсинга (один раз — список коллекций стабилен)
        if args.collection:
            target = [c for c in collections
                      if c["title"].lower() == args.collection.lower()]
            if not target:
                print(f"{RED}Коллекция '{args.collection}' не найдена в перепродаже.{RESET}")
                print("Доступные — запустите с --list")
                return
        elif args.all:
            target = collections
            if args.max_collections > 0:
                target = target[:args.max_collections]
        else:
            print(f"{RED}Укажите --all, --collection НАЗВАНИЕ или --list{RESET}")
            return

        # Однократный запуск или цикл по интервалу
        from datetime import datetime
        if args.interval > 0:
            print(f"{GREEN}🔁 Режим мониторинга: каждые {args.interval} сек."
                  f"{RESET}  (Ctrl+C — стоп)")
            print(f"{GREEN}   Фильтр: цена ниже флора минимум на "
                  f"{args.min_discount:.0f}%{RESET}")
            if args.interval < 60:
                print(f"{YELLOW}   ⚠ Интервал {args.interval} сек по "
                      f"{len(target)} коллекциям — это часто. При FloodWait "
                      f"скрипт сам подождёт, но безопаснее 60–120 сек.{RESET}")
            print(f"{CYAN}   Анти-FloodWait: пауза {args.req_delay} сек "
                  f"между коллекциями.{RESET}")
            cycle = 0
            while True:
                cycle += 1
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n{BOLD}═══ Проверка #{cycle} в {ts} ═══{RESET}")
                await scan_once(market, notifier, history, args, target, buy_state)
                nxt = datetime.now().timestamp() + args.interval
                nxt_str = datetime.fromtimestamp(nxt).strftime("%H:%M:%S")
                print(f"{CYAN}⏳ Следующая проверка через {args.interval} сек "
                      f"(в {nxt_str})...{RESET}")
                await asyncio.sleep(args.interval)
        else:
            await scan_once(market, notifier, history, args, target, buy_state)
            print(f"\n{YELLOW}ℹ Это была разовая проверка (interval=0).{RESET}")
            print(f"{YELLOW}  Чтобы проверять автоматически, задайте интервал: "
                  f"перезапустите setup.bat и введите, например, 60.{RESET}")

    except KeyboardInterrupt:
        print(f"\n{YELLOW}Остановлено пользователем.{RESET}")
    except Exception as exc:
        print(f"\n{RED}Ошибка: {exc}{RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        await market.stop()


if __name__ == "__main__":
    asyncio.run(main())
