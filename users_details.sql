-- Richer Subscribers page: name, last sign-in, scan and order counts.
-- Replaces admin_subscribers() from admin_dashboard.sql. Run once in the
-- Supabase SQL editor. Safe to re-run.

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
               nullif(trim(u.raw_user_meta_data->>'name'), '') as name,
               u.created_at as signed_up_at,
               u.last_sign_in_at,
               coalesce(s.status, 'trialing') as status,
               s.trial_ends_at,
               s.current_period_end,
               coalesce(s.cancel_at_period_end, false) as cancel_at_period_end,
               (select count(*) from finds f where f.user_id = u.id) as scans,
               (select count(*) from orders o where o.user_id = u.id) as orders,
               (select coalesce(sum(o.total), 0) from orders o where o.user_id = u.id) as spent
          from auth.users u
          left join subscriptions s on s.user_id = u.id
      ) t
  ), '[]'::jsonb);
end;
$$;
revoke execute on function admin_subscribers() from public;
grant execute on function admin_subscribers() to authenticated;
