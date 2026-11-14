# -*- coding: utf-8 -*-
"""
Бот со складом аккаунтов + автоотправка в канал по таймеру.
Только стандартная библиотека Python (requirements.txt не нужен).

Переменные окружения (Railway -> Variables):
  BOT_TOKEN  - токен бота (обязательно)
  ADMIN_IDS  - id админов через запятую (необязательно; если пусто,
               первый, кто напишет /admin, станет админом)
  DB_PATH    - путь к базе (по умолчанию bot.db)
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


# ---------------------------------------------------------------- ПАНЕЛЬ
states = {}  # uid -> "add_acc" | "add_ch" | "set_time"
notified_empty = {"v": False}


def stock_count():
    return q("SELECT COUNT(*) FROM stock", (), True)[0][0]


def channels_list():
    return q("SELECT chat_id, title FROM channels", (), True)


def interval():
    try:
        return max(1, int(get_s("interval", DEFAULT_INTERVAL)))
    except Exception:
        return DEFAULT_INTERVAL


def enabled():
    return get_s("enabled", "1") == "1"


def panel_text():
    left = ""
    if enabled():
        last = float(get_s("last_send", time.time()))
        sec = max(0, int(last + interval() * 60 - time.time()))
        left = "\n⏳ Следующая отправка через: <b>%d мин %d сек</b>" % (sec // 60, sec % 60)
    return (
        "🛠 <b>Админ-панель</b>\n\n"
        "📦 На складе: <b>%d</b>\n"
        "📢 Каналов: <b>%d</b>\n"
        "⏱ Интервал: каждые <b>%d мин</b>\n"
        "🔁 Автоотправка: <b>%s</b>%s"
        % (stock_count(), len(channels_list()), interval(),
           "включена ✅" if enabled() else "выключена ⏸", left)
    )


def panel_kb():
    return [
        [{"text": "➕ Добавить аккаунт", "callback_data": "add_acc"},
         {"text": "📢 Добавить канал", "callback_data": "add_ch"}],
        [{"text": "⏱ Время отправки", "callback_data": "set_time"},
         {"text": "⏸ Выкл" if enabled() else "▶️ Вкл", "callback_data": "toggle"}],
        [{"text": "📦 Склад", "callback_data": "stock"},
         {"text": "📋 Каналы", "callback_data": "channels"}],
        [{"text": "🚀 Отправить сейчас", "callback_data": "send_now"},
         {"text": "🔄 Обновить", "callback_data": "back"}],
    ]


def back_kb():
    return [[{"text": "⬅️ В панель", "callback_data": "back"}]]


def show_panel(chat_id, message_id=None):
    if message_id:
        edit(chat_id, message_id, panel_text(), panel_kb())
    else:
        send(chat_id, panel_text(), panel_kb())


# ------------------------------------------------------------ ОТПРАВКА
def notify_admins(text):
    for a in admin_ids():
        send(a, text)


def send_one():
    """Берёт 1 аккаунт со склада и отправляет во все каналы.
    Возвращает (статус, подробности)."""
    chans = channels_list()
    if not chans:
        return "nochannels", "Нет каналов. Добавьте канал."
    with lock:
        row = q("SELECT id, text FROM stock ORDER BY id LIMIT 1", (), True)
        if not row:
            return "empty", "Склад пуст."
        acc_id, acc = row[0]
        q("DELETE FROM stock WHERE id=?", (acc_id,))
    msg = "🎁 <b>Новый аккаунт</b>\n\n<code>%s</code>" % html.escape(acc)
    ok, errs = 0, []
    for chat_id, title in chans:
        r = send(chat_id, msg)
        if r.get("ok"):
            ok += 1
        else:
            errs.append("%s: %s" % (title or chat_id, r.get("description")))
    if ok == 0:
        # никуда не ушло - возвращаем аккаунт на склад
        q("INSERT INTO stock (id, text) VALUES (?, ?)", (acc_id, acc))
        return "fail", "\n".join(errs)
    set_s("last_send", time.time())
    return "ok", "Отправлено в каналов: %d%s" % (
        ok, ("\n⚠️ Ошибки:\n" + "\n".join(errs)) if errs else "")


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
                        # чтобы не спамить, ждём следующий интервал
                        set_s("last_send", time.time())
                        notify_admins("⚠️ Автоотправка не удалась:\n" + html.escape(info))
        except Exception as e:
            print("auto_loop error:", e)
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


# ------------------------------------------------------------- ХЕНДЛЕРЫ
def handle_message(msg):
    chat_id = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()

    if msg["chat"].get("type") != "private":
        return

    if text.startswith("/start"):
        send(chat_id, "👋 Привет! Бот работает.")
        if is_admin(uid):
            show_panel(chat_id)
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
        send(chat_id, "⏱ Выберите интервал или отправьте число минут (например <b>5</b>).", kb)

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
