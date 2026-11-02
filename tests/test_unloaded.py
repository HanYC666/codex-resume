import contextlib
import io
import unittest
from unittest.mock import patch

import codex_multi_recovery as m


class UnloadingDaemon:
    def __init__(self, goal=True):
        self.phase=0
        self.loaded={'a','b'}
        self.goal=goal
        self.commands=[]
        self.restores=[]
        self.turns={tid:'initial' for tid in self.loaded}

    def call(self, method, params):
        tid=params.get('threadId')
        if method=='thread/loaded/list':
            if self.phase in (2,3): self.loaded.clear()
            return {'data':sorted(self.loaded)}
        if method=='thread/read':
            return {'thread':{'name':tid,'model':'gpt-6-sol','reasoningEffort':'medium',
                              'status':{'type':'notLoaded' if tid not in self.loaded else
                                                'active' if self.phase==0 else 'systemError'}}}
        if method=='thread/resume':
            self.restores.append((tid,self.phase,dict(params)))
            if self.phase==3: raise m.RPCRejected('session temporarily unavailable')
            self.loaded.add(tid)
            return {'thread':{'id':tid}}
        if method=='thread/turns/list':
            if tid not in self.loaded: raise m.RPCRejected('thread not loaded')
            return {'data':[{'id':self.turns[tid], 'status':'inProgress' if self.phase==0 else 'failed',
                             'error':{'message':'compaction_upstream_unavailable'}}]}
        if method=='thread/goal/get':
            return {'goal':{'status':'blocked'} if self.goal else None}
        if method in ('thread/goal/set','turn/start'):
            self.commands.append((tid,self.phase,method))
            self.turns[tid]='retry-'+str(self.phase)
            return {'turn':{'id':self.turns[tid]}}
        raise AssertionError(method)


class UnloadedTests(unittest.TestCase):
    def exercise(self, goal, dry_run=False):
        daemon=UnloadingDaemon(goal)
        stats=m.RunStats([]);dashboard=m.Dashboard(1,dry_run,enabled=False)
        clock=[0];times=[0,1,2,3,17,900,901,1801,2701,3601]
        snapshots=[]
        def advance(_):
            snapshots.append((daemon.phase,{tid:row.copy() for tid,row in dashboard.rows.items()}))
            daemon.phase+=1
            if daemon.phase>=len(times):raise KeyboardInterrupt
            clock[0]=times[daemon.phase]
        output=io.StringIO()
        with patch.object(m.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(m.time,'sleep',side_effect=advance),contextlib.redirect_stdout(output):
            with self.assertRaises(KeyboardInterrupt):m.run(daemon,dry_run,1,stats,dashboard)
        return daemon,stats,snapshots,output.getvalue()

    def test_both_unloaded_during_backoff_keep_retrying_same_error(self):
        for goal in (False,True):
            with self.subTest(goal=goal):
                daemon,stats,snapshots,output=self.exercise(goal)
                self.assertEqual([phase for tid,phase,_ in daemon.commands if tid=='a'],[1,6,7,8,9])
                self.assertEqual([phase for tid,phase,_ in daemon.commands if tid=='b'],[1,6,7,8,9])
                self.assertEqual(output.count('Tracking:'),2)  # Never reenrolled with a new baseline.
                self.assertNotIn('automatic recovery stopped',output)
                self.assertIn('tracking retained',output)
                by_phase=dict(snapshots)
                for phase,rows in snapshots:self.assertEqual(set(rows),{'a','b'})
                self.assertTrue(all(row['unavailable'] for row in by_phase[3].values()))
                self.assertTrue(all(row['deadline']==901 for row in by_phase[3].values()))
                self.assertTrue(all(row['deadline']==901 for row in by_phase[4].values()))
                self.assertTrue(all(row['attempts']==5 for row in by_phase[9].values()))
                self.assertTrue(all(set(params)=={'threadId','excludeTurns'} for _,_,params in daemon.restores))
                self.assertEqual(len(stats.history),10)

    def test_dry_run_does_not_reload_sessions(self):
        daemon,stats,snapshots,output=self.exercise(True,True)
        self.assertEqual(daemon.restores,[])
        self.assertEqual(daemon.commands,[])
        self.assertEqual(set(snapshots[-1][1]),{'a','b'})

    def test_untrack_stays_untracked_and_pause_survives_unloading(self):
        for command in ('untrack','pause'):
            with self.subTest(command=command):
                daemon=UnloadingDaemon();stats=m.RunStats([]);dash=m.Dashboard(1,enabled=False)
                controls=[];real_keyboard=m.Keyboard
                def keyboard_factory(dashboard):
                    keyboard=real_keyboard(dashboard)
                    keyboard.start=lambda:None
                    controls.append(keyboard)
                    return keyboard
                clock=[0]
                def advance(_):
                    daemon.phase+=1;clock[0]=daemon.phase*20
                    if daemon.phase==1:controls[0].commands.put((command,'a'))
                    if daemon.phase>=6:raise KeyboardInterrupt
                with patch.object(m,'Keyboard',side_effect=keyboard_factory), \
                     patch.object(m.time,'monotonic',side_effect=lambda:clock[0]), \
                     patch.object(m.time,'sleep',side_effect=advance),contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(KeyboardInterrupt):m.run(daemon,False,1,stats,dash)
                self.assertFalse(any(tid=='a' for tid,_,_ in daemon.restores))
                if command=='untrack':self.assertNotIn('a',dash.rows)
                else:
                    self.assertIn('a',dash.rows)
                    self.assertTrue(dash.rows['a']['user_paused'])
                    self.assertEqual(m.Dashboard.presentation(dash.rows['a'])[0],'Paused')

    def test_reconnecting_is_visible(self):
        row={'flags':(), 'kind':'notLoaded','unavailable':True,'title':'a'}
        self.assertEqual(m.Dashboard.presentation(row)[0],'Reconnecting')
        row['user_paused']=True
        self.assertEqual(m.Dashboard.presentation(row)[0],'Paused')
        row['user_paused']=False;row['stopped']='repeated configuration_update after fallback'
        self.assertEqual(m.Dashboard.presentation(row)[0],'Stopped')

if __name__=='__main__':unittest.main()
