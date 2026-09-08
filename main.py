# ============================================
# 🚀 استضافة البوتات - ªGE تيم HOSTING
# نظام كامل لإدارة وتشغيل البوتات
# ============================================
from flask import Flask, render_template, render_template_string, request, redirect, url_for, session, jsonify, send_from_directory, send_file, Response
import json
import os
import subprocess
import random
import string
import uuid
from datetime import datetime, timedelta
import sys
import shutil
import threading
import time
import zipfile
import tarfile
import gzip
import ast
import re
import hashlib
import platform
import urllib.request
import stat
import tempfile
try:
    from importlib import metadata as importlib_metadata
except ImportError:
    importlib_metadata = None
try:
    import psutil
except ImportError:  # Railway/runtime fallback
    psutil = None
import importlib.util
from io import BytesIO
# ============================================
# ⚙️ إعدادات التطبيق
# ============================================
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'AGE')  # مفتاح التشفير
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # حد رفع الملفات 50 ميجابايت
# ============================================
# 📁 المتغيرات العامة
# ============================================
BASE_DIR = os.getenv('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
USERS_FILE = os.path.join(BASE_DIR, 'users.json')  # ملف قاعدة بيانات المستخدمين
SETTINGS_FILE = os.path.join(BASE_DIR, 'settings.json')  # إعدادات الاستضافة العامة
BOTS_DIR = os.path.join(BASE_DIR, 'bots')  # مجلد تخزين البوتات
PIP_CACHE_DIR = os.path.join(BASE_DIR, '.pip-cache')  # كاش pip دائم داخل مساحة البيانات
PIP_INSTALL_LOCKS = {}  # منع تثبيت نفس المشروع بالتوازي
PIP_INSTALL_LOCKS_GUARD = threading.Lock()
# ============================================
# ☁️ Cloudflare Tunnel — حالة مستقلة لكل مشروع
CLOUDFLARED_DIR = os.getenv('CLOUDFLARED_DIR', os.path.join(tempfile.gettempdir(), 'age-hosting-cloudflared'))
CLOUDFLARED_LOCK = threading.RLock()
CLOUDFLARE_STATES = {}  # server_id -> {process, url, port, started_at, log_path}
CLOUDFLARED_TARGET = os.getenv('CLOUDFLARED_TARGET', '')
USERS_FILE_LOCK = threading.RLock()
# ============================================
# 🛡️ صيانة تلقائية شاملة
# ============================================
AUTO_MAINTENANCE_INTERVAL = int(os.getenv('AUTO_MAINTENANCE_INTERVAL', '60'))  # تنظيف السجلات كل دقيقة
AUTO_FORCED_RESTART_INTERVAL = int(os.getenv('AUTO_FORCED_RESTART_INTERVAL', '0'))  # 0 = لا توجد إعادة تشغيل دورية للبوت
AUTO_MAINTENANCE_ENABLED = os.getenv('AUTO_MAINTENANCE_ENABLED', '1') != '0'
AUTO_MAINTENANCE_LOCK = threading.Lock()
AUTO_MAINTENANCE_STARTED = False
BOT_MONITORS = {}
BOT_MONITORS_LOCK = threading.Lock()
BOT_RESTART_LOCKS = {}
BOT_RESTART_LOCKS_GUARD = threading.Lock()
BOT_START_LOCKS = {}
BOT_START_LOCKS_GUARD = threading.Lock()
# إنشاء مجلد البوتات إذا لم يكن موجوداً
os.makedirs(BOTS_DIR, exist_ok=True)
os.makedirs(PIP_CACHE_DIR, exist_ok=True)
os.makedirs(CLOUDFLARED_DIR, exist_ok=True)
# ============================================
# 🔧 نظام تحديد الاستخدام (Rate Limiter)
# ============================================
def _psutil_available():
    return psutil is not None
class RateLimiter:
    """التحقق من استخدام المعالج ومنع التجاوز"""
    
    def check_rate(self, server_id, limit_percent):
        """التحقق من استخدام المعالج للخادم"""
        
        # إنشاء سجل للخادم إذا لم يكن موجوداً
        if server_id not in CPU_HISTORY:
            CPU_HISTORY[server_id] = []
        
        # تحميل بيانات المستخدمين
        users = load_users()
        server = None
        
        # البحث عن الخادم
        for uname, data in users.items():
            if uname == 'admin': 
                continue  # تخطي حساب الأدمن
            servers = data.get('servers', [])
            if not isinstance(servers, list): 
                continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    server = s
                    break
        
        # إذا لم يكن الخادم قيد التشغيل
        if not server or server.get('status') != 'running':
            return False, 0

        # CPU غير محدود: لا تطبق أي Rate Limit على هذا الخادم.
        # 100% تعني أيضًا الحد الأقصى الطبيعي للعملية ولا ينبغي إيقاف البوت
        # بسبب أن psutil قد يعرض أكثر من 100% عند استخدام أكثر من نواة.
        if limit_percent in (0, None) or str(limit_percent).lower() in ('unlimited', 'none', 'غير محدود', 'غير_محدود'):
            return False, 0
        try:
            if float(limit_percent) >= 100:
                return False, 0
        except Exception:
            pass
        
        # الحصول على معرف العملية
        pid = server.get('pid')
        if not pid: 
            return False, 0
        
        try:
            # قياس استخدام المعالج
            proc = psutil.Process(pid)
            cpu = proc.cpu_percent(interval=1)
            now = time.time()
            
            # حفظ القياس
            CPU_HISTORY[server_id].append({'time': now, 'cpu': cpu})
            
            # الاحتفاظ بآخر 30 ثانية فقط
            CPU_HISTORY[server_id] = [h for h in CPU_HISTORY[server_id] if now - h['time'] < 30]
            
            # حساب متوسط آخر 10 ثواني
            recent = [h['cpu'] for h in CPU_HISTORY[server_id] if now - h['time'] < 10]
            if recent:
                avg_cpu = sum(recent) / len(recent)
                # التحقق من تجاوز الحد
                if avg_cpu > limit_percent:
                    return True, avg_cpu
        except: 
            pass
        
        return False, 0
# إنشاء كائن من نظام تحديد الاستخدام
rate_limiter = RateLimiter()
# ============================================
# 🔄 نظام إعادة التشغيل التلقائي
# ============================================
def should_auto_restart(server_id):
    """تحديد ما إذا كان يجب إعادة تشغيل البوت تلقائياً"""
    
    # إنشاء سجل للخادم إذا لم يكن موجوداً
    if server_id not in CRASH_COUNT:
        CRASH_COUNT[server_id] = {'count': 0, 'last_crash': time.time()}
    
    crash_info = CRASH_COUNT[server_id]
    
    # إذا تعطل 3 مرات في آخر دقيقة، لا تعيد التشغيل
    if time.time() - crash_info['last_crash'] < 60:
        if crash_info['count'] >= 3:
            return False
    else:
        # إعادة تعيين العداد بعد مرور دقيقة
        crash_info['count'] = 0
    
    # زيادة عداد التعطل
    crash_info['count'] += 1
    crash_info['last_crash'] = time.time()
    return True
# ============================================
# 🛠️ وظائف مساعدة
# ============================================
def generate_random_password(length=10):
    """توليد كلمة مرور عشوائية"""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=length))
def load_users():
    """تحميل بيانات المستخدمين بأمان حتى مع تعدد الـThreads."""
    default = {"admin": {"password": "KINGFF", "role": "admin"}}
    try:
        with USERS_FILE_LOCK:
            if not os.path.exists(USERS_FILE):
                save_users(default)
                return default
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            if 'admin' not in data:
                data['admin'] = default['admin']
                save_users(data)
            return data
    except (json.JSONDecodeError, OSError, ValueError):
        # لا تجعل ملف حالة تالف يطيح بخدمة الاستضافة كلها.
        try:
            backup = USERS_FILE + '.corrupt'
            if os.path.exists(USERS_FILE):
                shutil.copy2(USERS_FILE, backup)
        except Exception:
            pass
        return default.copy()

def save_users(data):
    """حفظ users.json بكتابة ذرية لمنع الملف من أن يصبح فارغًا أو ناقصًا."""
    tmp_path = USERS_FILE + f'.tmp.{os.getpid()}.{threading.get_ident()}'
    with USERS_FILE_LOCK:
        os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp_path, USERS_FILE)

def load_settings():
    """تحميل إعدادات الاستضافة العامة"""
    default_max = int(os.getenv('MAX_SERVERS_PER_USER', '3'))
    defaults = {
        'max_servers_per_user': max(1, default_max)
    }
    if not os.path.exists(SETTINGS_FILE):
        save_settings(defaults)
        return defaults
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    if 'max_servers_per_user' not in data:
        data['max_servers_per_user'] = defaults['max_servers_per_user']
    try:
        data['max_servers_per_user'] = max(1, int(data.get('max_servers_per_user', defaults['max_servers_per_user'])))
    except Exception:
        data['max_servers_per_user'] = defaults['max_servers_per_user']
    return data
def save_settings(data):
    """حفظ إعدادات الاستضافة العامة"""
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
def get_user_project_limit(users, username):
    """الحد الأقصى للمشاريع الخاصة بالمستخدم"""
    settings = load_settings()
    base_limit = int(settings.get('max_servers_per_user', 3))
    user_info = users.get(username, {}) if isinstance(users, dict) else {}
    try:
        custom_limit = int(user_info.get('max_servers', base_limit))
    except Exception:
        custom_limit = base_limit
    return max(1, custom_limit)
def get_server_dir(server_id):
    """الحصول على مسار مجلد الخادم"""
    server_dir = os.path.join(BOTS_DIR, server_id)
    os.makedirs(server_dir, exist_ok=True)
    return server_dir
def resolve_server_path(server_id, relative_path=''):
    """إرجاع مسار آمن داخل مجلد الخادم فقط"""
    server_root = os.path.abspath(get_server_dir(server_id))
    rel = (relative_path or '').replace('\\', '/').lstrip('/')
    abs_path = os.path.abspath(os.path.join(server_root, rel))
    if abs_path == server_root or abs_path.startswith(server_root + os.sep):
        return server_root, abs_path
    return server_root, None


def is_archive_filename(filename):
    """التحقق من أن الملف مضغوط/أرشيف شائع"""
    name = (filename or "").lower().strip()
    return name.endswith((
        '.zip', '.tar', '.tgz', '.tar.gz', '.tar.bz2', '.tar.xz', '.gz'
    ))


def _is_within_directory(directory, target):
    """منع Zip Slip / Path Traversal"""
    abs_directory = os.path.abspath(directory)
    abs_target = os.path.abspath(target)
    return abs_target == abs_directory or abs_target.startswith(abs_directory + os.sep)


def auto_extract_archive(archive_path, extract_to):
    """فك ضغط الأرشيفات تلقائيًا بعد الرفع"""
    if not archive_path or not os.path.exists(archive_path):
        return 0
    archive_lower = archive_path.lower()
    extracted = 0
    target_dir = os.path.abspath(extract_to)

    try:
        if archive_lower.endswith('.zip'):
            with zipfile.ZipFile(archive_path, 'r') as zf:
                for member in zf.infolist():
                    member_path = os.path.join(target_dir, member.filename)
                    if not _is_within_directory(target_dir, member_path):
                        raise ValueError('Unsafe archive entry detected')
                zf.extractall(target_dir)
                extracted = len(zf.infolist())

        elif archive_lower.endswith(('.tar', '.tgz', '.tar.gz', '.tar.bz2', '.tar.xz')):
            with tarfile.open(archive_path, 'r:*') as tf:
                for member in tf.getmembers():
                    member_path = os.path.join(target_dir, member.name)
                    if not _is_within_directory(target_dir, member_path):
                        raise ValueError('Unsafe archive entry detected')
                tf.extractall(target_dir)
                extracted = len(tf.getmembers())

        elif archive_lower.endswith('.gz'):
            # gzip مفرد: يفك الملف إلى نفس المجلد بدون .gz
            base_name = os.path.basename(archive_path)[:-3]
            if not base_name:
                return 0
            out_path = os.path.join(target_dir, base_name)
            if not _is_within_directory(target_dir, out_path):
                raise ValueError('Unsafe archive entry detected')
            with gzip.open(archive_path, 'rb') as f_in, open(out_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
            extracted = 1
        else:
            return 0

        # بعد نجاح الفك، نحذف الأرشيف حتى لا يزعج المستخدم
        try:
            os.remove(archive_path)
        except Exception:
            pass
        return extracted
    except Exception:
        return 0

def parse_expiry_datetime(expiry_value):
    """تحويل قيمة الانتهاء إلى datetime آمن"""
    if not expiry_value:
        return None
    if isinstance(expiry_value, datetime):
        return expiry_value
    text = str(expiry_value).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def format_expiry_datetime(expiry_dt):
    """تنسيق الانتهاء بشكل موحد"""
    if not expiry_dt:
        return ''
    if isinstance(expiry_dt, str):
        parsed = parse_expiry_datetime(expiry_dt)
        if not parsed:
            return expiry_dt
        expiry_dt = parsed
    return expiry_dt.strftime('%Y-%m-%d %H:%M:%S.%f')


def get_user_effective_expiry(users, username):
    """الحصول على تاريخ انتهاء الحساب/المشاريع للمستخدم"""
    if not isinstance(users, dict):
        return None
    user_info = users.get(username, {}) or {}
    account_expiry = parse_expiry_datetime(user_info.get('account_expiry'))
    if account_expiry:
        return account_expiry
    expiries = []
    servers = user_info.get('servers', [])
    if isinstance(servers, list):
        for server in servers:
            if not isinstance(server, dict):
                continue
            exp = parse_expiry_datetime(server.get('expiry'))
            if exp:
                expiries.append(exp)
    if expiries:
        return min(expiries)
    return None


def sync_user_expiry(users, username, expiry_dt):
    """توحيد انتهاء جميع مشاريع الحساب على نفس التاريخ"""
    if not isinstance(users, dict) or username not in users:
        return False
    expiry_text = format_expiry_datetime(expiry_dt)
    users[username]['account_expiry'] = expiry_text
    servers = users[username].get('servers', [])
    if not isinstance(servers, list):
        servers = []
    for server in servers:
        if isinstance(server, dict):
            server['expiry'] = expiry_text
    users[username]['servers'] = servers
    return True


def disable_user_projects(users, username, reason='expired'):
    """تعطيل كل مشاريع المستخدم بدون حذفها"""
    if not isinstance(users, dict) or username not in users:
        return False
    changed = False
    servers = users[username].get('servers', [])
    if not isinstance(servers, list):
        servers = []
    for server in servers:
        if not isinstance(server, dict):
            continue
        pid = server.get('pid')
        if pid:
            try:
                stop_bot_process(pid)
            except Exception:
                pass
        server['status'] = 'disabled'
        server['disabled'] = True
        server['disabled_reason'] = reason
        server['pid'] = None
        server['stopped_by_user'] = True
        server['disabled_at'] = str(datetime.now())
        changed = True
    users[username]['servers'] = servers
    if reason == 'expired':
        users[username]['account_status'] = 'expired'
    return changed


def restore_expired_user_projects(users, username):
    """إرجاع المشاريع التي تعطلت بسبب انتهاء الصلاحية فقط"""
    if not isinstance(users, dict) or username not in users:
        return False
    changed = False
    servers = users[username].get('servers', [])
    if not isinstance(servers, list):
        servers = []
    for server in servers:
        if not isinstance(server, dict):
            continue
        if server.get('disabled_reason') == 'expired':
            server['status'] = 'stopped'
            server['disabled'] = False
            server['disabled_reason'] = ''
            server['stopped_by_user'] = False
            server['pid'] = None
            server.pop('disabled_at', None)
            changed = True
    users[username]['servers'] = servers
    users[username]['account_status'] = 'active'
    return changed


def ensure_user_expiry(users, username, default_days=30):
    """ضمان وجود مدة صالحة للحساب، وإنشاء واحدة افتراضية عند الحاجة"""
    expiry_dt = get_user_effective_expiry(users, username)
    if expiry_dt:
        return expiry_dt
    expiry_dt = datetime.now() + timedelta(days=max(1, int(default_days)))
    sync_user_expiry(users, username, expiry_dt)
    return expiry_dt


def enforce_expired_accounts(users=None):
    """تعطيل جميع الحسابات التي انتهت صلاحيتها"""
    if users is None:
        users = load_users()
    if not isinstance(users, dict):
        return False
    changed = False
    now = datetime.now()
    for uname in list(users.keys()):
        if uname == 'admin':
            continue
        expiry_dt = get_user_effective_expiry(users, uname)
        if expiry_dt and now > expiry_dt:
            if disable_user_projects(users, uname, reason='expired'):
                changed = True
    if changed:
        save_users(users)
    return changed

# ============================================
# 📦 مزامنة المتطلبات تلقائيًا من ملف بايثون
# ============================================
PYTHON_IMPORT_TO_PACKAGE = {
    'bs4': 'beautifulsoup4',
    'cv2': 'opencv-python',
    'dotenv': 'python-dotenv',
    'flask': 'Flask',
    'flask_session': 'Flask-Session',
    'jinja2': 'Jinja2',
    'markupsafe': 'MarkupSafe',
    'pillow': 'Pillow',
    'pil': 'Pillow',
    'pyyaml': 'PyYAML',
    'yaml': 'PyYAML',
    'crypto': 'pycryptodome',
    'cryptodome': 'pycryptodome',
    'telebot': 'pyTelegramBotAPI',
    'telegram': 'python-telegram-bot',
    'cachelib': 'cachelib',
    'werkzeug': 'Werkzeug',
    'itsdangerous': 'itsdangerous',
    'psutil': 'psutil',
    'google': 'protobuf',
    'protobuf': 'protobuf',
}
def _requirement_key(line):
    """مفتاح موحد للمقارنة بين أسماء الحزم"""
    if not line:
        return None
    line = str(line).strip()
    if not line or line.startswith('#'):
        return None
    if line.startswith('-r ') or line.startswith('--requirement '):
        return None
    match = re.match(r'([A-Za-z0-9_.-]+)', line)
    return match.group(1).lower() if match else None
def _is_stdlib_module(module_name):
    """التحقق مما إذا كان الاسم تابعًا للمكتبة القياسية"""
    base = (module_name or '').split('.')[0].lower()
    stdlib = getattr(sys, 'stdlib_module_names', set())
    return base in stdlib or base in {
        'os', 'sys', 'json', 'time', 'datetime', 'typing', 'pathlib',
        'subprocess', 'threading', 'zipfile', 'shutil', 'tempfile',
        'uuid', 'random', 'string', 're', 'ast', 'traceback', 'math',
        'functools', 'itertools', 'collections', 'importlib', 'io'
    }
def _normalize_import_to_package(module_name):
    """تحويل اسم الموديول إلى اسم حزمة pip إن لزم"""
    module_name = module_name or ''
    base = module_name.split('.')[0].lower()

    # google.* غالباً يقصد protobuf، وحتى الخطأ البسيط 'No module named google'
    # يجب أن يعود إلى protobuf لأن هذا هو اسم الحزمة الفعلي المطلوب.
    if base == 'google':
        return 'protobuf'

    return PYTHON_IMPORT_TO_PACKAGE.get(base, PYTHON_IMPORT_TO_PACKAGE.get(base.replace('_', ''), module_name.split('.')[0]))
def extract_python_requirements(source_text):
    """استخراج حزم pip المحتملة من ملف بايثون"""
    requirements = set()
    if not source_text:
        return requirements
    try:
        tree = ast.parse(source_text)
    except Exception:
        return requirements
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name or ''
                pkg = _normalize_import_to_package(module_name)
                if pkg and not _is_stdlib_module(module_name):
                    requirements.add(str(pkg).strip())
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            module_name = node.module or ''
            if not module_name:
                continue
            pkg = _normalize_import_to_package(module_name)
            if pkg and not _is_stdlib_module(module_name):
                requirements.add(str(pkg).strip())
    return requirements
def read_requirements_lines(req_path):
    """قراءة أسطر requirements الحالية"""
    if not os.path.exists(req_path):
        return []
    lines = []
    try:
        with open(req_path, 'r', encoding='utf-8', errors='ignore') as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('#'):
                    continue
                lines.append(line)
    except Exception:
        pass
    return lines
def merge_requirements(existing_lines, discovered_packages):
    """دمج المتطلبات الحالية مع المكتشفة من ملف التشغيل"""
    merged = {}
    for line in existing_lines:
        key = _requirement_key(line)
        if key and key not in merged:
            merged[key] = line
    for pkg in sorted({str(p).strip() for p in discovered_packages if str(p).strip()}, key=str.lower):
        key = _requirement_key(pkg)
        if key and key not in merged:
            merged[key] = pkg
    return list(merged.values())


def _collect_project_python_files(server_dir):
    """جمع ملفات Python الفعلية داخل المشروع مع تجاهل الكاش والبيئات والملفات المؤقتة."""
    root = os.path.abspath(server_dir)
    result = []
    ignored_dirs = {'.git', '.hg', '.svn', '__pycache__', '.venv', 'venv', 'env', '.env',
                    'node_modules', '.mypy_cache', '.pytest_cache', '.ruff_cache', '.pip-cache'}
    try:
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith('.')]
            for name in files:
                if not name.endswith('.py'):
                    continue
                full = os.path.abspath(os.path.join(current, name))
                if full.startswith(root + os.sep):
                    result.append(full)
    except Exception:
        return []
    return sorted(set(result))

def _collect_local_module_names(server_dir):
    """جمع أسماء الموديولات المحلية حتى لا يتم اعتبارها حزم pip."""
    root = os.path.abspath(server_dir)
    modules = set()
    try:
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in {'__pycache__', '.git', '.venv', 'venv', 'env', 'node_modules'}]
            rel = os.path.relpath(current, root)
            package_parts = [] if rel == '.' else [x for x in rel.split(os.sep) if x and x != '.']
            for name in files:
                if name.endswith('.py'):
                    modules.add(name[:-3].lower())
            if '__init__.py' in files and package_parts:
                modules.add(package_parts[-1].lower())
            for d in dirs:
                if os.path.isfile(os.path.join(current, d, '__init__.py')):
                    modules.add(d.lower())
    except Exception:
        pass
    return modules

def _discover_project_requirements(server_dir, main_file=None):
    """اكتشاف حزم المشروع من جميع ملفات Python داخل الخادم"""
    source_files = _collect_project_python_files(server_dir)
    startup_file = (main_file or 'main.py').strip() or 'main.py'
    startup_path = os.path.join(server_dir, startup_file)
    if os.path.exists(startup_path) and startup_path not in source_files and startup_path.endswith('.py'):
        source_files.insert(0, startup_path)

    local_modules = _collect_local_module_names(server_dir)
    discovered = []
    seen = set()

    for source_path in source_files:
        try:
            with open(source_path, 'r', encoding='utf-8', errors='ignore') as f:
                source_text = f.read()
        except Exception:
            continue

        try:
            packages = extract_python_requirements(source_text)
        except Exception:
            packages = set()

        for pkg in packages:
            pkg_text = str(pkg).strip()
            if not pkg_text:
                continue
            pkg_key = _requirement_key(pkg_text)
            if not pkg_key:
                continue
            if pkg_key.lower() in local_modules:
                continue
            if pkg_key.lower() in {'pip', 'setuptools', 'wheel'}:
                continue
            if pkg_key not in seen:
                seen.add(pkg_key)
                discovered.append(pkg_text)

    return discovered


def _extract_missing_modules_from_output(output_text):
    """استخراج أسماء الموديولات المفقودة من مخرجات pip أو Traceback"""
    output_text = output_text or ''
    found = []
    patterns = [
        r'No module named [\'"]([^\'"]+)[\'"]',
        r'ModuleNotFoundError: No module named [\'"]([^\'"]+)[\'"]',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, output_text, flags=re.IGNORECASE):
            name = (match.group(1) or '').strip()
            if name and name not in found:
                found.append(name)
    return found

def _extract_missing_modules_from_output(output_text):
    """استخراج أسماء الموديولات المفقودة من مخرجات pip أو Traceback"""
    output_text = output_text or ''
    found = []
    patterns = [
        r'No module named [\'\"]([^\'\"]+)[\'\"]',
        r'ModuleNotFoundError: No module named [\'\"]([^\'\"]+)[\'\"]',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, output_text, flags=re.IGNORECASE):
            name = (match.group(1) or '').strip()
            if name and name not in found:
                found.append(name)
    return found


def _install_packages_for_server(server_dir, main_file='main.py', requirements_file='requirements.txt', log=None, ts=None, python_exe=None, reason_text=''):
    """اكتشاف الحزم من ملفات المشروع ثم تثبيتها تلقائيًا"""
    python_exe = python_exe or sys.executable
    req_name = (requirements_file or 'requirements.txt').strip() or 'requirements.txt'
    main_file = (main_file or 'main.py').strip() or 'main.py'
    req_path = os.path.join(server_dir, req_name)

    discovered = _discover_project_requirements(server_dir, main_file)
    existing = read_requirements_lines(req_path)
    merged = merge_requirements(existing, discovered)

    # استكشاف الأخطاء من Traceback أو pip output وإضافتها أيضاً.
    missing_modules = _extract_missing_modules_from_output(reason_text)
    if missing_modules:
        for module_name in missing_modules:
            pkg = _normalize_import_to_package(module_name)
            if pkg and pkg not in merged:
                merged.insert(0, pkg)

    # protobuf مهم جدًا لمراجع google.protobuf حتى لو ظهر الخطأ بصيغة google فقط.
    try:
        with open(os.path.join(server_dir, main_file), 'r', encoding='utf-8', errors='ignore') as f:
            main_text = f.read().lower()
        if 'google.protobuf' in main_text and not any(_requirement_key(x) == 'protobuf' for x in merged):
            merged.insert(0, 'protobuf')
    except Exception:
        pass

    if merged:
        try:
            os.makedirs(os.path.dirname(req_path), exist_ok=True)
            with open(req_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(merged) + '\n')
        except Exception:
            pass
        return _pip_install_targets(merged, server_dir=server_dir, python_exe=python_exe, log=log, ts=ts)

    return [], []



def _requirements_packages_satisfied_from_targets(targets):
    if importlib_metadata is None:
        return False
    for target in targets or []:
        target = str(target).strip()
        if not target or '://' in target:
            return False
        match = re.match(r'^([A-Za-z0-9_.-]+)', target)
        if not match:
            return False
        package_name = match.group(1)
        try:
            importlib_metadata.version(package_name)
        except Exception:
            return False
    return True


def _pip_install_targets(targets, server_dir=None, python_exe=None, log=None, ts=None):
    """تثبيت حزمة/حزم pip واحدة تلو الأخرى مع الاستمرار عند الخطأ"""
    python_exe = python_exe or sys.executable
    installed = []
    failed = []
    seen = set()
    creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0

    for target in targets or []:
        target = str(target).strip()
        if not target:
            continue
        key = _requirement_key(target) or target.lower()
        if key in seen:
            continue
        seen.add(key)

        # لا تشغّل pip على حزمة مثبتة بالفعل.
        try:
            if _requirements_packages_satisfied_from_targets([target]):
                installed.append(target)
                continue
        except Exception:
            pass

        cmd = [python_exe, '-m', 'pip', 'install', target,
               '--disable-pip-version-check', '--prefer-binary']
        env = os.environ.copy()
        env['PIP_CACHE_DIR'] = os.path.abspath(PIP_CACHE_DIR)
        env['PIP_NO_INPUT'] = '1'
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                cwd=server_dir,
                creationflags=creation_flags,
                env=env,
            )
            for line in iter(proc.stdout.readline, ''):
                if line and log and line.strip():
                    prefix = f"[{ts()}] " if ts else ''
                    try:
                        log(f"{prefix}{line.rstrip()}" )
                    except Exception:
                        pass
            proc.wait()
            if proc.returncode == 0:
                installed.append(target)
            else:
                failed.append(target)
                if log:
                    prefix = f"[{ts()}] " if ts else ''
                    try:
                        log(f"{prefix}فشل تثبيت {target}")
                    except Exception:
                        pass
        except Exception:
            failed.append(target)
    return installed, failed


def _requirements_state_path(server_dir, req_path):
    """ملف حالة التثبيت الخاص بالمشروع."""
    return os.path.join(server_dir, '.pip_install_state.json')


def _requirements_fingerprint(req_path):
    """بصمة محتوى requirements لمنع إعادة التثبيت بدون أي تغيير."""
    try:
        with open(req_path, 'rb') as f:
            content = f.read()
    except Exception:
        return ''
    return hashlib.sha256(content).hexdigest()


def _load_install_state(server_dir):
    path = _requirements_state_path(server_dir, '')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_install_state(server_dir, state):
    path = _requirements_state_path(server_dir, '')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _requirements_packages_satisfied(req_path):
    """تحقق سريع من الحزم المثبتة فعليًا. القيود المعقدة تُعاد معالجتها بواسطة pip."""
    if importlib_metadata is None:
        return False
    for line in read_requirements_lines(req_path):
        item = line.strip()
        if not item or item.startswith(('#', '-r ', '--requirement ')):
            continue
        # روابط git/URL/path لا يمكن التحقق منها بأمان من اسم التوزيعة فقط.
        if '://' in item or item.startswith(('.', '/')):
            return False
        # استخراج اسم الحزمة فقط من == >= <= ~= != > <
        match = re.match(r'^\s*([A-Za-z0-9_.-]+)', item)
        if not match:
            return False
        package_name = match.group(1)
        # تحويل اسم التوزيعة للصيغة القياسية التي تستخدمها importlib.metadata.
        normalized = re.sub(r'[-_.]+', '-', package_name).lower()
        try:
            found = {
                re.sub(r'[-_.]+', '-', d).lower()
                for d in importlib_metadata.packages_distributions().get(
                    package_name.replace('-', '_'), []
                )
            }
            if normalized not in found:
                # المسار الأبسط: محاولة version بالاسم مباشرة.
                importlib_metadata.version(package_name)
        except Exception:
            return False
    return True


def _get_server_install_lock(server_id):
    with PIP_INSTALL_LOCKS_GUARD:
        lock = PIP_INSTALL_LOCKS.get(server_id)
        if lock is None:
            lock = threading.Lock()
            PIP_INSTALL_LOCKS[server_id] = lock
        return lock


def install_requirements_with_fallback(req_path, server_dir, main_file=None, log=None, ts=None, python_exe=None):
    """تثبيت المتطلبات مرة واحدة فقط عند الحاجة، مع كاش دائم ومنع التثبيت المتوازي."""
    python_exe = python_exe or sys.executable
    creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0
    req_path = os.path.abspath(req_path)
    startup_file = (main_file or 'main.py').strip() or 'main.py'
    startup_path = os.path.join(server_dir, startup_file)

    discovered = _discover_project_requirements(server_dir, startup_file)
    existing = read_requirements_lines(req_path)
    merged = merge_requirements(existing, discovered)

    try:
        startup_text = ''
        if os.path.exists(startup_path):
            with open(startup_path, 'r', encoding='utf-8', errors='ignore') as f:
                startup_text = f.read().lower()
        if ('google.protobuf' in startup_text or 'from google.protobuf' in startup_text or 'import google.protobuf' in startup_text) and not any((_requirement_key(x) == 'protobuf') for x in merged):
            merged.append('protobuf')
    except Exception:
        pass

    if merged:
        os.makedirs(os.path.dirname(req_path), exist_ok=True)
        try:
            with open(req_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(merged) + "\n")
        except Exception:
            pass

    if not os.path.exists(req_path):
        return {'ok': False, 'mode': 'empty', 'installed': [], 'failed': [], 'output': '', 'requirements_path': req_path}

    fingerprint = _requirements_fingerprint(req_path)
    state = _load_install_state(server_dir)

    # نفس requirements + تثبيت ناجح سابقًا = لا pip ولا تنزيل ولا فصل.
    if fingerprint and state.get('fingerprint') == fingerprint and state.get('installed') is True:
        return {
            'ok': True,
            'mode': 'cached',
            'installed': [],
            'failed': [],
            'output': 'المتطلبات مثبتة مسبقًا، تم تجاوز pip.',
            'requirements_path': req_path,
        }

    # حتى بعد إعادة تشغيل تطبيق الاستضافة، تحقّق فقط بسرعة قبل استدعاء pip.
    if fingerprint and _requirements_packages_satisfied(req_path):
        _save_install_state(server_dir, {
            'fingerprint': fingerprint,
            'installed': True,
            'updated_at': str(datetime.now()),
        })
        return {
            'ok': True,
            'mode': 'already-installed',
            'installed': [],
            'failed': [],
            'output': 'جميع الحزم مثبتة مسبقًا.',
            'requirements_path': req_path,
        }

    lock = _get_server_install_lock(os.path.basename(server_dir))
    with lock:
        # إعادة الفحص بعد انتظار أي تثبيت جارٍ من مسار آخر.
        state = _load_install_state(server_dir)
        fingerprint = _requirements_fingerprint(req_path)
        if fingerprint and state.get('fingerprint') == fingerprint and state.get('installed') is True:
            return {
                'ok': True, 'mode': 'cached', 'installed': [], 'failed': [],
                'output': 'المتطلبات مثبتة مسبقًا، تم تجاوز pip.',
                'requirements_path': req_path,
            }

        env = os.environ.copy()
        env['PIP_CACHE_DIR'] = os.path.abspath(PIP_CACHE_DIR)
        env['PIP_NO_INPUT'] = '1'

        cmd = [python_exe, '-m', 'pip', 'install', '-r', req_path,
               '--disable-pip-version-check', '--prefer-binary']
        output_lines = []
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, universal_newlines=True,
                cwd=server_dir, creationflags=creation_flags, env=env
            )
            for line in iter(proc.stdout.readline, ''):
                if line:
                    line = line.rstrip('\n\r')
                    output_lines.append(line)
                    if log and line.strip():
                        prefix = f"[{ts()}] " if ts else ''
                        try:
                            log(f"{prefix}{line}")
                        except Exception:
                            pass
            proc.wait()
            if proc.returncode == 0:
                _save_install_state(server_dir, {
                    'fingerprint': _requirements_fingerprint(req_path),
                    'installed': True,
                    'updated_at': str(datetime.now()),
                })
                return {
                    'ok': True, 'mode': 'requirements', 'installed': merged,
                    'failed': [], 'output': '\n'.join(output_lines),
                    'requirements_path': req_path
                }
            output_text = '\n'.join(output_lines)
        except Exception as e:
            output_text = str(e)
            if log:
                prefix = f"[{ts()}] " if ts else ''
                log(f"{prefix}خطأ في pip: {e}")

        fallback_targets = []
        seen = set()
        for item in merged:
            key = _requirement_key(item) or item.lower()
            if key not in seen:
                seen.add(key)
                fallback_targets.append(item)
        for module_name in _extract_missing_modules_from_output(output_text):
            pkg = _normalize_import_to_package(module_name)
            if pkg:
                key = _requirement_key(pkg) or str(pkg).lower()
                if key not in seen:
                    seen.add(key)
                    fallback_targets.insert(0, pkg)

        installed, failed = _pip_install_targets(
            fallback_targets, server_dir=server_dir, python_exe=python_exe,
            log=log, ts=ts
        )
        if installed and not failed:
            _save_install_state(server_dir, {
                'fingerprint': _requirements_fingerprint(req_path),
                'installed': True,
                'updated_at': str(datetime.now()),
            })
        return {
            'ok': len(installed) > 0 and not failed,
            'mode': 'fallback',
            'installed': installed,
            'failed': failed,
            'output': output_text,
            'requirements_path': req_path
        }



def sync_startup_requirements(server_id, main_file=None, requirements_file='requirements.txt', auto_install=True):
    """فحص ملفات Python داخل المشروع وتحديث requirements.txt تلقائيًا"""
    server_dir = get_server_dir(server_id)
    server, _ = get_server_by_id(server_id)
    if not server:
        return {
            'ok': False,
            'message': 'الخادم غير موجود',
            'added': [],
            'requirements_path': os.path.join(server_dir, requirements_file or 'requirements.txt')
        }

    startup_file = (main_file or server.get('main_file') or 'main.py').strip()
    req_name = (requirements_file or server.get('requirements_file') or 'requirements.txt').strip() or 'requirements.txt'
    startup_path = os.path.join(server_dir, startup_file)
    req_path = os.path.join(server_dir, req_name)

    source_files = _collect_project_python_files(server_dir)
    if os.path.exists(startup_path) and startup_path not in source_files and startup_path.endswith('.py'):
        source_files.insert(0, startup_path)

    if not source_files:
        return {
            'ok': False,
            'message': 'لم يتم العثور على ملفات Python',
            'added': [],
            'requirements_path': req_path
        }

    local_modules = _collect_local_module_names(server_dir)
    discovered = set()

    for source_path in source_files:
        try:
            with open(source_path, 'r', encoding='utf-8', errors='ignore') as f:
                source_text = f.read()
        except Exception:
            continue

        try:
            packages = extract_python_requirements(source_text)
        except Exception:
            packages = set()

        for pkg in packages:
            pkg_text = str(pkg).strip()
            if not pkg_text:
                continue
            pkg_key = _requirement_key(pkg_text)
            if not pkg_key:
                continue
            if pkg_key.lower() in local_modules:
                continue
            if pkg_key.lower() in {'pip', 'setuptools', 'wheel'}:
                continue
            discovered.add(pkg_text)

    existing = read_requirements_lines(req_path)
    merged = merge_requirements(existing, discovered)
    existing_keys = {_requirement_key(line) for line in existing if _requirement_key(line)}
    added = [item for item in merged if _requirement_key(item) not in existing_keys]

    if merged:
        os.makedirs(os.path.dirname(req_path), exist_ok=True)
        with open(req_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(merged) + "\n")

    if auto_install and merged:
        try:
            install_requirements_with_fallback(
                req_path,
                server_dir,
                main_file=startup_file,
                python_exe=sys.executable,
            )
        except Exception:
            pass

    return {
        'ok': True,
        'message': 'تمت مزامنة المتطلبات',
        'added': added,
        'requirements_path': req_path,
        'startup_path': startup_path
    }

def list_all_server_ids():
    """جمع كل معرفات الخوادم الفعّالة"""
    users = load_users()
    seen = []
    for uname, data in users.items():
        if uname == 'admin':
            continue
        servers = data.get('servers', [])
        if not isinstance(servers, list):
            continue
        for s in servers:
            if isinstance(s, dict):
                sid = s.get('server_id')
                if sid and sid not in seen:
                    seen.append(sid)
    return seen
def clear_server_log_files(server_id):
    """مسح سجل البوت الرئيسي كل دقيقة فقط. لا يلمس سجل Cloudflare أو سجلات النشر."""
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        if os.path.exists(log_file):
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write(f"[LOG RESET] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    except Exception:
        pass
def update_server_record(server_id, **changes):
    """تحديث بيانات الخادم داخل users.json"""
    users = load_users()
    updated = False
    for uname, data in users.items():
        if uname == 'admin':
            continue
        servers = data.get('servers', [])
        if not isinstance(servers, list):
            continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                s.update(changes)
                updated = True
                break
        if updated:
            break
    if updated:
        save_users(users)
    return updated

# ============================================
# 🔒 حماية التشغيل المزدوج للبوتات
# ============================================
def _process_is_alive(pid):
    """التحقق من أن PID ما زال لعملية تعمل فعلاً."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if psutil is not None:
            return psutil.pid_exists(pid)
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        return False

def _server_start_lock_path(server_id):
    # محفوظ للتوافق مع النسخ القديمة؛ القفل الفعلي أصبح داخل العملية.
    return os.path.join(get_server_dir(server_id), '.startup.lock')

def _acquire_server_start_lock(server_id):
    """قفل تشغيل داخل الذاكرة يمنع بدء نسختين في نفس الوقت دون بقاء lock قديم."""
    with BOT_START_LOCKS_GUARD:
        lock = BOT_START_LOCKS.get(server_id)
        if lock is None:
            lock = threading.Lock()
            BOT_START_LOCKS[server_id] = lock
    return lock if lock.acquire(blocking=False) else None

def _release_server_start_lock(lock):
    if lock is None:
        return
    try:
        lock.release()
    except RuntimeError:
        pass

def _get_restart_lock(server_id):
    with BOT_RESTART_LOCKS_GUARD:
        lock = BOT_RESTART_LOCKS.get(server_id)
        if lock is None:
            lock = threading.Lock()
            BOT_RESTART_LOCKS[server_id] = lock
        return lock

def _wait_process_exit(pid, timeout=10.0):
    try:
        pid = int(pid)
    except Exception:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _process_is_alive(pid):
            return True
        time.sleep(0.2)
    return not _process_is_alive(pid)

def _start_monitor_once(server_id, pid):
    with BOT_MONITORS_LOCK:
        current = BOT_MONITORS.get(server_id)
        if current and current.is_alive():
            return False
        t = threading.Thread(target=monitor_bot, args=(server_id, pid), daemon=True,
                             name=f'bot-monitor-{server_id}')
        BOT_MONITORS[server_id] = t
        t.start()
        return True

def restart_server_now(server_id, reason='scheduled'):
    """إعادة تشغيل عملية واحدة فقط، وانتظار موت الـPID القديم قبل البدء."""
    lock = _get_restart_lock(server_id)
    if not lock.acquire(blocking=False):
        return False, 'restart_in_progress'
    try:
        server, _ = get_server_by_id(server_id)
        if not server:
            return False, 'not_found'
        if server.get('stopped_by_user') or server.get('rate_limit_exceeded'):
            return False, 'disabled'

        update_server_record(server_id,
            restart_in_progress=True,
            last_restart_reason=reason,
            last_restart_at=str(datetime.now()))

        pid = server.get('pid')
        if pid and _process_is_alive(pid):
            stop_bot_process(pid)
            if not _wait_process_exit(pid, timeout=12.0):
                # محاولة أخيرة قبل تشغيل النسخة الجديدة.
                try:
                    if psutil is not None:
                        psutil.Process(int(pid)).kill()
                    elif sys.platform == 'win32':
                        subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
                    else:
                        os.kill(int(pid), 9)
                except Exception:
                    pass
                _wait_process_exit(pid, timeout=5.0)

        update_server_record(server_id, status='stopped', pid=None, stopped_by_user=False)

        new_pid, error = run_bot(server_id,
            server.get('main_file', 'main.py'),
            server.get('requirements_file', 'requirements.txt'))
        if new_pid:
            update_server_record(server_id,
                status='running', pid=new_pid, started_at=str(datetime.now()),
                stopped_by_user=False, restart_in_progress=False)
            _start_monitor_once(server_id, new_pid)
            return True, new_pid

        update_server_record(server_id, status='stopped', pid=None, restart_in_progress=False)
        return False, error or 'restart_failed'
    finally:
        lock.release()

def run_auto_maintenance_cycle():
    """تنظيف/تدوير السجلات ومراقبة البوتات دون إعادة تشغيل دورية افتراضيًا."""
    with AUTO_MAINTENANCE_LOCK:
        users = load_users()
        try:
            enforce_expired_accounts(users)
        except Exception:
            pass
        server_ids = list_all_server_ids()
        now = time.time()

        # تنظيف سريع فقط؛ لا نحجز القفل أثناء restart أو pip.
        for sid in server_ids:
            try:
                clear_server_log_files(sid)
            except Exception:
                pass

        restart_ids = []
        for sid in server_ids:
            try:
                server, _ = get_server_by_id(sid)
                if not server:
                    continue
                valid, _state = check_server_valid(sid)
                if not valid or server.get('stopped_by_user') or server.get('rate_limit_exceeded'):
                    continue
                if server.get('restart_in_progress'):
                    continue

                pid = server.get('pid')
                if server.get('status') == 'running' and not _process_is_alive(pid):
                    restart_ids.append((sid, 'auto_maintenance_dead_process'))
                    continue
                if server.get('status') != 'running':
                    continue

                # إعادة التشغيل الدوري اختيارية فقط. الافتراضي 0 حتى لا تنقطع البوتات بعد مدة.
                if AUTO_FORCED_RESTART_INTERVAL > 0:
                    last_restart = server.get('last_restart_at') or server.get('started_at')
                    elapsed = AUTO_FORCED_RESTART_INTERVAL + 1
                    if last_restart:
                        try:
                            elapsed = now - datetime.fromisoformat(str(last_restart)).timestamp()
                        except Exception:
                            pass
                    if elapsed >= AUTO_FORCED_RESTART_INTERVAL:
                        restart_ids.append((sid, 'forced_interval'))
            except Exception:
                continue

    # لا نحجز قفل الصيانة أثناء إيقاف/تشغيل البوت أو تثبيت المتطلبات.
    for index, (sid, reason) in enumerate(restart_ids):
        # توزيع عمليات restart قليلًا حتى لا تنطفئ كل الـAPIs/البوتات في نفس الثانية.
        def _restart_later(server_id=sid, restart_reason=reason, delay=index * 2):
            if delay:
                time.sleep(delay)
            restart_server_now(server_id, restart_reason)
        threading.Thread(target=_restart_later, daemon=True,
                         name=f'restart-{sid}').start()

def auto_maintenance_worker():
    """حلقة الصيانة التلقائية."""
    while True:
        try:
            run_auto_maintenance_cycle()
        except Exception:
            pass
        time.sleep(max(5, AUTO_MAINTENANCE_INTERVAL))

def start_auto_maintenance():
    """تشغيل خدمة الصيانة مرة واحدة داخل العملية الحالية."""
    global AUTO_MAINTENANCE_STARTED
    if not AUTO_MAINTENANCE_ENABLED or AUTO_MAINTENANCE_STARTED:
        return
    AUTO_MAINTENANCE_STARTED = True
    threading.Thread(target=auto_maintenance_worker, daemon=True, name='auto-maintenance').start()

def check_server_valid(server_id):
    """التحقق من صحة الخادم (غير منتهي الصلاحية)"""

    users = load_users()

    for uname, data in users.items():
        if uname == 'admin':
            continue
        servers = data.get('servers', [])
        if not isinstance(servers, list):
            continue

        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                if s.get('disabled') or s.get('status') == 'disabled':
                    return False, "disabled"

                account_expiry = get_user_effective_expiry(users, uname)
                if account_expiry and datetime.now() > account_expiry:
                    if disable_user_projects(users, uname, reason='expired'):
                        save_users(users)
                    return False, "expired"

                expiry = s.get('expiry', '')
                if expiry:
                    try:
                        exp_date = parse_expiry_datetime(expiry)
                        if exp_date and datetime.now() > exp_date:
                            if disable_user_projects(users, uname, reason='expired'):
                                save_users(users)
                            return False, "expired"
                    except Exception:
                        pass
                return True, s

    return False, "deleted"
def get_server_by_id(server_id):
    """الحصول على معلومات الخادم بواسطة المعرف"""
    
    users = load_users()
    
    for uname, data in users.items():
        if uname == 'admin': 
            continue
        servers = data.get('servers', [])
        if not isinstance(servers, list): 
            continue
        
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                return s, uname  # إرجاع الخادم واسم المستخدم
    
    return None, None
def create_default_files(server_dir, server_type='python'):
    """إنشاء الملفات الافتراضية للخادم حسب نوع المشروع"""
    os.makedirs(server_dir, exist_ok=True)
    def write_if_missing(path, content):
        if not os.path.exists(path):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
    # صفحة افتراضية عامة
    write_if_missing(os.path.join(server_dir, 'index.html'), """<!DOCTYPE html>
<html lang="ar">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ªGE تيم Hosting</title>
  <style>
    body{margin:0;font-family:Arial,sans-serif;background:#0b0f14;color:#e5eef7;direction:rtl}
    .wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
    .card{max-width:760px;width:100%;background:linear-gradient(180deg,#111827,#0a100c);border:1px solid #243244;border-radius:20px;padding:26px;box-shadow:0 20px 50px rgba(0,0,0,.35)}
    .tag{display:inline-block;background:#0f172a;color:#7CFF9B;border:1px solid #1f3b2c;padding:8px 14px;border-radius:999px;margin-bottom:16px}
    h1{margin:0 0 8px;color:#7CFF9B;font-size:32px}
    p{color:#cbd5e1;line-height:1.8}
    .btn{display:inline-block;background:#2563eb;color:#fff;text-decoration:none;padding:12px 16px;border-radius:12px;margin-top:10px}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="tag">ªGE تيم</div>
      <h1>الموقع يعمل بنجاح</h1>
      <p>هذه الصفحة الافتراضية قابلة للتعديل من مدير الملفات. استبدلها بملفك <b>index.html</b> أو ارفع تطبيق <b>Flask</b> داخل <b>app.py</b> أو <b>main.py</b>.</p>
      <a class="btn" href="./">تحديث الصفحة</a>
    </div>
  </div>
</body>
</html>""")
    # ملف تشغيل Python عام
    write_if_missing(os.path.join(server_dir, 'main.py'), """# 🚀 بوت افتراضي - ªGE تيم HOSTING
import time
print("=" * 40)
print("البوت يعمل على ªGE تيم HOSTING")
print("الخادم جاهز!")
print("=" * 40)
عداد = 0
while True:
    عداد += 1
    print(f"[{time.strftime('%H:%M:%S')}] نبضة #{عداد} | الخادم نشط")
    time.sleep(10)
""")
    # ملف المتطلبات
    write_if_missing(os.path.join(server_dir, 'requirements.txt'), '# أضف حزم pip هنا\n')
    # قوالب خاصة حسب نوع المشروع
    server_type = (server_type or 'python').lower().strip()
    if server_type in {'flask', 'fastapi'}:
        write_if_missing(os.path.join(server_dir, 'app.py'), """from flask import Flask
app = Flask(__name__)
@app.route("/")
def home():
    return "<h1>ªGE تيم Hosting - Flask App</h1>"
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
""")
    elif server_type in {'node', 'express'}:
        write_if_missing(os.path.join(server_dir, 'index.js'), """const http = require('http');
const server = http.createServer((req, res) => {
  res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'});
  res.end('<h1>ªGE تيم Hosting - Node.js App</h1>');
});
server.listen(process.env.PORT || 3000);
""")
        write_if_missing(os.path.join(server_dir, 'package.json'), """{
  "name": "age-team-app",
  "version": "1.0.0",
  "main": "index.js",
  "scripts": { "start": "node index.js" }
}
""")
    elif server_type in {'php'}:
        write_if_missing(os.path.join(server_dir, 'index.php'), """<?php
echo "<h1>ªGE تيم Hosting - PHP Website</h1>";
?>
""")
    elif server_type in {'static', 'html'}:
        write_if_missing(os.path.join(server_dir, 'style.css'), "body{font-family:Arial,sans-serif;background:#0b0f14;color:#fff;}")
        write_if_missing(os.path.join(server_dir, 'script.js'), "console.log('ªGE تيم Hosting');")
# ============================================
# 🧠 معاينة وتشغيل مواقع Flask
# ============================================
SITE_BROWSER_TEMPLATE = '''
<!DOCTYPE html>
<html lang="ar">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ªGE تيم | Site Browser</title>
  <style>
    body{margin:0;font-family:Arial,sans-serif;background:#0b0f14;color:#e5eef7;direction:rtl}
    .wrap{max-width:1100px;margin:24px auto;padding:16px}
    .card{background:#111827;border:1px solid #243244;border-radius:18px;padding:18px}
    .top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
    .tag{display:inline-block;background:#0f172a;color:#7CFF9B;border:1px solid #1f3b2c;padding:8px 14px;border-radius:999px}
    a{color:#7dd3fc;text-decoration:none}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:16px}
    .item{background:#0f172a;border:1px solid #243244;border-radius:14px;padding:14px;min-height:110px;display:flex;flex-direction:column;justify-content:space-between}
    .name{font-weight:700;word-break:break-word}
    .meta{font-size:12px;color:#94a3b8;margin-top:8px}
    .badge{display:inline-block;font-size:11px;padding:4px 8px;border-radius:999px;background:#1f2937;color:#cbd5e1;border:1px solid #334155;margin-top:8px;width:max-content}
    .btn{display:inline-block;background:#2563eb;color:#fff;padding:10px 14px;border-radius:12px}
    .crumb{color:#94a3b8;font-size:13px;margin-top:8px;word-break:break-word}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="top">
        <div>
          <div class="tag">ªGE تيم • SITE</div>
          <h1 style="margin:12px 0 4px">عرض ملفات الموقع</h1>
          <div class="crumb">{{ display_path }}</div>
        </div>
        <div>
          <a class="btn" href="{{ base_url }}/{{ server_id }}/home/api/server/">Refresh</a>
        </div>
      </div>
      {% if parent_link %}
      <div style="margin-top:14px"><a href="{{ parent_link }}">↩ الرجوع للمجلد السابق</a></div>
      {% endif %}
      <div class="grid">
        {% for e in entries %}
        <div class="item">
          <div>
            <div class="name">
              {% if e.is_dir %}📁{% else %}{{ e.icon }}{% endif %}
              <a href="{{ e.url }}">{{ e.name }}</a>
            </div>
            <div class="meta">{{ e.modified }}</div>
            {% if e.size_text %}<div class="meta">{{ e.size_text }}</div>{% endif %}
          </div>
          <div class="badge">{{ e.badge }}</div>
        </div>
        {% endfor %}
      </div>
    </div>
  </div>
</body>
</html>
'''
SITE_APP_CACHE = {}  # كاش تطبيقات Flask للمواقع المرفوعة
SITE_INDEX_FILES = ('index.html', 'index.htm', 'home.html')
def is_flask_python_file(file_path):
    # التأكد أن ملف Python يحتوي على Flask
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        return (
            'from flask import' in content
            or 'import flask' in content
            or 'Flask(' in content
            or 'Flask (__name__' in content
            or 'Flask(__name__)' in content
        )
    except Exception:
        return False
def list_site_python_candidates(server_dir):
    # ترتيب ملفات Python المرشحة لصفحة الموقع
    priority = []
    for name in ('app.py', 'main.py'):
        p = os.path.join(server_dir, name)
        if os.path.isfile(p):
            priority.append(p)
    others = []
    for root, _, files in os.walk(server_dir):
        for name in files:
            if name.endswith('.py'):
                p = os.path.join(root, name)
                if p not in priority and p not in others:
                    others.append(p)
    return priority + sorted(others)
def serve_static_site_file(server_dir, subpath):
    # تقديم ملف ثابت من الموقع إذا كان موجوداً
    target = os.path.normpath(subpath or '').lstrip(os.sep)
    if target:
        candidate = os.path.join(server_dir, target)
        if os.path.isdir(candidate):
            for index_name in SITE_INDEX_FILES:
                index_candidate = os.path.join(candidate, index_name)
                if os.path.isfile(index_candidate):
                    rel = os.path.relpath(index_candidate, server_dir)
                    return send_from_directory(server_dir, rel)
        if os.path.isfile(candidate):
            return send_from_directory(server_dir, target)
    else:
        for index_name in SITE_INDEX_FILES:
            index_candidate = os.path.join(server_dir, index_name)
            if os.path.isfile(index_candidate):
                return send_from_directory(server_dir, index_name)
    return None
def load_flask_site_app(server_id):
    # تحميل تطبيق Flask الموجود داخل ملفات الموقع إن وجد
    server_dir = get_server_dir(server_id)
    candidates = list_site_python_candidates(server_dir)
    sig = tuple((p, os.path.getmtime(p)) for p in candidates if os.path.exists(p))
    cached = SITE_APP_CACHE.get(server_id)
    if cached and cached.get('sig') == sig:
        return cached.get('app')

    last_error = None
    retried_after_install = False

    while True:
        for path in candidates:
            if not is_flask_python_file(path):
                continue
            module_name = f"site_module_{server_id}_{abs(hash(path))}_{int(os.path.getmtime(path))}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if not spec or not spec.loader:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                flask_app = getattr(module, 'app', None) or getattr(module, 'application', None)
                if flask_app and hasattr(flask_app, 'test_client'):
                    SITE_APP_CACHE[server_id] = {'sig': sig, 'app': flask_app, 'path': path, 'module': module}
                    return flask_app
            except Exception as exc:
                last_error = str(exc)

        missing_hint = (last_error or '').lower()
        if (not retried_after_install) and ('module not found' in missing_hint or 'no module named' in missing_hint):
            try:
                _install_packages_for_server(
                    server_dir,
                    main_file='main.py',
                    requirements_file='requirements.txt',
                    log=None,
                    ts=None,
                    python_exe=sys.executable,
                    reason_text=last_error or ''
                )
            except Exception:
                pass
            SITE_APP_CACHE.pop(server_id, None)
            retried_after_install = True
            continue
        break

    SITE_APP_CACHE[server_id] = {'sig': sig, 'app': None, 'error': last_error}
    return None
def proxy_request_to_flask_app(flask_app, subpath=''):
    # تمرير الطلب إلى تطبيق Flask المرفوع داخل الموقع
    client = flask_app.test_client()
    path = '/' + (subpath or '').lstrip('/')
    if path == '/':
        path = '/'
    headers = {}
    for key, value in request.headers.items():
        lk = key.lower()
        if lk in {'host', 'content-length', 'connection', 'accept-encoding', 'transfer-encoding'}:
            continue
        headers[key] = value
    resp = client.open(
        path=path,
        method=request.method,
        data=request.get_data(),
        headers=headers,
        query_string=request.query_string.decode('utf-8', errors='ignore'),
        content_type=request.content_type,
        follow_redirects=False
    )
    excluded = {'content-length', 'connection', 'transfer-encoding', 'content-encoding'}
    response_headers = []
    for key, value in resp.headers.items():
        if key.lower() not in excluded:
            response_headers.append((key, value))
    return Response(resp.get_data(), status=resp.status_code, headers=response_headers)
def render_site_browser(server_id, server_dir, subpath=''):
    # عرض متصفح ملفات الموقع عندما لا يوجد index أو Flask app
    rel_path = subpath.strip('/')
    if rel_path:
        current_dir = os.path.join(server_dir, rel_path)
    else:
        current_dir = server_dir
    if not os.path.exists(current_dir):
        current_dir = server_dir
        rel_path = ''
    entries = []
    try:
        for name in sorted(os.listdir(current_dir), key=lambda x: (not os.path.isdir(os.path.join(current_dir, x)), x.lower())):
            full = os.path.join(current_dir, name)
            entry_path = f"{rel_path}/{name}" if rel_path else name
            is_dir = os.path.isdir(full)
            if is_dir:
                url = f"/{server_id}/home/api/server/{entry_path}/"
                badge = 'Folder'
                size_text = ''
                icon = '📄'
            else:
                url = f"/{server_id}/home/api/server/{entry_path}"
                ext = os.path.splitext(name)[1].lower()
                icon = {
                    '.html': '🌐',
                    '.htm': '🌐',
                    '.css': '🎨',
                    '.js': '🧩',
                    '.py': '🐍',
                    '.json': '📦',
                    '.txt': '📝',
                    '.zip': '🗜️'
                }.get(ext, '📄')
                if ext == '.py' and is_flask_python_file(full):
                    badge = 'Flask App'
                elif ext == '.html':
                    badge = 'HTML'
                elif ext == '.py':
                    badge = 'Python'
                else:
                    badge = (ext.replace('.', '').upper() or 'File')
                try:
                    size = os.path.getsize(full)
                    size_text = f"{size/1024:.1f} KB" if size < 1024 * 1024 else f"{size/(1024*1024):.2f} MB"
                except Exception:
                    size_text = ''
            entries.append({
                'name': name,
                'url': url,
                'is_dir': is_dir,
                'badge': badge,
                'size_text': size_text,
                'icon': icon,
                'modified': datetime.fromtimestamp(os.path.getmtime(full)).strftime('%Y-%m-%d %H:%M')
            })
    except Exception:
        entries = []
    parent_link = None
    if rel_path:
        parent = '/'.join(rel_path.split('/')[:-1]).strip('/')
        parent_link = f"/{server_id}/home/api/server/{parent}/" if parent else f"/{server_id}/home/api/server/"
    display_path = '/' + rel_path if rel_path else '/'
    return render_template_string(
        SITE_BROWSER_TEMPLATE,
        entries=entries,
        parent_link=parent_link,
        display_path=display_path,
        server_id=server_id,
        base_url=request.host_url.rstrip('/')
    )
# ============================================
# 🤖 تشغيل البوت
# ============================================
def run_bot(server_id, main_file='main.py', requirements_file='requirements.txt'):
    """تشغيل البوت في خادم مع منع التشغيل المزدوج."""

    start_lock = _acquire_server_start_lock(server_id)
    if start_lock is None:
        existing_server, _ = get_server_by_id(server_id)
        existing_pid = existing_server.get('pid') if existing_server else None
        if _process_is_alive(existing_pid):
            return existing_pid, None
        return None, 'البوت قيد التشغيل أو البدء بالفعل، حاول مرة أخرى بعد لحظة.'
    
    # تحديد المسارات
    server_dir = get_server_dir(server_id)
    main_path = os.path.join(server_dir, main_file)
    log_file = os.path.join(server_dir, 'output.log')
    python_exe = sys.executable
    
    # دالة لتسجيل الرسائل
    def log(msg):
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"{msg}\n")
                f.flush()
        except: 
            pass
    
    # التحقق من وجود ملف main.py
    if not os.path.exists(main_path):
        _release_server_start_lock(start_lock)
        return None, f"خطأ: {main_file} غير موجود!"
    
    # تنظيف سجل التشغيل القديم
    if os.path.exists(log_file):
        try: 
            os.remove(log_file)
        except: 
            open(log_file, 'w').close()
    
    # دالة للحصول على الوقت
    ts = lambda: datetime.now().strftime('%I:%M:%S %p')
    
    # جلب حد المعالج
    server, _ = get_server_by_id(server_id)
    cpu_limit = server.get('cpu_limit', 80) if server else 80

    existing_pid = server.get('pid') if server else None
    if _process_is_alive(existing_pid):
        log(f"[{ts()}] ⚠️ البوت يعمل بالفعل على PID: {existing_pid} — تم منع التشغيل المكرر")
        _release_server_start_lock(start_lock)
        return existing_pid, None
    log(f"[{ts()}] جاري التحقق من حد الاستخدام...")
    log(f"[{ts()}] حد المعالج: {'غير محدود' if cpu_limit in (0, None) else str(cpu_limit) + '%'}")
    log("")
    
            # ====== تثبيت المتطلبات ======
    req_name = (requirements_file or 'requirements.txt').strip() or 'requirements.txt'
    req_path = os.path.join(server_dir, req_name)

    # مزامنة requirements.txt من جميع ملفات Python داخل المشروع تلقائيًا
    try:
        sync_result = sync_startup_requirements(
            server_id,
            main_file=main_file,
            requirements_file=req_name,
            auto_install=False
        )
        if sync_result.get('added'):
            log(f"[{ts()}] تمت إضافة متطلبات جديدة تلقائيًا: {', '.join(sync_result['added'])}")
    except Exception as e:
        log(f"[{ts()}] تعذر مزامنة المتطلبات تلقائيًا: {str(e)}")

    if not os.path.exists(req_path):
        try:
            open(req_path, 'a', encoding='utf-8').close()
        except Exception:
            pass

    log(f"[{ts()}] جاري فحص وتثبيت الحزم من main.py و requirements.txt")
    log("")
    try:
        install_result = install_requirements_with_fallback(
            req_path,
            server_dir,
            main_file=main_file,
            log=log,
            ts=ts,
            python_exe=python_exe,
        )
        if install_result.get('ok'):
            if install_result.get('mode') == 'fallback':
                log(f"[{ts()}] تم تثبيت الحزم مع وضع الطوارئ")
            elif install_result.get('mode') == 'discovered':
                log(f"[{ts()}] تم تثبيت الحزم المكتشفة من main.py")
            else:
                log(f"[{ts()}] تم تثبيت المتطلبات بنجاح!")
        else:
            log(f"[{ts()}] لم يتم تثبيت أي حزم جديدة أو فشل التثبيت")
            try:
                fallback_text = str(install_result.get('output') or '')
                if install_result.get('failed'):
                    fallback_text += '\n' + '\n'.join(install_result.get('failed') or [])
                extra_installed, extra_failed = _install_packages_for_server(
                    server_dir,
                    main_file=main_file,
                    requirements_file=req_name,
                    log=log,
                    ts=ts,
                    python_exe=python_exe,
                    reason_text=fallback_text
                )
                if extra_installed:
                    log(f"[{ts()}] تم اكتشاف وتثبيت حزم إضافية: {', '.join(extra_installed)}")
            except Exception as e2:
                log(f"[{ts()}] تعذر تنفيذ محاولات إضافية لتثبيت الحزم: {str(e2)}")
    except Exception as e:
        log(f"[{ts()}] خطأ في تثبيت الحزم: {str(e)}")
# ====== تشغيل البوت ======
    log("")
    log(f"[{ts()}] جاري تشغيل: python {main_file}")
    log(f"[{ts()}] إصدار Python: {sys.version.split()[0]}")
    log("")
    
    try:
        main_path_abs = os.path.abspath(main_path)
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'
        
        # تشغيل البوت كعملية منفصلة
        proc = subprocess.Popen(
            [python_exe, main_path_abs],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=server_dir,
            text=True, encoding='utf-8', errors='replace',
            bufsize=1, env=env, universal_newlines=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )
        
        log(f"[{ts()}] تم تشغيل الخادم")
        log(f"[{ts()}] معرف العملية (PID): {proc.pid}")
        log("")
        
        # ====== مراقبة المعالج ======
        def rate_monitor():
            while proc.poll() is None:
                time.sleep(5)
                exceeded, avg_cpu = rate_limiter.check_rate(server_id, cpu_limit)
                if exceeded:
                    log(f"[{datetime.now().strftime('%I:%M:%S %p')}] 🚨 تجاوز حد المعالج! {avg_cpu:.1f}% > {cpu_limit}%")
                    proc.terminate()
                    time.sleep(2)
                    if proc.poll() is None: 
                        proc.kill()
                    
                    # تحديث حالة الخادم
                    users = load_users()
                    for uname, data in users.items():
                        if uname == 'admin': continue
                        servers = data.get('servers', [])
                        if not isinstance(servers, list): continue
                        for s in servers:
                            if isinstance(s, dict) and s.get('server_id') == server_id:
                                s['status'] = 'stopped'
                                s['pid'] = None
                                s['rate_limit_exceeded'] = True
                                s['stopped_by_user'] = False
                                save_users(users)
                                break
                    break
        
        # بدء مراقبة المعالج في خيط منفصل
        threading.Thread(target=rate_monitor, daemon=True).start()
        
        # ====== تسجيل المخرجات ======
        def stream_output():
            try:
                with open(log_file, 'a', encoding='utf-8') as f:
                    for line in iter(proc.stdout.readline, ''):
                        if line:
                            line = line.rstrip('\n\r')
                            if line:
                                f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] {line}\n")
                                f.flush()
            except: 
                pass
        
        # بدء تسجيل المخرجات في خيط منفصل
        threading.Thread(target=stream_output, daemon=True).start()
        
        pid = proc.pid
        _release_server_start_lock(start_lock)
        return pid, None  # نجاح التشغيل
        
    except Exception as e:
        log(f"[{ts()}] ❌ خطأ: {str(e)}")
        _release_server_start_lock(start_lock)
        return None, str(e)
def stop_bot_process(pid):
    """إيقاف عملية البوت بأمان، مع SIGTERM ثم SIGKILL عند الحاجة."""
    try:
        pid = int(pid)
        if not _process_is_alive(pid):
            return True
        if sys.platform == 'win32':
            subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
            return True
        os.kill(pid, 15)
        if _wait_process_exit(pid, timeout=8.0):
            return True
        try:
            os.kill(pid, 9)
        except Exception:
            pass
        return _wait_process_exit(pid, timeout=3.0)
    except Exception:
        return False
def monitor_bot(server_id, pid):
    """مراقبة البوت بدون إنشاء monitors متكررة."""
    try:
        while True:
            if not _process_is_alive(pid):
                break
            time.sleep(5)

        server, _ = get_server_by_id(server_id)
        if not server:
            return
        if server.get('stopped_by_user') or server.get('rate_limit_exceeded') or server.get('restart_in_progress'):
            return

        if should_auto_restart(server_id):
            time.sleep(1)
            new_pid, error = run_bot(server_id,
                server.get('main_file', 'main.py'),
                server.get('requirements_file', 'requirements.txt'))
            if new_pid:
                update_server_record(server_id,
                    status='running', pid=new_pid, started_at=str(datetime.now()),
                    rate_limit_exceeded=False, stopped_by_user=False)
                _start_monitor_once(server_id, new_pid)
            else:
                update_server_record(server_id, status='stopped', pid=None)
        else:
            update_server_record(server_id, status='stopped', pid=None)
    finally:
        with BOT_MONITORS_LOCK:
            current = BOT_MONITORS.get(server_id)
            if current is threading.current_thread():
                BOT_MONITORS.pop(server_id, None)
# ============================================
# 📊 إحصائيات الأداء
# ============================================
def get_process_stats(pid):
    """جلب إحصائيات العملية (CPU و RAM)"""
    if not _psutil_available():
        return {'cpu_percent': 0, 'ram_mb': 0, 'ram_display': '0 MB'}
    try:
        proc = psutil.Process(pid)
        cpu = proc.cpu_percent(interval=0.5)
        mem = proc.memory_info()
        ram = mem.rss / (1024 * 1024)
        return {
            'cpu_percent': round(cpu, 1),
            'ram_mb': round(ram, 1),
            'ram_display': f"{ram:.1f} MB" if ram < 1024 else f"{ram/1024:.1f} GB",
        }
    except Exception:
        return {'cpu_percent': 0, 'ram_mb': 0, 'ram_display': '0 MB'}
def get_network_stats(psutil_pid):
    """جلب إحصائيات الشبكة"""
    if not _psutil_available():
        return "0 KB", "0 KB"
    try:
        proc = psutil.Process(psutil_pid)
        io = proc.io_counters()
        if io:
            read_kb = io.read_bytes / 1024
            write_kb = io.write_bytes / 1024
            return format_bytes(read_kb), format_bytes(write_kb)
    except Exception:
        pass
    return "0 KB", "0 KB"
def format_bytes(kb):
    """تنسيق حجم البايتات"""
    if kb < 1024: 
        return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb < 1024: 
        return f"{mb:.1f} MB"
    gb = mb / 1024
    return f"{gb:.2f} GB"


def build_hosting_details(username, password, cpu_limit, ram, disk, expiry_dt, server_id, panel_url, project_name='ªGE Hosting'):
    """بناء نص الاستضافة القابل للنسخ مباشرة"""
    expiry_text = format_expiry_datetime(expiry_dt)
    cpu_text = 'غير محدود' if cpu_limit in (0, None) else f"{cpu_limit}%"
    panel_text = panel_url or ''
    return f"""HOSTING CREATED SUCCESSFULLY

Project Name : {project_name}
Username     : {username}
Password     : {password}
CPU Limit    : {cpu_text}
RAM          : {ram}
Disk         : {disk}
Expiry Date  : {expiry_text}
Server ID    : {server_id}
Panel URL    : {panel_text}
Status       : ACTIVE"""

# ============================================
# ☁️ Cloudflare Tunnel helpers
def _cloud_state(server_id):
    with CLOUDFLARED_LOCK:
        return CLOUDFLARE_STATES.setdefault(server_id, {
            'process': None,
            'url': None,
            'port': None,
            'started_at': None,
            'log_path': os.path.join(get_server_dir(server_id), 'cloudflared.log'),
        })

def _cloud_log_path(server_id):
    path = os.path.join(get_server_dir(server_id), 'cloudflared.log')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path

def _cloudflared_binary_path():
    return os.path.join(CLOUDFLARED_DIR, 'cloudflared')

def _cloudflared_download_url():
    machine = platform.machine().lower()
    if machine in ('x86_64', 'amd64'):
        return 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64'
    if machine in ('aarch64', 'arm64'):
        return 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64'
    if machine in ('armv7l', 'armv7'):
        return 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm'
    raise RuntimeError(f'Unsupported architecture: {machine}')

def _cloudflared_candidates():
    candidates=[]
    found=shutil.which('cloudflared')
    if found: candidates.append(found)
    prefix=os.getenv('PREFIX')
    if prefix: candidates.append(os.path.join(prefix,'bin','cloudflared'))
    home=os.path.expanduser('~')
    candidates += [os.path.join(home,'.local','bin','cloudflared'), os.path.join(home,'bin','cloudflared'), _cloudflared_binary_path()]
    return list(dict.fromkeys(candidates))

def ensure_cloudflared():
    for candidate in _cloudflared_candidates():
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK): return candidate
    os.makedirs(CLOUDFLARED_DIR, exist_ok=True)
    path=_cloudflared_binary_path(); tmp_path=path+'.tmp'
    try:
        req=urllib.request.Request(_cloudflared_download_url(), headers={'User-Agent':'AGE-Hosting/1.0'})
        with urllib.request.urlopen(req, timeout=60) as response, open(tmp_path,'wb') as out: shutil.copyfileobj(response,out)
        os.chmod(tmp_path,0o755); os.replace(tmp_path,path); return path
    except Exception as exc:
        raise RuntimeError(f'تعذر تنزيل cloudflared: {exc}') from exc
    finally:
        try:
            if os.path.exists(tmp_path): os.remove(tmp_path)
        except Exception: pass

def _cloudflare_reader(server_id, proc):
    pattern=re.compile(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com')
    log_path=_cloud_log_path(server_id)
    try:
        with open(log_path,'a',encoding='utf-8') as log:
            for raw in iter(proc.stdout.readline,''):
                if not raw: break
                line=raw.rstrip('\r\n')
                log.write(f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {line}\n'); log.flush()
                match=pattern.search(line)
                if match:
                    with CLOUDFLARED_LOCK:
                        st=_cloud_state(server_id)
                        if st.get('process') is proc: st['url']=match.group(0)
    except Exception as exc:
        try:
            with open(log_path,'a',encoding='utf-8') as log: log.write(f'[READER ERROR] {exc}\n')
        except Exception: pass

def _valid_port(value):
    try: p=int(str(value).strip()); return 1 <= p <= 65535
    except Exception: return False

def _extract_ports_from_text(text):
    found=set()
    if not text: return found
    patterns=[r"(?:app|server)\.run\s*\([^\n]*?\bport\s*=\s*(\d{2,5})", r"\bport\s*[:=]\s*[\"']?(\d{2,5})", r"--port\s+(\d{2,5})", r"(?:PORT|port)\s*=\s*[\"']?(\d{2,5})", r"listen\s*\([^\n]*?,?\s*(\d{2,5})\s*\)", r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):([0-9]{2,5})"]
    for pattern in patterns:
        for m in re.finditer(pattern,text,flags=re.IGNORECASE):
            port=int(m.group(1))
            if _valid_port(port): found.add(port)
    return found

def detect_project_ports(server_id):
    ports=set(); server,_=get_server_by_id(server_id)
    if server:
        pid=server.get('pid')
        if pid and psutil is not None:
            try:
                proc=psutil.Process(int(pid))
                for p in [proc,*proc.children(recursive=True)]:
                    try:
                        for conn in p.net_connections(kind='inet'):
                            if conn.status == psutil.CONN_LISTEN and conn.laddr and _valid_port(conn.laddr.port): ports.add(int(conn.laddr.port))
                    except Exception: pass
            except Exception: pass
    server_dir=get_server_dir(server_id)
    for root,dirs,files in os.walk(server_dir):
        dirs[:]=[d for d in dirs if d not in {'.git','__pycache__','.venv','venv','node_modules'}]
        for name in files:
            if not name.endswith(('.py','.js','.ts','.json','.yaml','.yml','.toml','.ini','.env','.txt','.sh')): continue
            path=os.path.join(root,name)
            try:
                if os.path.getsize(path)>2*1024*1024: continue
                with open(path,'r',encoding='utf-8',errors='ignore') as f: ports.update(_extract_ports_from_text(f.read()))
            except Exception: pass
    env_port=os.getenv('PORT')
    if _valid_port(env_port): ports.add(int(env_port))
    return sorted(ports)

def choose_cloud_port(server_id, requested_port=None):
    if requested_port is not None and str(requested_port).strip()!='':
        if not _valid_port(requested_port): raise ValueError('المنفذ يجب أن يكون بين 1 و 65535')
        return int(requested_port),'manual'
    detected=detect_project_ports(server_id)
    if detected: return detected[0],'auto'
    env_port=os.getenv('PORT')
    if _valid_port(env_port): return int(env_port),'env'
    return 5000,'default'

def start_cloudflared(server_id, requested_port=None):
    try:
        port,port_source=choose_cloud_port(server_id,requested_port)
        binary=ensure_cloudflared()
    except Exception as exc:
        return False,None,f'فشل تجهيز Cloudflare: {exc}'
    with CLOUDFLARED_LOCK:
        st=_cloud_state(server_id); old=st.get('process')
        if old is not None and old.poll() is None:
            return True,st.get('url'),None
        st.update({'process':None,'url':None,'port':port,'started_at':datetime.now().isoformat(),'log_path':_cloud_log_path(server_id)})
    target=f'http://127.0.0.1:{port}'; log_path=_cloud_log_path(server_id)
    try:
        with open(log_path,'a',encoding='utf-8') as log:
            log.write(f'\n[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] === START CLOUDFLARE ({server_id}) ===\n')
            log.write(f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] Starting: cloudflared tunnel --url {target} --protocol http2 --no-autoupdate\n')
            log.write(f'[INFO] Project={server_id} | Port={port} | Source={port_source}\n'); log.flush()
        env=os.environ.copy(); env['TUNNEL_ORIGIN_REQUEST_HEADER']='X-AGE-Hosting: cloudflare'
        proc=subprocess.Popen([binary,'tunnel','--url',target,'--protocol','http2','--no-autoupdate'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1,env=env,cwd=os.path.dirname(binary) or None)
        with CLOUDFLARED_LOCK:
            st=_cloud_state(server_id); st['process']=proc
        threading.Thread(target=_cloudflare_reader,args=(server_id,proc),daemon=True).start()
        time.sleep(1.2)
        if proc.poll() is not None:
            with open(log_path,'r',encoding='utf-8',errors='replace') as log: details=log.read()[-6000:]
            with CLOUDFLARED_LOCK: st=_cloud_state(server_id); st.update({'process':None,'url':None})
            return False,None,'cloudflared توقف مباشرة.\n'+(details.strip() or 'تحقق من سجل Cloudflare.')
        return True,_cloud_state(server_id).get('url'),None
    except Exception as exc:
        with CLOUDFLARED_LOCK: st=_cloud_state(server_id); st.update({'process':None,'url':None})
        return False,None,f'فشل تشغيل Cloudflare: {exc}'

def stop_cloudflared(server_id):
    with CLOUDFLARED_LOCK:
        st=_cloud_state(server_id); proc=st.get('process')
        if proc is None or proc.poll() is not None:
            st.update({'process':None,'url':None,'port':None}); return True
    try:
        proc.terminate(); proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try: proc.kill(); proc.wait(timeout=3)
        except Exception: pass
    except Exception: return False
    finally:
        with CLOUDFLARED_LOCK: _cloud_state(server_id).update({'process':None,'url':None,'port':None})
    return True

def cloudflared_status(server_id):
    with CLOUDFLARED_LOCK:
        st=_cloud_state(server_id); proc=st.get('process'); running=proc is not None and proc.poll() is None
        return {'running':running,'url':st.get('url'),'target':f"http://127.0.0.1:{st.get('port')}" if st.get('port') else None,'port':st.get('port'),'ports':detect_project_ports(server_id)}

# ============================================
# 🌐 API العامة - إنشاء خادم جديد
# ============================================
@app.route('/api/create', methods=['GET'])
def api_create_server():
    """إنشاء خادم جديد عبر API"""
    
    # جلب المعلمات من الطلب
    username = request.args.get('username', '').strip()
    password = request.args.get('password', '').strip()
    server_type = request.args.get('type', 'python').strip()
    ram = request.args.get('ram', '1GB').strip()
    disk = request.args.get('disk', '1GB').strip()
    cpu_limit = int(request.args.get('cpu', '30'))
    try:
        days = int(os.getenv('DEFAULT_ACCOUNT_DAYS', '30'))
    except Exception:
        days = 30
    
    # توليد كلمة مرور إذا لم يتم توفيرها
    if not password:
        password = generate_random_password(10)
    
    # توليد اسم مستخدم إذا لم يتم توفيره
    if not username:
        username = f"ªGE تيم_CODEX{random.randint(10000, 99999)}"
    
    # التحقق من صحة البيانات
    if len(username) < 3:
        return jsonify({'status': 'error', 'message': 'اسم المستخدم يجب أن يكون 3 أحرف على الأقل!'}), 400
    
    if len(password) < 4:
        return jsonify({'status': 'error', 'message': 'كلمة المرور يجب أن تكون 4 أحرف على الأقل!'}), 400
    
    if cpu_limit != 0 and (cpu_limit < 10 or cpu_limit > 100):
        return jsonify({'status': 'error', 'message': 'حد المعالج يجب أن يكون 10–100 أو غير محدود!'}), 400
    
    if days < 1 or days > 365:
        return jsonify({'status': 'error', 'message': 'عدد الأيام يجب أن يكون بين 1 و 365!'}), 400
    
    # تحميل المستخدمين
    users = load_users()
    
    # التحقق من عدم وجود اسم المستخدم
    if username in users:
        return jsonify({'status': 'error', 'message': f"اسم المستخدم '{username}' موجود بالفعل!"}), 400
    
    # إنشاء معرف فريد للخادم
    server_id = str(uuid.uuid4())[:8]
    expiry_date = datetime.now() + timedelta(days=days)
    
    # إنشاء الملفات الافتراضية
    create_default_files(get_server_dir(server_id), server_type)
    
    # بناء رابط الخادم
    base_url = request.host_url.rstrip('/')
    login_url = f"{base_url}/{server_id}/login"
    panel_url = f"{base_url}/{server_id}/home/api/server"
    api_url = f"{panel_url}/api"
    full_url = panel_url
    
    # إنشاء بيانات الخادم
    new_server = {
        'server_id': server_id,
        'login_url': f"/{server_id}/login",
        'dashboard_url': f"/{server_id}/home/api/server",
        'full_link': full_url,
        'type': server_type,
        'ram': ram, 
        'disk': disk,
        'status': 'stopped', 
        'pid': None,
        'created': str(datetime.now()),
        'expiry': str(expiry_date),
        'main_file': 'main.py',
        'requirements_file': 'requirements.txt',
        'cpu_limit': cpu_limit,
        'rate_limit_exceeded': False,
        'stopped_by_user': False
    }
    
    # إضافة المستخدم والخادم
    users[username] = {
        'password': password, 
        'role': 'user', 
        'servers': [new_server]
    }
    save_users(users)
    
    # إرجاع النتيجة
    hosting_details = build_hosting_details(
        username=username,
        password=password,
        cpu_limit=cpu_limit,
        ram=ram,
        disk=disk,
        expiry_dt=expiry_date,
        server_id=server_id,
        panel_url=panel_url,
        project_name='ªGE Hosting'
    )

    return jsonify({
        'status': 'success',
        'message': 'تم إنشاء اللوحة بنجاح!',
        'hosting_details': hosting_details,
        'details_text': hosting_details,
        'username': username,
        'password': password,
        'server_type': server_type,
        'ram': ram,
        'disk': disk,
        'cpu_limit': cpu_limit,
        'validity': f'{days} يوم',
        'expiry_date': expiry_date.strftime('%Y-%m-%d'),
        'login_url': login_url,
        'full_url': full_url,
        'api_url': api_url,
        'dashboard_url': f"{base_url}/{server_id}/home/api/server",
        'server_id': server_id
    }), 200
# ============================================
# ☁️ Cloudflare Tunnel API
# ============================================
def _cloudflare_request_allowed(server_id):
    if 'user' not in session or session.get('role') != 'user':
        return False, jsonify({'status': 'error', 'msg': 'غير مصرح!'}), 403
    server, owner = get_server_by_id(server_id)
    if not server or owner != session.get('user'):
        return False, jsonify({'status': 'error', 'msg': 'غير موجود'}), 404
    valid, state = check_server_valid(server_id)
    if not valid:
        return False, jsonify({'status': 'error', 'msg': state or 'disabled'}), 400
    return True, server, 200


@app.route('/api/cloud/start/<server_id>', methods=['POST'])
def api_cloud_start(server_id):
    allowed, obj, code = _cloudflare_request_allowed(server_id)
    if not allowed:
        return obj, code
    requested_port = None
    try:
        payload = request.get_json(silent=True) or {}
        requested_port = payload.get('port') if isinstance(payload, dict) else None
    except Exception:
        requested_port = None
    if requested_port is None:
        requested_port = request.form.get('port') or request.args.get('port')
    ok, url, error = start_cloudflared(server_id, requested_port)
    if not ok:
        return jsonify({'status': 'error', 'msg': error or 'فشل تشغيل Cloudflare'}), 500
    state = cloudflared_status(server_id)
    return jsonify({
        'status': 'success',
        'running': True,
        'url': url or state.get('url'),
        'target': state.get('target'),
        'port': state.get('port'),
        'ports': state.get('ports', []),
        'msg': 'تم تشغيل Cloudflare. انتظر ثواني لظهور الرابط.' if not (url or state.get('url')) else 'تم التشغيل!'
    })


@app.route('/api/cloud/stop/<server_id>', methods=['POST'])
def api_cloud_stop(server_id):
    allowed, obj, code = _cloudflare_request_allowed(server_id)
    if not allowed:
        return obj, code
    if stop_cloudflared(server_id):
        return jsonify({'status': 'success', 'running': False, 'msg': 'تم إيقاف Cloudflare'})
    return jsonify({'status': 'error', 'msg': 'فشل إيقاف Cloudflare'}), 500


@app.route('/api/cloud/ports/<server_id>', methods=['GET'])
def api_cloud_ports(server_id):
    allowed, obj, code = _cloudflare_request_allowed(server_id)
    if not allowed:
        return obj, code
    return jsonify({'status': 'success', 'ports': detect_project_ports(server_id)})


@app.route('/api/cloud/status/<server_id>', methods=['GET'])
def api_cloud_status(server_id):
    allowed, obj, code = _cloudflare_request_allowed(server_id)
    if not allowed:
        return obj, code
    state = cloudflared_status(server_id)
    state['ports'] = detect_project_ports(server_id)
    return jsonify({'status': 'success', **state})

@app.route('/api/cloud/logs/<server_id>', methods=['GET'])
def api_cloud_logs(server_id):
    allowed, obj, code = _cloudflare_request_allowed(server_id)
    if not allowed:
        return obj, code
    try:
        log_path = _cloud_log_path(server_id)
        log = ''
        if os.path.exists(log_path):
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                log = f.read()[-20000:]
        matches = re.findall(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', log)
        url = matches[-1] if matches else None
        with CLOUDFLARED_LOCK:
            url = _cloud_state(server_id).get('url') or url
        return jsonify({'status': 'success', 'log': log, 'url': url, 'server_id': server_id})
    except Exception as exc:
        return jsonify({'status': 'error', 'msg': str(exc)}), 500



# ============================================
# 🏠 الصفحات الرئيسية
# ============================================
@app.route('/')
def index():
    """الصفحة الرئيسية"""
    return render_template('landing.html')
@app.route('/landing')
def landing():
    """صفحة الهبوط"""
    return render_template('landing.html')
# ============================================
# 🔐 تسجيل الدخول
# ============================================
@app.route('/login', methods=['GET', 'POST'])
def login():
    """تسجيل دخول الأدمن"""
    
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        users = load_users()
        
        # التحقق من بيانات الأدمن
        if username in ('admin', 'Vps') and password == users.get(username, {}).get('password'):
            session['user'] = 'Vps'
            session['role'] = 'admin'
            return redirect(url_for('admin_dashboard'))
        
        return render_template('login.html', error="بيانات الدخول غير صحيحة!")
    
    return render_template('login.html', error=None)
@app.route('/<server_id>/login', methods=['GET', 'POST'])
def server_login(server_id):
    """تسجيل دخول المستخدم للخادم"""
    
    # التحقق من صحة الخادم
    valid, result = check_server_valid(server_id)
    if not valid:
        return render_template('error.html', 
                             error_type=result if result else "deleted", 
                             server_link=server_id)
    
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        users = load_users()
        
        # البحث عن المستخدم
        for uname, data in users.items():
            if uname == 'admin': 
                continue
            servers = data.get('servers', [])
            if not isinstance(servers, list): 
                continue
            
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    # التحقق من اسم المستخدم وكلمة المرور
                    if username == uname and password == data.get('password'):
                        session['user'] = uname
                        session['role'] = 'user'
                        session['current_server_id'] = server_id
                        return redirect(url_for('projects_page'))
                    else:
                        return render_template('login.html', error="بيانات الدخول غير صحيحة!")
        
        return render_template('login.html', error="بيانات الدخول غير صحيحة!")
    
    return render_template('login.html', error=None)
@app.route('/<server_id>/home/api/server')
@app.route('/<server_id>/home')
def server_home(server_id):
    """لوحة تحكم المستخدم"""
    # التحقق من تسجيل الدخول
    if 'user' not in session or session.get('role') != 'user':
        return redirect(url_for('server_login', server_id=server_id))

    current_user = session.get('user', '')
    server, owner = get_server_by_id(server_id)
    if not server or owner != current_user:
        session.clear()
        return redirect(url_for('server_login', server_id=server_id))

    # السماح بالانتقال بين مشاريع نفس الحساب
    session['current_server_id'] = server_id

    # التحقق من صحة الخادم
    valid, result = check_server_valid(server_id)
    if not valid:
        session.clear()
        return render_template('error.html',
                             error_type=result if result else "deleted",
                             server_link=server_id)
    base_url = request.host_url.rstrip('/')
    panel_url = f"{base_url}/{server_id}/home/api/server"
    site_url = f"{panel_url}/"
    return render_template('home.html',
                         username=session['user'],
                         current_server=result,
                         server_panel_url=panel_url,
                         server_api_url=f"{panel_url}/api",
                         server_site_url=site_url,
                         projects_page_url=url_for('projects_page'),
                         cloud_page_url=url_for('cloud_control_page', server_id=server_id))
@app.route('/<server_id>/cloud')
def cloud_control_page(server_id):
    """صفحة تحكم Cloudflare مستقلة للمشروع."""
    if 'user' not in session or session.get('role') != 'user':
        return redirect(url_for('server_login', server_id=server_id))
    current_user = session.get('user', '')
    server, owner = get_server_by_id(server_id)
    if not server or owner != current_user:
        return redirect(url_for('projects_page'))
    valid, result = check_server_valid(server_id)
    if not valid:
        return render_template('error.html', error_type=result if result else 'deleted', server_link=server_id)
    session['current_server_id'] = server_id
    return render_template('cloud.html', username=current_user, server_id=server_id, server=result, home_url=url_for('server_home', server_id=server_id))

@app.route('/projects', methods=['GET', 'POST'])
@app.route('/projects/create', methods=['GET', 'POST'])
@app.route('/my-projects', methods=['GET', 'POST'])
def projects_page():
    """صفحة المشاريع وإنشاء مشروع جديد"""
    if 'user' not in session:
        return redirect(url_for('login'))

    current_role = session.get('role', 'user')
    current_user = session.get('user', '')
    users = load_users()
    enforce_expired_accounts(users)
    message = None
    error = None
    created_server = None
    creation_details = None

    if request.method == 'POST':
        server_type = request.form.get('server_type', 'python').strip().lower() or 'python'
        ram = request.form.get('ram', '512MB').strip() or '512MB'
        disk = request.form.get('disk', '1GB').strip() or '1GB'

        try:
            cpu_limit = int(request.form.get('cpu_limit', 80))
            if cpu_limit != 0:
                cpu_limit = max(10, min(100, cpu_limit))
        except Exception:
            cpu_limit = 80

        target_username = current_user
        target_password = ''

        # الأدمن يستطيع إنشاء مشروع لأي مستخدم
        if current_role == 'admin':
            target_username = request.form.get('username', '').strip() or current_user
            target_password = request.form.get('password', '').strip()

        target_expiry = get_user_effective_expiry(users, target_username)
        if current_role != 'admin' and target_expiry and datetime.now() > target_expiry:
            disable_user_projects(users, target_username, reason='expired')
            save_users(users)
            error = 'انتهت مدة الحساب، تواصل مع الأدمن لتجديد الصلاحية'

        if target_username not in users:
            if current_role == 'admin' and target_password:
                users[target_username] = {'password': target_password, 'role': 'user', 'servers': []}
            else:
                error = 'الحساب غير موجود'
        else:
            if current_role == 'admin' and target_password and not users[target_username].get('password'):
                users[target_username]['password'] = target_password

        if not error:
            user_servers = users.get(target_username, {}).get('servers', [])
            if not isinstance(user_servers, list):
                user_servers = []

            user_limit = get_user_project_limit(users, target_username)
            if len(user_servers) >= user_limit:
                error = f'تم الوصول للحد الأقصى للمشاريع لهذا الحساب ({user_limit})'
            else:
                server_id = str(uuid.uuid4())[:8]
                expiry_date = get_user_effective_expiry(users, target_username)
                if current_role == 'admin':
                    try:
                        admin_days = max(1, int(request.form.get('expiry_days', 30)))
                    except Exception:
                        admin_days = 30
                    expiry_date = datetime.now() + timedelta(days=admin_days)
                if expiry_date is None:
                    expiry_date = datetime.now() + timedelta(days=30)
                sync_user_expiry(users, target_username, expiry_date)

                # إنشاء ملفات المشروع الافتراضية
                server_dir = get_server_dir(server_id)
                create_default_files(server_dir, server_type)

                base_url = request.host_url.rstrip('/')
                panel_url = f"{base_url}/{server_id}/home/api/server"

                new_server = {
                    'server_id': server_id,
                    'link': server_id,
                    'login_url': f"/{server_id}/login",
                    'dashboard_url': panel_url,
                    'full_link': panel_url,
                    'type': server_type,
                    'ram': ram,
                    'disk': disk,
                    'status': 'stopped',
                    'pid': None,
                    'created': str(datetime.now()),
                    'expiry': str(expiry_date),
                    'main_file': 'main.py',
                    'requirements_file': 'requirements.txt',
                    'cpu_limit': cpu_limit,
                    'rate_limit_exceeded': False,
                    'stopped_by_user': False
                }

                if target_username not in users:
                    users[target_username] = {'password': target_password or '1234', 'role': 'user', 'servers': []}

                users[target_username].setdefault('servers', [])
                users[target_username]['servers'].append(new_server)
                save_users(users)

                created_server = new_server
                creation_details = build_hosting_details(
                    username=target_username,
                    password=users[target_username].get('password', target_password or ''),
                    cpu_limit=cpu_limit,
                    ram=ram,
                    disk=disk,
                    expiry_dt=expiry_date,
                    server_id=server_id,
                    panel_url=panel_url,
                    project_name='ªGE Hosting'
                )
                message = 'تم إنشاء المشروع بنجاح'

    # إعادة تحميل البيانات بعد أي عملية
    users = load_users()
    current_user_info = users.get(current_user, {}) if isinstance(users, dict) else {}
    user_servers = current_user_info.get('servers', [])
    if not isinstance(user_servers, list):
        user_servers = []

    if current_user != 'admin':
        ensure_user_expiry(users, current_user, default_days=30)
        save_users(users)
        current_user_info = users.get(current_user, {}) if isinstance(users, dict) else {}
        user_servers = current_user_info.get('servers', [])
        if not isinstance(user_servers, list):
            user_servers = []

    projects = []
    now = datetime.now()
    account_expiry_dt = get_user_effective_expiry(users, current_user)
    account_expiry_text = format_expiry_datetime(account_expiry_dt)
    account_days_left = None
    if account_expiry_dt:
        try:
            account_days_left = max(0, (account_expiry_dt.date() - now.date()).days)
        except Exception:
            account_days_left = None

    for srv in user_servers:
        if not isinstance(srv, dict):
            continue
        expiry_dt = parse_expiry_datetime(srv.get('expiry', ''))
        if account_expiry_dt and (not expiry_dt or account_expiry_dt < expiry_dt):
            expiry_dt = account_expiry_dt
        expired = False
        if expiry_dt:
            expired = expiry_dt < now
        projects.append({
            'server_id': srv.get('server_id', ''),
            'type': srv.get('type', 'python'),
            'status': (srv.get('status') or 'stopped').upper(),
            'ram': srv.get('ram', '512MB'),
            'disk': srv.get('disk', '1GB'),
            'cpu_limit': srv.get('cpu_limit', 80),
            'expiry': format_expiry_datetime(expiry_dt or srv.get('expiry', '')),
            'expired': expired,
            'site_url': f"{request.host_url.rstrip('/')}/{srv.get('server_id', '')}/home/api/server/",
            'dashboard_url': f"{request.host_url.rstrip('/')}/{srv.get('server_id', '')}/home/api/server"
        })

    project_limit = get_user_project_limit(users, current_user)

    return render_template(
        'projects.html',
        username=current_user,
        role=current_role,
        projects=projects,
        project_limit=project_limit,
        used_projects=len(user_servers),
        message=message,
        error=error,
        created_server=created_server,
        creation_details=creation_details,
        account_expiry=account_expiry_text,
        account_days_left=account_days_left,
    )

@app.route('/<server_id>/home/api/server/api', methods=['GET'])
@app.route('/<server_id>/home/api', methods=['GET'])
def server_home_api(server_id):
    """واجهة API للخادم تعرض الروابط الحالية على نفس المنفذ"""
    valid, result = check_server_valid(server_id)
    if not valid:
        return jsonify({
            'ok': False,
            'error': result if result else 'deleted',
            'server_id': server_id
        }), 404
    server, owner = get_server_by_id(server_id)
    base_url = request.host_url.rstrip('/')
    panel_url = f"{base_url}/{server_id}/home/api/server"
    payload = {
        'ok': True,
        'server_id': server_id,
        'owner': owner,
        'base_url': base_url,
        'login_url': f"{base_url}/{server_id}/login",
        'dashboard_url': panel_url,
        'api_url': f"{panel_url}/api",
        'panel_url': panel_url,
        'status': server.get('status') if isinstance(server, dict) else None,
        'type': server.get('type') if isinstance(server, dict) else None,
        'ram': server.get('ram') if isinstance(server, dict) else None,
        'disk': server.get('disk') if isinstance(server, dict) else None,
        'main_file': server.get('main_file') if isinstance(server, dict) else None,
        'requirements_file': server.get('requirements_file') if isinstance(server, dict) else None,
        'site_url': f"{panel_url}/"
    }
    return jsonify(payload)
@app.route('/<server_id>/home/api/server', defaults={'subpath': ''}, methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
@app.route('/<server_id>/home/api/server/', defaults={'subpath': ''}, methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
@app.route('/<server_id>/home/api/server/<path:subpath>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
def serve_site(server_id, subpath):
    """عرض الموقع المرفوع داخل مجلد الخادم"""
    valid, result = check_server_valid(server_id)
    if not valid:
        return render_template('error.html',
                               error_type=result if result else 'deleted',
                               server_link=server_id), 404
    server_dir = get_server_dir(server_id)
    static_resp = serve_static_site_file(server_dir, subpath)
    if static_resp is not None and request.method in ('GET', 'HEAD'):
        return static_resp
    flask_app = load_flask_site_app(server_id)
    if flask_app:
        return proxy_request_to_flask_app(flask_app, subpath)
    if request.method != 'GET':
        return jsonify({'ok': False, 'message': 'Only GET is supported for static folders without Flask app'}), 405
    return render_site_browser(server_id, server_dir, subpath)

@app.route('/<server_id>/editor')
def file_editor(server_id):
    """صفحة إنشاء/تعديل ملف مستقلة مع تلوين كود"""
    if 'user' not in session or session.get('role') != 'user':
        return redirect(url_for('server_login', server_id=server_id))
    if session.get('current_server_id') != server_id:
        session.clear()
        return redirect(url_for('server_login', server_id=server_id))
    valid, result = check_server_valid(server_id)
    if not valid:
        return render_template('error.html', error_type=result if result else 'deleted', server_link=server_id)

    filename = (request.args.get('file') or '').strip().lstrip('/\\')
    folder = (request.args.get('folder') or '').strip().strip('/\\')
    mode = (request.args.get('mode') or '').strip().lower() or 'python'
    base_url = request.host_url.rstrip('/')
    panel_url = f"{base_url}/{server_id}/home/api/server"

    file_content = ''
    if filename:
        _, filepath = resolve_server_path(server_id, filename)
        if filepath and os.path.isfile(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    file_content = f.read()
            except Exception:
                file_content = ''

    if not filename:
        defaults = {
            'python': "# New Python file\nfrom flask import Flask\n\napp = Flask(__name__)\n\n@app.route('/')\ndef home():\n    return 'Hello from ªGE Team'\n\nif __name__ == '__main__':\n    app.run(host='0.0.0.0', port=5000, debug=True)",
            'html': "<!DOCTYPE html>\n<html lang='en'>\n<head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>New Page</title></head>\n<body>\n  <h1>Hello from ªGE Team</h1>\n</body>\n</html>",
            'javascript': "console.log('Hello from ªGE Team');",
            'css': "body {\n  font-family: Arial, sans-serif;\n}\n",
            'php': "<?php\necho 'Hello from ªGE Team';\n?>",
            'json': "{\n  \"name\": \"new-project\"\n}",
            'yaml': "name: new-project\n",
            'xml': "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<root></root>",
            'sql': "SELECT 1;",
            'markdown': "# New file\n",
            'text': ''
        }
        file_content = defaults.get(mode, defaults['python'])

    return render_template('editor.html',
                           server_id=server_id,
                           filename=filename,
                           folder=folder,
                           mode=mode,
                           initial_content=file_content,
                           panel_url=panel_url)
@app.route('/logout')
def logout():
    """تسجيل الخروج"""
    server_id = session.get('current_server_id')
    session.clear()
    if server_id:
        return redirect(url_for('server_login', server_id=server_id))
    return redirect(url_for('login'))
# ============================================
# 👑 لوحة تحكم الأدمن
# ============================================
@app.route('/admin')
def admin_dashboard():
    """لوحة تحكم الأدمن"""

    # التحقق من صلاحيات الأدمن
    if 'user' not in session or session.get('role') != 'admin':
        return redirect(url_for('login'))

    users = load_users()
    user_list = []
    total_servers = 0
    total_running = 0
    max_servers_per_user = load_settings().get('max_servers_per_user', 3)

    # تجميع بيانات المستخدمين
    for uname, data in users.items():
        if uname == 'admin':
            continue

        servers = data.get('servers', [])
        if not isinstance(servers, list):
            servers = []

        running = sum(1 for s in servers if isinstance(s, dict) and s.get('status') == 'running')
        total_servers += len(servers)
        total_running += running

        account_expiry_dt = get_user_effective_expiry(users, uname)
        user_list.append({
            'username': uname,
            'password': data.get('password', ''),
            'servers': servers,
            'server_count': len(servers),
            'running_count': running,
            'max_servers': get_user_project_limit(users, uname),
            'account_expiry': format_expiry_datetime(account_expiry_dt),
            'account_expired': bool(account_expiry_dt and datetime.now() > account_expiry_dt)
        })

    return render_template('admin.html',
                         users=user_list,
                         total_servers=total_servers,
                         total_running=total_running,
                         max_servers_per_user=max_servers_per_user)
@app.route('/admin/create_server', methods=['POST'])
def create_server():
    """إنشاء خادم جديد (للأدمن فقط)"""
    # التحقق من صلاحيات الأدمن
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'غير مصرح!'}), 403
    # جلب البيانات
    data = request.get_json()
    username = data.get('username', '')
    password = data.get('password', '')
    server_type = data.get('server_type', 'python')
    ram = data.get('ram', '512MB')
    disk = data.get('disk', '1GB')
    try:
        expiry_days = int(data.get('expiry_days', 30))
    except Exception:
        expiry_days = 30
    expiry_days = max(1, min(expiry_days, 3650))
    try:
        cpu_limit = int(data.get('cpu_limit', 80))
    except Exception:
        cpu_limit = 80
    # التحقق من البيانات
    if not username or not password:
        return jsonify({'error': 'مطلوب!'}), 400
    users = load_users()
    user_limit = get_user_project_limit(users, username)
    existing_servers = users.get(username, {}).get('servers', [])
    if not isinstance(existing_servers, list):
        existing_servers = []
    if username in users and len(existing_servers) >= user_limit:
        return jsonify({
            'error': f'تم الوصول للحد الأقصى للمشاريع لهذا الحساب ({user_limit})'
        }), 400
    server_id = str(uuid.uuid4())[:8]
    expiry_date = datetime.now() + timedelta(days=expiry_days)
    # إنشاء الملفات الافتراضية
    create_default_files(get_server_dir(server_id), server_type)
    # إنشاء بيانات الخادم
    base_url = request.host_url.rstrip('/')
    panel_url = f"{base_url}/{server_id}/home/api/server"
    new_server = {
        'server_id': server_id,
        'link': server_id,
        'login_url': f"/{server_id}/login",
        'dashboard_url': panel_url,
        'full_link': panel_url,
        'type': server_type,
        'ram': ram,
        'disk': disk,
        'status': 'stopped',
        'pid': None,
        'created': str(datetime.now()),
        'expiry': str(expiry_date),
        'main_file': 'main.py',
        'requirements_file': 'requirements.txt',
        'cpu_limit': cpu_limit,
        'rate_limit_exceeded': False,
        'stopped_by_user': False
    }
    # إضافة المستخدم إذا لم يكن موجوداً
    if username not in users:
        users[username] = {'password': password, 'role': 'user', 'servers': []}

    users[username].setdefault('servers', [])

    # توحيد مدة الحساب على نفس المدة التي يحددها الأدمن
    expiry_dt = datetime.now() + timedelta(days=expiry_days)
    sync_user_expiry(users, username, expiry_dt)

    users[username]['servers'].append(new_server)
    save_users(users)

    hosting_details = build_hosting_details(
        username=username,
        password=password,
        cpu_limit=cpu_limit,
        ram=ram,
        disk=disk,
        expiry_dt=expiry_dt,
        server_id=server_id,
        panel_url=panel_url,
        project_name='ªGE Hosting'
    )

    return jsonify({
        'success': True,
        'hosting_details': hosting_details,
        'details_text': hosting_details,
        'username': username,
        'password': password,
        'login_url': new_server['login_url'],
        'dashboard_url': new_server['dashboard_url'],
        'api_url': f"{request.host_url.rstrip('/')}/{server_id}/home/api/server/api",
        'site_url': f"{request.host_url.rstrip('/')}/{server_id}/home/api/server/",
        'hostname': new_server['full_link'],
        'full_url': new_server['full_link'],
        'server_id': server_id,
        'projects_count': len(users[username]['servers']),
        'projects_limit': user_limit,
        'account_expiry': format_expiry_datetime(expiry_dt)
    })
@app.route('/admin/set_rate_limit/<server_id>', methods=['POST'])
def set_rate_limit(server_id):
    """تعيين حد المعالج لخادم (للأدمن فقط)"""
    
    # التحقق من صلاحيات الأدمن
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'غير مصرح!'}), 403
    
    try:
        cpu_limit = int(request.get_json().get('cpu_limit', 80))
    except Exception:
        return jsonify({'error': 'قيمة CPU غير صالحة'}), 400
    if cpu_limit != 0 and (cpu_limit < 10 or cpu_limit > 100):
        return jsonify({'error': 'CPU يجب أن يكون 10–100 أو 0 لغير محدود'}), 400
    users = load_users()
    
    # البحث عن الخادم وتحديث الحد
    for uname, udata in users.items():
        if uname == 'admin': 
            continue
        servers = udata.get('servers', [])
        if not isinstance(servers, list): 
            continue
        
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                s['cpu_limit'] = cpu_limit
                save_users(users)
                return jsonify({'success': True, 'cpu_limit': cpu_limit})
    
    return jsonify({'error': 'غير موجود'}), 404
@app.route('/admin/delete_server/<username>/<server_id>', methods=['POST'])
def delete_server(username, server_id):
    """تعطيل خادم بدون حذف الملفات (للأدمن فقط)"""

    # التحقق من صلاحيات الأدمن
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'غير مصرح!'}), 403

    users = load_users()

    if username in users:
        servers = users[username].get('servers', [])
        if not isinstance(servers, list):
            servers = []

        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                if s.get('pid'):
                    try:
                        stop_bot_process(s['pid'])
                    except Exception:
                        pass
                s['status'] = 'disabled'
                s['disabled'] = True
                s['disabled_reason'] = 'admin'
                s['pid'] = None
                s['stopped_by_user'] = True
                s['disabled_at'] = str(datetime.now())
                break

        users[username]['servers'] = servers
        save_users(users)

    return jsonify({'success': True, 'status': 'disabled'})



@app.route('/admin/set_user_limit/<username>', methods=['POST'])
def set_user_limit(username):
    """تحديد الحد الأقصى لمشاريع مستخدم معين (للأدمن فقط)"""
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'غير مصرح!'}), 403

    data = request.get_json(silent=True) or {}
    try:
        max_servers = int(data.get('max_servers', 3))
    except Exception:
        max_servers = 3
    max_servers = max(1, min(max_servers, 100))

    users = load_users()
    if username not in users:
        return jsonify({'error': 'المستخدم غير موجود'}), 404

    users[username]['max_servers'] = max_servers
    save_users(users)
    return jsonify({'success': True, 'username': username, 'max_servers': max_servers})


@app.route('/admin/disable_server/<username>/<server_id>', methods=['POST'])
def disable_server(username, server_id):
    """تعطيل مشروع بدون حذفه (للأدمن فقط)"""
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'غير مصرح!'}), 403

    users = load_users()
    if username not in users:
        return jsonify({'error': 'المستخدم غير موجود'}), 404

    servers = users[username].get('servers', [])
    if not isinstance(servers, list):
        servers = []

    for s in servers:
        if isinstance(s, dict) and s.get('server_id') == server_id:
            if s.get('pid'):
                try:
                    stop_bot_process(s['pid'])
                except Exception:
                    pass
            s['status'] = 'disabled'
            s['disabled'] = True
            s['disabled_reason'] = 'admin'
            s['pid'] = None
            s['stopped_by_user'] = True
            s['disabled_at'] = str(datetime.now())
            save_users(users)
            return jsonify({'success': True, 'status': 'disabled'})

    return jsonify({'error': 'غير موجود'}), 404


@app.route('/admin/enable_server/<username>/<server_id>', methods=['POST'])
def enable_server(username, server_id):
    """إعادة تفعيل مشروع معطّل (للأدمن فقط)"""
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'غير مصرح!'}), 403

    users = load_users()
    if username not in users:
        return jsonify({'error': 'المستخدم غير موجود'}), 404

    servers = users[username].get('servers', [])
    if not isinstance(servers, list):
        servers = []

    for s in servers:
        if isinstance(s, dict) and s.get('server_id') == server_id:
            s['status'] = 'stopped'
            s['disabled'] = False
            s['disabled_reason'] = ''
            s['stopped_by_user'] = False
            s['pid'] = None
            s.pop('disabled_at', None)
            save_users(users)
            return jsonify({'success': True, 'status': 'stopped'})

    return jsonify({'error': 'غير موجود'}), 404


@app.route('/admin/renew_server/<username>/<server_id>', methods=['POST'])
def renew_server(username, server_id):
    """تجديد صلاحية الحساب كاملًا بالأيام (للأدمن فقط)"""
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'غير مصرح!'}), 403

    data = request.get_json(silent=True) or {}
    try:
        days = int(data.get('days', 30))
    except Exception:
        days = 30
    days = max(1, min(days, 3650))

    users = load_users()
    if username not in users:
        return jsonify({'error': 'المستخدم غير موجود'}), 404

    expiry = datetime.now() + timedelta(days=days)
    sync_user_expiry(users, username, expiry)
    restore_expired_user_projects(users, username)
    save_users(users)
    return jsonify({
        'success': True,
        'expiry': format_expiry_datetime(expiry),
        'username': username,
        'server_id': server_id
    })


# ============================================
# 🤖 API البوتات
# ============================================
def _bot_request_allowed(server_id):
    """التحقق من تسجيل الدخول وملكية المشروع قبل أي عملية على ملفات/عملية البوت."""
    if 'user' not in session:
        return False, jsonify({'status': 'error', 'msg': 'غير مصرح!'}), 401
    server, owner = get_server_by_id(server_id)
    if not server:
        return False, jsonify({'status': 'error', 'msg': 'غير موجود'}), 404
    role = session.get('role')
    if role != 'admin' and owner != session.get('user'):
        return False, jsonify({'status': 'error', 'msg': 'غير مصرح!'}), 403
    valid, state = check_server_valid(server_id)
    if not valid:
        return False, jsonify({'status': 'error', 'msg': state or 'disabled'}), 400
    return True, server, 200

@app.route('/api/run/<server_id>', methods=['POST'])
def api_run(server_id):
    allowed, obj, code = _bot_request_allowed(server_id)
    if not allowed:
        return obj, code

    """تشغيل البوت"""

    server, _ = get_server_by_id(server_id)
    valid, state = check_server_valid(server_id)
    if not server:
        return jsonify({'status': 'error', 'msg': 'غير موجود'})
    if not valid:
        return jsonify({'status': 'error', 'msg': state or 'disabled'})

    if server.get('status') == 'running':
        return jsonify({'status': 'error', 'msg': 'يعمل بالفعل!'})

    # إعادة تعيين حالة التجاوز
    server['rate_limit_exceeded'] = False
    server['stopped_by_user'] = False

    # تشغيل البوت
    pid, error = run_bot(server_id, 
                         server.get('main_file', 'main.py'), 
                         server.get('requirements_file', 'requirements.txt'))

    if pid:
        # تحديث حالة الخادم
        users = load_users()
        for uname, data in users.items():
            if uname == 'admin': continue
            servers = data.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    s['status'] = 'running'
                    s['pid'] = pid
                    s['started_at'] = str(datetime.now())
                    save_users(users)
                    break

        # بدء مراقبة البوت
        _start_monitor_once(server_id, pid)
        return jsonify({'status': 'success', 'msg': 'تم التشغيل!'})

    return jsonify({'status': 'error', 'msg': error or 'فشل التشغيل'})

@app.route('/api/restart/<server_id>', methods=['POST'])
def api_restart(server_id):
    allowed, obj, code = _bot_request_allowed(server_id)
    if not allowed:
        return obj, code

    """إعادة تشغيل ذرية وآمنة دون سباق بين stop و run."""
    server, _ = get_server_by_id(server_id)
    valid, state = check_server_valid(server_id)
    if not server:
        return jsonify({'status': 'error', 'msg': 'غير موجود'}), 404
    if not valid:
        return jsonify({'status': 'error', 'msg': state or 'disabled'}), 400

    ok, result = restart_server_now(server_id, reason='manual_restart')
    if ok:
        return jsonify({'status': 'success', 'msg': 'تمت إعادة التشغيل!', 'pid': result})
    if result == 'restart_in_progress':
        return jsonify({'status': 'error', 'msg': 'إعادة التشغيل قيد التنفيذ، انتظر لحظة.'}), 409
    if result == 'disabled':
        return jsonify({'status': 'error', 'msg': 'الخادم متوقف أو معطل.'}), 400
    return jsonify({'status': 'error', 'msg': result or 'فشلت إعادة التشغيل'}), 500

@app.route('/api/stop/<server_id>', methods=['POST'])
def api_stop(server_id):
    allowed, obj, code = _bot_request_allowed(server_id)
    if not allowed:
        return obj, code

    """إيقاف البوت"""

    server, _ = get_server_by_id(server_id)
    valid, state = check_server_valid(server_id)
    if not server:
        return jsonify({'status': 'error', 'msg': 'غير موجود'})
    if not valid:
        return jsonify({'status': 'error', 'msg': state or 'disabled'})

    # إيقاف العملية
    if server.get('pid'):
        stop_bot_process(server['pid'])

    # تحديث حالة الخادم
    users = load_users()
    for uname, data in users.items():
        if uname == 'admin': continue
        servers = data.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                s['status'] = 'stopped'
                s['pid'] = None
                s['stopped_by_user'] = True
                save_users(users)
                break

    # تسجيل الإيقاف
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"\n[{datetime.now().strftime('%I:%M:%S %p')}] تم إيقاف الخادم بواسطة المستخدم\n")
    except:
        pass

    return jsonify({'status': 'success', 'msg': 'تم الإيقاف'})

@app.route('/api/logs/<server_id>')
def api_logs(server_id):
    """جلب آخر سجل البوت مع التحقق من ملكية المشروع."""
    allowed, obj, code = _bot_request_allowed(server_id)
    if not allowed:
        return obj, code
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        if not os.path.isfile(log_file):
            return jsonify({'logs': ''})
        # قراءة آخر 256KB فقط لمنع تحميل سجل ضخم إلى المتصفح.
        with open(log_file, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024), os.SEEK_SET)
            raw = f.read()
        logs = raw.decode('utf-8', errors='replace')
        return jsonify({'logs': logs})
    except Exception:
        return jsonify({'status': 'error', 'logs': ''}), 500

@app.route('/api/clear_logs/<server_id>', methods=['POST'])
def api_clear_logs(server_id):
    """مسح سجل البوت فقط؛ لا يلمس سجل Cloudflare."""
    allowed, obj, code = _bot_request_allowed(server_id)
    if not allowed:
        return obj, code
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(f"[LOG RESET] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        return jsonify({'status': 'success', 'msg': 'تم المسح'})
    except Exception as exc:
        return jsonify({'status': 'error', 'msg': str(exc)}), 500
@app.route('/api/command', methods=['POST'])
def api_command():
    """تنفيذ أوامر النظام فقط للمستخدم المالك/الأدمن، وعلى مجلد المشروع."""
    data = request.get_json(silent=True) or {}
    server_id = str(data.get('server_id') or '').strip()
    cmd = str(data.get('cmd') or '').strip()
    if not server_id or not cmd:
        return jsonify({'status': 'error', 'msg': 'server_id و cmd مطلوبان'}), 400
    allowed, obj, code = _bot_request_allowed(server_id)
    if not allowed:
        return obj, code
    if len(cmd) > 4000:
        return jsonify({'status': 'error', 'msg': 'الأمر طويل جدًا'}), 400
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=get_server_dir(server_id), timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )
        output = (result.stdout + result.stderr)[:4000]
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] $ {cmd}\n{output}\n")
        return jsonify({'status': 'success' if result.returncode == 0 else 'error', 'output': output, 'returncode': result.returncode})
    except subprocess.TimeoutExpired:
        return jsonify({'status': 'error', 'msg': 'انتهت المهلة'}), 408
    except Exception as exc:
        return jsonify({'status': 'error', 'msg': str(exc)}), 500

@app.route('/api/stats/<server_id>')
def api_stats(server_id):
    """جلب إحصائيات الأداء"""
    allowed, obj, code = _bot_request_allowed(server_id)
    if not allowed:
        return obj, code
    server, _ = get_server_by_id(server_id)
    if not server:
        return jsonify({
            'cpu': '0%', 
            'ram': '0 MB', 
            'uptime': '0h', 
            'status': 'unknown', 
            'cpu_limit': 80, 
            'net_in': '0 KB', 
            'net_out': '0 KB'
        })
    
    uptime, cpu, ram, net_in, net_out = "0h 0m", "0%", "0 MB", "0 KB", "0 KB"
    
    # جلب إحصائيات العملية إذا كانت تعمل
    if server.get('status') == 'running' and server.get('pid'):
        stats = get_process_stats(server['pid'])
        cpu = f"{stats['cpu_percent']}%"
        ram = stats['ram_display']
        net_in, net_out = get_network_stats(server['pid'])
    
    # حساب وقت التشغيل
    if server.get('status') == 'running' and server.get('started_at'):
        try:
            start = datetime.strptime(server['started_at'], '%Y-%m-%d %H:%M:%S.%f')
            diff = datetime.now() - start
            if diff.days > 0: 
                uptime = f"{diff.days}يوم {diff.seconds//3600}س"
            else:
                h, m, s = diff.seconds // 3600, (diff.seconds % 3600) // 60, diff.seconds % 60
                uptime = f"{h}س {m}د {s}ث"
        except: 
            pass
    
    return jsonify({
        'cpu': cpu, 
        'ram': ram, 
        'uptime': uptime, 
        'net_in': net_in, 
        'net_out': net_out, 
        'cpu_limit': server.get('cpu_limit', 80), 
        'status': server.get('status', 'stopped')
    })
# ============================================
# 🔑 تغيير كلمة المرور
# ============================================
@app.route('/api/change_password/<server_id>', methods=['POST'])
def api_change_password(server_id):
    """تغيير كلمة المرور"""
    
    # التحقق من تسجيل الدخول
    if 'user' not in session: 
        return jsonify({'error': 'الرجاء تسجيل الدخول أولاً!'}), 403
    
    data = request.get_json()
    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')
    
    # التحقق من البيانات
    if not current_password or not new_password: 
        return jsonify({'error': 'جميع الحقول مطلوبة!'})
    if len(new_password) < 4: 
        return jsonify({'error': 'كلمة المرور يجب أن تكون 4 أحرف على الأقل!'})
    
    users = load_users()
    username = session.get('user')
    
    # تغيير كلمة المرور
    if username in users:
        if users[username].get('password') == current_password:
            users[username]['password'] = new_password
            save_users(users)
            return jsonify({'success': True, 'msg': 'تم تغيير كلمة المرور!'})
        return jsonify({'error': 'كلمة المرور الحالية غير صحيحة!'})
    
    return jsonify({'error': 'المستخدم غير موجود!'}), 404
# ============================================
# 🔥 GitHub API - سحب الكود بدون Git
# ============================================
@app.route('/api/github/deploy/<server_id>', methods=['POST'])
def api_github_deploy(server_id):
    """سحب كود من GitHub باستخدام Python Requests"""
    
    data = request.get_json()
    repo_url = data.get('repo_url', '').strip()
    access_token = data.get('access_token', '').strip()
    is_private = data.get('is_private', False)
    
    if not repo_url:
        return jsonify({'status': 'error', 'msg': 'رابط المستودع مطلوب!'}), 400
    
    server_dir = get_server_dir(server_id)
    log_file = os.path.join(server_dir, 'github_deploy.log')
    
    # مسح سجل النشر القديم
    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] بدء النشر من GitHub...\n")
            f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] المستودع: {repo_url}\n")
            f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] النوع: {'خاص' if is_private else 'عام'}\n")
            f.write("─" * 40 + "\n")
    except:
        pass
    
    def deploy_thread():
        """دالة النشر في خيط منفصل"""
        try:
            import requests
            import shutil
            
            def deploy_log(msg):
                try:
                    with open(log_file, 'a', encoding='utf-8') as f:
                        f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] {msg}\n")
                        f.flush()
                except:
                    pass
            
            deploy_log("جاري تجهيز النشر...")
            
            # تنظيف الرابط
            clean_url = repo_url.replace('.git', '').rstrip('/')
            
            # استخراج اسم المستخدم والمستودع
            if 'github.com' not in clean_url:
                deploy_log("❌ خطأ: يدعم فقط روابط GitHub!")
                return
            
            parts = clean_url.split('github.com/')[-1].split('/')
            
            if len(parts) < 2:
                deploy_log("❌ خطأ: رابط GitHub غير صالح!")
                return
            
            owner = parts[0]
            repo = parts[1]
            branch = 'main'  # الفرع الافتراضي
            
            # التحقق من تحديد الفرع
            if len(parts) > 3 and parts[2] == 'tree':
                branch = parts[3]
            
            deploy_log(f"المالك: {owner}")
            deploy_log(f"المستودع: {repo}")
            deploy_log(f"الفرع: {branch}")
            deploy_log(f"جاري تحميل ملف ZIP...")
            
            # بناء رابط API
            api_url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{branch}"
            
            headers = {'Accept': 'application/vnd.github.v3+json'}
            if is_private and access_token:
                headers['Authorization'] = f'token {access_token}'
                deploy_log("استخدام رمز الوصول للمصادقة")
            
            # تحميل الملف
            response = requests.get(api_url, headers=headers, stream=True, timeout=60)
            
            if response.status_code == 200:
                deploy_log("✓ تم تحميل المستودع بنجاح!")
                deploy_log("جاري استخراج الملفات...")
                
                # حفظ الملف المؤقت
                temp_zip = os.path.join(server_dir, '_github_temp.zip')
                with open(temp_zip, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                # استخراج الملفات
                try:
                    with zipfile.ZipFile(temp_zip, 'r') as zf:
                        # GitHub يضع الملفات في مجلد فرعي
                        root_folder = zf.namelist()[0].split('/')[0]
                        
                        for member in zf.namelist():
                            # تخطي المجلد الجذر
                            relative_path = '/'.join(member.split('/')[1:])
                            if not relative_path:
                                continue
                            
                            target_path = os.path.join(server_dir, relative_path)
                            
                            if member.endswith('/'):
                                # مجلد
                                os.makedirs(target_path, exist_ok=True)
                            else:
                                # ملف
                                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                                with zf.open(member) as source, open(target_path, 'wb') as target:
                                    shutil.copyfileobj(source, target)
                                deploy_log(f"  ✓ {relative_path}")
                    
                    deploy_log("")
                    deploy_log("✅ تم النشر بنجاح!")
                    deploy_log(f"تم استخراج الملفات إلى: {server_dir}")
                    
                except Exception as e:
                    deploy_log(f"❌ خطأ في الاستخراج: {str(e)}")
                finally:
                    # حذف الملف المؤقت
                    try:
                        os.remove(temp_zip)
                    except:
                        pass
                    
            elif response.status_code == 404:
                deploy_log("❌ خطأ: المستودع غير موجود!")
                deploy_log("تأكد من صحة الرابط")
            elif response.status_code == 401:
                deploy_log("❌ خطأ: فشل المصادقة!")
                deploy_log("تحقق من رمز الوصول (للمستودعات الخاصة)")
            elif response.status_code == 403:
                deploy_log("❌ خطأ: تم تجاوز حد الطلبات أو تم رفض الوصول!")
                deploy_log("حاول مرة أخرى لاحقاً أو استخدم رمز وصول")
            else:
                deploy_log(f"❌ خطأ: HTTP {response.status_code}")
                deploy_log(f"الرد: {response.text[:200]}")
            
        except requests.exceptions.Timeout:
            try:
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] ❌ خطأ: انتهت المهلة! تحقق من اتصالك بالإنترنت.\n")
            except:
                pass
        except Exception as e:
            try:
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] ❌ خطأ: {str(e)}\n")
            except:
                pass
    
    # بدء النشر في خيط منفصل
    threading.Thread(target=deploy_thread, daemon=True).start()
    return jsonify({'status': 'success', 'msg': 'بدأ النشر! تحقق من الطرفية لمتابعة التقدم.'})
@app.route('/api/github/logs/<server_id>')
def api_github_logs(server_id):
    """جلب سجلات نشر GitHub"""
    
    log_file = os.path.join(get_server_dir(server_id), 'github_deploy.log')
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                logs = f.read()
        except:
            logs = "> جاهز للنشر..."
    else:
        logs = "> جاهز للنشر..."
    
    return jsonify({'logs': logs})
@app.route('/api/github/clear_logs/<server_id>', methods=['POST'])
def api_github_clear_logs(server_id):
    """مسح سجلات نشر GitHub"""
    
    log_file = os.path.join(get_server_dir(server_id), 'github_deploy.log')
    try:
        if os.path.exists(log_file):
            os.remove(log_file)
        return jsonify({'status': 'success'})
    except:
        return jsonify({'status': 'error'}), 500
# ============================================
# 📁 API الملفات
# ============================================
@app.route('/api/files/<server_id>')
def api_files(server_id):
    """عرض الملفات والمجلدات"""
    
    folder = request.args.get('folder', '')
    server_dir = get_server_dir(server_id)
    
    if folder:
        server_dir = os.path.join(server_dir, folder)
        # منع الوصول إلى خارج مجلد الخادم
        if not os.path.abspath(server_dir).startswith(os.path.abspath(get_server_dir(server_id))):
            return jsonify({'files': []})
    
    if not os.path.exists(server_dir): 
        return jsonify({'files': []})
    
    files = []
    try:
        for item in os.listdir(server_dir):
            item_path = os.path.join(server_dir, item)
            files.append({
                'name': item, 
                'is_dir': os.path.isdir(item_path), 
                'size': os.path.getsize(item_path) if os.path.isfile(item_path) else 0, 
                'modified': datetime.fromtimestamp(os.path.getmtime(item_path)).strftime('%Y-%m-%d %H:%M')
            })
    except: 
        pass
    
    return jsonify({'files': files})
@app.route('/api/file/<server_id>', methods=['GET'])
def api_get_file(server_id):
    """قراءة محتوى ملف"""
    
    filename = request.args.get('filename', '')
    filepath = os.path.join(get_server_dir(server_id), filename)
    
    if os.path.exists(filepath) and os.path.isfile(filepath):
        with open(filepath, 'r', encoding='utf-8') as f: 
            return jsonify({'content': f.read()})
    
    return jsonify({'error': 'غير موجود'}), 404
@app.route('/api/file/<server_id>', methods=['POST'])
def api_save_file(server_id):
    """حفظ محتوى ملف مع مزامنة متطلبات ملف التشغيل"""
    data = request.get_json(silent=True) or {}
    filename = str(data.get('filename', '') or '').strip()
    content = data.get('content', '')
    if not filename:
        return jsonify({'error': 'اسم الملف مطلوب'}), 400
    root_dir, filepath = resolve_server_path(server_id, filename)
    if not filepath:
        return jsonify({'error': 'مسار غير آمن'}), 400
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    # إذا تم حفظ ملف التشغيل الرئيسي، حدّث requirements.txt تلقائيًا
    server, _ = get_server_by_id(server_id)
    main_file = (server.get('main_file', 'main.py') if server else 'main.py').strip()
    req_file = (server.get('requirements_file', 'requirements.txt') if server else 'requirements.txt').strip() or 'requirements.txt'
    if os.path.basename(filepath) == os.path.basename(main_file) and filepath.endswith('.py'):
        sync_startup_requirements(server_id, main_file=main_file, requirements_file=req_file, auto_install=True)
    return jsonify({'success': True, 'filename': filename})
@app.route('/api/file/<server_id>', methods=['DELETE'])
def api_delete_file(server_id):
    """حذف ملف أو مجلد بأمان"""
    data = request.get_json(silent=True) or {}
    server_dir = os.path.abspath(get_server_dir(server_id))
    # يدعم حذف ملف واحد أو عدة ملفات
    targets = []
    if isinstance(data.get('filenames'), list):
        targets = data.get('filenames') or []
    elif data.get('filename'):
        targets = [data.get('filename')]
    if not targets:
        return jsonify({'error': 'لا يوجد ملف محدد للحذف'}), 400
    deleted = 0
    skipped = []
    for rel in targets:
        try:
            rel = str(rel).strip().lstrip('/\\')
            if not rel:
                skipped.append(rel)
                continue
            filepath = os.path.abspath(os.path.join(server_dir, rel))
            # منع حذف مجلد الخادم نفسه أو الخروج من المسار
            if filepath == server_dir or not filepath.startswith(server_dir + os.sep):
                skipped.append(rel)
                continue
            if os.path.exists(filepath):
                if os.path.isdir(filepath):
                    shutil.rmtree(filepath)
                else:
                    os.remove(filepath)
                deleted += 1
            else:
                skipped.append(rel)
        except Exception:
            skipped.append(rel)
    return jsonify({'success': True, 'deleted': deleted, 'skipped': skipped})

@app.route('/api/upload/<server_id>', methods=['POST'])
def api_upload(server_id):
    """رفع ملف أو عدة ملفات مع فك تلقائي للأرشيفات"""
    files = request.files.getlist('file')
    if not files:
        return jsonify({'error': 'لا يوجد ملف'}), 400

    folder = request.form.get('folder', '')
    server_dir = get_server_dir(server_id)
    if folder:
        root_dir, safe_folder = resolve_server_path(server_id, folder)
        if not safe_folder:
            return jsonify({'error': 'مجلد غير آمن'}), 400
        server_dir = safe_folder
        os.makedirs(server_dir, exist_ok=True)

    uploaded = 0
    extracted_archives = 0
    uploaded_python = False
    last_python_file = None

    server, _ = get_server_by_id(server_id)
    main_file = (server.get('main_file', 'main.py') if server else 'main.py').strip()
    req_file = (server.get('requirements_file', 'requirements.txt') if server else 'requirements.txt').strip() or 'requirements.txt'

    for file in files:
        if not file or not getattr(file, 'filename', ''):
            continue

        safe_name = os.path.basename(file.filename)
        if not safe_name:
            continue

        dest_path = os.path.join(server_dir, safe_name)
        file.save(dest_path)
        uploaded += 1

        if safe_name.endswith('.py'):
            uploaded_python = True
            last_python_file = dest_path

        if is_archive_filename(safe_name):
            extracted_archives += auto_extract_archive(dest_path, server_dir)

    # إذا تم رفع ملف التشغيل الرئيسي، حدّث المتطلبات تلقائيًا
    if uploaded_python:
        target = last_python_file if last_python_file and os.path.basename(last_python_file) == os.path.basename(main_file) else None
        if target:
            sync_startup_requirements(server_id, main_file=main_file, requirements_file=req_file, auto_install=True)

    if uploaded == 0:
        return jsonify({'error': 'لم يتم رفع أي ملف'}), 400

    return jsonify({
        'success': True,
        'uploaded': uploaded,
        'extracted': extracted_archives
    })
@app.route('/api/create_folder/<server_id>', methods=['POST'])
def api_create_folder(server_id):
    """إنشاء مجلد جديد"""
    
    data = request.get_json()
    os.makedirs(os.path.join(get_server_dir(server_id), data.get('foldername', '')), exist_ok=True)
    return jsonify({'success': True})
@app.route('/api/rename/<server_id>', methods=['POST'])
def api_rename(server_id):
    """إعادة تسمية ملف أو مجلد"""
    
    d = request.get_json()
    server_dir = get_server_dir(server_id)
    old_path = os.path.join(server_dir, d.get('old_name', ''))
    new_path = os.path.join(server_dir, d.get('new_name', ''))
    
    if os.path.exists(old_path):
        os.rename(old_path, new_path)
        return jsonify({'success': True})
    
    return jsonify({'error': 'غير موجود'}), 404
@app.route('/api/unzip/<server_id>', methods=['POST'])
def api_unzip(server_id):
    """فك ضغط ملف ZIP"""
    
    data = request.get_json()
    zip_path = os.path.join(get_server_dir(server_id), data.get('filename', ''))
    
    if os.path.exists(zip_path) and zip_path.endswith('.zip'):
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf: 
                zf.extractall(os.path.dirname(zip_path))
            return jsonify({'status': 'success', 'msg': 'تم الاستخراج!'})
        except Exception as e: 
            return jsonify({'status': 'error', 'msg': str(e)})
    
    return jsonify({'status': 'error', 'msg': 'ملف ZIP غير صالح'}), 400
@app.route('/api/download/<server_id>', methods=['GET'])
def api_download_file(server_id):
    """تحميل ملف أو مجلد (مجلد كملف ZIP)"""
    filename = request.args.get('filename', '')
    server_root, filepath = resolve_server_path(server_id, filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({'error': 'غير موجود'}), 404
    if os.path.isdir(filepath):
        archive_base = os.path.basename(os.path.normpath(filename.rstrip('/'))) or f'{server_id}_site'
        mem = BytesIO()
        with zipfile.ZipFile(mem, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(filepath):
                for file in files:
                    full = os.path.join(root, file)
                    arcname = os.path.relpath(full, server_root)
                    zf.write(full, arcname)
        mem.seek(0)
        return send_file(mem, as_attachment=True, download_name=f'{archive_base}.zip', mimetype='application/zip')
    return send_file(filepath, as_attachment=True)
@app.route('/api/compress/<server_id>', methods=['POST'])
def api_compress_files(server_id):
    """ضغط مجموعة ملفات أو مجلدات إلى ZIP"""
    data = request.get_json(silent=True) or {}
    filenames = data.get('filenames', [])
    if isinstance(filenames, str):
        filenames = [filenames]
    archive_name = (data.get('archive_name') or 'archive.zip').strip()
    if not archive_name.lower().endswith('.zip'):
        archive_name += '.zip'
    server_root = os.path.abspath(get_server_dir(server_id))
    mem = BytesIO()
    added = 0
    with zipfile.ZipFile(mem, 'w', zipfile.ZIP_DEFLATED) as zf:
        for rel in filenames:
            _, filepath = resolve_server_path(server_id, rel)
            if not filepath or not os.path.exists(filepath):
                continue
            if os.path.isdir(filepath):
                for root, _, files in os.walk(filepath):
                    for file in files:
                        full = os.path.join(root, file)
                        arcname = os.path.relpath(full, server_root)
                        zf.write(full, arcname)
                        added += 1
            else:
                arcname = os.path.relpath(filepath, server_root)
                zf.write(filepath, arcname)
                added += 1
    if not added:
        return jsonify({'error': 'لا يوجد ملفات للضغط'}), 400
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name=archive_name, mimetype='application/zip')
# ============================================
# ⚙️ إعدادات بدء التشغيل
# ============================================
@app.route('/api/get_startup/<server_id>')
def api_get_startup(server_id):
    """جلب إعدادات بدء التشغيل"""
    
    server, _ = get_server_by_id(server_id)
    if server: 
        return jsonify({
            'main_file': server.get('main_file', 'main.py'), 
            'requirements_file': server.get('requirements_file', 'requirements.txt')
        })
    
    return jsonify({'main_file': 'main.py', 'requirements_file': 'requirements.txt'})
@app.route('/api/set_startup/<server_id>', methods=['POST'])
def api_set_startup(server_id):
    """تعيين إعدادات بدء التشغيل"""
    
    d = request.get_json()
    users = load_users()
    
    for uname, udata in users.items():
        if uname == 'SUNIK': 
            continue
        servers = udata.get('servers', [])
        if not isinstance(servers, list): 
            continue
        
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                s['main_file'] = d.get('main_file', 'main.py')
                s['requirements_file'] = d.get('requirements_file')
                save_users(users)
                try:
                    sync_startup_requirements(
                        server_id,
                        main_file=s['main_file'],
                        requirements_file=s.get('requirements_file') or 'requirements.txt',
                        auto_install=True
                    )
                except Exception:
                    pass
                return jsonify({'success': True})
    
    return jsonify({'error': 'غير موجود'}), 404
@app.route('/api/sync_requirements/<server_id>', methods=['POST'])
def api_sync_requirements(server_id):
    """مزامنة requirements.txt تلقائيًا من ملف التشغيل الرئيسي"""
    data = request.get_json(silent=True) or {}
    server, _ = get_server_by_id(server_id)
    if not server:
        return jsonify({'error': 'غير موجود'}), 404
    result = sync_startup_requirements(
        server_id,
        main_file=data.get('main_file') or server.get('main_file', 'main.py'),
        requirements_file=data.get('requirements_file') or server.get('requirements_file', 'requirements.txt'),
        auto_install=True
    )
    code = 200 if result.get('ok') else 400
    return jsonify(result), code
@app.route('/admin/settings', methods=['POST'])
def admin_settings():
    """تحديث الإعدادات العامة"""
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'غير مصرح!'}), 403
    data = request.get_json(silent=True) or {}
    settings = load_settings()
    try:
        max_servers = int(data.get('max_servers_per_user', settings.get('max_servers_per_user', 3)))
    except Exception:
        max_servers = settings.get('max_servers_per_user', 3)
    max_servers = max(1, min(max_servers, 100))
    settings['max_servers_per_user'] = max_servers
    save_settings(settings)
    return jsonify({'success': True, 'max_servers_per_user': max_servers})
# ============================================
# 🚀 تشغيل التطبيق
# ============================================
# تشغيل الصيانة التلقائية فور تحميل التطبيق
start_auto_maintenance()
if __name__ == '__main__':
    host = os.getenv('HOST', '0.0.0.0')
    port = int(os.getenv('PORT', '5000'))
    debug_mode = os.getenv('FLASK_DEBUG', '0') == '1'
    print("\n" + "=" * 50)
    print("🚀 استضافة البوتات - ªGE تيم HOSTING")
    print("=" * 50)
    print(f"📍 الصفحة الرئيسية: http://{host}:{port}")
    print(f"📍 لوحة الأدمن: http://{host}:{port}/login")
    print(f"🔗 API الإنشاء: http://{host}:{port}/api/create")
    print(f"👤 الخادم التجريبي: http://{host}:{port}/09ea9eeb/home/api/server")
    print(f"🧩 API الخادم: http://{host}:{port}/09ea9eeb/home/api/server/api")
    print("👤 SUNIK / SUNIKFF")
    print("=" * 50 + "\n")
    app.run(host=host, port=port, debug=debug_mode, use_reloader=False, threaded=True)