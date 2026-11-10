class NoneGate:
    """Answer no one: every message is dropped silently (no reply, no read receipt).

    Use it when two gpettes share one identity (two installations of one inbox, e.g. one
    machine for the owner and another for everyone else): each sets `gate = "none"` on
    the people the other one serves, so exactly one of them replies.
    """

    def check_message(self, message, kind=None):
        return {'action': 'reject'}
