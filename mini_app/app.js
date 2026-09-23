const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();
const initData = tg?.initData || "";
const base = new URL(".", window.location.href).pathname;
const state = {tab:"cases", q:"", status:"", city:"", offset:0, more:false, loading:false, controller:null};
const $ = id => document.getElementById(id);
const labels = {draft:"Черновик",needs_clarification:"Нужно уточнить",ready:"Готова к поиску",researching:"Поиск",contacting:"Поставщики",evaluating:"Оценка",report_ready:"Отчёт готов",selected:"Выбрана",closed:"Закрыта",cancelled:"Отменена",candidate:"Кандидат",active:"Активный",inactive:"Неактивный",blocked:"Заблокирован",merged:"Объединён",lead:"Лид",confirm:"Подтвердить",exact:"Совпадает",mismatch:"Не подходит",not_found:"Не найден",withdrawn:"Снят"};
const badgeType = status => ["needs_clarification","confirm"].includes(status)?"warn":["cancelled","blocked","mismatch"].includes(status)?"danger":["closed","draft","inactive","merged"].includes(status)?"mute":"";
function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node}
function fmtDate(value){if(!value)return "Не указана";return new Intl.DateTimeFormat("ru-KZ",{day:"numeric",month:"short",year:"numeric"}).format(new Date(value))}
function fmtNumber(value){return new Intl.NumberFormat("ru-KZ",{maximumFractionDigits:2}).format(Number(value))}
function badge(status){return el("span","badge "+badgeType(status),labels[status]||status)}
async function api(path,signal){const response=await fetch(base+path.replace(/^\//,""),{headers:{Authorization:"tma "+initData},signal});if(!response.ok)throw new Error(response.status===401||response.status===403?"Откройте приложение через Telegram":"Не удалось загрузить данные");return response.json()}
function notice(message){const t=$("toast");t.textContent=message;t.hidden=false;setTimeout(()=>t.hidden=true,3500)}
function empty(title,description){const box=el("div","empty");box.append(el("div","empty-symbol","▤"),el("h3","",title),el("p","",description));return box}
function caseCard(row){
 const card=el("button","case-card");card.type="button";
 const top=el("div","card-top"),name=el("span","card-title",row.title||"Заявка без названия");
 top.append(name,el("span","card-chevron","›"));card.append(top);
 const meta=el("div","card-meta");meta.append(el("span","",row.city||"Город не указан"),el("span","",row.item_count+" позиций"));
 if(row.customer_name)meta.append(el("span","",row.customer_name));card.append(meta);
 const foot=el("div","card-foot");foot.append(badge(row.status),el("span","",fmtDate(row.updated_at)));card.append(foot);
 card.addEventListener("click",()=>openDetail(row.id));return card;
}
function supplierCard(row){
 const card=el("div","supplier-card"),top=el("div","card-top");
 top.append(el("span","card-title",row.display_name),badge(row.status));card.append(top);
 const meta=el("div","card-meta");meta.append(el("span","",row.offer_count+" предложений"),el("span","",row.last_offer_at?"Обновлено "+fmtDate(row.last_offer_at):""));card.append(meta);return card;
}
function setTab(tab){
 state.tab=tab;state.offset=0;state.q="";state.status="";state.city="";
 $("search").value="";$("status-filter").value="";$("city-filter").value="";
 document.querySelectorAll(".nav-item").forEach(node=>node.classList.toggle("active",node.dataset.tab===tab));
 $("page-title").textContent=tab==="cases"?"Мои заявки":"Поставщики";
 $("page-subtitle").textContent=tab==="cases"?"Все закупки и предложения в одном месте":"Контакты и предложения по вашим заявкам";
 $("list-title").textContent=tab==="cases"?"Заявки":"Поставщики";
 $("search").placeholder=tab==="cases"?"Найти заявку или город":"Найти поставщика";
 $("status-filter").hidden=tab!=="cases";$("city-filter").hidden=tab!=="cases";
 load();
}
async function load(append=false){
 state.controller?.abort();state.controller=new AbortController();
 const signal=state.controller.signal;state.loading=true;
 if(!append){state.offset=0;$("list").replaceChildren(empty("Загружаем данные","Подождите немного"))}
 const params=new URLSearchParams({limit:"20",offset:String(state.offset)});
 if(state.q)params.set("q",state.q);
 if(state.tab==="cases"){if(state.status)params.set("status",state.status);if(state.city)params.set("city",state.city)}
 try{
  const data=await api("/api/"+state.tab+"?"+params,signal);
  if(signal.aborted)return;
  if(!append)$("list").replaceChildren();
  data.rows.forEach(row=>$("list").append(state.tab==="cases"?caseCard(row):supplierCard(row)));
  if(!data.rows.length&&!append)$("list").append(empty(
   state.q||state.status||state.city?"Ничего не найдено":state.tab==="cases"?"Заявок пока нет":"Поставщиков пока нет",
   state.q||state.status||state.city?"Попробуйте изменить поиск или фильтры":state.tab==="cases"?"Заявки появятся здесь после создания в боте":"Поставщики появятся после поиска по заявкам"
  ));
  if(state.tab==="cases"&&data.cities){
   const selected=state.city;$("city-filter").replaceChildren(new Option("Все города",""));
   data.cities.forEach(city=>$("city-filter").add(new Option(city,city)));$("city-filter").value=selected;
  }
  state.more=data.has_more;state.offset+=data.rows.length;$("load-more").hidden=!state.more;
  $("result-count").textContent=state.offset?state.offset+" показано":"";
 }catch(error){if(!signal.aborted){$("list").replaceChildren(empty(error.message,"Проверьте подключение и откройте Mini App в боте"));$("load-more").hidden=true}}
 finally{if(!signal.aborted)state.loading=false}
}
async function openDetail(id){
 const dialog=$("detail"),root=$("detail-content");
 root.replaceChildren(empty("Загружаем заявку",""));dialog.showModal();
 try{
  const data=await api("/api/cases/"+encodeURIComponent(id));const c=data.case;
  const body=el("div","detail-body");body.append(badge(c.status),el("h2","",c.title||"Заявка без названия"),el("p","detail-sub",[c.city,c.customer_name].filter(Boolean).join(" · ")||"Основная информация"));
  const grid=el("div","info-grid");
  [["Город",c.city||"Не указан"],["Срок",fmtDate(c.deadline_at)],["Позиций",String(data.items.length)],["Обновлено",fmtDate(c.updated_at)]].forEach(([key,value])=>{const cell=el("div","info-cell");cell.append(el("small","",key),el("strong","",value));grid.append(cell)});
  body.append(grid,el("h3","","Позиции"));
  if(!data.items.length)body.append(empty("Позиций пока нет","Бот добавит их после обработки заявки"));
  data.items.forEach(item=>{
   const box=el("div","item-card"),head=el("div","item-head");
   head.append(el("span","",item.line_number+". "+item.name),badge(item.status));box.append(head);
   if(item.quantity)box.append(el("p","item-spec",fmtNumber(item.quantity)+" "+(item.unit||"")));
   if(item.specification_text)box.append(el("p","item-spec",item.specification_text));
   const offers=data.offers.filter(offer=>offer.request_item_id===item.id);
   offers.forEach(offer=>{const line=el("div","offer-line");line.append(el("strong","",offer.supplier_name),el("span","",[labels[offer.status]||offer.status,offer.price_amount!==null?fmtNumber(offer.price_amount)+" "+(offer.currency||""):"Цена не указана"].join(" · ")));box.append(line)});
   body.append(box);
  });
  root.replaceChildren(body);
 }catch(error){root.replaceChildren(empty(error.message,"Повторите попытку позже"))}
}
document.querySelectorAll(".nav-item").forEach(button=>button.addEventListener("click",()=>setTab(button.dataset.tab)));
let debounce; $("search").addEventListener("input",event=>{clearTimeout(debounce);debounce=setTimeout(()=>{state.q=event.target.value.trim();load()},250)});
$("status-filter").addEventListener("change",event=>{state.status=event.target.value;load()});
$("city-filter").addEventListener("change",event=>{state.city=event.target.value;load()});
$("load-more").addEventListener("click",()=>{if(!state.loading)load(true)});
$("close-detail").addEventListener("click",()=>$("detail").close());
async function start(){
 if(!initData){$("list").replaceChildren(empty("Откройте через Telegram","Для защиты данных приложение работает только внутри вашего бота"));return}
 try{const [user,summary]=await Promise.all([api("/api/me"),api("/api/summary")]);$("account-name").textContent=user.display_name||"Мой кабинет";$("active-count").textContent=summary.active_cases;$("items-count").textContent=summary.items;$("attention-count").textContent=summary.needs_attention;load()}
 catch(error){$("list").replaceChildren(empty(error.message,"Повторите вход через бота"))}
}
start();
