import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.4";

export const cors = {
  "Access-Control-Allow-Origin": Deno.env.get("ALLOWED_ORIGIN") ?? "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};
export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { ...cors, "Content-Type": "application/json" } });

// Service-role client: bypasses row level security. Never expose to the browser or app.
export const admin = () =>
  createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);

export async function getUser(req: Request) {
  const c = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_ANON_KEY")!, {
    global: { headers: { Authorization: req.headers.get("Authorization") ?? "" } },
  });
  const { data: { user } } = await c.auth.getUser();
  return user;
}

export async function paystack(path: string, init: RequestInit = {}) {
  const r = await fetch("https://api.paystack.co" + path, {
    ...init,
    headers: { Authorization: `Bearer ${Deno.env.get("PAYSTACK_SECRET_KEY")}`, "Content-Type": "application/json" },
  });
  const b = await r.json();
  if (!r.ok || !b.status) throw new Error(b.message ?? "Payment provider error");
  return b.data;
}

// ---------- email (Resend). Skipped silently when RESEND_API_KEY is not set ----------
export const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!));
export const naira = (kobo: number) => "₦" + (kobo / 100).toLocaleString("en-NG");

export async function sendEmail(to: string | undefined, subject: string, html: string) {
  const key = Deno.env.get("RESEND_API_KEY");
  if (!key || !to) return;
  try {
    await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
      body: JSON.stringify({ from: Deno.env.get("MAIL_FROM") ?? "Fashion House <orders@example.com>", to: [to], subject, html }),
    });
  } catch (_) { /* never let an email problem break payment handling */ }
}

async function userEmail(db: ReturnType<typeof admin>, id?: string) {
  if (!id) return undefined;
  const { data } = await db.auth.admin.getUserById(id);
  return data?.user?.email ?? undefined;
}

export async function notifyOrder(db: ReturnType<typeof admin>, reference: string, kind: "paid" | "refunded") {
  try {
    const { data: o } = await db.from("orders").select("total_kobo,customer_id,store_id,delivery,order_items(name,quantity)").eq("payment_ref", reference).single();
    if (!o) return;
    const { data: s } = await db.from("stores").select("name,owner_id").eq("id", o.store_id).single();
    const [storeTo, buyerTo] = await Promise.all([userEmail(db, s?.owner_id), userEmail(db, o.customer_id)]);
    const lines = (o.order_items ?? []).map((i: any) => `<li>${i.quantity} x ${esc(i.name)}</li>`).join("");
    const d: any = o.delivery ?? {};
    const total = naira(o.total_kobo);
    if (kind === "paid") {
      await sendEmail(storeTo, `New paid order: ${total}`,
        `<p>You have a new paid order.</p><ul>${lines}</ul><p><b>Total:</b> ${total}</p><p><b>Deliver to:</b> ${esc(d.name)}, ${esc(d.phone)}, ${esc(d.address)}</p><p>Open the site and go to Orders to update its status.</p>`);
      await sendEmail(buyerTo, `Payment received · ${s?.name ?? "Fashion House"}`,
        `<p>Thank you! We received your payment of ${total}.</p><ul>${lines}</ul><p>${esc(s?.name)} will prepare your order. You can follow its status under Orders.</p>`);
    } else {
      await sendEmail(buyerTo, `Refund processed · ${total}`, `<p>Your refund of ${total} has been processed. It can take a few days to reach your account.</p><ul>${lines}</ul>`);
      await sendEmail(storeTo, `Order refunded: ${total}`, `<p>An order was refunded.</p><ul>${lines}</ul>`);
    }
  } catch (_) { /* ignore */ }
}
