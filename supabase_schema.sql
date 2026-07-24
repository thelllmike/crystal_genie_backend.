-- Run this in the Supabase SQL editor (Dashboard -> SQL Editor -> New query)

-- Crystal catalog: one row per gem class your YOLO model can detect.
-- "name" must match the YOLO class name (case-insensitive).
create table if not exists crystals (
  id bigint generated always as identity primary key,
  name text not null unique,
  headline text,
  description text,
  star_sign text,
  chakras text,
  created_at timestamptz not null default now()
);

-- Detection history: the backend logs the top detection of every request.
create table if not exists detections (
  id bigint generated always as identity primary key,
  crystal_name text not null,
  confidence real not null,
  detected_at timestamptz not null default now()
);

-- The backend authenticates with the anon key, and the app reads the catalog
-- as a signed-in user, so BOTH roles need select. Granting only to anon makes
-- the crystal library come back empty for every logged-in user.
alter table crystals enable row level security;
drop policy if exists "anon can read crystals" on crystals;
drop policy if exists "anyone can read crystals" on crystals;
create policy "anyone can read crystals"
  on crystals for select to anon, authenticated using (true);

alter table detections enable row level security;
drop policy if exists "anon can log detections" on detections;
create policy "anon can log detections"
  on detections for insert to anon with check (true);

-- Example seed row (repeat for each of your gem classes, or import your CSV
-- via Dashboard -> Table Editor -> crystals -> Insert -> Import data from CSV):
-- insert into crystals (name, headline, description, star_sign, chakras) values
--   ('Amethyst', 'The stone of calm', 'Amethyst is a violet quartz...', 'Pisces', 'Crown, Third Eye');

-- ============================================================
-- App tables (auth users, finds, favorites, shop, cart, orders)
-- ============================================================

-- Detection history per signed-in user (shown on the home screen).
create table if not exists finds (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  crystal_name text not null,
  headline text default '',
  created_at timestamptz not null default now()
);
alter table finds enable row level security;
drop policy if exists "users manage own finds" on finds;
create policy "users manage own finds" on finds
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

-- Bookmarked crystals ("Favorites" tab / bookmark on the result screen).
create table if not exists saved_crystals (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  crystal_name text not null,
  headline text default '',
  created_at timestamptz not null default now(),
  unique (user_id, crystal_name)
);
alter table saved_crystals enable row level security;
drop policy if exists "users manage own saved crystals" on saved_crystals;
create policy "users manage own saved crystals" on saved_crystals
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

-- Shop catalog.
create table if not exists products (
  id bigint generated always as identity primary key,
  name text not null unique,
  headline text default '',
  description text default '',
  price numeric(10,2) not null,
  image_url text,
  stock int not null default 0,
  created_at timestamptz not null default now()
);
alter table products enable row level security;
drop policy if exists "anyone can browse products" on products;
create policy "anyone can browse products" on products
  for select to anon, authenticated using (true);

-- Cart, one row per user+product.
create table if not exists cart_items (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  product_id bigint not null references products on delete cascade,
  quantity int not null default 1 check (quantity > 0),
  created_at timestamptz not null default now(),
  unique (user_id, product_id)
);
alter table cart_items enable row level security;
drop policy if exists "users manage own cart" on cart_items;
create policy "users manage own cart" on cart_items
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

-- Orders + line items.
create table if not exists orders (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  total numeric(10,2) not null,
  status text not null default 'pending',
  created_at timestamptz not null default now()
);
alter table orders enable row level security;
drop policy if exists "users see own orders" on orders;
create policy "users see own orders" on orders
  for select to authenticated using (user_id = auth.uid());

create table if not exists order_items (
  id bigint generated always as identity primary key,
  order_id bigint not null references orders on delete cascade,
  product_name text not null,
  unit_price numeric(10,2) not null,
  quantity int not null
);
alter table order_items enable row level security;
drop policy if exists "users see own order items" on order_items;
create policy "users see own order items" on order_items
  for select to authenticated
  using (exists (
    select 1 from orders o
    where o.id = order_items.order_id and o.user_id = auth.uid()
  ));

-- Checkout: atomically turns the caller's cart into an order.
create or replace function place_order()
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

  insert into orders (user_id, total)
  values (auth.uid(), order_total)
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

revoke execute on function place_order() from public;
grant execute on function place_order() to authenticated;

-- Sample shop items so the shop isn't empty (safe to re-run):
insert into products (name, headline, price, stock) values
  ('Amethyst Cluster', 'The stone of calm', 24.99, 10),
  ('Rose Quartz', 'The stone of love', 14.99, 25),
  ('Clear Quartz Point', 'The master healer', 12.50, 18),
  ('Black Tourmaline', 'Protection from negative energy', 19.99, 12)
on conflict (name) do nothing;
