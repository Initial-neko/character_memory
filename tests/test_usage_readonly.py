import sqlite3
import pytest
from character_memory.llm.usage import LlmUsageStore


def test_readonly_usage_never_creates_disk_database_or_schema(tmp_path):
    path=tmp_path/'missing'/'usage.db'
    reader=LlmUsageStore(path,read_only=True)
    try:
        assert reader.usage()['summary']['requests']==0
        with pytest.raises(PermissionError): reader.add({})
        assert not path.parent.exists()
    finally: reader.close()
    path=tmp_path/'empty.db';writer=sqlite3.connect(path);writer.execute('CREATE TABLE facts(id INTEGER)');writer.commit()
    reader=LlmUsageStore(path,read_only=True)
    try: assert reader.usage()['summary']['requests']==0
    finally:reader.close()
    assert writer.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[('facts',)]
    writer.close()


def test_usage_inspection_survives_an_existing_writer_without_schema_lock(tmp_path):
    path=tmp_path/'usage.db';writer=LlmUsageStore(path);writer.add({'input_chars':1,'output_chars':1})
    writer.conn.execute('BEGIN IMMEDIATE')
    reader=LlmUsageStore(path,read_only=True)
    try:
        assert reader.usage()['summary']['requests']==1
        assert reader.conn.total_changes==0
    finally:reader.close();writer.conn.rollback();writer.close()
