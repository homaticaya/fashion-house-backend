// Full refund of a paid order. Admins always; the store owner only if ALLOW_STORE_REFUNDS=true and the order has not shipped.
// The order moves to 'refund_pending'; the webhook marks it 'refunded' (and restocks unshipped goods) when Paystack confirms.
import { admin, cors, getUser, json, paystack } from "../_shared/util.ts";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const user = await getUser(req);
    if (!user) return json({ error: "Sign in required" }, 401);
    const { order_id } = await req.json();
    const db = admin();

    const { data: o } = await db.from("orders").select("id,status,store_id,payment_ref").eq("id", order_id).single();
    if (!o) return json({ error: "Order not found" }, 404);
    const { data: prof } = await db.from("profiles").select("role").eq("id", user.id).single();
    const { data: st } = await db.from("stores").select("owner_id").eq("id", o.store_id).single();

    const isAdmin = prof?.role === "admin";
    const storeOk = st?.owner_id === user.id && Deno.env.get("ALLOW_STORE_REFUNDS") === "true" && ["paid", "processing"].includes(o.status);
    if (!isAdmin && !storeOk) return json({ error: "You cannot refund this order" }, 403);
    if (!["paid", "processing", "shipped", "delivered"].includes(o.status)) return json({ error: "This order cannot be refunded" }, 400);

    // Compare-and-set so a double click can never start two refunds.
    const { data: upd } = await db.from("orders").update({ status: "refund_pending", status_before_refund: o.status })
      .eq("id", o.id).eq("status", o.status).select("id");
    if (!upd?.length) return json({ error: "The order just changed. Please try again" }, 409);

    try {
      await paystack("/refund", { method: "POST", body: JSON.stringify({
        transaction: o.payment_ref, customer_note: "Refund from Fashion House", merchant_note: `Requested by ${user.email}`,
      }) });
    } catch (e) {
      await db.from("orders").update({ status: o.status, status_before_refund: null }).eq("id", o.id);
      return json({ error: (e as Error).message }, 400);
    }
    return json({ ok: true });
  } catch (e) {
    return json({ error: (e as Error).message }, 400);
  }
});
