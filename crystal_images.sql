-- Crystal photos: an image_url on each crystal plus a public storage bucket
-- that only admins can upload to. Run once in the Supabase SQL editor AFTER
-- admin_setup.sql. Safe to re-run.

alter table crystals add column if not exists image_url text;

-- Public bucket: anyone can view the photos (the app shows them), max 5 MB each.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('crystal-images', 'crystal-images', true, 5242880,
        array['image/jpeg', 'image/png', 'image/webp'])
on conflict (id) do update
  set public = excluded.public,
      file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "admins upload crystal images" on storage.objects;
create policy "admins upload crystal images" on storage.objects
  for insert to authenticated
  with check (bucket_id = 'crystal-images' and public.is_admin());

drop policy if exists "admins change crystal images" on storage.objects;
create policy "admins change crystal images" on storage.objects
  for update to authenticated
  using (bucket_id = 'crystal-images' and public.is_admin());

drop policy if exists "admins delete crystal images" on storage.objects;
create policy "admins delete crystal images" on storage.objects
  for delete to authenticated
  using (bucket_id = 'crystal-images' and public.is_admin());
