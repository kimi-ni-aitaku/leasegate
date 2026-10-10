"""Generate a live signed-QR webcam fixture and run the complete browser suite."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import httpx
import qrcode
from PIL import Image

parser=argparse.ArgumentParser()
parser.add_argument('--base-url',default=os.getenv('BASE_URL','http://127.0.0.1:8080'))
parser.add_argument('--chrome',default=os.getenv('CHROME_EXECUTABLE',''))
args=parser.parse_args()
with httpx.Client(base_url=args.base_url) as client:
    config=client.get('/api/config').json()
    if not config['demo_enabled'] or config['public_origin']!=args.base_url:
        raise RuntimeError('Browser QA requires demo mode and PUBLIC_ORIGIN matching --base-url')
    client.headers['X-CSRF-Token']=client.get('/api/csrf').json()['token']
    order=client.post('/api/demo/orders',json={'product_id':'test-60s'});order.raise_for_status()
    client.post('/api/activation/redeem',json={'code':order.json()['code']}).raise_for_status()
    qr=client.post('/api/qr/create',json={});qr.raise_for_status()
    encoded=qrcode.QRCode(box_size=1,border=4)
    encoded.add_data(qr.json()['url']);encoded.make(fit=True)
    encoded.box_size=max(1,440//len(encoded.get_matrix()))
    picture=encoded.make_image().convert('L')
with tempfile.TemporaryDirectory(prefix='leasegate-browser-') as temporary:
    canvas=Image.new('L',(640,480),255);canvas.paste(picture,((640-picture.width)//2,(480-picture.height)//2))
    y=canvas.point(lambda value:16+int(value*219/255)).tobytes();uv=bytes([128])*(640*480//2)
    fixture=Path(temporary)/'camera.y4m'
    with fixture.open('wb') as output:
        output.write(b'YUV4MPEG2 W640 H480 F5:1 Ip A1:1 C420jpeg\n')
        for _ in range(20):output.write(b'FRAME\n'+y+uv)
    env={**os.environ,'BASE_URL':args.base_url,'CAMERA_QR_FILE':str(fixture)}
    if args.chrome:env['CHROME_EXECUTABLE']=args.chrome
    result=subprocess.run(['npm','run','test:e2e'],env=env)
    raise SystemExit(result.returncode)
