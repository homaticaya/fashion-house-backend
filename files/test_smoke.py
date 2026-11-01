"""Offline smoke tests (no network). Run from the api folder:  pip install pytest && pytest"""
import hashlib, hmac, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.update(SUPABASE_URL="https://x.supabase.co", SUPABASE_ANON_KEY="anon", SUPABASE_SERVICE_ROLE_KEY="svc",
                  PAYSTACK_SECRET_KEY="sk_test_x", PAYMENT_CALLBACK_URL="https://site/", ALLOWED_ORIGIN="*")
from fastapi.testclient import TestClient
from app.main import app

c = TestClient(app)
STORE, PRODUCT = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


def sign(body: bytes) -> str:
    return hmac.new(b"sk_test_x", body, hashlib.sha512).hexdigest()


def test_health():
    assert c.get("/health").json() == {"ok": True}


def test_webhook_rejects_bad_signature():
    assert c.post("/paystack-webhook", content=b"{}", headers={"x-paystack-signature": "nope"}).status_code == 401


def test_webhook_ignores_unknown_events():
    body = json.dumps({"event": "something.else", "data": {}}).encode()
    assert c.post("/paystack-webhook", content=body, headers={"x-paystack-signature": sign(body)}).status_code == 200


def test_create_order_needs_login():
    r = c.post("/create-order", json={"store_id": STORE, "items": [{"product_id": PRODUCT, "quantity": 1}]})
    assert r.status_code == 401 and r.json()["error"] == "Sign in required"


def test_create_order_validates_input():
    r = c.post("/create-order", json={"store_id": "nope", "items": []}, headers={"Authorization": "Bearer x"})
    assert r.status_code == 400 and r.json()["error"] == "Invalid request"
    r = c.post("/create-order", json={"store_id": STORE, "items": [{"product_id": PRODUCT, "quantity": 0}]})
    assert r.status_code == 400
