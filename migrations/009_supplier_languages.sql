-- Explicit RU/KZ dialogue preference. Detection may suggest a language, but
-- the durable conversation value remains the authority for outbound text.

ALTER TABLE supplier_conversations
    ADD COLUMN IF NOT EXISTS preferred_language TEXT NOT NULL DEFAULT 'ru';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='supplier_conversations_preferred_language_check'
    ) THEN
        ALTER TABLE supplier_conversations
            ADD CONSTRAINT supplier_conversations_preferred_language_check
            CHECK (preferred_language IN ('ru','kk'));
    END IF;
END;
$$;

ALTER TABLE supplier_conversation_messages
    ADD COLUMN IF NOT EXISTS language_code TEXT NOT NULL DEFAULT '';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='supplier_conversation_messages_language_check'
    ) THEN
        ALTER TABLE supplier_conversation_messages
            ADD CONSTRAINT supplier_conversation_messages_language_check
            CHECK (language_code IN ('','ru','kk','unknown'));
    END IF;
END;
$$;
