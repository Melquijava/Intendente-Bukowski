ALTER TABLE registrations ADD COLUMN role_state TEXT NOT NULL DEFAULT 'not_attempted';
ALTER TABLE registrations ADD COLUMN nickname_state TEXT NOT NULL DEFAULT 'not_attempted';
ALTER TABLE registrations ADD COLUMN sync_error TEXT;
ALTER TABLE registrations ADD COLUMN synced_at TEXT;
CREATE TABLE request_messages(registration INTEGER NOT NULL REFERENCES registrations(id),channel INTEGER NOT NULL,message INTEGER NOT NULL,PRIMARY KEY(channel,message));
ALTER TABLE tables ADD COLUMN error_code TEXT;
INSERT INTO migrations VALUES(2);
