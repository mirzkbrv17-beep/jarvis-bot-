import os, json, time, hmac, hashlib, asyncio, random, re, logging
from urllib.parse import parse_qsl
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (ReplyKeyboardMarkup, KeyboardButton, WebAppInfo, InlineKeyboardMarkup,
                           InlineKeyboardButton, MenuButtonWebApp, Message)
from telethon import TelegramClient, events, errors, functions
from telethon.sessions import StringSession
from cryptography.fernet import Fernet

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("jarvis")

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
WEBAPP_URL = os.environ["WEBAPP_URL"].rstrip("/")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
fernet = Fernet(os.environ["SECRET_KEY"].encode())
DB_PATH = os.environ.get("DB_PATH", "data.db")
PORT = int(os.environ.get("PORT", "8080"))
BASE = os.path.dirname(os.path.abspath(__file__))
CONTACT, PRICE = "mirzkbrv17", "25 000"

FREE = dict(accounts=1, groups=5, texts=1, min_cycle=120, signature=True)
PRO = dict(accounts=3, groups=500, texts=10, min_cycle=60, signature=False)

SCHEMA = """
create table if not exists users(id integer primary key, pro_until integer default 0, active_acc integer default 0, created integer);
create table if not exists accounts(id integer primary key autoincrement, user_id integer, phone text, session text, name text, username text);
create table if not exists settings(account_id integer primary key, texts text default '[]', delay integer default 20,
  cycle integer default 120, running integer default 0, channel text default '', forward integer default 0, via_bot integer default 0,
  dm_on integer default 0, dm_text text default '', grp_on integer default 0, grp_text text default '', sub_on integer default 0,
  sent integer default 0, failed integer default 0, cycles integer default 0, last_error text default '', last_run integer default 0);
create table if not exists groups(account_id integer, chat_id integer, title text, enabled integer default 0, primary key(account_id, chat_id));
"""

# Eski bazalar uchun: yetishmayotgan ustunlarni qo'shadi (kanal moduli)
MIGRATE = {
    "settings": [("ch_texts", "text default '[]'"), ("ch_mode", "text default 'interval'"),
                 ("ch_every", "integer default 3600"), ("ch_times", "text default '[]'"),
                 ("ch_once", "integer default 0"), ("ch_tz", "integer default 300"),
                 ("ch_limit", "integer default 0"), ("ch_fwd", "integer default 0"),
                 ("ch_fwd_n", "integer default 0"), ("ch_running", "integer default 0"),
                 ("ch_posts", "integer default 0"), ("ch_fwd_sent", "integer default 0"),
                 ("ch_failed", "integer default 0"), ("ch_error", "text default ''"),
                 ("ch_last", "integer default 0")],
    "groups": [("fwd", "integer default 0")],
}

DB = None
BOT_USERNAME = ""
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
clients, tasks, ch_tasks, pending, replied = {}, {}, {}, {}, {}


class ApiError(Exception):
    pass


async def q(sql, args=(), one=False):
    cur = await DB.execute(sql, args)
    rows = await cur.fetchall()
    await DB.commit()
    return (rows[0] if rows else None) if one else rows


async def limits(uid):
    u = await q("select pro_until from users where id=?", (uid,), one=True)
    return PRO if u and u["pro_until"] > time.time() else FREE


async def active_of(uid):
    u = await q("select active_acc from users where id=?", (uid,), one=True)
    ids = [a["id"] for a in await q("select id from accounts where user_id=?", (uid,))]
    act = u["active_acc"] if u and u["active_acc"] in ids else (ids[0] if ids else 0)
    if u and act != u["active_acc"]:
        await q("update users set active_acc=? where id=?", (act, uid))
    return act


# ---------------- Telethon userbot ----------------
LINK = re.compile(r"(?:https?://)?t\.me/(\+[\w-]+|joinchat/[\w-]+|[A-Za-z]\w{3,})")


async def join(client, l):
    try:
        if l.startswith("+"):
            await client(functions.messages.ImportChatInviteRequest(l[1:]))
        elif l.startswith("joinchat/"):
            await client(functions.messages.ImportChatInviteRequest(l.split("/")[1]))
        else:
            await client(functions.channels.JoinChannelRequest(l))
        return True
    except errors.UserAlreadyParticipantError:
        return True
    except Exception as e:
        log.info("join fail %s: %s", l, type(e).__name__)
        return False


async def autosub(client, m):
    text = (m.raw_text or "")
    if not any(k in text.lower() for k in ("obuna", "подпис", "subscribe", "a'zo", "azo bo")):
        return
    links = set(LINK.findall(text))
    for b in (m.buttons or []):
        for x in (b if isinstance(b, list) else [b]):
            if getattr(x, "url", None):
                links.update(LINK.findall(x.url))
    for l in list(links)[:5]:
        await join(client, l)
        await asyncio.sleep(3)


async def on_message(acc, ev):
    try:
        s = await q("select * from settings where account_id=?", (acc,), one=True)
        if not s:
            return
        m, now = ev.message, time.time()
        if ev.is_private:
            if not (s["dm_on"] and s["dm_text"]):
                return
            sender = await ev.get_sender()
            if getattr(sender, "bot", False) or getattr(sender, "id", 0) == 777000:
                return
            key = (acc, ev.chat_id)
            if now - replied.get(key, 0) < 6 * 3600:
                return
            replied[key] = now
            await ev.reply(s["dm_text"])
        elif ev.is_group:
            hit = bool(m.mentioned)
            if not hit and m.is_reply:
                r = await m.get_reply_message()
                hit = bool(r and r.out)
            if not hit:
                return
            key = (acc, ev.chat_id)
            if s["grp_on"] and s["grp_text"] and now - replied.get(key, 0) >= 60:
                replied[key] = now
                await ev.reply(s["grp_text"])
            if s["sub_on"]:
                await autosub(clients[acc], m)
    except Exception:
        log.exception("on_message")


def attach(acc, client):
    client.add_event_handler(lambda ev: on_message(acc, ev), events.NewMessage(incoming=True))


async def run_loop(acc):
    try:
        while True:
            row = await q("select a.user_id uid, s.* from accounts a join settings s on s.account_id=a.id where a.id=?", (acc,), one=True)
            if not row or not row["running"]:
                return
            s = dict(row)
            lim = await limits(s["uid"])
            texts = json.loads(s["texts"])[:lim["texts"]]
            groups = [dict(g) for g in await q("select chat_id from groups where account_id=? and enabled=1", (acc,))][:lim["groups"]]
            client = clients.get(acc)
            if not texts or not groups or not client:
                await q("update settings set running=0,last_error=? where account_id=?", ("Xabar, guruh yoki akkaunt yo'q", acc))
                return
            text = texts[s["cycles"] % len(texts)]
            msg = text + (f"\n\n«@{BOT_USERNAME} orqali yuborildi»" if lim["signature"] else "")
            t0 = time.monotonic()
            sent = failed = 0
            err = ""
            for g in groups:
                try:
                    if s["via_bot"]:
                        await bot.send_message(g["chat_id"], msg)
                    else:
                        await client.send_message(g["chat_id"], msg)
                    sent += 1
                except errors.FloodWaitError as e:
                    failed += 1
                    err = f"FloodWait {e.seconds}s"
                    await asyncio.sleep(min(e.seconds, 3600) + 5)
                except Exception as e:
                    failed += 1
                    err = type(e).__name__
                await asyncio.sleep(max(10, s["delay"]) + random.uniform(0, 5))
            await q("update settings set sent=sent+?,failed=failed+?,cycles=cycles+1,last_error=?,last_run=? where account_id=?",
                    (sent, failed, err, int(time.time()), acc))
            interval = max(s["cycle"], lim["min_cycle"])  # soniyada, tsikl boshidan boshigacha
            await asyncio.sleep(max(1, interval - (time.monotonic() - t0)))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("run_loop")
        await q("update settings set running=0,last_error='Ichki xato' where account_id=?", (acc,))


def start_task(acc):
    t = tasks.get(acc)
    if not t or t.done():
        tasks[acc] = asyncio.create_task(run_loop(acc))


def start_ch(acc):
    t = ch_tasks.get(acc)
    if not t or t.done():
        ch_tasks[acc] = asyncio.create_task(ch_loop(acc))


def stop_ch(acc):
    t = ch_tasks.pop(acc, None)
    if t:
        t.cancel()


def next_daily(times, tz_min, now):
    """Keyingi "HH:MM" vaqtigacha (foydalanuvchi vaqt zonasida) UNIX vaqt."""
    off = tz_min * 60
    day0 = (int(now) + off) // 86400 * 86400
    c = []
    for d in (0, 1):
        for t in times:
            h, mi = (int(x) for x in t.split(":"))
            ts = day0 + d * 86400 + h * 3600 + mi * 60 - off
            if ts > now:
                c.append(ts)
    return min(c)


def ch_target(v):
    return int(v) if re.fullmatch(r"-?\d+", v) else v


async def ch_stop_db(acc, err=""):
    await q("update settings set ch_running=0,ch_error=? where account_id=?", (err, acc))


async def ch_loop(acc):
    """Kanalga xabar yozadi, xohlansa tanlangan guruhlarga forward qiladi."""
    try:
        while True:
            row = await q("select a.user_id uid, s.* from accounts a join settings s on s.account_id=a.id where a.id=?", (acc,), one=True)
            if not row or not row["ch_running"]:
                return
            s = dict(row)
            lim = await limits(s["uid"])
            texts = json.loads(s["ch_texts"])[:lim["texts"]]
            client = clients.get(acc)
            if not texts or not s["channel"] or not client:
                return await ch_stop_db(acc, "Kanal, xabar yoki akkaunt yo'q")
            if s["ch_limit"] and s["ch_posts"] >= s["ch_limit"]:
                return await ch_stop_db(acc, "")
            now, mode = time.time(), s["ch_mode"]
            if mode == "daily":
                times = json.loads(s["ch_times"])
                if not times:
                    return await ch_stop_db(acc, "Soat kiritilmagan")
                wait = next_daily(times, s["ch_tz"], now) - now + 1
            elif mode == "once":
                wait = s["ch_once"] - now + 1
                if wait < -3600:
                    return await ch_stop_db(acc, "Yuborish vaqti o'tib ketgan")
                wait = max(0, wait)
            else:
                every = max(s["ch_every"], lim["min_cycle"])
                wait = 0 if not s["ch_last"] else max(0, s["ch_last"] + every - now)
            if wait > 0:
                await asyncio.sleep(wait)
                r = await q("select ch_running from settings where account_id=?", (acc,), one=True)
                if not r or not r["ch_running"]:
                    return
            await q("update settings set ch_last=? where account_id=?", (int(time.time()), acc))
            msg = texts[s["ch_posts"] % len(texts)] + (f"\n\n«@{BOT_USERNAME} orqali yuborildi»" if lim["signature"] else "")
            try:
                post = await client.send_message(ch_target(s["channel"]), msg)
            except errors.FloodWaitError as e:
                await q("update settings set ch_error=? where account_id=?", (f"FloodWait {e.seconds}s", acc))
                await asyncio.sleep(min(e.seconds, 3600) + 5)
                continue
            except Exception as e:
                await q("update settings set ch_failed=ch_failed+1 where account_id=?", (acc,))
                return await ch_stop_db(acc, f"Kanalga yuborilmadi: {type(e).__name__}")
            sent = failed = 0
            err = ""
            if s["ch_fwd"]:
                n = min(s["ch_fwd_n"], lim["groups"])
                for g in await q("select chat_id from groups where account_id=? and fwd=1 limit ?", (acc, n)):
                    try:
                        await client.forward_messages(g["chat_id"], post)
                        sent += 1
                    except errors.FloodWaitError as e:
                        failed += 1
                        err = f"FloodWait {e.seconds}s"
                        await asyncio.sleep(min(e.seconds, 3600) + 5)
                    except Exception as e:
                        failed += 1
                        err = type(e).__name__
                    await asyncio.sleep(max(10, s["delay"]) + random.uniform(0, 5))
            await q("update settings set ch_posts=ch_posts+1,ch_fwd_sent=ch_fwd_sent+?,ch_failed=ch_failed+?,ch_error=? where account_id=?",
                    (sent, failed, err, acc))
            if mode == "once":
                return await ch_stop_db(acc, err)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("ch_loop")
        await q("update settings set ch_running=0,ch_error='Ichki xato' where account_id=?", (acc,))


async def start_account(row):
    try:
        c = TelegramClient(StringSession(fernet.decrypt(row["session"].encode()).decode()), API_ID, API_HASH)
        await c.connect()
        if not await c.is_user_authorized():
            return
        clients[row["id"]] = c
        attach(row["id"], c)
        s = await q("select running, ch_running from settings where account_id=?", (row["id"],), one=True)
        if s and s["running"]:
            start_task(row["id"])
        if s and s["ch_running"]:
            start_ch(row["id"])
    except Exception:
        log.exception("start_account %s", row["id"])


async def sync_groups(acc):
    found = {}
    async for d in clients[acc].iter_dialogs():
        if d.is_group:
            found[d.id] = d.title or ""
    for cid, title in found.items():
        await DB.execute("insert into groups(account_id,chat_id,title) values(?,?,?) on conflict(account_id,chat_id) do update set title=excluded.title", (acc, cid, title))
    old = [r["chat_id"] for r in await q("select chat_id from groups where account_id=?", (acc,))]
    for cid in old:
        if cid not in found:
            await DB.execute("delete from groups where account_id=? and chat_id=?", (acc, cid))
    await DB.commit()


async def clean_task(uid, acc):
    n = 0
    try:
        s = await q("select texts from settings where account_id=?", (acc,), one=True)
        pre = [t[:40] for t in json.loads(s["texts"]) if t]
        for g in await q("select chat_id from groups where account_id=? and enabled=1", (acc,)):
            async for m in clients[acc].iter_messages(g["chat_id"], from_user="me", limit=100):
                if m.raw_text and any(m.raw_text.startswith(p) for p in pre):
                    await m.delete()
                    n += 1
                    await asyncio.sleep(0.6)
        await bot.send_message(uid, f"🧹 Habar tozlash tugadi: {n} ta xabar o'chirildi.")
    except Exception as e:
        await bot.send_message(uid, f"🧹 Habar tozlashda xato: {type(e).__name__} (o'chirilgan: {n})")


# ---------------- Mini App API ----------------
def check_init(init):
    try:
        data = dict(parse_qsl(init, keep_blank_values=True))
        h = data.pop("hash")
        s = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, s.encode(), hashlib.sha256).hexdigest(), h):
            return None
        if time.time() - int(data.get("auth_date", 0)) > 86400:
            return None
        return json.loads(data["user"])
    except Exception:
        return None


def authed(fn):
    async def w(req):
        u = check_init(req.headers.get("X-Init-Data", ""))
        if not u:
            return web.json_response({"error": "Telegram ichida oching"}, status=401)
        await q("insert or ignore into users(id,created) values(?,?)", (u["id"], int(time.time())))
        req["uid"] = u["id"]
        try:
            return await fn(req)
        except ApiError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception:
            log.exception("api")
            return web.json_response({"error": "Ichki xato"}, status=500)
    return w


async def body_of(req):
    try:
        return await req.json()
    except Exception:
        return {}


async def build_state(uid):
    u = dict(await q("select * from users where id=?", (uid,), one=True))
    pro = u["pro_until"] > time.time()
    act = await active_of(uid)
    accs = [dict(a) for a in await q("select id,phone,name,username from accounts where user_id=?", (uid,))]
    out = dict(uid=uid, pro=pro, pro_until=u["pro_until"], limits=PRO if pro else FREE, free=FREE, pro_limits=PRO,
               price=PRICE, contact=CONTACT, bot=BOT_USERNAME, accounts=accs, active=act)
    if act:
        s = dict(await q("select * from settings where account_id=?", (act,), one=True))
        s["texts"] = json.loads(s["texts"])
        s["ch_texts"] = json.loads(s["ch_texts"])
        s["ch_times"] = json.loads(s["ch_times"])
        g = await q("select count(*) t, coalesce(sum(enabled),0) e, coalesce(sum(fwd),0) f from groups where account_id=?", (act,), one=True)
        out.update(settings=s, groups_total=g["t"], groups_enabled=g["e"], groups_fwd=g["f"],
                   running=bool(act in tasks and not tasks[act].done()),
                   ch_running=bool(act in ch_tasks and not ch_tasks[act].done()), connected=act in clients)
    return out


async def state_resp(uid):
    return web.json_response(await build_state(uid))


async def need_acc(uid):
    act = await active_of(uid)
    if not act:
        raise ApiError("Avval akkaunt ulang (Akkauntlar bo'limi)")
    if act not in clients:
        raise ApiError("Akkaunt ulanmagan. Qayta kiring")
    return act


@authed
async def api_state(req):
    return await state_resp(req["uid"])


@authed
async def api_settings(req):
    uid = req["uid"]
    act = await active_of(uid)
    if not act:
        raise ApiError("Avval akkaunt ulang")
    b, lim, upd = await body_of(req), await limits(uid), {}
    if "texts" in b:
        t = [str(x).strip()[:4000] for x in b["texts"] if str(x).strip()]
        if len(t) > lim["texts"]:
            raise ApiError(f"Bu tarifda {lim['texts']} ta xabar mumkin (Pro: {PRO['texts']} ta)")
        upd["texts"] = json.dumps(t, ensure_ascii=False)
    if "delay" in b:
        upd["delay"] = max(10, min(3600, int(b["delay"])))
    if "cycle" in b:
        v = int(b["cycle"])
        if v < lim["min_cycle"]:
            raise ApiError(f"Tsikl kamida {lim['min_cycle']} soniya (Pro: {PRO['min_cycle']} soniya)")
        upd["cycle"] = min(86400, v)
    for k in ("via_bot", "dm_on", "grp_on", "sub_on"):
        if k in b:
            upd[k] = int(bool(b[k]))
    for k, n in (("dm_text", 1000), ("grp_text", 1000)):
        if k in b:
            upd[k] = str(b[k]).strip()[:n]
    # ---- kanal moduli ----
    if "channel" in b:
        upd["channel"] = norm_channel(str(b["channel"]))
    if "ch_texts" in b:
        t = [str(x).strip()[:4000] for x in b["ch_texts"] if str(x).strip()]
        if len(t) > lim["texts"]:
            raise ApiError(f"Bu tarifda {lim['texts']} ta xabar mumkin (Pro: {PRO['texts']} ta)")
        upd["ch_texts"] = json.dumps(t, ensure_ascii=False)
    if "ch_mode" in b:
        if b["ch_mode"] not in ("interval", "daily", "once"):
            raise ApiError("Rejim noto'g'ri")
        upd["ch_mode"] = b["ch_mode"]
    if "ch_every" in b:
        v = int(b["ch_every"])
        if v < lim["min_cycle"]:
            raise ApiError(f"Interval kamida {lim['min_cycle'] // 60} daqiqa (Pro: {PRO['min_cycle'] // 60} daqiqa)")
        upd["ch_every"] = min(86400 * 7, v)
    if "ch_times" in b:
        ts = sorted({str(x) for x in b["ch_times"] if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(x))})[:24]
        upd["ch_times"] = json.dumps(ts)
    if "ch_once" in b:
        upd["ch_once"] = max(0, int(b["ch_once"]))
    if "ch_tz" in b:
        upd["ch_tz"] = max(-720, min(840, int(b["ch_tz"])))
    if "ch_limit" in b:
        upd["ch_limit"] = max(0, min(100000, int(b["ch_limit"])))
    if "ch_fwd" in b:
        upd["ch_fwd"] = int(bool(b["ch_fwd"]))
    if "ch_fwd_n" in b:
        v = max(0, int(b["ch_fwd_n"]))
        if v > lim["groups"]:
            raise ApiError(f"Bu tarifda {lim['groups']} ta guruh mumkin. Pro: {PRO['groups']} ta")
        upd["ch_fwd_n"] = v
    if upd:
        await q("update settings set " + ",".join(f"{k}=?" for k in upd) + " where account_id=?", (*upd.values(), act))
    if "ch_fwd_n" in upd:  # sondan ortiqcha tanlangan guruhlarni kamaytiramiz
        await q("update groups set fwd=0 where account_id=? and chat_id not in "
                "(select chat_id from groups where account_id=? and fwd=1 order by title limit ?)", (act, act, upd["ch_fwd_n"]))
    if any(k.startswith("ch_") or k == "channel" for k in upd) and act in ch_tasks and not ch_tasks[act].done():
        stop_ch(act)
        start_ch(act)  # yangi sozlamalar bilan qayta hisoblaydi
    return await state_resp(uid)


def norm_channel(v):
    v = v.strip()
    mt = re.match(r"(?:https?://)?t\.me/([A-Za-z]\w{3,})/?$", v)
    if mt:
        return "@" + mt.group(1)
    return v[:100]


@authed
async def api_ch_run(req):
    uid = req["uid"]
    if (await body_of(req)).get("on"):
        act = await need_acc(uid)
        s = dict(await q("select * from settings where account_id=?", (act,), one=True))
        if not s["channel"]:
            raise ApiError("Avval kanalni kiriting")
        if not json.loads(s["ch_texts"]):
            raise ApiError("Avval kanal uchun xabar yozing")
        if s["ch_mode"] == "daily" and not json.loads(s["ch_times"]):
            raise ApiError("Kamida bitta soat qo'shing")
        if s["ch_mode"] == "once" and s["ch_once"] <= time.time():
            raise ApiError("Yuborish vaqti o'tib ketgan, yangisini tanlang")
        if s["ch_fwd"]:
            if s["ch_fwd_n"] < 1:
                raise ApiError("Nechta guruhga forward qilishni kiriting")
            if not (await q("select 1 from groups where account_id=? and fwd=1", (act,), one=True)):
                raise ApiError("Forward uchun guruhlarni tanlang")
        await q("update settings set ch_running=1,ch_error='',ch_last=0,"
                "ch_posts=case when ch_limit>0 and ch_posts>=ch_limit then 0 else ch_posts end where account_id=?", (act,))
        stop_ch(act)
        start_ch(act)
    else:
        act = await active_of(uid)
        if act:
            await q("update settings set ch_running=0 where account_id=?", (act,))
            stop_ch(act)
    return await state_resp(uid)


@authed
async def api_run(req):
    uid = req["uid"]
    act = await need_acc(uid)
    if (await body_of(req)).get("on"):
        s = await q("select texts from settings where account_id=?", (act,), one=True)
        if not json.loads(s["texts"]):
            raise ApiError("Avval «Xabar matni» ni kiriting")
        if not (await q("select 1 from groups where account_id=? and enabled=1", (act,), one=True)):
            raise ApiError("Avval «Guruhlarni sozlash» da guruh tanlang")
        await q("update settings set running=1,last_error='' where account_id=?", (act,))
        start_task(act)
    else:
        await q("update settings set running=0 where account_id=?", (act,))
        t = tasks.pop(act, None)
        if t:
            t.cancel()
    return await state_resp(uid)


@authed
async def api_groups(req):
    act = await active_of(req["uid"])
    rows = await q("select chat_id,title,enabled,fwd from groups where account_id=? order by title", (act,))
    return web.json_response([dict(r) for r in rows])


@authed
async def api_groups_sync(req):
    act = await need_acc(req["uid"])
    await sync_groups(act)
    rows = await q("select chat_id,title,enabled,fwd from groups where account_id=? order by title", (act,))
    return web.json_response([dict(r) for r in rows])


async def group_cap(act, lim, kind):
    if kind != "fwd":
        return lim["groups"]
    s = await q("select ch_fwd_n from settings where account_id=?", (act,), one=True)
    n = min(s["ch_fwd_n"], lim["groups"]) if s else 0
    if n < 1:
        raise ApiError("Avval nechta guruhga forward qilishni kiriting")
    return n


@authed
async def api_groups_toggle(req):
    uid = req["uid"]
    act = await active_of(uid)
    b, lim = await body_of(req), await limits(uid)
    kind = "fwd" if b.get("kind") == "fwd" else "enabled"
    if b.get("enabled"):
        cap = await group_cap(act, lim, kind)
        n = (await q(f"select count(*) c from groups where account_id=? and {kind}=1", (act,), one=True))["c"]
        if n >= cap:
            if kind == "fwd":
                raise ApiError(f"Siz {cap} ta guruh belgilagansiz. Ko'proq tanlash uchun sonni oshiring")
            raise ApiError(f"Bu tarifda {cap} ta guruh mumkin. Pro: {PRO['groups']} ta")
    await q(f"update groups set {kind}=? where account_id=? and chat_id=?", (int(bool(b.get("enabled"))), act, int(b["chat_id"])))
    return web.json_response({"ok": True})


@authed
async def api_groups_all(req):
    uid = req["uid"]
    act = await active_of(uid)
    b, lim = await body_of(req), await limits(uid)
    kind = "fwd" if b.get("kind") == "fwd" else "enabled"
    if b.get("enabled"):
        cap = await group_cap(act, lim, kind)
        await q(f"update groups set {kind}=0 where account_id=?", (act,))
        await q(f"update groups set {kind}=1 where account_id=? and chat_id in (select chat_id from groups where account_id=? order by title limit ?)",
                (act, act, cap))
    else:
        await q(f"update groups set {kind}=0 where account_id=?", (act,))
    return web.json_response({"ok": True})


@authed
async def api_test(req):
    act = await need_acc(req["uid"])
    s = await q("select texts from settings where account_id=?", (act,), one=True)
    t = json.loads(s["texts"])
    if not t:
        raise ApiError("Avval xabar matnini kiriting")
    await clients[act].send_message("me", t[0])
    return web.json_response({"msg": "Xabar «Saqlangan xabarlar»ingizga yuborildi"})


@authed
async def api_clean(req):
    act = await need_acc(req["uid"])
    asyncio.create_task(clean_task(req["uid"], act))
    return web.json_response({"msg": "Tozalash boshlandi, tugagach bot yozadi"})


@authed
async def api_join(req):
    act = await need_acc(req["uid"])
    links = set()
    for l in str((await body_of(req)).get("links", "")).split():
        links.update(LINK.findall(l))
    if not links:
        raise ApiError("t.me havolalarini kiriting")
    ok = 0
    for l in list(links)[:10]:
        ok += await join(clients[act], l)
        await asyncio.sleep(3)
    return web.json_response({"msg": f"{ok}/{len(links)} ta joyga obuna bo'lindi"})


@authed
async def api_acc_select(req):
    uid = req["uid"]
    i = int((await body_of(req)).get("id", 0))
    if await q("select 1 from accounts where id=? and user_id=?", (i, uid), one=True):
        await q("update users set active_acc=? where id=?", (i, uid))
    return await state_resp(uid)


@authed
async def api_acc_delete(req):
    uid = req["uid"]
    i = int((await body_of(req)).get("id", 0))
    if await q("select 1 from accounts where id=? and user_id=?", (i, uid), one=True):
        t = tasks.pop(i, None)
        if t:
            t.cancel()
        stop_ch(i)
        c = clients.pop(i, None)
        if c:
            try:
                await c.log_out()
            except Exception:
                await c.disconnect()
        for tb, col in (("settings", "account_id"), ("groups", "account_id"), ("accounts", "id")):
            await q(f"delete from {tb} where {col}=?", (i,))
    return await state_resp(uid)


@authed
async def api_login_start(req):
    uid = req["uid"]
    phone = re.sub(r"[^\d+]", "", str((await body_of(req)).get("phone", "")))
    if not phone.startswith("+"):
        phone = "+" + phone
    if len(phone) < 9:
        raise ApiError("Telefon raqam noto'g'ri (+998...)")
    lim = await limits(uid)
    n = (await q("select count(*) c from accounts where user_id=?", (uid,), one=True))["c"]
    if n >= lim["accounts"]:
        raise ApiError(f"Bu tarifda {lim['accounts']} ta akkaunt mumkin (Pro: {PRO['accounts']} ta)")
    old = pending.pop(uid, None)
    if old:
        await old["c"].disconnect()
    c = TelegramClient(StringSession(), API_ID, API_HASH)
    await c.connect()
    try:
        r = await c.send_code_request(phone)
    except Exception as e:
        await c.disconnect()
        raise ApiError("Telefon raqam noto'g'ri" if isinstance(e, errors.PhoneNumberInvalidError) else f"Kod yuborilmadi: {type(e).__name__}")
    pending[uid] = dict(c=c, phone=phone, hash=r.phone_code_hash)
    return web.json_response({"ok": True})


async def finish_login(uid, p):
    c = p["c"]
    me = await c.get_me()
    sess = fernet.encrypt(c.session.save().encode()).decode()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    cur = await DB.execute("insert into accounts(user_id,phone,session,name,username) values(?,?,?,?,?)",
                           (uid, p["phone"], sess, name, me.username or ""))
    acc = cur.lastrowid
    await DB.execute("insert into settings(account_id) values(?)", (acc,))
    await DB.execute("update users set active_acc=? where id=?", (acc, uid))
    await DB.commit()
    clients[acc] = c
    attach(acc, c)
    pending.pop(uid, None)


@authed
async def api_login_code(req):
    uid = req["uid"]
    p = pending.get(uid)
    if not p:
        raise ApiError("Avval raqam kiriting")
    code = re.sub(r"\D", "", str((await body_of(req)).get("code", "")))
    try:
        await p["c"].sign_in(p["phone"], code, phone_code_hash=p["hash"])
    except errors.SessionPasswordNeededError:
        return web.json_response({"need_password": True})
    except errors.PhoneCodeInvalidError:
        raise ApiError("Kod noto'g'ri")
    except errors.PhoneCodeExpiredError:
        pending.pop(uid, None)
        raise ApiError("Kod eskirdi, qaytadan boshlang")
    await finish_login(uid, p)
    return await state_resp(uid)


@authed
async def api_login_password(req):
    uid = req["uid"]
    p = pending.get(uid)
    if not p:
        raise ApiError("Avval raqam kiriting")
    try:
        await p["c"].sign_in(password=str((await body_of(req)).get("password", "")))
    except errors.PasswordHashInvalidError:
        raise ApiError("Parol noto'g'ri")
    await finish_login(uid, p)
    return await state_resp(uid)


async def page_app(req):
    return web.FileResponse(os.path.join(BASE, "webapp", "index.html"), headers={"Cache-Control": "no-store"})


async def health(req):
    return web.Response(text="ok")


# ---------------- Bot ----------------
PAGES = [("📢 Avtohabar", "autohabar", "Guruhlarga avtomatik habar yuborish — bir marta sozlaysiz, akkauntingiz o'zi yuborib turadi."),
         ("📣 Kanal", "kanal", "Kanalga habar yuborish va xohlasangiz tanlangan guruhlarga forward qilish."),
         ("💬 Xabar matni", "text", "Avtohabar uchun yuboriladigan xabar(lar)."),
         ("⏱ Interval", "interval", "Guruhlar orasidagi kechikish va tsikl oralig'i."),
         ("👥 Guruhlarni sozlash", "groups", "Yuborish uchun guruhlarni tanlang."),
         ("🤖 Autoreply", "autoreply", "DM va guruhdagi murojaatlarga avtomatik javob."),
         ("🧰 Foydali funksiyalar", "tools", "Avtomatik obuna, habar tozlash, test yuborish."),
         ("👤 Profillar", "accounts", "Akkauntlarni ulash va almashtirish."),
         ("👑 Pro tarif", "pro", None)]
TEXT2PAGE = {t: (p, d) for t, p, d in PAGES}


def menu_kb():
    b = [KeyboardButton(text=t) for t, _, _ in PAGES]
    return ReplyKeyboardMarkup(keyboard=[b[i:i + 2] for i in range(0, len(b), 2)], resize_keyboard=True)


def open_kb(page, label="Ochish"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, web_app=WebAppInfo(url=f"{WEBAPP_URL}/app?p={page}"))]])


@dp.message(CommandStart())
async def start(m: Message):
    await q("insert or ignore into users(id,created) values(?,?)", (m.from_user.id, int(time.time())))
    await m.answer("🤖 Jarvis — Telegramdagi amallarni avtomatlashtirish uchun mo'ljallangan yordamchi. "
                   "Masalan, bir vaqtning o'zida bir nechta chatga belgilangan vaqtda xabar yuborish imkonini beradi.\n\n"
                   "Vazifa va akkauntlarni sozlash uchun Kabinetdan foydalaning.", reply_markup=menu_kb())
    await m.answer("⚙️ Boshlash uchun:", reply_markup=open_kb("home", "⚙️ Kabinetni ochish"))


@dp.message(F.text.in_(set(TEXT2PAGE)))
async def menu_press(m: Message):
    page, desc = TEXT2PAGE[m.text]
    if page == "pro":
        u = await q("select pro_until from users where id=?", (m.from_user.id,), one=True)
        st = "✅ Faol" if u and u["pro_until"] > time.time() else "❌ Faol emas"
        await m.answer(f"👑 Pro tarif — {PRICE} so'm / 1 oy\nHolat: {st}\n\n"
                       f"To'lov uchun @{CONTACT} ga yozing, ID'ingiz: {m.from_user.id}", reply_markup=open_kb("pro", "👑 Pro tarifni ko'rish"))
    else:
        await m.answer(f"{m.text}\n{desc}", reply_markup=open_kb(page))


@dp.message(Command("pro"))
async def cmd_pro(m: Message, command: CommandObject):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        uid, days = (int(x) for x in command.args.split())
    except Exception:
        return await m.answer("Format: /pro <user_id> <kun>   (0 = bekor qilish)")
    await q("insert or ignore into users(id,created) values(?,?)", (uid, int(time.time())))
    u = await q("select pro_until from users where id=?", (uid,), one=True)
    until = 0 if days <= 0 else int(max(time.time(), u["pro_until"]) + days * 86400)
    await q("update users set pro_until=? where id=?", (until, uid))
    await m.answer(f"OK: {uid} -> {days} kun")
    try:
        await bot.send_message(uid, "👑 Pro tarifingiz faollashtirildi!" if until else "Pro tarif bekor qilindi.")
    except Exception:
        pass


async def main():
    global DB, BOT_USERNAME
    DB = await aiosqlite.connect(DB_PATH)
    DB.row_factory = aiosqlite.Row
    await DB.executescript(SCHEMA)
    for tb, cols in MIGRATE.items():
        have = {r["name"] for r in await (await DB.execute(f"pragma table_info({tb})")).fetchall()}
        for c, d in cols:
            if c not in have:
                await DB.execute(f"alter table {tb} add column {c} {d}")
    await DB.commit()
    BOT_USERNAME = (await bot.get_me()).username
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Cabinet", web_app=WebAppInfo(url=f"{WEBAPP_URL}/app?p=home")))
    app = web.Application()
    app.add_routes([web.get("/app", page_app), web.get("/health", health), web.get("/", health),
                    web.get("/api/state", api_state), web.post("/api/settings", api_settings), web.post("/api/run", api_run), web.post("/api/ch/run", api_ch_run),
                    web.get("/api/groups", api_groups), web.post("/api/groups/sync", api_groups_sync),
                    web.post("/api/groups/toggle", api_groups_toggle), web.post("/api/groups/all", api_groups_all),
                    web.post("/api/test", api_test), web.post("/api/clean", api_clean), web.post("/api/join", api_join),
                    web.post("/api/account/select", api_acc_select), web.post("/api/account/delete", api_acc_delete),
                    web.post("/api/login/start", api_login_start), web.post("/api/login/code", api_login_code),
                    web.post("/api/login/password", api_login_password)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    for row in await q("select id,session from accounts"):
        await start_account(row)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
