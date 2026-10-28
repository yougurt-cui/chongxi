from types import SimpleNamespace
from unittest.mock import patch

import app_config as config


def test_database_configuration_cache_invalidation_and_environment(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ('config.yaml', 'column_map.yaml', 'dictionaries_seed.yaml')]
    for name, path in zip(('DEFAULT_CONFIG_PATH', 'DEFAULT_COLMAP_PATH', 'DEFAULT_DICT_SEED_PATH'), paths):
        path.write_text('initial');monkeypatch.setattr(config, name, path)
    value = SimpleNamespace(mysql={'host': 'configured', 'port': 13308, 'database': 'main', 'ssl': {'check_hostname': True}}, feature_mysql={'database': 'features'})
    config._cached_database_settings.cache_clear()
    try:
        with patch.object(config, 'load_settings', return_value=value) as load:
            monkeypatch.setenv('MYSQL_HOST', 'first')
            first = config.get_mysql_config();first['ssl']['check_hostname'] = False
            monkeypatch.setenv('MYSQL_HOST', 'second')
            second = config.get_mysql_config()
            assert second['host'] == 'second' and second['ssl']['check_hostname']
            assert load.call_count == 1
            paths[0].write_text('changed configuration')
            assert config.get_feature_mysql_config()['database'] == 'features'
            assert load.call_count == 2
    finally:
        config._cached_database_settings.cache_clear()
