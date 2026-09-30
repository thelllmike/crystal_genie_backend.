-- Admin dashboard: lets admins edit crystals and products, and see every
-- order (with buyer email + shipping address) and every subscriber.
-- Run once in the Supabase SQL editor AFTER admin_setup.sql. Safe to re-run.

-- ---------- Crystals: admins may add / edit / delete ----------
drop policy if exists "admins manage crystals" on crystals;
create policy "admins manage crystals" on crystals
  for all to authenticated
  using (is_admin())
  with check (is_admin());

-- (Products are already editable by admins via admin_setup.sql.)

-- ---------- Orders: admins read everything and can update status ----------
drop policy if exists "admins see all orders" on orders;
create policy "admins see all orders" on orders
  for select to authenticated using (is_admin());

drop policy if exists "admins update orders" on orders;
create policy "admins update orders" on orders
  for update to authenticated
  using (is_admin())
  with check (is_admin());

drop policy if exists "admins see all order items" on order_items;
create policy "admins see all order items" on order_items
  for select to authenticated using (is_admin());

-- Every order, newest first, with the buyer's email and line items.
-- Security definer because buyer emails live in auth.users, which the app
-- can't read directly; the is_admin() check keeps it admin-only.
create or replace function admin_orders()
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
begin
  if not is_admin() then
    raise exception 'Not authorized';
  end if;

  return coalesce((
    select jsonb_agg(to_jsonb(t) order by t.created_at desc)
      from (
        select o.id,
               o.created_at,
               o.total,
               o.status,
               u.email,
               o.ship_name,
               o.ship_phone,
               o.ship_address,
               o.ship_city,
               o.ship_postal,
               coalesce((
                 select jsonb_agg(jsonb_build_object(
                          'name', i.product_name,
                          'quantity', i.quantity,
                          'unit_price', i.unit_price)
                        order by i.id)
                   from order_items i
                  where i.order_id = o.id
               ), '[]'::jsonb) as items
          from orders o
          left join auth.users u on u.id = o.user_id
      ) t
  ), '[]'::jsonb);
end;
$$;
revoke execute on function admin_orders() from public;
grant execute on function admin_orders() to authenticated;

-- Every account's trial/subscription state with its email.
create or replace function admin_subscribers()
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
begin
  if not is_admin() then
    raise exception 'Not authorized';
  end if;

  return coalesce((
    select jsonb_agg(to_jsonb(t) order by t.signed_up_at desc)
      from (
        select u.email,
               u.created_at as signed_up_at,
               coalesce(s.status, 'trialing') as status,
               s.trial_ends_at,
               s.current_period_end,
               coalesce(s.cancel_at_period_end, false) as cancel_at_period_end
          from auth.users u
          left join subscriptions s on s.user_id = u.id
      ) t
  ), '[]'::jsonb);
end;
$$;
revoke execute on function admin_subscribers() from public;
grant execute on function admin_subscribers() to authenticated;
