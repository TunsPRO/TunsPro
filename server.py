"""TunsPro MVP API. Python standard library + SQLite; Stripe/SMTP/Twilio are optional via .env."""
from __future__ import annotations
import hashlib, hmac, http.server, json, os, secrets, smtplib, sqlite3, ssl, urllib.parse, urllib.request
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from http import cookies
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('TUNSPRO_DB', ROOT / 'tunspro.sqlite3'))
PORT = int(os.environ.get('PORT', '8765'))
TZ = ZoneInfo('Europe/Bucharest')
PRICE = 4900

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
        CREATE TABLE IF NOT EXISTS shops(id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, city TEXT NOT NULL, address TEXT NOT NULL, phone TEXT NOT NULL, tagline TEXT DEFAULT '', created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY, shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE, name TEXT NOT NULL, description TEXT DEFAULT '', duration INTEGER NOT NULL, price INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS staff(id INTEGER PRIMARY KEY, shop_id INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE, name TEXT NOT NULL, role TEXT DEFAULT 'Frizer', active INTEGER NOT NULL DEFAULT 1, weekly_schedule TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY, shop_id INTEGER NOT NULL REFERENCES shops(id), service_id INTEGER NOT NULL REFERENCES services(id), staff_id INTEGER NOT NULL REFERENCES staff(id), client TEXT NOT NULL, phone TEXT NOT NULL, email TEXT DEFAULT '', starts TEXT NOT NULL, ends TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed', created TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS bookings_staff_time ON bookings(staff_id, starts, ends, status);
        CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY, shop_id INTEGER UNIQUE NOT NULL REFERENCES shops(id) ON DELETE CASCADE, status TEXT NOT NULL DEFAULT 'inactive', paid_until TEXT, stripe_customer_id TEXT, stripe_subscription_id TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(shop_id INTEGER PRIMARY KEY REFERENCES shops(id) ON DELETE CASCADE, notification_email INTEGER NOT NULL DEFAULT 1, notification_sms INTEGER NOT NULL DEFAULT 0);
        ''')

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
def active_subscription(row):
    if not row or row['status'] not in ('active', 'trialing') or not row['paid_until']: return False
    try: return datetime.fromisoformat(row['paid_until'].replace('Z', '+00:00')) > now_utc()
    except ValueError: return False

def shop_public(c, shop):
    sub = c.execute('SELECT * FROM subscriptions WHERE shop_id=?', (shop['id'],)).fetchone()
    if not active_subscription(sub): return None
    svc = c.execute('SELECT id,name,description,duration,price FROM services WHERE shop_id=? AND active=1 ORDER BY id', (shop['id'],)).fetchall()
    team = c.execute('SELECT id,name,role FROM staff WHERE shop_id=? AND active=1 ORDER BY id', (shop['id'],)).fetchall()
    if not svc or not team: return None
    return {'id':shop['id'],'name':shop['name'],'slug':shop['slug'],'city':shop['city'],'address':shop['address'],'phone':shop['phone'],'tagline':shop['tagline'],'services':[dict(x) for x in svc],'team':[dict(x) for x in team]}

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
        if n>1000000: raise ValueError('Cerere prea mare.')
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
    def get(self):
        u=urllib.parse.urlparse(self.path); path=urllib.parse.unquote(u.path); q=urllib.parse.parse_qs(u.query)
        if path=='/api/me':
            with connect() as c:
                user=self.auth(c)
                if not user:return self.json_response(200,{'user':None})
                shop=c.execute('SELECT * FROM shops WHERE id=?',(user['shop_id'],)).fetchone(); sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(shop['id'],)).fetchone()
                return self.json_response(200,{'user':{'email':user['email'],'owner':user['owner']},'shop':dict(shop),'subscription':{'active':active_subscription(sub),'status':sub['status'],'paid_until':sub['paid_until']}})
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
                    svc=c.execute('SELECT * FROM services WHERE shop_id=? ORDER BY id',(shop['id'],)).fetchall(); team=c.execute('SELECT * FROM staff WHERE shop_id=? ORDER BY id',(shop['id'],)).fetchall(); bookings=c.execute("SELECT b.*,s.name service_name,s.duration,s.price,t.name staff_name FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id WHERE b.shop_id=? AND b.status='confirmed' ORDER BY b.starts",(shop['id'],)).fetchall(); sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(shop['id'],)).fetchone(); settings=c.execute('SELECT * FROM settings WHERE shop_id=?',(shop['id'],)).fetchone()
                    return self.json_response(200,{'shop':dict(shop),'services':[dict(x) for x in svc],'team':[dict(x) for x in team],'bookings':[dict(x) for x in bookings],'subscription':{'active':active_subscription(sub),'status':sub['status'],'paid_until':sub['paid_until']},'notifications':dict(settings) if settings else {}})
        if path.startswith('/api/'): return self.json_response(404,{'error':'Nu am găsit pagina.'})
        if path not in ('/','/index.html','/client.js','/styles.css'):return self.send_error(404)
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
            if path=='/api/auth/logout':
                with connect() as c:
                    user=self.auth(c)
                    if user:c.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
                return self.json_response(200,{'ok':True},self.clear_session())
            if path=='/api/public/bookings':return self.create_booking(self.body_json())
            if path=='/api/manage/bookings/cancel':return self.cancel_booking(self.body_json())
            if path=='/api/billing/checkout':return self.checkout()
            if path=='/api/webhooks/stripe':return self.stripe_webhook()
            return self.json_response(404,{'error':'Nu am găsit ruta.'})
        except ValueError as e:return self.json_response(400,{'error':str(e)})
        except Exception as e:print('POST error:',repr(e));return self.json_response(500,{'error':'A apărut o eroare. Încearcă din nou.'})
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
                    if fields:c.execute('UPDATE shops SET '+','.join(f'{k}=?' for k in fields)+' WHERE id=?',(*fields.values(),sid))
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
                    ids=[]
                    for x in data.get('team',[]):
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
            c.execute('INSERT INTO subscriptions(shop_id,status) VALUES(? ,"inactive")',(sid,)); c.execute('INSERT INTO settings(shop_id) VALUES(?)',(sid,))
            c.execute('INSERT INTO services(shop_id,name,description,duration,price,active) VALUES(?,?,?,?,?,1)',(sid,'Tuns clasic','Tuns și styling',30,60))
            # Add an owner barber with a weekday schedule; services remain owner-configurable.
            sched={str(i):['09:00','19:00'] for i in range(6)}
            c.execute('INSERT INTO staff(shop_id,name,role,weekly_schedule) VALUES(?,?,?,?)',(sid,d['owner'].strip(),'Frizer',json.dumps(sched)))
        return self.json_response(201,{'ok':True,'slug':slug},self.set_session(uid))
    def login(self,d):
        with connect() as c:
            user=c.execute('SELECT * FROM users WHERE email=?',(str(d.get('email','')).lower().strip(),)).fetchone()
            if not user or not password_ok(str(d.get('password','')),user['password']):return self.json_response(401,{'error':'E-mailul sau parola nu sunt corecte.'})
        return self.json_response(200,{'ok':True},self.set_session(user['id']))
    def create_booking(self,d):
        if len(str(d.get('client','')).strip())<2 or len(''.join(x for x in str(d.get('phone','')) if x.isdigit()))<9:raise ValueError('Completează numele și un număr de telefon valid.')
        slug=str(d.get('slug','')); day=date.fromisoformat(d.get('date','')); start_time=time.fromisoformat(d.get('time','')); starts=datetime.combine(day,start_time,TZ)
        if starts<=datetime.now(TZ):raise ValueError('Alege o oră viitoare.')
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            shop=c.execute('SELECT * FROM shops WHERE slug=?',(slug,)).fetchone(); sub=c.execute('SELECT * FROM subscriptions WHERE shop_id=?',(shop['id'],)).fetchone() if shop else None
            if not shop or not active_subscription(sub):return self.json_response(403,{'error':'Frizeria nu acceptă programări momentan.'})
            service=c.execute('SELECT * FROM services WHERE id=? AND shop_id=? AND active=1',(int(d.get('service_id',0)),shop['id'])).fetchone(); staff=c.execute('SELECT * FROM staff WHERE id=? AND shop_id=? AND active=1',(int(d.get('staff_id',0)),shop['id'])).fetchone()
            if not service or not staff:raise ValueError('Serviciul sau frizerul nu este disponibil.')
            sched=json.loads(staff['weekly_schedule'] or '{}').get(str(day.weekday()))
            finish=starts+timedelta(minutes=service['duration'])
            if not sched or starts.time()<time.fromisoformat(sched[0]) or finish.time()>time.fromisoformat(sched[1]):raise ValueError('Ora aleasă este în afara programului frizerului.')
            collision=c.execute("SELECT 1 FROM bookings WHERE staff_id=? AND status='confirmed' AND starts<? AND ends>?",(staff['id'],finish.isoformat(),starts.isoformat())).fetchone()
            if collision:raise ValueError('Ora tocmai a fost rezervată. Alege alt interval.')
            cur=c.execute('INSERT INTO bookings(shop_id,service_id,staff_id,client,phone,email,starts,ends,status,created) VALUES(?,?,?,?,?,?,?,?,?,?)',(shop['id'],service['id'],staff['id'],str(d.get('client','')).strip(),str(d.get('phone','')).strip(),str(d.get('email','')).strip(),starts.isoformat(),finish.isoformat(),'confirmed',iso_now())); booking_id=cur.lastrowid
            opts=c.execute('SELECT * FROM settings WHERE shop_id=?',(shop['id'],)).fetchone(); owner=c.execute('SELECT u.email FROM users u WHERE u.id=?',(shop['user_id'],)).fetchone()
        when=starts.strftime('%A %d %B, %H:%M')
        if opts and opts['notification_email']:
            try:
                smtp_notice(owner['email'],f'Programare nouă — {shop["name"]}',f'{d.get("client")} a rezervat {service["name"]} cu {staff["name"]}, {when}. Telefon: {d.get("phone")}')
            except Exception as e:print('Email notification failed:',repr(e))
        if d.get('email'):
            try:smtp_notice(d['email'],f'Programare confirmată — {shop["name"]}',f'Programarea ta: {service["name"]} cu {staff["name"]}, {when}. Adresă: {shop["address"]}, {shop["city"]}.')
            except Exception as e:print('Client confirmation failed:',repr(e))
        if opts and opts['notification_sms']:
            try:
                sms_notice(shop['phone'],f'TunsPro: programare nouă la {when}. Client: {d.get("client")}, {d.get("phone")}')
                sms_notice(d.get('phone',''),f'TunsPro: programarea ta la {shop["name"]} este confirmată pentru {when}.')
            except Exception as e:print('SMS notification failed:',repr(e))
        return self.json_response(201,{'ok':True,'booking_id':booking_id,'message':'Programarea este confirmată.'})
    def cancel_booking(self,d):
        with connect() as c:
            user=self.auth(c)
            if not user:return self.json_response(401,{'error':'Conectează-te pentru a continua.'})
            row=c.execute("SELECT b.*,s.name service_name,t.name staff_name FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff t ON t.id=b.staff_id WHERE b.id=? AND b.shop_id=? AND b.status='confirmed'",(int(d.get('id',0)),user['shop_id'])).fetchone()
            if not row:return self.json_response(404,{'error':'Programarea nu a fost găsită.'})
            c.execute("UPDATE bookings SET status='cancelled' WHERE id=?",(row['id'],))
            client_email=row['email'];client_phone=row['phone'];client=row['client'];starts=datetime.fromisoformat(row['starts']).astimezone(TZ).strftime('%d.%m.%Y, %H:%M'); shop_name=user['shop_name']
        try:
            if client_email:smtp_notice(client_email,f'Programare anulată — {shop_name}',f'Programarea ta de la {shop_name}, {starts}, a fost anulată de frizerie.')
            sms_notice(client_phone,f'TunsPro: programarea ta de la {shop_name}, {starts}, a fost anulata de frizerie.')
        except Exception as e:print('Cancellation notice failed:',repr(e))
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
            params={'mode':'subscription','success_url':public+'/?payment=success#dashboard-abonament','cancel_url':public+'/?payment=cancelled#dashboard-abonament','customer_email':user['email'],'client_reference_id':str(user['id']),'metadata[shop_id]':str(shop['id']),'line_items[0][quantity]':'1','line_items[0][price_data][currency]':'ron','line_items[0][price_data][unit_amount]':str(PRICE),'line_items[0][price_data][recurring][interval]':'month','line_items[0][price_data][product_data][name]':'Abonament TunsPro','line_items[0][price_data][product_data][description]':'Acces lunar la programări și administrare TunsPro'}
            params['subscription_data[metadata][shop_id]']=str(shop['id'])
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
                sid=int(obj.get('metadata',{}).get('shop_id','0') or 0); stripe_sub=obj.get('subscription'); status='active' if obj.get('payment_status')=='paid' else 'incomplete'
                if sid:c.execute('UPDATE subscriptions SET status=?,stripe_customer_id=?,stripe_subscription_id=? WHERE shop_id=?',(status,obj.get('customer'),stripe_sub,sid))
            elif typ.startswith('customer.subscription.') or typ.startswith('invoice.payment_'):
                stripe_sub=obj.get('id') if typ.startswith('customer.subscription.') else obj.get('subscription'); status=('active' if typ=='invoice.payment_succeeded' else obj.get('status','active')); paid_until=datetime.fromtimestamp(obj.get('current_period_end',0),timezone.utc).isoformat() if obj.get('current_period_end') else None
                if typ=='invoice.payment_failed':status='past_due'
                cur=c.execute('UPDATE subscriptions SET status=?,stripe_customer_id=COALESCE(?,stripe_customer_id),stripe_subscription_id=COALESCE(?,stripe_subscription_id),paid_until=COALESCE(?,paid_until) WHERE stripe_subscription_id=?',(status,obj.get('customer'),stripe_sub,paid_until,stripe_sub))
                if cur.rowcount==0 and obj.get('metadata',{}).get('shop_id'):
                    c.execute('UPDATE subscriptions SET status=?,stripe_customer_id=?,stripe_subscription_id=?,paid_until=COALESCE(?,paid_until) WHERE shop_id=?',(status,obj.get('customer'),stripe_sub,paid_until,int(obj['metadata']['shop_id'])))
        return self.json_response(200,{'received':True})

if __name__=='__main__':
    init_db()
    print(f'TunsPro running at http://localhost:{PORT}')
    http.server.ThreadingHTTPServer(('0.0.0.0',PORT),Handler).serve_forever()
