"""TunsPro MVP API. Python standard library + SQLite; Stripe/email/Twilio are optional via .env."""
from __future__ import annotations
import base64, hashlib, hmac, http.server, json, os, re, secrets, smtplib, sqlite3, ssl, urllib.error, urllib.parse, urllib.request, uuid
import unicodedata
import ipaddress
import threading
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from http import cookies
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('TUNSPRO_DB', ROOT / 'tunspro.sqlite3'))
MEDIA_DIR = DB_PATH.parent / 'uploads'
BACKUP_DIR = DB_PATH.parent / 'backups'
PORT = int(os.environ.get('PORT', '8765'))
TZ = ZoneInfo('Europe/Bucharest')
PLAN_PRICES = {'pro': 4900, 'business': 9900}
SUBSCRIPTION_GRACE_DAYS = max(0, min(30, int(os.environ.get('SUBSCRIPTION_GRACE_DAYS', '7'))))
MAX_REQUEST_BYTES = 2_200_000

def load_env():
    p = ROOT / '.env'
    if p.exists():
        for line in p.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1); os.environ.setdefault(k.strip(), v.strip().strip('"\''))
load_env()

def connect():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

def create_database_backup(label='daily'):
    """Create and verify an online SQLite snapshot outside the public web root."""
    if not DB_PATH.is_file() or DB_PATH.stat().st_size == 0:
        return None
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(TZ).strftime('%Y%m%d-%H%M%S')
    target = BACKUP_DIR / f'tunspro-{label}-{stamp}.sqlite3'
    temporary = target.with_suffix('.tmp')
    try:
        source = sqlite3.connect(DB_PATH, timeout=30)
        destination = sqlite3.connect(temporary, timeout=30)
        try:
            source.backup(destination)
            check = destination.execute('PRAGMA quick_check').fetchone()
            destination.commit()
        finally:
            destination.close()
            source.close()
        if not check or check[0] != 'ok':
            raise sqlite3.DatabaseError('backup integrity check failed')
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, target)
        cutoff = datetime.now().timestamp() - 14 * 24 * 60 * 60
        backups = sorted(BACKUP_DIR.glob('tunspro-*.sqlite3'), key=lambda p: p.stat().st_mtime, reverse=True)
        for index, old in enumerate(backups):
            if old != target and (old.stat().st_mtime < cutoff or index >= 14):
                old.unlink(missing_ok=True)
        return target
    finally:
        temporary.unlink(missing_ok=True)

def database_backup_loop():
    while True:
        threading.Event().wait(24 * 60 * 60)
        try:
            created = create_database_backup('daily')
            if created:
                print('Daily database backup completed and verified.', flush=True)
        except Exception as e:
            print('Daily database backup failed:', type(e).__name__, flush=True)

def init_db():
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, owner TEXT NOT NULL, created TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE IF NOT EXISTS shops(id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, city TEXT NOT NULL, address TEXT NOT NULL, phone TEXT NOT NULL, tagline TEXT DEFAULT '', photos TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL, approval_status TEXT NOT NULL DEFAULT 'approved');
        CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY, shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE, name TEXT NOT NULL, description TEXT DEFAULT '', duration INTEGER NOT NULL, price INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS staff(id INTEGER PRIMARY KEY, shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE, name TEXT NOT NULL, role TEXT DEFAULT 'Frizer', active INTEGER NOT NULL DEFAULT 1, weekly_schedule TEXT NOT NULL DEFAULT '{}', photo TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY, shop_id INTEGER NOT NULL REFERENCES shops(id), service_id INTEGER NOT NULL REFERENCES services(id), staff_id INTEGER NOT NULL REFERENCES staff(id), client TEXT NOT NULL, phone TEXT NOT NULL, email TEXT DEFAULT '', starts TEXT NOT NULL, ends TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed', created TEXT NOT NULL, reminder_at TEXT, reminder_sent INTEGER NOT NULL DEFAULT 0, customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL, price_at_booking INTEGER, manage_token_hash TEXT);
        CREATE INDEX IF NOT EXISTS bookings_staff_time ON bookings(staff_id, starts, ends, status);
        CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY, shop_id INTEGER UNIQUE NOT NULL REFERENCES shops(id) ON DELETE CASCADE, status TEXT NOT NULL DEFAULT 'inactive', paid_until TEXT, stripe_customer_id TEXT, stripe_subscription_id TEXT UNIQUE, plan TEXT NOT NULL DEFAULT 'free', stripe_event_created INTEGER NOT NULL DEFAULT 0, current_period_start TEXT, payment_failed_at TEXT, cancel_at_period_end INTEGER NOT NULL DEFAULT 0, cancel_at TEXT);
        CREATE TABLE IF NOT EXISTS stripe_events(event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, event_created INTEGER NOT NULL, received TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, stripe_invoice_id TEXT NOT NULL UNIQUE, shop_id INTEGER REFERENCES shops(id) ON DELETE SET NULL, stripe_customer_id TEXT, stripe_subscription_id TEXT, amount INTEGER NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'ron', status TEXT NOT NULL, paid_at TEXT, invoice_url TEXT, created TEXT NOT NULL, stripe_event_created INTEGER NOT NULL DEFAULT 0, refunded_amount INTEGER NOT NULL DEFAULT 0, stripe_payment_intent_id TEXT, paid_out_of_band INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS refunds(id INTEGER PRIMARY KEY, stripe_refund_id TEXT NOT NULL UNIQUE, stripe_invoice_id TEXT, stripe_charge_id TEXT, shop_id INTEGER REFERENCES shops(id) ON DELETE SET NULL, amount INTEGER NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'ron', status TEXT NOT NULL, created TEXT NOT NULL, stripe_event_created INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS subscription_history(id INTEGER PRIMARY KEY, stripe_event_id TEXT NOT NULL UNIQUE, shop_id INTEGER REFERENCES shops(id) ON DELETE SET NULL, stripe_subscription_id TEXT, event_type TEXT NOT NULL, status TEXT, plan TEXT, period_start TEXT, period_end TEXT, event_created INTEGER NOT NULL, created TEXT NOT NULL, cancel_at_period_end INTEGER NOT NULL DEFAULT 0, cancel_at TEXT);
        CREATE TABLE IF NOT EXISTS admin_audit(id INTEGER PRIMARY KEY, admin_email TEXT NOT NULL, action TEXT NOT NULL, shop_id INTEGER REFERENCES shops(id) ON DELETE SET NULL, details TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS booking_audit(id INTEGER PRIMARY KEY, booking_id INTEGER REFERENCES bookings(id) ON DELETE SET NULL, shop_id INTEGER REFERENCES shops(id) ON DELETE SET NULL, action TEXT NOT NULL, actor_type TEXT NOT NULL, actor_id INTEGER, old_starts TEXT, new_starts TEXT, old_status TEXT, new_status TEXT, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS client_notes(id INTEGER PRIMARY KEY, shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE, client_key TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL, UNIQUE(shop_id,client_key));
        CREATE TABLE IF NOT EXISTS login_limits(email_hash TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0, window_start TEXT NOT NULL, blocked_until TEXT);
        CREATE TABLE IF NOT EXISTS request_limits(bucket_hash TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0, window_start TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS password_resets(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires TEXT NOT NULL, created TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS password_resets_user ON password_resets(user_id);
        CREATE TABLE IF NOT EXISTS password_reset_limits(email_hash TEXT PRIMARY KEY, requested TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(shop_id INTEGER PRIMARY KEY REFERENCES shops(id) ON DELETE CASCADE, notification_email INTEGER NOT NULL DEFAULT 1, notification_sms INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL, created TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE IF NOT EXISTS client_sessions(token_hash TEXT PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE, expires TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS favorites(customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE, shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE, created TEXT NOT NULL, PRIMARY KEY(customer_id,shop_id));
        CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, booking_id INTEGER UNIQUE NOT NULL REFERENCES bookings(id) ON DELETE CASCADE, customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL, shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE, rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5), comment TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS promo_codes(id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, discount_percent INTEGER NOT NULL CHECK(discount_percent BETWEEN 1 AND 100), active INTEGER NOT NULL DEFAULT 1, starts_at TEXT, ends_at TEXT, max_uses INTEGER, uses_count INTEGER NOT NULL DEFAULT 0, created_by TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS platform_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admin_sessions(token_hash TEXT PRIMARY KEY, admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE, expires TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS reviews_shop_created ON reviews(shop_id,created);
        ''')
        for table, column, definition in [('shops','photos',"TEXT NOT NULL DEFAULT '[]'"),('shops','listing_enabled','INTEGER NOT NULL DEFAULT 1'),('shops','approval_status',"TEXT NOT NULL DEFAULT 'approved'"),('bookings','reminder_at','TEXT'),('bookings','reminder_sent','INTEGER NOT NULL DEFAULT 0'),('bookings','customer_id','INTEGER REFERENCES customers(id) ON DELETE SET NULL'),('bookings','price_at_booking','INTEGER'),('bookings','manage_token_hash','TEXT'),('payments','livemode','INTEGER NOT NULL DEFAULT 0'),('payments','paid_out_of_band','INTEGER NOT NULL DEFAULT 0'),('refunds','livemode','INTEGER NOT NULL DEFAULT 0'),('refunds','stripe_event_created','INTEGER NOT NULL DEFAULT 0')]:
            if column not in {row['name'] for row in c.execute(f'PRAGMA table_info({table})')}:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        if 'photo' not in {row['name'] for row in c.execute('PRAGMA table_info(staff)')}:
            c.execute("ALTER TABLE staff ADD COLUMN photo TEXT NOT NULL DEFAULT ''")
        if 'plan' not in {row['name'] for row in c.execute('PRAGMA table_info(subscriptions)')}:
            c.execute("ALTER TABLE subscriptions ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
        if 'stripe_event_created' not in {row['name'] for row in c.execute('PRAGMA table_info(subscriptions)')}:
            c.execute('ALTER TABLE subscriptions ADD COLUMN stripe_event_created INTEGER NOT NULL DEFAULT 0')
        if 'current_period_start' not in {row['name'] for row in c.execute('PRAGMA table_info(subscriptions)')}:
            c.execute('ALTER TABLE subscriptions ADD COLUMN current_period_start TEXT')
        for column,definition in [('payment_failed_at','TEXT'),('cancel_at_period_end','INTEGER NOT NULL DEFAULT 0'),('cancel_at','TEXT')]:
            if column not in {row['name'] for row in c.execute('PRAGMA table_info(subscriptions)')}:
                c.execute(f'ALTER TABLE subscriptions ADD COLUMN {column} {definition}')
        for column,definition in [('cancel_at_period_end','INTEGER NOT NULL DEFAULT 0'),('cancel_at','TEXT')]:
            if column not in {row['name'] for row in c.execute('PRAGMA table_info(subscription_history)')}:
                c.execute(f'ALTER TABLE subscription_history ADD COLUMN {column} {definition}')
        if 'stripe_event_created' not in {row['name'] for row in c.execute('PRAGMA table_info(payments)')}:
            c.execute('ALTER TABLE payments ADD COLUMN stripe_event_created INTEGER NOT NULL DEFAULT 0')
        for column,definition in [('refunded_amount','INTEGER NOT NULL DEFAULT 0'),('stripe_payment_intent_id','TEXT')]:
            if column not in {row['name'] for row in c.execute('PRAGMA table_info(payments)')}:
                c.execute(f'ALTER TABLE payments ADD COLUMN {column} {definition}')
        if 'paid_out_of_band' not in {row['name'] for row in c.execute('PRAGMA table_info(payments)')}:
            c.execute('ALTER TABLE payments ADD COLUMN paid_out_of_band INTEGER NOT NULL DEFAULT 0')
        for table in ('users','customers'):
            if 'status' not in {row['name'] for row in c.execute(f'PRAGMA table_info({table})')}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            if 'last_login' not in {row['name'] for row in c.execute(f'PRAGMA table_info({table})')}:
                c.execute(f'ALTER TABLE {table} ADD COLUMN last_login TEXT')
        if 'last_login' not in {row['name'] for row in c.execute('PRAGMA table_info(admins)')}:
            c.execute('ALTER TABLE admins ADD COLUMN last_login TEXT')
        for table,column,definition in [('reviews','is_visible','INTEGER NOT NULL DEFAULT 1'),('bookings','promo_code','TEXT'),('bookings','discount_amount','INTEGER NOT NULL DEFAULT 0')]:
            if column not in {row['name'] for row in c.execute(f'PRAGMA table_info({table})')}:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        if 'reason' not in {row['name'] for row in c.execute('PRAGMA table_info(booking_audit)')}:
            c.execute("ALTER TABLE booking_audit ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
        c.execute("UPDATE subscriptions SET plan='pro' WHERE plan='free' AND status IN ('active','trialing') AND paid_until IS NOT NULL")
        # Existing databases need the bookings columns added before this index
        # is created; CREATE TABLE IF NOT EXISTS does not migrate old tables.
        c.execute('CREATE INDEX IF NOT EXISTS bookings_customer_start ON bookings(customer_id,starts)')
        admin_email=os.environ.get('ADMIN_EMAIL','').lower().strip(); admin_password=os.environ.get('ADMIN_PASSWORD','')
        if admin_email and len(admin_password)>=16 and '@' in admin_email:
            c.execute('DELETE FROM admins WHERE email<>?',(admin_email,))
            existing=c.execute('SELECT id,password FROM admins WHERE email=?',(admin_email,)).fetchone()
            if not existing:
                c.execute('INSERT INTO admins(email,password,created) VALUES(?,?,?)',(admin_email,password_hash(admin_password),iso_now()))
            elif not password_ok(admin_password,existing['password']):
                c.execute('DELETE FROM admin_sessions WHERE admin_id=?',(existing['id'],))
                c.execute('UPDATE admins SET password=? WHERE id=?',(password_hash(admin_password),existing['id']))
    anonymize_old_bookings()

def anonymize_old_bookings():
    cutoff=(now_utc()-timedelta(days=365)).isoformat()
    with connect() as c:
        c.execute("UPDATE reviews SET customer_id=NULL WHERE booking_id IN (SELECT id FROM bookings WHERE julianday(ends)<julianday(?))",(cutoff,))
        old_clients=c.execute("SELECT DISTINCT shop_id,phone FROM bookings WHERE julianday(ends)<julianday(?) AND phone<>'' AND client!='Date anonimizate'",(cutoff,)).fetchall()
        for client in old_clients:
            key=hashlib.sha256(normalize_ro_mobile(client['phone']).encode()).hexdigest()
            retained=c.execute("SELECT 1 FROM bookings WHERE shop_id=? AND phone=? AND julianday(ends)>=julianday(?) AND client!='Date anonimizate' LIMIT 1",(client['shop_id'],client['phone'],cutoff)).fetchone()
            if not retained:c.execute('DELETE FROM client_notes WHERE shop_id=? AND client_key=?',(client['shop_id'],key))
        c.execute("UPDATE bookings SET client='Date anonimizate',phone='',email='',customer_id=NULL WHERE julianday(ends)<julianday(?) AND (client<>'Date anonimizate' OR phone<>'' OR email<>'' OR customer_id IS NOT NULL)",(cutoff,))

def anonymization_loop():
    while True:
        threading.Event().wait(24*60*60)
        try: anonymize_old_bookings()
        except Exception as e: print('Booking anonymization failed:',repr(e))

def send_due_reminders():
    with connect() as c:
        rows=c.execute("SELECT b.*,sh.name shop_name,sh.address,sh.city,sh.phone shop_phone,s.name service_name,t.name staff_name,COALESCE(cfg.notification_email,1) notification_email,COALESCE(cfg.notification_sms,0) notification_sms FROM bookings b JOIN shops sh ON sh.id=b.shop_id JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id LEFT JOIN settings cfg ON cfg.shop_id=sh.id WHERE b.status='confirmed' AND b.reminder_sent=0 AND b.reminder_at IS NOT NULL AND julianday(b.reminder_at)<=julianday(?) AND julianday(b.starts)>julianday(?)",(iso_now(),iso_now())).fetchall()
    for row in rows:
        when=datetime.fromisoformat(row['starts']).astimezone(TZ).strftime('%d.%m.%Y, %H:%M')
        body=f'Reamintire TunsPro: ai programare la {row["shop_name"]} pe {when}, pentru {row["service_name"]} cu {row["staff_name"]}. Adresă: {row["address"]}, {row["city"]}.'
        attempted=False
        if row['notification_email'] and row['email'] and email_configured():
            try:send_email(row['email'],f'Reamintire programare — {row["shop_name"]}',body);attempted=True
            except Exception as e:print('Email reminder failed:',repr(e))
        if row['notification_sms'] and row['phone'] and os.environ.get('TWILIO_ACCOUNT_SID') and os.environ.get('TWILIO_AUTH_TOKEN') and os.environ.get('TWILIO_FROM'):
            try:sms_notice(row['phone'],body);attempted=True
            except Exception as e:print('SMS reminder failed:',repr(e))
        if attempted:
            with connect() as c:c.execute('UPDATE bookings SET reminder_sent=1 WHERE id=? AND reminder_sent=0',(row['id'],))

def reminder_loop():
    while True:
        try:send_due_reminders()
        except Exception as e:print('Booking reminder worker failed:',repr(e))
        threading.Event().wait(5*60)

def now_utc(): return datetime.now(timezone.utc)
def iso_now(): return now_utc().isoformat()
def login_limit_key(kind,email): return hashlib.sha256((kind+':'+str(email).strip().lower()).encode()).hexdigest()
def login_is_limited(c,key):
    row=c.execute('SELECT blocked_until FROM login_limits WHERE email_hash=?',(key,)).fetchone()
    return bool(row and row['blocked_until'] and datetime.fromisoformat(row['blocked_until'])>now_utc())
def login_failure(c,key):
    now=now_utc();row=c.execute('SELECT failures,window_start FROM login_limits WHERE email_hash=?',(key,)).fetchone()
    if not row or now-datetime.fromisoformat(row['window_start'])>=timedelta(minutes=15):
        c.execute('INSERT INTO login_limits(email_hash,failures,window_start,blocked_until) VALUES(?,1,?,NULL) ON CONFLICT(email_hash) DO UPDATE SET failures=1,window_start=excluded.window_start,blocked_until=NULL',(key,now.isoformat()));return
    failures=row['failures']+1;blocked=(now+timedelta(minutes=15)).isoformat() if failures>=10 else None
    c.execute('UPDATE login_limits SET failures=?,blocked_until=COALESCE(?,blocked_until) WHERE email_hash=?',(failures,blocked,key))
def login_success(c,key): c.execute('DELETE FROM login_limits WHERE email_hash=?',(key,))
def request_limit_allows(c,scope,identity,maximum,window_seconds):
    key=hashlib.sha256((scope+':'+str(identity).strip().lower()).encode()).hexdigest();now=now_utc()
    c.execute('BEGIN IMMEDIATE')
    row=c.execute('SELECT requests,window_start FROM request_limits WHERE bucket_hash=?',(key,)).fetchone()
    if not row or now-datetime.fromisoformat(row['window_start'])>=timedelta(seconds=window_seconds):
        c.execute('INSERT INTO request_limits(bucket_hash,requests,window_start) VALUES(?,1,?) ON CONFLICT(bucket_hash) DO UPDATE SET requests=1,window_start=excluded.window_start',(key,now.isoformat()))
        c.execute('DELETE FROM request_limits WHERE window_start<?',((now-timedelta(days=7)).isoformat(),))
        return True
    if row['requests']>=maximum:return False
    c.execute('UPDATE request_limits SET requests=requests+1 WHERE bucket_hash=?',(key,))
    return True
def record_booking_event(c,booking_id,shop_id,action,actor_type,actor_id=None,old_starts=None,new_starts=None,old_status=None,new_status=None,reason=''):
    c.execute('INSERT INTO booking_audit(booking_id,shop_id,action,actor_type,actor_id,old_starts,new_starts,old_status,new_status,created,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(booking_id,shop_id,action,actor_type,actor_id,old_starts,new_starts,old_status,new_status,iso_now(),reason))
def record_subscription_event(c,event_id,event_type,event_created,shop_id,stripe_sub,status,plan,period_start,period_end,cancel_at_period_end=False,cancel_at=None):
    c.execute('INSERT OR IGNORE INTO subscription_history(stripe_event_id,shop_id,stripe_subscription_id,event_type,status,plan,period_start,period_end,event_created,created,cancel_at_period_end,cancel_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(event_id,shop_id,stripe_sub,event_type,status,plan,period_start,period_end,event_created,iso_now(),int(bool(cancel_at_period_end)),cancel_at))
def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    result = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 240000)
    return salt.hex() + '$' + result.hex()
def password_ok(password, stored):
    try:
        salt, _ = stored.split('$', 1)
        return hmac.compare_digest(password_hash(password, bytes.fromhex(salt)), stored)
    except Exception: return False
def slugify(s):
    import re
    s = ''.join(ch for ch in unicodedata.normalize('NFKD', s) if not unicodedata.combining(ch)).lower()
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', s)).strip('-')[:55] or 'frizerie'
def search_norm(s): return ''.join(ch for ch in unicodedata.normalize('NFD',str(s)) if not unicodedata.combining(ch)).lower()
def stripe_subscription_details(subscription_id):
    key=os.environ.get('STRIPE_SECRET_KEY')
    if not key or not subscription_id:return None
    req=urllib.request.Request(f'https://api.stripe.com/v1/subscriptions/{urllib.parse.quote(str(subscription_id), safe="")}')
    req.add_header('Authorization','Bearer '+key)
    details=json.loads(urllib.request.urlopen(req,timeout=20).read());details.setdefault('current_period_end',max((item.get('current_period_end',0) for item in details.get('items',{}).get('data',[])),default=0));return details

def stripe_api(key,path,params=None):
    url='https://api.stripe.com/v1/'+path
    data=None
    if params is not None:data=urllib.parse.urlencode(params).encode()
    req=urllib.request.Request(url,data=data);req.add_header('Authorization','Bearer '+key)
    return json.loads(urllib.request.urlopen(req,timeout=20).read())

def stripe_business_upgrade_session(key,customer_id,subscription_id,return_url):
    details=stripe_subscription_details(subscription_id)
    items=(details or {}).get('items',{}).get('data',[])
    if len(items)!=1:raise ValueError('Abonamentul nu poate fi schimbat automat. Contactează suportul TunsPro.')
    item=items[0]
    products=stripe_api(key,'products?active=true&limit=100').get('data',[])
    product=next((p for p in products if p.get('name')=='TunsPro BUSINESS' and p.get('metadata',{}).get('tunspro_plan')=='business'),None)
    if not product:product=stripe_api(key,'products',{'name':'TunsPro BUSINESS','description':'Abonament lunar TunsPro BUSINESS — echipă cu mai mulți frizeri.','metadata[tunspro_plan]':'business'})
    prices=stripe_api(key,'prices?active=true&limit=100&product='+urllib.parse.quote(product['id'],safe='')).get('data',[])
    price=next((p for p in prices if p.get('currency')=='ron' and p.get('unit_amount')==PLAN_PRICES['business'] and p.get('recurring',{}).get('interval')=='month' and p.get('metadata',{}).get('plan')=='business'),None)
    if not price:price=stripe_api(key,'prices',{'product':product['id'],'currency':'ron','unit_amount':str(PLAN_PRICES['business']),'recurring[interval]':'month','nickname':'TunsPro BUSINESS','metadata[plan]':'business'})
    configurations=stripe_api(key,'billing_portal/configurations?limit=100').get('data',[])
    config=next((x for x in configurations if x.get('active') and x.get('metadata',{}).get('tunspro_business_upgrade')=='1' and x.get('features',{}).get('subscription_update',{}).get('enabled') and any(p.get('product')==product['id'] and price['id'] in p.get('prices',[]) for p in x.get('features',{}).get('subscription_update',{}).get('products',[]))),None)
    if not config:
        config=stripe_api(key,'billing_portal/configurations',{'features[payment_method_update][enabled]':'true','features[subscription_update][enabled]':'true','features[subscription_update][default_allowed_updates][]':'price','features[subscription_update][proration_behavior]':'always_invoice','features[subscription_update][products][0][product]':product['id'],'features[subscription_update][products][0][prices][0]':price['id'],'metadata[tunspro_business_upgrade]':'1'})
    elif not config.get('features',{}).get('payment_method_update',{}).get('enabled'):
        config=stripe_api(key,'billing_portal/configurations/'+urllib.parse.quote(config['id'],safe=''),{'features[payment_method_update][enabled]':'true'})
    params={'customer':customer_id,'configuration':config['id'],'return_url':return_url,'flow_data[type]':'subscription_update_confirm','flow_data[subscription_update_confirm][subscription]':subscription_id,'flow_data[subscription_update_confirm][items][0][id]':item['id'],'flow_data[subscription_update_confirm][items][0][price]':price['id'],'flow_data[subscription_update_confirm][items][0][quantity]':'1','flow_data[after_completion][type]':'redirect','flow_data[after_completion][redirect][return_url]':return_url}
    return stripe_api(key,'billing_portal/sessions',params)

def active_subscription(row):
    if not row or row['plan'] not in PLAN_PRICES:return False
    if row['status']=='past_due' and SUBSCRIPTION_GRACE_DAYS and 'payment_failed_at' in row.keys() and row['payment_failed_at']:
        try:return datetime.fromisoformat(row['payment_failed_at'].replace('Z','+00:00'))+timedelta(days=SUBSCRIPTION_GRACE_DAYS)>now_utc()
        except ValueError:return False
    if row['status'] not in ('active','trialing') or not row['paid_until']:return False
    try:return datetime.fromisoformat(row['paid_until'].replace('Z', '+00:00')) > now_utc()
    except ValueError: return False

def shop_public(c, shop):
    owner=c.execute('SELECT status FROM users WHERE id=?',(shop['user_id'],)).fetchone()
    if not owner or owner['status']!='active':return None
    approval=c.execute('SELECT approval_status FROM shops WHERE id=?',(shop['id'],)).fetchone()
    if not approval or approval['approval_status']!='approved':return None
    sub = c.execute('SELECT * FROM subscriptions WHERE shop_id=?', (shop['id'],)).fetchone()
    if not active_subscription(sub) or not shop['listing_enabled']: return None
    svc = c.execute('SELECT id,name,description,duration,price FROM services WHERE shop_id=? AND active=1 ORDER BY id', (shop['id'],)).fetchall()
    team = c.execute('SELECT id,name,role,photo,weekly_schedule FROM staff WHERE shop_id=? AND active=1 ORDER BY id', (shop['id'],)).fetchall()
    if not svc or not team: return None
    reviews=c.execute('SELECT rating,comment,created FROM reviews WHERE shop_id=? AND is_visible=1 ORDER BY created DESC LIMIT 20',(shop['id'],)).fetchall()
    rating=c.execute('SELECT COUNT(*) count,AVG(rating) average FROM reviews WHERE shop_id=? AND is_visible=1',(shop['id'],)).fetchone()
    return {'id':shop['id'],'name':shop['name'],'slug':shop['slug'],'city':shop['city'],'address':shop['address'],'phone':shop['phone'],'tagline':shop['tagline'],'photos':json.loads(shop['photos'] or '[]'),'rating':round(rating['average'],1) if rating['average'] else None,'review_count':rating['count'],'reviews':[dict(x) for x in reviews],'services':[dict(x) for x in svc],'team':[{**dict(x),'weekly_schedule':json.loads(x['weekly_schedule'] or '{}')} for x in team]}

def normalize_ro_mobile(value):
    digits=''.join(ch for ch in value if ch.isdigit())
    if digits.startswith('0040'):digits=digits[2:]
    if digits.startswith('0'):digits='40'+digits[1:]
    if not digits.startswith('40'):return ''
    return '+'+digits if re.fullmatch(r'407[0-9]{8}',digits) else ''

def email_configured():
    resend_key=os.environ.get('RESEND_API_KEY','').strip()
    resend_from=os.environ.get('RESEND_FROM','').strip()
    if resend_key or resend_from:
        return bool(resend_key and resend_from)
    return bool(os.environ.get('SMTP_HOST','').strip() and os.environ.get('SMTP_FROM','').strip())

def email_provider():
    return 'Resend' if os.environ.get('RESEND_API_KEY','').strip() else 'SMTP'

def send_email(to_email, subject, body):
    if not to_email: raise ValueError('Missing email recipient')
    resend_key=os.environ.get('RESEND_API_KEY','').strip()
    resend_from=os.environ.get('RESEND_FROM','').strip()
    if resend_key or resend_from:
        if not resend_key or not resend_from:
            raise RuntimeError('Resend requires both RESEND_API_KEY and RESEND_FROM')
        payload=json.dumps({'from':resend_from,'to':[to_email],'subject':subject,'text':body}).encode()
        req=urllib.request.Request('https://api.resend.com/emails',data=payload,headers={'Authorization':'Bearer '+resend_key,'Accept':'application/json','Content-Type':'application/json','User-Agent':'TunsPro/1.0'})
        try:
            with urllib.request.urlopen(req,timeout=20) as response:
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f'Resend returned HTTP {response.status}')
                response.read()
        except urllib.error.HTTPError as e:
            detail=e.read().decode('utf-8','replace')[:500]
            raise RuntimeError(f'Resend returned HTTP {e.code}: {detail}') from None
        return 'resend'
    host=os.environ.get('SMTP_HOST','').strip(); sender=os.environ.get('SMTP_FROM','').strip()
    if not host or not sender: raise RuntimeError('E-mail is not configured')
    msg=EmailMessage(); msg['Subject']=subject; msg['From']=sender; msg['To']=to_email; msg.set_content(body)
    port=int(os.environ.get('SMTP_PORT','587')); user=os.environ.get('SMTP_USER',''); password=os.environ.get('SMTP_PASSWORD','')
    with smtplib.SMTP(host,port,timeout=20) as s:
        s.ehlo()
        s.starttls(context=ssl.create_default_context())
        s.ehlo()
        if user: s.login(user,password)
        s.send_message(msg)
    return 'smtp'

def sms_notice(phone, body):
    sid=os.environ.get('TWILIO_ACCOUNT_SID'); token=os.environ.get('TWILIO_AUTH_TOKEN'); sender=os.environ.get('TWILIO_FROM')
    if not (sid and token and sender and phone): return
    data=urllib.parse.urlencode({'From':sender,'To':phone,'Body':body}).encode()
    req=urllib.request.Request(f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json',data=data)
    req.add_header('Authorization','Basic '+__import__('base64').b64encode(f'{sid}:{token}'.encode()).decode())
    urllib.request.urlopen(req,timeout=15).read()

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw): super().__init__(*a,directory=str(ROOT),**kw)
    def log_message(self, fmt, *args): print('%s - %s' % (self.address_string(), fmt % args))
    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','strict-origin-when-cross-origin')
        self.send_header('Permissions-Policy','camera=(), microphone=(), geolocation=()')
        if os.environ.get('COOKIE_SECURE','0')=='1':self.send_header('Strict-Transport-Security','max-age=31536000')
        super().end_headers()
    def json_response(self, status, payload, extra=None):
        body=json.dumps(payload,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store')
        for k,v in (extra or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(body)
    def body_json(self):
        raw=self.read_request_body()
        if not raw:return {}
        content_type=self.headers.get('Content-Type','').split(';',1)[0].strip().lower()
        if content_type!='application/json':raise ValueError('Tipul cererii nu este valid.')
        payload=json.loads(raw)
        if not isinstance(payload,dict):raise ValueError('Formatul cererii nu este valid.')
        return payload
    def read_request_body(self):
        length=self.headers.get('Content-Length')
        if length is None or not re.fullmatch(r'\d{1,8}',length.strip()):raise ValueError('Dimensiunea cererii nu este validă.')
        n=int(length)
        if n>MAX_REQUEST_BYTES:raise ValueError('Cerere prea mare.')
        if self.headers.get('Transfer-Encoding'):raise ValueError('Formatul cererii nu este acceptat.')
        raw=self.rfile.read(n) if n else b''
        if len(raw)!=n:raise ValueError('Cererea este incompletă.')
        return raw
    def mutation_origin_valid(self):
        origin=self.headers.get('Origin')
        if not origin:return False
        try:
            parsed=urllib.parse.urlsplit(origin)
            host=self.headers.get('Host','').lower().strip()
            expected_scheme='https' if os.environ.get('COOKIE_SECURE','0')=='1' else parsed.scheme.lower()
            return parsed.scheme.lower()==expected_scheme and parsed.netloc.lower()==host and parsed.path in ('','/') and not parsed.query and not parsed.fragment
        except Exception:return False
    def client_ip(self):
        if os.environ.get('RENDER_SERVICE_ID'):
            cloudflare_ip=self.headers.get('CF-Connecting-IP','').strip()
            try:return str(ipaddress.ip_address(cloudflare_ip))
            except ValueError:pass
            forwarded=self.headers.get('X-Forwarded-For','')
            for candidate in reversed(forwarded.split(',')):
                try:return str(ipaddress.ip_address(candidate.strip()))
                except ValueError:continue
        try:return str(ipaddress.ip_address(self.client_address[0]))
        except (ValueError,IndexError):return 'unknown'
    def enforce_rate_limit(self,scope,maximum,window_seconds):
        with connect() as c:
            allowed=request_limit_allows(c,scope,self.client_ip(),maximum,window_seconds)
        if not allowed:self.json_response(429,{'error':'Prea multe cereri. Așteaptă puțin și încearcă din nou.'})
        return allowed
    def auth(self,c):
        jar=cookies.SimpleCookie(self.headers.get('Cookie','')); token=jar['tunspro_session'].value if 'tunspro_session' in jar else ''
        if not token: return None
        return c.execute("SELECT u.*,s.id shop_id,s.name shop_name,s.slug shop_slug FROM sessions x JOIN users u ON u.id=x.user_id JOIN shops s ON s.user_id=u.id WHERE x.token_hash=? AND x.expires>? AND u.status='active'",(hashlib.sha256(token.encode()).hexdigest(),iso_now())).fetchone()
    def set_session(self,user_id):
        token=secrets.token_urlsafe(32); expiry=now_utc()+timedelta(days=30)
        with connect() as c: c.execute('INSERT INTO sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user_id,expiry.isoformat()))
        secure='; Secure' if os.environ.get('COOKIE_SECURE','0')=='1' else ''
        return {'Set-Cookie':f'tunspro_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000{secure}'}
    def clear_session(self): return {'Set-Cookie':'tunspro_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0'+('; Secure' if os.environ.get('COOKIE_SECURE','0')=='1' else '')}
    def client_auth(self,c):
        jar=cookies.SimpleCookie(self.headers.get('Cookie','')); token=jar['tunspro_client'].value if 'tunspro_client' in jar else ''
        if not token:return None
        return c.execute("SELECT * FROM customers WHERE status='active' AND id=(SELECT customer_id FROM client_sessions WHERE token_hash=? AND expires>?)",(hashlib.sha256(token.encode()).hexdigest(),iso_now())).fetchone()
    def set_client_session(self,customer_id):
        token=secrets.token_urlsafe(32); expiry=now_utc()+timedelta(days=30)
        with connect() as c:c.execute('INSERT INTO client_sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),customer_id,expiry.isoformat()))
        secure='; Secure' if os.environ.get('COOKIE_SECURE','0')=='1' else ''
        return {'Set-Cookie':f'tunspro_client={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000{secure}'}
    def clear_client_session(self):return {'Set-Cookie':'tunspro_client=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0'+('; Secure' if os.environ.get('COOKIE_SECURE','0')=='1' else '')}
    def admin_auth(self,c):
        jar=cookies.SimpleCookie(self.headers.get('Cookie','')); token=jar['tunspro_admin'].value if 'tunspro_admin' in jar else ''
        if not token:return None
        return c.execute('SELECT * FROM admins WHERE id=(SELECT admin_id FROM admin_sessions WHERE token_hash=? AND expires>?)',(hashlib.sha256(token.encode()).hexdigest(),iso_now())).fetchone()
    def set_admin_session(self,admin_id):
        token=secrets.token_urlsafe(32); expiry=now_utc()+timedelta(hours=8)
        with connect() as c:c.execute('INSERT INTO admin_sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),admin_id,expiry.isoformat()))
        secure='; Secure' if os.environ.get('COOKIE_SECURE','0')=='1' else ''
        return {'Set-Cookie':f'tunspro_admin={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800{secure}'}
    def clear_admin_session(self):return {'Set-Cookie':'tunspro_admin=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'+('; Secure' if os.environ.get('COOKIE_SECURE','0')=='1' else '')}
    def get(self):
        u=urllib.parse.urlparse(self.path); path=urllib.parse.unquote(u.path); q=urllib.parse.parse_qs(u.query)
        if path=='/api/admin/backup':
            with connect() as c:
                admin=self.admin_auth(c)
                if not admin:return self.json_response(401,{'error':'Autentificare de administrator necesară.'})
                c.execute('INSERT INTO admin_audit(admin_email,action,details,created) VALUES(?,?,?,?)',(admin['email'],'database_backup_downloaded','{}',iso_now()))
            with connect() as source:
                snapshot=sqlite3.connect(':memory:')
                source.backup(snapshot);body=snapshot.serialize();snapshot.close()
            filename='tunspro-backup-'+datetime.now(TZ).strftime('%Y%m%d-%H%M%S')+'.sqlite3'
            self.send_response(200);self.send_header('Content-Type','application/vnd.sqlite3');self.send_header('Content-Disposition',f'attachment; filename="{filename}"');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body);return
        if path=='/api/admin/bookings.csv':
            with connect() as c:
                if not self.admin_auth(c):return self.json_response(401,{'error':'Autentificare de administrator necesară.'})
                rows=c.execute('SELECT b.id,b.client,b.phone,b.email,sh.name shop_name,sh.city,s.name service_name,t.name staff_name,b.starts,b.ends,s.duration,b.status,COALESCE(b.price_at_booking,s.price) price FROM bookings b JOIN shops sh ON sh.id=b.shop_id JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id ORDER BY b.starts DESC,b.id DESC').fetchall()
            import csv,io
            stream=io.StringIO(newline='');writer=csv.writer(stream);writer.writerow(['ID programare','Client','Telefon','E-mail','Frizerie','Localitate','Serviciu','Frizer','Început (Europe/Bucharest)','Sfârșit (Europe/Bucharest)','Durată minute','Status','Preț RON'])
            for row in rows:
                values=[row['id'],row['client'],row['phone'],row['email'],row['shop_name'],row['city'],row['service_name'],row['staff_name'],datetime.fromisoformat(row['starts']).astimezone(TZ).strftime('%Y-%m-%d %H:%M'),datetime.fromisoformat(row['ends']).astimezone(TZ).strftime('%Y-%m-%d %H:%M'),row['duration'],row['status'],row['price']]
                writer.writerow([("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for v in values])
            body=('\ufeff'+stream.getvalue()).encode('utf-8');self.send_response(200);self.send_header('Content-Type','text/csv; charset=utf-8');self.send_header('Content-Disposition','attachment; filename="tunspro-programari.csv"');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body);return
        if path.startswith('/media/'):
            name=path.rsplit('/',1)[-1]
            if not re.fullmatch(r'[0-9a-f]{32}\.(?:jpg|png|webp)',name):return self.send_error(404)
            file=MEDIA_DIR/name
            if not file.is_file():return self.send_error(404)
            mime={'jpg':'image/jpeg','png':'image/png','webp':'image/webp'}[name.rsplit('.',1)[1]]
            body=file.read_bytes();self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','public, max-age=31536000, immutable');self.end_headers();self.wfile.write(body);return
        if path=='/api/me':
            with connect() as c:
                user=self.auth(c)
                if not user:return self.json_response(200,{'user':None})
                shop=c.execute('SELECT * FROM shops WHERE id=?',(user['shop_id'],)).fetchone(); sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(shop['id'],)).fetchone()
                return self.json_response(200,{'user':{'email':user['email'],'owner':user['owner']},'shop':dict(shop,photos=json.loads(shop['photos'] or '[]')),'subscription':{'active':active_subscription(sub),'status':sub['status'],'paid_until':sub['paid_until']}})
        if path=='/api/client/me':
            with connect() as c:
                customer=self.client_auth(c)
                if not customer:return self.json_response(200,{'customer':None})
                favorites=[x['shop_id'] for x in c.execute('SELECT shop_id FROM favorites WHERE customer_id=?',(customer['id'],)).fetchall()]
                return self.json_response(200,{'customer':{'id':customer['id'],'name':customer['name'],'email':customer['email'],'phone':customer['phone'],'favorites':favorites}})
        if path=='/api/client/dashboard':
            with connect() as c:
                customer=self.client_auth(c)
                if not customer:return self.json_response(401,{'error':'Conectează-te la contul de client.'})
                bookings=c.execute("SELECT b.id,b.client,b.phone,b.email,b.starts,b.ends,b.status,b.service_id,s.name service_name,s.duration,COALESCE(b.price_at_booking,s.price) price,t.name staff_name,sh.name shop_name,sh.slug shop_slug,sh.city,sh.address,sh.phone shop_phone FROM bookings b JOIN shops sh ON sh.id=b.shop_id JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id WHERE b.customer_id=? AND b.status IN ('pending','confirmed','completed','cancelled') ORDER BY b.starts DESC",(customer['id'],)).fetchall()
                favorite_rows=c.execute('SELECT sh.* FROM favorites f JOIN shops sh ON sh.id=f.shop_id WHERE f.customer_id=? ORDER BY f.created DESC',(customer['id'],)).fetchall()
                favorites=[shop_public(c,shop) for shop in favorite_rows];favorites=[x for x in favorites if x]
                reviews=[dict(x) for x in c.execute('SELECT booking_id,rating,comment FROM reviews WHERE customer_id=?',(customer['id'],)).fetchall()]
                return self.json_response(200,{'customer':{'id':customer['id'],'name':customer['name'],'email':customer['email'],'phone':customer['phone']},'bookings':[dict(x) for x in bookings],'favorites':favorites,'reviews':reviews})
        if path=='/api/admin/me':
            with connect() as c:
                admin=self.admin_auth(c)
                return self.json_response(200,{'admin':{'email':admin['email']} if admin else None})
        if path=='/api/admin/dashboard':
            with connect() as c:
                if not self.admin_auth(c):return self.json_response(401,{'error':'Autentificare de administrator necesară.'})
                shops=c.execute('SELECT sh.id,sh.user_id,sh.name,sh.slug,sh.city,sh.address,sh.phone,sh.photos,sh.created,sh.listing_enabled,sh.approval_status,u.status account_status,sub.status subscription_status,sub.plan,sub.paid_until,(SELECT COUNT(*) FROM bookings b WHERE b.shop_id=sh.id) booking_count FROM shops sh JOIN users u ON u.id=sh.user_id LEFT JOIN subscriptions sub ON sub.shop_id=sh.id ORDER BY CASE sh.approval_status WHEN \'pending\' THEN 0 ELSE 1 END,sh.created DESC').fetchall()
                now=now_utc();local_today=datetime.now(TZ).date();today_start=datetime.combine(local_today,time.min,TZ).isoformat();today_end=datetime.combine(local_today+timedelta(days=1),time.min,TZ).isoformat();month_start=datetime.combine(local_today.replace(day=1),time.min,TZ).astimezone(timezone.utc).isoformat();next_month=local_today.replace(day=28)+timedelta(days=4);month_end=datetime.combine(next_month.replace(day=1),time.min,TZ).astimezone(timezone.utc).isoformat();days_30_start=datetime.combine(local_today-timedelta(days=29),time.min,TZ).isoformat();days_30_end=datetime.combine(local_today+timedelta(days=1),time.min,TZ).isoformat()
                count=lambda sql,args=():c.execute(sql,args).fetchone()[0]
                counts={'shops':count('SELECT COUNT(*) FROM shops'),'pending_approvals':count("SELECT COUNT(*) FROM shops WHERE approval_status='pending'"),'active_shops':count("SELECT COUNT(*) FROM shops sh JOIN users u ON u.id=sh.user_id JOIN subscriptions sub ON sub.shop_id=sh.id WHERE u.status='active' AND sh.approval_status='approved' AND sh.listing_enabled=1 AND sub.status IN ('active','trialing') AND julianday(sub.paid_until)>julianday(?)",(now.isoformat(),)),'suspended_shops':count("SELECT COUNT(*) FROM shops sh JOIN users u ON u.id=sh.user_id WHERE u.status='suspended'"),'hidden_shops':count("SELECT COUNT(*) FROM shops sh JOIN users u ON u.id=sh.user_id WHERE u.status='active' AND (sh.listing_enabled=0 OR sh.approval_status<>'approved')"),'active_subscriptions':count("SELECT COUNT(*) FROM subscriptions WHERE status IN ('active','trialing') AND julianday(paid_until)>julianday(?)",(now.isoformat(),)),'overdue_subscriptions':count("SELECT COUNT(*) FROM subscriptions WHERE status IN ('past_due','unpaid')"),'expired_subscriptions':count("SELECT COUNT(*) FROM subscriptions WHERE status IN ('active','trialing','past_due') AND paid_until IS NOT NULL AND julianday(paid_until)<=julianday(?)",(now.isoformat(),)),'subscriptions_expiring_7':count("SELECT COUNT(*) FROM subscriptions WHERE status IN ('active','trialing') AND julianday(paid_until)>julianday(?) AND julianday(paid_until)<=julianday(?)",(now.isoformat(),(now+timedelta(days=7)).isoformat())),'subscriptions_expiring_30':count("SELECT COUNT(*) FROM subscriptions WHERE status IN ('active','trialing') AND julianday(paid_until)>julianday(?) AND julianday(paid_until)<=julianday(?)",(now.isoformat(),(now+timedelta(days=30)).isoformat())),'customers':count('SELECT COUNT(*) FROM customers'),'new_shops_30':count('SELECT COUNT(*) FROM shops WHERE created>=?',(days_30_start,)),'new_customers_30':count('SELECT COUNT(*) FROM customers WHERE created>=?',(days_30_start,)),'bookings':count('SELECT COUNT(*) FROM bookings'),'bookings_today':count("SELECT COUNT(*) FROM bookings WHERE status<>'cancelled' AND starts>=? AND starts<?",(today_start,today_end)),'pending_confirmations':count("SELECT COUNT(*) FROM bookings WHERE status='pending'"),'failed_payments':count("SELECT COUNT(*) FROM payments WHERE status='failed' AND livemode=1"),'refund_count':count("SELECT COUNT(*) FROM refunds WHERE status IN ('succeeded','pending') AND livemode=1"),'monthly_platform_revenue':count("SELECT COALESCE(SUM(MAX(0,amount-refunded_amount)),0) FROM payments WHERE status IN ('paid','partially_refunded') AND paid_out_of_band=0 AND currency='ron' AND livemode=1 AND paid_at>=? AND paid_at<?",(month_start,month_end)),'monthly_platform_gross':count("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status IN ('paid','partially_refunded') AND paid_out_of_band=0 AND currency='ron' AND livemode=1 AND paid_at>=? AND paid_at<?",(month_start,month_end)),'monthly_refunds':count("SELECT COALESCE(SUM(amount),0) FROM refunds WHERE status='succeeded' AND currency='ron' AND livemode=1 AND created>=? AND created<?",(month_start,month_end)),'cancellations_30':count("SELECT COUNT(*) FROM bookings WHERE status='cancelled' AND created>=?",(days_30_start,)),'bookings_created_30':count('SELECT COUNT(*) FROM bookings WHERE created>=?',(days_30_start,)),'returning_customers':count('SELECT COUNT(*) FROM (SELECT customer_id FROM bookings WHERE customer_id IS NOT NULL GROUP BY customer_id HAVING COUNT(*)>1)')}
                counts['cancellation_rate_30']=round(counts['cancellations_30']/counts['bookings_created_30']*100,1) if counts['bookings_created_30'] else 0
                owners=c.execute('SELECT u.id,u.email,u.owner,u.created,u.last_login,u.status,sh.name shop_name,sh.city,sub.plan,sub.status subscription_status,sub.paid_until FROM users u JOIN shops sh ON sh.user_id=u.id LEFT JOIN subscriptions sub ON sub.shop_id=sh.id ORDER BY u.created DESC').fetchall()
                customers=c.execute('SELECT c.id,c.name,c.email,c.phone,c.created,c.last_login,c.status,COUNT(b.id) booking_count FROM customers c LEFT JOIN bookings b ON b.customer_id=c.id GROUP BY c.id ORDER BY c.created DESC').fetchall()
                admins=c.execute('SELECT id,email,created,last_login FROM admins ORDER BY created').fetchall()
                bookings=c.execute('SELECT b.id,b.client,b.phone,b.email,b.starts,b.ends,b.created,b.status,b.price_at_booking,b.promo_code,b.discount_amount,sh.name shop_name,sh.city,s.name service_name,s.duration,t.name staff_name,COALESCE(b.price_at_booking,s.price) price FROM bookings b JOIN shops sh ON sh.id=b.shop_id JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id ORDER BY b.starts DESC,b.id DESC').fetchall()
                staff_rows=c.execute('SELECT t.id,t.shop_id,t.name,t.role,t.photo,t.active,sh.name shop_name,sh.city,(SELECT COUNT(*) FROM bookings b WHERE b.staff_id=t.id) booking_count FROM staff t JOIN shops sh ON sh.id=t.shop_id ORDER BY sh.name,t.name').fetchall()
                service_rows=c.execute('SELECT s.id,s.shop_id,s.name,s.description,s.duration,s.price,s.active,sh.name shop_name,sh.city,(SELECT COUNT(*) FROM bookings b WHERE b.service_id=s.id) booking_count FROM services s JOIN shops sh ON sh.id=s.shop_id ORDER BY sh.name,s.name').fetchall()
                review_rows=c.execute('SELECT r.id,r.booking_id,r.shop_id,r.rating,r.comment,r.created,r.is_visible,b.client,s.name service_name,sh.name shop_name FROM reviews r JOIN bookings b ON b.id=r.booking_id JOIN services s ON s.id=b.service_id JOIN shops sh ON sh.id=r.shop_id ORDER BY r.created DESC LIMIT 500').fetchall()
                promo_codes=c.execute('SELECT * FROM promo_codes ORDER BY created DESC').fetchall()
                subscriptions=c.execute('SELECT sh.id shop_id,sh.name shop_name,sh.city,sub.status,sub.plan,sub.current_period_start,sub.paid_until,sub.stripe_subscription_id,sub.payment_failed_at,sub.cancel_at_period_end,sub.cancel_at FROM shops sh LEFT JOIN subscriptions sub ON sub.shop_id=sh.id ORDER BY sub.paid_until DESC').fetchall()
                payments=c.execute('SELECT p.*,sh.name shop_name FROM payments p LEFT JOIN shops sh ON sh.id=p.shop_id ORDER BY COALESCE(p.paid_at,p.created) DESC LIMIT 500').fetchall()
                refunds=c.execute('SELECT r.*,sh.name shop_name FROM refunds r LEFT JOIN shops sh ON sh.id=r.shop_id ORDER BY r.created DESC LIMIT 500').fetchall()
                subscription_history=c.execute('SELECT h.*,sh.name shop_name FROM subscription_history h LEFT JOIN shops sh ON sh.id=h.shop_id ORDER BY h.event_created DESC LIMIT 200').fetchall()
                subscription_rows=[]
                for item in subscriptions:
                    sub=dict(item);plan=str(sub.get('plan') or 'free').lower();raw_status=sub.get('status') or 'inactive';sub['price_monthly']=PLAN_PRICES.get(plan,0);sub['stripe_status']=raw_status
                    renewal=None
                    try:
                        renewal=datetime.fromisoformat(str(sub['paid_until']).replace('Z','+00:00')) if sub.get('paid_until') else None
                        if renewal and renewal.tzinfo is None:renewal=renewal.replace(tzinfo=timezone.utc)
                        sub['days_to_renewal']=max(0,int((renewal-now_utc()).total_seconds()/86400+0.999999)) if renewal and renewal>now_utc() else None
                    except (TypeError,ValueError):sub['days_to_renewal']=None
                    if raw_status=='past_due':
                        try:
                            failed=datetime.fromisoformat(str(sub['payment_failed_at']).replace('Z','+00:00')) if sub.get('payment_failed_at') else None
                            if failed and failed.tzinfo is None:failed=failed.replace(tzinfo=timezone.utc)
                            sub['status_label']='grace' if failed and SUBSCRIPTION_GRACE_DAYS and now_utc()<failed+timedelta(days=SUBSCRIPTION_GRACE_DAYS) else 'unpaid'
                        except (TypeError,ValueError):sub['status_label']='unpaid'
                    elif raw_status in ('active','trialing') and sub.get('paid_until'):
                        try:sub['status_label']='expired' if renewal and renewal<=now_utc() else 'active'
                        except (TypeError,ValueError):sub['status_label']='active'
                    elif raw_status in ('unpaid','incomplete','incomplete_expired','paused'):sub['status_label']='unpaid' if raw_status=='unpaid' else 'pending'
                    elif raw_status in ('canceled','cancelled'):sub['status_label']='canceled'
                    else:sub['status_label']=raw_status
                    if raw_status not in ('active','trialing','past_due') or not renewal or renewal<=now_utc():sub['days_to_renewal']=None
                    subscription_rows.append(sub)
                counts['active_subscriptions']=sum(1 for sub in subscription_rows if sub.get('status_label')=='active')
                counts['expired_subscriptions']=sum(1 for sub in subscription_rows if sub.get('status_label')=='expired')
                for horizon in (7,30):
                    counts[f'subscriptions_expiring_{horizon}']=sum(1 for sub in subscription_rows if sub.get('status_label') in ('active','grace') and sub.get('days_to_renewal') is not None and sub['days_to_renewal']<=horizon)
                booking_history=c.execute("SELECT a.*,b.client,sh.name shop_name,CASE WHEN a.actor_type='barber' THEN COALESCE(u.owner,u.email) WHEN a.actor_type='client' THEN COALESCE(c.name,b.client) ELSE a.actor_type END actor_name FROM booking_audit a LEFT JOIN bookings b ON b.id=a.booking_id LEFT JOIN shops sh ON sh.id=a.shop_id LEFT JOIN users u ON a.actor_type='barber' AND u.id=a.actor_id LEFT JOIN customers c ON a.actor_type='client' AND c.id=a.actor_id ORDER BY a.created DESC LIMIT 500").fetchall()
                daily_7=[];daily_30=[]
                for offset in range(29,-1,-1):
                    day=local_today-timedelta(days=offset);start=datetime.combine(day,time.min,TZ).isoformat();end=datetime.combine(day+timedelta(days=1),time.min,TZ).isoformat();point={'date':day.isoformat(),'count':count("SELECT COUNT(*) FROM bookings WHERE starts>=? AND starts<? AND status IN ('confirmed','completed')",(start,end))}
                    daily_30.append(point)
                    if offset<7:daily_7.append(point)
                monthly=[]
                for offset in range(5,-1,-1):
                    month_index=local_today.year*12+local_today.month-1-offset;year,month=divmod(month_index,12);month+=1;start=datetime(year,month,1,tzinfo=TZ);end=datetime(year+((month)%12==0),month%12+1,1,tzinfo=TZ);start_utc=start.astimezone(timezone.utc).isoformat();end_utc=end.astimezone(timezone.utc).isoformat()
                    monthly.append({'month':f'{year:04d}-{month:02d}','bookings':count("SELECT COUNT(*) FROM bookings WHERE starts>=? AND starts<? AND status IN ('confirmed','completed')",(start_utc,end_utc)),'new_customers':count('SELECT COUNT(*) FROM customers WHERE created>=? AND created<?',(start_utc,end_utc)),'revenue':count("SELECT COALESCE(SUM(MAX(0,amount-refunded_amount)),0) FROM payments WHERE status IN ('paid','partially_refunded') AND stripe_subscription_id IS NOT NULL AND paid_out_of_band=0 AND currency='ron' AND livemode=1 AND paid_at>=? AND paid_at<?",(start_utc,end_utc))})
                service_mix=[dict(x) for x in c.execute("SELECT s.name service_name,COUNT(b.id) count FROM services s LEFT JOIN bookings b ON b.service_id=s.id AND b.status IN ('confirmed','completed') GROUP BY s.id ORDER BY count DESC,s.name LIMIT 6").fetchall()]
                review_summary=c.execute('SELECT COUNT(*) count,AVG(rating) average FROM reviews WHERE is_visible=1').fetchone()
                hidden_reviews=count('SELECT COUNT(*) FROM reviews WHERE is_visible=0')
                active_services=count('SELECT COUNT(*) FROM services WHERE active=1'); active_staff=count('SELECT COUNT(*) FROM staff WHERE active=1')
                capacity_minutes=booked_minutes=0
                for person in c.execute('SELECT id,weekly_schedule FROM staff WHERE active=1').fetchall():
                    try:schedule=json.loads(person['weekly_schedule'] or '{}')
                    except (TypeError,ValueError):schedule={}
                    for offset in range(30):
                        work_day=local_today-timedelta(days=offset);hours=schedule.get(str(work_day.weekday()))
                        if not hours or len(hours)!=2:continue
                        try:capacity_minutes+=max(0,int((time.fromisoformat(hours[1]).hour*60+time.fromisoformat(hours[1]).minute)-(time.fromisoformat(hours[0]).hour*60+time.fromisoformat(hours[0]).minute)))
                        except (TypeError,ValueError):continue
                    booked_minutes+=count("SELECT COALESCE(SUM((julianday(ends)-julianday(starts))*1440),0) FROM bookings WHERE staff_id=? AND status IN ('confirmed','completed') AND starts>=? AND starts<?",(person['id'],(local_today-timedelta(days=29)).isoformat(),(local_today+timedelta(days=1)).isoformat()))
                occupancy_rate=round(min(100,booked_minutes/capacity_minutes*100),1) if capacity_minutes else None
                top_services=[dict(x) for x in c.execute("SELECT s.name service_name,COUNT(*) count FROM bookings b JOIN services s ON s.id=b.service_id WHERE b.created>=? AND b.status<>'cancelled' GROUP BY s.id ORDER BY count DESC LIMIT 5",(days_30_start,)).fetchall()]
                top_shops=[dict(x) for x in c.execute("SELECT sh.name shop_name,COUNT(*) count FROM bookings b JOIN shops sh ON sh.id=b.shop_id WHERE b.created>=? AND b.status<>'cancelled' GROUP BY sh.id ORDER BY count DESC LIMIT 5",(days_30_start,)).fetchall()]
                activity=[]
                activity.extend(dict(x,source='admin') for x in c.execute('SELECT a.id,a.admin_email,a.action,a.shop_id,a.details,a.created,sh.name shop_name FROM admin_audit a LEFT JOIN shops sh ON sh.id=a.shop_id ORDER BY a.created DESC LIMIT 15').fetchall())
                activity.extend({'id':'signup-'+str(x['id']),'action':'shop_registered','shop_id':x['id'],'shop_name':x['name'],'created':x['created'],'source':'system'} for x in c.execute('SELECT id,name,created FROM shops WHERE created>=? ORDER BY created DESC LIMIT 10',( (now-timedelta(days=30)).isoformat(),)).fetchall())
                activity.extend({'id':'expired-'+str(x['shop_id']),'action':'subscription_expired','shop_id':x['shop_id'],'shop_name':x['shop_name'],'created':x['paid_until'],'source':'system'} for x in c.execute("SELECT sub.shop_id,sh.name shop_name,sub.paid_until FROM subscriptions sub JOIN shops sh ON sh.id=sub.shop_id WHERE sub.status IN ('active','trialing','past_due') AND sub.paid_until IS NOT NULL AND julianday(sub.paid_until)<=julianday(?) AND julianday(sub.paid_until)>=julianday(?) ORDER BY sub.paid_until DESC LIMIT 10",(now.isoformat(),(now-timedelta(days=30)).isoformat())).fetchall())
                activity.extend({'id':'failed-'+str(x['id']),'action':'payment_failed','shop_id':x['shop_id'],'shop_name':x['shop_name'],'created':x['created'],'source':'system'} for x in c.execute("SELECT p.id,p.shop_id,sh.name shop_name,p.created FROM payments p LEFT JOIN shops sh ON sh.id=p.shop_id WHERE p.status='failed' AND p.livemode=1 ORDER BY p.created DESC LIMIT 10").fetchall())
                activity.extend({'id':'booking-'+str(x['id']),'action':x['action'],'shop_id':x['shop_id'],'shop_name':x['shop_name'],'created':x['created'],'actor_type':x['actor_type'],'source':'booking'} for x in c.execute("SELECT a.id,a.action,a.shop_id,sh.name shop_name,a.created,a.actor_type FROM booking_audit a LEFT JOIN shops sh ON sh.id=a.shop_id WHERE a.action='booking_cancelled' ORDER BY a.created DESC LIMIT 10").fetchall())
                activity=sorted(activity,key=lambda x:x.get('created') or '',reverse=True)[:30]
                return self.json_response(200,{'counts':{**counts,'active_services':active_services,'active_staff':active_staff,'visible_reviews':review_summary['count'],'average_rating':round(review_summary['average'],2) if review_summary['average'] else None,'hidden_reviews':hidden_reviews,'occupancy_rate_30':occupancy_rate},'shops':[dict(x) for x in shops],'owners':[dict(x) for x in owners],'customers':[dict(x) for x in customers],'admins':[dict(x) for x in admins],'staff':[dict(x) for x in staff_rows],'services':[dict(x) for x in service_rows],'reviews':[dict(x) for x in review_rows],'promo_codes':[dict(x) for x in promo_codes],'bookings':[dict(x) for x in bookings],'subscriptions':subscription_rows,'subscription_history':[dict(x) for x in subscription_history],'subscription_grace_days':SUBSCRIPTION_GRACE_DAYS,'payments':[dict(x) for x in payments],'refunds':[dict(x) for x in refunds],'booking_history':[dict(x) for x in booking_history],'activity':activity,'booking_activity':daily_7,'booking_activity_30':daily_30,'monthly':monthly,'service_mix':service_mix,'top_services':top_services,'top_shops':top_shops,'integrations':{'stripe':bool(os.environ.get('STRIPE_SECRET_KEY')),'email':email_configured(),'sms':bool(os.environ.get('TWILIO_ACCOUNT_SID') and os.environ.get('TWILIO_AUTH_TOKEN') and os.environ.get('TWILIO_FROM'))}})
        if path=='/api/public/shops':
            term=search_norm(q.get('q',[''])[0]).strip()
            with connect() as c:
                rows=c.execute('SELECT * FROM shops ORDER BY name').fetchall(); found=[]
                for row in rows:
                    item=shop_public(c,row)
                    if item and (not term or term in search_norm(' '.join([item['name'],item['city'],item['address'],item['tagline']]+[x['name'] for x in item['services']]+[x['name'] for x in item['team']]))): found.append(item)
                return self.json_response(200,{'shops':found})
        if path.startswith('/api/public/shops/'):
            parts=path.split('/'); slug=parts[4] if len(parts)>4 else ''
            with connect() as c:
                shop=c.execute('SELECT * FROM shops WHERE slug=?',(slug,)).fetchone()
                if not shop:return self.json_response(404,{'error':'Frizeria nu a fost găsită.'})
                public=shop_public(c,shop)
                if not public:return self.json_response(404,{'error':'Profil indisponibil.'})
                if len(parts)>5 and parts[5]=='availability':
                    try: day=date.fromisoformat(q.get('date',[''])[0]); service_id=int(q.get('service',['0'])[0]); staff_id=int(q.get('staff',['0'])[0])
                    except Exception:return self.json_response(400,{'error':'Alege data, serviciul și frizerul.'})
                    service=c.execute('SELECT * FROM services WHERE id=? AND shop_id=? AND active=1',(service_id,shop['id'])).fetchone()
                    staff=c.execute('SELECT * FROM staff WHERE id=? AND shop_id=? AND active=1',(staff_id,shop['id'])).fetchone()
                    if not service or not staff:return self.json_response(400,{'error':'Serviciul sau frizerul nu este disponibil.'})
                    schedule=json.loads(staff['weekly_schedule'] or '{}'); hours=schedule.get(str(day.weekday()))
                    slots=[]
                    if hours and len(hours)==2:
                        start=datetime.combine(day,time.fromisoformat(hours[0]),TZ); end=datetime.combine(day,time.fromisoformat(hours[1]),TZ)
                        rows=c.execute("SELECT starts,ends FROM bookings WHERE staff_id=? AND status IN ('confirmed','pending') AND julianday(starts)<julianday(?) AND julianday(ends)>julianday(?)",(staff_id,end.isoformat(),start.isoformat())).fetchall()
                        busy=[(datetime.fromisoformat(x['starts']).astimezone(TZ),datetime.fromisoformat(x['ends']).astimezone(TZ)) for x in rows]
                        cursor=start
                        while cursor+timedelta(minutes=service['duration'])<=end:
                            finish=cursor+timedelta(minutes=service['duration'])
                            if cursor>datetime.now(TZ) and all(finish<=a or cursor>=b for a,b in busy):slots.append(cursor.strftime('%H:%M'))
                            cursor+=timedelta(minutes=15)
                    return self.json_response(200,{'slots':slots})
                return self.json_response(200,{'shop':public})
        if path.startswith('/api/manage/'):
            with connect() as c:
                user=self.auth(c)
                if not user:return self.json_response(401,{'error':'Conectează-te pentru a continua.'})
                shop=c.execute('SELECT * FROM shops WHERE id=?',(user['shop_id'],)).fetchone()
                if path=='/api/manage/dashboard':
                    svc=c.execute('SELECT * FROM services WHERE shop_id=? ORDER BY id',(shop['id'],)).fetchall(); team=c.execute('SELECT * FROM staff WHERE shop_id=? ORDER BY id',(shop['id'],)).fetchall(); bookings=c.execute("SELECT b.*,s.name service_name,s.duration,COALESCE(b.price_at_booking,s.price) price,t.name staff_name FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id WHERE b.shop_id=? AND b.status IN ('pending','confirmed','completed','cancelled') ORDER BY b.starts",(shop['id'],)).fetchall(); sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(shop['id'],)).fetchone(); settings=c.execute('SELECT * FROM settings WHERE shop_id=?',(shop['id'],)).fetchone()
                    if sub and sub['plan'] in PLAN_PRICES and sub['status'] in ('active','trialing') and not sub['paid_until'] and sub['stripe_subscription_id']:
                        try:
                            details=stripe_subscription_details(sub['stripe_subscription_id'])
                            if details and details.get('current_period_end'):
                                paid_until=datetime.fromtimestamp(details['current_period_end'],timezone.utc).isoformat()
                                c.execute('UPDATE subscriptions SET status=?,paid_until=? WHERE shop_id=?',(details.get('status',sub['status']),paid_until,shop['id']))
                                sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(shop['id'],)).fetchone()
                        except Exception as e:print('Stripe subscription reconciliation failed:',repr(e))
                    paid=active_subscription(sub)
                    shop_data=dict(shop)
                    try:shop_data['photos']=json.loads(shop_data.get('photos') or '[]')
                    except (TypeError,ValueError):shop_data['photos']=[]
                    if not isinstance(shop_data['photos'],list):shop_data['photos']=[]
                    notes={x['client_key']:x['note'] for x in c.execute('SELECT client_key,note FROM client_notes WHERE shop_id=?',(shop['id'],)).fetchall()} if paid else {}
                    booking_data=[]
                    if paid:
                        for item in bookings:
                            booking=dict(item);phone_key=hashlib.sha256(normalize_ro_mobile(booking.get('phone','')).encode()).hexdigest() if booking.get('phone') else ''
                            booking['client_note']=notes.get(phone_key,'');booking_data.append(booking)
                    return self.json_response(200,{'shop':shop_data,'services':[dict(x) for x in svc],'team':[dict(x) for x in team],'bookings':booking_data,'subscription':{'active':paid,'status':sub['status'],'plan':sub['plan'],'paid_until':sub['paid_until'],'grace_days':SUBSCRIPTION_GRACE_DAYS},'notifications':dict(settings) if settings else {}})
        if path.startswith('/api/'): return self.json_response(404,{'error':'Nu am găsit pagina.'})
        if path not in ('/','/index.html','/client.js','/features.js','/styles.css'):return self.send_error(404)
        return super().do_GET()
    def do_GET(self):
        try:
            path=urllib.parse.urlparse(self.path).path
            if path.startswith('/api/') and not self.enforce_rate_limit('get:'+path,240,60):return
            self.get()
        except Exception as e: print('GET error:',repr(e)); self.json_response(500,{'error':'A apărut o eroare. Încearcă din nou.'})
    def do_POST(self):
        path=urllib.parse.urlparse(self.path).path
        try:
            if path.startswith('/api/'):
                if path=='/api/webhooks/stripe':limit,window=600,60
                elif path in ('/api/auth/login','/api/admin/login','/api/client/auth/login','/api/auth/register','/api/client/auth/register','/api/auth/password-reset/request','/api/auth/password-reset/confirm'):limit,window=30,900
                else:limit,window=120,60
                if not self.enforce_rate_limit('post:'+path,limit,window):return
            if path!='/api/webhooks/stripe' and not self.mutation_origin_valid():return self.json_response(403,{'error':'Originea cererii nu este permisă.'})
            if path=='/api/auth/register':return self.register(self.body_json())
            if path=='/api/auth/login':return self.login(self.body_json())
            if path=='/api/auth/password-reset/request':return self.request_password_reset(self.body_json())
            if path=='/api/auth/password-reset/confirm':return self.confirm_password_reset(self.body_json())
            if path=='/api/client/auth/register':return self.client_register(self.body_json())
            if path=='/api/client/auth/login':return self.client_login(self.body_json())
            if path=='/api/admin/login':return self.admin_login(self.body_json())
            if path=='/api/admin/logout':
                with connect() as c:
                    admin=self.admin_auth(c)
                    if admin:
                        c.execute('DELETE FROM admin_sessions WHERE admin_id=?',(admin['id'],))
                        c.execute('INSERT INTO admin_audit(admin_email,action,details,created) VALUES(?,?,?,?)',(admin['email'],'admin_logout','{}',iso_now()))
                return self.json_response(200,{'ok':True},self.clear_admin_session())
            if path=='/api/client/auth/logout':
                with connect() as c:
                    customer=self.client_auth(c)
                    if customer:c.execute('DELETE FROM client_sessions WHERE customer_id=?',(customer['id'],))
                return self.json_response(200,{'ok':True},self.clear_client_session())
            if path=='/api/auth/logout':
                with connect() as c:
                    user=self.auth(c)
                    if user:c.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
                return self.json_response(200,{'ok':True},self.clear_session())
            if path=='/api/public/bookings':return self.create_booking(self.body_json())
            if path=='/api/public/bookings/reschedule/details':return self.public_reschedule_details(self.body_json())
            if path=='/api/public/bookings/reschedule':return self.reschedule_public_booking(self.body_json())
            if path=='/api/public/bookings/cancel':return self.cancel_public_booking(self.body_json())
            if path=='/api/client/favorites':return self.client_favorite(self.body_json())
            if path=='/api/client/bookings/cancel':return self.client_cancel_booking(self.body_json())
            if path=='/api/client/reviews':return self.create_review(self.body_json())
            if path=='/api/client/account/delete':return self.delete_client_account()
            if path=='/api/admin/listing':return self.admin_listing(self.body_json())
            if path=='/api/admin/account':return self.admin_account(self.body_json())
            if path=='/api/admin/shop':return self.admin_shop_update(self.body_json())
            if path=='/api/admin/approval':return self.admin_shop_approval(self.body_json())
            if path=='/api/admin/staff':return self.admin_staff_toggle(self.body_json())
            if path=='/api/admin/service':return self.admin_service_toggle(self.body_json())
            if path=='/api/admin/review':return self.admin_review_visibility(self.body_json())
            if path=='/api/admin/promo-codes':return self.admin_promo_codes(self.body_json())
            if path=='/api/manage/photos':return self.upload_photo(self.body_json())
            if path=='/api/manage/staff-photo':return self.upload_staff_photo(self.body_json())
            if path=='/api/manage/bookings/confirm':return self.confirm_booking(self.body_json())
            if path=='/api/manage/bookings/cancel':return self.cancel_booking(self.body_json())
            if path=='/api/manage/bookings/complete':return self.complete_booking(self.body_json())
            if path=='/api/manage/client-note':return self.save_client_note(self.body_json())
            if path=='/api/billing/checkout':return self.checkout()
            if path=='/api/webhooks/stripe':return self.stripe_webhook()
            return self.json_response(404,{'error':'Nu am găsit ruta.'})
        except ValueError as e:return self.json_response(400,{'error':str(e)})
        except Exception as e:print('POST error:',repr(e));detail=(json.loads(e.read()).get('error',{}).get('message','') if isinstance(e,urllib.error.HTTPError) else '');return self.json_response(500,{'error':('Stripe: '+detail) if detail else 'A apărut o eroare. Încearcă din nou.'})
    def do_PUT(self):
        path=urllib.parse.urlparse(self.path).path
        try:
            if path.startswith('/api/') and not self.enforce_rate_limit('put:'+path,120,60):return
            if not self.mutation_origin_valid():return self.json_response(403,{'error':'Originea cererii nu este permisă.'})
            data=self.body_json()
            with connect() as c:
                user=self.auth(c)
                if not user:return self.json_response(401,{'error':'Conectează-te pentru a continua.'})
                sid=user['shop_id']
                if path=='/api/manage/shop':
                    fields={k:data[k] for k in ('name','city','address','phone','tagline') if k in data}
                    removed_photos=[]
                    if 'photos' in data:
                        photos=data['photos']
                        if not isinstance(photos,list) or len(photos)>8 or any(not isinstance(x,str) or not re.fullmatch(r'/media/[0-9a-f]{32}\.(?:jpg|png|webp)',x) for x in photos):return self.json_response(400,{'error':'Verifică fotografiile selectate.'})
                        current=c.execute('SELECT photos FROM shops WHERE id=?',(sid,)).fetchone()
                        removed_photos=set(json.loads(current['photos'] or '[]'))-set(photos)
                        fields['photos']=json.dumps(photos)
                    if fields:c.execute('UPDATE shops SET '+','.join(f'{k}=?' for k in fields)+' WHERE id=?',(*fields.values(),sid))
                    for url in removed_photos:
                        name=url.rsplit('/',1)[-1]
                        if re.fullmatch(r'[0-9a-f]{32}\.(?:jpg|png|webp)',name):(MEDIA_DIR/name).unlink(missing_ok=True)
                elif path=='/api/manage/services':
                    ids=[]
                    for x in data.get('services',[]):
                        vals=(x['name'],x.get('description',''),max(10,int(x['duration'])),max(0,int(x['price'])),int(bool(x.get('active',1))))
                        if x.get('id'):
                            c.execute('UPDATE services SET name=?,description=?,duration=?,price=?,active=? WHERE id=? AND shop_id=?',(*vals,int(x['id']),sid)); ids.append(int(x['id']))
                        else:
                            cur=c.execute('INSERT INTO services(shop_id,name,description,duration,price,active) VALUES(?,?,?,?,?,?)',(sid,*vals));ids.append(cur.lastrowid)
                    if ids:c.execute('UPDATE services SET active=0 WHERE shop_id=? AND id NOT IN ('+','.join('?' for _ in ids)+')',(sid,*ids))
                    else:c.execute('UPDATE services SET active=0 WHERE shop_id=?',(sid,))
                elif path=='/api/manage/team':
                    sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(sid,)).fetchone()
                    team=data.get('team',[])
                    if not (sub and sub['plan']=='business' and active_subscription(sub)) and sum(bool(x.get('active',1)) for x in team)>1:
                        return self.json_response(403,{'error':'Planul FREE și PRO include un singur barber. Treci la BUSINESS pentru echipă extinsă.'})
                    ids=[]
                    for x in team:
                        weekly=x.get('weekly_schedule',{})
                        if isinstance(weekly,str):
                            try:weekly=json.loads(weekly)
                            except Exception:weekly={}
                        photo=str(x.get('photo','') or '')
                        if photo and not re.fullmatch(r'/media/[0-9a-f]{32}\.(?:jpg|png|webp)',photo):return self.json_response(400,{'error':'Verifică fotografia frizerului.'})
                        vals=(x['name'],x.get('role','Frizer'),json.dumps(weekly),int(bool(x.get('active',1))),photo)
                        if x.get('id'):
                            c.execute('UPDATE staff SET name=?,role=?,weekly_schedule=?,active=?,photo=? WHERE id=? AND shop_id=?',(*vals,int(x['id']),sid));ids.append(int(x['id']))
                        else:
                            cur=c.execute('INSERT INTO staff(shop_id,name,role,weekly_schedule,active,photo) VALUES(?,?,?,?,?,?)',(sid,*vals));ids.append(cur.lastrowid)
                    if ids:c.execute('UPDATE staff SET active=0 WHERE shop_id=? AND id NOT IN ('+','.join('?' for _ in ids)+')',(sid,*ids))
                    else:c.execute('UPDATE staff SET active=0 WHERE shop_id=?',(sid,))
                elif path=='/api/manage/notifications':
                    sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(sid,)).fetchone()
                    if not active_subscription(sub):return self.json_response(403,{'error':'Notificările sunt incluse în planurile PRO și BUSINESS. Activează un abonament pentru a le configura.'})
                    c.execute('INSERT INTO settings(shop_id,notification_email,notification_sms) VALUES(?,?,?) ON CONFLICT(shop_id) DO UPDATE SET notification_email=excluded.notification_email,notification_sms=excluded.notification_sms',(sid,int(bool(data.get('email'))),int(bool(data.get('sms')))))
                else:return self.json_response(404,{'error':'Nu am găsit ruta.'})
            return self.json_response(200,{'ok':True})
        except Exception as e:print('PUT error:',repr(e));return self.json_response(400,{'error':'Verifică datele introduse.'})
    def register(self,d):
        required=['owner','email','password','name','city','address','phone']
        if any(not str(d.get(k,'')).strip() for k in required):raise ValueError('Completează toate câmpurile obligatorii.')
        if len(d['password'])<10:raise ValueError('Parola trebuie să aibă cel puțin 10 caractere.')
        if '@' not in d['email'] or len(d['phone'])<8:raise ValueError('Verifică adresa de e-mail și telefonul.')
        with connect() as c:
            if c.execute('SELECT 1 FROM users WHERE email=?',(d['email'].lower().strip(),)).fetchone():return self.json_response(409,{'error':'Există deja un cont cu acest e-mail.'})
            slug=slugify(d['name']); base=slug; n=2
            while c.execute('SELECT 1 FROM shops WHERE slug=?',(slug,)).fetchone():slug=f'{base}-{n}';n+=1
            cur=c.execute('INSERT INTO users(email,password,owner,created) VALUES(?,?,?,?)',(d['email'].lower().strip(),password_hash(d['password']),d['owner'].strip(),iso_now())); uid=cur.lastrowid
            cur=c.execute("INSERT INTO shops(user_id,name,slug,city,address,phone,tagline,created,approval_status) VALUES(?,?,?,?,?,?,?,?, 'pending')",(uid,d['name'].strip(),slug,d['city'].strip(),d['address'].strip(),d['phone'].strip(),d.get('tagline',''),iso_now())); sid=cur.lastrowid
            c.execute("INSERT INTO subscriptions(shop_id,status,plan) VALUES(?,'active','free')",(sid,)); c.execute('INSERT INTO settings(shop_id) VALUES(?)',(sid,))
            c.execute('INSERT INTO services(shop_id,name,description,duration,price,active) VALUES(?,?,?,?,?,1)',(sid,'Tuns clasic','Tuns și styling',30,60))
            # Add an owner barber with a weekday schedule; services remain owner-configurable.
            sched={str(i):['09:00','19:00'] for i in range(6)}
            c.execute('INSERT INTO staff(shop_id,name,role,weekly_schedule) VALUES(?,?,?,?)',(sid,d['owner'].strip(),'Frizer',json.dumps(sched)))
        return self.json_response(201,{'ok':True,'slug':slug},self.set_session(uid))
    def upload_photo(self,d):
        with connect() as c:
            user=self.auth(c)
            if not user:return self.json_response(401,{'error':'Conectează-te pentru a continua.'})
        data=str(d.get('data',''))
        match=re.fullmatch(r'data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/]+=*)',data)
        if not match:raise ValueError('Încarcă o fotografie JPG, PNG sau WebP.')
        raw=base64.b64decode(match.group(2),validate=True)
        if not raw or len(raw)>1_500_000:raise ValueError('Fotografia trebuie să aibă maximum 1,5 MB după comprimare.')
        ext={'jpeg':'jpg','png':'png','webp':'webp'}[match.group(1)]
        signatures={'jpg':raw.startswith(b'\xff\xd8\xff'),'png':raw.startswith(b'\x89PNG\r\n\x1a\n'),'webp':len(raw)>12 and raw.startswith(b'RIFF') and raw[8:12]==b'WEBP'}
        if not signatures[ext]:raise ValueError('Fișierul nu pare a fi o imagine validă.')
        name=uuid.uuid4().hex+'.'+ext;MEDIA_DIR.mkdir(parents=True,exist_ok=True);(MEDIA_DIR/name).write_bytes(raw);url='/media/'+name
        with connect() as c:
            shop=c.execute('SELECT photos FROM shops WHERE user_id=?',(user['id'],)).fetchone();photos=json.loads(shop['photos'] or '[]')
            if len(photos)>=8:
                (MEDIA_DIR/name).unlink(missing_ok=True)
                raise ValueError('Poți adăuga maximum 8 fotografii.')
            photos.append(url);c.execute('UPDATE shops SET photos=? WHERE user_id=?',(json.dumps(photos),user['id']))
        return self.json_response(201,{'ok':True,'url':url,'photos':photos})
    def upload_staff_photo(self,d):
        with connect() as c:
            user=self.auth(c)
            if not user:return self.json_response(401,{'error':'Conectează-te pentru a continua.'})
            staff=c.execute('SELECT id,photo FROM staff WHERE id=? AND shop_id=?',(int(d.get('staff_id',0)),user['shop_id'])).fetchone()
            if not staff:return self.json_response(404,{'error':'Frizerul nu a fost găsit în echipa ta.'})
        data=str(d.get('data',''))
        match=re.fullmatch(r'data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/]+=*)',data)
        if not match:raise ValueError('Încarcă o fotografie JPG, PNG sau WebP.')
        raw=base64.b64decode(match.group(2),validate=True)
        if not raw or len(raw)>1_500_000:raise ValueError('Fotografia trebuie să aibă maximum 1,5 MB după comprimare.')
        ext={'jpeg':'jpg','png':'png','webp':'webp'}[match.group(1)]
        signatures={'jpg':raw.startswith(b'\xff\xd8\xff'),'png':raw.startswith(b'\x89PNG\r\n\x1a\n'),'webp':len(raw)>12 and raw.startswith(b'RIFF') and raw[8:12]==b'WEBP'}
        if not signatures[ext]:raise ValueError('Fișierul nu pare a fi o imagine validă.')
        name=uuid.uuid4().hex+'.'+ext;MEDIA_DIR.mkdir(parents=True,exist_ok=True);(MEDIA_DIR/name).write_bytes(raw);url='/media/'+name
        with connect() as c:c.execute('UPDATE staff SET photo=? WHERE id=? AND shop_id=?',(url,staff['id'],user['shop_id']))
        return self.json_response(201,{'ok':True,'url':url})
    def login(self,d):
        email=str(d.get('email','')).lower().strip();key=login_limit_key('barber',email)
        with connect() as c:
            if login_is_limited(c,key):return self.json_response(429,{'error':'Prea multe încercări. Așteaptă 15 minute și încearcă din nou.'})
            user=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
            if not user or user['status']!='active' or not password_ok(str(d.get('password','')),user['password']):login_failure(c,key);return self.json_response(401,{'error':'E-mailul sau parola nu sunt corecte.'})
            login_success(c,key);c.execute('UPDATE users SET last_login=? WHERE id=?',(iso_now(),user['id']))
        return self.json_response(200,{'ok':True},self.set_session(user['id']))
    def request_password_reset(self,d):
        email=str(d.get('email','')).strip().lower()
        generic={'ok':True,'message':'Dacă adresa este asociată unui cont și e-mailul poate fi trimis, vei primi un link de resetare.'}
        if len(email)>254 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email):return self.json_response(200,generic)
        email_hash=hashlib.sha256(email.encode()).hexdigest();now=now_utc();token='';user_id=None
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM password_reset_limits WHERE requested<?',((now-timedelta(days=30)).isoformat(),))
            previous=c.execute('SELECT requested FROM password_reset_limits WHERE email_hash=?',(email_hash,)).fetchone()
            allowed=not previous or now-datetime.fromisoformat(previous['requested'])>=timedelta(minutes=5)
            c.execute('INSERT INTO password_reset_limits(email_hash,requested) VALUES(?,?) ON CONFLICT(email_hash) DO UPDATE SET requested=excluded.requested',(email_hash,now.isoformat()))
            if allowed:
                user=c.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()
                mail_ready=email_configured() and bool(os.environ.get('PUBLIC_URL'))
                print(f'Password reset request received: barber_account_match={bool(user)} email_configured={mail_ready} provider={email_provider() if mail_ready else "none"}',flush=True)
                if user and mail_ready:
                    user_id=user['id'];token=secrets.token_urlsafe(32);token_hash=hashlib.sha256(token.encode()).hexdigest()
                    c.execute('DELETE FROM password_resets WHERE user_id=? OR expires<=?',(user_id,now.isoformat()))
                    c.execute('INSERT INTO password_resets(token_hash,user_id,expires,created) VALUES(?,?,?,?)',(token_hash,user_id,(now+timedelta(minutes=30)).isoformat(),now.isoformat()))
            else:
                print('Password reset request suppressed: rate limit',flush=True)
        if user_id:
            reset_url=os.environ['PUBLIC_URL'].rstrip('/')+'/#resetare-parola/'+token
            threading.Thread(target=self.send_password_reset_email,args=(email,reset_url),daemon=True).start()
        return self.json_response(200,generic)
    @staticmethod
    def send_password_reset_email(email,reset_url):
        try:
            provider=send_email(email,'Resetarea parolei TunsPro',f'Am primit o cerere de resetare a parolei contului tău TunsPro. Deschide linkul în următoarele 30 de minute pentru a alege o parolă nouă:\n\n{reset_url}\n\nDacă nu ai solicitat resetarea, ignoră acest mesaj. Parola nu se schimbă până când nu confirmi linkul.')
            print(f'Password reset email accepted by {provider}',flush=True)
        except Exception as e:
            print(f'Password reset email failed: {type(e).__name__}: {e}',flush=True)
    def confirm_password_reset(self,d):
        token=str(d.get('token','')).strip();password=str(d.get('password',''))
        if len(token)<30:return self.json_response(400,{'error':'Linkul de resetare nu este valid sau a expirat. Cere un link nou.'})
        if len(password)<10 or len(password)>200:return self.json_response(400,{'error':'Alege o parolă între 10 și 200 de caractere.'})
        token_hash=hashlib.sha256(token.encode()).hexdigest();now=now_utc();user_id=None;email=None
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT r.user_id,r.expires,u.email FROM password_resets r JOIN users u ON u.id=r.user_id WHERE r.token_hash=?',(token_hash,)).fetchone()
            if not row or datetime.fromisoformat(row['expires'])<=now:
                if row:c.execute('DELETE FROM password_resets WHERE token_hash=?',(token_hash,))
                return self.json_response(400,{'error':'Linkul de resetare nu este valid sau a expirat. Cere un link nou.'})
            user_id=row['user_id'];email=row['email']
            c.execute('UPDATE users SET password=? WHERE id=?',(password_hash(password),user_id))
            c.execute('DELETE FROM password_resets WHERE user_id=?',(user_id,))
            c.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
        if email and email_configured():
            threading.Thread(target=self.send_password_changed_email,args=(email,),daemon=True).start()
        return self.json_response(200,{'ok':True,'message':'Parola a fost schimbată. Conectează-te cu parola nouă.'})
    @staticmethod
    def send_password_changed_email(email):
        try:send_email(email,'Parola contului TunsPro a fost schimbată','Parola contului tău TunsPro a fost schimbată. Dacă nu ai făcut tu această modificare, contactează-ne imediat la tunsprogramari@gmail.com.')
        except Exception as e:print('Password change notification failed:',repr(e))
    def client_register(self,d):
        name=str(d.get('name','')).strip();email=str(d.get('email','')).lower().strip();phone=str(d.get('phone','')).strip();password=str(d.get('password',''))
        if len(name)<2 or '@' not in email or len(''.join(x for x in phone if x.isdigit()))<9:raise ValueError('Verifică numele, e-mailul și numărul de telefon.')
        if len(password)<10:raise ValueError('Parola trebuie să aibă cel puțin 10 caractere.')
        with connect() as c:
            if c.execute('SELECT 1 FROM customers WHERE email=?',(email,)).fetchone():return self.json_response(409,{'error':'Există deja un cont client cu acest e-mail.'})
            cur=c.execute('INSERT INTO customers(email,password,name,phone,created) VALUES(?,?,?,?,?)',(email,password_hash(password),name,phone,iso_now()))
            customer_id=cur.lastrowid
        return self.json_response(201,{'ok':True},self.set_client_session(customer_id))
    def client_login(self,d):
        email=str(d.get('email','')).lower().strip();key=login_limit_key('client',email)
        with connect() as c:
            if login_is_limited(c,key):return self.json_response(429,{'error':'Prea multe încercări. Așteaptă 15 minute și încearcă din nou.'})
            customer=c.execute('SELECT * FROM customers WHERE email=?',(email,)).fetchone()
            if not customer or customer['status']!='active' or not password_ok(str(d.get('password','')),customer['password']):login_failure(c,key);return self.json_response(401,{'error':'E-mailul sau parola nu sunt corecte.'})
            login_success(c,key);c.execute('UPDATE customers SET last_login=? WHERE id=?',(iso_now(),customer['id']))
        return self.json_response(200,{'ok':True},self.set_client_session(customer['id']))
    def admin_login(self,d):
        email=str(d.get('email','')).lower().strip();key=login_limit_key('admin',email)
        with connect() as c:
            if login_is_limited(c,key):return self.json_response(429,{'error':'Prea multe încercări. Așteaptă 15 minute și încearcă din nou.'})
            admin=c.execute('SELECT * FROM admins WHERE email=?',(email,)).fetchone()
            if not admin or not password_ok(str(d.get('password','')),admin['password']):login_failure(c,key);return self.json_response(401,{'error':'E-mailul sau parola de administrator nu sunt corecte.'})
            login_success(c,key);c.execute('UPDATE admins SET last_login=? WHERE id=?',(iso_now(),admin['id']))
            c.execute('INSERT INTO admin_audit(admin_email,action,details,created) VALUES(?,?,?,?)',(admin['email'],'admin_login','{}',iso_now()))
        return self.json_response(200,{'ok':True},self.set_admin_session(admin['id']))
    def client_favorite(self,d):
        with connect() as c:
            customer=self.client_auth(c)
            if not customer:return self.json_response(401,{'error':'Conectează-te la contul de client.'})
            shop=c.execute('SELECT * FROM shops WHERE id=?',(int(d.get('shop_id',0)),)).fetchone()
            if not shop or not shop_public(c,shop):return self.json_response(404,{'error':'Frizeria nu este disponibilă.'})
            if d.get('action')=='remove':c.execute('DELETE FROM favorites WHERE customer_id=? AND shop_id=?',(customer['id'],shop['id']))
            else:c.execute('INSERT OR IGNORE INTO favorites(customer_id,shop_id,created) VALUES(?,?,?)',(customer['id'],shop['id'],iso_now()))
        return self.json_response(200,{'ok':True,'favorited':d.get('action')!='remove'})
    def client_cancel_booking(self,d):
        with connect() as c:
            customer=self.client_auth(c)
            if not customer:return self.json_response(401,{'error':'Conectează-te la contul de client.'})
            row=c.execute("SELECT b.*,sh.name shop_name,sh.phone shop_phone FROM bookings b JOIN shops sh ON sh.id=b.shop_id WHERE b.id=? AND b.customer_id=? AND b.status IN ('pending','confirmed')",(int(d.get('id',0)),customer['id'])).fetchone()
            if not row:return self.json_response(404,{'error':'Programarea viitoare nu a fost găsită.'})
            if datetime.fromisoformat(row['starts'])<=now_utc():return self.json_response(400,{'error':'Programarea nu mai poate fi anulată din cont. Sună frizeria.'})
            c.execute("UPDATE bookings SET status='cancelled' WHERE id=?",(row['id'],))
            record_booking_event(c,row['id'],row['shop_id'],'booking_cancelled','client',actor_id=customer['id'],old_starts=row['starts'],new_starts=row['starts'],old_status=row['status'],new_status='cancelled')
        try:send_email(customer['email'],f'Programare anulată — {row["shop_name"]}',f'Programarea ta a fost anulată. Pentru o nouă rezervare, caută frizeria în TunsPro.')
        except Exception as e:print('Client cancellation notice failed:',repr(e))
        return self.json_response(200,{'ok':True})
    def delete_client_account(self):
        with connect() as c:
            customer=self.client_auth(c)
            if not customer:return self.json_response(401,{'error':'Conectează-te la contul de client.'})
            c.execute('DELETE FROM reviews WHERE customer_id=?',(customer['id'],))
            c.execute("UPDATE bookings SET customer_id=NULL,client='Date anonimizate',phone='',email='' WHERE customer_id=?",(customer['id'],))
            c.execute('DELETE FROM customers WHERE id=?',(customer['id'],))
        return self.json_response(200,{'ok':True},self.clear_client_session())
    def create_review(self,d):
        try:rating=int(d.get('rating',0));booking_id=int(d.get('booking_id',0))
        except Exception:raise ValueError('Alege o evaluare între 1 și 5 stele.')
        comment=str(d.get('comment','')).strip()
        if rating<1 or rating>5 or len(comment)>1500:raise ValueError('Evaluarea trebuie să fie între 1 și 5 stele, iar textul sub 1.500 de caractere.')
        with connect() as c:
            customer=self.client_auth(c)
            if not customer:return self.json_response(401,{'error':'Conectează-te la contul de client.'})
            booking=c.execute("SELECT id,shop_id,ends,status FROM bookings WHERE id=? AND customer_id=?",(booking_id,customer['id'])).fetchone()
            if not booking or booking['status']!='completed' or datetime.fromisoformat(booking['ends'])>now_utc():return self.json_response(400,{'error':'Poți evalua doar o programare încheiată la care ai participat.'})
            if c.execute('SELECT 1 FROM reviews WHERE booking_id=?',(booking_id,)).fetchone():return self.json_response(409,{'error':'Ai trimis deja o recenzie pentru această programare.'})
            c.execute('INSERT INTO reviews(booking_id,customer_id,shop_id,rating,comment,created) VALUES(?,?,?,?,?,?)',(booking_id,customer['id'],booking['shop_id'],rating,comment,iso_now()))
        return self.json_response(201,{'ok':True})
    def admin_listing(self,d):
        with connect() as c:
            admin=self.admin_auth(c)
            if not admin:return self.json_response(401,{'error':'Autentificare de administrator necesară.'})
            shop_id=int(d.get('shop_id',0)); shop=c.execute('SELECT listing_enabled FROM shops WHERE id=?',(shop_id,)).fetchone()
            if not shop:return self.json_response(404,{'error':'Frizeria nu a fost găsită.'})
            enabled=bool(d.get('enabled'))
            if bool(shop['listing_enabled'])==enabled:return self.json_response(200,{'ok':True,'changed':False})
            c.execute('UPDATE shops SET listing_enabled=? WHERE id=?',(int(enabled),shop_id))
            c.execute('INSERT INTO admin_audit(admin_email,action,shop_id,details,created) VALUES(?,?,?,?,?)',(admin['email'],'shop_listing_enabled' if enabled else 'shop_listing_hidden',shop_id,json.dumps({'enabled':enabled}),iso_now()))
        return self.json_response(200,{'ok':True})
    def admin_account(self,d):
        account_type=str(d.get('account_type',''));status=str(d.get('status',''));account_id=int(d.get('account_id',0))
        if account_type not in ('barber','client') or status not in ('active','suspended') or account_id<1:return self.json_response(400,{'error':'Tipul, contul sau statusul nu sunt valide.'})
        with connect() as c:
            admin=self.admin_auth(c)
            if not admin:return self.json_response(401,{'error':'Autentificare de administrator necesară.'})
            table='users' if account_type=='barber' else 'customers'
            row=c.execute(f'SELECT id,status FROM {table} WHERE id=?',(account_id,)).fetchone()
            if not row:return self.json_response(404,{'error':'Contul nu a fost găsit.'})
            if row['status']==status:return self.json_response(200,{'ok':True,'changed':False})
            c.execute(f'UPDATE {table} SET status=? WHERE id=?',(status,account_id))
            if status=='suspended':
                session_table='sessions' if account_type=='barber' else 'client_sessions';owner_field='user_id' if account_type=='barber' else 'customer_id'
                c.execute(f'DELETE FROM {session_table} WHERE {owner_field}=?',(account_id,))
            shop_id=None
            if account_type=='barber':
                shop=c.execute('SELECT id FROM shops WHERE user_id=?',(account_id,)).fetchone();shop_id=shop['id'] if shop else None
            action='account_suspended' if status=='suspended' else 'account_reactivated'
            c.execute('INSERT INTO admin_audit(admin_email,action,shop_id,details,created) VALUES(?,?,?,?,?)',(admin['email'],action,shop_id,json.dumps({'account_type':account_type,'account_id':account_id}),iso_now()))
        return self.json_response(200,{'ok':True,'status':status})
    def admin_shop_update(self,d):
        try:shop_id=int(d.get('shop_id',0))
        except (TypeError,ValueError):return self.json_response(400,{'error':'Frizeria aleasă nu este validă.'})
        values={key:str(d.get(key,'')).strip() for key in ('name','city','address','phone')}
        if shop_id<1 or any(not value for value in values.values()) or len(values['name'])>120 or len(values['city'])>100 or len(values['address'])>240 or len(values['phone'])>30:return self.json_response(400,{'error':'Verifică numele, localitatea, adresa și telefonul.'})
        if len(''.join(ch for ch in values['phone'] if ch.isdigit()))<8:return self.json_response(400,{'error':'Introdu un număr de telefon valid.'})
        with connect() as c:
            admin=self.admin_auth(c)
            if not admin:return self.json_response(401,{'error':'Autentificare de administrator necesară.'})
            shop=c.execute('SELECT name,city,address,phone FROM shops WHERE id=?',(shop_id,)).fetchone()
            if not shop:return self.json_response(404,{'error':'Frizeria nu a fost găsită.'})
            before=dict(shop)
            if before==values:return self.json_response(200,{'ok':True,'changed':False})
            c.execute('UPDATE shops SET name=?,city=?,address=?,phone=? WHERE id=?',(values['name'],values['city'],values['address'],values['phone'],shop_id))
            c.execute('INSERT INTO admin_audit(admin_email,action,shop_id,details,created) VALUES(?,?,?,?,?)',(admin['email'],'shop_details_updated',shop_id,json.dumps({'before':before,'after':values},ensure_ascii=False),iso_now()))
        return self.json_response(200,{'ok':True,'changed':True})
    def admin_shop_approval(self,d):
        try:shop_id=int(d.get('shop_id',0))
        except (TypeError,ValueError):return self.json_response(400,{'error':'Frizeria aleasă nu este validă.'})
        status=str(d.get('status',''))
        if shop_id<1 or status not in ('approved','rejected'):return self.json_response(400,{'error':'Aprobarea aleasă nu este validă.'})
        with connect() as c:
            admin=self.admin_auth(c)
            if not admin:return self.json_response(401,{'error':'Autentificare de administrator necesară.'})
            shop=c.execute('SELECT approval_status,listing_enabled FROM shops WHERE id=?',(shop_id,)).fetchone()
            if not shop:return self.json_response(404,{'error':'Frizeria nu a fost găsită.'})
            if shop['approval_status']==status:return self.json_response(200,{'ok':True,'changed':False})
            c.execute('UPDATE shops SET approval_status=?,listing_enabled=CASE WHEN ?=\'rejected\' THEN 0 ELSE listing_enabled END WHERE id=?',(status,status,shop_id))
            c.execute('INSERT INTO admin_audit(admin_email,action,shop_id,details,created) VALUES(?,?,?,?,?)',(admin['email'],'shop_'+status,shop_id,json.dumps({'from':shop['approval_status'],'to':status}),iso_now()))
        return self.json_response(200,{'ok':True,'status':status})
    def admin_staff_toggle(self,d):
        try:staff_id=int(d.get('staff_id',0))
        except (TypeError,ValueError):return self.json_response(400,{'error':'Frizerul ales nu este valid.'})
        active=bool(d.get('active'))
        with connect() as c:
            admin=self.admin_auth(c)
            if not admin:return self.json_response(401,{'error':'Autentificare de administrator necesară.'})
            person=c.execute('SELECT id,shop_id,active FROM staff WHERE id=?',(staff_id,)).fetchone()
            if not person:return self.json_response(404,{'error':'Frizerul nu a fost găsit.'})
            if bool(person['active'])==active:return self.json_response(200,{'ok':True,'changed':False})
            c.execute('UPDATE staff SET active=? WHERE id=?',(int(active),staff_id))
            c.execute('INSERT INTO admin_audit(admin_email,action,shop_id,details,created) VALUES(?,?,?,?,?)',(admin['email'],'staff_activated' if active else 'staff_deactivated',person['shop_id'],json.dumps({'staff_id':staff_id}),iso_now()))
        return self.json_response(200,{'ok':True,'active':active})
    def admin_service_toggle(self,d):
        try:service_id=int(d.get('service_id',0))
        except (TypeError,ValueError):return self.json_response(400,{'error':'Serviciul ales nu este valid.'})
        active=bool(d.get('active'))
        with connect() as c:
            admin=self.admin_auth(c)
            if not admin:return self.json_response(401,{'error':'Autentificare de administrator necesară.'})
            service=c.execute('SELECT id,shop_id,active FROM services WHERE id=?',(service_id,)).fetchone()
            if not service:return self.json_response(404,{'error':'Serviciul nu a fost găsit.'})
            if bool(service['active'])==active:return self.json_response(200,{'ok':True,'changed':False})
            c.execute('UPDATE services SET active=? WHERE id=?',(int(active),service_id))
            c.execute('INSERT INTO admin_audit(admin_email,action,shop_id,details,created) VALUES(?,?,?,?,?)',(admin['email'],'service_activated' if active else 'service_deactivated',service['shop_id'],json.dumps({'service_id':service_id}),iso_now()))
        return self.json_response(200,{'ok':True,'active':active})
    def admin_review_visibility(self,d):
        try:review_id=int(d.get('review_id',0))
        except (TypeError,ValueError):return self.json_response(400,{'error':'Recenzia aleasă nu este validă.'})
        visible=bool(d.get('visible'))
        with connect() as c:
            admin=self.admin_auth(c)
            if not admin:return self.json_response(401,{'error':'Autentificare de administrator necesară.'})
            review=c.execute('SELECT id,shop_id,is_visible FROM reviews WHERE id=?',(review_id,)).fetchone()
            if not review:return self.json_response(404,{'error':'Recenzia nu a fost găsită.'})
            if bool(review['is_visible'])==visible:return self.json_response(200,{'ok':True,'changed':False})
            c.execute('UPDATE reviews SET is_visible=? WHERE id=?',(int(visible),review_id))
            c.execute('INSERT INTO admin_audit(admin_email,action,shop_id,details,created) VALUES(?,?,?,?,?)',(admin['email'],'review_shown' if visible else 'review_hidden',review['shop_id'],json.dumps({'review_id':review_id}),iso_now()))
        return self.json_response(200,{'ok':True,'visible':visible})
    def admin_promo_codes(self,d):
        action=str(d.get('action','create'))
        try:code_id=int(d.get('id',0))
        except (TypeError,ValueError):code_id=0
        with connect() as c:
            admin=self.admin_auth(c)
            if not admin:return self.json_response(401,{'error':'Autentificare de administrator necesară.'})
            if action=='create':
                code=str(d.get('code','')).strip().upper();percent=d.get('discount_percent');starts=str(d.get('starts_at','')).strip() or None;ends=str(d.get('ends_at','')).strip() or None;maximum=d.get('max_uses')
                try:percent=int(percent);maximum=int(maximum) if maximum not in (None,'') else None
                except (TypeError,ValueError):return self.json_response(400,{'error':'Procentul sau limita de utilizări nu sunt valide.'})
                if not re.fullmatch(r'[A-Z0-9_-]{3,32}',code) or not 1<=percent<=100 or (maximum is not None and not 1<=maximum<=100000):return self.json_response(400,{'error':'Codul trebuie să aibă 3–32 caractere, reducerea 1–100%, iar limita să fie pozitivă.'})
                try:
                    if starts:datetime.fromisoformat(starts)
                    if ends:datetime.fromisoformat(ends)
                    if starts and ends and datetime.fromisoformat(ends)<=datetime.fromisoformat(starts):raise ValueError()
                except ValueError:return self.json_response(400,{'error':'Intervalul de valabilitate nu este valid.'})
                try:
                    cur=c.execute('INSERT INTO promo_codes(code,discount_percent,active,starts_at,ends_at,max_uses,created_by,created) VALUES(?,?,1,?,?,?,?,?)',(code,percent,starts,ends,maximum,admin['email'],iso_now()))
                except sqlite3.IntegrityError:return self.json_response(409,{'error':'Codul promoțional există deja.'})
                c.execute('INSERT INTO admin_audit(admin_email,action,details,created) VALUES(?,?,?,?)',(admin['email'],'promo_code_created',json.dumps({'code':code,'discount_percent':percent}),iso_now()))
                return self.json_response(201,{'ok':True,'id':cur.lastrowid})
            promo=c.execute('SELECT * FROM promo_codes WHERE id=?',(code_id,)).fetchone()
            if not promo:return self.json_response(404,{'error':'Codul promoțional nu a fost găsit.'})
            if action=='toggle':
                active=bool(d.get('active'));c.execute('UPDATE promo_codes SET active=? WHERE id=?',(int(active),code_id));event='promo_code_activated' if active else 'promo_code_deactivated'
            elif action=='delete':
                c.execute('DELETE FROM promo_codes WHERE id=?',(code_id,));active=False;event='promo_code_deleted'
            else:return self.json_response(400,{'error':'Acțiunea nu este validă.'})
            c.execute('INSERT INTO admin_audit(admin_email,action,details,created) VALUES(?,?,?,?)',(admin['email'],event,json.dumps({'code':promo['code']}),iso_now()))
        return self.json_response(200,{'ok':True,'active':active})
    def create_booking(self,d):
        client_name=str(d.get('client','')).strip();client_email=str(d.get('email','')).strip().lower()
        client_phone=normalize_ro_mobile(str(d.get('phone','')))
        if len(client_name)<2 or len(client_name)>100:raise ValueError('Introdu numele complet (2–100 caractere).')
        if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',client_email) or len(client_email)>254:raise ValueError('Introdu o adresă de e-mail validă pentru confirmare.')
        if not client_phone:raise ValueError('Introdu un număr mobil din România valid pentru confirmarea prin SMS.')
        slug=str(d.get('slug','')); day=date.fromisoformat(d.get('date','')); start_time=time.fromisoformat(d.get('time','')); starts=datetime.combine(day,start_time,TZ)
        if starts<=datetime.now(TZ):raise ValueError('Alege o oră viitoare.')
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            shop=c.execute('SELECT sh.*,u.status owner_status FROM shops sh JOIN users u ON u.id=sh.user_id WHERE sh.slug=?',(slug,)).fetchone(); sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(shop['id'],)).fetchone() if shop else None
            if not shop or shop['owner_status']!='active' or shop['approval_status']!='approved' or not shop['listing_enabled'] or not active_subscription(sub):return self.json_response(403,{'error':'Frizeria nu acceptă programări momentan.'})
            service=c.execute('SELECT * FROM services WHERE id=? AND shop_id=? AND active=1',(int(d.get('service_id',0)),shop['id'])).fetchone(); staff=c.execute('SELECT * FROM staff WHERE id=? AND shop_id=? AND active=1',(int(d.get('staff_id',0)),shop['id'])).fetchone()
            if not service or not staff:raise ValueError('Serviciul sau frizerul nu este disponibil.')
            sched=json.loads(staff['weekly_schedule'] or '{}').get(str(day.weekday()))
            finish=starts+timedelta(minutes=service['duration'])
            if not sched or starts.time()<time.fromisoformat(sched[0]) or finish.time()>time.fromisoformat(sched[1]):raise ValueError('Ora aleasă este în afara programului frizerului.')
            collision=c.execute("SELECT 1 FROM bookings WHERE staff_id=? AND status IN ('confirmed','pending') AND julianday(starts)<julianday(?) AND julianday(ends)>julianday(?)",(staff['id'],finish.isoformat(),starts.isoformat())).fetchone()
            if collision:raise ValueError('Ora tocmai a fost rezervată. Alege alt interval.')
            promo_code=str(d.get('promo_code','')).strip().upper();discount_amount=0
            if promo_code:
                promo=c.execute('SELECT * FROM promo_codes WHERE code=? COLLATE NOCASE AND active=1',(promo_code,)).fetchone()
                if not promo:raise ValueError('Codul promoțional nu este valid sau nu mai este activ.')
                if promo['starts_at'] and day.isoformat()<promo['starts_at'][:10]:raise ValueError('Codul promoțional nu este încă valabil.')
                if promo['ends_at'] and day.isoformat()>promo['ends_at'][:10]:raise ValueError('Codul promoțional a expirat.')
                if promo['max_uses'] is not None and promo['uses_count']>=promo['max_uses']:raise ValueError('Codul promoțional și-a atins limita de utilizări.')
                discount_amount=round(service['price']*promo['discount_percent']/100)
            current=now_utc();reminder_at=starts-timedelta(hours=24);reminder_sent=0
            if reminder_at<=current:
                if starts>current+timedelta(hours=1):reminder_at=starts-timedelta(hours=1)
                else:reminder_sent=1
            cancel_token=secrets.token_urlsafe(32);token_hash=hashlib.sha256(cancel_token.encode()).hexdigest()
            cur=c.execute('INSERT INTO bookings(shop_id,service_id,staff_id,client,phone,email,starts,ends,status,created,reminder_at,reminder_sent,customer_id,price_at_booking,manage_token_hash,promo_code,discount_amount) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(shop['id'],service['id'],staff['id'],client_name,client_phone,client_email,starts.isoformat(),finish.isoformat(),'pending',iso_now(),reminder_at.isoformat(),reminder_sent,None,service['price']-discount_amount,token_hash,promo_code or None,discount_amount)); booking_id=cur.lastrowid
            if promo_code:c.execute('UPDATE promo_codes SET uses_count=uses_count+1 WHERE id=?',(promo['id'],))
            record_booking_event(c,booking_id,shop['id'],'booking_created','client')
            opts=c.execute('SELECT * FROM settings WHERE shop_id=?',(shop['id'],)).fetchone(); owner=c.execute('SELECT u.email FROM users u WHERE u.id=?',(shop['user_id'],)).fetchone()
        days_ro=['luni','marți','miercuri','joi','vineri','sâmbătă','duminică']
        months_ro=['ianuarie','februarie','martie','aprilie','mai','iunie','iulie','august','septembrie','octombrie','noiembrie','decembrie']
        when=f'{days_ro[day.weekday()]} {day.day} {months_ro[day.month-1]}, {starts:%H:%M}'
        manage_url=os.environ.get('PUBLIC_URL','').rstrip('/')+'/#anulare/'+cancel_token
        reschedule_url=os.environ.get('PUBLIC_URL','').rstrip('/')+'/#reprogramare/'+cancel_token
        mail_status='unavailable';sms_status='unavailable'
        if email_configured():
            try:
                send_email(client_email,f'Cerere de programare primită — {shop["name"]}',f'Am primit cererea ta pentru {service["name"]} cu {staff["name"]}, {when}. Programarea așteaptă confirmarea frizeriei. Adresă: {shop["address"]}, {shop["city"]}. Dacă nu mai dorești rezervarea, o poți anula aici: {manage_url}. Pentru ajutor, contactează frizeria la {shop["phone"]}.')
                mail_status='sent'
            except Exception as e:mail_status='failed';print('Client confirmation failed:',repr(e))
        if os.environ.get('TWILIO_ACCOUNT_SID') and os.environ.get('TWILIO_AUTH_TOKEN') and os.environ.get('TWILIO_FROM'):
            try:
                sms_notice(client_phone,f'TunsPro: cererea ta la {shop["name"]} pentru {when} a fost primita si asteapta confirmarea frizeriei. Anulare: {manage_url}.')
                sms_status='sent'
            except Exception as e:sms_status='failed';print('Client SMS confirmation failed:',repr(e))
        if opts and opts['notification_email'] and owner and email_configured():
            try:send_email(owner['email'],f'Programare nouă — {shop["name"]}',f'{client_name} a trimis o cerere pentru {service["name"]} cu {staff["name"]}, {when}. Așteaptă confirmarea ta. Telefon: {client_phone}')
            except Exception as e:print('Barber email notification failed:',repr(e))
        if opts and opts['notification_sms'] and os.environ.get('TWILIO_ACCOUNT_SID') and os.environ.get('TWILIO_AUTH_TOKEN') and os.environ.get('TWILIO_FROM'):
            try:sms_notice(shop['phone'],f'TunsPro: programare nouă la {when}. Client: {client_name}, {client_phone}')
            except Exception as e:print('Barber SMS notification failed:',repr(e))
        return self.json_response(201,{'ok':True,'booking_id':booking_id,'phone':client_phone,'cancel_token':cancel_token,'price':service['price']-discount_amount,'discount':discount_amount,'promo_code':promo_code or None,'notifications':{'email':mail_status,'sms':sms_status},'status':'pending','message':'Cererea de programare a fost trimisă și așteaptă confirmarea frizeriei.'})
    def public_reschedule_details(self,d):
        token=str(d.get('token','')).strip()
        if len(token)<30:return self.json_response(400,{'error':'Linkul de modificare nu este valid.'})
        token_hash=hashlib.sha256(token.encode()).hexdigest()
        with connect() as c:
            row=c.execute("SELECT b.id,b.client,b.phone,b.email,b.starts,b.service_id,b.staff_id,sh.slug shop_slug,sh.name shop_name FROM bookings b JOIN shops sh ON sh.id=b.shop_id WHERE b.manage_token_hash=? AND b.status IN ('pending','confirmed')",(token_hash,)).fetchone()
            if not row:return self.json_response(404,{'error':'Programarea nu mai poate fi modificată. Verifică dacă a fost deja anulată sau contactează frizeria.'})
            if datetime.fromisoformat(row['starts'])<=now_utc():return self.json_response(400,{'error':'Programarea a început deja și nu mai poate fi modificată online.'})
        return self.json_response(200,{'booking':dict(row)})
    def reschedule_public_booking(self,d):
        token=str(d.get('token','')).strip()
        if len(token)<30:return self.json_response(400,{'error':'Linkul de modificare nu este valid.'})
        try:
            day=date.fromisoformat(str(d.get('date','')));start_time=time.fromisoformat(str(d.get('time','')))
            service_id=int(d.get('service_id',0));staff_id=int(d.get('staff_id',0))
        except (TypeError,ValueError):return self.json_response(400,{'error':'Verifică data, ora, serviciul și frizerul ales.'})
        token_hash=hashlib.sha256(token.encode()).hexdigest();starts=datetime.combine(day,start_time,TZ)
        if starts<=datetime.now(TZ):return self.json_response(400,{'error':'Alege o oră viitoare.'})
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT b.*,sh.name shop_name,sh.slug shop_slug,sh.address,sh.city,sh.phone shop_phone,sh.listing_enabled,sh.approval_status,u.status owner_status,s.name old_service_name,t.name old_staff_name FROM bookings b JOIN shops sh ON sh.id=b.shop_id JOIN users u ON u.id=sh.user_id JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id WHERE b.manage_token_hash=? AND b.status IN ('pending','confirmed')",(token_hash,)).fetchone()
            if not row:return self.json_response(404,{'error':'Programarea nu mai poate fi modificată. Verifică dacă a fost deja anulată sau contactează frizeria.'})
            if datetime.fromisoformat(row['starts'])<=now_utc():return self.json_response(400,{'error':'Programarea a început deja și nu mai poate fi modificată online.'})
            sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(row['shop_id'],)).fetchone()
            if row['owner_status']!='active' or row['approval_status']!='approved' or not row['listing_enabled'] or not active_subscription(sub):return self.json_response(403,{'error':'Frizeria nu acceptă modificări online momentan. Contactează frizeria direct.'})
            service=c.execute('SELECT * FROM services WHERE id=? AND shop_id=? AND active=1',(service_id,row['shop_id'])).fetchone()
            staff=c.execute('SELECT * FROM staff WHERE id=? AND shop_id=? AND active=1',(staff_id,row['shop_id'])).fetchone()
            if not service or not staff:return self.json_response(400,{'error':'Serviciul sau frizerul nu mai este disponibil.'})
            sched=json.loads(staff['weekly_schedule'] or '{}').get(str(day.weekday()));finish=starts+timedelta(minutes=service['duration'])
            if not sched or starts.time()<time.fromisoformat(sched[0]) or finish.time()>time.fromisoformat(sched[1]):return self.json_response(400,{'error':'Ora aleasă este în afara programului frizerului.'})
            collision=c.execute("SELECT 1 FROM bookings WHERE staff_id=? AND status IN ('confirmed','pending') AND id<>? AND julianday(starts)<julianday(?) AND julianday(ends)>julianday(?)",(staff['id'],row['id'],finish.isoformat(),starts.isoformat())).fetchone()
            if collision:return self.json_response(409,{'error':'Ora tocmai a fost rezervată. Alege alt interval.'})
            reminder_at=starts-timedelta(hours=24);reminder_sent=0
            if reminder_at<=now_utc():
                if starts>now_utc()+timedelta(hours=1):reminder_at=starts-timedelta(hours=1)
                else:reminder_sent=1
            c.execute('UPDATE bookings SET service_id=?,staff_id=?,starts=?,ends=?,price_at_booking=?,reminder_at=?,reminder_sent=? WHERE id=?',(service['id'],staff['id'],starts.isoformat(),finish.isoformat(),service['price'],reminder_at.isoformat() if not reminder_sent else None,reminder_sent,row['id']))
            record_booking_event(c,row['id'],row['shop_id'],'booking_rescheduled','client',old_starts=row['starts'],new_starts=starts.isoformat(),old_status='confirmed',new_status='confirmed')
            settings=c.execute('SELECT * FROM settings WHERE shop_id=?',(row['shop_id'],)).fetchone();owner=c.execute('SELECT u.email FROM users u JOIN shops sh ON sh.user_id=u.id WHERE sh.id=?',(row['shop_id'],)).fetchone()
            client_email=row['email'];client_phone=row['phone'];client=row['client'];shop_name=row['shop_name'];shop_phone=row['shop_phone'];old_starts=datetime.fromisoformat(row['starts']).astimezone(TZ).strftime('%d.%m.%Y, %H:%M')
        when=starts.strftime('%d.%m.%Y, %H:%M');manage_url=os.environ.get('PUBLIC_URL','').rstrip('/')+'/#anulare/'+token;reschedule_url=os.environ.get('PUBLIC_URL','').rstrip('/')+'/#reprogramare/'+token
        mail_status='unavailable';sms_status='unavailable'
        if client_email and email_configured():
            try:send_email(client_email,f'Programare reprogramată — {shop_name}',f'Programarea ta a fost mutată de la {old_starts} la {when}, pentru {service["name"]} cu {staff["name"]}. Adresă: {row["address"]}, {row["city"]}. Pentru anulare online: {manage_url}. Pentru schimbarea zilei sau orei: {reschedule_url}.');mail_status='sent'
            except Exception as e:mail_status='failed';print('Client reschedule email failed:',repr(e))
        if client_phone and os.environ.get('TWILIO_ACCOUNT_SID') and os.environ.get('TWILIO_AUTH_TOKEN') and os.environ.get('TWILIO_FROM'):
            try:sms_notice(client_phone,f'TunsPro: programarea ta la {shop_name} a fost mutată pentru {when}, cu {staff["name"]}. Modificare: {reschedule_url}.');sms_status='sent'
            except Exception as e:sms_status='failed';print('Client reschedule SMS failed:',repr(e))
        if settings and settings['notification_email'] and owner and email_configured():
            try:send_email(owner['email'],f'Programare reprogramată — {shop_name}',f'{client} a reprogramat rezervarea din {old_starts} pentru {when}, cu {staff["name"]}. Telefon: {client_phone}.')
            except Exception as e:print('Shop reschedule email failed:',repr(e))
        if settings and settings['notification_sms'] and os.environ.get('TWILIO_ACCOUNT_SID') and os.environ.get('TWILIO_AUTH_TOKEN') and os.environ.get('TWILIO_FROM'):
            try:sms_notice(shop_phone,f'TunsPro: {client} a reprogramat rezervarea pentru {when}, cu {staff["name"]}.')
            except Exception as e:print('Shop reschedule SMS failed:',repr(e))
        return self.json_response(200,{'ok':True,'booking_id':row['id'],'cancel_token':token,'notifications':{'email':mail_status,'sms':sms_status}})
    def cancel_public_booking(self,d):
        token=str(d.get('token','')).strip()
        if len(token)<30:return self.json_response(400,{'error':'Linkul de anulare nu este valid.'})
        token_hash=hashlib.sha256(token.encode()).hexdigest()
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT b.*,sh.name shop_name,sh.phone shop_phone,s.name service_name,t.name staff_name FROM bookings b JOIN shops sh ON sh.id=b.shop_id JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id WHERE b.manage_token_hash=? AND b.status IN ('pending','confirmed')",(token_hash,)).fetchone()
            if not row:return self.json_response(404,{'error':'Programarea nu mai poate fi anulată. Verifică dacă a fost deja anulată sau contactează frizeria.'})
            if datetime.fromisoformat(row['starts'])<=now_utc():return self.json_response(400,{'error':'Programarea a început deja și nu mai poate fi anulată online.'})
            c.execute("UPDATE bookings SET status='cancelled' WHERE id=?",(row['id'],))
            record_booking_event(c,row['id'],row['shop_id'],'booking_cancelled','client',actor_id=row['customer_id'],old_starts=row['starts'],new_starts=row['starts'],old_status=row['status'],new_status='cancelled')
            settings=c.execute('SELECT * FROM settings WHERE shop_id=?',(row['shop_id'],)).fetchone();owner=c.execute('SELECT u.email FROM users u JOIN shops sh ON sh.user_id=u.id WHERE sh.id=?',(row['shop_id'],)).fetchone()
        when=datetime.fromisoformat(row['starts']).astimezone(TZ).strftime('%d.%m.%Y, %H:%M')
        if row['email'] and email_configured():
            try:send_email(row['email'],f'Programare anulată — {row["shop_name"]}',f'Programarea ta din {when} a fost anulată. Dacă te-ai răzgândit, poți face o nouă rezervare pe TunsPro.')
            except Exception as e:print('Client cancellation confirmation failed:',repr(e))
        if settings and settings['notification_email'] and owner and email_configured():
            try:send_email(owner['email'],f'Programare anulată — {row["shop_name"]}',f'{row["client"]} a anulat programarea din {when}.')
            except Exception as e:print('Barber cancellation notice failed:',repr(e))
        if settings and settings['notification_sms'] and os.environ.get('TWILIO_ACCOUNT_SID') and os.environ.get('TWILIO_AUTH_TOKEN') and os.environ.get('TWILIO_FROM'):
            try:sms_notice(row['shop_phone'],f'TunsPro: clientul {row["client"]} a anulat programarea din {when}.')
            except Exception as e:print('Barber cancellation SMS failed:',repr(e))
        return self.json_response(200,{'ok':True,'message':'Programarea a fost anulată.'})
    def cancel_booking(self,d):
        reason=str(d.get('reason','')).strip()
        if not reason:return self.json_response(400,{'error':'Adaugă motivul anulării.'})
        if len(reason)>240:return self.json_response(400,{'error':'Motivul anulării trebuie să aibă maximum 240 de caractere.'})
        with connect() as c:
            user=self.auth(c)
            if not user:return self.json_response(401,{'error':'Conectează-te pentru a continua.'})
            sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(user['shop_id'],)).fetchone()
            if not active_subscription(sub):return self.json_response(403,{'error':'Gestionează programările cu un abonament PRO sau BUSINESS activ.'})
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT b.*,s.name service_name,t.name staff_name FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id WHERE b.id=? AND b.shop_id=? AND b.status IN ('pending','confirmed')",(int(d.get('id',0)),user['shop_id'])).fetchone()
            if not row:return self.json_response(404,{'error':'Programarea nu a fost găsită.'})
            old_status=row['status']
            changed=c.execute("UPDATE bookings SET status='cancelled' WHERE id=? AND shop_id=? AND status IN ('pending','confirmed')",(row['id'],user['shop_id'])).rowcount
            if not changed:return self.json_response(409,{'error':'Programarea a fost modificată. Reîncarcă agenda și încearcă din nou.'})
            record_booking_event(c,row['id'],user['shop_id'],'booking_cancelled','barber',actor_id=user['id'],old_starts=row['starts'],new_starts=row['starts'],old_status=old_status,new_status='cancelled',reason=reason)
            client_email=row['email'];client_phone=row['phone'];client=row['client'];starts=datetime.fromisoformat(row['starts']).astimezone(TZ).strftime('%d.%m.%Y, %H:%M');shop_name=user['shop_name']
        body=f'Programarea ta de la {shop_name}, {starts}, a fost anulată de frizerie. Motiv: {reason}'
        if client_email and email_configured():
            try:send_email(client_email,f'Programare anulată — {shop_name}',body)
            except Exception as e:print('Client cancellation notice failed:',repr(e))
        if client_phone and os.environ.get('TWILIO_ACCOUNT_SID') and os.environ.get('TWILIO_AUTH_TOKEN') and os.environ.get('TWILIO_FROM'):
            try:sms_notice(client_phone,f'TunsPro: programarea ta de la {shop_name}, {starts}, a fost anulata. Motiv: {reason}')
            except Exception as e:print('Client cancellation SMS failed:',repr(e))
        return self.json_response(200,{'ok':True,'message':'Programarea a fost anulată.'})
    def confirm_booking(self,d):
        with connect() as c:
            user=self.auth(c)
            if not user:return self.json_response(401,{'error':'Conectează-te pentru a continua.'})
            sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(user['shop_id'],)).fetchone()
            if not active_subscription(sub):return self.json_response(403,{'error':'Gestionează programările cu un abonament PRO sau BUSINESS activ.'})
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT b.*,s.name service_name,t.name staff_name,sh.name shop_name,sh.city,sh.address,sh.phone shop_phone FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id JOIN shops sh ON sh.id=b.shop_id WHERE b.id=? AND b.shop_id=? AND b.status='pending'",(int(d.get('id',0)),user['shop_id'])).fetchone()
            if not row:return self.json_response(404,{'error':'Programarea în așteptare nu a fost găsită.'})
            changed=c.execute("UPDATE bookings SET status='confirmed' WHERE id=? AND shop_id=? AND status='pending'",(row['id'],user['shop_id'])).rowcount
            if not changed:return self.json_response(409,{'error':'Programarea a fost modificată. Reîncarcă agenda și încearcă din nou.'})
            record_booking_event(c,row['id'],user['shop_id'],'booking_confirmed','barber',actor_id=user['id'],old_starts=row['starts'],new_starts=row['starts'],old_status='pending',new_status='confirmed')
        when=datetime.fromisoformat(row['starts']).astimezone(TZ).strftime('%d.%m.%Y, %H:%M')
        body=f'Programarea ta a fost confirmată de frizerie: {row["service_name"]} cu {row["staff_name"]}, {when}. Adresă: {row["address"]}, {row["city"]}. Pentru ajutor, contactează frizeria la {row["shop_phone"]}.'
        if row['email'] and email_configured():
            try:send_email(row['email'],f'Programare confirmată — {row["shop_name"]}',body)
            except Exception as e:print('Client booking confirmation email failed:',repr(e))
        if row['phone'] and os.environ.get('TWILIO_ACCOUNT_SID') and os.environ.get('TWILIO_AUTH_TOKEN') and os.environ.get('TWILIO_FROM'):
            try:sms_notice(row['phone'],f'TunsPro: programarea ta la {row["shop_name"]} a fost confirmată pentru {when}, cu {row["staff_name"]}.')
            except Exception as e:print('Client booking confirmation SMS failed:',repr(e))
        return self.json_response(200,{'ok':True,'message':'Programarea a fost confirmată.'})
    def save_client_note(self,d):
        phone=d.get('phone')
        if not isinstance(phone,str) or not (normalized:=normalize_ro_mobile(phone)):
            return self.json_response(400,{'error':'Numărul clientului nu este valid.'})
        note=d.get('note','')
        if not isinstance(note,str):return self.json_response(400,{'error':'Nota nu este validă.'})
        note=note.strip()
        if len(note)>500:return self.json_response(400,{'error':'Nota poate avea maximum 500 de caractere.'})
        client_key=hashlib.sha256(normalized.encode()).hexdigest()
        with connect() as c:
            user=self.auth(c)
            if not user:return self.json_response(401,{'error':'Conectează-te pentru a continua.'})
            sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(user['shop_id'],)).fetchone()
            if not active_subscription(sub):return self.json_response(403,{'error':'Fișa clienților este disponibilă cu un abonament activ.'})
            exists=c.execute("SELECT 1 FROM bookings WHERE shop_id=? AND phone=? AND client!='Date anonimizate' LIMIT 1",(user['shop_id'],normalized)).fetchone()
            if not exists:return self.json_response(404,{'error':'Clientul nu a fost găsit în această frizerie.'})
            if note:
                c.execute('INSERT INTO client_notes(shop_id,client_key,note,updated) VALUES(?,?,?,?) ON CONFLICT(shop_id,client_key) DO UPDATE SET note=excluded.note,updated=excluded.updated',(user['shop_id'],client_key,note,iso_now()))
            else:c.execute('DELETE FROM client_notes WHERE shop_id=? AND client_key=?',(user['shop_id'],client_key))
        return self.json_response(200,{'ok':True,'message':'Nota clientului a fost salvată.'})
    def checkout(self):
        with connect() as c:
            user=self.auth(c)
            if not user:return self.json_response(401,{'error':'Conectează-te pentru a continua.'})
            key=os.environ.get('STRIPE_SECRET_KEY')
            if not key:return self.json_response(503,{'error':'Plata reală nu este configurată încă. Adaugă STRIPE_SECRET_KEY în .env.'})
            shop=c.execute('SELECT * FROM shops WHERE id=?',(user['shop_id'],)).fetchone()
            public=os.environ.get('PUBLIC_URL',f'http://localhost:{PORT}')
            current=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(shop['id'],)).fetchone()
            body=self.body_json(); plan=str(body.get('plan','pro')).lower()
            if plan not in PLAN_PRICES:return self.json_response(400,{'error':'Alege planul PRO sau BUSINESS.'})
            if active_subscription(current) and current['stripe_customer_id']:
                if current['plan']=='pro' and plan=='business' and current['stripe_subscription_id']:
                    try:session=stripe_business_upgrade_session(key,current['stripe_customer_id'],current['stripe_subscription_id'],public+'/?payment=success#dashboard-abonament')
                    except ValueError as e:return self.json_response(400,{'error':str(e)})
                    return self.json_response(200,{'url':session['url']})
                params=urllib.parse.urlencode({'customer':current['stripe_customer_id'],'return_url':public+'/#dashboard-abonament'}).encode()
                req=urllib.request.Request('https://api.stripe.com/v1/billing_portal/sessions',data=params);req.add_header('Authorization','Bearer '+key)
                response=json.loads(urllib.request.urlopen(req,timeout=20).read());return self.json_response(200,{'url':response['url']})
            params={'mode':'subscription','managed_payments[enabled]':'false','success_url':public+'/?payment=success#dashboard-abonament','cancel_url':public+'/?payment=cancelled#dashboard-abonament','customer_email':user['email'],'client_reference_id':str(user['id']),'metadata[shop_id]':str(shop['id']),'metadata[plan]':plan,'line_items[0][quantity]':'1','line_items[0][price_data][currency]':'ron','line_items[0][price_data][unit_amount]':str(PLAN_PRICES[plan]),'line_items[0][price_data][recurring][interval]':'month','line_items[0][price_data][product_data][name]':f'TunsPro {plan.upper()}','line_items[0][price_data][product_data][description]':f'Abonament lunar TunsPro {plan.upper()}'}
            params['subscription_data[metadata][shop_id]']=str(shop['id']);params['subscription_data[metadata][plan]']=plan
            req=urllib.request.Request('https://api.stripe.com/v1/checkout/sessions',data=urllib.parse.urlencode(params).encode());req.add_header('Authorization','Bearer '+key)
            response=json.loads(urllib.request.urlopen(req,timeout=20).read());return self.json_response(200,{'url':response['url']})
    def stripe_webhook(self):
        raw=self.read_request_body();sig=self.headers.get('Stripe-Signature','')
        secrets_by_mode={'live':os.environ.get('STRIPE_WEBHOOK_SECRET_LIVE',''),'test':os.environ.get('STRIPE_WEBHOOK_SECRET_TEST','') or os.environ.get('STRIPE_WEBHOOK_SECRET_SANDBOX','')}
        legacy=os.environ.get('STRIPE_WEBHOOK_SECRET','')
        if not any(secrets_by_mode.values()) and not legacy:return self.json_response(503,{'error':'Webhook Stripe neconfigurat.'})
        try:event=json.loads(raw)
        except (ValueError,UnicodeDecodeError):return self.json_response(400,{'error':'Eveniment Stripe invalid.'})
        parts=[x.strip().split('=',1) for x in sig.split(',') if '=' in x]
        timestamp=next((value for key,value in parts if key=='t'),'0')
        signatures=[value for key,value in parts if key=='v1']
        event_mode='live' if bool(event.get('livemode')) else 'test';secret=secrets_by_mode[event_mode] or legacy
        expected=hmac.new(secret.encode(),(timestamp+'.').encode()+raw,hashlib.sha256).hexdigest() if secret else ''
        valid_signature=bool(secret) and any(hmac.compare_digest(expected,candidate) for candidate in signatures)
        try: fresh=abs(datetime.now().timestamp()-int(timestamp))<300
        except ValueError:fresh=False
        if not fresh or not valid_signature:return self.json_response(400,{'error':'Semnătură invalidă.'})
        event_id=str(event.get('id',''));typ=str(event.get('type',''));event_created=int(event.get('created',0) or 0);obj=event.get('data',{}).get('object',{})
        if not event_id or not typ or not isinstance(obj,dict):return self.json_response(400,{'error':'Eveniment Stripe incomplet.'})
        received=iso_now();checkout_sub=obj.get('subscription') if typ=='checkout.session.completed' and obj.get('mode')=='subscription' else None
        checkout_details=stripe_subscription_details(checkout_sub) if checkout_sub and obj.get('payment_status')=='paid' else None
        try:
            with connect() as c:
                c.execute('BEGIN IMMEDIATE')
                inserted=c.execute('INSERT OR IGNORE INTO stripe_events(event_id,event_type,event_created,received) VALUES(?,?,?,?)',(event_id,typ,event_created,received)).rowcount
                if not inserted:return self.json_response(200,{'received':True,'duplicate':True})
                if typ=='checkout.session.completed' and obj.get('mode')=='subscription':
                    shop_id=int(obj.get('metadata',{}).get('shop_id','0') or 0);stripe_sub=obj.get('subscription');status='active' if obj.get('payment_status')=='paid' else 'incomplete';plan=obj.get('metadata',{}).get('plan','pro')
                    if shop_id and stripe_sub and plan in PLAN_PRICES:
                        details=checkout_details or {};period_start=datetime.fromtimestamp(details['current_period_start'],timezone.utc).isoformat() if details.get('current_period_start') else None;paid_until=datetime.fromtimestamp(details['current_period_end'],timezone.utc).isoformat() if details.get('current_period_end') else None
                        updated=c.execute("UPDATE subscriptions SET status=?,plan=?,stripe_customer_id=?,stripe_subscription_id=?,stripe_event_created=?,current_period_start=COALESCE(?,current_period_start),paid_until=COALESCE(?,paid_until),payment_failed_at=NULL,cancel_at_period_end=0,cancel_at=NULL WHERE shop_id=? AND stripe_event_created<=?",(status,plan,obj.get('customer'),stripe_sub,event_created,period_start,paid_until,shop_id,event_created))
                        if updated.rowcount:record_subscription_event(c,event_id,typ,event_created,shop_id,stripe_sub,status,plan,period_start,paid_until)
                elif typ.startswith('customer.subscription.'):
                    stripe_sub=obj.get('id');status=obj.get('status','active');paid_until=datetime.fromtimestamp(obj['current_period_end'],timezone.utc).isoformat() if obj.get('current_period_end') else None;period_start=datetime.fromtimestamp(obj['current_period_start'],timezone.utc).isoformat() if obj.get('current_period_start') else None
                    item_plans=[x.get('price',{}).get('metadata',{}).get('plan') for x in obj.get('items',{}).get('data',[]) if x.get('price',{}).get('metadata',{}).get('plan') in PLAN_PRICES]
                    plan=(item_plans[0] if item_plans else None) or obj.get('metadata',{}).get('plan');shop_id=int(obj.get('metadata',{}).get('shop_id','0') or 0)
                    if stripe_sub:
                        cancel_at=datetime.fromtimestamp(obj['cancel_at'],timezone.utc).isoformat() if obj.get('cancel_at') else None
                        updated=c.execute('UPDATE subscriptions SET status=?,plan=COALESCE(?,plan),stripe_customer_id=COALESCE(?,stripe_customer_id),paid_until=COALESCE(?,paid_until),current_period_start=COALESCE(?,current_period_start),cancel_at_period_end=?,cancel_at=?,payment_failed_at=CASE WHEN ? IN (\'active\',\'trialing\') THEN NULL ELSE payment_failed_at END,stripe_event_created=? WHERE stripe_subscription_id=? AND stripe_event_created<=?',(status,plan,obj.get('customer'),paid_until,period_start,int(bool(obj.get('cancel_at_period_end'))),cancel_at,status,event_created,stripe_sub,event_created))
                        if updated.rowcount==0 and shop_id:
                            updated=c.execute('UPDATE subscriptions SET status=?,plan=COALESCE(?,plan),stripe_customer_id=?,stripe_subscription_id=?,paid_until=COALESCE(?,paid_until),current_period_start=COALESCE(?,current_period_start),cancel_at_period_end=?,cancel_at=?,payment_failed_at=CASE WHEN ? IN (\'active\',\'trialing\') THEN NULL ELSE payment_failed_at END,stripe_event_created=? WHERE shop_id=? AND stripe_event_created<=?',(status,plan,obj.get('customer'),stripe_sub,paid_until,period_start,int(bool(obj.get('cancel_at_period_end'))),cancel_at,status,event_created,shop_id,event_created))
                        if updated.rowcount:
                            linked=c.execute('SELECT shop_id,plan FROM subscriptions WHERE stripe_subscription_id=?',(stripe_sub,)).fetchone()
                            record_subscription_event(c,event_id,typ,event_created,linked['shop_id'] if linked else shop_id,stripe_sub,status,plan or (linked['plan'] if linked else None),period_start,paid_until,bool(obj.get('cancel_at_period_end')),cancel_at)
                elif typ in ('invoice.payment_succeeded','invoice.payment_failed','invoice.paid'):
                    invoice_id=obj.get('id');stripe_sub=obj.get('subscription');stripe_sub=stripe_sub.get('id') if isinstance(stripe_sub,dict) else stripe_sub;livemode=int(bool(event.get('livemode')))
                    if not stripe_sub:stripe_sub=(obj.get('parent',{}).get('subscription_details',{}) or {}).get('subscription')
                    if isinstance(stripe_sub,dict):stripe_sub=stripe_sub.get('id')
                    paid=typ in ('invoice.payment_succeeded','invoice.paid');out_of_band=bool(paid and obj.get('paid_out_of_band'));amount=int(obj.get('amount_paid',0) if paid else obj.get('amount_due',0));currency=str(obj.get('currency','ron')).lower();paid_at=datetime.fromtimestamp(obj.get('status_transitions',{}).get('paid_at') or event_created,timezone.utc).isoformat() if paid else None
                    if invoice_id:
                        shop=c.execute('SELECT shop_id FROM subscriptions WHERE stripe_subscription_id=?',(stripe_sub,)).fetchone() if stripe_sub else None
                        metadata=obj.get('metadata',{}) or {};shop_id=shop['shop_id'] if shop else int(metadata.get('shop_id','0') or 0) or None
                        c.execute('''INSERT INTO payments(stripe_invoice_id,shop_id,stripe_customer_id,stripe_subscription_id,amount,currency,status,paid_at,invoice_url,created,stripe_event_created,stripe_payment_intent_id,livemode,paid_out_of_band)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(stripe_invoice_id) DO UPDATE SET shop_id=COALESCE(excluded.shop_id,payments.shop_id),stripe_customer_id=COALESCE(excluded.stripe_customer_id,payments.stripe_customer_id),stripe_subscription_id=COALESCE(excluded.stripe_subscription_id,payments.stripe_subscription_id),amount=excluded.amount,currency=excluded.currency,status=CASE WHEN payments.refunded_amount>=excluded.amount AND payments.refunded_amount>0 THEN 'refunded' WHEN payments.refunded_amount>0 THEN 'partially_refunded' ELSE excluded.status END,paid_at=COALESCE(excluded.paid_at,payments.paid_at),invoice_url=COALESCE(excluded.invoice_url,payments.invoice_url),stripe_event_created=excluded.stripe_event_created,stripe_payment_intent_id=COALESCE(excluded.stripe_payment_intent_id,payments.stripe_payment_intent_id),livemode=excluded.livemode,paid_out_of_band=excluded.paid_out_of_band WHERE excluded.stripe_event_created>=payments.stripe_event_created''',
                            (invoice_id,shop_id,obj.get('customer'),stripe_sub,amount,currency,'paid' if paid else 'failed',paid_at,obj.get('hosted_invoice_url'),received,event_created,obj.get('payment_intent'),livemode,int(out_of_band)))
                    if stripe_sub:
                        new_status='active' if paid else 'past_due';failed_at=datetime.fromtimestamp(event_created,timezone.utc).isoformat()
                        updated=c.execute("UPDATE subscriptions SET status=CASE WHEN status='canceled' THEN status ELSE ? END,payment_failed_at=CASE WHEN status='canceled' THEN payment_failed_at WHEN ?=1 THEN NULL ELSE COALESCE(payment_failed_at,?) END,stripe_event_created=? WHERE stripe_subscription_id=? AND stripe_event_created<=?",(new_status,int(paid),failed_at,event_created,stripe_sub,event_created))
                        if updated.rowcount:
                            linked=c.execute('SELECT shop_id,plan,current_period_start,paid_until,status,cancel_at_period_end,cancel_at FROM subscriptions WHERE stripe_subscription_id=?',(stripe_sub,)).fetchone()
                            record_subscription_event(c,event_id,typ,event_created,linked['shop_id'] if linked else None,stripe_sub,linked['status'] if linked else new_status,linked['plan'] if linked else None,linked['current_period_start'] if linked else None,linked['paid_until'] if linked else None,bool(linked['cancel_at_period_end']) if linked else False,linked['cancel_at'] if linked else None)
                elif typ=='charge.refunded':
                    charge_id=obj.get('id');invoice_id=obj.get('invoice');payment_intent=obj.get('payment_intent');invoice_id=invoice_id.get('id') if isinstance(invoice_id,dict) else invoice_id;payment_intent=payment_intent.get('id') if isinstance(payment_intent,dict) else payment_intent;currency=str(obj.get('currency','ron')).lower();refund_items=(obj.get('refunds',{}) or {}).get('data',[]);livemode=int(bool(event.get('livemode')))
                    for refund in refund_items:
                        refund_id=refund.get('id')
                        if not refund_id:continue
                        payment=c.execute('SELECT stripe_invoice_id,shop_id,amount,refunded_amount FROM payments WHERE stripe_invoice_id=? OR stripe_payment_intent_id=? ORDER BY CASE WHEN stripe_invoice_id=? THEN 0 ELSE 1 END LIMIT 1',(invoice_id,payment_intent,invoice_id)).fetchone() if invoice_id or payment_intent else None
                        c.execute('''INSERT INTO refunds(stripe_refund_id,stripe_invoice_id,stripe_charge_id,shop_id,amount,currency,status,created,livemode,stripe_event_created) VALUES(?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(stripe_refund_id) DO UPDATE SET stripe_invoice_id=COALESCE(excluded.stripe_invoice_id,refunds.stripe_invoice_id),stripe_charge_id=COALESCE(excluded.stripe_charge_id,refunds.stripe_charge_id),shop_id=COALESCE(excluded.shop_id,refunds.shop_id),amount=excluded.amount,currency=excluded.currency,status=excluded.status,created=MIN(refunds.created,excluded.created),livemode=excluded.livemode,stripe_event_created=excluded.stripe_event_created WHERE excluded.stripe_event_created>=refunds.stripe_event_created''',(refund_id,payment['stripe_invoice_id'] if payment else invoice_id,charge_id,payment['shop_id'] if payment else None,int(refund.get('amount',0)),currency,refund.get('status','succeeded'),datetime.fromtimestamp(refund.get('created',event_created),timezone.utc).isoformat(),livemode,event_created))
                    if invoice_id or payment_intent:
                        payment=c.execute('SELECT stripe_invoice_id,amount FROM payments WHERE stripe_invoice_id=? OR stripe_payment_intent_id=? ORDER BY CASE WHEN stripe_invoice_id=? THEN 0 ELSE 1 END LIMIT 1',(invoice_id,payment_intent,invoice_id)).fetchone()
                        if payment:
                            refund_total=c.execute("SELECT COALESCE(SUM(amount),0) FROM refunds WHERE stripe_invoice_id=? AND status='succeeded' AND livemode=?",(payment['stripe_invoice_id'],livemode)).fetchone()[0]
                            pay_status='refunded' if refund_total>=payment['amount'] and payment['amount']>0 else 'partially_refunded' if refund_total else 'paid'
                            c.execute('UPDATE payments SET refunded_amount=?,status=? WHERE stripe_invoice_id=? AND livemode=?',(refund_total,pay_status,payment['stripe_invoice_id'],livemode))
                elif typ in ('refund.created','refund.updated','refund.failed'):
                    refund_id=obj.get('id');charge_id=obj.get('charge');charge_id=charge_id.get('id') if isinstance(charge_id,dict) else charge_id;payment_intent=obj.get('payment_intent');payment_intent=payment_intent.get('id') if isinstance(payment_intent,dict) else payment_intent;livemode=int(bool(event.get('livemode')))
                    if refund_id:
                        payment=c.execute('SELECT stripe_invoice_id,shop_id,amount FROM payments WHERE stripe_payment_intent_id=? AND livemode=? LIMIT 1',(payment_intent,livemode)).fetchone() if payment_intent else None
                        prior=c.execute('SELECT stripe_invoice_id,shop_id FROM refunds WHERE stripe_refund_id=?',(refund_id,)).fetchone()
                        invoice_id=payment['stripe_invoice_id'] if payment else (prior['stripe_invoice_id'] if prior else None);shop_id=payment['shop_id'] if payment else (prior['shop_id'] if prior else None)
                        c.execute('''INSERT INTO refunds(stripe_refund_id,stripe_invoice_id,stripe_charge_id,shop_id,amount,currency,status,created,livemode,stripe_event_created) VALUES(?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(stripe_refund_id) DO UPDATE SET stripe_invoice_id=COALESCE(excluded.stripe_invoice_id,refunds.stripe_invoice_id),stripe_charge_id=COALESCE(excluded.stripe_charge_id,refunds.stripe_charge_id),shop_id=COALESCE(excluded.shop_id,refunds.shop_id),amount=excluded.amount,currency=excluded.currency,status=excluded.status,created=MIN(refunds.created,excluded.created),livemode=excluded.livemode,stripe_event_created=excluded.stripe_event_created WHERE excluded.stripe_event_created>=refunds.stripe_event_created''',(refund_id,invoice_id,charge_id,shop_id,int(obj.get('amount',0)),str(obj.get('currency','ron')).lower(),obj.get('status','succeeded'),datetime.fromtimestamp(obj.get('created',event_created),timezone.utc).isoformat(),livemode,event_created))
                        if invoice_id:
                            payment=c.execute('SELECT stripe_invoice_id,amount FROM payments WHERE stripe_invoice_id=? AND livemode=?',(invoice_id,livemode)).fetchone()
                            if payment:
                                refund_total=c.execute("SELECT COALESCE(SUM(amount),0) FROM refunds WHERE stripe_invoice_id=? AND status='succeeded' AND livemode=?",(invoice_id,livemode)).fetchone()[0]
                                pay_status='refunded' if refund_total>=payment['amount'] and payment['amount']>0 else 'partially_refunded' if refund_total else 'paid'
                                c.execute('UPDATE payments SET refunded_amount=?,status=? WHERE stripe_invoice_id=? AND livemode=?',(refund_total,pay_status,invoice_id,livemode))
        except (TypeError,ValueError,KeyError) as e:
            print('Stripe webhook payload rejected:',type(e).__name__,flush=True)
            return self.json_response(400,{'error':'Datele evenimentului Stripe nu sunt valide.'})
        return self.json_response(200,{'received':True})

if __name__=='__main__':
    try:
        if create_database_backup('pre-startup'):
            print('Pre-startup database backup completed and verified.',flush=True)
    except Exception as e:
        raise RuntimeError('Pre-startup database backup failed; refusing to start.') from e
    init_db()
    threading.Thread(target=database_backup_loop,daemon=True).start()
    threading.Thread(target=anonymization_loop,daemon=True).start()
    threading.Thread(target=reminder_loop,daemon=True).start()
    print(f'TunsPro running at http://localhost:{PORT}')
    http.server.ThreadingHTTPServer(('0.0.0.0',PORT),Handler).serve_forever()
