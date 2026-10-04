// Volatile authenticated capture feed. No provider parsing or page publication.
import {randomUUID} from 'node:crypto';
const schema = 'tap.capture.live/v1';
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
export const MAX_LIVE_BYTES = 128 * 1024;
export function validateObservation(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('invalid_observation');
  const fields = new Set(['schema','record_id','flow_id','sequence','ts','transport','direction','phase','metadata','data','reason','message','opcode','dropped_before']);
  if (Object.keys(value).some(key => !fields.has(key)) || value.schema !== schema
      || typeof value.record_id !== 'string' || typeof value.flow_id !== 'string'
      || !uuid.test(value.record_id) || !uuid.test(value.flow_id)
      || !Number.isSafeInteger(value.sequence) || value.sequence < 1
      || !Number.isFinite(value.ts) || value.ts < 0
      || !['http','websocket'].includes(value.transport)
      || !['request','response'].includes(value.direction)
      || !['start','data','end','error'].includes(value.phase)) throw Error('invalid_observation');
  if (value.dropped_before !== undefined && (!Number.isSafeInteger(value.dropped_before) || value.dropped_before < 1)) throw Error('invalid_loss');
  if (value.phase === 'data') {
    if (typeof value.data !== 'string' || value.data.length > 65536 || value.data.length % 4
        || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value.data)) throw Error('invalid_data');
  } else if (value.data !== undefined) throw Error('unexpected_data');
  if (value.phase === 'error' ? !['transport_error','observation_limit','body_unavailable'].includes(value.reason) : value.reason !== undefined) throw Error('invalid_reason');
  if ((value.message === undefined) !== (value.opcode === undefined)
      || value.message !== undefined && (value.transport !== 'websocket' || !Number.isSafeInteger(value.message)
          || value.message < 1 || !['data','end'].includes(value.phase) || ![1,2].includes(value.opcode))
      || value.transport === 'websocket' && value.phase === 'data' && value.message === undefined) throw Error('invalid_frame');
  if (value.transport === 'http' && value.phase === 'start' && value.metadata === undefined) throw Error('missing_metadata');
  if (value.metadata !== undefined) {
    const m = value.metadata;
    if (value.phase !== 'start' || value.transport !== 'http' || !m || typeof m !== 'object' || Array.isArray(m)
        || Buffer.byteLength(JSON.stringify(m)) > 16384) throw Error('invalid_metadata');
    const request = value.direction === 'request';
    const keys = request ? ['method','origin','path','path_truncated','headers','headers_omitted','content_type','content_encoding','body_available']
      : ['status','content_type','content_encoding'];
    if (Object.keys(m).some(k => !keys.includes(k)) || keys.some(k => !Object.hasOwn(m,k))) throw Error('invalid_metadata');
    if (typeof m.content_type !== 'string' || m.content_type.length > 1024
        || typeof m.content_encoding !== 'string' || m.content_encoding.length > 256) throw Error('invalid_metadata');
    if (request) {
      let url;
      try { url = new URL(m.origin); } catch { throw Error('invalid_origin'); }
      if (!['http:','https:'].includes(url.protocol) || url.origin !== m.origin
          || typeof m.method !== 'string' || m.method.length > 32
          || typeof m.path !== 'string' || !m.path.startsWith('/') || m.path.length > 8192
          || m.path.includes('?') || m.path.includes('#')
          || typeof m.path_truncated !== 'boolean' || typeof m.body_available !== 'boolean' || typeof m.headers_omitted !== 'boolean'
          || !m.headers || typeof m.headers !== 'object' || Array.isArray(m.headers)
          || Object.keys(m.headers).length > 16) throw Error('invalid_metadata');
      for (const [name,text] of Object.entries(m.headers)) {
        if (!/^[a-z0-9-]{1,64}$/.test(name) || /authorization|cookie|token|api-key/.test(name)
            || typeof text !== 'string') throw Error('credential_or_invalid_header');
      }
    } else if (!Number.isInteger(m.status) || m.status < 100 || m.status > 999) throw Error('invalid_status');
  }
  return value;
}

export class LiveCaptureFeed {
  constructor({retained=128, subscribers=8, queue=64} = {}) {
    this.epoch = randomUUID(); this.sequence = 0;
    this.records = []; this.clients = new Set(); this.limit = retained;
    this.clientLimit = subscribers; this.queue = queue;
  }
  cursor(sequence=this.sequence) { return `${this.epoch}:${sequence}`; }
  event(name, data, sequence) {
    return new TextEncoder().encode(`${sequence === undefined ? '' : 'id: '+this.cursor(sequence)+'\n'}event: ${name}\ndata: ${JSON.stringify(data)}\n\n`);
  }
  publish(value) {
    validateObservation(value);
    const text = JSON.stringify(value);
    if (Buffer.byteLength(text) > MAX_LIVE_BYTES) throw Error('input_limit');
    const duplicate = this.records.find(r => r.value.record_id === value.record_id);
    if (duplicate) {
      if (duplicate.text !== text) throw Error('record_conflict');
      return duplicate.sequence;
    }
    const record = {value, text, sequence:++this.sequence};
    this.records.push(record);
    if (this.records.length > this.limit) this.records.shift();
    for (const client of this.clients) this.deliver(client, record);
    return record.sequence;
  }
  deliver(client, record) {
    try {
      if (client.controller.desiredSize < (record.value.dropped_before ? 2 : 1)) {
        client.controller.enqueue(this.event('gap', {schema,reason:'subscriber_overflow'}));
        client.controller.close(); this.clients.delete(client); return;
      }
      if (record.value.dropped_before) client.controller.enqueue(this.event('gap',
        {schema,reason:'publisher_loss',dropped:record.value.dropped_before}));
      client.controller.enqueue(this.event('observation', record.value, record.sequence));
    } catch { this.clients.delete(client); }
  }
  subscribe(after) {
    if (this.clients.size >= this.clientLimit) return new Response('Busy',{status:503});
    let client;
    const stream = new ReadableStream({
      start: controller => {
        client = {controller}; this.clients.add(client);
        controller.enqueue(this.event('ready', {schema,cursor:this.cursor(),volatile:true}));
        if (!after) return;
        const match = /^([a-f0-9-]{36}):(\d{1,16})$/.exec(after);
        const sequence = match ? Number(match[2]) : NaN;
        let reason = !match || !Number.isSafeInteger(sequence) ? 'invalid_cursor' : match[1] !== this.epoch ? 'epoch_changed'
          : sequence > this.sequence ? 'future_cursor'
          : sequence < (this.records[0]?.sequence ?? this.sequence + 1) - 1 ? 'retention_gap' : null;
        if (reason) { controller.enqueue(this.event('gap', {schema,reason})); return; }
        // Replay is bounded too; a slow client must bootstrap instead of accumulating.
        const replay = this.records.filter(r => r.sequence > sequence);
        if (replay.length >= this.queue - 1) {
          controller.enqueue(this.event('gap', {schema,reason:'replay_overflow'})); return;
        }
        for (const record of replay) this.deliver(client, record);
      },
      cancel: () => { this.clients.delete(client); },
    }, {highWaterMark:this.queue});
    return new Response(stream,{headers:{'Content-Type':'text/event-stream','Cache-Control':'no-store'}});
  }
  health() { return {schema,cursor:this.cursor(),retained:this.records.length,subscribers:this.clients.size,volatile:true}; }
}
