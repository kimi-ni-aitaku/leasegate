import os
import time
import pytest
from conftest import activate


@pytest.mark.realtime
@pytest.mark.skipif(os.getenv('RUN_REALTIME') != '1', reason='Set RUN_REALTIME=1 for a real one-minute test')
def test_real_sixty_second_lease(system):
    service, client, _ = system
    service.clock = lambda: int(time.time())
    _, _, result = activate(client)
    expires = result['lease']['expire_at']
    token = result['access_token']
    assert client.get('/api/service').status_code == 200
    for remaining in (30, 1):
        delay = expires - remaining + .05 - time.time()
        if delay > 0:
            time.sleep(delay)
        assert client.get('/api/service').status_code == 200
    delay = expires + .05 - time.time()
    if delay > 0:
        time.sleep(delay)
    assert client.get('/api/service',headers={'Authorization':'Bearer '+token}).status_code == 401
    time.sleep(1)
    assert client.get('/api/service').status_code == 401
