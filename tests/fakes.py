"""Test stand-ins shared by the client tests."""

from cmtp import Message, MessageType, encode


class FakeConnection:
    """Records what a client sends instead of sending it."""

    def __init__(self, host="127.0.0.1", port=0):
        self.sent = []
        self.closed = False
        self.incoming = []

    def connect(self):
        pass

    def send(self, message_type, body=None):
        seq = len(self.sent) + 1
        encode(Message(message_type, seq, body or {}))  # check it, as the real connection does
        self.sent.append((MessageType(message_type), body or {}))
        return seq

    def poll(self):
        events, self.incoming = self.incoming, []
        return events

    def close(self):
        self.closed = True
