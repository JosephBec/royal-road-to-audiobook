"""The API an external tagger talks to: cast registry and script ingest."""
import pytest
from fastapi.testclient import TestClient

OPEN, CLOSE = "“", "”"
CHAPTER_TEXT = (
    f"The hall was empty when she arrived. {OPEN}You came after all,{CLOSE} said Gareth. "
    f"{OPEN}I said I would.{CLOSE} She set the lantern down on the table."
)


@pytest.fixture()
def client(monkeypatch):
    import export_worker, prefetch, epub_library
    monkeypatch.setattr(export_worker, "start_worker", lambda: None)
    monkeypatch.setattr(prefetch, "start_worker", lambda: None)
    monkeypatch.setattr(epub_library, "start", lambda: None)
    from main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def chapter(client):
    """A novel with one text-cached chapter, cleaned up afterwards."""
    import database
    db = database.SessionLocal()
    novel = database.Novel(title="Script Test", rr_url="https://example.com/fiction/9001/x")
    db.add(novel); db.flush()
    ch = database.Chapter(novel_id=novel.id, rr_chapter_id="1", title="Chapter 1",
                          order=1, rr_url="https://example.com/fiction/9001/x/chapter/1",
                          text=CHAPTER_TEXT)
    db.add(ch); db.commit()
    ids = (novel.id, ch.id)
    db.close()
    yield ids
    db = database.SessionLocal()
    db.query(database.ChapterScript).filter(
        database.ChapterScript.chapter_id == ids[1]).delete()
    db.query(database.Character).filter(database.Character.novel_id == ids[0]).delete()
    n = db.query(database.Novel).filter(database.Novel.id == ids[0]).first()
    if n:
        db.delete(n)
    db.commit(); db.close()


# ----- cast registry -----

def test_create_and_list_character(client, chapter):
    novel_id, _ = chapter
    r = client.post(f"/api/novels/{novel_id}/characters",
                    json={"name": "Gareth", "description": "gruff, older"})
    assert r.status_code == 201, r.text
    assert client.get(f"/api/novels/{novel_id}/characters").json()["characters"][0]["name"] == "Gareth"


def test_reposting_a_character_does_not_duplicate(client, chapter):
    """A tagger re-posts its cast list every run; that must be idempotent."""
    novel_id, _ = chapter
    client.post(f"/api/novels/{novel_id}/characters", json={"name": "Gareth"})
    client.post(f"/api/novels/{novel_id}/characters", json={"name": "Gareth"})
    assert len(client.get(f"/api/novels/{novel_id}/characters").json()["characters"]) == 1


def test_reposting_does_not_clobber_an_assigned_voice(client, chapter):
    novel_id, _ = chapter
    cid = client.post(f"/api/novels/{novel_id}/characters", json={"name": "Gareth"}).json()["id"]
    client.patch(f"/api/characters/{cid}", json={"voice": "cb_yearsley"})
    client.post(f"/api/novels/{novel_id}/characters",
                json={"name": "Gareth", "description": "from a later pass"})
    chars = client.get(f"/api/novels/{novel_id}/characters").json()["characters"]
    assert chars[0]["voice"] == "cb_yearsley"


def test_aliases_round_trip(client, chapter):
    novel_id, _ = chapter
    r = client.post(f"/api/novels/{novel_id}/characters",
                    json={"name": "Gareth", "aliases": ["the captain"]})
    assert r.json()["aliases"] == ["the captain"]


# ----- scripts -----

def test_script_is_generated_without_a_tagger(client, chapter):
    """No LLM anywhere: quote detection alone gives a usable script."""
    _, chapter_id = chapter
    body = client.get(f"/api/chapters/{chapter_id}/script").json()
    assert body["source"] == "rule"
    assert body["stored"] is False
    assert body["stats"]["speech"] == 2
    assert body["stats"]["attributed"] == 1  # the explicit "said Gareth"


def test_external_ingest_is_stored(client, chapter):
    _, chapter_id = chapter
    got = client.get(f"/api/chapters/{chapter_id}/script").json()
    r = client.put(f"/api/chapters/{chapter_id}/script", json={
        "text_hash": got["text_hash"],
        "spans": [{"i": 2, "kind": "speech", "speaker": "Mira", "confidence": 0.8}],
    })
    assert r.status_code == 200, r.text
    after = client.get(f"/api/chapters/{chapter_id}/script").json()
    assert after["stored"] is True
    assert after["spans"][2]["speaker"] == "Mira"


def test_ingest_rejected_when_text_changed(client, chapter):
    """Stale span indices would attribute lines to the wrong speaker."""
    _, chapter_id = chapter
    r = client.put(f"/api/chapters/{chapter_id}/script",
                   json={"text_hash": "deadbeef", "spans": [{"i": 0, "speaker": "X"}]})
    assert r.status_code == 409


def test_manual_override_survives_reingest(client, chapter):
    _, chapter_id = chapter
    client.patch(f"/api/chapters/{chapter_id}/script/2", json={"speaker": "Ana"})
    client.put(f"/api/chapters/{chapter_id}/script",
               json={"spans": [{"i": 2, "kind": "speech", "speaker": "Bob"}]})
    spans = client.get(f"/api/chapters/{chapter_id}/script").json()["spans"]
    assert spans[2]["speaker"] == "Ana", "a hand correction must outlive re-tagging"
    assert spans[2]["source"] == "manual"


def test_override_rejects_unknown_span(client, chapter):
    _, chapter_id = chapter
    assert client.patch(f"/api/chapters/{chapter_id}/script/999",
                        json={"speaker": "X"}).status_code == 404


def test_override_rejects_bad_kind(client, chapter):
    _, chapter_id = chapter
    assert client.patch(f"/api/chapters/{chapter_id}/script/0",
                        json={"kind": "singing"}).status_code == 400


def test_script_requires_fetched_text(client):
    import database
    db = database.SessionLocal()
    novel = database.Novel(title="No Text", rr_url="https://example.com/fiction/9002/y")
    db.add(novel); db.flush()
    ch = database.Chapter(novel_id=novel.id, title="C1", order=1,
                          rr_url="https://example.com/fiction/9002/y/chapter/1")
    db.add(ch); db.commit()
    chapter_id, novel_id = ch.id, novel.id
    db.close()

    assert client.get(f"/api/chapters/{chapter_id}/script").status_code == 409

    db = database.SessionLocal()
    db.delete(db.query(database.Novel).filter(database.Novel.id == novel_id).first())
    db.commit(); db.close()
