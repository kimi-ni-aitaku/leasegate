"""Run from project root against the development Compose stack. Creates isolated demo orders."""
import json
import subprocess
import time
import httpx


def docker(*args):
    return subprocess.run(['docker','compose',*args],check=True,capture_output=True,text=True).stdout


def ready(client):
    for _ in range(90):
        try:
            if client.get('/api/health').status_code==200:return
        except httpx.HTTPError:pass
        time.sleep(1)
    raise RuntimeError('Compose API did not become ready')


def main():
    docker('up','-d','--build')
    with httpx.Client(base_url='http://localhost',timeout=10) as client:
        ready(client)
        client.headers['X-CSRF-Token']=client.get('/api/csrf').json()['token']
        # This captured stdout intentionally stays in memory; keys are never printed or saved.
        admin=docker('exec','-T','api','python','scripts/show_admin.py').strip()
        assert client.post('/api/admin/login',json={'token':admin}).status_code==200
        def activate(product):
            response=client.post('/api/demo/orders',json={'product_id':product});response.raise_for_status();order=response.json()
            response=client.post('/api/activation/redeem',json={'code':order['code']});response.raise_for_status()
            qr=client.post('/api/qr/create',json={}).json()
            assert client.post(f'/api/qr/{qr["id"]}/scanned',json={'raw':qr['url']}).status_code==200
            assert client.post(f'/api/qr/{qr["id"]}/confirm',json={'raw':qr['url']}).status_code==200
            response=client.post('/api/activation/result',json={});response.raise_for_status();return response.json()
        long=activate('service-24h')
        for container in ['redis','postgres','api','worker']:
            docker('restart',container);ready(client)
            assert client.get('/api/service').status_code==200
        assert client.post(f'/api/admin/leases/{long["lease"]["id"]}/revoke',json={}).status_code==200
        assert client.get('/api/service',headers={'Authorization':'Bearer '+long['access_token']}).status_code==401
        baseline={r['id']:r['active_leases'] for r in client.get('/api/admin/resources').json()['items']}
        short=activate('test-60s');lease=short['lease'];start=lease['activated_at']
        docker('stop','worker')
        try:
            assert client.get('/api/service').status_code==200
            for elapsed in (30,59):
                time.sleep(max(0,start+elapsed+.05-time.time()))
                assert client.get('/api/service').status_code==200
            time.sleep(max(0,lease['expire_at']+.05-time.time()))
            assert client.get('/api/service',headers={'Authorization':'Bearer '+short['access_token']}).status_code==401
            time.sleep(1)
            assert client.get('/api/service').status_code==401
            leases=client.get('/api/admin/leases').json()['items']
            assert next(item for item in leases if item['id']==lease['id'])['status']=='expired'
            resources=client.get('/api/admin/resources').json()['items']
            assert next(item for item in resources if item['id']==lease['resource_id'])['active_leases']==baseline[lease['resource_id']]
        finally:docker('start','worker')
    print('Compose smoke passed: restart recovery, 30/59/60/61s expiry, revocation and resource release')


if __name__=='__main__':main()
