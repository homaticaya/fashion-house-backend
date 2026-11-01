// Creates an order for ONE store, reserves the stock for 15 minutes, and returns a Paystack checkout URL.
// Prices always come from the database, never from the client.
import { admin, cors, getUser, json, paystack } from "../_shared/util.ts";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const user = await getUser(req);
    if (!user?.email) return json({ error: "Sign in with an email account to pay" }, 401);

    const { store_id, items, delivery } = await req.json();
    if (!store_id || !Array.isArray(items) || items.length < 1 || items.length > 50) return json({ error: "Invalid cart" }, 400);

    const qty = new Map<string, number>();
    for (const i of items) {
      const q = Number(i.quantity);
      if (!i.product_id || !Number.isInteger(q) || q < 1 || q > 100) return json({ error: "Invalid quantity" }, 400);
      qty.set(i.product_id, (qty.get(i.product_id) ?? 0) + q);
    }

    const db = admin();
    const { data: store } = await db.from("stores").select("paystack_subaccount_code").eq("id", store_id).single();
    if (!store?.paystack_subaccount_code) return json({ error: "This store cannot accept payments yet" }, 400);

    const reference = "FH-" + crypto.randomUUID();
    const rpc = await db.rpc("create_order_reserved", {
      p_customer: user.id, p_store: store_id,
      p_items: [...qty].map(([product_id, quantity]) => ({ product_id, quantity })),
      p_delivery: delivery ?? null,
      p_bps: Number(Deno.env.get("COMMISSION_BPS") ?? "500"),     // 500 = 5%
      p_reference: reference,
    });
    if (rpc.error) return json({ error: rpc.error.message }, 400);   // e.g. "Not enough stock for ..."
    const { order_id, total_kobo, commission_kobo } = rpc.data;

    try {
      const init = await paystack("/transaction/initialize", {
        method: "POST",
        body: JSON.stringify({
          email: user.email, amount: total_kobo, currency: "NGN", reference,
          callback_url: Deno.env.get("PAYMENT_CALLBACK_URL"),
          subaccount: store.paystack_subaccount_code,
          transaction_charge: commission_kobo,
          metadata: { order_id },
        }),
      });
      return json({ order_id, reference, authorization_url: init.authorization_url });
    } catch (e) {
      await db.rpc("release_order", { p_order: order_id });          // give the stock back
      throw e;
    }
  } catch (e) {
    return json({ error: (e as Error).message }, 400);
  }
});
