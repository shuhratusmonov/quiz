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

    async def get_balance(self) -> Optional[int]:
        """Возвращает баланс Stars аккаунта (или None при ошибке)."""
        try:
            from telethon.tl.functions.payments import GetStarsStatusRequest
            from telethon.tl.types import InputPeerSelf
            status = await self.client(GetStarsStatusRequest(peer=InputPeerSelf()))
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
            form = await self.client(GetPaymentFormRequest(invoice=invoice))
            form_id = getattr(form, "form_id", None)
            if form_id is None:
                return False, "не получили форму оплаты"
            await self.client(SendStarsFormRequest(form_id=form_id, invoice=invoice))
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
    backdrop = f"  [{g.backdrop}]" if g.backdrop else ""
    print(f"{prefix}{BOLD}{g.model}{RESET}{backdrop}")
    print(f"       💰 {price}{avg}   👤 {g.seller}")
    if g.floor_stars is not None and g.discount_pct is not None:
        print(f"       🏷 Флор {g.floor_stars} ⭐  "
              f"{GREEN}🔥 −{g.discount_pct:.0f}% от флора{RESET}")
    if g.buy_url:
        print(f"       🔗 {g.buy_url}")
    print()


# ─── Автопокупка одного подарка ──────────────────────────────────────────────

async def _try_buy(market, args, buy_state, g: StarGift):
    """Пытается купить подарок g с учётом всех предохранителей."""
    price = g.stars_price or 0
    slug = _slug_from(g)
    dry = args._buy_dry

    # Предохранитель 0: скидка для ПОКУПКИ может быть строже, чем для отправки
    if g.discount_pct is None or g.discount_pct < args.buy_min_discount:
        return  # не дотягивает до порога покупки — молча пропускаем

    # Предохранитель 1: цена за подарок
    if args.buy_max_price > 0 and price > args.buy_max_price:
        print(f"       {YELLOW}⊘ покупка пропущена: {price} ⭐ дороже лимита "
              f"{args.buy_max_price} ⭐{RESET}")
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

    for c in target:
        gifts = await market.resale_listings(c["id"], limit=args.max)
        if not gifts:
            continue

        # Все текущие цены этой модели на рынке — для расчёта флора
        priced = [g for g in gifts if g.stars_price is not None]

        printed_header = False
        for i, g in enumerate(gifts, 1):
            history.enrich(g, n=10)     # средняя ДО записи текущей
            history.record(g)           # сохраняем в историю цен

            if g.stars_price is None:
                continue

            # Флор = самый дешёвый ДРУГОЙ подарок этой модели на рынке
            others = [x.stars_price for x in priced if x is not g]
            if not others:
                continue  # один лот — не с чем сравнивать
            floor = min(others)

            # Насколько этот лот дешевле флора (в %)
            discount = (floor - g.stars_price) / floor * 100.0

            # Отправляем только если цена ниже флора минимум на N%
            if discount < args.min_discount:
                continue

            g.floor_stars = floor
            g.discount_pct = discount

            # Доп. фильтр "ниже средней" (если включён)
            if args.below_average:
                if (g.avg_stars is None or g.stars_price >= g.avg_stars):
                    continue

            # Антиповтор: если уже обрабатывали это объявление — пропускаем
            # (учитывает и отправку, и покупку)
            gift_key = g.buy_url or f"{g.model}|{g.stars_price}|{g.seller}"
            if (notifier or args.auto_buy) and history.was_sent(gift_key):
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
                    print(f"       {GREEN}✓ отправлено в {args.channel}{RESET}")
                await asyncio.sleep(args.delay)

            # ── Автопокупка ──
            if args.auto_buy:
                await _try_buy(market, args, buy_state, g)
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
    if args.auto_buy:
        # Реальная покупка возможна только при заданном бюджете > 0
        if args.buy_real and args.buy_budget > 0:
            print(f"{RED}🛒 АВТОПОКУПКА ВКЛЮЧЕНА (РЕАЛЬНЫЕ ТРАТЫ){RESET}")
            print(f"{RED}   Условие: ниже флора на {args.buy_min_discount:.0f}%+, "
                  f"бюджет {args.buy_budget} ⭐"
                  + (f", не дороже {args.buy_max_price} ⭐/шт" if args.buy_max_price else "")
                  + f"{RESET}")
            args._buy_dry = False
        else:
            reason = "не задан buy_budget>0" if args.buy_real else "режим теста"
            print(f"{YELLOW}🛒 Автопокупка в ТЕСТОВОМ режиме (dry-run): {reason}.{RESET}")
            print(f"{YELLOW}   Условие покупки: ниже флора на "
                  f"{args.buy_min_discount:.0f}%+. Реальные траты НЕ выполняются.{RESET}")
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
