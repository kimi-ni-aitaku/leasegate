import {test,expect} from '@playwright/test';

test('create order, redeem, scan in another browser, SSE, service and admin revoke',async({page,browser})=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('/');await expect(page.locator('#demo-panel')).toBeVisible();
  await page.getByRole('button',{name:'创建测试订单'}).click();await expect(page.locator('#demo-inbox')).toBeVisible();
  await page.locator('#demo-use').click();await page.locator('#redeem').click();
  await expect(page.locator('#qr-image')).toBeVisible();
  await page.reload();await expect(page.locator('#qr-image')).toBeVisible();
  const url=await page.locator('#scan-link').getAttribute('href');
  const scanner=await browser.newContext();const scanPage=await scanner.newPage();await scanPage.goto(url);
  await expect(scanPage.locator('#confirmation')).toBeVisible();await scanPage.locator('#confirm-button').click();
  await expect(page.locator('#lease-title')).toHaveText('服务已激活');await page.locator('#service-test').click();
  await expect(page.locator('#service-result')).toContainText('服务可正常访问');
  await page.reload();await expect(page.locator('#lease-title')).toHaveText('服务已激活');
  if(process.env.ADMIN_TOKEN){await page.goto('/admin/leases');await page.locator('#admin-token').fill(process.env.ADMIN_TOKEN);await page.locator('#admin-signin').click();
    await expect(page.locator('#admin-console')).toBeVisible();await page.locator('.row-revoke').first().click();await expect(page.locator('#admin-table')).toContainText('revoked');}
  expect(errors).toEqual([]);await scanner.close();
});

for(const mode of ['upload','drop','paste'])test(`browser decodes ${mode} image locally, confirms and shows countdown`,async({page})=>{
  let imageUploads=0;page.on('request',r=>{if(r.url().endsWith('/api/qr/decode-image'))imageUploads++;});
  await page.goto('/activate');await page.locator('#demo-create').click();await page.locator('#demo-use').click();await page.locator('#redeem').click();
  await expect(page.locator('#qr-image')).toBeVisible();
  const dataURL=await page.locator('#qr-image').getAttribute('src');const buffer=Buffer.from(dataURL.split(',')[1],'base64');
  if(mode==='upload'){
    await page.getByRole('tab',{name:'上传二维码'}).click();await page.locator('#image-file').setInputFiles({name:'qr.png',mimeType:'image/png',buffer});
  }else{
    await page.getByRole('tab',{name:mode==='paste'?'粘贴截图':'上传二维码'}).click();
    await page.evaluate(({dataURL,mode})=>{const binary=atob(dataURL.split(',')[1]);const bytes=Uint8Array.from(binary,c=>c.charCodeAt(0));const file=new File([bytes],'qr.png',{type:'image/png'});const transfer=new DataTransfer();transfer.items.add(file);
      if(mode==='paste')document.dispatchEvent(new ClipboardEvent('paste',{clipboardData:transfer,bubbles:true}));
      else document.querySelector('label.dropzone').dispatchEvent(new DragEvent('drop',{dataTransfer:transfer,bubbles:true,cancelable:true}));
    },{dataURL,mode});
  }
  await expect(page.locator('#confirmation')).toBeVisible();expect(imageUploads).toBe(0);
  await page.locator('#confirm-button').click();await expect(page.locator('#lease-title')).toHaveText('服务已激活');
  await expect(page.locator('#countdown')).toHaveText(/00:00:\d{2}/);
});

test('mobile layout fits viewport and camera denial leaves upload usable',async({page})=>{
  await page.addInitScript(()=>{if(navigator.mediaDevices)navigator.mediaDevices.getUserMedia=async()=>{throw new DOMException('Camera permission denied','NotAllowedError');};});
  await page.setViewportSize({width:390,height:844});await page.goto('/');await expect(page.locator('#demo-create')).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.locator('#camera-open').click();await expect(page.locator('#status')).toHaveClass(/error/);
  await page.getByRole('tab',{name:'上传二维码'}).click();await expect(page.locator('#image-file')).toBeVisible();
});

test('getUserMedia and ZXing read a QR from a simulated webcam',async({page})=>{
  test.skip(!process.env.CAMERA_QR_FILE,'Set CAMERA_QR_FILE to a Y4M fixture');
  await page.goto('/activate');await page.locator('#camera-open').click();
  await expect(page.locator('#confirmation')).toBeVisible({timeout:15000});
  await expect(page.locator('#camera-placeholder')).toBeVisible();
  await expect(page.locator('#camera-stop')).toBeHidden();
});
