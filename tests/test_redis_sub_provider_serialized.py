"""
SerializedSockJSSubscriber: only the first subscribe may open the Redis
connection; the rest wait for it. Uses a fake tornadoredis client so no Redis
is needed. The fake records subscribe calls and lets the test complete them
when it chooses, which is the window the real race lives in.

Plain unittest, no Django: run with
    python -m pytest --noconftest tests/test_redis_sub_provider_serialized.py
"""
import unittest

from tornado.ioloop import IOLoop

from swampdragon.pubsub_providers.redis_sub_provider import SerializedSockJSSubscriber


class FakeRedis(object):
    def __init__(self):
        self.subscribed = set()
        self.calls = []        # (channel, callback) in the order SUBSCRIBE was sent
        self.listen_calls = 0
        self._io_loop = IOLoop.current()

    def subscribe(self, channel, callback=None):
        self.calls.append((channel, callback))

    def unsubscribe(self, channel, callback=None):
        self.subscribed.discard(channel)

    def listen(self, callback):
        self.listen_calls += 1

    def complete(self, index):
        """ Act like tornadoredis once SUBSCRIBE #index has gone out: mark the channel subscribed, then call back. """
        channel, callback = self.calls[index]
        self.subscribed.add(channel)
        if callback:
            callback(True)


class FakeSession(object):
    is_closed = False


class FakeConnection(object):
    def __init__(self):
        self.session = FakeSession()


class SerializedSubscribeTest(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.sub = SerializedSockJSSubscriber(self.redis)
        self.a = FakeConnection()
        self.b = FakeConnection()

    def test_second_subscribe_waits_for_the_first_connection(self):
        self.sub.subscribe('chan1', self.a)
        self.sub.subscribe('chan2', self.b)

        # only the first SUBSCRIBE is out; chan2 is queued, not sent
        self.assertEqual([c for c, _ in self.redis.calls], ['chan1'])
        self.assertEqual(self.redis.listen_calls, 0)

        self.redis.complete(0)

        # one listen loop, and the queued chan2 was sent after chan1 completed
        self.assertEqual(self.redis.listen_calls, 1)
        self.assertEqual([c for c, _ in self.redis.calls], ['chan1', 'chan2'])
        self.assertIn(self.b, self.sub.subscribers['chan2'])

        # once connected, the base class is asked to listen only the first time
        self.redis.complete(1)
        self.assertEqual(self.redis.listen_calls, 1)

    def test_same_channel_queued_twice_subscribes_once(self):
        self.sub.subscribe('chan1', self.a)
        self.sub.subscribe('chan1', self.b)
        self.redis.complete(0)

        self.assertEqual([c for c, _ in self.redis.calls], ['chan1'])
        self.assertEqual(self.sub.subscriber_count['chan1'], 2)
        self.assertIn(self.a, self.sub.subscribers['chan1'])
        self.assertIn(self.b, self.sub.subscribers['chan1'])

    def test_channel_lists_do_not_deadlock_the_gate(self):
        # swampdragon passes lists; the base class recurses through subscribe()
        # for each element, which must not be queued behind itself.
        self.sub.subscribe(['chan1', 'chan2'], self.a)
        self.sub.subscribe(['chan3'], self.b)
        self.assertEqual([c for c, _ in self.redis.calls], ['chan1'])

        self.redis.complete(0)
        # chan2 follows chan1 through the list chain; chan3 was queued and replayed
        self.assertEqual(sorted(c for c, _ in self.redis.calls), ['chan1', 'chan2', 'chan3'])
        self.assertEqual(self.redis.listen_calls, 1)

    def test_callbacks_still_run(self):
        seen = []
        self.sub.subscribe('chan1', self.a, callback=lambda *a, **k: seen.append('a'))
        self.sub.subscribe('chan2', self.b, callback=lambda *a, **k: seen.append('b'))
        self.redis.complete(0)
        self.assertEqual(seen, ['a'])
        self.redis.complete(1)
        self.assertEqual(seen, ['a', 'b'])

    def test_closed_client_is_dropped_from_the_queue(self):
        self.sub.subscribe('chan1', self.a)
        self.sub.subscribe('chan2', self.b)
        self.b.session.is_closed = True
        self.redis.complete(0)

        self.assertEqual([c for c, _ in self.redis.calls], ['chan1'])
        self.assertNotIn('chan2', self.sub.subscriber_count)

    def test_after_connecting_subscribes_go_straight_through(self):
        self.sub.subscribe('chan1', self.a)
        self.redis.complete(0)

        self.sub.subscribe('chan2', self.b)
        self.assertEqual([c for c, _ in self.redis.calls], ['chan1', 'chan2'])
        self.assertFalse(self.sub._connecting)
        self.assertEqual(self.sub._pending, [])

    def test_stalled_connect_times_out_and_retries_queued_subscribes(self):
        self.sub.connect_timeout = 0.05
        self.sub.subscribe('chan1', self.a)
        self.sub.subscribe('chan2', self.b)
        self.assertEqual([c for c, _ in self.redis.calls], ['chan1'])

        # never complete chan1; let the timeout fire, and stop as soon as the retry goes out
        io_loop = IOLoop.current()
        original_subscribe = self.redis.subscribe

        def subscribe_then_stop(channel, callback=None):
            original_subscribe(channel, callback=callback)
            io_loop.stop()
        self.redis.subscribe = subscribe_then_stop
        io_loop.add_timeout(io_loop.time() + 2, io_loop.stop)  # safety net only
        io_loop.start()

        # chan2 got its own turn at opening the connection
        self.assertEqual([c for c, _ in self.redis.calls], ['chan1', 'chan2'])
        self.assertTrue(self.sub._connecting)

        # a late completion of the first subscribe must not replay anything twice
        self.redis.complete(0)
        self.assertEqual([c for c, _ in self.redis.calls], ['chan1', 'chan2'])

    def test_connect_failure_does_not_wedge_the_queue(self):
        def boom(channel, callback=None):
            raise IOError('connection refused')
        self.redis.subscribe = boom

        with self.assertRaises(IOError):
            self.sub.subscribe('chan1', self.a)
        self.assertFalse(self.sub._connecting)
