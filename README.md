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
- E-mailuri prin Resend API sau SMTP și SMS prin Twilio, toate opționale.
- Recuperarea parolei contului frizeriei prin link de unică folosință, cu expirare după 30 de minute și invalidarea sesiunilor existente.

## Plăți reale

Copiază `.env.example` ca `.env`. Configurează `STRIPE_SECRET_KEY` și `PUBLIC_URL`. Creează endpointuri webhook pentru Stripe Live și Sandbox către `PUBLIC_URL/api/webhooks/stripe`, cu evenimentele `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_succeeded`, `invoice.payment_failed`, `invoice.paid`, `charge.refunded`, `refund.created`, `refund.updated` și `refund.failed`. Salvează separat secretele endpointurilor în `STRIPE_WEBHOOK_SECRET_LIVE` și `STRIPE_WEBHOOK_SECRET_TEST` în Render. Semnătura este verificată pe corpul brut, în limita temporală Stripe; ID-urile evenimentelor și rambursărilor sunt deduplicate, iar actualizările de abonament/plată protejează datele mai noi de evenimente sosite în altă ordine. Pentru dezvoltare folosește chei de test Stripe. Checkout creează abonamentul lunar; webhook-ul, nu redirectul din browser, stabilește dacă frizeria este activă.

Istoricul local începe cu evenimentele primite după configurarea webhook-ului; Stripe poate avea tranzacții mai vechi care nu au fost importate în aplicație. În Admin, venitul lunar include doar facturile efectiv încasate în Live, minus rambursările și înainte de comisioane. Plățile test și facturile achitate în afara Stripe nu sunt numărate ca încasări Stripe. Nu stocăm datele complete ale cardului.

Stripe suportă comercianți din România și abonamente recurente prin Checkout; verifică setările și disponibilitatea actuală ale contului înainte de lansare: [disponibilitate Stripe](https://stripe.com/global), [API Checkout Sessions](https://docs.stripe.com/api/checkout/sessions/create).

## Notificări

Pentru e-mail, configurează preferabil `RESEND_API_KEY` și `RESEND_FROM` în Render, după verificarea domeniului expeditor în Resend. Aplicația folosește Resend când ambele sunt configurate și păstrează `SMTP_*` ca alternativă. Pentru Gmail SMTP folosește `smtp.gmail.com`, portul `587`, utilizatorul Gmail și o parolă de aplicație când contul cere asta; parola se păstrează doar în Render, nu în repository. La rezervare, aplicația trimite confirmări clientului (e-mailul este obligatoriu în formular), plus notificări proprietarului în funcție de preferințele setate în dashboard. SMS se trimite către client și frizerie când integrarea Twilio este configurată.

Resetarea parolei folosește același furnizor de e-mail și `PUBLIC_URL` ca să construiască linkul. Cererile arată același mesaj indiferent dacă e-mailul există; linkurile sunt limitate la o utilizare și se invalidează după resetare. Pentru dezvoltare, testează într-un cont și o bază SQLite temporare; nu cere resetarea parolei unui cont real doar pentru a verifica e-mailul.

## Înainte de lansare

- Completează profilul cu datele reale ale frizeriei, serviciile, prețurile, personalul și programul. Aplicația nu creează automat frizerii sau programări demo; verifică datele existente înainte de a le modifica și nu șterge rezervări reale.
- Baza SQLite este pe discul persistent Render (`/var/data`). Render face snapshot-uri automate zilnice, păstrate cel puțin 7 zile. Verifică periodic fila **Disks** a serviciului. Restaurarea unui snapshot înlocuiește tot conținutul discului, inclusiv schimbările de după acel moment; exportă separat datele importante înainte de o restaurare.
- Pentru e-mail, setează `RESEND_API_KEY` și `RESEND_FROM` (expeditor de pe un domeniu verificat) sau completează `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` și `SMTP_FROM` în Render. `PUBLIC_URL` este necesar pentru linkurile de administrare/resetare. Confirmă că furnizorul acceptă expeditorul. Pentru SMS sunt necesare `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` și `TWILIO_FROM`; până atunci interfața nu poate livra mesaje SMS.
- Testează o rezervare completă pe mobil și desktop: serviciu, frizer, dată, oră, date client, confirmare, anulare și reprogramare. Verifică și că o a doua rezervare nu poate ocupa același interval.
- Verifică accesul public și rezervările pentru planurile FREE, PRO și BUSINESS în Stripe Sandbox. Nu considera plata reușită doar din redirect; verifică starea confirmată de webhook.
- Configurează Resend API sau SMTP în Render și confirmă că pagina de rezervare raportează e-mailul ca trimis. Dacă niciun furnizor nu este configurat, confirmarea rămâne pe ecran și e-mailul apare ca neconfigurat.
- SMS-ul prin Twilio rămâne opțional până la activarea contului și configurarea variabilelor `TWILIO_*`.
- Verifică notificările de anulare și reprogramare pentru client și frizerie, inclusiv când furnizorul de e-mail/SMS nu este configurat.
- Confirmă politica de confidențialitate, datele de contact și informațiile operatorului înainte de promovarea publică.
- Configurează și verifică o copie de siguranță a bazei SQLite de pe discul persistent Render și procedura de restaurare.

## Publicare pe Render

Fișierul `render.yaml` pregătește serviciul web și discul persistent pentru baza SQLite. Discul persistent cere un plan Render cu plată; verifică prețul afișat în cont înainte să creezi serviciul. Aplicația este cu o singură instanță.

1. Creează un cont GitHub și un repository privat. Încarcă în repository conținutul acestui folder (`outputs/tunspro`), fără fișierul `.env`.
2. Creează un cont Render și conectează GitHub. Din Render alege **New → Blueprint**, apoi selectează repository-ul.
3. La configurare, introdu `PUBLIC_URL` ca URL-ul serviciului Render (de exemplu `https://tunspro.onrender.com`; verifică numele disponibil în dashboard). Adaugă cheia Stripe de test în `STRIPE_SECRET_KEY`. Nu pune chei secrete în repository, în fișierele publice sau în mesaje.
4. După publicare, verifică URL-ul aplicației și corectează `PUBLIC_URL` în **Environment** dacă diferă. În Stripe, creează câte un webhook în Live și Sandbox la `https://<domeniul-aplicației>/api/webhooks/stripe`, selectează cele unsprezece evenimente din secțiunea **Plăți reale**, apoi copiază fiecare secret în variabila corespunzătoare din Render (`STRIPE_WEBHOOK_SECRET_LIVE` sau `STRIPE_WEBHOOK_SECRET_TEST`) și redeployează.
5. E-mailul și SMS-ul sunt opționale; completează variabilele lor în Render când ai conturile furnizorilor. Testează cu Stripe în modul test înainte să treci la chei live.

## Panoul Admin

Configurează `ADMIN_EMAIL` și `ADMIN_PASSWORD` în **Render → Environment**. Parola trebuie să aibă cel puțin 16 caractere. La pornirea aplicației, contul de administrator este creat sau sincronizat cu aceste valori; dacă schimbi parola, redeployează serviciul. Panoul este la `https://<domeniul-aplicației>/#admin-login`. Nu folosi o parolă comună cu alte conturi și nu o pune în repository.

## Securitate și recuperarea bazei de date

- Administratorii, frizeriile și clienții au sesiuni separate. Rutele administrative verifică sesiunea pe server; conturile frizeriilor sunt limitate la propriul `shop_id`, iar clienții la propriile rezervări. Parolele sunt stocate cu PBKDF2, iar sesiunile folosesc tokenuri aleatoare păstrate ca hash. Schimbarea parolei administratorului invalidează sesiunile administrative existente.
- Cererile API de modificare trebuie să provină din aceeași origine. API-ul limitează dimensiunea corpului și numărul de cereri pe IP și rută, inclusiv autentificările. Sunt trimise antete HTTP de protecție și cookie-uri `Secure` când `COOKIE_SECURE=1`. Schimbările administrative importante și autentificările administratorului sunt în jurnalul `admin_audit`.
- Secretele Stripe, Resend/SMTP și Twilio se configurează numai în **Render → Environment**. Nu introduce valori reale în GitHub, în codul clientului sau în loguri. Semnătura webhook-urilor Stripe Live și Sandbox este verificată pe server folosind corpul brut al cererii.
- Înaintea inițializării sau migrării schemei, serverul creează o copie SQLite și îi verifică integritatea; o copie invalidă oprește pornirea înainte de modificarea schemei. Apoi creează copii zilnice în `/var/data/backups`, în afara directorului public, păstrând cel mult 14 copii din ultimele 14 zile. Administratorul poate descărca și o copie manuală din panou; trateaz-o ca date confidențiale.
- Pentru recuperare rapidă, folosește **Render Dashboard → serviciul → Disks → Restore** și selectează un snapshot anterior. Restaurarea snapshotului înlocuiește întregul disc și pierde modificările ulterioare. Render creează snapshoturi zilnice pentru discurile persistente și le păstrează cel puțin 7 zile ([documentația Render](https://render.com/docs/disks)). Pentru o copie SQLite din `/var/data/backups`, oprește serviciul, păstrează separat fișierul curent, restaurează copia la calea `TUNSPRO_DB`, verifică `PRAGMA quick_check` și repornește serviciul. După recuperare, verifică autentificarea, rezervările și abonamentele.
- Copiile locale sunt pe același disc ca baza de date și nu înlocuiesc o copie separată. Descarcă periodic copia manuală de administrator într-un spațiu privat, separat de Render. Nu încărca backupul în repository.

Pentru un repository nou, publică doar fișierele aplicației; `.gitignore` exclude `.env`, baza de date și cache-urile. Păstrează cheile Stripe doar în setările protejate ale serviciului Render. Dacă cheia de test a fost distribuită public, rotește-o din Stripe înainte de folosire.

Rulează serverul în spatele unui HTTPS reverse proxy, setează `PUBLIC_URL` și `COOKIE_SECURE=1`, păstrează `.env` și fișierul SQLite în afara directoarelor publice și configurează backup pentru baza de date. Adaugă un domeniu public pentru webhook-uri Stripe. Fișierul SQLite este potrivit pentru un MVP cu instanță unică; pentru trafic și disponibilitate ridicate, mută datele într-un serviciu PostgreSQL administrat.
