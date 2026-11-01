-- Lets a store owner read their own store row (including pending ones) without exposing owner_id publicly.
create unique index if not exists one_store_per_owner on stores(owner_id);
create view my_store as
  select id, name, slug, description, area, phone, whatsapp, logo_url, status
  from stores where owner_id = auth.uid();
revoke all on my_store from anon;
grant select on my_store to authenticated;
