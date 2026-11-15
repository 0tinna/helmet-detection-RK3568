import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const base = 'datasets/raw/hard-hat-workers';
const selected = JSON.parse(fs.readFileSync('datasets/provenance/hard-hat-selection-file-list.json','utf8')).datasetFiles;
const names = selected.filter(f=>f.name.startsWith('annotations/') && f.name.endsWith('.xml')).map(f=>f.name);
if(names.length!==200) throw Error(`Expected exactly 200 preselected annotations, got ${names.length}`);
const tasks=names.flatMap(name=>[name,name.replace('annotations/','images/').replace('.xml','.png')]);
fs.writeFileSync('datasets/provenance/hard-hat-selection.json',JSON.stringify({source:'andrewmvd/hard-hat-detection',version:1,selection:'First 200 annotation filenames in the Kaggle API v1 listing order (pageSize=200). Fixed before inference. No performance-based selection.',annotation_names:names},null,2));
let cursor=0, done=0;
const rows=[];
async function worker(){
  while(cursor<tasks.length){
    const name=tasks[cursor++], target=path.join(base,name);
    fs.mkdirSync(path.dirname(target),{recursive:true});
    const url='https://www.kaggle.com/api/v1/datasets/download/andrewmvd/hard-hat-detection/'+encodeURIComponent(name)+'?datasetVersionNumber=1';
    let bytes;
    for(let attempt=0;attempt<10;attempt++){
      try{
        if(fs.existsSync(target)) bytes=fs.readFileSync(target);
        else{const r=await fetch(url,{signal:AbortSignal.timeout(60000)}); if(!r.ok) throw Error(`HTTP ${r.status}`); bytes=Buffer.from(await r.arrayBuffer()); fs.writeFileSync(target,bytes);}
        if(name.endsWith('.xml')&&!bytes.toString().includes('<annotation>'))throw Error('Not VOC XML');
        if(name.endsWith('.png')&&!bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])))throw Error('Not PNG');
        break;
      }catch(e){if(attempt===9)throw Error(`${name}: ${e.message}`); await new Promise(resolve=>setTimeout(resolve,Math.min(15000,1500*(attempt+1))));}
    }
    rows.push({path:target.replaceAll('\\','/'),source_path:name,url,bytes:bytes.length,sha256:crypto.createHash('sha256').update(bytes).digest('hex')});
    done++;if(done%40===0) console.log(`${done}/${tasks.length}`);
    fs.writeFileSync('datasets/provenance/hard-hat-acquisition-manifest.json',JSON.stringify({acquired_at:new Date().toISOString(),files:rows.sort((a,b)=>a.path.localeCompare(b.path))},null,2));
  }
}
await Promise.all(Array.from({length:3},worker));
console.log('COMPLETE',rows.length);
