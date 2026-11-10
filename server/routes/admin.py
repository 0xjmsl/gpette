"""Admin panel: where each conversation goes, and why. Read-only, this machine only.

    GET /admin                     the page
    GET /api/admin/routes          the last route of every conversation (filter, profile, gate, session)
    GET /api/admin/setup           the profiles, filter rules and gates this gpette runs with (no secrets)
    GET /api/admin/explain?conversation=<id>&sender=<inbox id>&kind=dm|group
                                   who WOULD handle such a message, without sending anything

Every route answers only requests from this machine (127.0.0.1 / ::1), whatever
[server] host is set to: the panel shows inbox ids and folders.
"""
import os

from flask import Blueprint, abort, current_app, jsonify, request, send_file

from config import Config
from modules import routes as route_log

bp = Blueprint('admin', __name__)
LOCAL = {'127.0.0.1', '::1', 'localhost'}
UI = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'ui', 'admin.html')


@bp.before_request
def _local_only():
    if (request.remote_addr or '') not in LOCAL:
        abort(403)


def _name(inbox_id):
    tc = current_app.config.get('THREAD_CONTROLLER')
    try:
        row = tc.conversation_db.get_resolved_contact(inbox_id) if tc and inbox_id else None
        return (row or {}).get('display_name')
    except Exception:
        return None


@bp.route('/admin')
def page():
    return send_file(UI)


@bp.route('/api/admin/routes')
def routes():
    rows = route_log.latest()
    for r in rows:
        r['sender_name'] = _name(r.get('sender'))
    return jsonify({'routes': rows})


@bp.route('/api/admin/setup')
def setup():
    profiles = {}
    for name, raw in (Config.PROFILES or {}).items():
        profiles[name] = {k: v for k, v in raw.items() if k not in ('system_prompt',)}
        if raw.get('system_prompt'):
            profiles[name]['system_prompt'] = f"({len(raw['system_prompt'])} characters)"
    gate = {'kind': Config.GATE_KIND, 'messages_per_person_per_day': Config.GATE_MESSAGES_PER_PERSON_PER_DAY}
    if Config.GATE_KIND == 'password':
        gate['new_people_per_day'] = Config.GATE_NEW_PEOPLE_PER_DAY      # never the password itself
    return jsonify({
        'provider': Config.LLM_SERVICE,
        'daemon': Config.XMTP_DAEMON_URL,
        'workdir': Config.CLAUDE_CODE_CWD,
        'group_workdir': Config.CLAUDE_CODE_GROUP_CWD,
        'allowed_tools': Config.CLAUDE_CODE_ALLOWED_TOOLS,
        'privacy': getattr(Config, 'PRIVACY', True),
        'sessions': {'routing': Config.SESSION_ROUTING,
                     'owner_resolver': getattr(Config, 'SESSION_OWNER_RESOLVER', '')},
        'gate': gate,
        'filter': Config.FILTER or {'default': 'answer', 'rules': []},
        'profiles': profiles,
    })


@bp.route('/api/admin/explain')
def explain():
    conv = request.args.get('conversation', '')
    sender = request.args.get('sender', '')
    kind = request.args.get('kind', 'dm')
    if not (conv or sender):
        return jsonify({'error': 'give conversation and/or sender'}), 400
    return jsonify(route_log.explain(conv, sender, kind))
