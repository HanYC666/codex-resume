import unittest
import codex_multi_recovery as m
class ActivityTests(unittest.TestCase):
 def test_only_current_turn_last_meaningful_item_counts(self):
  good={'id':'new','status':'inProgress','items':[{'type':'userMessage'}, {'type':'commandExecution','status':'completed','exitCode':0}]}
  self.assertTrue(m.productive_current_turn(good))
  for item in ({'type':'commandExecution','status':'failed'},{'type':'agentMessage','text':'{"error":{"message":"quota"}}'}, {'type':'reasoning'}):
   bad=dict(good,items=[item]);self.assertFalse(m.productive_current_turn(bad))
  self.assertFalse(m.productive_current_turn(dict(good,error={'message':'503'})))
  self.assertFalse(m.productive_current_turn(dict(good,status='failed')))
  self.assertFalse(m.productive_current_turn(dict(good,items=good['items']+[{'type':'commandExecution','status':'failed'}])))
 def test_success_between_polls_when_failure_fell_out_of_page(self):
  state=m.RecoveryState();state.failure({'id':'old','startedAt':100,'error':{'message':'503'}},0)
  self.assertTrue(state.completed_since_failure([{'id':'current','status':'inProgress','startedAt':400},{'id':'missed','status':'completed','startedAt':300}]))
  self.assertFalse(state.completed_since_failure([{'id':'historic','status':'completed','startedAt':90}]))
 def test_progress_does_not_falsely_count_a_completed_recovery(self):
  history=[{'thread_id':'t','turn_id':'old','recovery_turn_id':'new','outcome':'accepted','result':'pending'}]
  stats=m.RunStats(history);stats.observe('t',[{'id':'new','status':'inProgress','items':[{'type':'agentMessage','text':'Working now'}]}])
  self.assertEqual(history[0]['result'],'pending')

class PollProgressTests(unittest.TestCase):
 def test_backoff_clears_on_next_poll_with_current_tool_activity(self):
  import contextlib,io
  from unittest.mock import patch
  from test_monitor import FakeDaemon
  class Daemon(FakeDaemon):
   def call(self,method,params):
    if method=='thread/read' and self.phase>=3:return {'thread':{'name':params['threadId'],'status':{'type':'active'}}}
    return super().call(method,params)
   def turns(self):
    if self.phase>=3:return [{'id':'productive','status':'inProgress','items':[{'type':'commandExecution','status':'completed','exitCode':0}]},{'id':'recovery-1','status':'failed','error':{'message':'Unexpected status 503'}}]
    return super().turns()
  d=Daemon();clock=[0];stats=m.RunStats([]);dash=m.Dashboard(1,enabled=False)
  def advance(_):
   d.phase+=1;clock[0]=d.phase
   if d.phase>4:raise KeyboardInterrupt
  with patch.object(m.time,'monotonic',side_effect=lambda:clock[0]),patch.object(m.time,'sleep',side_effect=advance),contextlib.redirect_stdout(io.StringIO()):
   with self.assertRaises(KeyboardInterrupt):m.run(d,False,1,stats,dash)
  self.assertTrue(all(not row['slow'] for row in dash.rows.values()))
  self.assertEqual(len(d.commands),2)
