from sqlalchemy import inspect as sa_inspect


def test_new_schema_elements():
    import database
    database.init_db()
    insp = sa_inspect(database.engine)

    settings_cols = {c["name"] for c in insp.get_columns("settings")}
    assert {"audiobook_dir", "plex_url", "plex_token", "plex_section_id"} <= settings_cols

    chapter_cols = {c["name"] for c in insp.get_columns("chapters")}
    assert "text" in chapter_cols

    job_cols = {c["name"] for c in insp.get_columns("export_jobs")}
    assert {"novel_id", "novel_title", "author", "start_order", "end_order",
            "voice", "speed", "status", "chapters_done", "chapters_total",
            "detail", "output_path", "error", "created_at", "finished_at"} <= job_cols

    db = database.SessionLocal()
    try:
        s = db.query(database.Settings).first()
        assert s.audiobook_dir == r"E:\Plex\Audiobooks\Audiobooks"
        assert s.plex_url == "" and s.plex_token == "" and s.plex_section_id == ""
    finally:
        db.close()
