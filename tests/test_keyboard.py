import termios,json
import os,pty,select,signal,subprocess,sys,time,unittest

class KeyboardTests(unittest.TestCase):
 def test_keys_drive_the_monitor_and_restore_the_terminal(self):
  master,slave=pty.openpty();saved=termios.tcgetattr(slave)
  code='''
import sys
sys.path.insert(0,'tests')
from test_monitor import FakeDaemon
import codex_multi_recovery as m
class D(FakeDaemon):
 def __init__(self):super().__init__();self.phase=-1
 def call(self,method,params):
  if method=='thread/loaded/list':self.phase=min(2,self.phase+1)
  return super().call(method,params)
daemon=D()
try:m.run(daemon,False,.1,dashboard=m.Dashboard(.1))
except KeyboardInterrupt:pass
print('COMMANDS',__import__('json').dumps(daemon.commands),flush=True)
'''
  proc=subprocess.Popen([sys.executable,'-c',code],stdin=slave,stdout=slave,stderr=slave,env=dict(os.environ,TERM='xterm-256color'))
  os.close(slave);chunks=[]
  def collect(duration):
   end=time.monotonic()+duration
   while time.monotonic()<end:
    if select.select([master],[],[],.05)[0]:
     try:data=os.read(master,65536)
     except OSError:return
     if not data:return
     chunks.append(data)
  try:
   collect(.8)
   os.write(master,b'\x1b[B');collect(.2)
   os.write(master,b'?');collect(.2)
   self.assertIn('Keyboard help',b''.join(chunks).decode(errors='replace'))
   os.write(master,b'?p');collect(.4)
   os.write(master,b'r');collect(.4)
   os.write(master,b'\x7f');collect(.4)
   os.write(master,b'q');collect(.4)
   self.assertEqual(proc.wait(timeout=4),0)
   out=b''.join(chunks).decode(errors='replace')
   for term in ['Keyboard help','Conversation paused:','Untracked:','Run summary','\x1b[?25h']:
    self.assertIn(term,out)
   self.assertNotIn('Traceback',out)
   self.assertEqual(termios.tcgetattr(master),saved)
   commands=json.loads(out.split('COMMANDS ',1)[1].splitlines()[0])
   self.assertEqual([x[0] for x in commands].count('thread/goal/set'),3)
   self.assertEqual(out[out.rfind('Run summary'):].count('Tracking:'),0)
  finally:
   if proc.poll() is None:proc.kill();proc.wait()
   os.close(master)

if __name__=='__main__':unittest.main()
