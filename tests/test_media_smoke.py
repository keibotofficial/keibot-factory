"""Short local renders using synthetic media only; no uploads or Gallery files."""
import os
os.environ['KEIBOT_START_WORKERS'] = '0'
os.environ.setdefault('NUMBA_CACHE_DIR', '/tmp/keibot-numba-cache')
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.media import validate_video
from core.job_state import JobCancelled


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.source = cls.root / 'source.mp4'
        cls.audio = cls.root / 'audio.wav'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=red:s=320x180:r=30:d=1',
                        '-f','lavfi','-i','sine=frequency=440:sample_rate=22050:duration=1',
                        '-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(cls.source)],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(['ffmpeg','-v','error','-y','-i',str(cls.source),'-vn',str(cls.audio)],
                       check=True,capture_output=True,timeout=30)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def test_clipper_visualizer_real_ffmpeg(self):
        from clipper.ffmpeg_service import process_clip_pro
        target = str(self.root/'clip.mp4')
        process_clip_pro(str(self.source),target,0,0.3,'center','none',None,True,
                         vis_params={'color1':'#00e5ff','color2':'#ff0000','size':60,
                                     'num_bars':24,'height_scale':30,'pos_y':70})
        validate_video(target)
        self.assertFalse(Path(target+'.rendering.mp4').exists())

    def test_clipper_cancellation_does_not_publish_partial_output(self):
        from clipper.ffmpeg_service import process_clip_pro
        target = str(self.root/'cancelled.mp4')
        with self.assertRaises(JobCancelled):
            process_clip_pro(str(self.source),target,0,1,'center','none',None,False,
                             cancel_callback=lambda:True)
        self.assertFalse(Path(target).exists())
        self.assertFalse(Path(target+'.rendering.mp4').exists())

    def test_factory_real_render(self):
        import app
        target = str(self.root/'factory.mp4')
        with patch.object(app,'active_tasks',[]), patch.object(app,'stop_flags',{}), \
             patch.object(app,'active_processes',{}), patch.object(app,'save_tasks_db'):
            app.render_video_core(123,str(self.audio),[str(self.source)],target,0.2,
                                  {'layers':[],'use_floating_card':False})
        validate_video(target)

    def test_shorts_real_render(self):
        from shorts.render_engine import render_job
        target = str(self.root/'shorts.mp4')
        render_job(str(self.source),str(self.audio),target,effects_config=[],
                   vis_cfg={'layers':[],'use_floating_card':False},duration_range=(0.2,0.2))
        validate_video(target)

    def test_backgrounds_advance_at_end_of_each_video(self):
        import app
        bg = app.BackgroundManager([str(self.source), str(self.source)],128,72)
        try:
            for _ in range(32): bg.get_frame()
            self.assertEqual(bg.current_idx,1)
        finally: bg.close()

    def test_short_moment_detection_cleans_temporary_wav(self):
        from clipper.ai_engine import get_viral_moments
        result = get_viral_moments(str(self.source),count=1,duration_sec=40)
        self.assertTrue(result)
        self.assertLessEqual(float(result[0]['duration']),1.1)
        self.assertFalse(Path(str(self.source)+'_temp.wav').exists())


if __name__ == '__main__': unittest.main()
