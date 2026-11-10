from flask import Blueprint, jsonify, request, g, send_file, current_app
import base64
import logging
import json
import os
import tempfile
from werkzeug.utils import secure_filename
from services.attachment_handler import AttachmentHandler
from config import Config, save_overrides, validate_compaction_config, OVERRIDE_TYPES

bp = Blueprint('xmtp', __name__)
attachment_handler = AttachmentHandler()
allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf', 'txt', 'md', 'csv', 'json'}

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions

@bp.route('/api/')
def hello_world():
    """Simple API status check endpoint"""
    return "gpette running"

@bp.route('/conversations', methods=['GET'])
def get_conversations():
    """Get a list of active conversations"""
    thread_controller = current_app.config.get('THREAD_CONTROLLER')
    if not thread_controller:
        return jsonify({"error": "Thread controller not configured"}), 500
    
    # Get active conversations from the database
    conversation_db = thread_controller.conversation_db
    hours = request.args.get('hours', default=24, type=int)
    conversations = conversation_db.get_active_conversations(hours=hours)
    
    return jsonify({
        "success": True,
        "conversations": conversations
    })

@bp.route('/conversations/<conversation_id>/history', methods=['GET'])
def get_conversation_history(conversation_id):
    """Get message history for a specific conversation"""
    thread_controller = current_app.config.get('THREAD_CONTROLLER')
    if not thread_controller:
        return jsonify({"error": "Thread controller not configured"}), 500
    
    # Get conversation history
    limit = request.args.get('limit', default=50, type=int)
    history = thread_controller.get_chat_history(conversation_id, limit=limit)

    return jsonify({
        "success": True,
        "conversation_id": conversation_id,
        "messages": history
    })

@bp.route('/conversations/<conversation_id>/reset', methods=['POST'])
def reset_conversation(conversation_id):
    """Reset a conversation by clearing its history"""
    thread_controller = current_app.config.get('THREAD_CONTROLLER')
    if not thread_controller:
        return jsonify({"error": "Thread controller not configured"}), 500
    
    # Reset the conversation
    thread_controller.handle_reset_chat(conversation_id)
    
    return jsonify({
        "success": True,
        "message": "Conversation reset successfully",
        "conversation_id": conversation_id
    })

@bp.route('/upload_attachment', methods=['POST'])
def upload_attachment():
    """Upload a file attachment"""
    # Check if the post request has the file part
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    file = request.files['file']
    conversation_id = request.form.get('conversation_id')
    
    # Check if a conversation ID was provided
    if not conversation_id:
        return jsonify({"error": "Conversation ID is required"}), 400
    
    # Check if a file was actually selected
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
    
    if file and allowed_file(file.filename):
        # Secure the filename and create a temporary file
        filename = secure_filename(file.filename)
        with tempfile.NamedTemporaryFile(delete=False) as temp:
            file.save(temp.name)
            
            # Process the file using the attachment handler
            mime_type = file.content_type or 'application/octet-stream'
            with open(temp.name, 'rb') as f:
                file_data = f.read()
            
            processed = attachment_handler.process_incoming_attachment(
                file_data, filename, mime_type
            )
            
            # Clean up the temporary file
            os.unlink(temp.name)
            
            # Check for processing errors
            if processed.get('error'):
                return jsonify({
                    "success": False,
                    "error": processed['error']
                }), 400
            
            # Create a message with the attachment
            message = {
                "conversationId": conversation_id,
                "content": {
                    "type": "attachment",
                    "value": f"Attachment: {filename}",
                    "data": processed['data'],
                    "filename": filename,
                    "mimeType": mime_type
                },
                "role": "user",
                "timestamp": int(request.form.get('timestamp', 0)),
                "source": "API"
            }
            
            # Add the message to the thread controller
            thread_controller = current_app.config.get('THREAD_CONTROLLER')
            if thread_controller:
                thread_controller.add_message(message)
                
                return jsonify({
                    "success": True,
                    "message": "Attachment uploaded successfully",
                    "filename": filename,
                    "mime_type": mime_type,
                    "size": len(processed['data']),
                    "conversation_id": conversation_id
                })
            else:
                return jsonify({"error": "Thread controller not configured"}), 500
    
    return jsonify({"error": "File type not allowed"}), 400

@bp.route('/attachment/<conversation_id>/<filename>', methods=['GET'])
def get_attachment(conversation_id, filename):
    """Retrieve a stored attachment"""
    thread_controller = current_app.config.get('THREAD_CONTROLLER')
    if not thread_controller:
        return jsonify({"error": "Thread controller not configured"}), 500
    
    # Query the database for the attachment
    conversation_db = thread_controller.conversation_db
    query = """
    SELECT attachment_data, attachment_metadata
    FROM messages_v2
    WHERE conversation_id = %s AND message_type = 'attachment' AND
    attachment_metadata LIKE %s
    ORDER BY timestamp DESC
    LIMIT 1
    """

    # execute_query returns a JSON string; BLOBs arrive Base64-encoded.
    result = conversation_db.execute_query(query, (conversation_id, f'%{filename}%'))

    try:
        data = json.loads(result)
        if not isinstance(data, list) or not data or not data[0].get('attachment_data'):
            return jsonify({"error": "Attachment data not found"}), 404
        
        # Get attachment metadata
        metadata = json.loads(data[0].get('attachment_metadata', '{}'))
        mime_type = metadata.get('mimeType', 'application/octet-stream')
        
        # Create a temporary file to serve
        temp_file = tempfile.NamedTemporaryFile(delete=False)
        temp_file.write(base64.b64decode(data[0]['attachment_data']))
        temp_file.close()
        
        # Serve the file and schedule it for deletion after the request
        @after_this_request
        def remove_file(response):
            try:
                os.unlink(temp_file.name)
            except Exception as e:
                logging.error(f"Error deleting temporary file: {e}")
            return response
        
        return send_file(
            temp_file.name,
            mimetype=mime_type,
            as_attachment=True,
            download_name=filename
        )
        
    except Exception as e:
        logging.error(f"Error retrieving attachment: {e}")
        return jsonify({"error": f"Error retrieving attachment: {str(e)}"}), 500

@bp.route('/authenticated_inboxes', methods=['GET'])
def get_authenticated_inboxes():
    """Get all authenticated inboxes"""
    thread_controller = current_app.config.get('THREAD_CONTROLLER')
    if not thread_controller:
        return jsonify({"error": "Thread controller not configured"}), 500
    
    conversation_db = thread_controller.conversation_db
    inboxes = conversation_db.get_authenticated_inboxes()
    
    return jsonify({
        "success": True,
        "count": len(inboxes),
        "inboxes": inboxes
    })

# --- Timers API ---

@bp.route('/api/timers/<conversation_id>', methods=['GET'])
def get_timers(conversation_id):
    """Get all active timers for a conversation"""
    thread_controller = current_app.config.get('THREAD_CONTROLLER')
    if not thread_controller:
        return jsonify({"error": "Thread controller not configured"}), 500

    conversation_db = thread_controller.conversation_db
    status = request.args.get('status', 'active')
    timers = conversation_db.get_timers_for_conversation(conversation_id, status=status)
    return jsonify({"success": True, "timers": timers})

@bp.route('/api/timers/<conversation_id>/cancel/<timer_id>', methods=['POST'])
def cancel_timer(conversation_id, timer_id):
    """Cancel a timer"""
    thread_controller = current_app.config.get('THREAD_CONTROLLER')
    if not thread_controller:
        return jsonify({"error": "Thread controller not configured"}), 500

    conversation_db = thread_controller.conversation_db
    timer = conversation_db.get_timer_by_id(timer_id)
    if not timer:
        return jsonify({"error": "Timer not found"}), 404
    if timer.get('conversation_id') != conversation_id:
        return jsonify({"error": "Timer does not belong to this conversation"}), 403
    if timer.get('status') != 'active':
        return jsonify({"error": f"Timer is already {timer.get('status')}"}), 400

    conversation_db.cancel_timer(timer_id)
    return jsonify({"success": True, "message": f"Timer '{timer_id}' cancelled"})

# --- Config API ---

# Runtime-editable Config keys = everything in OVERRIDE_TYPES (so the
# persisted-override surface and the runtime-editable surface are the same
# set), plus LLM_SERVICE which is special-cased because changing it triggers
# `chat_controller.change_service` to swap the live LLM client.
#
# Casting comes from OVERRIDE_TYPES too — single source of truth.
CONFIG_WHITELIST = list(OVERRIDE_TYPES.keys()) + ['LLM_SERVICE']
CONFIG_TYPES = {**OVERRIDE_TYPES, 'LLM_SERVICE': str}

@bp.route('/api/config', methods=['GET'])
def get_config():
    """Return whitelisted Config values"""
    values = {}
    for key in CONFIG_WHITELIST:
        values[key] = getattr(Config, key, None)
    return jsonify({"success": True, "config": values})

@bp.route('/api/config', methods=['POST'])
def update_config():
    """Update whitelisted Config values at runtime"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    updated = []
    errors = []
    # Snapshot any keys we touch so we can roll back if validation rejects them.
    snapshot = {}
    for key, value in data.items():
        if key not in CONFIG_WHITELIST:
            errors.append(f"'{key}' is not configurable")
            continue
        cast = CONFIG_TYPES.get(key, str)
        try:
            typed_value = cast(value)
        except (ValueError, TypeError) as e:
            errors.append(f"'{key}': invalid value ({e})")
            continue
        snapshot[key] = getattr(Config, key, None)
        setattr(Config, key, typed_value)
        updated.append(key)

    # If COMPACTION_PROVIDER changed, validate the new value+credentials. The
    # in-process compactor handles missing keys gracefully (falls back to
    # truncate-with-note), but the API caller deserves an explicit rejection.
    if 'COMPACTION_PROVIDER' in updated:
        try:
            validate_compaction_config()
        except ValueError as e:
            # Roll back COMPACTION_PROVIDER + COMPACTION_MODEL so the live
            # state matches what the API reports.
            for k in ('COMPACTION_PROVIDER', 'COMPACTION_MODEL'):
                if k in snapshot:
                    setattr(Config, k, snapshot[k])
            for k in ('COMPACTION_PROVIDER', 'COMPACTION_MODEL'):
                if k in updated:
                    updated.remove(k)
            errors.append(str(e))

    # If LLM_SERVICE changed, swap the live service
    if 'LLM_SERVICE' in updated:
        chat_controller = current_app.config.get('CHAT_CONTROLLER')
        if chat_controller:
            chat_controller.change_service(Config.LLM_SERVICE)

    # Persist to disk so settings survive restarts
    if updated:
        save_overrides(updated)

    return jsonify({"success": True, "updated": updated, "errors": errors})

# Error handlers
@bp.errorhandler(404)
def not_found_error(error):
    return {"error": "Not Found"}, 404

@bp.errorhandler(500)
def internal_error(error):
    return {"error": "Internal Server Error"}, 500
    
# Function to use in a decorator for cleaning up temporary files
def after_this_request(f):
    """Decorator to run a function after the request is complete"""
    if not hasattr(g, 'after_request_callbacks'):
        g.after_request_callbacks = []
    g.after_request_callbacks.append(f)
    return f

@bp.after_request
def call_after_request_callbacks(response):
    """Execute all registered after_request callbacks"""
    for callback in getattr(g, 'after_request_callbacks', ()):
        callback(response)
    return response
