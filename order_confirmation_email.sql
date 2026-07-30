-- Tracks which orders have already had their confirmation email sent, so a
-- retry (or a second tap) can't email the customer twice.
-- Run once in the Supabase SQL editor. Safe to re-run.

alter table orders
  add column if not exists confirmation_email_sent_at timestamptz;

-- Users have select-only access to orders, so the flag is set through a
-- security-definer function that still checks ownership via auth.uid().
create or replace function mark_order_email_sent(p_order_id bigint)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  update orders
     set confirmation_email_sent_at = now()
   where id = p_order_id
     and user_id = auth.uid()
     and confirmation_email_sent_at is null;
end;
$$;

grant execute on function mark_order_email_sent(bigint) to authenticated;
