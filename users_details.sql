-- Users page: every account with name, trial/subscription state, last
-- sign-in, scans and orders. Replaces admin_subscribers() from
-- admin_dashboard.sql. Run once in the Supabase SQL editor. Safe to re-run.
--
-- Works before subscriptions.sql has been run too: users are then listed
-- with status 'none' (trials and payments aren't being tracked yet).

create or replace function admin_subscribers()
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  has_subs boolean := to_regclass('public.subscriptions') is not null;
  result jsonb;
begin
  if not is_admin() then
    raise exception 'Not authorized';
  end if;

  -- Dynamic SQL so the function still runs when the table doesn't exist.
  execute format($q$
    select coalesce(jsonb_agg(to_jsonb(t) order by t.signed_up_at desc), '[]'::jsonb)
      from (
        select u.email,
               nullif(trim(u.raw_user_meta_data->>'name'), '') as name,
               u.created_at as signed_up_at,
               u.last_sign_in_at,
               %s,
               (select count(*) from finds f where f.user_id = u.id) as scans,
               (select count(*) from orders o where o.user_id = u.id) as orders,
               (select coalesce(sum(o.total), 0) from orders o where o.user_id = u.id) as spent
          from auth.users u
          %s
      ) t
  $q$,
    case when has_subs then
      'coalesce(s.status, ''trialing'') as status, s.trial_ends_at, s.current_period_end,
       coalesce(s.cancel_at_period_end, false) as cancel_at_period_end'
    else
      '''none'' as status, null::timestamptz as trial_ends_at, null::timestamptz as current_period_end,
       false as cancel_at_period_end'
    end,
    case when has_subs then 'left join subscriptions s on s.user_id = u.id' else '' end
  ) into result;

  return result;
end;
$$;
revoke execute on function admin_subscribers() from public;
grant execute on function admin_subscribers() to authenticated;
