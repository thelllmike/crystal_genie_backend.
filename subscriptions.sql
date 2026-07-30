-- Crystal Genie subscriptions: 7-day free trial, then $3.69/month.
-- Run once in the Supabase SQL editor. Safe to re-run.

create table if not exists subscriptions (
  user_id uuid primary key references auth.users on delete cascade,
  -- trialing | active | past_due | canceled
  status text not null default 'trialing',
  trial_ends_at timestamptz not null default now() + interval '7 days',
  current_period_end timestamptz,
  cancel_at_period_end boolean not null default false,
  stripe_customer_id text,
  stripe_subscription_id text,
  updated_at timestamptz not null default now()
);

create index if not exists subscriptions_stripe_customer_idx
  on subscriptions (stripe_customer_id);
create index if not exists subscriptions_stripe_subscription_idx
  on subscriptions (stripe_subscription_id);

-- Users may read their own row and nothing else. Every write goes through the
-- backend's service-role key (Stripe is the source of truth for paid status,
-- so a client must never be able to mark itself active).
alter table subscriptions enable row level security;
drop policy if exists "users see own subscription" on subscriptions;
create policy "users see own subscription" on subscriptions
  for select to authenticated using (user_id = auth.uid());

-- Start the trial clock the moment an account is created.
create or replace function start_trial_for_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into subscriptions (user_id, status, trial_ends_at)
  values (new.id, 'trialing', now() + interval '7 days')
  on conflict (user_id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created_start_trial on auth.users;
create trigger on_auth_user_created_start_trial
  after insert on auth.users
  for each row execute function start_trial_for_new_user();

-- Existing accounts get a trial measured from when they signed up, so someone
-- who registered two weeks ago is already expired rather than newly trialing.
insert into subscriptions (user_id, status, trial_ends_at)
select id, 'trialing', created_at + interval '7 days'
  from auth.users
on conflict (user_id) do nothing;

-- Single source of truth for "may this user scan crystals?", usable from
-- policies and from the app.
create or replace function has_app_access()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from subscriptions s
     where s.user_id = auth.uid()
       and (
         (s.status = 'trialing' and s.trial_ends_at > now())
         or (s.status in ('active', 'past_due')
             and (s.current_period_end is null or s.current_period_end > now()))
       )
  );
$$;

grant execute on function has_app_access() to authenticated;
