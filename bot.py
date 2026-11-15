# -*- coding: utf-8 -*-
"""
Бот со складом аккаунтов + автоотправка в каналы + платные клиенты (подписки).
Только стандартная библиотека Python (requirements.txt не нужен).

Переменные окружения (Railway -> Variables):
  BOT_TOKEN  - токен бота (обязательно)
  ADMIN_IDS  - id админов через запятую (необязательно; если пусто,
               первый, кто напишет /admin, станет админом)
  DB_PATH    - путь к базе (по умолчанию bot.db)
  TZ_OFFSET  - часовой пояс в часах относительно UTC (по умолчанию 5)
"""
import os
import json
import time
import html
import sqlite3
import threading
import urllib.request
import urllib.error

TOKEN = os.environ["BOT_TOKEN"]
API = "https://api.telegram.org/bot%s/" % TOKEN
DB_PATH = os.environ.get("DB_PATH", "bot.db")
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", "5"))
DEFAULT_INTERVAL = 5  # минут

# ---------------------------------------------------------------- БАЗА
db = sqlite3.connect(DB_PATH, check_same_thread=False)
lock = threading.RLock()


def q(sql, args=(), fetch=False):
    with lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur.fetchall() if fetch else cur


q("CREATE TABLE IF NOT EXISTS stock (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL)")
q("CREATE TABLE IF NOT EXISTS channels (chat_id INTEGER PRIMARY KEY, title TEXT)")
q("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
q("CREATE TABLE IF NOT EXISTS users (uid INTEGER PRIMARY KEY, username TEXT, name TEXT)")
q("CREATE TABLE IF NOT EXISTS clients (uid INTEGER PRIMARY KEY, expires REAL, "
  "interval INTEGER, last_send REAL DEFAULT 0, status TEXT DEFAULT 'active', got INTEGER DEFAULT 0)")


def get_s(key, default=None):
    r = q("SELECT value FROM settings WHERE key=?", (key,), True)
    return r[0][0] if r else default


def set_s(key, value):
    q("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))


# ------------------------------------------------------------ TELEGRAM
def api(method, **params):
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        API + method, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "description": "HTTP %s" % e.code}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def send(chat_id, text, kb=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    if kb:
        p["reply_markup"] = {"inline_keyboard": kb}
    return api("sendMessage", **p)


def edit(chat_id, message_id, text, kb=None):
    p = {"chat_id": chat_id, "message_id": message_id, "text": text,
         "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb:
        p["reply_markup"] = {"inline_keyboard": kb}
    r = api("editMessageText", **p)
    if not r.get("ok") and "not modified" not in str(r.get("description", "")):
        send(chat_id, text, kb)


# --------------------------------------------------------------- АДМИНЫ
def admin_ids():
    ids = set()
    for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(","):
        if x.lstrip("-").isdigit():
            ids.add(int(x))
    for x in (get_s("admin_ids", "") or "").split(","):
        if x.strip().lstrip("-").isdigit():
            ids.add(int(x))
    return ids


def is_admin(uid):
    return uid in admin_ids()


def claim_admin(uid):
    """Если админов ещё нет - первый становится админом."""
    if not admin_ids():
        set_s("admin_ids", str(uid))
        return True
    return False


# ------------------------------------------------------------- УТИЛИТЫ
states = {}  # uid -> "add_acc" | "add_ch" | "set_time" | "cl_user" | "cl_dur" | "cl_int" | "cl_setint"
tmp = {}     # uid -> временные данные (для добавления клиента)
notified_empty = {"v": False}


def fmt(ts):
    return time.strftime("%d.%m.%Y %H:%M", time.gmtime(ts + TZ_OFFSET * 3600))


def left_str(sec):
    sec = max(0, int(sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    parts = []
    if d:
        parts.append("%d д" % d)
    if h:
        parts.append("%d ч" % h)
    parts.append("%d мин" % m)
    return " ".join(parts)


def save_user(u):
    if not u or not u.get("id"):
        return
    name = ((u.get("first_name") or "") + " " + (u.get("last_name") or "")).strip()
    q("INSERT OR REPLACE INTO users (uid, username, name) VALUES (?, ?, ?)",
      (u["id"], u.get("username") or "", name))


def user_label(uid):
    r = q("SELECT username, name FROM users WHERE uid=?", (uid,), True)
    if r:
        un, name = r[0]
        if un:
            return "@%s" % un
        if name:
            return name
    return str(uid)


def stock_count():
    return q("SELECT COUNT(*) FROM stock", (), True)[0][0]


def take_stock():
    """Забирает 1 аккаунт со склада (атомарно). Возвращает (id, text) или None."""
    with lock:
        row = q("SELECT id, text FROM stock ORDER BY id LIMIT 1", (), True)
        if not row:
            return None
        q("DELETE FROM stock WHERE id=?", (row[0][0],))
        return row[0]


def put_back(acc_id, acc):
    q("INSERT INTO stock (id, text) VALUES (?, ?)", (acc_id, acc))


def channels_list():
    return q("SELECT chat_id, title FROM channels", (), True)


def interval():
    try:
        return max(1, int(get_s("interval", DEFAULT_INTERVAL)))
    except Exception:
        return DEFAULT_INTERVAL


def enabled():
    return get_s("enabled", "1") == "1"


def active_clients_count():
    return q("SELECT COUNT(*) FROM clients WHERE status='active'", (), True)[0][0]


# ---------------------------------------------------------------- ПАНЕЛЬ
def panel_text():
    left = ""
    if enabled():
        last = float(get_s("last_send", time.time()))
        sec = max(0, int(last + interval() * 60 - time.time()))
        left = "\n⏳ Следующая отправка в каналы через: <b>%d мин %d сек</b>" % (sec // 60, sec % 60)
    return (
        "🛠 <b>Админ-панель</b>\n\n"
        "📦 На складе: <b>%d</b>\n"
        "📢 Каналов: <b>%d</b>\n"
        "⏱ Интервал каналов: каждые <b>%d мин</b>\n"
        "🔁 Автоотправка в каналы: <b>%s</b>%s\n"
        "👤 Активных клиентов: <b>%d</b>"
        % (stock_count(), len(channels_list()), interval(),
           "включена ✅" if enabled() else "выключена ⏸", left, active_clients_count())
    )


def panel_kb():
    return [
        [{"text": "➕ Добавить аккаунт", "callback_data": "add_acc"},
         {"text": "📢 Добавить канал", "callback_data": "add_ch"}],
        [{"text": "⏱ Время отправки", "callback_data": "set_time"},
         {"text": "⏸ Выкл" if enabled() else "▶️ Вкл", "callback_data": "toggle"}],
        [{"text": "📦 Склад", "callback_data": "stock"},
         {"text": "📋 Каналы", "callback_data": "channels"}],
        [{"text": "👤 Клиенты", "callback_data": "clients"}],
        [{"text": "🚀 Отправить сейчас", "callback_data": "send_now"},
         {"text": "🔄 Обновить", "callback_data": "back"}],
    ]


def show_panel(chat_id, message_id=None):
    if message_id:
        edit(chat_id, message_id, panel_text(), panel_kb())
    else:
        send(chat_id, panel_text(), panel_kb())


# ------------------------------------------------------- ОТПРАВКА В КАНАЛЫ
def notify_admins(text):
    for a in admin_ids():
        send(a, text)


def send_one():
    """Берёт 1 аккаунт со склада и отправляет во все каналы.
    Возвращает (статус, подробности)."""
    chans = channels_list()
    if not chans:
        return "nochannels", "Нет каналов. Добавьте канал."
    row = take_stock()
    if not row:
        return "empty", "Склад пуст."
    acc_id, acc = row
    msg = "🎁 <b>Новый аккаунт</b>\n\n<code>%s</code>" % html.escape(acc)
    ok, errs = 0, []
    for chat_id, title in chans:
        r = send(chat_id, msg)
        if r.get("ok"):
            ok += 1
        else:
            errs.append("%s: %s" % (title or chat_id, r.get("description")))
    if ok == 0:
        put_back(acc_id, acc)
        return "fail", "\n".join(errs)
    set_s("last_send", time.time())
    return "ok", "Отправлено в каналов: %d%s" % (
        ok, ("\n⚠️ Ошибки:\n" + "\n".join(errs)) if errs else "")


# ------------------------------------------------------ ОТПРАВКА КЛИЕНТАМ
def deliver_to_client(cid, expires):
    """Отправляет 1 аккаунт клиенту. Возвращает 'ok' | 'empty' | 'fail'."""
    row = take_stock()
    if not row:
        return "empty", ""
    acc_id, acc = row
    msg = ("🎁 <b>Ваш новый аккаунт</b>\n\n<code>%s</code>\n\n"
           "⏳ Подписка до: <b>%s</b>" % (html.escape(acc), fmt(expires)))
    r = send(cid, msg)
    if r.get("ok"):
        q("UPDATE clients SET last_send=?, got=got+1 WHERE uid=?", (time.time(), cid))
        return "ok", ""
    put_back(acc_id, acc)
    q("UPDATE clients SET last_send=? WHERE uid=?", (time.time(), cid))
    return "fail", str(r.get("description"))


def clients_tick():
    now = time.time()
    rows = q("SELECT uid, expires, interval, last_send, status FROM clients", (), True)
    for cid, exp, iv, last, st in rows:
        if st != "active":
            continue
        if now >= exp:
            q("UPDATE clients SET status='expired' WHERE uid=?", (cid,))
            send(cid, "⌛️ Ваша подписка закончилась. Для продления напишите администратору.")
            notify_admins("⌛️ Подписка клиента <b>%s</b> закончилась." % html.escape(user_label(cid)))
            continue
        if now - last >= iv * 60:
            st2, info = deliver_to_client(cid, exp)
            if st2 == "empty":
                if not notified_empty["v"]:
                    notified_empty["v"] = True
                    notify_admins("⚠️ Склад пуст! Клиенты не получают аккаунты. Добавьте аккаунты.")
            elif st2 == "ok":
                notified_empty["v"] = False
            elif st2 == "fail":
                notify_admins("⚠️ Не удалось отправить клиенту <b>%s</b>:\n%s"
                              % (html.escape(user_label(cid)), html.escape(info)))


def auto_loop():
    set_s("last_send", get_s("last_send", time.time()))
    while True:
        try:
            if enabled():
                last = float(get_s("last_send", time.time()))
                if time.time() - last >= interval() * 60:
                    st, info = send_one()
                    if st == "empty":
                        if not notified_empty["v"]:
                            notified_empty["v"] = True
                            notify_admins("⚠️ Склад пуст! Добавьте аккаунты.")
                    elif st == "ok":
                        notified_empty["v"] = False
                    elif st in ("fail", "nochannels"):
                        set_s("last_send", time.time())
                        notify_admins("⚠️ Автоотправка не удалась:\n" + html.escape(info))
        except Exception as e:
            print("auto_loop (channels) error:", e)
        try:
            clients_tick()
        except Exception as e:
            print("auto_loop (clients) error:", e)
        time.sleep(3)


# --------------------------------------------------------------- КАНАЛЫ
BOT_ID = {"v": None}


def resolve_channel(msg):
    """Достаёт канал из пересланного поста или из текста (@name / -100... / t.me/name)."""
    fo = msg.get("forward_origin")
    if fo and fo.get("type") == "channel":
        return fo["chat"]["id"]
    fc = msg.get("forward_from_chat")
    if fc:
        return fc["id"]
    t = (msg.get("text") or "").strip()
    if not t:
        return None
    if "t.me/" in t:
        t = "@" + t.split("t.me/")[-1].split("/")[0].split("?")[0]
    if t.lstrip("-").isdigit():
        return int(t)
    if not t.startswith("@"):
        t = "@" + t
    return t


def add_channel(msg):
    ref = resolve_channel(msg)
    if ref is None:
        return "❌ Не понял. Отправьте @username канала или перешлите любой пост из канала."
    r = api("getChat", chat_id=ref)
    if not r.get("ok"):
        return "❌ Канал не найден: %s\nДобавьте бота в канал как администратора и попробуйте снова." % html.escape(str(r.get("description")))
    chat = r["result"]
    m = api("getChatMember", chat_id=chat["id"], user_id=BOT_ID["v"])
    mem = m.get("result", {}) if m.get("ok") else {}
    if mem.get("status") not in ("administrator", "creator"):
        return "❌ Бот не администратор в этом канале. Сделайте бота админом (с правом публикации) и попробуйте снова."
    if mem.get("status") == "administrator" and mem.get("can_post_messages") is False:
        return "❌ У бота нет права «Публикация сообщений». Включите его и попробуйте снова."
    title = chat.get("title") or chat.get("username") or str(chat["id"])
    q("INSERT OR REPLACE INTO channels (chat_id, title) VALUES (?, ?)", (chat["id"], title))
    return "✅ Канал добавлен: <b>%s</b>" % html.escape(title)


# --------------------------------------------------------------- КЛИЕНТЫ
def resolve_user(msg):
    """ID клиента из пересланного сообщения, числа или @username (из тех, кто нажал /start)."""
    fo = msg.get("forward_origin")
    if fo and fo.get("type") == "user":
        return fo["sender_user"]["id"]
    ff = msg.get("forward_from")
    if ff:
        return ff["id"]
    t = (msg.get("text") or "").strip()
    if not t:
        return None
    if t.lstrip("-").isdigit():
        return int(t)
    name = t.lstrip("@").lower()
    r = q("SELECT uid FROM users WHERE lower(username)=?", (name,), True)
    return r[0][0] if r else None


def parse_hours(text):
    """'3' -> 3 дня (72 ч); '5ч' / '5h' -> 5 часов. Возвращает часы или None."""
    t = text.strip().lower().replace(" ", "")
    mult = 24
    if t.endswith("ч") or t.endswith("h"):
        mult = 1
        t = t[:-1]
    if t.isdigit() and int(t) >= 1:
        return int(t) * mult
    return None


def status_icon(st, exp):
    if st == "paused":
        return "⏸"
    if st == "expired" or exp <= time.time():
        return "🔴"
    return "🟢"


def clients_text_kb():
    rows = q("SELECT uid, expires, status FROM clients ORDER BY expires DESC LIMIT 40", (), True)
    kb = [[{"text": "➕ Добавить клиента", "callback_data": "cl_add"}]]
    for cid, exp, st in rows:
        kb.append([{"text": "%s %s · до %s" % (status_icon(st, exp), user_label(cid), fmt(exp)),
                    "callback_data": "cl:%d" % cid}])
    kb.append([{"text": "⬅️ В панель", "callback_data": "back"}])
    text = "👤 <b>Клиенты</b>: %d (активных: %d)" % (len(rows), active_clients_count())
    if not rows:
        text += "\n\nПока нет клиентов."
    return text, kb


def client_text_kb(cid):
    r = q("SELECT expires, interval, status, got FROM clients WHERE uid=?", (cid,), True)
    if not r:
        return "Клиент не найден.", [[{"text": "⬅️ К клиентам", "callback_data": "clients"}]]
    exp, iv, st, got = r[0]
    names = {"active": "активна ✅", "paused": "на паузе ⏸", "expired": "закончилась 🔴"}
    text = ("👤 <b>%s</b> (<code>%d</code>)\n\n"
            "Подписка: <b>%s</b>\n"
            "До: <b>%s</b>\n"
            "Осталось: <b>%s</b>\n"
            "Интервал: каждые <b>%d мин</b>\n"
            "Получено аккаунтов: <b>%d</b>"
            % (html.escape(user_label(cid)), cid, names.get(st, st), fmt(exp),
               left_str(exp - time.time()) if exp > time.time() else "0", iv, got))
    kb = [
        [{"text": "+1 час", "callback_data": "ext:%d:1" % cid},
         {"text": "+1 день", "callback_data": "ext:%d:24" % cid},
         {"text": "+7 дней", "callback_data": "ext:%d:168" % cid}],
        [{"text": "⏱ Интервал", "callback_data": "cint:%d" % cid},
         {"text": "▶️ Продолжить" if st == "paused" else "⏸ Пауза", "callback_data": "ctog:%d" % cid}],
        [{"text": "🗑 Удалить", "callback_data": "cdel:%d" % cid}],
        [{"text": "⬅️ К клиентам", "callback_data": "clients"}],
    ]
    return text, kb


def interval_kb():
    return [[{"text": "%d мин" % m, "callback_data": "ci:%d" % m} for m in (10, 30, 60)],
            [{"text": "%d мин" % m, "callback_data": "ci:%d" % m} for m in (120, 180, 360)],
            [{"text": "⬅️ В панель", "callback_data": "back"}]]


def finish_client(admin_uid, chat_id, minutes):
    d = tmp.get(admin_uid) or {}
    cid, hours = d.get("cid"), d.get("hours")
    states.pop(admin_uid, None)
    tmp.pop(admin_uid, None)
    if not cid or not hours:
        send(chat_id, "❌ Что-то пошло не так, начните заново.")
        show_panel(chat_id)
        return
    exp = time.time() + hours * 3600
    q("INSERT OR REPLACE INTO clients (uid, expires, interval, last_send, status, got) "
      "VALUES (?, ?, ?, 0, 'active', COALESCE((SELECT got FROM clients WHERE uid=?), 0))",
      (cid, exp, minutes, cid))
    r = send(cid, "✅ <b>Подписка активирована!</b>\n\nДо: <b>%s</b>\n"
                  "Аккаунты будут приходить сюда каждые <b>%d мин</b>." % (fmt(exp), minutes))
    warn = "" if r.get("ok") else "\n⚠️ Клиенту не удалось отправить сообщение: %s" % html.escape(str(r.get("description")))
    send(chat_id, "✅ Клиент <b>%s</b> добавлен.\nДо: <b>%s</b>, интервал: <b>%d мин</b>.%s"
         % (html.escape(user_label(cid)), fmt(exp), minutes, warn))
    text, kb = clients_text_kb()
    send(chat_id, text, kb)


SUPPORT_USER = "@CPM_SSM"
SUPPORT_URL = "https://t.me/CPM_SSM"


def buy_kb(text="💳 Купить доступ"):
    return [[{"text": text, "url": SUPPORT_URL}]]


def my_status(chat_id, uid):
    r = q("SELECT expires, interval, status FROM clients WHERE uid=?", (uid,), True)
    if not r:
        send(chat_id,
             "👋 <b>Добро пожаловать!</b>\n\n"
             "🎁 Свежие аккаунты приходят прямо в этот чат автоматически.\n\n"
             "💳 Для покупки доступа пишите: %s" % SUPPORT_USER,
             buy_kb())
        return
    exp, iv, st = r[0]
    if st == "active" and exp > time.time():
        send(chat_id, "👋 Привет!\n\n✅ Подписка активна до <b>%s</b>\n⏳ Осталось: <b>%s</b>\n"
                      "🔁 Аккаунты приходят каждые <b>%d мин</b>." % (fmt(exp), left_str(exp - time.time()), iv),
             buy_kb("🔄 Продлить"))
    elif st == "paused":
        send(chat_id, "⏸ Ваша подписка сейчас на паузе. Напишите %s" % SUPPORT_USER,
             buy_kb("💬 Написать"))
    else:
        send(chat_id, "⌛️ Ваша подписка закончилась.\n\n💳 Для продления пишите: %s" % SUPPORT_USER,
             buy_kb("🔄 Продлить"))


# ------------------------------------------------------------- ХЕНДЛЕРЫ
def handle_message(msg):
    chat_id = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()

    if msg["chat"].get("type") != "private":
        return

    save_user(msg.get("from"))

    if text.startswith("/start"):
        if is_admin(uid):
            send(chat_id, "👋 Привет! Бот работает.")
            show_panel(chat_id)
        else:
            my_status(chat_id, uid)
        return

    if text.startswith("/my") or text.startswith("/status"):
        my_status(chat_id, uid)
        return

    if text.startswith("/admin"):
        if claim_admin(uid):
            send(chat_id, "✅ Вы теперь админ бота.")
        if is_admin(uid):
            show_panel(chat_id)
        return

    if not is_admin(uid):
        return

    if text.startswith("/cancel"):
        states.pop(uid, None)
        tmp.pop(uid, None)
        show_panel(chat_id)
        return

    st = states.get(uid)

    if st == "add_acc":
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if not lines:
            send(chat_id, "❌ Отправьте текст аккаунта (каждый аккаунт с новой строки).")
            return
        with lock:
            for l in lines:
                q("INSERT INTO stock (text) VALUES (?)", (l,))
        states.pop(uid, None)
        notified_empty["v"] = False
        send(chat_id, "✅ Добавлено: <b>%d</b>\n📦 Всего на складе: <b>%d</b>" % (len(lines), stock_count()))
        show_panel(chat_id)
        return

    if st == "add_ch":
        res = add_channel(msg)
        if res.startswith("✅"):
            states.pop(uid, None)
            send(chat_id, res)
            show_panel(chat_id)
        else:
            send(chat_id, res + "\n\nИли /cancel для отмены.")
        return

    if st == "set_time":
        if text.isdigit() and int(text) >= 1:
            set_s("interval", int(text))
            set_s("last_send", time.time())
            states.pop(uid, None)
            send(chat_id, "✅ Интервал: каждые <b>%d мин</b>" % int(text))
            show_panel(chat_id)
        else:
            send(chat_id, "❌ Отправьте число минут, например <b>5</b>.")
        return

    # ---- добавление клиента: шаг 1 - кто
    if st == "cl_user":
        cid = resolve_user(msg)
        if cid is None:
            send(chat_id, "❌ Не нашёл такого пользователя. Клиент должен сначала нажать /start в боте.\n"
                          "Отправьте ID, @username или перешлите его сообщение.\n\n/cancel — отмена")
            return
        if not q("SELECT 1 FROM users WHERE uid=?", (cid,), True):
            send(chat_id, "❌ Этот пользователь ещё не нажимал /start в боте. Пусть сначала откроет бота "
                          "и нажмёт /start, потом добавьте его.\n\n/cancel — отмена")
            return
        tmp[uid] = {"cid": cid}
        states[uid] = "cl_dur"
        kb = [[{"text": "1 день", "callback_data": "cd:24"},
               {"text": "3 дня", "callback_data": "cd:72"}],
              [{"text": "7 дней", "callback_data": "cd:168"},
               {"text": "30 дней", "callback_data": "cd:720"}],
              [{"text": "⬅️ В панель", "callback_data": "back"}]]
        send(chat_id, "👤 Клиент: <b>%s</b>\n\n⏳ На какой срок подписка? Выберите или отправьте число дней "
                      "(например <b>2</b>) либо часов (например <b>5ч</b>)." % html.escape(user_label(cid)), kb)
        return

    # ---- шаг 2 - срок
    if st == "cl_dur":
        h = parse_hours(text)
        if not h:
            send(chat_id, "❌ Отправьте число дней (<b>2</b>) или часов (<b>5ч</b>).")
            return
        tmp.setdefault(uid, {})["hours"] = h
        states[uid] = "cl_int"
        send(chat_id, "⏱ Как часто присылать аккаунт? Выберите или отправьте число минут (например <b>60</b>).",
             interval_kb())
        return

    # ---- шаг 3 - интервал
    if st == "cl_int":
        if text.isdigit() and int(text) >= 1:
            finish_client(uid, chat_id, int(text))
        else:
            send(chat_id, "❌ Отправьте число минут, например <b>60</b>.")
        return

    # ---- смена интервала у существующего клиента
    if st == "cl_setint":
        if text.isdigit() and int(text) >= 1:
            cid = (tmp.get(uid) or {}).get("cid")
            states.pop(uid, None)
            tmp.pop(uid, None)
            if cid:
                q("UPDATE clients SET interval=? WHERE uid=?", (int(text), cid))
                t, kb = client_text_kb(cid)
                send(chat_id, t, kb)
        else:
            send(chat_id, "❌ Отправьте число минут, например <b>60</b>.")
        return


def handle_callback(cb):
    uid = cb["from"]["id"]
    data = cb.get("data", "")
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    mid = msg.get("message_id")
    api("answerCallbackQuery", callback_query_id=cb["id"])
    if not is_admin(uid) or not chat_id:
        return

    if data == "back":
        states.pop(uid, None)
        tmp.pop(uid, None)
        show_panel(chat_id, mid)

    elif data == "add_acc":
        states[uid] = "add_acc"
        send(chat_id, "➕ Отправьте аккаунт(ы).\nКаждый аккаунт — с новой строки.\n\n/cancel — отмена")

    elif data == "add_ch":
        states[uid] = "add_ch"
        send(chat_id,
             "📢 Сначала добавьте бота в канал как <b>администратора</b> (с правом публикации).\n\n"
             "Потом отправьте <b>@username</b> канала или <b>перешлите любой пост</b> из канала.\n\n/cancel — отмена")

    elif data == "set_time":
        states[uid] = "set_time"
        kb = [[{"text": "%d мин" % m, "callback_data": "t:%d" % m} for m in (1, 5, 10)],
              [{"text": "%d мин" % m, "callback_data": "t:%d" % m} for m in (30, 60, 120)],
              [{"text": "⬅️ В панель", "callback_data": "back"}]]
        send(chat_id, "⏱ Выберите интервал для каналов или отправьте число минут (например <b>5</b>).", kb)

    elif data.startswith("t:"):
        m = int(data[2:])
        set_s("interval", m)
        set_s("last_send", time.time())
        states.pop(uid, None)
        send(chat_id, "✅ Интервал: каждые <b>%d мин</b>" % m)
        show_panel(chat_id)

    elif data == "toggle":
        set_s("enabled", "0" if enabled() else "1")
        if enabled():
            set_s("last_send", time.time())
        show_panel(chat_id, mid)

    elif data == "stock":
        rows = q("SELECT id, text FROM stock ORDER BY id LIMIT 10", (), True)
        body = "\n".join("%d. <code>%s</code>" % (i + 1, html.escape(t[:60])) for i, (_, t) in enumerate(rows)) or "пусто"
        kb = [[{"text": "🗑 Очистить склад", "callback_data": "clear_stock"}],
              [{"text": "⬅️ В панель", "callback_data": "back"}]]
        edit(chat_id, mid, "📦 <b>Склад: %d</b>\n\n%s" % (stock_count(), body), kb)

    elif data == "clear_stock":
        kb = [[{"text": "✅ Да, очистить", "callback_data": "clear_yes"},
               {"text": "❌ Нет", "callback_data": "stock"}]]
        edit(chat_id, mid, "Точно очистить весь склад?", kb)

    elif data == "clear_yes":
        q("DELETE FROM stock")
        show_panel(chat_id, mid)

    elif data == "channels":
        rows = channels_list()
        kb = [[{"text": "🗑 %s" % (t or c), "callback_data": "delch:%d" % c}] for c, t in rows]
        kb.append([{"text": "⬅️ В панель", "callback_data": "back"}])
        edit(chat_id, mid, "📋 <b>Каналы</b> (нажмите, чтобы удалить):" if rows else "📋 Каналов нет.", kb)

    elif data.startswith("delch:"):
        q("DELETE FROM channels WHERE chat_id=?", (int(data[6:]),))
        cb["data"] = "channels"
        handle_callback(cb)

    elif data == "send_now":
        st, info = send_one()
        send(chat_id, ("✅ " if st == "ok" else "⚠️ ") + html.escape(info))
        show_panel(chat_id)

    # -------------------------------------------------------- КЛИЕНТЫ
    elif data == "clients":
        states.pop(uid, None)
        tmp.pop(uid, None)
        text, kb = clients_text_kb()
        edit(chat_id, mid, text, kb)

    elif data == "cl_add":
        states[uid] = "cl_user"
        send(chat_id, "👤 Отправьте <b>ID</b> или <b>@username</b> клиента, либо <b>перешлите его сообщение</b>.\n\n"
                      "⚠️ Клиент должен сначала открыть бота и нажать /start.\n\n/cancel — отмена")

    elif data.startswith("cd:"):
        if states.get(uid) == "cl_dur":
            tmp.setdefault(uid, {})["hours"] = int(data[3:])
            states[uid] = "cl_int"
            send(chat_id, "⏱ Как часто присылать аккаунт? Выберите или отправьте число минут (например <b>60</b>).",
                 interval_kb())

    elif data.startswith("ci:"):
        m = int(data[3:])
        if states.get(uid) == "cl_int":
            finish_client(uid, chat_id, m)
        elif states.get(uid) == "cl_setint":
            cid = (tmp.get(uid) or {}).get("cid")
            states.pop(uid, None)
            tmp.pop(uid, None)
            if cid:
                q("UPDATE clients SET interval=? WHERE uid=?", (m, cid))
                t, kb = client_text_kb(cid)
                send(chat_id, t, kb)

    elif data.startswith("cl:"):
        t, kb = client_text_kb(int(data[3:]))
        edit(chat_id, mid, t, kb)

    elif data.startswith("ext:"):
        _, cid, h = data.split(":")
        cid, h = int(cid), int(h)
        r = q("SELECT expires, status FROM clients WHERE uid=?", (cid,), True)
        if r:
            exp, st = r[0]
            new_exp = max(exp, time.time()) + h * 3600
            new_st = "paused" if st == "paused" else "active"
            q("UPDATE clients SET expires=?, status=? WHERE uid=?", (new_exp, new_st, cid))
            send(cid, "✅ Ваша подписка продлена до <b>%s</b>." % fmt(new_exp))
        t, kb = client_text_kb(cid)
        edit(chat_id, mid, t, kb)

    elif data.startswith("cint:"):
        cid = int(data[5:])
        states[uid] = "cl_setint"
        tmp[uid] = {"cid": cid}
        send(chat_id, "⏱ Новый интервал для клиента. Выберите или отправьте число минут.", interval_kb())

    elif data.startswith("ctog:"):
        cid = int(data[5:])
        r = q("SELECT expires, status FROM clients WHERE uid=?", (cid,), True)
        if r:
            exp, st = r[0]
            if st == "active":
                q("UPDATE clients SET status='paused' WHERE uid=?", (cid,))
            else:
                q("UPDATE clients SET status=? WHERE uid=?", ("active" if exp > time.time() else "expired", cid))
        t, kb = client_text_kb(cid)
        edit(chat_id, mid, t, kb)

    elif data.startswith("cdel:"):
        cid = int(data[5:])
        kb = [[{"text": "✅ Да, удалить", "callback_data": "cdely:%d" % cid},
               {"text": "❌ Нет", "callback_data": "cl:%d" % cid}]]
        edit(chat_id, mid, "Удалить клиента <b>%s</b>?" % html.escape(user_label(cid)), kb)

    elif data.startswith("cdely:"):
        q("DELETE FROM clients WHERE uid=?", (int(data[6:]),))
        text, kb = clients_text_kb()
        edit(chat_id, mid, text, kb)


# ------------------------------------------------------------------ MAIN
def main():
    me = api("getMe")
    if not me.get("ok"):
        raise SystemExit("Неверный BOT_TOKEN: %s" % me.get("description"))
    BOT_ID["v"] = me["result"]["id"]
    print("Бот запущен: @%s" % me["result"].get("username"))
    threading.Thread(target=auto_loop, daemon=True).start()

    offset = 0
    while True:
        r = api("getUpdates", offset=offset, timeout=30,
                allowed_updates=["message", "callback_query"])
        if not r.get("ok"):
            time.sleep(3)
            continue
        for u in r["result"]:
            offset = u["update_id"] + 1
            try:
                if "message" in u:
                    handle_message(u["message"])
                elif "callback_query" in u:
                    handle_callback(u["callback_query"])
            except Exception as e:
                print("handler error:", e)


if __name__ == "__main__":
    main()
