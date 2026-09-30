-- Training pipeline: labeled photo dataset, training jobs, and which trained
-- model the scanner uses. Run once in the Supabase SQL editor AFTER
-- admin_setup.sql. Safe to re-run.
--
-- Flow: admin uploads photos -> labels each with a crystal name -> queues a
-- training job -> trainer/train_worker.py trains a YOLO classifier and uploads
-- the weights -> admin clicks Deploy -> the backend picks the new model up.

-- ---------- Photos to train on ----------
create table if not exists training_images (
  id bigint generated always as identity primary key,
  storage_path text not null unique,   -- path on the VPS or in the bucket (see stored_on)
  label text,                          -- crystal name; null = not labeled yet
  created_at timestamptz not null default now(),
  labeled_at timestamptz,
  labeled_by uuid references auth.users on delete set null
);
-- Where the file lives: 'vps' = the API server's disk (current uploads),
-- 'supabase' = the training-images bucket (photos uploaded before the move).
alter table training_images add column if not exists stored_on text not null default 'supabase';

create index if not exists training_images_label_idx on training_images (label);
create index if not exists training_images_unlabeled_idx
  on training_images (created_at) where label is null;

alter table training_images enable row level security;
drop policy if exists "admins manage training images" on training_images;
create policy "admins manage training images" on training_images
  for all to authenticated
  using (is_admin())
  with check (is_admin());

-- Photos per crystal, for the Training page.
create or replace function training_stats()
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
  return jsonb_build_object(
    'unlabeled', (select count(*) from training_images where label is null),
    'labeled', (select count(*) from training_images where label is not null),
    'classes', coalesce((
      select jsonb_agg(jsonb_build_object('label', label, 'count', n) order by n desc, label)
        from (select label, count(*) as n
                from training_images
               where label is not null
               group by label) t
    ), '[]'::jsonb)
  );
end;
$$;
revoke execute on function training_stats() from public;
grant execute on function training_stats() to authenticated;

-- ---------- Training jobs ----------
create table if not exists training_jobs (
  id bigint generated always as identity primary key,
  -- queued | running | succeeded | failed | canceled
  status text not null default 'queued',
  params jsonb not null default '{}'::jsonb,  -- epochs, imgsz, base_model, min_images
  classes text[],                    -- filled in by the trainer
  image_count int,
  progress jsonb,                    -- {"epoch": 3, "epochs": 30}
  metrics jsonb,                     -- {"top1": 0.93, "top5": 0.99}
  log text,
  error text,
  model_path text,                   -- path inside the models bucket
  created_by uuid default auth.uid() references auth.users on delete set null,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  deployed_at timestamptz            -- latest deployed_at = the live model
);

alter table training_jobs enable row level security;
drop policy if exists "admins manage training jobs" on training_jobs;
create policy "admins manage training jobs" on training_jobs
  for all to authenticated
  using (is_admin())
  with check (is_admin());

-- ---------- Storage ----------
-- Training photos: public read so the admin panel can show thumbnails cheaply
-- (they're crystal photos, nothing personal). Only admins can write.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('training-images', 'training-images', true, 10485760,
        array['image/jpeg', 'image/png', 'image/webp'])
on conflict (id) do update
  set public = excluded.public,
      file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "admins upload training images" on storage.objects;
create policy "admins upload training images" on storage.objects
  for insert to authenticated
  with check (bucket_id = 'training-images' and public.is_admin());

drop policy if exists "admins delete training images" on storage.objects;
create policy "admins delete training images" on storage.objects
  for delete to authenticated
  using (bucket_id = 'training-images' and public.is_admin());

-- Trained weights: private. The trainer and backend use the service-role key.
insert into storage.buckets (id, name, public)
values ('models', 'models', false)
on conflict (id) do nothing;

-- Admins may read them, so the Test page can try a run before it's deployed.
drop policy if exists "admins read models" on storage.objects;
create policy "admins read models" on storage.objects
  for select to authenticated
  using (bucket_id = 'models' and public.is_admin());
