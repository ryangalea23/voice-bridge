"""Test setup. Dummy env so bridge.py imports without real keys, a phone or
the network. Nothing here talks to Twilio, Deepgram or Anthropic."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-twilio-token")
os.environ.setdefault("DEEPGRAM_API_KEY", "test-deepgram-key")
os.environ["VALIDATE_TWILIO"] = "false"
os.environ["ALLOWED_CALLERS"] = "+15551112222"
os.environ["SESSION_TOKEN"] = "test-session-token"
os.environ["TYPING_SOUND"] = "0"
