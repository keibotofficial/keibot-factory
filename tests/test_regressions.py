"""Offline regressions. All writes use temporary paths; production workers stay off."""
import os
os.environ['KEIBOT_START_WORKERS'] = '0'
import copy
import importlib
import io
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from core.json_store import read_json, write_json, update_json, merge_snapshot
from core.job_state import terminal, channel_id, JobCancelled
from core.jobs import cancel_jobs_in, check_job, update_job_in, recover_jobs
import core.upload_state as upload


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'jobs.json'

    def test_concurrent_transactions_do_not_lose_jobs(self):
        write_json(self.path, [])
        def append(start):
            for i in range(20):
                update_json(self.path, [], lambda jobs: jobs.append({'id': start+i}))
        workers = [threading.Thread(target=append, args=(i*20,)) for i in range(5)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual(len({j['id'] for j in read_json(self.path, [])}), 100)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_database_is_not_silently_replaced(self):
        self.path.write_text('{bad json')
        with self.assertRaises(json.JSONDecodeError):
            update_json(self.path, [], lambda jobs: jobs.clear())
        self.assertEqual(self.path.read_text(), '{bad json')

    def test_cancelled_job_cannot_be_overwritten_by_worker(self):
        write_json(self.path, [{'id': 'a', 'yt_id': 'ch', 'status': 'queued'}])
        cancel_jobs_in(self.path, job_id='a')
        with self.assertRaises(JobCancelled):
            update_job_in(self.path, 'a', {'status': 'done'})
        with self.assertRaises(JobCancelled): check_job(self.path, 'a')

    def test_live_snapshot_preserves_stop_new_jobs_and_deleted_jobs(self):
        before = [{'id': 1, 'status': 'LIVE'}, {'id': 2, 'status': 'LIVE'}]
        write_json(self.path, [{'id': 1, 'status': 'FORCE_STOP'}, {'id': 3, 'status': 'QUEUED_RENDER'}])
        after = [{'id': 1, 'status': 'LIVE', 'viewers': 2}, {'id': 2, 'status': 'FINISHED'}]
        merge_snapshot(self.path, before, after)
        self.assertEqual(read_json(self.path, []), [{'id': 1, 'status': 'FORCE_STOP'}, {'id': 3, 'status': 'QUEUED_RENDER'}])

    def test_recovery_requeues_render_but_not_uncertain_upload(self):
        write_json(self.path, [{'id': 1, 'status': 'encoding'},
            {'id': 2, 'status': 'Mengunggah ke YOUTUBE...', 'stage': 'Upload ke youtube...'},
            {'id': 3, 'status': 'Dibatalkan 🛑'}])
        recover_jobs(self.path)
        jobs = read_json(self.path, [])
        self.assertEqual(jobs[0]['status'], 'queued')
        self.assertTrue(jobs[1]['status'].startswith('Gagal'))
        self.assertEqual(jobs[2]['status'], 'Dibatalkan 🛑')

    def test_existing_status_classification(self):
        self.assertFalse(terminal({'status': 'Gagal Upload, Antre Ulang (Limit) ⏳'}))
        self.assertTrue(terminal({'status': 'Tayang! ✅'}))
        self.assertEqual(channel_id({'blueprint': {'yt_id': 'ch'}}), 'ch')


class UploadTests(unittest.TestCase):
    def setUp(self):
        from googleapiclient.http import MediaFileUpload, HttpRequest
        from httplib2 import Response
        self.MediaFileUpload, self.HttpRequest, self.Response = MediaFileUpload, HttpRequest, Response
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.video = Path(self.tmp.name) / 'video.mp4'
        self.video.write_bytes(b'a' * (256*1024 + 5))
        self.state = Path(self.tmp.name) / 'session.json'
        self.addCleanup(patch.stopall)
        patch.object(upload, 'state_path', return_value=self.state).start()
        self.calls = []
        self.offset = 0
        self.completed = False
        def request(uri, method='GET', body=None, headers=None, **kwargs):
            self.calls.append((uri, method, dict(headers or {})))
            if method == 'POST':
                # No bytes have been sent yet. The session must be saved before PUT.
                return self.Response({'status':'200','location':'https://upload.test/session'}), b''
            self.assertEqual(read_json(self.state, {})['uri'], uri)
            if headers.get('Content-Range', '').startswith('bytes */'):
                if self.completed:
                    return self.Response({'status':'200'}), b'{"id":"test-video"}'
                return self.Response({'status':'308','range':f'bytes=0-{self.offset-1}'}), b''
            data = body.read()
            self.offset += len(data)
            if self.offset == self.video.stat().st_size:
                self.completed = True
                return self.Response({'status':'200'}), b'{"id":"test-video"}'
            return self.Response({'status':'308','range':f'bytes=0-{self.offset-1}'}), b''
        self.http = Mock()
        self.http.request.side_effect = request

    def make_request(self):
        def postproc(response, content): return json.loads(content)
        return self.HttpRequest(self.http, postproc, 'https://upload.test/start', method='POST',
                                body='{}', resumable=self.MediaFileUpload(str(self.video), mimetype='video/mp4',
                                                                         chunksize=256*1024, resumable=True))

    def test_resume_uses_saved_session_and_server_offset(self):
        first = upload.DurableUpload(self.make_request(), 1, 'ch', self.video)
        status, response = first.next_chunk()
        self.assertIsNone(response)
        self.assertEqual(self.offset, 256*1024)
        second = upload.DurableUpload(self.make_request(), 1, 'ch', self.video)
        _, response = second.next_chunk()
        self.assertEqual(response['id'], 'test-video')
        self.assertEqual(sum(method=='POST' for _,method,_ in self.calls), 1)
        self.assertTrue(any(h.get('Content-Range','').startswith('bytes */') for _,_,h in self.calls))
        self.assertEqual(read_json(self.state,{})['video_id'], 'test-video')

    def test_completed_upload_returns_without_network(self):
        write_json(self.state, {'video_id':'already-uploaded'})
        durable = upload.DurableUpload(self.make_request(), 1, 'ch', self.video)
        self.assertEqual(durable.next_chunk()[1]['id'], 'already-uploaded')
        self.assertFalse(self.calls)

    def test_changed_media_cannot_reuse_session(self):
        durable = upload.DurableUpload(self.make_request(), 1, 'ch', self.video)
        durable.next_chunk()
        self.video.write_bytes(b'different')
        with self.assertRaisesRegex(RuntimeError, 'file berbeda'):
            upload.DurableUpload(self.make_request(), 1, 'ch', self.video)

    def test_completion_is_saved_before_sdk_postprocess(self):
        first = upload.DurableUpload(self.make_request(), 1, 'ch', self.video)
        first.next_chunk()
        first.request.postproc = Mock(side_effect=RuntimeError('simulated crash after acceptance'))
        with self.assertRaises(RuntimeError): first.next_chunk()
        self.assertEqual(read_json(self.state,{})['video_id'], 'test-video')


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        cls.module = app

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        self.app = self.module
        self.q = queue.PriorityQueue()
        patch.multiple(self.app, render_queue=self.q, active_tasks=[], history_tasks=[],
                       stop_flags={}, active_processes={}, database_channel=[
                           {'yt_id':'TARGET','name':'Target'}, {'yt_id':'OTHER','name':'Other'}],
                       BASE_UPLOAD=self.tmp.name, BASE_DIR=self.tmp.name).start()
        patch.object(self.app, 'save_tasks_db').start()
        for name in ('shorts.queue_worker','clipper.queue_worker'):
            module = importlib.import_module(name)
            patch.object(module, 'DB_PATH', str(Path(self.tmp.name)/(name.split('.')[0]+'.json'))).start()

    def test_priority_queue_removal_keeps_other_channel_and_accounting(self):
        self.q.put((1,0,{'id':1,'yt_id':'OTHER'}))
        self.q.put((2,1,{'id':2,'yt_id':'TARGET'}))
        self.app.remove_queued_tasks(lambda bp: bp['yt_id']=='TARGET')
        self.assertEqual(self.q.get()[2]['id'],1)
        self.q.task_done()
        self.assertEqual(self.q.unfinished_tasks,0)

    def test_stop_channel_finds_legacy_nested_channel(self):
        self.app.active_tasks.append({'id':2,'blueprint':{'yt_id':'TARGET'},'status':'Menunggu Giliran (Queue)'})
        self.q.put((1,0,{'id':1,'yt_id':'OTHER'}))
        self.q.put((2,1,{'id':2,'yt_id':'TARGET'}))
        with self.app.app.test_request_context('/api/stop_channel/TARGET', method='POST'):
            response=self.app.stop_channel('TARGET')
        self.assertEqual(response.status_code,200)
        self.assertTrue(self.app.stop_flags[2])
        self.assertFalse(self.app.active_tasks)
        self.assertEqual(self.q.get()[2]['yt_id'],'OTHER')

    def test_gallery_paths_cannot_escape_or_accept_unknown_channel(self):
        for name in ('..','/tmp','../TARGET','unknown'):
            with self.assertRaises(ValueError): self.app.channel_gallery_path(name)
        self.assertEqual(self.app.channel_gallery_path('TARGET'),str(Path(self.tmp.name)/'TARGET'))

    def test_missing_audio_does_not_kill_factory_worker(self):
        class EndLoop(BaseException): pass
        self.app.active_tasks.append({'id':1,'status':'Queue','title':'Test'})
        self.q.put((1,0,{'id':1,'yt_id':'TARGET','title':'Test'}))
        history=Mock()
        with patch.object(self.app,'acquire_resource'), patch.object(self.app,'release_resource'), \
             patch.object(self.app,'get_mixed_audios',return_value=[]), \
             patch.object(self.app,'move_to_history',history), patch.object(self.app,'notify_event'), \
             patch.object(self.app.time,'sleep',side_effect=EndLoop):
            with self.assertRaises(EndLoop): self.app.background_worker()
        self.assertIn('Gallery Audio',history.call_args.args[1])
        self.assertEqual(self.q.unfinished_tasks,0)

    def test_fb_metadata_does_not_expose_token(self):
        with patch.object(self.app,'load_fb_pages',return_value=[{'name':'Page','page_id':'1','access_token':'fake-secret'}]), \
             self.app.app.test_request_context('/api/get_fb_pages'):
            result=self.app.api_get_fb_pages().get_json()
        self.assertNotIn('access_token',result[0])


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        import app
        self.app = app
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)

    def test_new_batches_keep_existing_tasks_and_use_distinct_safe_ids(self):
        old = [{'id': i, 'blueprint': {'yt_id': 'ch', 'title': f'old-{i}'},
                'status': 'Antrean Ditunda (Cooldown YT)'} for i in range(90)]
        baseline = copy.deepcopy(old)
        patch.multiple(self.app, active_tasks=old, history_tasks=[], render_queue=queue.PriorityQueue(),
                       database_channel=[{'yt_id':'ch'}]).start()
        patch.object(self.app, 'save_tasks_db').start()
        patch.object(self.app, 'save_channels').start()
        for _ in range(2):
            with self.app.app.test_request_context('/api/batch_create', method='POST', json={
                'yt_id':'ch','count':3,'start_date':'2026-12-01T12:00',
                'generated_titles':['one','two','three']}):
                self.assertEqual(self.app.batch_create().status_code, 200)
        self.assertEqual(old[:90], baseline)
        fresh = old[90:]
        self.assertEqual(len({job['id'] for job in fresh}), 6)
        self.assertTrue(all(0 <= job['id'] < 2**53 for job in fresh))
        self.assertTrue(all(job['yt_id']=='ch' for job in fresh))

    def test_masked_token_roundtrip_preserves_existing_token(self):
        config = Path(self.tmp.name)/'config.json'
        write_json(config, {'tg_token':'fake-private-token','tg_chat_id':''})
        patch.object(self.app, 'CONFIG_FILE', str(config)).start()
        with self.app.app.test_request_context('/api/get_telegram'):
            result=self.app.get_telegram().get_json()
        self.assertEqual(result['token'],'********')
        with self.app.app.test_request_context('/api/save_telegram',method='POST',
                                              json={'token':result['token'],'chat_id':''}):
            self.app.save_telegram()
        self.assertEqual(read_json(config,{})['tg_token'],'fake-private-token')

    def test_shorts_selected_facebook_page_overrides_channel_default(self):
        import shorts.queue_worker as worker
        pages=Path(self.tmp.name)/'facebook_pages.json'
        write_json(pages,[{'page_id':'SELECTED','access_token':'fake-selected'}])
        video=Path(self.tmp.name)/'video.mp4';video.write_bytes(b'fake')
        channel={'facebook_creds':{'page_id':'WRONG','page_access_token':'fake-wrong'}}
        replies=[{'video_id':'v','upload_url':'https://upload.test'}, {'success':True}, {'success':True}]
        calls=[]
        def post(url,**kwargs):
            calls.append((url,kwargs))
            reply=Mock();reply.json.return_value=replies.pop(0);return reply
        with patch.object(worker,'CHANNELS_DB',str(Path(self.tmp.name)/'channels_db.json')), \
             patch.object(worker,'_get_channel',return_value=channel), patch('requests.post',side_effect=post):
            self.assertEqual(worker._upload_facebook(str(video),'ch',fb_page_id='SELECTED'),'v')
        self.assertIn('/SELECTED/video_reels',calls[0][0])
        self.assertEqual(calls[0][1]['data']['access_token'],'fake-selected')

    def test_factory_recovery_preserves_retry_tasks_and_cooldown(self):
        job={'id':1,'blueprint':{'id':1,'yt_id':'ch','publish_date':'2026-12-01 12:00'},
             'status':'Gagal Upload, Antre Ulang (Limit) ⏳'}
        patch.multiple(self.app, render_queue=queue.PriorityQueue(), channel_cooldowns={'ch':10**12}).start()
        self.assertFalse(terminal(job))
        self.app._reschedule_task(job)
        self.assertIn('Antrean Ditunda',job['status'])
        self.assertEqual(self.app.queue_get()['id'],1)



class LiveTests(unittest.TestCase):
    def setUp(self):
        import live.routes as routes
        import live.queue_worker as worker
        self.routes,self.worker=routes,worker
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        self.path=Path(self.tmp.name)/'live.json'
        for module in (routes,worker):patch.object(module,'DB_PATH',str(self.path)).start()

    def test_restart_restarts_live_sender_without_creating_another_broadcast(self):
        from datetime import datetime,timedelta
        from zoneinfo import ZoneInfo
        class EndLoop(BaseException): pass
        base=Path(self.tmp.name)/'base.mp4';base.write_bytes(b'fake')
        start=(datetime.now(ZoneInfo('Asia/Jakarta'))-timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M')
        write_json(self.path,[{'id':1,'status':'LIVE','start_time':start,'duration_hours':4,
                              'base_video_path':str(base),'stream_key':'fake-key','broadcast_id':'existing'}])
        patch.object(self.worker,'ffmpeg_processes',{}).start()
        patch.object(self.worker,'sync_priority_lock').start()
        popen=patch.object(self.worker.subprocess,'Popen').start()
        start_api=patch.object(self.worker,'start_youtube_broadcast').start()
        patch.object(self.worker.time,'sleep',side_effect=EndLoop).start()
        with self.assertRaises(EndLoop):self.worker.live_worker_loop()
        popen.assert_called_once()
        start_api.assert_not_called()
        self.assertEqual(read_json(self.path,[])[0]['status'],'LIVE')
        self.assertEqual(read_json(self.path,[])[0]['broadcast_id'],'existing')

    def test_stop_during_broadcast_setup_keeps_new_id_for_cleanup(self):
        before=[{'id':1,'status':'READY_TO_LOCK','broadcast_id':'','stream_key':''}]
        write_json(self.path,[{'id':1,'status':'FORCE_STOP','broadcast_id':'','stream_key':''}])
        after=[{'id':1,'status':'WAITING_TO_START','broadcast_id':'new','stream_key':'fake-key'}]
        merge_snapshot(self.path,before,after)
        result=read_json(self.path,[])[0]
        self.assertEqual(result['status'],'FORCE_STOP')
        self.assertEqual(result['broadcast_id'],'new')



if __name__ == '__main__':
    unittest.main()
