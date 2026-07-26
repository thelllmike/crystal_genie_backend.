-- Adds shipping/contact details to orders and lets place_order() store them.
-- Run once in the Supabase SQL editor. Safe to re-run.

alter table orders add column if not exists ship_name    text default '';
alter table orders add column if not exists ship_phone   text default '';
alter table orders add column if not exists ship_address text default '';
alter table orders add column if not exists ship_city    text default '';
alter table orders add column if not exists ship_postal  text default '';

-- Replace place_order with a version that records the shipping details passed
-- from checkout. All params default to '' so an argument-less call still works.
create or replace function place_order(
  ship_name    text default '',
  ship_phone   text default '',
  ship_address text default '',
  ship_city    text default '',
  ship_postal  text default ''
)
returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare
  new_order_id bigint;
  order_total numeric(10,2);
begin
  select coalesce(sum(p.price * c.quantity), 0)
    into order_total
    from cart_items c
    join products p on p.id = c.product_id
   where c.user_id = auth.uid();

  if order_total = 0 then
    raise exception 'Cart is empty';
  end if;

  insert into orders (
    user_id, total,
    ship_name, ship_phone, ship_address, ship_city, ship_postal
  )
  values (
    auth.uid(), order_total,
    ship_name, ship_phone, ship_address, ship_city, ship_postal
  )
  returning id into new_order_id;

  insert into order_items (order_id, product_name, unit_price, quantity)
  select new_order_id, p.name, p.price, c.quantity
    from cart_items c
    join products p on p.id = c.product_id
   where c.user_id = auth.uid();

  delete from cart_items where user_id = auth.uid();

  return new_order_id;
end;
$$;
