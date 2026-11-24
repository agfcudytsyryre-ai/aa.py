import telebot
from telebot import types
import json
import os
import time
import threading
import html
import re
import gzip
import hashlib
import heapq
import random
import atexit
import signal
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# ⚠️⚠️ تم إعداد الآيدي الخاص بك هنا ⚠️⚠️
DEV_ID = 1920665874
ORDERS_CHANNEL = "@fhfjhvj"  # قناة طلبات التمويل (لازم البوت يكون مشرف فيها)
DEV_USERNAME = "@u_8_y"

TOKEN = '9262886630:AAEob_KIgVc3eKCes1jBmXjWFEny2AtrJAs'
# عدد خيوط المعالجة (الافتراضي 2 فقط = سبب التأخير عند ضغط عدة أشخاص بنفس الوقت)
bot = telebot.TeleBot(TOKEN, num_threads=16)
try:
    from telebot import apihelper as _apih
    _apih.RETRY_ON_ERROR = False     # لا نعيد إرسال الرسائل تلقائياً (كان يسبب تكرار الرسالة إذا تأخر رد الشبكة)
    # توقف الأزرار 30 ثانية كان سببه طلب لتلغرام يعلق حتى انتهاء المهلة (30ث). نقصّر المهل ونجبر IPv4 (بعض الاستضافات IPv6 عندها معطّل)
    _apih.CONNECT_TIMEOUT = 5
    _apih.READ_TIMEOUT = 8
    _apih.SESSION_TIME_TO_LIVE = 30     # تجديد اتصال الخيط كل 30 ثانية حتى لا نستعمل اتصالاً ميتاً
except Exception: pass
try:
    import socket as _socket, urllib3.util.connection as _u3c
    _u3c.allowed_gai_family = lambda: _socket.AF_INET
except Exception: pass
_me = bot.get_me()
BOT_USERNAME = _me.username
BOT_ID = _me.id
# أمر "رسالة البدء" بقائمة الأوامر (يظهر جنب زر القائمة)
try: bot.set_my_commands([types.BotCommand('start', 'رسالة البدء')])
except Exception: pass

# True = رسالة الترحيب تطلع مع كل /start | False = تطلع للعضو الجديد فقط (أول مرة)
WELCOME_ALWAYS = False

# مجمّعات خيوط للأعمال الخلفية (إجابة الأزرار، فحص الاشتراكات المتوازي...)
_io_pool = ThreadPoolExecutor(max_workers=16)
_watch_pool = ThreadPoolExecutor(max_workers=8)

# كشف تشغيل البوت بأكثر من مكان بنفس التوكن (سبب شائع لتكرار الرسائل 2-3 مرات)
_last_conflict_alert = [0]
class _PollErrHandler:
    def handle(self, exception):
        try:
            m = str(exception)
            if '409' in m or 'Conflict' in m:
                print("⚠️ يوجد نسخة أخرى من البوت تعمل بنفس التوكن! أوقف النسخ الزائدة.")
                if time.time() - _last_conflict_alert[0] > 3600:
                    _last_conflict_alert[0] = time.time()
                    _io_pool.submit(lambda: bot.send_message(DEV_ID, "⚠️ <b>تنبيه:</b> البوت يعمل حالياً بأكثر من مكان بنفس التوكن، وهذا يسبب تكرار الرسائل 2-3 مرات.\nأوقف النسخ الزائدة (المنصة القديمة / Pydroid / الجهاز) وخلّ نسخة وحدة فقط.", parse_mode='HTML'))
        except Exception: pass
        return False
try: bot.exception_handler = _PollErrHandler()
except Exception: pass

_recent_msgs = {}
def _is_dup_message(message):
    """يتجاهل نفس الرسالة إذا وصلت مرتين خلال 30 ثانية"""
    k = (message.chat.id, message.message_id)
    now = time.time()
    if now - _recent_msgs.get(k, 0) < 30: return True
    _recent_msgs[k] = now
    if len(_recent_msgs) > 3000:
        for kk in [kk for kk, t in _recent_msgs.items() if now - t > 60]: _recent_msgs.pop(kk, None)
    return False

DB_FILE = 'database.json'
COUPONS_FILE = 'coupons.json'
SETTINGS_FILE = 'settings.json'
FUNDINGS_FILE = 'fundings.json'
FORCE_SUB_FILE = 'force_sub.json'
POINTS_CHANNELS_FILE = 'points_channels.json'
TURBO_CHANNELS_FILE = 'turbo_channels.json'

user_states = {}  
data_lock = threading.Lock()

# ========== حفظ الملفات بالخلفية (بدون تعليق البوت) وبشكل آمن ضد التلف ==========
_dirty_files = {}
_dirty_lock = threading.Lock()
_write_event = threading.Event()
_data_version = 0

def _queue_write(path, getter):
    global _data_version
    with _dirty_lock:
        _dirty_files[path] = getter
        _data_version += 1
    _write_event.set()

def _write_file_now(path, getter):
    last_ex = None
    for _ in range(6):
        try:
            raw = json.dumps(getter(), ensure_ascii=False, separators=(',', ':'), default=str)
            break
        except RuntimeError as ex:      # القاموس تغيّر أثناء القراءة
            last_ex = ex; time.sleep(0.05)
    else:
        raise last_ex
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(raw)
        f.flush()
        try: os.fsync(f.fileno())
        except Exception: pass
    os.replace(tmp, path)               # استبدال ذرّي: لا يمكن أن يبقى ملف نصف مكتوب

def flush_all_writes():
    with _dirty_lock:
        items = dict(_dirty_files); _dirty_files.clear()
    for path, getter in items.items():
        try: _write_file_now(path, getter)
        except Exception as ex: print(f"[save] فشل حفظ {path}: {ex}")

def _writer_loop():
    while True:
        _write_event.wait()
        _sz = len(globals().get('_c_db') or {})
        time.sleep(min(4.0, 0.4 + _sz / 10000.0))   # كلما كبرت القاعدة نجمّع الحفظ أكثر حتى ما يتأخر البوت
        _write_event.clear()
        flush_all_writes()

threading.Thread(target=_writer_loop, daemon=True).start()
atexit.register(flush_all_writes)

def _backup_corrupt(path):
    try:
        if os.path.exists(path): os.replace(path, f"{path}.corrupt_{int(time.time())}")
    except Exception: pass


# ========== الكاش الداخلي (لتسريع البوت ومنع التعليق) ==========
_c_db = None
_c_coupons = None
_c_settings = None
_c_fundings = None
_c_force_sub = None
_c_points = None
_c_turbo = None

# ========== دوال المساعدة للأزرار الملونة ==========
def make_btn(text, callback_data=None, url=None, style=None, icon_key=None, raw=False, switch_inline_query=None):
    if not raw:
        _ov = ''; _own = ''
        try:
            _st = load_settings()
            _ov = _st.get('btn_color', '')
            _ck = icon_key if (icon_key and icon_key != '__nav__') else btn_key(text)
            _own = (_st.get('btn_colors') or {}).get(_ck, '')   # لون خاص بهذا الزر فقط
        except Exception: pass
        if _own in ('primary', 'success', 'danger'): style = _own        # لون الزر الفردي له الأولوية
        elif _ov in ('primary', 'success', 'danger'): style = _ov        # لون موحّد لكل الأزرار من لوحة المطور
        elif 'رجوع' in (text or ''): style = 'danger'                  # كل أزرار الرجوع حمراء
    base = {}
    if callback_data is not None: base['callback_data'] = callback_data
    if url is not None: base['url'] = url
    if switch_inline_query is not None: base['switch_inline_query'] = switch_inline_query or ' '   # بعض إصدارات المكتبة تحذف النص الفارغ فيصير الزر بلا وظيفة (خطأ 400)
    new_text, icon_id = apply_btn_icon(text, icon_key)
    attempts = []
    if icon_id:
        a = dict(base); a['icon_custom_emoji_id'] = icon_id
        if style is not None: a['style'] = style
        attempts.append((new_text, a))
        if style is not None:
            b = dict(base); b['icon_custom_emoji_id'] = icon_id
            attempts.append((new_text, b))
        fallback_text = text   # إذا المكتبة قديمة ولا تدعم الإيموجي المميز يرجع الإيموجي الأصلي
    else:
        fallback_text = new_text
    if style is not None:
        c = dict(base); c['style'] = style
        attempts.append((fallback_text, c))
    attempts.append((fallback_text, dict(base)))
    for t, kw in attempts:
        try:
            return types.InlineKeyboardButton(t, **kw)
        except TypeError:
            continue
    return types.InlineKeyboardButton(fallback_text, **base)

# ========== نظام إيموجيات الأزرار (يُطبَّق تلقائياً على كل أزرار البوت) ==========
# المفتاح = نص الزر بدون إيموجيات وبدون أرقام، والتخزين في settings['btn_icons']
def _is_pic(ch):
    o = ord(ch)
    if ch == '\u200d': return True
    return o >= 0x2190 and not (0xFB50 <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF)

def btn_key(text):
    t = ''.join(c for c in str(text) if not (_is_pic(c) or c in '•~'))
    t = re.sub(r'\d+', '#', t)
    return re.sub(r'\s+', ' ', t).strip()

def _split_leading_emoji(text):
    i = 0
    while i < len(text) and (_is_pic(text[i]) or text[i] == ' '):
        i += 1
    return text[:i], text[i:]

def _first_emoji(text):
    """يستخرج أول إيموجي عادي من النص (مع دعم التركيبات والأعلام)"""
    n = len(text); i = 0
    while i < n and not _is_pic(text[i]): i += 1
    if i >= n: return None
    j = i + 1
    if 0x1F1E6 <= ord(text[i]) <= 0x1F1FF and j < n and 0x1F1E6 <= ord(text[j]) <= 0x1F1FF:
        return text[i:j + 1]
    while j < n:
        o = ord(text[j])
        if o == 0xFE0F or 0x1F3FB <= o <= 0x1F3FF: j += 1
        elif o == 0x200D and j + 1 < n: j += 2
        else: break
    return text[i:j]

_icon_cache = {}

def apply_btn_icon(text, icon_key=None):
    """نسخة مخزّنة مؤقتاً (تُمسح عند حفظ الإعدادات) لتسريع بناء الأزرار"""
    ck = (text, icon_key)
    r = _icon_cache.get(ck)
    if r is None:
        r = _apply_btn_icon_raw(text, icon_key)
        if len(_icon_cache) > 5000: _icon_cache.clear()
        _icon_cache[ck] = r
    return r

def _fast_mode():
    try: return bool(load_settings().get('fast_mode'))
    except Exception: return False

def _apply_btn_icon_raw(text, icon_key=None):
    """يرجع (النص، آيدي الإيموجي المميز أو None) حسب ما حفظه المطور"""
    try:
        key = icon_key or btn_key(text)
        cfg = (load_settings().get('btn_icons') or {}).get(key)
        lead, rest = _split_leading_emoji(text)
        if not rest.strip(): return text, None
        if not cfg:
            lead_emo = _first_emoji(lead) if lead else None
            cid = _te_map().get(_te_key(lead_emo)) if lead_emo else None
            if cid: return (text, None) if _fast_mode() else (rest, str(cid))
            return text, None
        if cfg.get('id'): return (text, None) if _fast_mode() else (rest, str(cfg['id']))
        if cfg.get('emoji'): return f"{cfg['emoji']} {rest}", None
    except Exception:
        pass
    return text, None

BTN_CATALOG = [
    ('NEW إكتمال الطلبات', 'NEW إكتمال الطلبات', 'main'),
    ('اشتر مضاعف نقاط', '⚡ اشتر مضاعف نقاط', 'main'),
    ('هديتك اليوميه', '🎁 هديتك اليوميه', 'main'),
    ('شروط الاستخدام', '🛑 شروط الاستخدام', 'main'),
    ('مشتركين اجباري', '⭐ مشتركين اجباري', 'main'),
    ('لوحة المطور', '🛠️ لوحة المطور', 'main'),
    ('تجميع النقاط', '➕ تجميع النقاط', 'main'),
    ('المهام', '📅 المهام', 'main'),
    ('رصيدك: #', '💎 رصيدك', 'main'),
    ('تمويل قناتك أو مجموعتك', '🚀 تمويل قناتك أو مجموعتك', 'main'),
    ('تحويل النقاط', '💸 تحويل النقاط', 'main'),
    ('شحن النقاط', '⭐ شحن النقاط', 'main'),
    ('معلومات حسابك', '📋 معلومات حسابك', 'main'),
    ('ادع صديقاً', '🤝 ادع صديقاً', 'main'),
    ('قسائمي', '🎟️ قسائمي', 'main'),
    ('إلغاء العملية', '❌ إلغاء العملية', 'int'),
    ('تحقق من الاشتراك', 'تحقق من الاشتراك ✳️', 'int'),
    ('اطلب الان', 'اطلب الان ✅', 'int'),
    ('الرئيسية', '➡️ الرئيسية ⬅️', 'int'),
    ('مشاركة مع الأصدقاء', '✉️ مشاركة مع الأصدقاء', 'int'),
    ('تيربو', '⚡ تيربو', 'int'),
    ('قنوات', '📺 قنوات', 'int'),
    ('رابط الدعوة', '👥 رابط الدعوة 🔗', 'int'),
    ('رجوع', '⬅️ رجوع', 'int'),
    ('تحقق من الاشتراكات', '✅ تحقق من الاشتراكات', 'int'),
    ('تمويلاتك', 'تمويلاتك', 'int'),
    ('شحن بالنجوم', '⭐ شحن بالنجوم', 'int'),
    ('شحن بالرصيد (دولار)', '💵 شحن بالرصيد (دولار)', 'int'),
    ('إغلاق', '❌ إغلاق ❌', 'int'),
    ('مشتركين إجباري (نجوم)', '⭐ مشتركين إجباري (نجوم)', 'int'),
    ('مشتركين إجباري (رصيد)', '💵 مشتركين إجباري (رصيد)', 'int'),
    ('مراسلة المطور', '👤 مراسلة المطور', 'int'),
    ('مشتركين حقيقيين', '👥 مشتركين حقيقيين', 'int'),
    ('شراء النقاط', '💎 شراء النقاط', 'int'),
    ('سجل نقاطي', '💰 سجل نقاطي', 'int'),
    ('تأكيد الطلب', '✅ تأكيد الطلب', 'int'),
    ('إلغاء الطلب', '❌ إلغاء الطلب', 'int'),
    ('افتح القناة', '📺 افتح القناة', 'int'),
    ('التالي', '▶️ التالي', 'int'),
    ('تحقق', '✅ تحقق', 'int'),
    ('إبلاغ', '🚩 إبلاغ', 'int'),
    ('شحن # نقطة = #', '🛒 أزرار شحن النقاط (نجوم)', 'int'),
    ('شحن # نقطة = # $', '🛒 أزرار شحن النقاط (دولار)', 'int'),
    ('# مشترك = #', '🛒 أزرار شراء مشتركين (نجوم)', 'int'),
    ('# مشترك = # $', '🛒 أزرار شراء مشتركين (دولار)', 'int'),
    ('إضافة كليشة البوت', '📝 إضافة كليشة البوت', 'dev'),
    ('تبديل إيموجيات الأزرار', '😀 تبديل إيموجيات الأزرار', 'dev'),
    ('هدية اليوم', '🎁 هدية اليوم', 'dev'),
    ('سعر العضو', '💵 سعر العضو', 'dev'),
    ('الحد الأدنى', '📉 الحد الأدنى', 'dev'),
    ('الحد الأقصى', '📈 الحد الأقصى', 'dev'),
    ('نقاط الدعوة', '🤝 نقاط الدعوة', 'dev'),
    ('اسم البوت', '✏️ اسم البوت', 'dev'),
    ('تفعيل/تعطيل التحويل', '💸 تفعيل/تعطيل التحويل', 'dev'),
    ('الاشتراك الإجباري', '🔒 الاشتراك الإجباري', 'dev'),
    ('إدارة قنوات النقاط', '📺 إدارة قنوات النقاط', 'dev'),
    ('إدارة قنوات التيربو', '🚀 إدارة قنوات التيربو', 'dev'),
    ('قناة إكمال الطلبات', '📢 قناة إكمال الطلبات', 'dev'),
    ('إحصائيات مفصلة', '📊 إحصائيات مفصلة', 'dev'),
    ('تعديل رصيد', '💰 تعديل رصيد', 'dev'),
    ('إضافة قسيمة', '🎟️ إضافة قسيمة', 'dev'),
    ('قائمة القسائم', '📋 قائمة القسائم', 'dev'),
    ('حذف قسيمة', '🗑️ حذف قسيمة', 'dev'),
    ('الإذاعة (Broadcast)', '📢 الإذاعة (Broadcast)', 'dev'),
    ('حظر مستخدم', '🚫 حظر مستخدم', 'dev'),
    ('فك حظر مستخدم', '✅ فك حظر مستخدم', 'dev'),
    ('إضافة قناة', '➕ إضافة قناة', 'dev'),
    ('إضافة كروب', '➕ إضافة كروب', 'dev'),
    ('حذف قناة / كروب', '🗑️ حذف قناة / كروب', 'dev'),
    ('رجوع للوحة', '🔙 رجوع للوحة', 'dev'),
    ('تعديل المكافأة', '💵 تعديل المكافأة', 'dev'),
    ('حذف قناة', '🗑️ حذف قناة', 'dev'),
    ('للمستخدمين (الخاص)', '👤 للمستخدمين (الخاص)', 'dev'),
    ('للقنوات', '📺 للقنوات', 'dev'),
    ('للمجموعات', '👥 للمجموعات', 'dev'),
    ('قسم مشاهدات وتفاعلات للقنوات', '📊 قسم مشاهدات وتفاعلات للقنوات', 'int'),
    ('مشاهدات تيليجرام على رسالة واحدة', '👁 مشاهدات تيليجرام على رسالة واحدة', 'int'),
    ('تفاعلات تيليجرام على رسالة واحدة', '❤️ تفاعلات تيليجرام على رسالة واحدة', 'int'),
    ('تسريع البوت', '⚡ تسريع البوت', 'dev'),
    ('سعر المشاهدات', '👁 سعر المشاهدات', 'dev'),
    ('سعر التفاعلات', '❤️ سعر التفاعلات', 'dev'),
    ('حدود المشاهدات', '👁 حدود المشاهدات', 'dev'),
    ('تقدم طلباتي', '📈 تقدم طلباتي', 'int'),
    ('تحديث التقدم', '📈 تحديث التقدم', 'dev'),
    ('حدود التفاعلات', '❤️ حدود التفاعلات', 'dev'),
]

BTN_GROUPS = [
    ('main', '🏠 الأزرار الرئيسية'),
    ('int', '📂 الأزرار الداخلية'),
    ('dev', '🛠️ أزرار لوحة المطور'),
]
ICON_PAGE_SIZE = 10

def _icon_state(key):
    cfg = (load_settings().get('btn_icons') or {}).get(key)
    if not cfg: return 'الافتراضي'
    if cfg.get('id'): return 'إيموجي مميز ✨'
    return f"إيموجي {cfg.get('emoji', '')}"


# ========== ألوان الأزرار الفردية (لكل زر لونه) ==========
COLOR_NAMES = {'primary': '🔵 أزرق', 'success': '🟢 أخضر', 'danger': '🔴 أحمر'}
COLOR_CODES = {'p': 'primary', 's': 'success', 'd': 'danger'}
COLOR_PAGE_SIZE = 10

def _own_color(key):
    return (load_settings().get('btn_colors') or {}).get(key, '')

def get_btn_colors_menu():
    st = load_settings()
    cur = st.get('btn_color', '')
    fast = bool(st.get('fast_mode'))
    names = {'primary': '🔵 أزرق', 'success': '🟢 أخضر', 'danger': '🔴 أحمر'}
    text = (f"🎨 <b>ألوان الأزرار</b>\n\nاللون الحالي: <b>{names.get(cur, 'الألوان الأصلية (مختلطة)')}</b>\n\n"
            "اختر لوناً موحداً لـ<b>كل أزرار البوت</b> (يمسح الألوان الفردية)، أو اضغط <b>🎯 تلوين كل زر لوحده</b> لتختار لون كل زر على حدة.\n\n"
            f"⚡ <b>وضع السرعة:</b> {'مفعّل' if fast else 'معطّل'}\n"
            "يستبدل الإيموجي المميز (على الأزرار والنصوص) بإيموجي عادي، لأن تلغرام يتأخر في تعديل الرسائل اللي فيها إيموجي مميز. الألوان تبقى كما هي.")
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.row(make_btn('🔴 أحمر', callback_data='dev_color_danger', style='danger', raw=True),
               make_btn('🟢 أخضر', callback_data='dev_color_success', style='success', raw=True),
               make_btn('🔵 أزرق', callback_data='dev_color_primary', style='primary', raw=True))
    markup.add(make_btn('🎯 تلوين كل زر لوحده', callback_data='dev_cmenu', style='success', raw=True))
    markup.add(make_btn('⚡ وضع السرعة: ' + ('مفعّل ✅ (اضغط للإلغاء)' if fast else 'معطّل (اضغط للتفعيل)'), callback_data='dev_fast', style='success' if fast else 'primary', raw=True))
    markup.add(make_btn('♻️ رجوع للألوان الأصلية', callback_data='dev_color_default', raw=True))
    markup.add(make_btn('⬅️ لوحة المطور', callback_data='dev_panel_back', raw=True))
    return text, markup

def get_color_groups_menu():
    cols = load_settings().get('btn_colors') or {}
    text = ("🎯 <b>تلوين كل زر لوحده</b>\n\n"
            "اختر القسم، ثم اضغط على الزر اللي تريد تغيّر لونه.\n"
            "كل زر يظهر هنا بلونه الحالي، واللون ينطبق عليه بكل أماكن ظهوره.\n\n"
            f"🎨 عدد الأزرار الملونة يدوياً: <b>{len(cols)}</b>")
    markup = types.InlineKeyboardMarkup(row_width=1)
    for g, title in BTN_GROUPS:
        n = len([1 for _, _, gg in BTN_CATALOG if gg == g])
        markup.add(make_btn(f'{title} ({n})', callback_data=f'dev_cg_{g}_0', style='primary', raw=True))
    markup.add(make_btn('🧹 مسح كل ألوان الأزرار الفردية', callback_data='dev_cclear', style='danger', raw=True))
    markup.add(make_btn('🔙 رجوع', callback_data='dev_btn_colors', style='danger', raw=True))
    return text, markup

def get_color_list(group, page):
    items = [(i, k, l) for i, (k, l, g) in enumerate(BTN_CATALOG) if g == group]
    pages = max(1, -(-len(items) // COLOR_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = items[page * COLOR_PAGE_SIZE:(page + 1) * COLOR_PAGE_SIZE]
    title = dict(BTN_GROUPS).get(group, '')
    text = (f"🎯 <b>{title}</b>  —  صفحة {page + 1}/{pages}\n\n"
            "اضغط على الزر لتغيير لونه (الزر يظهر بلونه الحالي).")
    markup = types.InlineKeyboardMarkup(row_width=2)
    row = []
    for i, k, l in chunk:
        cur = _own_color(k) or None
        row.append(make_btn(l, callback_data=f'dev_cp_{i}', style=cur, icon_key=k, raw=True))
        if len(row) == 2:
            markup.row(*row); row = []
    if row: markup.row(*row)
    nav = []
    if page > 0: nav.append(make_btn('◀️ السابق', callback_data=f'dev_cg_{group}_{page - 1}', style='primary', raw=True))
    if page < pages - 1: nav.append(make_btn('التالي ▶️', callback_data=f'dev_cg_{group}_{page + 1}', style='primary', raw=True))
    if nav: markup.row(*nav)
    markup.add(make_btn('🔙 رجوع', callback_data='dev_cmenu', style='danger', raw=True))
    return text, markup

def get_color_pick(idx):
    key, label, grp = BTN_CATALOG[idx]
    back_page = len([1 for _, _, g in BTN_CATALOG[:idx] if g == grp]) // COLOR_PAGE_SIZE
    cur = _own_color(key)
    text = (f"🎨 <b>لون الزر</b>\n\n🔘 الزر: <b>{html.escape(label)}</b>\n"
            f"📌 اللون الحالي: <b>{COLOR_NAMES.get(cur, 'الافتراضي')}</b>\n\nاختر اللون الجديد:")
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.row(make_btn('🔴 أحمر', callback_data=f'dev_cs_{idx}_d', style='danger', raw=True),
               make_btn('🟢 أخضر', callback_data=f'dev_cs_{idx}_s', style='success', raw=True),
               make_btn('🔵 أزرق', callback_data=f'dev_cs_{idx}_p', style='primary', raw=True))
    markup.add(make_btn('♻️ استرجاع اللون الافتراضي', callback_data=f'dev_cs_{idx}_x', raw=True))
    markup.add(make_btn('🔙 رجوع', callback_data=f'dev_cg_{grp}_{back_page}', style='danger', raw=True))
    return text, markup

def get_icons_groups_menu():
    text = ("😀 <b>تبديل إيموجيات الأزرار</b>\n\n"
            "✨ <b>إيموجيات كل النصوص والرسائل</b>: تبدّل أي إيموجي عادي بإيموجي مميز بكل رسائل البوت.\n"
            "أو اختر قسم الأزرار لتغيير إيموجي زر معيّن فقط.\n"
            "التغيير يخص الإيموجي فقط، ونص الزر يبقى كما هو.")
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(make_btn('✨ إيموجيات كل النصوص والرسائل', callback_data='dev_te_0', style='success', icon_key='__nav__'))
    markup.add(make_btn('🧪 فحص الإيموجي المميز', callback_data='dev_te_diag', style='primary', icon_key='__nav__'))
    for g, title in BTN_GROUPS:
        n = len([1 for _, _, gg in BTN_CATALOG if gg == g])
        markup.add(make_btn(f'{title} ({n})', callback_data=f'dev_ic_g_{g}_0', style='primary', icon_key='__nav__'))
    markup.add(make_btn('🔙 رجوع', callback_data='dev_panel', style='danger', icon_key='__nav__'))
    return text, markup

def get_icons_list(group, page):
    items = [(i, k, l) for i, (k, l, g) in enumerate(BTN_CATALOG) if g == group]
    pages = max(1, -(-len(items) // ICON_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = items[page * ICON_PAGE_SIZE:(page + 1) * ICON_PAGE_SIZE]
    title = dict(BTN_GROUPS).get(group, '')
    text = (f"😀 <b>{title}</b>  —  صفحة {page + 1}/{pages}\n\n"
            "اضغط على الزر الذي تريد تغيير إيموجيه (الأزرار المعروضة هي نفسها كما تظهر للمستخدمين).")
    markup = types.InlineKeyboardMarkup(row_width=2)
    row = []
    for i, k, l in chunk:
        row.append(make_btn(l, callback_data=f'dev_ic_pick_{i}', icon_key=k))
        if len(row) == 2:
            markup.row(*row); row = []
    if row: markup.row(*row)
    nav = []
    if page > 0: nav.append(make_btn('◀️ السابق', callback_data=f'dev_ic_g_{group}_{page - 1}', style='primary', icon_key='__nav__'))
    if page < pages - 1: nav.append(make_btn('التالي ▶️', callback_data=f'dev_ic_g_{group}_{page + 1}', style='primary', icon_key='__nav__'))
    if nav: markup.row(*nav)
    markup.add(make_btn('🔙 رجوع', callback_data='dev_edit_emojis', style='danger', icon_key='__nav__'))
    return text, markup


# ========== إيموجيات مميزة لكل نصوص ورسائل البوت ==========
# settings['text_emojis'] = {الإيموجي العادي: آيدي الإيموجي المميز}
TEXT_EMOJI_LIST = ['❌', '✅', '💎', '🛒', '⭐', '⬅', '💵', '📺', '🔙', '📝', '🎁', '👥', '👤', '💰', '➕', '🚀', '⚠', '💸', '✨', '➡', '🗑', '🎟', '🔗', '⬇', '📊', '🚫', '⚡', '🤝', '📢', '📌', '📅', '📋', '📈', '📉', '✉', '🎉', '🛠', '🚩', '😀', '✏', '🔒', '🏠', '🤖', '🛑', '▶', '🆔', '🔥', '👇', '🔢', '✳', '⚙', '🎊', '💡', '🏅', '🏆', '⏰', '🔜', '📂', '◀', '🪙', '🔔', '📦', '🎯', '👋', '🥇', '🥈', '🥉', '❤', '🏙', '🔄', '📡', '✋', '🟩', '🕒', '📥', '🔘', '♻', '⏳', '🌟']

TE_PAGE_SIZE = 20

def _emoji_clusters(text):
    res = []; n = len(text); i = 0
    while i < n:
        if _is_pic(text[i]):
            j = i + 1
            if 0x1F1E6 <= ord(text[i]) <= 0x1F1FF and j < n and 0x1F1E6 <= ord(text[j]) <= 0x1F1FF: j += 1
            while j < n:
                o = ord(text[j])
                if o == 0xFE0F or 0x1F3FB <= o <= 0x1F3FF or o == 0x20E3: j += 1
                elif o == 0x200D and j + 1 < n: j += 2
                else: break
            res.append((i, j)); i = j
        else:
            i += 1
    return res

def _te_key(cluster):
    return cluster.replace('\ufe0f', '')

def _te_map():
    try: return load_settings().get('text_emojis') or {}
    except Exception: return {}

def _premium_html(text, mp):
    out = []; skip = 0
    for part in re.split(r'(<[^>]*>)', text):
        if len(part) > 1 and part.startswith('<') and part.endswith('>'):
            tg = re.match(r'</?\s*([a-zA-Z\-]+)', part)
            if tg and tg.group(1).lower() in ('code', 'pre', 'tg-emoji'):
                if part.startswith('</'): skip = max(0, skip - 1)
                elif not part.endswith('/>'): skip += 1
            out.append(part)
        elif skip:
            out.append(part)
        else:
            res = []; last = 0
            for s, e in _emoji_clusters(part):
                cid = mp.get(_te_key(part[s:e]))
                if cid:
                    res.append(part[last:s])
                    res.append(f'<tg-emoji emoji-id="{cid}">{part[s:e]}</tg-emoji>')
                    last = e
            res.append(part[last:])
            out.append(''.join(res))
    return ''.join(out)

def _premium_entities(text, mp):
    ents = []; pos = 0; off = 0
    for s, e in _emoji_clusters(text):
        cid = mp.get(_te_key(text[s:e]))
        if not cid: continue
        off += len(text[pos:s].encode('utf-16-le')) // 2
        ln = len(text[s:e].encode('utf-16-le')) // 2
        ents.append(types.MessageEntity(type='custom_emoji', offset=off, length=ln, custom_emoji_id=cid))
        off += ln; pos = e
    return ents

def _te_convert(text, kwargs):
    if _fast_mode(): return None
    mp = _te_map()
    if not mp or not isinstance(text, str): return None
    pm = kwargs.get('parse_mode')
    if pm == 'HTML':
        nt = _premium_html(text, mp)
        return (nt, kwargs) if nt != text else None
    if pm is None and not kwargs.get('entities'):
        ents = _premium_entities(text, mp)
        if ents:
            kw = dict(kwargs); kw['entities'] = ents
            return text, kw
    return None

_orig_send_message = bot.send_message
_net_tl = threading.local()
def _timed_net(fn):
    def w(*a, **k):
        _t = time.time()
        try: return fn(*a, **k)
        finally: _net_tl.t = getattr(_net_tl, 't', 0.0) + (time.time() - _t)
    return w
_orig_edit_message_text = _timed_net(bot.edit_message_text)

def _send_message_premium(chat_id, text, *args, **kwargs):
    conv = None
    if not args:
        try: conv = _te_convert(text, kwargs)
        except Exception: conv = None
    if conv:
        try: return _orig_send_message(chat_id, conv[0], **conv[1])
        except Exception as ex:
            if 'not modified' in str(ex).lower(): raise
            if getattr(ex, 'error_code', None) != 400: raise   # الرجوع للإرسال العادي فقط إذا رفض تيليجرام الإيموجي (لا نعيد الإرسال عند مهلة/انقطاع)
            print(f"[premium-emoji] تيليجرام رفض الإيموجي المميز وتم الإرسال بالعادي: {ex}")
    return _orig_send_message(chat_id, text, *args, **kwargs)

def _edit_message_text_premium(*args, **kwargs):
    conv = None
    try:
        if len(args) <= 1:
            text = args[0] if args else kwargs.get('text')
            kw = {k: v for k, v in kwargs.items() if k != 'text'}
            conv = _te_convert(text, kw)
    except Exception:
        conv = None
    if conv:
        try: return _orig_edit_message_text(conv[0], **conv[1])
        except Exception as ex:
            if 'not modified' in str(ex).lower(): raise
            if getattr(ex, 'error_code', None) != 400: raise
            print(f"[premium-emoji] تيليجرام رفض الإيموجي المميز وتم التعديل بالعادي: {ex}")
    return _orig_edit_message_text(*args, **kwargs)

bot.send_message = _send_message_premium
bot.edit_message_text = _edit_message_text_premium

def get_text_emojis_list(page):
    pages = max(1, -(-len(TEXT_EMOJI_LIST) // TE_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    start = page * TE_PAGE_SIZE
    chunk = TEXT_EMOJI_LIST[start:start + TE_PAGE_SIZE]
    mp = _te_map()
    done = len([1 for e in TEXT_EMOJI_LIST if mp.get(e)])
    text = (f"✨ <b>إيموجيات النصوص والرسائل</b>  —  صفحة {page + 1}/{pages}\n"
            f"✅ المضبوط: {done}/{len(TEXT_EMOJI_LIST)}\n\n"
            "أي إيموجي تضبطه هنا يتبدل بإيموجي مميز بكل رسائل البوت (وبالأزرار اللي تبدأ فيه).\n"
            "• اضغط إيموجي لتغيير واحد.\n"
            "• أو اضغط «تعيين الصفحة دفعة واحدة» وأرسل إيموجيات مميزة بنفس ترتيب هالصفحة.")
    markup = types.InlineKeyboardMarkup(row_width=5)
    row = []
    for off, e in enumerate(chunk):
        label = e + (' ✅' if mp.get(e) else '')
        row.append(make_btn(label, callback_data=f'dev_te_pick_{start + off}', icon_key='__nav__'))
        if len(row) == 5:
            markup.row(*row); row = []
    if row: markup.row(*row)
    markup.add(make_btn('📦 تعيين الصفحة دفعة واحدة', callback_data=f'dev_te_bulk_{page}', style='success', icon_key='__nav__'))
    nav = []
    if page > 0: nav.append(make_btn('◀️ السابق', callback_data=f'dev_te_{page - 1}', style='primary', icon_key='__nav__'))
    if page < pages - 1: nav.append(make_btn('التالي ▶️', callback_data=f'dev_te_{page + 1}', style='primary', icon_key='__nav__'))
    if nav: markup.row(*nav)
    markup.add(make_btn('🔙 رجوع', callback_data='dev_edit_emojis', style='danger', icon_key='__nav__'))
    return text, markup

def _clear_te_state(uid, chat_id):
    st = user_states.get(uid)
    if st and st.get('step') in ('dev_save_text_emoji', 'dev_save_text_emoji_bulk'):
        user_states.pop(uid, None)
        try: bot.clear_step_handler_by_chat_id(chat_id)
        except Exception: pass

def run_emoji_diagnostic(chat_id):
    import inspect
    lines = ["🧪 <b>فحص الإيموجي المميز</b>\n"]
    lines.append(f"📚 نسخة المكتبة: <code>{getattr(telebot, '__version__', '؟')}</code>")
    try:
        sup = 'icon_custom_emoji_id' in inspect.signature(types.InlineKeyboardButton.__init__).parameters
    except Exception:
        sup = False
    lines.append("🔘 الأزرار بإيموجي مميز: " + ("✅ المكتبة تدعمها" if sup else "❌ المكتبة قديمة — نفّذ: <code>pip install -U pyTelegramBotAPI</code> وأعد تشغيل البوت"))
    mp = _te_map()
    lines.append(f"🔢 إيموجيات النصوص المضبوطة: <b>{len(mp)}</b>")
    if not mp:
        lines.append("❌ ما في ولا إيموجي مضبوط. من لوحة المطور ← تبديل إيموجيات الأزرار ← ✨ إيموجيات كل النصوص.")
    else:
        emo, cid = next(iter(mp.items()))
        try:
            res = _orig_send_message(chat_id, f'<tg-emoji emoji-id="{cid}">{emo}</tg-emoji> رسالة اختبار', parse_mode='HTML')
            ok = any(getattr(e, 'type', '') == 'custom_emoji' for e in (getattr(res, 'entities', None) or []))
            if ok:
                lines.append("✅ تيليجرام قبل الإيموجي المميز بالرسائل.")
            else:
                lines.append("❌ تيليجرام <b>تجاهل</b> الإيموجي المميز وأرسله عادي.\n"
                             "السبب الأكثر احتمالاً: <b>مالك البوت</b> (الحساب اللي أنشأ البوت من BotFather) ما عنده Telegram Premium، "
                             "أو البوت ما اشترى يوزرات إضافية من Fragment.\n"
                             "والقنوات: الإيموجي المميز من البوت ما ينفع فيها إلا بيوزر Fragment.")
        except Exception as ex:
            lines.append(f"❌ خطأ من تيليجرام: <code>{html.escape(str(ex)[:250])}</code>")
    bot.send_message(chat_id, "\n".join(lines), parse_mode='HTML')

def _clear_emoji_state(uid, chat_id):
    st = user_states.get(uid)
    if st and st.get('step') == 'dev_save_emoji':
        user_states.pop(uid, None)
        try: bot.clear_step_handler_by_chat_id(chat_id)
        except Exception: pass

def _render_template(tpl, name, uid):
    """{id} يظهر داخل <code> ليُنسخ بالضغط عليه، وبدون تداخل إذا كان داخل code أصلاً"""
    parts = re.split(r'(<pre>.*?</pre>|<code>.*?</code>)', tpl, flags=re.S)
    out = []
    for part in parts:
        if part.startswith('<pre>') or part.startswith('<code>'):
            out.append(part.replace('{name}', name).replace('{id}', str(uid)))
        else:
            out.append(part.replace('{name}', name).replace('{id}', f'<code>{uid}</code>'))
    return ''.join(out)

def get_cancel_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(make_btn('❌ إلغاء العملية', callback_data='back_to_start', style='danger'))
    return markup

# ========== دوال الإعدادات ==========
def load_settings():
    global _c_settings
    if _c_settings is not None: return _c_settings
    default_settings = {
        "daily_reward": 500, "fund_rate": 50, "min_fund": 100, "max_fund": 20000,
        "ref_reward": 5000, "bot_name": "بوت تمويل سيد", "points_reward": 40,
        "turbo_reward": 200, "completion_channel": "", "complete_tolerance": 1, "transfer_enabled": True, "captcha_enabled": True, "stars_bonus_percent": 0, "btn_color": "", "btn_colors": {},
        "views_price": 500, "reacts_price": 500, "eng_min": 100, "eng_max": 100000, "bot_template": "", "new_user_template": "", "btn_emojis": {}, "btn_icons": {}, "text_emojis": {}
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for k, v in default_settings.items():
                    if k not in data: data[k] = v
                _c_settings = data
                return _c_settings
        except Exception:
            _c_settings = default_settings
            return _c_settings
    _c_settings = default_settings
    return _c_settings

def save_settings(data):
    global _c_settings
    _c_settings = data
    _icon_cache.clear()
    _queue_write(SETTINGS_FILE, lambda: _c_settings)

# ========== قاعدة البيانات ==========
def load_db():
    global _c_db
    if _c_db is not None: return _c_db
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r', encoding='utf-8') as f: 
                _c_db = json.load(f)
                return _c_db
        except Exception: 
            _backup_corrupt(DB_FILE)     # نحتفظ بنسخة من الملف التالف بدل ضياعه
            _c_db = {}
            return _c_db
    _c_db = {}
    return _c_db

def save_db(data):
    global _c_db
    _c_db = data
    _queue_write(DB_FILE, lambda: _c_db)

def load_coupons():
    global _c_coupons
    if _c_coupons is not None: return _c_coupons
    if os.path.exists(COUPONS_FILE):
        try:
            with open(COUPONS_FILE, 'r', encoding='utf-8') as f: 
                _c_coupons = json.load(f)
                return _c_coupons
        except Exception: 
            _c_coupons = {}
            return _c_coupons
    _c_coupons = {}
    return _c_coupons

def save_coupons(data):
    global _c_coupons
    _c_coupons = data
    _queue_write(COUPONS_FILE, lambda: _c_coupons)

# ========== روابط النقاط (رابط دعوة بعدد نقاط وعدد أشخاص محدد، كل شخص مرة وحدة) ==========
import secrets
GIFT_LINKS_FILE = 'gift_links.json'
_c_gifts = None
_gift_lock = threading.Lock()
_coupon_lock = threading.Lock()

def load_gift_links():
    global _c_gifts
    if _c_gifts is not None: return _c_gifts
    if os.path.exists(GIFT_LINKS_FILE):
        try:
            with open(GIFT_LINKS_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
                if isinstance(d, dict):
                    _c_gifts = d
                    return _c_gifts
        except Exception: pass
    _c_gifts = {}
    return _c_gifts

def save_gift_links(data):
    global _c_gifts
    _c_gifts = data
    _queue_write(GIFT_LINKS_FILE, lambda: _c_gifts)

def gift_link_url(code):
    return f"https://t.me/{BOT_USERNAME}?start=p_{code}"

def notify_dev_points_used(kind, code, uid, pts, used, mx):
    """يبلّغ المطور (DEV_ID) كل ما شخص يستخدم كود قسيمة أو رابط نقاط"""
    def _send():
        try:
            name = ''
            username = ''
            try:
                ch = bot.get_chat(uid)
                name = f"{getattr(ch, 'first_name', '') or ''} {getattr(ch, 'last_name', '') or ''}".strip()
                username = getattr(ch, 'username', '') or ''
            except Exception: pass
            name = html.escape(name or 'مستخدم')
            uname = f"@{html.escape(username)}" if username else "لا يوجد"
            bot.send_message(
                DEV_ID,
                f"🔔 <b>استخدام {kind}</b>\n\n"
                f"🔑 الاسم/الكود: <code>{html.escape(str(code))}</code>\n"
                f"👤 الشخص: <a href=\"tg://user?id={uid}\">{name}</a>\n"
                f"📌 المعرف: {uname}\n"
                f"🆔 الآيدي: <code>{uid}</code>\n"
                f"🎁 النقاط: <b>{int(pts):,}</b>\n"
                f"👥 المستخدمين: <b>{used}/{mx}</b>",
                parse_mode='HTML')
        except Exception: pass
    _io_pool.submit(_send)

def claim_gift_link(uid):
    """يستلم العضو نقاط رابط النقاط (إذا فاتح الرابط). مرة وحدة لكل شخص وبحد أقصى لعدد الأشخاص"""
    db = load_db()
    rec = db.get(str(uid))
    if not isinstance(rec, dict): return
    code = rec.pop('gift_pending', None)
    if not code: return
    finished = None
    used_now = None
    with _gift_lock:
        links = load_gift_links()
        info = links.get(code)
        if not isinstance(info, dict):
            msg = "❌ <b>هذا الرابط غير صالح أو تم حذفه.</b>"
        elif int(uid) in info.get('used_by', []):
            msg = "⚠️ <b>سبق واستخدمت هذا الرابط!</b>\n\nكل شخص يكدر ياخذ نقاط الرابط مرة وحدة فقط."
        elif info.get('used', 0) >= info.get('max_uses', 0):
            msg = "❌ <b>انتهى هذا الرابط!</b>\n\nاكتمل عدد الأشخاص المسموح لهم."
        else:
            pts = int(info.get('points', 0))
            rec['balance'] = rec.get('balance', 0) + pts
            _log_tx(rec, pts, 'link', 'رابط نقاط')
            info['used'] = info.get('used', 0) + 1
            info.setdefault('used_by', []).append(int(uid))
            save_gift_links(links)
            msg = f"🎉 <b>مبروك!</b>\n\n🎁 حصلت على <b>{pts:,}</b> نقطة\n💎 رصيدك الآن: <b>{rec['balance']:,}</b> نقطة"
            used_now = (code, pts, info['used'], info.get('max_uses', 0))
            if info['used'] >= info.get('max_uses', 0): finished = (code, pts, info['used'])
    save_db(db)
    try: bot.send_message(uid, msg, parse_mode='HTML')
    except Exception: pass
    if used_now: notify_dev_points_used('رابط نقاط', used_now[0], uid, used_now[1], used_now[2], used_now[3])
    if finished:
        try: bot.send_message(DEV_ID, f"✅ <b>اكتمل رابط نقاط</b>\n\n🔑 الكود: <code>{finished[0]}</code>\n🎁 النقاط: {finished[1]:,}\n👥 المستخدمين: {finished[2]}", parse_mode='HTML')
        except Exception: pass

def get_gift_links_menu():
    links = load_gift_links()
    items = sorted(((c, i) for c, i in links.items() if isinstance(i, dict)), key=lambda x: x[1].get('created_at', 0), reverse=True)[:10]
    text = "🔗 <b>روابط النقاط</b>\n────────────────────\n"
    if not items: text += "لا توجد روابط حالياً.\n"
    markup = types.InlineKeyboardMarkup(row_width=1)
    for n, (code, info) in enumerate(items, start=1):
        used, mx = info.get('used', 0), info.get('max_uses', 0)
        state = "✅ مكتمل" if used >= mx else "🟢 فعّال"
        text += f"\n{n}. <code>{gift_link_url(code)}</code>\n   🎁 {int(info.get('points', 0)):,} نقطة | 👥 {used}/{mx} | {state}\n"
        markup.add(make_btn(f"🗑️ حذف الرابط {n}", callback_data=f"dev_gift_del_{code}", style='danger', icon_key='__nav__'))
    markup.add(make_btn('🔗 إنشاء رابط جديد', callback_data='dev_gift_new', style='success', icon_key='__nav__'))
    markup.add(make_btn('🔙 رجوع للوحة', callback_data='dev_panel', style='primary'))
    return text, markup

def load_fundings():
    global _c_fundings
    if _c_fundings is not None: return _c_fundings
    if os.path.exists(FUNDINGS_FILE):
        try:
            with open(FUNDINGS_FILE, 'r', encoding='utf-8') as f: 
                _c_fundings = json.load(f)
                return _c_fundings
        except Exception: 
            _c_fundings = {}
            return _c_fundings
    _c_fundings = {}
    return _c_fundings

def save_fundings(data):
    global _c_fundings
    _c_fundings = data
    _queue_write(FUNDINGS_FILE, lambda: _c_fundings)

def load_force_sub():
    global _c_force_sub
    if _c_force_sub is not None: return _c_force_sub
    if os.path.exists(FORCE_SUB_FILE):
        try:
            with open(FORCE_SUB_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list): 
                    _c_force_sub = data
                    return _c_force_sub
        except Exception: pass
    _c_force_sub = []
    return _c_force_sub

def save_force_sub(data):
    global _c_force_sub
    _c_force_sub = data
    _queue_write(FORCE_SUB_FILE, lambda: _c_force_sub)

def load_points_channels():
    global _c_points
    if _c_points is not None: return _c_points
    if os.path.exists(POINTS_CHANNELS_FILE):
        try:
            with open(POINTS_CHANNELS_FILE, 'r', encoding='utf-8') as f: 
                _c_points = json.load(f)
                return _c_points
        except Exception: 
            _c_points = []
            return _c_points
    _c_points = []
    return _c_points

def save_points_channels(data):
    global _c_points
    _c_points = data
    _queue_write(POINTS_CHANNELS_FILE, lambda: _c_points)

def load_turbo_channels():
    global _c_turbo
    if _c_turbo is not None: return _c_turbo
    if os.path.exists(TURBO_CHANNELS_FILE):
        try:
            with open(TURBO_CHANNELS_FILE, 'r', encoding='utf-8') as f: 
                _c_turbo = json.load(f)
                return _c_turbo
        except Exception: 
            _c_turbo = []
            return _c_turbo
    _c_turbo = []
    return _c_turbo

def save_turbo_channels(data):
    global _c_turbo
    _c_turbo = data
    _queue_write(TURBO_CHANNELS_FILE, lambda: _c_turbo)

def is_dev(user_id):
    try: return int(user_id) == int(DEV_ID)
    except Exception: return False

def is_banned(user_id):
    if is_dev(user_id): return False
    ud = get_user_data(user_id)
    return ud.get('banned', False)

# ========== سجل نقاط العضو ==========
_TX_ICONS = {'funding': '🚀', 'invite': '🤝', 'gift': '🎁', 'channel': '📺', 'turbo': '⚡',
             'transfer_out': '🔄', 'transfer_in': '🔄', 'coupon': '🎟️', 'link': '🔗', 'admin': '💳'}

def _log_tx(rec, amount, kind, desc=""):
    """يسجل عملية على نقاط العضو داخل سجله (آخر 30 عملية فقط)"""
    try:
        if not isinstance(rec, dict): return
        log = rec.setdefault('tx_log', [])
        log.append({'t': int(time.time()), 'a': int(amount), 'k': kind, 'd': str(desc)[:80]})
        if len(log) > 10: del log[:-10]
        if amount > 0: rec['total_earned'] = rec.get('total_earned', 0) + int(amount)
    except Exception: pass

def _ago_text(ts):
    d = max(0, int(time.time()) - int(ts))
    if d < 60: return "الآن"
    if d < 3600: return f"منذ {d // 60} دقيقة"
    if d < 86400: return f"منذ {d // 3600} ساعة"
    return f"منذ {d // 86400} يوم"

def build_points_log_text(uid):
    u = get_user_data(uid)
    log = [e for e in u.get('tx_log', []) if isinstance(e, dict)][-8:][::-1]
    earned = sum(e['a'] for e in log if e['a'] > 0)
    spent = sum(-e['a'] for e in log if e['a'] < 0)
    t = "💰 <b>سجل نقاطك</b>\n💎 رصيدك الحالي : <b>{:,}</b>\n".format(u.get('balance', 0))
    t += "━━━━━━━━━━━━━━\n"
    t += f"📈 إجمالي ما حصلت عليه : <b>{u.get('total_earned', 0):,}</b>\n"
    t += f"💸 إجمالي ما صرفته : <b>{u.get('total_spent', 0):,}</b>\n"
    t += f"🚀 عدد تمويلاتك : <b>{u.get('active_channels', 0)}</b> | 👥 الأعضاء المطلوبين : <b>{u.get('required_members', 0):,}</b>\n"
    t += f"🤝 دعواتك : <b>{u.get('total_shares', 0)}</b> | 🎁 هداياك : <b>{u.get('total_gifts', 0)}</b>\n"
    t += "━━━━━━━━━━━━━━\n"
    if not log:
        return t + "📭 لا توجد عمليات بعد."
    t += f"📊 آخر {len(log)} عملية · +{earned:,} ➖ {spent:,}\n\n"
    for e in log:
        sign = "+" if e['a'] >= 0 else "-"
        t += f"{_TX_ICONS.get(e.get('k'), '•')} <b>{sign}{abs(e['a']):,}</b> · {html.escape(e.get('d', ''))}\n🕒 {_ago_text(e['t'])}\n\n"
    return t.rstrip()

def get_user_data(user_id):
    db = load_db()
    user_id = str(user_id)
    if user_id not in db or not isinstance(db[user_id], dict): db[user_id] = {}
    defaults = {
        "balance": 0, "streak": 1, "today_subs": 0, "today_spent": 0,
        "total_subs": 0, "total_gifts": 0, "total_transfers": 0,
        "total_shares": 0, "total_spent": 0, "active_channels": 0,
        "required_members": 0, "last_daily": 0, "banned": False,
        "verified_channels": [], "turbo_completed": False
    }
    updated = False
    for k, v in defaults.items():
        if k not in db[user_id]:
            db[user_id][k] = v
            updated = True
    if updated: save_db(db)
    return db[user_id]

def _plain_markup(m):
    """نسخة أزرار بسيطة (بدون إيموجي مميز/ألوان) تُستعمل فقط إذا رفض تلغرام الأزرار الأصلية"""
    try:
        nm = types.InlineKeyboardMarkup()
        for row in m.keyboard:
            nm.row(*[types.InlineKeyboardButton(b.text, url=getattr(b, 'url', None), callback_data=getattr(b, 'callback_data', None),
                                                switch_inline_query=getattr(b, 'switch_inline_query', None)) for b in row])
        return nm
    except Exception:
        return None

def safe_edit(chat_id, message_id, text, reply_markup=None, parse_mode='HTML', disable_preview=None):
    kwargs = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'reply_markup': reply_markup}
    if parse_mode: kwargs['parse_mode'] = parse_mode
    if disable_preview is not None: kwargs['disable_web_page_preview'] = disable_preview
    t0 = time.time()
    for attempt in range(2):
        try:
            bot.edit_message_text(**kwargs); return
        except Exception as ex:
            d = str(ex).lower()
            if 'not modified' in d: return
            print(f"[edit] فشل بعد {time.time() - t0:.1f}ث: {str(ex)[:220]}")
            if attempt == 0 and any(w in d for w in ('timed out', 'timeout', 'connection', 'max retries')):
                continue                                   # مهلة شبكة: نعيد المحاولة مرة وحدة
            if attempt == 0 and getattr(ex, 'error_code', None) == 400 and reply_markup is not None \
                    and any(w in d for w in ('button', 'icon', 'style', 'keyboard', 'emoji', 'markup')):
                pm = _plain_markup(reply_markup)           # تلغرام رفض شكل الأزرار: نرسلها بسيطة بدل ما يتجمد الزر
                if pm is not None:
                    kwargs['reply_markup'] = pm; print("[edit] إعادة المحاولة بأزرار بسيطة"); continue
            return

def cancel_check(message):
    if not message.text: return False
    txt = message.text.strip().lower()
    return txt in ['/start', 'الغاء', 'إلغاء', 'cancel', 'إلغاء العملية', 'الغاء العملية']

DEFAULT_NEW_USER_TEMPLATE = (
    "📌 دخل عضو جديد للبوت\n"
    "📌 الاسم : {name}\n"
    "📌 المعرف : {username}\n"
    "📌 الايدي : {id}\n"
    "📌 عدد المشتركين : {count}"
)

def render_new_user_message(user_obj, total_users, template=None):
    tpl = template or load_settings().get('new_user_template') or DEFAULT_NEW_USER_TEMPLATE
    full_name = f"{user_obj.first_name or ''} {getattr(user_obj, 'last_name', None) or ''}".strip() or "مستخدم"
    vals = {
        'name': html.escape(full_name),
        'username': f"@{html.escape(user_obj.username)}" if user_obj.username else "لا يوجد",
        'id': str(user_obj.id),
        'count': str(total_users),
    }
    # استبدال بمرور واحد حتى لا يتأثر النص إذا كان اسم العضو نفسه يحتوي {id} مثلاً
    return re.sub(r'\{(name|username|id|count)\}', lambda m: vals[m.group(1)], tpl)

def notify_new_user_to_dev(user_obj, total_users):
    try:
        bot.send_message(DEV_ID, render_new_user_message(user_obj, total_users), parse_mode='HTML', disable_web_page_preview=True)
    except Exception:
        try: bot.send_message(DEV_ID, render_new_user_message(user_obj, total_users, DEFAULT_NEW_USER_TEMPLATE), parse_mode='HTML', disable_web_page_preview=True)
        except Exception: pass

_register_lock = threading.Lock()

def register_user_if_new(user_obj):
    """يسجّل أي عضو جديد فوراً (من /start أو أي زر أو أي رسالة) ويبلغ المطور. يرجع True إذا كان جديداً."""
    if user_obj is None or getattr(user_obj, 'is_bot', False): return False
    key = str(user_obj.id)
    with _register_lock:
        db = load_db()
        if isinstance(db.get(key), dict): return False
        db[key] = {
            "balance": 0, "streak": 1, "today_subs": 0, "today_spent": 0,
            "total_subs": 0, "total_gifts": 0, "total_transfers": 0,
            "total_shares": 0, "total_spent": 0, "active_channels": 0,
            "required_members": 0, "last_daily": 0, "banned": False,
            "verified_channels": [], "turbo_completed": False,
            "captcha_ok": False, "welcomed": False
        }
        save_db(db)
        total = len(db)
    _io_pool.submit(notify_new_user_to_dev, user_obj, total)
    request_backup()
    return True

def claim_referral(uid, user_name):
    """يمنح مكافأة الدعوة للداعي (مرة واحدة) بعد اجتياز الاشتراك الإجباري"""
    db = load_db()
    rec = db.get(str(uid))
    if not isinstance(rec, dict): return
    inviter_id = rec.pop('ref_pending', None)
    if inviter_id is None: return
    inviter = db.get(str(inviter_id))
    if not isinstance(inviter, dict) or int(inviter_id) == int(uid):
        save_db(db); return
    reward = load_settings().get('ref_reward', 5000)
    inviter_data = get_user_data(inviter_id)
    inviter_data['balance'] += reward
    _log_tx(inviter_data, reward, 'invite', f"دعوة : {user_name or 'مستخدم'}")
    inviter_data['total_shares'] += 1
    save_db(load_db())
    safe_name = html.escape(user_name or "مستخدم")
    name_link = f'<a href="tg://user?id={int(uid)}">{safe_name}</a>'
    try: bot.send_message(inviter_id, f"🎉 هنيئاً لك! قام {name_link} بالانضمام عبر رابط الدعوة الخاص بك\nوقد حصلت على <b>{reward}</b> نقطة\n💥 قم بمشاركة رابط الدعوة للمزيد من الأصدقاء", parse_mode='HTML', disable_web_page_preview=True)
    except Exception: pass
    try: bot.send_message(uid, f"🎊 <b>أهلاً بك!</b>\n\nلقد انضممت عبر رابط صديقك، وقد حصل هو على <b>{reward} نقطة</b> كمكافأة لدعوتك! 🎁✨", parse_mode='HTML')
    except Exception: pass

# ========== الاشتراك الإجباري ==========
def parse_chat_identifier_and_url(raw_input):
    s = _clean_text(raw_input or '')
    if not s: return None, None
    chat_id_or_user = None
    url = None
    if s.startswith('https://t.me/') or s.startswith('http://t.me/') or s.startswith('t.me/'):
        clean = s.replace('https://', '').replace('http://', '')
        slug = clean.split('t.me/', 1)[1].strip('/').split('?')[0]
        url = 'https://' + clean if not s.startswith('http') else s
        if slug.startswith('+') or slug.startswith('joinchat/'): chat_id_or_user = None
        else: chat_id_or_user = '@' + slug
    elif s.startswith('@'):
        uname = s[1:].strip()
        chat_id_or_user = '@' + uname
        url = f"https://t.me/{uname}"
    else:
        try: chat_id_or_user = int(s)
        except Exception:
            chat_id_or_user = '@' + s
            url = f"https://t.me/{s}"
    return chat_id_or_user, url

def safe_get_chat(target):
    if target is None: return None
    try: return bot.get_chat(target)
    except Exception: return None

_joined_cache = {}
JOIN_CACHE_TTL = 300

_last_fsub_alert = {}

def _alert_dev_fsub_problem(chat_target, ex):
    """ينبه المطور (مرة كل ساعة لكل قناة) إذا البوت ما يكدر يفحص القناة"""
    try:
        k = str(chat_target)
        if time.time() - _last_fsub_alert.get(k, 0) < 3600: return
        _last_fsub_alert[k] = time.time()
        _io_pool.submit(lambda: bot.send_message(
            DEV_ID,
            f"⚠️ <b>البوت لا يستطيع فحص الاشتراك في:</b> <code>{html.escape(k)}</code>\n"
            f"<code>{html.escape(str(ex)[:200])}</code>\n\n"
            "تأكد أن البوت <b>مشرف (Admin)</b> في القناة/الكروب. إلى أن يتصلح، المستخدمين راح يُمنعون (لأن الفحص ما يشتغل).",
            parse_mode='HTML'))
    except Exception: pass

def is_user_joined_chat(chat_target, user_id):
    """True فقط إذا العضو موجود فعلاً. أي فشل بالفحص = غير مشترك (بدل ما نسمح للكل)"""
    if chat_target is None: return True
    last_ex = None
    for attempt in range(2):
        try:
            member = bot.get_chat_member(chat_target, int(user_id))
            status = getattr(member, 'status', '')
            if status in ('creator', 'administrator', 'member'): return True
            if status == 'restricted': return bool(getattr(member, 'is_member', True))
            return False   # left / kicked
        except Exception as ex:
            last_ex = ex
            d = str(ex).lower()
            if 'participant_id_invalid' in d or 'user not found' in d:
                return False
            time.sleep(0.3)
    _alert_dev_fsub_problem(chat_target, last_ex)
    return False

def is_bot_admin_in_chat(chat_target):
    if chat_target is None: return False
    try:
        member = bot.get_chat_member(chat_target, BOT_ID)
        status = getattr(member, 'status', '')
        return status in ('administrator', 'creator')
    except Exception:
        return False

# كاش الاشتراك الإجباري: بدل ما نسأل تلغرام عن كل قناة مع كل ضغطة زر (هذا كان سبب البطء الأساسي)،
# نحفظ نتيجة "مشترك" لهذه المدة. زر "تحقق من الاشتراك" يتجاوز الكاش دائماً.
FSUB_CACHE_SECONDS = 120
_fsub_ok_cache = {}
def _fsub_ttl():
    return 300 if _fast_mode() else FSUB_CACHE_SECONDS   # وضع التسريع: نفحص الاشتراك كل 5 دقائق بدل دقيقتين

def get_unjoined_force_subs(user_id, fresh=False):
    """fresh=True يتجاوز الكاش (زر التحقق). الكاش يحفظ فقط نتيجة (مشترك) لمدة FSUB_CACHE_SECONDS"""
    if is_dev(user_id): return []
    items = [i for i in load_force_sub() if isinstance(i, dict) and i.get('target') is not None]
    if not items: return []
    now = time.time(); uid_i = int(user_id)
    if fresh: to_check = items
    else: to_check = [i for i in items if now - _fsub_ok_cache.get((uid_i, str(i['target'])), 0) > _fsub_ttl()]
    if not to_check: return []
    if len(to_check) == 1: results = [is_user_joined_chat(to_check[0]['target'], user_id)]
    else: results = list(_io_pool.map(lambda i: is_user_joined_chat(i['target'], user_id), to_check))
    if len(_fsub_ok_cache) > 30000:
        _cut = now - _fsub_ttl()
        for k in [k for k, t in list(_fsub_ok_cache.items()) if t < _cut]: _fsub_ok_cache.pop(k, None)
    for item, ok in zip(to_check, results):
        k = (uid_i, str(item['target']))
        if ok: _fsub_ok_cache[k] = now
        else: _fsub_ok_cache.pop(k, None)
    return [item for item, ok in zip(to_check, results) if not ok]

def build_force_sub_prompt(missing_items):
    text = "للمتابعة، يرجى الاشتراك في القنوات التالية:"
    markup = types.InlineKeyboardMarkup(row_width=1)
    for item in missing_items:
        title = item.get('title') or item.get('url') or str(item.get('target'))
        url = item.get('url')
        if url: markup.add(make_btn(title, url=url, style='success'))
    markup.add(make_btn("تحقق من الاشتراك ✳️", callback_data='check_force_sub', style='primary'))
    return text, markup

def enforce_force_sub_message(message):
    missing = get_unjoined_force_subs(message.from_user.id)
    if not missing: return True
    text, markup = build_force_sub_prompt(missing)
    bot.send_message(message.chat.id, text, reply_markup=markup, disable_web_page_preview=True)
    return False

def enforce_force_sub_callback(call):
    if call.data == 'check_force_sub': return True
    missing = get_unjoined_force_subs(call.from_user.id)
    if not missing: return True
    text, markup = build_force_sub_prompt(missing)
    try: bot.answer_callback_query(call.id, "⚠️ يجب الاشتراك في جميع القنوات والكروبات أولاً!", show_alert=True)
    except Exception: pass
    safe_edit(call.message.chat.id, call.message.message_id, text, markup, 'HTML', True)
    return False

# ========== دوال التقدم ==========
import unicodedata

_AR_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')

def _clean_text(s):
    """يحوّل أي خط مزخرف (𝗙𝗗_𝗖𝗤 / ＦＤ / ⓕ ...) إلى حروف عادية ويشيل الرموز المخفية والتشكيل"""
    s = unicodedata.normalize('NFKC', s or '').translate(_AR_DIGITS)
    return ''.join(ch for ch in s if ch in ' \n' or unicodedata.category(ch) not in ('Cf', 'Cc', 'Mn', 'Me')).strip()

_RE_TME = re.compile(r'(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:s/)?(\+[\w\-]+|joinchat/[\w\-]+|[A-Za-z][A-Za-z0-9_]{3,31})', re.I)
_RE_AT = re.compile(r'@([A-Za-z][A-Za-z0-9_]{3,31})')
_RE_ID = re.compile(r'(?<![\w])-100\d{6,}(?![\w])')

def extract_channel_target(message):
    """يطلع القناة من رسالة العضو بأي شكل: رابط / @يوزر / يوزر بدون @ / آيدي رقمي / خط مزخرف /
    رابط مخفي داخل النص / توجيه (Forward) من القناة.
    يرجع ('chat', target) أو ('private', None) لرابط دعوة خاص أو None إذا ما فهم."""
    for src in (getattr(message, 'forward_from_chat', None), getattr(getattr(message, 'forward_origin', None), 'chat', None)):
        if src is not None and getattr(src, 'id', None): return ('chat', int(src.id))
    cands = []
    ents = getattr(message, 'entities', None) or getattr(message, 'caption_entities', None) or []
    for e in ents:
        u = getattr(e, 'url', None)
        if u: cands.append(u)
    cands.append(message.text or getattr(message, 'caption', None) or '')
    private = False
    for raw in cands:
        s = _clean_text(raw)
        if not s: continue
        m = _RE_ID.search(s)
        if m: return ('chat', int(m.group(0)))
        m = _RE_TME.search(s)
        if m:
            slug = m.group(1)
            if slug.startswith('+') or slug.lower().startswith('joinchat/'): private = True; continue
            return ('chat', '@' + slug)
        m = _RE_AT.search(s)
        if m: return ('chat', '@' + m.group(1))
        one = re.sub(r'\s+', '', s)
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', one): return ('chat', '@' + one)
    return ('private', None) if private else None

def normalize_chat_target(channel_str):
    if isinstance(channel_str, int): return channel_str
    s = (channel_str or '').strip()
    if re.fullmatch(r'-?\d{5,}', s): return int(s)
    if s.startswith('https://t.me/') or s.startswith('http://t.me/'):
        slug = s.split('t.me/', 1)[1].strip('/').split('?')[0]
        if slug and not slug.startswith('+') and not slug.startswith('joinchat/'): return '@' + slug
    return s

def resolve_channel_title_and_count(channel_str, need_title=True):
    target = normalize_chat_target(channel_str)
    title = channel_str.strip() if need_title else None
    member_count = None
    if target:
        if need_title:
            try:
                chat_obj = bot.get_chat(target)
                if getattr(chat_obj, 'title', None): title = chat_obj.title
            except Exception: pass
        try:
            cnt = bot.get_chat_member_count(target)
            if cnt is not None and int(cnt) >= 0: member_count = int(cnt)
        except Exception: pass
    return title, member_count

def build_progress_bar(percent):
    pct = max(0, min(100, int(percent)))
    filled = pct // 10
    return ("■" * filled) + ("□" * (10 - filled)) + f" {pct}%"

def fetch_live_channel(order):
    """يجلب عدد الأعضاء الحالي (والاسم فقط إذا غير محفوظ) — يُستدعى بالتوازي من المراقب"""
    try: return resolve_channel_title_and_count(order.get('channel', ''), need_title=not order.get('channel_title'))
    except Exception: return (None, None)

def compute_real_progress(order, live=None, fetch=True):
    target_count = max(1, int(order.get('count', 100)))
    if live is not None: title, live_count = live
    elif fetch: title, live_count = resolve_channel_title_and_count(order.get('channel', ''), need_title=not order.get('channel_title'))
    else: title, live_count = None, None
    if title: order['channel_title'] = title
    start_members = order.get('start_members')
    if live_count is not None:
        order['last_members'] = live_count
        # نعتمد أعلى عدد وصلته القناة: إذا طلع عضو بعد الوصول للهدف ما يرجع التمويل للخلف
        order['peak_members'] = max(int(order.get('peak_members') or 0), int(live_count))
        if start_members is None:
            order['start_members'] = live_count
            start_members = live_count
    ref = order.get('peak_members')
    if ref is None: ref = order.get('last_members')
    added = max(0, int(ref) - int(start_members)) if (start_members is not None and ref is not None) else 0
    # سماح الإكمال: بعض الأعضاء يطلعون من القناة فيبقى التمويل عالق (مثلاً 99/100)
    try: tol_pct = int(load_settings().get('complete_tolerance', 1))
    except Exception: tol_pct = 1
    tol = max(1, -((-target_count * tol_pct) // 100)) if tol_pct > 0 else 0
    done_flag = added >= target_count - tol
    shown_count = target_count if done_flag else min(target_count, int(added))
    percent = min(100, int((shown_count / target_count) * 100))
    return {
        "title": order.get('channel_title') or order.get('channel'),
        "shown_count": shown_count, "target_count": target_count,
        "percent": percent, "bar": build_progress_bar(percent),
        "completed": shown_count >= target_count
    }

def format_start_funding_message(channel_title, count, cost, remaining_balance):
    return (
        f"🚀 <b>تم بدء التمويل بنجاح!</b>\n\n"
        f"📌 <b>{channel_title}</b>\n"
        f"👥 الأعضاء المطلوب: <b>{count:,}</b>\n"
        f"🪙 تم خصم: <b>{cost:,}</b> نقطة\n"
        f"💎 رصيدك الحالي: <b>{remaining_balance:,}</b> نقطة\n"
        f"────────────────────\n"
        f"⚠️ تنبيه: لا تطرد البوت أو تنزله من الإدارة حتى انتهاء التمويل، وإلا سيتم إيقاف القناة."
    )

def format_progress_message(prog):
    return (
        f"📈 <b>تقدّم التمويل</b>\n"
        f"────────────────────\n"
        f"📌 <b>{prog['title']}</b>\n"
        f"👥 <b>{prog['shown_count']} / {prog['target_count']}</b>\n"
        f"{prog['bar']}"
    )

# ========== رسالة إكمال الطلب (قناة إكمال الطلبات) ==========
def build_completion_message(order):
    channel_name = html.escape(str(order.get('channel_title') or 'قناة غير معروفة'))
    user_id = order.get('user_id')
    count = order.get('count')
    line = '━━━━━━━━━━━━━━━━━━'
    return (
        f"🔔 طلب تمويل جديد تم إكماله بنجاح ✅\n\n"
        f"⚙️ المعلومات الإحصائية للعملية:\n"
        f"{line}\n"
        f"<blockquote>"
        f"📦 القناة ⇐ {channel_name}\n"
        f"🆔 الآيدي ⇐ <a href=\"tg://user?id={user_id}\">{user_id}</a>\n"
        f"🎯 الخدمة ⇐ قناة\n"
        f"📊 العدد الحالي ⇐ {count} عضو\n"
        f"✨ الحالة ⇐ مكتمل بنسبة 100%"
        f"</blockquote>"
        f"{line}"
    )

def post_completion(order):
    comp = load_settings().get('completion_channel')
    if not comp: raise ValueError("قناة إكمال الطلبات غير محددة")
    markup = types.InlineKeyboardMarkup()
    # زر "اطلب الان" يفتح البوت
    markup.add(make_btn('اطلب الان ✅', url=f"https://t.me/{BOT_USERNAME}", style='success'))
    bot.send_message(comp, build_completion_message(order), reply_markup=markup, parse_mode='HTML')

# ========== مراقب التقدم ==========
_warned_no_comp = False

def progress_watcher_loop():
    while True:
        try:
            fundings = load_fundings()
            changed = False
            to_notify = []
            to_channel = []
            settings = load_settings()
            comp_channel = settings.get('completion_channel')

            active_orders = [(oid, o) for oid, o in list(fundings.items())
                             if isinstance(o, dict) and o.get('status') == 'active' and not o.get('completed_notified')]
            lives = list(_watch_pool.map(lambda it: fetch_live_channel(it[1]), active_orders)) if active_orders else []
            for (order_id, order), live in zip(active_orders, lives):
                if order.get('start_members') is None:
                    init_cnt = live[1]
                    if init_cnt is not None:
                        order['start_members'] = init_cnt
                        order['last_members'] = init_cnt
                        changed = True
                    continue
                prog = compute_real_progress(order, live=live)
                last_reported = int(order.get('last_reported_count', 0))
                current_shown = int(prog['shown_count'])
                if current_shown > last_reported or prog['completed']:
                    order['last_reported_count'] = current_shown
                    changed = True
                    if prog['completed']:
                        order['completed_notified'] = True
                        order['status'] = 'completed'
                        order['channel_posted'] = False   # بانتظار النشر بقناة إكمال الطلبات
                    to_notify.append((int(order['user_id']), format_progress_message(prog), prog['completed']))
            if changed: save_fundings(fundings)

            for target_uid, msg_text, is_done in to_notify:
                try:
                    final_txt = msg_text
                    if is_done: final_txt += "\n────────────────────\n🎉 <b>خلص تمويلك!</b> اكتمل التمويل المطلوب بنجاح ✅"
                    bot.send_message(target_uid, final_txt, parse_mode='HTML')
                except Exception: pass

            # نشر الطلبات المكتملة في قناة إكمال الطلبات (مع إعادة المحاولة عند الفشل)
            posted_changed = False
            for oid, order in list(fundings.items()):
                if not isinstance(order, dict) or order.get('channel_posted') is not False: continue
                if not comp_channel:
                    global _warned_no_comp
                    if not _warned_no_comp:
                        _warned_no_comp = True
                        try: bot.send_message(DEV_ID, "⚠️ اكتمل طلب تمويل لكن <b>قناة إكمال الطلبات غير محددة</b>. حددها من لوحة المطور وراح ينشر الطلبات المنتظرة تلقائياً.", parse_mode='HTML')
                        except Exception: pass
                    break
                if int(order.get('post_tries', 0)) >= 5: continue
                try:
                    post_completion(order)
                    order['channel_posted'] = True
                except Exception as ex:
                    order['post_tries'] = int(order.get('post_tries', 0)) + 1
                    print(f"[completion-channel] فشل النشر: {ex}")
                    if order['post_tries'] == 1:
                        try:
                            bot.send_message(DEV_ID, f"⚠️ <b>تعذّر نشر طلب مكتمل في قناة إكمال الطلبات</b>\n\n<code>{html.escape(str(ex)[:300])}</code>\n\nتأكد أن البوت <b>مشرف</b> في القناة وعنده صلاحية نشر الرسائل، وأن معرف القناة صحيح.", parse_mode='HTML')
                        except Exception: pass
                posted_changed = True
            if posted_changed: save_fundings(fundings)
        except Exception: pass
        time.sleep(10)

# ========== المتجر (شحن النقاط / مشتركين إجباري) ==========
from urllib.parse import quote as _urlquote

SHOP_LISTS = {
    'charge_stars': {'kind': 'points', 'pay': 'star', 'parent': 'charge', 'prefix': 'buy_points_star_', 'icon': 'شحن # نقطة = #',
                     'title': '⭐ <b>شحن النقاط بنجوم تيليجرام</b>', 'items': [(320, 1), (1600, 5), (3500, 10), (5500, 15), (10000, 30), (19999, 50), (42999, 100), (100000, 250), (250000, 500)]},
    'charge_balance': {'kind': 'points', 'pay': 'bal', 'parent': 'charge', 'prefix': 'buy_points_bal_', 'icon': 'شحن # نقطة = # $',
                       'title': '💵 <b>شحن النقاط بالرصيد (دولار)</b>', 'items': [(20000, 1), (40000, 2), (60000, 3), (100000, 5), (200000, 10)]},
    'members_stars': {'kind': 'member', 'pay': 'star', 'parent': 'mandatory', 'prefix': 'buy_member_star_', 'icon': '# مشترك = #',
                      'title': '⭐ <b>مشتركين إجباري بالنجوم</b>', 'items': [(400, 250), (800, 500), (1200, 750), (1600, 1000), (2000, 1250), (2400, 1500), (3200, 2000), (4800, 3000)]},
    'members_balance': {'kind': 'member', 'pay': 'bal', 'parent': 'mandatory', 'prefix': 'buy_member_bal_', 'icon': '# مشترك = # $',
                        'title': '💵 <b>مشتركين إجباري بالرصيد (دولار)</b>', 'items': [(400, 5), (800, 10), (1200, 15), (1600, 20), (2000, 25), (2400, 30), (3200, 40), (4800, 60)]},
}
SHOP_LINE = '━━━━━━━━━━━━━━'

def add_nav_row(markup, back_cb=None):
    """صف تنقل موحّد: رجوع + الرئيسية"""
    row = []
    if back_cb: row.append(make_btn('🔙 رجوع', callback_data=back_cb, style='primary'))
    row.append(make_btn('🏠 الرئيسية', callback_data='back_to_start', style='danger'))
    markup.row(*row)

def show_shop_menu(chat_id, msg_id, uid, cd):
    bal = get_user_data(uid).get('balance', 0)
    markup = types.InlineKeyboardMarkup(row_width=1)
    if cd == 'charge':
        text = f"⭐ <b>شحن النقاط</b>\n{SHOP_LINE}\n💎 رصيدك الحالي: <b>{bal:,}</b> نقطة\n\nاختر طريقة الدفع المناسبة لك 👇"
        markup.add(make_btn('⭐ شحن بالنجوم', callback_data='charge_stars', style='primary'))
        markup.add(make_btn('💵 شحن بالرصيد (دولار)', callback_data='charge_balance', style='success'))
        add_nav_row(markup)
    elif cd == 'mandatory':
        text = f"🛒 <b>المتجر — مشتركين إجباري</b>\n{SHOP_LINE}\nزوّد قناتك أو مجموعتك بمشتركين حقيقيين.\n\nاختر طريقة الدفع المناسبة لك 👇"
        markup.add(make_btn('⭐ مشتركين إجباري (نجوم)', callback_data='members_stars', style='primary'))
        markup.add(make_btn('💵 مشتركين إجباري (رصيد)', callback_data='members_balance', style='success'))
        add_nav_row(markup)
    else:
        cfg = SHOP_LISTS[cd]
        unit = '⭐' if cfg['pay'] == 'star' else '$'
        what = 'نقطة' if cfg['kind'] == 'points' else 'مشترك'
        head = f"💎 رصيدك الحالي: <b>{bal:,}</b> نقطة\n" if cfg['kind'] == 'points' else ""
        text = f"{cfg['title']}\n{SHOP_LINE}\n{head}اختر الباقة المناسبة 👇"
        if cd == 'charge_stars':
            text = f"{cfg['title']}\n{SHOP_LINE}\n{head}اضغط على الباقة وادفع بالنجوم، والنقاط توصلك <b>تلقائياً</b> ✅"
            markup.row(make_btn('السعر', callback_data='shop_noop', style='danger'), make_btn('السلعة', callback_data='shop_noop', style='danger'))
            for amount, price in cfg['items']:
                link = get_cached_stars_link(amount)   # من الكاش فقط (بدون انتظار API)
                item_txt = f"💎 {amount:,} نقطة"
                price_txt = f"⭐ {price:,}"
                if link: markup.row(make_btn(price_txt, url=link, style='primary'), make_btn(item_txt, url=link, style='primary'))
                else: markup.row(make_btn(price_txt, callback_data=f'paystar_{amount}', style='primary'), make_btn(item_txt, callback_data=f'paystar_{amount}', style='primary'))
        else:
            for amount, price in cfg['items']:
                markup.add(make_btn(f"🛒 {amount:,} {what} = {price:,} {unit}", callback_data=f"{cfg['prefix']}{amount}", style='success', icon_key=cfg['icon']))
        add_nav_row(markup, cfg['parent'])
    safe_edit(chat_id, msg_id, text, markup, 'HTML')

def show_shop_item(chat_id, msg_id, cd):
    parts = cd.split('_')   # buy_points_star_320
    kind = 'points' if parts[1] == 'points' else 'member'
    pay = parts[2]
    amount = int(parts[3])
    list_key = {('points', 'star'): 'charge_stars', ('points', 'bal'): 'charge_balance',
                ('member', 'star'): 'members_stars', ('member', 'bal'): 'members_balance'}[(kind, pay)]
    cfg = SHOP_LISTS[list_key]
    price = dict(cfg['items']).get(amount)
    unit_txt = f"{price:,} ⭐ نجمة" if pay == 'star' else f"{price:,} $ دولار"
    if kind == 'points':
        title = "🛒 <b>طلب شحن نقاط</b>"
        item_line = f"💎 الباقة: <b>{amount:,}</b> نقطة"
        wish = f"أريد شراء باقة {amount} نقطة بسعر {price} " + ('نجمة' if pay == 'star' else 'دولار')
    else:
        title = "🚀 <b>طلب مشتركين إجباري</b>"
        item_line = f"👥 الباقة: <b>{amount:,}</b> مشترك"
        wish = f"أريد شراء باقة {amount} مشترك بسعر {price} " + ('نجمة' if pay == 'star' else 'دولار')
    text = (f"{title}\n{SHOP_LINE}\n{item_line}\n💳 السعر: <b>{unit_txt}</b>\n{SHOP_LINE}\n"
            f"1️⃣ اضغط «مراسلة المطور» (طلبك يكون جاهز بالرسالة)\n"
            f"2️⃣ أرسل الرسالة وأتمّ الدفع حسب تعليماته\n"
            f"3️⃣ يوصلك طلبك بأقرب وقت ✅\n\n👤 المطور: {html.escape(DEV_USERNAME)}")
    markup = types.InlineKeyboardMarkup(row_width=1)
    if kind == 'points' and pay == 'star':
        text = (f"{title}\n{SHOP_LINE}\n{item_line}\n💳 السعر: <b>{unit_txt}</b>\n{SHOP_LINE}\n"
                f"اضغط الزر بالأسفل وادفع بالنجوم، والنقاط توصلك <b>تلقائياً</b> فوراً بعد الدفع ✅")
        pay_btn = None
        link = get_cached_stars_link(amount)
        if link: pay_btn = make_btn(f'⭐ ادفع {price:,} نجمة', url=link, style='success')
        markup.add(pay_btn or make_btn(f'⭐ ادفع {price:,} نجمة', callback_data=f'paystar_{amount}', style='success'))
    else:
        markup.add(make_btn('👤 مراسلة المطور', url=f"https://t.me/{DEV_USERNAME.lstrip('@')}?text={_urlquote(wish)}", style='success'))
    add_nav_row(markup, list_key)
    safe_edit(chat_id, msg_id, text, markup, 'HTML')

_invoice_link_cache = {}   # روابط الفواتير تُنشأ مرة وحدة لكل باقة ونسبة مكافأة

def create_stars_invoice_link(uid, amount):
    """رابط فاتورة: عند الضغط عليه تفتح نافذة الدفع مباشرة بدون إرسال رسالة"""
    price = dict(SHOP_LISTS['charge_stars']['items']).get(amount)
    if not price: return None
    desc = f"شحن {amount:,} نقطة"
    key = (int(amount), int(price))
    link = _invoice_link_cache.get(key)
    if link: return link
    link = bot.create_invoice_link(title="شحن النقاط", description=desc, payload=f"pts_{amount}",
                                   provider_token="", currency="XTR", prices=[types.LabeledPrice(label=desc, amount=int(price))])
    _invoice_link_cache[key] = link
    return link

_warm_state = {'last': 0}

def get_cached_stars_link(amount):
    """يرجع الرابط من الكاش فوراً. إذا ناقص يجهّزه بالخلفية (مرة كل دقيقة بالكثير) ويرجع None"""
    price = dict(SHOP_LISTS['charge_stars']['items']).get(amount)
    link = _invoice_link_cache.get((int(amount), int(price))) if price else None
    if not link and time.time() - _warm_state['last'] > 60:
        _warm_state['last'] = time.time()
        threading.Thread(target=warm_stars_links, daemon=True).start()
    return link

def warm_stars_links():
    """يجهّز روابط فواتير النجوم بالخلفية حتى تنفتح قائمة الشحن فوراً"""
    for amount, _p in SHOP_LISTS['charge_stars']['items']:
        try: create_stars_invoice_link(0, amount)
        except Exception as e: print(f"[warm links] {e}")

threading.Thread(target=warm_stars_links, daemon=True).start()

def send_stars_invoice(chat_id, uid, amount):
    price = dict(SHOP_LISTS['charge_stars']['items']).get(amount)
    if not price: return False
    desc = f"شحن {amount:,} نقطة"
    bot.send_invoice(chat_id, title="شحن النقاط", description=desc, invoice_payload=f"pts_{amount}_{uid}",
                     provider_token="", currency="XTR", prices=[types.LabeledPrice(label=desc, amount=int(price))])
    return True

# ========== القائمة الرئيسية ==========
def get_welcome_text():
    """رسالة الترحيب/الشروط (نفس الشكل بالصورة). الإيموجيات تتحول لمميزة تلقائياً إذا ضبطتها من لوحة المطور"""
    bn = html.escape(load_settings().get('bot_name', 'بوت تمويل سيد'))
    return (
        f"خطوتك الأولى مع {bn} ✋\n\n"
        "🟩 موافقتك الذهبية\n"
        "عند البدء باستخدام البوت، فأنت تمنحنا ثقتك... وفي المقابل نمنحك خدماتنا. وقبولك للشروط هو مفتاحك للحصول على تمويل سريع و آمن ✅\n\n"
        "🛑 المحظورات والقواعد الأساسية\n\n"
        "المحتوى الممنوع تمويله\n\n"
        "‼️ يُمنع منعاً باتاً تمويل أو الترويج لأي محتوى يتضمن أو يندرج ضمن الآتي:\n\n"
        "العنف , الإرهاب , المواد الإباحية , الاحتيال , قناة بوتات , الكراهية بجميع أشكالها , المحتوى الطائفي...\n\n"
        "🔗 دقة الرابط\n"
        "يجب تزويد البوت برابط صحيح ونهائي للقناة أو المحتوى.\n"
        "أي خطأ في الرابط يؤدي إلى خسارة التمويل دون الأسترداد.\n\n"
        "✨ شكراً لالتزامك!\n\n"
        f"نتمنى لك تجربة تمويل احترافية ونتائج سريعة مع {bn}.\n"
        "معاً نحو نمو أسرع وأكثر ثباتاً 🚀"
    )

def get_main_menu(user_name, user_id):
    user_data = get_user_data(user_id)
    balance = user_data.get('balance', 0)
    settings = load_settings()
    safe_user_name = html.escape(user_name or "مستخدم")
    
    # استرجاع الكليشة المخصصة أو الافتراضية
    custom_template = settings.get('bot_template', '')
    if custom_template:
        welcome_text = _render_template(custom_template, safe_user_name, user_id)
    else:
        bot_name = html.escape(settings.get('bot_name', 'بوت تمويل سيد'))
        welcome_text = f"""
مرحباً {safe_user_name} 👋🔥
• إنـت حالياً داخل {bot_name}
~ المكان الأسهل للحصول على نقاط وتمويل لقناتك

🤖 داخل البوت تذكر:
① تربح نقاط من الاشتراك بالقنوات 💎
② تشارك رابط الدعوة وتضاعف رصيدك 💰
③ تستلم هديتك اليومية 🎁

• ID أيـديـك: <code>{user_id}</code>
"""
    
    # استرجاع إيموجيات الأزرار المخصصة
    emojis = settings.get('btn_emojis', {})
    
    # دالة مساعدة لبناء النص مع الإيموجي المخصص
    def btn_txt(key, default_txt):
        return default_txt  # الإيموجيات تُطبَّق الآن تلقائياً داخل make_btn

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(make_btn(btn_txt('balance', f'💎 رصيدك: {balance}'), callback_data='balance', style='primary'))
    markup.row(make_btn(btn_txt('fund', '🚀 تمويل قناتك أو مجموعتك'), callback_data='fund', style='primary'))
    markup.row(make_btn('📊 قسم مشاهدات وتفاعلات للقنوات', callback_data='eng_menu', style='primary'))
    markup.row(make_btn(btn_txt('tasks', '📅 المهام'), callback_data='tasks', style='danger'), make_btn(btn_txt('collect', '➕ تجميع النقاط'), callback_data='collect', style='danger'))
    markup.row(make_btn(btn_txt('transfer', '💸 تحويل النقاط'), callback_data='transfer', style='danger'), make_btn(btn_txt('charge', '⭐ شحن النقاط'), callback_data='charge', style='success'))
    markup.row(make_btn(btn_txt('info', '📋 معلومات حسابك'), callback_data='info', style='danger'))
    markup.row(make_btn(btn_txt('invite_link', '🤝 ادع صديقاً'), switch_inline_query='', style='primary', icon_key='ادع صديقاً'), make_btn(btn_txt('coupons', '🎟️ قسائمي'), callback_data='coupons', style='primary'))
    
    comp_channel = settings.get('completion_channel', '@ax_bq')
    comp_url = f"https://t.me/{comp_channel.replace('@', '')}" if comp_channel else "https://t.me/ax_bq"
    
    markup.row(make_btn('NEW إكتمال الطلبات', url=comp_url, style='primary'), make_btn('⚡ اشتر مضاعف نقاط', callback_data='multiplier', style='primary'))
    markup.row(make_btn('🎁 هديتك اليوميه', callback_data='daily', style='primary'))
    markup.row(make_btn('🛑 شروط الاستخدام', callback_data='terms', style='danger'))
    markup.row(make_btn('⭐ مشتركين اجباري', callback_data='mandatory', style='success'))
    if is_dev(user_id): markup.row(make_btn('🛠️ لوحة المطور', callback_data='dev_panel', style='primary'))
    return welcome_text, markup

# ========== لوحة المطور ==========
def get_dev_panel_data():
    db = load_db()
    settings = load_settings()
    fundings = load_fundings()
    force_items = load_force_sub()
    f_channels = sum(1 for x in force_items if isinstance(x, dict) and x.get('kind') == 'channel')
    f_groups = sum(1 for x in force_items if isinstance(x, dict) and x.get('kind') == 'group')
    active_fundings = sum(1 for f in fundings.values() if isinstance(f, dict) and f.get('status') == 'active')
    total_users = len(db)
    total_balance = 0; total_subs = 0; total_shares = 0; banned_count = 0
    for u in db.values():
        if isinstance(u, dict):
            total_balance += u.get('balance', 0)
            total_subs += u.get('total_subs', 0)
            total_shares += u.get('total_shares', 0)
            if u.get('banned', False): banned_count += 1
    coupons = load_coupons()
    min_fund = settings.get('min_fund', 100); max_fund = settings.get('max_fund', 20000)
    safe_bot_name = html.escape(settings.get('bot_name', 'بوت تمويل سيد'))
    comp_channel = settings.get('completion_channel', 'غير محدد')
    transfer_status = "✅ مفعل" if settings.get('transfer_enabled', True) else "❌ معطل"
    
    text = f"""🛠️ <b>لوحة المطور</b>
👤 المطور: {DEV_USERNAME} (ID: <code>{DEV_ID}</code>)

⚙️ <b>الإعدادات الحالية:</b>
🤖 اسم البوت: <code>{safe_bot_name}</code>
🎁 الهدية اليومية: <code>{settings.get('daily_reward', 500)}</code> نقطة
💵 سعر العضو: <code>{settings.get('fund_rate', 50)}</code> نقطة
📉 الحد الأدنى: <code>{min_fund}</code> | 📈 الحد الأقصى: <code>{max_fund}</code>
🤝 نقاط الدعوة: <code>{settings.get('ref_reward', 5000)}</code> نقطة
📺 مكافأة القنوات: <code>{settings.get('points_reward', 40)}</code> | ⚡ التيربو: <code>{settings.get('turbo_reward', 200)}</code>
💸 تحويل النقاط: <code>{transfer_status}</code>
🎨 لون الأزرار: <code>{ {'primary': '🔵 أزرق', 'success': '🟢 أخضر', 'danger': '🔴 أحمر'}.get(settings.get('btn_color', ''), 'الألوان الأصلية') }</code>
⭐ مكافأة شحن النجوم: <code>{settings.get('stars_bonus_percent', 0)}%</code>
⚡ تسريع البوت: <code>{'✅ مفعّل' if settings.get('fast_mode') else '❌ معطّل'}</code>
👁 سعر المشاهدات: <code>{settings.get('views_price', 500)}</code> نقطة لكل 100 | ❤️ سعر التفاعلات: <code>{settings.get('reacts_price', 500)}</code> نقطة لكل 100
👁 حدود المشاهدات: <code>{settings.get('views_min', settings.get('eng_min', 100))}</code> – <code>{settings.get('views_max', settings.get('eng_max', 100000))}</code> | ❤️ حدود التفاعلات: <code>{settings.get('reacts_min', settings.get('eng_min', 100))}</code> – <code>{settings.get('reacts_max', settings.get('eng_max', 100000))}</code>
🤖 التحقق من الروبوت: <code>{'✅ مفعل' if settings.get('captcha_enabled', True) else '❌ معطل'}</code>
📢 قناة الإكمال: <code>{comp_channel}</code>
✅ سماح الإكمال: <code>{settings.get('complete_tolerance', 1)}%</code>
🔒 اشتراك إجباري: <code>{f_channels}</code> قناة | <code>{f_groups}</code> كروب
🚀 التمويلات النشطة: <code>{active_fundings}</code>

📊 <b>إحصائيات سريعة:</b>
👥 المستخدمين: <code>{total_users}</code> | 🚫 المحظورين: <code>{banned_count}</code>
💎 إجمالي النقاط: <code>{total_balance}</code>
📺 اشتراكات: <code>{total_subs}</code> | ✉️ مشاركات: <code>{total_shares}</code>
🎟️ القسائم: <code>{len(coupons)}</code>
💾 النسخ الاحتياطي: <code>{html.escape(_backup_state_text())}</code>
"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    # أزرار الكليشة والإيموجي
    markup.row(make_btn('📝 إضافة كليشة البوت', callback_data='dev_add_template', style='primary'), make_btn('😀 تبديل إيموجيات الأزرار', callback_data='dev_edit_emojis', style='success'))
    markup.row(make_btn('🔔 رسالة العضو الجديد', callback_data='dev_edit_newuser_tpl', style='primary'))
    
    _turbo_on = bool(settings.get('fast_mode'))
    markup.row(make_btn('⚡ تسريع البوت: ' + ('مفعّل ✅ (اضغط للإيقاف)' if _turbo_on else 'معطّل (اضغط للتفعيل)'), callback_data='dev_turbo_speed', style='success' if _turbo_on else 'danger'))
    markup.row(make_btn('🎁 هدية اليوم', callback_data='dev_edit_daily', style='primary'), make_btn('💵 سعر العضو', callback_data='dev_edit_fund', style='success'))
    markup.row(make_btn('👁 سعر المشاهدات', callback_data='dev_edit_views_price', style='primary'), make_btn('❤️ سعر التفاعلات', callback_data='dev_edit_reacts_price', style='primary'))
    markup.row(make_btn('👁 حدود المشاهدات', callback_data='dev_edit_views_lim', style='primary'), make_btn('❤️ حدود التفاعلات', callback_data='dev_edit_reacts_lim', style='primary'))
    markup.row(make_btn('📉 الحد الأدنى', callback_data='dev_edit_min_fund', style='primary'), make_btn('📈 الحد الأقصى', callback_data='dev_edit_max_fund', style='primary'))
    markup.row(make_btn('🤝 نقاط الدعوة', callback_data='dev_edit_ref', style='success'), make_btn('✏️ اسم البوت', callback_data='dev_edit_bot_name', style='primary'))
    markup.row(make_btn('🤖 تفعيل/تعطيل التحقق من الروبوت', callback_data='dev_toggle_captcha', style='primary'))
    markup.row(make_btn('⭐ مكافأة شحن النجوم', callback_data='dev_edit_stars_bonus', style='success'))
    markup.row(make_btn('🎨 ألوان الأزرار', callback_data='dev_btn_colors', style='primary'))
    markup.row(make_btn('💸 تفعيل/تعطيل التحويل', callback_data='dev_toggle_transfer', style='danger'))
    markup.row(make_btn('🔒 الاشتراك الإجباري', callback_data='dev_force_sub_menu', style='primary'), make_btn('📺 إدارة قنوات النقاط', callback_data='dev_points_menu', style='success'))
    markup.row(make_btn('🚀 إدارة قنوات التيربو', callback_data='dev_turbo_menu', style='primary'))
    markup.row(make_btn('📢 قناة إكمال الطلبات', callback_data='dev_edit_comp_channel', style='success'), make_btn('✅ سماح الإكمال', callback_data='dev_edit_tol', style='primary'))
    markup.row(make_btn('📊 إحصائيات مفصلة', callback_data='dev_stats', style='success'), make_btn('💰 تعديل رصيد', callback_data='dev_edit_balance', style='primary'))
    markup.row(make_btn('🎟️ إضافة قسيمة', callback_data='dev_add_coupon', style='success'), make_btn('📋 قائمة القسائم', callback_data='dev_list_coupons', style='primary'))
    markup.row(make_btn('🗑️ حذف قسيمة', callback_data='dev_del_coupon', style='danger'), make_btn('📢 الإذاعة (Broadcast)', callback_data='dev_broadcast', style='primary'))
    markup.row(make_btn('🚫 حظر مستخدم', callback_data='dev_ban_user', style='danger'), make_btn('✅ فك حظر مستخدم', callback_data='dev_unban_user', style='success'))
    markup.row(make_btn('🔗 إنشاء رابط نقاط', callback_data='dev_gift_new', style='success', icon_key='__nav__'), make_btn('📋 روابط النقاط', callback_data='dev_gift_list', style='primary', icon_key='__nav__'))
    markup.row(make_btn('💾 نسخة احتياطية الآن', callback_data='dev_backup_now', style='success'))
    markup.row(make_btn('➡️ الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
    return text, markup

def get_force_sub_menu_data():
    items = load_force_sub()
    text = "🔒 <b>إدارة الاشتراك الإجباري</b>\n────────────────────\n"
    if not items: text += "لا توجد قنوات أو كروبات مضافة حالياً.\n"
    else:
        for idx, item in enumerate(items, start=1):
            kind_label = "قناة" if item.get('kind') == 'channel' else "كروب"
            title = html.escape(item.get('title') or item.get('url') or str(item.get('target')))
            text += f"{idx}. [{kind_label}] <b>{title}</b>\n"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(make_btn('➕ إضافة قناة', callback_data='dev_add_fsub_channel', style='success'), make_btn('➕ إضافة كروب', callback_data='dev_add_fsub_group', style='success'))
    if items: markup.add(make_btn('🗑️ حذف قناة / كروب', callback_data='dev_del_fsub_menu', style='danger'))
    markup.add(make_btn('🔙 رجوع للوحة', callback_data='dev_panel', style='primary'))
    return text, markup

def get_points_menu_data():
    items = load_points_channels()
    settings = load_settings()
    reward = settings.get('points_reward', 40)
    text = f"📺 <b>إدارة قنوات النقاط</b>\n✨ المكافأة الحالية: <code>{reward}</code> نقطة لكل قناة\n────────────────────\n"
    if not items: text += "لا توجد قنوات مضافة حالياً.\n"
    else:
        for idx, item in enumerate(items, start=1):
            title = html.escape(item.get('title') or item.get('url') or str(item.get('target')))
            text += f"{idx}. <b>{title}</b>\n"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(make_btn('➕ إضافة قناة', callback_data='dev_add_pt_channel', style='success'), make_btn('💵 تعديل المكافأة', callback_data='dev_edit_pt_reward', style='primary'))
    if items: markup.add(make_btn('🗑️ حذف قناة', callback_data='dev_del_pt_menu', style='danger'))
    markup.add(make_btn('🔙 رجوع للوحة', callback_data='dev_panel', style='primary'))
    return text, markup

def get_turbo_menu_data():
    items = load_turbo_channels()
    settings = load_settings()
    reward = settings.get('turbo_reward', 200)
    text = f"🚀 <b>إدارة قنوات التيربو</b>\n✨ المكافأة الإجمالية: <code>{reward}</code> نقطة\n────────────────────\n"
    if not items: text += "لا توجد قنوات مضافة حالياً.\n"
    else:
        for idx, item in enumerate(items, start=1):
            title = html.escape(item.get('title') or item.get('url') or str(item.get('target')))
            text += f"{idx}. <b>{title}</b>\n"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(make_btn('➕ إضافة قناة', callback_data='dev_add_turbo', style='success'), make_btn('💵 تعديل المكافأة', callback_data='dev_edit_turbo_reward', style='primary'))
    if items: markup.add(make_btn('🗑️ حذف قناة', callback_data='dev_del_turbo_menu', style='danger'))
    markup.add(make_btn('🔙 رجوع للوحة', callback_data='dev_panel', style='primary'))
    return text, markup

def send_dev_panel(chat_id, user_id):
    if not is_dev(user_id): return
    text, markup = get_dev_panel_data()
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')

# ========== /start ==========

_last_post = {}

def build_points_post(kind, points, count, code=None, link=None):
    """منشور جاهز للنشر بالقنوات (رابط/كود نقاط) مع زرين يفتحون قسم المشتركين الإجباري وشحن النقاط بالبوت"""
    ref = load_settings().get('ref_reward', 5000)
    if kind == 'link':
        head = f"• تم صنع رابط هدية بقيمة <b>{int(points):,}</b> نقطة"
        body = f"<blockquote>🔗 الرابط :\n{link}</blockquote>"
        who = f"• يمكن لـ <b>{int(count):,}</b> عضو الدخول الى الرابط"
    else:
        head = f"• تم صنع كود هدية بقيمة <b>{int(points):,}</b> نقطة"
        body = (f"<blockquote>🎟️ الكود : <code>{html.escape(str(code))}</code>\n"
                "ادخل البوت واضغط «🎟️ قسائمي» وأرسل الكود</blockquote>")
        who = f"• يمكن لـ <b>{int(count):,}</b> عضو استخدام الكود"
    note = (f"<blockquote>🔔 ملاحظة: رابط الدعوة يمنحك <b>{int(ref):,}</b> نقطة لكل صديق، "
            "استغلها وشارك الرابط واربح أكثر</blockquote>")
    text = f"{head}\n{body}\n{note}\n{who}"
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(make_btn('👥 مشتركين حقيقيين', url=f"https://t.me/{BOT_USERNAME}?start=mandatory", style='danger'))
    markup.add(make_btn('💎 شراء النقاط', url=f"https://t.me/{BOT_USERNAME}?start=charge", style='danger'))
    return text, markup

def send_points_post(chat_id, kind, points, count, code=None, link=None):
    text, markup = build_points_post(kind, points, count, code, link)
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)
    _last_post[DEV_ID] = {'kind': kind, 'points': points, 'count': count, 'code': code, 'link': link}
    qb = types.InlineKeyboardMarkup(row_width=2)
    qb.row(make_btn('👤 للمستخدمين', callback_data='dev_bcpost_users', style='success', icon_key='__nav__'),
           make_btn('📺 للقنوات', callback_data='dev_bcpost_channels', style='primary', icon_key='__nav__'))
    qb.row(make_btn('👥 للمجموعات', callback_data='dev_bcpost_groups', style='primary', icon_key='__nav__'),
           make_btn('🌐 للجميع', callback_data='dev_bcpost_all', style='primary', icon_key='__nav__'))
    bot.send_message(chat_id, "📢 <b>تريد تذيع هذا المنشور بنفس الشكل والأزرار؟</b>\nاختر المستهدف، أو تجاهل هذي الرسالة.", reply_markup=qb, parse_mode='HTML')

# ========== التحقق من الروبوت (كابتشا) ==========
# الأعضاء الجدد فقط يحتاجون التحقق (captcha_ok=False). الأعضاء القدامى بدون هذا المفتاح يُعتبرون متحققين.
_captcha = {}
_captcha_lock = threading.Lock()
CAPTCHA_MAX_FAILS = 5
CAPTCHA_BLOCK_SECONDS = 300

def is_captcha_ok(uid):
    if is_dev(uid): return True
    if not load_settings().get('captcha_enabled', True): return True
    rec = load_db().get(str(uid))
    return not (isinstance(rec, dict) and rec.get('captcha_ok') is False)

_AR_INDIC = str.maketrans('0123456789', '٠١٢٣٤٥٦٧٨٩')

def build_captcha(uid):
    """يرجع (نص، أزرار) أو (نص، None) إذا العضو محظور مؤقتاً بسبب محاولات خاطئة"""
    now = time.time()
    with _captcha_lock:
        st = _captcha.setdefault(uid, {'fails': 0, 'until': 0})
        if st['until'] > now:
            mins = int((st['until'] - now) // 60) + 1
            return f"⏳ <b>محاولات خاطئة كثيرة.</b>\n\nحاول مرة ثانية بعد {mins} دقيقة.", None
        ans = str(random.randint(10000, 99999))
        opts = {ans}
        while len(opts) < 3: opts.add(str(random.randint(10000, 99999)))
        opts = list(opts); random.shuffle(opts)
        st['ans'] = ans
    text = ("🛑 | يجب التحقق أنك لست روبوت\n"
            f"✅ | اختر الكود من الزر الظاهر : <code>{ans}</code>")
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(*[make_btn(o, callback_data=f'cap_{o}', style='primary', icon_key='__nav__') for o in opts])
    return text, markup

def send_captcha(chat_id, uid, edit_msg_id=None):
    text, markup = build_captcha(uid)
    if edit_msg_id: safe_edit(chat_id, edit_msg_id, text, markup, 'HTML')
    else: bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')

def _handle_captcha_click(call):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    if is_captcha_ok(uid):
        text, markup = get_main_menu(call.from_user.first_name, uid)
        return safe_edit(chat_id, msg_id, text, markup, 'HTML')
    code = call.data[4:]
    now = time.time()
    with _captcha_lock:
        st = _captcha.setdefault(uid, {'fails': 0, 'until': 0})
        blocked = st['until'] > now
        correct = (not blocked) and st.get('ans') is not None and st.get('ans') == code
        expired = (not blocked) and st.get('ans') is None
        if correct:
            st.pop('ans', None); st['fails'] = 0
        elif not blocked and not expired:
            st['fails'] += 1
            st.pop('ans', None)
            if st['fails'] >= CAPTCHA_MAX_FAILS:
                st['until'] = now + CAPTCHA_BLOCK_SECONDS; st['fails'] = 0
    if correct:
        rec = get_user_data(uid)
        rec['captcha_ok'] = True
        save_db(load_db())
        try: bot.answer_callback_query(call.id, "✅ تم التحقق بنجاح")
        except Exception: pass
        try: bot.delete_message(chat_id, msg_id)
        except Exception: pass
        return _start_continue(chat_id, call.from_user)
    try: bot.answer_callback_query(call.id, "⏳ انتهت صلاحية الرمز، جرّب هذا الجديد" if expired else "❌ الكود غير صحيح، جرّب الرمز الجديد", show_alert=not expired)
    except Exception: pass
    send_captcha(chat_id, uid, edit_msg_id=msg_id)

def _start_continue(chat_id, user):
    """بقية خطوات /start بعد اجتياز التحقق من الروبوت"""
    uid = user.id
    user_name = user.first_name or "مستخدم"
    missing = get_unjoined_force_subs(uid, fresh=True)   # /start دائماً فحص حقيقي (بدون كاش)
    if missing:
        text, markup = build_force_sub_prompt(missing)
        bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
        return
    claim_referral(uid, user_name)

    rec = get_user_data(uid)
    welcome_needed = rec.get('welcomed', True) is False
    param = rec.pop('start_param', None)
    if welcome_needed: rec['welcomed'] = True
    if welcome_needed or param: save_db(load_db())
    if welcome_needed or WELCOME_ALWAYS:
        try: bot.send_message(chat_id, get_welcome_text(), parse_mode='HTML')
        except Exception as ex: print(f"[welcome] {ex}")

    if param in ('mandatory', 'charge'):
        m0 = bot.send_message(chat_id, "⏳")
        show_shop_menu(chat_id, m0.message_id, uid, param)
    else:
        text, markup = get_main_menu(user_name, uid)
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')
    claim_gift_link(uid)

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if _is_dup_message(message): return
    uid = message.from_user.id
    # التسجيل أولاً (قبل أي فحص) حتى ما يفلت أي عضو جديد
    is_new_user = register_user_if_new(message.from_user)
    if is_banned(uid):
        bot.send_message(message.chat.id, f"🚫 <b>أنت محظور من استخدام البوت.</b>\n\nإذا كنت تعتقد أن هذا خطأ، يرجى التواصل مع المطور: {DEV_USERNAME}", parse_mode='HTML')
        return
    user_states.pop(uid, None)

    # نحفظ الداعي أولاً، والمكافأة تُمنح بعد اجتياز التحقق والاشتراك الإجباري
    if is_new_user and message.text and len(message.text.split()) > 1:
        try:
            inviter_id = int(message.text.split()[1])
            if inviter_id != uid and isinstance(load_db().get(str(inviter_id)), dict):
                load_db()[str(uid)]['ref_pending'] = inviter_id
                save_db(load_db())
        except Exception: pass

    # رابط نقاط (start=p_CODE) أو رابط قسم المتجر: نحفظهم أولاً ويُنفذوا بعد التحقق
    _parts = (message.text or '').split()
    if len(_parts) > 1 and _parts[1].startswith('p_'):
        get_user_data(uid)['gift_pending'] = _parts[1][2:]
        save_db(load_db())
    if len(_parts) > 1 and _parts[1] in ('mandatory', 'charge'):
        get_user_data(uid)['start_param'] = _parts[1]
        save_db(load_db())

    if not is_captcha_ok(uid):
        return send_captcha(message.chat.id, uid)
    _start_continue(message.chat.id, message.from_user)

@bot.message_handler(commands=['emojitest'])
def emojitest_cmd(message):
    if not is_dev(message.from_user.id): return
    run_emoji_diagnostic(message.chat.id)

@bot.message_handler(commands=['speedtest'])
def speedtest_cmd(message):
    """للمطور فقط: يقيس زمن تعديل الرسالة بثلاث طرق لمعرفة إذا كان الإيموجي المميز هو سبب البطء"""
    if not is_dev(message.from_user.id): return
    cid = message.chat.id
    try:
        text, markup = get_main_menu(message.from_user.first_name, message.from_user.id)
        m0 = _orig_send_message(cid, "⏱ جاري قياس السرعة...")
        mid = m0.message_id
        plain_m = _plain_markup(markup)
        def run(fn):
            res = []
            for i in range(2):
                t = time.time()
                try: fn(i)
                except Exception as ex: print(f"[speedtest] {ex}")
                res.append(int((time.time() - t) * 1000))
            return res
        z = '\u200b'
        r1 = run(lambda i: _orig_edit_message_text(text + z * (1 + i), chat_id=cid, message_id=mid, parse_mode='HTML'))
        r2 = run(lambda i: _orig_edit_message_text(text + z * (3 + i), chat_id=cid, message_id=mid, parse_mode='HTML', reply_markup=plain_m))
        r3 = run(lambda i: bot.edit_message_text(text + z * (5 + i), chat_id=cid, message_id=mid, parse_mode='HTML', reply_markup=markup))
        try: bot.delete_message(cid, mid)
        except Exception: pass
        _orig_send_message(cid,
            "⏱ <b>نتيجة قياس السرعة</b> (مللي ثانية، تجربتين لكل نوع)\n\n"
            f"1️⃣ نص فقط بدون أزرار: <code>{r1}</code>\n"
            f"2️⃣ نص + أزرار عادية: <code>{r2}</code>\n"
            f"3️⃣ الحالة الفعلية (إيموجي مميز + ألوان): <code>{r3}</code>\n\n"
            "إذا الرقم 3 أكبر بوضوح من 1 و 2 فالسبب الإيموجي المميز/الأزرار الملونة.", parse_mode='HTML')
    except Exception as ex:
        print(f"[speedtest] فشل: {ex}")

@bot.message_handler(commands=['admin'])
def admin_cmd(message):
    if not is_dev(message.from_user.id): return bot.reply_to(message, "❌ غير مصرح لك بهذا الأمر.")
    text, markup = get_dev_panel_data()
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')

# ========== معالج أزرار الواجهة ==========
# answer_callback_query: إجابة واحدة فقط لكل ضغطة (الإجابة الثانية يرفضها تيليجرام، وهذا كان يخفي رسائل التنبيه)
_answered_ids = {}
_answer_lock = threading.Lock()
_orig_answer_cb = bot.answer_callback_query

def _answer_once(call_id, *args, **kwargs):
    with _answer_lock:
        if call_id in _answered_ids: return False
        _answered_ids[call_id] = time.time()
        if len(_answered_ids) > 4000:
            cutoff = time.time() - 120
            for k in [k for k, t in _answered_ids.items() if t < cutoff]: _answered_ids.pop(k, None)
    try: return _orig_answer_cb(call_id, *args, **kwargs)
    except Exception: return False

bot.answer_callback_query = _answer_once
_user_locks = {}

# ========== المشاركة عبر الوضع المضمّن (Inline) — زر "ادع صديقاً" ==========
@bot.inline_handler(func=lambda q: True)
def inline_invite_handler(q):
    try:
        uid = q.from_user.id
        print(f"[inline] طلب مشاركة من {uid}")
        if (load_db().get(str(uid)) or {}).get('banned'):
            bot.answer_inline_query(q.id, [], cache_time=0, is_personal=True); return
        link = f"https://t.me/{BOT_USERNAME}?start={uid}"      # رابط البوت + آيدي صاحب الدعوة (من حساب المرسل نفسه)
        bn = html.escape(load_settings().get('bot_name', 'بوت تمويل سيد'))
        msg = f"🚀 انضم إلى <b>{bn}</b> الآن!\nالميزة: تمويل وتصعيد قنواتك مجاناً وبسرعة.\n{link}"
        kb = types.InlineKeyboardMarkup()
        _t = '✅ اضغط هنا وابدأ التمويل مجاناً 🚀'
        try: kb.add(types.InlineKeyboardButton(_t, url=link, style='success'))     # زر أخضر
        except TypeError: kb.add(types.InlineKeyboardButton(_t, url=link))         # مكتبة قديمة لا تدعم الألوان
        try: content = types.InputTextMessageContent(msg, parse_mode='HTML', disable_web_page_preview=True)
        except TypeError: content = types.InputTextMessageContent(msg, parse_mode='HTML')
        res = types.InlineQueryResultArticle(id=f'inv_{uid}', title='اضغط هنا للدخول الى البوت!',
                                             description='مشاركة رابط الدعوة مع اصدقائك',
                                             input_message_content=content, reply_markup=kb)
        bot.answer_inline_query(q.id, [res], cache_time=0, is_personal=True)
    except Exception as ex:
        import traceback; traceback.print_exc()
        print(f"[inline] فشل: {ex}")

@bot.callback_query_handler(func=lambda call: True)
def callback_inline(call):
    # إذا تأخرت المعالجة أكثر من 1.5 ثانية نُوقف دوران الزر تلقائياً
    t = threading.Timer(1.5, _answer_once, args=(call.id,))
    t.daemon = True; t.start()
    lock = _user_locks.setdefault(call.from_user.id, threading.Lock())   # ضغطات نفس المستخدم بالتسلسل (يمنع تكرار الهدايا/الخصم)
    _t0 = time.time(); _net_tl.t = 0.0
    try:
        if not lock.acquire(timeout=3):          # ضغطة سابقة لنفس العضو ما خلصت: نتجاهل التكرار بدل ما نتراكم
            _io_pool.submit(_answer_once, call.id); t.cancel(); return
        try: _callback_inline_impl(call)
        finally: lock.release()
    finally:
        _dt = time.time() - _t0
        if _dt > 0.8: print(f"[بطيء] الزر {call.data} أخذ {_dt:.1f} ثانية (منها {getattr(_net_tl, 't', 0.0):.1f}ث انتظار تلغرام لتعديل الرسالة)")
        t.cancel()
        _io_pool.submit(_answer_once, call.id)

_INSTANT_ANSWER = {'back_to_start', 'balance', 'charge', 'mandatory', 'charge_stars', 'charge_balance', 'members_stars', 'members_balance'}

def _callback_inline_impl(call):
    uid = call.from_user.id
    register_user_if_new(call.from_user)
    if is_banned(uid):
        try: bot.answer_callback_query(call.id, "🚫 أنت محظور من استخدام البوت.", show_alert=True)
        except Exception: pass
        return
    if call.data.startswith('cap_'): return _handle_captcha_click(call)
    if not is_captcha_ok(uid):
        try: bot.answer_callback_query(call.id)
        except Exception: pass
        return send_captcha(call.message.chat.id, uid, edit_msg_id=call.message.message_id)
    if not enforce_force_sub_callback(call): return
    if call.data in _INSTANT_ANSWER: _io_pool.submit(_answer_once, call.id)   # أزرار تنقل فقط: نوقف الدوران فوراً
    
    try:
        cd = call.data
        chat_id = call.message.chat.id
        msg_id = call.message.message_id

        if cd == 'check_force_sub':
            missing = get_unjoined_force_subs(uid, fresh=True)
            if missing:
                text, markup = build_force_sub_prompt(missing)
                try: bot.answer_callback_query(call.id, "❌ لم تكمل الاشتراك في جميع القنوات/الكروبات بعد!", show_alert=True)
                except Exception: pass
                safe_edit(chat_id, msg_id, text, markup, 'HTML', True)
                return
            claim_referral(uid, call.from_user.first_name)
            text, markup = get_main_menu(call.from_user.first_name, uid)
            safe_edit(chat_id, msg_id, text, markup, 'HTML')
            claim_gift_link(uid)

        elif cd == 'back_to_start':
            user_states.pop(uid, None)
            text, markup = get_main_menu(call.from_user.first_name, uid)
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'balance':
            ud = get_user_data(uid)
            text = f"💎 <b>رصيدك</b>\n\n💰 الرصيد الحالي: <b>{ud.get('balance', 0)}</b> نقطة\n\n💡 طرق كسب النقاط:\n• 🎁 الهدية اليومية\n• 🎟️ القسائم\n• 👥 دعوة الأصدقاء\n• 📺 الاشتراك بالقنوات"
            markup = types.InlineKeyboardMarkup()
            markup.add(make_btn('➕ تجميع النقاط', callback_data='collect', style='success'))
            markup.add(make_btn('➡️ الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'invite_link':
            user_data = get_user_data(uid)
            invite_link = f"https://t.me/{BOT_USERNAME}?start={uid}"
            settings = load_settings()
            ref_reward = settings.get('ref_reward', 5000)
            db = load_db()
            sorted_users = heapq.nlargest(5, [(k, v) for k, v in list(db.items()) if isinstance(v, dict)], key=lambda item: item[1].get('total_shares', 0))
            top_text = ""
            medals = ["🥇", "🥈", "🥉", "🏅", "🏅"]
            for index, (uid_top, udata) in enumerate(sorted_users):
                shares = udata.get('total_shares', 0)
                if shares > 0: top_text += f"{medals[index]}: ({shares}) -> {uid_top}\n"
            if top_text == "": top_text = "لا يوجد مشاركين حتى الآن. كن أنت الأول! 🚀"
            text = f"👥 <b>دعوة أصدقاء</b>\n_______________________\n\n✨ لكل صديق · +{ref_reward} نقطة\n📊 مشاركاتك · {user_data.get('total_shares', 0)}\n\n_______________________\n\n🔗 رابطك الخاص:\n<code>{invite_link}</code>\n📋 اضغط على الرابط لنسخه\n\n_______________________\n\n🏆 أفضل المشاركين:\n{top_text}\n_______________________\n\n💎 رصيدك · {user_data.get('balance', 0)}"
            markup = types.InlineKeyboardMarkup(row_width=1)
            share_url = f"https://t.me/share/url?url={invite_link}&text=انضم إلي واربح {ref_reward} نقطة مجاناً!"
            markup.add(make_btn('✉️ مشاركة مع الأصدقاء', switch_inline_query='', style='success'))
            markup.add(make_btn('➡️ الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML', True)

        elif cd == 'collect':
            user_data = get_user_data(uid)
            balance = user_data.get('balance', 0)
            settings = load_settings()
            pts_reward = settings.get('points_reward', 40)
            turbo_reward = settings.get('turbo_reward', 200)
            text = f"💎 رصيدك: {balance} نقطة\n\n👇 اختر طريقة تجميع النقاط\n\n📺 قنوات – {pts_reward} نقطة لكل قناة\n⚡ تيربو – {turbo_reward} نقطة عند اشتراكك بجميع قنوات التيربو\n📅 مهام – إنجاز مهام متنوعة\n👥 دعوة أصدقاء – {settings.get('ref_reward', 5000):,} نقطة لكل صديق"
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.row(make_btn('⚡ تيربو', callback_data='turbo', style='primary'), make_btn('📺 قنوات', callback_data='channels', style='success'))
            markup.row(make_btn('📅 المهام', callback_data='tasks', style='primary'))
            markup.row(make_btn('👥 رابط الدعوة 🔗', callback_data='invite_link', style='success'))
            markup.row(make_btn('➡️ الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'channels' or cd.startswith('pt_next_'):
            idx = 0 if cd == 'channels' else int(cd.split('_')[2])
            show_points_channel(chat_id, msg_id, uid, idx)

        elif cd.startswith('pt_verify_'):
            idx = int(cd.split('_')[2])
            channels = load_points_channels()
            user = get_user_data(uid)
            verified = user.get('verified_channels', [])
            unverified = [c for c in channels if c['target'] not in verified]
            if not unverified or idx >= len(unverified):
                bot.answer_callback_query(call.id, "❌ القناة غير موجودة أو تم التحقق منها مسبقاً.", show_alert=True)
                return
            channel = unverified[idx]
            if is_user_joined_chat(channel['target'], uid):
                settings = load_settings()
                reward = settings.get('points_reward', 40)
                db = load_db()
                if str(uid) not in db: db[str(uid)] = {}
                db[str(uid)]['balance'] = db[str(uid)].get('balance', 0) + reward
                _log_tx(db[str(uid)], reward, 'channel', 'اشتراك في قناة')
                vc = db[str(uid)].get('verified_channels', [])
                if channel['target'] not in vc: vc.append(channel['target'])
                db[str(uid)]['verified_channels'] = vc
                save_db(db)
                bot.answer_callback_query(call.id, f"✅ تم التحقق بنجاح! +{reward} نقطة", show_alert=True)
                show_points_channel(chat_id, msg_id, uid, 0)
            else: bot.answer_callback_query(call.id, "❌ لم تنضم للقناة بعد! يرجى الانضمام أولاً.", show_alert=True)

        elif cd == 'pt_report':
            bot.answer_callback_query(call.id, "🚩 تم إرسال البلاغ إلى الإدارة. شكراً لك.", show_alert=True)
            try: bot.send_message(DEV_ID, f"🚩 إبلاغ جديد من المستخدم <code>{uid}</code> بشأن قناة في قسم قنوات النقاط.", parse_mode='HTML')
            except: pass

        elif cd == 'turbo':
            user_data = get_user_data(uid)
            if user_data.get('turbo_completed', False):
                text = "🚀 <b>قنوات التيربو</b>\n\n✅ لقد قمت بإكمال هذا القسم بالفعل وحصلت على مكافأتك."
                markup = types.InlineKeyboardMarkup()
                markup.add(make_btn('⬅️ رجوع', callback_data='collect', style='danger'))
                safe_edit(chat_id, msg_id, text, markup, 'HTML')
                return
            turbo_channels = load_turbo_channels()
            if not turbo_channels:
                text = "🚀 <b>قنوات التيربو</b>\n\nلا توجد قنوات حالياً.\nعد لاحقاً!"
                markup = types.InlineKeyboardMarkup()
                markup.add(make_btn('⬅️ رجوع', callback_data='collect', style='danger'))
                safe_edit(chat_id, msg_id, text, markup, 'HTML')
                return
            settings = load_settings()
            reward = settings.get('turbo_reward', 200)
            text = f"⚡ <b>الاشتراك السريع (تيربو)</b>\n────────────────────\n📺 القنوات: <b>{len(turbo_channels)}</b>\n✨ المكافأة الإجمالية: <b>+{reward}</b> نقطة\n\nالخطوات:\n1️⃣ اشترك في جميع القنوات أدناه\n2️⃣ اضغط على زر التحقق ✅\n\n💎 رصيدك : {user_data.get('balance', 0)}"
            markup = types.InlineKeyboardMarkup(row_width=1)
            for ch in turbo_channels: markup.add(make_btn(ch['title'], url=ch['url'], style='primary'))
            markup.add(make_btn('✅ تحقق من الاشتراكات', callback_data='turbo_verify', style='success'))
            markup.add(make_btn('⬅️ رجوع', callback_data='collect', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'turbo_verify':
            turbo_channels = load_turbo_channels()
            user_data = get_user_data(uid)
            if user_data.get('turbo_completed', False):
                bot.answer_callback_query(call.id, "❌ لقد حصلت على مكافأة هذا القسم مسبقاً!", show_alert=True)
                return
            if not turbo_channels:
                bot.answer_callback_query(call.id, "❌ لا توجد قنوات.", show_alert=True)
                return
            missing = [ch for ch in turbo_channels if not is_user_joined_chat(ch['target'], uid)]
            if missing:
                bot.answer_callback_query(call.id, f"❌ لم تشترك في {len(missing)} قناة بعد! يرجى الاشتراك بجميع القنوات أولاً.", show_alert=True)
                return
            settings = load_settings()
            reward = settings.get('turbo_reward', 200)
            db = load_db()
            if str(uid) not in db: db[str(uid)] = {}
            db[str(uid)]['balance'] = db[str(uid)].get('balance', 0) + reward
            _log_tx(db[str(uid)], reward, 'turbo', 'اشتراك سريع (تيربو)')
            db[str(uid)]['turbo_completed'] = True
            save_db(db)
            bot.answer_callback_query(call.id, f"🎉 مبروك! تم التحقق بنجاح وحصلت على {reward} نقطة.", show_alert=True)
            text = f"🚀 <b>قنوات التيربو</b>\n\n🎉 <b>مبروك!</b> لقد أكملت الاشتراك في جميع القنوات.\n\n💎 <b>رصيدك الآن:</b> {db[str(uid)]['balance']} نقطة"
            markup = types.InlineKeyboardMarkup()
            markup.add(make_btn('⬅️ رجوع', callback_data='collect', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'fund':
            user_data = get_user_data(uid)
            balance = user_data.get('balance', 0)
            settings = load_settings()
            rate = settings.get('fund_rate', 50)
            min_fund = settings.get('min_fund', 100)
            max_fund = settings.get('max_fund', 20000)
            max_affordable = min(balance // rate, max_fund)
            text = (
                f"💰 <b>تمويل جديد</b>\n\n"
                f"💎 <b>رصيدك:</b> {balance} نقطة\n"
                f"💵 <b>السعر:</b> {rate} نقطة لكل عضو\n"
                f"📉 <b>الحد الأدنى للتمويل:</b> {min_fund} عضو\n"
                f"📈 <b>الحد الأقصى للتمويل:</b> {max_fund} عضو\n"
                f"📊 <b>أقصى ما يمكنك تمويله:</b> {max_affordable} عضو\n\n"
                f"👇 أرسل عدد الأعضاء كرقم، أو اضغط على زر الإلغاء بالأسفل"
            )
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(make_btn('❌ إلغاء العملية', callback_data='back_to_start', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')
            user_states[uid] = {'step': 'fund_count'}
            bot.register_next_step_handler(call.message, process_fund_count, min_fund, max_fund, rate)

        elif cd == 'my_fundings':
            fundings = load_fundings()
            user_orders = [f for f in fundings.values() if isinstance(f, dict) and str(f.get('user_id')) == str(uid)]
            user_orders.sort(key=lambda x: x.get('created_at', 0), reverse=True)
            if not user_orders:
                text = "🚀 <b>تمويلاتك</b>\n\nلا توجد طلبات تمويل حتى الآن."
                markup = types.InlineKeyboardMarkup()
                markup.add(make_btn('🏠 الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
                safe_edit(chat_id, msg_id, text, markup, 'HTML')
            else:
                latest = user_orders[0]
                prog = compute_real_progress(latest, fetch=False)
                fundings[latest['order_id']] = latest
                save_fundings(fundings)
                text = format_progress_message(prog)
                if len(user_orders) > 1:
                    text += "\n────────────────────\n"
                    for extra in user_orders[1:5]:
                        p2 = compute_real_progress(extra, fetch=False)
                        text += f"• {html.escape(str(p2['title']))}: <b>{p2['shown_count']}/{p2['target_count']}</b> ({p2['percent']}%)\n"
                    save_fundings(fundings)
                markup = types.InlineKeyboardMarkup(row_width=2)
                markup.row(make_btn('تمويلاتك', callback_data='my_fundings', style='primary'), make_btn('🏠 الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
                safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'transfer':
            settings = load_settings()
            if not settings.get('transfer_enabled', True) and not is_dev(uid):
                text = "💸 <b>تحويل النقاط</b>\n\n⚠️ عذراً، خدمة تحويل النقاط معطلة حالياً من قبل الإدارة."
                markup = types.InlineKeyboardMarkup()
                markup.add(make_btn('➡️ الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
                safe_edit(chat_id, msg_id, text, markup, 'HTML')
                return

            ud = get_user_data(uid)
            text = f"💸 <b>تحويل النقاط</b>\n\n💎 رصيدك: {ud.get('balance', 0)}\n\n👇 أرسل ID المستخدم الذي تريد التحويل إليه\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'transfer_target'}
            bot.register_next_step_handler(call.message, transfer_target)

        elif cd in ('charge', 'mandatory', 'charge_stars', 'charge_balance', 'members_stars', 'members_balance'):
            show_shop_menu(chat_id, msg_id, uid, cd)

        elif cd.startswith('buy_points_') or cd.startswith('buy_member_'):
            show_shop_item(chat_id, msg_id, cd)

        elif cd == 'tasks':
            text = "📅 المهام\n_______________________\n🚫 لا توجد مهام حالياً.\n\nبإمكانك ربح النقاط بطرق أخرى:\n• 🏙️ تجميع النقاط – انضم لقنوات ممولة\n• 🤝 ادعُ صديقاً – اربح نقاط مع كل دعوة\n• 🎁 الهدية اليومية – مجاناً كل يوم"
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.row(make_btn('➕ تجميع النقاط', callback_data='collect', style='success'))
            markup.row(make_btn('📅 المهام', callback_data='tasks', style='primary'), make_btn('➡️ الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'info':
            user_data = get_user_data(uid)
            text = f"👤 حسابك\n_______________________\n🆔 الأيـدي : <code>{uid}</code>\n💎 الرصيد : {user_data.get('balance', 0)}\n🔥 السلسلة : {user_data.get('streak', 1)}\n_______________________\n📅 اليوم\n_______________________\n📺 اشتراكات اليوم : {user_data.get('today_subs', 0)}\n💸 نقاط مصروفة اليوم : {user_data.get('today_spent', 0)}\n_______________________\n📊 إجمالي النشاط\n_______________________\n📺 إجمالي الاشتراكات : {user_data.get('total_subs', 0)}\n🎁 الهدايا اليومية : {user_data.get('total_gifts', 0)}\n🔄 عمليات التحويل : {user_data.get('total_transfers', 0)}\n✉️ مشاركات الرابط : {user_data.get('total_shares', 0)}\n💸 إجمالي المصروف : {user_data.get('total_spent', 0)}\n_______________________\n🚀 التمويل\n_______________________\n📡 قنوات نشطة : {user_data.get('active_channels', 0)}\n👥 إجمالي الأعضاء المطلوبين : {user_data.get('required_members', 0)}"
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(make_btn('تمويلاتك', callback_data='my_fundings', style='primary'))
            markup.add(make_btn('💰 سجل نقاطي', callback_data='points_log', style='success'))
            markup.add(make_btn('👥 رابط الدعوة 🔗', callback_data='invite_link', style='primary'))
            markup.add(make_btn('➡️ الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd.startswith('paystar_'):
            try: amt = int(cd.split('_')[1])
            except Exception: return
            try:
                if not send_stars_invoice(chat_id, uid, amt):
                    bot.answer_callback_query(call.id, "❌ الباقة غير موجودة.", show_alert=True)
            except Exception as e:
                print(f"[stars invoice] {e}")
                try: bot.answer_callback_query(call.id, "❌ تعذر إنشاء الفاتورة، حاول لاحقاً.", show_alert=True)
                except Exception: pass

        elif cd == 'shop_noop':
            pass

        elif cd == 'points_log':
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(make_btn('🔄 تحديث', callback_data='points_log', style='success'))
            markup.add(make_btn('👤 معلومات حسابك', callback_data='info', style='primary'))
            markup.add(make_btn('➡️ الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
            safe_edit(chat_id, msg_id, build_points_log_text(uid), markup, 'HTML')

        elif cd == 'terms':
            text = get_welcome_text()
            markup = types.InlineKeyboardMarkup()
            markup.add(make_btn('• رجوع •', callback_data='back_to_start', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'daily':
            uid = call.from_user.id
            db = load_db()
            user = db.get(str(uid), {})
            last_daily = user.get('last_daily', 0)
            if isinstance(last_daily, str):
                try:
                    from datetime import datetime
                    last_daily = datetime.strptime(last_daily, '%Y-%m-%d').timestamp()
                except Exception: last_daily = 0
            now = time.time()
            cooldown = 24 * 60 * 60
            if now - last_daily < cooldown:
                remaining = int(cooldown - (now - last_daily))
                hours = remaining // 3600
                minutes = (remaining % 3600) // 60
                text = f"⏰ <b>لقد استلمت هديتك اليوم بالفعل!</b>\n\n🕒 يرجى الانتظار: <b>{hours} ساعة و {minutes} دقيقة</b> لاستلام الهدية القادمة.\n💎 رصيدك الحالي: <b>{user.get('balance', 0)}</b> نقطة"
                markup = types.InlineKeyboardMarkup()
                markup.add(make_btn('➡️ الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
                safe_edit(chat_id, msg_id, text, markup, 'HTML')
            else:
                settings = load_settings()
                reward = settings.get('daily_reward', 500)
                if str(uid) not in db: db[str(uid)] = {}
                db[str(uid)]['balance'] = db[str(uid)].get('balance', 0) + reward
                db[str(uid)]['total_gifts'] = db[str(uid)].get('total_gifts', 0) + 1
                _log_tx(db[str(uid)], reward, 'gift', 'هدية يومية')
                db[str(uid)]['last_daily'] = now
                save_db(db)
                text = f"🎁 <b>مبروك!</b>\nحصلت على <code>{reward}</code> نقطة من هديتك اليومية.\n\n💎 رصيدك الحالي: <b>{db[str(uid)]['balance']}</b> نقطة\n⏰ عد غداً لاستلام الهدية القادمة!"
                markup = types.InlineKeyboardMarkup()
                markup.add(make_btn('➡️ الرئيسية ⬅️', callback_data='back_to_start', style='primary'))
                safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'multiplier':
            text = "⚡ مضاعف النقاط\n\n🔜 قريباً جداً!\n\nستتمكن من مضاعفة نقاطك من المهام والاشتراكات.\nتابعنا للحصول على التحديثات!"
            markup = types.InlineKeyboardMarkup()
            markup.add(make_btn('➡️ الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'coupons':
            text = "🎟️ <b>تفعيل قسيمة</b>\n\nأرسل كود القسيمة للحصول على النقاط:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'redeem_coupon'}
            bot.register_next_step_handler(call.message, redeem_coupon)

        # ================= تأكيد التمويل =================
        elif cd == 'fund_confirm_yes':
            state = user_states.get(uid)
            if not state or state.get('step') != 'fund_confirm':
                bot.answer_callback_query(call.id, "❌ انتهت صلاحية هذا الطلب. يرجى المحاولة مجدداً.", show_alert=True)
                text, markup = get_main_menu(call.from_user.first_name, uid)
                safe_edit(chat_id, msg_id, text, markup, 'HTML')
                return
            
            # تنفيذ عملية التمويل
            db = load_db()
            if str(uid) not in db: db[str(uid)] = {}
            current_balance = db[str(uid)].get('balance', 0)
            cost = state['cost']
            
            if current_balance < cost:
                bot.answer_callback_query(call.id, "❌ رصيدك غير كافٍ الآن.", show_alert=True)
                user_states.pop(uid, None)
                text, markup = get_main_menu(call.from_user.first_name, uid)
                safe_edit(chat_id, msg_id, text, markup, 'HTML')
                return
                
            new_balance = current_balance - cost
            db[str(uid)]['balance'] = new_balance
            _log_tx(db[str(uid)], -cost, 'funding', f"تمويل قناة : {state['title']} - {state['count']} عضو")
            db[str(uid)]['today_spent'] = db[str(uid)].get('today_spent', 0) + cost
            db[str(uid)]['total_spent'] = db[str(uid)].get('total_spent', 0) + cost
            db[str(uid)]['active_channels'] = db[str(uid)].get('active_channels', 0) + 1
            db[str(uid)]['required_members'] = db[str(uid)].get('required_members', 0) + int(state['count'])
            save_db(db)
            
            order_id = f"ORD_{int(time.time())}_{uid}"
            order_data = {
                "order_id": order_id, "user_id": uid, "username": call.from_user.username or "", "first_name": call.from_user.first_name or "",
                "channel": state['link'], "channel_title": state['title'], "count": int(state['count']), "rate": int(state['rate']), "cost": cost,
                "start_members": state['start_members'], "last_members": state['start_members'], "last_reported_count": 0, "completed_notified": False, "status": "active", "created_at": int(time.time())
            }
            fundings = load_fundings()
            fundings[order_id] = order_data
            save_fundings(fundings)
            
            user_states.pop(uid, None)
            
            try:
                user_label = html.escape(call.from_user.first_name or "") + (f" (@{html.escape(call.from_user.username)})" if call.from_user.username else "")
                _order_txt = f"📥 <b>طلب تمويل جديد (تم تأكيده)</b>\n\n📌 القناة: <b>{html.escape(state['title'])}</b> (<code>{html.escape(state['link'])}</code>)\n👥 العدد: <b>{state['count']}</b>\n👤 الشخص: {user_label} — <code>{uid}</code>\n\n🤖 <b>حالة البوت:</b> مشرف ✅ (تم التحقق)"
                bot.send_message(DEV_ID, _order_txt, parse_mode='HTML')
                try: bot.send_message(ORDERS_CHANNEL, _order_txt, parse_mode='HTML')
                except Exception as _e: print(f"[orders channel] {_e}")
            except Exception: pass
            
            msg_text = format_start_funding_message(html.escape(state['title']), state['count'], cost, new_balance)
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.row(make_btn('تمويلاتك', callback_data='my_fundings', style='primary'), make_btn('🏠 الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
            safe_edit(chat_id, msg_id, msg_text, markup, 'HTML')
            bot.answer_callback_query(call.id, "✅ تم تأكيد التمويل وبدء العملية!", show_alert=True)
            
        elif cd == 'fund_confirm_no':
            user_states.pop(uid, None)
            text, markup = get_main_menu(call.from_user.first_name, uid)
            safe_edit(chat_id, msg_id, text, markup, 'HTML')
            bot.answer_callback_query(call.id, "❌ تم إلغاء طلب التمويل.", show_alert=True)
        # ==========================================

        # ================= لوحة المطور =================
        elif cd == 'dev_panel':
            if not is_dev(uid): return bot.answer_callback_query(call.id, "❌ غير مصرح لك", show_alert=True)
            text, markup = get_dev_panel_data()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_toggle_captcha':
            if not is_dev(uid): return
            settings = load_settings()
            settings['captcha_enabled'] = not settings.get('captcha_enabled', True)
            save_settings(settings)
            try: bot.answer_callback_query(call.id, "✅ تم تفعيل التحقق من الروبوت" if settings['captcha_enabled'] else "❌ تم تعطيل التحقق من الروبوت", show_alert=True)
            except Exception: pass
            text, markup = get_dev_panel_data()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_toggle_transfer':
            if not is_dev(uid): return
            settings = load_settings()
            current_status = settings.get('transfer_enabled', True)
            settings['transfer_enabled'] = not current_status
            save_settings(settings)
            status_msg = "❌ تم تعطيل التحويل بنجاح! لن يتمكن الأعضاء من التحويل." if current_status else "✅ تم تفعيل التحويل بنجاح! يمكن للأعضاء التحويل الآن."
            try: bot.answer_callback_query(call.id, status_msg, show_alert=True)
            except Exception: pass
            text, markup = get_dev_panel_data()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        # ======= إضافة كليشة =======
        elif cd == 'dev_add_template':
            if not is_dev(uid): return
            text = "📝 <b>إضافة كليشة البوت</b>\n\n📌 أرسل الآن الرسالة التي تريدها أن تظهر في <b>الواجهة الرئيسية (رسالة /start)</b>.\n\n💡 يمكنك استخدام:\n- إيموجيات تيليجرام المميزة (Premium Emojis)\n- تنسيقات مثل <b>عريض</b> و <i>مائل</i>\n- وضع <code>{name}</code> ليظهر اسم المستخدم.\n- وضع <code>{id}</code> ليظهر آيدي المستخدم.\n\nأو اضغط إلغاء."
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_save_template'}
            bot.register_next_step_handler(call.message, dev_save_template_process)

        # ======= تبديل إيموجيات الأزرار =======
        elif cd == 'dev_edit_emojis':
            if not is_dev(uid): return
            _clear_emoji_state(uid, chat_id)
            _clear_te_state(uid, chat_id)
            text, markup = get_icons_groups_menu()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        # ======= إيموجيات مميزة لكل النصوص =======
        elif cd == 'dev_te_diag':
            if not is_dev(uid): return
            run_emoji_diagnostic(chat_id)

        elif cd.startswith('dev_te_pick_'):
            if not is_dev(uid): return
            idx = int(cd.rsplit('_', 1)[1])
            emo = TEXT_EMOJI_LIST[idx]
            cur = 'مضبوط ✅' if _te_map().get(emo) else 'عادي'
            text = (f"✏️ <b>تغيير إيموجي النصوص</b>\n\nالإيموجي: {emo}\nالحالة: <b>{cur}</b>\n\n"
                    "أرسل الآن إيموجي مميز (Premium) واحد ليحل محله بكل رسائل البوت.")
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(make_btn('♻️ استرجاع الإيموجي العادي', callback_data=f'dev_te_reset_{idx}', style='primary', icon_key='__nav__'))
            markup.add(make_btn('🔙 رجوع', callback_data=f'dev_te_{idx // TE_PAGE_SIZE}', style='danger', icon_key='__nav__'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')
            user_states[uid] = {'step': 'dev_save_text_emoji', 'idx': idx}
            bot.register_next_step_handler(call.message, dev_save_text_emoji_process)

        elif cd.startswith('dev_te_reset_'):
            if not is_dev(uid): return
            _clear_te_state(uid, chat_id)
            idx = int(cd.rsplit('_', 1)[1])
            settings = load_settings()
            mp = settings.get('text_emojis') or {}
            mp.pop(TEXT_EMOJI_LIST[idx], None)
            settings['text_emojis'] = mp
            save_settings(settings)
            text, markup = get_text_emojis_list(idx // TE_PAGE_SIZE)
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd.startswith('dev_te_bulk_'):
            if not is_dev(uid): return
            page = int(cd.rsplit('_', 1)[1])
            items = TEXT_EMOJI_LIST[page * TE_PAGE_SIZE:(page + 1) * TE_PAGE_SIZE]
            text = (f"📦 <b>تعيين الصفحة {page + 1} دفعة واحدة</b>\n\n"
                    f"أرسل رسالة وحدة فيها <b>{len(items)}</b> إيموجي مميز بنفس هذا الترتيب بالضبط:\n\n"
                    + ' '.join(items))
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(make_btn('🔙 رجوع', callback_data=f'dev_te_{page}', style='danger', icon_key='__nav__'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')
            user_states[uid] = {'step': 'dev_save_text_emoji_bulk', 'page': page}
            bot.register_next_step_handler(call.message, dev_save_text_emoji_process)

        elif re.fullmatch(r'dev_te_\d+', cd):
            if not is_dev(uid): return
            _clear_te_state(uid, chat_id)
            text, markup = get_text_emojis_list(int(cd.rsplit('_', 1)[1]))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd.startswith('dev_ic_g_'):
            if not is_dev(uid): return
            _clear_emoji_state(uid, chat_id)
            _p = cd.split('_')
            text, markup = get_icons_list(_p[3], int(_p[4]))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd.startswith('dev_ic_pick_'):
            if not is_dev(uid): return
            idx = int(cd.rsplit('_', 1)[1])
            key, label, grp = BTN_CATALOG[idx]
            back_page = len([1 for _, _, g in BTN_CATALOG[:idx] if g == grp]) // ICON_PAGE_SIZE
            text = (f"✏️ <b>تغيير إيموجي الزر</b>\n\n🔘 الزر: <b>{html.escape(label)}</b>\n"
                    f"📌 الحالة الحالية: <b>{_icon_state(key)}</b>\n\n"
                    "أرسل الآن الإيموجي الجديد:\n"
                    "• إيموجي مميز (Premium) ← يظهر بدل الإيموجي العادي في الزر\n"
                    "• أو إيموجي عادي\n\n"
                    "ملاحظة: نص الزر ما يتغير، فقط الإيموجي.")
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(make_btn('♻️ استرجاع الافتراضي', callback_data=f'dev_ic_reset_{idx}', style='primary', icon_key='__nav__'))
            markup.add(make_btn('🔙 رجوع', callback_data=f'dev_ic_g_{grp}_{back_page}', style='danger', icon_key='__nav__'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')
            user_states[uid] = {'step': 'dev_save_emoji', 'idx': idx}
            bot.register_next_step_handler(call.message, dev_save_emoji_process)

        elif cd.startswith('dev_ic_reset_'):
            if not is_dev(uid): return
            _clear_emoji_state(uid, chat_id)
            idx = int(cd.rsplit('_', 1)[1])
            key, label, grp = BTN_CATALOG[idx]
            settings = load_settings()
            icons = settings.get('btn_icons') or {}
            icons.pop(key, None)
            settings['btn_icons'] = icons
            save_settings(settings)
            text, markup = get_icons_list(grp, len([1 for _, _, g in BTN_CATALOG[:idx] if g == grp]) // ICON_PAGE_SIZE)
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        # ================= الإذاعة =================
        elif cd == 'dev_broadcast':
            if not is_dev(uid): return
            bc_drafts.pop(uid, None)
            st = user_states.get(uid)
            if st and st.get('step') == 'dev_bc_send':
                user_states.pop(uid, None)
                try: bot.clear_step_handler_by_chat_id(chat_id)
                except Exception: pass
            n_u = len(_bc_raw('users')); n_c = len(_bc_raw('channels')); n_g = len(_bc_raw('groups'))
            text = ("📢 <b>الإذاعة المتقدمة</b>\n\n"
                    f"👤 المستخدمين (الخاص): <b>{n_u}</b>\n"
                    f"📺 القنوات: <b>{n_c}</b>\n"
                    f"👥 المجموعات: <b>{n_g}</b>\n\n"
                    "✅ تدعم: نص، صور، فيديو، ملفات، صوت، ملصقات وغيرها\n"
                    "✨ تدعم الإيموجيات المميزة والتنسيق (عريض/مائل/روابط)\n\n"
                    "اختر نوع الإذاعة:")
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(make_btn('👤 للمستخدمين (الخاص)', callback_data='dev_bc_users', style='success'))
            markup.add(make_btn('📺 للقنوات', callback_data='dev_bc_channels', style='primary'))
            markup.add(make_btn('👥 للمجموعات', callback_data='dev_bc_groups', style='primary'))
            markup.add(make_btn('🌐 للجميع', callback_data='dev_bc_all', style='primary', icon_key='__nav__'))
            if uid in bc_running:
                markup.add(make_btn('⏹️ إيقاف الإذاعة الجارية', callback_data='dev_bcstop', style='danger', icon_key='__nav__'))
            markup.add(make_btn('🔙 رجوع', callback_data='dev_panel', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd.startswith('dev_bcpost_'):
            if not is_dev(uid): return
            if uid in bc_running:
                return bot.answer_callback_query(call.id, "⏳ توجد إذاعة جارية حالياً، انتظر انتهاءها أو أوقفها.", show_alert=True)
            bc_type = cd.split('_')[2]
            info = _last_post.get(uid)
            if bc_type not in ('users', 'channels', 'groups', 'all') or not info:
                return bot.answer_callback_query(call.id, "❌ انتهت صلاحية المنشور، أنشئ رابطاً أو قسيمة من جديد.", show_alert=True)
            ptext, pmarkup = build_points_post(info['kind'], info['points'], info['count'], info.get('code'), info.get('link'))
            _bc_confirm(chat_id, uid, {'kind': bc_type, 'ctype': 'text', 'html': ptext, 'markup': pmarkup,
                                       'chat_id': chat_id, 'message_id': msg_id})

        elif cd.startswith('dev_bc_'):
            if not is_dev(uid): return
            if uid in bc_running:
                return bot.answer_callback_query(call.id, "⏳ توجد إذاعة جارية حالياً، انتظر انتهاءها أو أوقفها.", show_alert=True)
            bc_type = cd.split('_')[2]
            if bc_type not in ('users', 'channels', 'groups', 'all'): return
            user_states[uid] = {'step': 'dev_bc_send', 'bc_type': bc_type}
            type_label = BC_LABELS.get(bc_type, 'المستخدمين')
            text = (f"📢 <b>إذاعة لـ {type_label}</b>\n\n"
                    "📝 أرسل الآن الرسالة التي تريد إذاعتها (نص / صورة / فيديو / ملف ...)\n"
                    "✨ الإيموجيات المميزة والتنسيق تنرسل مثل ما كتبتها.\n\n"
                    "ستظهر لك معاينة وتأكيد قبل الإرسال.\n"
                    "أو اضغط على زر الإلغاء بالأسفل")
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            bot.register_next_step_handler(call.message, dev_bc_send_process)

        elif cd == 'dev_bcgo':
            if not is_dev(uid): return
            draft = bc_drafts.get(uid)
            if not draft:
                return bot.answer_callback_query(call.id, "❌ انتهت صلاحية الإذاعة، ابدأ من جديد.", show_alert=True)
            if uid in bc_running:
                return bot.answer_callback_query(call.id, "⏳ توجد إذاعة جارية بالفعل.", show_alert=True)
            bc_drafts.pop(uid, None)
            bc_running[uid] = {'stop': False}
            safe_edit(chat_id, msg_id, "⏳ <b>جاري تجهيز الإذاعة...</b>", None, 'HTML')
            threading.Thread(target=_bc_run, args=(uid, chat_id, msg_id, draft), daemon=True).start()

        elif cd == 'dev_bcstop':
            if not is_dev(uid): return
            ctrl = bc_running.get(uid)
            if ctrl: ctrl['stop'] = True
            bot.answer_callback_query(call.id, "⏹️ جاري الإيقاف...")

        # ================= باقي لوحة المطور =================
        elif cd == 'dev_points_menu':
            if not is_dev(uid): return
            text, markup = get_points_menu_data()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_add_pt_channel':
            if not is_dev(uid): return
            text = "➕ <b>إضافة قناة للنقاط</b>\n\nأرسل رابط القناة أو معرفها (مثال: <code>@MyChannel</code>):\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_add_pt_channel'}
            bot.register_next_step_handler(call.message, dev_add_pt_channel_process)

        elif cd == 'dev_del_pt_menu':
            if not is_dev(uid): return
            items = load_points_channels()
            if not items: return safe_edit(chat_id, msg_id, *get_points_menu_data(), 'HTML')
            text = "🗑️ <b>اختر القناة المراد حذفها:</b>"
            markup = types.InlineKeyboardMarkup(row_width=1)
            for idx, item in enumerate(items):
                title = item.get('title') or item.get('url') or str(item.get('target'))
                markup.add(make_btn(f"❌ حذف {title}", callback_data=f"dev_del_pt_{idx}", style='danger'))
            markup.add(make_btn('🔙 رجوع', callback_data='dev_points_menu', style='primary'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd.startswith('dev_del_pt_'):
            if not is_dev(uid): return
            try: idx = int(cd.replace('dev_del_pt_', '', 1))
            except Exception: return
            items = load_points_channels()
            if 0 <= idx < len(items): items.pop(idx); save_points_channels(items)
            text, markup = get_points_menu_data()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_edit_pt_reward':
            if not is_dev(uid): return
            current = load_settings().get('points_reward', 40)
            text = f"💵 <b>تعديل مكافأة قنوات النقاط</b>\n\nالمكافأة الحالية: <code>{current}</code> نقطة\n\n📝 أرسل العدد الجديد (أرقام فقط):\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_set_pt_reward'}
            bot.register_next_step_handler(call.message, dev_set_pt_reward)

        elif cd == 'dev_turbo_menu':
            if not is_dev(uid): return
            text, markup = get_turbo_menu_data()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_add_turbo':
            if not is_dev(uid): return
            text = "➕ <b>إضافة قناة للتيربو</b>\n\nأرسل رابط القناة أو معرفها:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_add_turbo'}
            bot.register_next_step_handler(call.message, dev_add_turbo_process)

        elif cd == 'dev_del_turbo_menu':
            if not is_dev(uid): return
            items = load_turbo_channels()
            if not items: return safe_edit(chat_id, msg_id, *get_turbo_menu_data(), 'HTML')
            text = "🗑️ <b>اختر القناة المراد حذفها:</b>"
            markup = types.InlineKeyboardMarkup(row_width=1)
            for idx, item in enumerate(items):
                title = item.get('title') or item.get('url') or str(item.get('target'))
                markup.add(make_btn(f"❌ حذف {title}", callback_data=f"dev_del_turbo_{idx}", style='danger'))
            markup.add(make_btn('🔙 رجوع', callback_data='dev_turbo_menu', style='primary'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd.startswith('dev_del_turbo_'):
            if not is_dev(uid): return
            try: idx = int(cd.replace('dev_del_turbo_', '', 1))
            except Exception: return
            items = load_turbo_channels()
            if 0 <= idx < len(items): items.pop(idx); save_turbo_channels(items)
            text, markup = get_turbo_menu_data()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_edit_turbo_reward':
            if not is_dev(uid): return
            current = load_settings().get('turbo_reward', 200)
            text = f"💵 <b>تعديل مكافأة التيربو</b>\n\nالمكافأة الحالية: <code>{current}</code> نقطة\n\n📝 أرسل العدد الجديد (أرقام فقط):\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_set_turbo_reward'}
            bot.register_next_step_handler(call.message, dev_set_turbo_reward)

        elif cd == 'dev_edit_tol':
            if not is_dev(uid): return
            current = load_settings().get('complete_tolerance', 1)
            text = (f"✅ <b>سماح إكمال التمويل</b>\n\nالنسبة الحالية: <code>{current}%</code>\n\n"
                    "التمويل يُعتبر <b>مكتملاً</b> إذا نقص عن الهدف بهذي النسبة (أو عضو واحد على الأقل). "
                    "مثلاً 1% معناها 99/100 يكتمل وينزل بقناة الإكمال، لأن بعض الأعضاء يطلعون من القناة.\n"
                    "اكتب 0 إذا تريد الإكمال بالعدد الكامل فقط.\n\n📝 أرسل رقم من 0 إلى 20:\n\nأو اضغط على زر الإلغاء بالأسفل")
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_set_tol'}
            bot.register_next_step_handler(call.message, dev_set_tol)

        elif cd == 'dev_edit_comp_channel':
            if not is_dev(uid): return
            current = load_settings().get('completion_channel', 'غير محدد')
            text = f"📢 <b>تعديل قناة إكمال الطلبات</b>\n\nالقناة الحالية: <code>{current}</code>\n\n📝 أرسل معرف القناة (مثال: <code>@MyChannel</code>) أو رابطها:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_set_comp_channel'}
            bot.register_next_step_handler(call.message, dev_set_comp_channel)

        elif cd == 'dev_force_sub_menu':
            if not is_dev(uid): return
            text, markup = get_force_sub_menu_data()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_add_fsub_channel':
            if not is_dev(uid): return
            text = "➕ <b>إضافة قناة للاشتراك الإجباري</b>\n\nأرسل رابط القناة أو معرفها:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_add_fsub', 'kind': 'channel'}
            bot.register_next_step_handler(call.message, dev_add_fsub_process, 'channel')

        elif cd == 'dev_add_fsub_group':
            if not is_dev(uid): return
            text = "➕ <b>إضافة كروب للاشتراك الإجباري</b>\n\nأرسل رابط الكروب أو معرفه:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_add_fsub', 'kind': 'group'}
            bot.register_next_step_handler(call.message, dev_add_fsub_process, 'group')

        elif cd == 'dev_del_fsub_menu':
            if not is_dev(uid): return
            items = load_force_sub()
            if not items: return safe_edit(chat_id, msg_id, *get_force_sub_menu_data(), 'HTML')
            text = "🗑️ <b>اختر القناة أو الكروب المراد حذفه:</b>"
            markup = types.InlineKeyboardMarkup(row_width=1)
            for idx, item in enumerate(items):
                kind_label = "قناة" if item.get('kind') == 'channel' else "كروب"
                title = item.get('title') or item.get('url') or str(item.get('target'))
                markup.add(make_btn(f"❌ حذف [{kind_label}] {title}", callback_data=f"dev_del_fsub_item_{idx}", style='danger'))
            markup.add(make_btn('🔙 رجوع', callback_data='dev_force_sub_menu', style='primary'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd.startswith('dev_del_fsub_item_'):
            if not is_dev(uid): return
            try: idx = int(cd.replace('dev_del_fsub_item_', '', 1))
            except Exception: return
            items = load_force_sub()
            if 0 <= idx < len(items): items.pop(idx); save_force_sub(items)
            text, markup = get_force_sub_menu_data()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_edit_min_fund':
            if not is_dev(uid): return
            current = load_settings().get('min_fund', 100)
            text = f"📉 <b>تعديل الحد الأدنى للتمويل</b>\n\nالحد الأدنى الحالي: <code>{current}</code> عضو\n\n📝 أرسل الحد الأدنى الجديد:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_set_min_fund'}
            bot.register_next_step_handler(call.message, dev_set_min_fund)

        elif cd == 'dev_edit_max_fund':
            if not is_dev(uid): return
            current = load_settings().get('max_fund', 20000)
            text = f"📈 <b>تعديل الحد الأقصى للتمويل</b>\n\nالحد الأقصى الحالي: <code>{current}</code> عضو\n\n📝 أرسل الحد الأقصى الجديد:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_set_max_fund'}
            bot.register_next_step_handler(call.message, dev_set_max_fund)

        elif cd == 'dev_btn_colors':
            if not is_dev(uid): return
            text, markup = get_btn_colors_menu()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_fast':
            if not is_dev(uid): return
            settings = load_settings()
            settings['fast_mode'] = not settings.get('fast_mode')
            save_settings(settings)
            try: bot.answer_callback_query(call.id, "⚡ وضع السرعة مفعّل" if settings['fast_mode'] else "✨ وضع السرعة معطّل (الإيموجي المميز رجع)")
            except Exception: pass
            text, markup = get_btn_colors_menu()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_cmenu':
            if not is_dev(uid): return
            text, markup = get_color_groups_menu()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd.startswith('dev_cg_'):
            if not is_dev(uid): return
            _p = cd.split('_')
            text, markup = get_color_list(_p[2], int(_p[3]))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd.startswith('dev_cp_'):
            if not is_dev(uid): return
            text, markup = get_color_pick(int(cd.rsplit('_', 1)[1]))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd.startswith('dev_cs_'):
            if not is_dev(uid): return
            _p = cd.split('_')
            idx, code = int(_p[2]), _p[3]
            key = BTN_CATALOG[idx][0]
            settings = load_settings()
            cols = dict(settings.get('btn_colors') or {})
            if code in COLOR_CODES: cols[key] = COLOR_CODES[code]
            else: cols.pop(key, None)
            settings['btn_colors'] = cols
            save_settings(settings)
            try: bot.answer_callback_query(call.id, f"✅ تم: {COLOR_NAMES[COLOR_CODES[code]]}" if code in COLOR_CODES else "✅ رجع اللون الافتراضي")
            except Exception: pass
            text, markup = get_color_pick(idx)
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_cclear':
            if not is_dev(uid): return
            settings = load_settings()
            settings['btn_colors'] = {}
            save_settings(settings)
            try: bot.answer_callback_query(call.id, "✅ تم مسح كل الألوان الفردية", show_alert=True)
            except Exception: pass
            text, markup = get_color_groups_menu()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd.startswith('dev_color_'):
            if not is_dev(uid): return
            val = cd[len('dev_color_'):]
            settings = load_settings()
            settings['btn_color'] = val if val in ('primary', 'success', 'danger') else ''
            settings['btn_colors'] = {}     # اللون الموحّد يمسح الألوان الفردية
            save_settings(settings)
            names = {'primary': '🔵 أزرق', 'success': '🟢 أخضر', 'danger': '🔴 أحمر'}
            try: bot.answer_callback_query(call.id, f"✅ تم تغيير لون كل الأزرار إلى {names[val]}" if val in names else "✅ رجعت الألوان الأصلية", show_alert=True)
            except Exception: pass
            text, markup = get_dev_panel_data()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_panel_back':
            if not is_dev(uid): return
            text, markup = get_dev_panel_data()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_edit_stars_bonus':
            if not is_dev(uid): return
            current = load_settings().get('stars_bonus_percent', 0)
            text = f"⭐ <b>مكافأة شحن النجوم</b>\n\nالقيمة الحالية: <code>{current}%</code> زيادة على كل باقة\n(مثال: 10 تعني من يشحن 1000 نقطة ياخذ 1100)\n\n📝 أرسل النسبة الجديدة (0 لإيقاف المكافأة):\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_set_stars_bonus'}
            bot.register_next_step_handler(call.message, dev_set_stars_bonus)

        elif cd == 'dev_edit_daily':
            if not is_dev(uid): return
            current = load_settings().get('daily_reward', 500)
            text = f"🎁 <b>تعديل الهدية اليومية</b>\n\nالقيمة الحالية: <code>{current}</code> نقطة\n\n📝 أرسل العدد الجديد:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_set_daily'}
            bot.register_next_step_handler(call.message, dev_set_daily)

        elif cd == 'dev_edit_fund':
            if not is_dev(uid): return
            current = load_settings().get('fund_rate', 50)
            text = f"💵 <b>تعديل سعر العضو للتمويل</b>\n\nالسعر الحالي: <code>{current}</code> نقطة\n\n📝 أرسل السعر الجديد:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_set_fund'}
            bot.register_next_step_handler(call.message, dev_set_fund)

        elif cd == 'dev_edit_ref':
            if not is_dev(uid): return
            current = load_settings().get('ref_reward', 5000)
            text = f"🤝 <b>تعديل نقاط رابط الدعوة</b>\n\nالنقاط الحالية: <code>{current}</code> نقطة\n\n📝 أرسل العدد الجديد:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_set_ref'}
            bot.register_next_step_handler(call.message, dev_set_ref)

        elif cd == 'dev_edit_bot_name':
            if not is_dev(uid): return
            current = html.escape(load_settings().get('bot_name', 'بوت تمويل سيد'))
            text = f"✏️ <b>تغيير اسم البوت</b>\n\nالاسم الحالي: <code>{current}</code>\n\n📝 أرسل الاسم الجديد:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_set_bot_name'}
            bot.register_next_step_handler(call.message, dev_set_bot_name)

        elif cd == 'dev_stats':
            if not is_dev(uid): return
            db = load_db()
            total_users = len(db)
            total_balance = sum(u.get('balance', 0) for u in db.values() if isinstance(u, dict))
            total_subs = sum(u.get('total_subs', 0) for u in db.values() if isinstance(u, dict))
            total_shares = sum(u.get('total_shares', 0) for u in db.values() if isinstance(u, dict))
            total_gifts = sum(u.get('total_gifts', 0) for u in db.values() if isinstance(u, dict))
            banned_count = sum(1 for u in db.values() if isinstance(u, dict) and u.get('banned', False))
            top_users = sorted([(k, v) for k, v in db.items() if isinstance(v, dict)], key=lambda x: x[1].get('balance', 0), reverse=True)[:5]
            text = f"📊 <b>إحصائيات مفصلة</b>\n\n👥 المستخدمين: <code>{total_users}</code>\n🚫 المحظورين: <code>{banned_count}</code>\n💎 إجمالي النقاط: <code>{total_balance}</code>\n📺 إجمالي الاشتراكات: <code>{total_subs}</code>\n✉️ إجمالي المشاركات: <code>{total_shares}</code>\n🎁 إجمالي الهدايا: <code>{total_gifts}</code>\n\n🏆 <b>أعلى 5 مستخدمين بالرصيد:</b>\n"
            for i, (u_id, u) in enumerate(top_users): text += f"{i+1}. <code>{u_id}</code> — {u.get('balance', 0)} نقطة\n"
            markup = types.InlineKeyboardMarkup()
            markup.add(make_btn('🔙 رجوع للوحة', callback_data='dev_panel', style='primary'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_add_coupon':
            if not is_dev(uid): return
            text = "🎟️ <b>إضافة قسيمة جديدة</b>\n\n📝 الخطوة 1 من 3:\nأرسل <b>اسم القسيمة</b> (مثال: عماد) — هذا هو الكود اللي ترسله للأعضاء\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_coupon_code'}
            bot.register_next_step_handler(call.message, dev_coupon_code)

        elif cd == 'dev_edit_balance':
            if not is_dev(uid): return
            text = "💰 <b>تعديل رصيد مستخدم</b>\n\n📝 أرسل ID المستخدم\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_edit_balance_id'}
            bot.register_next_step_handler(call.message, dev_edit_balance_id)

        elif cd == 'dev_ban_user':
            if not is_dev(uid): return
            text = "🚫 <b>حظر مستخدم</b>\n\n📝 أرسل ID المستخدم المراد حظره:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_ban_user_id'}
            bot.register_next_step_handler(call.message, dev_ban_user_id)

        elif cd == 'dev_unban_user':
            if not is_dev(uid): return
            text = "✅ <b>فك حظر مستخدم</b>\n\n📝 أرسل ID المستخدم المراد فك الحظر عنه:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_unban_user_id'}
            bot.register_next_step_handler(call.message, dev_unban_user_id)

        elif cd == 'dev_list_coupons':
            if not is_dev(uid): return
            coupons = load_coupons()
            if not coupons: text = "📋 <b>قائمة القسائم</b>\n\nلا توجد قسائم حتى الآن."
            else:
                text = "📋 <b>قائمة القسائم</b>\n\n"
                for code, info in coupons.items(): text += f"🎟️ <code>{html.escape(str(code))}</code>\n   💰 {info.get('points', 0)} نقطة\n   🔢 {info.get('used', 0)}/{info.get('max_uses', 0)}\n\n"
            markup = types.InlineKeyboardMarkup()
            markup.add(make_btn('🔙 رجوع للوحة', callback_data='dev_panel', style='primary'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_del_coupon':
            if not is_dev(uid): return
            text = "🗑️ <b>حذف قسيمة</b>\n\n📝 أرسل كود القسيمة المراد حذفها:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_del_coupon'}
            bot.register_next_step_handler(call.message, dev_del_coupon_code)

        elif cd == 'dev_edit_newuser_tpl':
            if not is_dev(uid): return
            text = ("🔔 <b>رسالة العضو الجديد</b>\n\n"
                    "هذي الرسالة توصلك <b>أنت فقط</b> (المطور) كل ما يدخل عضو جديد للبوت.\n\n"
                    "📝 أرسل الآن الرسالة الجديدة. تقدر تستخدم:\n"
                    "<code>{name}</code> ← الاسم\n"
                    "<code>{username}</code> ← المعرف\n"
                    "<code>{id}</code> ← الآيدي\n"
                    "<code>{count}</code> ← عدد المشتركين\n\n"
                    "✨ تدعم الإيموجيات المميزة (Premium) والتنسيق (عريض/مائل...).\n\n"
                    "<b>الشكل الافتراضي:</b>\n"
                    "<blockquote>📌 دخل عضو جديد للبوت\n📌 الاسم : {name}\n📌 المعرف : {username}\n📌 الايدي : {id}\n📌 عدد المشتركين : {count}</blockquote>")
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(make_btn('👁️ معاينة الحالية', callback_data='dev_newuser_tpl_preview', style='primary', icon_key='__nav__'))
            markup.add(make_btn('♻️ استرجاع الافتراضي', callback_data='dev_newuser_tpl_reset', style='success', icon_key='__nav__'))
            markup.add(make_btn('❌ إلغاء العملية', callback_data='dev_panel', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')
            user_states[uid] = {'step': 'dev_save_newuser_tpl'}
            bot.register_next_step_handler(call.message, dev_save_newuser_tpl_process)

        elif cd == 'dev_newuser_tpl_preview':
            if not is_dev(uid): return
            try: bot.send_message(chat_id, render_new_user_message(call.from_user, len(load_db())), parse_mode='HTML', disable_web_page_preview=True)
            except Exception as ex: bot.send_message(chat_id, f"❌ الرسالة الحالية فيها تنسيق غير صالح:\n<code>{html.escape(str(ex)[:200])}</code>", parse_mode='HTML')

        elif cd == 'dev_newuser_tpl_reset':
            if not is_dev(uid): return
            user_states.pop(uid, None)
            try: bot.clear_step_handler_by_chat_id(chat_id)
            except Exception: pass
            settings = load_settings()
            settings['new_user_template'] = ''
            save_settings(settings)
            bot.answer_callback_query(call.id, "✅ تم استرجاع الشكل الافتراضي", show_alert=True)
            text, markup = get_dev_panel_data()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd == 'dev_gift_new':
            if not is_dev(uid): return
            text = "🔗 <b>إنشاء رابط نقاط</b>\n\n📝 الخطوة 1 من 2:\nأرسل عدد النقاط اللي ياخذها كل شخص:\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_gift_points'}
            bot.register_next_step_handler(call.message, dev_gift_points)

        elif cd == 'dev_gift_list' or cd.startswith('dev_gift_del_'):
            if not is_dev(uid): return
            if cd.startswith('dev_gift_del_'):
                with _gift_lock:
                    _links = load_gift_links()
                    _links.pop(cd[len('dev_gift_del_'):], None)
                    save_gift_links(_links)
            text, markup = get_gift_links_menu()
            safe_edit(chat_id, msg_id, text, markup, 'HTML', True)

        elif cd == 'dev_backup_now':
            if not is_dev(uid): return
            ok = do_backup(force=True)
            if ok: bot.send_message(chat_id, f"✅ <b>تم حفظ نسخة احتياطية جديدة</b>\n👥 الأعضاء المحفوظين: <code>{len(load_db())}</code>\n\n📌 النسخة مثبّتة في هذه المحادثة وتتحدث تلقائياً كل بضع دقائق.", parse_mode='HTML')
            else: bot.send_message(chat_id, f"❌ <b>فشل حفظ النسخة الاحتياطية</b>\n<code>{html.escape(_backup_state.get('last_error') or '')}</code>", parse_mode='HTML')
            text, markup = get_dev_panel_data()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')


        # ================= قسم مشاهدات وتفاعلات القنوات =================
        elif cd == 'eng_menu':
            try: bot.clear_step_handler_by_chat_id(chat_id)
            except Exception: pass
            user_states.pop(uid, None)
            _st = load_settings()
            text = (f"📊 <b>قسم مشاهدات وتفاعلات القنوات</b>\n\n"
                    f"💎 رصيدك: <b>{get_user_data(uid).get('balance', 0):,}</b> نقطة\n\n"
                    f"👁 كل 100 مشاهدة = <b>{_st.get('views_price', 500)}</b> نقطة\n"
                    f"❤️ كل 100 تفاعل = <b>{_st.get('reacts_price', 500)}</b> نقطة\n\n"
                    f"اختر الخدمة 👇")
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(make_btn('👁 مشاهدات تيليجرام على رسالة واحدة', callback_data='eng_views', style='primary'))
            markup.add(make_btn('❤️ تفاعلات تيليجرام على رسالة واحدة', callback_data='eng_reacts', style='primary'))
            markup.add(make_btn('📈 تقدم طلباتي', callback_data='eng_my', style='success'))
            markup.add(make_btn('⬅️ رجوع', callback_data='back_to_start', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd in ('eng_views', 'eng_reacts'):
            try: bot.clear_step_handler_by_chat_id(chat_id)
            except Exception: pass
            kind = 'views' if cd == 'eng_views' else 'reacts'
            _st = load_settings()
            price = int(_st.get(kind + '_price', 500)); mn, mx = eng_limits(kind)
            name = 'المشاهدات' if kind == 'views' else 'التفاعلات'
            _bal = get_user_data(uid).get('balance', 0)
            _aff = min(mx, (_bal * 100) // max(1, price))
            text = (f"💰 <b>طلب {name} جديد</b>\n\n"
                    f"💎 <b>رصيدك:</b> {_bal:,} نقطة\n"
                    f"💵 <b>السعر:</b> كل 100 = {price} نقطة\n"
                    f"📉 <b>الحد الأدنى للطلب:</b> {mn:,}\n"
                    f"📈 <b>الحد الأقصى للطلب:</b> {mx:,}\n"
                    f"📊 <b>أقصى ما يمكنك طلبه:</b> {_aff:,}\n\n"
                    f"👇 أرسل عدد {name} كرقم، أو اضغط على زر الإلغاء بالأسفل")
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(make_btn('❌ إلغاء العملية', callback_data='back_to_start', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')
            user_states[uid] = {'step': 'eng_count', 'kind': kind}
            bot.register_next_step_handler(call.message, eng_process_count)

        elif cd == 'eng_confirm_no':
            user_states.pop(uid, None)
            text, markup = get_main_menu(call.from_user.first_name, uid)
            safe_edit(chat_id, msg_id, text, markup, 'HTML')
            try: bot.answer_callback_query(call.id, "❌ تم إلغاء الطلب.")
            except Exception: pass

        elif cd == 'eng_confirm_yes':
            state = user_states.get(uid)
            if not state or state.get('step') != 'eng_confirm':
                try: bot.answer_callback_query(call.id, "❌ انتهت صلاحية هذا الطلب. حاول مجدداً.", show_alert=True)
                except Exception: pass
                text, markup = get_main_menu(call.from_user.first_name, uid)
                safe_edit(chat_id, msg_id, text, markup, 'HTML')
                return
            db = load_db()
            rec = db.setdefault(str(uid), {})
            cost = int(state['cost'])
            if rec.get('balance', 0) < cost:
                user_states.pop(uid, None)
                try: bot.answer_callback_query(call.id, "❌ رصيدك غير كافٍ الآن.", show_alert=True)
                except Exception: pass
                text, markup = get_main_menu(call.from_user.first_name, uid)
                safe_edit(chat_id, msg_id, text, markup, 'HTML')
                return
            rec['balance'] = rec.get('balance', 0) - cost
            kname = 'مشاهدات' if state['kind'] == 'views' else 'تفاعلات'
            _log_tx(rec, -cost, 'funding', f"{kname} رسالة - {state['count']}")
            rec['today_spent'] = rec.get('today_spent', 0) + cost
            rec['total_spent'] = rec.get('total_spent', 0) + cost
            save_db(db)
            oid = f"ENG_{int(time.time())}_{uid}"
            with _eng_lock:
                _eo = load_engage()
                _eo[oid] = {"order_id": oid, "user_id": uid, "username": call.from_user.username or "", "first_name": call.from_user.first_name or "",
                            "kind": state['kind'], "count": int(state['count']), "link": state['link'], "channel": state.get('ch_link', ''), "channel_title": state.get('ch_title', ''), "cost": cost,
                            "status": "pending", "delivered": 0, "completed_notified": False, "created_at": int(time.time())}
                save_engage(_eo)
            user_states.pop(uid, None)
            try:
                ulabel = html.escape(call.from_user.first_name or "") + (f" (@{html.escape(call.from_user.username)})" if call.from_user.username else "")
                otxt = (f"📥 <b>طلب {kname} جديد (تم تأكيده)</b>\n\n📌 القناة: <b>{html.escape(state.get('ch_title', ''))}</b> (<code>{html.escape(state.get('ch_link', ''))}</code>)\n"
                        f"🔢 العدد: <b>{state['count']:,}</b>\n🔗 الرابط: {html.escape(state['link'])}\n"
                        f"💎 التكلفة: <b>{cost:,}</b> نقطة\n👤 الشخص: {ulabel} — <code>{uid}</code>\n🆔 <code>{oid}</code>\n\n"
                        f"🤖 <b>حالة البوت:</b> مشرف ✅ (تم التحقق)\n📌 <b>حالة الطلب:</b> ⏳ قيد التنفيذ")
                dmk = types.InlineKeyboardMarkup(row_width=2)
                dmk.row(make_btn('📈 تحديث التقدم', callback_data=f'eng_prog_{oid}', style='primary', raw=True))
                dmk.row(make_btn('✅ تم التنفيذ', callback_data=f'eng_done_{oid}', style='success', raw=True),
                        make_btn('❌ رفض + استرجاع النقاط', callback_data=f'eng_refund_{oid}', style='danger', raw=True))
                bot.send_message(DEV_ID, otxt, parse_mode='HTML', reply_markup=dmk, disable_web_page_preview=True)
                try: bot.send_message(ORDERS_CHANNEL, otxt, parse_mode='HTML', disable_web_page_preview=True)
                except Exception as _e: print(f"[orders channel] {_e}")
            except Exception as _e: print(f"[eng notify] {_e}")
            text = (f"✅ <b>تم استلام طلبك</b>\n\n{'👁' if state['kind'] == 'views' else '❤️'} {kname}: <b>{state['count']:,}</b>\n"
                    f"💵 التكلفة: <b>{cost:,}</b> نقطة\n💎 رصيدك المتبقي: <b>{rec['balance']:,}</b> نقطة\n\nسيتم تنفيذ الطلب قريباً.")
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.row(make_btn('📈 تقدم طلباتي', callback_data='eng_my', style='primary'), make_btn('🏠 الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')
            try: bot.answer_callback_query(call.id, "✅ تم استلام طلبك!", show_alert=True)
            except Exception: pass

        elif cd == 'eng_my':
            _all = [o for o in load_engage().values() if isinstance(o, dict) and str(o.get('user_id')) == str(uid)]
            _all.sort(key=lambda x: x.get('created_at', 0), reverse=True)
            markup = types.InlineKeyboardMarkup(row_width=2)
            if not _all:
                text = "📈 <b>تقدم طلباتي</b>\n\nلا توجد طلبات مشاهدات أو تفاعلات حتى الآن."
            else:
                text = eng_progress_text(_all[0])
                if len(_all) > 1:
                    text += "\n────────────────────\n"
                    for ex in _all[1:5]:
                        d, c, pct, _b = eng_progress_info(ex)
                        text += f"• {'👁' if ex['kind'] == 'views' else '❤️'} {html.escape(str(ex.get('channel_title') or ex.get('channel') or ''))}: <b>{d:,}/{c:,}</b> ({pct}%)\n"
            markup.row(make_btn('📈 تقدم طلباتي', callback_data='eng_my', style='primary'), make_btn('🏠 الرئيسية ⬅️', callback_data='back_to_start', style='danger'))
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd.startswith('eng_prog_'):
            if not is_dev(uid): return
            oid = cd[len('eng_prog_'):]
            od = load_engage().get(oid)
            if not od or od.get('status') != 'pending':
                try: bot.answer_callback_query(call.id, "⚠️ هذا الطلب تمت معالجته مسبقاً.", show_alert=True)
                except Exception: pass
                return
            d, c, pct, _b = eng_progress_info(od)
            user_states[uid] = {'step': 'dev_eng_prog', 'oid': oid}
            msg = bot.send_message(chat_id, f"📈 <b>تحديث تقدم الطلب</b>\n\n🆔 <code>{oid}</code>\nالمنفّذ حالياً: <b>{d:,}</b> من <b>{c:,}</b>\n\n📝 أرسل العدد المنفّذ حتى الآن (إذا أرسلت {c:,} أو أكثر يكتمل الطلب ويُنشر بقناة الإكمال):\n\nأو اضغط على زر الإلغاء بالأسفل", parse_mode='HTML', reply_markup=get_cancel_markup())
            bot.register_next_step_handler(msg, dev_eng_progress)

        elif cd.startswith('eng_done_') or cd.startswith('eng_refund_'):
            if not is_dev(uid): return
            refund = cd.startswith('eng_refund_')
            oid = cd[len('eng_refund_'):] if refund else cd[len('eng_done_'):]
            with _eng_lock:
                _eo = load_engage()
                od = _eo.get(oid)
                if not od or od.get('status') != 'pending':
                    try: bot.answer_callback_query(call.id, "⚠️ هذا الطلب تمت معالجته مسبقاً.", show_alert=True)
                    except Exception: pass
                    return
                od['status'] = 'refunded' if refund else 'done'
                if not refund: od['delivered'] = int(od['count'])
                save_engage(_eo)
            kname = 'مشاهدات' if od['kind'] == 'views' else 'تفاعلات'
            if refund:
                db = load_db(); rec = db.setdefault(str(od['user_id']), {})
                rec['balance'] = rec.get('balance', 0) + int(od['cost'])
                _log_tx(rec, int(od['cost']), 'admin', f"استرجاع طلب {kname}")
                save_db(db)
            try:
                bot.send_message(od['user_id'], (f"❌ تم رفض طلب {kname} وتم استرجاع <b>{int(od['cost']):,}</b> نقطة لرصيدك." if refund
                                                 else f"✅ تم تنفيذ طلب {kname} (<b>{int(od['count']):,}</b>) بنجاح."), parse_mode='HTML')
            except Exception: pass
            try:
                _stt = (f"{'❌' if refund else '✅'} <b>تحديث حالة الطلب</b>\n\n📌 القناة: <b>{html.escape(od.get('channel_title', '') or od.get('channel', ''))}</b>\n"
                        f"{'👁' if od['kind'] == 'views' else '❤️'} {kname}: <b>{int(od['count']):,}</b>\n🔗 {html.escape(od['link'])}\n"
                        f"👤 <code>{od['user_id']}</code> | 🆔 <code>{oid}</code>\n\n"
                        f"📌 <b>حالة الطلب:</b> {'❌ مرفوض وتم استرجاع النقاط' if refund else '✅ تم التنفيذ'}")
                bot.send_message(ORDERS_CHANNEL, _stt, parse_mode='HTML', disable_web_page_preview=True)
            except Exception as _e: print(f"[orders channel] {_e}")
            if not refund: eng_finish(oid)
            try: _base = call.message.html_text or ''
            except Exception: _base = ''
            safe_edit(chat_id, msg_id, _base + ("\n\n❌ <b>تم الرفض واسترجاع النقاط</b>" if refund else "\n\n✅ <b>تم التنفيذ</b>"), None, 'HTML', True)

        # ================= لوحة المطور: التسريع + الأسعار =================
        elif cd == 'dev_turbo_speed':
            if not is_dev(uid): return
            settings = load_settings()
            settings['fast_mode'] = not settings.get('fast_mode')
            save_settings(settings)
            try: bot.answer_callback_query(call.id, "⚡ تم تفعيل التسريع (أقصى سرعة)" if settings['fast_mode'] else "✨ تم إيقاف التسريع (الإيموجي المميز رجع)")
            except Exception: pass
            text, markup = get_dev_panel_data()
            safe_edit(chat_id, msg_id, text, markup, 'HTML')

        elif cd in ('dev_edit_views_lim', 'dev_edit_reacts_lim'):
            if not is_dev(uid): return
            kind = 'views' if cd == 'dev_edit_views_lim' else 'reacts'
            name = 'المشاهدات' if kind == 'views' else 'التفاعلات'
            mn, mx = eng_limits(kind)
            text = f"📉📈 <b>حدود {name}</b>\n\nالحالي: الأدنى <code>{mn:,}</code> | الأقصى <code>{mx:,}</code>\n\n📝 أرسل <b>الحد الأدنى</b> أولاً (مثال: <code>50</code>):\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_set_lim_min', 'kind': kind}
            bot.register_next_step_handler(call.message, dev_set_lim_min)

        elif cd in ('dev_edit_views_price', 'dev_edit_reacts_price'):
            if not is_dev(uid): return
            key = 'views_price' if cd == 'dev_edit_views_price' else 'reacts_price'
            name = 'المشاهدات' if key == 'views_price' else 'التفاعلات'
            current = load_settings().get(key, 500)
            text = f"💵 <b>تعديل سعر {name}</b>\n\nالسعر الحالي: <code>{current}</code> نقطة لكل 100\n\n📝 أرسل عدد النقاط اللي تكلفه كل 100 (مثال: <code>500</code> يعني 100 = 500 نقطة):\n\nأو اضغط على زر الإلغاء بالأسفل"
            safe_edit(chat_id, msg_id, text, get_cancel_markup(), 'HTML')
            user_states[uid] = {'step': 'dev_set_eng_price'}
            bot.register_next_step_handler(call.message, dev_set_eng_price, key)

        elif cd == 'no_action': pass
        else: 
            try: bot.answer_callback_query(call.id, "🔜 هذا الخيار قيد التطوير حالياً", show_alert=True)
            except: pass
    except Exception as e: print(f"Error in callback: {e}")

# ================= قسم مشاهدات وتفاعلات القنوات =================
ENGAGE_FILE = 'engage_orders.json'
_c_engage = None
_eng_lock = threading.RLock()
_ENG_LINK_RE = re.compile(r'(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:c/\d+|[A-Za-z][A-Za-z0-9_]{3,})/\d+', re.I)

def load_engage():
    global _c_engage
    if _c_engage is not None: return _c_engage
    _c_engage = {}
    if os.path.exists(ENGAGE_FILE):
        try:
            with open(ENGAGE_FILE, 'r', encoding='utf-8') as f: _c_engage = json.load(f)
        except Exception:
            _backup_corrupt(ENGAGE_FILE); _c_engage = {}
    return _c_engage

def save_engage(data):
    global _c_engage
    _c_engage = data
    _queue_write(ENGAGE_FILE, lambda: _c_engage)

def eng_limits(kind):
    st = load_settings()
    return int(st.get(kind + '_min', st.get('eng_min', 100))), int(st.get(kind + '_max', st.get('eng_max', 100000)))

def eng_cost(kind, count):
    price = int(load_settings().get(kind + '_price', 500))
    return max(1, -(-int(count) * price // 100))     # السعر لكل 100 (تقريب للأعلى)

def eng_process_count(message):
    uid = message.from_user.id
    if is_banned(uid) or not enforce_force_sub_message(message): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.get(uid)
    if not state or state.get('step') != 'eng_count': return
    kind = state['kind']; name = 'المشاهدات' if kind == 'views' else 'التفاعلات'
    mn, mx = eng_limits(kind)
    def _again(txt):
        msg = bot.reply_to(message, txt, parse_mode='HTML', reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, eng_process_count)
    try: count = int((message.text or '').translate(_AR_DIGITS).replace(',', '').strip())
    except Exception: return _again("❌ أرسل رقماً صحيحاً أو اضغط على زر الإلغاء:")
    if count < mn: return _again(f"❌ الحد الأدنى هو <b>{mn:,}</b>. أرسل عدداً أكبر:")
    if count > mx: return _again(f"❌ الحد الأقصى هو <b>{mx:,}</b>. أرسل عدداً أقل:")
    cost = eng_cost(kind, count)
    bal = get_user_data(uid).get('balance', 0)
    if bal < cost: return _again(f"❌ رصيدك غير كافٍ.\n💎 رصيدك: <b>{bal:,}</b>\n💸 المطلوب: <b>{cost:,}</b>\nأرسل عدداً أقل:")
    user_states[uid] = {'step': 'eng_link', 'kind': kind, 'count': count, 'cost': cost}
    msg = bot.reply_to(message, f"🔗 <b>أرسل رابط الرسالة</b> (مثال: <code>https://t.me/channel/123</code>)\n\n📌 {name}: <b>{count:,}</b> | 💵 التكلفة: <b>{cost:,}</b> نقطة\n\n⚠️ <b>ملاحظة هامة:</b> يجب رفع البوت <b>مشرفاً</b> <a href=\"https://t.me/{BOT_USERNAME}\">@{BOT_USERNAME}</a> في القناة أولاً.\n\nأو اضغط على زر الإلغاء بالأسفل",
                       parse_mode='HTML', disable_web_page_preview=True, reply_markup=get_cancel_markup())
    bot.register_next_step_handler(msg, eng_process_link)

def eng_process_link(message):
    uid = message.from_user.id
    if is_banned(uid) or not enforce_force_sub_message(message): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.get(uid)
    if not state or state.get('step') != 'eng_link': return
    m = _ENG_LINK_RE.search(message.text or '')
    if not m:
        msg = bot.reply_to(message, "❌ الرابط غير صحيح. أرسل رابط رسالة من قناة مثل:\n<code>https://t.me/channel/123</code>\n\nأو اضغط على زر الإلغاء:", parse_mode='HTML', reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, eng_process_link)
        return
    link = m.group(0)
    if not link.lower().startswith('http'): link = 'https://' + link
    kind = state['kind']; count = state['count']; cost = state['cost']
    bal = get_user_data(uid).get('balance', 0)
    if bal < cost:
        user_states.pop(uid, None)
        bot.reply_to(message, "❌ رصيدك غير كافٍ.")
        return send_welcome(message)
    def _retry(txt):
        m2 = bot.send_message(message.chat.id, txt, parse_mode='HTML', disable_web_page_preview=True, reply_markup=get_cancel_markup())
        user_states[uid] = {'step': 'eng_link', 'kind': kind, 'count': count, 'cost': cost}
        bot.register_next_step_handler(m2, eng_process_link)
    _path = re.sub(r'^https?://(?:www\.)?(?:t|telegram)\.me/', '', link, flags=re.I).split('?')[0].strip('/').split('/')
    if _path[0].lower() == 'c' and len(_path) >= 3: target = int('-100' + _path[1])
    else: target = '@' + _path[0]
    chat_obj = safe_get_chat(target)
    if chat_obj is None:
        return _retry("❌ <b>ما لقيت هذه القناة.</b>\n\nتأكد من الرابط وأرسله مرة ثانية.")
    if not is_bot_admin_in_chat(chat_obj.id):
        return _retry("❌ <b>البوت غير مشرف في القناة!</b>\n\n"
                      f"جرّب ترفع البوت مشرفاً <a href=\"https://t.me/{BOT_USERNAME}\">@{BOT_USERNAME}</a> في قناتك:\n"
                      "1️⃣ افتح قناتك ← المشرفين ← إضافة مشرف\n"
                      f"2️⃣ ابحث عن البوت <a href=\"https://t.me/{BOT_USERNAME}\">@{BOT_USERNAME}</a> وأضفه مشرفاً\n"
                      "3️⃣ أرسل رابط الرسالة مرة ثانية 👇")
    ch_link = f"@{chat_obj.username}" if getattr(chat_obj, 'username', None) else str(chat_obj.id)
    ch_title = getattr(chat_obj, 'title', None) or ch_link
    user_states[uid] = {'step': 'eng_confirm', 'kind': kind, 'count': count, 'cost': cost, 'link': link, 'ch_title': ch_title, 'ch_link': ch_link}
    kname = 'مشاهدات' if kind == 'views' else 'تفاعلات'
    text = (f"⚠️ <b>تأكيد الطلب</b>\n────────────────────\n"
            f"📌 <b>القناة:</b> {html.escape(ch_title)}\n"
            f"{'👁' if kind == 'views' else '❤️'} <b>الخدمة:</b> {kname}\n🔢 <b>العدد:</b> {count:,}\n🔗 <b>الرابط:</b> {html.escape(link)}\n"
            f"💵 <b>التكلفة:</b> {cost:,} نقطة\n💎 <b>رصيدك بعد الطلب:</b> {bal - cost:,} نقطة\n────────────────────\nهل تريد تأكيد الطلب؟")
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(make_btn('✅ تأكيد الطلب', callback_data='eng_confirm_yes', style='success'), make_btn('❌ إلغاء الطلب', callback_data='eng_confirm_no', style='danger'))
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML', disable_web_page_preview=True)

def eng_progress_info(od):
    c = max(1, int(od.get('count', 1))); d = max(0, min(c, int(od.get('delivered', 0))))
    pct = int(d * 100 / c)
    return d, c, pct, build_progress_bar(pct)

def eng_progress_text(od):
    d, c, pct, bar = eng_progress_info(od)
    kname = 'المشاهدات' if od.get('kind') == 'views' else 'التفاعلات'
    st = od.get('status')
    extra = "\n❌ <b>تم رفض الطلب واسترجاع النقاط</b>" if st == 'refunded' else ("\n✅ <b>مكتمل</b>" if st == 'done' else "")
    return (f"📈 <b>تقدّم {kname}</b>\n────────────────────\n"
            f"📌 <b>{html.escape(str(od.get('channel_title') or od.get('channel') or ''))}</b>\n"
            f"{'👁' if od.get('kind') == 'views' else '❤️'} <b>{d:,} / {c:,}</b>\n{bar}{extra}")

def eng_post_completion(od):
    comp = load_settings().get('completion_channel')
    if not comp: raise ValueError("قناة إكمال الطلبات غير محددة")
    kname = 'مشاهدات' if od.get('kind') == 'views' else 'تفاعلات'
    line = '━━━━━━━━━━━━━━━━━━'
    text = (f"🔔 طلب {kname} جديد تم إكماله بنجاح ✅\n\n⚙️ المعلومات الإحصائية للعملية:\n{line}\n<blockquote>"
            f"📦 القناة ⇐ {html.escape(str(od.get('channel_title') or 'قناة غير معروفة'))}\n"
            f"🆔 الآيدي ⇐ <a href=\"tg://user?id={od['user_id']}\">{od['user_id']}</a>\n"
            f"🎯 الخدمة ⇐ {kname}\n📊 العدد ⇐ {int(od['count']):,} {'مشاهدة' if od.get('kind') == 'views' else 'تفاعل'}\n"
            f"✨ الحالة ⇐ مكتمل بنسبة 100%</blockquote>{line}")
    markup = types.InlineKeyboardMarkup()
    markup.add(make_btn('اطلب الان ✅', url=f"https://t.me/{BOT_USERNAME}", style='success'))
    bot.send_message(comp, text, reply_markup=markup, parse_mode='HTML')

def eng_finish(oid):
    """ينشر إكمال الطلب بقناة الإكمال مرة وحدة فقط"""
    with _eng_lock:
        _eo = load_engage(); od = _eo.get(oid)
        if not od or od.get('completed_notified'): return False
        od['completed_notified'] = True
        save_engage(_eo)
    try: eng_post_completion(od)
    except Exception as ex:
        print(f"[eng completion] {ex}")
        try: bot.send_message(DEV_ID, f"⚠️ تعذر نشر إكمال الطلب بقناة الإكمال: {html.escape(str(ex))}\nتأكد من تحديد قناة إكمال الطلبات وأن البوت مشرف فيها.", parse_mode='HTML')
        except Exception: pass
    return True

def dev_eng_progress(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.get(uid)
    if not state or state.get('step') != 'dev_eng_prog':
        send_dev_panel(message.chat.id, uid)
        return
    oid = state['oid']
    try:
        v = int((message.text or '').translate(_AR_DIGITS).replace(',', '').strip())
        if v < 0: raise ValueError()
    except Exception:
        msg = bot.reply_to(message, "❌ أرسل رقماً صحيحاً أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_eng_progress)
        return
    user_states.pop(uid, None)
    with _eng_lock:
        _eo = load_engage(); od = _eo.get(oid)
        if not od or od.get('status') != 'pending':
            bot.reply_to(message, "⚠️ هذا الطلب تمت معالجته مسبقاً.")
            return
        c = int(od['count']); finished = v >= c
        od['delivered'] = c if finished else v
        if finished: od['status'] = 'done'
        save_engage(_eo)
    d, c, pct, bar = eng_progress_info(od)
    kname = 'مشاهدات' if od['kind'] == 'views' else 'تفاعلات'
    bot.reply_to(message, f"✅ <b>تم تحديث التقدم</b>\n{d:,} / {c:,}\n{bar}" + ("\n\n🎉 اكتمل الطلب وتم نشره بقناة الإكمال." if finished else ""), parse_mode='HTML')
    if finished:
        try: bot.send_message(od['user_id'], f"✅ تم تنفيذ طلب {kname} (<b>{c:,}</b>) بنجاح.", parse_mode='HTML')
        except Exception: pass
        try: bot.send_message(ORDERS_CHANNEL, f"✅ <b>تحديث حالة الطلب</b>\n\n📌 القناة: <b>{html.escape(str(od.get('channel_title') or od.get('channel') or ''))}</b>\n{'👁' if od['kind'] == 'views' else '❤️'} {kname}: <b>{c:,}</b>\n🆔 <code>{oid}</code>\n\n📌 <b>حالة الطلب:</b> ✅ تم التنفيذ", parse_mode='HTML', disable_web_page_preview=True)
        except Exception as _e: print(f"[orders channel] {_e}")
        eng_finish(oid)

def dev_set_lim_min(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.get(uid)
    if not state or state.get('step') != 'dev_set_lim_min':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        v = int((message.text or '').translate(_AR_DIGITS).replace(',', '').strip())
        if v <= 0: raise ValueError()
    except Exception:
        msg = bot.reply_to(message, "❌ أرسل رقماً صحيحاً أكبر من صفر أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_lim_min)
        return
    user_states[uid] = {'step': 'dev_set_lim_max', 'kind': state['kind'], 'min': v}
    msg = bot.reply_to(message, f"✅ الحد الأدنى: <code>{v:,}</code>\n\n📝 الآن أرسل <b>الحد الأقصى</b> (مثال: <code>20000</code>):", parse_mode='HTML', reply_markup=get_cancel_markup())
    bot.register_next_step_handler(msg, dev_set_lim_max)

def dev_set_lim_max(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.get(uid)
    if not state or state.get('step') != 'dev_set_lim_max':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        v = int((message.text or '').translate(_AR_DIGITS).replace(',', '').strip())
        if v < state['min']: raise ValueError()
    except Exception:
        msg = bot.reply_to(message, f"❌ أرسل رقماً صحيحاً أكبر أو يساوي الحد الأدنى (<code>{state['min']:,}</code>) أو اضغط على زر الإلغاء:", parse_mode='HTML', reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_lim_max)
        return
    user_states.pop(uid, None)
    kind = state['kind']
    settings = load_settings()
    settings[kind + '_min'] = int(state['min']); settings[kind + '_max'] = v
    save_settings(settings)
    name = 'المشاهدات' if kind == 'views' else 'التفاعلات'
    bot.reply_to(message, f"✅ <b>تم تعديل حدود {name}!</b>\n\n📉 الأدنى: <code>{state['min']:,}</code>\n📈 الأقصى: <code>{v:,}</code>", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

def dev_set_eng_price(message, key):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_set_eng_price':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        new_val = int((message.text or '').translate(_AR_DIGITS).strip())
        if new_val <= 0: raise ValueError()
    except Exception:
        user_states[uid] = state
        msg = bot.reply_to(message, "❌ أرسل رقماً صحيحاً أكبر من صفر أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_eng_price, key)
        return
    settings = load_settings()
    settings[key] = new_val
    save_settings(settings)
    name = 'المشاهدات' if key == 'views_price' else 'التفاعلات'
    bot.reply_to(message, f"✅ <b>تم تعديل سعر {name}!</b>\n\n💵 السعر الجديد: <code>{new_val}</code> نقطة لكل 100", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

# ================= دوال الإضافة الجديدة =================
def dev_save_template_process(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_dev_panel(message.chat.id, uid)
    
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_save_template':
        send_dev_panel(message.chat.id, uid)
        return
    
    new_template = message.html_text if message.html_text else message.text
    if not new_template:
        msg = bot.reply_to(message, "❌ لم يتم استلام نص. يرجى إرسال الكليشة أو الضغط على إلغاء.", reply_markup=get_cancel_markup())
        user_states[uid] = {'step': 'dev_save_template'}
        bot.register_next_step_handler(msg, dev_save_template_process)
        return

    settings = load_settings()
    settings['bot_template'] = new_template
    save_settings(settings)
    
    bot.reply_to(message, "✅ <b>تم حفظ كليشة البوت بنجاح!</b>\n\nسيتم استخدامها الآن في واجهة /start", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

def dev_save_newuser_tpl_process(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_dev_panel(message.chat.id, uid)
    state = user_states.get(uid)
    if not state or state.get('step') != 'dev_save_newuser_tpl':
        send_dev_panel(message.chat.id, uid)
        return
    new_tpl = (message.html_text if message.text else None)
    if not new_tpl:
        msg = bot.reply_to(message, "❌ أرسل الرسالة كنص، أو اضغط إلغاء.", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_save_newuser_tpl_process)
        return
    # نعرض المعاينة أولاً؛ إذا رفضها تيليجرام (تنسيق خاطئ) لا نحفظ
    try:
        bot.send_message(message.chat.id, "👁️ <b>معاينة:</b>", parse_mode='HTML')
        bot.send_message(message.chat.id, render_new_user_message(message.from_user, len(load_db()), new_tpl), parse_mode='HTML', disable_web_page_preview=True)
    except Exception as ex:
        msg = bot.reply_to(message, f"❌ التنسيق غير صالح ولم يُحفظ:\n<code>{html.escape(str(ex)[:200])}</code>\n\nأرسل رسالة ثانية أو اضغط إلغاء.", parse_mode='HTML', reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_save_newuser_tpl_process)
        return
    user_states.pop(uid, None)
    settings = load_settings()
    settings['new_user_template'] = new_tpl
    save_settings(settings)
    bot.reply_to(message, "✅ <b>تم حفظ رسالة العضو الجديد!</b>\nراح توصلك بهذا الشكل مع كل عضو جديد.", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

def dev_save_emoji_process(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_dev_panel(message.chat.id, uid)

    state = user_states.get(uid)
    if not state or state.get('step') != 'dev_save_emoji':
        send_dev_panel(message.chat.id, uid)
        return

    idx = state.get('idx')
    key, label, grp = BTN_CATALOG[idx]

    cfg = None
    for ent in (message.entities or message.caption_entities or []):
        if ent.type == 'custom_emoji' and getattr(ent, 'custom_emoji_id', None):
            cfg = {'id': str(ent.custom_emoji_id)}
            break
    if not cfg and message.text:
        emo = _first_emoji(message.text)
        if emo: cfg = {'emoji': emo}

    if not cfg:
        msg = bot.reply_to(message, "❌ ما لقيت إيموجي بالرسالة. أرسل إيموجي (مميز أو عادي) أو اضغط إلغاء.", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_save_emoji_process)
        return

    user_states.pop(uid, None)
    settings = load_settings()
    icons = settings.get('btn_icons') or {}
    icons[key] = cfg
    settings['btn_icons'] = icons
    save_settings(settings)

    bot.reply_to(message, f"✅ <b>تم تحديث إيموجي الزر:</b> {html.escape(label)}", parse_mode='HTML')
    page = len([1 for _, _, g in BTN_CATALOG[:idx] if g == grp]) // ICON_PAGE_SIZE
    text, markup = get_icons_list(grp, page)
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')

def dev_save_text_emoji_process(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_dev_panel(message.chat.id, uid)

    state = user_states.get(uid)
    if not state or state.get('step') not in ('dev_save_text_emoji', 'dev_save_text_emoji_bulk'):
        send_dev_panel(message.chat.id, uid)
        return

    ids = []
    for ent in (message.entities or message.caption_entities or []):
        if ent.type == 'custom_emoji' and getattr(ent, 'custom_emoji_id', None):
            ids.append(str(ent.custom_emoji_id))

    if not ids:
        msg = bot.reply_to(message, "❌ ما لقيت إيموجي مميز بالرسالة. أرسل إيموجي Premium أو اضغط إلغاء.", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_save_text_emoji_process)
        return

    settings = load_settings()
    mp = settings.get('text_emojis') or {}
    if state['step'] == 'dev_save_text_emoji':
        idx = state['idx']
        mp[TEXT_EMOJI_LIST[idx]] = ids[0]
        page = idx // TE_PAGE_SIZE
        note = f"✅ تم ضبط الإيموجي {TEXT_EMOJI_LIST[idx]}"
    else:
        page = state['page']
        items = TEXT_EMOJI_LIST[page * TE_PAGE_SIZE:(page + 1) * TE_PAGE_SIZE]
        for e, cid in zip(items, ids):
            mp[e] = cid
        n = min(len(items), len(ids))
        note = f"✅ تم ضبط {n} من {len(items)} إيموجي"
        if len(ids) < len(items): note += "\n⚠️ العدد أقل من المطلوب، تم ضبط الأوائل فقط بالترتيب."
    user_states.pop(uid, None)
    settings['text_emojis'] = mp
    save_settings(settings)

    bot.reply_to(message, note)
    text, markup = get_text_emojis_list(page)
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')

# ================= نظام الإذاعة =================
CHATS_FILE = 'bot_chats.json'
BC_LABELS = {'users': 'المستخدمين (الخاص)', 'channels': 'القنوات', 'groups': 'المجموعات', 'all': 'الجميع'}
bc_drafts = {}
bc_running = {}
_chats_lock = threading.Lock()

_c_chats = None

def load_chats():
    global _c_chats
    if _c_chats is not None: return _c_chats
    with _chats_lock:
        if _c_chats is not None: return _c_chats
        d = {}
        if os.path.exists(CHATS_FILE):
            try:
                with open(CHATS_FILE, 'r', encoding='utf-8') as f:
                    x = json.load(f)
                    if isinstance(x, dict): d = x
            except Exception: pass
        _c_chats = d
        return _c_chats

def save_chats(data):
    global _c_chats
    _c_chats = data
    _queue_write(CHATS_FILE, lambda: _c_chats)

def _bc_raw(kind):
    """قائمة الأهداف الخام (بدون استدعاءات API) مع إزالة التكرار"""
    out = []; seen = set()
    def add(t):
        if t is None: return
        k = str(t)
        if k in seen: return
        seen.add(k); out.append(t)
    if kind in ('users', 'all'):
        for u, d in load_db().items():
            if isinstance(d, dict) and not d.get('banned', False):
                try: add(int(u))
                except Exception: pass
    want_ch = kind in ('channels', 'all'); want_gr = kind in ('groups', 'all')
    if want_ch or want_gr:
        for cid, info in load_chats().items():
            if not info.get('active', True): continue
            t = info.get('type')
            if (want_ch and t == 'channel') or (want_gr and t in ('group', 'supergroup')):
                add(int(cid))
        for item in load_force_sub():
            if not isinstance(item, dict): continue
            if (want_ch and item.get('kind') == 'channel') or (want_gr and item.get('kind') == 'group'):
                add(item.get('target'))
        if want_ch:
            for item in list(load_points_channels()) + list(load_turbo_channels()):
                if isinstance(item, dict): add(item.get('target'))
    return out

def _url_only_markup(markup):
    """يُبقي أزرار الروابط فقط (أزرار callback ما تنفع بالإذاعة) مع نفس شكلها وألوانها"""
    try:
        rows = [[b for b in row if getattr(b, 'url', None)] for row in (markup.keyboard or [])]
        rows = [r for r in rows if r]
        if not rows: return None
        m = types.InlineKeyboardMarkup()
        for r in rows: m.row(*r)
        return m
    except Exception:
        return None

def _bc_send_one(target, draft):
    if draft['ctype'] == 'text':
        return bot.send_message(target, draft['html'], parse_mode='HTML', disable_web_page_preview=True, reply_markup=draft.get('markup'))
    kw = {}
    if draft.get('markup') is not None: kw['reply_markup'] = draft['markup']
    cap = draft.get('caption')
    if cap:
        mp = _te_map()
        if mp:
            conv = _premium_html(cap, mp)
            if conv != cap: kw.update({'caption': conv, 'parse_mode': 'HTML'})
    return bot.copy_message(target, draft['chat_id'], draft['message_id'], **kw)

def _bc_deliver(target, draft):
    for _ in range(4):
        try:
            _bc_send_one(target, draft)
            return 'ok'
        except Exception as ex:
            code = getattr(ex, 'error_code', None)
            desc = str(getattr(ex, 'description', ex)).lower()
            ra = None
            try: ra = (ex.result_json or {}).get('parameters', {}).get('retry_after')
            except Exception: pass
            if code == 429 or ra:
                time.sleep(min(int(ra or 3), 60) + 1)
                continue
            if any(w in desc for w in ('blocked', 'kicked', 'deactivated', 'chat not found', 'not a member',
                                        'forbidden', 'not enough rights', 'need administrator', 'have no rights', 'was deleted')):
                return 'blocked'
            return 'failed'
    return 'failed'

def _bc_bar(done, total, width=12):
    filled = int(width * done / total) if total else width
    return '▓' * filled + '░' * (width - filled)

def _bc_run(uid, chat_id, status_id, draft):
    ctrl = bc_running.get(uid) or {'stop': False}
    kind = draft['kind']
    sent = blocked = failed = 0
    bad_chats = []
    try:
        targets = []  # (id, is_chat, title)
        if kind in ('users', 'all'):
            targets += [(u, False, None) for u in _bc_raw('users')]
        if kind != 'users':
            wanted = set()
            if kind in ('channels', 'all'): wanted.add('channel')
            if kind in ('groups', 'all'): wanted |= {'group', 'supergroup'}
            seen_ids = set()
            for t in _bc_raw(kind if kind != 'all' else 'groups') + (_bc_raw('channels') if kind == 'all' else []):
                ch = safe_get_chat(t)
                if ch is None:
                    failed += 1; bad_chats.append(str(t)); continue
                if ch.id in seen_ids or ch.type not in wanted: continue
                seen_ids.add(ch.id)
                targets.append((ch.id, True, getattr(ch, 'title', None) or str(ch.id)))
        total = len(targets)
        if total == 0:
            bot.edit_message_text("❌ لا يوجد مستهدفين من هذا النوع.", chat_id, status_id)
            return
        stop_markup = types.InlineKeyboardMarkup()
        stop_markup.add(make_btn('⏹️ إيقاف', callback_data='dev_bcstop', style='danger', icon_key='__nav__'))
        last_edit = 0.0
        done = 0

        def _work(item):
            if ctrl.get('stop'): return item, None
            r = _bc_deliver(item[0], draft)
            time.sleep(0.3)      # يبقينا ضمن حدود تيليجرام (~20 رسالة/ثانية بالمجموع)
            return item, r

        with ThreadPoolExecutor(max_workers=10) as _ex:
            futs = [_ex.submit(_work, t) for t in targets]
            for fut in as_completed(futs):
                (tid, is_chat, title), res = fut.result()
                if res is None: continue
                if res == 'ok': sent += 1
                elif res == 'blocked': blocked += 1
                else: failed += 1
                if res != 'ok' and is_chat: bad_chats.append(title or str(tid))
                done += 1
                now = time.time()
                if now - last_edit >= 3 or done == total:
                    last_edit = now
                    try:
                        bot.edit_message_text(
                            f"📤 <b>جاري الإذاعة...</b>\n\n{_bc_bar(done, total)} {done}/{total}\n\n"
                            f"✅ نجح: {sent}\n🚫 محظور/غير متاح: {blocked}\n❌ فشل: {failed}",
                            chat_id, status_id, parse_mode='HTML', reply_markup=stop_markup)
                    except Exception: pass
        stopped = ctrl.get('stop')
        title_line = "⏹️ <b>تم إيقاف الإذاعة</b>" if stopped else "✅ <b>اكتملت الإذاعة!</b>"
        report = (f"{title_line}\n\n🎯 النوع: {BC_LABELS.get(kind, '')}\n"
                  f"📊 تمت معالجة: {done}/{total}\n\n"
                  f"✅ نجح: <b>{sent}</b>\n🚫 محظور/غير متاح: <b>{blocked}</b>\n❌ فشل: <b>{failed}</b>")
        if bad_chats:
            report += "\n\n⚠️ قنوات/مجموعات لم يصلها الإرسال:\n" + "\n".join(f"• {html.escape(str(x))}" for x in bad_chats[:10])
            if len(bad_chats) > 10: report += f"\n... و {len(bad_chats) - 10} أخرى"
            report += "\n\n💡 تأكد أن البوت مشرف بالقنوات وعضو بالمجموعات."
        end_markup = types.InlineKeyboardMarkup()
        end_markup.add(make_btn('🔙 رجوع للوحة', callback_data='dev_panel', style='primary'))
        try: bot.edit_message_text(report, chat_id, status_id, parse_mode='HTML', reply_markup=end_markup)
        except Exception: bot.send_message(chat_id, report, parse_mode='HTML', reply_markup=end_markup)
    except Exception as ex:
        try: bot.send_message(chat_id, f"❌ حدث خطأ أثناء الإذاعة: {html.escape(str(ex)[:300])}", parse_mode='HTML')
        except Exception: pass
    finally:
        bc_running.pop(uid, None)

def dev_bc_send_process(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_dev_panel(message.chat.id, uid)

    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_bc_send':
        send_dev_panel(message.chat.id, uid)
        return

    ctype = message.content_type
    draft = {'kind': state['bc_type'], 'ctype': 'text' if ctype == 'text' else 'copy',
             'chat_id': message.chat.id, 'message_id': message.message_id}
    if ctype == 'text':
        draft['html'] = message.html_text or message.text
        if not draft['html']:
            bot.reply_to(message, "❌ الرسالة فارغة.")
            return send_dev_panel(message.chat.id, uid)
    else:
        draft['caption'] = getattr(message, 'html_caption', None)

    if getattr(message, 'reply_markup', None) is not None:
        draft['markup'] = _url_only_markup(message.reply_markup)
    _bc_confirm(message.chat.id, uid, draft)

def _bc_confirm(chat_id, uid, draft):
    """معاينة الإذاعة + زر التأكيد"""
    kind = draft['kind']
    count = len(_bc_raw(kind))
    if count == 0:
        bot.send_message(chat_id, "❌ لا يوجد مستهدفين من هذا النوع في البوت.")
        return send_dev_panel(chat_id, uid)
    bc_drafts[uid] = draft
    try:
        bot.send_message(chat_id, "👁️ <b>معاينة الإذاعة:</b>", parse_mode='HTML')
        _bc_send_one(chat_id, draft)
    except Exception as ex:
        bot.send_message(chat_id, f"⚠️ تعذّر عرض المعاينة: {html.escape(str(ex)[:200])}", parse_mode='HTML')
    text = (f"📢 <b>تأكيد الإذاعة</b>\n\n🎯 المستهدف: <b>{BC_LABELS.get(kind, '')}</b>\n"
            f"👥 العدد التقريبي: <b>{count}</b>\n\nهل تريد إرسالها الآن؟")
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(make_btn('✅ تأكيد وإرسال', callback_data='dev_bcgo', style='success', icon_key='__nav__'),
               make_btn('❌ إلغاء', callback_data='dev_broadcast', style='danger', icon_key='__nav__'))
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='HTML')

# ================= دوال التمويل (المعدلة) =================
def process_fund_count(message, min_fund, max_fund, rate):
    uid = message.from_user.id
    if is_banned(uid) or not enforce_force_sub_message(message): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.get(uid)
    if not state or state.get('step') != 'fund_count': return
    try:
        count = int(message.text)
    except Exception:
        msg = bot.reply_to(message, "❌ أرسل رقماً صحيحاً أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, process_fund_count, min_fund, max_fund, rate)
        return
    if count < min_fund:
        msg = bot.reply_to(message, f"❌ الحد الأدنى للتمويل هو {min_fund} عضو.\nأرسل عدداً يساوي {min_fund} أو أكثر:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, process_fund_count, min_fund, max_fund, rate)
        return
    if count > max_fund:
        msg = bot.reply_to(message, f"❌ الحد الأقصى للتمويل هو {max_fund} عضو.\nأرسل عدداً أقل أو يساوي {max_fund}:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, process_fund_count, min_fund, max_fund, rate)
        return
    ud = get_user_data(uid)
    cost = count * rate
    if ud.get('balance', 0) < cost:
        max_affordable = ud.get('balance', 0) // rate
        msg = bot.reply_to(message, f"❌ رصيدك غير كافٍ.\n💎 رصيدك: {ud.get('balance', 0)}\n💸 المطلوب: {cost}\n📊 أقصى عدد: {max_affordable}", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, process_fund_count, min_fund, max_fund, rate)
        return
    user_states[uid] = {'step': 'fund_link', 'count': count, 'rate': rate}
    msg = bot.reply_to(message, "🔗 <b>أرسل رابط قناتك أو معرفها (مثال: @FD_CQ)</b>\n\n⚠️ <b>ملاحظة هامة:</b> يجب رفع البوت <b>مشرفاً</b> <a href=\"https://t.me/SWUDBOT\">@SWUDBOT</a> في القناة أولاً.\n\nأو اضغط على زر الإلغاء بالأسفل", parse_mode='HTML', disable_web_page_preview=True, reply_markup=get_cancel_markup())
    bot.register_next_step_handler(msg, process_fund_link, count, rate)

def process_fund_link(message, count, rate=None):
    uid = message.from_user.id
    if is_banned(uid) or not enforce_force_sub_message(message): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'fund_link': return
    def _retry(txt):
        m2 = bot.send_message(message.chat.id, txt, parse_mode='HTML', disable_web_page_preview=True, reply_markup=get_cancel_markup())
        user_states[uid] = {'step': 'fund_link', 'count': count, 'rate': rate}
        bot.register_next_step_handler(m2, process_fund_link, count, rate)

    parsed = extract_channel_target(message)
    if parsed is None or parsed[0] == 'private':
        return _retry("❌ <b>ما قدرت أتعرف على القناة.</b>\n\n"
                      "أرسل يوزر القناة مثل <code>@FD_CQ</code> أو رابطها <code>https://t.me/FD_CQ</code> أو وجّه (Forward) أي منشور من القناة.\n\n"
                      "⚠️ رابط الدعوة الخاص (<code>t.me/+...</code>) ما يشتغل، استخدم اليوزر أو وجّه منشور من القناة.")
    target = parsed[1]

    if rate is None: rate = state.get('rate') or load_settings().get('fund_rate', 50)
    cost = int(count) * int(rate)

    chat_obj = safe_get_chat(target)
    if chat_obj is None and isinstance(target, str):
        return _retry(f"❌ <b>ما لقيت قناة بهذا اليوزر:</b> <code>{html.escape(target)}</code>\n\nتأكد من كتابته وأرسله مرة ثانية.")
    if chat_obj is None or not is_bot_admin_in_chat(chat_obj.id):
        return _retry("❌ <b>البوت غير مشرف في القناة!</b>\n\n"
                      "جرّب ترفع البوت مشرفاً <a href=\"https://t.me/SWUDBOT\">@SWUDBOT</a> في قناتك:\n"
                      "1️⃣ افتح قناتك ← المشرفين ← إضافة مشرف\n"
                      "2️⃣ ابحث عن البوت <a href=\"https://t.me/SWUDBOT\">@SWUDBOT</a> وأضفه مشرفاً\n"
                      "3️⃣ أرسل رابط القناة مرة ثانية 👇")

    channel_link = f"@{chat_obj.username}" if getattr(chat_obj, 'username', None) else str(chat_obj.id)
    channel_title = getattr(chat_obj, 'title', None) or channel_link
    try: start_members = int(bot.get_chat_member_count(chat_obj.id))
    except Exception: start_members = None
    
    ud = get_user_data(uid)
    if ud.get('balance', 0) < cost:
        bot.reply_to(message, "❌ رصيدك غير كافٍ.")
        return send_welcome(message)
        
    # حفظ المعلومات في الذاكرة لعرض شاشة التأكيد
    user_states[uid] = {
        'step': 'fund_confirm', 'count': count, 'rate': rate, 
        'link': channel_link, 'title': channel_title, 'start_members': start_members, 'cost': cost
    }
    
    confirm_text = (
        f"⚠️ <b>تأكيد طلب التمويل</b>\n"
        f"────────────────────\n"
        f"📌 <b>القناة:</b> {html.escape(channel_title)}\n"
        f"👥 <b>عدد الأعضاء:</b> {count}\n"
        f"💵 <b>التكلفة:</b> {cost:,} نقطة\n"
        f"💎 <b>رصيدك المتبقي بعد التمويل:</b> {ud.get('balance', 0) - cost:,} نقطة\n"
        f"────────────────────\n"
        f"هل أنت متأكد من رغبتك في بدء عملية التمويل؟"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(make_btn('✅ تأكيد الطلب', callback_data='fund_confirm_yes', style='success'), make_btn('❌ إلغاء الطلب', callback_data='fund_confirm_no', style='danger'))
    
    bot.send_message(message.chat.id, confirm_text, reply_markup=markup, parse_mode='HTML')

# ================= باقي دوال الوظائف =================
def transfer_target(message):
    uid = message.from_user.id
    if is_banned(uid) or not enforce_force_sub_message(message): return
    settings = load_settings()
    if not settings.get('transfer_enabled', True) and not is_dev(uid):
        bot.reply_to(message, "⚠️ عذراً، خدمة تحويل النقاط معطلة حالياً من قبل الإدارة.")
        return send_welcome(message)
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.get(uid)
    if not state or state.get('step') != 'transfer_target': return
    try: target = int(message.text)
    except Exception:
        msg = bot.reply_to(message, "❌ أرسل ID رقمي صحيح أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, transfer_target)
        return
    if target == uid:
        msg = bot.reply_to(message, "❌ لا يمكنك التحويل لنفسك. أرسل ID آخر:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, transfer_target)
        return
    db = load_db()
    if str(target) not in db:
        msg = bot.reply_to(message, "❌ هذا المستخدم غير موجود في البوت. أرسل ID آخر:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, transfer_target)
        return
    if db[str(target)].get('banned', False):
        msg = bot.reply_to(message, "❌ لا يمكنك التحويل لمستخدم محظور. أرسل ID آخر:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, transfer_target)
        return
    user_states[uid] = {'step': 'transfer_amount', 'target': target}
    msg = bot.reply_to(message, "💰 أرسل عدد النقاط المراد تحويلها أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
    bot.register_next_step_handler(msg, transfer_amount)

def transfer_amount(message):
    uid = message.from_user.id
    if is_banned(uid) or not enforce_force_sub_message(message): return
    settings = load_settings()
    if not settings.get('transfer_enabled', True) and not is_dev(uid):
        bot.reply_to(message, "⚠️ عذراً، خدمة تحويل النقاط معطلة حالياً من قبل الإدارة.")
        return send_welcome(message)
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'transfer_amount': return
    try:
        amount = int(message.text)
        if amount <= 0: raise ValueError()
    except Exception:
        user_states[uid] = state
        msg = bot.reply_to(message, "❌ أرسل عدداً صحيحاً موجباً أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, transfer_amount)
        return
    target = state['target']
    db = load_db()
    if str(uid) not in db: db[str(uid)] = {}
    if str(target) not in db: db[str(target)] = {}
    sender_balance = db[str(uid)].get('balance', 0)
    if sender_balance < amount:
        bot.reply_to(message, f"❌ رصيدك غير كافٍ.\n💎 رصيدك: {sender_balance}\n💸 المطلوب: {amount}")
        return send_welcome(message)
    db[str(uid)]['balance'] = sender_balance - amount
    db[str(uid)]['total_transfers'] = db[str(uid)].get('total_transfers', 0) + 1
    db[str(target)]['balance'] = db[str(target)].get('balance', 0) + amount
    _log_tx(db[str(uid)], -amount, 'transfer_out', f'تحويل إلى {target}')
    _log_tx(db[str(target)], amount, 'transfer_in', f'تحويل من {uid}')
    save_db(db)
    bot.reply_to(message, f"✅ <b>تم التحويل بنجاح!</b>\n\n💸 {amount} نقطة إلى <code>{target}</code>\n💎 رصيدك الآن: {db[str(uid)]['balance']}", parse_mode='HTML')
    try: bot.send_message(target, f"🎁 <b>استلمت {amount} نقطة!</b>\n💎 رصيدك الآن: {db[str(target)]['balance']}", parse_mode='HTML')
    except Exception: pass
    send_welcome(message)

def redeem_coupon(message):
    uid = message.from_user.id
    if is_banned(uid) or not enforce_force_sub_message(message): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'redeem_coupon': return
    code = (message.text or '').strip().upper()
    result = None
    with _coupon_lock:
        coupons = load_coupons()
        info = coupons.get(code)
        if not isinstance(info, dict):
            result = ('err', "❌ الكود غير صحيح أو غير موجود.")
        elif uid in info.get('used_by', []):
            result = ('err', "❌ لقد استخدمت هذا الكود بالفعل!\n\nكل شخص يكدر يستخدم الكود مرة وحدة فقط.")
        elif info.get('used', 0) >= info.get('max_uses', 0):
            result = ('err', "❌ انتهت صلاحية هذا الكود!\n\nاكتمل عدد الأعضاء المسموح لهم.")
        else:
            points = int(info.get('points', 0))
            db = load_db()
            if str(uid) not in db or not isinstance(db[str(uid)], dict): db[str(uid)] = {}
            db[str(uid)]['balance'] = db[str(uid)].get('balance', 0) + points
            _log_tx(db[str(uid)], points, 'coupon', 'استخدام قسيمة')
            save_db(db)
            info['used'] = info.get('used', 0) + 1
            info.setdefault('used_by', []).append(uid)
            save_coupons(coupons)
            result = ('ok', points, db[str(uid)]['balance'], info['used'], info.get('max_uses', 0))
    if result[0] == 'err':
        bot.reply_to(message, result[1])
        return send_welcome(message)
    _, points, new_bal, used, mx = result
    bot.reply_to(message, f"✅ <b>تم تفعيل القسيمة!</b>\n\n🎁 +{points:,} نقطة\n💎 رصيدك الآن: {new_bal:,}", parse_mode='HTML')
    notify_dev_points_used('كود قسيمة', code, uid, points, used, mx)
    send_welcome(message)

# ================= دوال المطور الباقية =================
def dev_set_min_fund(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_set_min_fund':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        new_val = int(message.text)
        if new_val <= 0: raise ValueError()
    except Exception:
        user_states[uid] = state
        msg = bot.reply_to(message, "❌ أرسل رقماً صحيحاً أكبر من صفر أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_min_fund)
        return
    settings = load_settings()
    if new_val > settings.get('max_fund', 20000):
        user_states[uid] = state
        msg = bot.reply_to(message, f"❌ لا يمكن أن يتجاوز الحد الأقصى ({settings.get('max_fund', 20000)}). أرسل رقماً مناسباً:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_min_fund)
        return
    settings['min_fund'] = new_val
    save_settings(settings)
    bot.reply_to(message, f"✅ <b>تم تعديل الحد الأدنى للتمويل!</b>\n\n📉 الحد الأدنى الجديد: <code>{new_val}</code> عضو", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

def dev_set_max_fund(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_set_max_fund':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        new_val = int(message.text)
        if new_val <= 0: raise ValueError()
    except Exception:
        user_states[uid] = state
        msg = bot.reply_to(message, "❌ أرسل رقماً صحيحاً أكبر من صفر أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_max_fund)
        return
    settings = load_settings()
    if new_val < settings.get('min_fund', 100):
        user_states[uid] = state
        msg = bot.reply_to(message, f"❌ لا يمكن أن يكون أقل من الحد الأدنى ({settings.get('min_fund', 100)}). أرسل رقماً مناسباً:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_max_fund)
        return
    settings['max_fund'] = new_val
    save_settings(settings)
    bot.reply_to(message, f"✅ <b>تم تعديل الحد الأقصى للتمويل!</b>\n\n📈 الحد الأقصى الجديد: <code>{new_val}</code> عضو", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

def dev_set_stars_bonus(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_set_stars_bonus':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        new_val = int((message.text or '').strip().replace('%', ''))
        if new_val < 0 or new_val > 1000: raise ValueError()
    except Exception:
        user_states[uid] = state
        msg = bot.reply_to(message, "❌ أرسل رقماً صحيحاً من 0 إلى 1000 أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_stars_bonus)
        return
    settings = load_settings()
    settings['stars_bonus_percent'] = new_val
    save_settings(settings)
    threading.Thread(target=warm_stars_links, daemon=True).start()
    bot.reply_to(message, f"✅ <b>تم تعديل مكافأة شحن النجوم!</b>\n\n⭐ القيمة الجديدة: <code>{new_val}%</code>", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

def dev_set_daily(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_set_daily':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        new_val = int(message.text)
        if new_val < 0: raise ValueError()
    except Exception:
        user_states[uid] = state
        msg = bot.reply_to(message, "❌ أرسل رقماً صحيحاً موجباً أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_daily)
        return
    settings = load_settings()
    settings['daily_reward'] = new_val
    save_settings(settings)
    bot.reply_to(message, f"✅ <b>تم تعديل الهدية اليومية بنجاح!</b>\n\n🎁 القيمة الجديدة: <code>{new_val}</code> نقطة", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

def dev_set_fund(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_set_fund':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        new_val = int(message.text)
        if new_val <= 0: raise ValueError()
    except Exception:
        user_states[uid] = state
        msg = bot.reply_to(message, "❌ أرسل رقماً صحيحاً أكبر من صفر أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_fund)
        return
    settings = load_settings()
    settings['fund_rate'] = new_val
    save_settings(settings)
    bot.reply_to(message, f"✅ <b>تم تعديل سعر العضو بنجاح!</b>\n\n💵 السعر الجديد: <code>{new_val}</code> نقطة لكل عضو", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

def dev_set_ref(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_set_ref':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        new_val = int(message.text)
        if new_val < 0: raise ValueError()
    except Exception:
        user_states[uid] = state
        msg = bot.reply_to(message, "❌ أرسل رقماً صحيحاً موجباً أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_ref)
        return
    settings = load_settings()
    settings['ref_reward'] = new_val
    save_settings(settings)
    bot.reply_to(message, f"✅ <b>تم تعديل نقاط رابط الدعوة بنجاح!</b>\n\n🤝 النقاط الجديدة: <code>{new_val}</code> نقطة", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

def dev_set_bot_name(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_set_bot_name':
        send_dev_panel(message.chat.id, uid)
        return
    new_name = (message.text or '').strip()
    if not new_name:
        user_states[uid] = state
        msg = bot.reply_to(message, "❌ الاسم لا يمكن أن يكون فارغاً:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_bot_name)
        return
    settings = load_settings()
    settings['bot_name'] = new_name
    save_settings(settings)
    bot.reply_to(message, f"✅ <b>تم تغيير اسم البوت بنجاح!</b>\n\n🤖 الاسم الجديد: <code>{html.escape(new_name)}</code>", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

def dev_coupon_code(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.get(uid)
    if not state or state.get('step') != 'dev_coupon_code':
        send_dev_panel(message.chat.id, uid)
        return
    code = (message.text or '').strip().upper()
    if not code or ' ' in code:
        msg = bot.reply_to(message, "❌ الاسم غير صالح (بدون مسافات):", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_coupon_code)
        return
    coupons = load_coupons()
    if code in coupons:
        msg = bot.reply_to(message, "❌ هذا الاسم موجود بالفعل، اختر اسم ثاني:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_coupon_code)
        return
    user_states[uid] = {'step': 'dev_coupon_points', 'code': code}
    msg = bot.reply_to(message, "💰 الخطوة 2 من 3:\nأرسل <b>عدد النقاط</b> اللي ياخذها كل عضو:", parse_mode='HTML', reply_markup=get_cancel_markup())
    bot.register_next_step_handler(msg, dev_coupon_points)

def dev_coupon_points(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.get(uid)
    if not state or state.get('step') != 'dev_coupon_points':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        points = int(message.text)
        if points <= 0: raise ValueError()
    except Exception:
        msg = bot.reply_to(message, "❌ أرسل عدداً صحيحاً موجباً:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_coupon_points)
        return
    state['points'] = points
    state['step'] = 'dev_coupon_uses'
    user_states[uid] = state
    msg = bot.reply_to(message, "👥 الخطوة 3 من 3:\nأرسل <b>عدد الأعضاء</b> المسموح لهم يستخدمون القسيمة:", parse_mode='HTML', reply_markup=get_cancel_markup())
    bot.register_next_step_handler(msg, dev_coupon_uses)

def dev_coupon_uses(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_coupon_uses':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        uses = int(message.text)
        if uses <= 0: raise ValueError()
    except Exception:
        user_states[uid] = state
        msg = bot.reply_to(message, "❌ أرسل عدداً صحيحاً موجباً:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_coupon_uses)
        return
    code = state['code']
    points = state['points']
    coupons = load_coupons()
    coupons[code] = {'points': points, 'max_uses': uses, 'used': 0, 'used_by': []}
    save_coupons(coupons)
    bot.reply_to(message, f"✅ <b>تم إنشاء القسيمة!</b>\n\n🎟️ الاسم/الكود: <code>{html.escape(code)}</code>\n💰 النقاط لكل عضو: {points:,}\n👥 عدد الأعضاء: {uses:,}\n\n📤 أرسل الكود للأعضاء، يدخلونه من زر «🎟️ قسائمي» ويحصلون النقاط (كل شخص مرة وحدة فقط).\n🔔 توصلك رسالة مع كل شخص يستخدمه.", parse_mode='HTML')
    send_points_post(message.chat.id, 'code', points, uses, code=code)
    send_dev_panel(message.chat.id, uid)

def dev_edit_balance_id(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.get(uid)
    if not state or state.get('step') != 'dev_edit_balance_id':
        send_dev_panel(message.chat.id, uid)
        return
    try: target = int(message.text)
    except Exception:
        msg = bot.reply_to(message, "❌ أرسل ID رقمي صحيح:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_edit_balance_id)
        return
    db = load_db()
    if str(target) not in db:
        msg = bot.reply_to(message, "❌ المستخدم غير موجود. أرسل ID آخر:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_edit_balance_id)
        return
    user_states[uid] = {'step': 'dev_edit_balance_amount', 'target': target}
    current = db[str(target)].get('balance', 0)
    msg = bot.reply_to(message, f"💰 الرصيد الحالي: {current}\n\nأرسل المبلغ المراد إضافته (أو رقم سالب للخصم):", reply_markup=get_cancel_markup())
    bot.register_next_step_handler(msg, dev_edit_balance_amount)

def dev_edit_balance_amount(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_edit_balance_amount':
        send_dev_panel(message.chat.id, uid)
        return
    try: amount = int(message.text)
    except Exception:
        user_states[uid] = state
        msg = bot.reply_to(message, "❌ أرسل رقماً صحيحاً:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_edit_balance_amount)
        return
    target = state['target']
    db = load_db()
    if str(target) not in db: db[str(target)] = {}
    db[str(target)]['balance'] = db[str(target)].get('balance', 0) + amount
    _log_tx(db[str(target)], amount, 'admin', 'شحن من الإدارة' if amount >= 0 else 'خصم من الإدارة')
    save_db(db)
    sign = "+" if amount >= 0 else ""
    # إشعار للمستخدم بالخاص عند الشحن
    notified = False
    if amount > 0:
        try:
            bot.send_message(target, f"✅ <b>تم شحن حسابك!</b>\n\n💰 عدد النقاط المشحونة: <b>{amount:,}</b> نقطة\n💎 رصيدك الآن: <b>{db[str(target)]['balance']:,}</b> نقطة", parse_mode='HTML')
            notified = True
        except Exception: pass
    note = ("\n📩 وصل المستخدم إشعار بالشحن." if notified else ("\n⚠️ تعذّر إرسال الإشعار للمستخدم (ربما حظر البوت)." if amount > 0 else ""))
    bot.reply_to(message, f"✅ <b>تم التعديل!</b>\n\n👤 المستخدم: <code>{target}</code>\n💰 التغيير: {sign}{amount}\n💎 الرصيد الجديد: {db[str(target)]['balance']}{note}", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

def dev_ban_user_id(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_ban_user_id':
        send_dev_panel(message.chat.id, uid)
        return
    try: target = int(message.text)
    except Exception:
        msg = bot.reply_to(message, "❌ أرسل ID رقمي صحيح:", reply_markup=get_cancel_markup())
        user_states[uid] = {'step': 'dev_ban_user_id'}
        bot.register_next_step_handler(msg, dev_ban_user_id)
        return
    if target == DEV_ID:
        bot.reply_to(message, "❌ لا يمكنك حظر نفسك!")
        return send_dev_panel(message.chat.id, uid)
    db = load_db()
    if str(target) not in db:
        bot.reply_to(message, "❌ هذا المستخدم غير موجود في البوت.")
        return send_dev_panel(message.chat.id, uid)
    db[str(target)]['banned'] = True
    save_db(db)
    bot.reply_to(message, f"✅ <b>تم حظر المستخدم بنجاح!</b>\n\n👤 الآيدي: <code>{target}</code>", parse_mode='HTML')
    try: bot.send_message(target, "🚫 <b>لقد تم حظرك من استخدام البوت.</b>", parse_mode='HTML')
    except Exception: pass
    send_dev_panel(message.chat.id, uid)

def dev_unban_user_id(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_unban_user_id':
        send_dev_panel(message.chat.id, uid)
        return
    try: target = int(message.text)
    except Exception:
        msg = bot.reply_to(message, "❌ أرسل ID رقمي صحيح:", reply_markup=get_cancel_markup())
        user_states[uid] = {'step': 'dev_unban_user_id'}
        bot.register_next_step_handler(msg, dev_unban_user_id)
        return
    db = load_db()
    if str(target) not in db:
        bot.reply_to(message, "❌ هذا المستخدم غير موجود في البوت.")
        return send_dev_panel(message.chat.id, uid)
    db[str(target)]['banned'] = False
    save_db(db)
    bot.reply_to(message, f"✅ <b>تم فك الحظر بنجاح!</b>\n\n👤 الآيدي: <code>{target}</code>", parse_mode='HTML')
    try: bot.send_message(target, "✅ <b>تم فك الحظر عنك، يمكنك استخدام البوت الآن!</b>\nأرسل /start للبدء.", parse_mode='HTML')
    except Exception: pass
    send_dev_panel(message.chat.id, uid)

def dev_del_coupon_code(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_del_coupon':
        send_dev_panel(message.chat.id, uid)
        return
    code = (message.text or '').strip().upper()
    coupons = load_coupons()
    if code not in coupons:
        bot.reply_to(message, "❌ الكود غير موجود.")
        return send_dev_panel(message.chat.id, uid)
    del coupons[code]
    save_coupons(coupons)
    bot.reply_to(message, f"✅ تم حذف القسيمة <code>{html.escape(code)}</code>", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

# ================= إنشاء رابط نقاط =================
def dev_gift_points(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_dev_panel(message.chat.id, uid)
    state = user_states.get(uid)
    if not state or state.get('step') != 'dev_gift_points':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        points = int(_clean_text(message.text or '').replace(',', ''))
        if points <= 0: raise ValueError()
    except Exception:
        msg = bot.reply_to(message, "❌ أرسل عدداً صحيحاً موجباً أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_gift_points)
        return
    user_states[uid] = {'step': 'dev_gift_uses', 'points': points}
    msg = bot.reply_to(message, "👥 الخطوة 2 من 2:\nأرسل عدد الأشخاص المسموح لهم يستخدمون الرابط:", reply_markup=get_cancel_markup())
    bot.register_next_step_handler(msg, dev_gift_uses)

def dev_gift_uses(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_dev_panel(message.chat.id, uid)
    state = user_states.get(uid)
    if not state or state.get('step') != 'dev_gift_uses':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        uses = int(_clean_text(message.text or '').replace(',', ''))
        if uses <= 0: raise ValueError()
    except Exception:
        msg = bot.reply_to(message, "❌ أرسل عدداً صحيحاً موجباً أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_gift_uses)
        return
    user_states.pop(uid, None)
    points = state['points']
    with _gift_lock:
        links = load_gift_links()
        code = secrets.token_hex(4)
        while code in links: code = secrets.token_hex(4)
        links[code] = {'points': points, 'max_uses': uses, 'used': 0, 'used_by': [], 'created_at': int(time.time())}
        save_gift_links(links)
    bot.reply_to(message,
                 f"✅ <b>تم إنشاء رابط النقاط!</b>\n\n🎁 النقاط لكل شخص: <b>{points:,}</b>\n👥 عدد الأشخاص: <b>{uses:,}</b>\n\n"
                 f"🔗 الرابط (اضغط عليه للنسخ):\n<code>{gift_link_url(code)}</code>\n\n"
                 "كل شخص ياخذ النقاط مرة وحدة فقط، وينتهي الرابط بعد اكتمال العدد.",
                 parse_mode='HTML', disable_web_page_preview=True)
    send_points_post(message.chat.id, 'link', points, uses, link=gift_link_url(code))
    send_dev_panel(message.chat.id, uid)

# ================= دوال إضافة القنوات =================
def dev_add_fsub_process(message, kind):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_add_fsub':
        send_dev_panel(message.chat.id, uid)
        return
    raw = (message.text or '').strip()
    target, url = parse_chat_identifier_and_url(raw)
    if not raw or (target is None and not url):
        msg = bot.reply_to(message, "❌ أرسل رابطاً أو معرفاً صحيحاً أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        user_states[uid] = {'step': 'dev_add_fsub', 'kind': kind}
        bot.register_next_step_handler(msg, dev_add_fsub_process, kind)
        return
    title = raw
    chat_obj = safe_get_chat(target)
    if chat_obj is None or not is_bot_admin_in_chat(chat_obj.id):
        msg = bot.reply_to(message, "❌ <b>ما أقدر أتحقق من هذي القناة/الكروب.</b>\n\nارفع البوت <b>مشرف (Admin)</b> فيها أولاً، وأرسل <b>معرفها (@username)</b> أو <b>الآيدي الرقمي</b> (مثل <code>-1001234567890</code>) وليس رابط الدعوة الخاص.\n\nأو اضغط على زر الإلغاء:", parse_mode='HTML', reply_markup=get_cancel_markup())
        user_states[uid] = {'step': 'dev_add_fsub', 'kind': kind}
        bot.register_next_step_handler(msg, dev_add_fsub_process, kind)
        return
    target = chat_obj.id if not getattr(chat_obj, 'username', None) else target
    if getattr(chat_obj, 'title', None): title = chat_obj.title
    if not url and getattr(chat_obj, 'username', None): url = f"https://t.me/{chat_obj.username}"
    if not url:
        msg = bot.reply_to(message, "🔗 أرسل رابط الدعوة المباشر أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        user_states[uid] = {'step': 'dev_add_fsub_url', 'kind': kind, 'target': target, 'title': title}
        bot.register_next_step_handler(msg, dev_add_fsub_url_process)
        return
    items = load_force_sub()
    items.append({'kind': kind, 'target': target, 'url': url, 'title': title})
    save_force_sub(items)
    bot.reply_to(message, f"✅ تم إضافة {('القناة' if kind == 'channel' else 'الكروب')} <b>{html.escape(title)}</b> للاشتراك الإجباري!", parse_mode='HTML')
    text, markup = get_force_sub_menu_data()
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')

def dev_add_fsub_url_process(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_add_fsub_url':
        send_dev_panel(message.chat.id, uid)
        return
    url = (message.text or '').strip()
    if not (url.startswith('https://t.me/') or url.startswith('http://t.me/')):
        msg = bot.reply_to(message, "❌ أرسل رابط تيليجرام يبدأ بـ https://t.me/ أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        state['step'] = 'dev_add_fsub_url'
        user_states[uid] = state
        bot.register_next_step_handler(msg, dev_add_fsub_url_process)
        return
    items = load_force_sub()
    items.append({'kind': state.get('kind', 'channel'), 'target': state.get('target'), 'url': url, 'title': state.get('title') or url})
    save_force_sub(items)
    bot.reply_to(message, "✅ تم الإضافة بنجاح للاشتراك الإجباري!")
    text, markup = get_force_sub_menu_data()
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')

def dev_add_pt_channel_process(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_add_pt_channel':
        send_dev_panel(message.chat.id, uid)
        return
    raw = (message.text or '').strip()
    target, url = parse_chat_identifier_and_url(raw)
    if not raw or (target is None and not url):
        msg = bot.reply_to(message, "❌ أرسل رابطاً أو معرفاً صحيحاً أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        user_states[uid] = {'step': 'dev_add_pt_channel'}
        bot.register_next_step_handler(msg, dev_add_pt_channel_process)
        return
    title = raw
    chat_obj = safe_get_chat(target)
    if chat_obj is not None:
        if getattr(chat_obj, 'title', None): title = chat_obj.title
        if not url and getattr(chat_obj, 'username', None): url = f"https://t.me/{chat_obj.username}"
    if not url:
        msg = bot.reply_to(message, "🔗 أرسل رابط الدعوة المباشر أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        user_states[uid] = {'step': 'dev_add_pt_channel_url', 'target': target, 'title': title}
        bot.register_next_step_handler(msg, dev_add_pt_channel_url_process)
        return
    items = load_points_channels()
    items.append({'target': target, 'url': url, 'title': title})
    save_points_channels(items)
    bot.reply_to(message, f"✅ تم إضافة القناة <b>{html.escape(title)}</b> إلى قنوات النقاط!", parse_mode='HTML')
    text, markup = get_points_menu_data()
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')

def dev_add_pt_channel_url_process(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_add_pt_channel_url':
        send_dev_panel(message.chat.id, uid)
        return
    url = (message.text or '').strip()
    if not (url.startswith('https://t.me/') or url.startswith('http://t.me/')):
        msg = bot.reply_to(message, "❌ أرسل رابط تيليجرام يبدأ بـ https://t.me/ أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        state['step'] = 'dev_add_pt_channel_url'
        user_states[uid] = state
        bot.register_next_step_handler(msg, dev_add_pt_channel_url_process)
        return
    items = load_points_channels()
    items.append({'target': state.get('target'), 'url': url, 'title': state.get('title') or url})
    save_points_channels(items)
    bot.reply_to(message, "✅ تم الإضافة بنجاح إلى قنوات النقاط!")
    text, markup = get_points_menu_data()
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')

def dev_set_pt_reward(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_set_pt_reward':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        new_val = int(message.text)
        if new_val <= 0: raise ValueError()
    except Exception:
        user_states[uid] = state
        msg = bot.reply_to(message, "❌ أرسل رقماً صحيحاً أكبر من صفر أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_pt_reward)
        return
    settings = load_settings()
    settings['points_reward'] = new_val
    save_settings(settings)
    bot.reply_to(message, f"✅ <b>تم تعديل مكافأة قنوات النقاط!</b>\n\n💵 المكافأة الجديدة: <code>{new_val}</code> نقطة", parse_mode='HTML')
    text, markup = get_points_menu_data()
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')

def dev_add_turbo_process(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_add_turbo':
        send_dev_panel(message.chat.id, uid)
        return
    raw = (message.text or '').strip()
    target, url = parse_chat_identifier_and_url(raw)
    if not raw or (target is None and not url):
        msg = bot.reply_to(message, "❌ أرسل رابطاً أو معرفاً صحيحاً أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        user_states[uid] = {'step': 'dev_add_turbo'}
        bot.register_next_step_handler(msg, dev_add_turbo_process)
        return
    title = raw
    chat_obj = safe_get_chat(target)
    if chat_obj is not None:
        if getattr(chat_obj, 'title', None): title = chat_obj.title
        if not url and getattr(chat_obj, 'username', None): url = f"https://t.me/{chat_obj.username}"
    if not url:
        msg = bot.reply_to(message, "🔗 أرسل رابط الدعوة المباشر أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        user_states[uid] = {'step': 'dev_add_turbo_url', 'target': target, 'title': title}
        bot.register_next_step_handler(msg, dev_add_turbo_url_process)
        return
    items = load_turbo_channels()
    items.append({'target': target, 'url': url, 'title': title})
    save_turbo_channels(items)
    bot.reply_to(message, f"✅ تم إضافة القناة <b>{html.escape(title)}</b> إلى قنوات التيربو!", parse_mode='HTML')
    text, markup = get_turbo_menu_data()
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')

def dev_add_turbo_url_process(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_add_turbo_url':
        send_dev_panel(message.chat.id, uid)
        return
    url = (message.text or '').strip()
    if not (url.startswith('https://t.me/') or url.startswith('http://t.me/')):
        msg = bot.reply_to(message, "❌ أرسل رابط تيليجرام يبدأ بـ https://t.me/ أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        state['step'] = 'dev_add_turbo_url'
        user_states[uid] = state
        bot.register_next_step_handler(msg, dev_add_turbo_url_process)
        return
    items = load_turbo_channels()
    items.append({'target': state.get('target'), 'url': url, 'title': state.get('title') or url})
    save_turbo_channels(items)
    bot.reply_to(message, "✅ تم الإضافة بنجاح إلى قنوات التيربو!")
    text, markup = get_turbo_menu_data()
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')

def dev_set_turbo_reward(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_set_turbo_reward':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        new_val = int(message.text)
        if new_val <= 0: raise ValueError()
    except Exception:
        user_states[uid] = state
        msg = bot.reply_to(message, "❌ أرسل رقماً صحيحاً أكبر من صفر أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_turbo_reward)
        return
    settings = load_settings()
    settings['turbo_reward'] = new_val
    save_settings(settings)
    bot.reply_to(message, f"✅ <b>تم تعديل مكافأة التيربو!</b>\n\n💵 المكافأة الجديدة: <code>{new_val}</code> نقطة", parse_mode='HTML')
    text, markup = get_turbo_menu_data()
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode='HTML')

def dev_set_tol(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_set_tol':
        send_dev_panel(message.chat.id, uid)
        return
    try:
        new_val = int(message.text)
        if new_val < 0 or new_val > 20: raise ValueError()
    except Exception:
        user_states[uid] = state
        msg = bot.reply_to(message, "❌ أرسل رقماً من 0 إلى 20 أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        bot.register_next_step_handler(msg, dev_set_tol)
        return
    settings = load_settings()
    settings['complete_tolerance'] = new_val
    save_settings(settings)
    bot.reply_to(message, f"✅ <b>تم الحفظ!</b>\n\nسماح الإكمال الجديد: <code>{new_val}%</code>", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

def dev_set_comp_channel(message):
    uid = message.from_user.id
    if not is_dev(uid): return
    if cancel_check(message):
        user_states.pop(uid, None)
        return send_welcome(message)
    state = user_states.pop(uid, None)
    if not state or state.get('step') != 'dev_set_comp_channel':
        send_dev_panel(message.chat.id, uid)
        return
    new_channel = (message.text or '').strip()
    target, _ = parse_chat_identifier_and_url(new_channel)
    if not target:
        msg = bot.reply_to(message, "❌ أرسل معرف قناة صحيح (مثال: @MyChannel) أو اضغط على زر الإلغاء:", reply_markup=get_cancel_markup())
        user_states[uid] = {'step': 'dev_set_comp_channel'}
        bot.register_next_step_handler(msg, dev_set_comp_channel)
        return
    settings = load_settings()
    settings['completion_channel'] = target
    save_settings(settings)
    fd = load_fundings()
    for o in fd.values():
        if isinstance(o, dict) and o.get('channel_posted') is False: o['post_tries'] = 0
    save_fundings(fd)
    bot.reply_to(message, f"✅ <b>تم تعيين قناة إكمال الطلبات إلى:</b> <code>{html.escape(target)}</code>", parse_mode='HTML')
    send_dev_panel(message.chat.id, uid)

def show_points_channel(chat_id, msg_id, uid, idx=0):
    channels = load_points_channels()
    user = get_user_data(uid)
    verified = user.get('verified_channels', [])
    unverified = [c for c in channels if c['target'] not in verified]
    if not unverified:
        text = "📺 <b>قنوات النقاط</b>\n\nلا توجد قنوات حالياً.\nعد لاحقاً!"
        markup = types.InlineKeyboardMarkup()
        markup.add(make_btn('⬅️ رجوع', callback_data='collect', style='danger'))
        safe_edit(chat_id, msg_id, text, markup, 'HTML')
        return
    if idx >= len(unverified): idx = 0
    channel = unverified[idx]
    settings = load_settings()
    reward = settings.get('points_reward', 40)
    text = f"تجميع النقاط 🌟\n────────────────────\n\n📺 <b>{channel['title']}</b>\n✨ المكافأة : <b>+{reward}</b> نقطة\n\nالخطوات:\n1️⃣ افتح الرابط وانضم\n2️⃣ اضغط على تحقق ✅\n\n💎 رصيدك : {user.get('balance', 0)}\n"
    markup = types.InlineKeyboardMarkup()
    markup.add(make_btn('📺 افتح القناة', url=channel['url'], style='primary'))
    markup.row(make_btn('▶️ التالي', callback_data=f'pt_next_{idx+1}', style='primary'), make_btn('✅ تحقق', callback_data=f'pt_verify_{idx}', style='success'))
    markup.add(make_btn('🚩 إبلاغ', callback_data='pt_report', style='danger'))
    markup.add(make_btn('⬅️ الرئيسية', callback_data='back_to_start', style='danger'))
    safe_edit(chat_id, msg_id, text, markup, 'HTML')

# ========== تتبع المجموعات والقنوات التي أُضيف لها البوت (للإذاعة) ==========
# ========== إشعار المطور عند حظر/إلغاء حظر البوت من عضو ==========
def _handle_private_block_status(upd):
    u = upd.from_user
    if u is None or getattr(u, 'is_bot', False) or is_dev(u.id): return
    status = upd.new_chat_member.status
    key = str(u.id)
    db = load_db()
    full_name = f"{u.first_name or ''} {getattr(u, 'last_name', None) or ''}".strip() or "مستخدم"
    uname = f"@{html.escape(u.username)}" if u.username else "لا يوجد"
    info = (f"📌 الاسم : {html.escape(full_name)}\n"
            f"📌 المعرف : {uname}\n"
            f"📌 الايدي : <code>{u.id}</code>\n"
            f"📌 عدد الأعضاء : {len(db)}")
    if status == 'kicked':
        if isinstance(db.get(key), dict):
            db[key]['blocked_bot'] = True
            save_db(db)
        _io_pool.submit(lambda: bot.send_message(DEV_ID, "🚫 <b>عضو قام بحظر البوت</b>\n" + info, parse_mode='HTML'))
    elif status == 'member':
        # إشعار فقط إذا كان محظوراً سابقاً (حتى لا يتكرر مع إشعار العضو الجديد)
        if isinstance(db.get(key), dict) and db[key].get('blocked_bot'):
            db[key]['blocked_bot'] = False
            save_db(db)
            _io_pool.submit(lambda: bot.send_message(DEV_ID, "✅ <b>عضو ألغى حظر البوت</b>\n" + info, parse_mode='HTML'))

@bot.my_chat_member_handler()
def _track_my_chat_member(upd):
    try:
        chat = upd.chat
        if chat.type == 'private':
            _handle_private_block_status(upd)
            return
        status = upd.new_chat_member.status
        chats = load_chats()
        key = str(chat.id)
        if status in ('left', 'kicked'):
            if key in chats: chats[key]['active'] = False
        else:
            chats[key] = {'type': chat.type, 'title': getattr(chat, 'title', '') or '', 'active': True}
        save_chats(chats)
    except Exception: pass

@bot.message_handler(func=lambda m: m.chat.type in ('group', 'supergroup'), content_types=['text'])
def _track_group_message(message):
    try:
        key = str(message.chat.id)
        chats = load_chats()
        if key not in chats or not chats[key].get('active', True):
            chats[key] = {'type': message.chat.type, 'title': getattr(message.chat, 'title', '') or '', 'active': True}
            save_chats(chats)
    except Exception: pass

# ========== النسخ الاحتياطي الدائم (الأعضاء ما تضيع حتى لو تغيّرت المنصة) ==========
# كل البيانات (الأعضاء/الأرصدة/التمويلات/الإعدادات...) تُحفظ كملف مضغوط داخل رسالة مثبّتة
# بمحادثة المطور مع البوت. عند التشغيل على منصة جديدة بدون ملفات، يسترجعها البوت تلقائياً.
BACKUP_FILE_NAME = 'bot_backup.json.gz'
BACKUP_INTERVAL = 180   # أقصى فترة بين نسختين (ثواني)
BACKUP_MIN_GAP = 45     # أقل فترة بين نسختين (ثواني)
_backup_lock = threading.Lock()
_backup_event = threading.Event()
_backup_state = {'last_hash': None, 'msg_id': None, 'last_time': 0, 'last_ok': 0, 'last_error': ''}

def request_backup():
    _backup_event.set()

def _backup_state_text():
    if _backup_state.get('last_error'): return 'خطأ: ' + _backup_state['last_error'][:80]
    if _backup_state.get('last_ok'): return time.strftime('%Y-%m-%d %H:%M', time.localtime(_backup_state['last_ok']))
    return 'لم تتم بعد'

def _collect_backup_data():
    sources = (
        ('database', load_db), ('settings', load_settings), ('coupons', load_coupons),
        ('fundings', load_fundings), ('force_sub', load_force_sub),
        ('points_channels', load_points_channels), ('turbo_channels', load_turbo_channels),
        ('chats', load_chats), ('gift_links', load_gift_links),
    )
    last_ex = None
    for _ in range(5):
        try:
            data = {k: fn() for k, fn in sources}
            raw = json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8')
            return raw, len(data['database'])
        except RuntimeError as ex:   # تغيّر القاموس أثناء القراءة، نعيد المحاولة
            last_ex = ex
            time.sleep(0.2)
    raise last_ex

def do_backup(force=False):
    """يرفع نسخة احتياطية لرسالة مثبّتة بمحادثة المطور (يعدّل نفس الرسالة بدل إرسال رسائل جديدة)"""
    with _backup_lock:
        try:
            ver = _data_version
            if not force and ver == _backup_state.get('last_ver'):
                return True          # لا تغيير منذ آخر نسخة: لا نضيّع معالجاً
            raw, users = _collect_backup_data()
            h = hashlib.md5(raw).hexdigest()
            if not force and h == _backup_state['last_hash']:
                _backup_state['last_ver'] = ver
                return True
            _backup_state['last_time'] = time.time()
            with open(BACKUP_FILE_NAME, 'wb') as f:
                f.write(gzip.compress(raw, 6))
            caption = f"💾 نسخة احتياطية للبوت — لا تحذف هذه الرسالة\n👥 الأعضاء: {users}\n🕒 {time.strftime('%Y-%m-%d %H:%M:%S')}"
            done = False
            msg_id = _backup_state['msg_id']
            if msg_id:
                try:
                    with open(BACKUP_FILE_NAME, 'rb') as f:
                        bot.edit_message_media(types.InputMediaDocument(f, caption=caption), chat_id=DEV_ID, message_id=msg_id)
                    done = True
                except Exception as ex:
                    if 'not modified' in str(ex).lower(): done = True
            if not done:
                with open(BACKUP_FILE_NAME, 'rb') as f:
                    m = bot.send_document(DEV_ID, f, caption=caption, disable_notification=True)
                try: bot.pin_chat_message(DEV_ID, m.message_id, disable_notification=True)
                except Exception as ex: print(f"[backup] تعذّر التثبيت: {ex}")
                old = _backup_state['msg_id']
                _backup_state['msg_id'] = m.message_id
                if old and old != m.message_id:
                    try: bot.delete_message(DEV_ID, old)
                    except Exception: pass
            _backup_state.update(last_hash=h, last_ver=ver, last_ok=time.time(), last_error='')
            return True
        except Exception as ex:
            _backup_state['last_error'] = str(ex)
            print(f"[backup] فشل النسخ الاحتياطي: {ex}  (تأكد أن حساب المطور ضغط /start على البوت)")
            return False

def _fetch_pinned_backup():
    try:
        chat = bot.get_chat(DEV_ID)
    except Exception as ex:
        print(f"[backup] تعذّر قراءة محادثة المطور: {ex}")
        return None
    pm = getattr(chat, 'pinned_message', None)
    doc = getattr(pm, 'document', None) if pm else None
    if not doc or 'bot_backup' not in (getattr(doc, 'file_name', '') or ''): return None
    try:
        fi = bot.get_file(doc.file_id)
        return bot.download_file(fi.file_path), pm.message_id
    except Exception as ex:
        print(f"[backup] تعذّر تحميل النسخة المثبّتة: {ex}")
        return None

def _parse_backup(blob):
    try: raw = gzip.decompress(blob)
    except Exception: raw = blob
    data = json.loads(raw.decode('utf-8'))
    if not isinstance(data, dict): raise ValueError("ملف غير صالح")
    if 'database' not in data:
        # ملف database.json قديم (أعضاء فقط)
        if not data or not all(str(k).isdigit() and isinstance(v, dict) for k, v in data.items()):
            raise ValueError("الملف ليس نسخة احتياطية ولا ملف أعضاء صالح")
        data = {'database': data}
    if not isinstance(data.get('database'), dict): raise ValueError("بيانات الأعضاء غير صالحة")
    return data

def apply_backup(data, full=True):
    """full=True: النسخة تُعتمد عند التعارض. full=False: تُضاف فقط الأعضاء/الطلبات الناقصة محلياً."""
    cur = load_db()
    bk = data.get('database') or {}
    added = len([k for k in bk if k not in cur])
    merged = dict(cur)
    if full: merged.update(bk)
    else: merged.update({k: v for k, v in bk.items() if k not in cur})
    save_db(merged)

    def merge_dict(cur_d, bk_d, saver):
        if not isinstance(bk_d, dict) or not bk_d: return
        m = dict(cur_d)
        if full: m.update(bk_d)
        else: m.update({k: v for k, v in bk_d.items() if k not in cur_d})
        saver(m)
    merge_dict(load_fundings(), data.get('fundings'), save_fundings)
    merge_dict(load_coupons(), data.get('coupons'), save_coupons)
    merge_dict(load_gift_links(), data.get('gift_links'), save_gift_links)
    ch = data.get('chats')
    if isinstance(ch, dict) and ch:
        c = load_chats(); c.update(ch if full else {k: v for k, v in ch.items() if k not in c}); save_chats(c)

    if isinstance(data.get('settings'), dict) and data['settings']:
        if full:
            st = dict(load_settings()); st.update(data['settings']); save_settings(st)
    for key, loader, saver in (('force_sub', load_force_sub, save_force_sub),
                               ('points_channels', load_points_channels, save_points_channels),
                               ('turbo_channels', load_turbo_channels, save_turbo_channels)):
        lst = data.get(key)
        if isinstance(lst, list) and lst and (full or not loader()): saver(lst)
    return added, len(merged)

def restore_on_startup():
    res = _fetch_pinned_backup()
    if not res:
        print("[backup] لا توجد نسخة احتياطية مثبّتة (أول تشغيل أو المطور لم يضغط /start).")
        return
    blob, mid = res
    _backup_state['msg_id'] = mid
    local_empty = not load_db()
    data = _parse_backup(blob)
    added, total = apply_backup(data, full=local_empty)
    print(f"[backup] تم الاسترجاع: أُضيف {added} عضو، الإجمالي {total}")
    if local_empty or added:
        try: bot.send_message(DEV_ID, f"♻️ <b>تم استرجاع البيانات من النسخة الاحتياطية</b>\n👥 أعضاء مستردّين: <b>{added}</b>\n📊 إجمالي الأعضاء الآن: <b>{total}</b>", parse_mode='HTML')
        except Exception: pass

def backup_loop():
    time.sleep(20)
    while True:
        try:
            _backup_event.wait(timeout=BACKUP_INTERVAL)
            _backup_event.clear()
            gap = time.time() - _backup_state['last_time']
            if gap < BACKUP_MIN_GAP: time.sleep(BACKUP_MIN_GAP - gap)
            do_backup()
        except Exception as ex:
            print(f"[backup] خطأ بالحلقة: {ex}")
            time.sleep(10)

# ========== الشحن التلقائي بنجوم تيليجرام ==========
@bot.pre_checkout_query_handler(func=lambda q: True)
def _pre_checkout(q):
    ok = False
    try:
        parts = (q.invoice_payload or '').split('_')
        amount = int(parts[1])
        price = dict(SHOP_LISTS['charge_stars']['items']).get(amount)
        ok = bool(parts[0] == 'pts' and price and q.currency == 'XTR' and int(q.total_amount) == int(price)
                  and not is_banned(q.from_user.id))
    except Exception: ok = False
    try:
        if ok: bot.answer_pre_checkout_query(q.id, ok=True)
        else: bot.answer_pre_checkout_query(q.id, ok=False, error_message="تعذر إتمام العملية، حاول مرة ثانية.")
    except Exception as e: print(f"[pre_checkout] {e}")

@bot.message_handler(content_types=['successful_payment'])
def _on_successful_payment(message):
    try:
        sp = message.successful_payment
        uid = message.from_user.id
        parts = (sp.invoice_payload or '').split('_')
        amount = int(parts[1])
        charge_id = sp.telegram_payment_charge_id
        db = load_db()
        if str(uid) not in db or not isinstance(db[str(uid)], dict): db[str(uid)] = {}
        rec = db[str(uid)]
        paid = rec.setdefault('paid_charges', [])
        if charge_id in paid: return   # منع التكرار
        bonus_pct = int(load_settings().get('stars_bonus_percent', 0) or 0)
        bonus = amount * bonus_pct // 100
        total = amount + bonus
        rec['balance'] = rec.get('balance', 0) + total
        _log_tx(rec, total, 'admin', f"شحن بالنجوم {sp.total_amount}⭐" + (f" (+{bonus:,} مكافأة)" if bonus else ""))
        paid.append(charge_id)
        if len(paid) > 20: del paid[:-20]
        rec['stars_paid'] = rec.get('stars_paid', 0) + int(sp.total_amount)
        save_db(db)
        txt = f"✅ <b>تم الشحن تلقائياً!</b>\n\n💰 النقاط: <b>{amount:,}</b>"
        if bonus: txt += f"\n🎁 مكافأة الشحن: <b>+{bonus:,}</b>"
        txt += f"\n💎 رصيدك الآن: <b>{rec['balance']:,}</b> نقطة"
        bot.send_message(uid, txt, parse_mode='HTML')
        name = html.escape(message.from_user.first_name or "")
        un = f" (@{html.escape(message.from_user.username)})" if message.from_user.username else ""
        try: bot.send_message(DEV_ID, f"⭐ <b>عملية شحن بالنجوم</b>\n👤 {name}{un} — <code>{uid}</code>\n⭐ النجوم: {sp.total_amount}\n💎 النقاط: {amount:,} + مكافأة {bonus:,} = <b>{total:,}</b>", parse_mode='HTML')
        except Exception: pass
    except Exception as e:
        print(f"[successful_payment] {e}")
        try: bot.send_message(DEV_ID, f"⚠️ دفعة نجوم نجحت لكن تعذر الشحن التلقائي!\n👤 <code>{message.from_user.id}</code>\nالتفاصيل: {html.escape(str(message.successful_payment.invoice_payload))}\nرقم العملية: <code>{message.successful_payment.telegram_payment_charge_id}</code>", parse_mode='HTML')
        except Exception: pass

@bot.message_handler(commands=['backup'])
def backup_cmd(message):
    if not is_dev(message.from_user.id): return
    if do_backup(force=True):
        try:
            with open(BACKUP_FILE_NAME, 'rb') as f:
                bot.send_document(message.chat.id, f, caption=f"✅ نسخة احتياطية — {len(load_db())} عضو\nاحتفظ بها، ولاسترجاعها أرسلها للبوت مع تعليق /restore")
        except Exception: pass
    else:
        bot.reply_to(message, f"❌ فشل النسخ الاحتياطي: {_backup_state.get('last_error')}")

@bot.message_handler(content_types=['document'], func=lambda m: m.chat.type == 'private' and is_dev(m.from_user.id) and (m.caption or '').strip().lower().startswith('/restore'))
def restore_cmd(message):
    try:
        fi = bot.get_file(message.document.file_id)
        data = _parse_backup(bot.download_file(fi.file_path))
        added, total = apply_backup(data, full=True)
        bot.reply_to(message, f"✅ <b>تم الاسترجاع بنجاح</b>\n👥 أعضاء جدد: <b>{added}</b>\n📊 إجمالي الأعضاء: <b>{total}</b>", parse_mode='HTML')
        request_backup()
    except Exception as ex:
        bot.reply_to(message, f"❌ فشل الاسترجاع: {ex}")

# ========== التقاط أي عضو يراسل البوت بالخاص (آخر معالج، يسجّل فقط) ==========
@bot.message_handler(func=lambda m: m.chat.type == 'private',
                     content_types=['text', 'photo', 'video', 'document', 'audio', 'voice', 'sticker', 'animation', 'video_note', 'contact', 'location'])
def _catch_all_private(message):
    try: register_user_if_new(message.from_user)
    except Exception: pass

# ========== تشغيل ==========
def _graceful_exit(signum=None, frame=None):
    try: flush_all_writes()
    except Exception: pass
    try: do_backup(force=True)
    except Exception: pass
    sys.exit(0)

if __name__ == '__main__':
    # تحميل كل الكاشات قبل بدء المعالجة (يمنع تضارب القراءة الأولى بين الخيوط)
    for _f in (load_db, load_settings, load_coupons, load_fundings, load_force_sub, load_points_channels, load_turbo_channels, load_chats, load_gift_links): _f()
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try: signal.signal(_sig, _graceful_exit)
        except Exception: pass
    try: restore_on_startup()
    except Exception as _e: print(f"[backup] فشل الاسترجاع عند التشغيل: {_e}")
    threading.Thread(target=backup_loop, daemon=True).start()
    watcher = threading.Thread(target=progress_watcher_loop, daemon=True)
    watcher.start()
    print(f"✅ لوحة المطور مفعّلة للمستخدم: {DEV_ID} ({DEV_USERNAME})")
    print(f"✅ البوت يعمل الآن... (@{BOT_USERNAME})")
    try:
        _lat = []
        for _ in range(4):
            _t = time.time(); bot.get_me(); _lat.append((time.time() - _t) * 1000)
        print(f"📡 زمن الوصول لتلغرام: أول طلب {_lat[0]:.0f}ms | بعده {min(_lat[1:]):.0f}–{max(_lat[1:]):.0f}ms")
    except Exception as _ex: print(f"[net] تعذر قياس زمن الوصول: {_ex}")
    if not getattr(_me, 'supports_inline_queries', True):
        print("⚠️ الوضع المضمّن (Inline) غير مفعّل لهذا البوت! فعّله من @BotFather ← /setinline، وإلا زر ادع صديقاً ما راح يشتغل.")
    else:
        print("✅ الوضع المضمّن (Inline) مفعّل")
    while True:
        try: bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True, allowed_updates=['message', 'callback_query', 'inline_query', 'my_chat_member', 'pre_checkout_query'])
        except Exception as e:
            print(f"Polling stopped, restarting in 3s: {e}")
            time.sleep(3)