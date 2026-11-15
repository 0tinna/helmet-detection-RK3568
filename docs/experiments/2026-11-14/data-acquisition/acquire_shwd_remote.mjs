import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import zlib from 'node:zlib';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';

const url='https://drive.usercontent.google.com/download?id=1qWm7rrwvjAWs1slymbrLaCf7Q-wnGLEX&export=download&confirm=t';
const base='datasets/raw/shwd';
const sha256=b=>crypto.createHash('sha256').update(b).digest('hex');
const execFileAsync=promisify(execFile);
fs.mkdirSync('datasets/raw/shwd-archive-ranges',{recursive:true});
async function range(start,end){
  const target=path.resolve(`datasets/raw/shwd-archive-ranges/${start}-${end}.bin`);
  if(!fs.existsSync(target)||fs.statSync(target).size!==end-start+1){
    await execFileAsync('powershell.exe',['-NoProfile','-File',path.resolve('docs/experiments/2026-11-14/data-acquisition/get_shwd_range.ps1'),'-Start',String(start),'-End',String(end),'-Target',target],{windowsHide:true,timeout:90000});
  }
  const bytes=fs.readFileSync(target);
  if(bytes.length!==end-start+1)throw Error(`Wrong range length ${bytes.length}`);
  return bytes;
}
// Size and Last-Modified came from a successful original Drive HTTP HEAD,
// then HTTP 206 Content-Range independently confirmed the total below.
const size=1117970537;
const tailStart=Math.max(0,size-2*1024*1024),tail=fs.existsSync('datasets/provenance/shwd-original-tail.bin')?fs.readFileSync('datasets/provenance/shwd-original-tail.bin'):await range(tailStart,size-1);
let eocd=-1;for(let i=tail.length-22;i>=0;i--){if(tail.readUInt32LE(i)===0x06054b50){eocd=i;break;}}
if(eocd<0)throw Error('No ZIP end record');
const count=tail.readUInt16LE(eocd+10),cdSize=tail.readUInt32LE(eocd+12),cdStart=tail.readUInt32LE(eocd+16);
const cd=cdStart>=tailStart?tail.subarray(cdStart-tailStart,cdStart-tailStart+cdSize):await range(cdStart,cdStart+cdSize-1);
fs.writeFileSync('datasets/provenance/shwd-original-central-directory.bin',cd);
const entries=[];
for(let i=0;i<cd.length;){
  if(cd.readUInt32LE(i)!==0x02014b50)throw Error('Bad central directory');
  const nl=cd.readUInt16LE(i+28),el=cd.readUInt16LE(i+30),cl=cd.readUInt16LE(i+32);
  entries.push({name:cd.subarray(i+46,i+46+nl).toString('utf8'),method:cd.readUInt16LE(i+10),crc32:cd.readUInt32LE(i+16),compressed_size:cd.readUInt32LE(i+20),size:cd.readUInt32LE(i+24),offset:cd.readUInt32LE(i+42)});
  i+=46+nl+el+cl;
}
const evidence={url,archive_name:'VOC2028.zip',archive_bytes:size,archive_full_sha256:null,archive_full_sha256_reason:'Only exact source ZIP members acquired using HTTP byte ranges; central directory and member hashes retained.',source_headers:{'content-length':String(size),'last-modified':'Fri, 16 Aug 2019 11:03:14 GMT','content-disposition':'attachment; filename="VOC2028.zip"','accept-ranges':'bytes','verified-content-range':`bytes ${tailStart}-${size-1}/${size}`},central_directory_sha256:sha256(cd),entry_count:count,entries};
fs.writeFileSync('datasets/provenance/shwd-original-archive-index.json',JSON.stringify(evidence,null,2));
async function getEntry(entry){
  const hdr=await range(entry.offset,entry.offset+29),nl=hdr.readUInt16LE(26),el=hdr.readUInt16LE(28);
  const start=entry.offset+30+nl+el;
  const data=await range(start,start+entry.compressed_size-1);
  const bytes=entry.method===0?data:entry.method===8?zlib.inflateRawSync(data):(()=>{throw Error(`ZIP compression ${entry.method}`);})();
  if(bytes.length!==entry.size)throw Error('Wrong uncompressed member size');
  const relative=entry.name.replace(/^VOC2028\//,'');
  if(relative.startsWith('/')||relative.includes('..'))throw Error('Unsafe ZIP member');
  const target=path.join(base,relative);fs.mkdirSync(path.dirname(target),{recursive:true});fs.writeFileSync(target,bytes);
  return {path:target.replaceAll('\\','/'),source_member:entry.name,bytes:bytes.length,sha256:sha256(bytes),zip_crc32:entry.crc32};
}
const splits=entries.filter(e=>/^VOC2028\/ImageSets\/Main\/.*\.txt$/.test(e.name));
const rows=[];for(const entry of splits)rows.push(await getEntry(entry));
const test=rows.find(r=>r.source_member.endsWith('/test.txt'));
if(!test)throw Error('Original test.txt missing');
const ids=fs.readFileSync(test.path,'utf8').trim().split(/\s+/).slice(0,200);
fs.writeFileSync('datasets/provenance/shwd-selection.json',JSON.stringify({selection:'First 200 IDs in original VOC2028/ImageSets/Main/test.txt order, fixed before inference.',original_test_ids:fs.readFileSync(test.path,'utf8').trim().split(/\s+/).length,ids},null,2));
const tasks=ids.flatMap(id=>[`VOC2028/Annotations/${id}.xml`,`VOC2028/JPEGImages/${id}.jpg`]).map(name=>{const e=entries.find(e=>e.name.toLowerCase()===name.toLowerCase());if(!e)throw Error(`Missing ${name}`);return e;});
let cursor=0,done=0;
async function worker(){while(cursor<tasks.length){const e=tasks[cursor++];let row;for(let a=0;a<4;a++){try{row=await getEntry(e);break;}catch(err){if(a===3)throw err;}}rows.push(row);done++;if(done%40===0)console.log(`${done}/${tasks.length}`);fs.writeFileSync('datasets/provenance/shwd-acquisition-manifest.json',JSON.stringify({acquired_at:new Date().toISOString(),source:url,files:rows.sort((a,b)=>a.path.localeCompare(b.path))},null,2));}}
await Promise.all(Array.from({length:8},worker));
console.log('COMPLETE',rows.length,'original archive entries',entries.length);
