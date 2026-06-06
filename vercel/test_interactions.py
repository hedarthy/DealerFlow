"""Offline tests for the Vercel interactions function (no network, no Discord, no Vercel).

Run:  python vercel/test_interactions.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "api"))
import interactions as I  # noqa: E402

from nacl.signing import SigningKey  # noqa: E402


def _ok(msg):
    print(f"ok  {msg}")


def test_normalize_ticker():
    assert I.normalize_ticker(" tsla ") == ("TSLA", None)
    assert I.normalize_ticker("brk.b")[0] == "BRK.B"
    assert I.normalize_ticker("")[0] is None
    assert I.normalize_ticker("123")[0] is None
    assert I.normalize_ticker("TOOLONGX")[0] is None
    _ok("normalize_ticker (upper/trim, accept dotted, reject junk/empty/too-long)")


def test_verify_signature():
    sk = SigningKey.generate()
    pub_hex = sk.verify_key.encode().hex()
    ts = "1700000000"
    body = b'{"type":1}'
    sig_hex = sk.sign(ts.encode() + body).signature.hex()

    assert I.verify_signature(pub_hex, sig_hex, ts, body) is True
    # tampered body, wrong timestamp, garbage signature, and empty inputs all fail
    assert I.verify_signature(pub_hex, sig_hex, ts, b'{"type":2}') is False
    assert I.verify_signature(pub_hex, sig_hex, "1700000001", body) is False
    assert I.verify_signature(pub_hex, "00" * 64, ts, body) is False
    assert I.verify_signature(pub_hex, "", ts, body) is False
    assert I.verify_signature(SigningKey.generate().verify_key.encode().hex(), sig_hex, ts, body) is False
    _ok("verify_signature (valid passes; tamper/wrong-ts/bad-sig/wrong-key/empty fail)")


def test_ping():
    assert I.build_interaction_response({"type": 1}) == {"type": 1}
    _ok("build_interaction_response PING -> PONG")


def test_timestamp_freshness():
    now = 1_700_000_000.0
    assert I.timestamp_is_fresh(str(now), now=now) is True
    assert I.timestamp_is_fresh(str(now - 120), now=now) is True       # 2 min old: ok
    assert I.timestamp_is_fresh(str(now - 600), now=now) is False      # 10 min old: replay
    assert I.timestamp_is_fresh(str(now + 600), now=now) is False      # far future: reject
    assert I.timestamp_is_fresh("", now=now) is False
    assert I.timestamp_is_fresh("not-a-number", now=now) is False
    _ok("timestamp_is_fresh (accepts recent; rejects stale/future/garbage)")


def test_command_success(monkeypatched=None):
    orig = I.dispatch_workflow
    I.dispatch_workflow = lambda t: (True, "queued")
    try:
        os.environ.pop("TICKER_GEX_CHANNEL_ID", None)
        payload = {"type": 2, "channel_id": "555",
                   "data": {"name": "gex", "options": [{"name": "ticker", "value": "tsla"}]}}
        resp = I.build_interaction_response(payload)
        assert resp["type"] == 4
        assert resp["data"]["flags"] == 64
        assert "TSLA" in resp["data"]["content"]
    finally:
        I.dispatch_workflow = orig
    _ok("command -> dispatch ok -> ephemeral 'Queued TSLA' (type 4, flags 64)")


def test_command_invalid_ticker():
    os.environ.pop("TICKER_GEX_CHANNEL_ID", None)
    payload = {"type": 2, "data": {"name": "gex", "options": [{"name": "ticker", "value": "$$$"}]}}
    resp = I.build_interaction_response(payload)
    assert resp["type"] == 4 and resp["data"]["flags"] == 64
    assert "doesn't look like a ticker" in resp["data"]["content"]
    _ok("command -> invalid ticker -> ephemeral error, no dispatch")


def test_channel_restriction():
    orig = I.dispatch_workflow
    called = {"n": 0}

    def _spy(t):
        called["n"] += 1
        return True, "queued"

    I.dispatch_workflow = _spy
    os.environ["TICKER_GEX_CHANNEL_ID"] = "999"
    try:
        payload = {"type": 2, "channel_id": "111",
                   "data": {"name": "gex", "options": [{"name": "ticker", "value": "SPY"}]}}
        resp = I.build_interaction_response(payload)
        assert "999" in resp["data"]["content"]
        assert called["n"] == 0  # wrong channel must not dispatch
    finally:
        I.dispatch_workflow = orig
        os.environ.pop("TICKER_GEX_CHANNEL_ID", None)
    _ok("command in wrong channel -> redirected, workflow NOT dispatched")


def test_command_dispatch_failure():
    orig = I.dispatch_workflow
    I.dispatch_workflow = lambda t: (False, "GitHub returned 404")
    try:
        os.environ.pop("TICKER_GEX_CHANNEL_ID", None)
        payload = {"type": 2, "data": {"name": "gex", "options": [{"name": "ticker", "value": "QQQ"}]}}
        resp = I.build_interaction_response(payload)
        assert resp["type"] == 4 and resp["data"]["flags"] == 64
        assert "Couldn't start" in resp["data"]["content"] and "QQQ" in resp["data"]["content"]
    finally:
        I.dispatch_workflow = orig
    _ok("command -> dispatch fails -> ephemeral error surfaced")


def main():
    test_normalize_ticker()
    test_verify_signature()
    test_ping()
    test_timestamp_freshness()
    test_command_success()
    test_command_invalid_ticker()
    test_channel_restriction()
    test_command_dispatch_failure()
    print("\nAll vercel interaction tests passed.")


if __name__ == "__main__":
    main()
