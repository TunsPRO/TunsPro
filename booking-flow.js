(function(){
  const steps=[
    {key:'service',label:'Serviciu',target:'servicii'},
    {key:'barber',label:'Frizer',target:'echipa'},
    {key:'time',label:'Zi și oră',target:'disponibilitate'},
    {key:'details',label:'Datele tale',target:'booking-form'}
  ];
  let observerQueued=false,reviewForm=null,reviewSubmitter=null;
  const one=(root,selector)=>root.querySelector(selector);
  function bookingRoot(){return document.querySelector('.booking-profile-page')}
  function state(root){
    const service=one(root,'.booking-service.selected');
    const barber=one(root,'.booking-barber.selected');
    const date=one(root,'#booking-date')?.value||'';
    const time=one(root,'.booking-time-grid .slot.selected')?.textContent.trim()||'';
    return {service,barber,date,time,ready:!!(service&&barber&&date&&time)};
  }
  function currentIndex(s){return !s.service?0:!s.barber?1:!s.date||!s.time?2:3}
  function scrollToStep(root,index){
    const s=state(root),required=currentIndex(s),targetIndex=Math.min(index,required);
    const id=steps[targetIndex]?.target||'servicii';
    const target=id==='booking-form'?one(root,'#booking-form'):one(root,'#'+id);
    (target||one(root,'#servicii'))?.scrollIntoView({behavior:'smooth',block:'center'});
    if(id==='booking-form')setTimeout(()=>one(root,'[name="client"]')?.focus({preventScroll:true}),350);
  }
  function ensureProgress(root){
    const tabs=one(root,'.profile-tabs');
    if(!tabs||one(root,'[data-booking-progress]'))return;
    const nav=document.createElement('nav');
    nav.className='booking-flow-progress';
    nav.dataset.bookingProgress='';
    nav.setAttribute('aria-label','Pașii rezervării');
    nav.innerHTML=steps.map((step,index)=>'<button type="button" data-booking-step="'+index+'"><span class="booking-flow-index">'+(index+1)+'</span><span class="booking-flow-label">'+step.label+'</span></button>').join('');
    tabs.insertAdjacentElement('afterend',nav);
  }
  function dateLabel(s){
    if(!s.date)return '';
    const d=new Date(s.date+'T12:00:00');
    if(Number.isNaN(d.getTime()))return s.date;
    return new Intl.DateTimeFormat('ro-RO',{weekday:'short',day:'numeric',month:'short'}).format(d);
  }
  function update(root){
    if(!root)return;
    ensureProgress(root);
    const s=state(root),active=currentIndex(s),progress=one(root,'[data-booking-progress]');
    if(progress){
      progress.querySelectorAll('[data-booking-step]').forEach((button,index)=>{
        const complete=index<active;
        button.classList.toggle('is-complete',complete);
        button.classList.toggle('is-current',index===active);
        button.setAttribute('aria-current',index===active?'step':'false');
        button.setAttribute('aria-label',(index+1)+'. '+steps[index].label+(complete?' — completat':index===active?' — pasul următor':''));
      });
    }
    root.dataset.bookingStage=String(active+1);
    const dock=one(root,'.booking-continue-bar'),button=one(root,'[data-booking-continue]');
    if(!dock||!button)return;
    const serviceName=s.service?.querySelector('.booking-service-copy strong')?.textContent.trim()||'';
    const price=s.service?.querySelector('.booking-service-price strong')?.textContent.trim()||'';
    const barberName=s.barber?.querySelector('strong')?.textContent.trim()||'';
    const duration=s.service?.querySelector('.booking-service-copy small')?.textContent.trim().split('·')[0].trim()||'';
    const copy=one(dock,'.booking-continue-copy');
    if(copy){
      const eyebrow=one(copy,'span'),title=one(copy,'strong'),detail=one(copy,'small');
      if(eyebrow)eyebrow.textContent=s.ready?'ALEGEREA TA':'PASUL '+(active+1)+' DIN 4';
      if(title)title.textContent=serviceName||'Alege serviciul dorit';
      if(detail){
        if(s.ready)detail.textContent=[barberName,dateLabel(s),s.time,duration].filter(Boolean).join(' · ');
        else if(active===0)detail.textContent='Alege un serviciu pentru a începe.';
        else if(active===1)detail.textContent='Serviciul este ales · alege frizerul.';
        else if(!s.date)detail.textContent='Alege o zi pentru frizerul selectat.';
        else detail.textContent='Alege o oră liberă pentru '+dateLabel(s)+'.';
      }
    }
    const priceNode=one(dock,'.booking-continue-price');
    if(priceNode)priceNode.textContent=price;
    button.disabled=false;
    button.removeAttribute('disabled');
    button.setAttribute('aria-disabled','false');
    const labels=['Alege serviciul','Alege frizerul',s.date?'Alege ora':'Alege ziua','Introdu datele'];
    button.innerHTML=labels[active]+' <span aria-hidden="true">→</span>';
  }
  function scheduleUpdate(){
    if(observerQueued)return;
    observerQueued=true;
    requestAnimationFrame(()=>{observerQueued=false;update(bookingRoot())});
  }
  function ensureDialog(){
    let dialog=document.querySelector('#booking-review-dialog');
    if(dialog)return dialog;
    dialog=document.createElement('dialog');
    dialog.id='booking-review-dialog';
    dialog.className='booking-review-dialog';
    dialog.setAttribute('aria-labelledby','booking-review-title');
    dialog.innerHTML='<div class="booking-review-content"><span class="booking-review-kicker">ULTIMA VERIFICARE</span><h2 id="booking-review-title">Verifică rezervarea</h2><p class="booking-review-intro">Asigură-te că detaliile sunt corecte înainte să trimiți cererea.</p><dl class="booking-review-details"></dl><div class="booking-review-actions"><button type="button" class="btn ghost" data-review-back>Înapoi la date</button><button type="button" class="btn lime" data-review-confirm>Confirmă programarea</button></div></div>';
    document.body.append(dialog);
    return dialog;
  }
  function showReview(form){
    const root=bookingRoot(),s=state(root);
    if(!s.ready)return;
    const dialog=ensureDialog(),serviceName=s.service?.querySelector('.booking-service-copy strong')?.textContent.trim()||'Serviciu';
    const price=s.service?.querySelector('.booking-service-price strong')?.textContent.trim()||'';
    const barberName=s.barber?.querySelector('strong')?.textContent.trim()||'Frizer';
    const duration=s.service?.querySelector('.booking-service-copy small')?.textContent.trim().split('·')[0].trim()||'';
    const values=[
      ['Frizerie',one(root,'.salon-identity h1')?.textContent.trim()||''],
      ['Serviciu',serviceName],
      ['Frizer',barberName],
      ['Data și ora',[dateLabel(s),s.time].filter(Boolean).join(' · ')],
      ['Durată',duration],
      ['Preț',price],
      ['Nume',form.elements.client?.value.trim()||''],
      ['Telefon',form.elements.phone?.value.trim()||''],
      ['E-mail',form.elements.email?.value.trim()||'']
    ];
    const list=one(dialog,'.booking-review-details');
    list.replaceChildren(...values.map(([label,value])=>{
      const row=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');
      row.className='booking-review-row';dt.textContent=label;dd.textContent=value||'—';row.append(dt,dd);return row;
    }));
    reviewForm=form;reviewSubmitter=null;dialog.showModal();
  }
  document.addEventListener('click',event=>{
    const step=event.target.closest('[data-booking-step]');
    if(step){const root=bookingRoot();if(!root)return;event.preventDefault();event.stopImmediatePropagation();scrollToStep(root,Number(step.dataset.bookingStep));return}
    const dockButton=event.target.closest('[data-booking-continue]');
    if(dockButton){const root=bookingRoot();if(!root)return;event.preventDefault();event.stopImmediatePropagation();scrollToStep(root,currentIndex(state(root)))}
  },true);
  document.addEventListener('submit',event=>{
    const form=event.target;
    if(!(form instanceof HTMLFormElement)||form.id!=='booking-form')return;
    if(form.dataset.reviewConfirmed==='true'){delete form.dataset.reviewConfirmed;return}
    event.preventDefault();event.stopImmediatePropagation();
    showReview(form);
  },true);
  document.addEventListener('click',event=>{
    const back=event.target.closest('[data-review-back]');
    if(back){ensureDialog().close();if(reviewForm)one(reviewForm,'[name="client"]')?.focus({preventScroll:true});return}
    const confirm=event.target.closest('[data-review-confirm]');
    if(!confirm||!reviewForm)return;
    if(!reviewForm.reportValidity())return;
    const form=reviewForm,submitter=reviewSubmitter;
    form.dataset.reviewConfirmed='true';
    ensureDialog().close();
    reviewForm=null;reviewSubmitter=null;
    if(submitter&&submitter.form===form)form.requestSubmit(submitter);else form.requestSubmit();
  });
  document.addEventListener('click',event=>{
    const submit=event.target.closest('#booking-form button[type="submit"],#booking-form button:not([type])');
    if(submit)reviewSubmitter=submit;
  },true);
  const app=document.querySelector('#app');
  if(app){
    new MutationObserver(scheduleUpdate).observe(app,{childList:true,subtree:true,attributes:true,attributeFilter:['class','value','aria-pressed']});
    scheduleUpdate();
  }
})();