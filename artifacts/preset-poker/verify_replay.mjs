// Headless Chrome/CDP verification via private pipe, not a replacement web server.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

const here=path.dirname(fileURLToPath(import.meta.url));
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'preset-poker-chrome-'));
const chrome=spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',[
 '--headless=new','--disable-gpu','--disable-extensions','--no-first-run',
 '--no-default-browser-check','--remote-debugging-pipe',`--user-data-dir=${profile}`,'about:blank',
],{stdio:['ignore','ignore','pipe','pipe','pipe']});
let nextId=0,buffer='',errors='';
const pending=new Map(),listeners=[];
chrome.stderr.on('data',b=>{errors+=b.toString();});
chrome.stdio[4].on('data',b=>{
 buffer+=b.toString();
 let end;
 while((end=buffer.indexOf('\0'))!==-1){
  const text=buffer.slice(0,end);buffer=buffer.slice(end+1);
  if(!text)continue;
  const msg=JSON.parse(text);
  if(msg.id){const item=pending.get(msg.id);if(item){pending.delete(msg.id);msg.error?item.reject(new Error(JSON.stringify(msg.error))):item.resolve(msg.result);}}
  else for(const listener of [...listeners])listener(msg);
 }
});
const command=(method,params={},sessionId)=>new Promise((resolve,reject)=>{
 const id=++nextId;pending.set(id,{resolve,reject});
 chrome.stdio[3].write(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})})+'\0');
});
const event=(method,sessionId)=>new Promise(resolve=>{
 const listener=msg=>{if(msg.method===method&&msg.sessionId===sessionId){listeners.splice(listeners.indexOf(listener),1);resolve(msg.params);}};
 listeners.push(listener);
});
const timeout=setTimeout(()=>{console.error('Chrome verification timed out',errors.slice(-1500));chrome.kill();process.exitCode=1;},25000);
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
  if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));
  return r.result.value;
 };
 assert.deepEqual(await evaluate(`({step:document.getElementById('step').textContent,players:document.querySelectorAll('#replay-players .player').length,cards:document.querySelectorAll('#replay-players .card').length,rows:[...document.querySelectorAll('section[id^="hand-"] table')].filter(t=>t.querySelector('th').textContent==='#').reduce((n,t)=>n+t.querySelectorAll('tbody tr').length,0)})`),{step:'1 / 81',players:4,cards:8,rows:63});
 assert.equal(await evaluate(`document.getElementById('next').click();document.getElementById('step').textContent`),'2 / 81');
 assert.equal(await evaluate(`document.getElementById('prev').click();document.getElementById('step').textContent`),'1 / 81');
 assert.equal(await evaluate(`document.getElementById('slider').value=80;document.getElementById('slider').dispatchEvent(new Event('input'));document.getElementById('replay-pot').textContent`),'底池 0');
 assert.deepEqual(await evaluate(`[...document.querySelectorAll('#replay-players .stack')].map(x=>Number(x.textContent))`),[98,80,320,302]);
 assert.equal(await evaluate(`document.getElementById('play').click();document.getElementById('step').textContent`),'1 / 81');
 assert.equal(await evaluate(`document.getElementById('play').click();document.getElementById('play').textContent`),'自动播放');
 await evaluate(`document.getElementById('slider').value=28;document.getElementById('slider').dispatchEvent(new Event('input'));document.getElementById('replay').scrollIntoView()`);
 const screenshot=await command('Page.captureScreenshot',{format:'png',captureBeyondViewport:false},sessionId);
 fs.writeFileSync(path.join(here,'回放预览.png'),Buffer.from(screenshot.data,'base64'));
 console.log('PASS: Chrome loaded offline replay; 81 frames, 63 action rows, four players, previous/next/scrub/play/pause, final stacks.');
 await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true},sessionId);
 assert.equal(await evaluate(`document.documentElement.scrollWidth<=document.documentElement.clientWidth`),true);
 console.log('PASS: 390px mobile viewport has no page-level horizontal overflow.');
}catch(error){console.error(error);process.exitCode=1;}
finally{
 clearTimeout(timeout);
 chrome.kill();
 await new Promise(resolve=>chrome.once('exit',resolve));
 fs.rmSync(profile,{recursive:true,force:true});
}
