import contextlib
import io
import unittest
from unittest.mock import patch

import codex_multi_recovery as m


def provider_error(index=318):
    return {'message': 'Unexpected status 400: '+__import__('json').dumps({'error': {
        'code':'invalid_parameter','message':"Invalid input type 'configuration_update' at index %d" % index,
        'type':'invalid_request_error','details':{'field':'input.%d.type' % index,'code':'invalid_input_type'}}})}


class Daemon:
    def __init__(self, source='gpt-6.1-sol', effort='high', goal=False, repeat=True, confirm=True, reject=False):
        self.model=source; self.effort=effort; self.goal='blocked' if goal else None
        self.phase=0; self.commands=[]; self.repeat=repeat; self.confirm=confirm; self.reject=reject

    def call(self, method, params):
        if method=='thread/loaded/list': return {'data':['thread']}
        if method=='thread/read':
            kind='active' if self.phase==0 else 'idle' if self.phase==3 and not self.repeat else 'systemError'
            return {'thread':{'name':'fallback-test','model':self.model,'reasoningEffort':self.effort,'status':{'type':kind}}}
        if method=='thread/goal/get': return {'goal':{'status':self.goal} if self.goal else None}
        if method=='thread/turns/list':
            if self.phase==0: turns=[{'id':'initial','status':'inProgress'}]
            elif self.phase<3: turns=[{'id':'initial','status':'failed','error':provider_error()}]
            elif self.repeat: turns=[{'id':'recovery','status':'failed','error':provider_error(319)},
                                     {'id':'initial','status':'failed','error':provider_error()}]
            elif self.phase==3: turns=[{'id':'recovery','status':'completed'}, {'id':'initial','status':'failed','error':provider_error()}]
            else: turns=[{'id':'later','status':'failed','error':provider_error(420)}, {'id':'recovery','status':'completed'}]
            return {'data':turns[:params.get('limit',10)]}
        if method=='thread/settings/update':
            self.commands.append((method,dict(params)))
            if self.reject: raise m.RPCRejected('unsupported effort')
            if self.confirm: self.model=params['model']; self.effort=params['effort']
            return {}
        if method=='thread/goal/set':
            self.commands.append((method,dict(params))); self.goal=params['status']; return {}
        if method=='turn/start':
            self.commands.append((method,dict(params))); return {'turn':{'id':'recovery'}}
        if method=='model/list': return {'data':[{'id':self.model,'defaultReasoningEffort':'xhigh'}]}
        raise AssertionError(method)


class FallbackTests(unittest.TestCase):
    def run_monitor(self, daemon, dry=False):
        clock=[0]
        stats=m.RunStats([]); dash=m.Dashboard(5,dry,enabled=False)
        def advance(_):
            daemon.phase+=1; clock[0]=daemon.phase*1000
            if daemon.phase>6: raise KeyboardInterrupt
        out=io.StringIO()
        with patch.object(m.time,'monotonic',side_effect=lambda:clock[0]), \
                patch.object(m.time,'sleep',side_effect=advance),contextlib.redirect_stdout(out):
            with self.assertRaises(KeyboardInterrupt): m.run(daemon,dry,5,stats,dash)
        return stats,dash,out.getvalue()

    def test_switch_preserves_effort_and_recurrence_stops_prompt_or_goal(self):
        for source in m.MODEL_FALLBACKS:
            for effort in ('low','high','xhigh','max'):
                for goal in (False,True):
                    with self.subTest(source=source,effort=effort,goal=goal):
                        d=Daemon(source,effort,goal); stats,dash,out=self.run_monitor(d)
                        methods=[method for method,_ in d.commands]
                        self.assertEqual(methods,['thread/settings/update','thread/goal/set','thread/goal/set'] if goal
                                         else ['thread/settings/update','turn/start'])
                        self.assertEqual(d.commands[0][1],{'threadId':'thread','model':'gpt-6-sol','effort':effort})
                        self.assertEqual(len(stats.history),1)
                        self.assertEqual(stats.history[0]['result'],'failed')
                        self.assertEqual(len(stats.errors),2)
                        self.assertTrue(dash.rows['thread']['stopped'])
                        self.assertEqual(dash.rows['thread']['model'],'gpt-6-sol')
                        self.assertEqual(dash.rows['thread']['effort'],effort)
                        if goal: self.assertEqual(d.goal,'paused')

    def test_fallback_latch_survives_a_successful_turn(self):
        d=Daemon(repeat=False);stats,dash,out=self.run_monitor(d)
        self.assertEqual([x[0] for x in d.commands],['thread/settings/update','turn/start'])
        self.assertEqual(stats.history[0]['result'],'success')
        self.assertTrue(dash.rows['thread']['stopped'])

    def test_dry_run_does_not_change_models_or_submit_or_pause(self):
        d=Daemon(goal=True);stats,dash,out=self.run_monitor(d,True)
        self.assertEqual(d.commands,[])
        self.assertEqual(d.model,'gpt-6.1-sol')
        self.assertIn('DRY RUN:',out)
        self.assertFalse(dash.rows['thread']['switching'])
        self.assertTrue(all(e['outcome']=='dry-run' for e in stats.history))

    def test_no_recovery_until_switch_confirmed_and_no_lowered_effort(self):
        for kwargs in ({'confirm':False},{'reject':True}):
            d=Daemon(goal=True,**kwargs);stats,dash,out=self.run_monitor(d)
            self.assertEqual(len([x for x in d.commands if x[0]=='thread/settings/update']),1)
            self.assertEqual(stats.history,[])
            self.assertTrue(dash.rows['thread']['stopped'])
            self.assertEqual(d.effort,'high')

    def test_default_effort_is_resolved_before_switch(self):
        d=Daemon(effort=None);stats,dash,out=self.run_monitor(d)
        self.assertEqual(d.commands[0][1]['effort'],'xhigh')

    def test_unmapped_model_stops_instead_of_guessing_another_model(self):
        d=Daemon(source='gpt-6-sol',goal=True);stats,dash,out=self.run_monitor(d)
        self.assertEqual([x[0] for x in d.commands],['thread/goal/set'])
        self.assertEqual(d.goal,'paused')
        self.assertEqual(stats.history,[])

    def test_active_goal_and_prompt_metadata_refresh_on_the_panel(self):
        from rich.console import Console
        dash=m.Dashboard(5,enabled=False);state=m.RecoveryState()
        state.model='gpt-6.1-sol';state.effort='xhigh';state.goal_status='active'
        dash.update_thread('thread','Test',{'type':'active'},[],state,m.RunStats([]))
        sink=io.StringIO();dash.console=Console(file=sink,width=150,height=24,color_system=None)
        dash.console.print(dash.render())
        for term in ('gpt-6.1-sol','xhigh','Goal active'):self.assertIn(term,sink.getvalue())
        state.model='gpt-6-sol';state.effort='low';state.goal_status=None
        dash.update_thread('thread','Test',{'type':'active'},[],state,m.RunStats([]))
        sink.seek(0);sink.truncate();dash.console.print(dash.render())
        for term in ('gpt-6-sol','low','Prompt'):self.assertIn(term,sink.getvalue())
        self.assertNotIn('gpt-6.1-sol',sink.getvalue())

    def test_trigger_is_narrow_and_normalizes_changing_indices(self):
        self.assertTrue(m.configuration_update_error(provider_error()))
        self.assertEqual(m.error_identity(provider_error(318)),m.error_identity(provider_error(420)))
        self.assertFalse(m.configuration_update_error({'message':"Invalid input type 'other' at index 318"}))
        self.assertFalse(m.configuration_update_error({'message':'configuration_update failed with 503'}))

    def test_panel_shows_model_effort_mode_and_stopped_state(self):
        from rich.console import Console
        d=Daemon(goal=True);stats,dash,out=self.run_monitor(d)
        sink=io.StringIO();dash.console=Console(file=sink,width=150,height=32,color_system=None)
        dash.console.print(dash.render());text=sink.getvalue()
        for term in ('gpt-6-sol','high','Goal paused','Stopped','Auto off'):self.assertIn(term,text)


if __name__=='__main__':unittest.main()
