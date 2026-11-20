"""The blocks section of the system prompt (modules/prompts.blocks_note): each profile is
told about the blocks it can use, and only those.
    python tests/test_blocks_note.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from types import SimpleNamespace as P
from modules.prompts import blocks_note

fails = 0
def check(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += 0 if ok else 1

plain = blocks_note(P(send=False, public_profile='', admin_profile='', admin=False))
check("a plain profile knows [NO_REPLY]", "[NO_REPLY]" in plain)
check("a plain profile is not told about sending", "SEND_XMTP_MESSAGE" not in plain)
check("a plain profile is not told about admin turns", "ADMIN_TURN" not in plain and "Admin turn" not in plain)

main = blocks_note(P(send=True, public_profile='bot_public', admin_profile='', admin=True))
check("a sending profile gets the block's shape", "{INBOXID: <their inbox id>" in main)
check("it is told what the block sets up", "bot_public" in main and "folder of their own" in main)
check("it is told to ask when unsure", "ask before sending" in main)
check("a wakeable profile is told what an admin turn message is", "Admin turn asked by the public profile" in main)
check("a main is not told to raise admin turns", "alone on the last line" not in main)

no_public = blocks_note(P(send=True, public_profile='', admin_profile='', admin=False))
check("send without a public profile: the block, no folder talk", "SEND_XMTP_MESSAGE" in no_public and "folder" not in no_public)

api = blocks_note(P(send=True, public_profile='bot_public', admin_profile='', admin=False), claude_code=False)
check("on an API engine the send block is not offered", "SEND_XMTP_MESSAGE" not in api)

public = blocks_note(P(send=False, public_profile='', admin_profile='bot_main', admin=False))
check("a public profile is told how to ask its main", "[ADMIN_TURN] alone on the last line" in public)
check("and that notes are its own words", "never copy" in public)
check("a public profile is not told about sending", "SEND_XMTP_MESSAGE" not in public)

print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
