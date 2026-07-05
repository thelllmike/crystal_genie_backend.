-- Admin role setup — run in the Supabase SQL editor.
-- (If the RLS warning popup appears, choose "Run without RLS".)

-- Who is an admin.
create table if not exists admin_users (
  user_id uuid primary key references auth.users on delete cascade,
  created_at timestamptz not null default now()
);
alter table admin_users enable row level security;
drop policy if exists "users can check own admin status" on admin_users;
create policy "users can check own admin status" on admin_users
  for select to authenticated using (user_id = auth.uid());

-- Helper the app and policies call to check admin rights.
create or replace function is_admin()
returns boolean
language sql
security definer
set search_path = public
as $$
  select exists (select 1 from admin_users where user_id = auth.uid());
$$;
revoke execute on function is_admin() from public;
grant execute on function is_admin() to authenticated;

-- Admins may insert/update/delete products (everyone can still browse).
drop policy if exists "admins manage products" on products;
create policy "admins manage products" on products
  for all to authenticated
  using (is_admin())
  with check (is_admin());

-- ============================================================
-- Make YOUR account an admin: sign up in the app first, then
-- run this line (change the email if needed):
-- ============================================================
insert into admin_users (user_id)
select id from auth.users where email = 'bultolanka@gmail.com'
on conflict do nothing;
