from flask import Flask
from flask_cors import CORS
from flask_socketio import SocketIO

from modules.thread_controller import ThreadController
from modules.gates import get_gate, invalidate_conversation as invalidate_gate_cache
from modules.profiles import resolve_profile
from modules import filter as message_filter
from modules import routes as route_log
from modules import admin_menu
from modules.proactive_system import ProactiveSystem
from .routes.routes import bp as xmtp_routes
from .routes.admin import bp as admin_routes
from modules.chat_controller import ChatController
from modules.function_registry import FunctionRegistry
from services.attachment_handler import AttachmentHandler
from models.conversation_db import ConversationDB

import json

from config import Config, validate_compaction_config

from utils.message_logger import log_message_flow
import threading
import logging
import time
import base64

socketio = SocketIO()
thread_controller = ThreadController()
attachment_handler = AttachmentHandler()
conversation_db = ConversationDB()

# The access gate (modules/gates): `[gate] kind`, overridden per profile by
# `gate = ...`. Built at start so a broken plug-in fails loudly at boot; profile
# gates are built on first use. print() (not logging): this runs at import,
# before main.py configures logging; print is teed to the log.
get_gate(Config.GATE_KIND, conversation_db)
print(f"Access gate: {Config.GATE_KIND} "
      f"(per-person cap {Config.GATE_MESSAGES_PER_PERSON_PER_DAY or 'off'}/day)")

def create_server(config_class):
    # Boot-time config validation. Refuses to start if COMPACTION_PROVIDER
    # points at a provider whose API key is missing — surfaces misconfig
    # loudly instead of silently failing on the first compaction call.
    validate_compaction_config()

    server = Flask(__name__)
    server.config.from_object(config_class)

    # Initialize extensions
    CORS(server, resources={r"/*": {"origins": "*"}})
    socketio.init_app(server, cors_allowed_origins="*",
                      ping_timeout=server.config.get('SOCKETIO_PING_TIMEOUT', 60),
                      ping_interval=server.config.get('SOCKETIO_PING_INTERVAL', 25),
                      max_http_buffer_size=server.config.get('SOCKETIO_MAX_HTTP_BUFFER', 10*1024*1024))

    # Register blueprint
    server.register_blueprint(xmtp_routes)
    server.register_blueprint(admin_routes)

    # Initialize function registry and chat controller
    function_registry = FunctionRegistry(thread_controller)
    chat_controller = ChatController(service=config_class.LLM_SERVICE, thread_controller=thread_controller, function_registry=function_registry)
    thread_controller.register_address("gpette")
    thread_controller.register_handler("FunctionRegistry", function_registry.receive_Message_FunctionRegistryr)
    thread_controller.register_handler("ChatController", chat_controller.receive_Message_ChatController)
    thread_controller.register_handler("SocketIO", sendMessage)
    thread_controller.start()

    # Store thread controller and chat controller in app config so routes can access them
    server.config['THREAD_CONTROLLER'] = thread_controller
    server.config['CHAT_CONTROLLER'] = chat_controller

    # Initialize and start proactive messaging system
    proactive_system = ProactiveSystem(
        thread_controller=thread_controller,
        conversation_db=ConversationDB()
    )
    proactive_system.start()

    @socketio.on('connect')
    def handle_connect():
        logging.info("Client connected")

    @socketio.on('disconnect')
    def handle_disconnect():
        logging.info("Client disconnected")

    @socketio.on('xmtp_initialized')
    def handle_xmtp_initialized(data):
        logging.info(f"XMTP client initialized with address: {data['address']}")

    @socketio.on('client_ready')
    def handle_client_ready():
        logging.info("Client is ready to receive messages")

    @socketio.on('group_members')
    def handle_group_members(data):
        """Handle group member list from XMTP SDK"""
        conversation_id = data.get('conversationId')
        members = data.get('members', [])
        if not conversation_id or not members:
            return

        conv_db = ConversationDB()
        conv_db.track_conversation(conversation_id, is_group=True)

        for member in members:
            inbox_id = member.get('inboxId')
            if not inbox_id:
                continue
            # Map permission strings to levels
            perm_str = member.get('permissionLevel', 'member')
            perm_level = {'super_admin': 2, 'admin': 1, 'member': 0}.get(perm_str, 0)
            conv_db.register_group_member(conversation_id, inbox_id, perm_level)

        # Membership may have changed — drop cached group-auth decision so the
        # next message re-evaluates the participant intersection.
        invalidate_gate_cache(conversation_id)

        logging.info(f"Group members updated for {conversation_id}: {len(members)} members")

    @socketio.on('incoming_message')
    def handle_incoming_message(data):
        # print(f"Incoming message received via Socket.IO: {data}")
        standardized_message = standardize_message(data)
        sender_id = data.get('senderInboxId', 'unknown')
        log_message_flow("SocketIO", "Server", "incoming_message", sender_id, "received")
        if not standardized_message:
            logging.error("Failed to standardize message")
            return

        # Conversation classification is carried on the envelope by xmtp_daemon.
        # If it's missing, the envelope is half-formed — drop it silently rather
        # than guess (xmtp_daemon already drops messages it can't classify).
        kind = standardized_message.get('kind')
        if not kind:
            logging.warning(f"Dropping message without kind: conversation={standardized_message.get('conversationId')}")
            return

        # Commit the conversation's classification + participants before the
        # security check, so the gate evaluates against current state.
        conversation_db.commit_conversation(
            standardized_message.get('conversationId'),
            kind,
            standardized_message.get('members'),
        )

        # Who this gpette answers ([filter] rules), before anything else: a message
        # another installation of this inbox serves is dropped here, silently.
        verdict, rule = message_filter.decide(standardized_message.get('conversationId'),
                                              standardized_message.get('senderInboxId'), kind)
        if verdict == 'ignore':
            logging.info(f"Filter: ignored {standardized_message.get('senderInboxId')} in "
                         f"{standardized_message.get('conversationId')} ({rule})")
            route_log.note(standardized_message, kind, None, f'filter: {rule} -> ignore')
            return

        # Access gate - first thing after standardization. The message's profile
        # (group id, then sender) picks the gate; no profile gate = [gate] kind.
        profile = resolve_profile(standardized_message.get('conversationId'),
                                  standardized_message.get('senderInboxId'), kind)
        gate = get_gate(profile.gate, conversation_db)
        auth_result = gate.check_message(standardized_message, kind=kind)
        route_log.note(standardized_message, kind, profile,
                       f"{rule} -> gate {profile.gate}: {auth_result['action']}")

        if auth_result['action'] == 'reject':
            logging.warning(f"Security: Rejected message from {standardized_message.get('senderInboxId')}")
            return  # Silent rejection

        if auth_result['action'] == 'auth_success':
            # Send auth success response directly, don't process further
            sendMessage(auth_result['response'])
            return

        if auth_result['action'] == 'auth_limit':
            # Send rate limit response
            sendMessage(auth_result['response'])
            return

        # auth_result['action'] == 'allow' - continue with normal flow

        # /menu and its buttons (admin profiles only), answered before the model
        # so they work while it is over its limit. A button press (intent) on a
        # profile without the menu is dropped: it is not text for the model.
        # A menu that raises (here or while its answer is sent) must not take
        # the conversation down with it: the press is dropped, the user is told,
        # and ordinary chat goes on to the model.
        try:
            menu_msgs, consumed, replacement = admin_menu.handle(
                standardized_message, profile, conversation_db)
            for m in menu_msgs:
                sendMessage(m)
        except Exception as e:
            logging.exception("admin_menu failed, press dropped")
            notice = admin_menu.failure_notice(standardized_message, profile, e)
            consumed, replacement = notice is not None, None
            if notice:
                try:
                    sendMessage(notice)
                except Exception:
                    logging.exception("admin_menu: failure notice not sent")
        if consumed and replacement is None:
            return
        if consumed:
            standardized_message['contentType'] = 'text'
            standardized_message['content'] = {'type': 'text', 'value': replacement}
        elif standardized_message.get('contentType') == 'intent':
            logging.info("Intent on a profile without the menu, ignored")
            return

        logging.info(f"Incoming message from: {standardized_message.get('senderInboxId')} in conversation: {standardized_message.get('conversationId')}")
        content_type = standardized_message.get('contentType', 'text')
        logging.info(f"Incoming message: content type: {content_type}")
        log_message_flow("Server", "SYSTEM", "content_type_check", standardized_message.get('senderInboxId'), content_type)
        
        # Handle attachments
        if standardized_message.get('contentType') and 'attachment' in standardized_message.get('contentType'):
            try:
                attachment_data = standardized_message.get('content')

                # Decode base64 string from xmtp_daemon to bytes
                if isinstance(attachment_data, str):
                    attachment_data = base64.b64decode(attachment_data)
                # Fallback: Convert array of numbers (legacy format) to bytes
                elif isinstance(attachment_data, list):
                    attachment_data = bytes(attachment_data)

                processed_attachment = attachment_handler.process_incoming_attachment(
                    attachment_data,
                    standardized_message.get('filename', 'unknown'),
                    standardized_message.get('mimeType', 'application/octet-stream')
                )

                # Check for processing errors
                if processed_attachment.get('error'):
                    logging.error(f"Attachment processing error: {processed_attachment.get('error')}")
                    # Say what was sent and keep the caption (it was lost before).
                    note = (f"[The user sent {standardized_message.get('filename', 'a file')} "
                            f"({standardized_message.get('mimeType', 'unknown type')}, "
                            f"{len(attachment_data) / 1048576:.1f} MB); it was too large to pass on.]")
                    caption = standardized_message.get('description', '')
                    standardized_message['content'] = {
                        "type": "text",
                        "value": f"{note} {caption}".strip()
                    }
                else:
                    # Base64 encode the binary data for JSON compatibility and storage
                    binary_data = processed_attachment.get('data')
                    base64_data = base64.b64encode(binary_data).decode('utf-8') if binary_data else None

                    # Use the caption as the text for LLM, fallback to filename
                    description = standardized_message.get('description', '')
                    if description:
                        text_value = description  # User's description becomes the prompt for the image
                        logging.info(f"Attachment has description: {description}")
                    else:
                        text_value = f"Attachment: {processed_attachment.get('filename')}"

                    standardized_message['content'] = {
                        "type": "attachment",
                        "value": text_value,  # This becomes the text sent alongside image to LLM
                        "data": base64_data,
                        "filename": processed_attachment.get('filename'),
                        "mimeType": processed_attachment.get('mimeType'),
                        "description": description  # Store separately for reference
                    }

                    # Gallery (xmtp_daemon `attachments`, 2+ files): the first file
                    # is the one above; the rest ride along as `extra_images`. Only
                    # the Claude Code path reads them; other providers and the DB
                    # still see the first image, as before.
                    extras = []
                    for item in (standardized_message.get('attachments') or [])[1:]:
                        try:
                            p = attachment_handler.process_incoming_attachment(
                                base64.b64decode(item.get('content', '')),
                                item.get('filename', 'unknown'),
                                item.get('mimeType', 'application/octet-stream'))
                            if not p.get('error') and p.get('mimeType', '').startswith('image/'):
                                extras.append({
                                    "data": base64.b64encode(p['data']).decode('utf-8'),
                                    "mimeType": p['mimeType'],
                                })
                        except Exception as e:
                            logging.error(f"Error processing gallery item: {e}")
                    if extras:
                        standardized_message['content']['extra_images'] = extras
            except Exception as e:
                logging.error(f"Error processing attachment: {e}")
                standardized_message['content'] = {
                    "type": "text",
                    "value": "Failed to process attachment"
                }
        
        # The raw gallery list (base64 bodies) stops here; extras are in content.
        standardized_message.pop('attachments', None)
        standardized_message['source'] = 'SocketIO'

        # Ensure message has the required fields
        if not standardized_message.get('content'):
            logging.error("Message is missing content")
            return
            
        # Log the full message for debugging
        logging.info(f"Message being sent to thread controller: {standardized_message}")
        
        # Fire read receipt with a short delay so the XMTP network timestamp
        # lands after the sender's message (avoids receipt appearing before the message)
        if standardized_message.get("role") == "user" and Config.SEND_READ_RECEIPTS:
            conversation_id = standardized_message.get("conversationId")
            if conversation_id:
                threading.Timer(0.4, sendMessage, args=[{
                    "conversationId": conversation_id,
                    "contentType": "readReceipt",
                    "content": ""
                }]).start()

        # Hand off to thread controller
        thread_controller.add_message(standardized_message)

    @socketio.on('outgoing_message')
    def handle_outgoing_message(data):
        standardized_message = standardize_message(data)
        logging.info(f"Outgoing message: {standardized_message}")

    # xmtp_daemon over plain HTTP: the same two handlers, reached by POST.
    # The socket.io events above serve the local web UI (ui/).
    from .xmtp_http_bridge import register_routes
    register_routes(server, handle_incoming_message, handle_group_members)

    return server

def standardize_message(data):
    """
    Standardize incoming message to a consistent dictionary format.
    
    New format includes conversationId and senderInboxId for conversation tracking.
    Also handles attachments (binary data).
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            logging.error("Failed to parse JSON string")
            return None

    # The log gets the shape of a message, never its text: this runs before the
    # filter and the gate, so the text may be from anyone.
    logging.info(f"Standardize message input: {message_shape(data)}")

    # Extract conversation identifiers
    conversation_id = data.get('conversationId')
    sender_inbox_id = data.get('senderInboxId')
    
    # Skip messages without conversation context
    if not conversation_id:
        logging.warning("Message missing conversationId, skipping")
        return None
    
    standardized_message = {
        'role': data.get('role') or 'user',
        'timestamp': data.get('timestamp', int(time.time())),
        'content': None,  # Will be set below
        'tool_calls': data.get('tool_calls'),
        'tool_call_id': data.get('tool_call_id'),  # For tool responses
        'token_count': data.get('token_count'),
        'source': data.get('source', 'SocketIO'),
        'conversationId': conversation_id,
        'senderInboxId': sender_inbox_id,
        'contentType': data.get('contentType'),
        # Conversation classification filled at the source by xmtp_daemon —
        # 'dm' | 'group'. The envelope carries kind so no layer has to infer it
        # from the DB or from out-of-band events.
        'kind': data.get('kind'),
        'members': data.get('members'),
    }
    
    # A button press on a menu (xmtp_daemon `intent`): {id, actionId, metadata?}
    if data.get('intent'):
        standardized_message['intent'] = data['intent']

    # Extract metadata for attachments
    if data.get('contentType') and 'attachment' in data.get('contentType'):
        standardized_message['filename'] = data.get('filename', 'file')
        standardized_message['mimeType'] = data.get('mimeType', 'application/octet-stream')
        standardized_message['description'] = data.get('description', '')
        # xmtp_daemon galleries: every file, the first one included.
        if data.get('attachments'):
            standardized_message['attachments'] = data['attachments']

    # Process content - THIS IS THE IMPORTANT PART
    if 'content' in data:
        if isinstance(data['content'], dict):
            standardized_message['content'] = data['content']
        elif data.get('contentType') and 'attachment' in data.get('contentType'):
            # This is a binary attachment, preserve as is
            standardized_message['content'] = data['content']
        else:
            # Plain text or string content
            standardized_message['content'] = {
                'type': 'text',
                'value': data['content']
            }
    else:
        logging.warning("No content field in message data")
    
    # Add tool_calls if present
    if 'tool_calls' in data:
        standardized_message['tool_calls'] = data['tool_calls']
    
    # Add tool_call_id if present (for tool responses)
    if 'tool_call_id' in data:
        standardized_message['tool_call_id'] = data['tool_call_id']

    # Final check to ensure content is set
    if standardized_message['content'] is None:
        logging.warning("Content still None after processing, setting default")
        standardized_message['content'] = {
            'type': 'text',
            'value': 'Empty message'
        }

    sender_id = standardized_message.get('senderInboxId', 'unknown')
    log_message_flow("Server", "CLIENT", "standardized_message", sender_id,
                     standardized_message.get('contentType') or 'text')

    return standardized_message


def message_shape(data):
    """One log line about a message that holds none of what its sender wrote:
    who, where, what kind, how long. Used before the filter and the gate."""
    content = data.get('content')
    if isinstance(content, dict):
        content = content.get('value')
    size = len(content) if isinstance(content, (str, bytes, list)) else 0
    return (f"conversation={data.get('conversationId')} sender={data.get('senderInboxId')} "
            f"kind={data.get('kind')} type={data.get('contentType') or 'text'} size={size}")

def _emit_outgoing(message: dict):
    """Hand one outgoing message to xmtp_daemon, and show it to any web UI
    watching (socket.io). A conversation the daemon does not know (a made-up
    id typed in the web UI) only logs a send error."""
    socketio.emit('outgoing_message', message)
    from .xmtp_http_bridge import send_via_daemon
    send_via_daemon(message)

def sendMessage(data: dict):
    """Send a message via xmtp_daemon."""
    conversation_id = data.get("conversationId")
    if not conversation_id:
        logging.warning("Attempted to send message without conversationId")
        return

    # Read receipt — empty payload, just the content type signal
    if data.get("contentType") == "readReceipt":
        logging.info(f"Sending read receipt to conversation: {conversation_id}")
        _emit_outgoing({
            "conversationId": conversation_id,
            "contentType": "readReceipt",
            "content": ""
        })
        return

    # A menu of buttons (modules/admin_menu.py), passed through as is.
    if data.get("contentType") == "actions":
        _emit_outgoing({
            "conversationId": conversation_id,
            "contentType": "actions",
            "actions": data.get("actions"),
        })
        return

    if not data.get("content"):
        logging.warning("Attempted to send message without content")
        return

    # Check if this is an attachment
    is_attachment = False
    attachment_data = None

    if isinstance(data.get("content"), dict) and data["content"].get("type") == "attachment":
        is_attachment = True
        # Extract and process attachment data
        attachment_info = data["content"]
        attachment_data = attachment_info.get("data")

        # Create attachment message format
        message = {
            "conversationId": conversation_id,
            "contentType": "attachment",
            "filename": attachment_info.get("filename", "file"),
            "mimeType": attachment_info.get("mimeType", "application/octet-stream"),
            "content": attachment_data,
        }
    else:
        # Regular text message
        message = {
            "conversationId": conversation_id,
            "content": data.get("content", "Empty Message").get("value", "Empty Message"),
            "tool_calls": data.get("tool_calls")
        }

    # Send the message
    logging.info(f"Sending {'attachment' if is_attachment else 'text'} message to conversation: {conversation_id}")
    _emit_outgoing(message)

def run_server(server):
    # 127.0.0.1 by default: the web UI and /api/config can change what the bot may
    # do, so never expose this port to a network you do not trust.
    socketio.run(server, host=Config.API_HOST, port=int(server.config.get('API_PORT', 4849)),
                 allow_unsafe_werkzeug=True)