-- Admin tools, stock reservation (15-minute hold), automatic cleanup, refunds.
alter table orders add column if not exists expires_at timestamptz;
alter table orders add column if not exists status_before_refund order_status;

-- ---------- stock reservation ----------
-- Stock is taken when the order is created (row-locked, so two buyers cannot both get the last item)
-- and given back if the order is cancelled, expires unpaid, or is refunded before shipping.
create or replace function create_order_reserved(p_customer uuid, p_store uuid, p_items jsonb, p_delivery jsonb, p_bps int, p_reference text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare it jsonb; pr products%rowtype; q int; total bigint := 0; oid uuid := gen_random_uuid(); commission bigint;
begin
  if not exists (select 1 from stores where id = p_store and status = 'approved' and paystack_subaccount_code is not null) then
    raise exception 'This store cannot accept payments yet';
  end if;
  if (select count(*) from orders where customer_id = p_customer and status = 'pending_payment' and expires_at > now()) >= 5 then
    raise exception 'You have too many unpaid orders. Pay or cancel one first';
  end if;
  insert into orders(id, customer_id, store_id, total_kobo, commission_kobo, payment_ref, delivery, expires_at)
    values (oid, p_customer, p_store, 0, 0, p_reference, p_delivery, now() + interval '15 minutes');
  for it in select e from jsonb_array_elements(p_items) e order by e->>'product_id' loop   -- fixed lock order avoids deadlocks
    q := (it->>'quantity')::int;
    select * into pr from products where id = (it->>'product_id')::uuid and store_id = p_store and is_active for update;
    if not found then raise exception 'Some items are unavailable'; end if;
    if pr.stock < q then raise exception 'Not enough stock for %', pr.name; end if;
    update products set stock = stock - q where id = pr.id;
    insert into order_items(order_id, product_id, name, unit_price_kobo, quantity) values (oid, pr.id, pr.name, pr.price_kobo, q);
    total := total + pr.price_kobo * q;
  end loop;
  commission := floor(total * p_bps / 10000.0);
  update orders set total_kobo = total, commission_kobo = commission where id = oid;
  return jsonb_build_object('order_id', oid, 'total_kobo', total, 'commission_kobo', commission);
end $$;

create or replace function release_order(p_order uuid) returns text
language plpgsql security definer set search_path = public as $$
declare o orders%rowtype;
begin
  select * into o from orders where id = p_order for update;
  if not found then return 'not_found'; end if;
  if o.status <> 'pending_payment' then return 'not_pending'; end if;
  update orders set status = 'cancelled' where id = o.id;
  update products p set stock = p.stock + oi.quantity from order_items oi where oi.order_id = o.id and oi.product_id = p.id;
  return 'released';
end $$;

create or replace function expire_unpaid_orders() returns int
language plpgsql security definer set search_path = public as $$
declare r record; n int := 0;
begin
  for r in select id from orders where status = 'pending_payment' and expires_at < now() loop
    perform release_order(r.id); n := n + 1;
  end loop;
  return n;
end $$;

create or replace function cancel_my_order(p_order uuid) returns text
language plpgsql security definer set search_path = public as $$
begin
  if not exists (select 1 from orders where id = p_order and customer_id = auth.uid()) then return 'not_found'; end if;
  return release_order(p_order);
end $$;

-- Payment confirmed. Stock was already reserved, so it is no longer reduced here.
-- A payment that arrives after the hold expired re-reserves the stock if it is still available.
create or replace function fulfill_order(p_reference text, p_amount_kobo bigint) returns text
language plpgsql security definer set search_path = public as $$
declare o orders%rowtype; short boolean;
begin
  select * into o from orders where payment_ref = p_reference for update;
  if not found then return 'not_found'; end if;
  if o.total_kobo <> p_amount_kobo then return 'amount_mismatch'; end if;
  if o.status = 'pending_payment' then
    update orders set status = 'paid', paid_at = now() where id = o.id;
    return 'ok';
  elsif o.status = 'cancelled' and o.paid_at is null then
    perform 1 from products p join order_items oi on oi.product_id = p.id where oi.order_id = o.id for update of p;
    select exists (select 1 from order_items oi left join products p on p.id = oi.product_id
                   where oi.order_id = o.id and (p.id is null or p.stock < oi.quantity)) into short;
    if short then return 'late_no_stock'; end if;
    update products p set stock = p.stock - oi.quantity from order_items oi where oi.order_id = o.id and oi.product_id = p.id;
    update orders set status = 'paid', paid_at = now() where id = o.id;
    return 'ok';
  end if;
  return 'already_processed';
end $$;

-- ---------- refunds ----------
create or replace function complete_refund(p_reference text) returns text
language plpgsql security definer set search_path = public as $$
declare o orders%rowtype; prev order_status;
begin
  select * into o from orders where payment_ref = p_reference for update;
  if not found then return 'not_found'; end if;
  if o.status = 'refunded' then return 'already_processed'; end if;
  prev := case when o.status = 'refund_pending' then o.status_before_refund else o.status end;
  update orders set status = 'refunded' where id = o.id;
  if prev in ('paid', 'processing') then          -- goods never left: put them back on the shelf
    update products p set stock = p.stock + oi.quantity from order_items oi where oi.order_id = o.id and oi.product_id = p.id;
  end if;
  return 'ok';
end $$;

create or replace function fail_refund(p_reference text) returns text
language plpgsql security definer set search_path = public as $$
begin
  update orders set status = coalesce(status_before_refund, 'paid'), status_before_refund = null
    where payment_ref = p_reference and status = 'refund_pending';
  return 'ok';
end $$;

-- ---------- admin ----------
create or replace function admin_list_stores()
returns table (id uuid, name text, area text, phone text, status store_status, owner_email text, product_count bigint, created_at timestamptz)
language plpgsql security definer set search_path = public as $$
begin
  if not is_admin() then raise exception 'Admins only'; end if;
  return query
    select s.id, s.name, s.area, s.phone, s.status, u.email::text,
           (select count(*) from products p where p.store_id = s.id), s.created_at
    from stores s join auth.users u on u.id = s.owner_id
    order by (s.status = 'pending') desc, s.created_at desc;
end $$;

create or replace function admin_set_store_status(p_store uuid, p_status store_status) returns void
language plpgsql security definer set search_path = public as $$
begin
  if not is_admin() then raise exception 'Admins only'; end if;
  update stores set status = p_status where id = p_store;
end $$;

-- Stores may progress an order but can no longer cancel a paid one (that is a refund, done through the refund function).
drop policy if exists "stores progress orders" on orders;
create policy "stores progress orders" on orders for update to authenticated
  using (owns_store(store_id) and status in ('paid','processing','shipped'))
  with check (owns_store(store_id) and status in ('processing','shipped','delivered'));

-- ---------- permissions ----------
revoke all on function create_order_reserved(uuid,uuid,jsonb,jsonb,int,text) from public, anon, authenticated;
revoke all on function release_order(uuid)           from public, anon, authenticated;
revoke all on function expire_unpaid_orders()        from public, anon, authenticated;
revoke all on function fulfill_order(text,bigint)    from public, anon, authenticated;
revoke all on function complete_refund(text)         from public, anon, authenticated;
revoke all on function fail_refund(text)             from public, anon, authenticated;
grant execute on function create_order_reserved(uuid,uuid,jsonb,jsonb,int,text), release_order(uuid), expire_unpaid_orders(),
                          fulfill_order(text,bigint), complete_refund(text), fail_refund(text) to service_role;
revoke all on function cancel_my_order(uuid), admin_list_stores(), admin_set_store_status(uuid,store_status) from public, anon;
grant execute on function cancel_my_order(uuid), admin_list_stores(), admin_set_store_status(uuid,store_status) to authenticated;

-- ---------- automatic cleanup every 5 minutes (needs the pg_cron extension) ----------
do $$ begin
  create extension if not exists pg_cron;
  perform cron.schedule('expire-unpaid-orders', '*/5 * * * *', 'select public.expire_unpaid_orders()');
exception when others then
  raise notice 'pg_cron not available. Enable it in Supabase > Database > Extensions, then run: select cron.schedule(''expire-unpaid-orders'',''*/5 * * * *'',''select public.expire_unpaid_orders()'');';
end $$;
