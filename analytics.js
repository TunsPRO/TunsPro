(() => {
  'use strict';
  const MEASUREMENT_ID = 'G-SEQ20S17EJ';
  const CONSENT_KEY = 'tunspro-analytics-consent-v1';
  let analyticsReady = false;
  let scriptLoading = false;
  let routeObserverStarted = false;

  const allowedRoutes = () => {
    const raw = decodeURIComponent((location.hash || '').slice(1).split('?')[0]);
    if (!raw) return { path: '/' };
    if (raw === 'cauta' || raw === 'preturi' || raw === 'barberi' || raw === 'despre' || raw === 'confirmare') return { path: '/' + raw };
    if (raw.startsWith('frizerie/')) {
      const slug = raw.slice('frizerie/'.length);
      if (/^[a-zA-Z0-9_-]{1,100}$/.test(slug)) return { path: '/frizerie/' + slug, profile: true };
    }
    if (raw === 'connect' || raw === 'login') return { path: '/' + raw };
    return null;
  };

  function consentValue() {
    try { return localStorage.getItem(CONSENT_KEY); } catch { return null; }
  }

  function deleteAnalyticsCookies() {
    document.cookie.split(';').forEach((entry) => {
      const name = entry.split('=')[0].trim();
      if (name === '_ga' || name.startsWith('_ga_') || name === '_gid' || name === '_gat') {
        document.cookie = name + '=; Max-Age=0; path=/; SameSite=Lax';
        document.cookie = name + '=; Max-Age=0; path=/; domain=' + location.hostname + '; SameSite=Lax';
      }
    });
  }

  function sendPageView() {
    if (!analyticsReady || consentValue() !== 'accepted') return;
    const route = allowedRoutes();
    if (!route) return;
    const title = document.title || 'TunsPro';
    const params = { page_title: title, page_location: location.origin + route.path };
    if (route.profile) gtag('event', 'view_barber_profile', { page_location: params.page_location });
    gtag('event', 'page_view', params);
  }

  function safeCampaignValue(name) {
    const value = new URLSearchParams(location.search).get(name);
    if (!value || !/^[a-zA-Z0-9._-]{1,100}$/.test(value)) return null;
    return value;
  }

  function loadAnalytics() {
    if (analyticsReady || scriptLoading || consentValue() !== 'accepted') return;
    scriptLoading = true;
    window.dataLayer = window.dataLayer || [];
    window.gtag = window.gtag || function(){ window.dataLayer.push(arguments); };
    gtag('consent', 'default', { analytics_storage: 'denied', ad_storage: 'denied', ad_user_data: 'denied', ad_personalization: 'denied' });
    const script = document.createElement('script');
    script.async = true;
    script.src = 'https://www.googletagmanager.com/gtag/js?id=' + encodeURIComponent(MEASUREMENT_ID);
    script.onload = () => {
      gtag('js', new Date());
      gtag('consent', 'update', { analytics_storage: 'granted', ad_storage: 'denied', ad_user_data: 'denied', ad_personalization: 'denied' });
      gtag('config', MEASUREMENT_ID, { send_page_view: false, allow_google_signals: false, allow_ad_personalization_signals: false });
      const campaign = {
        campaign_source: safeCampaignValue('utm_source'),
        campaign_medium: safeCampaignValue('utm_medium'),
        campaign_name: safeCampaignValue('utm_campaign')
      };
      analyticsReady = true;
      const route = allowedRoutes();
      if (route) {
        const params = { page_title: document.title || 'TunsPro', page_location: location.origin + route.path };
        if (campaign.campaign_source) params.campaign_source = campaign.campaign_source;
        if (campaign.campaign_medium) params.campaign_medium = campaign.campaign_medium;
        if (campaign.campaign_name) params.campaign_name = campaign.campaign_name;
        gtag('event', 'page_view', params);
        if (route.profile) gtag('event', 'view_barber_profile', { page_location: params.page_location });
      }
      if (!routeObserverStarted) {
        routeObserverStarted = true;
        window.addEventListener('hashchange', sendPageView);
      }
    };
    script.onerror = () => { scriptLoading = false; };
    document.head.appendChild(script);
  }

  function showConsentDialog() {
    let dialog = document.getElementById('tunspro-consent');
    if (!dialog) {
      dialog = document.createElement('section');
      dialog.id = 'tunspro-consent';
      dialog.className = 'tunspro-consent';
      dialog.setAttribute('role', 'dialog');
      dialog.setAttribute('aria-labelledby', 'tunspro-consent-title');
      dialog.setAttribute('aria-describedby', 'tunspro-consent-copy');
      dialog.innerHTML = '<div class="tunspro-consent-copy"><span class="tunspro-consent-mark" aria-hidden="true">T</span><div><h2 id="tunspro-consent-title">Confidențialitatea ta contează</h2><p id="tunspro-consent-copy">Folosim Google Analytics doar cu acordul tău, pentru a înțelege vizitele și a îmbunătăți TunsPro. Nu activăm publicitate personalizată. Poți schimba alegerea oricând.</p><a href="#confidentialitate">Detalii în politica de confidențialitate</a></div></div><div class="tunspro-consent-actions"><button type="button" data-consent-choice="rejected" class="tunspro-consent-secondary">Refuză analiza</button><button type="button" data-consent-choice="accepted" class="tunspro-consent-primary">Acceptă analiza</button></div>';
      document.body.appendChild(dialog);
      dialog.addEventListener('click', (event) => {
        const choice = event.target.closest('[data-consent-choice]');
        if (choice) saveConsent(choice.dataset.consentChoice);
      });
    }
    dialog.hidden = false;
    document.getElementById('tunspro-cookie-settings')?.setAttribute('aria-expanded', 'true');
    document.querySelector('[data-consent-choice="accepted"]')?.focus({ preventScroll: true });
  }

  function hideConsentDialog() {
    const dialog = document.getElementById('tunspro-consent');
    if (dialog) dialog.hidden = true;
    document.getElementById('tunspro-cookie-settings')?.setAttribute('aria-expanded', 'false');
  }

  function saveConsent(value) {
    try { localStorage.setItem(CONSENT_KEY, value); } catch {}
    hideConsentDialog();
    if (value === 'accepted') {
      loadAnalytics();
      if (analyticsReady && window.gtag) gtag('consent', 'update', { analytics_storage: 'granted', ad_storage: 'denied', ad_user_data: 'denied', ad_personalization: 'denied' });
    } else {
      if (window.gtag) gtag('consent', 'update', { analytics_storage: 'denied', ad_storage: 'denied', ad_user_data: 'denied', ad_personalization: 'denied' });
      analyticsReady = false;
      deleteAnalyticsCookies();
    }
  }

  function addSettingsButton() {
    if (document.getElementById('tunspro-cookie-settings')) return;
    const button = document.createElement('button');
    button.id = 'tunspro-cookie-settings';
    button.className = 'tunspro-cookie-settings';
    button.type = 'button';
    button.textContent = 'Cookie-uri';
    button.setAttribute('aria-label', 'Deschide setările de confidențialitate');
    button.setAttribute('aria-expanded', 'false');
    button.addEventListener('click', showConsentDialog);
    document.body.appendChild(button);
  }

  function addPrivacyDisclosure() {
    const page = document.querySelector('.legal-page');
    if (!page || page.querySelector('#analytics-privacy-section')) return;
    const section = document.createElement('section');
    section.id = 'analytics-privacy-section';
    section.innerHTML = '<h2>Analiză de utilizare și consimțământ</h2><p>Google Analytics 4 este încărcat doar după ce alegi „Acceptă analiza”. Dacă refuzi, nu trimitem evenimente de analiză către Google. După acord, putem măsura vizualizările paginilor publice, vizualizarea profilurilor frizeriilor, rezervările create și înscrierile frizeriilor, fără nume, e-mail, telefon, detalii de plată sau conținutul formularelor. Adresele URL transmise sunt limitate la rutele publice și nu includ parametrii de interogare.</p><p>Google poate prelucra date tehnice de utilizare ca furnizor al serviciului Analytics. Semnalele de publicitate și personalizarea reclamelor rămân dezactivate. Alegerea ta este salvată în browserul acestui dispozitiv; o poți schimba folosind butonul „Cookie-uri” afișat pe site.</p><p><a href="https://policies.google.com/privacy" target="_blank" rel="noopener">Politica de confidențialitate Google</a>.</p>';
    page.appendChild(section);
  }

  function trackApiSuccess(url, method, response) {
    if (!analyticsReady || consentValue() !== 'accepted' || method !== 'POST' || !response.ok) return;
    const path = new URL(url, location.href).pathname;
    if (path === '/api/public/bookings') gtag('event', 'booking_created');
    else if (path === '/api/public/bookings/reschedule') gtag('event', 'booking_rescheduled');
    else if (path === '/api/auth/register') gtag('event', 'barber_signup');
  }

  function installApiObserver() {
    if (window.__tunsproAnalyticsFetchWrapped) return;
    window.__tunsproAnalyticsFetchWrapped = true;
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async function(input, init) {
      const method = String(init?.method || input?.method || 'GET').toUpperCase();
      const url = typeof input === 'string' || input instanceof URL ? String(input) : input.url;
      const response = await nativeFetch(input, init);
      try { trackApiSuccess(url, method, response); } catch {}
      return response;
    };
  }

  function observePrivacyPage() {
    const observer = new MutationObserver(addPrivacyDisclosure);
    observer.observe(document.getElementById('app') || document.body, { childList: true, subtree: true });
    addPrivacyDisclosure();
  }

  function init() {
    addSettingsButton();
    observePrivacyPage();
    installApiObserver();
    if (consentValue() === 'accepted') loadAnalytics();
    else if (consentValue() !== 'rejected') showConsentDialog();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})();
