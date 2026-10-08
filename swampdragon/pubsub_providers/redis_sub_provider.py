import json
import logging
from datetime import timedelta

import tornadoredis.pubsub
import tornadoredis
from tornado.ioloop import IOLoop
from .base_provider import BaseProvider
from .redis_settings import get_redis_host, get_redis_port, get_redis_db, get_redis_password


logger = logging.getLogger(__name__)


class SerializedSockJSSubscriber(tornadoredis.pubsub.SockJSSubscriber):
    """
    SockJSSubscriber that lets only one subscribe open the Redis connection.

    BaseSubscriber.subscribe starts a ``listen`` loop from the callback of the
    first SUBSCRIBE whenever ``redis.subscribed`` is empty. ``subscribed`` is
    only filled once that SUBSCRIBE (and the SELECT before it) has gone out, so
    a second subscribe arriving in that window starts a second ``listen`` loop
    on the same socket. The two loops then take turns reading lines of one
    reply and the stream desyncs: "Unknown response type s" and "failed to
    format reply to LISTEN, raw data: $15...". Both loops die and the process
    never reads from Redis again.

    This happens at every process start, when the lobby servers reconnect
    together. Here a subscribe that arrives while the first one is still
    connecting is queued and replayed once the connection is up, so exactly one
    ``listen`` loop ever runs. The same gate covers reconnects after Redis drops
    the connection, since that clears ``redis.subscribed`` too.
    """

    # Seconds to wait for the connecting subscribe before giving up on it and
    # letting the queued subscribes try again (a stalled connect would otherwise
    # wedge the queue forever).
    connect_timeout = 10

    def __init__(self, tornado_redis_client):
        super(SerializedSockJSSubscriber, self).__init__(tornado_redis_client)
        self._connecting = False
        self._pending = []

    def _io_loop(self):
        return getattr(self.redis, '_io_loop', None) or IOLoop.current()

    def subscribe(self, channel_name, subscriber, callback=None):
        if isinstance(channel_name, (list, tuple)) or self.redis.subscribed:
            # A list is handled by the base class one channel at a time through
            # this method, so its first channel passes the gate below.
            return super(SerializedSockJSSubscriber, self).subscribe(channel_name, subscriber, callback=callback)

        if self._connecting:
            self._pending.append((channel_name, subscriber, callback))
            return

        self._connecting = True
        state = {'done': False, 'timeout': None}
        io_loop = self._io_loop()

        def finish():
            if state['done']:
                return False
            state['done'] = True
            if state['timeout'] is not None:
                io_loop.remove_timeout(state['timeout'])
            self._connecting = False
            return True

        def on_connected(*args, **kwargs):
            if finish():
                self._replay_pending()
                if callback:
                    callback(*args, **kwargs)

        def on_timeout():
            if finish():
                logger.warning(
                    'Redis subscribe to %s did not complete within %ss; retrying %s queued subscribe(s)',
                    channel_name, self.connect_timeout, len(self._pending))
                self._replay_pending()

        state['timeout'] = io_loop.add_timeout(timedelta(seconds=self.connect_timeout), on_timeout)
        try:
            super(SerializedSockJSSubscriber, self).subscribe(channel_name, subscriber, callback=on_connected)
        except Exception:
            # e.g. Redis refused the connection. Let the queued subscribes have
            # their own go (from the io loop, not this stack) as they would have
            # without the gate.
            if finish():
                io_loop.add_callback(self._replay_pending)
            raise

    def _replay_pending(self):
        pending, self._pending = self._pending, []
        for channel_name, subscriber, callback in pending:
            session = getattr(subscriber, 'session', None)
            if session is not None and getattr(session, 'is_closed', False):
                # The client went away while waiting; subscribing it now would
                # leave a dead entry in the subscriber counters.
                continue
            self.subscribe(channel_name, subscriber, callback=callback)


class RedisSubProvider(BaseProvider):
    def __init__(self):
        self._subscriber = SerializedSockJSSubscriber(tornadoredis.Client(
            host=get_redis_host(),
            port=get_redis_port(),
            password=get_redis_password(),
            selected_db=get_redis_db()
        ))

    def close(self, broadcaster):
        for channel in self._subscriber.subscribers:
            if broadcaster in self._subscriber.subscribers[channel]:
                self._subscriber.unsubscribe(channel, broadcaster)

    def get_channel(self, base_channel, **channel_filter):
        return self._construct_channel(base_channel, **channel_filter)

    def subscribe(self, channels, broadcaster):
        self._subscriber.subscribe(channels, broadcaster)

    def unsubscribe(self, channels, broadcaster):
        for channel in channels:
            if broadcaster in self._subscriber.subscribers[channel]:
                self._subscriber.subscribers[channel].pop(broadcaster)

    def publish(self, channel, data):
        if isinstance(data, dict):
            data = json.dumps(data)
        broadcasters = list(self._subscriber.subscribers[channel].keys())
        if broadcasters:
            for bc in broadcasters:
                if not bc.session.is_closed:
                    bc.broadcast(broadcasters, data)
                    break
