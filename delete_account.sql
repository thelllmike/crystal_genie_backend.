-- Account self-deletion — required by App Store guideline 5.1.1(v).
-- Run in the Supabase SQL editor. (Choose "Run without RLS" if the popup appears.)
--
-- Deleting the auth.users row cascades to finds, saved_crystals,
-- cart_items, orders and admin_users via their foreign keys.

create or replace function delete_user()
returns void
language sql
security definer
set search_path = ''
as $$
  delete from auth.users where id = auth.uid();
$$;

revoke execute on function public.delete_user() from public;
grant execute on function public.delete_user() to authenticated;
