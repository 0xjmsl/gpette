import time

from modules import admin_menu
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from apscheduler.schedulers.background import BackgroundScheduler
from models.conversation_db import ConversationDB
from config import Config


class ProactiveSystem:
    """
    Timer-based messaging system. Checks for due timers every 60 seconds
    and fires them through the thread controller.
    """

    def __init__(self, thread_controller, conversation_db: ConversationDB = None):
        self.thread_controller = thread_controller
        self.conversation_db = conversation_db or ConversationDB()
        self.scheduler = BackgroundScheduler()
        self.is_running = False
        self._check_lock = threading.Lock()
        logging.info("ProactiveSystem initialized")

    def start(self):
        """Start the timer check scheduler"""
        if self.is_running:
            logging.warning("ProactiveSystem is already running")
            return

        # max_instances must be high — APScheduler's instance counter gets
        # permanently stuck when ThreadPoolExecutor kills a timed-out thread
        # (the counter is never decremented). Real concurrency is guarded by
        # self._check_lock instead.
        self.scheduler.add_job(
            self._check_due_timers,
            'interval',
            seconds=Config.PROACTIVE_CHECK_INTERVAL,
            id='timer_check',
            replace_existing=True,
            max_instances=10
        )

        self.scheduler.start()
        self.is_running = True
        logging.info(f"ProactiveSystem scheduler started - checking every {Config.PROACTIVE_CHECK_INTERVAL} seconds")

    def stop(self):
        """Stop the timer check scheduler"""
        if not self.is_running:
            return

        self.scheduler.shutdown()
        self.is_running = False
        logging.info("ProactiveSystem scheduler stopped")

    def _check_due_timers(self):
        """Check for timers that are due and send proactive messages for them.
        Runs DB work in a separate thread with a hard 15s timeout so the
        APScheduler thread always returns quickly. A threading lock prevents
        overlapping checks (replaces APScheduler's broken max_instances=1).
        """
        if not self._check_lock.acquire(blocking=False):
            return  # previous check still running, skip silently

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self._do_timer_check)
                future.result(timeout=15)
        except TimeoutError:
            logging.error("ProactiveSystem: _check_due_timers timed out after 15s — the database may be locked")
        except Exception as e:
            logging.error(f"ProactiveSystem error in _check_due_timers: {e}")
        finally:
            self._check_lock.release()

    def _do_timer_check(self):
        """Actual timer check logic, called from a worker thread."""
        due_timers = self.conversation_db.get_due_timers()
        if not due_timers:
            return

        logging.info(f"ProactiveSystem: Found {len(due_timers)} due timers")

        for timer in due_timers:
            timer_id = timer.get('timer_id')
            conversation_id = timer.get('conversation_id')
            timer_type = timer.get('timer_type', 'one_shot')
            interval_seconds = timer.get('interval_seconds')

            # Mark/advance before sending to avoid double-fire on slow processing
            if timer_type == 'recurring' and interval_seconds:
                self.conversation_db.advance_recurring_timer(timer_id, interval_seconds)
            else:
                self.conversation_db.mark_timer_fired(timer_id)

            # Paused from /menu: the timer keeps its schedule but doesn't fire.
            if admin_menu.timers_paused():
                logging.info(f"ProactiveSystem: timers paused, '{timer_id}' skipped")
                continue

            # Build proactive message with timer context
            proactive_message = {
                "role": "Proactive",
                "content": {"type": "text", "value": "TIMER_TRIGGER"},
                "conversationId": conversation_id,
                "senderInboxId": "ProactiveSystem",
                "source": "ProactiveSystem",
                "timestamp": int(time.time()),
                "proactive_context": {
                    "timer": {
                        "timer_id": timer_id,
                        "description": timer.get('description', ''),
                        "instructions": timer.get('instructions', ''),
                        "timer_type": timer_type,
                        "skip_session": timer.get('skip_session', False)
                    }
                }
            }

            logging.info(f"ProactiveSystem: Firing timer '{timer_id}' for conversation {conversation_id}")
            self.thread_controller.add_message(proactive_message)
