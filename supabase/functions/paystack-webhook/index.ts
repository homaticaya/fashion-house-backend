// Paystack calls this after payments and refunds. Deploy with --no-verify-jwt (Paystack has no Supabase login).
import { admin, notifyOrder, paystack } from "../_shared/util.ts";

const enc = new TextEncoder();
async function hmacSha512(body: string) {
  const key = await crypto.subtle.importKey("raw", enc.encode(Deno.env.get("PAYSTACK_SECRET_KEY")!),
    { name: "HMAC", hash: "SHA-512" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(body));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function safeEqual(a: string, b: string) {
  if (a.length !== b.length) return false;
  let d = 0; for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

Deno.serve(async (req) => {
  const raw = await req.text();
  const given = req.headers.get("x-paystack-signature") ?? "";
  if (!safeEqual(given, await hmacSha512(raw))) return new Response("invalid signature", { status: 401 });

  const event = JSON.parse(raw);
  const d = event.data ?? {};
  const db = admin();

  if (event.event === "charge.success") {
    if (d.currency !== "NGN") return new Response("ignored", { status: 200 });
    await db.from("payments").upsert({ reference: d.reference, amount_kobo: d.amount, status: "success", raw: d }, { onConflict: "reference" });
    const { data: result, error } = await db.rpc("fulfill_order", { p_reference: d.reference, p_amount_kobo: d.amount });
    if (error) return new Response("retry", { status: 500 });          // non-200 makes Paystack retry
    if (result === "ok") await notifyOrder(db, d.reference, "paid");
    if (result === "late_no_stock") {
      // Customer paid after the 15-minute hold ended and the item is gone: refund automatically.
      try {
        await paystack("/refund", { method: "POST", body: JSON.stringify({ transaction: d.reference, merchant_note: "Paid after reservation expired; out of stock" }) });
        await db.from("orders").update({ status: "refund_pending", status_before_refund: "cancelled" }).eq("payment_ref", d.reference);
      } catch (_) { return new Response("retry", { status: 500 }); }
    }
    return new Response(String(result), { status: 200 });
  }

  if (event.event === "refund.processed" || event.event === "refund.failed") {
    // Verify the payload field names with a Paystack TEST refund; both shapes are handled here.
    const ref = d.transaction_reference ?? d.transaction?.reference;
    if (!ref) return new Response("no reference", { status: 200 });
    if (event.event === "refund.processed") {
      const { data: r, error } = await db.rpc("complete_refund", { p_reference: ref });
      if (error) return new Response("retry", { status: 500 });
      if (r === "ok") await notifyOrder(db, ref, "refunded");
    } else {
      await db.rpc("fail_refund", { p_reference: ref });
    }
    return new Response("ok", { status: 200 });
  }

  return new Response("ignored", { status: 200 });
});
