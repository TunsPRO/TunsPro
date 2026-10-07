# TunsPro MVP

Aplicație web multi-frizerie cu API Python și SQLite. Folosește biblioteca standard Python; nu are nevoie de instalări de pachete.

## Pornire locală

1. Instalează Python 3.10 sau mai nou.
2. În acest folder, pornește `python server.py`.
3. Deschide `http://localhost:8765`.

Baza de date `tunspro.sqlite3` este creată la prima pornire. Nu deschide `index.html` direct ca fișier; aplicația folosește API-ul de pe server.

## Ce include

- Înregistrare cu parolă hashu-uită PBKDF2, sesiuni server-side și cookie HttpOnly.
- Izolarea datelor fiecărei frizerii, servicii, angajați și programări într-o bază SQLite.
- Căutare publică numai pentru frizeriile cu abonament activ, cu filtrare după frizerie, oraș, servicii și personal.
- Program săptămânal per frizer, intervale de 15 minute, verificarea suprapunerilor și rezervări tranzacționale.
- Anularea programărilor din dashboard.
- Conturi de client separate, cu programări, istoric, favorite și ștergerea contului.
- Recenzii publice permise doar după programări marcate ca încheiate.
- Panou Admin pentru statistici de platformă și vizibilitatea profilurilor.
- Pagini de marketing pentru frizerii: link public, distribuire și statistici.
- Abonament recurent de 49 RON prin Stripe Checkout; webhooks activează/dezactivează accesul pe baza stării din Stripe.
- E-mailuri prin SMTP și SMS prin Twilio, ambele opționale.
- Recuperarea parolei contului frizeriei prin link de unică folosință, cu expirare după 30 de minute și invalidarea sesiunilor existente.

## Plăți reale

Copiază `.env.example` ca `.env`. Configurează `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` și `PUBLIC_URL`. Creează endpoint webhook Stripe către `PUBLIC_URL/api/webhooks/stripe` și abonează-l la `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_succeeded` și `invoice.payment_failed`. Pentru dezvoltare folosește chei de test Stripe. Checkout creează abonamentul lunar la 49 RON; webhook-ul, nu redirectul din browser, stabilește dacă frizeria este activă.

Stripe suportă comercianți din România și abonamente recurente prin Checkout; verifică setările și disponibilitatea actuală ale contului înainte de lansare: [disponibilitate Stripe](https://stripe.com/global), [API Checkout Sessions](https://docs.stripe.com/api/checkout/sessions/create).

## Notificări

Completează variabilele `SMTP_*` pentru confirmările prin e-mail și `TWILIO_*` pentru SMS. La rezervare, aplicația trimite confirmări clientului (e-mailul este opțional în formular), plus notificări proprietarului în funcție de preferințele setate în dashboard. SMS se trimite către client și frizerie când integrarea Twilio este configurată.

Resetarea parolei folosește același SMTP și `PUBLIC_URL` ca să construiască linkul. Cererile arată același mesaj indiferent dacă e-mailul există; linkurile sunt limitate la o utilizare și se invalidează după resetare. Pentru dezvoltare, testează într-un cont și o bază SQLite temporare; nu cere resetarea parolei unui cont real doar pentru a verifica e-mailul.

## Înainte de lansare

- Completează profilul cu datele reale ale frizeriei, serviciile, prețurile, personalul și programul. Aplicația nu creează automat frizerii sau programări demo; verifică datele existente înainte de a le modifica și nu șterge rezervări reale.
- Baza SQLite este pe discul persistent Render (`/var/data`). Render face snapshot-uri automate zilnice, păstrate cel puțin 7 zile. Verifică periodic fila **Disks** a serviciului. Restaurarea unui snapshot înlocuiește tot conținutul discului, inclusiv schimbările de după acel moment; exportă separat datele importante înainte de o restaurare.
- Pentru e-mail, setează `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` și `PUBLIC_URL` în Render. Confirmă că `SMTP_FROM` este acceptat de furnizor. Pentru SMS sunt necesare `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` și `TWILIO_FROM`; până atunci interfața nu poate livra mesaje SMS.
- Testează o rezervare completă pe mobil și desktop: serviciu, frizer, dată, oră, date client, confirmare, anulare și reprogramare. Verifică și că o a doua rezervare nu poate ocupa același interval.
- Verifică accesul public și rezervările pentru planurile FREE, PRO și BUSINESS în Stripe Sandbox. Nu considera plata reușită doar din redirect; verifică starea confirmată de webhook.
- Configurează SMTP în Render și confirmă că pagina de rezervare raportează e-mailul ca trimis. Dacă SMTP nu este configurat, confirmarea rămâne pe ecran și e-mailul apare ca neconfigurat.
- SMS-ul prin Twilio rămâne opțional până la activarea contului și configurarea variabilelor `TWILIO_*`.
- Verifică notificările de anulare și reprogramare pentru client și frizerie, inclusiv când furnizorul de e-mail/SMS nu este configurat.
- Confirmă politica de confidențialitate, datele de contact și informațiile operatorului înainte de promovarea publică.
- Configurează și verifică o copie de siguranță a bazei SQLite de pe discul persistent Render și procedura de restaurare.

## Publicare pe Render

Fișierul `render.yaml` pregătește serviciul web și discul persistent pentru baza SQLite. Discul persistent cere un plan Render cu plată; verifică prețul afișat în cont înainte să creezi serviciul. Aplicația este cu o singură instanță.

1. Creează un cont GitHub și un repository privat. Încarcă în repository conținutul acestui folder (`outputs/tunspro`), fără fișierul `.env`.
2. Creează un cont Render și conectează GitHub. Din Render alege **New → Blueprint**, apoi selectează repository-ul.
3. La configurare, introdu `PUBLIC_URL` ca URL-ul serviciului Render (de exemplu `https://tunspro.onrender.com`; verifică numele disponibil în dashboard). Adaugă cheia Stripe de test în `STRIPE_SECRET_KEY`. Nu pune chei secrete în repository, în fișierele publice sau în mesaje.
4. După publicare, verifică URL-ul aplicației și corectează `PUBLIC_URL` în **Environment** dacă diferă. În Stripe, creează webhook-ul la `https://<domeniul-aplicației>/api/webhooks/stripe`, selectează evenimentele enumerate în secțiunea **Plăți reale**, apoi copiază secretul webhook în `STRIPE_WEBHOOK_SECRET` din Render și redeployează.
5. E-mailul și SMS-ul sunt opționale; completează variabilele lor în Render când ai conturile furnizorilor. Testează cu Stripe în modul test înainte să treci la chei live.

## Panoul Admin

Configurează `ADMIN_EMAIL` și `ADMIN_PASSWORD` în **Render → Environment**. Parola trebuie să aibă cel puțin 16 caractere. La pornirea aplicației, contul de administrator este creat sau sincronizat cu aceste valori; dacă schimbi parola, redeployează serviciul. Panoul este la `https://<domeniul-aplicației>/#admin-login`. Nu folosi o parolă comună cu alte conturi și nu o pune în repository.

Pentru un repository nou, publică doar fișierele aplicației; `.gitignore` exclude `.env`, baza de date și cache-urile. Păstrează cheile Stripe doar în setările protejate ale serviciului Render. Dacă cheia de test a fost distribuită public, rotește-o din Stripe înainte de folosire.

Rulează serverul în spatele unui HTTPS reverse proxy, setează `PUBLIC_URL` și `COOKIE_SECURE=1`, păstrează `.env` și fișierul SQLite în afara directoarelor publice și configurează backup pentru baza de date. Adaugă un domeniu public pentru webhook-uri Stripe. Fișierul SQLite este potrivit pentru un MVP cu instanță unică; pentru trafic și disponibilitate ridicate, mută datele într-un serviciu PostgreSQL administrat.
