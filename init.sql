CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  tg_id INTEGER UNIQUE,
  full_name TEXT,
  username TEXT,
  phone TEXT,
  is_verified INTEGER DEFAULT 0,
  email TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT,
  price INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'EUR',
  photo_url TEXT,
  is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS carts (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cart_items (
  id INTEGER PRIMARY KEY,
  cart_id INTEGER NOT NULL,
  product_id INTEGER NOT NULL,
  qty INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'EUR',
  status TEXT NOT NULL DEFAULT 'pending',
  address TEXT,
  note TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL,
  product_id INTEGER NOT NULL,
  qty INTEGER NOT NULL,
  price INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL,
  provider TEXT,
  payload TEXT,
  telegram_charge_id TEXT,
  provider_charge_id TEXT,
  status TEXT DEFAULT 'pending',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS email_otps (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  email TEXT NOT NULL,
  code TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS content_sections (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,              -- 'brand:waka', 'brand:vozol', 'liquids', 'pods'
  text TEXT,                              -- список моделей/вкусов (HTML разрешён)
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS content_photos (
  id INTEGER PRIMARY KEY,
  section_key TEXT NOT NULL,              -- ключ из content_sections.key
  file_id TEXT NOT NULL,                  -- Telegram file_id (лучший вариант)
  sort_order INTEGER DEFAULT 0
);

