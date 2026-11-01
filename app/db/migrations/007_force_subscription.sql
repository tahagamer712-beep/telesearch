-- وحدة Force Subscription (access control عام): قنوات مطلوبة. لا علاقة لها ببيانات المرضى.
CREATE TABLE force_sub_channels (
    id          INTEGER PRIMARY KEY,
    chat_ref    TEXT NOT NULL UNIQUE,   -- @username او -100xxxxxxxxxx
    title       TEXT NOT NULL,
    invite_link TEXT,                   -- رابط انضمام (للقنوات الخاصة)
    enabled     INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
