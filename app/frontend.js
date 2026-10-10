import {BrowserQRCodeReader} from '@zxing/browser';
import QRCode from 'qrcode';

const $ = id => document.getElementById(id);
const hide = id => $(id).classList.add('hidden');
const show = id => $(id).classList.remove('hidden');
let csrf, config, controls, media, eventSource, currentProof, countdownTimer, accessToken, lease,
    clockAtSync = 0, syncPerformance = 0, demoCode, adminOffset = 0, adminNext, quarkRequest, quarkPoll;
const reader = new BrowserQRCodeReader();

async function api(path, body, options={}) {
  const headers = {...options.headers};
  if (body !== undefined) {
    headers['X-CSRF-Token'] = csrf;
    if (!(body instanceof FormData)) headers['Content-Type'] = 'application/json';
  }
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', credentials:'same-origin',
    headers, body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body)});
  const data = await response.json();
  if(response.status===403&&body!==undefined&&!options.csrfRetried&&['csrf_required','invalid_csrf'].includes(data.detail)){
    csrf=(await api('/api/csrf')).token;return api(path,body,{...options,csrfRetried:true});
  }
  if (!response.ok) throw new Error(data.detail || '请求失败');
  return data;
}
const messages = {invalid_cdk:'卡密无效，请检查后重试', cdk_already_used:'卡密已兑换，请返回原浏览器继续授权',
  qr_expired:'二维码已过期，请重新生成', cdk_expired:'卡密已过期', resource_capacity_unavailable:'当前资源已满，请稍后重试',
  lease_expired:'服务已到期', invalid_refresh_session:'刷新会话已失效', activation_session_required:'请先输入卡密验证订单',
  csrf_required:'安全会话已失效，请刷新页面', rate_limited:'操作过于频繁，请稍后再试', qr_not_found:'图片中未找到可读取的二维码',
  quark_native_app_signature_required:'缺少会员账号的夸克 App 签名，当前 Cookie 未能完成扫码确认',
  native_app_signature_expired:'签名时间码已超出本系统联调允许的范围，请使用最新的实际请求参数',
  quark_qr_already_assigned:'这张二维码已绑定其他订单，请在自己的夸克页面刷新二维码',
  quark_qr_unavailable:'二维码已失效，请在夸克页面刷新后重新上传',
  quark_request_already_pending:'会员端正在确认，请先取消当前请求再更换授权方式',
  quark_phone_confirmation_required:'请先在会员 App 完成扫码确认',
  quark_member_confirmation_required:'请等待会员端确认'};
function status(text, state='') { const target = location.pathname.startsWith('/admin/') ? $('admin-status') : $('status'); target.textContent=text; target.className='status '+state; }
function error(e) {status(messages[e.message] || e.message, 'error');}
function action(id, callback) {$(id).addEventListener('click', async () => {$(id).disabled=true; try {await callback();} catch(e){error(e);} finally {$(id).disabled=false;}});}
function tab(name) {stopCamera(); document.querySelectorAll('[data-tab]').forEach(b=>{const selected=b.dataset.tab===name;b.classList.toggle('selected',selected);b.setAttribute('aria-selected',selected);}); document.querySelectorAll('.tab-content').forEach(t=>t.classList.add('hidden'));show(name+'-tab');}
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>tab(b.dataset.tab)));
function stopCamera(){controls?.stop();controls=null;media?.getTracks().forEach(t=>t.stop());media=null;$('video').srcObject=null;show('camera-placeholder');hide('camera-stop');show('camera-open');}
async function startCamera(){
  if(!navigator.mediaDevices?.getUserMedia)throw new Error('此环境无法使用摄像头，请使用 HTTPS、localhost 或上传图片');
  media=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'}},audio:false});hide('camera-placeholder');hide('camera-open');show('camera-stop');
  let decoded=false;
  try{controls=await reader.decodeFromStream(media,$('video'),(result,err,scanner)=>{if(result&&!decoded){decoded=true;scanner.stop();stopCamera();navigator.vibrate?.(100);handleRaw(result.getText()).catch(error);}});if(decoded)stopCamera();}
  catch(e){stopCamera();throw e;}
}
action('camera-open',startCamera);action('camera-stop',stopCamera);
window.addEventListener('pagehide',()=>{stopCamera();eventSource?.close();clearInterval(countdownTimer);clearInterval(quarkPoll);});

async function imageDecode(file){
  stopCamera();
  if(!['image/png','image/jpeg','image/webp'].includes(file.type))throw new Error('请选择 PNG、JPG 或 WEBP 图片');
  if(file.size>config.image_max_bytes)throw new Error('图片不能超过 5 MB');
  status('正在浏览器中读取二维码…');
  const url=URL.createObjectURL(file);let raw;
  try{const img=new Image();img.src=url;await img.decode();if(img.naturalWidth*img.naturalHeight>16000000)throw new Error('图片像素过大');raw=(await reader.decodeFromImageElement(img)).getText();}
  catch(e){status('本地读取失败，正在使用服务器解析…');const form=new FormData();form.append('image',file);const parsed=await api('/api/qr/decode-image',form);raw=parsed.raw;}
  finally{URL.revokeObjectURL(url);}
  await handleRaw(raw);
}
$('image-file').addEventListener('change',e=>{if(e.target.files[0])imageDecode(e.target.files[0]).catch(error);e.target.value='';});
document.querySelectorAll('.dropzone').forEach(zone=>{zone.addEventListener('dragover',e=>{e.preventDefault();zone.classList.add('drag');});zone.addEventListener('dragleave',()=>zone.classList.remove('drag'));zone.addEventListener('drop',e=>{e.preventDefault();zone.classList.remove('drag');if(e.dataTransfer.files[0])imageDecode(e.dataTransfer.files[0]).catch(error);});});
document.addEventListener('paste',e=>{if(e.target===$('code-input')||e.target===$('admin-token'))return;const item=[...(e.clipboardData?.items||[])].find(i=>i.type.startsWith('image/'));if(item){e.preventDefault();imageDecode(item.getAsFile()).catch(error);}});
action('clipboard-read',async()=>{if(!navigator.clipboard?.read)throw new Error('浏览器不支持直接读取，请使用快捷键粘贴');const items=await navigator.clipboard.read();for(const item of items){const type=item.types.find(t=>t.startsWith('image/'));if(type){await imageDecode(new File([await item.getType(type)],'clipboard.png',{type}));return;}}throw new Error('剪贴板中没有图片');});

async function handleRaw(raw){
  status('✓ 已读取二维码 · 正在解析…','success');
  const parsed=await api('/api/qr/parse',{raw});
  if(parsed.provider==='local'&&parsed.metadata.kind==='cdk'){ $('code-input').value=parsed.token;tab('code');await redeem(parsed.token); }
  else if(parsed.type==='local_authorization'){
    const info=await api(`/api/qr/${parsed.metadata.qr_id}/scanned`,{raw});
    currentProof={id:parsed.metadata.qr_id,raw};$('confirm-summary').textContent=`订单 ${info.order_no} · 授权时长 ${info.duration_seconds===60?'60 秒':'24 小时'}。确认后立即开始计时。`;
    show('confirmation');status('订单已验证，请确认激活。','success');
  }else if(parsed.provider==='oauth')throw new Error('请从本订单发起官方 Device 授权，不接受外部设备代码');
  else if(parsed.provider==='quark_reference'){
    quarkRequest=await api('/api/activation/quark/submit',{raw});eventSource?.close();stopCamera();renderQuark(quarkRequest);pollQuark();
  }
  else throw new Error('该能力需要官方 API 或获得授权的 Provider。此二维码不会被自动确认。');
}
async function redeem(code){
  status('正在验证订单…');const data=await api('/api/activation/redeem',{code});$('code-input').value='';
  if(data.product_id==='service-24h'){prepareQuarkUpload();return;}
  if(config.oauth_enabled)show('oauth-actions');
  await generateQR();
}
action('redeem',()=>redeem($('code-input').value.trim()));
async function generateQR(){
  status('正在创建授权…');const qr=await api('/api/qr/create',{});
  $('qr-image').src=await QRCode.toDataURL(qr.url,{width:320,margin:2,errorCorrectionLevel:'M'});$('scan-link').href=qr.url;
  show('scan-link');show('qr-renew');$('qr-description').textContent='用另一台设备扫码，并确认激活订单';
  hide('empty-qr');show('qr-box');$('state-pill').textContent='等待扫码';status('授权二维码已创建，扫码后请确认订单。');
  eventSource?.close();eventSource=new EventSource(`/api/qr/${qr.id}/events`);
  eventSource.addEventListener('scanned',()=>{status('二维码已被扫描，等待确认。');$('state-pill').textContent='等待确认';});
  eventSource.addEventListener('confirmed',()=>{eventSource.close();loadResult().catch(error);});
  for(const state of ['expired','cancelled'])eventSource.addEventListener(state,()=>{eventSource.close();status(state==='expired'?'二维码已过期，请重新生成。':'此二维码已取消。','error');$('state-pill').textContent='二维码失效';});
  eventSource.onerror=()=>status('连接已中断；浏览器会自动重连，也可重新生成二维码。');
}
action('qr-renew',generateQR);
action('confirm-button',async()=>{if(!currentProof)return;status('正在确认并创建授权…');await api(`/api/qr/${currentProof.id}/confirm`,{raw:currentProof.raw});hide('confirmation');status('授权成功。请返回原激活页面查看服务。','success');try{await loadResult();}catch(e){if(e.message!=='activation_session_required')throw e;}});

function prepareQuarkUpload(){
  quarkRequest=null;eventSource?.close();hide('qr-box');hide('oauth-actions');hide('lease-box');show('empty-qr');hide('quark-request-box');tab('upload');
  $('state-pill').textContent='等待上传';$('state-pill').className='pill';
  $('empty-title').textContent='上传你自己的夸克登录二维码';
  $('empty-description').textContent=config.quark_manual_confirmation_enabled?'在夸克官网打开扫码登录，截图后上传到这里。请保留官网窗口，等待会员端确认。':'在夸克官网打开扫码登录，截图后上传到这里。自动确认需要有效会员凭据和官方签名来源；当前尚缺 App 签名。';
  status('订单已验证，请上传自己夸克官网的登录二维码。','success');
}
function renderQuark(task){
  if(!config.quark_manual_confirmation_enabled&&!task.member_confirmation_recorded){prepareQuarkUpload();return;}
  quarkRequest=task;hide('qr-box');hide('empty-qr');hide('oauth-actions');show('quark-request-box');
  const pending=task.status==='WAIT_USER', confirmed=task.member_confirmation_recorded;
  $('quark-buyer-title').textContent=pending?'待会员确认':confirmed?(task.buyer_reported_login?'你已标记登录完成':'会员端已确认'):'二维码请求已结束';
  $('quark-buyer-description').textContent=pending?`二维码已绑定本订单。请保持自己的夸克登录页打开，不要刷新二维码。本站受理截止：${date(task.expire_at)}；夸克可能更早使二维码失效。`:
    confirmed?'请返回自己的夸克官网窗口检查登录结果。本站记录会员确认和买家反馈，不能直接读取你的夸克登录状态。':'请在自己的夸克官网刷新二维码，然后重新上传。';
  $('quark-cancel').classList.toggle('hidden',!pending);$('quark-login-reported').classList.toggle('hidden',!confirmed||task.buyer_reported_login);
  if(pending){$('state-pill').textContent='待会员确认';status('二维码已提交，等待会员端用 App 扫描并确认。');}
  else if(confirmed){status(task.buyer_reported_login?'已记录你的登录反馈。':'会员端已记录确认，请在自己的夸克官网检查是否登录。','success');}
  else{status('二维码请求已结束，可重新上传。');clearInterval(quarkPoll);}
}
function pollQuark(){clearInterval(quarkPoll);quarkPoll=setInterval(()=>loadResult().catch(e=>{clearInterval(quarkPoll);error(e);}),5000);}
action('quark-cancel',async()=>{await api(`/api/activation/quark/${quarkRequest.id}/cancel`,{});clearInterval(quarkPoll);quarkRequest=null;prepareQuarkUpload();});
action('quark-login-reported',async()=>{renderQuark(await api(`/api/activation/quark/${quarkRequest.id}/login-reported`,{}));});

function date(timestamp){return new Date(timestamp*1000).toLocaleString('zh-CN',{hour12:false});}
function setLease(data){
  lease=data.lease;accessToken=data.access_token||accessToken;clockAtSync=data.server_time;syncPerformance=performance.now();
  hide('qr-box');hide('empty-qr');hide('oauth-actions');show('lease-box');$('lease-title').textContent=quarkRequest?'订单服务已激活':'服务已激活';
  $('lease-scope').classList.toggle('hidden',!quarkRequest);
  $('lease-order').textContent=lease.order_id;$('lease-start').textContent=date(lease.activated_at);$('lease-expire').textContent=date(lease.expire_at);$('lease-provider').textContent=lease.provider;
  $('state-pill').textContent='已激活';$('state-pill').className='pill active';status('授权成功，服务已激活。','success');
  clearInterval(countdownTimer);let expired=false;
  const update=()=>{const now=clockAtSync+(performance.now()-syncPerformance)/1000;const seconds=Math.max(0,Math.ceil(lease.expire_at-now));$('countdown').textContent=[Math.floor(seconds/3600),Math.floor(seconds%3600/60),seconds%60].map(n=>String(n).padStart(2,'0')).join(':');
    if(seconds===0&&!expired){expired=true;clearInterval(countdownTimer);$('state-pill').textContent='已到期';$('state-pill').className='pill expired';$('lease-title').textContent='服务已到期';status('授权已到期，服务器将拒绝后续请求。');testService().catch(()=>{});}};
  update();countdownTimer=setInterval(update,250);
  if(lease.expire_at-clockAtSync>settingsAccessRefreshSeconds())setTimeout(()=>api('/api/auth/refresh',{}).then(setLease).catch(error),settingsAccessRefreshSeconds()*1000);
}
function settingsAccessRefreshSeconds(){return 840;}
let resultRequest;
async function loadResult(){if(resultRequest)return resultRequest;resultRequest=(async()=>{const data=await api('/api/activation/result',{});if(data.quark_request)quarkRequest=data.quark_request;if(data.status==='active'){setLease(data);clearInterval(quarkPoll);}if(data.quark_request)renderQuark(data.quark_request);return data;})();try{return await resultRequest;}finally{resultRequest=null;}}
async function testService(){
  try{const data=await api('/api/service',undefined,{headers:accessToken?{Authorization:`Bearer ${accessToken}`}:{}});$('service-result').textContent=`✓ ${data.message} · 剩余 ${data.lease.remaining_seconds} 秒`;}
  catch(e){$('service-result').textContent=`API 已拒绝请求：${messages[e.message]||e.message}`;throw e;}
}
action('service-test',testService);
action('demo-create',async()=>{const data=await api('/api/demo/orders',{product_id:$('demo-product').value});demoCode=data.code;$('demo-order').textContent=`收件箱 · ${data.order_no} · 已付款`;$('demo-code').textContent=data.code;show('demo-inbox');status('订单已付款，卡密已投递到演示收件箱。','success');});
action('demo-use',()=>{tab('code');$('code-input').value=demoCode;$('code-input').focus();});
async function showOAuth(data){
  eventSource?.close();
  $('oauth-code').textContent=`官方确认码：${data.user_code}`;$('oauth-link').href=data.verification_uri_complete;
  $('qr-image').src=await QRCode.toDataURL(data.verification_uri_complete,{width:320,margin:2});show('qr-box');hide('scan-link');hide('qr-renew');$('qr-description').textContent='请在官方认证页面确认授权';
  const poll=setInterval(async()=>{try{const result=await loadResult();if(result.status==='active'||['FAILED','EXPIRED','CANCELLED'].includes(result.authorization_status)){clearInterval(poll);if(result.status!=='active')status('官方授权未完成或已过期。','error');}}catch(e){clearInterval(poll);error(e);}},Math.max(5,data.interval)*1000);
}
action('oauth-start',async()=>showOAuth(await api('/api/providers/oauth/authorize',{})));

const names={'orders':'订单','cdks':'卡密','leases':'Lease','resources':'资源','quark-requests':'夸克待确认','authorization-tasks':'授权任务','audit':'审计日志','qr-sessions':'扫码状态','deliveries':'投递记录'};
let collection=location.pathname.split('/')[2]||'orders';
function renderQuarkAdmin(items,now){
  const queue=$('quark-admin-queue');queue.replaceChildren();show('quark-admin-queue');hide('admin-table-wrap');
  if(!items.length){queue.textContent='暂无买家上传的夸克二维码';return;}
  for(const item of items){
    const card=document.createElement('section');card.className='panel quark-card';
    const title=document.createElement('h3');title.textContent=`订单 ${item.order_id}`;
    const detail=document.createElement('p');detail.textContent=`${item.status} · 截止 ${date(item.expire_at)}`;card.append(title,detail);
    if(item.status==='WAIT_USER'&&item.expire_at>now){
      const image=document.createElement('img');image.src=`/api/admin/quark-requests/${item.id}/image`;image.alt='买家自己的夸克登录二维码';card.append(image);
      const instructions=document.createElement('p');instructions.textContent='用已登录会员账号的夸克 App 扫描此码，并在手机完成确认。买家官网会自行领取登录票据。';card.append(instructions);
      const label=document.createElement('label'),check=document.createElement('input');check.type='checkbox';label.append(check,document.createTextNode(' 我已在会员 App 完成确认'));card.append(label);
      const button=document.createElement('button');button.textContent='记录会员端确认';button.className='primary';button.disabled=true;check.addEventListener('change',()=>button.disabled=!check.checked);
      button.addEventListener('click',async()=>{button.disabled=true;try{await api(`/api/admin/quark-requests/${item.id}/confirmed`,{phone_confirmed:true});await loadAdmin();status('会员确认已记录。请让买家在自己的夸克页面检查登录。','success');}catch(e){error(e);button.disabled=!check.checked;}});card.append(button);
    }else{const note=document.createElement('p');note.textContent=['OWNER_CONFIRMED','BUYER_REPORTED_LOGIN'].includes(item.status)?'会员端确认已记录；买家实际登录状态需在其夸克页面检查。':'此二维码不可继续确认，请让买家刷新后重新上传。';card.append(note);}
    queue.append(card);
  }
}
async function loadAdmin(){
  const data=await api(`/api/admin/${collection}?offset=${adminOffset}`);hide('admin-login');show('admin-console');$('admin-title').textContent=names[collection]||collection;adminNext=data.next_offset;
  $('admin-prev').disabled=adminOffset===0;$('admin-next').disabled=adminNext===null;
  document.querySelectorAll('.admin-nav a').forEach(a=>a.classList.toggle('selected',a.pathname.endsWith('/'+collection)));
  if(collection==='quark-requests'){renderQuarkAdmin(data.items,data.server_time);return;}
  hide('quark-admin-queue');show('admin-table-wrap');
  if(collection==='resources'){show('resource-form');show('quark-cookie-panel');const state=await api('/api/admin/quark-cookie/status');$('quark-cookie-result').textContent=state.configured?`凭据已保存 · ${state.entry_count} 个条目 · 账号登录尚未验证${state.partial?' · 部分条目缺失，请替换原始 Cookie':''}`:'尚未配置 Cookie';}
  const table=$('admin-table');table.replaceChildren();
  if(!data.items.length){const tr=table.insertRow();tr.insertCell().textContent='暂无记录';return;}
  const columns=Object.keys(data.items[0]);const head=table.createTHead().insertRow();columns.forEach(key=>{const th=document.createElement('th');th.textContent=key;head.append(th);});
  if(collection==='leases'){const th=document.createElement('th');th.textContent='操作';head.append(th);}
  const tbody=table.createTBody();for(const item of data.items){const row=tbody.insertRow();for(const key of columns){const cell=row.insertCell();cell.textContent=key.endsWith('_at')&&item[key]?date(item[key]):String(item[key]??'—');}
    if(collection==='leases'){const cell=row.insertCell();if(item.status==='active'){const button=document.createElement('button');button.textContent='立即撤销';button.className='row-revoke';button.addEventListener('click',async()=>{button.disabled=true;try{await api(`/api/admin/leases/${item.id}/revoke`,{});await loadAdmin();}catch(e){error(e);}finally{button.disabled=false;}});cell.append(button);}}}
  document.querySelectorAll('.admin-nav a').forEach(a=>a.classList.toggle('selected',a.pathname.endsWith('/'+collection)));
}
action('admin-signin',async()=>{await api('/api/admin/login',{token:$('admin-token').value});$('admin-token').value='';await loadAdmin();});
action('admin-signout',async()=>{await api('/api/admin/logout',{});hide('admin-console');show('admin-login');});
action('admin-refresh',loadAdmin);action('admin-prev',async()=>{adminOffset=Math.max(0,adminOffset-100);await loadAdmin();});action('admin-next',async()=>{if(adminNext!==null){adminOffset=adminNext;await loadAdmin();}});
action('resource-add',async()=>{await api('/api/admin/resources',{provider:$('resource-provider').value,capacity:Number($('resource-capacity').value)});await loadAdmin();});
action('quark-cookie-import',async()=>{const input=$('quark-cookie-input');const value=input.value;input.value='';await api('/api/admin/quark-cookie/import',{cookie:value});await loadAdmin();});
action('quark-cookie-check',async()=>{const result=await api('/api/admin/quark-cookie/check',{});$('quark-cookie-result').textContent=`账号可用：${result.authenticated?'是':'尚未验证'} · 账号接口：${result.account_check}${result.account_check_code!==undefined?' ('+result.account_check_code+')':''} · 会员接口：${result.membership_check}${result.member_type!==undefined?' · 会员类型码：'+result.member_type:''}${result.network_or_response_error?' · 网络或响应异常':''}`;});
action('quark-cookie-clear',async()=>{await api('/api/admin/quark-cookie/clear',{});await loadAdmin();});
action('quark-native-confirm',async()=>{
  const ids=['quark-native-qr','quark-native-sign','quark-native-kps','quark-native-vcode'];
  const values=ids.map(id=>$(id).value);ids.forEach(id=>$(id).value='');
  const result=await api('/api/admin/quark-cookie/confirm-qr',{raw:values[0],sign_wg:values[1],kps_wg:values[2],vcode:Number(values[3])});
  $('quark-native-result').textContent=`官方确认${result.confirmation_accepted?'已接受':'未接受'} · 状态码 ${result.upstream_status} · 二维码状态 ${result.poll.state}。请在买家官网页面检查最终登录结果。`;
});

async function boot(){
  csrf=(await api('/api/csrf')).token;config=await api('/api/config');
  if(location.pathname.startsWith('/admin/')){hide('buyer-page');show('admin-page');try{await loadAdmin();}catch(e){if(e.message!=='admin_authentication_required')throw e;}return;}
  if(config.demo_enabled)show('demo-panel');
  if(location.pathname.startsWith('/scan/')){const raw=location.href;history.replaceState(null,'',location.pathname);if(!raw.includes('#'))throw new Error('扫码凭据缺失，请重新扫描原二维码');await handleRaw(raw);return;}
  const code=new URLSearchParams(location.hash.slice(1)).get('code');history.replaceState(null,'',location.pathname);
  if(code){tab('code');$('code-input').value=code;await redeem(code);}else{try{const result=await loadResult();if(result.quark_request){tab('upload');if(result.quark_request.status==='WAIT_USER')pollQuark();}else if(result.status==='pending'){if(result.product_id==='service-24h')prepareQuarkUpload();else{if(config.oauth_enabled)show('oauth-actions');if(result.authorization)await showOAuth(result.authorization);else if(result.authorization_status==='AUTHORIZED')status('官方授权已成功，正在等待服务资源…');else await generateQR();}}}catch(e){if(!['activation_session_required','activation_already_claimed'].includes(e.message))error(e);}}
}
boot().catch(error);
