import sqlite3
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
import asyncio
from dotenv import load_dotenv
import os
import logging # أضفنا logging لتسجيل رسائل التنبيهات

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))
# 🟢 متغير جديد: معرف المجموعة/القناة لإرسال التنبيهات
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID")) 

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# --- Database Setup ---
conn = sqlite3.connect("genshin_bot.db")
cursor = conn.cursor()

# --- Database Tables Setup ---

cursor.execute("""
CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY
)
""")

# Create the 'content' table with separate time columns
cursor.execute("""
CREATE TABLE IF NOT EXISTS content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section TEXT,
    title TEXT,
    name TEXT,
    end_time_asia TEXT,
    end_time_europe TEXT,
    end_time_america TEXT,
    description TEXT,
    image_file_id TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS server_offsets (
    server TEXT PRIMARY KEY,
    offset_hours INTEGER
)
""")

# 🟢 جدول جديد لتتبع التنبيهات المُرسلة
cursor.execute("""
CREATE TABLE IF NOT EXISTS sent_alerts (
    content_id INTEGER,
    server TEXT,
    alert_type TEXT, -- '1_hour_remaining' or 'expired'
    PRIMARY KEY (content_id, server, alert_type)
)
""")

conn.commit()

# Populate default admins and server offsets
cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (OWNER_ID,))
cursor.execute("INSERT OR IGNORE INTO server_offsets (server, offset_hours) VALUES (?, ?)", ('asia', 8)) # UTC+8
cursor.execute("INSERT OR IGNORE INTO server_offsets (server, offset_hours) VALUES (?, ?)", ('europe', 1)) # UTC+1
cursor.execute("INSERT OR IGNORE INTO server_offsets (server, offset_hours) VALUES (?, ?)", ('america', -5)) # UTC-5
conn.commit()

# --- Utility Functions ---

def is_admin(user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None

def time_left_str(end_time: datetime, now: datetime) -> str:
    diff = end_time - now
    total_seconds = int(diff.total_seconds())
    if total_seconds <= 0:
        return "انتهى."
    
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    
    return f"{days} يوم و {hours} ساعة و {minutes} دقيقة و {seconds} ثانية"

def parse_end_datetime(date_time_str: str, offset_hours: int = 0):
    try:
        # Create a timezone with the specified offset
        tz = timezone(timedelta(hours=offset_hours))
        # Parse the string and attach the timezone
        end_time = datetime.strptime(date_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        # Convert it to UTC for consistent comparison
        return end_time.astimezone(timezone.utc)
    except:
        return None

# --- FSM States ---

class UpdateContent(StatesGroup):
    waiting_for_title_and_name = State()
    waiting_for_title = State()
    waiting_for_event_text = State()
    waiting_for_asia_time = State()
    waiting_for_europe_time = State()
    waiting_for_america_time = State()
    waiting_for_photo = State()

# --- Alert System Core ---

async def check_and_send_alerts():
    """المهمة الخلفية لفحص مواعيد الانتهاء وإرسال التنبيهات."""
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            
            # 1. جلب كل المحتوى القابل للتنبيه (باستثناء الأحداث التي تُحذف آلياً)
            cursor.execute("SELECT id, section, title, name, end_time_asia, end_time_europe, end_time_america, image_file_id FROM content WHERE section != 'events'")
            contents = cursor.fetchall()

            for content_id, section, title, name, asia_time_str, europe_time_str, america_time_str, file_id in contents:
                
                # إنشاء قاموس لسهولة الوصول إلى بيانات السيرفر
                server_data = {
                    'asia': asia_time_str,
                    'europe': europe_time_str,
                    'america': america_time_str
                }
                
                server_arabic_names = {
                    'asia': 'آسيا',
                    'europe': 'أوروبا',
                    'america': 'أمريكا'
                }

                arabic_section_titles = {
                    'abyss': 'الأبِس',
                    'stygian': 'ستيجيان',
                    'theater': 'المسرح',
                    'banner': 'البنر'
                }
                
                # تحديد اسم المحتوى في الرسالة
                content_name = title or arabic_section_titles.get(section, section)
                
                for server, end_time_str in server_data.items():
                    if not end_time_str:
                        continue
                    
                    end_time_utc = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    time_diff = end_time_utc - now_utc
                    
                    # 🟢 التنبيه الأول: ساعة واحدة متبقية
                    if timedelta(hours=0) < time_diff <= timedelta(hours=1, minutes=1):
                        alert_type = '1_hour_remaining'
                        cursor.execute("SELECT 1 FROM sent_alerts WHERE content_id=? AND server=? AND alert_type=?", (content_id, server, alert_type))
                        if cursor.fetchone() is None:
                            message_text = (
                                f"🔔 **تنبيه الإنتهاء (ساعة واحدة):**\n"
                                f"**{content_name}** - سيرفر **{server_arabic_names[server]}**\n"
                                f"⏳ الوقت المتبقي: حوالي ساعة واحدة."
                            )
                            await bot.send_message(TARGET_CHAT_ID, message_text, parse_mode="Markdown")
                            cursor.execute("INSERT INTO sent_alerts (content_id, server, alert_type) VALUES (?, ?, ?)", (content_id, server, alert_type))
                            conn.commit()
                            logging.info(f"Alert sent: {content_name} - {server} ({alert_type})")

                    # 🟢 التنبيه الثاني: المحتوى انتهى
                    elif time_diff <= timedelta(minutes=0):
                        alert_type = 'expired'
                        cursor.execute("SELECT 1 FROM sent_alerts WHERE content_id=? AND server=? AND alert_type=?", (content_id, server, alert_type))
                        if cursor.fetchone() is None:
                            message_text = (
                                f"❌ **انتهى المحتوى:**\n"
                                f"**{content_name}** - سيرفر **{server_arabic_names[server]}**\n"
                                f"تم إزالة المحتوى من اللعبة."
                            )
                            await bot.send_message(TARGET_CHAT_ID, message_text, parse_mode="Markdown")
                            cursor.execute("INSERT INTO sent_alerts (content_id, server, alert_type) VALUES (?, ?, ?)", (content_id, server, alert_type))
                            conn.commit()
                            logging.info(f"Alert sent: {content_name} - {server} ({alert_type})")
                            
                            # اختيارياً: حذف المحتوى بالكامل بعد انتهاءه من جميع السيرفرات
                            if all(datetime.strptime(server_data[s], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) <= now_utc for s in server_data if server_data[s]):
                                cursor.execute("DELETE FROM content WHERE id = ?", (content_id,))
                                conn.commit()
                                logging.info(f"Content deleted: {content_name} (ID: {content_id})")

        except Exception as e:
            logging.error(f"Error in alert system task: {e}")
            
        # الانتظار قبل الفحص التالي
        await asyncio.sleep(30) # الفحص كل 30 ثانية

# --- Command Handlers ---

# Unified handler for setting commands
@dp.message(Command(
    'setbanner',
    'setbanner_ar',
    'setabyss',
    'setabyss_ar',
    'setstygian',
    'setstygian_ar',
    'settheater',
    'settheater_ar'
))
async def cmd_start_update_single_title_only(message: types.Message, state: FSMContext, command: Command):
    if not is_admin(message.from_user.id):
        await message.reply("🚫 ليس لديك صلاحية تعديل المحتوى.")
        return

    command_text = command.command
    await state.update_data(section=command_text.replace("set", "").replace("_ar", ""))

    if 'banner' in command_text:
        await message.reply(
            "أرسل البيانات بهذا الشكل:\n"
            "عنوان المحتوى ; اسم الحدث\n"
            "مثال:\n"
            "بنرات 5.8 النصف الاول ; سيتلالي + اينيفيا\n"
        )
        await state.set_state(UpdateContent.waiting_for_title_and_name)
    else:
        await message.reply(
            "أرسل البيانات بهذا الشكل:\n"
            "عنوان المحتوى\n"
            "مثال:\n"
            "أبس 5.8\n"
        )
        await state.set_state(UpdateContent.waiting_for_title)

@dp.message(Command('setevents', 'setevents_ar'))
async def cmd_start_update_events(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.reply("🚫 ليس لديك صلاحية تعديل المحتوى.")
        return
    await state.update_data(section='events')
    await message.reply(
        "أرسل البيانات بهذا الشكل لإضافة حدث جديد:\n"
        "اسم الحدث ; YYYY-MM-DD HH:MM:SS\n"
        "مثال:\n"
        "حدث جديد ; 2025-10-25 15:30:00\n"
    )
    await state.set_state(UpdateContent.waiting_for_event_text)

@dp.message(UpdateContent.waiting_for_title, F.content_type == types.ContentType.TEXT)
async def process_title(message: types.Message, state: FSMContext):
    title = message.text.strip()
    if not title:
        await message.reply("❌ العنوان لا يمكن أن يكون فارغًا. يرجى إرسال: عنوان المحتوى")
        return

    await state.update_data(title=title, name="")
    await message.reply("يرجى إدخال وقت انتهاء سيرفر آسيا: YYYY-MM-DD HH:MM:SS")
    await state.set_state(UpdateContent.waiting_for_asia_time)

@dp.message(UpdateContent.waiting_for_title_and_name, F.content_type == types.ContentType.TEXT)
async def process_title_and_name(message: types.Message, state: FSMContext):
    text = message.text
    parts = [p.strip() for p in text.split(";", 1)]

    if len(parts) < 2:
        await message.reply("❌ الخطأ في الصيغة.\nالصيغة:\nعنوان المحتوى ; اسم الحدث")
        return

    title = parts[0]
    name = parts[1]

    await state.update_data(title=title, name=name)
    await message.reply("يرجى إدخال وقت انتهاء سيرفر آسيا: YYYY-MM-DD HH:MM:SS")
    await state.set_state(UpdateContent.waiting_for_asia_time)

@dp.message(UpdateContent.waiting_for_event_text, F.content_type == types.ContentType.TEXT)
async def process_event_text(message: types.Message, state: FSMContext):
    text = message.text
    parts = [p.strip() for p in text.split(";", 1)]

    if len(parts) < 2:
        await message.reply("❌ الخطأ في الصيغة.\nالصيغة:\nاسم الحدث ; YYYY-MM-DD HH:MM:SS")
        return

    name = parts[0]
    # Use parse_end_datetime with Asia's offset (8)
    end_time_utc = parse_end_datetime(parts[1], offset_hours=8)
    if not end_time_utc:
        await message.reply("❌ تنسيق التاريخ والوقت غير صحيح. استخدم الصيغة `YYYY-MM-DD HH:MM:SS`.")
        return

    end_time_str = end_time_utc.strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO content (section, name, end_time_asia)
        VALUES (?, ?, ?)
    """, ('events', name, end_time_str))
    conn.commit()
    await message.reply(f"✅ تم إضافة حدث جديد بنجاح.")
    await state.clear()

@dp.message(UpdateContent.waiting_for_asia_time, F.content_type == types.ContentType.TEXT)
async def process_asia_time(message: types.Message, state: FSMContext):
    end_time_str = message.text
    # We will parse this time later in process_photo
    await state.update_data(end_time_asia=end_time_str)
    await message.reply("يرجى إدخال وقت انتهاء سيرفر أوروبا: YYYY-MM-DD HH:MM:SS")
    await state.set_state(UpdateContent.waiting_for_europe_time)

@dp.message(UpdateContent.waiting_for_europe_time, F.content_type == types.ContentType.TEXT)
async def process_europe_time(message: types.Message, state: FSMContext):
    end_time_str = message.text
    await state.update_data(end_time_europe=end_time_str)
    await message.reply("يرجى إدخال وقت انتهاء سيرفر أمريكا: YYYY-MM-DD HH:MM:SS")
    await state.set_state(UpdateContent.waiting_for_america_time)

@dp.message(UpdateContent.waiting_for_america_time, F.content_type == types.ContentType.TEXT)
async def process_america_time(message: types.Message, state: FSMContext):
    end_time_str = message.text
    await state.update_data(end_time_america=end_time_str)
    await message.reply("الآن أرسل صورة مرفقة للحدث.")
    await state.set_state(UpdateContent.waiting_for_photo)

@dp.message(UpdateContent.waiting_for_photo, F.content_type == types.ContentType.PHOTO)
async def process_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    section = data['section']
    title = data.get('title', '')
    name = data.get('name', '')

    # Get server offsets from the database
    cursor.execute("SELECT offset_hours FROM server_offsets WHERE server = 'asia'")
    asia_offset = cursor.fetchone()[0]
    cursor.execute("SELECT offset_hours FROM server_offsets WHERE server = 'europe'")
    europe_offset = cursor.fetchone()[0]
    cursor.execute("SELECT offset_hours FROM server_offsets WHERE server = 'america'")
    america_offset = cursor.fetchone()[0]

    # Parse and convert to UTC based on the offsets
    end_time_asia_utc = parse_end_datetime(data['end_time_asia'], offset_hours=asia_offset)
    end_time_europe_utc = parse_end_datetime(data['end_time_europe'], offset_hours=europe_offset)
    end_time_america_utc = parse_end_datetime(data['end_time_america'], offset_hours=america_offset)

    if not end_time_asia_utc or not end_time_europe_utc or not end_time_america_utc:
        await message.reply("❌ تنسيق التاريخ والوقت غير صحيح في أحد السيرفرات. يرجى البدء من جديد.")
        await state.clear()
        return

    # Convert back to string for database storage (in UTC)
    end_time_asia = end_time_asia_utc.strftime("%Y-%m-%d %H:%M:%S")
    end_time_europe = end_time_europe_utc.strftime("%Y-%m-%d %H:%M:%S")
    end_time_america = end_time_america_utc.strftime("%Y-%m-%d %H:%M:%S")

    photo = message.photo[-1]
    file_id = photo.file_id

    cursor.execute("SELECT id FROM content WHERE section = ?", (section,))
    existing_row = cursor.fetchone()

    if existing_row:
        content_id = existing_row[0]
        cursor.execute("""
            UPDATE content SET
                title=?,
                name=?,
                end_time_asia=?,
                end_time_europe=?,
                end_time_america=?,
                image_file_id=?
            WHERE section=?
        """, (title, name, end_time_asia, end_time_europe, end_time_america, file_id, section))
        # 🟢 عند التحديث، احذف سجلات التنبيهات القديمة ليتم إرسالها من جديد
        cursor.execute("DELETE FROM sent_alerts WHERE content_id = ?", (content_id,))
    else:
        cursor.execute("""
            INSERT INTO content (section, title, name, end_time_asia, end_time_europe, end_time_america, image_file_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (section, title, name, end_time_asia, end_time_europe, end_time_america, file_id))

    conn.commit()

    await message.reply(f"✅ تم تحديث محتوى {section} بنجاح. **(سيتم تفعيل التنبيهات تلقائياً)**")
    await state.clear()

@dp.message(UpdateContent.waiting_for_photo, F.content_type != types.ContentType.PHOTO)
async def process_not_photo(message: types.Message):
    await message.reply("❌ الرجاء إرسال صورة فقط.")

# Unified handler for showing content
@dp.message(Command('the_banner', 'banner', 'abyss', 'stygian', 'theater', 'spiral_abyss')) # Added 'the_banner' here
@dp.message(F.text.lower().in_(['التبنر', 'البنر', 'الابيس', 'ستيجيان', 'المسرح'])) # Added 'التبنر' here for the new command
async def cmd_show_content_single(message: types.Message, command: Command = None):
    section_map = {
        'the_banner': 'banner', # Changed from 'banner' to 'the_banner'
        'banner': 'banner', 'البنر': 'banner',
        'abyss': 'abyss', 'الابيس': 'abyss',
        'stygian': 'stygian', 'ستيجيان': 'stygian',
        'theater': 'theater', 'المسرح': 'theater',
        'spiral_abyss': 'abyss'
    }

    if command:
        section_key = section_map.get(command.command)
    else:
        # Map user's text input to the correct section key
        text_to_section_map = {
            'التبنر': 'banner', # New mapping for 'التبنر'
            'البنر': 'banner',
            'الابيس': 'abyss',
            'ستيجيان': 'stygian',
            'المسرح': 'theater',
        }
        section_key = text_to_section_map.get(message.text.lower())

    if not section_key:
        return

    cursor.execute("SELECT title, name, end_time_asia, end_time_europe, end_time_america, image_file_id FROM content WHERE section=?", (section_key,))
    row = cursor.fetchone()

    if not row:
        await message.reply(f"لا يوجد محتوى مضاف لقسم {section_key}.")
        return

    title, name, end_time_asia, end_time_europe, end_time_america, file_id = row

    if title:
        text = f"🔹 **{title} :**\n\n"
    else:
        arabic_section_titles = {
            'abyss': 'الأبِس',
            'stygian': 'ستيجيان',
            'theater': 'المسرح',
            'banner': 'البنر'
        }
        arabic_section_title = arabic_section_titles.get(section_key, section_key)
        text = f"🔹 **{arabic_section_title} :**\n\n"

    if section_key == 'banner' and name:
        text += f"**{name}**\n\n"

    times_dict = {
        'end_time_asia': end_time_asia,
        'end_time_europe': end_time_europe,
        'end_time_america': end_time_america
    }

    server_name_map = {
        'end_time_asia': 'اسيا',
        'end_time_europe': 'اوروبا',
        'end_time_america': 'امريكا'
    }

    now_utc = datetime.now(timezone.utc)

    for server_key, end_time_str in times_dict.items():
        if not end_time_str:
            continue

        arabic_server_name = server_name_map.get(server_key, server_key)

        # The stored time is now UTC, so we can replace the timezone directly
        end_time_utc = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        time_left = time_left_str(end_time_utc, now_utc)

        text += f"⏳الوقت المتبقي سيرفر {arabic_server_name} :\n"
        text += f" ●← {time_left}\n\n"

    if file_id:
        await message.reply_photo(photo=file_id, caption=text, parse_mode="Markdown")
    else:
        await message.reply(text, parse_mode="Markdown")

# Unified handler for events
@dp.message(Command('events', 'event'))
@dp.message(F.text.lower().in_(['الاحداث']))
async def cmd_show_events(message: types.Message):
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%Y-%m-%d %H:%M:%S")

    # 🟢 التنظيف التلقائي للأحداث المنتهية
    cursor.execute("DELETE FROM content WHERE section='events' AND end_time_asia <= ?", (now_str,))
    conn.commit()

    cursor.execute("SELECT name, end_time_asia FROM content WHERE section='events'")
    events = cursor.fetchall()

    if not events:
        await message.reply("لا يوجد أحداث مضافة حاليًا.")
        return

    text = "📌 **قائمة الأحداث الحالية:**\n\n"
    for i, event in enumerate(events):
        name, end_time_str = event
        end_time_utc = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        time_left = time_left_str(end_time_utc, now_utc)

        text += f"**{i+1}. {name}**\n\n"
        text += f"⏳الوقت المتبقي\n{time_left}\n"
        text += "---\n"

    await message.reply(text, parse_mode="Markdown")

# Unified handler for deleting events
@dp.message(Command('delevents'))
@dp.message(F.text.lower().in_(['حذف_الاحداث']))
async def cmd_delete_events(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.reply("🚫 ليس لديك صلاحية حذف الأحداث.")
        return

    cursor.execute("DELETE FROM content WHERE section='events'")
    conn.commit()
    await message.reply("✅ تم حذف جميع الأحداث بنجاح.")

# New handler for the custom 'الاوامر' command
@dp.message(F.text.lower().in_(['الاوامر']))
async def cmd_custom_commands(message: types.Message):
    await message.reply(
        "اوامر بوت ساندرون :\n\n"
        "/the_banner البنر او\n" # Changed /banner to /the_banner
        "/stygian ستيجيان او\n"
        "/spiral_abyss الابيس او\n"
        "/theater المسرح او\n"
        "/event الاحداث او"
    )

# Unified handler for adding admin
@dp.message(Command('addadmin'))
@dp.message(F.text.lower().startswith('اضافة_مشرف'))
async def cmd_addadmin(message: types.Message, command: Command=None):
    if message.from_user.id != OWNER_ID:
        await message.reply("🚫 فقط المالك يمكنه إضافة مشرفين.")
        return

    if command and command.args:
        args = command.args.split()
    else:
        args = message.text.split()[1:]

    if not args:
        await message.reply("يرجى كتابة معرف المستخدم لإضافته كمشرف.\nمثال:\n/addadmin 123456789")
        return
    try:
        new_id = int(args[0])
        cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_id,))
        conn.commit()
        await message.reply(f"✅ تم إضافة المستخدم {new_id} كمشرف.")
    except (ValueError, IndexError):
        await message.reply("❌ حدث خطأ أثناء الإضافة. يرجى التأكد من أن المعرف هو رقم صحيح.")

# Unified handler for removing admin
@dp.message(Command('removeadmin'))
@dp.message(F.text.lower().startswith('ازالة_مشرف'))
async def cmd_removeadmin(message: types.Message, command: Command=None):
    if message.from_user.id != OWNER_ID:
        await message.reply("🚫 فقط المالك يمكنه إزالة المشرفين.")
        return

    if command and command.args:
        args = command.args.split()
    else:
        args = message.text.split()[1:]

    if not args:
        await message.reply("يرجى كتابة معرف المستخدم لإزالته من المشرفين.\nمثال:\n/removeadmin 123456789")
        return
    try:
        rem_id = int(args[0])
        if rem_id == OWNER_ID:
            await message.reply("🚫 لا يمكن إزالة المالك نفسه.")
            return
        cursor.execute("DELETE FROM admins WHERE user_id = ?", (rem_id,))
        conn.commit()
        await message.reply(f"✅ تم إزالة المستخدم {rem_id} من المشرفين.")
    except (ValueError, IndexError):
        await message.reply("❌ حدث خطأ أثناء الحذف. يرجى التأكد من أن المعرف هو رقم صحيح.")

# Unified handler for start/help message
@dp.message(Command('start', 'help'))
@dp.message(F.text.lower().in_(['بدء']))
async def cmd_start(message: types.Message):
    await message.reply(
        "مرحبًا! أنا بوت مواعيد Genshin.\n"
        "الأوامر:\n"
        "/the_banner البنر أو التبـنر - عرض البنر الحالي\n" # Changed /banner to /the_banner and added 'التبنر'
        "events أو الاحداث - عرض الأحداث\n"
        "abyss أو الابيس - عرض موعد الأبِس\n"
        "stygian أو ستيجيان - عرض موعد ستيجيان\n"
        "theater أو المسرح - عرض موعد المسرح\n\n"
        "للمشرفين:\n"
        "setbanner أو setbanner_ar - تحديث البنر (يرسل نص ثم صورة)\n"
        "setevents أو setevents_ar - إضافة حدث جديد (يرسل نص فقط)\n"
        "delevents أو حذف_الاحداث - حذف جميع الأحداث (للمشرفين)\n"
        "setabyss أو setabyss_ar - تحديث الأبِس (يرسل نص ثم صورة)\n"
        "setstygian أو setstygian_ar - تحديث ستيجيان (يرسل نص ثم صورة)\n"
        "settheater أو settheater_ar - تحديث المسرح (يرسل نص ثم صورة)\n\n"
        "لإضافة/حذف مشرفين:\n"
        "addadmin [user_id] أو اضافة_مشرف [user_id]\n"
        "removeadmin [user_id] أو ازالة_مشرف [user_id]"
    )

async def main():
    print("بوت Genshin شغال...")
    # 🟢 تشغيل مهمة التنبيهات الخلفية
    asyncio.create_task(check_and_send_alerts())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
