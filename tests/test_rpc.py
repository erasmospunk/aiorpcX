import asyncio
from collections import deque
import contextlib
from functools import partial
import json
import logging
import numbers
import time
import traceback

import pytest

from aiorpcx import *
from aiorpcx.rpc import (RPCRequest, RPCRequestOut, RPCResponse, RPCBatch,
                         RPCBatchOut, RPCError, RPCHelperBase, RPCProcessor)
from tests.test_util import run_briefly, TaskSet


class LogError(Exception):
    pass


class ExpectAsyncioLog(object):

    def __init__(self, rpc, text):
        self.rpc = rpc
        self.text = text

    def __enter__(self):
        assert not self.rpc.asyncio_logs

    def __exit__(self, exc_type, exc_value, traceback):
        assert len(self.rpc.asyncio_logs) == 1
        assert self.text in self.rpc.asyncio_logs[0]
        self.rpc.asyncio_logs.clear()


def test_RPCRequest():
    request = RPCRequestOut('method', [1], None)
    assert request.method == 'method'
    assert request.args == [1]
    assert request.request_id == 0
    assert not request.is_notification()
    assert repr(request) == "RPCRequest('method', [1], 0)"
    request = RPCRequestOut('method', (1, ), None)
    assert request.method == 'method'
    assert request.args == (1, )
    assert request.request_id == 1
    assert not request.is_notification()
    assert repr(request) == "RPCRequest('method', (1,), 1)"
    request = RPCRequestOut('foo', {"bar": 1}, None)
    assert request.method == 'foo'
    assert request.args == {"bar": 1}
    assert request.request_id == 2
    assert not request.is_notification()
    assert repr(request) == "RPCRequest('foo', {'bar': 1}, 2)"
    request = RPCRequest('foo', [], None)
    assert request.method == 'foo'
    assert request.args == []
    assert request.request_id is None
    assert request.is_notification()
    assert repr(request) == "RPCRequest('foo', [], None)"
    request = RPCRequest('foo', (), None)
    assert request.method == 'foo'
    assert request.args == ()
    assert request.request_id is None
    assert request.is_notification()
    assert repr(request) == "RPCRequest('foo', (), None)"
    # Check {} is preserved (different call semantics)
    for request in [RPCRequest('add', {}, 0), RPCRequestOut('add', {}, None)]:
        assert request.method == 'add'
        assert request.args == {}
        request = RPCRequest('add', {}, 0)
    # Check None gives []
    for request in [RPCRequest('add', None, 0),
                    RPCRequestOut('add', None, None)]:
        request = RPCRequest('add', None, 0)
        assert request.method == 'add'
        assert request.args == []

    loop = asyncio.get_event_loop()
    # Result setting
    valid = False

    def on_done(req):
        nonlocal valid
        valid = req is request

    # Test args good type
    with pytest.raises(ValueError):
        RPCRequestOut('method', 0, on_done)

    request = RPCRequestOut('method', [0], on_done)
    assert not request.done()
    with pytest.raises(asyncio.InvalidStateError):
        request.result()
    assert not request.done()
    request.set_result(42)

    assert not valid  # Not scheduled yet
    loop.run_until_complete(asyncio.sleep(0))
    assert valid

    assert request.result() == 42
    with pytest.raises(asyncio.InvalidStateError):
        request.set_result(35)
    assert request.result() == 42

    # Result setting
    request = RPCRequestOut('method', None, on_done)
    loop.call_later(0.001, request.set_result, 45)
    valid = False
    assert loop.run_until_complete(request) == 45
    assert valid
    assert request.result() == 45

    request = RPCRequestOut('method', None, on_done)
    loop = asyncio.get_event_loop()
    request.set_result(46)
    assert loop.run_until_complete(request) == 46


def test_RPCResponse():
    response = RPCResponse('result', 1)
    assert response.result == 'result'
    assert response.request_id == 1
    assert repr(response) == "RPCResponse('result', 1)"
    error = RPCError(1, 'message', 1)
    assert error.request_id == 1
    response = RPCResponse(error, 5)
    assert response.result == error
    assert response.request_id == 5
    assert error.request_id == 5
    error_repr = repr(error)
    assert repr(response) == f"RPCResponse({error_repr}, 5)"


def test_RPCError():
    error = RPCError(1, 'foo', 2)
    assert error.code == 1
    assert error.message == 'foo'
    assert error.request_id == 2
    assert repr(error) == "RPCError(1, 'foo', 2)"
    for error in (RPCError(5, 'bra'), RPCError(5, 'bra', None)):
        assert error.code == 5
        assert error.message == 'bra'
        assert error.request_id is None
        assert repr(error) == "RPCError(5, 'bra')"


def test_RPCBatch():
    with pytest.raises(AssertionError):
        RPCBatch([])
    with pytest.raises(AssertionError):
        RPCBatch(x for x in (1, 2))
    with pytest.raises(AssertionError):
        RPCBatch([RPCRequest('method', [], 0), RPCResponse(6, 0)])
    with pytest.raises(AssertionError):
        RPCBatch([RPCError(0, 'message', 0), RPCResponse(6, 0)])

    requests = [RPCRequest('method', [], 0),
                RPCRequest('foo', [], None),
                RPCRequest('t', [1], 1)]
    batch = RPCBatch(requests)
    assert batch.is_request_batch()
    assert len(batch) == len(requests)
    assert list(batch.requests()) == [requests[0], requests[2]]
    # test iter()
    assert requests == list(batch)
    assert batch.request_ids() == {1, 0}
    assert isinstance(batch.request_ids(), frozenset)
    assert repr(batch) == ("RPCBatch([RPCRequest('method', [], 0), "
                           "RPCRequest('foo', [], None), "
                           "RPCRequest('t', [1], 1)])")

    responses = [RPCResponse(6, 2)]
    batch = RPCBatch(responses)
    assert not batch.is_request_batch()
    assert len(batch) == len(responses)
    assert list(batch.requests()) == responses
    # test iter()
    assert responses == list(batch)
    assert batch.request_ids() == {2}
    assert repr(batch) == "RPCBatch([RPCResponse(6, 2)])"


def test_RPCHelperBase():

    class MyHelper(RPCHelperBase):
        pass

    helper = MyHelper()
    with pytest.raises(NotImplementedError):
        helper.send_message(b'')
    with pytest.raises(NotImplementedError):
        helper.create_task(None)
    with pytest.raises(NotImplementedError):
        helper.semaphore()
    assert helper.notification_handler('n') is None
    assert helper.request_handler('n') is None


def test_RPCBatchOut():
    with MyRPCProcessor() as rpc:
        batch = RPCBatchOut()
        assert isinstance(batch, RPCBatch)
        with pytest.raises(RuntimeError):
            rpc.send_batch(batch)

        batch.add_notification("method")
        batch.add_request("method")
        batch.add_request("method", [])
        batch.add_notification("method", [])
        batch.add_request("method", {})
        batch.add_notification("method", [])

        request_ids = batch.request_ids()
        assert len(batch) == 6
        assert len(request_ids) == 3
        low = min(request_ids)
        high = low + len(request_ids)
        assert request_ids == frozenset(range(low, high))
        # Simple send works
        rpc.send_batch(batch)
        assert request_ids in rpc.requests


def test_RPCProtocolBase():
    x = RPCProtocolBase()
    with pytest.raises(NotImplementedError):
        x.request_message(None)
    with pytest.raises(NotImplementedError):
        x.response_message(None)
    with pytest.raises(NotImplementedError):
        x.batch_message(None)
    with pytest.raises(NotImplementedError):
        x.batch_message_from_parts(None)
    with pytest.raises(NotImplementedError):
        x.error_message(None)


# RPC processor tests


class MyRPCProcessor(RPCProcessor):

    def __init__(self, protocol=None):
        protocol = protocol or JSONRPCv2
        self.loop = asyncio.get_event_loop()
        super().__init__(protocol, self)
        self.logger = self
        self.responses = deque()
        self.debug_messages = []
        self.debug_message_count = 0
        self.error_messages = []
        self.error_message_count = 0
        self.task_set = TaskSet()
        # Ensure our operation causes no unwanted errors
        alogger = logging.getLogger('asyncio')
        alogger.warning = self.asyncio_log
        alogger.error = self.asyncio_log
        self.asyncio_logs = []
        self.max_response_size = 500
        self._semaphore = asyncio.Semaphore(10)

    # This is just to verify things are as expected
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        assert not self.asyncio_logs

    def create_task(self, coro):
        return self.task_set.create_task(coro)

    def semaphore(self):
        return self._semaphore

    # RPCHelper methods
    def send_message(self, message):
        self.responses.append(message)

    def notification_handler(self, method):
        if method == 'bad_notification':
            made_bad_notification
        return getattr(self, f'on_{method}', None)

    def request_handler(self, method):
        if method == 'bad_request':
            made_bad_request
        if method == 'partial_add_async':
            return partial(self.on_add_async, 100)
        return getattr(self, f'on_{method}', None)

    # RPCHelper methods -- end

    def process_all(self):
        tasks = self.task_set.tasks
        if tasks:
            self.loop.run_until_complete(asyncio.wait(tasks))
        else:
            self.loop.run_until_complete(asyncio.sleep(0))

    def yield_to_loop(self):
        run_briefly(self.loop)

    def expect_asyncio_log(self, text):
        return ExpectAsyncioLog(self, text)

    def consume_one_response(self):
        if not self.responses:
            self.yield_to_loop()
        return self.responses.popleft()

    def consume_responses(self):
        result = [response for response in self.responses if response]
        self.responses.clear()
        return result

    def all_done(self):
        return not self.task_set.tasks and not self.responses

    def expect_error(self, code, text, request_id):
        message = self.consume_one_response()
        assert message
        item = self.protocol.message_to_item(message)
        assert isinstance(item, RPCResponse)
        assert item.request_id == request_id
        item = item.result
        assert isinstance(item, RPCError)
        assert item.code == code
        assert text in item.message
        assert item.request_id == request_id
        self.expect_debug_message(text)

    def expect_debug_message(self, text):
        assert any(text in message for message in self.debug_messages)

    def expect_internal_error(self, text, request_id):
        message = self.consume_one_response()
        assert message
        item = self.protocol.message_to_item(message)
        assert isinstance(item, RPCResponse)
        assert item.request_id == request_id
        item = item.result
        assert isinstance(item, RPCError)
        assert item.code == self.protocol.INTERNAL_ERROR
        assert 'internal' in item.message
        assert item.request_id == request_id
        self.expect_error_message(text)

    def expect_error_message(self, text):
        assert any(text in message for message in self.error_messages)

    def expect_response(self, result, request_id):
        self.process_all()
        message = self.consume_one_response()
        response = RPCResponse(result, request_id)
        assert message == self.protocol.response_message(response)

    def expect_nothing(self):
        with pytest.raises(IndexError):
            self.consume_one_response()

    def add_batch(self, batch):
        self.message_received(self.protocol.batch_message(batch))

    def add_request(self, request):
        self.message_received(self.protocol.request_message(request))

    def add_requests(self, requests):
        for request in requests:
            self.add_request(request)

    def message_receiveds(self, messages):
        for message in messages:
            self.message_received(message)

    def asyncio_log(self, message, *args, **kwargs):
        print(message, args, kwargs)
        self.asyncio_logs.append(message)

    def error(self, message, *args, **kwargs):
        logging.error(message, *args, **kwargs)
        raise LogError

    def debug(self, message, *args, **kwargs):
        # This is to test log_debug is being called
        self.debug_messages.append(message)
        self.debug_messages.extend(args)
        self.debug_message_count += 1
        logging.debug(message, *args, **kwargs)

    def debug_clear(self):
        self.debug_messages.clear()
        self.debug_message_count = 0

    def exception(self, message, *args, **kwargs):
        self.error_messages.append(message)
        self.error_messages.extend(args)
        self.error_messages.append(traceback.format_exc())
        self.error_message_count += 1
        logging.exception(message, *args, **kwargs)

    def on_echo(self, arg):
        return arg

    def on_notify(self, x, y, z=0):
        return x + y + z

    def on_raise(self):
        return something

    async def on_raise_async(self):
        return anything

    async def on_sleep(self):
        await asyncio.sleep(1)

    def on_add(self, x, y=4, z=2):
        values = (x, y, z)
        if any(not isinstance(value, numbers.Number) for value in values):
            raise RPCError(-1, 'all values must be numbers')
        return sum(values)

    async def on_add_async(self, x, y=4, z=2):
        return self.on_add(x, y, z)

    # Special function signatures
    def on_add_many(self, first, second=0, *values):
        values += (first, second)
        if any(not isinstance(value, numbers.Number) for value in values):
            raise RPCError(-1, 'all values must be numbers')
        return sum(values)

    # Built-in; 2 positional args, 1 optional 3rd named arg
    on_pow = pow

    def on_echo_2(self, first, *, second=2):
        return [first, second]

    def on_kwargs(self, start, *kwargs):
        return start + len(kwargs)

    def on_both(self, start=2, *args, **kwargs):
        return start + len(args) * 10 + len(kwargs) * 4


def test_basic():
    with MyRPCProcessor() as rpc:
        # With no messages added, there is nothing to do
        assert rpc.all_done()
        assert rpc.recv_count == 0
        assert rpc.sent_count == 0
        assert rpc.errors == 0
        assert rpc.internal_errors == 0

# INCOMING REQUESTS


def test_unknown_method():
    with MyRPCProcessor() as rpc:
        # Test unknown method, for both notification and request
        rpc.add_request(RPCRequest('unk1', ["a"], 5))
        rpc.expect_error(rpc.protocol.METHOD_NOT_FOUND, "unk1", 5)
        assert rpc.errors == 1
        rpc.add_request(RPCRequest('unk2', ["a"], None))
        rpc.expect_nothing()
        assert rpc.errors == 2
        assert rpc.recv_count == 2
        assert rpc.all_done()


def test_too_many_or_few_array_args():
    with MyRPCProcessor() as rpc:
        # Test too many args, both notification and request
        rpc.add_requests([
            RPCRequest('add', [], 0),
            RPCRequest('add', [], None),
            RPCRequest('add', [1, 2, 3, 4], 0),
            RPCRequest('add', [1, 2, 3, 4], None),
            RPCRequest('add_async', [], 0),
            RPCRequest('add_async', [], None),
            RPCRequest('add_async', [1, 2, 3, 4], 0),
            RPCRequest('add_async', [1, 2, 3, 4], None),
        ])
        rpc.expect_error(rpc.protocol.INVALID_ARGS, "0 arguments", 0)
        rpc.expect_error(rpc.protocol.INVALID_ARGS, "4 arguments", 0)
        rpc.expect_error(rpc.protocol.INVALID_ARGS, "0 arguments", 0)
        rpc.expect_error(rpc.protocol.INVALID_ARGS, "4 arguments", 0)
        assert rpc.all_done()


def test_good_args():
    with MyRPCProcessor() as rpc:
        # Test 2, 1 and no default args
        rpc.add_requests([
            RPCRequest('add', [1], 0),
            RPCRequest('add', [1, 2], 0),
            RPCRequest('add', [1, 2, 3], 0),
            RPCRequest('add', [1], None),
            RPCRequest('add_async', [1], 0),
            RPCRequest('add_async', [1, 2], 0),
            RPCRequest('add_async', [1, 2, 3], 0),
            RPCRequest('add_async', [1], None),
        ])
        rpc.expect_response(7, 0)
        rpc.expect_response(5, 0)
        rpc.expect_response(6, 0)

        # Order may not be reliable here...
        rpc.expect_response(7, 0)
        rpc.expect_response(5, 0)
        rpc.expect_response(6, 0)
        assert rpc.all_done()


def test_named_args_good():
    with MyRPCProcessor() as rpc:
        # Test 2, 1 and no default args
        rpc.add_requests([
            RPCRequest('add', {"x": 1}, 0),
            RPCRequest('add', {"x": 1, "y": 2}, 0),
            RPCRequest('add', {"x": 1, "y": 2, "z": 3}, 0),
            RPCRequest('add', {"x": 1, "z": 8}, 0),
            RPCRequest('add', {"x": 1}, None),
            RPCRequest('add_async', {"x": 1}, 0),
            RPCRequest('add_async', {"x": 1, "y": 2}, 0),
            RPCRequest('add_async', {"x": 1, "y": 2, "z": 3}, "a"),
            RPCRequest('add_async', {"x": 1, "z": 8}, 0),
            RPCRequest('add_async', {"x": 1}, None),
        ])
        rpc.yield_to_loop()
        assert rpc.recv_count == 10

        rpc.expect_response(7, 0)
        rpc.expect_response(5, 0)
        rpc.expect_response(6, 0)
        rpc.expect_response(13, 0)

        # Order may not be reliable here...
        rpc.expect_response(7, 0)
        rpc.expect_response(5, 0)
        rpc.expect_response(6, "a")
        rpc.expect_response(13, 0)
        assert rpc.all_done()


def test_named_args_bad():
    with MyRPCProcessor() as rpc:
        # Test 2, 1 and no default args
        for method in ('add', 'add_async'):
            rpc.add_requests([
                # Bad names
                RPCRequest(method, {"x": 1, "t": 1, "u": 3}, 0),
                RPCRequest(method, {"x": 1, "t": 2}, 0),
                RPCRequest(method, {"x": 1, "t": 2}, None),
                # x is required
                RPCRequest(method, {}, 0),
                RPCRequest(method, {"y": 3}, 0),
                RPCRequest(method, {"y": 3, "z": 4}, 0),
                RPCRequest(method, {"y": 3}, None),
            ])

        for method in ('add', 'add_async'):
            rpc.expect_error(rpc.protocol.INVALID_ARGS,
                             'parameters "t", "u"', 0)
            rpc.expect_error(rpc.protocol.INVALID_ARGS, 'parameter "t"', 0)
            rpc.expect_error(rpc.protocol.INVALID_ARGS, 'parameter "x"', 0)
            rpc.expect_error(rpc.protocol.INVALID_ARGS, 'parameter "x"', 0)
            rpc.expect_error(rpc.protocol.INVALID_ARGS, 'parameter "x"', 0)
        assert rpc.all_done()

        # Test plural
        rpc.add_request(RPCRequest('notify', {}, 0))
        rpc.expect_error(rpc.protocol.INVALID_ARGS, 'parameters "x", "y"', 0)
        assert rpc.all_done()


def test_bad_handler_lookup():
    with MyRPCProcessor() as rpc:
        rpc.add_requests([
            RPCRequest('bad_request', [], 0),
            RPCRequest('bad_notification', [], None),
        ])

        rpc.expect_internal_error("made_bad_request", 0)
        rpc.expect_error_message("made_bad_notification")
        assert rpc.internal_errors == 2
        assert rpc.errors == 0
        assert rpc.all_done()


def test_partial_async():
    with MyRPCProcessor() as rpc:
        rpc.add_requests([
            RPCRequest('partial_add_async', [10, 15], 3),
        ])

        rpc.expect_response(125, 3)
        assert rpc.all_done()


def test_erroneous_request():
    with MyRPCProcessor() as rpc:
        rpc.message_receiveds([
            b'\xff',
            b'{"req',
            b'{"method": 2, "id": 1, "jsonrpc": "2.0"}',
        ])

        rpc.expect_error(rpc.protocol.PARSE_ERROR, 'decode', None)
        rpc.expect_error(rpc.protocol.PARSE_ERROR, 'JSON', None)
        rpc.expect_error(rpc.protocol.METHOD_NOT_FOUND, 'string', 1)
        assert rpc.all_done()
        assert rpc.errors == 3


def test_request_round_trip():
    '''Round trip test - we send binary requests to ourselves, process the
    requests, send binary responses in response also to ourselves, and then
    process the results.

    This tests request and response serialization, and also that request
    handlers are invoked when a response is received.

    The tests cover a range of valid and invalid requests, and problem
    triggers.

    We also insert a fake duplicate response, and a fake response without
    a request ID, to test they are appropriately handled.
    '''
    handled = []

    def handle_add(request):
        assert request.method == 'add'
        if request.args[1] == "a":
            result = request.exception()
            assert isinstance(result, RPCError)
            assert result.code == -1
            assert "numbers" in result.message
            handled.append('add_bad')
        else:
            result = request.result()
            assert request.args == [1, 5, 10]
            assert result == 16
            handled.append(request.method)

    def handle_add_async(request):
        assert request.method == 'add_async'
        if request.args[0] == "b":
            result = request.exception()
            assert isinstance(result, RPCError)
            assert result.code == -1
            assert "numbers" in result.message
            handled.append('add_async_bad')
        else:
            result = request.result()
            assert request.args == [1, 5, 10]
            assert result == 16
            handled.append(request.method)

    def handle_echo(request):
        assert request.method == 'echo'
        assert request.args[0] == request.result()
        handled.append(request.method)

    def handle_bad_echo(request):
        assert request.method == 'echo'
        assert not request.args
        result = request.exception()
        assert isinstance(result, RPCError)
        assert result.code == rpc.protocol.INVALID_ARGS
        handled.append('bad_echo')

    def bad_request_handler(request):
        assert request.method == 'bad_request'
        result = request.exception()
        assert isinstance(result, RPCError)
        assert result.code == rpc.protocol.INTERNAL_ERROR
        handled.append(request.method)

    null_handler_request = RPCRequestOut('echo', [2], None)
    requests = [
        RPCRequestOut('add', [1, 5, 10], handle_add),
        # Bad type
        RPCRequestOut('add', [1, "a", 10], handle_add),
        # Test a notification, and a bad one
        RPCRequestOut('echo', ["ping"], handle_echo),
        RPCRequest('echo', [], None),
        # Test a None response
        RPCRequestOut('echo', [None], handle_echo),
        # Throw in an async request
        RPCRequestOut('add_async', [1, 5, 10], handle_add_async),
        RPCRequestOut('add_async', ["b"], handle_add_async),
        # An invalid request
        RPCRequestOut('echo', [], handle_bad_echo),
        # test a null handler
        null_handler_request,
        # test a remote bad request getter
        RPCRequestOut('bad_request', [], bad_request_handler),
    ]

    with MyRPCProcessor() as rpc:
        # Send each request and feed them back into the RPC object as if
        # it were receiving its own messages.
        for request in requests:
            # Send it and fake receiving it
            rpc.send_request(request)
            rpc.message_received(rpc.responses.pop())
        assert rpc.sent_count == len(requests)

        # Check all_requests
        assert isinstance(rpc.all_requests(), list)
        assert set(rpc.all_requests()) == set(
            req for req in requests if isinstance(req, RPCRequestOut))

        # Now process the queue and the async jobs, generating queued responses
        rpc.process_all()

        # Get the queued responses and send them back to ourselves
        responses = rpc.consume_responses()
        assert rpc.all_done()

        # Did we did get the null handler response - no other way to detect it
        text = f'"id": {null_handler_request.request_id}'.encode()
        assert any(text in response for response in responses)

        for response in responses:
            rpc.message_received(response)
        rpc.yield_to_loop()

        # Nothing left
        assert not rpc.all_requests()

        assert sorted(handled) == [
            'add', 'add_async', 'add_async_bad', 'add_bad',
            'bad_echo', 'bad_request', 'echo', 'echo']

        # Responses are handled synchronously so no process_all() is needed
        assert rpc.all_done()


def test_bad_reponses():
    def handle_add(request):
        assert request.method == 'add'
        assert request.args == [1, 5, 10]
        assert request.result() == 16
        handled.append(request.method)

    handled = []
    requests = [
        RPCRequestOut('add', [1, 5, 10], handle_add),
    ]

    with MyRPCProcessor() as rpc:
        # Send each request and feed them back into the RPC object as if
        # it were receiving its own messages.
        for request in requests:
            # Send it and fake receiving it
            rpc.send_request(request)
            rpc.message_received(rpc.responses.pop())

        # Now process the queue and the async jobs, generating queued responses
        rpc.process_all()

        # Get the queued responses and send them back to ourselves
        response, = rpc.consume_responses()
        assert rpc.all_done()

        # Send response twice.
        rpc.message_received(response)
        rpc.yield_to_loop()
        assert handled == ['add']
        assert not rpc.debug_messages
        rpc.message_received(response)
        # Handler NOT called twice
        assert handled == ['add']
        rpc.expect_debug_message('response to unsent')
        rpc.debug_clear()
        # Send a response with no ID
        rpc.message_received(rpc.protocol.
                             response_message(RPCResponse(6, None)))
        rpc.expect_debug_message('missing id')

        # Responses are handled synchronously so no process_all() is needed
        assert rpc.all_done()


def test_batch_round_trip():
    def handle_add(request):
        assert request.method in ('add', 'add_async')
        assert request.args == [1, 5, 10]
        assert request.result() == 16
        handled.append(request.method)

    def handle_echo(request):
        assert request.method == 'echo'
        assert request.result() == request.args[0]
        handled.append(request.method)

    def handle_bad_echo(request):
        assert request.method == 'echo'
        assert not request.args
        result = request.exception()
        assert isinstance(result, RPCError)
        assert result.code == rpc.protocol.INVALID_ARGS
        handled.append('bad_echo')

    with MyRPCProcessor() as rpc:
        handled = []
        batch_message = None

        batch = RPCBatchOut()
        batch.add_request('add', [1, 5, 10], handle_add)
        batch.add_request('add_async', [1, 5, 10], handle_add)
        batch.add_request('echo', [], handle_bad_echo)   # An erroneous request
        batch.add_notification('add')   # Erroneous; gets swallowed anyway
        batch.add_request('echo', ["ping"], handle_echo)
        rpc.send_batch(batch)
        assert rpc.sent_count == len(batch)
        batch_message = rpc.responses.pop()

        assert not rpc.debug_messages
        assert not rpc.error_messages
        # Fake receiving it.  This processes the request and sees invalid
        # requests, and creates jobs to process the valid requests
        rpc.message_received(batch_message)
        assert not rpc.all_done()

        # Now process the request jobs, generating queued response messages
        rpc.process_all()
        assert rpc.debug_message_count == 2  # Both notification and request
        assert not rpc.error_messages
        rpc.debug_clear()

        # Get the batch response and send it back to ourselves
        response, = rpc.consume_responses()
        assert rpc.all_done()

        # Process the batch response
        rpc.message_received(response)
        rpc.yield_to_loop()
        assert rpc.all_done()

        assert sorted(handled) == ['add', 'add_async', 'bad_echo',
                                   'echo']

        assert rpc.error_message_count == 0
        assert rpc.debug_message_count == 1  # Only request


def test_some_invalid_requests():
    with MyRPCProcessor() as rpc:
        # First message is good.  2nd misses "jsonrpc" and also has a
        # non-string method.
        batch_message = (
            b'[{"jsonrpc": "2.0", "method": "add", "params": [1]}, '
            b'{"jsonrpc": "2.0", "method": 2}]'
        )
        rpc.message_received(batch_message)

        # Now process the request jobs, generating queued response messages
        rpc.process_all()
        assert rpc.recv_count == 2

        # Test a single non-empty response was created for the invalid method
        response, = rpc.consume_responses()
        assert rpc.all_done()


def test_all_notification_batch():
    with MyRPCProcessor() as rpc:
        batch = RPCBatchOut()
        batch.add_notification('echo', ["ping"])
        batch.add_notification('add')   # Erroneous; gets swallowed anyway
        rpc.send_batch(batch)
        # Fake reeiving it
        rpc.message_received(rpc.responses.pop())

        # Now process the request jobs, generating queued response messages
        rpc.process_all()

        # There is no response!
        assert rpc.all_done()


def test_batch_response_bad():
    handled = []

    def handler(request):
        handled.append(request.method)

    with MyRPCProcessor() as rpc:
        def send_batch():
            batch = RPCBatchOut()
            batch.add_request('add', [1, 5, 10], handler)
            batch.add_request('echo', ["ping"], handler)
            rpc.send_batch(batch)
            return batch

        batch = send_batch()

        # Fake receiving it
        rpc.message_received(rpc.responses.pop())

        # Now process the request jobs, generating queued response messages
        rpc.process_all()
        assert not rpc.debug_messages
        assert not rpc.error_messages

        # Before sending a good response, send a batch response with a good ID
        # and a bad ID
        responses = [
            RPCResponse(5, -1),
            RPCResponse(6, list(batch.request_ids())[0])
        ]
        parts = [rpc.protocol.response_message(response)
                 for response in responses]
        rpc.message_received(rpc.protocol.batch_message_from_parts(parts))

        rpc.expect_debug_message('unsent or forced batch request')
        assert not rpc.error_messages
        assert not handled
        rpc.debug_clear()

        # Get the batch response and send it back to ourselves
        response, = rpc.consume_responses()
        assert rpc.all_done()
        rpc.message_received(response)
        rpc.yield_to_loop()
        assert sorted(handled) == ['add', 'echo']

        # Send it again, check no handlers are called and it's logged
        rpc.debug_clear()
        rpc.message_received(response)
        rpc.yield_to_loop()
        assert sorted(handled) == ['add', 'echo']
        rpc.expect_debug_message('unsent or forced batch request')

        # Now send the batch again.  Create a response with the correct IDs
        # but an additional response with a None id.
        handled.clear()
        rpc.debug_clear()
        batch = send_batch()
        rpc.responses.pop()
        assert rpc.all_done()

        responses = [RPCResponse(5, request_id)
                     for request_id in batch.request_ids()]
        responses.insert(1, RPCResponse(5, None))
        parts = [rpc.protocol.response_message(response)
                 for response in responses]
        rpc.message_received(rpc.protocol.batch_message_from_parts(parts))
        rpc.yield_to_loop()

        # Check (only) the None id was logged.  Check the 2 good respones
        # were handled as expected.
        assert not rpc.error_messages
        assert rpc.debug_message_count == 1
        rpc.expect_debug_message('batch response missing id')
        assert sorted(handled) == ['add', 'echo']
        assert rpc.all_done()


def test_outgoing_request_cancellation_and_setting():
    '''Tests cancelling requests.'''
    called = 0

    def on_done(req):
        nonlocal called
        called += 1

    with MyRPCProcessor() as rpc:
        request = RPCRequestOut('add_async', [1], on_done)
        rpc.send_request(request)
        assert request.request_id in rpc.requests
        request.cancel()
        rpc.yield_to_loop()
        assert called == 1
        assert request.cancelled()
        assert not rpc.requests
        with pytest.raises(asyncio.CancelledError):
            request.result()

        request = RPCRequestOut('add_async', [1], on_done)
        rpc.send_request(request)
        assert request.request_id in rpc.requests
        request.set_result(4)
        rpc.yield_to_loop()
        assert called == 2
        assert request.result() == 4
        assert not rpc.requests

        request = RPCRequestOut('add_async', [1], on_done)
        rpc.send_request(request)
        assert request.request_id in rpc.requests
        request.set_exception(ValueError())
        rpc.yield_to_loop()
        assert called == 3
        assert isinstance(request.exception(), ValueError)
        with pytest.raises(ValueError):
            request.result()
        assert not rpc.requests


def test_outgoing_batch_request_cancellation_and_setting():
    '''Tests cancelling outgoing batch requests.'''
    batch_done = request_done = 0

    def on_batch_done(batch):
        assert isinstance(batch, RPCBatchOut)
        assert batch.done()
        nonlocal batch_done
        batch_done += 1

    def on_request_done(request):
        assert isinstance(request, RPCRequestOut)
        assert request.done()
        nonlocal request_done
        request_done += 1

    with MyRPCProcessor() as rpc:
        def send_batch():
            nonlocal batch_done, request_done
            batch_done = request_done = 0
            batch = RPCBatchOut()
            batch.add_request('add_async', [1], on_request_done)
            batch.add_request('add_async', [1], on_request_done)
            batch.add_notification('add_async', [])
            batch.add_request('add_async', [1], None)
            rpc.send_batch(batch, on_batch_done)
            return batch

        # First, cancel the bzatch
        batch = send_batch()
        batch.cancel()
        rpc.yield_to_loop()
        assert batch.cancelled()
        for request in batch.requests():
            assert request.cancelled()
        assert batch_done == 1
        assert request_done == 2
        assert not rpc.requests

        # Now set its result
        batch = send_batch()
        batch.set_result(1)
        rpc.yield_to_loop()
        assert batch.result() == 1
        for request in batch.requests():
            assert request.cancelled()
        assert batch_done == 1
        assert request_done == 2
        assert not rpc.requests

        # Now set its exception
        batch = send_batch()
        batch.set_exception(ValueError())
        rpc.yield_to_loop()
        with pytest.raises(ValueError):
            batch.result()
        for request in batch.requests():
            assert request.cancelled()
        assert batch_done == 1
        assert request_done == 2
        assert not rpc.requests

        # Now set one before cancelling
        batch = send_batch()
        batch.items[0].set_result(1)
        batch.cancel()
        rpc.yield_to_loop()
        assert batch.cancelled()
        assert batch.items[0].result() == 1
        assert batch.items[1].cancelled()
        assert batch_done == 1
        assert request_done == 2
        assert not rpc.requests

        # Now cancel all manually; check the batch is also flagged done
        batch = send_batch()
        for request in batch.requests():
            request.set_result(0)
        rpc.yield_to_loop()
        assert batch.done() and not batch.cancelled()
        assert len(list(batch.requests())) == 3
        for request in batch.requests():
            assert request.result() == 0
        assert batch_done == 1
        assert request_done == 2
        assert batch.result() is False
        assert not rpc.requests

        # Now send a notification batch.  Assert it is flagged done
        # automatically
        batch = RPCBatchOut()
        batch.add_notification('add_async', [1])
        rpc.send_batch(batch, on_batch_done)
        assert not rpc.requests
        assert batch.done()


def test_incoming_request_cancellation():
    '''Tests cancelling async requests.'''
    with MyRPCProcessor() as rpc:
        # Simple request.
        rpc.add_request(RPCRequest('sleep', None, 0))
        rpc.yield_to_loop()
        rpc.task_set.cancel_all()
        rpc.yield_to_loop()
        assert not rpc.responses
        assert rpc.all_done()


def test_incoming_batch_request_cancellation():
    '''Test cancelling incoming batch cancellation, including when
    partially complete.'''
    with MyRPCProcessor() as rpc:
        rpc.add_batch(RPCBatch([
            RPCRequest('add_async', [1], 0),
            RPCRequest('sleep', None, 1),
        ]))

        rpc.yield_to_loop()
        assert len(rpc.task_set.tasks) == 1
        rpc.task_set.cancel_all()
        rpc.yield_to_loop()
        assert not rpc.responses
        assert rpc.all_done()


def test_direct_set_etc():
    loop = asyncio.get_event_loop()

    done = set()

    def on_done(request):
        done.add(request.request_id)

    # Send 4 requests
    requests = [
        RPCRequestOut('some_call', [1], on_done),
        RPCRequestOut('some_call', [2], on_done),
        RPCRequestOut('some_call', [3], on_done),
        RPCRequestOut('some_call', [4], on_done),
    ]

    with MyRPCProcessor() as rpc:
        for request in requests:
            rpc.send_request(request)

        # Interfere with all apart from one
        requests[0].set_result(6)
        response = RPCResponse(7, requests[1].request_id)
        rpc.message_received(rpc.protocol.response_message(response))
        requests[2].cancel()
        requests[3].set_exception(ValueError())
        rpc.yield_to_loop()   # Process done functions
        assert done == {requests[n].request_id for n in range(len(requests))}
        assert not rpc.requests


def test_odd_calls():
    handled = []

    def expect(answer, request):
        result = request.result()
        if result == answer:
            handled.append(request.method)

    def error(text, request):
        result = request.exception()
        if isinstance(result, RPCError) and text in result.message:
            handled.append(text)

    requests = [
        # Gives code coverage of notify func
        RPCRequestOut('notify', [1, 2, 3], partial(expect, 6)),
        RPCRequestOut('add_many', [1, "b"], partial(error, 'numbers')),
        RPCRequestOut('add_many', [], partial(error, 'requires 1')),
        RPCRequestOut('add_many', [1], partial(expect, 1)),
        RPCRequestOut('add_many', [5, 50, 500], partial(expect, 555)),
        RPCRequestOut('add_many', list(range(10)), partial(expect, 45)),
        RPCRequestOut('add_many', {'first': 1}, partial(expect, 1)),
        RPCRequestOut('add_many', {'first': 1, 'second': 10},
                      partial(expect, 11)),
        RPCRequestOut('add_many', {'first': 1, 'values': []},
                      partial(error, 'values')),
        RPCRequestOut('pow', [2, 3], partial(expect, 8)),
        RPCRequestOut('pow', [2, 3, 5], partial(expect, 3)),
        RPCRequestOut('pow', {"x": 2, "y": 3},
                      partial(error, 'cannot be called')),
        RPCRequestOut('echo_2', ['ping'],
                      partial(expect, ['ping', 2])),
        RPCRequestOut('echo_2', ['ping', 'pong'],
                      partial(error, 'at most 1')),
        RPCRequestOut('echo_2', {'first': 1, 'second': 8},
                      partial(expect, [1, 8])),
        RPCRequestOut('echo_2', {'first': 1, 'second': 8, '3rd': 1},
                      partial(error, '3rd')),
        RPCRequestOut('kwargs', [],
                      partial(error, 'requires 1')),
        RPCRequestOut('kwargs', [1],
                      partial(expect, 1)),
        RPCRequestOut('kwargs', [1, 2], partial(expect, 2)),
        RPCRequestOut('kwargs', {'end': 4}, partial(error, "start")),
        RPCRequestOut('kwargs', {'start': 3}, partial(expect, 3)),
        RPCRequestOut('kwargs', {'start': 3, 'end': 1, '3rd': 1},
                      partial(error, '3rd')),
        RPCRequestOut('both', [], partial(expect, 2)),
        RPCRequestOut('both', [1], partial(expect, 1)),
        RPCRequestOut('both', [5, 2], partial(expect, 15)),
        RPCRequestOut('both', {'end': 4}, partial(expect, 6)),
        RPCRequestOut('both', {'start': 3}, partial(expect, 3)),
        RPCRequestOut('both', {'start': 3, 'end': 1, '3rd': 1},
                      partial(expect, 11)),
    ]

    with MyRPCProcessor() as rpc:
        # Send each request and feed them back into the RPC object as if
        # it were receiving its own messages.
        for request in requests:
            rpc.send_request(request)
            rpc.message_received(rpc.responses.pop())

        # Now process the queue and the async jobs, generating queued responses
        rpc.process_all()

        # Get the queued responses and send them back to ourselves
        responses = rpc.consume_responses()
        assert rpc.all_done()

        for response in responses:
            rpc.message_received(response)

        rpc.yield_to_loop()
        assert len(handled) == len(requests)

        assert rpc.all_done()


def test_buggy_done_handler_is_logged():
    def on_done(req):
        undefined_var

    with MyRPCProcessor() as rpc:
        # First for a request
        request = RPCRequestOut('add_async', [1], on_done)
        rpc.send_request(request)
        request.cancel()
        with rpc.expect_asyncio_log('Exception in callback'):
            rpc.yield_to_loop()

    with MyRPCProcessor() as rpc:
        # Now a request in a batch
        batch = RPCBatchOut()
        batch.add_request('add_async', [2], on_done)
        rpc.send_batch(batch)
        batch.items[0].cancel()
        with rpc.expect_asyncio_log('Exception in callback'):
            rpc.yield_to_loop()

    with MyRPCProcessor() as rpc:
        # Finally the batch itself
        batch = RPCBatchOut()
        batch.add_request('add_async', [1])
        rpc.send_batch(batch, on_done)
        batch.cancel()
        with rpc.expect_asyncio_log('Exception in callback'):
            rpc.yield_to_loop()


def test_buggy_request_handler_is_logged():
    with MyRPCProcessor() as rpc:
        rpc.add_requests([
            RPCRequest('raise', [], 0),
            RPCRequest('raise', [], None),
        ])

        rpc.process_all()
        rpc.expect_internal_error("something", 0)
        assert rpc.error_message_count == 2
        assert rpc.all_done()

        rpc.add_request(RPCRequest('raise_async', [], 0))
        rpc.error_messages.clear()
        rpc.error_message_count = 0
        rpc.process_all()
        rpc.expect_internal_error("anything", 0)
        assert rpc.error_message_count == 1
        assert rpc.all_done()

        rpc.add_request(RPCRequest('raise_async', [], None))
        rpc.error_messages.clear()
        rpc.error_message_count = 0
        rpc.process_all()
        assert not rpc.responses
        assert rpc.error_message_count == 1
        rpc.expect_error_message("anything")
        assert rpc.all_done()


def test_protocol_autodetection_v1():
    with MyRPCProcessor() as rpc:
        rpc.protocol = JSONRPCAutoDetect
        rpc.message_received(b'{"error": "foo", "result": null, "id": 0}')
        assert rpc.protocol == JSONRPCv1


def test_protocol_autodetection_v2():
    with MyRPCProcessor() as rpc:
        rpc.protocol = JSONRPCAutoDetect
        rpc.message_received(b'{"jsonrpc": "2.0", "method": "m", "id" :0}')
        assert rpc.protocol == JSONRPCv2


def test_max_response_size():
    # Test max_response_size is observed
    with MyRPCProcessor() as rpc:
        rpc.add_request(RPCRequest('echo', ['a' * rpc.max_response_size], 0))
        rpc.expect_error(rpc.protocol.INVALID_REQUEST, "response too large", 0)
        assert rpc.errors == 1

    # Test 0 means no limit
    with MyRPCProcessor() as rpc:
        rpc.max_response_size = 0
        rpc.add_request(RPCRequest('echo', ['a' * 5000000], 0))
        rpc.consume_one_response()
        assert rpc.errors == 0


def test_max_response_size_for_batch():
    with MyRPCProcessor() as rpc:
        rpc.add_batch(RPCBatch([
            RPCRequest('echo', ["foo"], 0),
            RPCRequest('echo', ['a' * rpc.max_response_size], 1),
            RPCRequest('sleep', None, 2),
        ]))
        tasks = list(rpc.task_set.tasks)
        rpc.expect_error(rpc.protocol.INVALID_REQUEST, "response too large",
                         None)
        rpc.yield_to_loop()  # Allow the sleep to cancel
        assert sum(task.cancelled() for task in tasks) == 1
