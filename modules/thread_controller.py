import json
import time
import queue
import threading
from typing import List, Dict, Any, Callable
import logging

from models.conversation_db import ConversationDB
from modules.thread_joins import get_group
from modules import transcripts

from utils.message_logger import log_message_flow, truncate_message

class ThreadController:
    def __init__(self, max_continuous_non_user_messages=50):
        self.message_queue = queue.Queue()
        self.handlers: Dict[str, Callable] = {}
        self.db_lock = threading.Lock()
        self.address = ''
        self.conversation_db = ConversationDB()
        self.continuous_non_user_messages = 0
        self.max_continuous_non_user_messages = max_continuous_non_user_messages
        self.queue_locked = False
        
        # Map to track continuous non-user messages per conversation
        self.conversation_message_counters = {}

        logging.info("ThreadController initialized")

    def add_message(self, message: Dict[str, Any]):
        """Add a message to the processing queue"""
        conversation_id = message.get('conversationId')

        logging.info(f"Adding message to thread controller: {conversation_id}")
        sender_id = message.get('senderInboxId', 'unknown')
        content_preview = truncate_message(message.get('content'))
        log_message_flow("ChatController", "ThreadController", "add_message", sender_id, content_preview)

        # Skip messages without conversation context
        if not conversation_id:
            logging.warning("Skipping message without conversation ID")
            return

        # Track continuous non-user messages per conversation (rate limiting)
        # Skip rate limiting for Proactive messages from the ProactiveSystem
        is_proactive = message['role'] == 'Proactive' or message.get('source') == 'ProactiveSystem'

        if message['role'] == 'user':
            logging.info(f"Message from user in conversation {conversation_id}")
            if conversation_id in self.conversation_message_counters:
                self.conversation_message_counters[conversation_id] = 0
        elif not is_proactive:
            logging.info(f"Non-user message in conversation {conversation_id}")
            if conversation_id not in self.conversation_message_counters:
                self.conversation_message_counters[conversation_id] = 0

            self.conversation_message_counters[conversation_id] += 1

            # Check if we should skip this message due to too many non-user messages
            if self.conversation_message_counters[conversation_id] >= self.max_continuous_non_user_messages:
                logging.warning(f"Too many continuous non-user messages in conversation {conversation_id}")
                return

        # Guard: never queue empty assistant messages to XMTP. A tool call often
        # has no text and must still reach FunctionRegistry (SocketIO skips it).
        if message.get('role') == 'assistant' and not message.get('tool_calls'):
            content = message.get('content', {})
            value = content.get('value', '') if isinstance(content, dict) else str(content)
            if not value or not value.strip():
                logging.warning(f"Blocking empty assistant message from queue: conversation={conversation_id}")
                return

        # Add message to queue
        logging.info(f"Adding message to queue: conversation={conversation_id}, role={message.get('role')}")
        content_preview = truncate_message(message.get('content'))
        log_message_flow("ThreadController", "MessageQueue", "queue_message", conversation_id, content_preview)
        self.message_queue.put(message)

    def add_system_message(self, content: str, conversation_id=None):
        """Add a system message to the queue"""
        if not conversation_id:
            # Skip messages without conversation context
            logging.warning("Skipping system message without conversation ID")
            return
            
        system_message = {
            "role": "system",
            'address': self.address,
            "timestamp": int(time.time()),
            "content": {"type": "text", "value": content},
            "tool_calls": None,
            "token_count": None,
            "source": "system",
            "conversationId": conversation_id,
            "senderInboxId": self.address
        }
        
        logging.info(f"Adding system message to queue for conversation: {conversation_id}")
        self.message_queue.put(system_message)

    def store_message(self, message: Dict[str, Any]):
        """Store a message in the database"""
        try:
            logging.info(f"Storing message in database: conversation={message.get('conversationId')}, role={message.get('role')}")
            self.conversation_db.store_message(message)
        except Exception as e:
            logging.error(f"Error storing message: {e}")
        transcripts.record(message, message.get('kind'))

    def register_handler(self, handler_name: str, handler: Callable):
        """Register a handler function for message processing"""
        logging.info(f"Registering handler: {handler_name}")
        self.handlers[handler_name] = handler

    def register_address(self, address: str):
        """Register the AI's address"""
        self.address = address
        logging.info(f"Registered AI address: {address}")

    def get_address(self):
        """Get the AI's address"""
        return self.address

    def process_message(self, message: Dict[str, Any]):
        """Process a message from the queue"""
        conversation_id = message.get('conversationId')
        logging.info(f"Processing message: conversation={conversation_id}, role={message.get('role')}")

        # Store the message in the database (skip Proactive triggers and read receipts - they're ephemeral)
        if message.get('role') != 'Proactive' and message.get('contentType') != 'readReceipt':
            try:
                self.store_message(message)
            except Exception as e:
                logging.error(f"Error storing message: {e}")
        
        # Check for reset_chat function call (backward compatibility)
        message_source = message.get('source', 'unknown')
        
        # Check for reset_chat in tool_calls (OpenAI standard)
        if message_source == 'ChatController' and message.get('tool_calls'):
            tool_calls = message['tool_calls']
            if isinstance(tool_calls, str):
                try:
                    tool_calls = json.loads(tool_calls)
                except:
                    logging.error(f"Error parsing tool_calls: {tool_calls}")
            
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if tool_call.get('function', {}).get('name') == 'reset_chat':
                        logging.info(f"Handling reset_chat tool call for conversation: {conversation_id}")
                        self.handle_reset_chat(conversation_id)
                        return  # Skip sending reset message to handlers

        # Send the message to all registered handlers except the source
        for handler_name, handler in self.handlers.items():
            if handler_name != message_source:  # Skip if handler name matches message source
                if message_source == 'FunctionRegistry' and handler_name == 'SocketIO':
                    continue

                if message.get('role') == 'Proactive' and handler_name == 'SocketIO':
                    continue

                if message_source == 'ChatController' and message.get('tool_calls') and handler_name == 'SocketIO':
                    tool_calls = message['tool_calls']
                    if len(tool_calls) > 0:
                     continue        

                # logging.info(f"Sending message to handler: {handler_name}")
                try:
                    # print(f"Sending message to handler: {handler_name} with handler {handler}")
                    handler(message)
                except Exception as e:
                    logging.error(f"Error in handler {handler_name}: {e}")

    def get_chat_history(self, conversation_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Get the chat history for a specific conversation"""
        if not conversation_id:
            logging.warning("Requested chat history without conversation ID")
            return []

        # Thread joining: load history from all grouped conversation IDs
        conversation_ids = get_group(conversation_id)

        logging.info(f"Getting chat history for conversation: {conversation_id} (group: {conversation_ids})")
        # Get conversation history from the new database
        try:
            history = self.conversation_db.get_conversation_history(conversation_ids, limit=limit)
            logging.info(f"Retrieved {len(history)} messages from history")
            return history
        except Exception as e:
            logging.error(f"Error retrieving chat history: {e}")
            return []

    def handle_reset_chat(self, conversation_id: str = None):
        """Reset a conversation by clearing its history"""
        if not conversation_id:
            logging.warning("Attempted to reset chat without conversation ID")
            return

        logging.info(f"Resetting conversation: {conversation_id}")
        # Clear the conversation history
        try:
            self.conversation_db.reset_conversation(conversation_id)
        except Exception as e:
            logging.error(f"Error resetting conversation: {e}")

        # Reset the continuous message counter for this conversation
        self.conversation_message_counters[conversation_id] = 0

        # Create reset confirmation message
        reset_message = {
            "role": "assistant",
            'address': self.address,
            "timestamp": int(time.time()),
            "content": {"type": "text", "value": "Chat has been reset 🤯"},
            "tool_calls": None,
            "token_count": None,
            "source": "system",
            "conversationId": conversation_id,
            "senderInboxId": self.address
        }
        
        # Store the message but don't add it to the queue
        self.store_message(reset_message)
        
        # Send notification to user via sendMessage handler
        for handler_name, handler in self.handlers.items():
            if handler.__name__ == 'sendMessage':
                logging.info("Sending reset confirmation message")
                try:
                    handler(reset_message)
                except Exception as e:
                    logging.error(f"Error sending reset confirmation: {e}")
                break
    
    def run(self):
        """Main processing loop for the message queue"""
        logging.info("Starting message processing thread")
        while True:
            try:
                message = self.message_queue.get()
                logging.info(f"Got message from queue: conversation={message.get('conversationId')}, role={message.get('role')}")
                self.process_message(message)
            except Exception as e:
                logging.error(f"Error in message processing loop: {e}")

    def start(self):
        """Start the message processing thread"""
        logging.info("Starting thread controller")
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        logging.info("Thread controller started")
        
    def is_conversation_authenticated(self, conversation_id, inbox_id):
        """Check if a conversation is authenticated for a specific inbox"""
        result = self.conversation_db.is_conversation_authenticated(conversation_id, inbox_id)
        logging.info(f"Conversation authentication check: conversation={conversation_id}, inbox={inbox_id}, result={result}")
        return result
        
    def is_inbox_authenticated(self, inbox_id):
        """Check if an inbox is authenticated"""
        result = self.conversation_db.is_inbox_authenticated(inbox_id)
        logging.info(f"Inbox authentication check: inbox={inbox_id}, result={result}")
        return result
        
    def register_group_member(self, conversation_id, inbox_id, permission_level=0):
        """Register a group member for a conversation"""
        logging.info(f"Registering group member: conversation={conversation_id}, inbox={inbox_id}, permission={permission_level}")
        return self.conversation_db.register_group_member(conversation_id, inbox_id, permission_level)
        
    def get_conversation_participants(self, conversation_id):
        """Get participants for a conversation"""
        logging.info(f"Getting participants for conversation: {conversation_id}")
        return self.conversation_db.get_conversation_participants(conversation_id)