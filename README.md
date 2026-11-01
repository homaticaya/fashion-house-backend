# Fashion House backend

Stack: Supabase (Postgres, Auth, Storage, Edge Functions) + Paystack for payments in Naira.
Not yet run or deployed: test everything with Paystack TEST keys first.

## What it does
| Need | How |
|---|---|
| Customer and store logins | Supabase Auth (email/password, Google, phone OTP). Anyone can register; a user who creates a store becomes a store owner |
| Store approval | New stores start as `pending`. You approve them in the Supabase table editor (`stores.status = 'approved'`) |
| Product uploads | Approved stores insert into `products` and upload photos to bucket `product-images/<store_id>/...` (enforced by row level security) |
| Payments | `create-order` builds the order from database prices and returns a Paystack checkout URL; `paystack-webhook` confirms payment |
| Your commission | `COMMISSION_BPS` (default 500 = 5%), taken automatically on each sale; the rest goes to the store's bank account |
| Store payouts | `create-subaccount` registers the store's bank account with Paystack (bank details are never saved in your database) |

## Setup
1. Create a project at supabase.com. Install the Supabase CLI, then `supabase login` and `supabase link --project-ref <ref>`.
2. Apply the schema: `supabase db push` (or paste `001_schema.sql` into the SQL editor).
3. Create a Paystack account (paystack.com), complete business verification, and copy your **test** secret key.
4. Set secrets:
   `supabase secrets set PAYSTACK_SECRET_KEY=sk_test_... COMMISSION_BPS=500 PAYMENT_CALLBACK_URL=https://yoursite/thanks ALLOWED_ORIGIN=https://yoursite`
5. Deploy:
   `supabase functions deploy create-order`
   `supabase functions deploy create-subaccount`
   `supabase functions deploy paystack-webhook --no-verify-jwt`
6. In the Paystack dashboard (Settings > API Keys & Webhooks) set the webhook URL to
   `https://<ref>.supabase.co/functions/v1/paystack-webhook`.
7. Make yourself admin: after signing up, run `update profiles set role='admin' where id='<your user id>';`.

## Test flow
Sign up a store owner, create a store, approve it, call `create-subaccount`, add a product, sign up as a customer,
call `create-order`, pay with a Paystack test card, and confirm the order flips to `paid` and stock drops.

## Admin, stock hold, notifications, refunds (migrations 003 and 004)
- **Admin screen:** after `update profiles set role='admin' ...`, a 🛡️ button appears on the website. Admins approve, suspend or reset stores and see recent orders.
- **Stock hold:** stock is taken (row-locked) when an order is created and held for 15 minutes. Unpaid orders are cancelled automatically every 5 minutes (pg_cron) and the stock returns. Customers can also cancel an unpaid order. A payment that arrives after the hold ended re-takes the stock if available; if the item is gone the customer is refunded automatically. Each customer can hold at most 5 unpaid orders.
- **Notifications:** when an order is paid the store and the customer get an email, and both get one when a refund completes. Uses Resend (resend.com); without `RESEND_API_KEY` emails are skipped.
- **Refunds:** the `refund-order` function does full refunds. Admins always; store owners only if `ALLOW_STORE_REFUNDS=true` and the order has not shipped. The webhook completes the refund and restocks goods that never shipped. Stores can no longer cancel paid orders themselves.

New setup steps: run `003` then `004`; `supabase secrets set RESEND_API_KEY=re_... MAIL_FROM="Fashion House <orders@yourdomain.com>" ALLOW_STORE_REFUNDS=false`; deploy `refund-order` and redeploy `create-order` and `paystack-webhook`; confirm in Paystack that refund events reach the webhook; if the migration prints a pg_cron notice, enable it under Database > Extensions and run the `cron.schedule` line it shows.
Check with a Paystack TEST refund that (a) the webhook payload has `transaction_reference` and (b) a refund on a split-payment order behaves as you expect for the store's share. Paystack decides who bears a refund on split transactions.

## Still to build
- Privacy policy and terms (draft needed, then lawyer review).
- Email the store owner when their store is approved, and email customers when an order ships.
- Partial refunds, disputes, product reviews, delivery fees and tracking.

## Website (website/index.html)
The site talks to this backend with supabase-js. It must be hosted on your own domain (Netlify, Vercel, Cloudflare Pages or GitHub Pages all work for a single file); a claude.ai-published page cannot reach Supabase.
1. Run `002_store_view.sql` too (adds the `my_store` view and one store per owner).
2. In `website/index.html`, set `SUPABASE_URL` and `SUPABASE_ANON_KEY` (Supabase > Project Settings > API). Use the anon key only, never the service_role key.
3. Set `PAYMENT_CALLBACK_URL` to your site address and `ALLOWED_ORIGIN` to the same origin, then redeploy the functions.
4. Supabase > Authentication > URL Configuration: set Site URL to your site address. Decide whether email confirmation stays on.
5. Edit the `BRAND` block to change name, headline, colour and currency. Until the keys are set, the page runs in demo mode with sample items.
Store owners cannot see whether a payout account is already connected (the code is kept private); reconnecting simply replaces it.
