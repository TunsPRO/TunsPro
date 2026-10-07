"""TunsPro MVP API. Python standard library + SQLite; Stripe/SMTP/Twilio are optional via .env."""
from __future__ import annotations
import base64, hashlib, hmac, http.server, json, os, re, secrets, smtplib, sqlite3, ssl, urllib.parse, urllib.request, uuid
import unicodedata
import threading
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from http import cookies
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('TUNSPRO_DB', ROOT / 'tunspro.sqlite3'))
MEDIA_DIR = DB_PATH.parent / 'uploads'
PORT = int(os.environ.get('PORT', '8765'))
TZ = ZoneInfo('Europe/Bucharest')
PLAN_PRICES = {'pro': 4900, 'business': 9900}

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

def init_db():
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, owner TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS shops(id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, city TEXT NOT NULL, address TEXT NOT NULL, phone TEXT NOT NULL, tagline TEXT DEFAULT '', photos TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY, shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE, name TEXT NOT NULL, description TEXT DEFAULT '', duration INTEGER NOT NULL, price INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS staff(id INTEGER PRIMARY KEY, shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE, name TEXT NOT NULL, role TEXT DEFAULT 'Frizer', active INTEGER NOT NULL DEFAULT 1, weekly_schedule TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY, shop_id INTEGER NOT NULL REFERENCES shops(id), service_id INTEGER NOT NULL REFERENCES services(id), staff_id INTEGER NOT NULL REFERENCES staff(id), client TEXT NOT NULL, phone TEXT NOT NULL, email TEXT DEFAULT '', starts TEXT NOT NULL, ends TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed', created TEXT NOT NULL, reminder_at TEXT, reminder_sent INTEGER NOT NULL DEFAULT 0, customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL, price_at_booking INTEGER, manage_token_hash TEXT);
        CREATE INDEX IF NOT EXISTS bookings_staff_time ON bookings(staff_id, starts, ends, status);
        CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY, shop_id INTEGER UNIQUE NOT NULL REFERENCES shops(id) ON DELETE CASCADE, status TEXT NOT NULL DEFAULT 'inactive', paid_until TEXT, stripe_customer_id TEXT, stripe_subscription_id TEXT UNIQUE, plan TEXT NOT NULL DEFAULT 'free');
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(shop_id INTEGER PRIMARY KEY REFERENCES shops(id) ON DELETE CASCADE, notification_email INTEGER NOT NULL DEFAULT 1, notification_sms INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS client_sessions(token_hash TEXT PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE, expires TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS favorites(customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE, shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE, created TEXT NOT NULL, PRIMARY KEY(customer_id,shop_id));
        CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, booking_id INTEGER UNIQUE NOT NULL REFERENCES bookings(id) ON DELETE CASCADE, customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL, shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE, rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5), comment TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admin_sessions(token_hash TEXT PRIMARY KEY, admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE, expires TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS reviews_shop_created ON reviews(shop_id,created);
        ''')
        for table, column, definition in [('shops','photos',"TEXT NOT NULL DEFAULT '[]'"),('shops','listing_enabled','INTEGER NOT NULL DEFAULT 1'),('bookings','reminder_at','TEXT'),('bookings','reminder_sent','INTEGER NOT NULL DEFAULT 0'),('bookings','customer_id','INTEGER REFERENCES customers(id) ON DELETE SET NULL'),('bookings','price_at_booking','INTEGER'),('bookings','manage_token_hash','TEXT')]:
            if column not in {row['name'] for row in c.execute(f'PRAGMA table_info({table})')}:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        if 'plan' not in {row['name'] for row in c.execute('PRAGMA table_info(subscriptions)')}:
            c.execute("ALTER TABLE subscriptions ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
        c.execute("UPDATE subscriptions SET plan='pro' WHERE plan='free' AND status IN ('active','trialing') AND paid_until IS NOT NULL")
        # Existing databases need the bookings columns added before this index
        # is created; CREATE TABLE IF NOT EXISTS does not migrate old tables.
        c.execute('CREATE INDEX IF NOT EXISTS bookings_customer_start ON bookings(customer_id,starts)')
        admin_email=os.environ.get('ADMIN_EMAIL','').lower().strip(); admin_password=os.environ.get('ADMIN_PASSWORD','')
        if admin_email and len(admin_password)>=16 and '@' in admin_email:
            c.execute('DELETE FROM admins WHERE email<>?',(admin_email,))
            c.execute('INSERT INTO admins(email,password,created) VALUES(?,?,?) ON CONFLICT(email) DO UPDATE SET password=excluded.password',(admin_email,password_hash(admin_password),iso_now()))
    anonymize_old_bookings()

def anonymize_old_bookings():
    cutoff=(now_utc()-timedelta(days=365)).isoformat()
    with connect() as c:
        c.execute("UPDATE reviews SET customer_id=NULL WHERE booking_id IN (SELECT id FROM bookings WHERE julianday(ends)<julianday(?))",(cutoff,))
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
        if row['notification_email'] and row['email'] and os.environ.get('SMTP_HOST') and os.environ.get('SMTP_FROM'):
            try:smtp_notice(row['email'],f'Reamintire programare — {row["shop_name"]}',body);attempted=True
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

def active_subscription(row):
    if not row or row['plan'] not in PLAN_PRICES or row['status'] not in ('active', 'trialing') or not row['paid_until']: return False
    try: return datetime.fromisoformat(row['paid_until'].replace('Z', '+00:00')) > now_utc()
    except ValueError: return False

def shop_public(c, shop):
    sub = c.execute('SELECT * FROM subscriptions WHERE shop_id=?', (shop['id'],)).fetchone()
    if not active_subscription(sub) or not shop['listing_enabled']: return None
    svc = c.execute('SELECT id,name,description,duration,price FROM services WHERE shop_id=? AND active=1 ORDER BY id', (shop['id'],)).fetchall()
    team = c.execute('SELECT id,name,role,weekly_schedule FROM staff WHERE shop_id=? AND active=1 ORDER BY id', (shop['id'],)).fetchall()
    if not svc or not team: return None
    reviews=c.execute('SELECT rating,comment,created FROM reviews WHERE shop_id=? ORDER BY created DESC LIMIT 20',(shop['id'],)).fetchall()
    rating=c.execute('SELECT COUNT(*) count,AVG(rating) average FROM reviews WHERE shop_id=?',(shop['id'],)).fetchone()
    return {'id':shop['id'],'name':shop['name'],'slug':shop['slug'],'city':shop['city'],'address':shop['address'],'phone':shop['phone'],'tagline':shop['tagline'],'photos':json.loads(shop['photos'] or '[]'),'rating':round(rating['average'],1) if rating['average'] else None,'review_count':rating['count'],'reviews':[dict(x) for x in reviews],'services':[dict(x) for x in svc],'team':[{**dict(x),'weekly_schedule':json.loads(x['weekly_schedule'] or '{}')} for x in team]}

def normalize_ro_mobile(value):
    digits=''.join(ch for ch in value if ch.isdigit())
    if digits.startswith('0040'):digits=digits[2:]
    if digits.startswith('0'):digits='40'+digits[1:]
    if not digits.startswith('40'):return ''
    return '+'+digits if re.fullmatch(r'407[0-9]{8}',digits) else ''

def smtp_notice(to_email, subject, body):
    host=os.environ.get('SMTP_HOST'); sender=os.environ.get('SMTP_FROM');
    if not host or not sender or not to_email: return
    msg=EmailMessage(); msg['Subject']=subject; msg['From']=sender; msg['To']=to_email; msg.set_content(body)
    port=int(os.environ.get('SMTP_PORT','587')); user=os.environ.get('SMTP_USER',''); password=os.environ.get('SMTP_PASSWORD','')
    with smtplib.SMTP(host,port,timeout=12) as s:
        s.starttls(context=ssl.create_default_context())
        if user: s.login(user,password)
        s.send_message(msg)

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
    def json_response(self, status, payload, extra=None):
        body=json.dumps(payload,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store')
        for k,v in (extra or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(body)
    def body_json(self):
        n=int(self.headers.get('Content-Length','0'))
        if n>2200000: raise ValueError('Cerere prea mare.')
        return json.loads(self.rfile.read(n) or b'{}')
    def auth(self,c):
        jar=cookies.SimpleCookie(self.headers.get('Cookie','')); token=jar['tunspro_session'].value if 'tunspro_session' in jar else ''
        if not token: return None
        return c.execute('SELECT u.*,s.id shop_id,s.name shop_name,s.slug shop_slug FROM sessions x JOIN users u ON u.id=x.user_id JOIN shops s ON s.user_id=u.id WHERE x.token_hash=? AND x.expires>?',(hashlib.sha256(token.encode()).hexdigest(),iso_now())).fetchone()
    def set_session(self,user_id):
        token=secrets.token_urlsafe(32); expiry=now_utc()+timedelta(days=30)
        with connect() as c: c.execute('INSERT INTO sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user_id,expiry.isoformat()))
        secure='; Secure' if os.environ.get('COOKIE_SECURE','0')=='1' else ''
        return {'Set-Cookie':f'tunspro_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000{secure}'}
    def clear_session(self): return {'Set-Cookie':'tunspro_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0'}
    def client_auth(self,c):
        jar=cookies.SimpleCookie(self.headers.get('Cookie','')); token=jar['tunspro_client'].value if 'tunspro_client' in jar else ''
        if not token:return None
        return c.execute('SELECT * FROM customers WHERE id=(SELECT customer_id FROM client_sessions WHERE token_hash=? AND expires>?)',(hashlib.sha256(token.encode()).hexdigest(),iso_now())).fetchone()
    def set_client_session(self,customer_id):
        token=secrets.token_urlsafe(32); expiry=now_utc()+timedelta(days=30)
        with connect() as c:c.execute('INSERT INTO client_sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),customer_id,expiry.isoformat()))
        secure='; Secure' if os.environ.get('COOKIE_SECURE','0')=='1' else ''
        return {'Set-Cookie':f'tunspro_client={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000{secure}'}
    def clear_client_session(self):return {'Set-Cookie':'tunspro_client=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0'}
    def admin_auth(self,c):
        jar=cookies.SimpleCookie(self.headers.get('Cookie','')); token=jar['tunspro_admin'].value if 'tunspro_admin' in jar else ''
        if not token:return None
        return c.execute('SELECT * FROM admins WHERE id=(SELECT admin_id FROM admin_sessions WHERE token_hash=? AND expires>?)',(hashlib.sha256(token.encode()).hexdigest(),iso_now())).fetchone()
    def set_admin_session(self,admin_id):
        token=secrets.token_urlsafe(32); expiry=now_utc()+timedelta(hours=8)
        with connect() as c:c.execute('INSERT INTO admin_sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),admin_id,expiry.isoformat()))
        secure='; Secure' if os.environ.get('COOKIE_SECURE','0')=='1' else ''
        return {'Set-Cookie':f'tunspro_admin={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800{secure}'}
    def clear_admin_session(self):return {'Set-Cookie':'tunspro_admin=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'}
    def get(self):
        u=urllib.parse.urlparse(self.path); path=urllib.parse.unquote(u.path); q=urllib.parse.parse_qs(u.query)
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
                bookings=c.execute("SELECT b.id,b.client,b.phone,b.email,b.starts,b.ends,b.status,b.service_id,s.name service_name,s.duration,COALESCE(b.price_at_booking,s.price) price,t.name staff_name,sh.name shop_name,sh.slug shop_slug,sh.city,sh.address,sh.phone shop_phone FROM bookings b JOIN shops sh ON sh.id=b.shop_id JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id WHERE b.customer_id=? AND b.status IN ('confirmed','completed','cancelled') ORDER BY b.starts DESC",(customer['id'],)).fetchall()
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
                shops=c.execute('SELECT sh.id,sh.name,sh.slug,sh.city,sh.created,sh.listing_enabled,sub.status subscription_status,sub.paid_until,(SELECT COUNT(*) FROM bookings b WHERE b.shop_id=sh.id) booking_count FROM shops sh LEFT JOIN subscriptions sub ON sub.shop_id=sh.id ORDER BY sh.created DESC').fetchall()
                counts={'shops':c.execute('SELECT COUNT(*) FROM shops').fetchone()[0],'active_subscriptions':c.execute('SELECT COUNT(*) FROM subscriptions WHERE status IN (\'active\',\'trialing\') AND julianday(paid_until)>julianday(?)',(iso_now(),)).fetchone()[0],'customers':c.execute('SELECT COUNT(*) FROM customers').fetchone()[0],'bookings':c.execute('SELECT COUNT(*) FROM bookings').fetchone()[0]}
                owners=c.execute('SELECT u.id,u.email,u.owner,u.created,sh.name shop_name,sh.city FROM users u JOIN shops sh ON sh.user_id=u.id ORDER BY u.created DESC').fetchall()
                customers=c.execute('SELECT c.id,c.name,c.email,c.phone,c.created,COUNT(b.id) booking_count FROM customers c LEFT JOIN bookings b ON b.customer_id=c.id GROUP BY c.id ORDER BY c.created DESC').fetchall()
                bookings=c.execute('SELECT b.id,b.client,b.phone,b.starts,b.status,sh.name shop_name,s.name service_name FROM bookings b JOIN shops sh ON sh.id=b.shop_id JOIN services s ON s.id=b.service_id ORDER BY b.created DESC LIMIT 100').fetchall()
                subscriptions=c.execute('SELECT sh.id shop_id,sh.name shop_name,sh.city,sub.status,sub.paid_until FROM shops sh LEFT JOIN subscriptions sub ON sub.shop_id=sh.id ORDER BY sub.paid_until DESC').fetchall()
                return self.json_response(200,{'counts':counts,'shops':[dict(x) for x in shops],'owners':[dict(x) for x in owners],'customers':[dict(x) for x in customers],'bookings':[dict(x) for x in bookings],'subscriptions':[dict(x) for x in subscriptions]})
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
                        rows=c.execute("SELECT starts,ends FROM bookings WHERE staff_id=? AND status='confirmed' AND starts<? AND ends>?",(staff_id,end.isoformat(),start.isoformat())).fetchall()
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
                    svc=c.execute('SELECT * FROM services WHERE shop_id=? ORDER BY id',(shop['id'],)).fetchall(); team=c.execute('SELECT * FROM staff WHERE shop_id=? ORDER BY id',(shop['id'],)).fetchall(); bookings=c.execute("SELECT b.*,s.name service_name,s.duration,COALESCE(b.price_at_booking,s.price) price,t.name staff_name FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id WHERE b.shop_id=? AND b.status IN ('confirmed','completed') ORDER BY b.starts",(shop['id'],)).fetchall(); sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(shop['id'],)).fetchone(); settings=c.execute('SELECT * FROM settings WHERE shop_id=?',(shop['id'],)).fetchone()
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
                    return self.json_response(200,{'shop':shop_data,'services':[dict(x) for x in svc],'team':[dict(x) for x in team],'bookings':[dict(x) for x in bookings] if paid else [],'subscription':{'active':paid,'status':sub['status'],'plan':sub['plan'],'paid_until':sub['paid_until']},'notifications':dict(settings) if settings else {}})
        if path.startswith('/api/'): return self.json_response(404,{'error':'Nu am găsit pagina.'})
        if path not in ('/','/index.html','/client.js','/features.js','/styles.css'):return self.send_error(404)
        return super().do_GET()
    def do_GET(self):
        try:self.get()
        except Exception as e: print('GET error:',repr(e)); self.json_response(500,{'error':'A apărut o eroare. Încearcă din nou.'})
    def do_POST(self):
        path=urllib.parse.urlparse(self.path).path
        try:
            origin=self.headers.get('Origin')
            if origin and urllib.parse.urlparse(origin).netloc!=self.headers.get('Host'):return self.json_response(403,{'error':'Origine invalidă.'})
            if path=='/api/auth/register':return self.register(self.body_json())
            if path=='/api/auth/login':return self.login(self.body_json())
            if path=='/api/client/auth/register':return self.client_register(self.body_json())
            if path=='/api/client/auth/login':return self.client_login(self.body_json())
            if path=='/api/admin/login':return self.admin_login(self.body_json())
            if path=='/api/admin/logout':
                with connect() as c:
                    admin=self.admin_auth(c)
                    if admin:c.execute('DELETE FROM admin_sessions WHERE admin_id=?',(admin['id'],))
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
            if path=='/api/public/bookings/cancel':return self.cancel_public_booking(self.body_json())
            if path=='/api/client/favorites':return self.client_favorite(self.body_json())
            if path=='/api/client/bookings/cancel':return self.client_cancel_booking(self.body_json())
            if path=='/api/client/reviews':return self.create_review(self.body_json())
            if path=='/api/client/account/delete':return self.delete_client_account()
            if path=='/api/admin/listing':return self.admin_listing(self.body_json())
            if path=='/api/manage/photos':return self.upload_photo(self.body_json())
            if path=='/api/manage/bookings/cancel':return self.cancel_booking(self.body_json())
            if path=='/api/manage/bookings/complete':return self.complete_booking(self.body_json())
            if path=='/api/billing/checkout':return self.checkout()
            if path=='/api/webhooks/stripe':return self.stripe_webhook()
            return self.json_response(404,{'error':'Nu am găsit ruta.'})
        except ValueError as e:return self.json_response(400,{'error':str(e)})
        except Exception as e:print('POST error:',repr(e));detail=(json.loads(e.read()).get('error',{}).get('message','') if isinstance(e,urllib.error.HTTPError) else '');return self.json_response(500,{'error':('Stripe: '+detail) if detail else 'A apărut o eroare. Încearcă din nou.'})
    def do_PUT(self):
        path=urllib.parse.urlparse(self.path).path
        try:
            origin=self.headers.get('Origin')
            if origin and urllib.parse.urlparse(origin).netloc!=self.headers.get('Host'):return self.json_response(403,{'error':'Origine invalidă.'})
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
                        vals=(x['name'],x.get('role','Frizer'),json.dumps(weekly),int(bool(x.get('active',1))))
                        if x.get('id'):
                            c.execute('UPDATE staff SET name=?,role=?,weekly_schedule=?,active=? WHERE id=? AND shop_id=?',(*vals,int(x['id']),sid));ids.append(int(x['id']))
                        else:
                            cur=c.execute('INSERT INTO staff(shop_id,name,role,weekly_schedule,active) VALUES(?,?,?,?,?)',(sid,*vals));ids.append(cur.lastrowid)
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
            cur=c.execute('INSERT INTO shops(user_id,name,slug,city,address,phone,tagline,created) VALUES(?,?,?,?,?,?,?,?)',(uid,d['name'].strip(),slug,d['city'].strip(),d['address'].strip(),d['phone'].strip(),d.get('tagline',''),iso_now())); sid=cur.lastrowid
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
    def login(self,d):
        with connect() as c:
            user=c.execute('SELECT * FROM users WHERE email=?',(str(d.get('email','')).lower().strip(),)).fetchone()
            if not user or not password_ok(str(d.get('password','')),user['password']):return self.json_response(401,{'error':'E-mailul sau parola nu sunt corecte.'})
        return self.json_response(200,{'ok':True},self.set_session(user['id']))
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
        with connect() as c:customer=c.execute('SELECT * FROM customers WHERE email=?',(str(d.get('email','')).lower().strip(),)).fetchone()
        if not customer or not password_ok(str(d.get('password','')),customer['password']):return self.json_response(401,{'error':'E-mailul sau parola nu sunt corecte.'})
        return self.json_response(200,{'ok':True},self.set_client_session(customer['id']))
    def admin_login(self,d):
        email=str(d.get('email','')).lower().strip()
        with connect() as c:admin=c.execute('SELECT * FROM admins WHERE email=?',(email,)).fetchone()
        if not admin or not password_ok(str(d.get('password','')),admin['password']):return self.json_response(401,{'error':'E-mailul sau parola de administrator nu sunt corecte.'})
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
            row=c.execute("SELECT b.*,sh.name shop_name,sh.phone shop_phone FROM bookings b JOIN shops sh ON sh.id=b.shop_id WHERE b.id=? AND b.customer_id=? AND b.status='confirmed'",(int(d.get('id',0)),customer['id'])).fetchone()
            if not row:return self.json_response(404,{'error':'Programarea viitoare nu a fost găsită.'})
            if datetime.fromisoformat(row['starts'])<=now_utc():return self.json_response(400,{'error':'Programarea nu mai poate fi anulată din cont. Sună frizeria.'})
            c.execute("UPDATE bookings SET status='cancelled' WHERE id=?",(row['id'],))
        try:smtp_notice(customer['email'],f'Programare anulată — {row["shop_name"]}',f'Programarea ta a fost anulată. Pentru o nouă rezervare, caută frizeria în TunsPro.')
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
            if not self.admin_auth(c):return self.json_response(401,{'error':'Autentificare de administrator necesară.'})
            c.execute('UPDATE shops SET listing_enabled=? WHERE id=?',(int(bool(d.get('enabled'))),int(d.get('shop_id',0))))
            if not c.total_changes:return self.json_response(404,{'error':'Frizeria nu a fost găsită.'})
        return self.json_response(200,{'ok':True})
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
            shop=c.execute('SELECT * FROM shops WHERE slug=?',(slug,)).fetchone(); sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(shop['id'],)).fetchone() if shop else None
            if not shop or not shop['listing_enabled'] or not active_subscription(sub):return self.json_response(403,{'error':'Frizeria nu acceptă programări momentan.'})
            service=c.execute('SELECT * FROM services WHERE id=? AND shop_id=? AND active=1',(int(d.get('service_id',0)),shop['id'])).fetchone(); staff=c.execute('SELECT * FROM staff WHERE id=? AND shop_id=? AND active=1',(int(d.get('staff_id',0)),shop['id'])).fetchone()
            if not service or not staff:raise ValueError('Serviciul sau frizerul nu este disponibil.')
            sched=json.loads(staff['weekly_schedule'] or '{}').get(str(day.weekday()))
            finish=starts+timedelta(minutes=service['duration'])
            if not sched or starts.time()<time.fromisoformat(sched[0]) or finish.time()>time.fromisoformat(sched[1]):raise ValueError('Ora aleasă este în afara programului frizerului.')
            collision=c.execute("SELECT 1 FROM bookings WHERE staff_id=? AND status='confirmed' AND starts<? AND ends>?",(staff['id'],finish.isoformat(),starts.isoformat())).fetchone()
            if collision:raise ValueError('Ora tocmai a fost rezervată. Alege alt interval.')
            current=now_utc();reminder_at=starts-timedelta(hours=24);reminder_sent=0
            if reminder_at<=current:
                if starts>current+timedelta(hours=1):reminder_at=starts-timedelta(hours=1)
                else:reminder_sent=1
            cancel_token=secrets.token_urlsafe(32);token_hash=hashlib.sha256(cancel_token.encode()).hexdigest()
            cur=c.execute('INSERT INTO bookings(shop_id,service_id,staff_id,client,phone,email,starts,ends,status,created,reminder_at,reminder_sent,customer_id,price_at_booking,manage_token_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(shop['id'],service['id'],staff['id'],client_name,client_phone,client_email,starts.isoformat(),finish.isoformat(),'confirmed',iso_now(),reminder_at.isoformat(),reminder_sent,None,service['price'],token_hash)); booking_id=cur.lastrowid
            opts=c.execute('SELECT * FROM settings WHERE shop_id=?',(shop['id'],)).fetchone(); owner=c.execute('SELECT u.email FROM users u WHERE u.id=?',(shop['user_id'],)).fetchone()
        days_ro=['luni','marți','miercuri','joi','vineri','sâmbătă','duminică']
        months_ro=['ianuarie','februarie','martie','aprilie','mai','iunie','iulie','august','septembrie','octombrie','noiembrie','decembrie']
        when=f'{days_ro[day.weekday()]} {day.day} {months_ro[day.month-1]}, {starts:%H:%M}'
        manage_url=os.environ.get('PUBLIC_URL','').rstrip('/')+'/#anulare/'+cancel_token
        mail_status='unavailable';sms_status='unavailable'
        if os.environ.get('SMTP_HOST') and os.environ.get('SMTP_FROM'):
            try:
                smtp_notice(client_email,f'Programare confirmată — {shop["name"]}',f'Programarea ta: {service["name"]} cu {staff["name"]}, {when}. Adresă: {shop["address"]}, {shop["city"]}. Pentru anulare online: {manage_url}. Pentru modificare, contactează frizeria la {shop["phone"]}.')
                mail_status='sent'
            except Exception as e:mail_status='failed';print('Client confirmation failed:',repr(e))
        if os.environ.get('TWILIO_ACCOUNT_SID') and os.environ.get('TWILIO_AUTH_TOKEN') and os.environ.get('TWILIO_FROM'):
            try:
                sms_notice(client_phone,f'TunsPro: programarea ta la {shop["name"]} este înregistrată pentru {when}. Anulare online: {manage_url}.')
                sms_status='sent'
            except Exception as e:sms_status='failed';print('Client SMS confirmation failed:',repr(e))
        if opts and opts['notification_email'] and owner and os.environ.get('SMTP_HOST') and os.environ.get('SMTP_FROM'):
            try:smtp_notice(owner['email'],f'Programare nouă — {shop["name"]}',f'{client_name} a rezervat {service["name"]} cu {staff["name"]}, {when}. Telefon: {client_phone}')
            except Exception as e:print('Barber email notification failed:',repr(e))
        if opts and opts['notification_sms'] and os.environ.get('TWILIO_ACCOUNT_SID') and os.environ.get('TWILIO_AUTH_TOKEN') and os.environ.get('TWILIO_FROM'):
            try:sms_notice(shop['phone'],f'TunsPro: programare nouă la {when}. Client: {client_name}, {client_phone}')
            except Exception as e:print('Barber SMS notification failed:',repr(e))
        return self.json_response(201,{'ok':True,'booking_id':booking_id,'phone':client_phone,'cancel_token':cancel_token,'notifications':{'email':mail_status,'sms':sms_status},'message':'Programarea a fost înregistrată.'})
    def cancel_public_booking(self,d):
        token=str(d.get('token','')).strip()
        if len(token)<30:return self.json_response(400,{'error':'Linkul de anulare nu este valid.'})
        token_hash=hashlib.sha256(token.encode()).hexdigest()
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT b.*,sh.name shop_name,sh.phone shop_phone,s.name service_name,t.name staff_name FROM bookings b JOIN shops sh ON sh.id=b.shop_id JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id WHERE b.manage_token_hash=? AND b.status='confirmed'",(token_hash,)).fetchone()
            if not row:return self.json_response(404,{'error':'Programarea nu mai poate fi anulată. Verifică dacă a fost deja anulată sau contactează frizeria.'})
            if datetime.fromisoformat(row['starts'])<=now_utc():return self.json_response(400,{'error':'Programarea a început deja și nu mai poate fi anulată online.'})
            c.execute("UPDATE bookings SET status='cancelled' WHERE id=?",(row['id'],))
            settings=c.execute('SELECT * FROM settings WHERE shop_id=?',(row['shop_id'],)).fetchone();owner=c.execute('SELECT u.email FROM users u JOIN shops sh ON sh.user_id=u.id WHERE sh.id=?',(row['shop_id'],)).fetchone()
        when=datetime.fromisoformat(row['starts']).astimezone(TZ).strftime('%d.%m.%Y, %H:%M')
        if row['email'] and os.environ.get('SMTP_HOST') and os.environ.get('SMTP_FROM'):
            try:smtp_notice(row['email'],f'Programare anulată — {row["shop_name"]}',f'Programarea ta din {when} a fost anulată. Dacă te-ai răzgândit, poți face o nouă rezervare pe TunsPro.')
            except Exception as e:print('Client cancellation confirmation failed:',repr(e))
        if settings and settings['notification_email'] and owner and os.environ.get('SMTP_HOST') and os.environ.get('SMTP_FROM'):
            try:smtp_notice(owner['email'],f'Programare anulată — {row["shop_name"]}',f'{row["client"]} a anulat programarea din {when}.')
            except Exception as e:print('Barber cancellation notice failed:',repr(e))
        if settings and settings['notification_sms'] and os.environ.get('TWILIO_ACCOUNT_SID') and os.environ.get('TWILIO_AUTH_TOKEN') and os.environ.get('TWILIO_FROM'):
            try:sms_notice(row['shop_phone'],f'TunsPro: clientul {row["client"]} a anulat programarea din {when}.')
            except Exception as e:print('Barber cancellation SMS failed:',repr(e))
        return self.json_response(200,{'ok':True,'message':'Programarea a fost anulată.'})
    def cancel_booking(self,d):
        with connect() as c:
            user=self.auth(c)
            if not user:return self.json_response(401,{'error':'Conectează-te pentru a continua.'})
            sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(user['shop_id'],)).fetchone()
            if not active_subscription(sub):return self.json_response(403,{'error':'Gestionează programările cu un abonament PRO sau BUSINESS activ.'})
            row=c.execute("SELECT b.*,s.name service_name,t.name staff_name FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id WHERE b.id=? AND b.shop_id=? AND b.status='confirmed'",(int(d.get('id',0)),user['shop_id'])).fetchone()
            if not row:return self.json_response(404,{'error':'Programarea nu a fost găsită.'})
            c.execute("UPDATE bookings SET status='cancelled' WHERE id=?",(row['id'],))
            client_email=row['email'];client_phone=row['phone'];client=row['client'];starts=datetime.fromisoformat(row['starts']).astimezone(TZ).strftime('%d.%m.%Y, %H:%M'); shop_name=user['shop_name']
        try:
            if client_email:smtp_notice(client_email,f'Programare anulată — {shop_name}',f'Programarea ta de la {shop_name}, {starts}, a fost anulată de frizerie.')
            sms_notice(client_phone,f'TunsPro: programarea ta de la {shop_name}, {starts}, a fost anulata de frizerie.')
        except Exception as e:print('Cancellation notice failed:',repr(e))
        return self.json_response(200,{'ok':True})
    def complete_booking(self,d):
        with connect() as c:
            user=self.auth(c)
            if not user:return self.json_response(401,{'error':'Conectează-te pentru a continua.'})
            sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(user['shop_id'],)).fetchone()
            if not active_subscription(sub):return self.json_response(403,{'error':'Gestionează programările cu un abonament PRO sau BUSINESS activ.'})
            row=c.execute("SELECT id,ends FROM bookings WHERE id=? AND shop_id=? AND status='confirmed'",(int(d.get('id',0)),user['shop_id'])).fetchone()
            if not row:return self.json_response(404,{'error':'Programarea nu a fost găsită.'})
            if datetime.fromisoformat(row['ends'])>now_utc():return self.json_response(400,{'error':'Programarea poate fi încheiată după ora rezervată.'})
            c.execute("UPDATE bookings SET status='completed' WHERE id=?",(row['id'],))
        return self.json_response(200,{'ok':True})
    def checkout(self):
        with connect() as c:
            user=self.auth(c)
            if not user:return self.json_response(401,{'error':'Conectează-te pentru a continua.'})
            key=os.environ.get('STRIPE_SECRET_KEY')
            if not key:return self.json_response(503,{'error':'Plata reală nu este configurată încă. Adaugă STRIPE_SECRET_KEY în .env.'})
            shop=c.execute('SELECT * FROM shops WHERE id=?',(user['shop_id'],)).fetchone()
            public=os.environ.get('PUBLIC_URL',f'http://localhost:{PORT}')
            current=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(shop['id'],)).fetchone()
            if active_subscription(current) and current['stripe_customer_id']:
                params=urllib.parse.urlencode({'customer':current['stripe_customer_id'],'return_url':public+'/#dashboard-abonament'}).encode()
                req=urllib.request.Request('https://api.stripe.com/v1/billing_portal/sessions',data=params);req.add_header('Authorization','Bearer '+key)
                response=json.loads(urllib.request.urlopen(req,timeout=20).read());return self.json_response(200,{'url':response['url']})
            body=self.body_json(); plan=str(body.get('plan','pro')).lower()
            if plan not in PLAN_PRICES:return self.json_response(400,{'error':'Alege planul PRO sau BUSINESS.'})
            if current and current['plan']==plan and active_subscription(current) and current['stripe_customer_id']:
                params=urllib.parse.urlencode({'customer':current['stripe_customer_id'],'return_url':public+'/#dashboard-abonament'}).encode()
                req=urllib.request.Request('https://api.stripe.com/v1/billing_portal/sessions',data=params);req.add_header('Authorization','Bearer '+key)
                response=json.loads(urllib.request.urlopen(req,timeout=20).read());return self.json_response(200,{'url':response['url']})
            params={'mode':'subscription','managed_payments[enabled]':'false','success_url':public+'/?payment=success#dashboard-abonament','cancel_url':public+'/?payment=cancelled#dashboard-abonament','customer_email':user['email'],'client_reference_id':str(user['id']),'metadata[shop_id]':str(shop['id']),'metadata[plan]':plan,'line_items[0][quantity]':'1','line_items[0][price_data][currency]':'ron','line_items[0][price_data][unit_amount]':str(PLAN_PRICES[plan]),'line_items[0][price_data][recurring][interval]':'month','line_items[0][price_data][product_data][name]':f'TunsPro {plan.upper()}','line_items[0][price_data][product_data][description]':f'Abonament lunar TunsPro {plan.upper()}'}
            params['subscription_data[metadata][shop_id]']=str(shop['id']);params['subscription_data[metadata][plan]']=plan
            req=urllib.request.Request('https://api.stripe.com/v1/checkout/sessions',data=urllib.parse.urlencode(params).encode());req.add_header('Authorization','Bearer '+key)
            response=json.loads(urllib.request.urlopen(req,timeout=20).read());return self.json_response(200,{'url':response['url']})
    def stripe_webhook(self):
        raw=self.rfile.read(int(self.headers.get('Content-Length','0')));secret=os.environ.get('STRIPE_WEBHOOK_SECRET','');sig=self.headers.get('Stripe-Signature','')
        if not secret:return self.json_response(503,{'error':'Webhook Stripe neconfigurat.'})
        parts=[x.strip().split('=',1) for x in sig.split(',') if '=' in x]
        timestamp=next((value for key,value in parts if key=='t'),'0')
        signatures=[value for key,value in parts if key=='v1']
        expected=hmac.new(secret.encode(),(timestamp+'.').encode()+raw,hashlib.sha256).hexdigest()
        # Stripe signs timestamp + dot + raw body.
        try: fresh=abs(datetime.now().timestamp()-int(timestamp))<300
        except ValueError:fresh=False
        if not fresh or not any(hmac.compare_digest(expected,candidate) for candidate in signatures):return self.json_response(400,{'error':'Semnătură invalidă.'})
        event=json.loads(raw); obj=event.get('data',{}).get('object',{}); typ=event.get('type','')
        with connect() as c:
            if typ=='checkout.session.completed' and obj.get('mode')=='subscription':
                sid=int(obj.get('metadata',{}).get('shop_id','0') or 0); stripe_sub=obj.get('subscription'); status='active' if obj.get('payment_status')=='paid' else 'incomplete'; plan=obj.get('metadata',{}).get('plan','pro')
                details=stripe_subscription_details(stripe_sub) if status=='active' and stripe_sub else None
                paid_until=datetime.fromtimestamp(details['current_period_end'],timezone.utc).isoformat() if details and details.get('current_period_end') else None
                if sid and plan in PLAN_PRICES:c.execute('UPDATE subscriptions SET status=?,plan=?,stripe_customer_id=?,stripe_subscription_id=?,paid_until=COALESCE(?,paid_until) WHERE shop_id=?',(status,plan,obj.get('customer'),stripe_sub,paid_until,sid))
            elif typ.startswith('customer.subscription.') or typ.startswith('invoice.payment_'):
                stripe_sub=obj.get('id') if typ.startswith('customer.subscription.') else obj.get('subscription'); status=('active' if typ=='invoice.payment_succeeded' else obj.get('status','active')); paid_until=datetime.fromtimestamp(obj.get('current_period_end',0),timezone.utc).isoformat() if obj.get('current_period_end') else None
                plan=obj.get('metadata',{}).get('plan')
                if typ=='invoice.payment_failed':status='past_due'
                cur=c.execute('UPDATE subscriptions SET status=?,plan=COALESCE(?,plan),stripe_customer_id=COALESCE(?,stripe_customer_id),stripe_subscription_id=COALESCE(?,stripe_subscription_id),paid_until=COALESCE(?,paid_until) WHERE stripe_subscription_id=?',(status,plan,obj.get('customer'),stripe_sub,paid_until,stripe_sub))
                if cur.rowcount==0 and obj.get('metadata',{}).get('shop_id'):
                    c.execute('UPDATE subscriptions SET status=?,plan=COALESCE(?,plan),stripe_customer_id=?,stripe_subscription_id=?,paid_until=COALESCE(?,paid_until) WHERE shop_id=?',(status,plan,obj.get('customer'),stripe_sub,paid_until,int(obj['metadata']['shop_id'])))
        return self.json_response(200,{'received':True})

if __name__=='__main__':
    init_db()
    threading.Thread(target=anonymization_loop,daemon=True).start()
    threading.Thread(target=reminder_loop,daemon=True).start()
    print(f'TunsPro running at http://localhost:{PORT}')
    http.server.ThreadingHTTPServer(('0.0.0.0',PORT),Handler).serve_forever()
