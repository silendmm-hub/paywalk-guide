"""Бот клуба «Блог без лица»: подписка через Точку, доступ в Telegram-группу.

Как работает:
  1. Человек жмёт «Вступить», бот создаёт ему подписку в Точке (без графика, recurring)
     и присылает ссылку на оплату картой.
  2. Точка присылает вебхук об оплате, бот открывает доступ и даёт ссылку-заявку в клуб.
  3. Раз в месяц бот сам списывает оплату (Charge Subscription).
  4. Не прошло списание: бот предупреждает и пробует раз в сутки, через GRACE_DAYS дней
     удаляет из группы. Оплатит снова, бот пустит обратно.

Запуск:
  python bot.py                 # работа
  python bot.py check           # проверить токены и найти customerCode / merchantId
  python bot.py setup-webhook   # подключить вебхук Точки на PUBLIC_URL
"""
import asyncio
import calendar
import json
import logging
import os
import sqlite3
import ssl
import sys
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
import jwt
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("club")
MSK = timezone(timedelta(hours=3))

# Тексты, которые видят люди. Править можно смело, только не трогай {переменные}.
TEXTS = {
    "start": (
        "Привет! Это бот закрытого клуба «{club}».\n\n"
        "Внутри:\n"
        "🔥 пошаговая система блога без лица: от первого ролика до денег\n"
        "🔥 свежие обновления каждый месяц\n"
        "🔥 разборы, практика и обратная связь\n"
        "🔥 чат с теми, кто делает то же самое\n\n"
        "Доступ: {price} ₽ в месяц. Отменить можно в любой момент прямо здесь, в боте."
    ),
    "old_member": "\n\nТы из нашей первой группы, поэтому для тебя цена {price} ₽ вместо {full} ₽, пока подписка активна 🤝",
    "btn_join": "Вступить за {price} ₽/мес",
    "ask_email": "Напиши почту, на неё придёт чек об оплате.",
    "bad_email": "Похоже, в почте опечатка. Напиши ещё раз, например: name@mail.ru",
    "pay": (
        "Готово, вот ссылка на оплату 👇\n\n"
        "Оплата только картой (СБП для подписки банк не поддерживает). "
        "Дальше {price} ₽ будут списываться раз в месяц, за день до списания я напомню.\n\n"
        "После оплаты доступ откроется сам в течение минуты."
    ),
    "btn_pay": "Оплатить картой",
    "btn_paid": "Я оплатил(а)",
    "not_paid_yet": "Оплату пока не вижу. Если только что оплатил(а), подожди минуту и нажми ещё раз.",
    "welcome": (
        "Оплата прошла, добро пожаловать в клуб! 🎉\n\n"
        "Жми кнопку ниже и отправляй заявку, я одобрю её автоматически.\n"
        "Доступ оплачен до {until}."
    ),
    "btn_enter": "Войти в клуб",
    "status": "Подписка активна, оплачено до {until}.\nСледующее списание: {price} ₽.",
    "status_cancelled": "Подписка отменена. Доступ в клуб сохранится до {until}.",
    "status_past_due": "Не получилось списать оплату. Доступ сохранится до {until}, потом придётся вступать заново.",
    "btn_cancel": "Отменить подписку",
    "confirm_cancel": "Точно отменить? Списаний больше не будет, доступ останется до {until}.",
    "btn_yes_cancel": "Да, отменить",
    "btn_no": "Нет, остаюсь",
    "cancelled": "Подписка отменена. Доступ останется до {until}. Захочешь вернуться, просто нажми /start.",
    "remind": "Напоминаю: завтра спишется {price} ₽ за следующий месяц в клубе. Отменить можно командой /start.",
    "renewed": "Оплата за следующий месяц прошла ✅ Доступ продлён до {until}.",
    "charge_failed": (
        "Не получилось списать {price} ₽ за клуб. Проверь, что на карте есть деньги, "
        "я попробую ещё раз завтра. Если не выйдет до {until}, доступ закроется."
    ),
    "expired": "Доступ в клуб закрыт, подписка не продлилась. Вернуться можно в любой момент 👇",
    "btn_return": "Вернуться в клуб",
    "join_declined": "Вход в клуб только по подписке. Оформить её можно здесь: /start",
    "error": "Что-то пошло не так на стороне банка. Попробуй через пару минут или напиши менеджеру @{manager}.",
}


# ---------------------------------------------------------------- настройки

def load_env_file(path):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Config:
    def __init__(self):
        load_env_file(os.path.join(HERE, ".env"))
        e = os.environ.get
        self.bot_token = e("BOT_TOKEN", "")
        self.club_chat_id = int(e("CLUB_CHAT_ID", "0") or 0)
        self.old_chat_id = int(e("OLD_CHAT_ID", "0") or 0)
        self.admin_ids = {int(x) for x in e("ADMIN_IDS", "").replace(" ", "").split(",") if x}
        self.club_name = e("CLUB_NAME", "Блог без лица")
        self.manager = e("MANAGER_USERNAME", "alphach_manager")
        self.price = float(e("PRICE", "990"))
        self.old_price = float(e("OLD_PRICE", "0") or 0) or self.price
        self.grace_days = int(e("GRACE_DAYS", "3"))
        self.tochka_jwt = e("TOCHKA_JWT", "")
        self.tochka_client_id = e("TOCHKA_CLIENT_ID", "")
        self.customer_code = e("TOCHKA_CUSTOMER_CODE", "")
        self.merchant_id = e("TOCHKA_MERCHANT_ID", "")
        self.sandbox = e("TOCHKA_SANDBOX", "0") == "1"
        self.receipts = e("RECEIPTS", "1") == "1"
        self.tax_system = e("TAX_SYSTEM", "")
        self.vat = e("VAT", "none")
        self.public_url = e("PUBLIC_URL", "").rstrip("/")
        self.webhook_secret = e("WEBHOOK_SECRET", "")
        self.port = int(e("PORT", "8080"))
        self.db_path = e("DB_PATH", os.path.join(HERE, "data", "club.db"))


def fmt_price(x):
    return f"{x:,.0f}".replace(",", " ")


def now():
    return datetime.now(timezone.utc)


def fmt_date(iso):
    return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m.%Y")


def add_month(dt):
    """Тот же день следующего месяца (31 января → 28/29 февраля)."""
    y, m = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


# ---------------------------------------------------------------- база

class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self.c.executescript("""
            create table if not exists users (
                tg_id integer primary key,
                username text, name text, email text,
                price real,
                status text not null default 'new',  -- new, pending, active, past_due, cancelled, expired
                order_id text, sub_id text,
                paid_until text,
                charging integer not null default 0,   -- 1, пока ждём итог списания
                last_attempt text, reminded_for text,
                created_at text, updated_at text
            );
            create table if not exists payments (
                id integer primary key autoincrement,
                tg_id integer, amount real, kind text, sub_id text, at text
            );
            create table if not exists settings (key text primary key, value text);
        """)
        self.c.commit()

    def user(self, tg_id):
        return self.c.execute("select * from users where tg_id=?", (tg_id,)).fetchone()

    def user_by_order(self, order_id, sub_id):
        return self.c.execute("select * from users where order_id=? or sub_id=?",
                              (order_id or "-", sub_id or "-")).fetchone()

    def upsert(self, tg_id, **kw):
        kw["updated_at"] = now().isoformat()
        if not self.user(tg_id):
            self.c.execute("insert into users (tg_id, created_at) values (?, ?)", (tg_id, kw["updated_at"]))
        self.c.execute(f"update users set {', '.join(k + '=?' for k in kw)} where tg_id=?", (*kw.values(), tg_id))
        self.c.commit()

    def take_charging(self, tg_id):
        """Снимает флаг «ждём списание». True только у того, кто снял его первым."""
        cur = self.c.execute("update users set charging=0 where tg_id=? and charging=1", (tg_id,))
        self.c.commit()
        return cur.rowcount == 1

    def add_payment(self, tg_id, amount, kind, sub_id):
        self.c.execute("insert into payments (tg_id, amount, kind, sub_id, at) values (?,?,?,?,?)",
                       (tg_id, amount, kind, sub_id, now().isoformat()))
        self.c.commit()

    def by_status(self, *statuses):
        q = ",".join("?" * len(statuses))
        return self.c.execute(f"select * from users where status in ({q})", statuses).fetchall()

    def setting(self, key, value=None):
        if value is None:
            r = self.c.execute("select value from settings where key=?", (key,)).fetchone()
            return r[0] if r else None
        self.c.execute("insert or replace into settings values (?, ?)", (key, value))
        self.c.commit()


# ---------------------------------------------------------------- Точка

class TochkaError(Exception):
    pass


def tochka_ssl():
    # API Точки на сертификате НУЦ Минцифры, его нет в стандартном наборе.
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(os.path.join(HERE, "certs", "russian_trusted_root_ca.pem"))
    return ctx


class Tochka:
    KEY_URL = "https://enter.tochka.com/doc/openapi/static/keys/public"

    def __init__(self, cfg, session):
        self.cfg = cfg
        self.s = session
        self.base = "https://enter.tochka.com/sandbox/v2/" if cfg.sandbox else "https://enter.tochka.com/uapi/"
        self.ssl = tochka_ssl()
        self._key = None

    async def req(self, method, path, data=None):
        headers = {"Authorization": f"Bearer {self.cfg.tochka_jwt}"}
        body = {"Data": data} if data is not None and not path.startswith("webhook") else data
        async with self.s.request(method, self.base + path, json=body, headers=headers, ssl=self.ssl,
                                  timeout=aiohttp.ClientTimeout(total=40)) as r:
            text = await r.text()
            if r.status >= 300:
                raise TochkaError(f"{method} {path}: HTTP {r.status} {text[:500]}")
            return json.loads(text) if text else {}

    async def create_subscription(self, order_id, amount, email, return_url):
        d = {
            "customerCode": self.cfg.customer_code,
            "amount": amount,
            "purpose": f"Подписка на клуб «{self.cfg.club_name}», 1 месяц",
            "recurring": True,
            "paymentLinkId": order_id,
        }
        if return_url:
            d["redirectUrl"] = d["failRedirectUrl"] = return_url
        if self.cfg.merchant_id:
            d["merchantId"] = self.cfg.merchant_id
        path = "acquiring/v1.0/subscriptions"
        if self.cfg.receipts:
            path = "acquiring/v1.0/subscriptions_with_receipt"
            d["Client"] = {"email": email}
            d["Items"] = [{"name": f"Доступ в клуб «{self.cfg.club_name}», 1 месяц", "amount": amount,
                           "quantity": 1, "vatType": self.cfg.vat, "paymentMethod": "full_payment",
                           "paymentObject": "service"}]
            if self.cfg.tax_system:
                d["taxSystemCode"] = self.cfg.tax_system
        data = (await self.req("POST", path, d))["Data"]
        return data["operationId"], data["paymentLink"]

    async def charge(self, sub_id, amount):
        data = await self.req("POST", f"acquiring/v1.0/subscriptions/{sub_id}/charge", {"amount": amount})
        return bool(data.get("Data", {}).get("result"))

    async def status(self, sub_id):
        return (await self.req("GET", f"acquiring/v1.0/subscriptions/{sub_id}/status"))["Data"]["status"]

    async def cancel(self, sub_id):
        await self.req("POST", f"acquiring/v1.0/subscriptions/{sub_id}/status", {"status": "Cancelled"})

    async def public_key(self):
        if self._key is None:
            async with self.s.get(self.KEY_URL, ssl=self.ssl) as r:
                k = json.loads(await r.text())
            self._key = jwt.PyJWK(k["keys"][0] if "keys" in k else k, "RS256").key
        return self._key

    async def decode_webhook(self, body):
        return jwt.decode(body.strip(), await self.public_key(), algorithms=["RS256"],
                          options={"verify_aud": False, "verify_exp": False})


# ---------------------------------------------------------------- Telegram

class Telegram:
    def __init__(self, token, session):
        self.url = f"https://api.telegram.org/bot{token}/"
        self.s = session

    async def call(self, method, **params):
        params = {k: v for k, v in params.items() if v is not None}
        async with self.s.post(self.url + method, json=params, timeout=aiohttp.ClientTimeout(total=70)) as r:
            j = await r.json()
        if not j.get("ok"):
            raise RuntimeError(f"Telegram {method}: {j.get('description')}")
        return j["result"]

    async def send(self, chat_id, text, buttons=None):
        markup = {"inline_keyboard": buttons} if buttons else None
        try:
            return await self.call("sendMessage", chat_id=chat_id, text=text, reply_markup=markup,
                                   disable_web_page_preview=True)
        except RuntimeError as e:
            log.warning("не отправилось %s: %s", chat_id, e)


def url_btn(text, url):
    return [{"text": text, "url": url}]


def cb_btn(text, data):
    return [{"text": text, "callback_data": data}]


# ---------------------------------------------------------------- логика клуба

class Club:
    def __init__(self, cfg, db, tg, tochka):
        self.cfg, self.db, self.tg, self.tochka = cfg, db, tg, tochka
        self.T = TEXTS
        self.bot_username = ""
        self.waiting_email = set()

    # --- вспомогательное

    async def price_for(self, tg_id):
        if self.cfg.old_chat_id and self.cfg.old_price < self.cfg.price:
            try:
                m = await self.tg.call("getChatMember", chat_id=self.cfg.old_chat_id, user_id=tg_id)
                if m["status"] in ("member", "administrator", "creator", "restricted"):
                    return self.cfg.old_price, True
            except RuntimeError:
                pass
        return self.cfg.price, False

    async def invite_link(self):
        link = self.db.setting("invite_link")
        if not link:
            r = await self.tg.call("createChatInviteLink", chat_id=self.cfg.club_chat_id,
                                   name="Вход по подписке", creates_join_request=True)
            link = r["invite_link"]
            self.db.setting("invite_link", link)
        return link

    def has_access(self, u):
        if not u or not u["paid_until"]:
            return False
        until = datetime.fromisoformat(u["paid_until"])
        if u["status"] == "cancelled" or (u["status"] == "active" and not u["sub_id"]):
            return until > now()
        if u["status"] in ("active", "past_due"):  # пока идут попытки списания, доступ есть
            return until + timedelta(days=self.cfg.grace_days) > now()
        return False

    # --- сообщения от людей

    async def on_message(self, msg):
        chat = msg["chat"]
        if chat["type"] != "private":
            return
        uid, text = msg["from"]["id"], (msg.get("text") or "").strip()
        if text.startswith("/") and uid in self.cfg.admin_ids and await self.admin(uid, text):
            return
        if uid in self.waiting_email and not text.startswith("/"):
            return await self.got_email(uid, text)
        self.waiting_email.discard(uid)
        self.db.upsert(uid, username=msg["from"].get("username"), name=msg["from"].get("first_name"))
        await self.show_start(uid)

    async def show_start(self, uid):
        u = self.db.user(uid)
        if self.has_access(u):
            until = fmt_date(u["paid_until"])
            if u["status"] == "cancelled":
                text = self.T["status_cancelled"].format(until=until)
                buttons = [url_btn(self.T["btn_enter"], await self.invite_link())]
            elif u["status"] == "past_due":
                until = fmt_date((datetime.fromisoformat(u["paid_until"]) +
                                  timedelta(days=self.cfg.grace_days)).isoformat())
                text = self.T["status_past_due"].format(until=until)
                buttons = [url_btn(self.T["btn_enter"], await self.invite_link())]
            else:
                text = self.T["status"].format(until=until, price=fmt_price(u["price"]))
                buttons = [url_btn(self.T["btn_enter"], await self.invite_link())]
                if u["sub_id"]:
                    buttons.append(cb_btn(self.T["btn_cancel"], "cancel"))
            return await self.tg.send(uid, text, buttons)
        price, old = await self.price_for(uid)
        text = self.T["start"].format(club=self.cfg.club_name, price=fmt_price(self.cfg.price))
        if old:
            text += self.T["old_member"].format(price=fmt_price(price), full=fmt_price(self.cfg.price))
        await self.tg.send(uid, text, [cb_btn(self.T["btn_join"].format(price=fmt_price(price)), "join")])

    async def got_email(self, uid, text):
        email = text.lower()
        if " " in email or "@" not in email or "." not in email.split("@")[-1]:
            return await self.tg.send(uid, self.T["bad_email"])
        self.waiting_email.discard(uid)
        self.db.upsert(uid, email=email)
        await self.create_payment(uid)

    async def create_payment(self, uid):
        u = self.db.user(uid)
        price, _ = await self.price_for(uid)
        order_id = f"club-{uid}-{uuid.uuid4().hex[:8]}"
        back = f"https://t.me/{self.bot_username}" if self.bot_username else None
        try:
            sub_id, link = await self.tochka.create_subscription(order_id, price, u["email"], back)
        except (TochkaError, aiohttp.ClientError, asyncio.TimeoutError, KeyError) as e:
            log.error("подписка не создалась для %s: %s", uid, e)
            return await self.tg.send(uid, self.T["error"].format(manager=self.cfg.manager))
        self.db.upsert(uid, status="pending", order_id=order_id, sub_id=sub_id, price=price, charging=0)
        await self.tg.send(uid, self.T["pay"].format(price=fmt_price(price)),
                           [url_btn(self.T["btn_pay"], link), cb_btn(self.T["btn_paid"], "paid")])

    async def on_callback(self, cq):
        uid, data = cq["from"]["id"], cq.get("data")
        await self.tg.call("answerCallbackQuery", callback_query_id=cq["id"])
        self.db.upsert(uid, username=cq["from"].get("username"), name=cq["from"].get("first_name"))
        u = self.db.user(uid)
        if data == "join":
            if self.has_access(u) and u["status"] != "cancelled":
                return await self.show_start(uid)
            if self.cfg.receipts and not u["email"]:
                self.waiting_email.add(uid)
                return await self.tg.send(uid, self.T["ask_email"])
            await self.create_payment(uid)
        elif data == "paid":
            if u["status"] != "pending":
                return await self.show_start(uid)
            try:
                st = await self.tochka.status(u["sub_id"])
            except (TochkaError, aiohttp.ClientError, asyncio.TimeoutError, KeyError) as e:
                log.error("статус подписки %s: %s", uid, e)
                st = None
            if st in ("Active", "Trial"):
                await self.activate(uid)
            else:
                await self.tg.send(uid, self.T["not_paid_yet"])
        elif data == "cancel" and u["sub_id"] and u["status"] in ("active", "past_due"):
            await self.tg.send(uid, self.T["confirm_cancel"].format(until=fmt_date(u["paid_until"])),
                               [cb_btn(self.T["btn_yes_cancel"], "cancel_yes"), cb_btn(self.T["btn_no"], "start")])
        elif data == "cancel_yes" and u["sub_id"] and u["status"] in ("active", "past_due"):
            try:
                await self.tochka.cancel(u["sub_id"])
            except (TochkaError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.error("отмена подписки %s: %s", uid, e)
                return await self.tg.send(uid, self.T["error"].format(manager=self.cfg.manager))
            self.db.upsert(uid, status="cancelled")
            await self.tg.send(uid, self.T["cancelled"].format(until=fmt_date(u["paid_until"])))
            await self.notify_admins(f"Отменил подписку: {self.who(u)}")
        else:
            await self.show_start(uid)

    # --- оплата

    async def activate(self, uid):
        """Первая оплата прошла. Срабатывает один раз, даже если придут и вебхук, и кнопка."""
        cur = self.db.c.execute("update users set status='active' where tg_id=? and status='pending'", (uid,))
        self.db.c.commit()
        if cur.rowcount != 1:
            return
        u = self.db.user(uid)
        until = add_month(now()).isoformat()
        self.db.upsert(uid, paid_until=until, last_attempt=None, reminded_for=None)
        self.db.add_payment(uid, u["price"], "first", u["sub_id"])
        await self.tg.send(uid, self.T["welcome"].format(until=fmt_date(until)),
                           [url_btn(self.T["btn_enter"], await self.invite_link())])
        await self.notify_admins(f"Новый участник: {self.who(u)}, {fmt_price(u['price'])} ₽")

    async def renewed(self, uid):
        u = self.db.user(uid)
        until = add_month(datetime.fromisoformat(u["paid_until"])).isoformat()
        self.db.upsert(uid, status="active", paid_until=until, last_attempt=None)
        self.db.add_payment(uid, u["price"], "renew", u["sub_id"])
        await self.tg.send(uid, self.T["renewed"].format(until=fmt_date(until)))

    async def on_tochka_webhook(self, data):
        if data.get("webhookType") != "acquiringInternetPayment" or data.get("status") != "APPROVED":
            return
        u = self.db.user_by_order(data.get("paymentLinkId"), data.get("operationId"))
        if not u:
            log.info("вебхук не про клуб: %s", data.get("paymentLinkId"))
            return
        if u["status"] == "pending":
            await self.activate(u["tg_id"])
        elif self.db.take_charging(u["tg_id"]):
            # ответ на списание потерялся, а деньги пришли
            await self.renewed(u["tg_id"])

    async def try_charge(self, u):
        uid = u["tg_id"]
        self.db.upsert(uid, charging=1, last_attempt=now().isoformat())
        try:
            ok = await self.tochka.charge(u["sub_id"], u["price"])
        except (TochkaError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            # Итог неизвестен: флаг charging оставляем, если деньги дойдут, вебхук продлит доступ.
            log.error("списание %s: %s", uid, e)
            ok = None
        if ok:
            if self.db.take_charging(uid):  # иначе вебхук успел продлить раньше
                await self.renewed(uid)
            return
        if ok is False:
            self.db.take_charging(uid)
        if self.db.user(uid)["status"] == "active":
            self.db.upsert(uid, status="past_due")
            until = datetime.fromisoformat(u["paid_until"]) + timedelta(days=self.cfg.grace_days)
            await self.tg.send(uid, self.T["charge_failed"].format(price=fmt_price(u["price"]),
                                                                   until=fmt_date(until.isoformat())))
            await self.notify_admins(f"Не прошло списание: {self.who(u)}")

    async def expire(self, u, cancel_sub=True):
        uid = u["tg_id"]
        if cancel_sub and u["sub_id"] and u["status"] != "cancelled":
            try:
                await self.tochka.cancel(u["sub_id"])
            except (TochkaError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.error("отмена при удалении %s: %s", uid, e)
        self.db.upsert(uid, status="expired", charging=0)
        await self.kick(uid)
        await self.tg.send(uid, self.T["expired"], [cb_btn(self.T["btn_return"], "join")])
        await self.notify_admins(f"Удалён из клуба: {self.who(u)}")

    async def kick(self, uid):
        try:
            await self.tg.call("banChatMember", chat_id=self.cfg.club_chat_id, user_id=uid)
            await self.tg.call("unbanChatMember", chat_id=self.cfg.club_chat_id, user_id=uid, only_if_banned=True)
        except RuntimeError as e:
            log.warning("не удалось удалить %s: %s", uid, e)

    async def tick(self):
        """Раз в полчаса: напоминания, списания, удаление должников."""
        t = now()
        for u in self.db.by_status("active"):
            until = datetime.fromisoformat(u["paid_until"])
            if not u["sub_id"]:  # выдан вручную
                if until <= t:
                    await self.expire(u, cancel_sub=False)
            elif until <= t:
                await self.try_charge(u)
            elif until - t <= timedelta(days=1) and u["reminded_for"] != u["paid_until"]:
                self.db.upsert(u["tg_id"], reminded_for=u["paid_until"])
                await self.tg.send(u["tg_id"], self.T["remind"].format(price=fmt_price(u["price"])))
        for u in self.db.by_status("past_due"):
            until = datetime.fromisoformat(u["paid_until"])
            if until + timedelta(days=self.cfg.grace_days) <= t:
                await self.expire(u)
            elif not u["last_attempt"] or t - datetime.fromisoformat(u["last_attempt"]) >= timedelta(hours=23):
                await self.try_charge(u)
        for u in self.db.by_status("cancelled"):
            if datetime.fromisoformat(u["paid_until"]) <= t:
                await self.expire(u, cancel_sub=False)

    # --- группа

    async def on_join_request(self, req):
        uid = req["from"]["id"]
        if req["chat"]["id"] != self.cfg.club_chat_id:
            return
        if self.has_access(self.db.user(uid)):
            await self.tg.call("approveChatJoinRequest", chat_id=self.cfg.club_chat_id, user_id=uid)
        else:
            await self.tg.call("declineChatJoinRequest", chat_id=self.cfg.club_chat_id, user_id=uid)
            await self.tg.send(uid, self.T["join_declined"])

    # --- админка

    def who(self, u):
        name = u["name"] or ""
        return f"{name} @{u['username']} ({u['tg_id']})" if u["username"] else f"{name} ({u['tg_id']})"

    async def notify_admins(self, text):
        for a in self.cfg.admin_ids:
            await self.tg.send(a, text)

    async def admin(self, uid, text):
        parts = text.split()
        cmd = parts[0].split("@")[0]
        if cmd == "/stats":
            rows = self.db.c.execute("select status, count(*), sum(price) from users group by status").fetchall()
            month = self.db.c.execute("select coalesce(sum(amount),0) from payments where at >= ?",
                                      ((now() - timedelta(days=30)).isoformat(),)).fetchone()[0]
            lines = [f"{r[0]}: {r[1]}" for r in rows]
            paying = sum(r[2] or 0 for r in rows if r[0] in ("active", "past_due"))
            lines += [f"\nПлатящих в месяц: {fmt_price(paying)} ₽", f"Пришло за 30 дней: {fmt_price(month)} ₽"]
            await self.tg.send(uid, "\n".join(lines))
        elif cmd == "/grant" and len(parts) == 3:
            target, days = int(parts[1]), int(parts[2])
            self.db.upsert(target, status="active", sub_id=None, price=0,
                           paid_until=(now() + timedelta(days=days)).isoformat())
            await self.tg.send(target, self.T["welcome"].format(until=fmt_date(self.db.user(target)["paid_until"])),
                               [url_btn(self.T["btn_enter"], await self.invite_link())])
            await self.tg.send(uid, f"Выдал доступ {target} на {days} дн.")
        elif cmd == "/revoke" and len(parts) == 2:
            u = self.db.user(int(parts[1]))
            if u:
                await self.expire(u)
            await self.tg.send(uid, "Готово" if u else "Такого нет в базе")
        elif cmd == "/admin":
            await self.tg.send(uid, "/stats — сводка\n/grant ID ДНЕЙ — дать доступ бесплатно\n/revoke ID — удалить")
        else:
            return False
        return True


# ---------------------------------------------------------------- запуск

async def poll_telegram(club):
    offset = None
    while True:
        try:
            updates = await club.tg.call("getUpdates", offset=offset, timeout=50,
                                         allowed_updates=["message", "callback_query", "chat_join_request"])
        except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("getUpdates: %s", e)
            await asyncio.sleep(5)
            continue
        for up in updates:
            offset = up["update_id"] + 1
            try:
                if "message" in up:
                    await club.on_message(up["message"])
                elif "callback_query" in up:
                    await club.on_callback(up["callback_query"])
                elif "chat_join_request" in up:
                    await club.on_join_request(up["chat_join_request"])
            except Exception:
                log.exception("ошибка при обработке %s", up.get("update_id"))


async def scheduler(club):
    while True:
        try:
            await club.tick()
        except Exception:
            log.exception("ошибка планировщика")
        await asyncio.sleep(30 * 60)


def make_app(club):
    async def hook(request):
        if request.match_info["secret"] != club.cfg.webhook_secret:
            return web.Response(status=404)
        body = await request.text()
        try:
            data = await club.tochka.decode_webhook(body)
        except Exception as e:
            log.warning("вебхук с неверной подписью: %s", e)
            return web.Response(status=200)  # Точка проверяет доступность адреса, отвечаем 200
        try:
            await club.on_tochka_webhook(data)
        except Exception:
            log.exception("ошибка вебхука")
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post("/tochka/{secret}", hook)
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    return app


async def main():
    cfg = Config()
    for name in ("bot_token", "club_chat_id", "tochka_jwt", "customer_code", "webhook_secret"):
        if not getattr(cfg, name):
            sys.exit(f"Не заполнено {name.upper()} в .env")
    async with aiohttp.ClientSession() as s:
        club = Club(cfg, DB(cfg.db_path), Telegram(cfg.bot_token, s), Tochka(cfg, s))
        club.bot_username = (await club.tg.call("getMe"))["username"]
        await club.tg.call("deleteWebhook")
        runner = web.AppRunner(make_app(club))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
        log.info("бот @%s запущен, вебхуки Точки ждём на :%s", club.bot_username, cfg.port)
        await asyncio.gather(poll_telegram(club), scheduler(club))


async def check():
    cfg = Config()
    async with aiohttp.ClientSession() as s:
        if cfg.bot_token:
            me = await Telegram(cfg.bot_token, s).call("getMe")
            print(f"Бот: @{me['username']} ✓")
            if cfg.club_chat_id:
                m = await Telegram(cfg.bot_token, s).call("getChatMember", chat_id=cfg.club_chat_id, user_id=me["id"])
                print(f"В клубе бот: {m['status']}, может удалять: {m.get('can_restrict_members')}, "
                      f"приглашать: {m.get('can_invite_users')}")
        t = Tochka(cfg, s)
        for c in (await t.req("GET", "open-banking/v1.0/customers"))["Data"]["Customer"]:
            print(f"customerCode={c.get('customerCode')} тип={c.get('customerType')} {c.get('shortName', '')}")
        code = cfg.customer_code or input("Впиши customerCode с типом Business: ").strip()
        r = await t.req("GET", f"acquiring/v1.0/retailers?customerCode={code}")
        for x in r["Data"].get("Retailer", []):
            print(f"merchantId={x.get('merchantId')} статус={x.get('status')} активна={x.get('isActive')} {x.get('name', '')}")


async def setup_webhook():
    cfg = Config()
    url = f"{cfg.public_url}/tochka/{cfg.webhook_secret}"
    async with aiohttp.ClientSession() as s:
        t = Tochka(cfg, s)
        body = {"webhooksList": ["acquiringInternetPayment"], "url": url}
        try:
            await t.req("PUT", f"webhook/v1.0/{cfg.tochka_client_id}", body)
        except TochkaError as e:
            if "exist" not in str(e).lower() and "409" not in str(e):
                raise
            await t.req("POST", f"webhook/v1.0/{cfg.tochka_client_id}", body)
        print("Вебхук подключён:", url.replace(cfg.webhook_secret, "***"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    asyncio.run({"check": check, "setup-webhook": setup_webhook}.get(cmd, main)())
