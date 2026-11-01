// Lets an approved store owner connect their bank account for payouts. Bank details go to Paystack only.
import { admin, cors, getUser, json, paystack } from "../_shared/util.ts";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const user = await getUser(req);
    if (!user) return json({ error: "Sign in required" }, 401);
    const { store_id, business_name, bank_code, account_number } = await req.json();
    if (!/^\d{10}$/.test(String(account_number)) || !bank_code || !business_name) return json({ error: "Invalid bank details" }, 400);

    const db = admin();
    const { data: store } = await db.from("stores").select("id,owner_id,status").eq("id", store_id).single();
    if (!store || store.owner_id !== user.id) return json({ error: "Not your store" }, 403);
    if (store.status !== "approved") return json({ error: "Store is awaiting approval" }, 403);

    const sub = await paystack("/subaccount", {
      method: "POST",
      body: JSON.stringify({ business_name, settlement_bank: bank_code, account_number, percentage_charge: 0 }),
    });
    await db.from("stores").update({ paystack_subaccount_code: sub.subaccount_code }).eq("id", store_id);
    return json({ subaccount_code: sub.subaccount_code });
  } catch (e) {
    return json({ error: (e as Error).message }, 400);
  }
});
