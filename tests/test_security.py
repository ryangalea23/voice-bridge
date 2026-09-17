"""The three gates still fail closed: caller allowlist, stream ticket, session token."""
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import bridge

client = TestClient(bridge.app)
FORM = {"content-type": "application/x-www-form-urlencoded"}


def test_non_allowlisted_caller_rejected():
    r = client.post("/twilio/voice", content="From=%2B19998887777&CallSid=CA1", headers=FORM)
    assert r.status_code == 200
    assert "not authorized" in r.text and "<Hangup/>" in r.text
    assert "<Stream" not in r.text


def test_allowlisted_caller_gets_stream_with_ticket():
    r = client.post("/twilio/voice", content="From=%2B15551112222&CallSid=CA2", headers=FORM)
    assert "<Stream" in r.text and 'name="ticket"' in r.text


def test_websocket_without_ticket_closed_1008():
    with client.websocket_connect("/twilio/stream") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ1", "customParameters": {}}}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 1008


def test_websocket_with_forged_ticket_closed_1008():
    with client.websocket_connect("/twilio/stream?ticket=forged") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ2", "customParameters": {"ticket": "forged"}}}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 1008


def test_session_routes_without_token_403():
    assert client.post("/session/register", json={"session_num": 1, "hwnd": "1"}).status_code == 403
    assert client.post("/session/register", json={"session_num": 1, "hwnd": "1"},
                       headers={"X-Bridge-Token": "wrong"}).status_code == 403
    assert client.post("/session/deregister", json={"session_num": 1}).status_code == 403
