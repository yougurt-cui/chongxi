from unittest.mock import MagicMock
import pytest
from services import model_knowledge_import_service as store
from services import health_knowledge_import_service as health


def setup(monkeypatch,base):
    conn=MagicMock();conn.__enter__.return_value=conn
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value={'knowledge_base':base}
    monkeypatch.setattr(store,'connect',lambda:conn)
    monkeypatch.setattr(store,'ensure_database',lambda:None)
    monkeypatch.setattr(store,'database_name',lambda:'chongxi_model_knowledge')
    generic=MagicMock(return_value={'status':'generic'})
    monkeypatch.setattr(store,'_import_batch_source',generic)
    return conn,generic


def test_health_only_writes_specialized_store(monkeypatch):
    conn,generic=setup(monkeypatch,'health')
    importer=MagicMock(return_value={'health_status':'already_imported','health_point_count':5})
    monkeypatch.setattr(health,'import_batch',importer)
    result=store.import_batch(8)
    generic.assert_not_called();importer.assert_called_once_with(8)
    assert result['inserted_count']==0 and result['total_count']==5


def test_selection_keeps_existing_path(monkeypatch):
    _,generic=setup(monkeypatch,'selection')
    assert store.import_batch(2)=={'status':'generic'}
    generic.assert_called_once_with(2)


def test_failed_health_does_not_write_generic_copy(monkeypatch):
    conn,generic=setup(monkeypatch,'health')
    monkeypatch.setattr(health,'import_batch',MagicMock(side_effect=ValueError('invalid')))
    with pytest.raises(ValueError):store.import_batch(8)
    generic.assert_not_called()
    assert any('import_error' in c.args[0] for c in conn.cursor.return_value.__enter__.return_value.execute.call_args_list)
