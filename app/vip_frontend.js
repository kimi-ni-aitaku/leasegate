import {BrowserQRCodeReader} from '@zxing/browser';

const $ = id => document.getElementById(id);
const messages = {
  vip_card_invalid:'卡密无效，请检查后重试。', vip_card_expired:'卡密已到期。',
  vip_card_disabled:'卡密已停用，请联系管理员。', vip_card_session_required:'请先验证卡密。',
  vip_scan_limit:'扫码次数已用尽。', vip_login_pending:'已有登录请求正在处理，请稍候。',
  vip_device_limit:'这张卡密已绑定其他电脑。', vip_qr_already_assigned:'二维码已被使用，请刷新电脑上的二维码。',
  vip_qr_expired:'二维码已过期，请在电脑上刷新后重新扫描。', vip_inventory_empty:'账号库存暂不可用，请稍后重试或联系管理员。',
  vip_service_unavailable:'服务暂未开放，请联系管理员。', vip_gateway_unavailable:'授权服务暂不可用，请稍后重试。',
  vip_duplicate_account:'批次内或账号池中存在重复账号，请检查后重试。', vip_authorization_rejected:'本次授权未通过，请刷新二维码重试。',
  vip_verification_not_pending:'当前无需提交验证信息，请等待状态更新。', target_device_unverifiable:'无法确认这台电脑的授权信息，请联系管理员。',
  invalid_quark_reference_qr:'请上传或扫描夸克电脑网页登录二维码。', qr_not_found:'图片中没有可读取的二维码。',
  rate_limited:'操作过于频繁，请稍后再试。', invalid_request:'请检查填写内容。', invalid_admin_token:'管理员密钥不正确。',
  image_too_large:'图片不能超过 5 MB。', invalid_image:'图片无法读取，请重新截图。',
  unsupported_image_type:'请上传 PNG、JPG 或 WEBP 图片。', image_dimensions_too_large:'图片尺寸过大，请裁剪二维码后重试。',
};
let csrf, config, card, currentTask, serverNow=0, syncedAt=0, controls, media, polling, busy=false;
function status(text, kind=''){ $('status').textContent=text; $('status').className='status '+kind; }
function error(e){ status(messages[e.message] || '操作未完成，请稍后重试。','error'); }
async function api(path, body, retried=false){
  const form=body instanceof FormData;
  const headers=body===undefined?{}:{'X-CSRF-Token':csrf,...(form?{}:{'Content-Type':'application/json'})};
  const response=await fetch(path,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers,
    body:body===undefined?undefined:form?body:JSON.stringify(body)});
  const data=await response.json();
  if(response.status===403&&body!==undefined&&!retried&&['csrf_required','invalid_csrf'].includes(data.detail)){
    csrf=(await api('/api/csrf')).token;return api(path,body,true);
  }
  if(!response.ok)throw new Error(data.detail||'request_failed');
  return data;
}
function action(id,fn){$(id).addEventListener('click',async()=>{const b=$(id);b.disabled=true;try{await fn();}catch(e){error(e);}finally{b.disabled=false;if(document.body.dataset.page==='buyer')updateButtons();}});}
function form(id,fn){$(id).addEventListener('submit',async e=>{e.preventDefault();const b=e.target.querySelector('[type=submit]');b.disabled=true;try{await fn();}catch(err){error(err);}finally{b.disabled=false;}});}
const date=n=>n?new Date(n*1000).toLocaleString('zh-CN',{hour12:false}):'首次授权后开始计时';
function updateButtons(){const usable=card&&['issued','active'].includes(card.status)&&card.remaining_scans>0&&config.available&&!busy;
  $('camera-open').disabled=!usable;$('upload-open').disabled=!usable;}
function renderCard(value,now){card=value;serverNow=now;syncedAt=performance.now();$('open-form').hidden=true;$('card-info').hidden=false;
  $('card-label').textContent=card.label;$('card-status').textContent={issued:'待首次授权',active:'使用中',expired:'已到期',disabled:'已停用'}[card.status]||card.status;
  $('card-status').className='badge '+(card.status==='active'?'active':['expired','disabled'].includes(card.status)?'error':'');
  $('scan-count').textContent=card.remaining_scans;$('expires').textContent=date(card.expire_at);updateClock();updateButtons();}
function updateClock(){if(!card)return;if(!card.expire_at){$('time-left').textContent='暂未计时';return;}
  const left=Math.max(0,Math.ceil(card.expire_at-serverNow-(performance.now()-syncedAt)/1000));
  $('time-left').textContent=[Math.floor(left/3600),Math.floor(left%3600/60),left%60].map(n=>String(n).padStart(2,'0')).join(':');
  if(!left&&card.status==='active'){card.status='expired';$('card-status').textContent='已到期';$('card-status').className='badge error';updateButtons();}}
function renderTask(task){if(!task)return;currentTask=task;busy=['QUEUED','PENDING','UNCERTAIN','WAIT_VERIFY','VERIFY_PENDING'].includes(task.status);
  const text={QUEUED:'二维码已提交，正在安排授权。',PENDING:'正在确认授权，请保持电脑登录窗口打开。',UNCERTAIN:'正在核对授权结果，请勿重复提交。',
    WAIT_VERIFY:'本次登录需要补充验证。',VERIFY_PENDING:'验证信息已提交，正在核对。',AUTHORIZED:task.buyer_login_verified?'电脑登录已验证，授权完成。':'授权已确认，请回到电脑查看登录结果。',
    FAILED:messages[task.error_code]||'本次授权未完成，请重试。',EXPIRED:'二维码已过期，请刷新后重新扫描。'};
  status(text[task.status]||'正在更新授权状态。',task.status==='AUTHORIZED'?'success':['FAILED','EXPIRED'].includes(task.status)?'error':'');
  $('verify-form').hidden=task.status!=='WAIT_VERIFY'||task.challenge_kind!=='sms';
  $('owner-note').hidden=task.status!=='WAIT_VERIFY'||task.challenge_kind==='sms';
  $('owner-note').textContent=task.challenge_kind==='captcha'?'请在官方授权设备上完成人机验证，页面将自动更新。':'请在会员授权设备上完成确认，页面将自动更新。';
  $('verify-label').textContent='本次授权要求的短信验证码';updateButtons();}
async function refreshBuyer(){const data=await api('/api/vip/state');renderCard(data.card,data.server_time);
  const task=data.tasks.find(t=>t.id===currentTask?.id)||data.tasks.find(t=>['QUEUED','PENDING','UNCERTAIN','WAIT_VERIFY','VERIFY_PENDING'].includes(t.status))||data.tasks[0];
  renderTask(task);if(!task)status(config.available?'卡密已验证，可以提交电脑上的二维码。':'服务暂未开放，请联系管理员。');}
function stopCamera(){controls?.stop();controls=null;media?.getTracks().forEach(t=>t.stop());media=null;if($('camera-box')){$('camera-video').srcObject=null;$('camera-box').hidden=true;}}
async function submitRaw(raw){if(busy)return;stopCamera();busy=true;updateButtons();status('正在提交二维码…');try{renderTask(await api('/api/vip/submit',{raw}));await refreshBuyer();}catch(e){busy=false;updateButtons();throw e;}}
async function decodeImage(file){if(!card||busy){status(busy?'正在处理上一笔登录请求。':'请先验证卡密。');return;}stopCamera();
  if(!['image/png','image/jpeg','image/webp'].includes(file.type))throw new Error('unsupported_image_type');
  if(file.size>config.image_max_bytes)throw new Error('image_too_large');
  status('正在读取二维码截图…');const objectURL=URL.createObjectURL(file);let raw;
  try{const image=new Image();image.src=objectURL;await image.decode();if(image.naturalWidth*image.naturalHeight>16000000)throw new Error('image_dimensions_too_large');
    try{raw=(await new BrowserQRCodeReader().decodeFromImageElement(image)).getText();}
    catch{const data=new FormData();data.append('image',file);raw=(await api('/api/qr/decode-image',data)).raw;}}
  finally{URL.revokeObjectURL(objectURL);}
  await submitRaw(raw);
}
async function buyer(){
  config=await api('/api/vip/config');$('notice').textContent=config.notice;
  form('open-form',async()=>{const code=$('card-code').value.trim();$('card-code').value='';await api('/api/vip/open',{code});currentTask=null;await refreshBuyer();});
  action('switch-card',async()=>{stopCamera();await api('/api/vip/logout',{});card=null;currentTask=null;busy=false;$('card-info').hidden=true;$('open-form').hidden=false;
    $('card-label').textContent='打开你的卡密';$('card-status').textContent='待验证';$('card-status').className='badge';$('verify-form').hidden=true;$('owner-note').hidden=true;status('请输入另一张卡密。');});
  action('camera-open',async()=>{if(!navigator.mediaDevices?.getUserMedia)throw new Error('camera_unavailable');
    stopCamera();media=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'}},audio:false});$('camera-box').hidden=false;
    let decoded=false;try{controls=await new BrowserQRCodeReader().decodeFromStream(media,$('camera-video'),(result,err,scanner)=>{
      if(result&&!decoded){decoded=true;scanner.stop();stopCamera();submitRaw(result.getText()).catch(error);}});if(decoded)stopCamera();}catch(e){stopCamera();throw e;}});
  action('camera-close',async()=>stopCamera());action('upload-open',async()=>{$('image-file').click();});
  $('drop-zone').addEventListener('click',()=>{if(!$('upload-open').disabled)$('image-file').click();});
  $('drop-zone').addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();if(!$('upload-open').disabled)$('image-file').click();}});
  $('image-file').addEventListener('change',e=>{const file=e.target.files[0];e.target.value='';if(file)decodeImage(file).catch(error);});
  $('drop-zone').addEventListener('dragover',e=>{e.preventDefault();$('drop-zone').classList.add('dragover');});
  $('drop-zone').addEventListener('dragleave',()=>$('drop-zone').classList.remove('dragover'));
  $('drop-zone').addEventListener('drop',e=>{e.preventDefault();$('drop-zone').classList.remove('dragover');if(e.dataTransfer.files[0])decodeImage(e.dataTransfer.files[0]).catch(error);});
  document.addEventListener('paste',e=>{if(['INPUT','TEXTAREA'].includes(e.target.tagName))return;const item=[...(e.clipboardData?.items||[])].find(i=>i.type.startsWith('image/'));
    if(item){e.preventDefault();decodeImage(item.getAsFile()).catch(error);}});
  form('verify-form',async()=>{const answer=$('verify-answer').value;$('verify-answer').value='';renderTask(await api(`/api/vip/tasks/${currentTask.id}/answer`,{answer}));});
  async function openLink(){const code=new URLSearchParams(location.hash.slice(1)).get('card');history.replaceState(null,'',location.pathname);
    if(!code)return false;stopCamera();currentTask=null;busy=false;await api('/api/vip/open',{code});await refreshBuyer();return true;}
  window.addEventListener('hashchange',()=>openLink().catch(error));
  try{if(!await openLink())await refreshBuyer();}catch(e){if(e.message!=='vip_card_session_required')error(e);}
  let refreshing=false;polling=setInterval(async()=>{if(!card||refreshing||document.hidden)return;refreshing=true;try{await refreshBuyer();}catch(e){error(e);}finally{refreshing=false;}},5000);
  setInterval(updateClock,1000);
}

let collection='accounts',offset=0,issued=[];
const labels={id:'记录 ID',alias:'账号名称',credential_type:'凭据形式',capacity:'设备容量',status:'状态',checked_at:'检查时间',created_at:'创建时间',occupied:'占用',label:'商品',
  activated_at:'激活时间',expire_at:'到期时间',valid_until:'未激活有效期',max_devices:'绑定上限',remaining_scans:'剩余次数',remaining_seconds:'剩余秒数',error_code:'结果',challenge_kind:'验证类型',
  buyer_login_verified:'电脑登录已验证',card_id:'卡密 ID',binding_id:'绑定 ID',account_id:'账号 ID',revoke_supported:'支持设备注销'};
const states={unverified:'待检查',ready:'可用',unavailable:'不可用',disabled:'停用',issued:'未激活',active:'使用中',expired:'到期',reserved:'预留',released:'已释放',
  revoke_pending:'待注销',remote_unconfirmed:'外部注销未确认',revoked:'已注销',QUEUED:'排队',PENDING:'处理中',WAIT_VERIFY:'待验证',VERIFY_PENDING:'验证处理中',
  UNCERTAIN:'核对中',AUTHORIZED:'授权确认',FAILED:'失败',EXPIRED:'二维码到期'};
async function loadAdmin(){const data=await api(`/api/admin/vip/${collection}?offset=${offset}`);$('admin-login').hidden=true;$('admin-workspace').hidden=false;
  if(collection==='accounts'){$('gateway-state').textContent=data.gateway_configured?'授权服务已配置。账号检查通过后可进入可用库存。':'授权服务尚未接入。可管理账号和卡密，买家扫码授权暂未开放。';}
  $('table-head').replaceChildren();$('table-body').replaceChildren();
  const cols=data.items.length?Object.keys(data.items[0]):[];const header=document.createElement('tr');
  for(const key of cols){const th=document.createElement('th');th.textContent=labels[key]||key;header.append(th);}
  if(['accounts','cards'].includes(collection)){const th=document.createElement('th');th.textContent='操作';header.append(th);}$('table-head').append(header);
  for(const item of data.items){const row=document.createElement('tr');for(const key of cols){const cell=document.createElement('td');let value=item[key];
    if(key.endsWith('_at')||key==='valid_until')value=value?date(value):'—';else if(key==='status')value=states[value]||value;
    else if(key==='error_code')value=messages[value]||value||'—';else if(typeof value==='boolean')value=value?'是':'否';
    cell.textContent=value??'—';if(key.endsWith('_id')||key==='id'){cell.title=String(value);cell.textContent=String(value||'—').slice(0,8);}row.append(cell);}
    if(['accounts','cards'].includes(collection)){const cell=document.createElement('td');
      if(collection==='accounts'){const check=document.createElement('button');check.textContent='检查';check.className='secondary';check.addEventListener('click',async()=>{check.disabled=true;try{await api(`/api/admin/vip/accounts/${item.id}/check`,{});await loadAdmin();}catch(e){error(e);}finally{check.disabled=false;}});cell.append(check);}
      const disable=document.createElement('button');disable.textContent='停用';disable.className='secondary';disable.disabled=item.status==='disabled';disable.addEventListener('click',async()=>{disable.disabled=true;try{await api(`/api/admin/vip/${collection}/${item.id}/disable`,{});await loadAdmin();}catch(e){error(e);disable.disabled=false;}});cell.append(disable);row.append(cell);}
    $('table-body').append(row);}
  if(!data.items.length){const row=document.createElement('tr'),cell=document.createElement('td');cell.textContent='暂无记录';cell.colSpan=20;row.append(cell);$('table-body').append(row);}
  $('previous').disabled=offset===0;$('next').disabled=data.items.length<100;$('page-label').textContent=`第 ${offset/100+1} 页`;
  document.querySelectorAll('[data-collection]').forEach(b=>b.classList.toggle('selected',b.dataset.collection===collection));
}
async function admin(){
  form('admin-login',async()=>{const token=$('admin-secret').value;$('admin-secret').value='';await api('/api/admin/login',{token});await loadAdmin();status('管理后台已登录。','success');});
  action('admin-logout',async()=>{await api('/api/admin/logout',{});$('admin-workspace').hidden=true;$('admin-login').hidden=false;issued=[];$('issued-cards').value='';$('issued-box').hidden=true;status('已退出管理后台。');});
  form('import-form',async()=>{const lines=$('account-lines').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);$('account-lines').value='';
    const prefix='账号-'+crypto.randomUUID().slice(0,8),type=$('credential-type').value,capacity=Number($('account-capacity').value);
    const data=await api('/api/admin/vip/accounts/import',{accounts:lines.map((credential,i)=>({alias:`${prefix}-${i+1}`,credential_type:type,credential,capacity}))});
    collection='accounts';offset=0;await loadAdmin();status(`已加密导入 ${data.items.length} 个账号。请检查账号可用性。`,'success');});
  form('cards-form',async()=>{const data=await api('/api/admin/vip/cards',{count:Number($('card-number').value),label:$('card-name').value,
    duration_seconds:Math.round(Number($('card-hours').value)*3600),valid_seconds:Number($('card-valid').value)*86400,max_scans:Number($('card-scans').value),max_devices:Number($('card-devices').value)});
    issued=data.items;$('issued-cards').value=issued.map(c=>`${c.code}\n${c.activation_url}`).join('\n\n');$('issued-box').hidden=false;collection='cards';offset=0;await loadAdmin();status(`已生成 ${issued.length} 张卡密，请保存本批次链接。`,'success');});
  action('export-cards',async()=>{const blob=new Blob([$('issued-cards').value],{type:'text/plain;charset=utf-8'});const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='leasegate-cards.txt';a.click();URL.revokeObjectURL(url);});
  document.querySelectorAll('[data-collection]').forEach(b=>b.addEventListener('click',()=>{collection=b.dataset.collection;offset=0;loadAdmin().catch(error);}));
  action('refresh',loadAdmin);action('previous',async()=>{offset=Math.max(0,offset-100);await loadAdmin();});action('next',async()=>{offset+=100;await loadAdmin();});
  try{await loadAdmin();}catch(e){if(e.message!=='admin_authentication_required')error(e);}
}
window.addEventListener('pagehide',()=>{stopCamera();clearInterval(polling);});
(async()=>{csrf=(await api('/api/csrf')).token;if(document.body.dataset.page==='admin')await admin();else await buyer();})().catch(error);
