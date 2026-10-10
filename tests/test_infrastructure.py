import os
import shutil
import socket
import subprocess
import time
from urllib.parse import urlsplit, parse_qs
import pytest
import redis
from sqlalchemy import select
from app.models import Resource, Lease
from conftest import activate


@pytest.mark.infrastructure
@pytest.mark.skipif(os.getenv('RUN_INFRA') != '1', reason='Explicit temporary infrastructure required')
def test_live_redis_stop_restart_and_rebuild(system,tmp_path):
    service,client,_=system
    binary=shutil.which('redis-server')
    assert binary
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    command=[binary,'--bind','127.0.0.1','--port',str(port),'--dir',str(tmp_path),'--appendonly','yes']
    def start():
        process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        cache=redis.Redis(host='127.0.0.1',port=port,socket_timeout=.2,socket_connect_timeout=.2)
        for _ in range(100):
            try:
                if cache.ping():return process,cache
            except redis.RedisError:time.sleep(.05)
        raise RuntimeError('redis_test_start_failed')
    process,cache=start();service.redis=cache
    try:
        _,_,result=activate(client)
        assert cache.zscore('lease:expiry',result['lease']['id'])
        process.terminate();process.wait(timeout=10)
        assert client.get('/api/service').status_code==200
        service.clock.advance(60)
        assert service.expire_due()==1
        process,cache=start();service.redis=cache
        service.expire_due()
        assert cache.zcard('lease:expiry')==0
        assert client.get('/api/service').status_code==401
        with service.database.transaction() as db:assert db.scalar(select(Resource)).active_leases==0
    finally:
        process.terminate();process.wait(timeout=10)


@pytest.mark.infrastructure
@pytest.mark.skipif(os.getenv('RUN_INFRA') != '1' or not os.getenv('PG_TEST_CTL'), reason='Explicit temporary PostgreSQL restart control required')
def test_live_postgres_stop_restart_preserves_lease(system):
    service,client,_=system
    assert not service.database.sqlite
    _,_,result=activate(client)
    control=os.environ['PG_TEST_CTL'];data=os.environ['PG_TEST_DATA']
    socketdir=parse_qs(urlsplit(service.settings.database_url).query)['host'][0]
    def start():
        subprocess.run([control,'-D',data,'-w','-l',data+'/restart.log','-o',f'-h "" -k "{socketdir}"','start'],check=True,stdout=subprocess.DEVNULL)
    subprocess.run([control,'-D',data,'-w','-m','fast','stop'],check=True,stdout=subprocess.DEVNULL)
    try:
        assert client.get('/api/service').status_code==503
    finally:start()
    assert client.get('/api/service').status_code==200
    with service.database.transaction() as db:assert db.get(Lease,result['lease']['id']).status=='active'
    service.clock.advance(61)
    assert client.get('/api/service').status_code==401
