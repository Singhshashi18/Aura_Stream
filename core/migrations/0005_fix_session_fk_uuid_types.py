from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_remove_product'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            DROP TABLE IF EXISTS core_thoughtlog CASCADE;
            DROP TABLE IF EXISTS core_audioartifact CASCADE;
            DROP TABLE IF EXISTS core_agentactivity CASCADE;

            CREATE TABLE core_thoughtlog (
                id bigserial PRIMARY KEY,
                thought_block text NOT NULL,
                final_response text NOT NULL,
                session_id uuid NOT NULL REFERENCES core_aurasession(uuid) DEFERRABLE INITIALLY DEFERRED
            );
            CREATE INDEX core_thoughtlog_session_id_idx ON core_thoughtlog(session_id);

            CREATE TABLE core_audioartifact (
                id bigserial PRIMARY KEY,
                file_path varchar(512) NOT NULL,
                duration double precision NOT NULL,
                session_id uuid NOT NULL REFERENCES core_aurasession(uuid) DEFERRABLE INITIALLY DEFERRED
            );
            CREATE INDEX core_audioartifact_session_id_idx ON core_audioartifact(session_id);

            CREATE TABLE core_agentactivity (
                id bigserial PRIMARY KEY,
                tool_called varchar(128) NOT NULL,
                result jsonb NOT NULL,
                session_id uuid NOT NULL REFERENCES core_aurasession(uuid) DEFERRABLE INITIALLY DEFERRED
            );
            CREATE INDEX core_agentactivity_session_id_idx ON core_agentactivity(session_id);
            """,
            reverse_sql="""
            -- No reverse migration: schema repair for legacy bigint FK mismatch.
            """,
        )
    ]
