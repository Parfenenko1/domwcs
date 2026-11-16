"""Сбои скачивания: повтор запроса и сохранение прежнего расписания."""
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location('content_network', Path(__file__).with_name('update_content.py'))
uc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uc)


class ContentNetworkTest(unittest.TestCase):
    def test_getfile_recovers_after_connection_reset(self):
        with patch.object(uc.urllib.request, 'urlopen', side_effect=[
            urllib.error.URLError(ConnectionResetError(104, 'reset')),
            io.BytesIO(b'{"ok":true,"result":{"file_path":"photo.jpg"}}'),
        ]) as request, patch.object(uc.time, 'sleep') as sleep:
            self.assertEqual(uc.api_call('test', 'getFile', {'file_id': 'x'}), {'file_path': 'photo.jpg'})
            self.assertEqual(request.call_count, 2)
            sleep.assert_called_once_with(2)

    def test_permanent_http_error_is_not_retried(self):
        error = urllib.error.HTTPError('https://example.test', 400, 'Bad Request', {}, None)
        with patch.object(uc.urllib.request, 'urlopen', side_effect=error) as request, patch.object(uc.time, 'sleep'):
            with self.assertRaises(urllib.error.HTTPError):
                uc.api_call('test', 'getFile')
            self.assertEqual(request.call_count, 1)

    def test_repeated_failure_is_bounded_and_keeps_image_and_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'schedule.jpg'
            offset = Path(directory) / 'offset.txt'
            image.write_bytes(b'old image')
            offset.write_text('100')
            post = {'update_id': 101, 'channel_post': {'message_id': 495, 'date': 1794816184,
                    'chat': {'username': 'domwcs'}, 'caption': '#расписание',
                    'photo': [{'file_id': 'x', 'file_unique_id': 'new'}]}}
            with patch.dict(uc.os.environ, TELEGRAM_BOT_TOKEN='test', TELEGRAM_CHANNEL='@domwcs'), \
                 patch.object(uc, 'OFFSET_FILE', str(offset)), \
                 patch.object(uc, 'SCHEDULE_IMAGE', str(image)), \
                 patch.object(uc, 'update_subscriber_count'), \
                 patch.object(uc, 'get_updates', return_value=[post]), \
                 patch.object(uc, 'load_schedule_meta', return_value={}), \
                 patch.object(uc.urllib.request, 'urlopen', side_effect=urllib.error.URLError('reset')) as request, \
                 patch.object(uc.time, 'sleep'):
                with self.assertRaises(urllib.error.URLError):
                    uc.main()
                self.assertEqual(request.call_count, 4)
            self.assertEqual(image.read_bytes(), b'old image')
            self.assertEqual(offset.read_text(), '100')

    def test_success_replaces_image_without_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'schedule.jpg'
            image.write_bytes(b'old')
            with patch.object(uc, 'telegram_file', return_value=b'new'):
                uc.download_file('test', 'x', str(image))
            self.assertEqual(image.read_bytes(), b'new')
            self.assertEqual(list(Path(directory).iterdir()), [image])


if __name__ == '__main__':
    unittest.main()
