"""Requirement 31: opt-in ranking and transactional click deduplication."""
from datetime import datetime
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask
from api.miniprogram_api import miniprogram_api
from services import miniprogram_moment_service as moments


class MomentPopularityTest(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.cursor = self.conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        for mock in (patch.object(moments, 'init_miniprogram_moment_tables'),
                     patch.object(moments, '_connect_app', return_value=self.conn),
                     patch.object(moments, '_now', return_value='2026-10-23 10:00:00')):
            mock.start(); self.addCleanup(mock.stop)

    def query(self, **kwargs):
        self.cursor.fetchall.return_value = []
        moments.list_moments(**kwargs)
        return self.cursor.execute.call_args.args

    def test_popular_sort_is_before_limit_and_has_stable_ties(self):
        sql, params = self.query(sort='popular', limit=3)
        self.assertIn('ORDER BY view_count DESC, COALESCE(source_published_at, created_at) DESC, id DESC', sql)
        self.assertLess(sql.index('ORDER BY'), sql.index('LIMIT'))
        self.assertEqual(params[-1], 3)

    def test_default_category_and_private_lists_keep_chronological_sort(self):
        for kwargs in ({}, {'sort': 'popular', 'category_code': 'FUNNY'},
                       {'sort': 'popular', 'include_private': True, 'user_id': 'u'}):
            sql, _ = self.query(**kwargs)
            self.assertIn('ORDER BY created_at DESC, id DESC', sql)
        with self.assertRaises(ValueError):
            moments.list_moments(sort='view_count; DROP TABLE')

    def record(self, previous=None, **post):
        self.cursor.fetchone.side_effect = [{'id': 'p1', 'visibility': 'public', 'view_count': 5, **post}, previous]
        return moments.record_moment_view('p1', user_id='u1')

    def test_first_click_increments_and_does_not_change_post_timestamp(self):
        result = self.record()
        self.assertEqual(result, {'ok': True, 'counted': True, 'view_count': 6})
        sqls = [args.args[0] for args in self.cursor.execute.call_args_list]
        self.assertIn('FOR UPDATE', sqls[0])
        update = next(sql for sql in sqls if 'SET view_count' in sql)
        self.assertNotIn('updated_at', update)
        self.conn.__enter__.return_value.commit.assert_called_once()

    def test_rolling_window_boundary_and_repeat_does_not_extend_window(self):
        for last, expected in (('2026-10-23 09:30:01', False), ('2026-10-23 09:30:00', True),
                               (datetime(2026, 10, 23, 9, 59), False)):
            self.cursor.reset_mock()
            result = self.record({'last_viewed_at': last})
            self.assertEqual(result['counted'], expected)
            increments = [c for c in self.cursor.execute.call_args_list if 'SET view_count' in c.args[0]]
            self.assertEqual(len(increments), int(expected))

    def test_guest_identity_validated_and_not_confused_with_account_identity(self):
        with self.assertRaises(ValueError):
            moments.record_moment_view('p1', visitor_id='u1')
        self.cursor.fetchone.side_effect = [{'visibility': 'public'}, None]
        moments.record_moment_view('p1', visitor_id='a' * 32)
        guest_key = self.cursor.execute.call_args_list[1].args[1][1]
        self.cursor.reset_mock()
        self.record()
        user_key = self.cursor.execute.call_args_list[1].args[1][1]
        self.assertNotEqual(guest_key, user_key)

    def test_missing_deleted_and_other_users_private_posts_cannot_be_counted(self):
        for row in (None, {'visibility': 'private', 'user_id': 'other'}):
            self.cursor.fetchone.side_effect = [row]
            with self.assertRaises(LookupError):
                moments.record_moment_view('p1', user_id='u1')
        result = self.record(visibility='private', user_id='u1')
        self.assertFalse(result['counted'])

    def test_view_endpoint_uses_token_identity_ignores_client_user_id_and_accepts_guests(self):
        app = Flask(__name__); app.register_blueprint(miniprogram_api)
        client = app.test_client()
        with patch('api.miniprogram_api.get_user_by_token', return_value={'id': 'real'}), \
             patch('api.miniprogram_api.record_moment_view', return_value={'ok': True}) as record:
            self.assertEqual(client.post('/api/miniprogram/moments/p1/view',
                json={'user_id': 'forged', 'visitor_id': 'a' * 32}, headers={'Authorization': 'Bearer token'}).status_code, 200)
            self.assertEqual(record.call_args.kwargs['user_id'], 'real')
            self.assertEqual(client.post('/api/miniprogram/moments/p1/view', json={'visitor_id': 'a' * 32}).status_code, 200)
            self.assertEqual(record.call_args.kwargs['user_id'], '')


if __name__ == '__main__':
    unittest.main()
