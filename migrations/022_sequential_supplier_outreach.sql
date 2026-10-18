-- A supplier may be asked about another offer only after the previous chat is
-- terminal. The active-chat uniqueness is global to the contact and session,
-- so an inbound message can never be assigned to two active case dialogues.
ALTER TABLE supplier_conversations
    DROP CONSTRAINT supplier_conversations_case_id_supplier_id_supplier_contact_key;
CREATE UNIQUE INDEX supplier_conversations_one_active_chat_idx
    ON supplier_conversations(supplier_contact_id,session_name,external_chat_id)
    WHERE state NOT IN ('complete','closed_no_reply','opted_out');
