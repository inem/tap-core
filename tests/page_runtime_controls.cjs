const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const sockets=[],events={},timers=new Map();let n=0,replaced=null,fetchOk=true;
let currentPlan='a'.repeat(64),currentPacks=[{id:'fixture.page',version:'1.2.3',features:[{id:'archive',label:'Archive',value:'Local',folder:'data/readers/fixture.page'}]}];
class WS {static OPEN=1;constructor(){this.readyState=0;this.messages=[];sockets.push(this);}send(s){this.sent=JSON.parse(s);this.messages.push(this.sent);}close(){this.readyState=3;this.onclose?.();}}
const location={href:'https://fixture.test/path?q=1',origin:'https://fixture.test',protocol:'https:',replace:value=>{replaced=value;}};
const scope={WebSocket:WS,URL,TextEncoder,crypto:{randomUUID:()=>String(++n)},fetch:async()=>({ok:fetchOk,json:async()=>({version:'tap.page-plan/v1',revision:currentPlan,scripts:[],packs:currentPacks,access:'current',application:'reload'})}),document:{currentScript:{dataset:{tapToken:'synthetic',tapPlan:currentPlan,tapWs:'true'}}},location,addEventListener:(name,f)=>events[name]=f,setTimeout:(f,delay)=>{timers.set(++n,{f,delay});return n;},clearTimeout:id=>timers.delete(id)};
scope.window=scope;scope.top=scope;vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),scope);
const b=scope.TapBridge;
function welcome(s){s.readyState=1;s.onopen();s.onmessage({data:JSON.stringify({version:'tap.bridge/v1',kind:'Welcome',page:s.sent.page,session:'fixture'})});}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
async function runDelay(delay){const item=[...timers].find(([,v])=>v.delay===delay);assert(item);timers.delete(item[0]);await item[1].f();await flush();}
(async()=>{
 await runDelay(0);assert.equal(b.status().packs[0].id,'fixture.page');assert.equal(b.status().packs[0].features[0].folder,'data/readers/fixture.page');
 assert.equal(sockets.length,1);b.connect();assert.equal(sockets.length,1);
 welcome(sockets[0]);await flush();assert(b.isReady());
 let finish;
 const dispose=b.expose('fixture.inspect',args=>new Promise(resolve=>{finish=()=>resolve({seen:args});}));
 sockets[0].onmessage({data:JSON.stringify({version:'tap.bridge/v1',kind:'Command',session:'fixture',id:'command-1',operation:'fixture.inspect',args:{value:7}})});
 await flush();assert.equal(b.status().activity.inbound,1);assert.equal(b.status().activity.sequence,1);
 finish();await flush();assert.equal(b.status().activity.inbound,0);
 assert.deepEqual(sockets[0].sent.value.seen,{value:7});assert.equal(sockets[0].sent.kind,'CommandResult');dispose();
 const pending=b.request('fixture',{}).catch(e=>e);b.disconnect();assert.equal(b.status().state,'paused');assert(!b.isReady());assert.equal((await pending).completion,'unknown');assert.equal([...timers.values()].filter(v=>v.delay===2000).length,1);
 events.pagehide();assert.equal(timers.size,0);events.pageshow({persisted:true});assert.equal(sockets.length,1);
 b.connect();assert.equal(sockets.length,2);sockets[0].onmessage({data:JSON.stringify({version:'tap.bridge/v1',kind:'Welcome',page:'1',session:'stale'})});assert(!b.isReady());
 welcome(sockets[1]);await flush();assert(b.isReady());assert.equal(sockets[1].sent.kind,'Hello');
 sockets[1].close();assert.equal(b.status().state,'retrying');b.disconnect();assert.equal(timers.size,1);
 b.reconnect();welcome(sockets[2]);await flush();assert(b.isReady());assert.equal(b.status().pending,0);
 currentPlan='b'.repeat(64);fetchOk=false;
 sockets[2].onmessage({data:JSON.stringify({version:'tap.bridge/v1',kind:'PlanChanged',session:'fixture',revision:'c'.repeat(64)})});
 await flush();assert.equal(b.status().plan_state,'unavailable');assert.equal(replaced,null);
 fetchOk=true;await runDelay(5000);
 assert.equal(replaced,'https://fixture.test/path?q=1&tap-ui='+currentPlan.slice(0,12));
 console.log('PASS connection controls, pending unknown/no replay, stale sockets, BFCache pause, retry cancellation, plan refresh');
})().catch(e=>{console.error(e);process.exitCode=1;});
