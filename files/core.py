"""Shared plumbing: settings, Supabase REST access, Paystack, email."""
import html
import logging
import os

import httpx

log = logging.getLogger("fashionhouse")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Settings:
    """Read from the environment at call time."""
    supabase_url = property(lambda s: _env("SUPABASE_URL").rstrip("/"))
    anon_key = property(lambda s: _env("SUPABASE_ANON_KEY"))
    service_key = property(lambda s: _env("SUPABASE_SERVICE_ROLE_KEY"))
    paystack_secret = property(lambda s: _env("PAYSTACK_SECRET_KEY"))
    callback_url = property(lambda s: _env("PAYMENT_CALLBACK_URL"))
    origins = property(lambda s: [o.strip() for o in _env("ALLOWED_ORIGIN").split(",") if o.strip()])
    commission_bps = property(lambda s: int(_env("COMMISSION_BPS", "500")))
    store_refunds = property(lambda s: _env("ALLOW_STORE_REFUNDS").lower() == "true")
    resend_key = property(lambda s: _env("RESEND_API_KEY"))
    mail_from = property(lambda s: _env("MAIL_FROM", "Fashion House <orders@example.com>"))


settings = Settings()
http = httpx.AsyncClient(timeout=20.0)


def check_settings() -> None:
    names = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY", "PAYSTACK_SECRET_KEY", "PAYMENT_CALLBACK_URL", "ALLOWED_ORIGIN"]
    missing = [n for n in names if not _env(n)]
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))


# ---------- Supabase (PostgREST + Auth over HTTPS, using the service-role key) ----------
def _svc(prefer: str | None = None) -> dict:
    k = settings.service_key
    h = {"apikey": k, "Authorization": f"Bearer {k}"}
    if prefer:
        h["Prefer"] = prefer
    return h


async def rest(method: str, path: str, *, params=None, json=None, prefer=None):
    r = await http.request(method, f"{settings.supabase_url}/rest/v1/{path}", params=params, json=json, headers=_svc(prefer))
    if r.status_code >= 400:
        try:
            j = r.json()
        except ValueError:
            j = {}
        if j.get("code") == "P0001":          # a deliberate `raise exception` in our SQL, safe to show
            raise ApiError(j.get("message", "Request failed"), 400)
        log.error("Supabase error %s on %s: %s", r.status_code, path, r.text[:300])
        raise ApiError("Something went wrong. Please try again", 500)
    return r.json() if r.content else None


def _eq(where: dict | None) -> dict:
    return {k: f"eq.{v}" for k, v in (where or {}).items()}


async def select(table: str, cols: str, where: dict | None = None, limit: int | None = None) -> list:
    params = {"select": cols, **_eq(where)}
    if limit:
        params["limit"] = str(limit)
    return await rest("GET", table, params=params) or []


async def one(table: str, cols: str, where: dict) -> dict | None:
    rows = await select(table, cols, where, 1)
    return rows[0] if rows else None


async def update(table: str, values: dict, where: dict) -> list:
    return await rest("PATCH", table, params=_eq(where), json=values, prefer="return=representation") or []


async def upsert(table: str, row: dict, on_conflict: str) -> None:
    await rest("POST", table, params={"on_conflict": on_conflict}, json=row, prefer="resolution=merge-duplicates")


async def rpc(fn: str, args: dict):
    return await rest("POST", f"rpc/{fn}", json=args)


async def current_user(authorization: str | None) -> dict:
    """Validates the customer's Supabase login token and returns their user record."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError("Sign in required", 401)
    r = await http.get(f"{settings.supabase_url}/auth/v1/user",
                       headers={"apikey": settings.anon_key, "Authorization": "Bearer " + authorization[7:].strip()})
    if r.status_code != 200:
        raise ApiError("Sign in required", 401)
    return r.json()


async def user_email(user_id: str | None) -> str | None:
    if not user_id:
        return None
    r = await http.get(f"{settings.supabase_url}/auth/v1/admin/users/{user_id}", headers=_svc())
    return r.json().get("email") if r.status_code == 200 else None


# ---------- Paystack ----------
async def paystack(method: str, path: str, payload: dict | None = None) -> dict:
    r = await http.request(method, "https://api.paystack.co" + path, json=payload,
                           headers={"Authorization": f"Bearer {settings.paystack_secret}"})
    try:
        b = r.json()
    except ValueError:
        b = {}
    if r.status_code >= 400 or not b.get("status"):
        raise ApiError(b.get("message") or "Payment provider error", 400)
    return b.get("data") or {}


# ---------- email (Resend). Skipped silently when RESEND_API_KEY is empty ----------
def naira(kobo: int) -> str:
    return f"₦{kobo / 100:,.2f}"


async def send_email(to: str | None, subject: str, body: str) -> None:
    if not settings.resend_key or not to:
        return
    try:
        await http.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {settings.resend_key}"},
                        json={"from": settings.mail_from, "to": [to], "subject": subject, "html": body})
    except Exception:                      # an email problem must never break payment handling
        log.warning("Email to %s failed", to)


async def notify_order(reference: str, kind: str) -> None:
    """kind is 'paid' or 'refunded'. Emails the store owner and the customer."""
    try:
        o = await one("orders", "total_kobo,customer_id,store_id,delivery,order_items(name,quantity)", {"payment_ref": reference})
        if not o:
            return
        store = await one("stores", "name,owner_id", {"id": o["store_id"]}) or {}
        store_to, buyer_to = await user_email(store.get("owner_id")), await user_email(o["customer_id"])
        e = lambda v: html.escape(str(v or ""))
        lines = "".join(f"<li>{i['quantity']} x {e(i['name'])}</li>" for i in o.get("order_items") or [])
        d, total = o.get("delivery") or {}, naira(o["total_kobo"])
        if kind == "paid":
            await send_email(store_to, f"New paid order: {total}",
                f"<p>You have a new paid order.</p><ul>{lines}</ul><p><b>Total:</b> {total}</p>"
                f"<p><b>Deliver to:</b> {e(d.get('name'))}, {e(d.get('phone'))}, {e(d.get('address'))}</p>"
                "<p>Open the site and go to Orders to update its status.</p>")
            await send_email(buyer_to, f"Payment received · {store.get('name') or 'Fashion House'}",
                f"<p>Thank you! We received your payment of {total}.</p><ul>{lines}</ul>"
                f"<p>{e(store.get('name'))} will prepare your order. Follow its status under Orders.</p>")
        else:
            await send_email(buyer_to, f"Refund processed · {total}",
                f"<p>Your refund of {total} has been processed. It can take a few days to reach your account.</p><ul>{lines}</ul>")
            await send_email(store_to, f"Order refunded: {total}", f"<p>An order was refunded.</p><ul>{lines}</ul>")
    except Exception:
        log.warning("Order notification failed for %s", reference)
