// Export only session headers from the first Zstandard frame; no reasoning bodies.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {zstdDecompressSync} from 'node:zlib';
import assert from 'node:assert/strict';
const here=path.dirname(fileURLToPath(import.meta.url));
const run=path.join(here,'live-match');
const summary=JSON.parse(fs.readFileSync(path.join(run,'summary.json'),'utf8'));
const proof={};
for(const [seat,p] of Object.entries(summary.players)){
 const source=path.join(process.env.DSH_HOME||path.join(os.homedir(),'.dsh'),'sessions','--Users-gengruilin-relaxweb--',p.session_id,'session.v4.jsonl.zstd');
 const header=JSON.parse(zstdDecompressSync(fs.readFileSync(source)).toString('utf8').split('\n')[0]);
 assert.equal(header.type,'session');
 assert.equal(header.id,p.session_id);
 assert.equal(header.agentPreset,summary.presets[seat]);
 assert.equal(header.origin,'subagent');
 proof[seat]={source,physical_header:header};
}
assert.equal(Object.keys(proof).length,8);
assert.equal(new Set(Object.values(proof).map(x=>x.physical_header.id)).size,8);
const presetCounts={};
for(const p of Object.values(proof))presetCounts[p.physical_header.agentPreset]=(presetCounts[p.physical_header.agentPreset]||0)+1;
assert.deepEqual(presetCounts,{standard:2,ptc:2,minimal:2,cordis:2});
fs.writeFileSync(path.join(run,'runtime-proof.json'),JSON.stringify(proof,null,2)+'\n');
console.log('PASS: eight unique actual subagent headers; standard / ptc / minimal / cordis each appear exactly twice.');
