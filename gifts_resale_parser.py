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
import os
import sys
from typing import List, Optional, Tuple

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

    model_name = getattr(model_a, "name", "") if model_a else ""
    backdrop_name = getattr(backdrop_a, "name", "") if backdrop_a else ""

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
    )


# ─── Работа с Telegram API ───────────────────────────────────────────────────

class ResaleMarket:
    def __init__(self, api_id: int, api_hash: str, session: str = "gifts"):
        from telethon import TelegramClient
        self.client = TelegramClient(session, api_id, api_hash)

    async def start(self):
        await self.client.start()

    async def stop(self):
        await self.client.disconnect()

    async def list_collections(self) -> List[dict]:
        """Возвращает коллекции, доступные на вторичном рынке.
        [{id, title, resale, floor_stars}, ...]"""
        from telethon.tl.functions.payments import GetStarGiftsRequest
        res = await self.client(GetStarGiftsRequest(hash=0))
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
        res = await self.client(GetResaleStarGiftsRequest(
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
    backdrop = f"  [{g.backdrop}]" if g.backdrop else ""
    print(f"{prefix}{BOLD}{g.model}{RESET}{backdrop}")
    print(f"       💰 {price}{avg}   👤 {g.seller}")
    if g.buy_url:
        print(f"       🔗 {g.buy_url}")
    print()


# ─── Одна проверка рынка ─────────────────────────────────────────────────────

async def scan_once(market, notifier, history, args, target) -> Tuple[int, int]:
    """Один проход по коллекциям. Возвращает (найдено, отправлено)."""
    total_found = 0
    total_sent = 0

    for c in target:
        gifts = await market.resale_listings(c["id"], limit=args.max)
        if not gifts:
            continue

        printed_header = False
        for i, g in enumerate(gifts, 1):
            history.enrich(g, n=10)     # средняя ДО записи текущей
            history.record(g)           # сохраняем в историю цен

            # Фильтр по максимальной цене в звёздах
            if args.max_price_stars > 0:
                if g.stars_price is None or g.stars_price >= args.max_price_stars:
                    continue

            # Фильтр "ниже средней"
            if args.below_average:
                if (g.avg_stars is None or g.stars_price is None
                        or g.stars_price >= g.avg_stars):
                    continue

            # Антиповтор: если уже отправляли это объявление — пропускаем
            gift_key = g.buy_url or f"{g.model}|{g.stars_price}|{g.seller}"
            if notifier and history.was_sent(gift_key):
                continue

            if not printed_header:
                print(f"\n{BOLD}── {c['title']} ──{RESET}")
                printed_header = True

            print_gift(g, i)
            total_found += 1

            if notifier:
                ok = await notifier.send_star_gift(g)
                if ok:
                    total_sent += 1
                    history.mark_sent(gift_key)
                    print(f"       {GREEN}✓ отправлено в {args.channel}{RESET}\n")
                await asyncio.sleep(args.delay)

    suffix = f", отправлено {total_sent}" if notifier else ""
    if total_found == 0:
        print(f"  {YELLOW}Новых подходящих объявлений нет.{RESET}")
    else:
        print(f"\n{BOLD}Итого: найдено {total_found}{suffix}{RESET}")
    return total_found, total_sent


# ─── Точка входа ─────────────────────────────────────────────────────────────

async def main():
    ap = argparse.ArgumentParser(
        description="Парсер официального вторичного рынка подарков Telegram",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--api-id", type=int, default=os.getenv("TG_API_ID"),
                    help="Telegram API ID (или env TG_API_ID)")
    ap.add_argument("--api-hash", default=os.getenv("TG_API_HASH"),
                    help="Telegram API Hash (или env TG_API_HASH)")
    ap.add_argument("--session", default="gifts", help="Имя файла сессии (default: gifts)")

    ap.add_argument("--list", action="store_true",
                    help="Показать все коллекции с перепродажей и выйти")
    ap.add_argument("--all", action="store_true",
                    help="Парсить все коллекции с перепродажей")
    ap.add_argument("--collection", default="",
                    help="Название одной коллекции (напр. 'Plush Pepe')")
    ap.add_argument("--max", type=int, default=10,
                    help="Сколько объявлений на коллекцию (default: 10)")
    ap.add_argument("--max-collections", type=int, default=0,
                    help="Ограничить число коллекций при --all (0 = без лимита)")

    ap.add_argument("--below-average", action="store_true",
                    help="Слать только объявления дешевле средней цены по истории")
    ap.add_argument("--max-price-stars", type=int, default=0,
                    help="Слать только дешевле N звёзд (0 = без лимита, напр. 500)")
    ap.add_argument("--interval", type=int, default=0,
                    help="Повторять каждые N секунд (0 = один раз, напр. 60)")
    ap.add_argument("--db-path", default=PriceHistory.DEFAULT_DB,
                    help=f"Файл истории цен (default: {PriceHistory.DEFAULT_DB})")

    ap.add_argument("--token", default=os.getenv("BOT_TOKEN", ""),
                    help="Токен бота для постинга в канал (или env BOT_TOKEN)")
    ap.add_argument("--channel", default="", help="Канал назначения, напр. @abcuzbek")
    ap.add_argument("--delay", type=float, default=0.6, help="Пауза между постами, сек")

    args = ap.parse_args()

    if not args.api_id or not args.api_hash:
        print(f"{RED}Ошибка: нужны --api-id и --api-hash "
              f"(получить на https://my.telegram.org){RESET}")
        sys.exit(1)

    _require_telethon()

    notifier = None
    if args.token and args.channel:
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

    history = PriceHistory(args.db_path)

    market = ResaleMarket(int(args.api_id), args.api_hash, session=args.session)
    print(f"{BOLD}Подключение к Telegram...{RESET}")
    await market.start()

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
        if args.interval > 0:
            print(f"{GREEN}🔁 Режим мониторинга: каждые {args.interval} сек."
                  f"{RESET}  (Ctrl+C — стоп)")
            if args.max_price_stars > 0:
                print(f"{GREEN}   Фильтр: дешевле {args.max_price_stars} ⭐{RESET}")
            cycle = 0
            while True:
                cycle += 1
                from datetime import datetime
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n{BOLD}═══ Проверка #{cycle} в {ts} ═══{RESET}")
                await scan_once(market, notifier, history, args, target)
                await asyncio.sleep(args.interval)
        else:
            await scan_once(market, notifier, history, args, target)

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
