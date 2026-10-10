import {chromium} from '@playwright/test';
import fs from 'node:fs';
const outputDir=process.env.QUARK_TEST_OUTPUT||'test-results/quark-buyer';
fs.mkdirSync(outputDir,{recursive:true});
const baseURL=process.env.BASE_URL||'http://localhost:8000';
const report={scenario:'Fresh Quark browser + demo paid order + real official QR screenshot',simulation:'demo payment; live official login QR',steps:[]};
const browser=await chromium.launch({...(process.env.CHROME_EXECUTABLE?{executablePath:process.env.CHROME_EXECUTABLE}:{}),headless:true});
try {
 const buyer=await browser.newContext({viewport:{width:1365,height:900}});
 const pan=await buyer.newPage();
 const pollStatuses=[];
 pan.on('response',async r=>{try{if(new URL(r.url()).pathname==='/cas/ajax/getServiceTicketByQrcodeToken'){const d=await r.json();pollStatuses.push({http:r.status(),status:d.status,has_service_ticket:!!d.data?.members?.service_ticket});}}catch{}});
 const qrReady=pan.waitForResponse(r=>new URL(r.url()).pathname==='/cas/ajax/getTokenForQrcodeLogin',{timeout:30000});
 const response=await pan.goto('https://pan.quark.cn/',{waitUntil:'domcontentloaded',timeout:30000});
 await pan.getByText('扫码登录',{exact:false}).first().waitFor();
 await qrReady;
 await pan.waitForFunction(()=>[...document.querySelectorAll('canvas')].some(c=>{const box=c.getBoundingClientRect();if(box.x<800||box.y<180||box.y>400)return false;try{const pixels=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let dark=0;for(let i=0;i<pixels.length;i+=4)if(pixels[i]+pixels[i+1]+pixels[i+2]<100&&pixels[i+3]>200)dark++;return dark>300;}catch{return false;}}),null,{timeout:15000});
 const screenshot=await pan.screenshot({path:outputDir+'/official-login.png'});
 report.steps.push({step:'official_login_page',http:response.status(),login_prompt_present:(await pan.locator('body').innerText()).includes('扫码登录')});
 const local=await buyer.newPage();await local.goto(baseURL+'/activate');
 await local.locator('#demo-panel').waitFor({state:'visible'});
 await local.locator('#demo-product').selectOption('service-24h');
 await local.locator('#demo-create').click();await local.locator('#demo-inbox').waitFor({state:'visible'});
 await local.locator('#demo-use').click();await local.locator('#redeem').click();await local.locator('#upload-tab').waitFor({state:'visible'});
 report.steps.push({step:'order_redeemed',state:await local.locator('#state-pill').innerText()});
 await local.getByRole('tab',{name:'上传二维码'}).click();
 await local.locator('#image-file').setInputFiles({name:'official-login.png',mimeType:'image/png',buffer:screenshot});
 await local.waitForFunction(()=>document.getElementById('status').classList.contains('error')||!document.getElementById('quark-request-box').classList.contains('hidden'),null,{timeout:15000});
 report.steps.push({step:'real_screenshot_uploaded',message:await local.locator('#status').innerText()});
 const csrf=(await (await buyer.request.get(baseURL+'/api/csrf')).json()).token;
 const auth=await buyer.request.post(baseURL+'/api/providers/quark_reference/authorize',{data:{},headers:{'X-CSRF-Token':csrf}});
 report.steps.push({step:'provider_authorization_attempt',http:auth.status(),detail:(await auth.json()).detail});
 // Observe the real official page's polling without sending any fabricated confirmation.
 try{await pan.waitForResponse(r=>new URL(r.url()).pathname==='/cas/ajax/getServiceTicketByQrcodeToken',{timeout:10000});}catch{}
 const status=await buyer.request.post(baseURL+'/api/activation/result',{data:{},headers:{'X-CSRF-Token':csrf}});
 const state=await status.json();
 report.steps.push({step:'local_authorization_result',http:status.status(),status:state.status,authorization_status:state.authorization_status});
 const protectedResponse=await buyer.request.get(baseURL+'/api/service');
 report.steps.push({step:'local_service_access',http:protectedResponse.status()});
 const visible=(await pan.locator('body').innerText()).includes('扫码登录');
 const cookies=await buyer.cookies('https://pan.quark.cn');
 report.steps.push({step:'official_login_after_attempt',login_prompt_present:visible,has_primary_auth_cookie:cookies.some(c=>c.name==='__pus'),poll_statuses:pollStatuses});
 report.logged_in=(!visible)&&cookies.some(c=>c.name==='__pus');
 report.conclusion=report.logged_in?'official_login_success':'official_login_did_not_complete';
 fs.writeFileSync(outputDir+'/result.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 process.exitCode=report.logged_in?0:2;
 await buyer.close();
} finally {await browser.close();}
