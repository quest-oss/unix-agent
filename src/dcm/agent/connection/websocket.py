#  ========= CONFIDENTIAL =========
#
#  Copyright (C) 2010-2014 Dell, Inc. - ALL RIGHTS RESERVED
#
#  ======================================================================
#   NOTICE: All information contained herein is, and remains the property
#   of Dell, Inc. The intellectual and technical concepts contained herein
#   are proprietary to Dell, Inc. and may be covered by U.S. and Foreign
#   Patents, patents in process, and are protected by trade secret or
#   copyright law. Dissemination of this information or reproduction of
#   this material is strictly forbidden unless prior written permission
#   is obtained from Dell, Inc.
#  ======================================================================

import datetime
import errno
import json
import logging
import Queue
import socket
import ssl
import threading
from dcm.agent import handshake

import ws4py.client.threadedclient as ws4py_client

import dcm.agent.exceptions as exceptions
import dcm.agent.state_machine as state_machine
import dcm.agent.messaging.utils as utils
import dcm.agent.utils as agent_utils

from dcm.agent.events import global_space as dcm_events

_g_logger = logging.getLogger(__name__)
_g_wire_logger = agent_utils.get_wire_logger()


class WsConnEvents:
    POLL = "POLL"
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
    INCOMING_MESSAGE = "INCOMING_MESSAGE"
    ERROR = "ERROR"
    CLOSE = "CLOSE"


class WsConnStates:
    WAITING = "WAITING"
    HANDSHAKING = "HANDSHAKING"
    OPEN = "OPEN"
    DONE = "DONE"


g_dcm_backoff_key = "FORCED_BACKOFF"


class Backoff(object):

    def __init__(self, max_backoff_seconds,
                 initial_backoff_second=0.5,
                 idle_modifier=0.25):
        self._backoff_seconds = initial_backoff_second
        self._max_backoff = max_backoff_seconds
        self._idle_modifier = idle_modifier
        self._ready_time = datetime.datetime.now()
        self._last_activity = self._ready_time

    def activity(self):
        self._ready_time = datetime.datetime.now()
        self._last_activity = self._ready_time

    def _set_ready_time(self, backoff):
        if backoff > self._max_backoff:
            backoff = self._max_backoff
        self._backoff_seconds = backoff
        new_ready_time = datetime.datetime.now() +\
            datetime.timedelta(seconds=self._backoff_seconds)
        if new_ready_time > self._ready_time:
            self._ready_time = new_ready_time

    def error(self):
        self._set_ready_time(self._backoff_seconds*2.0)

    def closed(self):
        idle_time = datetime.datetime.now() - self._last_activity
        self._set_ready_time(idle_time.total_seconds() * self._idle_modifier)

    def ready(self):
        return self._ready_time < datetime.datetime.now()

    def force_backoff_time(self, backoff_seconds):
        self._ready_time = datetime.datetime.now() +\
            datetime.timedelta(seconds=backoff_seconds)

    def seconds_until_ready(self):
        d = self._ready_time - datetime.datetime.now()
        return max(0.0, d.total_seconds())


class RepeatQueue(object):

    def __init__(self, max_req_id=500):
        self._q = Queue.Queue()
        self._lock = threading.RLock()
        self._message_id_set = set()
        self._request_id_count = {}
        self._max_req_id = max_req_id + 1

    def put(self, item, block=True, timeout=None):
        self._lock.acquire()
        try:
            try:
                if 'message_id' in item:
                    message_id = item['message_id']
                    if message_id in self._message_id_set:
                        _g_logger.info("Skipping sending a retransmission "
                                       "of message id %s" % message_id)
                        return
                    else:
                        _g_logger.debug("Adding the message with id %s " %
                                        message_id)
                    self._message_id_set.add(message_id)
                if 'request_id' in item:
                    request_id = item['request_id']
                    if request_id not in self._request_id_count:
                        self._request_id_count[request_id] = 0
                    self._request_id_count[request_id] += 1
                    if self._request_id_count[request_id] >= self._max_req_id:
                        msg = "TOO MANY MESSAGES FOR %s!" % request_id
                        _g_logger.error(msg)
                        agent_utils.build_assertion_exception(_g_logger, msg)
                        if self._request_id_count[request_id] ==\
                                self._max_req_id:
                            agent_utils.log_to_dcm(logging.ERROR, msg)
                        return
            except Exception as ex:
                _g_logger.warn("Exception checking if message is a retrans "
                               "%s" % ex.message)
            return self._q.put(item, block=block, timeout=timeout)
        finally:
            self._lock.release()

    def get(self, block=True, timeout=None):
        self._lock.acquire()
        try:
            item = self._q.get(block=block, timeout=timeout)
            try:
                if 'message_id' in item:
                    if item['message_id'] in self._message_id_set:
                        self._message_id_set.remove(item['message_id'])
            except Exception as ex:
                _g_logger.info("Exception checking if message has an id "
                               "%s" % ex.message)
            return item
        finally:
            self._lock.release()

    def task_done(self):
        return self._q.task_done()


class _WebSocketClient(ws4py_client.WebSocketClient):

    def __init__(self, manager, url, _receive_object, protocols=None,
                 extensions=None,
                 heartbeat_freq=None, ssl_options=None, headers=None):
        ws4py_client.WebSocketClient.__init__(
            self, url, protocols=protocols, extensions=extensions,
            heartbeat_freq=heartbeat_freq, ssl_options=ssl_options,
            headers=headers)
        _g_logger.info("Attempting to connect to %s" % url)

        self._receive_object = _receive_object
        self.manager = manager
        self._url = url
        self._dcm_closed_called = False

    def opened(self):
        _g_logger.debug("Web socket %s has been opened" % self._url)

    def closed(self, code, reason=None):
        _g_logger.info("Web socket %s has been closed %d %s"
                       % (self._url, code, reason))
        _g_logger.debug("Sending error event to connection manager.")
        self.manager.throw_error(Exception(
            "Connection unexpectedly closed: %d %s" % (code, reason)))

    def close(self, code=1000, reason=''):
        self._dcm_closed_called = True
        return ws4py_client.WebSocketClient.close(
            self, code=code, reason=reason)

    def received_message(self, m):
        _g_wire_logger.debug("INCOMING\n--------\n%s\n--------" % str(m.data))
        json_doc = json.loads(m.data)
        self.manager.event_incoming_message(json_doc)

    def send(self, payload, binary=False):
        _g_wire_logger.debug("OUTGOING\n--------\n%s\n--------" % str(payload))
        super(_WebSocketClient, self).send(payload, binary=binary)


class WebSocketConnection(threading.Thread):

    def __init__(self, server_url,
                 backoff_amount=5000, max_backoff=300000,
                 heartbeat=None, allow_unknown_certs=False, ca_certs=None):
        super(WebSocketConnection, self).__init__()
        self._send_queue = RepeatQueue()
        self._ws_manager = None
        self._server_url = server_url
        self._cond = threading.Condition()
        self._done_event = threading.Event()

        self._connect_timer = None

        self._backoff = Backoff(float(max_backoff) / 1000.0,
                                float(backoff_amount) / 1000.0)

        self._sm = state_machine.StateMachine(WsConnStates.WAITING)
        self._setup_states()
        self._handshake_manager = None
        self._heartbeat_freq = heartbeat
        if allow_unknown_certs:
            cert_reqs = ssl.CERT_NONE
        else:
            cert_reqs = ssl.CERT_REQUIRED
        self._ssl_options = {'cert_reqs': cert_reqs, 'ca_certs': ca_certs}

    @agent_utils.class_method_sync
    def set_backoff(self, backoff_seconds):
        self._backoff.force_backoff_time(backoff_seconds)

    @agent_utils.class_method_sync
    def connect(self, receive_object, handshake_manager):
        self._receive_object = receive_object
        self._handshake_manager = handshake_manager
        self.start()

    def _register_connect(self):
        if self._connect_timer is not None:
            raise exceptions.AgentRuntimeException(
                "There is already a connection registered")
        self._connect_timer = dcm_events.register_callback(
            self.event_connect_timeout,
            delay=self._backoff.seconds_until_ready())

    @agent_utils.class_method_sync
    def event_connect_timeout(self):
        self._connect_timer = None
        self._sm.event_occurred(WsConnEvents.CONNECT_TIMEOUT)

    @agent_utils.class_method_sync
    def send(self, doc):
        _g_logger.debug("Adding a message to the send queue")
        self._send_queue.put(doc)
        self._cond.notify()

    @agent_utils.class_method_sync
    def close(self):
        _g_logger.debug("Websocket connection closed.")
        self.event_close()

    @agent_utils.class_method_sync
    def run(self):
        self._register_connect()
        while not self._done_event.is_set():
            try:
                self._sm.event_occurred(WsConnEvents.POLL)
                self._cond.wait()
            except Exception as ex:
                _g_logger.exception("The ws connection poller loop had "
                                    "an unexpected exception.")
                self._throw_error(ex)

    #########
    # incoming events
    #########
    @agent_utils.class_method_sync
    def event_close(self):
        self._backoff.closed()
        self._sm.event_occurred(WsConnEvents.CLOSE)

    @agent_utils.class_method_sync
    def event_incoming_message(self, incoming_data):
        self._sm.event_occurred(WsConnEvents.INCOMING_MESSAGE,
                                incoming_data=incoming_data)

    @agent_utils.class_method_sync
    def event_error(self, exception=None):
        self._sm.event_occurred(WsConnEvents.ERROR)
        _g_logger.error(
            "State machine received an exception %s" % str(exception))

    def _throw_error(self, exception, notify=True):
        _g_logger.warning("throwing error %s" % str(exception))
        self._backoff.error()
        dcm_events.register_callback(self.event_error,
                                     kwargs={"exception": exception})
        if notify:
            self._cond.notify()

    def throw_error(self, exception):
        self._throw_error(exception)

    def lock(self):
        self._cond.acquire()

    def unlock(self):
        self._cond.release()

    #########
    # state transitions
    #########

    def _sm_connect_poll(self):
        """
        Attempting to connect and setup the handshake
        """
        pass

    def _sm_connect(self):
        try:
            self._ws = _WebSocketClient(
                self, self._server_url, self._receive_object,
                protocols=['dcm'], heartbeat_freq=self._heartbeat_freq,
                ssl_options=self._ssl_options)
            self._ws.connect()
            hs_doc = self._handshake_manager.get_send_document()
            _g_logger.debug("Sending handshake")
            self._ws.send(json.dumps(hs_doc))
        except Exception as ex:
            _g_logger.exception("Failed to connect to %s" % self._server_url)
            self._throw_error(ex, notify=False)
            self._cond.notify()

    def _sm_received_hs(self, incoming_data=None):
        """
        The handshake has arrived
        """
        try:
            # if the handshake is rejected an exception will be thrown
            hs = self._handshake_manager.incoming_document(incoming_data)
            if hs.reply_type != handshake.HandshakeIncomingReply.REPLY_CODE_SUCCESS:
                if hs.reply_type == handshake.HandshakeIncomingReply.REPLY_CODE_FORCE_BACKOFF:
                    self._backoff.force_backoff_time(hs.force_backoff)
                self._ws.close()
                ex = exceptions.AgentHandshakeException(hs.reply_type)
                self._throw_error(ex)
            self._cond.notify()
        except Exception as ex:
            self._throw_error(ex)

    def _sm_open_incoming_message(self, incoming_data=None):
        _g_logger.debug("New message received")
        dcm_events.register_callback(self._receive_object, args=[incoming_data])
        self._backoff.activity()

    def _sm_hs_failed(self):
        """
        An error occurred while waiting for the handshake
        """
        self._cond.notify()
        self._register_connect()

    def _sm_close_open(self):
        """
        A user called close while the connection was open
        """
        _g_logger.debug("close called when open")

        self._done_event.set()
        self._cond.notify()

    def _sm_hs_close(self):
        """
        A user called close while waiting for a handshake
        """
        _g_logger.debug("close event while handshaking")
        self._done_event.set()
        self._cond.notify()

    def _sm_not_open_close(self):
        """
        A user called close when the connection was not open
        """
        _g_logger.debug("close event while not open")
        self._done_event.set()
        self._cond.notify()

    def _sm_open_poll(self):
        """
        A poll event occurred in the open state.  Check the send queue
        """
        # check the send queue
        done = False
        while not done:
            try:
                doc = self._send_queue.get(False)
                self._send_queue.task_done()

                msg = json.dumps(doc)
                self._ws.send(msg)
            except socket.error as er:
                if er.errno == errno.EPIPE:
                    _g_logger.info(
                        "The ws connection broke for %s" % self._server_url)
                else:
                    _g_logger.info(
                        "A WS socket error occurred %s" % self._server_url)
                self._throw_error(er)
                done = True
            except Queue.Empty:
                done = True
            except Exception as ex:
                _g_logger.exception(str(ex))
                self._throw_error(ex)
                done = True

    def _sm_open_error(self):
        """
        And error occured while the connection was open
        """
        self._cond.notify()
        self._register_connect()

    def _sm_waiting_error(self):
        """
        An error occurred while waiting on the connection.  This is an odd
        case
        """
        _g_logger.warn("An error occurred while waiting to try a new "
                       "connection.")

    def _sm_handshake_poll(self):
        """
        While waiting for the handshake a poll event occurred
        """
        pass

    def _sm_handshake_connect(self):
        """
        This could happen if the POLL event happened twice in the waiting
        state before the first one could try to connect.  Just ignore
        """
        pass

    def _setup_states(self):
        self._sm.add_transition(WsConnStates.WAITING,
                                WsConnEvents.POLL,
                                WsConnStates.WAITING,
                                self._sm_connect_poll)
        self._sm.add_transition(WsConnStates.WAITING,
                                WsConnEvents.ERROR,
                                WsConnStates.WAITING,
                                self._sm_waiting_error)
        self._sm.add_transition(WsConnStates.WAITING,
                                WsConnEvents.CONNECT_TIMEOUT,
                                WsConnStates.HANDSHAKING,
                                self._sm_connect)
        self._sm.add_transition(WsConnStates.WAITING,
                                WsConnEvents.CLOSE,
                                WsConnStates.DONE,
                                self._sm_not_open_close)

        self._sm.add_transition(WsConnStates.HANDSHAKING,
                                WsConnEvents.INCOMING_MESSAGE,
                                WsConnStates.OPEN,
                                self._sm_received_hs)
        self._sm.add_transition(WsConnStates.HANDSHAKING,
                                WsConnEvents.ERROR,
                                WsConnStates.WAITING,
                                self._sm_hs_failed)
        self._sm.add_transition(WsConnStates.HANDSHAKING,
                                WsConnEvents.POLL,
                                WsConnStates.HANDSHAKING,
                                self._sm_handshake_poll)
        self._sm.add_transition(WsConnStates.HANDSHAKING,
                                WsConnEvents.CONNECT_TIMEOUT,
                                WsConnStates.HANDSHAKING,
                                self._sm_handshake_connect)
        self._sm.add_transition(WsConnStates.HANDSHAKING,
                                WsConnEvents.CLOSE,
                                WsConnStates.DONE,
                                self._sm_hs_close)

        self._sm.add_transition(WsConnStates.OPEN,
                                WsConnEvents.CLOSE,
                                WsConnStates.DONE,
                                self._sm_close_open)
        self._sm.add_transition(WsConnStates.OPEN,
                                WsConnEvents.POLL,
                                WsConnStates.OPEN,
                                self._sm_open_poll)
        self._sm.add_transition(WsConnStates.OPEN,
                                WsConnEvents.INCOMING_MESSAGE,
                                WsConnStates.OPEN,
                                self._sm_open_incoming_message)
        self._sm.add_transition(WsConnStates.OPEN,
                                WsConnEvents.ERROR,
                                WsConnStates.WAITING,
                                self._sm_open_error)

        self._sm.add_transition(WsConnStates.DONE,
                                WsConnEvents.POLL,
                                WsConnStates.DONE,
                                None)
        self._sm.add_transition(WsConnStates.DONE,
                                WsConnEvents.CONNECT_TIMEOUT,
                                WsConnStates.DONE,
                                None)
        self._sm.add_transition(WsConnStates.DONE,
                                WsConnEvents.ERROR,
                                WsConnStates.DONE,
                                None)
        self._sm.add_transition(WsConnStates.DONE,
                                WsConnEvents.INCOMING_MESSAGE,
                                WsConnStates.DONE,
                                None)
