"""The open gate: everyone is answered (the per-person daily cap still applies)."""


class OpenGate:
    def check_message(self, message, kind=None):
        return {'action': 'allow'}
