import unittest,contextlib,io
import codex_multi_recovery as m

class Daemon:
 def __init__(self,goal=False,active=True):self.goal='active' if goal else None;self.active=active;self.commands=[]
 def call(self,method,params):
  if method=='thread/read':return {'thread':{'status':{'type':'active' if self.active else 'idle'}}}
  if method=='thread/goal/get':return {'goal':{'status':self.goal} if self.goal else None}
  if method=='thread/turns/list':return {'data':[{'id':'turn','status':'inProgress' if self.active else 'interrupted'}]}
  self.commands.append((method,params))
  if method=='thread/goal/set':self.goal=params['status']
  elif method=='turn/interrupt':self.active=False
  elif method=='turn/start':self.active=True;return {'turn':{'id':'continued'}}
  else:raise AssertionError(method)
  return {}

class PauseTests(unittest.TestCase):
 def test_pause_stops_goal_before_interrupt_and_resume_uses_correct_action(self):
  for goal in (False,True):
   with self.subTest(goal=goal),contextlib.redirect_stdout(io.StringIO()):
    d=Daemon(goal);s=m.RecoveryState();stats=m.RunStats([])
    m.pause_conversation(d,'thread',s,False)
    self.assertFalse(d.active);self.assertTrue(s.user_paused)
    self.assertEqual([x[0] for x in d.commands],['thread/goal/set','turn/interrupt'] if goal else ['turn/interrupt'])
    m.resume_conversation(d,'thread',s,stats,False)
    self.assertFalse(s.user_paused)
    self.assertEqual(d.commands[-1][0],'thread/goal/set' if goal else 'turn/start')
    self.assertEqual(len(stats.history),1)
 def test_dry_run_never_interrupts_or_changes_goal(self):
  with contextlib.redirect_stdout(io.StringIO()):
   d=Daemon(True);s=m.RecoveryState();m.pause_conversation(d,'thread',s,True)
   self.assertEqual(d.commands,[]);self.assertTrue(d.active)
 def test_manual_resume_already_active_does_not_send_another_command(self):
  with contextlib.redirect_stdout(io.StringIO()):
   d=Daemon(True);s=m.RecoveryState();s.user_paused=True;stats=m.RunStats([])
   m.resume_conversation(d,'thread',s,stats,False)
   self.assertEqual(d.commands,[]);self.assertEqual(stats.history,[])
 def test_interruption_failure_does_not_claim_success(self):
  class Reject(Daemon):
   def call(self,method,params):
    if method=='turn/interrupt':raise m.RPCRejected('cannot interrupt')
    return super().call(method,params)
  with contextlib.redirect_stdout(io.StringIO()):
   d=Reject();s=m.RecoveryState()
   with self.assertRaises(m.RPCRejected):m.pause_conversation(d,'thread',s,False)
   self.assertTrue(s.pause_error);self.assertTrue(d.active)
