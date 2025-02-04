import asyncio
import sys
import traceback
import pytest
import pytest_mock
import rx

import deriv_api
from deriv_api.errors import APIError, ConstructionError, ResponseError
from deriv_api.easy_future import EasyFuture
from rx.subject import Subject
import rx.operators as op
import pickle
import json
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError

class MockedWs:
    def __init__(self):
        self.data = []
        self.called = {'send': [], 'recv' : []}
        self.slept_at = 0
        self.queue = Subject()
        self.req_res_map = {}
        async def build_queue():
            while 1:
                await asyncio.sleep(0.01)
                # make queue
                for idx, d in enumerate(self.data):
                    if d is None:
                        continue
                    await asyncio.sleep(0.01)
                    try:
                        self.queue.on_next(json.dumps(d))
                    except Exception as err:
                        print(str(err))
                    # if subscription, then we keep it
                    if not d.get('subscription'):
                        self.data[idx] = None
        self.task_build_queue = asyncio.create_task(build_queue())
    async def send(self, request):
        self.called['send'].append(request)
        request = json.loads(request)
        new_request = request.copy()
        # req_id will be generated by api automatically
        req_id = new_request.pop('req_id')
        key = pickle.dumps(new_request)
        response = self.req_res_map.get(key)
        if response:
            response['req_id'] = req_id
            self.data.append(response)
            self.req_res_map.pop(key)
        forget_id = request.get('forget')
        if forget_id:
            found = 0
            for idx, d in enumerate(self.data):
                if d is None:
                    continue
                subscription_data = d.get('subscription')
                if subscription_data and subscription_data['id'] == forget_id:
                    self.data[idx] = None
                    found = 1
                    break
            self.data.append({"echo_req": {
                'req_id': req_id,
                'forget': forget_id,
            },
                'forget': found,
                'req_id': req_id,
                'msg_type': 'forget'
            })

    async def recv(self):
        self.called['recv'].append(None)
        data = await self.queue.pipe(op.first(),op.to_future())
        return data

    def add_data(self,response):
        request = response['echo_req'].copy()
        # req_id will be added by api automatically
        # we remove it here for consistence
        request.pop('req_id', None)
        key = pickle.dumps(request)
        self.req_res_map[key] = response

    def clear(self):
        self.task_build_queue.cancel('end')

def test_connect_parameter():
    with pytest.raises(ConstructionError, match=r"An app_id is required to connect to the API"):
        deriv_api_obj = deriv_api.DerivAPI(endpoint=5432)

    with pytest.raises(ConstructionError, match=r"Endpoint must be a string, passed: <class 'int'>"):
        deriv_api_obj = deriv_api.DerivAPI(app_id=1234, endpoint=5432)

    with pytest.raises(ConstructionError, match=r"Invalid URL:local123host"):
        deriv_api_obj = deriv_api.DerivAPI(app_id=1234, endpoint='local123host')

@pytest.mark.asyncio
async def test_deriv_api(mocker):
    mocker.patch('deriv_api.DerivAPI.api_connect', return_value='')
    api = deriv_api.DerivAPI(app_id=1234, endpoint='localhost')
    assert(isinstance(api, deriv_api.DerivAPI))
    await asyncio.sleep(0.1)
    await api.clear()

@pytest.mark.asyncio
async def test_get_url(mocker):
    api = get_deriv_api(mocker)
    assert api.get_url("localhost") == "wss://localhost"
    assert api.get_url("ws://localhost") == "ws://localhost"
    with pytest.raises(ConstructionError, match=r"Invalid URL:testurl"):
        api.get_url("testurl")
    await asyncio.sleep(0.1)
    await api.clear()

def get_deriv_api(mocker):
    mocker.patch('deriv_api.DerivAPI.api_connect', return_value=EasyFuture().set_result(1))
    api = deriv_api.DerivAPI(app_id=1234, endpoint='localhost')
    return api

@pytest.mark.asyncio
async def test_mocked_ws():
    wsconnection = MockedWs()
    data1 = {"echo_req":{"ticks" : 'R_50', 'req_id': 1} ,"msg_type": "ticks", "req_id": 1, "subscription": {"id": "world"}}
    data2 = {"echo_req":{"ping": 1, 'req_id': 2},"msg_type": "ping", "pong": 1, "req_id": 2}
    wsconnection.add_data(data1)
    wsconnection.add_data(data2)
    await wsconnection.send(json.dumps(data1["echo_req"]))
    await wsconnection.send(json.dumps(data2["echo_req"]))
    assert json.loads(await wsconnection.recv()) == data1, "we can get first data"
    assert json.loads(await wsconnection.recv()) == data2, "we can get second data"
    assert json.loads(await wsconnection.recv()) == data1, "we can still get first data becaues it is a subscription"
    assert json.loads(await wsconnection.recv()) == data1, "we will not get second data because it is not a subscription"
    assert len(wsconnection.called['send']) == 2
    assert len(wsconnection.called['recv']) == 4
    wsconnection.clear()

@pytest.mark.asyncio
async def test_simple_send():
    wsconnection = MockedWs()
    api = deriv_api.DerivAPI(connection = wsconnection)
    data1 = {"echo_req":{"ping": 1},"msg_type": "ping", "pong": 1}
    data2 = {"echo_req":{"ticks" : 'R_50'} ,"msg_type": "ticks"}
    wsconnection.add_data(data1)
    wsconnection.add_data(data2)
    res1 = data1.copy()
    add_req_id(res1, 1)
    res2 = data2.copy()
    add_req_id(res2, 2)
    assert await api.send(data1['echo_req']) == res1
    assert await api.ticks(data2['echo_req']) == res2
    assert len(wsconnection.called['send']) == 2
    wsconnection.clear()
    await api.clear()

@pytest.mark.asyncio
async def test_subscription():
    wsconnection = MockedWs()
    api = deriv_api.DerivAPI(connection=wsconnection)
    r50_data = {
        'echo_req': {'ticks': 'R_50', 'subscribe': 1},
        'msg_type': 'tick',
        'subscription': {'id': 'A11111'}
    }
    r100_data = {
        'echo_req': {'ticks': 'R_100', 'subscribe': 1},
        'msg_type': 'tick',
        'subscription': {'id': 'A22222'}
    }
    wsconnection.add_data(r50_data)
    wsconnection.add_data(r100_data)
    r50_req = r50_data['echo_req']
    r50_req.pop('subscribe');
    r100_req = r100_data['echo_req']
    r100_req.pop('subscribe');
    sub1 = await api.subscribe(r50_req)
    sub2 = await api.subscribe(r100_req)
    f1 = sub1.pipe(op.take(2), op.to_list(), op.to_future())
    f2 = sub2.pipe(op.take(2), op.to_list(), op.to_future())
    result = await asyncio.gather(f1, f2)
    assert result == [[r50_data, r50_data], [r100_data, r100_data]]
    await asyncio.sleep(0.01)  # wait sending 'forget' finished
    assert wsconnection.called['send'] == [
        '{"ticks": "R_50", "subscribe": 1, "req_id": 1}',
        '{"ticks": "R_100", "subscribe": 1, "req_id": 2}',
        '{"forget": "A11111", "req_id": 3}',
        '{"forget": "A22222", "req_id": 4}']
    wsconnection.clear()
    await api.clear()

@pytest.mark.asyncio
async def test_forget():
    wsconnection = MockedWs()
    api = deriv_api.DerivAPI(connection=wsconnection)
    # test subscription forget will mark source done
    r50_data = {
        'echo_req': {'ticks': 'R_50', 'subscribe': 1},
        'msg_type': 'tick',
        'subscription': {'id': 'A11111'}
    }
    wsconnection.add_data(r50_data)
    r50_req = r50_data['echo_req']
    r50_req.pop('subscribe');
    sub1: rx.Observable = await api.subscribe(r50_req)
    complete = False

    def on_complete():
        nonlocal complete
        complete = True

    sub1.subscribe(on_completed=on_complete)
    await asyncio.sleep(0.1)
    assert not complete, 'subscription not stopped'
    await api.forget('A11111')
    await asyncio.sleep(0.1)
    assert complete, 'subscription stopped after forget'
    wsconnection.clear()
    await api.clear()


@pytest.mark.asyncio
async def test_extra_response():
    wsconnection = MockedWs()
    api = deriv_api.DerivAPI(connection=wsconnection)
    error = None
    async def get_sanity_error():
        nonlocal error
        error = await api.sanity_errors.pipe(op.first(),op.to_future())
    error_task = asyncio.create_task(get_sanity_error())
    wsconnection.data.append({"hello":"world"})
    try:
        await asyncio.wait_for(error_task, timeout=0.1)
        assert str(error) == 'APIError:Extra response'
    except asyncio.exceptions.TimeoutError:
        assert False, "error data apppear timeout "
    wsconnection.clear()
    await api.clear()

@pytest.mark.asyncio
async def test_response_error():
    wsconnection = MockedWs()
    api = deriv_api.DerivAPI(connection=wsconnection)
    r50_data = {
        'echo_req': {'ticks': 'R_50', 'subscribe': 1},
        'msg_type': 'tick',
        'error': {'code': 'TestError', 'message': 'test error message'}
    }
    wsconnection.add_data(r50_data)
    sub1 = await api.subscribe(r50_data['echo_req'])
    f1 = sub1.pipe(op.first(), op.to_future())
    with pytest.raises(ResponseError, match='ResponseError: test error message'):
        await f1
    r50_data = {
        'echo_req': {'ticks': 'R_50', 'subscribe': 1},
        'msg_type': 'tick',
        'req_id': f1.exception().req_id,
        'subscription': {'id': 'A111111'}
    }
    wsconnection.data.append(r50_data) # add back r50 again
    #will send a `forget` if get a response again
    await asyncio.sleep(0.1)
    assert wsconnection.called['send'][-1] == '{"forget": "A111111", "req_id": 2}'
    poc_data = {
        'echo_req': {'proposal_open_contract': 1, 'subscribe': 1},
        'msg_type': 'proposal_open_contract',
        'error': {'code': 'TestError', 'message': 'test error message'},
        'subscription': {'id': 'ABC11111'}
    }
    wsconnection.add_data(poc_data)
    sub1 = await api.subscribe(poc_data['echo_req'])
    response = await sub1.pipe(op.first(), op.to_future())
    assert 'error' in response, "for the poc stream with out contract_id, the error response will not terminate the stream"
    wsconnection.clear()
    await api.clear()

@pytest.mark.asyncio
async def test_cache():
    wsconnection = MockedWs()
    api = deriv_api.DerivAPI(connection=wsconnection)
    wsconnection.add_data({'ping':'pong', 'msg_type': 'ping', 'echo_req' : {'ping': 1}})
    ping1 = await api.ping({'ping': 1})
    assert len(wsconnection.called['send']) == 1
    ping2 = await api.expect_response('ping')
    assert len(wsconnection.called['send']) == 1, 'send can cache value for expect_response. get ping2 from cache, no send happen'
    assert ping1 == ping2, "ping2 is ping1 "
    ping3 = await api.cache.ping({'ping': 1})
    assert len(wsconnection.called['send']) == 1, 'get ping3 from cache, no send happen'
    assert ping1 == ping3, "ping3 is ping1 "
    wsconnection.clear()
    await api.clear()

    wsconnection = MockedWs()
    api = deriv_api.DerivAPI(connection=wsconnection)
    wsconnection.add_data({'ping': 'pong', 'msg_type': 'ping', 'echo_req': {'ping': 1}})
    ping1 = await api.cache.ping({'ping': 1})
    assert len(wsconnection.called['send']) == 1
    ping2 = await api.expect_response('ping')
    assert len(wsconnection.called['send']) == 1, 'api.cache.ping can cache value. get ping2 from cache, no send happen'
    assert ping1 == ping2, "ping2 is ping1 "
    wsconnection.clear()
    await api.clear()

@pytest.mark.asyncio
async def test_can_subscribe_one_source_many_times():
    wsconnection = MockedWs()
    api = deriv_api.DerivAPI(connection=wsconnection)
    r50_data = {
        'echo_req': {'ticks': 'R_50', 'subscribe': 1},
        'msg_type': 'tick',
        'subscription': {'id': 'A11111'}
    }
    wsconnection.add_data(r50_data)
    r50_req = r50_data['echo_req']
    r50_req.pop('subscribe');
    sub1 = await api.subscribe(r50_req)
    f1 = sub1.pipe(op.take(2), op.to_list(), op.to_future())
    f2 = sub1.pipe(op.take(2), op.to_list(), op.to_future())
    result = await asyncio.gather(f1,f2)
    assert result == [[r50_data, r50_data],[r50_data, r50_data]]
    await asyncio.sleep(0.01)  # wait sending 'forget' finished
    assert wsconnection.called['send'] == [
        '{"ticks": "R_50", "subscribe": 1, "req_id": 1}',
        '{"forget": "A11111", "req_id": 2}']
    wsconnection.clear()
    await api.clear()

@pytest.mark.asyncio
async def test_reuse_poc_stream():
    wsconnection = MockedWs()
    api = deriv_api.DerivAPI(connection=wsconnection)
    buy_data = {'echo_req': {'buy': 1, 'subscribe': 1},
                           'subscription':  {'id': 'B111111'},
                           'buy': {'contract_id': 1234567},
                           'msg_type': 'proposal_open_contract'
                           }
    wsconnection.add_data(buy_data)
    sub1 = await api.subscribe(buy_data['echo_req'])
    await asyncio.sleep(0.1) # wait for setting reused stream
    sub2 = await api.subscribe({'proposal_open_contract': 1, 'contract_id': 1234567})
    assert id(sub1) == id(sub2)
    assert len(api.subscription_manager.buy_key_to_contract_id) == 1
    await api.forget('B111111')
    assert len(api.subscription_manager.buy_key_to_contract_id) == 0
    wsconnection.clear()
    await api.clear()

@pytest.mark.asyncio
async def test_expect_response():
    wsconnection = MockedWs()
    api = deriv_api.DerivAPI(connection=wsconnection)
    wsconnection.add_data({'ping':'pong', 'msg_type': 'ping', 'echo_req' : {'ping': 1}})
    get_ping = api.expect_response('ping')
    assert not get_ping.done(), 'get ping is a future and is pending'
    ping_result = await api.ping({'ping': 1})
    assert get_ping.done(), 'get ping done'
    assert ping_result == await get_ping
    wsconnection.clear()
    await api.clear()

@pytest.mark.asyncio
async def test_ws_disconnect():
    class MockedWs2(MockedWs):
        def __init__(self):
            self.closed = EasyFuture()
            self.exception = ConnectionClosedOK(1000, 'test disconnect')
            super().__init__()
        async def close(self):
            self.closed.resolve(self.exception)
            pass
        async def send(self):
            exc = await self.closed
            raise exc
        async def recv(self):
            exc = await self.closed
            raise exc

    # closed by api
    wsconnection = MockedWs2()
    wsconnection.exception = ConnectionClosedOK(1000, 'Closed by api')
    api = deriv_api.DerivAPI(connection=wsconnection)
    await asyncio.sleep(0.1)
    api.wsconnection_from_inside = True
    last_error = api.sanity_errors.pipe(op.first(), op.to_future())
    await asyncio.sleep(0.1) # waiting for init finished
    print("here 382")
    await api.disconnect() # it will set connected as 'Closed by disconnect', and cause MockedWs2 raising `test disconnect`
    print("here 384")
    assert isinstance((await last_error), ConnectionClosedOK), 'sanity error get errors'
    print("here 386")
    with pytest.raises(ConnectionClosedOK, match='Closed by disconnect'):
        await api.send({'ping': 1})  # send will get same error
    print("here 389")
    with pytest.raises(ConnectionClosedOK, match='Closed by disconnect'):
        await api.connected # send will get same error
    wsconnection.clear()
    print("here 391")
    await api.clear()

    # closed by remote
    wsconnection = MockedWs2()
    api = deriv_api.DerivAPI(connection=wsconnection)
    wsconnection.exception = ConnectionClosedError(1234, 'Closed by remote')
    last_error = api.sanity_errors.pipe(op.first(), op.to_future())
    await asyncio.sleep(0.1) # waiting for init finished
    await wsconnection.close() # it will set connected as 'Closed by disconnect', and cause MockedWs2 raising `test disconnect`
    assert isinstance((await last_error), ConnectionClosedError), 'sanity error get errors'
    with pytest.raises(ConnectionClosedError, match='Closed by remote'):
        await api.send({'ping': 1})  # send will get same error
    with pytest.raises(ConnectionClosedError, match='Closed by remote'):
        await api.connected  # send will get same error
    wsconnection.clear()
    await api.clear()

@pytest.mark.asyncio
async def test_add_task():
    wsconnection = MockedWs()
    api = deriv_api.DerivAPI(connection=wsconnection)
    exception_f = api.sanity_errors.pipe(op.first(), op.to_future())
    async def raise_an_exception():
        raise Exception("test add_task")
    api.add_task(raise_an_exception(), 'raise an exception')
    exception = await exception_f
    assert str(exception) == 'deriv_api:raise an exception: test add_task'
    await api.clear()

def add_req_id(response, req_id):
    response['echo_req']['req_id'] = req_id
    response['req_id'] = req_id
    return response