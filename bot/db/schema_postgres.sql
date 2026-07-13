-- PostgreSQL schema for the server database.
-- Local development can keep using SQLite until the bot is switched to PostgreSQL.

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    format TEXT NOT NULL CHECK (format IN ('proverka', '1plus1', 'best', 'masterclass', 'hitloto')),
    event_date DATE NOT NULL,
    weekday TEXT,
    event_time TIME NOT NULL,
    address TEXT NOT NULL,
    location TEXT NOT NULL,
    description TEXT,
    image_url TEXT,
    price INTEGER NOT NULL DEFAULT 0 CHECK (price >= 0),
    payment_url TEXT,
    host TEXT,
    max_seats INTEGER NOT NULL DEFAULT 0 CHECK (max_seats >= 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'past', 'hidden')),
    source_sheet TEXT,
    source_row INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (format, event_date, event_time, location)
);

CREATE INDEX IF NOT EXISTS idx_events_active_date
    ON events (event_date, event_time)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_events_format_date
    ON events (format, event_date, event_time);


CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE,
    vk_id BIGINT UNIQUE,
    username TEXT,
    name TEXT,
    phone TEXT,
    source TEXT NOT NULL DEFAULT 'telegram' CHECK (source IN ('telegram', 'vkontakte', 'import')),
    rozygrysh_used BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (telegram_id IS NOT NULL OR vk_id IS NOT NULL OR phone IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_users_phone
    ON users (phone);

CREATE INDEX IF NOT EXISTS idx_users_last_active
    ON users (last_active_at);


CREATE TABLE IF NOT EXISTS bookings (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    event_id BIGINT NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
    guests INTEGER NOT NULL CHECK (guests BETWEEN 1 AND 4),
    format TEXT NOT NULL CHECK (format IN ('proverka', '1plus1', 'rozygrysh')),
    source TEXT NOT NULL DEFAULT 'telegram' CHECK (source IN ('telegram', 'vkontakte', 'import')),
    status TEXT NOT NULL DEFAULT 'booked' CHECK (status IN ('booked', 'confirmed', 'cancelled', 'annulled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    annulled_at TIMESTAMPTZ,
    reminder_24h_sent BOOLEAN NOT NULL DEFAULT false,
    reminder_day_sent BOOLEAN NOT NULL DEFAULT false,
    ticket_message_id BIGINT,
    confirm_message_id BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bookings_user_status
    ON bookings (user_id, status);

CREATE INDEX IF NOT EXISTS idx_bookings_event_status
    ON bookings (event_id, status);

CREATE INDEX IF NOT EXISTS idx_bookings_reminders
    ON bookings (status, reminder_24h_sent, reminder_day_sent);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_booking_per_user_event
    ON bookings (user_id, event_id)
    WHERE status IN ('booked', 'confirmed');
