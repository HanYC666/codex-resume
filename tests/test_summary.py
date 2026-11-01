import io
import unittest
from unittest.mock import patch

from rich.console import Console
from rich.cells import cell_len
import codex_multi_recovery as monitor


def entry(tid, action, result, outcome='accepted'):
    return dict(thread_id=tid, action=action, result=result, outcome=outcome)


class SummaryTests(unittest.TestCase):
    def stats(self):
        stats=monitor.RunStats([
            entry('thread-abcd', 'Continue', 'success'),
            entry('thread-abcd', 'Continue', 'failed', 'rejected'),
            entry('thread-abcd', 'Continue', 'pending'),
            entry('thread-abcd', 'resume goal', 'interrupted'),
            entry('other-abcd', 'resume goal', 'unknown', 'error-or-unknown'),
            entry('other-abcd', 'resume goal', 'dry-run', 'dry-run'),
        ])
        stats.thread_names={'thread-abcd':'smallS', 'other-abcd':'smallS', 'quiet':'Quiet thread'}
        stats.errors={('thread-abcd','one'),('thread-abcd','two')}
        stats.started=100
        return stats

    def render(self, stats, width, color=False):
        out=io.StringIO()
        console=Console(file=out,width=width,force_terminal=color,color_system='standard' if color else None)
        with patch.object(monitor.time, 'monotonic', return_value=18761):
            stats.print_summary(console)
        return out.getvalue()

    def test_outcomes_reconcile_without_mutating_pending(self):
        stats=self.stats()
        self.assertEqual(stats.counts(stats.history),(5,1,1,2,1,1))
        self.assertEqual(sum(stats.counts(stats.history)[1:5]),5)
        before=[e.copy() for e in stats.history]
        output=self.render(stats,120)
        self.assertIn('5h 11m 01s',output)
        self.assertIn('Recovery attempts',output)
        self.assertIn('Goal resume',output)
        self.assertIn('Quiet thread',output)
        self.assertIn('#d-abcd',output)
        self.assertIn('#r-abcd',output)
        self.assertEqual(stats.history,before)

    def test_layouts_preserve_all_outcomes_and_fit(self):
        for width in (24,40,60,80,120,160):
            with self.subTest(width=width):
                output=self.render(self.stats(),width)
                self.assertTrue(all(cell_len(line)<=width for line in output.splitlines()))
                for label in ('Attempts','Succeeded','Failed','Unresolved','Interrupted','Dry-run'):
                    self.assertIn(label,output)
                self.assertNotIn('\x1b',output)
        self.assertIn('\x1b',self.render(self.stats(),100,True))

    def test_names_are_literal_safe_and_retained_after_untracking(self):
        stats=self.stats()
        stats.thread_names['thread-abcd']='[bold]任务\x1b[31m\nname'
        output=self.render(stats,80)
        self.assertIn('[bold]任务 name',output)
        self.assertNotIn('\x1b',output)
        self.assertIn('By conversation',output)

    def test_empty_run(self):
        output=self.render(monitor.RunStats([]),80)
        self.assertIn('No recovery commands were sent',output)
        self.assertNotIn('By conversation',output)
        self.assertEqual(output.count('Run summary'),1)


if __name__=='__main__':
    unittest.main()
