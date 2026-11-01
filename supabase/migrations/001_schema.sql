-- Fashion House backend schema (Supabase / Postgres). Prices are stored in kobo (₦1 = 100 kobo).
create extension if not exists pgcrypto;

create type user_role    as enum ('customer','store','admin');
create type store_status as enum ('pending','approved','suspended');
create type order_status as enum ('pending_payment','paid','processing','shipped','delivered','cancelled');

-- ---------- profiles ----------
create table profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  role user_role not null default 'customer',
  full_name text, phone text,
  created_at timestamptz not null default now()
);
create function handle_new_user() returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into profiles(id, full_name) values (new.id, new.raw_user_meta_data->>'full_name');
  return new;
end $$;
create trigger on_auth_user_created after insert on auth.users
  for each row execute function handle_new_user();

create function is_admin() returns boolean language sql stable security definer set search_path = public as $$
  select exists(select 1 from profiles where id = auth.uid() and role = 'admin') $$;

-- ---------- stores ----------
create table stores (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references auth.users(id) on delete cascade,
  name text not null, slug text not null unique,
  description text, area text, phone text, whatsapp text, logo_url text,
  status store_status not null default 'pending',
  paystack_subaccount_code text,            -- payout destination; bank details are NOT stored here
  created_at timestamptz not null default now()
);
create function owns_store(sid uuid) returns boolean language sql stable security definer set search_path = public as $$
  select exists(select 1 from stores where id = sid and owner_id = auth.uid()) $$;
create function owns_approved_store(sid uuid) returns boolean language sql stable security definer set search_path = public as $$
  select exists(select 1 from stores where id = sid and owner_id = auth.uid() and status = 'approved') $$;

create function promote_store_owner() returns trigger language plpgsql security definer set search_path = public as $$
begin update profiles set role = 'store' where id = new.owner_id and role = 'customer'; return new; end $$;
create trigger on_store_created after insert on stores for each row execute function promote_store_owner();

-- ---------- catalogue ----------
create table categories (
  id serial primary key, name text not null unique
);
insert into categories(name) values ('Fabrics'),('Bags'),('Shoes'),('Accessories'),('Ready-to-wear');

create table products (
  id uuid primary key default gen_random_uuid(),
  store_id uuid not null references stores(id) on delete cascade,
  category_id int references categories(id),
  name text not null, description text,
  price_kobo bigint not null check (price_kobo >= 0),
  stock int not null default 0 check (stock >= 0),
  images text[] not null default '{}',
  is_active boolean not null default true,
  created_at timestamptz not null default now()
);
create index on products(store_id); create index on products(category_id);

-- ---------- orders (one order per store) ----------
create table orders (
  id uuid primary key default gen_random_uuid(),
  customer_id uuid not null references auth.users(id),
  store_id uuid not null references stores(id),
  status order_status not null default 'pending_payment',
  total_kobo bigint not null, commission_kobo bigint not null default 0,
  payment_ref text unique, delivery jsonb,
  created_at timestamptz not null default now(), paid_at timestamptz
);
create table order_items (
  id uuid primary key default gen_random_uuid(),
  order_id uuid not null references orders(id) on delete cascade,
  product_id uuid references products(id) on delete set null,
  name text not null, unit_price_kobo bigint not null, quantity int not null check (quantity > 0)
);
create table payments (
  id uuid primary key default gen_random_uuid(),
  reference text not null unique, amount_kobo bigint not null,
  status text not null, raw jsonb, created_at timestamptz not null default now()
);

-- Marks an order paid exactly once, checks the amount, and reduces stock. Called only by the webhook.
create function fulfill_order(p_reference text, p_amount_kobo bigint) returns text
language plpgsql security definer set search_path = public as $$
declare o orders%rowtype;
begin
  select * into o from orders where payment_ref = p_reference for update;
  if not found then return 'not_found'; end if;
  if o.status <> 'pending_payment' then return 'already_processed'; end if;
  if o.total_kobo <> p_amount_kobo then return 'amount_mismatch'; end if;
  update orders set status = 'paid', paid_at = now() where id = o.id;
  update products p set stock = greatest(p.stock - oi.quantity, 0)
    from order_items oi where oi.order_id = o.id and oi.product_id = p.id;
  return 'ok';
end $$;
revoke all on function fulfill_order(text, bigint) from public, anon, authenticated;
grant execute on function fulfill_order(text, bigint) to service_role;

-- ---------- row level security ----------
alter table profiles    enable row level security;
alter table stores      enable row level security;
alter table categories  enable row level security;
alter table products    enable row level security;
alter table orders      enable row level security;
alter table order_items enable row level security;
alter table payments    enable row level security;   -- no policies: service role only

create policy "own profile" on profiles for select using (id = auth.uid() or is_admin());
revoke update on profiles from authenticated;
grant update (full_name, phone) on profiles to authenticated;
create policy "edit own profile" on profiles for update using (id = auth.uid());

create policy "view stores" on stores for select
  using (status = 'approved' or owner_id = auth.uid() or is_admin());
create policy "create own store" on stores for insert to authenticated
  with check (owner_id = auth.uid() and status = 'pending');
create policy "edit own store" on stores for update using (owner_id = auth.uid());
revoke select, insert, update on stores from anon, authenticated;
grant select (id,name,slug,description,area,phone,whatsapp,logo_url,status,created_at) on stores to anon, authenticated;
grant insert (owner_id,name,slug,description,area,phone,whatsapp,logo_url) on stores to authenticated;
grant update (name,description,area,phone,whatsapp,logo_url) on stores to authenticated;

create policy "view categories" on categories for select using (true);

create policy "view products" on products for select
  using ((is_active and exists (select 1 from stores s where s.id = store_id and s.status = 'approved')) or owns_store(store_id));
create policy "add products"    on products for insert to authenticated with check (owns_approved_store(store_id));
create policy "edit products"   on products for update to authenticated using (owns_approved_store(store_id));
create policy "delete products" on products for delete to authenticated using (owns_store(store_id));

create policy "view orders" on orders for select using (customer_id = auth.uid() or owns_store(store_id) or is_admin());
revoke update on orders from authenticated;
grant update (status) on orders to authenticated;
create policy "stores progress orders" on orders for update to authenticated
  using (owns_store(store_id) and status in ('paid','processing','shipped'))
  with check (status in ('processing','shipped','delivered','cancelled'));
create policy "view order items" on order_items for select using (exists (select 1 from orders o where o.id = order_id));

-- ---------- product photos ----------
insert into storage.buckets (id, name, public) values ('product-images','product-images', true) on conflict do nothing;
create policy "public read images" on storage.objects for select using (bucket_id = 'product-images');
create policy "owners upload images" on storage.objects for insert to authenticated
  with check (bucket_id = 'product-images' and owns_store(((storage.foldername(name))[1])::uuid));
create policy "owners delete images" on storage.objects for delete to authenticated
  using (bucket_id = 'product-images' and owns_store(((storage.foldername(name))[1])::uuid));
-- Upload path convention: product-images/<store_id>/<filename>
