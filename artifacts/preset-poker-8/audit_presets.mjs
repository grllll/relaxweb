// Independently inspect all Zstandard frames of the host's original child logs.
// Export only identity, completed preset-selection events, advertised tool metadata,
// and executed tool names. Never export prompts, reasoning, tool arguments or results.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {zstdDecompressSync} from 'node:zlib';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
const here=path.dirname(fileURLToPath(import.meta.url));
const run=path.join(here,'live-match');
const summary=JSON.parse(fs.readFileSync(path.join(run,'summary.json'),'utf8'));
const sha=value=>createHash('sha256').update(value).digest('hex');
function parseFrames(bytes){
 const events=[];
 while(bytes.length){
  const r=zstdDecompressSync(bytes,{info:true});
  assert(r.engine.bytesWritten>0,'Zstd parser made no progress');
  bytes=bytes.subarray(r.engine.bytesWritten);
  for(const line of r.buffer.toString('utf8').split('\n'))if(line.trim())events.push(JSON.parse(line));
 }
 return events;
}
const players=[];
for(const [seat,p] of Object.entries(summary.players)){
 const source=path.join(process.env.DSH_HOME||path.join(os.homedir(),'.dsh'),'sessions','--Users-gengruilin-relaxweb--',p.session_id,'session.v4.jsonl.zstd');
 const original=fs.readFileSync(source),events=parseFrames(original);
 const header=events[0];
 assert.equal(header.type,'session');
 assert.equal(header.id,p.session_id);
 assert.equal(header.origin,'subagent');
 assert.equal(header.delegationDepth,1);
 assert.equal(header.agentPreset,summary.presets[seat]);
 const selection=events.filter(e=>e.type==='agent-preset/selected').map(e=>({seq:e.seq,time:e.time,agentPreset:e.data.agentPreset}));
 assert.equal(selection.length,1);
 assert.equal(selection[0].agentPreset,header.agentPreset);
 const catalogs=events.filter(e=>e.type==='request/header').map(e=>({seq:e.seq,sha256:sha(JSON.stringify(e.data.header.tools)),tools:e.data.header.tools.map(t=>({name:t.name,parameterNames:Object.keys(t.parameters?.properties||{}),schemaSha256:sha(JSON.stringify(t.parameters))}))}));
 assert(catalogs.length>0,'No actual request tool catalog');
 const calls=events.filter(e=>e.type==='tool/call'||e.type==='tool/ptc-dispatch').map(e=>({seq:e.seq,type:e.type,name:e.data.name}));
 const names=catalogs[0].tools.map(t=>t.name);
 for(const c of catalogs)assert.deepEqual(c.tools.map(t=>t.name),names,'Catalog changed during child execution');
 if(header.agentPreset==='ptc'){
  assert.deepEqual(names,['run_code']);
  assert(calls.some(c=>c.type==='tool/ptc-dispatch'&&c.name==='bash'));
 }else if(header.agentPreset==='minimal'){
  // Do not mistake the shipped preset's single shell for the effective catalog:
  // this host also injected Agent Teams and the temporary delegation tool.
  assert(names.includes('bash'));
  assert(!names.includes('read')&&!names.includes('write')&&!names.includes('job_output'));
 }else{
  assert(names.includes('bash')&&names.includes('read')&&names.includes('write'));
  if(header.agentPreset==='cordis')assert(names.includes('plugin_manager')&&names.includes('cordis_inspect_list'));
  if(header.agentPreset==='standard')assert(!names.includes('plugin_manager')&&!names.includes('cordis_inspect_list'));
 }
 const descriptor=events.find(e=>e.type==='subagent/descriptor');
 players.push({seat,source,sourceSha256:sha(original),physicalHeader:header,selection,catalogs,calls,descriptor:descriptor?{seq:descriptor.seq,...descriptor.data}:null});
}
assert.equal(players.length,8);
assert.equal(new Set(players.map(p=>p.physicalHeader.id)).size,8);
assert.equal(new Set(players.map(p=>p.physicalHeader.parentSession)).size,1);
const counts={};
for(const p of players)counts[p.physicalHeader.agentPreset]=(counts[p.physicalHeader.agentPreset]||0)+1;
assert.deepEqual(counts,{standard:2,ptc:2,minimal:2,cordis:2});
const report={method:'Read original host session files, decode every Zstandard frame, whitelist identity/preset-selection/request-tool-catalog/tool-call metadata only.',limits:'Local unsigned records, not independent tamper-proof attestation. A checksum computed now detects future differences only. No full resolved plugin inventory was captured during this run.',hostExtensions:players.filter(p=>p.physicalHeader.agentPreset==='minimal').map(p=>({seat:p.seat,additionalToolNames:p.catalogs[0].tools.map(t=>t.name).filter(n=>n!=='bash'),note:'The effective minimal catalog was not an unextended single-tool environment; shared host tools were also visible.'})),counts,players};
const target=path.join(run,'preset-audit.json');
if(process.argv.includes('--check')){
 assert.deepEqual(JSON.parse(fs.readFileSync(target,'utf8')),report);
 console.log('PASS: saved audit exactly matches current original host logs.');
}else{
 fs.writeFileSync(target,JSON.stringify(report,null,2)+'\n');
 console.log('Wrote sanitized runtime audit:',target);
}
console.table(players.map(p=>({seat:p.seat,preset:p.physicalHeader.agentPreset,tools:p.catalogs[0].tools.length,actualCalls:[...new Set(p.calls.map(c=>(c.type==='tool/ptc-dispatch'?'PTC→':'')+c.name))].join(', '),creatorTools:p.catalogs[0].tools.filter(t=>t.name==='plugin_manager'||t.name.startsWith('cordis_inspect')).map(t=>t.name).join(', ')})));
console.log('PASS: eight unique child sessions; two per preset; completed selection events; distinct actual tool catalogs and dispatch styles.');
