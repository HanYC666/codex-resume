import contextlib
import io
import os
import pty
import select
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import codex_multi_recovery as monitor
from rich.console import Console
from rich.cells import cell_len


class FakeDaemon:
    def __init__(self):
        self.phase = 0
        self.commands = []

    def turns(self):
        if self.phase == 0:
            return [{'id': 'initial', 'status': 'inProgress'}]
        failed = lambda tid: {'id': tid, 'status': 'failed', 'error': {'message': 'Unexpected status 503'}}
        if self.phase == 1:
            return [failed('initial')]
        if self.phase < 5:
            return [failed('recovery-1'), failed('initial')]
        return [{'id': 'recovery-4', 'status': 'completed'}, failed('recovery-1')]

    def call(self, method, params):
        if method == 'thread/loaded/list':
            return {'data': ['prompt', 'goal']}
        if method == 'thread/read':
            kind = 'active' if self.phase == 0 else 'idle' if self.phase == 5 else 'systemError'
            return {'thread': {'name': params['threadId'], 'status': {'type': kind}}}
        if method == 'thread/turns/list':
            return {'data': self.turns()}
        if method == 'thread/goal/get':
            return {'goal': {'status': 'blocked'} if params['threadId'] == 'goal' else None}
        if method in ('turn/start', 'thread/goal/set'):
            self.commands.append((method, self.phase))
            return {'turn': {'id': 'recovery-%d' % self.phase}}
        raise AssertionError(method)


class MonitorTests(unittest.TestCase):
    def exercise_run(self, dry_run=False):
        daemon = FakeDaemon()
        clock = [0]
        dashboard = monitor.Dashboard(5, dry_run, enabled=False)
        stats = monitor.RunStats([])
        moments = iter([5, 10, 904, 905, 910])

        def advance(_):
            try:
                clock[0] = next(moments)
                daemon.phase += 1
            except StopIteration:
                raise KeyboardInterrupt

        output = io.StringIO()
        with patch.object(monitor.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(monitor.time, 'sleep', side_effect=advance), contextlib.redirect_stdout(output):
            with self.assertRaises(KeyboardInterrupt):
                monitor.run(daemon, dry_run, 5, stats, dashboard)
        return daemon, stats, dashboard, output.getvalue()

    def test_two_threads_recover_backoff_and_complete(self):
        daemon, stats, dashboard, output = self.exercise_run()
        self.assertEqual(daemon.commands, [('turn/start', 1), ('thread/goal/set', 1),
                                           ('turn/start', 4), ('thread/goal/set', 4)])
        self.assertEqual(len(stats.errors), 4)
        self.assertEqual([e['result'] for e in stats.history], ['failed', 'failed', 'success', 'success'])
        self.assertEqual([e['slow'] for e in stats.history], [False, False, True, True])
        self.assertEqual(output.count('Run summary'), 1)
        self.assertEqual(output.count('failed turn detected'), 4)
        for row in dashboard.rows.values():
            self.assertEqual((row['attempts'], row['successes'], row['failures']), (2, 1, 1))
            self.assertFalse(row['slow'])
        self.assertNotIn('\x1b', output)

    def test_dry_run_has_plans_without_live_attempts(self):
        daemon, stats, dashboard, output = self.exercise_run(True)
        self.assertEqual(daemon.commands, [])
        self.assertTrue(all(e['outcome'] == 'dry-run' for e in stats.history))
        for row in dashboard.rows.values():
            self.assertEqual(row['attempts'], 0)
            self.assertGreater(row['plans'], 0)

    def test_panel_counts_and_countdown_at_multiple_widths(self):
        state = monitor.RecoveryState()
        state.slow, state.next_retry = True, 1000
        stats = monitor.RunStats([{'thread_id': 'a', 'action': 'Continue',
                                   'outcome': 'accepted', 'result': 'pending'}])
        dashboard = monitor.Dashboard(5, enabled=False)
        dashboard.update_thread('a', 'smallS', {'type': 'systemError'},
                                [{'id': 'failed', 'status': 'failed', 'error': {'message': '503'}}], state, stats)
        dashboard.update_thread('b', 'panda', {'type': 'active'}, [], monitor.RecoveryState(), stats)
        for width in (40, 80, 140):
            sink = io.StringIO()
            dashboard.console = Console(file=sink, width=width, height=30, color_system=None)
            with patch.object(monitor.time, 'monotonic', return_value=105):
                dashboard.console.print(dashboard.render())
            result = sink.getvalue()
            self.assertIn('14:55', result)
            self.assertIn('smallS', result)
            self.assertIn('Backoff', result)
            self.assertLessEqual(max(map(len, result.splitlines())), width)

    def test_event_timestamps_and_terminal_text_sanitizing(self):
        sink = io.StringIO()
        monitor.event('test\x1b[2J\n', 'sk-' + 'a' * 32, file=sink)
        value = sink.getvalue()
        self.assertRegex(value, r'^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[+-]\d{4}\]')
        self.assertNotIn('\x1b', value)
        self.assertNotIn('a' * 32, value)
        self.assertEqual(value.count('\n'), 1)

    def test_all_threads_rotate_into_view_without_hiding_the_footer(self):
        dashboard=monitor.Dashboard(5,enabled=False)
        dashboard.started=100
        for n in range(40):
            dashboard.update_thread('same-prefix-%04d' % n, 'task-%02d' % n,
                                    {'type':'active'},[],monitor.RecoveryState(),monitor.RunStats([]))
        sink=io.StringIO()
        dashboard.console=Console(file=sink,width=80,height=24,color_system=None)
        frames=[]
        for page in range(8):
            with patch.object(monitor.time,'monotonic',return_value=100+page*8):
                dashboard.console.print(dashboard.render())
            frame=sink.getvalue(); sink.seek(0); sink.truncate()
            self.assertLessEqual(len(frame.splitlines()),20)
            self.assertIn('rotates 8s',frame)
            self.assertIn('Ctrl+C summary',frame)
            frames.append(frame)
        for n in range(40): self.assertIn('task-%02d' % n, ''.join(frames))

    def test_long_unicode_names_unique_ids_and_small_terminal_layouts(self):
        dashboard=monitor.Dashboard(5,enabled=False)
        for tid in ('same-prefix-aaaa1dead','same-prefix-bbbb1dead'):
            dashboard.update_thread(tid,'研究筆記 '*30,{'type':'active'},[],monitor.RecoveryState(),monitor.RunStats([]))
        for width,height in ((24,8),(40,16),(60,20),(80,24),(110,24),(150,32)):
            sink=io.StringIO()
            dashboard.console=Console(file=sink,width=width,height=height,color_system=None)
            dashboard.console.print(dashboard.render())
            frame=sink.getvalue()
            self.assertLessEqual(len(frame.splitlines()),height-2)
            self.assertLessEqual(max(map(cell_len,frame.splitlines())),width)
            self.assertIn('Working',frame)
            if width>=60:
                self.assertIn('#a1dead',frame)
                self.assertIn('#b1dead',frame)

    def test_dry_run_and_empty_screen_are_explicit_without_colour(self):
        dashboard=monitor.Dashboard(5,dry_run=True,enabled=False)
        sink=io.StringIO(); dashboard.console=Console(file=sink,width=80,height=24,color_system=None)
        dashboard.console.print(dashboard.render())
        frame=sink.getvalue()
        self.assertIn('DRY RUN',frame)
        self.assertIn('nothing sent',frame)
        self.assertIn('Waiting for a working thread',frame)
        self.assertNotIn('\x1b',frame)

    def test_live_terminal_refresh_logs_and_ctrl_c_cleanup(self):
        master, slave = pty.openpty()
        child = '''
import sys
sys.path.insert(0, 'tests')
from test_monitor import FakeDaemon
import codex_multi_recovery as m
class Daemon(FakeDaemon):
    def __init__(self):
        super().__init__()
        self.phase = -1
    def call(self, method, params):
        if method == 'thread/loaded/list': self.phase = min(2, self.phase + 1)
        return super().call(method, params)
try:
    m.run(Daemon(), False, 0.1, dashboard=m.Dashboard(0.1))
except KeyboardInterrupt:
    m.event('Stopped.')
'''
        proc = subprocess.Popen([sys.executable, '-c', child], stdout=slave, stderr=slave,
                                env=dict(os.environ, TERM='xterm-256color'))
        os.close(slave)
        chunks = []

        def collect(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        return
                    if not data: return
                    chunks.append(data)

        try:
            collect(2.2)
            proc.send_signal(signal.SIGINT)
            collect(2)
            self.assertEqual(proc.wait(timeout=3), 0)
            output = b''.join(chunks).decode(errors='replace')
            self.assertIn('Codex recovery', output)
            self.assertIn('15m', output)
            self.assertIn('failed turn detected', output)
            self.assertGreater(output.count('Codex recovery'), 1)
            self.assertEqual(output.count('Run summary'), 1)
            self.assertIn('\x1b[?25h', output)  # cursor restored by Live.stop()
            self.assertNotIn('Traceback', output)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)


if __name__ == '__main__':
    unittest.main()
