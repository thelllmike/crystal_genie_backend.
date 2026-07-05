-- Seed the crystals table with your YOLO model's 5 classes.
-- Run AFTER supabase_schema.sql. Edit the texts as you like — the names
-- must stay exactly as below (they match the model's class names).

insert into crystals (name, headline, description, star_sign, chakras) values
  ('Alexandrite', 'The stone of transformation',
   'Alexandrite is a rare color-changing gem that shifts from green in daylight to red under warm light. It is said to balance emotions, strengthen intuition, and bring good fortune during times of change.',
   'Gemini', 'Crown, Heart'),
  ('Amethyst', 'The stone of calm',
   'Amethyst is a violet quartz prized for its soothing energy. It is associated with clarity of mind, restful sleep, and protection from negative thoughts.',
   'Pisces', 'Crown, Third Eye'),
  ('Aquamarine', 'The stone of courage',
   'Aquamarine carries the calming spirit of the sea. It is believed to ease stress, quiet the mind, and give courage to speak one''s truth.',
   'Aries', 'Throat'),
  ('Aventurine', 'The stone of opportunity',
   'Aventurine is known as one of the luckiest crystals, thought to attract prosperity and new opportunities while soothing the heart and calming anger.',
   'Virgo', 'Heart'),
  ('Benitoite', 'The stone of joy',
   'Benitoite is a rare blue gem that fluoresces brilliantly under UV light. It is said to spark joy, heighten intuition, and help one find their place in the universe.',
   'Sagittarius', 'Third Eye, Crown')
on conflict (name) do update set
  headline = excluded.headline,
  description = excluded.description,
  star_sign = excluded.star_sign,
  chakras = excluded.chakras;
