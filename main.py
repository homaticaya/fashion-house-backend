"""Fashion House API (FastAPI). Replaces the earlier TypeScript edge functions with the same behaviour."""
import hashlib
import hmac
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .core import (ApiError, check_settings, current_user, http, notify_order, one, paystack, rpc, select,
                   settings, update, upsert)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fashionhouse")


@asynccontextmanager
async def lifespan(_: FastAPI):
    check_settings()
    yield
    await http.aclose()


app = FastAPI(title="Fashion House API", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=settings.origins or ["*"], allow_methods=["POST", "GET", "OPTIONS"],
                   allow_headers=["authorization", "content-type"])


@app.exception_handler(ApiError)
async def _api_error(_, e: ApiError):
    return JSONResponse({"error": e.message}, status_code=e.status)


@app.exception_handler(RequestValidationError)
async def _invalid(_, e):
    return JSONResponse({"error": "Invalid request"}, status_code=400)


@app.exception_handler(Exception)
async def _unexpected(_, e: Exception):
    log.exception("Unhandled error")
    return JSONResponse({"error": "Something went wrong. Please try again"}, status_code=500)


@app.get("/health")
async def health():
    return {"ok": True}


# ---------------------------------------------------------------- create-order
class Item(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(ge=1, le=100)


class Delivery(BaseModel):
    name: str = Field(max_length=120)
    phone: str = Field(max_length=40)
    address: str = Field(max_length=300)


class OrderIn(BaseModel):
    store_id: uuid.UUID
    items: list[Item] = Field(min_length=1, max_length=50)
    delivery: Delivery | None = None


@app.post("/create-order")
async def create_order(body: OrderIn, authorization: str | None = Header(default=None)):
    """Creates an order for ONE store, holds the stock for 15 minutes, returns the Paystack checkout URL.
    Prices always come from the database, never from the browser."""
    user = await current_user(authorization)
    if not user.get("email"):
        raise ApiError("Sign in with an email account to pay", 401)

    qty: dict[str, int] = {}
    for i in body.items:
        qty[str(i.product_id)] = qty.get(str(i.product_id), 0) + i.quantity
    if any(q > 100 for q in qty.values()):
        raise ApiError("Invalid quantity")

    store = await one("stores", "paystack_subaccount_code", {"id": str(body.store_id)})
    if not store or not store.get("paystack_subaccount_code"):
        raise ApiError("This store cannot accept payments yet")

    reference = "FH-" + str(uuid.uuid4())
    order = await rpc("create_order_reserved", {
        "p_customer": user["id"], "p_store": str(body.store_id),
        "p_items": [{"product_id": p, "quantity": q} for p, q in qty.items()],
        "p_delivery": body.delivery.model_dump() if body.delivery else None,
        "p_bps": settings.commission_bps, "p_reference": reference,
    })   # raises "Not enough stock for ..." etc. when it cannot reserve
    try:
        init = await paystack("POST", "/transaction/initialize", {
            "email": user["email"], "amount": order["total_kobo"], "currency": "NGN", "reference": reference,
            "callback_url": settings.callback_url,
            "subaccount": store["paystack_subaccount_code"],
            "transaction_charge": order["commission_kobo"],       # flat amount (kobo) kept by the marketplace
            "metadata": {"order_id": order["order_id"]},
        })
    except Exception as e:
        await rpc("release_order", {"p_order": order["order_id"]})   # give the stock back
        if isinstance(e, ApiError):
            raise
        log.exception("Paystack initialize failed")
        raise ApiError("Could not reach the payment provider. Please try again", 502)
    return {"order_id": order["order_id"], "reference": reference, "authorization_url": init["authorization_url"]}


# ---------------------------------------------------------------- create-subaccount
class SubaccountIn(BaseModel):
    store_id: uuid.UUID
    business_name: str = Field(min_length=1, max_length=120)
    bank_code: str = Field(min_length=2, max_length=12)
    account_number: str


@app.post("/create-subaccount")
async def create_subaccount(body: SubaccountIn, authorization: str | None = Header(default=None)):
    """Lets an approved store owner connect a bank account for payouts. Bank details go to Paystack only."""
    user = await current_user(authorization)
    if not re.fullmatch(r"\d{10}", body.account_number):
        raise ApiError("Invalid bank details")
    store = await one("stores", "id,owner_id,status", {"id": str(body.store_id)})
    if not store or store["owner_id"] != user["id"]:
        raise ApiError("Not your store", 403)
    if store["status"] != "approved":
        raise ApiError("Store is awaiting approval", 403)
    sub = await paystack("POST", "/subaccount", {
        "business_name": body.business_name, "settlement_bank": body.bank_code,
        "account_number": body.account_number, "percentage_charge": 0})
    await update("stores", {"paystack_subaccount_code": sub["subaccount_code"]}, {"id": str(body.store_id)})
    return {"subaccount_code": sub["subaccount_code"]}


# ---------------------------------------------------------------- refund-order
class RefundIn(BaseModel):
    order_id: uuid.UUID


@app.post("/refund-order")
async def refund_order(body: RefundIn, authorization: str | None = Header(default=None)):
    """Full refund. Admins always; the store owner only if ALLOW_STORE_REFUNDS=true and the order has not shipped.
    The order becomes 'refund_pending'; the webhook marks it 'refunded' (and restocks unshipped goods) when Paystack confirms."""
    user = await current_user(authorization)
    oid = str(body.order_id)
    o = await one("orders", "id,status,store_id,payment_ref", {"id": oid})
    if not o:
        raise ApiError("Order not found", 404)
    prof = await one("profiles", "role", {"id": user["id"]}) or {}
    store = await one("stores", "owner_id", {"id": o["store_id"]}) or {}
    is_admin = prof.get("role") == "admin"
    store_ok = store.get("owner_id") == user["id"] and settings.store_refunds and o["status"] in ("paid", "processing")
    if not is_admin and not store_ok:
        raise ApiError("You cannot refund this order", 403)
    if o["status"] not in ("paid", "processing", "shipped", "delivered"):
        raise ApiError("This order cannot be refunded")

    # Compare-and-set, so a double click can never start two refunds.
    claimed = await update("orders", {"status": "refund_pending", "status_before_refund": o["status"]}, {"id": oid, "status": o["status"]})
    if not claimed:
        raise ApiError("The order just changed. Please try again", 409)
    try:
        await paystack("POST", "/refund", {"transaction": o["payment_ref"], "customer_note": "Refund from Fashion House",
                                           "merchant_note": f"Requested by {user.get('email')}"})
    except Exception as e:
        await update("orders", {"status": o["status"], "status_before_refund": None}, {"id": oid})
        if isinstance(e, ApiError):
            raise
        log.exception("Paystack refund failed")
        raise ApiError("Could not reach the payment provider. Please try again", 502)
    return {"ok": True}


# ---------------------------------------------------------------- paystack-webhook
@app.post("/paystack-webhook")
async def paystack_webhook(request: Request):
    raw = await request.body()
    given = request.headers.get("x-paystack-signature", "")
    expected = hmac.new(settings.paystack_secret.encode(), raw, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(given.encode(), expected.encode()):
        return PlainTextResponse("invalid signature", status_code=401)
    try:
        event = json.loads(raw)
    except ValueError:
        return PlainTextResponse("bad json", status_code=400)
    kind, d = event.get("event"), event.get("data") or {}

    if kind == "charge.success":
        if d.get("currency") != "NGN":
            return PlainTextResponse("ignored")
        ref, amount = d["reference"], d["amount"]
        await upsert("payments", {"reference": ref, "amount_kobo": amount, "status": "success", "raw": d}, "reference")
        result = await rpc("fulfill_order", {"p_reference": ref, "p_amount_kobo": amount})   # any error here -> 500 -> Paystack retries
        if result == "ok":
            await notify_order(ref, "paid")
        elif result == "late_no_stock":
            # Paid after the 15-minute hold ended and the item is gone: refund automatically.
            try:
                await paystack("POST", "/refund", {"transaction": ref, "merchant_note": "Paid after reservation expired; out of stock"})
                await update("orders", {"status": "refund_pending", "status_before_refund": "cancelled"}, {"payment_ref": ref})
            except Exception:
                log.exception("Automatic refund failed for %s", ref)
                return PlainTextResponse("retry", status_code=500)
        return PlainTextResponse(str(result))        # ok | already_processed | amount_mismatch | not_found | late_no_stock

    if kind in ("refund.processed", "refund.failed"):
        # Confirm the payload field names with a Paystack TEST refund; both known shapes are handled.
        ref = d.get("transaction_reference") or (d.get("transaction") or {}).get("reference")
        if not ref:
            return PlainTextResponse("no reference")
        if kind == "refund.processed":
            if await rpc("complete_refund", {"p_reference": ref}) == "ok":
                await notify_order(ref, "refunded")
        else:
            await rpc("fail_refund", {"p_reference": ref})
        return PlainTextResponse("ok")

    return PlainTextResponse("ignored")
