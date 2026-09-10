const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const sockets=[],events={},timers=new Map();let n=0;
class WS {static OPEN=1;constructor(){this.readyState=0;sockets.push(this);}send(s){this.sent=JSON.parse(s);}close(){this.readyState=3;this.onclose?.();}}
const scope={WebSocket:WS,URL,TextEncoder,crypto:{randomUUID:()=>String(++n)},document:{currentScript:{dataset:{tapToken:'synthetic'}}},location:{href:'https://fixture.test',origin:'https://fixture.test',protocol:'https:'},addEventListener:(n,f)=>events[n]=f,setTimeout:f=>{timers.set(++n,f);return n;},clearTimeout:id=>timers.delete(id)};
scope.window=scope;scope.top=scope;vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),scope);
const b=scope.TapBridge;
function welcome(s){s.readyState=1;s.onopen();s.onmessage({data:JSON.stringify({version:'tap.bridge/v1',kind:'Welcome',page:s.sent.page,session:'fixture'})});}
(async()=>{
 assert.equal(sockets.length,1);b.connect();assert.equal(sockets.length,1);
 welcome(sockets[0]);assert(b.isReady());
 const pending=b.request('fixture',{}).catch(e=>e);b.disconnect();assert.equal(b.status().state,'paused');assert(!b.isReady());assert.equal((await pending).completion,'unknown');assert.equal(timers.size,0);
 events.pagehide();events.pageshow({persisted:true});assert.equal(sockets.length,1);
 b.connect();assert.equal(sockets.length,2);sockets[0].onmessage({data:JSON.stringify({version:'tap.bridge/v1',kind:'Welcome',page:'1',session:'stale'})});assert(!b.isReady());
 welcome(sockets[1]);assert(b.isReady());assert.equal(sockets[1].sent.kind,'Hello');
 sockets[1].close();assert.equal(b.status().state,'retrying');b.disconnect();assert.equal(timers.size,0);
 b.reconnect();welcome(sockets[2]);assert(b.isReady());assert.equal(b.status().pending,0);
 console.log('PASS connection controls, pending unknown/no replay, stale sockets, BFCache pause, retry cancellation');
})().catch(e=>{console.error(e);process.exitCode=1;});
