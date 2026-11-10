"""
Per-inbox daily message cap (`[gate] messages_per_person_per_day`).

Applied by modules/gates after any gate says 'allow', so one spammy (or
looping) sender can't burn the operator's LLM budget. The existing
conversation_message_counters only caps continuous tool-call loops, not per-user
message volume; this fills that gap.

State is a small JSON file (no new DB tables): {"date": "YYYY-MM-DD",
"counts": {inbox: n}}. Resets on the first message of a new day. Survives
restarts. Disabled when per_day <= 0.
"""

import json
import logging
import os
import threading
from datetime import datetime


class DailyRateLimiter:
    def __init__(self, per_day: int, state_file: str):
        self.per_day = per_day
        self.state_file = state_file
        self._lock = threading.Lock()
        self._date = None
        self._counts = {}
        self._load()

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _load(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                if data.get("date") == self._today():
                    self._date = data["date"]
                    self._counts = data.get("counts", {})
                    logging.info(f"DailyRateLimiter: resumed {len(self._counts)} inbox counts for {self._date}")
        except Exception as e:
            logging.error(f"DailyRateLimiter: failed to load {self.state_file}: {e}")

    def _save(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump({"date": self._date, "counts": self._counts}, f)
        except Exception as e:
            logging.error(f"DailyRateLimiter: failed to save {self.state_file}: {e}")

    def allow(self, inbox_id: str) -> bool:
        """Record a message for this inbox; return False once it's over the daily cap."""
        if self.per_day <= 0:
            return True
        key = (inbox_id or "").lower().replace("0x", "")
        with self._lock:
            today = self._today()
            if self._date != today:
                self._date = today
                self._counts = {}
            n = self._counts.get(key, 0)
            if n >= self.per_day:
                logging.warning(f"DailyRateLimiter: inbox {key[:12]}... hit daily cap ({self.per_day}) — drop")
                return False
            self._counts[key] = n + 1
            self._save()
            return True
