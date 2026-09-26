// Real Chrome/CDP smoke test over a local file; no HTTP server or GUI replacement.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const summary=JSON.parse(fs.readFileSync(path.join(here,'live-match/summary.json'),'utf8'));
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'poker-eight-chrome-'));
const chrome=spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',[
 '--headless=new','--disable-gpu','--disable-extensions','--no-first-run','--no-default-browser-check',
 '--remote-debugging-pipe',`--user-data-dir=${profile}`,'about:blank'],{stdio:['ignore','ignore','pipe','pipe','pipe']});
let id=0,buffer='',errors='';
const pending=new Map(),listeners=[];
chrome.stderr.on('data',b=>errors+=b.toString());
chrome.stdio[4].on('data',b=>{
 buffer+=b.toString();let end;
 while((end=buffer.indexOf('\0'))!==-1){
  const text=buffer.slice(0,end);buffer=buffer.slice(end+1);if(!text)continue;
  const m=JSON.parse(text);
  if(m.id){const p=pending.get(m.id);if(p){pending.delete(m.id);m.error?p.reject(new Error(JSON.stringify(m.error))):p.resolve(m.result);}}
  else for(const f of [...listeners])f(m);
 }
});
const command=(method,params={},sessionId)=>new Promise((resolve,reject)=>{
 const n=++id;pending.set(n,{resolve,reject});chrome.stdio[3].write(JSON.stringify({id:n,method,params,...(sessionId?{sessionId}:{})})+'\0');
});
const event=(method,sid)=>new Promise(resolve=>{
 const f=m=>{if(m.method===method&&m.sessionId===sid){listeners.splice(listeners.indexOf(f),1);resolve(m.params);}};listeners.push(f);
});
const timer=setTimeout(()=>{console.error('Chrome verification timeout',errors.slice(-1200));chrome.kill();process.exitCode=1;},25000);
try{
 const {targetId}=await command('Target.createTarget',{url:'about:blank'});
 const {sessionId}=await command('Target.attachToTarget',{targetId,flatten:true});
 await command('Page.enable',{},sessionId);
 await command('Emulation.setDeviceMetricsOverride',{width:1400,height:1100,deviceScaleFactor:1,mobile:false},sessionId);
 const loaded=event('Page.loadEventFired',sessionId);
 await command('Page.navigate',{url:pathToFileURL(path.join(here,'完整对局回放.html')).href},sessionId);
 await loaded;
 const evaluate=async expression=>{
  const r=await command('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},sessionId);
  if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;
 };
 const frameCount=await evaluate(`JSON.parse(document.getElementById('replay-data').textContent).frames.length`);
 assert.equal(frameCount,155);
 assert.deepEqual(await evaluate(`({step:document.getElementById('step').textContent,players:document.querySelectorAll('#replay-players .player').length,cards:document.querySelectorAll('#replay-players .card').length,rows:[...document.querySelectorAll('section[id^="hand-"] table')].filter(t=>t.querySelector('th').textContent==='#').reduce((n,t)=>n+t.querySelectorAll('tbody tr').length,0)})`),{step:'1 / 155',players:8,cards:16,rows:122});
 assert.equal(await evaluate(`document.getElementById('next').click();document.getElementById('step').textContent`),'2 / 155');
 assert.equal(await evaluate(`document.getElementById('prev').click();document.getElementById('step').textContent`),'1 / 155');
 assert.equal(await evaluate(`document.getElementById('slider').value=154;document.getElementById('slider').dispatchEvent(new Event('input'));document.getElementById('replay-pot').textContent`),'底池 0');
 assert.deepEqual(await evaluate(`[...document.querySelectorAll('#replay-players .stack')].map(x=>Number(x.textContent))`),summary.seats.map(p=>summary.stacks[p]));
 assert.equal(await evaluate(`document.getElementById('play').click();document.getElementById('step').textContent`),'1 / 155');
 assert.equal(await evaluate(`new Promise(resolve=>setTimeout(()=>{document.getElementById('play').click();resolve(Number(document.getElementById('step').textContent.split(' / ')[0])>=2);},1150))`),true);
 assert.equal(await evaluate(`document.getElementById('play').textContent`),'自动播放');
 for(let hand=1;hand<=8;hand++){
  assert.equal(await evaluate(`document.getElementById('hand-select').value=${hand};document.getElementById('hand-select').dispatchEvent(new Event('change'));document.getElementById('replay-stage').textContent`),`第 ${hand} 手 · 翻牌前`);
  assert.equal(await evaluate(`document.querySelectorAll('#replay-board .card').length`),0);
 }
 await evaluate(`document.getElementById('slider').value=35;document.getElementById('slider').dispatchEvent(new Event('input'));document.getElementById('replay').scrollIntoView()`);
 const shot=await command('Page.captureScreenshot',{format:'png',captureBeyondViewport:false},sessionId);
 fs.writeFileSync(path.join(here,'回放预览.png'),Buffer.from(shot.data,'base64'));
 console.log('PASS: 8 real seats, 16 hole cards, 122 action rows, 155 frames; navigation, autoplay, pause, every hand jump, final stacks.');
 await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true},sessionId);
 assert.equal(await evaluate(`document.documentElement.scrollWidth<=document.documentElement.clientWidth`),true);
 console.log('PASS: 390px mobile screen has no page-level horizontal overflow.');
}catch(e){console.error(e);process.exitCode=1;}
finally{
 clearTimeout(timer);
 if(chrome.exitCode===null){chrome.kill();await new Promise(resolve=>chrome.once('exit',resolve));}
 fs.rmSync(profile,{recursive:true,force:true});
}
