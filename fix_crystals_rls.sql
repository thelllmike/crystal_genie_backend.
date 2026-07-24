-- Fix: the crystal library was empty for signed-in users.
--
-- `crystals` has RLS enabled but its only select policy was granted `to anon`.
-- After login the app's role is `authenticated`, which matched no policy, so
-- every query returned zero rows *without* raising an error — an empty library
-- and a blank crystal detail page.
--
-- Run this once in the Supabase SQL editor. Safe to re-run.

drop policy if exists "anon can read crystals" on crystals;
drop policy if exists "anyone can read crystals" on crystals;

create policy "anyone can read crystals"
  on crystals for select to anon, authenticated using (true);

-- Verify: should report the seeded row count (718 after seed_crystals_full.sql)
-- and list the policy with roles {anon,authenticated}.
select count(*) as crystal_rows from crystals;

select policyname, roles, cmd
from pg_policies
where tablename = 'crystals';
