"""Проверка логики клуба без сети: Telegram и Точка подменены.

  python -m unittest test_bot -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

USER = 111
ADMIN = 999
CLUB = -100500


class FakeTG:
    def __init__(self):
        self.calls = []
        self.old_members = set()

    async def call(self, method, **p):
        self.calls.append((method, p))
        if method == "getChatMember":
            if p["user_id"] in self.old_members:
                return {"status": "member"}
            return {"status": "left"}
        if method == "createChatInviteLink":
            return {"invite_link": "https://t.me/+club"}
        return True

    async def send(self, chat_id, text, buttons=None):
        self.calls.append(("send", {"chat_id": chat_id, "text": text, "buttons": buttons}))

    def sent(self, chat_id):
        return [p["text"] for m, p in self.calls if m == "send" and p["chat_id"] == chat_id]

    def did(self, method):
        return [p for m, p in self.calls if m == method]


class FakeTochka:
    def __init__(self):
        self.charge_ok = True
        self.charges = []
        self.cancelled = []
        self.created = []
        self.sub_status = "Preparing"

    async def create_subscription(self, order_id, amount, email, back, period="месяц"):
        self.created.append((order_id, amount, email))
        return f"sub-{len(self.created)}", "https://pay.tochka/link"

    async def charge(self, sub_id, amount):
        self.charges.append((sub_id, amount))
        if self.charge_ok is None:
            raise bot.TochkaError("timeout")
        return self.charge_ok

    async def status(self, sub_id):
        return self.sub_status

    async def cancel(self, sub_id):
        self.cancelled.append(sub_id)


def msg(text, uid=USER):
    return {"chat": {"type": "private", "id": uid}, "from": {"id": uid, "first_name": "Лена", "username": "lena"},
            "text": text}


def cq(data, uid=USER):
    return {"id": "1", "from": {"id": uid, "first_name": "Лена", "username": "lena"}, "data": data}


class ClubTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = bot.Config.__new__(bot.Config)
        cfg.__dict__.update(club_chat_id=CLUB, admin_ids={ADMIN}, club_name="Клуб", manager="m",
                            plans={1: 2990.0, 3: 4990.0, 12: 12990.0}, grace_days=3, receipts=True)
        self.db = bot.DB(os.path.join(self.tmp.name, "t.db"))
        self.tg, self.tochka = FakeTG(), FakeTochka()
        self.club = bot.Club(cfg, self.db, self.tg, self.tochka)

    def tearDown(self):
        self.tmp.cleanup()

    def shift(self, uid, **delta):
        """Сдвигает paid_until в прошлое, будто прошло время."""
        u = self.db.user(uid)
        self.db.upsert(uid, paid_until=(datetime.fromisoformat(u["paid_until"]) - timedelta(**delta)).isoformat())

    async def join_and_pay(self, months=1):
        await self.club.on_message(msg("/start"))
        await self.club.on_callback(cq(f"join:{months}"))
        await self.club.on_message(msg("Lena@Mail.ru"))
        u = self.db.user(USER)
        await self.club.on_tochka_webhook({"webhookType": "acquiringInternetPayment", "status": "APPROVED",
                                           "paymentLinkId": u["order_id"], "operationId": u["sub_id"]})

    async def test_full_path(self):
        await self.join_and_pay()
        u = self.db.user(USER)
        self.assertEqual(u["status"], "active")
        self.assertEqual(u["email"], "lena@mail.ru")
        self.assertEqual(self.tochka.created[0][1], 2990.0)
        self.assertTrue(any("добро пожаловать" in t for t in self.tg.sent(USER)))

        # заявка в группу одобряется, чужая отклоняется
        await self.club.on_join_request({"from": {"id": USER}, "chat": {"id": CLUB}})
        await self.club.on_join_request({"from": {"id": 222}, "chat": {"id": CLUB}})
        self.assertEqual([p["user_id"] for p in self.tg.did("approveChatJoinRequest")], [USER])
        self.assertEqual([p["user_id"] for p in self.tg.did("declineChatJoinRequest")], [222])

        # за день до списания напоминание, ровно одно
        self.db.upsert(USER, paid_until=(bot.now() + timedelta(hours=12)).isoformat())
        await self.club.tick()
        await self.club.tick()
        self.assertEqual(sum("завтра спишется" in t for t in self.tg.sent(USER)), 1)

        # списание прошло, доступ продлён на месяц от старой даты
        old_until = datetime.fromisoformat(self.db.user(USER)["paid_until"])
        self.shift(USER, days=2)
        before = datetime.fromisoformat(self.db.user(USER)["paid_until"])
        await self.club.tick()
        u = self.db.user(USER)
        self.assertEqual(u["status"], "active")
        self.assertEqual(datetime.fromisoformat(u["paid_until"]), bot.add_months(before))
        self.assertGreater(datetime.fromisoformat(u["paid_until"]), old_until)
        self.assertEqual(len(self.tochka.charges), 1)

        # вебхук о том же списании не продлевает второй раз
        until = u["paid_until"]
        await self.club.on_tochka_webhook({"webhookType": "acquiringInternetPayment", "status": "APPROVED",
                                           "paymentLinkId": u["order_id"], "operationId": u["sub_id"]})
        self.assertEqual(self.db.user(USER)["paid_until"], until)

    async def test_failed_charge_then_kick(self):
        await self.join_and_pay()
        self.tochka.charge_ok = False
        self.shift(USER, days=32)
        await self.club.tick()
        self.assertEqual(self.db.user(USER)["status"], "past_due")
        self.assertTrue(any("Не получилось списать" in t for t in self.tg.sent(USER)))
        # в льготные дни доступ остаётся, повтор не чаще раза в сутки
        await self.club.tick()
        self.assertEqual(len(self.tochka.charges), 1)
        await self.club.on_join_request({"from": {"id": USER}, "chat": {"id": CLUB}})
        self.assertEqual(len(self.tg.did("approveChatJoinRequest")), 1)
        # льготный срок вышел: удаляем и отменяем подписку
        self.shift(USER, days=3)
        await self.club.tick()
        self.assertEqual(self.db.user(USER)["status"], "expired")
        self.assertEqual([p["user_id"] for p in self.tg.did("banChatMember")], [USER])
        self.assertEqual([p["user_id"] for p in self.tg.did("unbanChatMember")], [USER])
        self.assertEqual(self.tochka.cancelled, ["sub-1"])
        # вернуться можно заново
        self.tochka.charge_ok = True
        await self.club.on_callback(cq("join:1"))
        self.assertEqual(self.db.user(USER)["status"], "pending")
        self.assertEqual(len(self.tochka.created), 2)

    async def test_lost_charge_response_webhook_renews(self):
        await self.join_and_pay()
        self.tochka.charge_ok = None  # ответ банка не дошёл
        self.shift(USER, days=32)
        before = self.db.user(USER)["paid_until"]
        await self.club.tick()
        self.assertEqual(self.db.user(USER)["status"], "past_due")
        u = self.db.user(USER)
        await self.club.on_tochka_webhook({"webhookType": "acquiringInternetPayment", "status": "APPROVED",
                                           "paymentLinkId": u["order_id"], "operationId": u["sub_id"]})
        u = self.db.user(USER)
        self.assertEqual(u["status"], "active")
        self.assertEqual(u["paid_until"], bot.add_months(datetime.fromisoformat(before)).isoformat())

    async def test_cancel_keeps_access_until_end(self):
        await self.join_and_pay()
        await self.club.on_callback(cq("cancel"))
        await self.club.on_callback(cq("cancel_yes"))
        self.assertEqual(self.db.user(USER)["status"], "cancelled")
        self.assertEqual(self.tochka.cancelled, ["sub-1"])
        await self.club.tick()
        self.assertEqual(self.tg.did("banChatMember"), [])
        self.shift(USER, days=32)
        await self.club.tick()
        self.assertEqual(self.tochka.charges, [])
        self.assertEqual(self.db.user(USER)["status"], "expired")
        self.assertEqual(len(self.tg.did("banChatMember")), 1)

    async def test_year_plan_from_landing_link(self):
        await self.club.on_message(msg("/start"))
        self.assertTrue(any("личный разбор" in t for t in self.tg.sent(USER)))
        await self.club.on_message(msg("/start m12"))  # ссылка с лендинга сразу на год
        self.assertTrue(any("почту" in t for t in self.tg.sent(USER)))
        await self.club.on_message(msg("не почта"))
        self.assertTrue(any("опечатка" in t for t in self.tg.sent(USER)))
        await self.club.on_message(msg("a@b.ru"))
        self.assertEqual(self.tochka.created[0][1], 12990.0)
        # кнопка «Я оплатил(а)»: пока не оплачено, потом активирует один раз
        await self.club.on_callback(cq("paid"))
        self.assertEqual(self.db.user(USER)["status"], "pending")
        self.tochka.sub_status = "Active"
        await self.club.on_callback(cq("paid"))
        await self.club.on_callback(cq("paid"))
        u = self.db.user(USER)
        self.assertEqual(u["status"], "active")
        self.assertEqual(self.db.c.execute("select count(*) from payments").fetchone()[0], 1)
        self.assertTrue(any("менеджеру" in t for t in self.tg.sent(USER)))
        self.assertTrue(any("личный разбор" in t for t in self.tg.sent(ADMIN)))
        # продление через год, на год, по той же цене
        start = datetime.fromisoformat(u["paid_until"])
        self.db.upsert(USER, paid_until=(bot.now() - timedelta(minutes=1)).isoformat())
        before = datetime.fromisoformat(self.db.user(USER)["paid_until"])
        await self.club.tick()
        self.assertEqual(self.tochka.charges, [("sub-1", 12990.0)])
        self.assertEqual(self.db.user(USER)["paid_until"], bot.add_months(before, 12).isoformat())
        self.assertGreater(start, before)

    async def test_quarter_plan(self):
        await self.join_and_pay(months=3)
        u = self.db.user(USER)
        self.assertEqual(u["price"], 4990.0)
        self.assertGreater(datetime.fromisoformat(u["paid_until"]), bot.now() + timedelta(days=88))

    async def test_admin_grant(self):
        await self.club.on_message(msg("/grant 333 7", uid=ADMIN))
        self.assertTrue(self.club.has_access(self.db.user(333)))
        await self.club.on_message(msg("/grant 333 7", uid=USER))  # не админ: команда не работает
        self.assertEqual(len(self.tochka.created), 0)
        self.shift(333, days=8)
        await self.club.tick()
        self.assertEqual(self.db.user(333)["status"], "expired")
        self.assertEqual(self.tochka.cancelled, [])

    def test_add_months(self):
        d = datetime(2026, 1, 31, 12)
        self.assertEqual(bot.add_months(d), datetime(2026, 2, 28, 12))
        self.assertEqual(bot.add_months(datetime(2026, 12, 15)), datetime(2027, 1, 15))
        self.assertEqual(bot.add_months(datetime(2026, 11, 30), 3), datetime(2027, 2, 28))
        self.assertEqual(bot.add_months(datetime(2026, 10, 1), 12), datetime(2027, 10, 1))


class WebhookSignatureTest(unittest.IsolatedAsyncioTestCase):
    async def test_signature(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        t = bot.Tochka.__new__(bot.Tochka)
        t._key = key.public_key()
        token = jwt.encode({"status": "APPROVED", "amount": "990"}, key, algorithm="RS256")
        self.assertEqual((await t.decode_webhook(token + "\n"))["status"], "APPROVED")
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(jwt.InvalidSignatureError):
            await t.decode_webhook(jwt.encode({"status": "APPROVED"}, other, algorithm="RS256"))

    def test_tochka_ssl_loads(self):
        bot.tochka_ssl()


if __name__ == "__main__":
    unittest.main()
